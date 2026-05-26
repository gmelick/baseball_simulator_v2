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
