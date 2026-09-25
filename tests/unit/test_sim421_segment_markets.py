"""SIM-421 (owner ruling 2026-09-12) — the simulator prices every game market.

``simulation/game_market_distributions.py`` turns per-iteration inning grids
into the probability each segment / team market needs, and
``scripts/clv_backtest.py`` grades those markets against the official inning
grid. These tests pin the conventions (strict over, push, the three-way tie,
the run-line cover, the first team to score) on hand-built grids, then drive a
real simulated game's play stream through the same path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from pipeline.odds_provider import GAME_MARKET_TYPES, LEGACY_GAME_MARKET_TYPES
from simulation.game_market_distributions import (
    AWAY_FIRST,
    HOME_FIRST,
    NOBODY_FIRST,
    SegmentRuns,
    cover_probabilities,
    market_outcome,
    market_probability,
    side_probabilities,
    total_probabilities,
)

# Four hand-built games: (home_cells, away_cells), nine innings each.
#   game A: away 1st-inning run, home wins 5-3; F1 away 1, home 0; F5 home 3, away 2
#   game B: home 1st-inning run, home wins 2-1 (bottom 9th unplayed = "x")
#   game C: scoreless through 5, away wins 0-1 in the 9th
#   game D: tied 2-2 after 5, home wins 3-2
_GRIDS = [
    ([0, 1, 0, 2, 0, 0, 1, 1, 0], [1, 0, 1, 0, 0, 0, 0, 1, 0]),
    ([1, 0, 0, 0, 0, 1, 0, 0, None], [0, 0, 0, 1, 0, 0, 0, 0, 0]),
    ([0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 0, 1]),
    ([0, 2, 0, 0, 0, 0, 0, 1, None], [1, 0, 1, 0, 0, 0, 0, 0, 0]),
]


@pytest.fixture
def runs() -> SegmentRuns:
    return SegmentRuns.from_inning_grids(_GRIDS)


class TestSegmentRuns:
    def test_segment_totals_from_the_grids(self, runs):
        assert runs.n == 4
        assert runs.home_game.tolist() == [5, 2, 0, 3]
        assert runs.away_game.tolist() == [3, 1, 1, 2]
        assert runs.home_f1.tolist() == [0, 1, 0, 0]
        assert runs.away_f1.tolist() == [1, 0, 0, 1]
        assert runs.home_f5.tolist() == [3, 1, 0, 2]
        assert runs.away_f5.tolist() == [2, 1, 0, 2]

    def test_unplayed_half_counts_as_zero(self, runs):
        # Game B's bottom ninth is "x": the home total is still 2.
        assert runs.home_game[1] == 2

    def test_first_to_score(self, runs):
        assert runs.first_to_score.tolist() == [AWAY_FIRST, HOME_FIRST, AWAY_FIRST, AWAY_FIRST]

    def test_nobody_scored_is_its_own_code(self):
        r = SegmentRuns.from_inning_grids([([0] * 9, [0] * 9)])
        assert r.first_to_score.tolist() == [NOBODY_FIRST]

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            SegmentRuns.from_inning_grids([])

    def test_official_grid_builder_is_one_iteration(self):
        actual = SegmentRuns.from_official_grid({"home": [0, 0, 3], "away": [1, 0, 0]})
        assert actual.n == 1
        assert actual.home_game[0] == 3 and actual.away_f1[0] == 1

    def test_market_samples_route_totals_and_team_totals(self, runs):
        assert runs.market_samples("total").tolist() == [8, 3, 1, 5]
        assert runs.market_samples("f1_total").tolist() == [1, 1, 0, 1]
        assert runs.market_samples("f5_total").tolist() == [5, 2, 0, 4]
        assert runs.market_samples("team_total_home").tolist() == [5, 2, 0, 3]
        assert runs.market_samples("f5_team_total_away").tolist() == [2, 1, 0, 2]
        with pytest.raises(ValueError):
            runs.market_samples("moneyline")


class TestProbabilities:
    def test_total_is_strict_over_with_push_mass_apart(self, runs):
        # Full-game totals 8, 3, 1, 5 at line 5.0: over 8 only; push on 5; under 3 and 1.
        over, under, push = total_probabilities(runs, "total", 5.0)
        assert (over, under, push) == (0.25, 0.5, 0.25)
        assert over + under + push == 1.0

    def test_first_inning_run_is_over_half_whatever_line_is_passed(self, runs):
        # F1 totals 1, 1, 0, 1: a run scored in three of four.
        assert total_probabilities(runs, "first_inning_run", 2.5)[0] == 0.75
        assert market_probability(runs, "first_inning_run") == 0.75

    def test_three_way_segment_moneyline_sums_to_one(self, runs):
        # F5 margins: +1, 0, 0, 0 -> home 0.25, away 0, draw 0.75.
        home, away, draw = side_probabilities(runs, "f5_moneyline")
        assert (home, away, draw) == (0.25, 0.0, 0.75)
        # F1 margins: -1, +1, 0, -1 -> home 0.25, away 0.5, draw 0.25.
        assert side_probabilities(runs, "f1_moneyline") == (0.25, 0.5, 0.25)

    def test_full_game_moneyline_has_no_tie(self, runs):
        home, away, draw = side_probabilities(runs, "moneyline")
        assert (home, away, draw) == (0.75, 0.25, 0.0)

    def test_first_to_score_side_probabilities(self, runs):
        assert side_probabilities(runs, "first_to_score") == (0.25, 0.75, 0.0)

    def test_run_line_cover_with_push_on_a_whole_spread(self, runs):
        # F5 margins +1, 0, 0, 0 at home spread 0 (a pick'em): cover 0.25, push 0.75.
        assert cover_probabilities(runs, "f5_runline", 0.0) == (0.25, 0.0, 0.75)
        # Full-game margins +2, +1, -1, +1 at home -1.5: covers only the +2.
        assert cover_probabilities(runs, "runline", -1.5) == (0.25, 0.75, 0.0)

    def test_market_probability_fixes_the_reference_side(self, runs):
        assert market_probability(runs, "f5_total", line=2.5) == 0.5  # 5 and 4 over 2.5
        assert market_probability(runs, "team_total_away", line=1.5) == 0.5  # 3 and 2
        assert market_probability(runs, "f1_runline", line=0.5) == 0.5  # +1, 0 cover; -1s not
        assert market_probability(runs, "f1_moneyline") == 0.25
        with pytest.raises(ValueError):
            market_probability(runs, "f5_total")  # a total needs a line
        with pytest.raises(ValueError):
            market_probability(runs, "not_a_market", line=1.0)

    def test_every_vocabulary_market_is_priceable(self, runs):
        for m in GAME_MARKET_TYPES:
            line = (
                1.5
                if m not in ("moneyline", "first_to_score", "f1_moneyline", "f5_moneyline")
                else None
            )
            p = market_probability(runs, m, line=line)
            assert 0.0 <= p <= 1.0


class TestOutcomes:
    def _actual(self, home, away):
        return SegmentRuns.from_official_grid({"home": home, "away": away})

    def test_total_outcome_and_push(self):
        a = self._actual([0, 0, 3, 0, 0, 0, 0, 0, None], [1, 0, 0, 0, 0, 0, 0, 0, 0])  # 3-1
        assert market_outcome(a, "total", line=3.5) == 1
        assert market_outcome(a, "total", line=4.5) == 0
        assert market_outcome(a, "total", line=4.0) is None  # push
        assert market_outcome(a, "team_total_home", line=2.5) == 1
        assert market_outcome(a, "f1_total", line=0.5) == 1
        assert market_outcome(a, "first_inning_run") == 1

    def test_three_way_tie_is_a_home_loss_not_a_push(self):
        a = self._actual([1, 0, 0, 0, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0, 0, 0, 0])
        assert market_outcome(a, "f1_moneyline") == 0
        assert market_outcome(a, "f5_moneyline") == 0
        # ... while the same tie on a two-way side market has no label.
        assert market_outcome(a, "moneyline") is None

    def test_run_line_outcome_and_push(self):
        a = self._actual(
            [0, 0, 0, 0, 2, 0, 0, 0, None], [1, 0, 0, 0, 0, 0, 0, 0, 0]
        )  # F5 home 2, away 1
        assert market_outcome(a, "f5_runline", line=-0.5) == 1
        assert market_outcome(a, "f5_runline", line=-1.0) is None  # margin 1 - 1 = 0
        assert market_outcome(a, "f5_runline", line=-1.5) == 0

    def test_first_to_score_outcome(self):
        a = self._actual([0, 1, 0, 0, 0, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0, 0, 0, 0])
        assert market_outcome(a, "first_to_score") == 1  # home scored in the 2nd, away in the 3rd
        b = self._actual([0] * 9, [0] * 9)
        assert market_outcome(b, "first_to_score") is None

    def test_outcome_grades_one_result_only(self):
        with pytest.raises(ValueError):
            market_outcome(SegmentRuns.from_inning_grids(_GRIDS), "total", line=5.5)


# ===========================================================================
# The backtest's segment-market scorer
# ===========================================================================


def _backtest():
    path = Path(__file__).resolve().parent.parent.parent / "scripts" / "clv_backtest.py"
    if "clv_backtest" in sys.modules:
        return sys.modules["clv_backtest"]
    spec = importlib.util.spec_from_file_location("clv_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clv_backtest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _closing(market_type: str, **cols):
    return {market_type: {"closing": cols}}


class TestSegmentMarketScorer:
    def test_segment_list_is_every_non_legacy_market(self):
        bt = _backtest()
        assert (
            tuple(m for m in GAME_MARKET_TYPES if m not in LEGACY_GAME_MARKET_TYPES)
            == bt.SEGMENT_MARKET_TYPES
        )

    def test_every_segment_market_has_a_trust_label(self):
        bt = _backtest()
        for m in bt.SEGMENT_MARKET_TYPES:
            assert bt.trust_label(m) == "unvalidated"

    def test_two_way_total_record(self, runs):
        bt = _backtest()
        odds = _closing("f5_total", total_line=2.5, over_ml=-110.0, under_ml=-110.0)
        grid = {"home": [0, 0, 3, 0, 0, 0, 0, 0, None], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        recs = bt.score_segment_market_accuracy(1, runs, odds, grid)
        assert len(recs) == 1
        r = recs[0]
        assert r.market == "f5_total" and r.market_type == "f5_total"
        assert r.sim_prob == 0.5  # F5 totals 5, 2, 0, 4 over 2.5
        assert r.market_prob == pytest.approx(0.5)  # -110 / -110 de-vigs to a coin flip
        assert r.outcome == 1  # F5 real total 4 > 2.5
        assert r.market_side_price == -110.0 and r.market_other_price == -110.0

    def test_three_way_record_uses_a_three_way_devig_and_no_other_price(self, runs):
        bt = _backtest()
        odds = _closing("f1_moneyline", home_ml=255.0, away_ml=290.0, draw_ml=-130.0)
        grid = {"home": [1, 0, 0, 0, 0, 0, 0, 0, None], "away": [0, 0, 0, 0, 0, 0, 0, 0, 0]}
        recs = bt.score_segment_market_accuracy(1, runs, odds, grid)
        assert len(recs) == 1
        r = recs[0]
        assert r.sim_prob == 0.25  # F1 home led in one of four
        # The market's fair home probability from the three-way de-vig.
        from betting.clv_engine import devig_multiway, implied_prob_from_american

        fair = devig_multiway(
            [
                implied_prob_from_american(255.0),
                implied_prob_from_american(290.0),
                implied_prob_from_american(-130.0),
            ]
        )
        assert r.market_prob == pytest.approx(fair[0])
        assert sum(fair) == pytest.approx(1.0)
        assert r.outcome == 1  # home scored in the first, away did not
        assert r.market_other_price is None  # no single fade price on a three-way

    def test_three_way_without_a_draw_price_is_skipped(self, runs):
        bt = _backtest()
        odds = _closing("f1_moneyline", home_ml=255.0, away_ml=290.0, draw_ml=None)
        grid = {"home": [1] + [0] * 8, "away": [0] * 9}
        assert bt.score_segment_market_accuracy(1, runs, odds, grid) == []

    def test_push_and_missing_grid_are_skipped(self, runs):
        bt = _backtest()
        odds = _closing("f5_total", total_line=4.0, over_ml=-110.0, under_ml=-110.0)
        grid = {"home": [0, 0, 3, 0, 0, 0, 0, 0, None], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        assert bt.score_segment_market_accuracy(1, runs, odds, grid) == []  # 4 == 4.0
        assert bt.score_segment_market_accuracy(1, runs, odds, None) == []

    def test_yes_no_and_first_to_score_and_run_line_records(self, runs):
        bt = _backtest()
        odds = {}
        odds.update(_closing("first_inning_run", total_line=0.5, over_ml=-115.0, under_ml=-120.0))
        odds.update(_closing("first_to_score", home_ml=110.0, away_ml=-145.0))
        odds.update(
            _closing(
                "f5_runline",
                home_spread=-0.5,
                home_spread_ml=105.0,
                away_spread=0.5,  # SIM-549: the mirror of the home spread — a pair
                away_spread_ml=-140.0,
            )
        )
        grid = {"home": [0, 0, 3, 0, 0, 0, 0, 0, None], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        recs = {r.market: r for r in bt.score_segment_market_accuracy(1, runs, odds, grid)}
        assert set(recs) == {"first_inning_run", "first_to_score", "f5_runline"}
        assert recs["first_inning_run"].outcome == 1 and recs["first_inning_run"].sim_prob == 0.75
        assert recs["first_to_score"].outcome == 0 and recs["first_to_score"].sim_prob == 0.25
        assert recs["f5_runline"].outcome == 1 and recs["f5_runline"].sim_prob == 0.25

    def test_full_game_markets_are_not_scored_here(self, runs):
        bt = _backtest()
        odds = _closing("total", total_line=7.5, over_ml=-110.0, under_ml=-110.0)
        grid = {"home": [5] + [0] * 8, "away": [3] + [0] * 8}
        assert bt.score_segment_market_accuracy(1, runs, odds, grid) == []


# ===========================================================================
# SIM-549: a run line listed as two separate bets
# ===========================================================================

# Five iterations whose first-five margins (home - away) are +2, +1, 0, -2, -3.
_F5_MARGIN_GRIDS = [
    ([2, 0, 0, 0, 0, 0, 0, 0, None], [0] * 9),
    ([1, 0, 0, 0, 0, 0, 0, 0, None], [0] * 9),
    ([0] * 9, [0] * 9),
    ([0] * 9, [2, 0, 0, 0, 0, 0, 0, 0, 0]),
    ([0] * 9, [3, 0, 0, 0, 0, 0, 0, 0, 0]),
]

# Game 744796's closing rows as the store holds them (the 2026-09-12 load) and
# its official grid: 4-4 after five innings, 4-7 final.
_G744796_ODDS = {
    "f5_runline": {
        "closing": {
            "home_spread": -1.5,
            "home_spread_ml": 350.0,
            "away_spread": -1.5,
            "away_spread_ml": 150.0,
        }
    },
    "f5_total": {"closing": {"total_line": 4.5, "over_ml": -109.0, "under_ml": -121.0}},
    "f5_moneyline": {"closing": {"home_ml": 175.0, "away_ml": -127.0, "draw_ml": 475.0}},
}
_G744796_GRID = {"home": [1, 0, 3, 0, 0, 0, 0, 0, 0], "away": [0, 1, 3, 0, 0, 0, 0, 0, 3]}


def _implied(american: float) -> float:
    from betting.clv_engine import implied_prob_from_american

    return implied_prob_from_american(american)


def _run_line_records(bt, odds, grid, runs):
    return {
        r.market: r
        for r in bt.score_segment_market_accuracy(744796, runs, odds, grid)
        if r.market_type == "f5_runline"
    }


class TestRunLineTwoBets:
    def test_market_probability_and_outcome_take_a_side(self, runs):
        f5 = SegmentRuns.from_inning_grids(_F5_MARGIN_GRIDS)
        # the away bet at its own spread A is the away leg at the mirrored home spread -A
        for a in (-1.5, -0.5, 0.0, 0.5, 1.5):
            assert market_probability(f5, "f5_runline", line=a, side="away") == pytest.approx(
                cover_probabilities(f5, "f5_runline", -a)[1]
            )
        assert market_probability(f5, "f5_runline", line=-1.5, side="home") == pytest.approx(0.2)
        assert market_probability(f5, "f5_runline", line=-1.5, side="away") == pytest.approx(0.4)
        # the home side stays the default
        assert market_probability(f5, "f5_runline", line=-1.5) == pytest.approx(0.2)
        # the outcome mirrors: a 3-1 home lead after five
        a = SegmentRuns.from_official_grid(
            {"home": [3, 0, 0, 0, 0, 0, 0, 0, None], "away": [1] + [0] * 8}
        )
        assert market_outcome(a, "f5_runline", line=-1.5, side="home") == 1
        assert market_outcome(a, "f5_runline", line=-1.5, side="away") == 0
        assert market_outcome(a, "f5_runline", line=2.5, side="away") == 1  # lost by two, +2.5
        assert market_outcome(a, "f5_runline", line=2.0, side="away") is None  # a push
        # a total-kind market ignores the side
        assert market_probability(runs, "f5_total", line=2.5, side="away") == market_probability(
            runs, "f5_total", line=2.5
        )
        with pytest.raises(ValueError):
            market_probability(f5, "f5_runline", line=-1.5, side="over")

    def test_first_five_run_line_two_bets_on_the_real_payload(self):
        bt = _backtest()
        f5 = SegmentRuns.from_inning_grids(_F5_MARGIN_GRIDS)
        recs = _run_line_records(bt, _G744796_ODDS, _G744796_GRID, f5)
        assert set(recs) == {"f5_runline", "f5_runline_away"}
        home, away = recs["f5_runline"], recs["f5_runline_away"]
        assert home.market_type == away.market_type == "f5_runline"
        # the book's margin from the same game's first-five TOTAL, never the
        # three-way first-five moneyline (whose margin prices the tie)
        margin = _implied(-109.0) + _implied(-121.0)
        assert margin == pytest.approx(1.0690, abs=1e-4)
        assert home.market_prob == pytest.approx((100 / 450) / margin)
        assert home.market_prob == pytest.approx(0.2079, abs=1e-4)
        assert away.market_prob == pytest.approx((100 / 250) / margin)
        assert away.market_prob == pytest.approx(0.3742, abs=1e-4)
        assert home.market_other_price is None and away.market_other_price is None
        assert home.market_side_price == 350.0 and away.market_side_price == 150.0
        # 4-4 after five: both bets lost
        assert home.outcome == 0 and away.outcome == 0
        # the simulator: home covers -1.5 on the +2 only; away covers -1.5 on -2 and -3
        assert home.sim_prob == pytest.approx(0.2)
        assert away.sim_prob == pytest.approx(0.4)

    def test_run_line_pair_is_unchanged(self, runs):
        from betting.clv_engine import devig_two_way

        bt = _backtest()
        odds = _closing(
            "f5_runline",
            home_spread=-0.5,
            home_spread_ml=105.0,
            away_spread=0.5,
            away_spread_ml=-140.0,
        )
        grid = {"home": [0, 0, 3, 0, 0, 0, 0, 0, None], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        recs = [
            r
            for r in bt.score_segment_market_accuracy(1, runs, odds, grid)
            if "runline" in r.market
        ]
        assert len(recs) == 1
        r = recs[0]
        assert (r.market, r.market_type) == ("f5_runline", "f5_runline")
        assert r.market_prob == pytest.approx(devig_two_way(105.0, -140.0)[0])
        assert r.market_side_price == 105.0 and r.market_other_price == -140.0
        assert r.sim_prob == market_probability(runs, "f5_runline", line=-0.5)
        assert r.outcome == 1

    def test_run_line_mirror_rule(self, runs):
        bt = _backtest()
        # the pick'em 0 / 0 IS a pair (the same sign, and the mirror of each other)
        assert bt.run_line_is_pair(0.0, 0.0)
        assert bt.run_line_is_pair(-1.5, 1.5) and bt.run_line_is_pair(1.5, -1.5)
        # two different lines are two bets, whatever their signs
        assert not bt.run_line_is_pair(0.5, -1.5)
        assert not bt.run_line_is_pair(-1.0, 1.5)
        assert not bt.run_line_is_pair(-1.5, -1.5)
        assert not bt.run_line_is_pair(-1.5, None)
        grid = {"home": [0, 0, 3, 0, 0, 0, 0, 0, None], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        odds = {
            **_closing(
                "f5_runline",
                home_spread=0.5,
                home_spread_ml=-200.0,
                away_spread=-1.5,
                away_spread_ml=250.0,
            ),
            **_closing("f5_total", total_line=4.5, over_ml=-110.0, under_ml=-110.0),
        }
        recs = {r.market: r for r in bt.score_segment_market_accuracy(1, runs, odds, grid)}
        # each bet at its OWN line: home +0.5 (covers on 3-1), away -1.5 (loses)
        assert recs["f5_runline"].sim_prob == market_probability(runs, "f5_runline", line=0.5)
        assert recs["f5_runline_away"].sim_prob == market_probability(
            runs, "f5_runline", line=-1.5, side="away"
        )
        assert recs["f5_runline"].outcome == 1 and recs["f5_runline_away"].outcome == 0
        # a missing away spread: the shape is unknown, so no record
        no_away = _closing(
            "f5_runline", home_spread=-0.5, home_spread_ml=105.0, away_spread_ml=-140.0
        )
        assert bt.score_segment_market_accuracy(1, runs, no_away, grid) == []

    def test_one_sided_push_drops_that_side_only(self, runs):
        bt = _backtest()
        odds = {
            **_closing(
                "f5_runline",
                home_spread=-1.0,
                home_spread_ml=160.0,
                away_spread=0.0,
                away_spread_ml=-180.0,
            ),
            **_closing("f5_total", total_line=4.5, over_ml=-110.0, under_ml=-110.0),
        }

        def run_line(grid):
            return [
                (r.market, r.outcome)
                for r in bt.score_segment_market_accuracy(1, runs, odds, grid)
                if "runline" in r.market
            ]

        # a one-run home lead: home -1 pushes, away 0 loses
        lead = {"home": [2, 0, 0, 0, 0, 0, 0, 0, None], "away": [1] + [0] * 8}
        assert run_line(lead) == [("f5_runline_away", 0)]
        # a tie: away 0 pushes, home -1 loses
        tie = {"home": [1] + [0] * 8, "away": [1] + [0] * 8}
        assert run_line(tie) == [("f5_runline", 0)]

    def test_reference_margin_order(self):
        bt = _backtest()
        f5_total = _closing("f5_total", over_ml=-109.0, under_ml=-121.0)
        total = _closing("total", over_ml=-108.0, under_ml=-112.0)
        moneyline = _closing("moneyline", home_ml=145.0, away_ml=-170.0)
        three_way = _closing("f5_moneyline", home_ml=175.0, away_ml=-127.0, draw_ml=475.0)
        everything = {**f5_total, **total, **moneyline, **three_way}
        margin, source = bt.reference_margin(everything, "f5")
        assert source == "f5_total"
        assert margin == pytest.approx(_implied(-109.0) + _implied(-121.0))
        margin, source = bt.reference_margin({**total, **moneyline, **three_way}, "f5")
        assert source == "total"
        assert margin == pytest.approx(_implied(-108.0) + _implied(-112.0))
        margin, source = bt.reference_margin({**moneyline, **three_way}, "f5")
        assert source == "moneyline"
        assert margin == pytest.approx(_implied(145.0) + _implied(-170.0))
        # a three-way market never enters: with only it, the flat margin
        assert bt.reference_margin(three_way, "f5") == (bt.DEFAULT_ONE_SIDED_MARGIN, "flat")
        # ... even when its home and away prices alone sit inside the band, as
        # on more than half the stored first-five moneyline rows (1.023 here)
        in_band = _closing("f5_moneyline", home_ml=105.0, away_ml=-115.0, draw_ml=450.0)
        assert 1.0 <= _implied(105.0) + _implied(-115.0) <= 1.15
        assert bt.reference_margin(in_band, "f5") == (bt.DEFAULT_ONE_SIDED_MARGIN, "flat")
        assert bt.reference_margin({**in_band, **moneyline}, "f5")[1] == "moneyline"
        f1_in_band = _closing("f1_moneyline", home_ml=105.0, away_ml=-115.0, draw_ml=150.0)
        assert bt.reference_margin(f1_in_band, "f1") == (bt.DEFAULT_ONE_SIDED_MARGIN, "flat")
        assert bt.reference_margin({}, "game") == (1.05, "flat")
        # the full game starts from the full-game total; the first inning from its own
        f1_total = _closing("f1_total", over_ml=-120.0, under_ml=-105.0)
        assert bt.reference_margin({**f1_total, **total}, "f1")[1] == "f1_total"
        assert bt.reference_margin({**f5_total, **total}, "game")[1] == "total"
        # a stored total whose prices are not a pair (they add to under 1.00) is skipped
        broken = _closing("f5_total", over_ml=150.0, under_ml=150.0)
        assert bt.reference_margin({**broken, **total}, "f5")[1] == "total"
        # ... and one whose prices add to more than 1.15 (0.6 + 0.6)
        wide = _closing("f5_total", over_ml=-150.0, under_ml=-150.0)
        assert bt.reference_margin({**wide, **total}, "f5")[1] == "total"

    def test_the_tally_names_the_two_shapes(self, runs):
        bt = _backtest()
        f5 = SegmentRuns.from_inning_grids(_F5_MARGIN_GRIDS)
        recs = bt.score_segment_market_accuracy(744796, f5, _G744796_ODDS, _G744796_GRID)
        pair_odds = _closing(
            "f5_runline",
            home_spread=-0.5,
            home_spread_ml=105.0,
            away_spread=0.5,
            away_spread_ml=-140.0,
        )
        recs += bt.score_segment_market_accuracy(2, runs, pair_odds, _G744796_GRID)
        tally = bt.market_shape_tally(recs)
        shape = tally["run_lines"]["f5_runline"]
        assert shape["pairs"] == 1
        assert shape["one_sided_games"] == 1 and shape["one_sided_records"] == 2
        assert shape["one_record_games"] == 0
        assert shape["paired_margin_mean"] == pytest.approx(_implied(105.0) + _implied(-140.0))
        # the one-sided records carry no fade price, so only the pair enters the margin
        assert tally["margin_mean"]["f5_runline"] == pytest.approx(shape["paired_margin_mean"])
        assert "f5_runline_away" not in tally["margin_mean"]
        # the line check of each side's one-sided bets
        line = shape["one_sided_line"]
        assert line["home"]["n"] == 1 and line["away"]["n"] == 1
        assert line["home"]["line_mean"] == pytest.approx(0.2079, abs=1e-4)
        assert line["home"]["outcome_rate"] == 0.0

    def test_a_game_with_one_record_is_counted_whatever_dropped_the_other(self, runs):
        bt = _backtest()
        odds = {
            **_closing(
                "f5_runline",
                home_spread=-1.0,
                home_spread_ml=160.0,
                away_spread=0.0,
                away_spread_ml=-180.0,
            ),
            **_closing("f5_total", total_line=4.5, over_ml=-110.0, under_ml=-110.0),
        }
        lead = {"home": [2, 0, 0, 0, 0, 0, 0, 0, None], "away": [1] + [0] * 8}
        recs = bt.score_segment_market_accuracy(1, runs, odds, lead)  # the home bet pushed
        shape = bt.market_shape_tally(recs)["run_lines"]["f5_runline"]
        assert shape["one_sided_games"] == 1 and shape["one_record_games"] == 1
        assert shape["one_sided_line"]["home"] is None

    def test_a_mis_stored_one_sided_line_is_named(self, caplog):
        import logging

        bt = _backtest()

        def rec(g, market, mkt, y):
            return bt.AccuracyRecord(
                game_pk=g,
                market=market,
                market_type="f1_runline",
                sim_prob=0.5,
                market_prob=mkt,
                outcome=y,
                market_side_price=-1600.0,
                market_other_price=None,
            )

        # the 2025 first-inning +1 / +1 shape: the line says 0.465, the bet wins 88%
        bad = [rec(g, "f1_runline", 0.465, int(g % 8 != 0)) for g in range(80)]
        tally = bt.market_shape_tally(bad)
        check = tally["run_lines"]["f1_runline"]["one_sided_line"]["home"]
        assert check["n"] == 80 and check["z"] < -bt.ONE_SIDED_LINE_Z
        with caplog.at_level(logging.WARNING):
            bt.warn_market_shapes(tally)
        assert "f1_runline home bets" in caplog.text and "Mis-stored lines?" in caplog.text
        # a calibrated one-sided line raises no such warning
        caplog.clear()
        good = [rec(g, "f1_runline", 0.5, g % 2) for g in range(80)]
        with caplog.at_level(logging.WARNING):
            bt.warn_market_shapes(bt.market_shape_tally(good))
        assert "Mis-stored" not in caplog.text


# ===========================================================================
# A real simulated play stream through the same path
# ===========================================================================


class TestFromRealPlays:
    """Three real simulated games (the no-DB recorder path) through the same
    builders: the grids must reproduce each game's final score, and every
    market must be priceable from them."""

    SIM_KWARGS = {
        "season": 2024,
        "pitcher_id": 600001,
        "bat_hand": "R",
        "away_lineup": [101, 102, 103, 104, 105, 106, 107, 108, 109],
        "home_lineup": [201, 202, 203, 204, 205, 206, 207, 208, 209],
        "max_innings": 12,
    }

    def test_recorded_plays_produce_grids_that_match_the_results(self):
        from simulation.linescore import linescore_from_plays
        from simulation.play_recorder import record_game_plays

        results, grids = [], []
        for seed in (7, 42, 99):
            result, plays = record_game_plays(seed=seed, sim_kwargs=dict(self.SIM_KWARGS))
            ls = linescore_from_plays(plays)
            results.append(result)
            grids.append((list(ls.home_by_inning), list(ls.away_by_inning)))
        runs = SegmentRuns.from_inning_grids(grids)
        assert runs.n == 3
        assert runs.home_game.tolist() == [r.home_score for r in results]
        assert runs.away_game.tolist() == [r.away_score for r in results]
        # The first five innings never exceed the game.
        assert bool(np.all(runs.home_f5 <= runs.home_game))
        assert bool(np.all(runs.away_f1 <= runs.away_f5))
        for m in GAME_MARKET_TYPES:
            side_market = m in ("moneyline", "first_to_score", "f1_moneyline", "f5_moneyline")
            p = market_probability(runs, m, line=None if side_market else 1.5)
            assert 0.0 <= p <= 1.0
        # A completed game always has a first scorer or ended 0-0 (impossible here).
        assert set(runs.first_to_score.tolist()) <= {HOME_FIRST, AWAY_FIRST}
