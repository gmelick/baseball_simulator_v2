"""
test_backend_sim330.py
======================
Unit tests for SIM-330 -- the **calibrated game win-probability** output
:func:`simulation.win_probability.win_probability` (Phase 4, Sprint 3).

These tests build synthetic :class:`GameSimResult`s / :class:`GameSimSummary`s
(no loop run, no DB/FAISS) and assert the calibration contract:

  * a lopsided run yields a high home win probability;
  * 0 home wins out of N yields a small-BUT-NONZERO probability (smoothing
    works -- a raw frequency would be a hard 0.0);
  * home + away probabilities sum to 1.0;
  * the CI contains the point estimate and narrows as N grows;
  * tie handling (SPLIT vs DROP) behaves as documented;
  * the calibration-map seam is honoured (identity by default; a fitted map
    plugs in);
  * the transform is deterministic (same input -> same output, no RNG).
"""

from __future__ import annotations

import pytest

from simulation.game_state import GameState
from simulation.results import ConfidenceInterval, GameSimResult, GameSimSummary
from simulation.win_probability import (
    IDENTITY_CALIBRATION,
    JEFFREYS_ALPHA,
    LAPLACE_ALPHA,
    CalibrationMap,
    TieHandling,
    WinProbability,
    win_probability,
)

# ===========================================================================
# Synthetic-input builders (mirror the SIM-327 test idiom)
# ===========================================================================


def _state() -> GameState:
    """A minimal valid GameState to satisfy GameSimResult.final_state."""
    return GameState(pitcher_id=0, bat_hand="R", season=2024)


def _result(home: int, away: int) -> GameSimResult:
    return GameSimResult(
        home_score=home,
        away_score=away,
        innings_played=9,
        final_state=_state(),
    )


def _summary(scores: list[tuple[int, int]], **kw) -> GameSimSummary:
    return GameSimSummary.from_results([_result(h, a) for (h, a) in scores], **kw)


def _summary_counts(home_wins: int, away_wins: int, ties: int = 0) -> GameSimSummary:
    """A summary with exactly the given home/away/tie outcome counts.

    Home wins use score (1, 0); away wins (0, 1); ties (0, 0).
    """
    scores = [(1, 0)] * home_wins + [(0, 1)] * away_wins + [(0, 0)] * ties
    return _summary(scores)


# ===========================================================================
# Lopsided run -> high home probability
# ===========================================================================


def test_lopsided_run_yields_high_home_prob():
    # 95 home wins, 5 away wins out of 100.
    s = _summary_counts(home_wins=95, away_wins=5)
    wp = win_probability(s)
    assert isinstance(wp, WinProbability)
    assert wp.home_win_prob > 0.9
    assert wp.away_win_prob < 0.1
    assert wp.home_win_prob > wp.away_win_prob


def test_lopsided_other_direction():
    s = _summary_counts(home_wins=3, away_wins=97)
    wp = win_probability(s)
    assert wp.away_win_prob > 0.9
    assert wp.home_win_prob < 0.1


# ===========================================================================
# Smoothing: 0 home wins of N -> small-but-NONZERO (never a hard 0.0/1.0)
# ===========================================================================


def test_zero_home_wins_is_small_but_nonzero():
    s = _summary_counts(home_wins=0, away_wins=50)
    wp = win_probability(s)
    assert wp.home_win_prob > 0.0  # smoothing prevents a hard 0.0
    assert wp.home_win_prob < 0.05  # but it is still small
    # away is correspondingly < 1.0 (never a hard 1.0).
    assert wp.away_win_prob < 1.0
    assert wp.home_win_prob + wp.away_win_prob == pytest.approx(1.0)


def test_all_home_wins_is_high_but_below_one():
    s = _summary_counts(home_wins=50, away_wins=0)
    wp = win_probability(s)
    assert wp.home_win_prob < 1.0  # never a hard 1.0
    assert wp.home_win_prob > 0.95


def test_smaller_n_pulls_harder_toward_half():
    # Same 0% raw home rate; smaller N -> estimate further from 0 (more shrinkage).
    small = win_probability(_summary_counts(home_wins=0, away_wins=5))
    large = win_probability(_summary_counts(home_wins=0, away_wins=500))
    assert small.home_win_prob > large.home_win_prob
    # both still strictly between 0 and 0.5.
    assert 0.0 < large.home_win_prob < small.home_win_prob < 0.5


def test_jeffreys_default_matches_explicit():
    s = _summary_counts(home_wins=0, away_wins=10)
    assert win_probability(s).home_win_prob == pytest.approx(
        win_probability(s, alpha=JEFFREYS_ALPHA).home_win_prob
    )
    # Jeffreys posterior mean for 0/10 = 0.5 / (10 + 1) = 0.04545...
    assert win_probability(s).home_win_prob == pytest.approx(0.5 / 11.0)


def test_laplace_alpha_one():
    s = _summary_counts(home_wins=0, away_wins=10)
    wp = win_probability(s, alpha=LAPLACE_ALPHA)
    # Laplace posterior mean for 0/10 = 1 / (10 + 2) = 0.08333...
    assert wp.home_win_prob == pytest.approx(1.0 / 12.0)
    assert wp.alpha == pytest.approx(1.0)


def test_alpha_zero_is_raw_frequency():
    # alpha == 0 -> no smoothing -> raw frequency (can be a hard 0.0).
    s = _summary_counts(home_wins=0, away_wins=10)
    wp = win_probability(s, alpha=0.0)
    assert wp.home_win_prob == pytest.approx(0.0)


def test_negative_alpha_raises():
    with pytest.raises(ValueError):
        win_probability(_summary_counts(5, 5), alpha=-0.1)


# ===========================================================================
# Probabilities sum to 1.0
# ===========================================================================


@pytest.mark.parametrize(
    "hw,aw,ties",
    [(50, 50, 0), (95, 5, 0), (0, 100, 0), (40, 30, 30), (1, 0, 0), (0, 0, 10)],
)
def test_probs_sum_to_one(hw, aw, ties):
    wp = win_probability(_summary_counts(hw, aw, ties))
    assert wp.home_win_prob + wp.away_win_prob == pytest.approx(1.0)
    assert 0.0 <= wp.home_win_prob <= 1.0
    assert 0.0 <= wp.away_win_prob <= 1.0


# ===========================================================================
# Tie handling: SPLIT vs DROP
# ===========================================================================


def test_tie_split_counts_half_each():
    # 40 home, 40 away, 20 ties -> SPLIT: home gets 40 + 10 = 50 of 100 -> ~0.5.
    s = _summary_counts(home_wins=40, away_wins=40, ties=20)
    wp = win_probability(s, tie_handling=TieHandling.SPLIT)
    assert wp.home_win_prob == pytest.approx(0.5, abs=1e-9)
    assert wp.n_decisive == 100  # ties stay in the denominator
    assert wp.tie_pct == pytest.approx(0.2)


def test_tie_drop_conditions_on_decisive():
    # 30 home, 10 away, 60 ties -> DROP: home of decisive = 30 / 40.
    s = _summary_counts(home_wins=30, away_wins=10, ties=60)
    wp = win_probability(s, tie_handling=TieHandling.DROP, alpha=0.0)
    assert wp.home_win_prob == pytest.approx(30.0 / 40.0)
    assert wp.n_decisive == 40  # ties removed from the denominator
    assert wp.tie_pct == pytest.approx(0.6)


def test_tie_drop_all_ties_falls_back_to_half():
    # Every game a tie -> no decisive signal -> prior centre 0.5.
    s = _summary_counts(home_wins=0, away_wins=0, ties=20)
    wp = win_probability(s, tie_handling=TieHandling.DROP)
    assert wp.home_win_prob == pytest.approx(0.5)
    assert wp.away_win_prob == pytest.approx(0.5)


def test_tie_handling_recorded():
    s = _summary_counts(10, 10)
    assert win_probability(s).tie_handling is TieHandling.SPLIT
    assert win_probability(s, tie_handling=TieHandling.DROP).tie_handling is TieHandling.DROP


# ===========================================================================
# Confidence interval: contains point estimate; narrows as N grows
# ===========================================================================


def test_ci_contains_point_estimate():
    wp = win_probability(_summary_counts(home_wins=70, away_wins=30))
    ci = wp.home_win_ci
    assert isinstance(ci, ConfidenceInterval)
    assert ci.low <= ci.point <= ci.high
    assert ci.point == pytest.approx(wp.home_win_prob)
    assert ci.method == "normal"
    assert ci.level == pytest.approx(0.95)


def test_ci_nondegenerate_even_for_lopsided():
    # A raw Wald interval at p=0 collapses to a point; the SMOOTHED interval does
    # not, because the smoothed p is interior.
    wp = win_probability(_summary_counts(home_wins=0, away_wins=50))
    assert wp.home_win_ci.high > wp.home_win_ci.low  # strictly non-degenerate
    assert 0.0 <= wp.home_win_ci.low <= wp.home_win_ci.high <= 1.0


def test_ci_narrows_as_n_grows():
    # Same 70% home rate, more iterations -> tighter interval.
    small = win_probability(_summary_counts(home_wins=70, away_wins=30))
    large = win_probability(_summary_counts(home_wins=7000, away_wins=3000))
    assert large.home_win_ci.half_width < small.home_win_ci.half_width


def test_ci_wider_at_higher_confidence():
    s = _summary_counts(home_wins=70, away_wins=30)
    ci95 = win_probability(s, confidence_level=0.95).home_win_ci
    ci99 = win_probability(s, confidence_level=0.99).home_win_ci
    assert ci99.half_width > ci95.half_width
    assert ci99.level == pytest.approx(0.99)


# ===========================================================================
# Calibration-map seam (identity default; fitted map plugs in)
# ===========================================================================


def test_default_calibration_is_identity():
    wp = win_probability(_summary_counts(70, 30))
    assert wp.calibration_map == "identity"


def test_fitted_calibration_map_is_applied():
    # A toy "fitted" map that sharpens probabilities away from 0.5.
    def sharpen(p: float) -> float:
        return p**2 / (p**2 + (1.0 - p) ** 2)

    cmap = CalibrationMap(fn=sharpen, name="toy-sharpen")
    raw = win_probability(_summary_counts(70, 30))
    cal = win_probability(_summary_counts(70, 30), calibration_map=cmap)
    assert cal.calibration_map == "toy-sharpen"
    # sharpening pushes a >0.5 prob higher.
    assert cal.home_win_prob > raw.home_win_prob
    assert cal.home_win_prob + cal.away_win_prob == pytest.approx(1.0)


def test_calibration_map_result_clamped():
    # A pathological map that returns out-of-range values is clamped to [0, 1].
    cmap = CalibrationMap(fn=lambda p: 5.0, name="overflow")
    wp = win_probability(_summary_counts(70, 30), calibration_map=cmap)
    assert wp.home_win_prob == pytest.approx(1.0)
    assert wp.away_win_prob == pytest.approx(0.0)


def test_identity_singleton_round_trips():
    wp = win_probability(_summary_counts(70, 30), calibration_map=IDENTITY_CALIBRATION)
    assert wp.calibration_map == "identity"


# ===========================================================================
# Determinism (no RNG of its own)
# ===========================================================================


def test_deterministic_same_input_same_output():
    s = _summary_counts(home_wins=63, away_wins=37, ties=0)
    a = win_probability(s)
    b = win_probability(s)
    assert a.home_win_prob == b.home_win_prob
    assert a.away_win_prob == b.away_win_prob
    assert a.home_win_ci.low == b.home_win_ci.low
    assert a.home_win_ci.high == b.home_win_ci.high
    assert a.n_decisive == b.n_decisive


def test_deterministic_across_equivalent_summaries():
    # Two summaries built from the same scores produce the same win prob.
    scores = [(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]
    a = win_probability(_summary(scores))
    b = win_probability(_summary(scores))
    assert a.home_win_prob == b.home_win_prob


# ===========================================================================
# Accepts a raw list of results (convenience path)
# ===========================================================================


def test_accepts_raw_result_list():
    results = [_result(5, 1), _result(4, 2), _result(0, 9)]
    wp = win_probability(results)
    assert wp.n_iterations == 3
    # 2 home wins of 3, Jeffreys: (2 + 0.5) / (3 + 1) = 0.625.
    assert wp.home_win_prob == pytest.approx(2.5 / 4.0)


def test_list_and_summary_agree():
    scores = [(5, 1), (4, 2), (6, 0), (1, 3), (2, 7)]
    from_list = win_probability([_result(h, a) for (h, a) in scores])
    from_summary = win_probability(_summary(scores))
    assert from_list.home_win_prob == pytest.approx(from_summary.home_win_prob)


def test_empty_list_raises():
    with pytest.raises(ValueError):
        win_probability([])


# ===========================================================================
# Result is self-describing
# ===========================================================================


def test_result_records_its_choices():
    s = _summary_counts(home_wins=70, away_wins=30)
    wp = win_probability(s, alpha=1.0, confidence_level=0.90)
    assert wp.n_iterations == 100
    assert wp.alpha == pytest.approx(1.0)
    assert wp.confidence_level == pytest.approx(0.90)
    assert wp.method == "beta-smoothed+wald"
