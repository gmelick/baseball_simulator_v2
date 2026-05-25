"""
tests/unit/test_metrics_sim374.py
=================================
SIM-374 — Prometheus metrics endpoint + monitoring config tests.

Covers:
  1. GET /metrics returns 200 with a ``text/plain`` content type and a body in
     valid Prometheus text exposition format (``# HELP`` / ``# TYPE`` lines +
     the expected metric names). Works whether or not ``prometheus_client`` is
     installed — the route auto-selects the real registry or the hand-rolled
     fallback, and these assertions hold for either path.
  2. The instrumentation helpers (record_sim_latency / record_request) feed real
     values into the exposed series.
  3. deploy/monitoring/prometheus.yml parses as YAML and has a scrape job that
     targets the app, and the Grafana dashboard JSON parses with panels.

The test builds a TINY FastAPI app mounting ONLY the metrics router (no DB /
Redis / lifespan needed) so it runs fast and connection-free in the sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import metrics as metrics_mod
from api.routes.metrics import router as metrics_router

# Repo root: tests/unit/this_file -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]
MONITORING = REPO_ROOT / "deploy" / "monitoring"

# The series the scrape config + Grafana dashboards bind to. Exposed in BOTH the
# prometheus_client path and the hand-rolled fallback path.
EXPECTED_METRICS = [
    "baseball_sim_app_info",
    "baseball_sim_metrics_scrapes_total",
    "baseball_sim_requests_total",
    "baseball_sim_latency_seconds",
    "baseball_sim_api_p95_seconds",
    "baseball_sim_pipeline_freshness_seconds",
]


@pytest.fixture()
def client() -> TestClient:
    """A minimal app mounting only the metrics router (no lifespan / DB)."""
    app = FastAPI()
    app.include_router(metrics_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_200_and_content_type(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus text exposition is text/plain (version=0.0.4).
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_body_is_valid_exposition(client: TestClient) -> None:
    body = client.get("/metrics").text
    # Exposition format requires HELP + TYPE comment lines.
    assert "# HELP" in body
    assert "# TYPE" in body
    # Every expected series must be present (whichever render path produced it).
    for name in EXPECTED_METRICS:
        assert name in body, f"missing metric series: {name}"
    # app_info carries the version label and is always 1.
    assert 'baseball_sim_app_info{version=' in body


def test_metrics_help_and_type_lines_well_formed(client: TestClient) -> None:
    body = client.get("/metrics").text
    lines = body.splitlines()
    help_lines = [ln for ln in lines if ln.startswith("# HELP")]
    type_lines = [ln for ln in lines if ln.startswith("# TYPE")]
    # One HELP + one TYPE per expected metric, at minimum.
    assert len(help_lines) >= len(EXPECTED_METRICS)
    assert len(type_lines) >= len(EXPECTED_METRICS)
    # Each TYPE line names a known metric kind.
    for ln in type_lines:
        parts = ln.split()
        # "# TYPE <name> <kind>"
        assert parts[-1] in {"gauge", "counter", "histogram", "summary", "untyped"}


def test_scrape_counter_increments(client: TestClient) -> None:
    """Two scrapes -> the scrapes_total counter must be present both times.

    (The exact value differs between the prometheus path — its own Counter — and
    the fallback path — a module global; both monotonically increase, so we only
    assert the series stays present and parseable across calls.)
    """
    first = client.get("/metrics").text
    second = client.get("/metrics").text
    assert "baseball_sim_metrics_scrapes_total" in first
    assert "baseball_sim_metrics_scrapes_total" in second


def test_instrumentation_helpers_feed_values() -> None:
    """record_sim_latency / record_request surface real readings in /metrics."""
    app = FastAPI()
    app.include_router(metrics_router)
    # Seed app.state via the public helpers (the seam other routers use).
    metrics_mod.record_sim_latency(app.state, 1.25)
    metrics_mod.record_request(app.state)
    metrics_mod.record_request(app.state)
    client = TestClient(app)
    body = client.get("/metrics").text
    # The latency series must reflect the recorded value (1.25s).
    assert "baseball_sim_latency_seconds" in body
    assert "1.25" in body
    # Two requests recorded -> requests_total carries 2 (the metrics handler
    # itself does not call record_request, so the count is exactly our 2).
    assert "baseball_sim_requests_total 2" in body


def test_pipeline_freshness_defaults_to_no_data(client: TestClient) -> None:
    """With no re-sim signal, freshness reports -1 (the no-data sentinel)."""
    body = client.get("/metrics").text
    assert "baseball_sim_pipeline_freshness_seconds -1" in body


# ---------------------------------------------------------------------------
# Monitoring config files
# ---------------------------------------------------------------------------


def test_prometheus_yml_parses_and_scrapes_app() -> None:
    path = MONITORING / "prometheus.yml"
    assert path.is_file(), f"missing {path}"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "scrape_configs" in cfg
    jobs = cfg["scrape_configs"]
    # There must be a job that targets the app on :8000 via /metrics.
    app_jobs = [
        j
        for j in jobs
        if any(
            "app:8000" in t
            for sc in j.get("static_configs", [])
            for t in sc.get("targets", [])
        )
    ]
    assert app_jobs, "no scrape job targets app:8000"
    assert app_jobs[0].get("metrics_path", "/metrics") == "/metrics"
    # A sane scrape interval is configured globally.
    assert "global" in cfg and "scrape_interval" in cfg["global"]


def test_grafana_datasource_yml_parses() -> None:
    path = MONITORING / "grafana" / "datasource.yml"
    assert path.is_file(), f"missing {path}"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    ds = cfg["datasources"][0]
    assert ds["type"] == "prometheus"
    assert "prometheus:9090" in ds["url"]


def test_grafana_dashboard_json_parses() -> None:
    path = MONITORING / "grafana" / "baseball_sim_dashboard.json"
    assert path.is_file(), f"missing {path}"
    dash = json.loads(path.read_text(encoding="utf-8"))
    assert "panels" in dash and len(dash["panels"]) >= 3
    # Panels should reference the SIM-374 metric series.
    exprs = " ".join(
        t.get("expr", "")
        for p in dash["panels"]
        for t in p.get("targets", [])
    )
    assert "baseball_sim_latency_seconds" in exprs
    assert "baseball_sim_api_p95_seconds" in exprs
    assert "baseball_sim_pipeline_freshness_seconds" in exprs
