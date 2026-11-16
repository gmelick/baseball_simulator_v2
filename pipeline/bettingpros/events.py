"""The event match: an MLB game resolved to its BettingPros event.

The module also holds the caches and the paged read of an event's offers.

The match:

* The matcher reads the PLAYED schedule entry: the first one not postponed.
  It also searches the vendor slate of each later entry, because the vendor
  may move a suspended game to its resumption date.
* An event's gap is its distance to the nearest start of the game.
  A gap of 2 hours or less matches.
* The single-event rule takes a gap of up to 12 hours. The event must be the
  two teams' only event on the played date, and the game is not a
  double-header. The vendor kept another start time for the game.
* Game 2 of a straight double-header has a placeholder start, 5 minutes after
  game 1's. The matcher takes the later of the two teams' two events. The
  rows measure their stamps from that event's start.
* Every row of a made-up game carries ``postponed_start``. The load guard
  refuses a price stamped no later than 15 minutes after it.

The caches:

* One ``/offers`` response for an (event, market) serves every player, every
  book and both line types. The offers cache keeps it for one time-to-live
  (TTL), so a game costs one read per market, not one per player and book.
* A per-date cache keeps each date's ``/events`` list for one TTL.
* The TTL is 30 seconds by default, half the live pipeline's 60-second fetch
  cycle, so each cycle sees a fresh line. ``ODDS_OFFERS_CACHE_TTL_S`` sets it,
  and 0 turns the caches off.
* Each store first drops every entry past the TTL. So a long-lived provider
  holds at most one TTL's worth of payloads.
* A found game, event or player name stays for the process lifetime.
  A failed lookup stays for one TTL only, so a game BettingPros lists later
  is found on a later call.
* An ``/offers`` market is paged. A failure on page 1 raises. A failure on a
  later page logs a warning and returns the pages in hand. Neither result is
  cached.
* Each caught read failure adds 1 to ``read_failures``. The loader uses the
  count to keep a game that a vendor error cut short off its done-list.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from typing import Any

from pipeline.bettingpros.parsing import (
    _K,
    _V,
    _normalize_name,
    _parse_utc,
)
from pipeline.bettingpros.schedule import (
    _MAX_EVENT_TIME_DELTA,
    _MAX_SINGLE_EVENT_DELTA,
    _lone_schedule,
    _read_schedule,
    _ScheduleInfo,
)
from pipeline.bettingpros.transport import _MAX_OFFER_PAGES, TransportMixin

log = logging.getLogger("pipeline.bettingpros")


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

    def _offers(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """Every offer of one (event, market), from the offers cache while fresh.

        Only a complete market is cached.
        """
        key = (int(event_id), int(market_id))
        hit = self._offers_cache.get(key)
        if hit is not None and self._is_fresh(hit[0]):
            return hit[1]
        offers, complete = self._fetch_offers_all_pages(event_id, market_id)
        if complete:
            self._store(self._offers_cache, key, offers)
        return offers

    def _fetch_offers_all_pages(
        self, event_id: int, market_id: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """GET every ``/offers`` page of one (event, market).

        The vendor pages at 10 offers, so a batter market needs every page
        to list every hitter. Returns ``(offers, complete)``. A failure on
        page 1 raises. A failure on a later page logs a warning and returns
        the offers in hand with ``complete=False``. A ``_pagination`` block
        that is not a dict counts as one page.
        """
        params: dict[str, Any] = {"sport": "MLB", "market_id": market_id, "event_id": event_id}
        data = self._bp_get("offers", params)
        offers = list(data.get("offers", []))
        pagination = data.get("_pagination")
        total_pages = 1
        if isinstance(pagination, dict):
            try:
                total_pages = int(pagination.get("total_pages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
        for page in range(2, min(total_pages, _MAX_OFFER_PAGES) + 1):
            try:
                more = self._bp_get("offers", {**params, "page": page})
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "BettingPros: offers page %d of %d failed (event %s, market %s); "
                    "using the %d offers in hand: %s",
                    page,
                    total_pages,
                    event_id,
                    market_id,
                    len(offers),
                    exc,
                )
                self.read_failures += 1  # the market comes back partial
                return offers, False
            offers.extend(more.get("offers", []))
        return offers, True

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name.

        A found name stays cached for the process lifetime. A failure stays
        for one time-to-live. A read that raised counts in ``read_failures``.
        """
        if player_id in self._player_name_cache:
            return self._player_name_cache[player_id]
        if self._failed_recently("player", player_id):
            return None
        name: str | None = None
        read_done = False
        try:
            data = self._mlb_get(f"people/{player_id}", {})
            read_done = True
            full = data["people"][0]["fullName"]
            name = _normalize_name(str(full))
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve player_id %s: %s", player_id, exc)
            if not read_done:
                self.read_failures += 1  # the player read failed
        if name is None:
            self._note_failed_lookup("player", player_id)
        else:
            self._player_name_cache[player_id] = name
        return name

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
