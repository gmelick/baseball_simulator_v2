"""
api/routes/auth.py — SIM-389
=============================
Browser session auth endpoints.

Routes (no prefix from the router — registered directly at /auth/*):

    POST /auth/login   — exchange the shared AUTH_PASSWORD for an httpOnly session cookie.
    GET  /auth/me      — probe the current session state (never raises 401; safe to call
                         on app boot to decide whether to show the login page).
    POST /auth/logout  — clear the session cookie.

Design notes
------------
Authentication uses a stateless signed session token stored in an httpOnly;
Secure; SameSite=strict cookie named ``sim_session``.  The token is an
HMAC-SHA256-signed payload (see api/auth._mint_session_token).  No server-side
session store (Redis / DB) is required — the HMAC signature proves the token
was issued by THIS server using SECRET_KEY; the exp claim enforces expiry
client-side on every request.

In the Vite dev setup the browser talks to localhost:5173; Vite proxies /auth/*
to localhost:8000.  The Set-Cookie response header flows back through the proxy
to the browser.  Subsequent requests to localhost:5173/api/* include the cookie,
which Vite also proxies (with the Cookie header) to localhost:8000 — the server
validates it as normal.

Auth password: the shared AUTH_PASSWORD env var (dev default: "dev").
Signing key:   the SECRET_KEY env var (dev default: logged warning + sentinel).
Session TTL:   SESSION_TTL_HOURS (default 8 h = one workday).

Programmatic API consumers (scripts, Prometheus, Grafana) continue to use
X-API-Key headers — the require_auth dependency accepts either form.

Owner: Backend Developer (SIM-389).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from fastapi.requests import Request
from pydantic import BaseModel

from api.auth import (
    SESSION_COOKIE_NAME,
    _get_auth_password,
    _is_development,
    _mint_session_token,
    _session_ttl_seconds,
    _verify_session_token,
    cookie_kwargs,
)

log = logging.getLogger("api.routes.auth")

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


class AuthStatusResponse(BaseModel):
    """Response for GET /auth/me — always 200, never 401."""

    authenticated: bool
    # "session"        — valid httpOnly cookie present
    # "api_key"        — valid X-API-Key header present
    # "dev_bypass"     — ENVIRONMENT=development, no auth enforced
    # "unauthenticated"— no valid credential found
    mode: str
    # Seconds until the current session expires (0 when not authenticated).
    expires_in: int


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@router.post(
    "/auth/login",
    summary="Exchange password for session cookie",
    description=(
        "Validate the shared AUTH_PASSWORD and issue an httpOnly session cookie "
        "(sim_session). The cookie is Secure + SameSite=strict in non-development "
        "environments. Session lifetime is SESSION_TTL_HOURS (default 8 hours). "
        "Returns 401 on an incorrect password."
    ),
    status_code=200,
)
async def login(body: LoginRequest, response: Response) -> dict:
    """Validate the shared password and set the session cookie."""
    expected = _get_auth_password()

    # Constant-time comparison to resist timing attacks.
    import hmac as _hmac

    if not _hmac.compare_digest(body.password, expected):
        log.warning("Failed login attempt.")
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password.",
        )

    token = _mint_session_token(subject="user")
    kwargs = cookie_kwargs()
    response.set_cookie(value=token, **kwargs)

    log.info("Login successful — session cookie issued (TTL=%ds).", _session_ttl_seconds())
    return {"ok": True, "message": "Logged in."}


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


@router.get(
    "/auth/me",
    response_model=AuthStatusResponse,
    summary="Check current session state",
    description=(
        "Probe whether the caller has a valid session.  Always returns 200 "
        "(never 401) so the frontend can call this on boot to decide whether to "
        "show the login page without triggering an error flow."
    ),
)
async def me(request: Request) -> AuthStatusResponse:
    """Return the caller's auth status without enforcing authentication."""
    # Dev bypass.
    if _is_development():
        return AuthStatusResponse(
            authenticated=True,
            mode="dev_bypass",
            expires_in=_session_ttl_seconds(),
        )

    # Check session cookie.
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_cookie:
        payload = _verify_session_token(raw_cookie)
        if payload:
            remaining = max(0, int(payload.get("exp", 0) - __import__("time").time()))
            return AuthStatusResponse(
                authenticated=True,
                mode="session",
                expires_in=remaining,
            )

    # Check API key (so programmatic callers that have a key also see "authenticated").
    from api.auth import API_KEY_HEADER, _configured_api_keys

    keys = _configured_api_keys()
    presented = request.headers.get(API_KEY_HEADER)
    if keys and presented and presented in keys:
        return AuthStatusResponse(
            authenticated=True,
            mode="api_key",
            expires_in=0,  # API keys don't expire
        )

    return AuthStatusResponse(authenticated=False, mode="unauthenticated", expires_in=0)


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/auth/logout",
    summary="Clear the session cookie",
    description="Expire the sim_session cookie.  Always returns 200 (idempotent).",
    status_code=200,
)
async def logout(response: Response) -> dict:
    """Clear the session cookie by setting max_age=0."""
    kwargs = cookie_kwargs(max_age=0)
    response.delete_cookie(**{k: v for k, v in kwargs.items() if k != "max_age"})
    log.info("Logout — session cookie cleared.")
    return {"ok": True, "message": "Logged out."}
