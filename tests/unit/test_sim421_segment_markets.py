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
            _closing("f5_runline", home_spread=-0.5, home_spread_ml=105.0, away_spread_ml=-140.0)
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
