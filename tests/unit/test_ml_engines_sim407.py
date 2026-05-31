"""
test_ml_engines_sim407.py
=========================
SIM-407 — validate prop PMFs + fit the win-probability reliability curve over
(synthetic, deterministic) outcomes.

Covers ``simulation.prop_validation``:

  1. **Binary calibration metrics** (win prob / over-under are binary events):
     a calibrated forecaster scores near-zero ECE and a near-diagonal reliability
     curve; an over-confident one scores high ECE. Brier/log-loss reward accuracy;
     input validation + empty-input handling.

  2. **The win-prob reliability fit** (the SIM-406 → 407 handoff): the fitted
     ``[[pred, obs], ...]`` curve, written onto a ``CalibrationReport``, makes
     ``CalibrationMap.from_report`` a NON-identity monotone map that corrects an
     over-confident simulator toward the truth.

  3. **Prop-PMF over/under validation**: a biased PMF (systematically forecasts the
     over too high) is detected (mean_pred_over ≫ observed); a matched PMF is not.
     The integer-line push convention is respected.

  4. **PMF goodness-of-fit**: calibrated PMFs give a mid-PIT mean ≈ 0.5 and central
     coverage ≈ nominal; a biased PMF shifts the PIT mean off 0.5.

  5. **Report JSON round-trip.**

All synthetic — no DB, no engines, no FAISS. A fixed RNG seed pins the numbers.
"""

from __future__ import annotations

import numpy as np

from simulation.prop_distributions import PropDistribution
from simulation.prop_validation import (
    PropValidationReport,
    binary_brier,
    binary_ece,
    binary_log_loss,
    binary_reliability_curve,
    build_validation_report,
    fit_reliability_curve,
    pit_values,
    pmf_coverage,
    reliability_curve_for_calibration_report,
    validate_prop_over_under,
    write_reliability_curve_to_calibration_report,
)
from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap


# ---------------------------------------------------------------------------
# Synthetic generators
# ---------------------------------------------------------------------------


def _calibrated_winprob(n=4000, seed=407):
    """Calibrated by construction: outcome_i ~ Bernoulli(p_i), p_i ~ U(0,1)."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.0, 1.0, size=n)
    y = (rng.uniform(0.0, 1.0, size=n) < p).astype(np.int64)
    return p, y


def _overconfident_winprob(n=4000, seed=407):
    """The forecaster pushes probabilities toward 0/1, but reality is milder:
    predicted = squash(true toward extremes), outcome ~ Bernoulli(true)."""
    rng = np.random.default_rng(seed)
    true = rng.uniform(0.2, 0.8, size=n)
    y = (rng.uniform(0.0, 1.0, size=n) < true).astype(np.int64)
    # Over-confident prediction: stretch away from 0.5.
    pred = np.clip(0.5 + (true - 0.5) * 2.2, 0.0, 1.0)
    return pred, y


def _pmf_from_poisson(lam, n_iter=400, seed=0):
    """A PropDistribution built from n_iter Poisson(lam) samples (a stand-in for a
    sim's per-iteration prop counts)."""
    rng = np.random.default_rng(seed)
    samples = rng.poisson(lam, size=n_iter).tolist()
    return PropDistribution.from_samples(player_id=1, prop="K", samples=samples)


# ===========================================================================
# 1. Binary calibration metrics
# ===========================================================================


class TestBinaryMetrics:
    def test_calibrated_has_low_ece(self):
        p, y = _calibrated_winprob()
        assert binary_ece(p, y, n_bins=10) < 0.05

    def test_overconfident_has_high_ece(self):
        p, y = _overconfident_winprob()
        assert binary_ece(p, y, n_bins=10) > 0.08

    def test_reliability_curve_is_near_diagonal_when_calibrated(self):
        p, y = _calibrated_winprob()
        curve = binary_reliability_curve(p, y, n_bins=10)
        assert len(curve) >= 8  # most bins populated
        for pt in curve:
            assert abs(pt["mean_pred"] - pt["observed"]) < 0.08
            # binary reliability bins the POSITIVE-class prob, not argmax confidence
            assert 0.0 <= pt["mean_pred"] <= 1.0

    def test_brier_rewards_accuracy(self):
        # A perfect forecast beats a constant 0.5 coin-flip.
        y = np.array([0, 1, 0, 1, 1, 0])
        perfect = y.astype(float)
        coin = np.full(6, 0.5)
        assert binary_brier(perfect, y) < binary_brier(coin, y)
        assert binary_brier(perfect, y) == 0.0

    def test_log_loss_finite_on_confident_wrong(self):
        # p=1.0 on a 0 outcome must be large-but-finite (eps clip), not inf.
        ll = binary_log_loss([1.0], [0])
        assert np.isfinite(ll)
        assert ll > 10.0

    def test_length_mismatch_raises(self):
        try:
            binary_ece([0.5, 0.6], [1])
        except ValueError:
            return
        raise AssertionError("expected ValueError on length mismatch")

    def test_non_binary_outcome_raises(self):
        try:
            binary_brier([0.5], [2])
        except ValueError:
            return
        raise AssertionError("expected ValueError on non-binary outcome")

    def test_empty_is_nan(self):
        assert np.isnan(binary_ece([], []))
        assert np.isnan(binary_brier([], []))
        assert np.isnan(binary_log_loss([], []))


# ===========================================================================
# 2. The win-prob reliability fit (SIM-406 -> 407 handoff)
# ===========================================================================


class TestFitReliabilityCurve:
    def test_calibrated_curve_is_near_diagonal(self):
        p, y = _calibrated_winprob()
        curve = fit_reliability_curve(p, y, n_bins=10)
        assert curve  # non-empty
        for pred, obs in curve:
            assert abs(pred - obs) < 0.1

    def test_endpoints_anchored(self):
        p, y = _calibrated_winprob()
        curve = fit_reliability_curve(p, y, n_bins=10, anchor_endpoints=True)
        assert curve[0][0] == 0.0
        assert curve[-1][0] == 1.0

    def test_empty_or_too_small_returns_empty(self):
        assert fit_reliability_curve([], []) == []
        # min_bin_count drops sparse bins; a single sample with min 5 -> nothing.
        assert fit_reliability_curve([0.5], [1], min_bin_count=5) == []

    def test_fitted_curve_makes_calibration_map_nonidentity_and_corrective(self):
        # THE SIM-406 -> 407 HANDOFF: fit a curve on an OVER-confident simulator,
        # write it onto a CalibrationReport, and confirm CalibrationMap.from_report
        # is (a) non-identity and (b) pulls an extreme prediction toward the centre.
        from similarity.similarity_calibration import CalibrationReport

        p, y = _overconfident_winprob()
        curve = fit_reliability_curve(p, y, n_bins=10)
        assert len(curve) >= 3

        report = CalibrationReport(reliability_curve=curve)
        cmap = CalibrationMap.from_report(report)
        assert cmap is not IDENTITY_CALIBRATION  # a real fitted map

        # An over-confident 0.9 forecast should be corrected DOWN (observed < 0.9),
        # i.e. the map shrinks the extreme toward the truth.
        corrected = cmap.apply(0.9)
        assert corrected < 0.9
        # Monotone: a higher input never maps below a lower input.
        assert cmap.apply(0.8) <= cmap.apply(0.9)

    def test_report_accessor_returns_curve_shape(self):
        p, y = _calibrated_winprob()
        rep = PropValidationReport(winprob_reliability_curve=fit_reliability_curve(p, y))
        out = reliability_curve_for_calibration_report(rep)
        assert isinstance(out, list)
        assert all(len(pt) == 2 for pt in out)


# ===========================================================================
# 3. Prop-PMF over/under validation
# ===========================================================================


class TestPropOverUnder:
    def test_matched_pmf_is_well_calibrated(self):
        # Build PMFs from Poisson(4.5) and draw actuals from the SAME law -> the
        # PMF's p_over should track the realized over rate (low ECE).
        rng = np.random.default_rng(11)
        pairs = []
        for i in range(800):
            dist = _pmf_from_poisson(4.5, n_iter=300, seed=1000 + i)
            actual = int(rng.poisson(4.5))
            pairs.append((dist, actual))
        result = validate_prop_over_under(pairs, line=4.5, n_bins=8)
        assert result.n == 800
        assert result.prop == "K"
        # Predicted over-rate should be close to the realized over-rate.
        assert abs(result.mean_pred_over - result.observed_over_rate) < 0.06
        assert result.ece < 0.12

    def test_biased_pmf_is_detected(self):
        # The sim's PMF is centred at 6 (forecasts lots of overs at 4.5) but the
        # actuals come from Poisson(3) -> the over is forecast far too high.
        rng = np.random.default_rng(22)
        pairs = []
        for i in range(600):
            dist = _pmf_from_poisson(6.0, n_iter=300, seed=2000 + i)
            actual = int(rng.poisson(3.0))
            pairs.append((dist, actual))
        result = validate_prop_over_under(pairs, line=4.5, n_bins=8)
        # The sim forecasts the over much more often than it happens.
        assert result.mean_pred_over - result.observed_over_rate > 0.2
        assert result.ece > 0.15

    def test_integer_line_push_is_not_over(self):
        # On an INTEGER line, an actual exactly ON the line is NOT an over win.
        # Build a degenerate PMF that always lands on 5; actual is exactly 5.
        dist = PropDistribution.from_samples(1, "K", [5] * 50)
        result = validate_prop_over_under([(dist, 5)], line=5.0)
        # actual (5) is not > 5 -> observed over = 0.
        assert result.observed_over_rate == 0.0

    def test_empty_pairs_is_n_zero_nan(self):
        result = validate_prop_over_under([], line=4.5)
        assert result.n == 0
        assert np.isnan(result.ece)
        assert np.isnan(result.brier)


# ===========================================================================
# 4. PMF goodness-of-fit (PIT / coverage)
# ===========================================================================


class TestPitCoverage:
    def test_calibrated_pmfs_have_pit_mean_near_half(self):
        rng = np.random.default_rng(33)
        pairs = []
        for i in range(1500):
            dist = _pmf_from_poisson(5.0, n_iter=400, seed=3000 + i)
            actual = int(rng.poisson(5.0))
            pairs.append((dist, actual))
        pit = pit_values(pairs)
        assert abs(float(pit.mean()) - 0.5) < 0.05

    def test_calibrated_pmfs_cover_at_nominal_rate(self):
        rng = np.random.default_rng(44)
        pairs = []
        for i in range(1500):
            dist = _pmf_from_poisson(5.0, n_iter=400, seed=4000 + i)
            actual = int(rng.poisson(5.0))
            pairs.append((dist, actual))
        cov = pmf_coverage(pairs, central=0.80)
        # Within ~6 points of nominal 0.80 (discreteness + sampling noise).
        assert abs(cov["coverage"] - 0.80) < 0.06
        assert cov["n"] == 1500

    def test_biased_pmf_shifts_pit_mean(self):
        # Actuals systematically BELOW the PMF centre -> PIT mean well under 0.5.
        rng = np.random.default_rng(55)
        pairs = []
        for i in range(800):
            dist = _pmf_from_poisson(7.0, n_iter=300, seed=5000 + i)
            actual = int(rng.poisson(3.0))
            pairs.append((dist, actual))
        pit = pit_values(pairs)
        assert float(pit.mean()) < 0.35

    def test_empty_coverage_is_nan(self):
        cov = pmf_coverage([], central=0.80)
        assert np.isnan(cov["coverage"])
        assert cov["n"] == 0


# ===========================================================================
# 5. Report persistence
# ===========================================================================


class TestReportPersistence:
    def test_round_trip(self):
        p, y = _calibrated_winprob()
        rep = PropValidationReport(
            winprob_n=len(p),
            winprob_ece=binary_ece(p, y),
            winprob_brier=binary_brier(p, y),
            winprob_log_loss=binary_log_loss(p, y),
            winprob_reliability_curve=fit_reliability_curve(p, y),
            prop_calibrations=[{"prop": "K", "line": 4.5, "ece": 0.03}],
            pmf_coverage={"K": {"nominal": 0.8, "coverage": 0.79, "pit_mean": 0.5, "n": 100}},
            n_games=len(p),
            seasons_used=[2023, 2024],
        )
        rep2 = PropValidationReport.from_json(rep.to_json())
        assert rep2.winprob_n == rep.winprob_n
        assert rep2.seasons_used == [2023, 2024]
        assert rep2.prop_calibrations[0]["prop"] == "K"
        assert rep2.winprob_reliability_curve == rep.winprob_reliability_curve

    def test_unknown_keys_ignored(self):
        d = PropValidationReport(winprob_n=5).to_dict()
        d["a_future_field"] = 99
        rep = PropValidationReport.from_dict(d)
        assert rep.winprob_n == 5


# ===========================================================================
# 6. build_validation_report aggregator + curve-write-back to CalibrationReport
# ===========================================================================


class TestBuildAndWriteBack:
    def test_build_report_aggregates_winprob_and_props(self):
        p, y = _calibrated_winprob(n=2000)
        winprob_pairs = list(zip(p.tolist(), y.tolist(), strict=True))
        rng = np.random.default_rng(77)
        k_pairs = [
            (_pmf_from_poisson(4.5, n_iter=200, seed=9000 + i), int(rng.poisson(4.5)))
            for i in range(300)
        ]
        rep = build_validation_report(
            winprob_pairs,
            {("K", 4.5): k_pairs},
            n_games=2000,
            seasons_used=[2023, 2024],
        )
        assert rep.winprob_n == 2000
        assert rep.winprob_ece < 0.06
        assert rep.winprob_reliability_curve  # fitted
        assert len(rep.prop_calibrations) == 1
        assert rep.prop_calibrations[0]["prop"] == "K"
        assert "K" in rep.pmf_coverage
        assert rep.seasons_used == [2023, 2024]

    def test_build_report_empty_inputs(self):
        rep = build_validation_report([], {})
        assert rep.winprob_n == 0
        assert np.isnan(rep.winprob_ece)
        assert rep.winprob_reliability_curve == []
        assert rep.prop_calibrations == []

    def test_write_curve_back_into_calibration_report(self, tmp_path):
        # End-to-end SIM-406 <-> 407 loop: a fitted curve is written into an
        # on-disk CalibrationReport, and reloading it yields a non-identity map.
        from similarity.similarity_calibration import CalibrationReport

        # Seed a calibration.json with NO reliability curve (the SIM-406 output).
        calib_path = tmp_path / "calibration.json"
        calib_path.write_text(CalibrationReport(sigma_discipline=0.9).to_json(), encoding="utf-8")

        p, y = _overconfident_winprob()
        rep = PropValidationReport(winprob_reliability_curve=fit_reliability_curve(p, y))
        ok = write_reliability_curve_to_calibration_report(rep, str(calib_path))
        assert ok is True

        reloaded = CalibrationReport.from_json(calib_path.read_text(encoding="utf-8"))
        assert reloaded.reliability_curve  # now populated
        assert reloaded.sigma_discipline == 0.9  # untouched
        cmap = CalibrationMap.from_report(reloaded)
        assert cmap is not IDENTITY_CALIBRATION

    def test_write_curve_back_empty_curve_is_noop(self, tmp_path):
        from similarity.similarity_calibration import CalibrationReport

        calib_path = tmp_path / "calibration.json"
        calib_path.write_text(CalibrationReport().to_json(), encoding="utf-8")
        rep = PropValidationReport(winprob_reliability_curve=[])
        assert write_reliability_curve_to_calibration_report(rep, str(calib_path)) is False

    def test_write_curve_back_missing_file_is_false(self, tmp_path):
        rep = PropValidationReport(winprob_reliability_curve=[[0.0, 0.0], [1.0, 1.0]])
        assert (
            write_reliability_curve_to_calibration_report(rep, str(tmp_path / "nope.json")) is False
        )
