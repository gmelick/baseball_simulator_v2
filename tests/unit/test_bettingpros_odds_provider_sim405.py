"""
test_bettingpros_odds_provider_sim405.py
========================================
Unit tests for the SIM-405 BettingPros odds provider. The two network seams
(_bp_get / _mlb_get) are overridden to serve captured fixtures
(tests/fixtures/bettingpros/), so no live API call is made.

Fixtures: game_pk 746437 (SEA @ DET, 2024-08-15) ↔ BettingPros event 92857.
Expected values are read straight from the captured responses.

SIM-555 (2026-09-28) changed the pins on purpose: ``get_odds`` / ``get_prop_odds``
now return ONE book's row, the first book on ``GRADED_BOOK_PREFERENCE`` with a
row, not the "best current line" the vendor flagged per side (which mixed books
inside one row). A test whose book falls past the list's head reads the order
from the constant, so a reorder of the list (as on 2026-09-29) never breaks it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider, _normalize_name
from pipeline.odds_provider import graded_book_labels, is_bettable

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"

#: The strikeout prop's closing (line, over, under) per sportsbook in the
#: captured fixture. DraftKings has no offer. BetMGM and PartyCasino post one
#: price; so do BetRivers and SugarHouse.
_K_CLOSES = {
    "bp:10": (5.5, 100, -128),  # FanDuel
    "bp:15": (5.5, -108, -127),  # SugarHouse
    "bp:18": (5.5, -108, -127),  # BetRivers
    "bp:19": (5.5, 100, -135),  # BetMGM
    "bp:27": (5.5, 100, -135),  # PartyCasino
}

_OFFERS_BY_MARKET = {
    122: "offers_ml_92857.json",
    175: "offers_total_92857.json",
    176: "offers_runline_92857.json",
    285: "offers_k_92857.json",
}


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


class _FixtureProvider(BettingProsOddsProvider):
    """Provider with the HTTP seams wired to the captured fixtures."""

    def __init__(
        self, *, player_full_name: str = "Bryce Miller", schedule_empty: bool = False, **kw
    ):
        super().__init__(api_key="test-key", **kw)
        self._player_full_name = player_full_name
        self._schedule_empty = schedule_empty

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            if self._schedule_empty:
                return {"dates": []}
            return _load("mlb_schedule_746437.json")
        if path.startswith("people/"):
            return {"people": [{"fullName": self._player_full_name}]}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            return _load("events_2024-08-15.json")
        if path == "offers":
            return _load(_OFFERS_BY_MARKET[params["market_id"]])
        raise AssertionError(f"unexpected BP path {path}")


# --------------------------------------------------------------------------- helper
def test_normalize_name_strips_accents_and_punct():
    assert _normalize_name("José  Ramírez Jr.") == "jose ramirez jr"
    assert _normalize_name("Detroit Tigers").endswith("tigers")


# --------------------------------------------------------------------------- get_odds
def test_get_odds_moneyline():
    odds = _FixtureProvider().get_odds(746437, market_type="moneyline")
    assert odds["source"] == "bettingpros"
    assert odds["is_mock"] is False
    assert odds["book"] == "bp:12"  # DraftKings: first on the graded-book preference
    assert odds["home_ml"] == 120  # DET
    assert odds["away_ml"] == -142  # SEA


def test_get_odds_total():
    odds = _FixtureProvider().get_odds(746437, market_type="total")
    assert odds["book"] == "bp:12"
    assert odds["total_line"] == 8.5
    assert odds["over_ml"] == -105
    assert odds["under_ml"] == -115
    # SIM-555: a total row fills only the total's columns.
    assert odds["home_ml"] is None and odds["home_spread"] is None


def test_get_odds_runline():
    odds = _FixtureProvider().get_odds(746437, market_type="runline")
    assert odds["book"] == "bp:12"
    assert odds["home_spread"] == 1.5
    assert odds["home_spread_ml"] == -142
    assert odds["away_spread"] == -1.5
    assert odds["away_spread_ml"] == 120


def test_get_odds_opening_line_type_uses_opening_line():
    odds = _FixtureProvider().get_odds(746437, line_type="opening", market_type="moneyline")
    assert odds["book"] == "bp:10"  # FanDuel opened both sides
    assert odds["home_ml"] == 126  # opening_line cost (vs 120 current)
    assert odds["away_ml"] == -148


def test_get_odds_names_a_book_and_echoes_line_type():
    odds = _FixtureProvider().get_odds(
        746437, line_type="closing", book="fanduel", is_sharp_book=True
    )
    assert odds["book"] == "bp:10"
    assert (odds["home_ml"], odds["away_ml"]) == (116, -136)
    assert odds["line_type"] == "closing"
    assert odds["is_sharp_book"] is True


def test_get_odds_unknown_book_is_an_empty_row_that_echoes_the_request():
    # SIM-555: BettingPros carries no Pinnacle line; the name no longer silently
    # returns another book's prices.
    odds = _FixtureProvider().get_odds(
        746437, line_type="closing", book="pinnacle", is_sharp_book=True
    )
    assert odds["book"] == "pinnacle"
    assert odds["line_type"] == "closing"
    assert odds["is_sharp_book"] is True
    assert odds["home_ml"] is None and odds["away_ml"] is None


def test_get_odds_unresolvable_game_returns_empty():
    odds = _FixtureProvider(schedule_empty=True).get_odds(999999)
    assert odds["home_ml"] is None
    assert odds["away_ml"] is None
    assert odds["source"] == "bettingpros"  # shape preserved


# --------------------------------------------------------------------------- get_prop_odds
def test_get_prop_odds_matches_player_by_name():
    provider = _FixtureProvider(player_full_name="Bryce Miller")
    rows = provider.get_prop_odds_by_book(746437, 682243, "strikeouts")
    closes = {
        row["book"]: (row["line"], row["over_ml"], row["under_ml"])
        for row in rows
        if is_bettable(row["book"])
    }
    assert closes == _K_CLOSES
    quote = provider.get_prop_odds(746437, 682243, "strikeouts")
    assert quote["prop_stat"] == "strikeouts"
    # The first book on the preference list with an offer.
    graded = next(label for label in graded_book_labels() if label in closes)
    assert quote["book"] == graded
    assert (quote["line"], quote["over_ml"], quote["under_ml"]) == _K_CLOSES[graded]
    assert quote["is_mock"] is False


def test_get_prop_odds_no_player_match_returns_nulls():
    quote = _FixtureProvider(player_full_name="Nobody Here").get_prop_odds(
        746437, 111111, "strikeouts"
    )
    assert quote["line"] is None
    assert quote["over_ml"] is None
    assert quote["prop_stat"] == "strikeouts"  # shape preserved


def test_get_prop_odds_unknown_stat_raises():
    # SIM-421 made "doubles" (the old probe value) a real market, so the probe is
    # now innings_pitched — a market the vocabulary still does not carry.
    with pytest.raises(ValueError, match="Unknown prop_stat"):
        _FixtureProvider().get_prop_odds(746437, 682243, "innings_pitched")


# --------------------------------------------------------------------------- registry
def test_registry_resolves_bettingpros_and_real():
    from pipeline.odds_provider import get_odds_provider

    for name in ("bettingpros", "real"):
        prov = get_odds_provider(name)
        assert isinstance(prov, BettingProsOddsProvider)


# --------------------------------------------------------------------------- SIM-536
# Wrong-game odds: the schedule date used to query BettingPros must come from
# `officialDate` (the local calendar date), never from truncating `gameDate` (a
# UTC timestamp) -- a West-/Mountain-time night game has already rolled its UTC
# clock into the next day while `officialDate` correctly stays put. A live check
# (2026-09-08) found this wrong on 5 of 15 real games that day.


class _DateRecordingProvider(BettingProsOddsProvider):
    """Records which `date` param `_bp_get('events', ...)` was queried with."""

    def __init__(self, schedule: dict, events_by_date: dict[str, dict], **kw):
        super().__init__(api_key="test-key", **kw)
        self._schedule = schedule
        self._events_by_date = events_by_date
        self.queried_dates: list[str] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return self._schedule
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            self.queried_dates.append(params["date"])
            return self._events_by_date.get(params["date"], {"events": []})
        raise AssertionError(f"unexpected BP path {path}")


def _schedule_for(game_pk: int, game_date_utc: str, official_date: str, home: str, away: str):
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": game_pk,
                        "gameDate": game_date_utc,
                        "officialDate": official_date,
                        "teams": {
                            "home": {"team": {"name": home}},
                            "away": {"team": {"name": away}},
                        },
                    }
                ]
            }
        ]
    }


def test_resolve_event_queries_official_date_not_utc_rollover_date():
    """A late West-Coast start (UTC rolls to the next day) must still query
    BettingPros for the day the game is actually played on."""
    schedule = _schedule_for(
        999001,
        game_date_utc="2026-09-09T01:40:00Z",  # UTC already the next day...
        official_date="2026-09-08",  # ...but the game is played on the 8th.
        home="San Diego Padres",
        away="Washington Nationals",
    )
    events_by_date = {
        "2026-09-08": {
            "events": [
                {
                    "id": 1,
                    "home": "SD",
                    "visitor": "WSH",
                    "scheduled": "2026-09-09 01:40:00",
                    "participants": [
                        {"id": "SD", "name": "Padres"},
                        {"id": "WSH", "name": "Nationals"},
                    ],
                }
            ]
        },
        # The WRONG day the pre-fix bug would have queried: a different game
        # between the same two teams the following day, if one existed. Left
        # non-empty so a regression that queries this date instead is caught by
        # `queried_dates`, not by an accidental empty-result pass.
        "2026-09-09": {
            "events": [
                {
                    "id": 2,
                    "home": "SD",
                    "visitor": "WSH",
                    "scheduled": "2026-09-10 01:40:00",
                    "participants": [
                        {"id": "SD", "name": "Padres"},
                        {"id": "WSH", "name": "Nationals"},
                    ],
                }
            ]
        },
    }
    provider = _DateRecordingProvider(schedule, events_by_date)
    event = provider._resolve_event(999001)
    assert provider.queried_dates == ["2026-09-08"]
    assert event is not None and event["id"] == 1


def test_resolve_event_picks_the_doubleheader_game_actually_requested():
    """Two BettingPros events match by team name (a double-header); the one
    picked must be whichever is closest to THIS game's own start time, not
    always the earlier of the two (the pre-SIM-536 behaviour)."""
    # game_pk here is game 2 of the doubleheader (the later start).
    schedule = _schedule_for(
        999002,
        game_date_utc="2024-08-15T23:40:00Z",
        official_date="2024-08-15",
        home="Detroit Tigers",
        away="Seattle Mariners",
    )
    game_1 = {
        "id": 111,
        "home": "DET",
        "visitor": "SEA",
        "scheduled": "2024-08-15 17:10:00",
        "participants": [{"id": "DET", "name": "Tigers"}, {"id": "SEA", "name": "Mariners"}],
    }
    game_2 = {
        "id": 222,
        "home": "DET",
        "visitor": "SEA",
        "scheduled": "2024-08-15 23:40:00",
        "participants": [{"id": "DET", "name": "Tigers"}, {"id": "SEA", "name": "Mariners"}],
    }
    events_by_date = {"2024-08-15": {"events": [game_1, game_2]}}
    provider = _DateRecordingProvider(schedule, events_by_date)
    event = provider._resolve_event(999002)
    assert event is not None
    assert event["id"] == 222  # game 2 — NOT the earliest-scheduled (111)


def test_resolve_event_rejects_a_match_far_from_the_real_start_time():
    """The single team-name match exists, but its scheduled time is hours away
    from the real game's start -- that must not be silently accepted."""
    schedule = _schedule_for(
        999003,
        game_date_utc="2024-08-15T17:10:00Z",
        official_date="2024-08-15",
        home="Detroit Tigers",
        away="Seattle Mariners",
    )
    far_off_event = {
        "id": 333,
        "home": "DET",
        "visitor": "SEA",
        "scheduled": "2024-08-15 23:40:00",  # 6.5 hours from the real 17:10 start
        "participants": [{"id": "DET", "name": "Tigers"}, {"id": "SEA", "name": "Mariners"}],
    }
    events_by_date = {"2024-08-15": {"events": [far_off_event]}}
    provider = _DateRecordingProvider(schedule, events_by_date)
    assert provider._resolve_event(999003) is None


def test_resolve_event_accepts_a_close_match_within_the_sanity_window():
    """A match within the 2-hour sanity window is accepted normally — the
    check should not reject legitimate small scheduling variance."""
    schedule = _schedule_for(
        999004,
        game_date_utc="2024-08-15T17:10:00Z",
        official_date="2024-08-15",
        home="Detroit Tigers",
        away="Seattle Mariners",
    )
    close_event = {
        "id": 444,
        "home": "DET",
        "visitor": "SEA",
        "scheduled": "2024-08-15 18:00:00",  # 50 minutes off — inside the window
        "participants": [{"id": "DET", "name": "Tigers"}, {"id": "SEA", "name": "Mariners"}],
    }
    events_by_date = {"2024-08-15": {"events": [close_event]}}
    provider = _DateRecordingProvider(schedule, events_by_date)
    event = provider._resolve_event(999004)
    assert event is not None and event["id"] == 444


# --------------------------------------------------------------------------- SIM-555: no cached miss
# A long-lived process (the live pipeline polls from the morning) used to keep
# a failed game lookup for the life of the provider: a game BettingPros listed
# later, or one whose first read failed, was never priced. Only a found game is
# cached now.


class _Clock:
    """A settable monotonic clock for the provider's short-lived caches."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _sd_wsh_event(event_id: int = 1) -> dict:
    return {
        "id": event_id,
        "home": "SD",
        "visitor": "WSH",
        "scheduled": "2026-09-09 01:40:00",
        "participants": [{"id": "SD", "name": "Padres"}, {"id": "WSH", "name": "Nationals"}],
    }


def _sd_wsh_schedule() -> dict:
    return _schedule_for(
        999005,
        game_date_utc="2026-09-09T01:40:00Z",
        official_date="2026-09-08",
        home="San Diego Padres",
        away="Washington Nationals",
    )


def test_a_game_with_no_event_yet_is_found_on_a_later_call():
    clock = _Clock()
    events_by_date: dict[str, dict] = {"2026-09-08": {"events": []}}
    provider = _DateRecordingProvider(
        _sd_wsh_schedule(), events_by_date, clock=clock, offers_cache_ttl_s=30
    )
    assert provider._resolve_event(999005) is None
    # BettingPros lists the game later in the day.
    events_by_date["2026-09-08"] = {"events": [_sd_wsh_event()]}
    # Inside the date list's time-to-live the repeat call reads no new list.
    assert provider._resolve_event(999005) is None
    assert provider.queried_dates == ["2026-09-08"]
    # Past it, the game is found, and the found event is cached from then on.
    clock.now += 31
    event = provider._resolve_event(999005)
    assert event is not None and event["id"] == 1
    events_by_date["2026-09-08"] = {"events": []}
    clock.now += 31
    assert provider._resolve_event(999005) == event
    assert provider.queried_dates == ["2026-09-08", "2026-09-08"]


class _FlakySchedule(_DateRecordingProvider):
    fail = True
    schedule_reads = 0

    def _mlb_get(self, path, params):  # type: ignore[override]
        self.schedule_reads += 1
        if self.fail:
            raise RuntimeError("HTTP 503")
        return super()._mlb_get(path, params)


def test_a_failed_schedule_read_is_kept_for_one_time_to_live_only():
    clock = _Clock()
    provider = _FlakySchedule(
        _sd_wsh_schedule(),
        {"2026-09-08": {"events": [_sd_wsh_event()]}},
        clock=clock,
        offers_cache_ttl_s=30,
    )
    assert provider._resolve_game_meta(999005) is None
    assert provider._resolve_event(999005) is None
    assert provider.schedule_reads == 1
    # Inside the time-to-live the failure answers: no new read, even once the
    # schedule is back.
    provider.fail = False
    assert provider._resolve_event(999005) is None
    assert provider.schedule_reads == 1
    # Past it, the game resolves.
    clock.now += 31
    event = provider._resolve_event(999005)
    assert event is not None and event["id"] == 1
    assert provider._resolve_game_meta(999005) is not None
    # A resolved game is cached for good: no further schedule read.
    reads = provider.schedule_reads
    provider.fail = True
    clock.now += 3600
    assert provider._resolve_game_meta(999005) is not None
    assert provider._resolve_event(999005) == event
    assert provider.schedule_reads == reads


def test_a_time_to_live_of_zero_keeps_no_failure():
    provider = _FlakySchedule(_sd_wsh_schedule(), {}, offers_cache_ttl_s=0)
    assert provider._resolve_game_meta(999005) is None
    assert provider._resolve_game_meta(999005) is None
    assert provider.schedule_reads == 2


# --------------------------------------------------------------------------- SIM-555 review fix
# The loader asks the provider about one game about 410 times (30 game-market
# reads, about 380 prop reads). When SIM-555 stopped caching a failed lookup,
# a failed schedule or event read was retried on every one of those calls, with
# no pause, and a timeout costs 15 s each: 1.7 hours per game. A failure is now
# kept for one time-to-live (600 s offline, 30 s live).


class _CountingFailures(BettingProsOddsProvider):
    """Counts every vendor and MLB read; ``fail`` names what fails."""

    def __init__(self, fail: str, **kw):
        super().__init__(api_key="test-key", **kw)
        self.fail = fail
        self.reads: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.reads[key] = self.reads.get(key, 0) + 1

    def _mlb_get(self, path, params):  # type: ignore[override]
        key = "schedule" if path == "schedule" else "people"
        self._count(key)
        if self.fail == key:
            raise TimeoutError("timed out")
        if path == "schedule":
            return _sd_wsh_schedule()
        return {"people": [{"fullName": "Some Player"}]}

    def _bp_get(self, path, params):  # type: ignore[override]
        self._count(path)
        if self.fail == path:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        if path == "events":
            return {"events": [_sd_wsh_event()]}
        return {"offers": []}


def _one_loader_game(provider: BettingProsOddsProvider, players: int = 20) -> None:
    from pipeline.odds_provider import BATTER_PROP_STATS, GAME_MARKET_TYPES

    for line_type in ("opening", "closing"):
        for market in GAME_MARKET_TYPES:
            provider.get_odds_by_book(999005, line_type=line_type, market_type=market)
        for player in range(players):
            for stat in BATTER_PROP_STATS:
                provider.get_prop_odds_by_book(999005, 600000 + player, stat, line_type=line_type)


@pytest.mark.parametrize("fail", ["schedule", "events"])
def test_a_failed_game_lookup_costs_one_read_per_game(fail):
    provider = _CountingFailures(fail, offers_cache_ttl_s=600)
    _one_loader_game(provider)
    assert provider.reads.get(fail) == 1
    assert provider.reads.get("offers", 0) == 0


def test_a_failed_player_lookup_is_kept_for_one_time_to_live_only():
    """The coordinator's item: a failed player lookup used to be cached as None
    for the process lifetime, so one timeout cost the player every prop for the
    rest of a load or of the live pipeline's day."""
    clock = _Clock()
    provider = _CountingFailures("people", clock=clock, offers_cache_ttl_s=30)
    assert provider._resolve_player_name(660271) is None
    assert provider._resolve_player_name(660271) is None
    assert provider.reads["people"] == 1
    provider.fail = ""
    clock.now += 31
    assert provider._resolve_player_name(660271) == "some player"
    # A found name is cached for good.
    provider.fail = "people"
    clock.now += 3600
    assert provider._resolve_player_name(660271) == "some player"
    assert provider.reads["people"] == 2
