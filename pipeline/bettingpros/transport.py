"""The HTTP reads of the BettingPros and MLB APIs, with the retry policy.

Every read goes through one method, ``_http_get_json``. ``_bp_get`` and
``_mlb_get`` are the only network calls; the unit tests stub them.

The retry rules:

* A transient failure is retried: an HTTP 429 or 5xx, a time-out, a refused
  or dropped connection, an unreachable network, a TLS error.
* The wait before retry k is ``retry_wait_s`` times 2 to the power k − 1,
  at most 60 seconds.
* A 429 or 503 with a numeric ``Retry-After`` header waits at least that long.
* Any other failure (an HTTP 4xx, a body that is not JSON) raises at once.
* After the last retry, the original error reaches the caller.
* The default is no retry (``BETTINGPROS_MAX_RETRIES`` unset means 0).
  The historical loader turns retries on with ``set_retry_policy``.

Use retries in offline jobs only, and leave ``BETTINGPROS_MAX_RETRIES`` unset
in the API container. A retry waits with ``time.sleep``. The opening-line
job calls the provider on the app's event loop, so one wait stops the whole
app. The live pipeline calls it on its one worker thread, so one wait stalls
every odds read.
"""

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

log = logging.getLogger("pipeline.bettingpros")

_BP_BASE = "https://api.bettingpros.com/v3"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"


#: The most ``/offers`` pages read for one (event, market). The API pages
#: at 10 offers, and a batter market fills two to three pages. The cap only
#: stops a pager that never ends.
_MAX_OFFER_PAGES = 20


#: The error types a read may retry. ``OSError`` covers ``URLError``,
#: ``HTTPError``, ``TimeoutError`` and ``ConnectionError``. It also covers the
#: bare errors urllib does not wrap while it reads the body, such as an
#: ``ssl.SSLError`` or an unreachable network. ``HTTPException`` covers
#: ``RemoteDisconnected`` and ``IncompleteRead``. :func:`_is_transient`
#: checks the status of an ``HTTPError``.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (OSError, http.client.HTTPException)

#: The statuses whose numeric ``Retry-After`` sets a floor on the wait.
_RETRY_AFTER_STATUSES = frozenset({429, 503})


def _is_transient(exc: BaseException) -> bool:
    """True when a failed read may succeed on a later attempt.

    An HTTP 429 or 5xx and every network error are transient. Any other HTTP
    status (a 4xx) is the server's final answer.
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code if isinstance(exc.code, int) else 0
        return code == 429 or code >= 500
    return isinstance(exc, _RETRYABLE_ERRORS)


def _retry_after_s(exc: BaseException) -> float | None:
    """The numeric ``Retry-After`` of a 429 or 503, in seconds, or ``None``.

    The function gives ``None`` for any other status, and for a header that
    is absent, an HTTP date, negative or not finite.
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
        """Set the number of retries of a transient read failure and the first wait.

        ``max_retries`` is a whole number, 0 or more (0 means one attempt).
        ``retry_wait_s`` is 0 or more seconds. Any other value raises
        ``ValueError``.
        """
        if max_retries < 0 or int(max_retries) != max_retries:
            raise ValueError(f"max_retries must be a whole number >= 0, not {max_retries!r}")
        if not retry_wait_s >= 0:  # a NaN fails this test too
            raise ValueError(f"retry_wait_s must be >= 0 seconds, not {retry_wait_s!r}")
        self._max_retries = int(max_retries)
        self._retry_wait_s = float(retry_wait_s)

    def _retry_wait(self, retry: int, exc: BaseException) -> float:
        """The wait in seconds before retry ``retry`` (1, 2, ...) after ``exc``.

        The wait doubles per retry. A numeric ``Retry-After`` sets a floor.
        :attr:`RETRY_WAIT_CAP_S` caps the wait.
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
        """GET ``url`` and decode its JSON body. Every vendor and MLB read uses it.

        The method retries a transient failure up to ``max_retries`` times.
        Each retry logs one INFO line without the query or the API key.
        Any other failure raises at once. After the last retry, the original
        error reaches the caller unchanged.
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
