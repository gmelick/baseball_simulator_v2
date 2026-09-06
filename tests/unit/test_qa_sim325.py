"""
test_qa_sim325.py
=================
SIM-325 -- tests for the **E2E historical-replay + chi-squared goodness-of-fit**
harness (:mod:`simulation.validation.replay_chi_squared`), the validation
spine's end-to-end check.

WHAT THESE TESTS COVER (the SIM-325 acceptance criteria)
--------------------------------------------------------
  1. The harness replays games through the SIM-320 ``simulate_game()`` loop and
     runs a chi-squared GOF of the simulated vs a reference per-team-game run
     distribution, reporting (chi2, dof, p) and asserting **p > 0.05**.  Because
     the real Statcast DB is not loadable here, the reference is a *calibrated
     league-average* distribution (the SIM-324 idiom) and the simulated draw is
     from the SAME calibrated model -- a self-consistency check that is
     meaningful and passes without the DB, with a documented real-data seam
     (:class:`HistoricalGame` / :func:`replay_and_test`) to plug real games.
  2. A **negative control**: a deliberately WRONG reference distribution is
     REJECTED (p < 0.05), proving the test has power.
  3. The **binning / pooling helper** is correct (histogram + low-expected-count
     tail pooling so the chi-squared assumptions hold).
  4. The real-data seam (:func:`replay_and_test` over :class:`HistoricalGame`)
     works end-to-end on a calibrated stand-in for actual history.

NOISE-ROBUSTNESS
----------------
Fixed seeds throughout; the simulated distribution is built from enough games
(150 team-pairs -> 300 team-games always-on) that the GOF is stable, the
reference distribution is a high-resolution constant baked from a 1500-game
calibrated draw (so the test is deterministic and does not re-pay for the
reference), and the gate is a p-value, not a brittle point tolerance.  The heavy
larger-sample replay is ``@pytest.mark.slow``.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simulation.validation.replay_chi_squared import (
    ChiSquaredResult,
    HistoricalGame,
    bin_run_totals,
    chi_squared_gof,
    pool_low_expected_bins,
    replay_and_test,
    replay_historical_games,
    run_total_distribution,
    simulate_run_distribution,
)

# ===========================================================================
# The reference per-team-game run distribution (bins 0,1,...,9,10+)
# ===========================================================================
#
# Baked from a 1500-game (3000 team-game) draw of the SIM-324 calibrated
# league-average model (mean ~4.4 R/team/G).  Using a FIXED probability vector
# (rather than re-simulating a reference every run) makes the always-on test
# deterministic and fast.  This is the "published / reference league
# run-per-game distribution" path of :func:`chi_squared_gof`; the real-data path
# (compare against ACTUAL historical run totals) is exercised by the
# HistoricalGame test below.
REFERENCE_RUN_DISTRIBUTION = [
    0.0640,
    0.1127,
    0.1310,
    0.1387,
    0.1277,
    0.1080,
    0.0907,
    0.0727,
    0.0497,
    0.0377,
    0.0673,
]
MAX_BIN = 10

# Always-on sample size: 150 games == 300 team-games. With the fixed reference
# this gives a stable, comfortably-passing p-value (~0.3-0.6) in ~1s.
ALWAYS_ON_GAMES = 150


# ===========================================================================
# 1. Binning + pooling helper correctness
# ===========================================================================


class TestBinningAndPooling:
    def test_bin_run_totals_histograms_into_0_to_maxbin_plus(self):
        totals = [0, 1, 1, 2, 3, 3, 3, 12, 15]  # two totals land in the 10+ tail
        h = bin_run_totals(totals, max_bin=10)
        assert h.shape[0] == 11  # 0..9 plus the 10+ tail
        assert h[0] == 1
        assert h[1] == 2
        assert h[2] == 1
        assert h[3] == 3
        assert h[10] == 2  # the two >=10 totals pooled into the open tail
        assert h.sum() == len(totals)

    def test_run_total_distribution_is_bin_alias(self):
        totals = [0, 2, 2, 5]
        assert np.array_equal(
            run_total_distribution(totals, max_bin=6),
            bin_run_totals(totals, max_bin=6),
        )

    def test_bin_run_totals_rejects_negative(self):
        with pytest.raises(ValueError):
            bin_run_totals([0, 1, -1], max_bin=5)

    def test_pool_low_expected_collapses_sparse_tail(self):
        # Expected counts: a fat body and a thin high tail that must be pooled
        # until every expected bin is >= min_expected (Cochran's rule).
        observed = np.array([20.0, 30.0, 25.0, 4.0, 1.0, 0.0], dtype=float)
        expected = np.array([22.0, 28.0, 24.0, 4.0, 1.5, 0.5], dtype=float)
        obs, exp, labels = pool_low_expected_bins(observed, expected, min_expected=5.0)
        # Every pooled expected bin now clears the threshold...
        assert all(e >= 5.0 for e in exp)
        # ...and pooling conserves the total counts (no games lost).
        assert obs.sum() == observed.sum()
        assert math.isclose(exp.sum(), expected.sum())
        # The sparse 3 high bins (indices 3,4,5) collapse into the body; the last
        # label is an open "k+" tail because index 5 was the original open tail.
        assert labels[-1].endswith("+")

    def test_pool_keeps_well_populated_bins_separate(self):
        observed = np.array([20.0, 30.0, 25.0, 18.0], dtype=float)
        expected = np.array([22.0, 28.0, 24.0, 19.0], dtype=float)
        obs, exp, labels = pool_low_expected_bins(observed, expected, min_expected=5.0)
        # Nothing was sparse -> no pooling -> all four bins survive.
        assert len(obs) == 4
        assert labels == ["0", "1", "2", "3+"]

    def test_pool_low_end_merges_upward_if_sparse(self):
        # A sparse FIRST bin must merge upward so no bin is left under-populated.
        observed = np.array([1.0, 40.0, 40.0], dtype=float)
        expected = np.array([2.0, 39.0, 41.0], dtype=float)
        obs, exp, labels = pool_low_expected_bins(observed, expected, min_expected=5.0)
        assert all(e >= 5.0 for e in exp)
        assert obs.sum() == observed.sum()


# ===========================================================================
# 2. The chi-squared GOF passes for the calibrated self-consistency replay
# ===========================================================================


class TestChiSquaredReplayPasses:
    def test_calibrated_replay_matches_reference_distribution(self):
        # Replay 150 calibrated games through simulate_game(), then chi-squared
        # the simulated per-team-game run distribution against the reference
        # league distribution.  p MUST exceed 0.05 (the spec gate).
        sim = simulate_run_distribution(ALWAYS_ON_GAMES, base_seed=0)
        assert len(sim) == 2 * ALWAYS_ON_GAMES  # home + away per game
        res = chi_squared_gof(
            sim,
            reference_distribution=REFERENCE_RUN_DISTRIBUTION,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert isinstance(res, ChiSquaredResult)
        # Report shape: a sensible dof and a real p-value.
        assert res.dof >= 1
        assert 0.0 <= res.p_value <= 1.0
        assert math.isclose(sum(res.observed), sum(res.expected), rel_tol=1e-6)
        assert res.passed, (
            f"calibrated replay rejected by chi-squared GOF: chi2={res.statistic:.3f} "
            f"dof={res.dof} p={res.p_value:.4f} (expected p > 0.05)"
        )
        assert res.p_value > 0.05

    def test_replay_is_deterministic_for_a_fixed_seed_set(self):
        # The whole simulated distribution is reproducible from the base seed
        # (noise-robust by construction).
        a = simulate_run_distribution(40, base_seed=0)
        b = simulate_run_distribution(40, base_seed=0)
        assert a == b

    def test_self_consistency_against_a_simulated_reference_totals(self):
        # The OTHER reference mode: compare the simulated draw against an
        # independent (disjoint-seed) simulated reference of ACTUAL totals -- the
        # path the real-data seam uses (reference_totals = real run totals).
        sim = simulate_run_distribution(ALWAYS_ON_GAMES, base_seed=0)
        ref_totals = simulate_run_distribution(400, base_seed=500_000)
        res = chi_squared_gof(
            sim,
            reference_totals=ref_totals,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert res.passed, (
            f"self-consistency vs simulated reference rejected: "
            f"chi2={res.statistic:.3f} dof={res.dof} p={res.p_value:.4f}"
        )


# ===========================================================================
# 3. Negative control — a WRONG distribution must be REJECTED (test has power)
# ===========================================================================


class TestNegativeControl:
    def test_wrong_low_scoring_reference_is_rejected(self):
        # A deliberately WRONG reference: a low-scoring Poisson(mean~2.2) run
        # environment. The calibrated ~4.4 R/team/G simulated draw must NOT match
        # it -- p < 0.05 -- proving the chi-squared has power to reject.
        sim = simulate_run_distribution(ALWAYS_ON_GAMES, base_seed=0)
        wrong = np.array(
            [math.exp(-2.2) * 2.2**k / math.factorial(k) for k in range(MAX_BIN + 1)],
            dtype=float,
        )
        wrong[-1] += max(0.0, 1.0 - wrong.sum())  # close the tail mass
        res = chi_squared_gof(
            sim,
            reference_distribution=wrong,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert not res.passed, (
            f"negative control NOT rejected (test lacks power): "
            f"chi2={res.statistic:.3f} dof={res.dof} p={res.p_value:.4f}"
        )
        assert res.p_value < 0.05

    def test_shifted_actual_totals_are_rejected(self):
        # The real-data-shaped negative control: ACTUAL totals shifted +3 runs
        # (a grossly wrong run environment) must be rejected.
        sim = simulate_run_distribution(ALWAYS_ON_GAMES, base_seed=0)
        ref_totals = simulate_run_distribution(300, base_seed=8000)
        wrong_totals = [r + 3 for r in ref_totals]
        res = chi_squared_gof(
            sim,
            reference_totals=wrong_totals,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert res.p_value < 0.05
        assert not res.passed


# ===========================================================================
# 4. chi_squared_gof argument / robustness guards
# ===========================================================================


class TestChiSquaredGuards:
    def test_requires_exactly_one_reference(self):
        sim = [1, 2, 3, 4]
        with pytest.raises(ValueError):
            chi_squared_gof(sim)  # neither reference given
        with pytest.raises(ValueError):
            chi_squared_gof(sim, reference_totals=[1, 2], reference_distribution=[1.0] * 11)

    def test_reference_distribution_wrong_length_raises(self):
        sim = [1, 2, 3]
        with pytest.raises(ValueError):
            chi_squared_gof(sim, reference_distribution=[0.5, 0.5], max_bin=10)

    def test_empty_simulated_raises(self):
        with pytest.raises(ValueError):
            chi_squared_gof([], reference_distribution=REFERENCE_RUN_DISTRIBUTION)


# ===========================================================================
# 5. The real-data seam: replay_and_test over HistoricalGame rows
# ===========================================================================


class TestRealDataSeam:
    @pytest.mark.slow
    @pytest.mark.timeout(300)  # 1,800 games over the synthetic bundle (~55 s locally)
    def test_replay_and_test_over_historical_games(self):
        # Exercise the real-data seam: replay HistoricalGames through the loop and
        # chi-squared the simulated run distribution against a reference of ACTUAL
        # totals.  With real data the reference is the games' true historical run
        # totals and replay_and_test's one-sample GOF is exactly right.
        #
        # SIM-421: in the NO-DB self-consistency mode (reference itself generated
        # from the model) two corrections are required, both surfaced by the
        # run-environment change:
        #   1. ``replay_historical_games`` scores the HOME half of each game, which
        #      runs ~0.3 R below the both-teams mean (the home team skips the
        #      bottom 9th when leading / walk-off truncation) -- so the reference
        #      must use the SAME home-only extraction.
        #   2. a one-sample GOF treats the reference as the KNOWN distribution, so
        #      it must be LARGE/well-estimated; a noisy small reference inflates
        #      chi2 ~2x and spuriously rejects two same-model samples.  Use a
        #      1500-game home-only reference, then GOF a disjoint 300-game replay.
        reference_totals = replay_historical_games(
            [HistoricalGame(runs=0) for _ in range(1500)], base_seed=700_000
        )
        games = [HistoricalGame(runs=0) for _ in range(300)]
        simulated = replay_historical_games(games, base_seed=0)
        res = chi_squared_gof(
            simulated,
            reference_totals=reference_totals,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert isinstance(res, ChiSquaredResult)
        assert res.dof >= 1
        assert res.passed, (
            f"historical replay rejected by chi-squared GOF: "
            f"chi2={res.statistic:.3f} dof={res.dof} p={res.p_value:.4f}"
        )
        assert res.p_value > 0.05

        # The replay_and_test wrapper (the real-data entry point) still runs and
        # returns a sane result; give it realistic per-game actual totals (its
        # small coupled reference is only statistically valid against TRUE totals,
        # so we smoke the path, not its p-value here).
        wrapped_games = [HistoricalGame(runs=int(r)) for r in reference_totals[:300]]
        wrapped = replay_and_test(wrapped_games, base_seed=0, max_bin=MAX_BIN, min_expected=5.0)
        assert isinstance(wrapped, ChiSquaredResult)
        assert 0.0 <= wrapped.p_value <= 1.0

    def test_historical_game_carries_the_matchup_seam(self):
        # The HistoricalGame seam carries the matchup keys a real replay needs.
        hg = HistoricalGame(runs=5, pitcher_id=12345, season=2023, bat_hand="L")
        assert hg.runs == 5
        assert hg.pitcher_id == 12345
        assert hg.season == 2023
        assert hg.bat_hand == "L"
        assert len(hg.away_lineup) == 9 and len(hg.home_lineup) == 9


# ===========================================================================
# 6. Heavy larger-sample replay (slow-marked) — a tighter GOF
# ===========================================================================


class TestLargeSampleReplay:
    @pytest.mark.slow
    def test_large_sample_replay_passes_with_tighter_estimate(self):
        # A larger replay (400 games -> 800 team-games) gives a tighter simulated
        # distribution; it must still pass the GOF against the reference (a
        # stronger statement: the loop's run distribution matches the calibrated
        # league shape even at a large sample where chi-squared is more sensitive).
        sim = simulate_run_distribution(400, base_seed=0)
        res = chi_squared_gof(
            sim,
            reference_distribution=REFERENCE_RUN_DISTRIBUTION,
            max_bin=MAX_BIN,
            min_expected=5.0,
        )
        assert res.passed, (
            f"large-sample replay rejected: chi2={res.statistic:.3f} "
            f"dof={res.dof} p={res.p_value:.4f}"
        )
        assert res.p_value > 0.05
