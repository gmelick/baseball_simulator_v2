"""
test_api_errors_sim416.py
=========================
Unit tests for the SIM-416 app-level exception handler / structured error
envelope. Builds a tiny FastAPI app and installs the same handler used by
``create_app`` (via ``api.errors.install_exception_handlers``) so the behaviour
is exercised without the DB-dependent lifespan.

TestClient is built with ``raise_server_exceptions=False`` so the handler's
JSON response is returned instead of the exception being re-raised into the
test (the default test behaviour).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.errors import install_exception_handlers


def _build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("kaboom-secret-internal-detail")

    @app.get("/http-error")
    async def http_error() -> dict:
        raise HTTPException(status_code=404, detail="not found here")

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    return app


def test_unhandled_exception_returns_structured_envelope():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_type"] == "internal_error"
    assert body["detail"] == "Internal server error."
    # request_id is a 12-char hex correlation id.
    assert isinstance(body["request_id"], str) and len(body["request_id"]) == 12
    int(body["request_id"], 16)  # parses as hex


def test_internal_message_is_not_leaked():
    """The raw exception message must never reach the client."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert "kaboom-secret-internal-detail" not in resp.text
    assert "RuntimeError" not in resp.text


def test_httpexception_shape_is_unchanged():
    """HTTPException keeps the FastAPI default {detail} shape (frontend reads it)."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/http-error")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"detail": "not found here"}
    assert "error_type" not in body  # catch-all envelope NOT applied to HTTPException


def test_normal_route_unaffected():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/ok")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
