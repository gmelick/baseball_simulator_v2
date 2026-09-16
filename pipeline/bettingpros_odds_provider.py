"""
pipeline/bettingpros_odds_provider.py
=====================================
SIM-405 — a real :class:`~pipeline.odds_provider.OddsProvider` backed by the
BettingPros v3 API (https://api.bettingpros.com/v3), replacing the SIM-370 stub.

Conforms to the structural ``OddsProvider`` protocol (``get_odds`` /
``get_prop_odds`` returning the ``MockOddsAPI`` dict shapes) so it is a drop-in:
set ``ODDS_PROVIDER=bettingpros`` (+ ``ODDS_API_KEY``) and the live pipeline /
betting routes use real lines instead of the deterministic mock.

How it bridges identifiers (the provider only receives ``game_pk`` / MLB
``player_id``, never names) — both via the public MLB Stats API, cached:
  * ``game_pk`` → BettingPros event: resolve the game's date + team names from
    the MLB schedule, then match the BettingPros ``events?date=…`` entry whose
    home/visitor nicknames suffix-match the MLB team names (double-headers
    disambiguated by scheduled time). Nickname-suffix matching avoids
    abbreviation-convention drift between the two APIs.
  * MLB ``player_id`` → prop offer: resolve the player's name from the MLB
    people endpoint, then match the BettingPros prop offer participant by
    normalized first+last name.

Markets (discovered from /v3/markets?sport=MLB; the eight SIM-421 prop ids
were verified against the owner's scraper, the twelve segment ids against a
live event on 2026-09-12):
  game:    moneyline 122, total 175, run-line 176
  segment: f1_moneyline 278, f5_moneyline 279 (three-way: home / away / draw),
           f1_total 280, f5_total 281, f1_runline 282, f5_runline 283,
           team_total_{home,away} 277 (one offer per team), f5_team_total 407,
           first_to_score 286, first_inning_run 369 (yes / no, stored as
           over / under at 0.5)
  pitcher: strikeouts 285, earned_runs 290, walks 408, outs_recorded 405,
           hits_allowed 404
  batter:  hits 287, home_runs 299, total_bases 293, rbis 289, singles 295,
           doubles 291, triples 292, runs 288, stolen_bases 294,
           hits_runs_rbis 403
``hits`` (287) is the batter market and ``hits_allowed`` (404) the pitcher
market; the vocabulary itself lives in ``pipeline/odds_provider.py``.

``line_type``: ``"opening"`` reads each selection's ``opening_line``;
``"closing"`` reads the most-recently-updated line (SIM-435 — the last line the
market posted before game time, the canonical closing-line proxy; BettingPros
has no explicit closing field); any other value reads the current best/main book
line. CLV is therefore available by comparing opening vs closing (or current).
``book``/``is_sharp_book`` are echoed through; a specific BettingPros ``book_id``
can be preferred via ``prefer_book_id`` (it also scopes the closing-line scan).

The offers cache (SIM-421)
--------------------------
One ``/offers`` response for an (event_id, market_id) pair carries EVERY
player's offer for that market, and the same response serves every book and
both the opening and the closing line (each line type is a different field of
the same selection). Before SIM-421 the provider fetched it again for every
(player, market, book, line type) — several hundred HTTP calls per game where
15 would do. The provider now keeps a per-instance cache keyed
``(event_id, market_id)`` with a time-to-live, plus a per-date cache of the
``/events`` list (``_resolve_event`` used to re-fetch the whole day's slate for
every game on that date).

The time-to-live matters because the live pipeline keeps ONE provider for the
process lifetime and fetches props every ``PROP_FETCH_CADENCE_S`` (60) seconds:
a cached snapshot must never be older than that cadence, or a cycle would
persist a stale line as if it were current. The default is 30 seconds (half the
cadence): long enough to collapse one cycle's calls into one fetch per market,
short enough that the next cycle always sees a fresh snapshot. Set it with the
``offers_cache_ttl_s`` constructor argument or the ``ODDS_OFFERS_CACHE_TTL_S``
environment variable (0 disables the cache); the offline historical loader
uses a longer value because its games are over and their lines are final. The
clock is ``time.monotonic()``, injectable for tests.

Both caches evict. A finished game's (event, market) key is never read again,
so a cache that only overwrote keys would hold every payload of a season-long
process (the live pipeline) or of a whole-season backfill (the historical
loader: 2,378 games × 18 markets, several MB per game, inside the app
container's 10 GB memory cap). Each store first drops every entry past the
time-to-live, so a cache holds at most one time-to-live's worth of payloads;
a time-to-live of 0 stores nothing.

A ``/offers`` market is paged. A failure on page 1 raises to the caller. A
failure on a later page logs a warning that names the event, the market and
the page, and returns the pages already in hand, so the players on page 1
keep their quotes. Neither result is cached: the next call fetches again.

HTTP is stdlib ``urllib`` (sync — the protocol methods are sync; the module
stays importable without aiohttp). The two ``_bp_get`` / ``_mlb_get`` seams are
the only network surface and are stubbed in unit tests (fixtures captured under
``tests/fixtures/bettingpros/``); no live call is made in tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    LEGACY_GAME_MARKET_TYPES,
    PROP_STATS,
)

_K = TypeVar("_K")
_V = TypeVar("_V")

log = logging.getLogger("pipeline.bettingpros_odds_provider")

_BP_BASE = "https://api.bettingpros.com/v3"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"

#: SIM-536: the largest gap allowed between the actual game's start time and the
#: BettingPros event we matched it to. A match beyond this is treated as no
#: match at all, rather than silently priced against the wrong game.
_MAX_EVENT_TIME_DELTA = timedelta(hours=2)

#: SIM-421: the most ``/offers`` pages read for one (event, market). The API
#: pages at 10 offers; a batter market lists every hitter in the game (two to
#: three pages), so the cap only guards against a runaway pager.
_MAX_OFFER_PAGES = 20


def _parse_utc(value: str) -> datetime | None:
    """Parse a UTC timestamp from either source, or ``None`` if unparseable.

    The MLB schedule's ``gameDate`` is ISO-8601 (``"2026-09-08T22:35:00Z"``);
    BettingPros' ``scheduled`` is space-separated with no zone marker
    (``"2026-09-08 22:35:00"``) but represents the same UTC instant (verified
    against a live game: both read the identical wall-clock value). Naive
    ``datetime`` objects from both are therefore directly comparable.
    """
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


#: market_type (the GAME_MARKET_TYPES vocabulary) → BettingPros market id.
#: ``run_line`` is a legacy alias of ``runline``. Both team-total markets of a
#: side pair share one BettingPros market: the response holds one offer per
#: team, and :meth:`_team_offer_selections` picks the side's offer.
_GAME_MARKET_IDS: dict[str, int] = {
    "moneyline": 122,
    "total": 175,
    "runline": 176,
    "run_line": 176,
    "f1_moneyline": 278,
    "f5_moneyline": 279,
    "f1_total": 280,
    "f5_total": 281,
    "f1_runline": 282,
    "f5_runline": 283,
    "team_total_home": 277,
    "team_total_away": 277,
    "f5_team_total_home": 407,
    "f5_team_total_away": 407,
    "first_to_score": 286,
    "first_inning_run": 369,
}
if set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS):
    raise RuntimeError(
        "bettingpros_odds_provider: _GAME_MARKET_IDS lacks a market id for "
        f"{sorted(set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS))} — keep it in step "
        "with pipeline.odds_provider.GAME_MARKET_TYPES"
    )

#: prop_stat (the PROP_STATS vocabulary) → BettingPros market id.
_PROP_MARKET_IDS: dict[str, int] = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
    # SIM-421: the eight markets the book already offers.
    "singles": 295,
    "doubles": 291,
    "triples": 292,
    "runs": 288,
    "stolen_bases": 294,
    "hits_runs_rbis": 403,
    "outs_recorded": 405,
    "hits_allowed": 404,
}
if set(_PROP_MARKET_IDS) != set(PROP_STATS):  # pragma: no cover — an import-time guard
    raise RuntimeError(
        "BettingPros market ids and PROP_STATS disagree: "
        f"{sorted(set(_PROP_MARKET_IDS) ^ set(PROP_STATS))}"
    )


def _normalize_name(name: str) -> str:
    """Lower-case, strip accents + non-alphanumerics, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in ascii_only)
    return " ".join(kept.lower().split())


class BettingProsOddsProvider:
    """Real odds provider backed by BettingPros v3 (SIM-405).

    ``offers_cache_ttl_s`` (SIM-421) is the time-to-live of the offers and the
    per-date events caches, in seconds. ``None`` reads ``ODDS_OFFERS_CACHE_TTL_S``
    and falls back to 30 seconds — half the live pipeline's 60-second prop
    cadence, so a cycle never persists a stale snapshot (see the module
    docstring). ``0`` disables the caches. ``clock`` is the monotonic time
    source, injectable for tests.
    """

    API_KEY_ENV = "ODDS_API_KEY"
    #: SIM-421: env var that sets the offers-cache time-to-live (seconds).
    OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"
    #: SIM-421: the default time-to-live — half PROP_FETCH_CADENCE_S (60 s).
    DEFAULT_OFFERS_CACHE_TTL_S = 30.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        prefer_book_id: int | None = None,
        timeout: float = 15.0,
        offers_cache_ttl_s: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.API_KEY_ENV)
        self._prefer_book_id = prefer_book_id
        self._timeout = timeout
        if offers_cache_ttl_s is None:
            offers_cache_ttl_s = float(
                os.environ.get(self.OFFERS_CACHE_TTL_ENV, self.DEFAULT_OFFERS_CACHE_TTL_S)
            )
        self._offers_cache_ttl_s = float(offers_cache_ttl_s)
        self._clock: Callable[[], float] = clock or time.monotonic
        # Per-instance caches (cleared by constructing a new provider).
        self._event_cache: dict[int, dict[str, Any] | None] = {}
        self._game_meta_cache: dict[int, tuple[str, str, str, datetime | None] | None] = {}
        self._player_name_cache: dict[int, str | None] = {}
        # SIM-421: time-stamped caches — (stored_at, payload), served while
        # younger than the time-to-live; every store drops the stale entries.
        self._offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}
        self._events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ----------------------------------------------------------------- HTTP
    def _http_get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed hosts
            return json.loads(resp.read().decode("utf-8"))

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

    # ------------------------------------------------------ identifier bridges
    def _resolve_game_meta(self, game_pk: int) -> tuple[str, str, str, datetime | None] | None:
        """``game_pk`` → (official local date, home name, away name, UTC start time).

        SIM-536: the date MUST come from ``officialDate`` (the schedule's own
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
        """
        if game_pk in self._game_meta_cache:
            return self._game_meta_cache[game_pk]
        meta: tuple[str, str, str, datetime | None] | None = None
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            game = data["dates"][0]["games"][0]
            date_str = str(game["officialDate"])
            home = str(game["teams"]["home"]["team"]["name"])
            away = str(game["teams"]["away"]["team"]["name"])
            game_dt = _parse_utc(str(game.get("gameDate", "")))
            meta = (date_str, home, away, game_dt)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve game_pk %s: %s", game_pk, exc)
        self._game_meta_cache[game_pk] = meta
        return meta

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        """``game_pk`` → the matching BettingPros event dict (or None).

        SIM-536: when more than one event matches by team name (a
        double-header), the earlier code always picked the earliest-scheduled
        one — silently returning game 1's odds even when ``game_pk`` was game
        2. It now picks whichever candidate's ``scheduled`` time is CLOSEST to
        the actual game's own start time (from :meth:`_resolve_game_meta`),
        which is what genuinely tells the two games apart. That same
        start-time check also acts as a sanity gate on every match, not only
        double-headers: a match more than :data:`_MAX_EVENT_TIME_DELTA` away
        from the real first pitch is treated as no match, rather than priced
        against whatever the closest name match happened to be.
        """
        if game_pk in self._event_cache:
            return self._event_cache[game_pk]
        event: dict[str, Any] | None = None
        meta = self._resolve_game_meta(game_pk)
        if meta is not None:
            date_str, home_name, away_name, game_dt = meta
            home_n, away_n = _normalize_name(home_name), _normalize_name(away_name)
            try:
                candidates = []
                for e in self._events_for_date(date_str):
                    parts = {p["id"]: _normalize_name(p["name"]) for p in e.get("participants", [])}
                    home_nick = parts.get(e.get("home"), "")
                    away_nick = parts.get(e.get("visitor"), "")
                    # Nickname suffix-matches the MLB full name ("Tigers" ⊂ "Detroit Tigers").
                    if home_n.endswith(home_nick) and away_n.endswith(away_nick) and home_nick:
                        candidates.append(e)
                if candidates and game_dt is not None:

                    def _delta(candidate: dict[str, Any]) -> timedelta:
                        c_dt = _parse_utc(str(candidate.get("scheduled", "")))
                        return timedelta.max if c_dt is None else abs(c_dt - game_dt)

                    best = min(candidates, key=_delta)
                    best_delta = _delta(best)
                    if best_delta > _MAX_EVENT_TIME_DELTA:
                        log.warning(
                            "BettingPros: closest event (scheduled=%s) for game_pk %s is "
                            "%s from the real start time — over the %s sanity limit, "
                            "treating as unmatched rather than risk the wrong game",
                            best.get("scheduled"),
                            game_pk,
                            best_delta,
                            _MAX_EVENT_TIME_DELTA,
                        )
                    else:
                        event = best
                        if len(candidates) > 1:
                            log.info(
                                "BettingPros: %d events matched game_pk %s (double-header); "
                                "picked scheduled=%s (%s from the real start time)",
                                len(candidates),
                                game_pk,
                                event.get("scheduled"),
                                best_delta,
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
        self._event_cache[game_pk] = event
        return event

    # ------------------------------------------------------- SIM-421 caches
    def _is_fresh(self, stored_at: float) -> bool:
        """True while a cache entry stored at ``stored_at`` is inside the time-to-live."""
        return self._clock() - stored_at < self._offers_cache_ttl_s

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
                return offers, False
            offers.extend(more.get("offers", []))
        return offers, True

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name (cached)."""
        if player_id in self._player_name_cache:
            return self._player_name_cache[player_id]
        name: str | None = None
        try:
            data = self._mlb_get(f"people/{player_id}", {})
            full = data["people"][0]["fullName"]
            name = _normalize_name(str(full))
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve player_id %s: %s", player_id, exc)
        self._player_name_cache[player_id] = name
        return name

    # ------------------------------------------------------------ line picking
    def _pick_line(
        self, selection: dict[str, Any], line_type: str
    ) -> tuple[float | None, float | None]:
        """Return (american_cost, line) for a selection at the requested line_type.

        ``opening`` reads ``selection.opening_line``; ``closing`` reads the LAST
        line posted before game time (SIM-435 — the line whose ``updated`` stamp
        is the most recent, the canonical closing-line proxy: BettingPros has no
        explicit "closing" field, so the latest-updated line as captured at /
        near first pitch IS the closing line); any other value reads the current
        best/main book line.
        """
        if line_type == "opening":
            ol = selection.get("opening_line") or {}
            return _opt_float(ol.get("cost")), _opt_float(ol.get("line"))

        # SIM-435: closing line = the most-recently-updated line (the last line
        # the market posted before game time). When prefer_book_id is set we
        # restrict to that book's lines; otherwise we scan every book's lines and
        # take the max-``updated`` one. This mirrors the SIM-340 "closing line is
        # the latest snapshot at/before first pitch" convention, applied at the
        # line level since one BettingPros offers pull is a single point in time.
        if line_type == "closing":
            best_line: dict[str, Any] | None = None
            best_updated = ""
            for book in selection.get("books", []):
                if self._prefer_book_id is not None and book.get("id") != self._prefer_book_id:
                    continue
                for ln in book.get("lines") or []:
                    updated = str(ln.get("updated") or "")
                    # >= so a later book at the same timestamp can still win, and
                    # an empty-timestamp line is only chosen if nothing else has one.
                    if best_line is None or updated >= best_updated:
                        best_line, best_updated = ln, updated
            if best_line is None:
                return None, None
            return _opt_float(best_line.get("cost")), _opt_float(best_line.get("line"))

        chosen: dict[str, Any] | None = None
        for book in selection.get("books", []):
            lines = book.get("lines") or []
            if self._prefer_book_id is not None and book.get("id") == self._prefer_book_id:
                chosen = lines[0] if lines else None
                break
            for ln in lines:
                if ln.get("best"):
                    chosen = ln
                    break
                if ln.get("main") and chosen is None:
                    chosen = ln
            if chosen is not None and chosen.get("best"):
                break
        if chosen is None:
            # Fall back to the very first line available.
            for book in selection.get("books", []):
                if book.get("lines"):
                    chosen = book["lines"][0]
                    break
        if chosen is None:
            return None, None
        return _opt_float(chosen.get("cost")), _opt_float(chosen.get("line"))

    # ----------------------------------------------------------------- get_odds
    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """Game-level lines for ``game_pk`` in the MockOddsAPI dict shape (SIM-405).

        ``market_type`` is any value of ``GAME_MARKET_TYPES`` (``run_line`` is
        accepted as the legacy alias of ``runline``); an unknown value raises
        ``ValueError``, mirroring :meth:`get_prop_odds`.

        The three full-game markets keep the pre-2026-09-12 behaviour: a row
        for any of them carries every full-game field the API resolves
        (moneyline + run line + total), because the stored dedup hashes were
        computed over that shape. A segment or team market fills only its own
        fields (see ``GAME_MARKET_KIND``); every other field is ``None``.
        ``source='bettingpros'``, ``is_mock=False``.
        """
        canonical = "runline" if market_type == "run_line" else market_type
        if canonical not in GAME_MARKET_KIND:
            known = ", ".join(GAME_MARKET_TYPES)
            raise ValueError(f"Unknown market_type '{market_type}'. Known values: {known}")

        result: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "bettingpros",
            "is_mock": False,
            "book": book,
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": is_sharp_book,
        }
        for field in GAME_ODDS_FIELDS:
            result[field] = None
        event = self._resolve_event(game_pk)
        if event is None:
            log.warning("BettingPros: no event for game_pk %s — returning empty odds", game_pk)
            return result
        event_id = event["id"]
        home_abbrev, away_abbrev = event.get("home"), event.get("visitor")

        if canonical in LEGACY_GAME_MARKET_TYPES:
            # The legacy shape: every full-game market on one row.
            self._fill_moneyline(
                result,
                self._selections(event_id, _GAME_MARKET_IDS["moneyline"]),
                home_abbrev,
                away_abbrev,
                line_type,
                three_way=False,
            )
            self._fill_runline(
                result,
                self._selections(event_id, _GAME_MARKET_IDS["runline"]),
                home_abbrev,
                away_abbrev,
                line_type,
            )
            self._fill_total(
                result, self._selections(event_id, _GAME_MARKET_IDS["total"]), line_type
            )
            return result

        market_id = _GAME_MARKET_IDS[canonical]
        kind = GAME_MARKET_KIND[canonical]
        if kind in ("moneyline", "three_way"):
            self._fill_moneyline(
                result,
                self._selections(event_id, market_id),
                home_abbrev,
                away_abbrev,
                line_type,
                three_way=(kind == "three_way"),
            )
        elif kind == "runline":
            self._fill_runline(
                result, self._selections(event_id, market_id), home_abbrev, away_abbrev, line_type
            )
        elif kind == "total":
            side = GAME_MARKET_SIDE[canonical]
            if side is None:
                selections = self._selections(event_id, market_id)
            else:
                team_abbrev = home_abbrev if side == "home" else away_abbrev
                selections = self._team_offer_selections(event_id, market_id, team_abbrev)
            self._fill_total(result, selections, line_type)
        elif kind == "yes_no":
            self._fill_yes_no(result, self._selections(event_id, market_id), line_type)
        return result

    # --------------------------------------------------- game-market parsers
    def _fill_moneyline(
        self,
        result: dict[str, Any],
        selections: list[dict[str, Any]],
        home_abbrev: Any,
        away_abbrev: Any,
        line_type: str,
        *,
        three_way: bool,
    ) -> None:
        """Fill ``home_ml`` / ``away_ml`` (and ``draw_ml`` on a three-way market).

        A selection names its team in ``participant``; the tie selection of a
        segment moneyline carries ``selection == 'draw'`` and no participant.
        BettingPros folds an unrelated yes / no pair into the first-five
        moneyline's selections (seen live 2026-09-12); those carry neither a
        participant nor ``draw`` and are ignored here.
        """
        for sel in selections:
            cost, _ = self._pick_line(sel, line_type)
            participant = sel.get("participant")
            if participant and participant == home_abbrev:
                result["home_ml"] = cost
            elif participant and participant == away_abbrev:
                result["away_ml"] = cost
            elif (
                three_way and str(sel.get("selection") or sel.get("label") or "").lower() == "draw"
            ):
                result["draw_ml"] = cost

    def _fill_runline(
        self,
        result: dict[str, Any],
        selections: list[dict[str, Any]],
        home_abbrev: Any,
        away_abbrev: Any,
        line_type: str,
    ) -> None:
        """Fill the spread and its price for each side (a run line, any segment)."""
        for sel in selections:
            cost, line = self._pick_line(sel, line_type)
            participant = sel.get("participant")
            if participant and participant == home_abbrev:
                result["home_spread"], result["home_spread_ml"] = line, cost
            elif participant and participant == away_abbrev:
                result["away_spread"], result["away_spread_ml"] = line, cost

    def _fill_total(
        self, result: dict[str, Any], selections: list[dict[str, Any]], line_type: str
    ) -> None:
        """Fill ``total_line`` / ``over_ml`` / ``under_ml`` from an over / under pair."""
        for sel in selections:
            cost, line = self._pick_line(sel, line_type)
            label = (sel.get("label") or sel.get("selection") or "").lower()
            if line is not None:
                result["total_line"] = line
            if "over" in label:
                result["over_ml"] = cost
            elif "under" in label:
                result["under_ml"] = cost

    def _fill_yes_no(
        self, result: dict[str, Any], selections: list[dict[str, Any]], line_type: str
    ) -> None:
        """Store a yes / no market as over / under at 0.5.

        "A run in the first inning: yes" IS "over 0.5 first-inning runs", so
        the row keeps the total layout and no consumer needs a special case.
        The line is set only when at least one price resolved.
        """
        for sel in selections:
            cost, _ = self._pick_line(sel, line_type)
            label = (sel.get("selection") or sel.get("label") or "").lower()
            if label == "yes":
                result["over_ml"] = cost
            elif label == "no":
                result["under_ml"] = cost
        if result["over_ml"] is not None or result["under_ml"] is not None:
            result["total_line"] = 0.5

    # ------------------------------------------------------------ get_prop_odds
    def get_prop_odds(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """Single player-prop quote for ``player_id`` in the MockOddsAPI shape.

        Raises ``ValueError`` for an unknown ``prop_stat`` (mirrors the mock).
        ``line``/``over_ml``/``under_ml`` are ``None`` when the player/market has
        no offer. ``source='bettingpros'``, ``is_mock=False``.
        """
        if prop_stat not in _PROP_MARKET_IDS:
            known = ", ".join(sorted(_PROP_MARKET_IDS))
            raise ValueError(f"Unknown prop_stat '{prop_stat}'. Known values: {known}")

        result: dict[str, Any] = {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": None,
            "over_ml": None,
            "under_ml": None,
            "book": book,
            "line_type": line_type,
            "is_sharp_book": is_sharp_book,
            "source": "bettingpros",
            "is_mock": False,
        }
        event = self._resolve_event(game_pk)
        player_name = self._resolve_player_name(player_id)
        if event is None or player_name is None:
            return result

        offer = self._find_player_offer(event["id"], _PROP_MARKET_IDS[prop_stat], player_name)
        if offer is None:
            return result

        for sel in offer.get("selections", []):
            cost, line = self._pick_line(sel, line_type)
            if line is not None:
                result["line"] = line
            label = (sel.get("label") or sel.get("selection") or "").lower()
            if "over" in label:
                result["over_ml"] = cost
            elif "under" in label:
                result["under_ml"] = cost
        return result

    # --------------------------------------------------------------- internals
    def _selections(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """All selections of the (single) offer for a game-level market."""
        try:
            offers = self._offers(event_id, market_id)  # SIM-421: cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: offers fetch failed (market %s): %s", market_id, exc)
            return []
        return offers[0].get("selections", []) if offers else []

    def _team_offer_selections(
        self, event_id: int, market_id: int, team_abbrev: Any
    ) -> list[dict[str, Any]]:
        """The selections of the offer for ONE team in a per-team market.

        A team-total market holds one offer per team; each offer names its
        team in ``team_id`` (the abbreviation the event uses for ``home`` /
        ``visitor``) and again as its only participant. Returns ``[]`` when the
        team has no offer.
        """
        try:
            offers = self._offers(event_id, market_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: team offers fetch failed (market %s): %s", market_id, exc)
            return []
        if not team_abbrev:
            return []
        for offer in offers:
            if offer.get("team_id") == team_abbrev:
                return offer.get("selections", [])
            ids = {p.get("id") for p in offer.get("participants", [])}
            if team_abbrev in ids:
                return offer.get("selections", [])
        return []

    def _find_player_offer(
        self, event_id: int, market_id: int, player_name: str
    ) -> dict[str, Any] | None:
        """The prop offer whose participant matches ``player_name`` (normalized)."""
        try:
            offers = self._offers(event_id, market_id)  # SIM-421: cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: prop offers fetch failed (market %s): %s", market_id, exc)
            return None
        for offer in offers:
            for part in offer.get("participants", []):
                player = part.get("player") or {}
                first = player.get("first_name", "")
                last = player.get("last_name", "")
                if _normalize_name(f"{first} {last}") == player_name:
                    return offer
                # Fall back to the participant display name if first/last absent.
                if part.get("name") and _normalize_name(part["name"]) == player_name:
                    return offer
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["BettingProsOddsProvider"]
