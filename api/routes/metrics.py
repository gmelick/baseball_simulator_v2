"""
api/routes/metrics.py
=====================
The Prometheus metrics endpoint.

    GET /metrics   (login required; not in the OpenAPI schema)
        The app's metrics in the Prometheus text exposition format
        (``text/plain; version=0.0.4``), for the Prometheus scrape
        (deploy/monitoring/prometheus.yml) and the Grafana dashboards
        (deploy/monitoring/grafana/).

``prometheus_client`` is optional (it is not in requirements.txt). When it
imports, the module uses its registry (``Counter``, ``Gauge``,
``generate_latest``); when it does not, a small hand-rolled writer
(:func:`_render_fallback`) emits the same metric names and the same ``# HELP``
and ``# TYPE`` lines. The scrape and the dashboards bind to the same series
either way.

THE SERIES
----------
    baseball_sim_app_info{version=...}       gauge    always 1; the build version is a label.
    baseball_sim_metrics_scrapes_total       counter  one per /metrics scrape.
    baseball_sim_requests_total              gauge    the requests served
                                                      (``LatencyMiddleware`` counts them
                                                      through :func:`record_request`).
    baseball_sim_latency_seconds             gauge    the wall-clock time of the newest
                                                      /simulate batch (:func:`record_sim_latency`).
    baseball_sim_api_p95_seconds             gauge    the API's p95 latency over the last
                                                      200 requests (``LatencyMiddleware``).
    baseball_sim_pipeline_freshness_seconds  gauge    seconds since the live service's
                                                      heartbeat, else since the last
                                                      in-process re-sim signal; -1 with none.
    baseball_sim_finals_age_hours            gauge    hours since the newest final game of the
                                                      current season loaded; -1 when unknown.
    baseball_sim_backup_age_hours{store=...} gauge    hours since the newest complete backup
                                                      set of each store (postgres, duckdb,
                                                      bundle); -1 when the store has none
                                                      (the scheduled backups, SIM-525).
    baseball_sim_offsite_backup_age_hours{store=...}
                                             gauge    the age in hours of each store's newest
                                                      set that is complete off-site, counted
                                                      from the set's creation; -1 when none
                                                      (the off-site copy, SIM-568).

The scrape reads ``request.app.state`` only, so it needs no database or Redis
connection and never fails on a cold app. The values that need a read (the
newest final game, the live heartbeat, the backup folders) refresh in the
background at most every few minutes (:func:`_schedule_finals_refresh`); a read
of the off-site folder runs on a daemon thread of its own, because a OneDrive
file can stall it.
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
# prometheus_client when it imports, else the hand-rolled writer
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


# /metrics requires a login; Prometheus sends an Authorization: Bearer key.
router = APIRouter(tags=["ops"], dependencies=[Depends(require_auth)])

# The build version, a label of the info gauge: APP_VERSION, else the
# version create_app gives FastAPI.
_APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-phase7")

#: The help text of the off-site backup-age gauge. The age counts from the
#: set's creation, because the off-site manifest is a verbatim copy of the
#: local one.
OFFSITE_BACKUP_AGE_HELP = (
    "Age in hours of the store's newest backup set that is complete off-site; -1 when none."
)


# ---------------------------------------------------------------------------
# The prometheus_client registry (built only when the library imports)
#
# A registry of its own, not the global default, keeps this module's series
# apart and avoids duplicate-series errors when one process builds the app
# more than once (the tests do).
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

    _OFFSITE_BACKUP_AGE = Gauge(
        "baseball_sim_offsite_backup_age_hours",
        OFFSITE_BACKUP_AGE_HELP,
        ["store"],
        registry=_REGISTRY,
    )


# ---------------------------------------------------------------------------
# The recorders other modules call
#
# The games routes and LatencyMiddleware feed the metrics through these, with no
# import of prometheus_client. They store each value on app.state, where both
# render paths read it.
# ---------------------------------------------------------------------------


def record_sim_latency(app_state: Any, seconds: float) -> None:
    """Record the wall-clock latency of the most recent simulation run.

    It stores the value on ``app.state`` and, when prometheus_client is
    present, sets the gauge too. It never raises.
    """
    try:
        app_state.sim_latency_last_seconds = float(seconds)
    except Exception:  # noqa: BLE001
        return
    if _PROM_AVAILABLE:
        _SIM_LATENCY_LAST.set(float(seconds))


def record_request(app_state: Any) -> None:
    """Increment the served-request counter on ``app.state``.

    It never raises. The scrape copies the count into the gauge.
    """
    try:
        app_state.request_count = int(getattr(app_state, "request_count", 0)) + 1
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The readings: from app.state, refreshed in the background
# ---------------------------------------------------------------------------


def _pipeline_freshness_seconds(app_state: Any) -> float:
    """Seconds since the live service's heartbeat, else since the last re-sim signal.

    The heartbeat (``app.state.live_heartbeat_ts``, read in the background from
    Redis) comes first; an in-process pipeline's signal
    (``app.state.last_resim_signal_ts``) is the fallback. With neither the gauge
    reads -1, so it exists for the dashboard without a made-up reading.
    """
    hb = getattr(app_state, "live_heartbeat_ts", None)
    ts = hb if hb is not None else getattr(app_state, "last_resim_signal_ts", None)
    if ts is None:
        return -1.0
    try:
        return max(0.0, time.time() - float(ts))
    except Exception:  # noqa: BLE001
        return -1.0


#: The newest final game's date is read at most this often, in seconds.
FINALS_AGE_REFRESH_S = 600

#: The date of the newest final game of the current season.
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


#: The heartbeat stamp is read at most this often, in seconds.
HEARTBEAT_REFRESH_S = 15


def _schedule_finals_refresh(app_state: Any) -> None:
    """Start each background refresh whose reading is stale.

    The newest final game, the live heartbeat and the off-site ages each have
    their own interval. The scrape never waits on a read: it serves the last
    stored value, and the next scrape sees the new one.
    """
    now = time.time()
    try:
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
    # A read of the off-site folder can stall on a OneDrive file that is only
    # online, so it runs on a daemon thread, never in the scrape. One read runs
    # at a time: while a read stalls, no scrape starts another.
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


#: The backup root is read at most this often, in seconds.
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


#: The off-site folder is read at most this often, in seconds.
OFFSITE_AGE_REFRESH_S = 600

#: The name of the daemon thread that reads the off-site folder.
OFFSITE_READ_THREAD_NAME = "offsite-backup-age"

#: One reading of the off-site folder and the time of the read.
_OffsiteReading = tuple[dict[str, float], float]


def _read_offsite_ages() -> dict[str, float]:
    """Read the off-site copy's ages from ``OFFSITE_ROOT`` (default ``/offsite``)."""
    return _read_ages("OFFSITE_ROOT", "/offsite")


def _read_offsite_ages_now() -> _OffsiteReading:
    """Read the off-site ages, then take the time of the read.

    The scrape adds the hours since this time to each age.
    """
    ages = _read_offsite_ages()
    return ages, time.time()


def _offsite_read_in_flight(app_state: Any) -> bool:
    """Return True while the last off-site read has not finished."""
    task = getattr(app_state, "offsite_refresh_task", None)
    if task is None:
        return False
    try:
        return not task.done()
    except Exception:  # noqa: BLE001 -- a metric must never fail the app
        return False


async def _read_offsite_on_daemon_thread() -> _OffsiteReading:
    """Run the off-site read on a daemon thread of its own.

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
    """Read the off-site ages on a daemon thread into ``app.state``.

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
    :func:`_schedule_finals_refresh` fills both.
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
# The hand-rolled text-exposition writer, for when prometheus_client is absent.
# It emits the same metric names and HELP and TYPE lines as the library path.
# ---------------------------------------------------------------------------


def _metric_block(name: str, help_text: str, mtype: str, lines: list[str]) -> str:
    """One metric's ``# HELP`` and ``# TYPE`` lines and its sample lines."""
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


# The fallback path's scrape counter (the library path has its own Counter),
# at module level, so the count lasts across the requests of one process.
_FALLBACK_SCRAPE_COUNT = 0


# ---------------------------------------------------------------------------
# The endpoint
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
        # Set the gauges from app.state, count the scrape, and let
        # prometheus_client write the registry.
        _SCRAPES.inc()
        vals = _collect(app_state)
        _REQUESTS.set(vals["requests_total"])
        _SIM_LATENCY_LAST.set(vals["sim_latency_last"])
        _API_P95.set(vals["api_p95"])
        _PIPELINE_FRESHNESS.set(vals["pipeline_freshness"])
        _FINALS_AGE.set(vals["finals_age"])
        for store, age in _backup_ages(app_state).items():
            _BACKUP_AGE.labels(store=store).set(age)
        # The stored off-site reading only, never a disk read.
        for store, age in _offsite_ages(app_state).items():
            _OFFSITE_BACKUP_AGE.labels(store=store).set(age)
        body = generate_latest(_REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    # The fallback: the hand-rolled exposition of the same series.
    global _FALLBACK_SCRAPE_COUNT
    _FALLBACK_SCRAPE_COUNT += 1
    body = _render_fallback(app_state, _FALLBACK_SCRAPE_COUNT)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
