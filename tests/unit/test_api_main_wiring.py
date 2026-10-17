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

import uuid

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
    # SIM-443e: no read-only DSN, so the lifespan opens no console pool.
    monkeypatch.delenv("BASEBALL_DB_RO_DSN", raising=False)

    # Stub the live-infra openers the lifespan calls (return AsyncMocks with the
    # close/aclose coroutines the shutdown path awaits).
    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()
    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()

    monkeypatch.setattr(main_mod, "open_pg_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(main_mod, "make_pg_name_resolver", lambda pool: lambda ids: {})
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
# SIM-443e — the lifespan opens the SQL console's pool only on a read-only role
# ---------------------------------------------------------------------------

_MAIN_DSN = "postgresql://baseball_app:app-secret@db:5432/baseball_sim"
_RO_DSN = "postgresql://baseball_ro:ro-secret@db:5432/baseball_sim"


def _ro_lifespan_setup(monkeypatch, role_check):
    """Stub the lifespan's openers; return (app, main_pool, ro_pool, opener).

    ``role_check`` is the ReadOnlyRoleCheck the stubbed check returns, or an
    exception instance the check raises.
    """
    from unittest.mock import AsyncMock, MagicMock

    import api.main as main_mod
    import api.routes.sql_safety as sql_safety

    monkeypatch.setenv("BASEBALL_DB_DSN", _MAIN_DSN)
    monkeypatch.setenv("BASEBALL_DB_RO_DSN", _RO_DSN)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SIMILARITY_ENGINE_ENABLED", "false")
    monkeypatch.setenv("LIVE_PIPELINE_ENABLED", "false")
    monkeypatch.setenv("REPLAY_PERSISTENCE_ENABLED", "false")
    monkeypatch.setenv("AUTH_DISABLED", "1")

    main_pool = MagicMock(name="main_pool")
    main_pool.close = AsyncMock()
    ro_pool = MagicMock(name="ro_pool")
    ro_pool.close = AsyncMock()

    async def _open(dsn, **kwargs):
        return ro_pool if dsn == _RO_DSN else main_pool

    opener = AsyncMock(side_effect=_open)
    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr(main_mod, "open_pg_pool", opener)
    monkeypatch.setattr(main_mod, "make_pg_name_resolver", lambda pool: lambda ids: {})
    monkeypatch.setattr(
        main_mod, "open_redis_cache", AsyncMock(return_value=(fake_redis, MagicMock()))
    )
    if isinstance(role_check, Exception):
        check = AsyncMock(side_effect=role_check)
    else:
        check = AsyncMock(return_value=role_check)
    monkeypatch.setattr(sql_safety, "check_read_only_role", check)

    # These tests read only the console pool. Stub the sim runner, its pre-warm
    # and the bundle load, so no test loads the real bundle or starts workers
    # (that raised the pytest process's peak memory past the SIM-280 cap).
    import api.bundle_watch as bundle_watch
    import simulation.batch_runner as batch_runner
    from pipeline.batch.engine_artifacts import EngineArtifacts

    def _no_bundle(*_args, **_kwargs):
        raise FileNotFoundError("no bundle in this test")

    monkeypatch.setattr(bundle_watch, "bundle_link", lambda: "/nonexistent/engine_artifacts")
    monkeypatch.setattr(EngineArtifacts, "load", staticmethod(_no_bundle))
    monkeypatch.setattr(batch_runner, "BatchRunner", MagicMock(name="BatchRunner"))
    monkeypatch.setattr(main_mod, "_background_prewarm", AsyncMock(return_value=None))
    return main_mod.create_app(), main_pool, ro_pool, opener


def _drive_lifespan(app):
    """Enter and exit the lifespan; return what app.state held inside it."""
    import asyncio

    import api.main as main_mod

    async def _drive():
        async with main_mod.lifespan(app):
            return app.state.pg_pool_ro, app.state.sql_console_role

    return asyncio.run(_drive())


def _role(**overrides):
    from api.routes.sql_safety import ReadOnlyRoleCheck

    fields = {
        "role": "baseball_ro",
        "superuser": False,
        "sim_usage": False,
        "raw_insert": False,
        "raw_select": True,
    }
    fields.update(overrides)
    return ReadOnlyRoleCheck(**fields)


def test_lifespan_refuses_a_superuser_console_pool(monkeypatch, caplog):
    """A superuser role is refused: the pool is closed and left None."""
    import logging

    app, _main, ro_pool, _opener = _ro_lifespan_setup(
        monkeypatch, _role(role="baseball_user", superuser=True)
    )
    with caplog.at_level(logging.ERROR, logger="api.main"):
        attached, role = _drive_lifespan(app)
    assert attached is None
    assert role is None
    ro_pool.close.assert_awaited()
    assert any("superuser=True" in r.getMessage() for r in caplog.records)
    # No password in any log line.
    assert all("ro-secret" not in r.getMessage() for r in caplog.records)


def test_lifespan_accepts_a_read_only_console_pool(monkeypatch, caplog):
    """A read-only role is attached with search_path=raw and closed at shutdown."""
    import logging

    app, _main, ro_pool, opener = _ro_lifespan_setup(monkeypatch, _role())
    with caplog.at_level(logging.INFO, logger="api.main"):
        attached, role = _drive_lifespan(app)
    assert attached is ro_pool
    assert role == "baseball_ro"
    ro_calls = [c for c in opener.await_args_list if c.args and c.args[0] == _RO_DSN]
    assert len(ro_calls) == 1
    assert ro_calls[0].kwargs.get("server_settings") == {"search_path": "raw"}
    ro_pool.close.assert_awaited()  # shutdown closed it
    opened = [r.getMessage() for r in caplog.records if "pool open as" in r.getMessage()]
    assert opened and "baseball_ro" in opened[0]
    assert all("ro-secret" not in r.getMessage() for r in caplog.records)


def test_lifespan_refuses_when_the_role_check_fails(monkeypatch):
    """A failing role check refuses the pool (fail closed) and closes it."""
    app, _main, ro_pool, _opener = _ro_lifespan_setup(
        monkeypatch, RuntimeError("connection refused for ro-secret")
    )
    attached, role = _drive_lifespan(app)
    assert attached is None
    assert role is None
    ro_pool.close.assert_awaited()


def test_lifespan_refuses_a_role_that_can_write(monkeypatch):
    """A role with sim usage or a raw insert grant is refused."""
    app, _main, ro_pool, _opener = _ro_lifespan_setup(
        monkeypatch, _role(role="baseball_app", sim_usage=True, raw_insert=True)
    )
    attached, _role_name = _drive_lifespan(app)
    assert attached is None
    ro_pool.close.assert_awaited()


def test_health_reports_the_sql_console_state():
    """/health carries the console's pool state and role, never a DSN."""
    from api.main import create_app

    app = create_app()
    client = TestClient(app)  # no `with`: the lifespan does not run
    body = client.get("/health").json()
    assert body["sql_console"] == {"ro_pool": False, "role": None}

    app.state.pg_pool_ro = object()
    app.state.sql_console_role = "baseball_ro"
    body = client.get("/health").json()
    assert body["sql_console"] == {"ro_pool": True, "role": "baseball_ro"}


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


# ---------------------------------------------------------------------------
# SIM-564: the limiter counts a request against a key only when the key is
# a configured API key. Every other request counts against the client address.
# ---------------------------------------------------------------------------

_SIM564_REAL_KEY = "sim564-real-key-0123456789"


def test_rate_limit_made_up_keys_count_against_client_address(monkeypatch):
    """SIM-564: made-up keys share the client address's bucket.

    A caller that sends a new made-up key on every request still reaches
    the limit, so the third request answers 429.
    """
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=2))
    assert client.get("/data", headers={"X-API-Key": "made-up-1"}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": "made-up-2"}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": "made-up-3"}).status_code == 429


def test_rate_limit_many_made_up_keys_reach_the_limit(monkeypatch):
    """SIM-564: twenty requests with made-up keys at limit 5 get 429 from the sixth request on."""
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=5))
    codes = [
        client.get("/data", headers={"X-API-Key": uuid.uuid4().hex}).status_code for _ in range(20)
    ]
    assert codes[:5] == [200] * 5
    assert set(codes[5:]) == {429}


def test_rate_limit_real_key_counts_under_the_key(monkeypatch):
    """SIM-564: a configured API key has its own bucket.

    The test guards the key's own bucket against an over-fix that always
    uses the client address. The key's bucket fills after two requests.
    A request with no key from the same client still answers 200, because
    the client address has a separate bucket.
    """
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=2))
    real = {"X-API-Key": _SIM564_REAL_KEY}
    assert client.get("/data", headers=real).status_code == 200
    assert client.get("/data", headers=real).status_code == 200
    assert client.get("/data", headers=real).status_code == 429
    assert client.get("/data").status_code == 200


def test_rate_limit_no_key_does_not_consume_the_real_key(monkeypatch):
    """SIM-564: requests with no key do not use up a configured key's bucket.

    The test guards the key's own bucket against an over-fix that always
    uses the client address.
    """
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=2))
    assert client.get("/data").status_code == 200
    assert client.get("/data").status_code == 200
    assert client.get("/data").status_code == 429
    real = {"X-API-Key": _SIM564_REAL_KEY}
    assert client.get("/data", headers=real).status_code == 200
    assert client.get("/data", headers=real).status_code == 200


def test_rate_limit_bearer_and_header_share_one_bucket(monkeypatch):
    """SIM-564: one key in X-API-Key or in Authorization: Bearer counts against one bucket.

    The login reads both headers through ``_presented_api_key``, so the
    limiter reads them the same way.
    """
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=2))
    bearer = {"Authorization": f"Bearer {_SIM564_REAL_KEY}"}
    header = {"X-API-Key": _SIM564_REAL_KEY}
    assert client.get("/data", headers=bearer).status_code == 200
    assert client.get("/data", headers=header).status_code == 200
    assert client.get("/data", headers=bearer).status_code == 429
    assert client.get("/data", headers=header).status_code == 429
    # The client address bucket is still empty.
    assert client.get("/data").status_code == 200


def test_rate_limit_no_configured_keys_counts_against_client_address(monkeypatch):
    """SIM-564: with no configured API keys, every key counts against the client address."""
    for value in (None, "", " , "):
        if value is None:
            monkeypatch.delenv("API_KEYS", raising=False)
        else:
            monkeypatch.setenv("API_KEYS", value)
        client = TestClient(_tiny_app(rate_limit=2))
        assert client.get("/data", headers={"X-API-Key": "any-1"}).status_code == 200
        assert client.get("/data", headers={"X-API-Key": "any-2"}).status_code == 200
        assert client.get("/data").status_code == 429


def test_rate_limit_bucket_name_holds_no_key_in_clear(monkeypatch):
    """SIM-564: no bucket name holds the text of a key.

    The test wraps a tiny app in the middleware directly, so it can read
    the middleware's ``_hits`` dict.
    """
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    inner = _tiny_app()
    middleware = RateLimitMiddleware(inner, limit=5, window_s=60, enabled=True)
    client = TestClient(middleware)
    assert client.get("/data", headers={"X-API-Key": _SIM564_REAL_KEY}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": "made-up-key-xyz"}).status_code == 200
    names = list(middleware._hits)
    assert names, "the middleware recorded no bucket"
    for name in names:
        assert _SIM564_REAL_KEY not in name
        assert "made-up-key-xyz" not in name
    assert any(name.startswith("key:") for name in names)
    assert "ip:testclient" in names


def test_rate_limit_made_up_keys_do_not_consume_the_real_key(monkeypatch):
    """SIM-564: made-up keys fill the client address bucket, not the real key's bucket."""
    monkeypatch.setenv("API_KEYS", _SIM564_REAL_KEY)
    client = TestClient(_tiny_app(rate_limit=2))
    assert client.get("/data", headers={"X-API-Key": "made-up-1"}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": "made-up-2"}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": "made-up-3"}).status_code == 429
    real = {"X-API-Key": _SIM564_REAL_KEY}
    assert client.get("/data", headers=real).status_code == 200


def test_rate_limit_two_configured_keys_keep_separate_buckets(monkeypatch):
    """SIM-564: each configured API key has a bucket of its own.

    A full bucket for the first key does not throttle the second key.
    """
    first = "sim564-first-key-0123456789"
    second = "sim564-second-key-0123456789"
    monkeypatch.setenv("API_KEYS", f"{first},{second}")
    client = TestClient(_tiny_app(rate_limit=2))
    assert client.get("/data", headers={"X-API-Key": first}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": first}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": first}).status_code == 429
    assert client.get("/data", headers={"X-API-Key": second}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": second}).status_code == 200
    assert client.get("/data", headers={"X-API-Key": second}).status_code == 429
