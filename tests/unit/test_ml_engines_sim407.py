"""
test_ml_engines_sim407.py
=========================
SIM-407 — validate prop PMFs + fit the win-probability reliability curve over
(synthetic, deterministic) outcomes, and derive real prop actuals from per-PA
``events``.

Covers ``simulation.prop_validation``:

  1. Binary calibration metrics (win prob / over-under are binary events):
     calibrated → ~0 ECE + near-diagonal reliability; over-confident → high ECE;
     Brier/log-loss reward accuracy; input validation + empty handling.
  2. The win-prob reliability fit (the SIM-406 → 407 handoff): the fitted
     ``[[pred, obs], …]`` curve, written onto a ``CalibrationReport``, makes
     ``CalibrationMap.from_report`` a NON-identity monotone map that corrects an
     over-confident simulator toward the truth.
  3. Prop-PMF over/under validation: a biased PMF is detected; a matched one is
     not; the integer-line push convention is respected.
  4. PMF goodness-of-fit: calibrated PMFs → mid-PIT mean ≈ 0.5 + nominal coverage.
  5. Report JSON round-trip + the aggregator/curve write-back.
  6. Real prop actuals from ``raw.pitches.events`` (the box-score derivation that
     replaced the over-cautious ``--no-props`` gate) + pairing with sim PMFs.
  7. ``scripts/validate_props.py`` contract guards (locks the seam so the
     BatchRunner/per-game-results mistakes can't regress without a live DB).

All synthetic — no DB, no engines, no FAISS. A fixed RNG seed pins the numbers.
"""

from __future__ import annotations

import numpy as np

from simulation.prop_distributions import PropDistribution
from simulation.prop_validation import (
    DEFAULT_PROP_LINES,
    DERIVABLE_BATTER_PROPS,
    DERIVABLE_PITCHER_PROPS,
    PropValidationReport,
    binary_brier,
    binary_ece,
    binary_log_loss,
    binary_reliability_curve,
    build_validation_report,
    fit_reliability_curve,
    pair_props_for_validation,
    pit_values,
    pmf_coverage,
    real_props_from_pa_events,
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
    """Forecaster pushes probabilities toward 0/1, but reality is milder."""
    rng = np.random.default_rng(seed)
    true = rng.uniform(0.2, 0.8, size=n)
    y = (rng.uniform(0.0, 1.0, size=n) < true).astype(np.int64)
    pred = np.clip(0.5 + (true - 0.5) * 2.2, 0.0, 1.0)
    return pred, y


def _pmf_from_poisson(lam, n_iter=400, seed=0):
    """A PropDistribution from n_iter Poisson(lam) samples (sim-prop stand-in)."""
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
        assert len(curve) >= 8
        for pt in curve:
            assert abs(pt["mean_pred"] - pt["observed"]) < 0.08
            assert 0.0 <= pt["mean_pred"] <= 1.0

    def test_brier_rewards_accuracy(self):
        y = np.array([0, 1, 0, 1, 1, 0])
        perfect = y.astype(float)
        coin = np.full(6, 0.5)
        assert binary_brier(perfect, y) < binary_brier(coin, y)
        assert binary_brier(perfect, y) == 0.0

    def test_log_loss_finite_on_confident_wrong(self):
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
        assert curve
        for pred, obs in curve:
            assert abs(pred - obs) < 0.1

    def test_endpoints_anchored(self):
        p, y = _calibrated_winprob()
        curve = fit_reliability_curve(p, y, n_bins=10, anchor_endpoints=True)
        assert curve[0][0] == 0.0
        assert curve[-1][0] == 1.0

    def test_empty_or_too_small_returns_empty(self):
        assert fit_reliability_curve([], []) == []
        assert fit_reliability_curve([0.5], [1], min_bin_count=5) == []

    def test_fitted_curve_makes_calibration_map_nonidentity_and_corrective(self):
        from similarity.similarity_calibration import CalibrationReport

        p, y = _overconfident_winprob()
        curve = fit_reliability_curve(p, y, n_bins=10)
        assert len(curve) >= 3

        report = CalibrationReport(reliability_curve=curve)
        cmap = CalibrationMap.from_report(report)
        assert cmap is not IDENTITY_CALIBRATION

        corrected = cmap.apply(0.9)
        assert corrected < 0.9
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
        rng = np.random.default_rng(11)
        pairs = []
        for i in range(800):
            dist = _pmf_from_poisson(4.5, n_iter=300, seed=1000 + i)
            actual = int(rng.poisson(4.5))
            pairs.append((dist, actual))
        result = validate_prop_over_under(pairs, line=4.5, n_bins=8)
        assert result.n == 800
        assert result.prop == "K"
        assert abs(result.mean_pred_over - result.observed_over_rate) < 0.06
        assert result.ece < 0.12

    def test_biased_pmf_is_detected(self):
        rng = np.random.default_rng(22)
        pairs = []
        for i in range(600):
            dist = _pmf_from_poisson(6.0, n_iter=300, seed=2000 + i)
            actual = int(rng.poisson(3.0))
            pairs.append((dist, actual))
        result = validate_prop_over_under(pairs, line=4.5, n_bins=8)
        assert result.mean_pred_over - result.observed_over_rate > 0.2
        assert result.ece > 0.15

    def test_integer_line_push_is_not_over(self):
        dist = PropDistribution.from_samples(1, "K", [5] * 50)
        result = validate_prop_over_under([(dist, 5)], line=5.0)
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
        assert abs(cov["coverage"] - 0.80) < 0.06
        assert cov["n"] == 1500

    def test_biased_pmf_shifts_pit_mean(self):
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
# 5. Report persistence + aggregator + curve write-back
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
        assert rep.winprob_reliability_curve
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
        from similarity.similarity_calibration import CalibrationReport

        calib_path = tmp_path / "calibration.json"
        calib_path.write_text(CalibrationReport(sigma_discipline=0.9).to_json(), encoding="utf-8")

        p, y = _overconfident_winprob()
        rep = PropValidationReport(winprob_reliability_curve=fit_reliability_curve(p, y))
        ok = write_reliability_curve_to_calibration_report(rep, str(calib_path))
        assert ok is True

        reloaded = CalibrationReport.from_json(calib_path.read_text(encoding="utf-8"))
        assert reloaded.reliability_curve
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


# ===========================================================================
# 6. Real prop outcomes from raw.pitches `events` (the prop ground truth)
# ===========================================================================


class TestRealPropsFromEvents:
    def test_batter_hits_hr_tb(self):
        pa = [
            (10, 1, "single"),
            (10, 1, "double"),
            (10, 2, "triple"),
            (10, 2, "home_run"),
            (10, 3, "strikeout"),
            (10, 3, "field_out"),
        ]
        batter, _pitcher = real_props_from_pa_events(pa)
        assert batter[10] == {"H": 4, "HR": 1, "TB": 1 + 2 + 3 + 4}

    def test_pitcher_k_bb_excludes_intentional_walk(self):
        pa = [
            (1, 99, "strikeout"),
            (2, 99, "strikeout_double_play"),
            (3, 99, "walk"),
            (4, 99, "intent_walk"),  # IBB excluded (sim doesn't model it)
            (5, 99, "single"),
        ]
        _batter, pitcher = real_props_from_pa_events(pa)
        assert pitcher[99] == {"K": 2, "BB": 1}

    def test_case_insensitive_and_blank_tolerant(self):
        pa = [(1, 2, "HOME_RUN"), (1, 2, ""), (1, 2, None), (1, 2, "  walk ")]
        batter, pitcher = real_props_from_pa_events(pa)
        assert batter[1] == {"H": 1, "HR": 1, "TB": 4}
        assert pitcher[2]["BB"] == 1

    def test_unknown_out_events_contribute_zero(self):
        pa = [
            (1, 2, "field_out"),
            (1, 2, "grounded_into_double_play"),
            (1, 2, "hit_by_pitch"),
            (1, 2, "sac_fly"),
        ]
        batter, pitcher = real_props_from_pa_events(pa)
        assert batter[1] == {"H": 0, "HR": 0, "TB": 0}
        assert pitcher[2] == {"K": 0, "BB": 0}

    def test_empty(self):
        batter, pitcher = real_props_from_pa_events([])
        assert batter == {}
        assert pitcher == {}

    def test_default_lines_cover_every_derivable_prop(self):
        for prop in DERIVABLE_BATTER_PROPS + DERIVABLE_PITCHER_PROPS:
            assert prop in DEFAULT_PROP_LINES


class _FakePset:
    """Minimal PropDistributionSet stand-in: ``get(pid, prop) -> PropDistribution``."""

    def __init__(self, mapping):
        self._m = mapping

    def get(self, pid, prop=None):
        return self._m.get((int(pid), prop))


class TestPairPropsForValidation:
    def test_pairs_only_where_pmf_exists(self):
        k_dist = PropDistribution.from_samples(99, "K", [6, 7, 8, 5, 7])
        h_dist = PropDistribution.from_samples(10, "H", [1, 2, 0, 1, 2])
        pset = _FakePset({(99, "K"): k_dist, (10, "H"): h_dist})
        batter_actuals = {10: {"H": 2, "HR": 0, "TB": 3}}
        pitcher_actuals = {99: {"K": 8, "BB": 3}}

        bucket: dict = {}
        added = pair_props_for_validation(pset, batter_actuals, pitcher_actuals, bucket)
        # H (10) and K (99) pair; HR/TB (10) and BB (99) are absent from the pset.
        assert added == 2
        assert ("K", DEFAULT_PROP_LINES["K"]) in bucket
        assert ("H", DEFAULT_PROP_LINES["H"]) in bucket
        (_dist, actual) = bucket[("K", DEFAULT_PROP_LINES["K"])][0]
        assert actual == 8

    def test_accumulates_across_calls(self):
        d = PropDistribution.from_samples(10, "H", [1, 2, 1])
        pset = _FakePset({(10, "H"): d})
        bucket: dict = {}
        pair_props_for_validation(pset, {10: {"H": 1, "HR": 0, "TB": 1}}, {}, bucket)
        pair_props_for_validation(pset, {10: {"H": 2, "HR": 0, "TB": 2}}, {}, bucket)
        assert len(bucket[("H", DEFAULT_PROP_LINES["H"])]) == 2

    def test_end_to_end_derive_then_validate(self):
        pa = [(b, 99, "strikeout") for b in range(7)]  # pitcher 99 fanned 7
        _b, pitcher = real_props_from_pa_events(pa)
        assert pitcher[99]["K"] == 7
        dist = PropDistribution.from_samples(99, "K", [6, 7, 8, 5, 7, 6, 7, 8])
        res = validate_prop_over_under([(dist, pitcher[99]["K"])], line=5.5)
        assert res.n == 1
        assert res.prop == "K"
        assert res.observed_over_rate == 1.0  # 7 > 5.5


# ===========================================================================
# 7. scripts/validate_props.py contract guards
#    (locks the seam so the BatchRunner / per-game-results mistakes — both caught
#    in review — can't regress without a live DB).
# ===========================================================================


class TestValidateScriptContract:
    def test_batchrunner_not_constructed_with_machine_factory(self):
        # The machine factory is a per-GAME concern (carried on GameSpec / passed to
        # record_game_plays), NOT a BatchRunner ctor arg. Lock the signature so the
        # original machine_factory= mistake is caught in unit CI.
        import inspect

        from simulation.batch_runner import BatchRunner

        params = inspect.signature(BatchRunner.__init__).parameters
        assert "cache" in params
        assert "max_workers" in params
        assert "machine_factory" not in params

    def test_record_game_plays_seam_signature(self):
        # The script collects per-iteration results via record_game_plays (since
        # BatchRunner retains only the aggregate summary). Lock that seam's keyword
        # contract so a refactor can't silently break the collector.
        import inspect

        from simulation.play_recorder import record_game_plays

        params = inspect.signature(record_game_plays).parameters
        for kw in ("factory_ref", "seed", "sim_kwargs"):
            assert kw in params

    def test_validate_props_script_imports_and_parses_args(self):
        import importlib.util
        import inspect
        import os

        # tests/unit/test_*.py -> tests/unit -> tests -> repo root (three dirnames).
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script_path = os.path.join(repo_root, "scripts", "validate_props.py")
        spec = importlib.util.spec_from_file_location("validate_props", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        args = mod.parse_args(["--seasons", "2024", "--no-props", "--max-games", "5"])
        assert args.seasons == [2024]
        assert args.no_props is True
        assert args.max_games == 5
        # The per-game collector returns a results LIST (no dead game_pk arg, no
        # BatchRunner): lock its signature.
        sig = inspect.signature(mod._collect_game_results)
        assert "game_pk" not in sig.parameters
        assert list(sig.parameters) == ["state", "n_iter", "base_seed"]
