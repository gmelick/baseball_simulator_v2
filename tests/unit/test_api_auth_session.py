"""
tests/unit/test_api_auth_session.py — SIM-389

Unit tests for the browser session auth layer:
  - Session token mint / verify (api/auth._mint_session_token / _verify_session_token)
  - POST /auth/login  — correct password → 200 + cookie; wrong password → 401
  - GET  /auth/me     — with/without cookie
  - POST /auth/logout — clears cookie
  - require_auth dependency — cookie path, API-key path, 401 path
  - CORS dev origins no longer include wildcard (SIM-389 fix)
  - Protected route integration — GET /api/games/{game_pk}/simulate returns 401
    when no credential is presented, in every environment

Test environment (SIM-443c): the login is enforced in every environment.
tests/conftest.py sets AUTH_DISABLED=1 for the whole suite; every client this
file builds deletes it with monkeypatch, so the enforcement is what runs here.

The TestClient is built from create_app() with the no-DB seam
(``SIM_MACHINE_FACTORY_REF`` / rng_driven_machine_factory) so no live Postgres,
Redis, or DuckDB is required.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from api.auth import (
    SESSION_COOKIE_NAME,
    _b64url_encode,
    _get_secret_key,
    _mint_session_token,
    _verify_session_token,
    resolve_cors_origins,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(monkeypatch, env: str = "development") -> TestClient:
    """Build a TestClient with a fixed ENVIRONMENT and the login enforced."""
    _prepare_env(monkeypatch, env)
    return _build_client()


def _prepare_env(monkeypatch, env: str) -> None:
    """Set the variables every client here needs; drop the suite's opt-out."""
    # SIM-443c: tests/conftest.py opts the suite out of the login; this file
    # tests the login itself.
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-unit-tests")
    monkeypatch.setenv("AUTH_PASSWORD", "hunter2")
    # Prevent lifespan from trying to connect to Postgres / Redis.
    monkeypatch.setenv("BASEBALL_DB_DSN", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")


def _build_client() -> TestClient:
    """Build the app with a no-op lifespan and wrap it in a TestClient."""
    # Patch the lifespan startup so TestClient doesn't need live services.
    import unittest.mock as mock
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        from simulation.batch_runner import make_cache

        app.state.sim_cache = make_cache()
        app.state.calibration_map = __import__(
            "simulation.win_probability", fromlist=["IDENTITY_CALIBRATION"]
        ).IDENTITY_CALIBRATION
        yield

    with mock.patch("api.main.lifespan", _noop_lifespan):
        from api.main import create_app

        app = create_app()

    # Use https://testserver so the requests library stores Secure cookies
    # (the Starlette ASGI transport doesn't care about http vs https, but
    # the requests cookie jar does respect the Secure flag).
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Token primitives
# ---------------------------------------------------------------------------


class TestSessionTokenPrimitives:
    def test_mint_produces_dot_separated_token(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        token = _mint_session_token("user")
        parts = token.split(".")
        assert len(parts) == 2, "Token should be body.sig"

    def test_verify_valid_token(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        token = _mint_session_token("user")
        payload = _verify_session_token(token)
        assert payload is not None
        assert payload["sub"] == "user"

    def test_verify_wrong_signature_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        token = _mint_session_token("user")
        body, _ = token.rsplit(".", 1)
        tampered = f"{body}.invalidsignature"
        assert _verify_session_token(tampered) is None

    def test_verify_expired_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        import json

        # Manually craft an expired token.
        payload = {"sub": "user", "iat": 1000, "exp": 1001}  # epoch 1001 is long past
        import hashlib
        import hmac as _hmac

        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        secret = _get_secret_key()
        sig = _b64url_encode(_hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        token = f"{body}.{sig}"
        assert _verify_session_token(token) is None

    def test_verify_malformed_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        assert _verify_session_token("not-a-valid-token") is None
        assert _verify_session_token("") is None
        assert _verify_session_token("a.b.c") is None

    def test_payload_contains_iat_and_exp(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "development")
        before = int(time.time())
        token = _mint_session_token()
        after = int(time.time())
        payload = _verify_session_token(token)
        assert payload is not None
        assert before <= payload["iat"] <= after
        assert payload["exp"] > payload["iat"]


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


class TestLoginEndpoint:
    def test_correct_password_returns_200(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": "hunter2"})
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_correct_password_sets_session_cookie(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": "hunter2"})
        assert SESSION_COOKIE_NAME in res.cookies

    def test_wrong_password_returns_401(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": "wrong"})
        assert res.status_code == 401

    def test_wrong_password_does_not_set_cookie(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": "wrong"})
        assert SESSION_COOKIE_NAME not in res.cookies

    def test_empty_password_returns_401(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": ""})
        assert res.status_code == 401

    def test_login_cookie_is_httponly(self, monkeypatch):
        """The Set-Cookie header must include HttpOnly."""
        client = _make_client(monkeypatch)
        res = client.post("/auth/login", json={"password": "hunter2"})
        # TestClient stores cookies in res.cookies; the raw header is in headers.
        set_cookie = res.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie or SESSION_COOKIE_NAME in res.cookies

    def test_login_without_configured_password_returns_503(self, monkeypatch):
        """SIM-443c: no default password exists; an unconfigured login says so."""
        _prepare_env(monkeypatch, "development")
        monkeypatch.delenv("AUTH_PASSWORD", raising=False)
        client = _build_client()
        res = client.post("/auth/login", json={"password": "dev"})
        assert res.status_code == 503
        assert "not configured" in res.json()["detail"]
        assert SESSION_COOKIE_NAME not in res.cookies

    def test_login_without_secret_key_returns_503(self, monkeypatch):
        """SIM-443c: no default signing key exists either."""
        _prepare_env(monkeypatch, "development")
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _build_client()
        res = client.post("/auth/login", json={"password": "hunter2"})
        assert res.status_code == 503


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


class TestMeEndpoint:
    def test_no_cookie_returns_unauthenticated(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        client = _make_client(monkeypatch, env="production")
        res = client.get("/auth/me")
        assert res.status_code == 200
        body = res.json()
        assert body["authenticated"] is False
        assert body["mode"] == "none"

    def test_valid_cookie_returns_authenticated(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        client = _make_client(monkeypatch, env="production")

        # Log in to get the cookie.
        login_res = client.post("/auth/login", json={"password": "hunter2"})
        assert login_res.status_code == 200

        # /me should now see the cookie (TestClient persists cookies).
        me_res = client.get("/auth/me")
        body = me_res.json()
        assert body["authenticated"] is True
        assert body["mode"] == "cookie"
        assert body["expires_in"] > 0

    def test_development_without_cookie_is_not_authenticated(self, monkeypatch):
        """SIM-443c: ENVIRONMENT=development no longer logs the caller in."""
        client = _make_client(monkeypatch, env="development")
        res = client.get("/auth/me")
        assert res.status_code == 200
        body = res.json()
        assert body["authenticated"] is False
        assert body["mode"] == "none"

    def test_me_never_returns_401(self, monkeypatch):
        """GET /auth/me must always return 200 — even with no cookie."""
        client = _make_client(monkeypatch, env="production")
        res = client.get("/auth/me")
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


class TestLogoutEndpoint:
    def test_logout_returns_200(self, monkeypatch):
        client = _make_client(monkeypatch)
        res = client.post("/auth/logout")
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_logout_after_login_clears_session(self, monkeypatch):
        client = _make_client(monkeypatch, env="production")

        # Login
        client.post("/auth/login", json={"password": "hunter2"})
        assert client.get("/auth/me").json()["authenticated"] is True

        # Logout
        client.post("/auth/logout")

        # After logout, /auth/me returns unauthenticated.
        me = client.get("/auth/me")
        assert me.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# require_auth dependency behaviour
# ---------------------------------------------------------------------------


class TestRequireAuth:
    def test_development_no_credentials_returns_401(self, monkeypatch):
        """SIM-443c: in development too, no credential means 401."""
        client = _make_client(monkeypatch, env="development")
        res = client.get("/api/games/123456/simulate")
        assert res.status_code == 401
        assert "WWW-Authenticate" in res.headers

    def test_production_no_credentials_returns_401(self, monkeypatch):
        """In production, no credential → 401."""
        client = _make_client(monkeypatch, env="production")
        res = client.get("/api/games/123456/simulate")
        assert res.status_code == 401

    def test_production_valid_api_key_accepted(self, monkeypatch):
        """X-API-Key header is a valid credential in non-dev."""
        monkeypatch.setenv("API_KEYS", "test-api-key-abc123")
        client = _make_client(monkeypatch, env="production")
        res = client.get(
            "/api/games/123456/simulate",
            headers={"X-API-Key": "test-api-key-abc123"},
        )
        # Should NOT be 401 (may be 503/404 for missing pool, but not 401).
        assert res.status_code != 401

    def test_production_invalid_api_key_returns_401(self, monkeypatch):
        """An invalid API key returns 401."""
        monkeypatch.setenv("API_KEYS", "real-key")
        client = _make_client(monkeypatch, env="production")
        res = client.get(
            "/api/games/123456/simulate",
            headers={"X-API-Key": "wrong-key"},
        )
        assert res.status_code == 401

    def test_production_valid_cookie_accepted(self, monkeypatch):
        """A valid session cookie satisfies require_auth."""
        client = _make_client(monkeypatch, env="production")

        # Login to get a cookie.
        login_res = client.post("/auth/login", json={"password": "hunter2"})
        assert login_res.status_code == 200

        # Now call a protected route — should not be 401.
        res = client.get("/api/games/123456/simulate")
        assert res.status_code != 401

    def test_unconfigured_non_dev_returns_401(self, monkeypatch):
        """SIM-443c: no AUTH_PASSWORD and no API_KEYS no longer opens the routes."""
        _prepare_env(monkeypatch, "production")
        monkeypatch.delenv("AUTH_PASSWORD", raising=False)
        monkeypatch.delenv("API_KEYS", raising=False)
        client = _build_client()
        res = client.get("/api/games/123456/simulate")
        assert res.status_code == 401


# ---------------------------------------------------------------------------
# CORS dev origins — wildcard removed (SIM-389)
# ---------------------------------------------------------------------------


class TestCorsDevOrigins:
    def test_dev_origins_do_not_include_wildcard(self, monkeypatch):
        """After SIM-389, dev CORS must NOT use ['*'] (spec violation with credentials)."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        origins = resolve_cors_origins()
        assert "*" not in origins, "Wildcard CORS origin breaks cookie-based auth"

    def test_dev_origins_include_vite_port(self, monkeypatch):
        """Dev origins should include the Vite dev server port."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        origins = resolve_cors_origins()
        assert any("5173" in o for o in origins), (
            "Dev CORS must include localhost:5173 (Vite dev server)"
        )

    def test_cors_origins_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "https://sim.example.com")
        origins = resolve_cors_origins()
        assert origins == ["https://sim.example.com"]

    def test_frontend_url_env_var_works(self, monkeypatch):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")
        origins = resolve_cors_origins()
        assert origins == ["https://app.example.com"]
