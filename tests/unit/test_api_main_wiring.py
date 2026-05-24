"""
test_api_main_wiring.py
=======================
Unit tests for api/main.py app wiring (SIM-354 + SIM-351 CORS/rate-limit).

These tests build the app via ``create_app()`` and inspect the resulting
routes / middleware WITHOUT entering the FastAPI lifespan. The lifespan opens
a live asyncpg pool + Redis and builds the similarity engine, none of which
are available in a unit-test run; route + middleware registration happens in
``create_app()`` itself and needs no live connection, so we assert against the
constructed app object directly.

For the handful of request-level assertions (/health needs no API key, the
rate-limit returns 429) we mount a tiny isolated app so the heavy
similarity-engine lifespan never runs.

Owned by Backend Developer (SIM-354 / SIM-351).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from api.auth import RateLimitMiddleware


# ---------------------------------------------------------------------------
# create_app() — import-time wiring (no live DB/Redis required)
# ---------------------------------------------------------------------------


def _route_paths(app: FastAPI) -> set[str]:
    """All registered route path templates (HTTP + WebSocket)."""
    return {getattr(r, "path", None) for r in app.routes}


def test_create_app_builds_without_live_connections(monkeypatch):
    """create_app() must succeed with no DB/Redis env — route registration
    happens at factory time; live connections only open inside the lifespan."""
    monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    from api.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)


def test_ws_and_odds_routers_are_mounted(monkeypatch):
    """SIM-354: ws_router (/ws/games/{game_pk}) and odds_router
    (/api/odds/...) must be present in app.routes regardless of
    LIVE_PIPELINE_ENABLED — they register unconditionally in create_app()."""
    monkeypatch.setenv("LIVE_PIPELINE_ENABLED", "false")
    from api.main import create_app

    app = create_app()
    paths = _route_paths(app)

    # WebSocket route from ws_router (prefix="/ws").
    assert "/ws/games/{game_pk}" in paths
    # REST routes from odds_router (prefix="/api/odds").
    assert "/api/odds/{game_pk}" in paths
    assert "/api/odds/today/all" in paths


def test_exact_exported_names_importable():
    """Pin the exact symbols SIM-354 mounts so a rename in the pipeline
    module is caught here rather than at app boot."""
    from pipeline.live.live_ingestion_pipeline import (  # noqa: F401
        LiveIngestionPipeline,
        odds_router,
        ws_router,
    )

    # ws_router carries a websocket route; odds_router carries REST routes.
    ws_paths = {getattr(r, "path", None) for r in ws_router.routes}
    odds_paths = {getattr(r, "path", None) for r in odds_router.routes}
    assert "/ws/games/{game_pk}" in ws_paths
    assert "/api/odds/{game_pk}" in odds_paths


def test_pipeline_not_started_at_factory_time(monkeypatch):
    """SIM-354: building the app must NOT start the live pipeline (that only
    happens in the lifespan when LIVE_PIPELINE_ENABLED=true). app.state must
    have no ``pipeline`` attribute before the lifespan runs."""
    monkeypatch.setenv("LIVE_PIPELINE_ENABLED", "false")
    from api.main import create_app

    app = create_app()
    assert getattr(app.state, "pipeline", None) is None


def test_simulation_callback_seam_exists():
    """SIM-354: the pipeline exposes the re-sim seam main.py wires into —
    a simulation_callback constructor arg and _signal_resimulation method."""
    import inspect

    from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

    sig = inspect.signature(LiveIngestionPipeline.__init__)
    assert "simulation_callback" in sig.parameters
    assert hasattr(LiveIngestionPipeline, "_signal_resimulation")


# ---------------------------------------------------------------------------
# SIM-360 — the lifespan attaches + closes the persistent BatchRunner
# ---------------------------------------------------------------------------


def test_lifespan_attaches_and_closes_sim_runner(monkeypatch):
    """SIM-360: entering the lifespan builds ONE long-lived BatchRunner on
    app.state.sim_runner; exiting it calls close() on that runner.

    The lifespan opens a live asyncpg pool + Redis + builds the similarity
    engine — all stubbed here (the engine build is also skipped via
    SIMILARITY_ENGINE_ENABLED=false) so the wiring runs with no live infra.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import api.main as main_mod

    monkeypatch.setenv("BASEBALL_DB_DSN", "postgresql://x/y")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SIMILARITY_ENGINE_ENABLED", "false")
    monkeypatch.setenv("LIVE_PIPELINE_ENABLED", "false")
    monkeypatch.setenv("REPLAY_PERSISTENCE_ENABLED", "false")

    # Stub the live-infra openers the lifespan calls (return AsyncMocks with the
    # close/aclose coroutines the shutdown path awaits).
    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()
    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()

    monkeypatch.setattr(main_mod, "open_pg_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(main_mod, "make_pg_name_resolver", lambda pool: (lambda ids: {}))
    monkeypatch.setattr(
        main_mod, "open_redis_cache", AsyncMock(return_value=(fake_redis, MagicMock()))
    )

    app = main_mod.create_app()

    async def _drive():
        async with main_mod.lifespan(app):
            # During the lifespan the runner is attached and is a BatchRunner.
            from simulation.batch_runner import BatchRunner

            runner = getattr(app.state, "sim_runner", None)
            assert isinstance(runner, BatchRunner)
            # Spy on close() so we can prove shutdown tears it down.
            runner.close = MagicMock(wraps=runner.close)
            app.state.sim_runner = runner
            return runner

        # control returns here AFTER the lifespan exits (shutdown ran).

    runner = asyncio.run(_drive())
    # Shutdown called close() on the persistent runner (SIM-360 teardown).
    assert runner.close.called


# ---------------------------------------------------------------------------
# CORS policy (SIM-351)
# ---------------------------------------------------------------------------


def _cors_origins(app: FastAPI) -> list[str] | None:
    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            return m.kwargs.get("allow_origins")
    return None


def test_cors_is_not_wildcard_in_non_dev(monkeypatch):
    """SIM-351: production CORS must never be ["*"] (would be invalid with
    allow_credentials=True). With ENVIRONMENT != development and nothing
    configured, the allowlist falls back to a concrete origin, not "*"."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    from api.main import create_app

    app = create_app()
    origins = _cors_origins(app)
    assert origins is not None
    assert "*" not in origins


def test_cors_uses_env_allowlist(monkeypatch):
    """SIM-351: CORS_ORIGINS (comma-separated) drives the allowlist verbatim."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    from api.main import create_app

    app = create_app()
    origins = _cors_origins(app)
    assert origins == ["https://app.example.com", "https://admin.example.com"]


# ---------------------------------------------------------------------------
# /health needs no API key + rate-limit 429 (isolated app, no heavy lifespan)
# ---------------------------------------------------------------------------


def _tiny_app(rate_limit: int | None = None) -> FastAPI:
    """A minimal app mirroring main.py's ops endpoints + rate-limit wiring,
    without the similarity-engine lifespan that needs live DB/Redis."""
    app = FastAPI()
    if rate_limit is not None:
        app.add_middleware(RateLimitMiddleware, limit=rate_limit, window_s=60, enabled=True)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/data")
    async def data() -> dict:
        return {"ok": True}

    return app


def test_health_requires_no_api_key():
    """SIM-351: /health is an unauthenticated liveness probe."""
    client = TestClient(_tiny_app())
    resp = client.get("/health")  # no X-API-Key header
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_rate_limit_returns_429_after_limit():
    """SIM-351: with a tiny configured limit, the (limit+1)th request to a
    non-exempt path returns 429."""
    client = TestClient(_tiny_app(rate_limit=2))
    assert client.get("/data").status_code == 200
    assert client.get("/data").status_code == 200
    third = client.get("/data")
    assert third.status_code == 429
    assert "Retry-After" in third.headers


def test_rate_limit_exempts_health():
    """SIM-351: /health/ /ready/ / are never throttled even over the limit —
    an over-eager k8s probe loop must not trip the limiter."""
    client = TestClient(_tiny_app(rate_limit=1))
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_rate_limit_disabled_by_default(monkeypatch):
    """SIM-351: with RATE_LIMIT_PER_MINUTE unset, the middleware is a no-op so
    dev + the test suite are unaffected."""
    monkeypatch.delenv("RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)  # reads env → disabled

    @app.get("/data")
    async def data() -> dict:
        return {"ok": True}

    client = TestClient(app)
    for _ in range(20):
        assert client.get("/data").status_code == 200
