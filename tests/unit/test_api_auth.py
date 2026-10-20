"""The CORS allowlist and the latency middleware of ``api/auth.py``.

- ``resolve_cors_origins``: CORS_ORIGINS first, then FRONTEND_URL, then the named
  dev origins, then one localhost origin. No default is the wildcard ``"*"``.
- ``LatencyMiddleware``: the p95 latency that it writes on ``app.state``.
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
    """In development, CORS uses named origins, never the wildcard.

    Browsers reject allow_origins=["*"] with allow_credentials=True. Named origins
    let the session cookie through.
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
    """Outside development, with nothing configured, the allowlist is never "*"."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    origins = resolve_cors_origins()
    assert "*" not in origins


# ---------------------------------------------------------------------------
# LatencyMiddleware — the p95 latency of the API
# ---------------------------------------------------------------------------


def _latency_app() -> FastAPI:
    """A small app with LatencyMiddleware, one timed route and one exempt route."""
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
    """The middleware sets app.state.api_p95_seconds after two timed requests."""
    app = _latency_app()
    client = TestClient(app)
    # The percentile needs at least two samples in the window.
    client.get("/ping")
    client.get("/ping")
    p95 = getattr(app.state, "api_p95_seconds", None)
    assert p95 is not None, "LatencyMiddleware did not set app.state.api_p95_seconds"
    assert isinstance(p95, float)
    assert p95 >= 0.0, f"p95 must be non-negative, got {p95}"


def test_latency_middleware_p95_grows_with_more_requests():
    """The middleware recomputes the p95 after each timed request."""
    app = _latency_app()
    client = TestClient(app)
    for _ in range(10):
        client.get("/ping")
    p95 = getattr(app.state, "api_p95_seconds", None)
    assert p95 is not None
    assert p95 >= 0.0


def test_latency_middleware_exempt_paths_do_not_populate_p95():
    """An exempt path, such as /health, does not feed the p95."""
    app = _latency_app()
    client = TestClient(app)
    # Only the exempt /health path runs, so the p95 stays unset.
    client.get("/health")
    client.get("/health")
    p95 = getattr(app.state, "api_p95_seconds", None)
    # The window holds no sample, so the middleware writes no p95.
    assert p95 is None, f"exempt path should not populate api_p95_seconds, got {p95}"
