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
    baseball_sim_api_p95_seconds            gauge      PLACEHOLDER — API p95 latency;
                                                              0 until the ASGI timing
                                                              middleware feeds it.
    baseball_sim_pipeline_freshness_seconds gauge      LIVE-WHERE-WIRED — seconds
                                                              since the last live re-sim
                                                              signal (app.state.last_resim_signal),
                                                              else -1 (no data yet).

The values are read from ``request.app.state`` where available (so no DB / Redis
connection is required — this module is fully import-safe and the endpoint never
5xxs on a cold app) and otherwise reported as not-yet-populated.

Owner: QA / DevOps (SIM-374).
"""

from __future__ import annotations

import os
import time
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


router = APIRouter(tags=["ops"])

# App build version surfaced as a label on the info gauge. Kept in sync with
# api/main.py's FastAPI(version=...) intent; overridable via env for releases.
_APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-phase5")


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
    ts = getattr(app_state, "last_resim_signal_ts", None)
    if ts is None:
        return -1.0
    try:
        return max(0.0, time.time() - float(ts))
    except Exception:  # noqa: BLE001
        return -1.0


def _collect(app_state: Any) -> dict[str, float]:
    """Snapshot the current scalar readings used by both render paths."""
    return {
        "requests_total": float(getattr(app_state, "request_count", 0) or 0),
        "sim_latency_last": float(getattr(app_state, "sim_latency_last_seconds", 0.0) or 0.0),
        "api_p95": float(getattr(app_state, "api_p95_seconds", 0.0) or 0.0),
        "pipeline_freshness": _pipeline_freshness_seconds(app_state),
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

    if _PROM_AVAILABLE:
        # Refresh the gauges from app.state at scrape time, bump the scrape
        # counter, and let prometheus_client serialize the registry.
        _SCRAPES.inc()
        vals = _collect(app_state)
        _REQUESTS.set(vals["requests_total"])
        _SIM_LATENCY_LAST.set(vals["sim_latency_last"])
        _API_P95.set(vals["api_p95"])
        _PIPELINE_FRESHNESS.set(vals["pipeline_freshness"])
        body = generate_latest(_REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    # Fallback: hand-rolled exposition with the identical series.
    global _FALLBACK_SCRAPE_COUNT
    _FALLBACK_SCRAPE_COUNT += 1
    body = _render_fallback(app_state, _FALLBACK_SCRAPE_COUNT)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
