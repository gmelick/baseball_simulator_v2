"""
api/routes/auth.py
==================
The browser session's login, status and logout.

The routes (the router has no prefix; they sit at /auth/*):

    POST /auth/login   — exchange the shared AUTH_PASSWORD for an httpOnly session cookie.
    GET  /auth/me      — probe the current session state (never raises 401; safe to call
                         on app boot to decide whether to show the login page).
    POST /auth/logout  — clear the session cookie.

The login issues a stateless signed session token in an httpOnly, Secure,
SameSite=strict cookie named ``sim_session``. The token is an HMAC-SHA256
signed payload (api/auth._mint_session_token). No session store is needed: the
signature proves this server issued the token with SECRET_KEY, and the exp
claim limits its life on every request.

In the Vite dev setup the browser talks to localhost:5173, and Vite proxies
/auth/* and /api/* to localhost:8000. The Set-Cookie header comes back through
the proxy, and the later requests carry the cookie through it.

The password: the shared AUTH_PASSWORD (required; no default).
The signing key: SECRET_KEY (required; no default).
The session's life: SESSION_TTL_HOURS (default 8 h, one workday).

A server with no AUTH_PASSWORD or SECRET_KEY answers POST /auth/login with 503
("Login is not configured on this server"), not a 500.

Programmatic API consumers (scripts, Prometheus, Grafana) use an X-API-Key or
an Authorization: Bearer header; the require_auth dependency accepts either.
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.requests import Request
from pydantic import BaseModel

from api.auth import (
    _auth_disabled,
    _authenticate,
    _get_auth_password,
    _get_secret_key,
    _mint_session_token,
    _session_ttl_seconds,
    cookie_kwargs,
)

LOGIN_NOT_CONFIGURED_DETAIL = "Login is not configured on this server."

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
    # "cookie"   — a valid httpOnly session cookie is present
    # "api_key"  — a valid X-API-Key or Authorization: Bearer key is present
    # "disabled" — AUTH_DISABLED=1, so the server enforces no login
    # "none"     — no valid credential was found
    # ``authenticated`` is true exactly when require_auth would pass.
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
        "Returns 401 on an incorrect password, and 503 when the server has no "
        "AUTH_PASSWORD or SECRET_KEY."
    ),
    status_code=200,
)
async def login(body: LoginRequest, response: Response) -> dict:
    """Validate the shared password and set the session cookie."""
    try:
        expected = _get_auth_password()
        _get_secret_key()
    except RuntimeError:
        # There is no default password or key. A server without them cannot
        # issue a session, so it says so plainly.
        log.error("Login refused: AUTH_PASSWORD or SECRET_KEY is not set.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LOGIN_NOT_CONFIGURED_DETAIL,
        ) from None

    # Constant-time comparison to resist timing attacks.
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        log.warning("Failed login attempt.")
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
    if _auth_disabled():
        return AuthStatusResponse(
            authenticated=True,
            mode="disabled",
            expires_in=_session_ttl_seconds(),
        )

    principal = _authenticate(request.cookies, request.headers)
    if principal is None:
        return AuthStatusResponse(authenticated=False, mode="none", expires_in=0)
    remaining = 0
    if principal.expires_at is not None:
        remaining = max(0, int(principal.expires_at - time.time()))
    # An API key does not expire, so its ``expires_in`` stays 0.
    return AuthStatusResponse(authenticated=True, mode=principal.mode, expires_in=remaining)


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
