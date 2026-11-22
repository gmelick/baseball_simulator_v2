"""The real BettingPros provider, fed by recorded real responses.

The owner's ruling (SIM-645): no test invents a price. A test that needs odds rows
runs the real :class:`BettingProsOddsProvider` over the responses recorded in
``tests/fixtures/bettingpros/``. Only the two network seams (``_bp_get`` and
``_mlb_get``) are replaced; every other line of the provider runs.

Two recorded games:

* :class:`Recorded92857`: SEA at DET, 2024-08-15, game_pk 746437, BettingPros event
  92857. The full-game moneyline, total and run line, and the strikeout, hits (two
  pages), singles and hits-allowed props.
* :class:`Recorded99026`: TB at ATL, 2026-09-10, BettingPros event 99026. The twelve
  segment markets (first inning, first five, team totals, first to score).

A market the recording does not hold answers an empty offer list: the books posted
nothing there.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from betting.bettingpros.fetch_odds import BettingProsOddsProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bettingpros"

#: The MLB game of BettingPros event 92857.
GAME_92857 = 746437
#: A game_pk for event 99026 (the recording resolves any game_pk to the event).
GAME_99026 = 776137

#: The players of the 92857 recording, by a made-up MLB id. The provider matches a
#: player to the offers by name, so only the names must be real.
BRYCE_MILLER = 45184  # SEA starter: strikeouts and hits allowed
ALEX_FAEDO = 45185  # DET starter: strikeouts
JULIO_RODRIGUEZ = 61001  # SEA batter: singles
RILEY_GREENE = 61002  # DET batter: singles
KERRY_CARPENTER = 61003  # DET batter: hits (on page two)

PLAYER_NAMES: dict[int, str] = {
    BRYCE_MILLER: "Bryce Miller",
    ALEX_FAEDO: "Alex Faedo",
    JULIO_RODRIGUEZ: "Julio Rodriguez",
    RILEY_GREENE: "Riley Greene",
    KERRY_CARPENTER: "Kerry Carpenter",
}

OFFERS_92857: dict[int, str] = {
    122: "offers_ml_92857.json",
    175: "offers_total_92857.json",
    176: "offers_runline_92857.json",
    285: "offers_k_92857.json",
    295: "offers_singles_92857.json",
    404: "offers_hits_allowed_92857.json",
}
#: The hits market (287) comes in two pages.
HITS_PAGES_92857: dict[int, str] = {
    1: "offers_hits_92857_page1.json",
    2: "offers_hits_92857_page2.json",
}

OFFERS_99026: dict[int, str] = {
    278: "offers_f1_moneyline_99026.json",
    279: "offers_f5_moneyline_99026.json",
    280: "offers_f1_total_99026.json",
    281: "offers_f5_total_99026.json",
    282: "offers_f1_runline_99026.json",
    283: "offers_f5_runline_99026.json",
    277: "offers_team_total_99026.json",
    407: "offers_f5_team_total_99026.json",
    286: "offers_first_to_score_99026.json",
    369: "offers_first_inning_run_99026.json",
}

_NO_OFFERS: dict[str, Any] = {"offers": []}


def load_fixture(name: str) -> dict[str, Any]:
    """One recorded response, parsed."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Recorded92857(BettingProsOddsProvider):
    """The real provider over the SEA at DET recording (game_pk 746437).

    ``names`` maps an MLB id to the player's full name (default :data:`PLAYER_NAMES`);
    an id it lacks answers no person. ``bp_calls`` lists every BettingPros request.
    """

    def __init__(self, *, names: dict[int, str] | None = None, **kw: Any) -> None:
        super().__init__(api_key="test-key", **kw)
        self._names = dict(PLAYER_NAMES if names is None else names)
        self.bp_calls: list[tuple[str, dict[str, Any]]] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return load_fixture("mlb_schedule_746437.json")
        if path.startswith("people/"):
            name = self._names.get(int(path.split("/")[1]))
            return {"people": [{"fullName": name}] if name else []}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        self.bp_calls.append((path, dict(params)))
        if path == "events":
            return load_fixture("events_2024-08-15.json")
        if path == "offers":
            market_id = int(params["market_id"])
            if market_id == 287:
                return load_fixture(HITS_PAGES_92857[int(params.get("page", 1))])
            name = OFFERS_92857.get(market_id)
            return load_fixture(name) if name else dict(_NO_OFFERS)
        raise AssertionError(f"unexpected BP path {path}")


class Recorded99026(BettingProsOddsProvider):
    """The real provider over the TB at ATL segment recording (event 99026).

    Any game_pk resolves to the event. ``game_date`` is the official date the dated
    first-five exclusion reads; ``None`` leaves the game's date unknown.
    """

    def __init__(self, *, game_date: str | None = "2024-08-14", **kw: Any) -> None:
        super().__init__(api_key="test-key", **kw)
        self._game_date = game_date
        self.offers_calls: list[int] = []

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 99026, "home": "ATL", "visitor": "TB", "scheduled": "2026-09-10 16:15:00"}

    def _resolve_game_meta(self, game_pk):  # type: ignore[override]
        if self._game_date is None:
            return None
        return (
            self._game_date,
            "Atlanta Braves",
            "Tampa Bay Rays",
            datetime(2026, 9, 10, 16, 15),
        )

    def _mlb_get(self, path, params):  # type: ignore[override]
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        assert path == "offers", path
        market_id = int(params["market_id"])
        self.offers_calls.append(market_id)
        name = OFFERS_99026.get(market_id)
        return load_fixture(name) if name else dict(_NO_OFFERS)


def recorded_game_rows(market_type: str = "moneyline", line_type: str = "current") -> list[dict]:
    """Every book's priced row of one full-game market of game 746437, as recorded."""
    rows = Recorded92857().get_odds_by_book(
        GAME_92857, line_type=line_type, market_type=market_type
    )
    return [row for row in rows if any(row.get(f) is not None for f in _GAME_PRICE_FIELDS)]


def recorded_prop_rows(
    player_id: int = BRYCE_MILLER, prop_stat: str = "strikeouts", line_type: str = "current"
) -> list[dict]:
    """Every book's row of one prop of game 746437, as recorded."""
    return Recorded92857().get_prop_odds_by_book(
        GAME_92857, player_id, prop_stat, line_type=line_type
    )


_GAME_PRICE_FIELDS = (
    "home_ml",
    "away_ml",
    "over_ml",
    "under_ml",
    "home_spread_ml",
    "away_spread_ml",
    "total_line",
)
