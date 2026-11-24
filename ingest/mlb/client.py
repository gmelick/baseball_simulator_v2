"""The one MLB Stats API client: the endpoint addresses, one GET with a retry, one async reader.

Every program read of the MLB Stats API goes through this module.

- :func:`get` and :func:`get_json` are the sync GET, over stdlib ``urllib``. A
  transient failure retries by :class:`RetryPolicy`; any other failure raises at
  once. After the last retry, the original error reaches the caller unchanged.
  The BettingPros reads use the same GET.
- :class:`LeagueFeed` is the async reader over one ``aiohttp`` session. The app
  reads the slate's schedule and a game's feed with it; the live service reads
  its feeds and lineups with it.

Keep the sync GET on ``urllib``: the long reload sweeps crashed with a memory
fault on ``requests`` and ran clean on ``urllib``. ``urllib`` sends
``Accept-Encoding: identity``, so a body never arrives compressed, and
:func:`_decode_body` needs no gunzip branch.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ingest.mlb.schedule import DEFAULT_HYDRATE, schedule_params

log = logging.getLogger(__name__)

MLB_BASE = "https://statsapi.mlb.com"
API_V1 = f"{MLB_BASE}/api/v1"
SCHEDULE_URL = f"{API_V1}/schedule"
GAME_FEED_URL = f"{MLB_BASE}/api/v1.1/game/{{game_pk}}/feed/live"
PEOPLE_URL = f"{API_V1}/people/{{player_id}}"
VENUES_URL = f"{API_V1}/venues"
VENUE_URL = f"{API_V1}/venues/{{venue_id}}"
COACHES_URL = f"{API_V1}/teams/{{team_id}}/coaches"

#: The statuses a read retries: a time-out, "too early", the rate limit, and
#: every 5xx. Any other status is the server's final answer.
RETRYABLE_STATUSES = frozenset({408, 425, 429})

#: The statuses whose numeric ``Retry-After`` sets a floor on the wait.
_RETRY_AFTER_STATUSES = frozenset({429, 503})

#: The error types a read may retry. ``OSError`` covers ``URLError``,
#: ``HTTPError``, ``TimeoutError``, ``ConnectionError`` and the errors urllib
#: does not wrap while it reads the body, such as ``ssl.SSLError``.
#: ``HTTPException`` covers ``RemoteDisconnected`` and ``IncompleteRead``.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (OSError, http.client.HTTPException)


@dataclass(frozen=True)
class RetryPolicy:
    """How a read retries a transient failure.

    ``retries`` is the number of retries after the first attempt (0 means one
    attempt). The wait before retry ``n`` is ``first_wait_s * 2 ** (n - 1)``,
    at least the server's numeric ``Retry-After`` on a 429 or 503, and at most
    ``cap_s``.
    """

    retries: int = 0
    first_wait_s: float = 1.0
    cap_s: float = 60.0

    def wait_s(self, retry: int, exc: BaseException) -> float:
        """The wait in seconds before retry ``retry`` (1, 2, ...) after ``exc``."""
        # Cap the exponent: 2.0 ** n overflows for a large n, and 2 ** 64 passes any cap.
        wait = self.first_wait_s * 2.0 ** min(retry - 1, 64)
        retry_after = retry_after_s(exc)
        if retry_after is not None:
            wait = max(wait, retry_after)
        return min(wait, self.cap_s)


#: The policy for a caller with no need of its own: three retries, the first after one second.
DEFAULT_POLICY = RetryPolicy(retries=3, first_wait_s=1.0)


def is_transient(exc: BaseException) -> bool:
    """True when a failed read may succeed on a later attempt."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code if isinstance(exc.code, int) else 0
        return code in RETRYABLE_STATUSES or code >= 500
    return isinstance(exc, _RETRYABLE_ERRORS)


def retry_after_s(exc: BaseException) -> float | None:
    """The numeric ``Retry-After`` of a 429 or 503, in seconds, or None.

    None also for a header that is absent, an HTTP date, negative or not finite.
    """
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in _RETRY_AFTER_STATUSES:
        return None
    headers = exc.headers
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def log_target(url: str) -> str:
    """The host and path of a URL, for a log line: no query string, no user part."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname or ''}{parts.path}"


@dataclass(frozen=True)
class Response:
    """A successful response: the status, the headers, the decoded body and the URL."""

    status: int
    headers: dict[str, str]
    text: str
    url: str

    def json(self) -> Any:
        """The body parsed as JSON."""
        return json.loads(self.text)


def _decode_body(body: bytes, headers: Any) -> str:
    """Decode a body with its declared charset, else UTF-8."""
    ctype = ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        ctype = getter("Content-Type") or getter("content-type") or ""
    charset = "utf-8"
    if "charset=" in ctype.lower():
        charset = ctype.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:  # a charset Python does not know
        return body.decode("utf-8", errors="replace")


def get(
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], None] | None = None,
    logger: logging.Logger = log,
    label: str = "MLB Stats API",
    describe: Callable[[BaseException], str] | None = None,
) -> Response:
    """GET ``url`` and return the response. Retry a transient failure by ``policy``.

    Each retry logs one INFO line on ``logger``, without the query string.
    ``describe`` may clean the error text: the odds reads take their key out.
    ``sleep`` defaults to ``time.sleep``, looked up at each wait, so a test can patch it.
    After the last retry, or on any other failure, the original error raises.
    """
    # A list value repeats its key (``gameTypes=R&gameTypes=F``).
    full = f"{url}?{urllib.parse.urlencode(dict(params), doseq=True)}" if params else url
    retry = 0
    while True:
        try:
            req = urllib.request.Request(full, headers=dict(headers or {}))
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — fixed hosts
                # A test stub may carry only read(); a real response has both.
                status = getattr(resp, "status", 200)
                resp_headers = getattr(resp, "headers", None)
                body = _decode_body(resp.read(), resp_headers)
                return Response(status, dict(resp_headers or {}), body, full)
        except _RETRYABLE_ERRORS as exc:
            if retry >= policy.retries or not is_transient(exc):
                raise
            retry += 1
            wait = policy.wait_s(retry, exc)
            text = (
                describe(exc)
                if describe
                else f"{type(exc).__name__}: {exc}".replace(full, log_target(full))
            )
            logger.info(
                "%s: %s failed on attempt %d of %d (%s); retrying in %.1f s",
                label,
                log_target(full),
                retry,
                policy.retries + 1,
                text,
                wait,
            )
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # free the error response's connection before the next attempt
            (sleep or time.sleep)(wait)


def get_json(url: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
    """GET ``url`` and parse its body as JSON. A body that is not JSON raises at once."""
    return get(url, params, **kwargs).json()


class LeagueFeed:
    """The async reader: GETs over one lazy ``aiohttp`` session.

    The app attaches one as ``app.state.league_feed`` for the slate and the game
    page; a test app attaches a fake with the same coroutine methods, or none.
    The live service holds its own. A non-2xx status raises
    ``aiohttp.ClientResponseError``.
    """

    def __init__(self, *, timeout_s: float | None = None) -> None:
        """``timeout_s`` defaults to ``SLATE_FEED_TIMEOUT_S`` (5 seconds when unset or bad)."""
        if timeout_s is None:
            try:
                timeout_s = float(os.environ.get("SLATE_FEED_TIMEOUT_S", "5"))
            except ValueError:
                timeout_s = 5.0
        self._timeout_s = timeout_s
        self._session: Any = None

    async def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``url`` and parse its JSON body. Raises on any failure."""
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                headers={"User-Agent": "daily-diamond/1.0"},
            )
        async with self._session.get(url, params=dict(params) if params else None) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def schedule_payload(self, start: date, end: date, hydrate: str = DEFAULT_HYDRATE) -> Any:
        """The raw schedule response for ``start..end``. Raises on any failure."""
        return await self.get_json(SCHEDULE_URL, schedule_params(start, end, hydrate))

    async def game_feed_payload(self, game_pk: int) -> Any:
        """The raw live feed of one game. Raises on any failure.

        ``hydrate=alignment`` adds each pitch's fielders: the what-if reads them
        at every plate appearance.
        """
        return await self.get_json(
            GAME_FEED_URL.format(game_pk=int(game_pk)), {"hydrate": "alignment"}
        )

    async def close(self) -> None:
        """Close the session, if one is open."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
