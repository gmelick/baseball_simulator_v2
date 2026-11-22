"""Find BettingPros' event for an MLB game.

The module holds the HTTP client every BettingPros read uses (the retries and the
caches), the MLB schedule read, and the match of a BettingPros event to an MLB game.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, NamedTuple, TypeVar

_K = TypeVar("_K")


_V = TypeVar("_V")


def _parse_utc(value: str) -> datetime | None:
    """Parse a UTC timestamp from either source as a naive datetime, or return ``None``.

    The MLB schedule writes ``"2026-09-08T22:35:00Z"``.
    BettingPros writes ``"2026-09-08 22:35:00"``, with no zone, but also in UTC.
    So the naive datetimes from the two sources compare directly.
    """
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _normalize_name(name: str) -> str:
    """Return the name key: lower case, no accents or punctuation, single spaces."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in ascii_only)
    return " ".join(kept.lower().split())


#: The largest gap between a game's start and its BettingPros event.
#: A larger gap is no match, so the game never takes another game's prices.
_MAX_EVENT_TIME_DELTA = timedelta(hours=2)


#: The largest gap the single-event rule accepts. The vendor sometimes keeps
#: a game's first start time after MLB moves it. For example, MLB moved game
#: 745169 to 18:15Z, and the vendor's event stayed at 00:15Z.
_MAX_SINGLE_EVENT_DELTA = timedelta(hours=12)


class _ScheduleInfo(NamedTuple):
    """The MLB schedule facts of one game, beyond the meta 4-tuple.

    The meta 4-tuple is (official date, home name, away name, start).
    Every time is naive UTC, as :func:`_parse_utc` gives it.
    """

    #: The vendor slates to search: the played date first, then the date of
    #: each later entry that is not postponed (a suspended game's resumption).
    slate_dates: tuple[str, ...]
    #: The played entry's start, then each later start that is not postponed.
    anchor_starts: tuple[datetime, ...]
    #: The played entry's ``doubleHeader`` code ('N', 'Y', 'S'), or ``None``.
    double_header: str | None
    #: The start of the last postponed entry before the played one, or ``None``.
    postponed_start: datetime | None
    #: The played entry's position among the game's schedule entries (0 = first).
    played_entry: int = 0
    #: The number of schedule entries the game has.
    n_entries: int = 1
    #: True when every entry is postponed: the game was never played.
    never_played: bool = False
    #: The played entry's ``gameNumber`` (1, or 2 on a double-header), or ``None``.
    game_number: int | None = None
    #: The played entry's ``status.startTimeTBD``: MLB lists no real start.
    start_tbd: bool = False

    @property
    def start_unknown(self) -> bool:
        """True for game 2 of a straight double-header with no listed start.

        MLB sets ``startTimeTBD`` on such a game. Its ``gameDate`` is a
        placeholder: game 1's start plus 5 minutes.
        """
        return self.double_header == "Y" and self.game_number == 2 and self.start_tbd


def _lone_schedule(date_str: str, game_dt: datetime | None) -> _ScheduleInfo:
    """The schedule facts of a game with one entry.

    The matcher uses them when the schedule cache holds nothing for the game.
    """
    anchors = () if game_dt is None else (game_dt,)
    return _ScheduleInfo((date_str,), anchors, None, None)


def _is_postponed(game: Mapping[str, Any]) -> bool:
    """True when an MLB schedule entry is a postponed game.

    The entry is postponed when ``detailedState`` starts with "Postponed" or
    ``codedGameState`` is "D". A suspended game is not postponed: MLB marks
    both of its entries "Final".
    """
    status = game.get("status") or {}
    detailed = str(status.get("detailedState") or "")
    return detailed.startswith("Postponed") or status.get("codedGameState") == "D"


def _read_schedule(
    data: Mapping[str, Any],
) -> tuple[tuple[str, str, str, datetime | None], _ScheduleInfo]:
    """The meta 4-tuple and the schedule facts of one ``schedule?gamePk=`` answer.

    A made-up game has a postponed entry at the original start, then a
    played entry. A suspended game has two "Final" entries: the first pitch
    and the resumption. The PLAYED entry is the first entry that is not
    postponed, or the first entry when every entry is postponed. The meta
    comes from the played entry, so a suspended game keeps its original
    first pitch.

    Raises ``LookupError`` when the answer lists no entry. Raises ``KeyError``
    when the played entry lacks a field the meta needs.
    """
    entries: list[tuple[Any, Mapping[str, Any]]] = [
        (day.get("date"), game)
        for day in data.get("dates") or []
        for game in day.get("games") or []
    ]
    if not entries:
        raise LookupError("the schedule lists no entry for the game")
    played_i = next((i for i, (_d, g) in enumerate(entries) if not _is_postponed(g)), 0)
    played = entries[played_i][1]
    date_str = str(played["officialDate"])
    home = str(played["teams"]["home"]["team"]["name"])
    away = str(played["teams"]["away"]["team"]["name"])
    game_dt = _parse_utc(str(played.get("gameDate", "")))
    slate_dates = [date_str]
    anchors = [] if game_dt is None else [game_dt]
    for day_date, game in entries[played_i + 1 :]:
        if _is_postponed(game):
            continue
        for value in (day_date, game.get("officialDate")):
            if value and str(value) not in slate_dates:
                slate_dates.append(str(value))
        resumed = _parse_utc(str(game.get("gameDate", "")))
        if resumed is not None and resumed not in anchors:
            anchors.append(resumed)
    postponed_start: datetime | None = None
    for _day_date, game in entries[:played_i]:
        parsed = _parse_utc(str(game.get("gameDate", "")))
        if parsed is not None:
            postponed_start = parsed
    double_header = played.get("doubleHeader")
    game_number = played.get("gameNumber")
    info = _ScheduleInfo(
        slate_dates=tuple(slate_dates),
        anchor_starts=tuple(anchors),
        double_header=None if double_header is None else str(double_header),
        postponed_start=postponed_start,
        played_entry=played_i,
        n_entries=len(entries),
        never_played=_is_postponed(played),
        game_number=game_number if isinstance(game_number, int) else None,
        start_tbd=(played.get("status") or {}).get("startTimeTBD") is True,
    )
    return (date_str, home, away, game_dt), info


class ProviderBase:
    """The provider's settings and caches, declared once for every part of the provider.

    Each part is a mixin: a class that adds methods to the provider class.
    """

    API_KEY_ENV: ClassVar[str]
    MAX_RETRIES_ENV: ClassVar[str]
    RETRY_WAIT_ENV: ClassVar[str]
    DEFAULT_MAX_RETRIES: ClassVar[int]
    DEFAULT_RETRY_WAIT_S: ClassVar[float]
    RETRY_WAIT_CAP_S: ClassVar[float]
    OFFERS_CACHE_TTL_ENV: ClassVar[str]
    DEFAULT_OFFERS_CACHE_TTL_S: ClassVar[float]
    _api_key: str | None
    _timeout: float
    _offers_cache_ttl_s: float
    _clock: Callable[[], float]
    _max_retries: int
    _retry_wait_s: float
    _retry_sleep: Callable[[float], None]
    read_failures: int
    _event_cache: dict[int, dict[str, Any]]
    _game_meta_cache: dict[int, tuple[str, str, str, datetime | None]]
    _game_schedule_cache: dict[int, _ScheduleInfo]
    _player_name_cache: dict[int, str]
    _failed_lookups: dict[tuple[str, int], tuple[float, None]]
    _offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]]
    _events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]]
    _f5_logged: set[tuple[int, int, str]]


log = logging.getLogger("betting.bettingpros")


_BP_BASE = "https://api.bettingpros.com/v3"


_MLB_BASE = "https://statsapi.mlb.com/api/v1"


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


class EventMatchMixin(TransportMixin):
    """Resolves a game to its BettingPros event and reads the event's offers."""

    # ------------------------------------------------------ identifier bridges
    def _resolve_game_meta(self, game_pk: int) -> tuple[str, str, str, datetime | None] | None:
        """``game_pk`` → (official local date, home name, away name, UTC start).

        The date comes from ``officialDate``, never from the UTC ``gameDate``.
        A West-Coast night game passes midnight UTC, so ``gameDate`` names the
        next day's slate.

        The meta comes from the played schedule entry (:func:`_read_schedule`).
        The same read fills ``_game_schedule_cache``. A found game stays
        cached for the process lifetime. A failure returns ``None`` and stays
        for one time-to-live. A read that raised counts in ``read_failures``.
        """
        if game_pk in self._game_meta_cache:
            return self._game_meta_cache[game_pk]
        if self._failed_recently("game", game_pk):
            return None
        meta: tuple[str, str, str, datetime | None] | None = None
        read_done = False
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            read_done = True
            meta, info = _read_schedule(data)
            self._game_schedule_cache[game_pk] = info
            if info.never_played:
                log.info(
                    "BettingPros: every schedule entry of game_pk %s is postponed "
                    "(the game was not played); matching on its first entry",
                    game_pk,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve game_pk %s: %s", game_pk, exc)
            if not read_done:
                self.read_failures += 1  # the schedule read failed
        if meta is not None:
            self._game_meta_cache[game_pk] = meta
        else:
            self._note_failed_lookup("game", game_pk)
        return meta

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        """``game_pk`` → its BettingPros event, or ``None``.

        With a known start, :meth:`_match_by_start` picks the event. Without
        one, the method takes a lone same-team event and refuses two.

        A matched event stays cached for the process lifetime. No match stays
        for one time-to-live. A failed slate read counts in ``read_failures``;
        no match does not.
        """
        if game_pk in self._event_cache:
            return self._event_cache[game_pk]
        if self._failed_recently("event", game_pk):
            return None
        event: dict[str, Any] | None = None
        meta = self._resolve_game_meta(game_pk)
        if meta is not None:
            date_str, home_name, away_name, game_dt = meta
            home_n, away_n = _normalize_name(home_name), _normalize_name(away_name)
            read_done = False
            try:
                slate = self._events_for_date(date_str)
                read_done = True
                candidates = self._same_team_events(slate, home_n, away_n)
                if game_dt is not None:
                    info = self._game_schedule_cache.get(game_pk) or _lone_schedule(
                        date_str, game_dt
                    )
                    event = self._match_by_start(
                        game_pk, candidates, info, date_str, game_dt, home_n, away_n
                    )
                elif len(candidates) == 1:
                    # No start to check against, but only one team match: take it.
                    event = candidates[0]
                elif len(candidates) > 1:
                    # Two or more matches and no start to tell them apart: refuse.
                    log.warning(
                        "BettingPros: %d events matched game_pk %s (double-header) but "
                        "the game's own start time is unknown — refusing to guess which",
                        len(candidates),
                        game_pk,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("BettingPros: event lookup failed for game_pk %s: %s", game_pk, exc)
                if not read_done:
                    self.read_failures += 1  # the events read failed
        if event is not None:
            self._event_cache[game_pk] = event
        else:
            self._note_failed_lookup("event", game_pk)
        return event

    @staticmethod
    def _same_team_events(
        slate: list[dict[str, Any]], home_n: str, away_n: str
    ) -> list[dict[str, Any]]:
        """The slate's events whose home and visitor nicknames match the two MLB teams.

        A nickname matches the end of the MLB name ("Tigers" in "Detroit
        Tigers"). A participant without an id raises ``KeyError``, and the
        caller treats it as no match.
        """
        candidates = []
        for e in slate:
            parts = {p["id"]: _normalize_name(p["name"]) for p in e.get("participants", [])}
            home_nick = parts.get(e.get("home"), "")
            away_nick = parts.get(e.get("visitor"), "")
            if home_n.endswith(home_nick) and away_n.endswith(away_nick) and home_nick:
                candidates.append(e)
        return candidates

    def _match_by_start(
        self,
        game_pk: int,
        played: list[dict[str, Any]],
        info: _ScheduleInfo,
        date_str: str,
        game_dt: datetime,
        home_n: str,
        away_n: str,
    ) -> dict[str, Any] | None:
        """The vendor event of a game with a known start, or ``None``.

        ``played`` holds the same-team events of the played date's slate. The
        method adds the same-team events of each later slate in
        ``info.slate_dates``. The event with the smallest gap to any start of
        the game wins; the played slate wins a tie.

        * A gap of 2 hours or less is a match.
        * The single-event rule takes a gap of up to 12 hours (a WARNING).
          The event must be the only same-team event on the played slate, and
          the game is not a double-header.
        * Any other gap is no match (a WARNING).

        Game 2 of a straight double-header with no listed start goes to
        :meth:`_match_game_two`. When a later slate fails to read, the method
        counts the failure and uses the 2-hour rule only.
        """
        if info.start_unknown:
            return self._match_game_two(game_pk, played, info)
        candidates = list(played)
        slate_of = {id(e): date_str for e in played}
        seen = {e.get("id") for e in played if e.get("id") is not None}
        extra_failed = False
        for extra_date in info.slate_dates[1:]:
            if extra_date == date_str:
                continue
            try:
                extra = self._events_for_date(extra_date)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "BettingPros: the %s slate (a later date of game_pk %s) could not be "
                    "read; matching on the slates read, by the two-hour rule only: %s",
                    extra_date,
                    game_pk,
                    exc,
                )
                self.read_failures += 1  # a later date's slate read failed
                extra_failed = True
                continue
            for e in self._same_team_events(extra, home_n, away_n):
                key = e.get("id")
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                candidates.append(e)
                slate_of[id(e)] = extra_date
        if not candidates:
            return None
        anchors = (game_dt, *(a for a in info.anchor_starts if a != game_dt))

        def _gap(candidate: dict[str, Any]) -> tuple[timedelta, int]:
            """(the smallest gap to a start, the index of that start)."""
            c_dt = _parse_utc(str(candidate.get("scheduled", "")))
            if c_dt is None:
                return timedelta.max, 0
            gaps = [abs(c_dt - a) for a in anchors]
            nearest = min(range(len(gaps)), key=gaps.__getitem__)
            return gaps[nearest], nearest

        best = min(candidates, key=lambda c: _gap(c)[0])
        best_gap, anchor_i = _gap(best)
        from_played = slate_of[id(best)] == date_str
        event: dict[str, Any] | None = None
        if best_gap <= _MAX_EVENT_TIME_DELTA:
            event = best
            if len(candidates) > 1:
                log.info(
                    "BettingPros: %d events matched game_pk %s (double-header); "
                    "picked scheduled=%s (%s from the real start time)",
                    len(candidates),
                    game_pk,
                    event.get("scheduled"),
                    best_gap,
                )
        else:
            best_dt = _parse_utc(str(best.get("scheduled", "")))
            played_gap = None if best_dt is None else abs(best_dt - game_dt)
            if (
                from_played
                and len(played) == 1
                and info.double_header in (None, "N")
                and not extra_failed
                and played_gap is not None
                and played_gap <= _MAX_SINGLE_EVENT_DELTA
            ):
                event = best
                log.warning(
                    "BettingPros: game_pk %s matched event %s (scheduled=%s) by the "
                    "single-event rule. The event is %s from the real start %s, over the "
                    "%s limit. It is the two teams' only event that day, so the vendor "
                    "kept another start time for the game. The load guard drops a "
                    "closing price stamped more than 15 minutes after the real first pitch",
                    game_pk,
                    best.get("id"),
                    best.get("scheduled"),
                    played_gap,
                    game_dt,
                    _MAX_EVENT_TIME_DELTA,
                )
            else:
                log.warning(
                    "BettingPros: closest event (scheduled=%s) for game_pk %s is "
                    "%s from the real start time — over the %s sanity limit, "
                    "treating as unmatched rather than risk the wrong game",
                    best.get("scheduled"),
                    game_pk,
                    best_gap,
                    _MAX_EVENT_TIME_DELTA,
                )
        if event is not None and (info.played_entry > 0 or not from_played or anchor_i > 0):
            notes = []
            if info.played_entry > 0:
                notes.append(
                    f"the game was postponed from {info.postponed_start} and made up "
                    f"(schedule entry {info.played_entry + 1} of {info.n_entries})"
                )
            if anchor_i > 0:
                notes.append(f"the event matches the resumption at {anchors[anchor_i]}")
            if not from_played:
                notes.append(f"the event sits on a later date's slate, not on {date_str}'s")
            log.info(
                "BettingPros: game_pk %s matched event %s (scheduled=%s) on the %s slate: %s",
                game_pk,
                event.get("id"),
                event.get("scheduled"),
                slate_of[id(event)],
                "; ".join(notes),
            )
        return event

    def _match_game_two(
        self, game_pk: int, played: list[dict[str, Any]], info: _ScheduleInfo
    ) -> dict[str, Any] | None:
        """The vendor event of game 2 of a straight double-header with no listed start.

        The placeholder start sits nearest game 1's event. So the method takes
        the LATER of exactly two same-team events on the played slate. Any
        other count, or two events at one time, is no match (a WARNING).
        """
        starts = [(_parse_utc(str(e.get("scheduled", ""))), e) for e in played]
        timed = [(start, e) for start, e in starts if start is not None]
        if len(played) != 2 or len(timed) != 2 or timed[0][0] == timed[1][0]:
            log.warning(
                "BettingPros: game_pk %s is game 2 of a straight double-header with no "
                "listed start; the played slate holds %d same-team events (%s), not two at "
                "two times, so treating it as unmatched rather than take game 1's event",
                game_pk,
                len(played),
                ", ".join(str(e.get("scheduled")) for e in played) or "none",
            )
            return None
        event = max(timed, key=lambda pair: pair[0])[1]
        made_up = ""
        if info.played_entry > 0:
            made_up = (
                f"; the game was postponed from {info.postponed_start} and made up "
                f"(schedule entry {info.played_entry + 1} of {info.n_entries})"
            )
        log.info(
            "BettingPros: game_pk %s matched event %s (scheduled=%s): game 2 of a straight "
            "double-header with no listed start, the later of the two events%s",
            game_pk,
            event.get("id"),
            event.get("scheduled"),
            made_up,
        )
        return event

    # ------------------------------------------------------- the caches
    def _is_fresh(self, stored_at: float) -> bool:
        """True while a cache entry stored at ``stored_at`` is inside the time-to-live."""
        return self._clock() - stored_at < self._offers_cache_ttl_s

    def _failed_recently(self, kind: str, key: int) -> bool:
        """True while a failed ``kind`` lookup of ``key`` is inside the time-to-live.

        ``kind`` is ``'game'``, ``'event'`` or ``'player'``.
        """
        hit = self._failed_lookups.get((kind, int(key)))
        return hit is not None and self._is_fresh(hit[0])

    def _note_failed_lookup(self, kind: str, key: int) -> None:
        """Keep a failed lookup for one time-to-live (see :meth:`_failed_recently`)."""
        self._store(self._failed_lookups, (kind, int(key)), None)

    def forget_failed_lookups(self) -> None:
        """Drop every stored failed lookup, so the next call reads again.

        The historical loader calls this before each game. Without it, a
        failure stored in one game cuts the next game short without a count
        in ``read_failures``.
        """
        self._failed_lookups.clear()

    def forget_game(self, game_pk: int) -> None:
        """Drop every cached fact of one game, so the next call reads it again.

        A game postponed or suspended after its lookup otherwise keeps its old
        start and event. The live pipeline calls this for such a game.
        """
        key = int(game_pk)
        self._game_meta_cache.pop(key, None)
        self._game_schedule_cache.pop(key, None)
        self._event_cache.pop(key, None)
        self._failed_lookups.pop(("game", key), None)
        self._failed_lookups.pop(("event", key), None)

    def _store(self, cache: dict[_K, tuple[float, _V]], key: _K, payload: _V) -> None:
        """Put ``payload`` in ``cache`` under ``key`` after dropping every stale entry.

        The drop holds a cache to one time-to-live's worth of entries.
        A time-to-live of 0 stores nothing.
        """
        if self._offers_cache_ttl_s <= 0:
            return
        stale = [k for k, (stored_at, _) in cache.items() if not self._is_fresh(stored_at)]
        for k in stale:
            del cache[k]
        cache[key] = (self._clock(), payload)

    def _events_for_date(self, date_str: str) -> list[dict[str, Any]]:
        """The BettingPros events of one date, from the per-date cache while fresh.

        A read failure raises to the caller and is not cached.
        """
        hit = self._events_by_date_cache.get(date_str)
        if hit is not None and self._is_fresh(hit[0]):
            return hit[1]
        data = self._bp_get("events", {"sport": "MLB", "date": date_str})
        events = list(data.get("events", []))
        self._store(self._events_by_date_cache, date_str, events)
        return events

    # ------------------------------------------------ one row per book
    def _game_meta_fields(
        self, game_pk: int, event: Mapping[str, Any] | None = None
    ) -> tuple[datetime | None, date | None, str | None]:
        """(start as aware UTC, official date, official date text) for a game.

        The load guard reads the start. Game 2 of a straight double-header
        with no listed start takes ``event``'s start instead of MLB's
        placeholder. The vendor's start is an estimate, so an early game 2
        can let an in-play price pass the guard.
        """
        meta = self._resolve_game_meta(game_pk)
        if meta is None:
            return None, None, None
        date_str, _home, _away, game_dt = meta
        info = self._game_schedule_cache.get(game_pk)
        if event is not None and info is not None and info.start_unknown:
            vendor_start = _parse_utc(str(event.get("scheduled", "")))
            if vendor_start is not None:
                game_dt = vendor_start
        start = None if game_dt is None else game_dt.replace(tzinfo=UTC)
        try:
            official = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            official = None
        return start, official, date_str

    def _postponed_start(self, game_pk: int) -> datetime | None:
        """The original start of a game postponed and made up later, as aware UTC.

        The method gives ``None`` for any other game, or when the schedule
        facts are missing. The load guard refuses a price stamped no later
        than 15 minutes after this start.
        """
        info = self._game_schedule_cache.get(game_pk)
        if info is None or info.postponed_start is None:
            return None
        return info.postponed_start.replace(tzinfo=UTC)
