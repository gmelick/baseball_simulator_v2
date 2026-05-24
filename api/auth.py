"""
api/auth.py
===========
Baseline API security primitives for the MLB Simulation Platform (SIM-351).

This module is intentionally dependency-free beyond FastAPI/Starlette (no
``slowapi``, no Redis). It provides two seams Phase-5 routers can attach:

  - ``require_api_key`` — a FastAPI dependency that validates an ``X-API-Key``
    request header against a comma-separated ``API_KEYS`` env var. In
    ``development`` env, or when no keys are configured, it is a no-op
    pass-through so local dev + the test suite work without ceremony. In any
    non-dev environment with keys configured, a missing/invalid key yields
    ``401``.

  - ``RateLimitMiddleware`` — a pure-stdlib in-memory sliding-window limiter
    keyed by API key (falling back to client IP). Defaults to
    ``RATE_LIMIT_PER_MINUTE`` requests/min and returns ``429`` when exceeded.
    Disabled when ``RATE_LIMIT_ENABLED`` is false or the per-minute limit is
    ``<= 0`` (the test/dev default), so it never interferes with the suite
    unless a test explicitly opts in.

Design notes
------------
The in-memory limiter is process-local and therefore correct only for a
single worker. That is deliberate for a *baseline*: it gives us a real 429
contract and a tested seam without pulling in Redis. A multi-worker
production deployment should swap the backing store for the shared Redis
cache (the same one ``api/state.py`` already opens) — the middleware's
public surface (constructor knobs + ``_client_key`` seam) is written so that
swap is a body change, not an interface change.

Owned by Backend Developer (SIM-351).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

_FALSEY = ("0", "false", "no", "off", "")


def _is_development() -> bool:
    """True when ENVIRONMENT is development (the auth/rate-limit relaxed mode)."""
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "development"


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse a boolean env var. Unset → ``default``; recognised falsey strings → False."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _configured_api_keys() -> set[str]:
    """Parse ``API_KEYS`` (comma-separated) into a set of non-empty keys.

    Read at request time (not import time) so tests can monkeypatch the env
    per-case and so a key rotation via container restart takes effect without
    re-import gymnastics.
    """
    raw = os.environ.get("API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


# ---------------------------------------------------------------------------
# API-key auth dependency (SIM-351)
# ---------------------------------------------------------------------------

API_KEY_HEADER = "X-API-Key"


async def require_api_key(request: Request) -> str | None:
    """FastAPI dependency enforcing ``X-API-Key`` on a protected route.

    Behaviour matrix:
      - development env                         → pass-through (returns None)
      - non-dev env AND no API_KEYS configured  → pass-through (returns None)
      - non-dev env AND keys configured:
            valid X-API-Key   → returns the matched key
            missing/invalid   → raises 401

    Phase-5 routers attach this via ``dependencies=[Depends(require_api_key)]``
    on the router or ``Depends(require_api_key)`` on individual handlers. It is
    deliberately NOT applied to ``/health`` / ``/ready`` / ``/`` so liveness and
    readiness probes never require a credential.
    """
    keys = _configured_api_keys()

    # Relaxed mode: dev env, or no keys configured anywhere → no-op.
    if _is_development() or not keys:
        return None

    presented = request.headers.get(API_KEY_HEADER)
    if presented and presented in keys:
        return presented

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid API key.",
        headers={"WWW-Authenticate": API_KEY_HEADER},
    )


# ---------------------------------------------------------------------------
# Rate-limit middleware (SIM-351)
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window rate limiter (pure stdlib).

    Keyed by API key when present (so a single shared NAT'd IP with distinct
    keys isn't collectively throttled), else by client IP. Each key gets a
    ``deque`` of recent request timestamps; on every request we evict
    timestamps older than ``window_s`` and reject with ``429`` once the deque
    length reaches ``limit``.

    Constructor knobs (all overridable for tests; defaults read from env):
      limit     — max requests per window (``RATE_LIMIT_PER_MINUTE``)
      window_s  — window length in seconds (default 60)
      enabled   — master switch (``RATE_LIMIT_ENABLED``)

    A ``limit <= 0`` (the default when the env var is unset) means "no limit",
    which is the safe default for dev + the test suite. Tests opt in by setting
    a tiny ``RATE_LIMIT_PER_MINUTE`` before building the app.

    Exempt paths (``/health``, ``/ready``, ``/``) are never throttled so an
    over-eager k8s probe loop can't trip the limiter and flap the pod.
    """

    _EXEMPT_PATHS = frozenset({"/health", "/ready", "/"})

    def __init__(
        self,
        app,
        *,
        limit: int | None = None,
        window_s: int = 60,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(app)
        # Resolve config at construction time. ``create_app`` builds the app
        # (and thus this middleware) per-process, and the test suite rebuilds
        # the app per-case, so reading env here gives each app its own knobs.
        if limit is None:
            try:
                limit = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "0"))
            except ValueError:
                limit = 0
        self._limit = limit
        self._window_s = window_s
        if enabled is None:
            # Enabled only when a positive limit is configured AND not explicitly
            # disabled. Unset RATE_LIMIT_ENABLED defaults to True so a configured
            # limit "just works", but a non-positive limit short-circuits to off.
            enabled = _env_flag("RATE_LIMIT_ENABLED", default=True) and limit > 0
        self._enabled = enabled
        # client-key -> deque[timestamp]
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _client_key(request: Request) -> str:
        """Bucket key: API key if presented, else client IP, else 'unknown'."""
        api_key = request.headers.get(API_KEY_HEADER)
        if api_key:
            return f"key:{api_key}"
        client = request.client
        ip = client.host if client else "unknown"
        return f"ip:{ip}"

    async def dispatch(self, request: Request, call_next):
        if not self._enabled or self._limit <= 0:
            return await call_next(request)

        if request.url.path in self._EXEMPT_PATHS:
            return await call_next(request)

        now = time.monotonic()
        window_start = now - self._window_s
        bucket = self._hits[self._client_key(request)]

        # Evict timestamps that have aged out of the window.
        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= self._limit:
            retry_after = max(1, int(self._window_s - (now - bucket[0])))
            return JSONResponse(
                {
                    "detail": "Rate limit exceeded. Slow down.",
                    "limit_per_window": self._limit,
                    "window_seconds": self._window_s,
                },
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        return await call_next(request)


# ---------------------------------------------------------------------------
# CORS allowlist (SIM-351)
# ---------------------------------------------------------------------------


def resolve_cors_origins() -> list[str]:
    """Resolve the CORS allowlist from the environment.

    Precedence:
      1. ``CORS_ORIGINS`` (comma-separated) if set — used verbatim.
      2. else ``FRONTEND_URL`` if set.
      3. else, ONLY in development, a permissive localhost dev default.
      4. else (non-dev, nothing configured) a safe localhost fallback.

    Production must never end up as ``["*"]`` while ``allow_credentials=True``
    (a CORS spec violation browsers reject anyway), so the wildcard is reachable
    only under ENVIRONMENT=development.
    """
    raw = os.environ.get("CORS_ORIGINS")
    if raw and raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]

    frontend = os.environ.get("FRONTEND_URL")
    if frontend and frontend.strip():
        return [frontend.strip()]

    if _is_development():
        # Local dev convenience: allow the typical Vite/CRA dev origins plus
        # the wildcard so a developer hitting the API from any localhost port
        # isn't blocked. Never used in non-dev (see below).
        return ["*"]

    # Non-dev with nothing configured: do NOT fall back to "*". A locked-down
    # localhost default is the least-surprising safe choice; operators set
    # CORS_ORIGINS / FRONTEND_URL explicitly for real deployments.
    return ["http://localhost:3000"]
