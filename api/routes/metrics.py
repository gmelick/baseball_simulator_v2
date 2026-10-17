"""
api/routes/metrics.py
=====================
Phase-5 Prometheus metrics endpoint (SIM-374 — monitoring, FINAL ticket).

Route:

    GET /metrics            (ops; excluded from the OpenAPI schema)
        Returns the application's metrics in the Prometheus **text exposition
        format** (Content-Type ``text/plain; version=0.0.4``) so a Prometheus
        server (deploy/monitoring/prometheus.yml) can scrape them and Grafana
        (deploy/monitoring/grafana/) can dashboard them.

DEPENDENCY POLICY — optional ``prometheus_client``
--------------------------------------------------
``prometheus_client`` is NOT a hard dependency of this project (it is not in
requirements.txt). This module therefore tries to import it and, when present,
uses the real collector registry (``Counter`` / ``Histogram`` / ``Gauge`` +
``generate_latest`` + ``CONTENT_TYPE_LATEST``). When it is absent, it falls back
to a tiny hand-rolled text-exposition writer (:func:`_render_fallback`) that
emits the IDENTICAL metric names / ``# HELP`` / ``# TYPE`` lines. The endpoint
and its unit test therefore work in either environment with no code change and
the scrape config / dashboards bind to the same series either way.

WHAT THE METRICS MEAN — live vs. placeholder-for-instrumentation
----------------------------------------------------------------
This is the monitoring *surface* (SIM-374). A few series carry real data the
moment the app is up; the rest are registered with the correct names / help /
type so the Prometheus scrape and the Grafana dashboards bind immediately, and
the corresponding request handlers can populate them as instrumentation lands
without touching this file's contract:

    baseball_sim_app_info{version=...}      gauge      LIVE  — always 1; carries
                                                              the build version as
                                                              a label.
    baseball_sim_metrics_scrapes_total      counter    LIVE  — incremented on
                                                              every /metrics scrape.
    baseball_sim_requests_total             counter    LIVE-where-recorded —
                                                              mirrors the request
                                                              counter on app.state
                                                              (see record_request);
                                                              0 until a handler bumps it.
    baseball_sim_latency_seconds            histogram  POPULATED-FROM-STATE — the
                                                              games router can record
                                                              the last sim wall-clock
                                                              via record_sim_latency();
                                                              exposed as a summary-style
                                                              gauge set (last/count) in
                                                              the fallback path.
    baseball_sim_api_p95_seconds            gauge      Live API p95 latency, fed by the
                                                              SIM-410 LatencyMiddleware
                                                              (rolling 200-request window).
    baseball_sim_pipeline_freshness_seconds gauge      LIVE-WHERE-WIRED — seconds
                                                              since the last live re-sim
                                                              signal (app.state.last_resim_signal),
                                                              else -1 (no data yet).
    baseball_sim_finals_age_hours           gauge      SIM-519 Part D — hours since the
                                                              newest final game of the
                                                              current season loaded; -1 when
                                                              unknown. A background task reads
                                                              it at most every 10 minutes.
    baseball_sim_backup_age_hours{store=...} gauge     The scheduled backups (SIM-525) —
                                                              hours since the newest complete
                                                              backup set of each store
                                                              (postgres, duckdb, bundle); -1
                                                              when the store has none. Read
                                                              from BACKUPS_ROOT at most every
                                                              10 minutes.
    baseball_sim_offsite_backup_age_hours{store=...} gauge
                                                              The off-site copy (SIM-568) —
                                                              the age in hours of each store's
                                                              newest backup set that is
                                                              complete off-site, counted from
                                                              the set's creation (not from the
                                                              copy); -1 when none. One daemon
                                                              thread reads OFFSITE_ROOT at most
                                                              every 10 minutes, one read at a
                                                              time. The scrape never reads the
                                                              off-site folder: it ages the last
                                                              reading to the scrape time.

The values are read from ``request.app.state`` where available (so no DB / Redis
connection is required — this module is fully import-safe and the endpoint never
5xxs on a cold app) and otherwise reported as not-yet-populated.

Owner: QA / DevOps (SIM-374).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from api.auth import require_auth

# ---------------------------------------------------------------------------
# Optional prometheus_client — used when importable, else a hand-rolled writer.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised whichever branch the environment provides
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure → fallback path
    _PROM_AVAILABLE = False
    # The canonical Prometheus text-exposition content type (version 0.0.4).
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


# SIM-443c: /metrics requires a login; Prometheus sends an Authorization: Bearer key.
router = APIRouter(tags=["ops"], dependencies=[Depends(require_auth)])

# App build version surfaced as a label on the info gauge. Kept in sync with
# api/main.py's FastAPI(version=...) intent; overridable via env for releases.
_APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-phase7")

#: The off-site copy (SIM-568): the help text of the off-site backup-age gauge.
#: The age counts from the set's creation, because the off-site manifest is a
#: verbatim copy of the local one.
OFFSITE_BACKUP_AGE_HELP = (
    "Age in hours of the store's newest backup set that is complete off-site; -1 when none."
)


# ---------------------------------------------------------------------------
# prometheus_client registry (only built when the dependency is present).
#
# A fresh CollectorRegistry (not the global default) keeps this module's series
# self-contained and avoids duplicate-timeseries errors if the app is created
# more than once in a single process (e.g. across tests).
# ---------------------------------------------------------------------------
if _PROM_AVAILABLE:
    _REGISTRY = CollectorRegistry()

    _INFO = Gauge(
        "baseball_sim_app_info",
        "Static application info; value is always 1, build version is a label.",
        ["version"],
        registry=_REGISTRY,
    )
    _INFO.labels(version=_APP_VERSION).set(1)

    _SCRAPES = Counter(
        "baseball_sim_metrics_scrapes_total",
        "Total number of times the /metrics endpoint has been scraped.",
        registry=_REGISTRY,
    )

    _REQUESTS = Gauge(
        "baseball_sim_requests_total",
        "Total HTTP requests served (mirrored from app.state.request_count).",
        registry=_REGISTRY,
    )

    _SIM_LATENCY_LAST = Gauge(
        "baseball_sim_latency_seconds",
        "Most recent Monte-Carlo simulation wall-clock latency, in seconds.",
        registry=_REGISTRY,
    )

    _API_P95 = Gauge(
        "baseball_sim_api_p95_seconds",
        "API p95 response latency in seconds (fed by request-timing middleware).",
        registry=_REGISTRY,
    )

    _PIPELINE_FRESHNESS = Gauge(
        "baseball_sim_pipeline_freshness_seconds",
        "Seconds since the last live re-sim signal; -1 when no signal yet.",
        registry=_REGISTRY,
    )

    _FINALS_AGE = Gauge(
        "baseball_sim_finals_age_hours",
        "Hours since the newest final game of the current season loaded; -1 when unknown.",
        registry=_REGISTRY,
    )

    _BACKUP_AGE = Gauge(
        "baseball_sim_backup_age_hours",
        "Hours since the newest complete backup set of the store; -1 when none.",
        ["store"],
        registry=_REGISTRY,
    )

    # The off-site copy (SIM-568).
    _OFFSITE_BACKUP_AGE = Gauge(
        "baseball_sim_offsite_backup_age_hours",
        OFFSITE_BACKUP_AGE_HELP,
        ["store"],
        registry=_REGISTRY,
    )


# ---------------------------------------------------------------------------
# Public instrumentation helpers — import-safe no-ops when prometheus is absent.
#
# Other routers (notably api/routes/games.py) can call these to feed real data
# into the metrics without importing prometheus_client themselves. They are also
# the single seam the fallback path reads from app.state.
# ---------------------------------------------------------------------------


def record_sim_latency(app_state: Any, seconds: float) -> None:
    """Record the wall-clock latency of the most recent simulation run.

    Stores the value on ``app.state`` (read by the fallback renderer) and, when
    prometheus_client is present, also updates the gauge. Safe to call from any
    handler — never raises.
    """
    try:
        app_state.sim_latency_last_seconds = float(seconds)
    except Exception:  # noqa: BLE001
        return
    if _PROM_AVAILABLE:
        _SIM_LATENCY_LAST.set(float(seconds))


def record_request(app_state: Any) -> None:
    """Increment the served-request counter on ``app.state``.

    Best-effort; never raises. The /metrics handler mirrors the resulting count
    into the prometheus gauge at scrape time.
    """
    try:
        app_state.request_count = int(getattr(app_state, "request_count", 0)) + 1
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Value collection — pull current readings off app.state (no DB / Redis).
# ---------------------------------------------------------------------------


def _pipeline_freshness_seconds(app_state: Any) -> float:
    """Seconds since the last live re-sim signal, or -1 if none observed yet.

    Reads ``app.state.last_resim_signal_ts`` (a unix timestamp the pipeline hook
    may set) when present; otherwise reports -1 (no data) so the gauge exists for
    the dashboard without fabricating a freshness reading.
    """
    # SIM-519 Part C: the live service's heartbeat, when the background read has one.
    hb = getattr(app_state, "live_heartbeat_ts", None)
    ts = hb if hb is not None else getattr(app_state, "last_resim_signal_ts", None)
    if ts is None:
        return -1.0
    try:
        return max(0.0, time.time() - float(ts))
    except Exception:  # noqa: BLE001
        return -1.0


#: SIM-519 Part D: the newest final game's date is read at most this often.
FINALS_AGE_REFRESH_S = 600

_SQL_NEWEST_FINAL = """
    SELECT MAX(game_date) AS newest FROM raw.games
     WHERE status = 'Final' AND season = EXTRACT(YEAR FROM NOW())::int
"""


async def _refresh_newest_final(app_state: Any) -> None:
    """Read the newest final game's date into ``app.state`` (best effort)."""
    pool = getattr(app_state, "pg_pool", None)
    if pool is None:
        return
    try:
        rows = await pool.fetch(_SQL_NEWEST_FINAL)
        newest = rows[0]["newest"] if rows else None
        app_state.finals_newest_date = newest
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return


async def _refresh_live_heartbeat(app_state: Any) -> None:
    """Read the live service's heartbeat stamp into ``app.state`` (best effort)."""
    redis = getattr(app_state, "redis_client", None)
    if redis is None:
        return
    try:
        from pipeline.live.broadcast import HEARTBEAT_KEY

        raw = await redis.get(HEARTBEAT_KEY)
        app_state.live_heartbeat_ts = float(raw) if raw is not None else None
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return


#: SIM-519 Part C: the heartbeat stamp is read at most this often.
HEARTBEAT_REFRESH_S = 15


def _schedule_finals_refresh(app_state: Any) -> None:
    """Start the refresh in the background when the stored date is stale.

    The scrape itself never waits on the database: it reads the last stored
    value, and the next scrape sees the new one.
    """
    now = time.time()
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if now - float(getattr(app_state, "finals_checked_at", 0.0) or 0.0) >= FINALS_AGE_REFRESH_S:
        app_state.finals_checked_at = now
        # Keep a reference until the task ends.
        app_state.finals_refresh_task = loop.create_task(_refresh_newest_final(app_state))
    if now - float(getattr(app_state, "heartbeat_checked_at", 0.0) or 0.0) >= HEARTBEAT_REFRESH_S:
        app_state.heartbeat_checked_at = now
        app_state.heartbeat_refresh_task = loop.create_task(_refresh_live_heartbeat(app_state))
    # The off-site copy (SIM-568): a read of the off-site folder can stall on a
    # dehydrated OneDrive file, so it runs on a daemon thread, never in the scrape.
    # One read runs at a time: while a read stalls, no scrape starts another.
    offsite_due = (
        now - float(getattr(app_state, "offsite_checked_at", 0.0) or 0.0) >= OFFSITE_AGE_REFRESH_S
    )
    if offsite_due and not _offsite_read_in_flight(app_state):
        app_state.offsite_checked_at = now
        app_state.offsite_refresh_task = loop.create_task(_refresh_offsite_ages(app_state))


def _finals_age_hours(app_state: Any) -> float:
    """Hours from the start (UTC) of the newest final game's date to now; -1 if unknown."""
    newest = getattr(app_state, "finals_newest_date", None)
    if newest is None:
        return -1.0
    try:
        start = datetime(newest.year, newest.month, newest.day, tzinfo=UTC).timestamp()
        return max(0.0, (time.time() - start) / 3600.0)
    except Exception:  # noqa: BLE001
        return -1.0


#: The scheduled backups (SIM-525): the backup root is read at most this often.
BACKUP_AGE_REFRESH_S = 600

#: The three backup stores, in the order the gauge lists them.
BACKUP_STORES = ("postgres", "duckdb", "bundle")


def _no_backup_ages() -> dict[str, float]:
    """Return -1 (no complete set) for every backup store."""
    return dict.fromkeys(BACKUP_STORES, -1.0)


def _read_ages(env_name: str, default_root: str) -> dict[str, float]:
    """Read the age in hours of the newest complete set of each backup store.

    The root is the value of the environment variable ``env_name``, else
    ``default_root``. A missing root, a missing reader module or any read error
    gives -1 for every store. The reader never raises.
    """
    try:
        from pipeline.backup.status import age_hours, newest_complete_sets
    except ImportError:
        return _no_backup_ages()
    ages = _no_backup_ages()
    try:
        root = Path(os.environ.get(env_name, default_root))
        if not root.is_dir():
            return ages
        sets = newest_complete_sets(root)
        now = datetime.now(UTC)
        for store in BACKUP_STORES:
            ages[store] = float(age_hours(sets.get(store), now))
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return _no_backup_ages()
    return ages


def _read_backup_ages() -> dict[str, float]:
    """Read the local backup ages from ``BACKUPS_ROOT`` (default ``/backups``)."""
    return _read_ages("BACKUPS_ROOT", "/backups")


#: The off-site copy (SIM-568): the off-site folder is read at most this often.
OFFSITE_AGE_REFRESH_S = 600

#: The off-site copy (SIM-568): the name of the daemon thread that reads the folder.
OFFSITE_READ_THREAD_NAME = "offsite-backup-age"

#: The off-site copy (SIM-568): one reading of the folder and the time of the read.
_OffsiteReading = tuple[dict[str, float], float]


def _read_offsite_ages() -> dict[str, float]:
    """Read the off-site copy's ages from ``OFFSITE_ROOT`` (default ``/offsite``) (SIM-568)."""
    return _read_ages("OFFSITE_ROOT", "/offsite")


def _read_offsite_ages_now() -> _OffsiteReading:
    """Read the off-site ages, then take the time of the read (SIM-568).

    The scrape adds the hours since this time to each age.
    """
    ages = _read_offsite_ages()
    return ages, time.time()


def _offsite_read_in_flight(app_state: Any) -> bool:
    """Return True while the last off-site read has not finished (SIM-568)."""
    task = getattr(app_state, "offsite_refresh_task", None)
    if task is None:
        return False
    try:
        return not task.done()
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return False


async def _read_offsite_on_daemon_thread() -> _OffsiteReading:
    """Run the off-site read on a daemon thread of its own (SIM-568).

    The read never takes a thread from the loop's default executor, which
    ``/simulate``, the what-if and the other routes share. A read that stalls on
    OneDrive therefore holds only its own thread. A daemon thread also never
    holds back the exit of the process, so the restart on a new bundle still
    exits. The thread hands its result to the loop with
    ``call_soon_threadsafe``. A result that arrives after its task was
    cancelled, or after the loop closed, is dropped.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_OffsiteReading] = loop.create_future()

    def deliver(outcome: _OffsiteReading | BaseException) -> None:
        if future.done():  # The task was cancelled while the read ran.
            return
        if isinstance(outcome, BaseException):
            future.set_exception(outcome)
        else:
            future.set_result(outcome)

    def work() -> None:
        outcome: _OffsiteReading | BaseException = RuntimeError(
            "the off-site read ended without a result"
        )
        try:
            outcome = _read_offsite_ages_now()
        except Exception as exc:  # noqa: BLE001 -- the task receives the error
            outcome = exc
        finally:
            try:
                loop.call_soon_threadsafe(deliver, outcome)
            except RuntimeError:  # The loop closed while the read ran.
                pass

    threading.Thread(target=work, name=OFFSITE_READ_THREAD_NAME, daemon=True).start()
    return await future


async def _refresh_offsite_ages(app_state: Any) -> None:
    """Read the off-site ages on a daemon thread into ``app.state`` (SIM-568).

    The task stores the reading in ``app.state.offsite_backup_ages`` and the
    time of the read in ``app.state.offsite_backup_read_at``. It never raises:
    a metric must never fail the app.
    """
    try:
        ages, read_at = await _read_offsite_on_daemon_thread()
        app_state.offsite_backup_ages = ages
        app_state.offsite_backup_read_at = read_at
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return


def _offsite_ages(app_state: Any) -> dict[str, float]:
    """Return the stored off-site ages, aged to now; -1 for every store before the first read.

    The scrape reads only ``app.state.offsite_backup_ages`` and
    ``app.state.offsite_backup_read_at``; the background read of
    :func:`_schedule_finals_refresh` fills both (the off-site copy, SIM-568).
    Each age grows by the hours since the read, so a read that stalls never
    freezes the gauge at a fresh value. A store with no set reads -1 and does
    not grow. With no read time, the stored ages render as they are.
    """
    stored = getattr(app_state, "offsite_backup_ages", None)
    ages = _no_backup_ages()
    if not isinstance(stored, dict):
        return ages
    since_read_h = 0.0
    read_at = getattr(app_state, "offsite_backup_read_at", None)
    if read_at is not None:
        try:
            since_read_h = max(0.0, time.time() - float(read_at)) / 3600.0
        except (TypeError, ValueError):
            since_read_h = 0.0
    for store in BACKUP_STORES:
        try:
            age = float(stored.get(store, -1.0))
        except (TypeError, ValueError):
            age = -1.0
        ages[store] = -1.0 if age < 0 else age + since_read_h
    return ages


def _backup_ages(app_state: Any) -> dict[str, float]:
    """Return the backup ages, read again only when the cached reading is stale.

    The reading and its check time live on ``app.state`` (``backup_ages`` and
    ``backup_ages_checked_at``), so one scrape in ten minutes reads the disk.
    """
    now = time.time()
    cached = getattr(app_state, "backup_ages", None)
    checked = float(getattr(app_state, "backup_ages_checked_at", 0.0) or 0.0)
    if isinstance(cached, dict) and now - checked < BACKUP_AGE_REFRESH_S:
        return dict(cached)
    ages = _read_backup_ages()
    try:
        app_state.backup_ages = ages
        app_state.backup_ages_checked_at = now
    except Exception:  # noqa: BLE001
        pass
    return dict(ages)


def _collect(app_state: Any) -> dict[str, float]:
    """Snapshot the current scalar readings used by both render paths."""
    return {
        "requests_total": float(getattr(app_state, "request_count", 0) or 0),
        "sim_latency_last": float(getattr(app_state, "sim_latency_last_seconds", 0.0) or 0.0),
        "api_p95": float(getattr(app_state, "api_p95_seconds", 0.0) or 0.0),
        "pipeline_freshness": _pipeline_freshness_seconds(app_state),
        "finals_age": _finals_age_hours(app_state),
    }


# ---------------------------------------------------------------------------
# Hand-rolled text-exposition writer (fallback when prometheus_client absent).
# Emits the SAME metric names / HELP / TYPE lines as the prometheus path so the
# scrape config + dashboards bind identically.
# ---------------------------------------------------------------------------


def _metric_block(name: str, help_text: str, mtype: str, lines: list[str]) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} {mtype}\n" + "".join(
        f"{ln}\n" for ln in lines
    )


def _render_fallback(app_state: Any, scrape_count: int) -> str:
    """Render the metrics in Prometheus text exposition format by hand."""
    vals = _collect(app_state)
    blocks = [
        _metric_block(
            "baseball_sim_app_info",
            "Static application info; value is always 1, build version is a label.",
            "gauge",
            [f'baseball_sim_app_info{{version="{_APP_VERSION}"}} 1'],
        ),
        _metric_block(
            "baseball_sim_metrics_scrapes_total",
            "Total number of times the /metrics endpoint has been scraped.",
            "counter",
            [f"baseball_sim_metrics_scrapes_total {scrape_count}"],
        ),
        _metric_block(
            "baseball_sim_requests_total",
            "Total HTTP requests served (mirrored from app.state.request_count).",
            "gauge",
            [f"baseball_sim_requests_total {vals['requests_total']:g}"],
        ),
        _metric_block(
            "baseball_sim_latency_seconds",
            "Most recent Monte-Carlo simulation wall-clock latency, in seconds.",
            "gauge",
            [f"baseball_sim_latency_seconds {vals['sim_latency_last']:g}"],
        ),
        _metric_block(
            "baseball_sim_api_p95_seconds",
            "API p95 response latency in seconds (fed by request-timing middleware).",
            "gauge",
            [f"baseball_sim_api_p95_seconds {vals['api_p95']:g}"],
        ),
        _metric_block(
            "baseball_sim_pipeline_freshness_seconds",
            "Seconds since the last live re-sim signal; -1 when no signal yet.",
            "gauge",
            [f"baseball_sim_pipeline_freshness_seconds {vals['pipeline_freshness']:g}"],
        ),
        _metric_block(
            "baseball_sim_finals_age_hours",
            "Hours since the newest final game of the current season loaded; -1 when unknown.",
            "gauge",
            [f"baseball_sim_finals_age_hours {vals['finals_age']:g}"],
        ),
        _metric_block(
            "baseball_sim_backup_age_hours",
            "Hours since the newest complete backup set of the store; -1 when none.",
            "gauge",
            [
                f'baseball_sim_backup_age_hours{{store="{store}"}} {age:g}'
                for store, age in _backup_ages(app_state).items()
            ],
        ),
        # The off-site copy (SIM-568).
        _metric_block(
            "baseball_sim_offsite_backup_age_hours",
            OFFSITE_BACKUP_AGE_HELP,
            "gauge",
            [
                f'baseball_sim_offsite_backup_age_hours{{store="{store}"}} {age:g}'
                for store, age in _offsite_ages(app_state).items()
            ],
        ),
    ]
    return "".join(blocks)


# Module-level scrape counter for the fallback path (the prometheus path has its
# own Counter). Kept here so the count survives across requests in one process.
_FALLBACK_SCRAPE_COUNT = 0


# ---------------------------------------------------------------------------
# The endpoint.
# ---------------------------------------------------------------------------


@router.get(
    "/metrics",
    include_in_schema=False,
    summary="Prometheus metrics",
    dependencies=[Depends(require_auth)],
)
async def metrics(request: Request) -> Response:
    """Expose application metrics in Prometheus text exposition format.

    Import-safe and connection-free: it reads only ``request.app.state`` scalars
    (set by other handlers / the lifespan hook) so it never touches Postgres,
    Redis, or DuckDB and never 5xxs on a cold app.
    """
    app_state = request.app.state
    _schedule_finals_refresh(app_state)

    if _PROM_AVAILABLE:
        # Refresh the gauges from app.state at scrape time, bump the scrape
        # counter, and let prometheus_client serialize the registry.
        _SCRAPES.inc()
        vals = _collect(app_state)
        _REQUESTS.set(vals["requests_total"])
        _SIM_LATENCY_LAST.set(vals["sim_latency_last"])
        _API_P95.set(vals["api_p95"])
        _PIPELINE_FRESHNESS.set(vals["pipeline_freshness"])
        _FINALS_AGE.set(vals["finals_age"])
        for store, age in _backup_ages(app_state).items():
            _BACKUP_AGE.labels(store=store).set(age)
        # The off-site copy (SIM-568): the stored reading only, never a disk read.
        for store, age in _offsite_ages(app_state).items():
            _OFFSITE_BACKUP_AGE.labels(store=store).set(age)
        body = generate_latest(_REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    # Fallback: hand-rolled exposition with the identical series.
    global _FALLBACK_SCRAPE_COUNT
    _FALLBACK_SCRAPE_COUNT += 1
    body = _render_fallback(app_state, _FALLBACK_SCRAPE_COUNT)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
