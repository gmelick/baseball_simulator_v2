"""
api/auth.py
===========
The API's security primitives: the login check, the rate limit, the
latency window and the CORS allowlist.

The module needs nothing beyond FastAPI and Starlette (no ``slowapi``, no
Redis). It provides:

  - ``require_auth``: a FastAPI dependency that accepts a valid session
    cookie, an ``X-API-Key`` header or an ``Authorization: Bearer <key>``
    header. A request with none of them gets ``401``. The check holds in
    every environment (the login on every route, SIM-443c); ``ENVIRONMENT``
    plays no part. The one opt-out is an explicit ``AUTH_DISABLED=1``
    (``_auth_disabled``); the app logs a warning at boot and ``/health``
    reports ``auth_enforced``. Every data router carries this dependency at
    the router level.

  - ``authenticate_websocket``: the same check for the live WebSocket. A
    ``require_auth`` takes a Request, and FastAPI does not supply one in a
    WebSocket scope, so on the WebSocket router it would fail every handshake
    with a TypeError. That router therefore carries no router dependency, and
    the handler calls this first. A failed check closes the socket with code 1008 (policy
    violation) before the handshake is accepted.

  - ``RateLimitMiddleware``: an in-memory sliding-window limiter, standard
    library only. It counts a request against a configured API key when the
    request presents one, else against the client address (the rate
    limiter's bucket, SIM-564). Behind a proxy named in
    ``TRUSTED_PROXY_HOSTS`` the client address is the one the proxy sends in
    ``X-Real-IP`` (the trusted proxy, SIM-569). The limit is
    ``RATE_LIMIT_PER_MINUTE`` requests a minute; past it the middleware
    answers ``429``. It is off when ``RATE_LIMIT_ENABLED`` is false or the
    limit is ``<= 0`` (the dev and test default).

  - ``LatencyMiddleware``: the rolling p95 latency that /metrics reports.

  - ``resolve_cors_origins``: the CORS allowlist from the environment.

The in-memory limiter is local to one process, so it is right for one worker
only. A deployment with several workers would move its store to the shared
Redis cache; the middleware's surface (the constructor's knobs and the
``_client_key`` seam) lets that change stay inside the class.

``ENVIRONMENT`` drives two things here: the cookie's ``secure`` flag
(``cookie_kwargs``) and the CORS default (``resolve_cors_origins``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------

_FALSEY = ("0", "false", "no", "off", "")


def _is_development() -> bool:
    """True when ENVIRONMENT is development.

    It drives the cookie's ``secure`` flag and the CORS default only, never the
    login check.
    """
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "development"


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse an on/off environment variable.

    Unset gives ``default``; one of :data:`_FALSEY` gives False; any other
    value gives True.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _auth_disabled() -> bool:
    """True when ``AUTH_DISABLED`` is set to a value that is not off.

    This is the one opt-out from the login check. Every site that skips the
    check reads this helper, so removing the opt-out means deleting this helper
    and its call sites.
    """
    return _env_flag("AUTH_DISABLED", default=False)


def _configured_api_keys() -> set[str]:
    """Parse ``API_KEYS`` (comma-separated) into a set of non-empty keys.

    It reads the variable on each call, not at import, so a test can set the
    environment per case and a key rotation takes effect at a container
    restart.
    """
    raw = os.environ.get("API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


# ---------------------------------------------------------------------------
# The session token
#
# A stateless signed session token: a minimal JWT equivalent on HMAC-SHA256,
# standard library only (no python-jose, no PyJWT).
#
# Wire format:  <base64url(json_payload)>.<base64url(hmac_sha256_sig)>
# Payload keys: sub (subject), iat (issued at, Unix time), exp (expiry, Unix time).
#
# The environment variables it reads:
#   SECRET_KEY         — the HMAC signing key. Required; there is no default.
#   AUTH_PASSWORD      — the one shared login password of the browser UI.
#                        Required; there is no default.
#   SESSION_TTL_HOURS  — the cookie and token lifetime in hours (default 8).
#
# validate_environment() (api/env_check.py) refuses to boot without the first
# two, unless AUTH_DISABLED=1. scripts/gen_secrets.py writes both into .env.
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
    key: a published default would let anyone mint a session.
    """
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(f"SECRET_KEY is not set - {_GEN_SECRETS_HINT}")
    return key


def _get_auth_password() -> str:
    """Shared browser login password from the AUTH_PASSWORD env var.

    Raises ``RuntimeError`` when the variable is blank. There is no default
    password.
    """
    pw = os.environ.get("AUTH_PASSWORD", "").strip()
    if not pw:
        raise RuntimeError(f"AUTH_PASSWORD is not set - {_GEN_SECRETS_HINT}")
    return pw


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 with the ``=`` padding removed."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    """Decode :func:`_b64url_encode` text, restoring its padding."""
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

    ``secure`` is True only outside development: localhost over http cannot
    carry a Secure cookie, and the Vite proxy forwards the cookie anyway.
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
# The shared credential check
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

    Every data router carries this dependency. It gates every route except
    ``/health``, ``/ready``, ``/``, ``/auth/*`` and the API docs.

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
    """Check the credential of a WebSocket handshake.

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
# The client address behind a trusted proxy
# ---------------------------------------------------------------------------

TRUSTED_PROXY_HOSTS_ENV = "TRUSTED_PROXY_HOSTS"
REAL_IP_HEADER = "X-Real-IP"
#: How long a resolved list of proxy addresses stays valid, in seconds.
_PROXY_RESOLVE_TTL_S = 60.0
#: The one cached resolution: the raw variable -> (expiry, addresses).
_proxy_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _resolve_proxy_host(name: str) -> set[str]:
    """Return every address ``name`` resolves to, or an empty set.

    A name that does not resolve adds no address and raises nothing. Inside
    the compose network Docker's embedded DNS answers from its own table, so
    the call returns at once.
    """
    try:
        return {str(info[4][0]) for info in socket.getaddrinfo(name, None)}
    except OSError:
        return set()


def _trusted_proxy_addresses() -> frozenset[str]:
    """Return the addresses of the proxies that ``TRUSTED_PROXY_HOSTS`` names.

    The variable is a comma-separated list of host names or addresses. Empty,
    the default, trusts nothing. The names resolve at most once per
    ``_PROXY_RESOLVE_TTL_S``: a recreated nginx with a new address is trusted
    again within a minute, and no request in between pays a lookup.
    """
    spec = os.environ.get(TRUSTED_PROXY_HOSTS_ENV, "").strip()
    if not spec:
        return frozenset()
    now = time.monotonic()
    cached = _proxy_cache.get(spec)
    if cached is not None and cached[0] > now:
        return cached[1]
    addresses: set[str] = set()
    for name in spec.split(","):
        name = name.strip()
        if name:
            addresses |= _resolve_proxy_host(name)
    result = frozenset(addresses)
    _proxy_cache.clear()
    _proxy_cache[spec] = (now + _PROXY_RESOLVE_TTL_S, result)
    return result


def _reset_trusted_proxy_cache() -> None:
    """Forget the cached resolution (the tests call this)."""
    _proxy_cache.clear()


def _client_address(request: Request) -> str:
    """Return the address a request comes from.

    The connection's own address is the answer, with one exception. When the
    connection comes from a trusted proxy, the answer is the address in the
    proxy's ``X-Real-IP`` header. nginx sets that header to the connection's
    address on every request it forwards, so a value a browser sends never
    reaches the app. A request with no connection address reads ``unknown``.
    """
    client = request.client
    host = client.host if client else None
    if host and host in _trusted_proxy_addresses():
        real = request.headers.get(REAL_IP_HEADER)
        if real and real.strip():
            return real.strip()
    return host or "unknown"


# ---------------------------------------------------------------------------
# The rate limit
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window rate limiter (standard library only).

    The limiter counts a request against a configured API key when the
    request presents one, else against the client address.
    Callers with distinct configured API keys behind one shared address keep
    separate buckets. Each bucket holds a ``deque`` of recent request
    timestamps. On every request the limiter evicts timestamps older than
    ``window_s``. It answers ``429`` once the deque length reaches ``limit``.

    The constructor's knobs (a test can set each; the defaults come from the
    environment):
      limit     — the most requests per window (``RATE_LIMIT_PER_MINUTE``)
      window_s  — the window's length in seconds (default 60)
      enabled   — the on/off switch (``RATE_LIMIT_ENABLED``)

    A ``limit <= 0`` (the default when the variable is unset) means no limit,
    the default for a dev stack and the tests. A test opts in with a small
    ``RATE_LIMIT_PER_MINUTE`` before it builds the app.

    The exempt paths (``/health``, ``/ready``, ``/``) are never throttled, so
    a busy probe loop cannot trip the limiter.
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
        # The knobs resolve at construction. ``create_app`` builds the app (and
        # this middleware) once per process, and a test rebuilds the app per
        # case, so each app reads its own environment.
        if limit is None:
            try:
                limit = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "0"))
            except ValueError:
                limit = 0
        self._limit = limit
        self._window_s = window_s
        if enabled is None:
            # On only with a positive limit and no explicit off. An unset
            # RATE_LIMIT_ENABLED counts as on, so a configured limit works on
            # its own; a limit <= 0 turns the limiter off.
            enabled = _env_flag("RATE_LIMIT_ENABLED", default=True) and limit > 0
        self._enabled = enabled
        # bucket name -> deque[timestamp]
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def _client_key(request: Request) -> str:
        """Return the bucket name of a request.

        The limiter reads the key the same way the login does. A request
        that presents a configured API key counts against that key. The
        bucket name is then ``key:`` plus 16 hex characters of the key's
        SHA-256 digest. The bucket name never holds the text of the key.

        Every other request counts against the client address: no key, a
        made-up key, or no configured keys. Behind a trusted proxy the client
        address is the real client's (``_client_address``). A request
        with no client address counts against the bucket named ``ip:unknown``.
        The limiter never uses the raw header text, so a made-up key cannot
        open a new bucket.
        """
        presented = _presented_api_key(request.headers)
        if presented:
            keys = _configured_api_keys()
            if keys and _key_matches(presented, keys):
                digest = hashlib.sha256(presented.encode()).hexdigest()[:16]
                return f"key:{digest}"
        return f"ip:{_client_address(request)}"

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
# The p95 latency
# ---------------------------------------------------------------------------


class LatencyMiddleware(BaseHTTPMiddleware):
    """ASGI timing middleware that keeps a rolling p95 of the API latency.

    It records each timed request's wall-clock time in a ring buffer
    (``maxlen=window_size``, default 200). After each request it recomputes the
    window's p95 and stores it on ``request.app.state.api_p95_seconds``, which
    /metrics reports to Prometheus. It also counts the request for /metrics.

    The exempt paths (``/health``, ``/ready``, ``/``, ``/metrics``) are never
    timed, so probe loops and scrapes do not skew the distribution.

    Sorting at most ``window_size`` floats per request costs under 20 µs. The
    ring buffer takes no lock: the middleware runs in the event loop, one
    request step at a time.
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

        # Count the request for baseball_sim_requests_total (/metrics). The
        # import is local to avoid an import cycle; a failure is ignored.
        try:
            from api.routes.metrics import record_request

            record_request(request.app.state)
        except Exception:  # noqa: BLE001
            pass

        # The window's p95, kept on app.state.
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


# ---------------------------------------------------------------------------
# The CORS allowlist
# ---------------------------------------------------------------------------


def resolve_cors_origins() -> list[str]:
    """Resolve the CORS allowlist from the environment.

    The order:
      1. ``CORS_ORIGINS`` (comma-separated), when set, used as given.
      2. else ``FRONTEND_URL``, when set.
      3. else, in development only, the three localhost dev origins.
      4. else one localhost origin.

    No default is the wildcard ``"*"``: with ``allow_credentials=True``,
    browsers refuse to send a cookie to a wildcard origin. Only an explicit
    ``CORS_ORIGINS=*`` gives it.
    """
    raw = os.environ.get("CORS_ORIGINS")
    if raw and raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]

    frontend = os.environ.get("FRONTEND_URL")
    if frontend and frontend.strip():
        return [frontend.strip()]

    if _is_development():
        # Named origins, so the browser sends the session cookie; the Vite
        # proxy at :5173 forwards it to the API at :8000.
        return [
            "http://localhost:5173",  # Vite dev server (primary)
            "http://localhost:3000",  # alternative dev port
            "http://localhost:8000",  # direct API access in dev tools
        ]

    # Outside development with nothing configured: one localhost origin, never
    # the wildcard. A deployment sets CORS_ORIGINS or FRONTEND_URL.
    return ["http://localhost:3000"]
