"""
api/auth.py
===========
API security primitives for the MLB Simulation Platform (SIM-351, SIM-389, SIM-443c).

This module is dependency-free beyond FastAPI/Starlette (no ``slowapi``, no
Redis). It provides these seams:

  - ``require_auth``: a FastAPI dependency that accepts a valid session
    cookie, an ``X-API-Key`` header or an ``Authorization: Bearer <key>``
    header. A request with none of them gets ``401``. The gate is enforced in
    EVERY environment (SIM-443c): ``ENVIRONMENT`` no longer relaxes it. The
    one opt-out is an explicit ``AUTH_DISABLED=1`` (``_auth_disabled``); the
    app logs a warning at boot and ``/health`` reports ``auth_enforced``.
    Every data router carries this dependency at the router level.

  - ``authenticate_websocket``: the same check for the live WebSocket. A
    ``require_auth`` takes a Request, and FastAPI does not supply one in a
    WebSocket scope, so on the WebSocket router it would fail every handshake
    with a TypeError. That router therefore carries no router dependency, and
    the handler calls this first. A failed check closes the socket with code 1008 (policy
    violation) before the handshake is accepted.

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

``ENVIRONMENT`` still drives two things here: the cookie ``secure`` flag
(``cookie_kwargs``) and the CORS defaults (``resolve_cors_origins``).

Owned by Backend Developer (SIM-351).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

_FALSEY = ("0", "false", "no", "off", "")


def _is_development() -> bool:
    """True when ENVIRONMENT is development.

    SIM-443c: this no longer touches authentication. It drives the cookie
    ``secure`` flag and the CORS defaults only.
    """
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "development"


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse a boolean env var. Unset → ``default``; recognised falsey strings → False."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _auth_disabled() -> bool:
    """True when ``AUTH_DISABLED`` is set to a truthy value (SIM-443c).

    This is the ONE opt-out from authentication. Every site that skips the
    credential check reads this helper, so removing the opt-out later means
    deleting this helper and its call sites.
    """
    return _env_flag("AUTH_DISABLED", default=False)


def _configured_api_keys() -> set[str]:
    """Parse ``API_KEYS`` (comma-separated) into a set of non-empty keys.

    Read at request time (not import time) so tests can monkeypatch the env
    per-case and so a key rotation via container restart takes effect without
    re-import gymnastics.
    """
    raw = os.environ.get("API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


# ---------------------------------------------------------------------------
# Session token primitives (SIM-389)
#
# Stateless signed session tokens — a minimal JWT-equivalent backed by
# HMAC-SHA256 and stdlib only (no python-jose / PyJWT dependency).
#
# Wire format:  <base64url(json_payload)>.<base64url(hmac_sha256_sig)>
# Payload keys: sub (subject), iat (issued-at unix ts), exp (expiry unix ts).
#
# Environment variables consumed:
#   SECRET_KEY         — HMAC signing key. Required in every environment
#                        (SIM-443c); there is no default.
#   AUTH_PASSWORD      — The single shared login password for the browser UI.
#                        Required in every environment; there is no default.
#   SESSION_TTL_HOURS  — Cookie / token lifetime in hours (default: 8).
#
# validate_environment() (api/main.py) refuses to boot without the first two,
# unless AUTH_DISABLED=1. scripts/gen_secrets.py writes both into .env.
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "sim_session"
_GEN_SECRETS_HINT = "run: python scripts/gen_secrets.py --append .env"


def _session_ttl_seconds() -> int:
    """Session lifetime in seconds.  Reads SESSION_TTL_HOURS (default 8 h)."""
    try:
        return max(60, int(os.environ.get("SESSION_TTL_HOURS", "8")) * 3600)
    except (TypeError, ValueError):
        return 8 * 3600


def _get_secret_key() -> str:
    """HMAC signing key from the SECRET_KEY env var.

    Raises ``RuntimeError`` when the variable is blank. There is no default
    key (SIM-443c): a published default would let anyone mint a session.
    """
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(f"SECRET_KEY is not set - {_GEN_SECRETS_HINT}")
    return key


def _get_auth_password() -> str:
    """Shared browser login password from the AUTH_PASSWORD env var.

    Raises ``RuntimeError`` when the variable is blank. There is no default
    password (SIM-443c).
    """
    pw = os.environ.get("AUTH_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(f"AUTH_PASSWORD is not set - {_GEN_SECRETS_HINT}")
    return pw


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s.encode())


def _mint_session_token(subject: str = "user") -> str:
    """Create a signed session token for ``subject``.

    Returns a string of the form ``<b64url_payload>.<b64url_sig>`` that is safe
    to store in an httpOnly cookie.  The token is self-contained (no server-side
    session store) and expires after SESSION_TTL_HOURS hours.
    """
    now = int(time.time())
    payload = {"sub": subject, "iat": now, "exp": now + _session_ttl_seconds()}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url_encode(
        hmac.new(_get_secret_key().encode(), body.encode(), hashlib.sha256).digest()
    )
    return f"{body}.{sig}"


def _verify_session_token(token: str) -> dict | None:
    """Verify a session token string.  Returns the payload dict or None.

    Returns None for any of: malformed, wrong signature, expired.
    Never raises — callers treat None as "no valid session".
    """
    try:
        body, sig = token.rsplit(".", 1)
        expected_sig = _b64url_encode(
            hmac.new(_get_secret_key().encode(), body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload: dict = json.loads(_b64url_decode(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:  # noqa: BLE001
        return None


def cookie_kwargs(*, max_age: int | None = None) -> dict:
    """Build the kwargs to pass to ``response.set_cookie`` / ``response.delete_cookie``.

    ``secure`` is True only outside development (localhost http doesn't support Secure
    cookies and the Vite proxy handles the forwarding anyway).
    """
    ttl = max_age if max_age is not None else _session_ttl_seconds()
    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": not _is_development(),
        "samesite": "strict",
        "path": "/",
        "max_age": ttl,
    }


# ---------------------------------------------------------------------------
# The shared credential check (SIM-351, SIM-389, SIM-443c)
# ---------------------------------------------------------------------------

API_KEY_HEADER = "X-API-Key"
_BEARER_PREFIX = "bearer "
_WWW_AUTHENTICATE = 'Bearer realm="sim", ApiKey realm="X-API-Key", Cookie realm="sim_session"'
# The WebSocket close code for a refused credential (RFC 6455: policy violation).
WS_POLICY_VIOLATION = 1008


@dataclass(frozen=True)
class Principal:
    """The caller that a credential proves.

    ``mode`` is ``cookie`` (a valid session cookie) or ``api_key`` (a
    configured key in ``X-API-Key`` or ``Authorization: Bearer``).
    ``expires_at`` is the session's expiry as a unix time, or None for a key.
    """

    mode: str
    subject: str
    expires_at: int | None = None


def _presented_api_key(headers: Mapping[str, str]) -> str | None:
    """Return the API key that a request presents, or None.

    ``X-API-Key`` comes first. Otherwise an ``Authorization: Bearer <key>``
    header supplies the key (Prometheus sends its scrape token this way).
    """
    key = headers.get(API_KEY_HEADER)
    if key and key.strip():
        return key.strip()
    auth = headers.get("authorization")
    if auth and auth[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
        token = auth[len(_BEARER_PREFIX) :].strip()
        if token:
            return token
    return None


def _key_matches(presented: str, keys: set[str]) -> bool:
    """True when ``presented`` equals one configured key.

    The check compares against EVERY key with ``hmac.compare_digest`` and has
    no early exit, so its time does not tell which key matched.
    """
    presented_b = presented.encode()
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(presented_b, key.encode())
    return matched


def _authenticate(cookies: Mapping[str, str], headers: Mapping[str, str]) -> Principal | None:
    """Check the session cookie, then the API key. Never raises.

    Returns the ``Principal`` of the first valid credential, or None. A
    missing SECRET_KEY makes every cookie invalid; it does not raise.
    """
    raw_cookie = cookies.get(SESSION_COOKIE_NAME)
    if raw_cookie:
        payload = _verify_session_token(raw_cookie)
        if payload:
            exp = payload.get("exp")
            return Principal(
                mode="cookie",
                subject=str(payload.get("sub", "user")),
                expires_at=int(exp) if isinstance(exp, (int, float)) else None,
            )

    presented = _presented_api_key(headers)
    if presented:
        keys = _configured_api_keys()
        if keys and _key_matches(presented, keys):
            return Principal(mode="api_key", subject="key")
    return None


async def require_auth(request: Request) -> str | None:
    """FastAPI dependency: the caller must present a valid credential.

    Every data router carries this dependency (SIM-443c). It gates every
    route except ``/health``, ``/ready``, ``/``, ``/auth/*`` and the API docs.

    Accepted credentials (the first match wins):
      1. a valid ``sim_session`` httpOnly cookie -> returns "cookie:<sub>"
      2. a configured key in ``X-API-Key`` or ``Authorization: Bearer``
         -> returns "api_key:key" (the key itself never leaves this module)
    Otherwise the dependency raises 401 with a ``WWW-Authenticate`` header.

    ``AUTH_DISABLED=1`` is the one opt-out: the dependency then returns None
    and reads no credential. ``ENVIRONMENT`` plays no part.
    """
    if _auth_disabled():
        return None

    principal = _authenticate(request.cookies, request.headers)
    if principal is not None:
        return f"{principal.mode}:{principal.subject}"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication required. "
            "Log in via POST /auth/login to obtain a session cookie, "
            "or send a valid X-API-Key or Authorization: Bearer header."
        ),
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


async def authenticate_websocket(websocket: WebSocket) -> str | None:
    """Check the credential of a WebSocket handshake (SIM-443c).

    The check reads the same cookie and headers as ``require_auth``; a
    browser sends the session cookie on a same-origin handshake. On success
    the function returns the caller's label ("disabled" under
    ``AUTH_DISABLED=1``). On failure it closes the socket with code 1008
    BEFORE it accepts the socket (the server answers the handshake with 403)
    and returns None. The handler must then return at once.
    """
    if _auth_disabled():
        return "disabled"
    principal = _authenticate(websocket.cookies, websocket.headers)
    if principal is not None:
        return f"{principal.mode}:{principal.subject}"
    await websocket.close(code=WS_POLICY_VIOLATION)
    return None


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


# ---------------------------------------------------------------------------
# p95 latency middleware (SIM-410)
# ---------------------------------------------------------------------------


class LatencyMiddleware(BaseHTTPMiddleware):
    """ASGI timing middleware that computes a rolling p95 API latency (SIM-410).

    Records each non-exempt request's wall-clock response time in a fixed-size
    ring buffer (``maxlen=window_size``, default 200).  After every request the
    p95 of the current window is recomputed and stored on
    ``request.app.state.api_p95_seconds`` so the /metrics endpoint can expose
    the live p95 to Prometheus / Grafana — replacing the placeholder ``0``
    that existed before this middleware was registered.

    Exempt paths (``/health``, ``/ready``, ``/``, ``/metrics``) are never timed
    so probe loops and scrapes don't inflate the latency distribution.

    Sorting up to ``window_size`` floats on every request is O(N log N) where
    N ≤ 200, which benchmarks at < 20 µs — negligible vs any real handler.
    The ``deque(maxlen=...)`` ring buffer is not thread-safe under concurrent
    ``append`` from multiple async tasks, but Starlette dispatches middleware
    sequentially in the event loop so no locking is required here.
    """

    _EXEMPT_PATHS = frozenset({"/health", "/ready", "/", "/metrics"})

    def __init__(self, app, *, window_size: int = 200) -> None:
        super().__init__(app)
        self._window: deque[float] = deque(maxlen=window_size)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._EXEMPT_PATHS:
            return await call_next(request)

        t0 = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - t0

        self._window.append(elapsed)

        # SIM-410: feed the served-request counter via the /metrics seam, so
        # baseball_sim_requests_total reflects real traffic (was perpetually 0 —
        # the counter had no production caller). Local import avoids any import
        # cycle; record_request is best-effort and never raises.
        try:
            from api.routes.metrics import record_request

            record_request(request.app.state)
        except Exception:  # noqa: BLE001
            pass

        # Compute p95 from the window and stash it on app.state.
        w = self._window
        if len(w) >= 2:
            sorted_w = sorted(w)
            # 95th-percentile index: floor of 0.95 * (N-1), clamped.
            idx = min(int(0.95 * (len(sorted_w) - 1)), len(sorted_w) - 1)
            p95 = sorted_w[idx]
            try:
                request.app.state.api_p95_seconds = p95
            except Exception:  # noqa: BLE001
                pass

        return response


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
        # SIM-389: Use SPECIFIC dev origins instead of the previous wildcard.
        # allow_origins=["*"] + allow_credentials=True is a CORS spec violation
        # that browsers reject (they refuse to send cookies to a wildcard origin).
        # Specific origins allow credentials correctly; the Vite proxy at :5173
        # forwards cookies transparently to the API at :8000, so this is the right
        # pairing for cookie-based auth in development.
        return [
            "http://localhost:5173",  # Vite dev server (primary)
            "http://localhost:3000",  # alternative dev port
            "http://localhost:8000",  # direct API access in dev tools
        ]

    # Non-dev with nothing configured: do NOT fall back to "*". A locked-down
    # localhost default is the least-surprising safe choice; operators set
    # CORS_ORIGINS / FRONTEND_URL explicitly for real deployments.
    return ["http://localhost:3000"]
