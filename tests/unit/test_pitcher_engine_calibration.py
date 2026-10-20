"""The pitcher engine's arsenal calibration and its no-arsenal path.

The tests guard three rules and one drift check:

  1. **A pitcher without an arsenal scores on the full [0, 1] scale.** When
     a pitcher (or a pair) has no GMM arsenal (a Gaussian mixture model of
     the pitch mix), the engine rescales the command score by
     ``_TOTAL_WEIGHT / WEIGHT_COMMAND`` (1 / 0.35, about 2.857). A factor of
     1.0 would cap a command-only composite at ``WEIGHT_COMMAND`` (0.35), so
     a command-only pitcher could never compare with a full-arsenal one.

  2. **One arsenal transform.** The engine scores the arsenal by the linear
     form ``exp(-W₂ / ARSENAL_SCALE)`` (W₂: the Wasserstein-2 distance), at
     the calibrated default scale 4.10. ``arsenal_scale_from_gamma`` converts
     the calibrator's squared-form gamma (``exp(-gamma · W₂²)``) into that
     linear scale at the same median, so no second form diverges.

  3. **The calibration report reaches the engine.**
     ``PitcherSimilarityEngine.apply_calibration(report)`` turns
     ``CalibrationReport.arsenal_gamma`` (and the median) into the
     module-level ``ARSENAL_SCALE``, so the engine reads the calibrated value,
     not a literal.

  4. **Drift check.** On a controlled W₂ fixture, the calibrated arsenal
     transform keeps the MEDIAN arsenal similarity near 0.50, and the
     no-arsenal composite sits on the same scale as the full-arsenal one.

These tests build the engine in memory with the ``__new__`` bypass, as the
other pitcher-engine tests do (no live DuckDB).
"""

from __future__ import annotations

import unittest

import numpy as np

import similarity.engines.pitcher_similarity as ps
from similarity.calibration.fitting import calibrate_arsenal_gamma
from similarity.calibration.report import CalibrationReport
from similarity.engines.arsenal_cache import ArsenalCache
from similarity.engines.pitcher_similarity import (
    DEFAULT_ARSENAL_SCALE,
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
    WEIGHT_ARSENAL,
    WEIGHT_COMMAND,
    ArsenalSimilarity,
    FeatureNormalizer,
    GMMComponent,
    GMMModel,
    HandednessPartition,
    PitcherProfile,
    PitcherSimilarityEngine,
    RBFSimilarity,
    arsenal_scale_from_gamma,
)

_TOTAL_WEIGHT = WEIGHT_ARSENAL + WEIGHT_COMMAND


# ---------------------------------------------------------------------------
# Helpers (mirror test_pitcher_similarity.py conventions)
# ---------------------------------------------------------------------------


def _make_component(cid, weight, mean, var_diag=1.0, n_pitches=300) -> GMMComponent:
    return GMMComponent(
        component_id=cid,
        weight=float(weight),
        mean=np.asarray(mean, dtype=np.float64),
        covariance=np.eye(GMM_FEATURE_DIM, dtype=np.float64) * var_diag,
        n_pitches=n_pitches,
    )


def _make_gmm(components) -> GMMModel:
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=np.zeros(GMM_FEATURE_DIM),
        feature_stds=np.ones(GMM_FEATURE_DIM),
        components=components,
    )


def _make_profile(
    pitcher_id,
    season=2025,
    p_throws="R",
    gmm=None,
    command=None,
    sample_pitches=500,
    eb_alpha=1.0,
) -> PitcherProfile:
    return PitcherProfile(
        pitcher_id=pitcher_id,
        season=season,
        p_throws=p_throws,
        sample_pitches=sample_pitches,
        gmm=gmm,
        command_vec=np.asarray(
            command if command is not None else [0.08, 0.22, 0.30, 0.45, 0.28],
            dtype=np.float64,
        ),
        eb_alpha=eb_alpha,
    )


def _single_component_gmm(mean) -> GMMModel:
    return _make_gmm([_make_component(0, 1.0, mean)])


def _new_engine() -> PitcherSimilarityEngine:
    """Construct an engine without touching DuckDB (as the other pitcher tests do)."""
    eng = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    eng._profiles = {}
    eng._normalizer = FeatureNormalizer()
    eng._command_rbf = RBFSimilarity(sigma=1.0)
    eng._arsenal_cache = ArsenalCache()
    eng._arsenal_scale = DEFAULT_ARSENAL_SCALE
    return eng


# ============================================================================
# 1. A pitcher without an arsenal: the arsenal weight moves onto command
# ============================================================================


class TestNoArsenalRedistribution(unittest.TestCase):
    """A GMM-less pitcher's composite must be on the full [0,1] scale."""

    def test_vectorized_path_redistributes_arsenal_weight(self):
        # Query has NO gmm → every pair takes the no-arsenal path.
        query = _make_profile(1, gmm=None, command=[0.08, 0.22, 0.30, 0.45, 0.28])
        cand = _make_profile(
            2,
            gmm=_single_component_gmm([90.0] * GMM_FEATURE_DIM),
            command=[0.09, 0.20, 0.31, 0.44, 0.27],
        )

        norm = FeatureNormalizer()
        norm.fit([query, cand])
        part = HandednessPartition("R")
        part.build([cand], norm)

        results = part.score_all(query, norm, ps.ArsenalCache(), RBFSimilarity(sigma=1.0))
        self.assertEqual(len(results), 1)
        r = results[0]

        # Recompute the expected command score directly.
        cmd_q = norm.normalize_command(query.command_vec)
        cmd_c = norm.normalize_command(cand.command_vec)
        cmd_score = RBFSimilarity(sigma=1.0).score(cmd_q, cmd_c)
        expected = (_TOTAL_WEIGHT / WEIGHT_COMMAND) * cmd_score  # eb_alpha=1.0
        expected = float(np.clip(expected, 0.0, 1.0))

        self.assertAlmostEqual(r.score, expected, places=10)

        # A factor of 1.0 would give WEIGHT_COMMAND * cmd_score.
        buggy = WEIGHT_COMMAND * cmd_score
        self.assertNotAlmostEqual(r.score, buggy, places=6)
        # And the redistributed score must be strictly larger (×2.857 factor).
        self.assertGreater(r.score, buggy)

    def test_score_pair_path_redistributes_arsenal_weight(self):
        eng = _new_engine()
        q = _make_profile(10, gmm=None, command=[0.08, 0.22, 0.30, 0.45, 0.28])
        c = _make_profile(11, gmm=None, command=[0.10, 0.20, 0.32, 0.43, 0.30])
        eng._profiles[(q.pitcher_id, q.season)] = q
        eng._profiles[(c.pitcher_id, c.season)] = c
        eng._normalizer.fit([q, c])

        composite, arsenal_s, command_s = eng._score_pair(q, c)

        self.assertEqual(arsenal_s, 0.0)  # no gmm → no arsenal contribution
        expected = float(np.clip((_TOTAL_WEIGHT / WEIGHT_COMMAND) * command_s, 0.0, 1.0))
        self.assertAlmostEqual(composite, expected, places=10)

        buggy = WEIGHT_COMMAND * command_s
        self.assertGreater(composite, buggy)
        self.assertNotAlmostEqual(composite, buggy, places=6)

    def test_redistribution_factor_is_inverse_command_weight(self):
        # The redistribution multiplier must be 1 / WEIGHT_COMMAND, NOT 1.0.
        factor = _TOTAL_WEIGHT / WEIGHT_COMMAND
        self.assertAlmostEqual(factor, 1.0 / WEIGHT_COMMAND, places=12)
        self.assertGreater(factor, 1.0)  # never the 1.0 no-op

    def test_identical_command_no_arsenal_scores_near_one(self):
        # Two GMM-less pitchers with IDENTICAL command → command_s == 1.0 →
        # redistributed composite must hit the [0,1] ceiling (proving the
        # composite reaches the same range a full-arsenal clone would).
        eng = _new_engine()
        cmd = [0.08, 0.22, 0.30, 0.45, 0.28]
        q = _make_profile(20, gmm=None, command=cmd)
        c = _make_profile(21, gmm=None, command=cmd)
        eng._profiles[(20, 2025)] = q
        eng._profiles[(21, 2025)] = c
        eng._normalizer.fit([q, c])

        composite, _, command_s = eng._score_pair(q, c)
        self.assertAlmostEqual(command_s, 1.0, places=9)
        self.assertAlmostEqual(composite, 1.0, places=9)  # clipped 2.857 → 1.0


# ============================================================================
# 2. One arsenal transform: the squared gamma converts to the linear scale
# ============================================================================


class TestGammaScaleReconciliation(unittest.TestCase):
    def test_default_scale_is_locked_linear_value(self):
        self.assertAlmostEqual(DEFAULT_ARSENAL_SCALE, 4.10, places=10)
        # The module constant starts on the default.
        self.assertAlmostEqual(ps.ARSENAL_SCALE, 4.10, places=10)

    def test_arsenal_score_uses_linear_form(self):
        # ArsenalSimilarity.score must equal exp(-d / ARSENAL_SCALE), the
        # single canonical linear transform — NOT a squared exp(-g*d²) form.
        g_a = _single_component_gmm([0.0] * GMM_FEATURE_DIM)
        g_b = _single_component_gmm([1.0] * GMM_FEATURE_DIM)
        d = ArsenalSimilarity.distance(g_a, g_b)
        self.assertTrue(np.isfinite(d))
        score = ArsenalSimilarity.score(g_a, g_b)
        self.assertAlmostEqual(score, float(np.exp(-d / ps.ARSENAL_SCALE)), places=12)
        # Squared form would give a different number for d != 0/1.
        squared = float(np.exp(-(1.0 / ps.ARSENAL_SCALE) * d * d))
        self.assertNotAlmostEqual(score, squared, places=6)

    def test_gamma_to_scale_roundtrip_pins_same_median(self):
        # calibrate_arsenal_gamma (squared) and arsenal_scale_from_gamma
        # (→ linear) must map the SAME median W₂ to the SAME target score.
        rng = np.random.default_rng(346)
        dists = np.abs(rng.normal(2.84, 1.0, size=5000)) + 0.05
        target = 0.50
        gamma = calibrate_arsenal_gamma(dists, target_median_score=target)
        median = float(np.median(dists))
        scale = arsenal_scale_from_gamma(gamma, median)

        squared_at_median = float(np.exp(-gamma * median**2))
        linear_at_median = float(np.exp(-median / scale))
        self.assertAlmostEqual(squared_at_median, target, places=6)
        self.assertAlmostEqual(linear_at_median, target, places=6)
        self.assertAlmostEqual(squared_at_median, linear_at_median, places=9)

    def test_scale_from_gamma_defaults_on_bad_input(self):
        self.assertEqual(arsenal_scale_from_gamma(0.0, 2.84), DEFAULT_ARSENAL_SCALE)
        self.assertEqual(arsenal_scale_from_gamma(0.1, 0.0), DEFAULT_ARSENAL_SCALE)
        self.assertEqual(arsenal_scale_from_gamma(float("nan"), 2.84), DEFAULT_ARSENAL_SCALE)


# ============================================================================
# 3. CalibrationReport is wired into the engine
# ============================================================================


class TestCalibrationWiring(unittest.TestCase):
    def setUp(self):
        self._saved_scale = ps.ARSENAL_SCALE

    def tearDown(self):
        ps.ARSENAL_SCALE = self._saved_scale

    def test_apply_calibration_overrides_module_constant(self):
        eng = _new_engine()
        median = 3.20
        gamma = calibrate_arsenal_gamma(np.array([median]), target_median_score=0.50)
        report = CalibrationReport(arsenal_gamma=gamma, arsenal_median_w2=median)

        expected_scale = arsenal_scale_from_gamma(gamma, median)
        returned = eng.apply_calibration(report)

        self.assertAlmostEqual(returned, expected_scale, places=12)
        self.assertAlmostEqual(eng.arsenal_scale, expected_scale, places=12)
        # The module-level constant the scoring paths read must change too.
        self.assertAlmostEqual(ps.ARSENAL_SCALE, expected_scale, places=12)
        # And it must differ from the hardcoded default (proving it's wired).
        self.assertNotAlmostEqual(ps.ARSENAL_SCALE, DEFAULT_ARSENAL_SCALE, places=6)

    def test_apply_calibration_changes_engine_scoring(self):
        # After apply_calibration, _score_pair's arsenal sub-score must read
        # the new scale, not the default.
        eng = _new_engine()
        g_a = _single_component_gmm([0.0] * GMM_FEATURE_DIM)
        g_b = _single_component_gmm([2.0] * GMM_FEATURE_DIM)
        d = ArsenalSimilarity.distance(g_a, g_b)

        report = CalibrationReport(arsenal_gamma=0.20, arsenal_median_w2=1.50)
        new_scale = eng.apply_calibration(report)

        expected_arsenal = float(np.exp(-d / new_scale))
        # Recompute via the engine path on a fresh full-arsenal pair.
        qa = _make_profile(30, gmm=g_a, command=[0.0] * 5)
        qb = _make_profile(31, gmm=g_b, command=[0.0] * 5)
        eng._profiles[(30, 2025)] = qa
        eng._profiles[(31, 2025)] = qb
        eng._normalizer.fit([qa, qb])
        _, arsenal_s, _ = eng._score_pair(qa, qb)
        self.assertAlmostEqual(arsenal_s, expected_arsenal, places=9)

    def test_apply_calibration_keeps_default_when_no_gamma(self):
        eng = _new_engine()
        report = CalibrationReport()  # arsenal_gamma stays 0.0
        scale = eng.apply_calibration(report, median_w2=2.84)
        self.assertAlmostEqual(scale, DEFAULT_ARSENAL_SCALE, places=10)
        self.assertAlmostEqual(ps.ARSENAL_SCALE, DEFAULT_ARSENAL_SCALE, places=10)

    def test_apply_calibration_falls_back_to_cache_median(self):
        # If the report omits the median, the engine should pull it from its
        # own W₂ cache.
        eng = _new_engine()
        # Seed the cache with finite distances around 2.84.
        rng = np.random.default_rng(7)
        for i, val in enumerate(np.abs(rng.normal(2.84, 0.5, size=200)) + 0.1):
            eng._arsenal_cache._cache[((i, 0), (i, 1))] = float(val)
        report = CalibrationReport(arsenal_gamma=0.15)  # no median provided
        scale = eng.apply_calibration(report)
        cache_median = float(np.median(eng._arsenal_cache.finite_distances()))
        self.assertAlmostEqual(scale, arsenal_scale_from_gamma(0.15, cache_median), places=9)


# ============================================================================
# 4. Drift check: the calibrated composite stays on target
# ============================================================================


class TestCalibrationDriftRegression(unittest.TestCase):
    def setUp(self):
        self._saved_scale = ps.ARSENAL_SCALE

    def tearDown(self):
        ps.ARSENAL_SCALE = self._saved_scale

    def test_median_arsenal_similarity_stays_near_target(self):
        # Controlled W₂ fixture with a known median; after calibrate→apply,
        # the median arsenal similarity must land on the 0.50 target.
        rng = np.random.default_rng(2024)
        dists = np.abs(rng.normal(2.84, 0.9, size=8000)) + 0.05

        gamma = calibrate_arsenal_gamma(dists, target_median_score=0.50)
        median = float(np.median(dists))
        report = CalibrationReport(arsenal_gamma=gamma, arsenal_median_w2=median)

        eng = _new_engine()
        scale = eng.apply_calibration(report)

        scores = np.exp(-dists / scale)
        self.assertAlmostEqual(float(np.median(scores)), 0.50, delta=0.01)

    def test_default_scale_already_near_target_on_canonical_median(self):
        # With NO report applied, the default 4.10 keeps the canonical
        # median (~2.84) near the 0.50 target.
        median = 2.84
        at_default = float(np.exp(-median / DEFAULT_ARSENAL_SCALE))
        self.assertAlmostEqual(at_default, 0.50, delta=0.02)
        # A larger scale (4.25) puts the median score above the target.
        at_old = float(np.exp(-median / 4.25))
        self.assertGreater(at_old, at_default)

    def test_no_arsenal_path_comparable_to_full_path(self):
        # A GMM-less pitcher whose command perfectly matches the query must
        # land on the SAME [0,1] scale as a full-arsenal clone — not capped
        # at WEIGHT_COMMAND. We compare a perfect command-only match against
        # a perfect full-arsenal match (identical arsenal + identical command).
        eng = _new_engine()
        cmd = [0.08, 0.22, 0.30, 0.45, 0.28]
        gmm = _single_component_gmm([90.0] * GMM_FEATURE_DIM)

        # Full-arsenal clone pair (identical gmm + command → score ≈ 1.0).
        qf = _make_profile(40, gmm=gmm, command=cmd)
        cf = _make_profile(41, gmm=gmm, command=cmd)
        # No-arsenal pair with identical command (→ redistributed → ≈ 1.0).
        qn = _make_profile(42, gmm=None, command=cmd)
        cn = _make_profile(43, gmm=None, command=cmd)

        for p in (qf, cf, qn, cn):
            eng._profiles[(p.pitcher_id, p.season)] = p
        eng._normalizer.fit([qf, cf, qn, cn])

        full_score, _, _ = eng._score_pair(qf, cf)
        no_arsenal_score, _, _ = eng._score_pair(qn, cn)

        # Both perfect matches should be at (or near) the [0,1] ceiling and
        # within a small tolerance of each other — i.e. comparable scales.
        self.assertGreater(no_arsenal_score, 0.90)
        self.assertGreater(full_score, 0.90)
        self.assertAlmostEqual(no_arsenal_score, full_score, delta=0.10)

        # A factor of 1.0 would cap no_arsenal_score at WEIGHT_COMMAND (0.35),
        # far below the full-arsenal clone.
        self.assertGreater(no_arsenal_score, WEIGHT_COMMAND + 0.1)


if __name__ == "__main__":
    unittest.main()
