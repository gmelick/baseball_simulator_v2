"""The event match: an MLB game resolved to its BettingPros event, with the
caches of found and failed lookups, and the offer reads of a matched event.
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

log = logging.getLogger("pipeline.bettingpros_odds_provider")


class EventMatchMixin(TransportMixin):
    """Resolves a game to its BettingPros event and reads the event's offers."""

    # ------------------------------------------------------ identifier bridges
    def _resolve_game_meta(self, game_pk: int) -> tuple[str, str, str, datetime | None] | None:
        """``game_pk`` → (official local date, home name, away name, UTC start time).

        The date MUST come from ``officialDate`` (the schedule's own
        local-calendar-date field), never from truncating ``gameDate`` (a UTC
        timestamp). For any West-/Mountain-time night game, the UTC clock has
        already rolled past midnight into the next calendar day while the game
        is still being played on ``officialDate`` — truncating ``gameDate``
        then queries BettingPros for the WRONG day's slate, which most often
        pairs the game with a different one between the same two teams the
        following day. A live check (2026-09-08) found this on 5 of 15 games
        that day — every West-Coast game, exactly the affected set.

        The UTC start time (from ``gameDate``, unlike ``officialDate`` this one
        legitimately needs to stay in UTC) is returned too, so
        :meth:`_resolve_event` can pick the right BettingPros event by actual
        start time instead of guessing.

        A resolved game is cached for the process lifetime. A failed
        read returns ``None`` and is kept for one time-to-live
        (:meth:`_failed_recently`); the first call after that reads the
        schedule again. A read that raised counts in ``read_failures``; a
        schedule answer without the game does not (the game is absent).

        SIM-555 (2026-10-01): the meta comes from the PLAYED schedule entry,
        not the first one (see :func:`_read_schedule`). A game postponed and
        made up later used to resolve to its postponed entry, whose start is
        the original one, so the matcher declined the vendor's event at the
        real start. The same read fills ``_game_schedule_cache`` with the
        facts the matcher and the rows need (:class:`_ScheduleInfo`).
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
                self.read_failures += 1  # The schedule read failed
        if meta is not None:
            self._game_meta_cache[game_pk] = meta
        else:
            self._note_failed_lookup("game", game_pk)
        return meta

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        """``game_pk`` → the matching BettingPros event dict (or None).

        When more than one event matches by team name (a
        double-header), the earlier code always picked the earliest-scheduled
        one — silently returning game 1's odds even when ``game_pk`` was game
        2. It now picks whichever candidate's ``scheduled`` time is CLOSEST to
        the actual game's own start time (from :meth:`_resolve_game_meta`),
        which is what genuinely tells the two games apart. That same
        start-time check also acts as a sanity gate on every match, not only
        double-headers: a match more than :data:`_MAX_EVENT_TIME_DELTA` away
        from the real first pitch is treated as no match, rather than priced
        against whatever the closest name match happened to be.

        A matched event is cached for the process lifetime. No match
        (or a failed read) is kept for one time-to-live
        (:meth:`_failed_recently`), so a repeat call inside that window reads
        nothing, and the first call after it looks again. A failed slate read
        counts in ``read_failures``; no match does not, nor does a slate the
        matcher cannot read (a retry would read the same answer).

        SIM-555 (2026-10-01): with a known start, :meth:`_match_by_start`
        picks the event. It searches the slate of a suspended game's
        resumption too, measures each event against every start of the game,
        and adds the single-event rule for a start the vendor did not move.
        Game 2 of a straight double-header, whose MLB start is a placeholder,
        takes the later of the two teams' two events (:meth:`_match_game_two`).
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
                    # No usable start time to sanity-check against (gameDate was
                    # missing/unparseable) but only one name match anyway — accept it,
                    # matching the pre-SIM-536 behaviour for the common case.
                    event = candidates[0]
                elif len(candidates) > 1:
                    # Multiple candidates and no start time to tell them apart by —
                    # guessing "game 1" is exactly the bug this fix removes, so refuse.
                    log.warning(
                        "BettingPros: %d events matched game_pk %s (double-header) but "
                        "the game's own start time is unknown — refusing to guess which",
                        len(candidates),
                        game_pk,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("BettingPros: event lookup failed for game_pk %s: %s", game_pk, exc)
                if not read_done:
                    self.read_failures += 1  # The events read failed
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

        A nickname suffix-matches the MLB full name ("Tigers" ⊂ "Detroit
        Tigers"). An event without a participant id raises ``KeyError``: the
        slate is malformed, and the caller treats it as no match.
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
        """The vendor event of a game with a known start, or ``None`` (SIM-536; SIM-555).

        ``played`` holds the same-team events of the played date's slate. The
        method adds the same-team events of every other slate of
        ``info.slate_dates`` (a suspended game's resumption date), each event
        once. An event's gap is its smallest distance to any start of the
        game: the played start, then each resumption. The event with the
        smallest gap wins (the played slate first on a tie).

        * A gap of :data:`_MAX_EVENT_TIME_DELTA` (2 h) or less is a match. On a
          double-header this picks the right game.
        * The single-event rule: a larger gap still matches when the event is
          the ONLY same-team event on the played date's slate, the played
          entry is not a double-header (``doubleHeader`` 'N' or absent), and
          the event sits at most :data:`_MAX_SINGLE_EVENT_DELTA` (12 h) from
          the played start. The vendor kept another start time for the game.
          The load guard's closing-stamp rule drops a closing price stamped
          more than 15 minutes after the real first pitch
          (``CLOSING_STAMP_GRACE``); a price inside those 15 minutes stays. A
          WARNING names the event.
        * Any other gap is no match (a WARNING).

        Game 2 of a straight double-header with no listed start
        (:attr:`_ScheduleInfo.start_unknown`) skips the gaps: its start is a
        placeholder 5 minutes after game 1's, so the nearest event is game
        1's. :meth:`_match_game_two` picks the event.

        A failed read of an extra slate counts in ``read_failures`` and the
        match goes on with the slates read, by the 2-hour rule only. A match
        that close to a start is the game whatever the missing slate holds.
        The single-event rule needs every slate: the missing one may hold a
        closer event. The count keeps the game off the loader's done-list,
        so the next run reads the slate again.
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
                self.read_failures += 1  # The extra slate read failed
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

        MLB lists no real start for such a game (``startTimeTBD``); its
        ``gameDate`` is game 1's start plus 5 minutes. The event nearest that
        placeholder is game 1's: game 746773's placeholder is 18:15Z, game 1's
        event 18:10Z, game 2's event about three hours later. The loader's
        logs show such a game 2 matched 5 minutes off, to game 1's event, and
        no event at a placeholder. So the method takes the LATER of
        exactly two same-team events on the played date's slate. Any other
        count, or two events at one time, is no match (a WARNING): game 1's
        event is never game 2's. The rows then measure their stamps from the
        vendor event's start (:meth:`_game_meta_fields`).
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

    # ------------------------------------------------------- SIM-421 caches
    def _is_fresh(self, stored_at: float) -> bool:
        """True while a cache entry stored at ``stored_at`` is inside the time-to-live."""
        return self._clock() - stored_at < self._offers_cache_ttl_s

    def _failed_recently(self, kind: str, key: int) -> bool:
        """True while a failed ``kind`` lookup of ``key`` is inside the time-to-live.

        ``kind`` is ``'game'`` (the MLB schedule), ``'event'`` (the vendor
        event) or ``'player'`` (the MLB name). A time-to-live of 0 keeps no
        failure, so every call reads again.
        """
        hit = self._failed_lookups.get((kind, int(key)))
        return hit is not None and self._is_fresh(hit[0])

    def _note_failed_lookup(self, kind: str, key: int) -> None:
        """Keep a failed lookup for one time-to-live (SIM-555; see :meth:`_failed_recently`)."""
        self._store(self._failed_lookups, (kind, int(key)), None)

    def forget_failed_lookups(self) -> None:
        """Drop every stored failed lookup, so the next call reads again.

        The historical loader calls this before each game. A failed player
        lookup is stored for one time-to-live (600 s in the loader, about 33
        games) and counted once, in the game where it failed. Without this
        call, a later game that names the same player inside that window gets
        the stored failure: no read, no count, no props for the player, and
        the game goes on the done-list. The cost is one more read per failed
        player per game. A found game, event or name stays cached.
        """
        self._failed_lookups.clear()

    def forget_game(self, game_pk: int) -> None:
        """Drop every cached fact of one game, so the next call reads it again.

        The provider keeps a found game's meta, its schedule facts and its
        vendor event for the process lifetime. A game postponed or suspended
        after its lookup keeps its first entry's facts: the original start,
        the original event, no ``postponed_start``. The live pipeline is the
        one long-lived caller: its schedule poll calls this method for a
        postponed or suspended entry and for a made-up game before its first
        pitch. A stored failed lookup of the game goes too.
        """
        key = int(game_pk)
        self._game_meta_cache.pop(key, None)
        self._game_schedule_cache.pop(key, None)
        self._event_cache.pop(key, None)
        self._failed_lookups.pop(("game", key), None)
        self._failed_lookups.pop(("event", key), None)

    def _store(self, cache: dict[_K, tuple[float, _V]], key: _K, payload: _V) -> None:
        """Put ``payload`` in ``cache`` under ``key`` after dropping every stale entry.

        The sweep bounds the cache to one time-to-live's worth of entries. A
        long-lived provider (the live pipeline, the historical loader) would
        otherwise keep every finished game's payload for the process lifetime.
        A time-to-live of 0 stores nothing.
        """
        if self._offers_cache_ttl_s <= 0:
            return
        stale = [k for k, (stored_at, _) in cache.items() if not self._is_fresh(stored_at)]
        for k in stale:
            del cache[k]
        cache[key] = (self._clock(), payload)

    def _events_for_date(self, date_str: str) -> list[dict[str, Any]]:
        """The BettingPros events of one date, served from the per-date cache while fresh.

        Every game on a date shares one ``/events`` response; before SIM-421
        each game_pk fetched the whole slate again. A fetch failure raises to
        the caller (``_resolve_event`` logs it) and is not cached, so the next
        game retries.
        """
        hit = self._events_by_date_cache.get(date_str)
        if hit is not None and self._is_fresh(hit[0]):
            return hit[1]
        data = self._bp_get("events", {"sport": "MLB", "date": date_str})
        events = list(data.get("events", []))
        self._store(self._events_by_date_cache, date_str, events)
        return events

    def _offers(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """Every offer of one (event, market), served from the offers cache while fresh.

        One response carries every player's offer for the market and serves
        every book and both line types, so one fetch per (event, market) per
        time-to-live replaces one fetch per (player, market, book, line type).
        A page-1 fetch failure raises to the caller. A later-page failure
        returns the pages in hand. Neither result is cached, so the next call
        fetches again; only a complete market is stored.
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
        """GET ``/offers`` for one (event, market) and follow its pagination.

        The API pages at 10 offers (``_pagination.total_pages``). A batter
        market lists every hitter in the game, so page 1 alone silently
        dropped the players on later pages — their quotes came back empty.

        Returns ``(offers, complete)``. Page 1 raises on failure. A later page
        that fails logs a warning naming the event, the market and the page,
        and the offers already in hand come back with ``complete=False`` so
        the players on the earlier pages keep their quotes. A ``_pagination``
        block that is not a dict counts as one page.
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
                self.read_failures += 1  # The market comes back partial
                return offers, False
            offers.extend(more.get("offers", []))
        return offers, True

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name.

        SIM-555 (review fix 2026-09-28): a found name is cached for the process
        lifetime. A failed lookup is kept for one time-to-live only
        (:meth:`_failed_recently`); it used to be cached as ``None`` for the
        process lifetime, so one timeout cost the player every prop for the
        rest of a season's load or of the live pipeline's day. A read that
        raised counts in ``read_failures``; an answer without the player does
        not.
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
                self.read_failures += 1  # The people read failed
        if name is None:
            self._note_failed_lookup("player", player_id)
        else:
            self._player_name_cache[player_id] = name
        return name

    # ------------------------------------------------ SIM-555: one row per book
    def _game_meta_fields(
        self, game_pk: int, event: Mapping[str, Any] | None = None
    ) -> tuple[datetime | None, date | None, str | None]:
        """(scheduled start as aware UTC, official date, official date text) for a game.

        Read from :meth:`_resolve_game_meta` (cached; :meth:`_resolve_event`
        fills it first). The start is for the load guard only; the date drives
        the dated first-five exclusion.

        Game 2 of a straight double-header with no listed start
        (:attr:`_ScheduleInfo.start_unknown`) takes ``event``'s start instead.
        MLB's placeholder sits 5 minutes after game 1's start, so the guard
        would refuse every closing price of game 2 as late. The vendor's start
        is an estimate: when game 1 ends early, game 2 starts before it, and an
        in-play price stamped before the vendor's start plus 15 minutes then
        passes the guard.
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

        ``None`` for any other game, and for a game whose schedule facts the
        provider did not read (a subclass that resolves the meta another
        way). Every row of a made-up game carries it as ``postponed_start``:
        the load guard refuses a price stamped at or before it (the price of
        the game that was not played).
        """
        info = self._game_schedule_cache.get(game_pk)
        if info is None or info.postponed_start is None:
            return None
        return info.postponed_start.replace(tzinfo=UTC)
