"""Tests of how a fitted ``CalibrationReport`` reaches the engines.

``make calibrate`` fits the report; the app applies it to every engine at boot.
The pitcher engine's own apply is tested in ``test_pitcher_engine_calibration.py``.
These tests cover:

  1. **apply_calibration on every RBF engine** (an RBF is a Gaussian kernel,
     score = exp(-distance² / 2σ²)). The batter, fielder, baserunner, catcher,
     baserunner-steal, pitcher-steal and manager engines each rebuild their
     sub-score kernels from the report. A 0.0 or None field keeps the current
     value (the module default on a new engine). A reliability-weight field,
     where the report carries one, replaces the kernel's weights.

  2. **The seven RBF engines expose the seam** (``apply_calibration``). The
     three distance engines (situation, pitch-to-pitch, batted ball) have no
     sigma and no seam.

  3. **apply_calibration_to_engines (api.state)** applies the report to every
     engine with the seam, skips an engine without it, survives an engine that
     raises, and does nothing with a None report.

  4. **The report's sigma fields round-trip** through its JSON form.

  5. **The sigma fit's 0.0 sentinel** on a matrix with no spread.

Every test builds an engine through the real ``__init__``, which builds the
kernels from the module constants and opens no database (only ``build()``
reads one). No DuckDB file, Redis or FAISS is needed.
"""

from __future__ import annotations

import numpy as np

from similarity.calibration.report import CalibrationReport
from similarity.engines.baserunner_similarity import BaserunnerSimilarityEngine
from similarity.engines.baserunner_steal_similarity import BaserunnerStealSimilarityEngine
from similarity.engines.batter_similarity import BatterSimilarityEngine
from similarity.engines.catcher_similarity import CatcherSimilarityEngine
from similarity.engines.fielder_similarity import FielderSimilarityEngine
from similarity.engines.manager_similarity import ManagerSimilarityEngine
from similarity.engines.pitcher_steal_similarity import PitcherStealSimilarityEngine


def _gamma(sigma: float) -> float:
    return 1.0 / (2.0 * sigma**2)


# ===========================================================================
# 1. batter
# ===========================================================================


class TestBatterApplyCalibration:
    def test_overrides_all_four_sigmas(self):
        eng = BatterSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_discipline=0.5,
            sigma_batted_ball=0.6,
            sigma_platoon=0.7,
            sigma_power=0.8,
        )
        eng.apply_calibration(report)
        assert eng._disc_rbf.sigma == 0.5
        assert eng._bb_rbf.sigma == 0.6
        assert eng._platoon_rbf.sigma == 0.7
        assert eng._power_rbf.sigma == 0.8
        # The kernel derives gamma from the new sigma.
        assert eng._disc_rbf.gamma == _gamma(0.5)

    def test_zero_fields_keep_module_defaults(self):
        eng = BatterSimilarityEngine(duckdb_path=":memory:")
        before = (
            eng._disc_rbf.sigma,
            eng._bb_rbf.sigma,
            eng._platoon_rbf.sigma,
            eng._power_rbf.sigma,
        )
        eng.apply_calibration(CalibrationReport())  # every sigma is 0.0
        after = (
            eng._disc_rbf.sigma,
            eng._bb_rbf.sigma,
            eng._platoon_rbf.sigma,
            eng._power_rbf.sigma,
        )
        assert before == after

    def test_reliability_weights_override_when_present(self):
        eng = BatterSimilarityEngine(duckdb_path=":memory:")
        n = len(eng._disc_rbf.weights)
        custom = np.arange(1, n + 1, dtype=np.float64)
        report = CalibrationReport(sigma_discipline=0.9, reliability_weights_discipline=custom)
        eng.apply_calibration(report)
        # The kernel normalizes the weights to sum to 1.
        np.testing.assert_allclose(eng._disc_rbf.weights, custom / custom.sum())

    def test_sigma_fallback_chains_off_live_instance_not_module_constant(self):
        # Apply a report with a sigma, THEN an empty one. The empty report's
        # 0.0 must keep the sigma applied before, so the fallback reads the
        # live kernel's sigma, not the module constant.
        eng = BatterSimilarityEngine(duckdb_path=":memory:")
        eng.apply_calibration(CalibrationReport(sigma_discipline=0.5))
        assert eng._disc_rbf.sigma == 0.5
        eng.apply_calibration(CalibrationReport())  # all sigmas 0.0
        assert eng._disc_rbf.sigma == 0.5  # kept the live 0.5, not reset to default


# ===========================================================================
# 2. fielder (nine kernels, one of them the double-play and pivot weights joined)
# ===========================================================================


class TestFielderApplyCalibration:
    def test_overrides_if_and_of_sigmas(self):
        eng = FielderSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_if_range=0.5,
            sigma_if_dp=0.4,
            sigma_if_errors=0.6,
            sigma_if_specialty=0.7,
            sigma_of_range=0.55,
            sigma_of_arm=0.65,
            sigma_of_stars=0.45,
            sigma_of_errors=0.75,
        )
        eng.apply_calibration(report)
        assert eng._if_range_rbf.sigma == 0.5
        # Both double-play kernels share sigma_if_dp.
        assert eng._if_dp_rbf.sigma == 0.4
        assert eng._if_dp_rbf_corner.sigma == 0.4
        assert eng._if_error_rbf.sigma == 0.6
        assert eng._if_specialty_rbf.sigma == 0.7
        assert eng._of_range_rbf.sigma == 0.55
        assert eng._of_arm_rbf.sigma == 0.65
        assert eng._of_star_rbf.sigma == 0.45
        assert eng._of_error_rbf.sigma == 0.75

    def test_dp_composite_concatenates_dp_and_pivot_weights(self):
        from similarity.engines.fielder_similarity import IF_DP_FEATURES, IF_PIVOT_FEATURES

        eng = FielderSimilarityEngine(duckdb_path=":memory:")
        # Arrays of the real lengths: IF_DP_FEATURES (3) and IF_PIVOT_FEATURES (1).
        dp = np.array([1.0, 2.0, 3.0])
        pivot = np.array([4.0])
        assert len(dp) == len(IF_DP_FEATURES)
        assert len(pivot) == len(IF_PIVOT_FEATURES)
        report = CalibrationReport(
            sigma_if_dp=0.5,
            reliability_weights_if_dp=dp,
            reliability_weights_if_pivot=pivot,
        )
        eng.apply_calibration(report)
        # The middle-infield kernel joins DP and pivot (4); the corner kernel takes DP (3).
        combo = np.array([1.0, 2.0, 3.0, 4.0])
        np.testing.assert_allclose(eng._if_dp_rbf.weights, combo / combo.sum())
        np.testing.assert_allclose(eng._if_dp_rbf_corner.weights, dp / dp.sum())

    def test_dp_one_array_present_still_feeds_fitted_dp_to_both_scorers(self):
        # A partial report with only reliability_weights_if_dp (no pivot) must
        # feed the fitted DP weights to BOTH the middle (_if_dp_rbf) and the
        # corner (_if_dp_rbf_corner) kernels. Each array falls back on its own;
        # the missing pivot never reverts the whole joined vector.
        from similarity.engines.fielder_similarity import IF_DP_FEATURES, IF_PIVOT_FEATURES

        eng = FielderSimilarityEngine(duckdb_path=":memory:")
        dp = np.array([5.0, 6.0, 7.0])
        report = CalibrationReport(sigma_if_dp=0.5, reliability_weights_if_dp=dp)
        eng.apply_calibration(report)
        # The corner kernel takes the fitted DP weights.
        np.testing.assert_allclose(eng._if_dp_rbf_corner.weights, dp / dp.sum())
        # The middle kernel joins the fitted DP and the module-default pivot.
        pivot_default = np.array([w for _, w in IF_PIVOT_FEATURES])
        combo = np.concatenate([dp, pivot_default])
        np.testing.assert_allclose(eng._if_dp_rbf.weights, combo / combo.sum())
        # The middle kernel's DP part is the fit, not the IF_DP_FEATURES default.
        dp_default = np.array([w for _, w in IF_DP_FEATURES])
        assert not np.allclose(
            eng._if_dp_rbf.weights,
            np.concatenate([dp_default, pivot_default])
            / np.concatenate([dp_default, pivot_default]).sum(),
        )

    def test_reliability_weights_fallback_keeps_module_default(self):
        # A report with a sigma but NO reliability-weight field leaves the
        # kernel's weights at the normalized module default (the None branch of _wts).
        from similarity.engines.fielder_similarity import OF_ARM_FEATURES

        eng = FielderSimilarityEngine(duckdb_path=":memory:")
        eng.apply_calibration(CalibrationReport(sigma_of_arm=0.6))
        default = np.array([w for _, w in OF_ARM_FEATURES])
        np.testing.assert_allclose(eng._of_arm_rbf.weights, default / default.sum())

    def test_zero_fields_keep_defaults(self):
        eng = FielderSimilarityEngine(duckdb_path=":memory:")
        before = eng._if_range_rbf.sigma, eng._of_star_rbf.sigma
        eng.apply_calibration(CalibrationReport())
        assert (eng._if_range_rbf.sigma, eng._of_star_rbf.sigma) == before


# ===========================================================================
# 3. baserunner
# ===========================================================================


class TestBaserunnerApplyCalibration:
    def test_overrides_three_sigmas(self):
        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_baserunner_speed=0.4,
            sigma_baserunner_aggression=0.5,
            sigma_baserunner_success=0.6,
        )
        eng.apply_calibration(report)
        assert eng._speed_rbf.sigma == 0.4
        assert eng._agg_rbf.sigma == 0.5
        assert eng._success_rbf.sigma == 0.6

    def test_zero_fields_keep_defaults(self):
        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        before = eng._speed_rbf.sigma, eng._agg_rbf.sigma, eng._success_rbf.sigma
        eng.apply_calibration(CalibrationReport())
        assert (eng._speed_rbf.sigma, eng._agg_rbf.sigma, eng._success_rbf.sigma) == before

    def test_reliability_weights_override_when_present(self):
        from similarity.engines.baserunner_similarity import AGGRESSION_FEATURES

        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        n = len(AGGRESSION_FEATURES)
        custom = np.arange(1, n + 1, dtype=np.float64)
        eng.apply_calibration(
            CalibrationReport(
                sigma_baserunner_aggression=0.5,
                reliability_weights_baserunner_aggression=custom,
            )
        )
        np.testing.assert_allclose(eng._agg_rbf.weights, custom / custom.sum())


# ===========================================================================
# 4. catcher, baserunner steal, pitcher steal, manager
# ===========================================================================


class TestNewerEngineApplyCalibration:
    def test_catcher(self):
        eng = CatcherSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_catcher_framing=0.5,
            sigma_catcher_blocking=0.6,
            sigma_catcher_throwing=0.7,
            sigma_catcher_deterrence=0.8,
        )
        eng.apply_calibration(report)
        assert eng._framing_rbf.sigma == 0.5
        assert eng._blocking_rbf.sigma == 0.6
        assert eng._throwing_rbf.sigma == 0.7
        assert eng._deterrence_rbf.sigma == 0.8

    def test_catcher_zero_keeps_defaults(self):
        eng = CatcherSimilarityEngine(duckdb_path=":memory:")
        before = eng._framing_rbf.sigma
        eng.apply_calibration(CalibrationReport())
        assert eng._framing_rbf.sigma == before

    def test_baserunner_steal(self):
        eng = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_baserunner_steal_tendency=0.9,
            sigma_baserunner_steal_success=1.1,
        )
        eng.apply_calibration(report)
        assert eng._tend_rbf.sigma == 0.9
        assert eng._succ_rbf.sigma == 1.1

    def test_baserunner_steal_zero_keeps_defaults(self):
        eng = BaserunnerStealSimilarityEngine(duckdb_path=":memory:")
        before = eng._tend_rbf.sigma, eng._succ_rbf.sigma
        eng.apply_calibration(CalibrationReport())
        assert (eng._tend_rbf.sigma, eng._succ_rbf.sigma) == before

    def test_pitcher_steal(self):
        eng = PitcherStealSimilarityEngine(duckdb_path=":memory:")
        before = eng._out_rbf.sigma
        eng.apply_calibration(CalibrationReport())  # 0.0 keeps the default
        assert eng._out_rbf.sigma == before
        eng.apply_calibration(CalibrationReport(sigma_pitcher_steal_outcome=0.85))
        assert eng._out_rbf.sigma == 0.85

    def test_manager(self):
        eng = ManagerSimilarityEngine(duckdb_path=":memory:")
        report = CalibrationReport(
            sigma_manager_usage=0.95,
            sigma_manager_aggression=1.05,
            sigma_manager_platoon=1.15,
        )
        eng.apply_calibration(report)
        assert eng._usage_rbf.sigma == 0.95
        assert eng._agg_rbf.sigma == 1.05
        assert eng._plat_rbf.sigma == 1.15

    def test_manager_zero_keeps_defaults(self):
        eng = ManagerSimilarityEngine(duckdb_path=":memory:")
        before = eng._usage_rbf.sigma, eng._agg_rbf.sigma, eng._plat_rbf.sigma
        eng.apply_calibration(CalibrationReport())
        assert (eng._usage_rbf.sigma, eng._agg_rbf.sigma, eng._plat_rbf.sigma) == before


# ===========================================================================
# 5. the seam across the engines
# ===========================================================================


class TestSeamPresence:
    def test_eight_similarity_engines_expose_apply_calibration(self):
        # Eight engines score in [0, 1] and expose apply_calibration: the
        # pitcher engine and the seven RBF engines. This test builds the seven;
        # test_pitcher_engine_calibration.py covers the pitcher. The three
        # distance engines (situation, pitch-to-pitch, batted ball) have no
        # sigma and no seam. They need FAISS or a KD-tree, so the test does
        # not build them; TestApplyCalibrationToEngines covers the skip of an
        # engine without the seam.
        engines = [
            BatterSimilarityEngine(duckdb_path=":memory:"),
            FielderSimilarityEngine(duckdb_path=":memory:"),
            BaserunnerSimilarityEngine(duckdb_path=":memory:"),
            CatcherSimilarityEngine(duckdb_path=":memory:"),
            BaserunnerStealSimilarityEngine(duckdb_path=":memory:"),
            PitcherStealSimilarityEngine(duckdb_path=":memory:"),
            ManagerSimilarityEngine(duckdb_path=":memory:"),
        ]
        for eng in engines:
            assert callable(getattr(eng, "apply_calibration", None))


# ===========================================================================
# 6. api.state.apply_calibration_to_engines
# ===========================================================================


class _Supports:
    def __init__(self) -> None:
        self.applied: object = None

    def apply_calibration(self, report: object) -> None:
        self.applied = report


class _NoSeam:
    pass


class _Explodes:
    def apply_calibration(self, report: object) -> None:
        raise RuntimeError("simulated bad engine")


class TestApplyCalibrationToEngines:
    def test_applies_skips_and_is_resilient(self):
        from api.state import apply_calibration_to_engines

        report = object()
        supports, noseam, explodes = _Supports(), _NoSeam(), _Explodes()
        applied = apply_calibration_to_engines({"a": supports, "b": noseam, "c": explodes}, report)
        assert "a" in applied  # the seam ran
        assert "b" not in applied  # no seam: skipped
        assert "c" not in applied  # raised: caught and skipped, not fatal
        assert supports.applied is report

    def test_none_report_is_noop(self):
        from api.state import apply_calibration_to_engines

        assert apply_calibration_to_engines({"a": _Supports()}, None) == []

    def test_empty_engines_dict(self):
        from api.state import apply_calibration_to_engines

        assert apply_calibration_to_engines({}, object()) == []
        assert apply_calibration_to_engines(None, object()) == []


# ===========================================================================
# 7. the report's sigma fields round-trip
# ===========================================================================


class TestNewFieldsPersistence:
    def test_round_trip_lossless(self):
        r = CalibrationReport(
            sigma_catcher_framing=0.91,
            sigma_catcher_blocking=0.82,
            sigma_catcher_throwing=0.73,
            sigma_catcher_deterrence=0.64,
            sigma_baserunner_steal_tendency=1.05,
            sigma_baserunner_steal_success=1.02,
            sigma_pitcher_steal_outcome=1.10,
            sigma_manager_usage=1.00,
            sigma_manager_aggression=1.05,
            sigma_manager_platoon=1.02,
        )
        r2 = CalibrationReport.from_json(r.to_json())
        assert r.equals(r2)
        assert r2.sigma_catcher_framing == 0.91
        assert r2.sigma_baserunner_steal_tendency == 1.05
        assert r2.sigma_pitcher_steal_outcome == 1.10
        assert r2.sigma_manager_platoon == 1.02

    def test_defaults_are_zero(self):
        r = CalibrationReport()
        assert r.sigma_catcher_framing == 0.0
        assert r.sigma_manager_usage == 0.0
        assert r.sigma_pitcher_steal_outcome == 0.0


# ===========================================================================
# 8. the sigma fit's 0.0 sentinel on a matrix with no spread
# ===========================================================================


class TestFitSigmaSentinel:
    """The calibrator's sigma fit (``_fit_sigma``, in
    ``similarity/calibration/base.py``) returns the 0.0 keep-the-default
    sentinel, not ``calibrate_sigma``'s 1.0, on a matrix with no spread.

    An example is a database whose manager usage columns are all NULL. A
    literal 1.0 would persist in the report and override the engine's tuned
    sigma.
    """

    def test_zero_variance_matrix_returns_zero_sentinel(self):
        from similarity.similarity_calibration import SimilarityCalibrator

        # An all-zero z-scored matrix, what an all-NULL column produces. The
        # width 7 is the manager usage sub-score's feature count.
        zmat = np.zeros((50, 7), dtype=np.float64)
        assert SimilarityCalibrator._fit_sigma(zmat, 0.50) == 0.0

    def test_empty_matrix_returns_zero_sentinel(self):
        from similarity.similarity_calibration import SimilarityCalibrator

        assert SimilarityCalibrator._fit_sigma(np.zeros((0, 3)), 0.50) == 0.0

    def test_matrix_with_variance_calibrates_to_positive_sigma(self):
        from similarity.similarity_calibration import SimilarityCalibrator

        rng = np.random.default_rng(406)
        zmat = rng.normal(0.0, 1.0, size=(500, 4))
        sigma = SimilarityCalibrator._fit_sigma(zmat, 0.50)
        assert sigma > 0.0  # a real fit, not the 0.0 sentinel

    def test_sentinel_makes_engine_keep_default_on_degenerate_usage(self):
        # End to end: an all-zero usage matrix fits 0.0, the report carries
        # 0.0, and the manager engine keeps RBF_SIGMA_USAGE.
        from similarity.calibration.report import CalibrationReport
        from similarity.engines import manager_similarity
        from similarity.engines.manager_similarity import ManagerSimilarityEngine
        from similarity.similarity_calibration import SimilarityCalibrator

        usage_sigma = SimilarityCalibrator._fit_sigma(np.zeros((30, 7)), 0.50)
        assert usage_sigma == 0.0
        eng = ManagerSimilarityEngine(duckdb_path=":memory:")
        eng.apply_calibration(CalibrationReport(sigma_manager_usage=usage_sigma))
        assert eng._usage_rbf.sigma == manager_similarity.RBF_SIGMA_USAGE
