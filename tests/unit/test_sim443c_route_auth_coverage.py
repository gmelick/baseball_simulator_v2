"""
tests/unit/test_sim443c_route_auth_coverage.py — SIM-443c

The API requires a login on every data route, in every environment.

These tests check four things:
  - every non-exempt HTTP route of the real app carries ``require_auth`` in its
    dependency tree, and each data router carries it at the router level;
  - the live WebSocket refuses a handshake with no credential (close code
    1008) and accepts a session cookie or a Bearer key;
  - ``ENVIRONMENT`` plays no part: a protected route answers 401 in
    development, staging, production and with the variable unset;
  - ``AUTH_DISABLED=1`` is the one opt-out, and ``/health``, ``/auth/me`` and
    ``validate_environment`` report it.

tests/conftest.py sets AUTH_DISABLED=1 for the whole suite. Every test here
deletes it with monkeypatch first, unless the test is about the opt-out.
"""

from __future__ import annotations

import logging
import unittest.mock as mock
from contextlib import asynccontextmanager

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.auth import SESSION_COOKIE_NAME, _mint_session_token, require_auth

# Paths that stay open on purpose: the probes, the login itself, the API docs
# and the SPA catch-all (mounted only when frontend/dist exists).
_EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/ready",
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
        "/{full_path:path}",
    }
)

# One GET path per protected router; each one matches a real route.
_REPRESENTATIVE_GETS = [
    "/api/games/2026-10-13",
    "/api/betting/games/1/edges",
    "/api/players/search?q=ab",
    "/api/schema",
    "/api/data/freshness",
    "/api/odds/1",
    "/api/assistant/status",
    "/metrics",
]

_API_KEY = "test-key-sim443c"  # nosec - test-only literal


def _env(monkeypatch, *, environment: str | None = "development", keys: str = _API_KEY) -> None:
    """Enforce the login and set the variables the app needs to build."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SECRET_KEY", "sim443c-secret")
    monkeypatch.setenv("AUTH_PASSWORD", "sim443c-password")
    if keys:
        monkeypatch.setenv("API_KEYS", keys)
    else:
        monkeypatch.delenv("API_KEYS", raising=False)
    monkeypatch.setenv("BASEBALL_DB_DSN", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")


def _app():
    """Build the real app with a no-op lifespan (no Postgres, Redis or DuckDB)."""

    @asynccontextmanager
    async def _noop_lifespan(app):
        from simulation.batch_runner import make_cache

        app.state.sim_cache = make_cache()
        yield

    with mock.patch("api.main.lifespan", _noop_lifespan):
        from api.main import create_app

        return create_app()


def _client() -> TestClient:
    return TestClient(_app(), base_url="https://testserver")


def _calls(dependant) -> set:
    """Every dependency callable in a dependant's tree, recursively."""
    found = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= _calls(sub)
    return found


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith("/auth/")


# ---------------------------------------------------------------------------
# 1-3. The route walk
# ---------------------------------------------------------------------------


def test_every_data_route_requires_auth(monkeypatch):
    _env(monkeypatch)
    app = _app()
    http_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert len(http_routes) > 40, "the walk should see the whole app"
    ungated = [
        f"{sorted(r.methods)} {r.path}"
        for r in http_routes
        if not _is_exempt(r.path) and require_auth not in _calls(r.dependant)
    ]
    assert ungated == [], f"routes with no login: {ungated}"


def test_each_data_router_carries_the_gate_and_the_open_ones_do_not():
    from api.routes import (
        ai_assistant,
        analytics,
        betting,
        data_health,
        games,
        metrics,
        players,
        schema_introspect,
        similarity,
        similarity_explorer,
        sql_runner,
    )
    from api.routes.auth import router as auth_router
    from pipeline.live.live_ingestion_pipeline import odds_router, ws_router

    def gated(router) -> bool:
        return any(d.dependency is require_auth for d in router.dependencies)

    protected = [
        ai_assistant.router,
        analytics.router,
        betting.router,
        data_health.router,
        games.router,
        metrics.router,
        players.router,
        schema_introspect.router,
        similarity.router,
        similarity_explorer.router,
        sql_runner.router,
        odds_router,
    ]
    assert all(gated(r) for r in protected), [r.prefix for r in protected if not gated(r)]
    assert not gated(auth_router)
    assert not gated(ws_router)


def test_the_websocket_route_set_is_exactly_the_live_game_socket(monkeypatch):
    _env(monkeypatch)
    app = _app()
    ws_paths = {r.path for r in app.routes if isinstance(r, APIWebSocketRoute)}
    assert ws_paths == {"/ws/games/{game_pk}"}


# ---------------------------------------------------------------------------
# 4-5. The WebSocket handshake
# ---------------------------------------------------------------------------


def test_websocket_without_credentials_is_closed_with_1008(monkeypatch):
    _env(monkeypatch)
    client = _client()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/games/77") as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008


@pytest.mark.parametrize("how", ["cookie", "bearer", "x_api_key"])
def test_websocket_with_a_credential_is_accepted(monkeypatch, how):
    _env(monkeypatch)
    client = _client()
    if how == "cookie":
        headers = {"cookie": f"{SESSION_COOKIE_NAME}={_mint_session_token()}"}
    elif how == "bearer":
        headers = {"Authorization": f"Bearer {_API_KEY}"}
    else:
        headers = {"X-API-Key": _API_KEY}
    with client.websocket_connect("/ws/games/77", headers=headers) as ws:
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}


# ---------------------------------------------------------------------------
# 6-8. HTTP enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "staging", "production", None])
@pytest.mark.parametrize("path", _REPRESENTATIVE_GETS)
def test_protected_get_is_401_in_every_environment(monkeypatch, environment, path):
    _env(monkeypatch, environment=environment)
    res = _client().get(path)
    assert res.status_code == 401, (environment, path, res.status_code)
    assert "WWW-Authenticate" in res.headers
    assert "Bearer" in res.headers["WWW-Authenticate"]


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": f"Bearer {_API_KEY}"}, {"X-API-Key": _API_KEY}],
    ids=["bearer", "x_api_key"],
)
def test_a_configured_key_is_accepted(monkeypatch, headers):
    _env(monkeypatch)
    res = _client().get("/metrics", headers=headers)
    assert res.status_code == 200


def test_a_wrong_key_and_an_unconfigured_key_list_are_401(monkeypatch):
    _env(monkeypatch, keys="key-one,key-two")
    client = _client()
    assert client.get("/metrics", headers={"X-API-Key": "key-three"}).status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": "key-two"}).status_code == 200
    assert client.get("/metrics", headers={"Authorization": "Basic key-two"}).status_code == 401

    _env(monkeypatch, keys="")
    res = _client().get("/metrics", headers={"X-API-Key": _API_KEY})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# 9-11. The opt-out and how the app reports it
# ---------------------------------------------------------------------------


def test_health_reports_whether_auth_is_enforced(monkeypatch):
    _env(monkeypatch)
    assert _client().get("/health").json()["auth_enforced"] is True
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert _client().get("/health").json()["auth_enforced"] is False


def test_auth_disabled_opens_the_routes_and_me_says_disabled(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("AUTH_DISABLED", "1")
    client = _client()
    assert client.get("/metrics").status_code == 200
    body = client.get("/auth/me").json()
    assert body["authenticated"] is True
    assert body["mode"] == "disabled"


def test_auth_me_matrix(monkeypatch):
    _env(monkeypatch)
    client = _client()

    none = client.get("/auth/me").json()
    assert (none["authenticated"], none["mode"], none["expires_in"]) == (False, "none", 0)

    key = client.get("/auth/me", headers={"Authorization": f"Bearer {_API_KEY}"}).json()
    assert (key["authenticated"], key["mode"], key["expires_in"]) == (True, "api_key", 0)

    wrong = client.get("/auth/me", headers={"X-API-Key": "nope"}).json()
    assert (wrong["authenticated"], wrong["mode"]) == (False, "none")

    assert client.post("/auth/login", json={"password": "sim443c-password"}).status_code == 200
    cookie = client.get("/auth/me").json()
    assert (cookie["authenticated"], cookie["mode"]) == (True, "cookie")
    assert cookie["expires_in"] > 0


# ---------------------------------------------------------------------------
# 12-14. validate_environment
# ---------------------------------------------------------------------------


def test_validate_environment_requires_the_secrets_in_development(monkeypatch):
    from api.main import validate_environment

    _env(monkeypatch, environment="development")
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.delenv("AUTH_PASSWORD")
    with pytest.raises(RuntimeError, match="gen_secrets") as excinfo:
        validate_environment()
    assert "SECRET_KEY" in str(excinfo.value)
    assert "AUTH_PASSWORD" in str(excinfo.value)


def test_validate_environment_treats_whitespace_as_missing(monkeypatch):
    from api.main import validate_environment

    _env(monkeypatch, environment="production")
    monkeypatch.setenv("SECRET_KEY", "   ")
    with pytest.raises(RuntimeError, match="Missing required auth variable") as excinfo:
        validate_environment()
    assert "SECRET_KEY" in str(excinfo.value)
    assert "AUTH_PASSWORD" not in str(excinfo.value)


def test_validate_environment_warns_and_passes_when_auth_is_disabled(monkeypatch, caplog):
    from api.main import validate_environment

    _env(monkeypatch)
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.delenv("AUTH_PASSWORD")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    with caplog.at_level(logging.WARNING, logger="api.main"):
        validate_environment()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "AUTH_DISABLED" in warnings[0].getMessage()
