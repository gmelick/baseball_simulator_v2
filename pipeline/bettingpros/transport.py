"""The HTTP reads of the BettingPros and MLB APIs, with the retry policy."""

from __future__ import annotations

import http.client
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pipeline.bettingpros.base import ProviderBase

log = logging.getLogger("pipeline.bettingpros_odds_provider")

_BP_BASE = "https://api.bettingpros.com/v3"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"


#: The most ``/offers`` pages read for one (event, market). The API
#: pages at 10 offers; a batter market lists every hitter in the game (two to
#: three pages), so the cap only guards against a runaway pager.
_MAX_OFFER_PAGES = 20


#: The error types a read may retry. ``OSError`` is the base of
#: ``URLError`` (a time-out or a refused connection inside ``urlopen``; also
#: the base of ``HTTPError``, whose status :func:`_is_transient` checks), of
#: ``TimeoutError`` (``socket.timeout`` since Python 3.10) and of
#: ``ConnectionError``. ``OSError`` also covers what urllib does not wrap.
#: urllib wraps an ``OSError`` in ``URLError`` only around the request. An
#: error raised while the status line or the body is read comes through bare:
#: an ``ssl.SSLError``, or an ``EHOSTUNREACH``, ``ENETUNREACH`` or ``ENETDOWN``.
#: ``HTTPException`` covers ``RemoteDisconnected`` and ``IncompleteRead``.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (OSError, http.client.HTTPException)

#: The statuses whose numeric ``Retry-After`` sets a floor on the wait.
_RETRY_AFTER_STATUSES = frozenset({429, 503})


def _is_transient(exc: BaseException) -> bool:
    """True when a failed read may succeed on a later attempt.

    An HTTP 429 or 5xx, a time-out, a refused or dropped connection, an
    unreachable network, a TLS error. Any other HTTP status (a 4xx) is the
    server's final answer.
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code if isinstance(exc.code, int) else 0
        return code == 429 or code >= 500
    return isinstance(exc, _RETRYABLE_ERRORS)


def _retry_after_s(exc: BaseException) -> float | None:
    """The numeric ``Retry-After`` of a 429 or 503, in seconds, or ``None``.

    ``None`` when the status is another one, or the header is absent, an
    HTTP date, negative or not finite.
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


def _log_target(url: str) -> str:
    """The host and path of a URL, for a log line: no query string, no user part."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname or ''}{parts.path}"


class TransportMixin(ProviderBase):
    """The provider's reads: one GET with the retry policy, for each API."""

    # ----------------------------------------------------------------- HTTP
    def set_retry_policy(self, max_retries: int, retry_wait_s: float) -> None:
        """Set the retries of a transient read failure and the first wait.

        ``max_retries`` is a whole number 0 or more (0 = one attempt);
        ``retry_wait_s`` is 0 or more seconds. Raises ``ValueError`` otherwise.
        """
        if max_retries < 0 or int(max_retries) != max_retries:
            raise ValueError(f"max_retries must be a whole number >= 0, not {max_retries!r}")
        if not retry_wait_s >= 0:  # a NaN fails this test too
            raise ValueError(f"retry_wait_s must be >= 0 seconds, not {retry_wait_s!r}")
        self._max_retries = int(max_retries)
        self._retry_wait_s = float(retry_wait_s)

    def _retry_wait(self, retry: int, exc: BaseException) -> float:
        """The wait in seconds before retry ``retry`` (1, 2, ...) after ``exc``.

        ``retry_wait_s`` doubles per retry. A 429 or 503 with a numeric
        ``Retry-After`` waits at least that long. Both stop at
        :attr:`RETRY_WAIT_CAP_S`.
        """
        # The exponent stops at 64: a larger one adds nothing under the cap.
        wait = self._retry_wait_s * 2.0 ** min(retry - 1, 64)
        retry_after = _retry_after_s(exc)
        if retry_after is not None:
            wait = max(wait, retry_after)
        return min(wait, self.RETRY_WAIT_CAP_S)

    def _describe_error(self, exc: BaseException, url: str) -> str:
        """The error for a log line, with the URL's query and the API key taken out."""
        text = f"{type(exc).__name__}: {exc}".replace(url, _log_target(url))
        if self._api_key:
            text = text.replace(self._api_key, "<api key>")
        return text

    def _http_get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """GET ``url`` and decode its JSON body; the one read of every vendor and MLB call.

        A transient failure (an HTTP 429 or 5xx, a time-out, a refused
        or dropped connection) is retried up to ``max_retries`` times, after
        the wait of :meth:`_retry_wait`. Each retry logs one INFO line with the
        attempt, the error, the host and path (no query, no key) and the wait.
        Any other failure raises at once. After the last retry the original
        error propagates unchanged, so the callers' handlers still catch it.
        """
        retry = 0
        while True:
            try:
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed hosts
                    return json.loads(resp.read().decode("utf-8"))
            except _RETRYABLE_ERRORS as exc:
                if retry >= self._max_retries or not _is_transient(exc):
                    raise
                retry += 1
                wait = self._retry_wait(retry, exc)
                log.info(
                    "BettingPros: %s failed on attempt %d of %d (%s); retrying in %.1f s",
                    _log_target(url),
                    retry,
                    self._max_retries + 1,
                    self._describe_error(exc, url),
                    wait,
                )
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # free the error response's connection before the next attempt
                self._retry_sleep(wait)

    def _bp_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a BettingPros v3 endpoint (the network seam stubbed in tests)."""
        if not self._api_key:
            raise RuntimeError(
                f"BettingProsOddsProvider needs an API key — set {self.API_KEY_ENV}."
            )
        qs = urllib.parse.urlencode(params)
        url = f"{_BP_BASE}/{path}?{qs}"
        return self._http_get_json(
            url, headers={"x-api-key": self._api_key, "Content-Type": "application/json"}
        )

    def _mlb_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the network seam stubbed in tests)."""
        qs = urllib.parse.urlencode(params)
        url = f"{_MLB_BASE}/{path}?{qs}"
        return self._http_get_json(url)
