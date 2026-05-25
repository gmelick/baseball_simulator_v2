"""
test_ml_engines_sim346.py
=========================
SIM-346 (ML calibration fixes) — pitcher similarity engine.

Covers the three audited bugs plus a drift-regression guard:

  1. **No-arsenal fallback was a ×1.0 no-op.** When a pitcher (or pair) has
     no GMM arsenal, the old code rescaled command by
     ``WEIGHT_COMMAND / WEIGHT_COMMAND`` (== 1.0), so a GMM-less composite was
     capped at ``WEIGHT_COMMAND`` (0.35) instead of being redistributed onto
     the full [0, 1] scale. The fix rescales by
     ``_TOTAL_WEIGHT / WEIGHT_COMMAND`` (== 1/0.35 ≈ 2.857) so a command-only
     pitcher is comparable to a full-arsenal one.

  2. **``arsenal_gamma`` (squared exp) vs ``ARSENAL_SCALE`` (linear exp) were
     inconsistent.** The engine now uses ONE canonical linear form
     ``exp(-W₂ / ARSENAL_SCALE)`` with the locked calibrated scale 4.10, and
     ``arsenal_scale_from_gamma`` converts the calibrator's squared-form gamma
     into that linear scale (same median/target anchor, no divergent path).

  3. **``CalibrationReport`` was computed but never applied.**
     ``PitcherSimilarityEngine.apply_calibration(report)`` now wires
     ``CalibrationReport.arsenal_gamma`` (+ median) into the module-level
     ``ARSENAL_SCALE`` so the engine reads the calibrated value, not a literal.

  4. **Drift regression.** On a controlled W₂ fixture, the calibrated arsenal
     transform keeps the MEDIAN arsenal similarity ~0.50, and the no-arsenal
     composite is on the same scale as the full-arsenal composite.

These tests use the in-memory ``__new__``-bypass / direct-dataclass idioms of
the existing pitcher-engine tests — no live DuckDB.
"""

from __future__ import annotations

import unittest

import numpy as np

import similarity.engines.pitcher_similarity as ps
from similarity.engines.pitcher_similarity import (
    DEFAULT_ARSENAL_SCALE,
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
    WEIGHT_ARSENAL,
    WEIGHT_COMMAND,
    ArsenalCache,
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
from similarity.similarity_calibration import (
    CalibrationReport,
    calibrate_arsenal_gamma,
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
    """Construct an engine without touching DuckDB (mirrors existing tests)."""
    eng = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    eng._profiles = {}
    eng._normalizer = FeatureNormalizer()
    eng._command_rbf = RBFSimilarity(sigma=1.0)
    eng._arsenal_cache = ArsenalCache()
    eng._arsenal_scale = DEFAULT_ARSENAL_SCALE
    return eng


# ============================================================================
# 1. No-arsenal redistribution is no longer a ×1.0 no-op
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

        # The OLD buggy no-op would have produced WEIGHT_COMMAND * cmd_score.
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
        self.assertGreater(factor, 1.0)  # explicitly not the old no-op

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
# 2. gamma (squared) vs scale (linear) reconciled into ONE form
# ============================================================================


class TestGammaScaleReconciliation(unittest.TestCase):
    def test_default_scale_is_locked_linear_value(self):
        self.assertAlmostEqual(DEFAULT_ARSENAL_SCALE, 4.10, places=10)
        # Module default starts on the locked value.
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
        # After wiring, _score_pair's arsenal sub-score must reflect the new
        # scale (not the old default literal).
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
# 4. Drift regression — calibrated composite stays on target
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
        # Even with NO report applied, the locked default 4.10 keeps the
        # canonical median (~2.84) near the 0.50 target — guards against the
        # old 4.25 literal silently drifting the median off target.
        median = 2.84
        at_default = float(np.exp(-median / DEFAULT_ARSENAL_SCALE))
        self.assertAlmostEqual(at_default, 0.50, delta=0.02)
        # The OLD 4.25 literal drifted the median high (proving why we moved).
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

        # Under the OLD no-op, no_arsenal_score would have been ≤ WEIGHT_COMMAND
        # (0.35) — far below the full-arsenal clone. Guard against regression.
        self.assertGreater(no_arsenal_score, WEIGHT_COMMAND + 0.1)


if __name__ == "__main__":
    unittest.main()
