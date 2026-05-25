"""
test_backend_sim327.py
======================
Unit tests for SIM-327 -- the **multi-iteration aggregation contract**
:class:`simulation.results.GameSimSummary` (Phase 4, Sprint 3).

These tests build N synthetic per-game :class:`GameSimResult`s (no loop run, no
DB/FAISS) and assert the aggregation contract:

  * the three win rates (home / away / tie) sum to 1.0, ties included;
  * mean and median of home / away / total scores are correct;
  * the RAW per-iteration arrays are preserved (length N, INPUT ORDER);
  * ``simulated_at`` is a timezone-aware UTC timestamp;
  * the confidence intervals are sane (contain the point estimate and narrow as
    N grows);
  * :class:`GameSimResult` is re-exported from ``simulation.results`` (one import
    home for downstream consumers).
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

import numpy as np
import pytest

from simulation.game_state import GameState
from simulation.results import ConfidenceInterval, GameSimResult, GameSimSummary

# GameSimResult is also defined in sim_loop; confirm results.py re-exports the
# SAME object so a consumer importing from either place gets one type.
from simulation.sim_loop import GameSimResult as _LoopGameSimResult


def _state() -> GameState:
    """A minimal valid GameState to satisfy GameSimResult.final_state."""
    return GameState(pitcher_id=0, bat_hand="R", season=2024)


def _result(home: int, away: int) -> GameSimResult:
    """A synthetic per-game result with the given final score."""
    return GameSimResult(
        home_score=home,
        away_score=away,
        innings_played=9,
        final_state=_state(),
    )


def _results(scores: list[tuple[int, int]]) -> list[GameSimResult]:
    return [_result(h, a) for (h, a) in scores]


# ===========================================================================
# Re-export / import-home contract
# ===========================================================================


def test_gamesimresult_is_reexported_from_results():
    """results.GameSimResult IS the loop's GameSimResult (one import home)."""
    assert GameSimResult is _LoopGameSimResult


# ===========================================================================
# Win-rate buckets sum to 1.0 (ties included)
# ===========================================================================


def test_win_pcts_sum_to_one_no_ties():
    # 3 home wins, 2 away wins, 0 ties out of 5.
    results = _results([(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)])
    s = GameSimSummary.from_results(results)
    assert s.n_iterations == 5
    assert s.home_win_pct == pytest.approx(3 / 5)
    assert s.away_win_pct == pytest.approx(2 / 5)
    assert s.tie_pct == pytest.approx(0.0)
    assert s.home_win_pct + s.away_win_pct + s.tie_pct == pytest.approx(1.0)


def test_win_pcts_sum_to_one_with_ties():
    # 2 home, 1 away, 1 tie out of 4 -> ties must be bucketed, not dropped.
    results = _results([(5, 1), (4, 2), (3, 3), (0, 9)])
    s = GameSimSummary.from_results(results)
    assert s.home_win_pct == pytest.approx(2 / 4)
    assert s.away_win_pct == pytest.approx(1 / 4)
    assert s.tie_pct == pytest.approx(1 / 4)
    assert s.home_win_pct + s.away_win_pct + s.tie_pct == pytest.approx(1.0)


def test_all_ties():
    results = _results([(2, 2), (3, 3), (0, 0)])
    s = GameSimSummary.from_results(results)
    assert s.tie_pct == pytest.approx(1.0)
    assert s.home_win_pct == 0.0
    assert s.away_win_pct == 0.0


# ===========================================================================
# Mean / median correctness
# ===========================================================================


def test_mean_and_median_correct():
    scores = [(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]
    results = _results(scores)
    s = GameSimSummary.from_results(results)

    home = [h for (h, _) in scores]
    away = [a for (_, a) in scores]
    total = [h + a for (h, a) in scores]

    assert s.home_score_mean == pytest.approx(statistics.mean(home))
    assert s.away_score_mean == pytest.approx(statistics.mean(away))
    assert s.total_score_mean == pytest.approx(statistics.mean(total))

    assert s.home_score_median == pytest.approx(statistics.median(home))
    assert s.away_score_median == pytest.approx(statistics.median(away))
    assert s.total_score_median == pytest.approx(statistics.median(total))


def test_median_even_count_is_average_of_middle_two():
    # 4 values -> median is the mean of the middle two (statistics semantics).
    scores = [(1, 0), (2, 0), (3, 0), (10, 0)]
    s = GameSimSummary.from_results(_results(scores))
    # sorted home = [1, 2, 3, 10] -> median = (2 + 3) / 2 = 2.5
    assert s.home_score_median == pytest.approx(2.5)


# ===========================================================================
# RAW per-iteration arrays preserved (length N, input order)
# ===========================================================================


def test_raw_arrays_preserved_length_and_order():
    scores = [(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]
    s = GameSimSummary.from_results(_results(scores))

    assert s.home_scores.shape == (5,)
    assert s.away_scores.shape == (5,)
    assert s.total_scores.shape == (5,)

    # order is the INPUT order (not sorted / not binned).
    assert list(s.home_scores) == [5, 4, 6, 1, 2]
    assert list(s.away_scores) == [1, 2, 0, 3, 7]
    assert list(s.total_scores) == [6, 6, 6, 4, 9]


def test_raw_arrays_are_not_a_histogram():
    # A histogram would collapse repeats; raw arrays keep every iteration.
    scores = [(3, 1), (3, 1), (3, 1)]
    s = GameSimSummary.from_results(_results(scores))
    assert len(s.home_scores) == 3
    assert list(s.home_scores) == [3, 3, 3]


# ===========================================================================
# simulated_at is timezone-aware UTC
# ===========================================================================


def test_simulated_at_is_utc_aware():
    s = GameSimSummary.from_results(_results([(1, 0)]))
    assert isinstance(s.simulated_at, datetime)
    assert s.simulated_at.tzinfo is not None
    assert s.simulated_at.utcoffset() == UTC.utcoffset(None)


def test_simulated_at_injectable():
    fixed = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
    s = GameSimSummary.from_results(_results([(1, 0)]), simulated_at=fixed)
    assert s.simulated_at == fixed


# ===========================================================================
# Confidence intervals: contain the point estimate; narrow as N grows
# ===========================================================================


def test_ci_contains_point_estimate():
    # mixed outcomes so win% and score variance are both non-degenerate.
    rng = np.random.default_rng(7)
    scores = [(int(rng.integers(0, 8)), int(rng.integers(0, 8))) for _ in range(40)]
    s = GameSimSummary.from_results(_results(scores))

    for ci in (
        s.home_win_ci,
        s.away_win_ci,
        s.home_score_ci,
        s.away_score_ci,
        s.total_score_ci,
    ):
        assert isinstance(ci, ConfidenceInterval)
        assert ci.low <= ci.point <= ci.high
        assert ci.level == pytest.approx(0.95)
        assert ci.method == "normal"

    # the CI point estimates match the headline summary numbers.
    assert s.home_win_ci.point == pytest.approx(s.home_win_pct)
    assert s.home_score_ci.point == pytest.approx(s.home_score_mean)


def test_proportion_ci_clamped_to_unit_interval():
    # all home wins -> p = 1.0; Wald margin is 0 at p=1, and never exceeds [0,1].
    s = GameSimSummary.from_results(_results([(5, 0)] * 10))
    assert s.home_win_pct == pytest.approx(1.0)
    assert 0.0 <= s.home_win_ci.low <= s.home_win_ci.high <= 1.0


def test_ci_narrows_as_n_grows():
    # Same underlying distribution, more iterations -> tighter intervals.
    def sample(n: int):
        rng = np.random.default_rng(123)
        sc = [(int(rng.integers(0, 8)), int(rng.integers(0, 8))) for _ in range(n)]
        return GameSimSummary.from_results(_results(sc))

    small = sample(50)
    large = sample(2000)

    assert large.home_win_ci.half_width < small.home_win_ci.half_width
    assert large.home_score_ci.half_width < small.home_score_ci.half_width
    assert large.total_score_ci.half_width < small.total_score_ci.half_width


def test_single_result_has_zero_width_score_ci():
    # n == 1 -> no spread is estimable; score CI collapses to the point.
    s = GameSimSummary.from_results(_results([(4, 2)]))
    assert s.home_score_ci.low == s.home_score_ci.high == s.home_score_ci.point
    assert s.n_iterations == 1


# ===========================================================================
# Guard rails
# ===========================================================================


def test_empty_results_raises():
    with pytest.raises(ValueError):
        GameSimSummary.from_results([])


def test_confidence_level_recorded():
    s = GameSimSummary.from_results(_results([(1, 0), (0, 1)]), confidence_level=0.99)
    assert s.confidence_level == pytest.approx(0.99)
    assert s.home_win_ci.level == pytest.approx(0.99)
    # a 99% interval is wider than a 95% interval for the same data.
    s95 = GameSimSummary.from_results(
        _results([(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]), confidence_level=0.95
    )
    s99 = GameSimSummary.from_results(
        _results([(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]), confidence_level=0.99
    )
    assert s99.home_score_ci.half_width > s95.home_score_ci.half_width
