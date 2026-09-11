"""
test_bettingpros_odds_provider_sim405.py
========================================
Unit tests for the SIM-405 BettingPros odds provider. The two network seams
(_bp_get / _mlb_get) are overridden to serve captured fixtures
(tests/fixtures/bettingpros/), so no live API call is made.

Fixtures: game_pk 746437 (SEA @ DET, 2024-08-15) ↔ BettingPros event 92857.
Expected values are read straight from the captured responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider, _normalize_name

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"

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
    assert odds["home_ml"] == 125  # DET (best current line)
    assert odds["away_ml"] == -135  # SEA


def test_get_odds_total():
    odds = _FixtureProvider().get_odds(746437, market_type="total")
    assert odds["total_line"] == 8.5
    assert odds["over_ml"] == 100
    assert odds["under_ml"] == -108


def test_get_odds_runline():
    odds = _FixtureProvider().get_odds(746437, market_type="runline")
    assert odds["home_spread"] == 1.5
    assert odds["home_spread_ml"] == -135
    assert odds["away_spread"] == -1.5
    assert odds["away_spread_ml"] == 125


def test_get_odds_opening_line_type_uses_opening_line():
    odds = _FixtureProvider().get_odds(746437, line_type="opening", market_type="moneyline")
    assert odds["home_ml"] == 126  # opening_line cost (vs 120 current)
    assert odds["away_ml"] == -148


def test_get_odds_echoes_book_and_line_type():
    odds = _FixtureProvider().get_odds(
        746437, line_type="closing", book="pinnacle", is_sharp_book=True
    )
    assert odds["book"] == "pinnacle"
    assert odds["line_type"] == "closing"
    assert odds["is_sharp_book"] is True


def test_get_odds_unresolvable_game_returns_empty():
    odds = _FixtureProvider(schedule_empty=True).get_odds(999999)
    assert odds["home_ml"] is None
    assert odds["away_ml"] is None
    assert odds["source"] == "bettingpros"  # shape preserved


# --------------------------------------------------------------------------- get_prop_odds
def test_get_prop_odds_matches_player_by_name():
    quote = _FixtureProvider(player_full_name="Bryce Miller").get_prop_odds(
        746437, 682243, "strikeouts"
    )
    assert quote["prop_stat"] == "strikeouts"
    assert quote["line"] == 5.5
    assert quote["over_ml"] == 100
    assert quote["under_ml"] == -127
    assert quote["is_mock"] is False


def test_get_prop_odds_no_player_match_returns_nulls():
    quote = _FixtureProvider(player_full_name="Nobody Here").get_prop_odds(
        746437, 111111, "strikeouts"
    )
    assert quote["line"] is None
    assert quote["over_ml"] is None
    assert quote["prop_stat"] == "strikeouts"  # shape preserved


def test_get_prop_odds_unknown_stat_raises():
    with pytest.raises(ValueError, match="Unknown prop_stat"):
        _FixtureProvider().get_prop_odds(746437, 682243, "doubles")


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
