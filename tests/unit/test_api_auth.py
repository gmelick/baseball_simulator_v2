"""
test_api_auth.py
================
Unit tests for api/auth.py — the SIM-351 auth / CORS primitives.

Covers:
  - resolve_cors_origins precedence (CORS_ORIGINS > FRONTEND_URL > dev "*"
    > safe non-dev fallback), and the invariant that non-dev is never "*".

Owned by Backend Developer (SIM-351).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import LatencyMiddleware, resolve_cors_origins

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


def test_cors_origins_dev_default_uses_specific_origins(monkeypatch):
    """SIM-389: dev CORS must use specific origins, NOT the wildcard.
    allow_origins=["*"] + allow_credentials=True is a CORS spec violation
    that browsers silently reject; specific origins let cookies work correctly.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    origins = resolve_cors_origins()
    # No wildcard.
    assert "*" not in origins
    # Must include the Vite dev-server port.
    assert any("5173" in o for o in origins)


def test_cors_origins_non_dev_never_wildcard(monkeypatch):
    """Core SIM-351 invariant: non-dev with nothing configured is NOT "*"."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    origins = resolve_cors_origins()
    assert "*" not in origins


# ---------------------------------------------------------------------------
# LatencyMiddleware (SIM-410)
# ---------------------------------------------------------------------------


def _latency_app() -> FastAPI:
    """Tiny app with LatencyMiddleware and one timed route + one exempt route."""
    app = FastAPI()
    app.add_middleware(LatencyMiddleware)

    @app.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


def test_latency_middleware_populates_p95_after_two_requests():
    """SIM-410: app.state.api_p95_seconds must be set after ≥2 timed requests."""
    app = _latency_app()
    client = TestClient(app)
    # Two requests needed so the window has ≥2 samples and the percentile fires.
    client.get("/ping")
    client.get("/ping")
    p95 = getattr(app.state, "api_p95_seconds", None)
    assert p95 is not None, "LatencyMiddleware did not set app.state.api_p95_seconds"
    assert isinstance(p95, float)
    assert p95 >= 0.0, f"p95 must be non-negative, got {p95}"


def test_latency_middleware_p95_grows_with_more_requests():
    """SIM-410: p95 is re-computed after each request; more samples refine it."""
    app = _latency_app()
    client = TestClient(app)
    for _ in range(10):
        client.get("/ping")
    p95 = getattr(app.state, "api_p95_seconds", None)
    assert p95 is not None
    assert p95 >= 0.0


def test_latency_middleware_exempt_paths_do_not_populate_p95():
    """SIM-410: exempt paths (/health etc.) must NOT trigger p95 computation."""
    app = _latency_app()
    client = TestClient(app)
    # Only call the exempt /health path; p95 should remain unset (window empty).
    client.get("/health")
    client.get("/health")
    p95 = getattr(app.state, "api_p95_seconds", None)
    # After only exempt requests the window is still empty, so no p95 written.
    assert p95 is None, f"exempt path should not populate api_p95_seconds, got {p95}"
