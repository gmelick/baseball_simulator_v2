"""
test_api_auth.py
================
Unit tests for api/auth.py — the SIM-351 auth / CORS primitives.

Covers:
  - require_api_key dependency: dev pass-through, no-keys pass-through,
    non-dev rejects missing/invalid (401), non-dev accepts valid key.
  - resolve_cors_origins precedence (CORS_ORIGINS > FRONTEND_URL > dev "*"
    > safe non-dev fallback), and the invariant that non-dev is never "*".

The dependency reads a Request header, so we exercise it through a tiny
FastAPI app with a route protected by Depends(require_api_key) — that mirrors
exactly how Phase-5 routers will attach it.

Owned by Backend Developer (SIM-351).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.auth import API_KEY_HEADER, require_api_key, resolve_cors_origins

# ---------------------------------------------------------------------------
# require_api_key — via a protected route
# ---------------------------------------------------------------------------


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict:
        return {"ok": True}

    return app


def test_dev_env_is_passthrough(monkeypatch):
    """In development env, auth is a no-op even with keys configured."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("API_KEYS", "secret-key")
    client = TestClient(_protected_app())
    # No header presented, yet dev bypass lets it through.
    assert client.get("/protected").status_code == 200


def test_non_dev_no_keys_is_passthrough(monkeypatch):
    """Non-dev but no API_KEYS configured → pass-through (nothing to enforce)."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEYS", raising=False)
    client = TestClient(_protected_app())
    assert client.get("/protected").status_code == 200


def test_non_dev_missing_key_rejected(monkeypatch):
    """Non-dev + keys configured + no X-API-Key header → 401."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEYS", "secret-key")
    client = TestClient(_protected_app())
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_non_dev_invalid_key_rejected(monkeypatch):
    """Non-dev + keys configured + wrong key → 401."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEYS", "secret-key")
    client = TestClient(_protected_app())
    resp = client.get("/protected", headers={API_KEY_HEADER: "wrong"})
    assert resp.status_code == 401


def test_non_dev_valid_key_accepted(monkeypatch):
    """Non-dev + a valid key from the comma-separated allowlist → 200."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEYS", "key-a, key-b , key-c")
    client = TestClient(_protected_app())
    resp = client.get("/protected", headers={API_KEY_HEADER: "key-b"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# resolve_cors_origins — precedence + invariants
# ---------------------------------------------------------------------------


def test_cors_origins_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example.com,https://b.example.com")
    monkeypatch.setenv("FRONTEND_URL", "https://ignored.example.com")
    assert resolve_cors_origins() == ["https://a.example.com", "https://b.example.com"]


def test_cors_origins_falls_back_to_frontend_url(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")
    assert resolve_cors_origins() == ["https://app.example.com"]


def test_cors_origins_dev_default_allows_wildcard(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    assert resolve_cors_origins() == ["*"]


def test_cors_origins_non_dev_never_wildcard(monkeypatch):
    """Core SIM-351 invariant: non-dev with nothing configured is NOT "*"."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    origins = resolve_cors_origins()
    assert "*" not in origins
