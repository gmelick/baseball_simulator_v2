"""SIM-555 (2026-10-01) — the BettingPros provider retries a transient read failure and
counts every failed read it catches.

The historical loader re-loaded every season for SIM-555. A vendor read that failed (a
time-out, a burst of HTTP 502 / 503) was caught inside the provider, logged and turned
into an empty result; the loader then put the game on its done-list, so the gap was
silent and permanent (game 717171 lost every row to one schedule time-out). These tests
pin the provider half of the fix:

  * ``_http_get_json`` retries an HTTP 429 / 5xx, a time-out, a refused or dropped
    connection, an unreachable network, a TLS error. The wait doubles from
    ``retry_wait_s`` and stops at 60 s. A numeric ``Retry-After`` on a 429 or 503 sets
    a floor on the wait. Any other failure raises at once, and after the last retry
    the original error propagates;
  * the default is one attempt (``BETTINGPROS_MAX_RETRIES`` unset), so the live
    pipeline and the opening-line job keep today's behaviour; ``set_retry_policy``
    refuses a bad value;
  * ``read_failures`` grows by one at each place the provider catches a failed read,
    and not for a legitimate absence (no event, a matcher decline, a people answer
    without the player, a slate the matcher cannot read, an empty offer list, a player
    with no offer, a first-five exclusion, a failed lookup still cached);
  * ``forget_failed_lookups`` (the loader calls it before each game) makes the next
    game read a failed player again, so a failure stored in one game never cuts the
    next one short without a count;
  * the retry's log line carries no API key and no query string.

No network. The retry tests replace ``urllib.request.urlopen`` with a scripted fake; the
count tests serve the captured payloads under ``tests/fixtures/bettingpros/`` (game
746437, SEA at DET, 2024-08-15, BettingPros event 92857) through the ``_bp_get`` /
``_mlb_get`` seams.
"""

from __future__ import annotations

import copy
import email.message
import errno
import http.client
import json
import logging
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider
from pipeline.odds_provider import get_odds_provider

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"

_KEY = "sk-test-secret-555"
_OFFERS_PARAMS = {"sport": "MLB", "market_id": 285, "event_id": 92857}
_OFFERS_URL = "https://api.bettingpros.com/v3/offers?sport=MLB&market_id=285&event_id=92857"
_PAYLOAD = {"offers": [], "_pagination": {"total_pages": 1}}


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- the HTTP fake


class _Response:
    """The context-manager response ``urlopen`` returns; ``error`` makes ``read`` raise."""

    def __init__(self, body: bytes, error: BaseException | None = None) -> None:
        self._body = body
        self._error = error

    def read(self) -> bytes:
        if self._error is not None:
            raise self._error
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _ReadRaises:
    """A script step: the status line arrives, then reading the body raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error


class _ScriptedUrlopen:
    """``urlopen`` that follows a script: each step is an error to raise or a body to return."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, timeout: float | None = None) -> _Response:
        self.requests.append(req)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, _ReadRaises):
            return _Response(b"", error=step.error)
        return _Response(step if isinstance(step, bytes) else json.dumps(step).encode("utf-8"))


@pytest.fixture
def urlopen(monkeypatch: pytest.MonkeyPatch):
    """Install a scripted ``urlopen``; call it with the script's steps."""

    def install(*steps: Any) -> _ScriptedUrlopen:
        fake = _ScriptedUrlopen(*steps)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return install


def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(_OFFERS_URL, code, "status", headers, None)


def _provider(
    max_retries: int = 3, retry_wait_s: float = 2.0, **kw: Any
) -> tuple[BettingProsOddsProvider, list[float]]:
    """A provider whose retry waits are recorded, not slept."""
    waits: list[float] = []
    provider = BettingProsOddsProvider(
        api_key=_KEY, max_retries=max_retries, retry_wait_s=retry_wait_s, sleep=waits.append, **kw
    )
    return provider, waits


def _read(provider: BettingProsOddsProvider) -> dict[str, Any]:
    return provider._bp_get("offers", dict(_OFFERS_PARAMS))


# --------------------------------------------------------------------------- the retry


class TestRetry:
    def test_a_502_then_success_returns_the_payload_after_one_retry(self, urlopen) -> None:
        fake = urlopen(_http_error(502), _PAYLOAD)
        provider, waits = _provider(max_retries=3, retry_wait_s=2.0)
        assert _read(provider) == _PAYLOAD
        assert waits == [2.0]
        assert len(fake.requests) == 2
        # Both attempts send the key in the header and the same URL.
        assert {r.full_url for r in fake.requests} == {_OFFERS_URL}
        assert all(r.get_header("X-api-key") == _KEY for r in fake.requests)
        assert provider.read_failures == 0  # the read succeeded: nothing was caught

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_every_429_and_5xx_is_retried(self, urlopen, code: int) -> None:
        urlopen(_http_error(code), _PAYLOAD)
        provider, waits = _provider(max_retries=1, retry_wait_s=1.5)
        assert _read(provider) == _PAYLOAD
        assert waits == [1.5]

    def test_the_waits_double_and_stop_at_60_seconds(self, urlopen) -> None:
        urlopen(*[_http_error(502)] * 5, _PAYLOAD)
        provider, waits = _provider(max_retries=5, retry_wait_s=10.0)
        assert _read(provider) == _PAYLOAD
        assert waits == [10.0, 20.0, 40.0, 60.0, 60.0]
        assert BettingProsOddsProvider.RETRY_WAIT_CAP_S == 60.0

    def test_a_large_retry_count_stays_at_the_cap(self, urlopen) -> None:
        urlopen(*[_http_error(503)] * 80, _PAYLOAD)
        provider, waits = _provider(max_retries=80, retry_wait_s=2.0)
        assert _read(provider) == _PAYLOAD
        assert len(waits) == 80 and waits[-1] == 60.0 and max(waits) == 60.0

    @pytest.mark.parametrize(
        ("code", "retry_after", "expected"),
        [
            (429, "30", 30.0),  # longer than the computed 2 s: the server's figure
            (503, "12.5", 12.5),
            (503, "1", 2.0),  # shorter than the computed wait: the computed one stands
            (429, "600", 60.0),  # capped
            (429, "Wed, 21 Oct 2026 07:28:00 GMT", 2.0),  # an HTTP date is not numeric
            (429, "-5", 2.0),
            (502, "30", 2.0),  # a 502's Retry-After is not read
            (500, "30", 2.0),
        ],
    )
    def test_retry_after_sets_a_floor_on_a_429_or_503(
        self, urlopen, code: int, retry_after: str, expected: float
    ) -> None:
        urlopen(_http_error(code, retry_after=retry_after), _PAYLOAD)
        provider, waits = _provider(max_retries=2, retry_wait_s=2.0)
        assert _read(provider) == _PAYLOAD
        assert waits == [expected]

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
    def test_a_4xx_other_than_429_raises_at_once(self, urlopen, code: int) -> None:
        error = _http_error(code)
        fake = urlopen(error, _PAYLOAD)
        provider, waits = _provider(max_retries=3)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _read(provider)
        assert excinfo.value is error
        assert waits == [] and len(fake.requests) == 1

    def test_a_body_that_is_not_json_raises_at_once(self, urlopen) -> None:
        fake = urlopen(b"<html>Bad Gateway</html>", _PAYLOAD)
        provider, waits = _provider(max_retries=3)
        with pytest.raises(json.JSONDecodeError):
            _read(provider)
        assert waits == [] and len(fake.requests) == 1

    def test_time_outs_and_dropped_connections_are_retried(self, urlopen) -> None:
        fake = urlopen(
            urllib.error.URLError(TimeoutError("timed out")),
            urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
            TimeoutError("The read operation timed out"),
            ConnectionResetError(104, "Connection reset by peer"),
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            http.client.IncompleteRead(b'{"offers"', 120),
            _PAYLOAD,
        )
        provider, waits = _provider(max_retries=6, retry_wait_s=0.5)
        assert _read(provider) == _PAYLOAD
        assert waits == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
        assert len(fake.requests) == 7

    def test_errors_urllib_does_not_wrap_are_retried(self, urlopen) -> None:
        """urllib wraps an OSError in URLError only around the request. A TLS error or
        an unreachable network raised while the status line or the body is read comes
        through bare, and is still a passing failure."""
        tls = ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed")
        fake = urlopen(
            _ReadRaises(tls),
            _ReadRaises(OSError(errno.EHOSTUNREACH, "No route to host")),
            OSError(errno.ENETUNREACH, "Network is unreachable"),
            OSError(errno.ENETDOWN, "Network is down"),
            _PAYLOAD,
        )
        provider, waits = _provider(max_retries=4, retry_wait_s=0.5)
        assert _read(provider) == _PAYLOAD
        assert waits == [0.5, 1.0, 2.0, 4.0]
        assert len(fake.requests) == 5
        assert provider.read_failures == 0

    def test_a_tls_error_after_the_last_retry_propagates_unchanged(self, urlopen) -> None:
        last = ssl.SSLError(1, "[SSL] bad record")
        urlopen(_ReadRaises(ssl.SSLError(1, "[SSL] bad record")), _ReadRaises(last))
        provider, waits = _provider(max_retries=1)
        with pytest.raises(ssl.SSLError) as excinfo:
            _read(provider)
        assert excinfo.value is last
        assert waits == [2.0]

    def test_after_the_last_retry_the_original_error_propagates(self, urlopen) -> None:
        last = _http_error(504)
        fake = urlopen(_http_error(502), _http_error(503), last, _PAYLOAD)
        provider, waits = _provider(max_retries=2, retry_wait_s=3.0)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _read(provider)
        assert excinfo.value is last
        assert waits == [3.0, 6.0]
        assert len(fake.requests) == 3  # one attempt and two retries; the payload is never read

    def test_a_time_out_after_the_last_retry_propagates_unchanged(self, urlopen) -> None:
        last = urllib.error.URLError(TimeoutError("timed out"))
        urlopen(urllib.error.URLError(TimeoutError("timed out")), last)
        provider, _waits = _provider(max_retries=1)
        with pytest.raises(urllib.error.URLError) as excinfo:
            _read(provider)
        assert excinfo.value is last

    def test_the_mlb_reads_retry_too(self, urlopen) -> None:
        schedule = _load("mlb_schedule_746437.json")
        fake = urlopen(_http_error(503), schedule)
        provider, waits = _provider(max_retries=1, retry_wait_s=2.0)
        assert provider._resolve_game_meta(746437) is not None
        assert waits == [2.0]
        assert fake.requests[0].full_url.startswith("https://statsapi.mlb.com/api/v1/schedule?")
        assert provider.read_failures == 0


# --------------------------------------------------------------------------- the policy


class TestRetryPolicy:
    def test_max_retries_0_is_one_attempt(self, urlopen) -> None:
        error = _http_error(502)
        fake = urlopen(error, _PAYLOAD)
        provider, waits = _provider(max_retries=0)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _read(provider)
        assert excinfo.value is error
        assert waits == [] and len(fake.requests) == 1

    def test_the_default_is_one_attempt(self, urlopen, monkeypatch) -> None:
        """The live pipeline and the opening-line job build the provider with no arguments."""
        monkeypatch.delenv(BettingProsOddsProvider.MAX_RETRIES_ENV, raising=False)
        monkeypatch.delenv(BettingProsOddsProvider.RETRY_WAIT_ENV, raising=False)
        assert BettingProsOddsProvider.MAX_RETRIES_ENV == "BETTINGPROS_MAX_RETRIES"
        assert BettingProsOddsProvider.RETRY_WAIT_ENV == "BETTINGPROS_RETRY_WAIT_S"
        provider = BettingProsOddsProvider(api_key=_KEY)
        assert (provider._max_retries, provider._retry_wait_s) == (0, 2.0)
        registry_built = get_odds_provider("bettingpros")
        assert isinstance(registry_built, BettingProsOddsProvider)
        assert registry_built._max_retries == 0
        fake = urlopen(_http_error(503), _PAYLOAD)
        with pytest.raises(urllib.error.HTTPError):
            _read(provider)
        assert len(fake.requests) == 1

    def test_the_environment_sets_the_defaults(self, urlopen, monkeypatch) -> None:
        monkeypatch.setenv(BettingProsOddsProvider.MAX_RETRIES_ENV, "2")
        monkeypatch.setenv(BettingProsOddsProvider.RETRY_WAIT_ENV, "0.25")
        waits: list[float] = []
        provider = BettingProsOddsProvider(api_key=_KEY, sleep=waits.append)
        assert (provider._max_retries, provider._retry_wait_s) == (2, 0.25)
        urlopen(_http_error(502), _http_error(502), _PAYLOAD)
        assert _read(provider) == _PAYLOAD
        assert waits == [0.25, 0.5]
        # A constructor argument wins over the environment.
        explicit, _ = _provider(max_retries=5, retry_wait_s=1.0)
        assert (explicit._max_retries, explicit._retry_wait_s) == (5, 1.0)

    def test_set_retry_policy_changes_the_policy(self, urlopen) -> None:
        provider, waits = _provider(max_retries=0, retry_wait_s=2.0)
        provider.set_retry_policy(3, 5.0)
        assert (provider._max_retries, provider._retry_wait_s) == (3, 5.0)
        urlopen(_http_error(502), _http_error(503), _PAYLOAD)
        assert _read(provider) == _PAYLOAD
        assert waits == [5.0, 10.0]
        provider.set_retry_policy(0, 0.0)  # zero is allowed on both
        assert (provider._max_retries, provider._retry_wait_s) == (0, 0.0)

    @pytest.mark.parametrize(
        ("max_retries", "retry_wait_s"),
        [(-1, 2.0), (1.5, 2.0), (3, -0.1), (3, float("nan"))],
    )
    def test_set_retry_policy_refuses_a_bad_value(
        self, max_retries: Any, retry_wait_s: float
    ) -> None:
        provider, _ = _provider(max_retries=2, retry_wait_s=1.0)
        with pytest.raises(ValueError):
            provider.set_retry_policy(max_retries, retry_wait_s)
        assert (provider._max_retries, provider._retry_wait_s) == (2, 1.0)  # unchanged
        with pytest.raises(ValueError):
            BettingProsOddsProvider(
                api_key=_KEY, max_retries=max_retries, retry_wait_s=retry_wait_s
            )


# --------------------------------------------------------------------------- the log line


class TestRetryLog:
    def test_the_log_line_names_the_attempt_the_wait_and_the_host_and_path(
        self, urlopen, caplog
    ) -> None:
        urlopen(_http_error(502), _PAYLOAD)
        provider, _ = _provider(max_retries=3, retry_wait_s=2.0)
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            _read(provider)
        lines = [r for r in caplog.records if r.name == "pipeline.bettingpros_odds_provider"]
        assert len(lines) == 1 and lines[0].levelno == logging.INFO
        message = lines[0].getMessage()
        assert "api.bettingpros.com/v3/offers" in message
        assert "attempt 1 of 4" in message
        assert "HTTP Error 502" in message
        assert "retrying in 2.0 s" in message
        for secret in (_KEY, "?", "market_id", "event_id", "92857", "x-api-key"):
            assert secret not in message, secret

    def test_an_error_text_that_holds_the_url_or_the_key_is_cleaned(self, urlopen, caplog) -> None:
        noisy = urllib.error.URLError(f"timed out reading {_OFFERS_URL} with key {_KEY}")
        urlopen(noisy, _PAYLOAD)
        provider, _ = _provider(max_retries=1)
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            _read(provider)
        (record,) = [r for r in caplog.records if r.name == "pipeline.bettingpros_odds_provider"]
        message = record.getMessage()
        assert "timed out reading api.bettingpros.com/v3/offers" in message
        assert _KEY not in message and "?" not in message and "market_id" not in message


# --------------------------------------------------------------------------- read_failures

_OFFERS_92857 = {
    122: "offers_ml_92857.json",
    175: "offers_total_92857.json",
    176: "offers_runline_92857.json",
    285: "offers_k_92857.json",
}
_PAGED_HITS = {1: "offers_hits_92857_page1.json", 2: "offers_hits_92857_page2.json"}


def _line(line: float, cost: float) -> dict[str, Any]:
    return {
        "line": line,
        "cost": cost,
        "updated": "2024-08-15 17:05:00",
        "main": True,
        "active": True,
        "is_off": False,
    }


def _runline_offers(prices: dict[int, tuple[float, float]]) -> dict[str, Any]:
    """A run-line market for DET (home, +0.5) and SEA (away, -0.5): book id → (home, away)."""
    det = {
        "participant": "DET",
        "selection": "",
        "label": "",
        "books": [{"id": b, "lines": [_line(0.5, h)]} for b, (h, _a) in prices.items()],
    }
    sea = {
        "participant": "SEA",
        "selection": "",
        "label": "",
        "books": [{"id": b, "lines": [_line(-0.5, a)]} for b, (_h, a) in prices.items()],
    }
    return {"offers": [{"selections": [det, sea], "participants": []}]}


class _ReadError(RuntimeError):
    """The error a failing stub read raises (the retry has already given up)."""


class _Counted(BettingProsOddsProvider):
    """Game 746437 from the captured payloads; ``fail`` names the reads that raise.

    A name in ``fail`` is ``'schedule'``, ``'people'``, ``'events'``, ``('offers',
    market_id)`` (every page) or ``('offers', market_id, page)``. ``reads`` records
    every read, so a test can tell a cached failure from a new read. ``people``
    replaces the people answer (an answer without the player, for one).
    """

    def __init__(
        self,
        *,
        fail: set[Any] | None = None,
        schedule: dict[str, Any] | None = None,
        events: dict[str, Any] | None = None,
        offers: dict[int, dict[str, Any]] | None = None,
        player: str = "Bryce Miller",
        people: dict[str, Any] | None = None,
        now: list[float] | None = None,
    ) -> None:
        self.now = now if now is not None else [0.0]
        super().__init__(api_key="test-key", offers_cache_ttl_s=30.0, clock=lambda: self.now[0])
        self.fail = fail if fail is not None else set()
        self._schedule = schedule if schedule is not None else _load("mlb_schedule_746437.json")
        self._events = events if events is not None else _load("events_2024-08-15.json")
        self._extra_offers = offers or {}
        self._player = player
        self._people = people
        self.reads: list[Any] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        key = "schedule" if path == "schedule" else "people"
        self.reads.append(key)
        if key in self.fail:
            raise _ReadError(f"{key}: timed out")
        if key == "schedule":
            return self._schedule
        if self._people is not None:
            return self._people
        return {"people": [{"fullName": self._player}]}

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            self.reads.append("events")
            if "events" in self.fail:
                raise _ReadError("events: HTTP Error 503")
            return self._events
        market, page = int(params["market_id"]), int(params.get("page", 1))
        self.reads.append(("offers", market, page))
        if ("offers", market) in self.fail or ("offers", market, page) in self.fail:
            raise _ReadError(f"offers {market} page {page}: HTTP Error 502")
        if market in self._extra_offers:
            return self._extra_offers[market]
        if market == 287:
            return _load(_PAGED_HITS[page])
        return _load(_OFFERS_92857[market])


def _schedule(**game_fields: Any) -> dict[str, Any]:
    """The captured schedule with fields of its one game replaced."""
    schedule = copy.deepcopy(_load("mlb_schedule_746437.json"))
    schedule["dates"][0]["games"][0].update(game_fields)
    return schedule


class TestReadFailures:
    def test_it_starts_at_zero_and_a_clean_game_adds_nothing(self) -> None:
        provider = _Counted()
        assert provider.read_failures == 0
        assert provider.get_odds_by_book(746437, line_type="closing")
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts", line_type="closing")
        assert provider.read_failures == 0

    # ---- one per caught failure, at every kind of catch site

    def test_a_failed_schedule_read_counts_once(self) -> None:
        provider = _Counted(fail={"schedule"})
        assert provider.get_odds_by_book(746437, line_type="closing") == []
        assert provider.read_failures == 1
        # The failure is cached for one time-to-live: no new read, no new count.
        assert provider.get_odds_by_book(746437, market_type="total") == []
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider._resolve_game_meta(746437) is None
        assert provider.reads.count("schedule") == 1
        assert provider.read_failures == 1
        # After the time-to-live the schedule is read again; a new failure counts again.
        provider.now[0] = 31.0
        assert provider.get_odds_by_book(746437) == []
        assert provider.reads.count("schedule") == 2
        assert provider.read_failures == 2

    def test_a_failed_events_read_counts_once(self) -> None:
        provider = _Counted(fail={"events"})
        assert provider.get_odds_by_book(746437, line_type="closing") == []
        assert provider.read_failures == 1
        assert provider.get_odds_by_book(746437, line_type="opening") == []
        assert provider.reads.count("events") == 1
        assert provider.read_failures == 1

    def test_a_failed_offers_read_counts_at_each_catch(self) -> None:
        provider = _Counted(fail={("offers", 122)})
        assert provider.get_odds_by_book(746437, line_type="closing") == []
        assert provider.read_failures == 1
        # A failed offers read is not cached: the next call reads again and counts again.
        assert provider.get_odds_by_book(746437, line_type="opening") == []
        assert provider.read_failures == 2
        # The other markets of the game are untouched.
        assert provider.get_odds_by_book(746437, line_type="closing", market_type="total")
        assert provider.read_failures == 2

    def test_a_failed_team_offers_read_counts(self) -> None:
        provider = _Counted(fail={("offers", 277)})
        assert provider.get_odds_by_book(746437, market_type="team_total_home") == []
        assert provider.read_failures == 1

    def test_a_failed_prop_offers_read_counts(self) -> None:
        provider = _Counted(fail={("offers", 285)})
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.read_failures == 1

    def test_a_failed_later_offers_page_counts_and_keeps_page_one(self) -> None:
        provider = _Counted(fail={("offers", 287, 2)}, player="Batter Pageone1")
        rows = provider.get_prop_odds_by_book(746437, 1, "hits", line_type="closing")
        assert rows, "the player on page 1 keeps his quotes"
        assert provider.read_failures == 1
        assert ("offers", 287, 2) in provider.reads

    def test_a_failed_first_inning_read_counts_once_per_market_read(self) -> None:
        f5 = _runline_offers({19: (-110, -110)})
        provider = _Counted(fail={("offers", 282)}, offers={283: f5})
        assert (
            provider.get_odds_by_book(746437, line_type="closing", market_type="f5_runline") == []
        )
        assert provider.read_failures == 1

    def test_a_failed_player_read_counts_once(self) -> None:
        provider = _Counted(fail={"people"})
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.read_failures == 1
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts", line_type="opening") == []
        assert provider.reads.count("people") == 1
        assert provider.read_failures == 1
        # The game reads stayed clean: one schedule read, one events read.
        assert provider.reads.count("schedule") == 1 and provider.reads.count("events") == 1

    # ---- a legitimate absence counts nothing

    def test_no_event_on_the_slate_counts_nothing(self) -> None:
        provider = _Counted(events={"events": []})
        assert provider.get_odds_by_book(746437) == []
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.read_failures == 0

    def test_a_matcher_decline_counts_nothing(self) -> None:
        # The real start five hours after the vendor's event: over the two-hour limit.
        # The game is a double-header, so the single-event rule does not apply.
        provider = _Counted(schedule=_schedule(gameDate="2024-08-15T22:10:00Z", doubleHeader="S"))
        assert provider._resolve_event(746437) is None
        assert provider.get_odds_by_book(746437) == []
        assert provider.read_failures == 0

    def test_a_schedule_without_the_game_counts_nothing(self) -> None:
        provider = _Counted(schedule={"dates": []})
        assert provider.get_odds_by_book(746437) == []
        assert provider.read_failures == 0

    def test_an_empty_offer_list_counts_nothing(self) -> None:
        provider = _Counted(offers={122: {"offers": []}, 285: {"offers": []}})
        assert provider.get_odds_by_book(746437, line_type="closing") == []
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.read_failures == 0

    def test_a_player_with_no_offer_counts_nothing(self) -> None:
        provider = _Counted(player="Nobody Atall")
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.read_failures == 0

    @pytest.mark.parametrize("people", [{"people": []}, {}, {"people": [{}]}])
    def test_a_people_answer_without_the_player_counts_nothing(
        self, people: dict[str, Any]
    ) -> None:
        """The read succeeded and MLB lists no such player: an absence, not a failure.
        Counting it would keep the game off the done-list on every run."""
        provider = _Counted(people=people)
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts") == []
        assert provider.reads.count("people") == 1
        assert provider.read_failures == 0

    def test_a_slate_the_matcher_cannot_read_counts_nothing(self) -> None:
        """The events read succeeded but an event lacks a participant id. The slate is
        malformed, not unreachable: a retry would read the same answer, so the game is
        unmatched (as a matcher decline) and the count stays at 0."""
        events = copy.deepcopy(_load("events_2024-08-15.json"))
        for event in events["events"]:
            for participant in event.get("participants", []):
                participant.pop("id", None)
        provider = _Counted(events=events)
        assert provider._resolve_event(746437) is None
        assert provider.get_odds_by_book(746437) == []
        assert provider.reads.count("events") == 1
        assert provider.read_failures == 0

    # ---- a failure stored in one game does not reach the next

    def test_forget_failed_lookups_reads_a_failed_player_again_in_the_next_game(self) -> None:
        """The loader stores a failed lookup for 600 s (about 33 games) and calls
        forget_failed_lookups before each game. Without the call, the next game with
        the same player got the stored failure: no read, no count, no props."""
        provider = _Counted(fail={"people"})
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts", line_type="closing") == []
        assert provider.read_failures == 1
        provider.fail.clear()  # MLB answers again; the clock stays inside the time-to-live
        # Without the call: the stored failure answers, with no read and no count.
        assert provider.get_prop_odds_by_book(746438, 1, "strikeouts", line_type="closing") == []
        assert provider.reads.count("people") == 1 and provider.read_failures == 1
        # With the call: the next game reads the player again and gets his props.
        provider.forget_failed_lookups()
        rows = provider.get_prop_odds_by_book(746439, 1, "strikeouts", line_type="closing")
        assert rows and {r["game_pk"] for r in rows} == {746439}
        assert provider.reads.count("people") == 2 and provider.read_failures == 1

    def test_after_forget_a_failure_that_repeats_counts_in_the_next_game(self) -> None:
        provider = _Counted(fail={"people"})
        provider.get_prop_odds_by_book(746437, 1, "strikeouts")
        provider.forget_failed_lookups()
        assert provider.get_prop_odds_by_book(746438, 1, "strikeouts") == []
        assert provider.reads.count("people") == 2
        assert provider.read_failures == 2  # the second game is marked incomplete too

    def test_forget_failed_lookups_keeps_the_found_values(self) -> None:
        provider = _Counted()
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts", line_type="closing")
        provider.forget_failed_lookups()
        assert provider.get_prop_odds_by_book(746437, 1, "strikeouts", line_type="closing")
        assert provider.reads.count("people") == 1
        assert provider.reads.count("schedule") == 1 and provider.reads.count("events") == 1

    def test_a_first_five_exclusion_counts_nothing(self) -> None:
        """DraftKings (12) is dated-excluded on a 2025 game; book 24 copies its own
        first-inning lines (the twin rule); BetMGM (19) stays."""
        f5 = _runline_offers({19: (-110, -110), 24: (-150, 120), 12: (-105, -115)})
        f1 = _runline_offers({19: (-300, 220), 24: (-150, 120), 12: (-240, 180)})
        provider = _Counted(
            schedule=_schedule(officialDate="2025-08-15"), offers={283: f5, 282: f1}
        )
        rows = provider.get_odds_by_book(746437, line_type="closing", market_type="f5_runline")
        assert [r["book"] for r in rows] == ["bp:19"]
        assert provider.read_failures == 0


# --------------------------------------------------------------------------- the two together


class TestRetryKeepsTheCountAtZero:
    def test_a_retried_schedule_read_is_not_a_failure(self, urlopen) -> None:
        urlopen(_http_error(503), _load("mlb_schedule_746437.json"))
        provider, waits = _provider(max_retries=1)
        assert provider._resolve_game_meta(746437) is not None
        assert waits == [2.0] and provider.read_failures == 0

    def test_without_a_retry_the_same_burst_is_a_counted_failure(self, urlopen) -> None:
        urlopen(_http_error(503), _load("mlb_schedule_746437.json"))
        provider, waits = _provider(max_retries=0)
        assert provider._resolve_game_meta(746437) is None
        assert waits == [] and provider.read_failures == 1
