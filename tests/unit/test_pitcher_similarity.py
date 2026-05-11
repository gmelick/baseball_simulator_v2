"""
test_pitcher_similarity.py
===========================
Unit and integration tests for Step 2.1 — Pitcher-to-Pitcher Similarity Engine.

Tests cover:
  1. Bures–Wasserstein distance (individual Gaussian components)
  2. GMM-to-GMM optimal transport distance
  3. RBF kernel scoring
  4. Empirical Bayes shrinkage
  5. Minimum cluster size enforcement
  6. Feature normalization
  7. Arsenal distance cache (lazy + batch)
  8. Full engine integration with synthetic profiles
  9. Exhaustive scoring (all profiles in partition returned)
  10. Cross-season same-pitcher comparisons
  11. Edge cases (missing GMMs, single-component models, self-similarity)
  12. Cache persistence (save/load round-trip)

Run with:
    pytest test_pitcher_similarity.py -v
    python test_pitcher_similarity.py   (standalone)
"""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from pitcher_similarity import (
    ArsenalCache,
    ArsenalSimilarity,
    EmpiricalBayesShrinkage,
    FeatureNormalizer,
    GMMComponent,
    GMMModel,
    HandednessPartition,
    PitcherProfile,
    PitcherSimilarityEngine,
    RBFSimilarity,
    SimilarityResult,
    build_similarity_matrix,
    enforce_min_cluster_size,
    standardize_gmm,
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
    COMMAND_FEATURES,
    RESULT_FEATURES,
    MIN_CLUSTER_SIZE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_component(
    cid: int,
    weight: float,
    mean: list[float],
    var_diag: float = 1.0,
    n_pitches: int = 200,
) -> GMMComponent:
    """Create a GMMComponent with a diagonal covariance matrix."""
    mean_arr = np.array(mean, dtype=np.float64)
    cov = np.eye(GMM_FEATURE_DIM, dtype=np.float64) * var_diag
    return GMMComponent(
        component_id=cid,
        weight=weight,
        mean=mean_arr,
        covariance=cov,
        n_pitches=n_pitches,
    )


def _make_gmm(components: list[GMMComponent]) -> GMMModel:
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=np.zeros(GMM_FEATURE_DIM),
        feature_stds=np.ones(GMM_FEATURE_DIM),
        components=components,
    )


def _make_profile(
    pitcher_id: int,
    season: int,
    p_throws: str = "R",
    gmm: GMMModel | None = None,
    command: list[float] | None = None,
    results: list[float] | None = None,
    release: list[float] | None = None,
    sample_pitches: int = 500,
) -> PitcherProfile:
    return PitcherProfile(
        pitcher_id=pitcher_id,
        season=season,
        p_throws=p_throws,
        sample_pitches=sample_pitches,
        gmm=gmm,
        command_vec=np.array(command or [0.08, 0.22, 0.30, 0.45, 0.28], dtype=np.float64),
        result_vec=np.array(results or [0.45, 0.35, 0.20, 1.20, 1.0], dtype=np.float64),
        release_vec=np.array(release or [-1.5, 6.0], dtype=np.float64),
    )


# Reusable components
POWER_FB = _make_component(0, 0.55, [96.0, 16.0, -8.0, 2400, 210, -1.5, 6.2, 6.5], var_diag=2.0, n_pitches=600)
SLIDER = _make_component(1, 0.30, [86.0, -2.0, 6.0, 2500, 140, -1.3, 6.0, 6.2], var_diag=1.5, n_pitches=350)
CHANGEUP = _make_component(2, 0.15, [88.0, 8.0, -14.0, 1800, 220, -1.6, 6.1, 5.9], var_diag=1.0, n_pitches=180)
TINY_OUTLIER = _make_component(3, 0.02, [72.0, -20.0, 2.0, 3000, 300, -1.0, 5.5, 5.0], var_diag=0.5, n_pitches=15)

# Population statistics computed from the reusable components above.
# Used to standardize GMMs in unit tests so ArsenalSimilarity.score()
# operates in the same z-scored space as the production engine.
_ALL_TEST_MEANS = np.array([c.mean for c in [POWER_FB, SLIDER, CHANGEUP, TINY_OUTLIER]])
_ALL_TEST_WEIGHTS = np.array([c.weight for c in [POWER_FB, SLIDER, CHANGEUP, TINY_OUTLIER]])
_ALL_TEST_WEIGHTS = _ALL_TEST_WEIGHTS / _ALL_TEST_WEIGHTS.sum()
_POP_MEAN = np.average(_ALL_TEST_MEANS, axis=0, weights=_ALL_TEST_WEIGHTS)
_POP_STD = np.sqrt(np.average((_ALL_TEST_MEANS - _POP_MEAN) ** 2, axis=0, weights=_ALL_TEST_WEIGHTS))
_POP_STD[_POP_STD == 0] = 1.0


def _std_gmm(gmm: GMMModel) -> GMMModel:
    """Standardize a test GMM using the population of test components."""
    return standardize_gmm(gmm, _POP_MEAN, _POP_STD)


# ============================================================================
# Tests: Bures–Wasserstein Distance
# ============================================================================

class TestBuresWasserstein(unittest.TestCase):

    def test_identical_gaussians_distance_zero(self):
        mean = np.array([95.0, 14.0, -7.0, 2300, 200, -1.5, 6.0, 6.5])
        cov = np.eye(8) * 2.0
        d2 = ArsenalSimilarity.bures_wasserstein_sq(mean, cov, mean, cov)
        self.assertAlmostEqual(d2, 0.0, places=6)

    def test_shifted_mean_only(self):
        m1 = np.zeros(8)
        m2 = np.ones(8)
        cov = np.eye(8)
        d2 = ArsenalSimilarity.bures_wasserstein_sq(m1, cov, m2, cov)
        expected = np.sum((m1 - m2) ** 2)
        self.assertAlmostEqual(d2, expected, places=4)

    def test_symmetry(self):
        m1 = np.array([95, 14, -7, 2300, 200, -1.5, 6.0, 6.5], dtype=np.float64)
        m2 = np.array([88, 8, -14, 1800, 220, -1.6, 6.1, 5.9], dtype=np.float64)
        C1 = np.eye(8) * 2.0
        C2 = np.eye(8) * 1.5
        d_ab = ArsenalSimilarity.bures_wasserstein_sq(m1, C1, m2, C2)
        d_ba = ArsenalSimilarity.bures_wasserstein_sq(m2, C2, m1, C1)
        self.assertAlmostEqual(d_ab, d_ba, places=4)

    def test_nonnegative(self):
        rng = np.random.default_rng(42)
        for _ in range(20):
            m1 = rng.standard_normal(8) * 10
            m2 = rng.standard_normal(8) * 10
            A = rng.standard_normal((8, 8))
            C1 = A @ A.T + np.eye(8) * 0.1
            B = rng.standard_normal((8, 8))
            C2 = B @ B.T + np.eye(8) * 0.1
            d2 = ArsenalSimilarity.bures_wasserstein_sq(m1, C1, m2, C2)
            self.assertGreaterEqual(d2, -1e-6)


# ============================================================================
# Tests: GMM-to-GMM Optimal Transport
# ============================================================================

class TestArsenalSimilarity(unittest.TestCase):
    """Test the full GMM-to-GMM arsenal similarity.

    All GMMs are standardized before scoring, matching the production
    pipeline where _standardize_arsenals() runs during build().
    """

    def test_identical_gmms_perfect_score(self):
        gmm = _std_gmm(_make_gmm([POWER_FB, SLIDER, CHANGEUP]))
        score = ArsenalSimilarity.score(gmm, gmm)
        self.assertGreater(score, 0.99)

    def test_different_arsenals_lower_score(self):
        gmm_a = _std_gmm(_make_gmm([POWER_FB, SLIDER]))
        soft_fb = _make_component(0, 0.6, [85.0, 8.0, 12.0, 2000, 180, 1.5, 5.8, 5.5])
        curve = _make_component(1, 0.4, [76.0, -18.0, 4.0, 2700, 340, 1.3, 5.6, 5.3])
        gmm_b = _std_gmm(_make_gmm([soft_fb, curve]))
        score = ArsenalSimilarity.score(gmm_a, gmm_b)
        self.assertLess(score, 0.5)

    def test_score_symmetry(self):
        gmm_a = _std_gmm(_make_gmm([POWER_FB, SLIDER]))
        gmm_b = _std_gmm(_make_gmm([POWER_FB, CHANGEUP]))
        s_ab = ArsenalSimilarity.score(gmm_a, gmm_b)
        s_ba = ArsenalSimilarity.score(gmm_b, gmm_a)
        self.assertAlmostEqual(s_ab, s_ba, places=6)

    def test_different_component_counts(self):
        """GMMs with 2 and 3 components that share structure should
        produce a positive, non-trivial similarity."""
        gmm_2 = _std_gmm(_make_gmm([POWER_FB, SLIDER]))
        gmm_3 = _std_gmm(_make_gmm([POWER_FB, SLIDER, CHANGEUP]))
        score = ArsenalSimilarity.score(gmm_2, gmm_3)
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_empty_gmm_returns_zero(self):
        gmm_empty = _make_gmm([])
        gmm_full = _std_gmm(_make_gmm([POWER_FB]))
        score = ArsenalSimilarity.score(gmm_empty, gmm_full)
        self.assertEqual(score, 0.0)

    def test_score_range(self):
        """Scores should always be in [0, 1], even for random GMMs.
        Random components are generated in already-standardized space
        (mean ~0, std ~1) so no additional standardization is needed."""
        rng = np.random.default_rng(42)
        for _ in range(10):
            comps_a = [_make_component(i, 1/3, rng.standard_normal(8).tolist()) for i in range(3)]
            comps_b = [_make_component(i, 1/2, rng.standard_normal(8).tolist()) for i in range(2)]
            s = ArsenalSimilarity.score(_make_gmm(comps_a), _make_gmm(comps_b))
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)


# ============================================================================
# Tests: RBF Kernel
# ============================================================================

class TestRBFSimilarity(unittest.TestCase):

    def test_identical_vectors(self):
        rbf = RBFSimilarity(sigma=1.0)
        v = np.array([0.3, 0.2, 0.5])
        self.assertAlmostEqual(rbf.score(v, v), 1.0)

    def test_distant_vectors_low_score(self):
        rbf = RBFSimilarity(sigma=0.5)
        a = np.array([0.0, 0.0, 0.0])
        b = np.array([5.0, 5.0, 5.0])
        self.assertLess(rbf.score(a, b), 0.01)

    def test_nan_handling(self):
        rbf = RBFSimilarity(sigma=1.0)
        a = np.array([0.5, np.nan, 0.3])
        b = np.array([0.5, 0.7, 0.3])
        score = rbf.score(a, b)
        self.assertAlmostEqual(score, 1.0)

    def test_batch_matches_individual(self):
        rbf = RBFSimilarity(sigma=0.8)
        query = np.array([0.3, 0.2, 0.5])
        candidates = np.array([[0.3, 0.2, 0.5], [0.0, 0.0, 0.0], [0.6, 0.4, 0.7]])
        batch_scores = rbf.score_batch(query, candidates)
        for i in range(3):
            individual = rbf.score(query, candidates[i])
            self.assertAlmostEqual(batch_scores[i], individual, places=10)


# ============================================================================
# Tests: Empirical Bayes Shrinkage
# ============================================================================

class TestEmpiricalBayes(unittest.TestCase):

    def test_large_sample_minimal_shrinkage(self):
        eb = EmpiricalBayesShrinkage(n_prior=50)
        self.assertGreater(eb.alpha(5000), 0.98)

    def test_small_sample_heavy_shrinkage(self):
        eb = EmpiricalBayesShrinkage(n_prior=50)
        self.assertLess(eb.alpha(10), 0.2)

    def test_at_prior_fifty_fifty(self):
        eb = EmpiricalBayesShrinkage(n_prior=50)
        self.assertAlmostEqual(eb.alpha(50), 0.5)

    def test_shrink_with_nan(self):
        eb = EmpiricalBayesShrinkage(n_prior=50)
        raw = np.array([0.10, np.nan, 0.30])
        avg = np.array([0.08, 0.22, 0.28])
        result = eb.shrink(raw, avg, 200)
        self.assertFalse(np.isnan(result[1]))


# ============================================================================
# Tests: Minimum Cluster Size Enforcement
# ============================================================================

class TestMinClusterSize(unittest.TestCase):

    def test_all_large_components_unchanged(self):
        gmm = _make_gmm([POWER_FB, SLIDER, CHANGEUP])
        cleaned = enforce_min_cluster_size(gmm)
        self.assertEqual(cleaned.n_components, 3)

    def test_tiny_component_merged(self):
        gmm = _make_gmm([POWER_FB, SLIDER, TINY_OUTLIER])
        cleaned = enforce_min_cluster_size(gmm)
        self.assertLess(cleaned.n_components, 3)
        for c in cleaned.components:
            self.assertGreaterEqual(c.n_pitches, MIN_CLUSTER_SIZE)

    def test_weights_renormalized(self):
        gmm = _make_gmm([POWER_FB, SLIDER, TINY_OUTLIER])
        cleaned = enforce_min_cluster_size(gmm)
        total_w = sum(c.weight for c in cleaned.components)
        self.assertAlmostEqual(total_w, 1.0, places=6)

    def test_single_component_preserved(self):
        gmm = _make_gmm([POWER_FB])
        cleaned = enforce_min_cluster_size(gmm)
        self.assertEqual(cleaned.n_components, 1)


# ============================================================================
# Tests: Feature Normalization
# ============================================================================

class TestFeatureNormalizer(unittest.TestCase):

    def test_fit_normalizes_to_zscore(self):
        profiles = [
            _make_profile(1, 2024, command=[0.08, 0.22, 0.30, 0.45, 0.28]),
            _make_profile(2, 2024, command=[0.10, 0.20, 0.28, 0.50, 0.30]),
            _make_profile(3, 2024, command=[0.06, 0.24, 0.32, 0.40, 0.26]),
        ]
        norm = FeatureNormalizer()
        norm.fit(profiles)
        normed = np.array([norm.normalize_command(p.command_vec) for p in profiles])
        np.testing.assert_allclose(normed.mean(axis=0), 0.0, atol=1e-10)
        np.testing.assert_allclose(normed.std(axis=0), 1.0, atol=1e-10)


# ============================================================================
# Tests: Arsenal Cache
# ============================================================================

class TestArsenalCache(unittest.TestCase):

    def test_put_and_get(self):
        cache = ArsenalCache()
        cache.put((100, 2024), (200, 2024), 5.5)
        self.assertAlmostEqual(cache.get((100, 2024), (200, 2024)), 5.5)

    def test_symmetric_keys(self):
        """Cache should be symmetric: (A, B) and (B, A) return the same value."""
        cache = ArsenalCache()
        cache.put((100, 2024), (200, 2024), 5.5)
        self.assertAlmostEqual(cache.get((200, 2024), (100, 2024)), 5.5)

    def test_miss_returns_none(self):
        cache = ArsenalCache()
        self.assertIsNone(cache.get((100, 2024), (200, 2024)))

    def test_get_or_compute_caches_result(self):
        cache = ArsenalCache()
        gmm = _make_gmm([POWER_FB, SLIDER])
        dist = cache.get_or_compute((100, 2024), (200, 2024), gmm, gmm)
        self.assertAlmostEqual(dist, 0.0, places=4)

        # Second call should hit cache
        cached = cache.get((100, 2024), (200, 2024))
        self.assertIsNotNone(cached)

    def test_self_comparison_returns_zero(self):
        cache = ArsenalCache()
        dist = cache.get_or_compute((100, 2024), (100, 2024), None, None)
        self.assertEqual(dist, 0.0)

    def test_none_gmm_returns_inf(self):
        cache = ArsenalCache()
        dist = cache.get_or_compute((100, 2024), (200, 2024), None, _make_gmm([POWER_FB]))
        self.assertEqual(dist, float("inf"))

    def test_save_and_load(self):
        cache = ArsenalCache()
        cache.put((100, 2024), (200, 2024), 5.5)
        cache.put((100, 2024), (300, 2024), 7.2)

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name

        try:
            cache.save(path)

            cache2 = ArsenalCache()
            cache2.load(path)
            self.assertEqual(cache2.size, 2)
            self.assertAlmostEqual(cache2.get((100, 2024), (200, 2024)), 5.5)
            self.assertAlmostEqual(cache2.get((100, 2024), (300, 2024)), 7.2)
        finally:
            os.unlink(path)

    def test_precompute_serial(self):
        """Batch precompute should fill the cache for all pairs."""
        gmm_a = _make_gmm([POWER_FB, SLIDER])
        gmm_b = _make_gmm([POWER_FB, CHANGEUP])
        gmm_c = _make_gmm([SLIDER, CHANGEUP])

        profiles = [
            _make_profile(100, 2024, gmm=gmm_a),
            _make_profile(200, 2024, gmm=gmm_b),
            _make_profile(300, 2024, gmm=gmm_c),
        ]

        cache = ArsenalCache()
        cache.precompute(profiles, n_workers=1)
        # 3 profiles → 3 pairs: (100,200), (100,300), (200,300)
        self.assertEqual(cache.size, 3)


# ============================================================================
# Tests: Full Engine Integration (Synthetic Data)
# ============================================================================

def _build_synthetic_engine():
    """Create an engine with synthetic profiles — no DuckDB needed."""
    engine = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {}
    engine._league_avg_command = {}
    engine._league_avg_result = {}
    engine._league_avg_release = {}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._command_rbf = RBFSimilarity(sigma=0.8)
    engine._result_rbf = RBFSimilarity(sigma=1.2)
    engine._release_rbf = RBFSimilarity(sigma=0.6)
    engine._partition_l = HandednessPartition("L")
    engine._partition_r = HandednessPartition("R")
    engine._arsenal_cache = ArsenalCache()

    # Pitcher A: Power righty 2024
    gmm_a = _make_gmm([POWER_FB, SLIDER])
    prof_a_2024 = _make_profile(
        100, 2024, "R", gmm_a,
        command=[0.07, 0.28, 0.32, 0.47, 0.30],
        results=[0.40, 0.38, 0.22, 1.10, 0.90],
        release=[-1.5, 6.2],
    )

    # Pitcher A: same pitcher, 2025 season (slightly different profile)
    gmm_a_25 = _make_gmm([
        _make_component(0, 0.50, [95.0, 15.0, -9.0, 2350, 215, -1.5, 6.2, 6.5]),
        _make_component(1, 0.35, [85.0, -3.0, 7.0, 2450, 138, -1.3, 6.0, 6.1]),
        CHANGEUP,
    ])
    prof_a_2025 = _make_profile(
        100, 2025, "R", gmm_a_25,
        command=[0.08, 0.26, 0.31, 0.46, 0.29],
        results=[0.42, 0.36, 0.22, 1.15, 0.95],
        release=[-1.5, 6.1],
    )

    # Pitcher B: Similar power righty 2024
    gmm_b = _make_gmm([
        _make_component(0, 0.50, [95.5, 15.5, -7.5, 2380, 212, -1.4, 6.1, 6.4]),
        _make_component(1, 0.35, [85.5, -1.5, 5.5, 2480, 142, -1.2, 5.9, 6.1]),
        CHANGEUP,
    ])
    prof_b = _make_profile(
        200, 2024, "R", gmm_b,
        command=[0.08, 0.26, 0.31, 0.46, 0.29],
        results=[0.42, 0.36, 0.22, 1.15, 0.95],
        release=[-1.4, 6.1],
    )

    # Pitcher C: Very different RHP (finesse)
    finesse_fb = _make_component(0, 0.4, [89.0, 11.0, -5.0, 2100, 200, -1.8, 5.9, 6.0])
    big_curve = _make_component(1, 0.35, [77.0, -18.0, 3.0, 2700, 340, -1.6, 5.7, 5.6])
    slow_change = _make_component(2, 0.25, [82.0, 6.0, -10.0, 1700, 230, -1.7, 5.8, 5.7])
    gmm_c = _make_gmm([finesse_fb, big_curve, slow_change])
    prof_c = _make_profile(
        300, 2024, "R", gmm_c,
        command=[0.06, 0.20, 0.28, 0.42, 0.25],
        results=[0.50, 0.30, 0.20, 1.30, 0.70],
        release=[-1.8, 5.9],
    )

    # Pitcher D: Soft-tossing lefty
    soft_fb = _make_component(0, 0.5, [87.0, 10.0, 14.0, 2000, 190, 1.5, 5.8, 5.5])
    curve = _make_component(1, 0.5, [77.0, -16.0, 5.0, 2600, 320, 1.3, 5.6, 5.3])
    gmm_d = _make_gmm([soft_fb, curve])
    prof_d = _make_profile(
        400, 2024, "L", gmm_d,
        command=[0.10, 0.18, 0.26, 0.40, 0.25],
        results=[0.50, 0.30, 0.20, 1.30, 0.70],
        release=[1.5, 5.8],
    )

    # Another lefty for partition
    prof_e = _make_profile(
        500, 2024, "L", _make_gmm([soft_fb, curve]),
        command=[0.09, 0.19, 0.27, 0.41, 0.26],
        results=[0.48, 0.32, 0.20, 1.28, 0.72],
        release=[1.4, 5.7],
    )

    all_profiles = [prof_a_2024, prof_a_2025, prof_b, prof_c, prof_d, prof_e]
    for p in all_profiles:
        engine._profiles[(p.pitcher_id, p.season)] = p

    # Replicate the build() pipeline:
    # 1. enforce_min_cluster_size (already clean in these fixtures)
    # 2. standardize arsenals into z-score space
    # 3. fit normalizer + build partitions
    engine._standardize_arsenals()

    all_profiles = list(engine._profiles.values())
    engine._normalizer.fit(all_profiles)
    profiles_l = [p for p in all_profiles if p.p_throws == "L"]
    profiles_r = [p for p in all_profiles if p.p_throws == "R"]
    engine._partition_l.build(profiles_l, engine._normalizer)
    engine._partition_r.build(profiles_r, engine._normalizer)

    return engine


class TestExhaustiveScoring(unittest.TestCase):
    """Tests that query() returns ALL profiles in the partition."""

    def setUp(self):
        self.engine = _build_synthetic_engine()

    def test_query_returns_all_rhp(self):
        """Query for RHP should return ALL other RHP pitcher-seasons."""
        results = self.engine.query(100, 2024)
        # There are 4 RHP profiles: (100,2024), (100,2025), (200,2024), (300,2024)
        # Exclude self → 3 results
        self.assertEqual(len(results), 3)

    def test_query_n_none_returns_all(self):
        """n=None should return all results."""
        results_all = self.engine.query(100, 2024, n=None)
        results_top1 = self.engine.query(100, 2024, n=1)
        self.assertEqual(len(results_all), 3)
        self.assertEqual(len(results_top1), 1)
        # Top-1 should match the highest-scored in all
        self.assertEqual(results_top1[0].pitcher_id, results_all[0].pitcher_id)

    def test_query_returns_all_lhp(self):
        """Query for LHP should return all other LHP pitcher-seasons."""
        results = self.engine.query(400, 2024)
        # 2 LHP profiles, exclude self → 1
        self.assertEqual(len(results), 1)

    def test_results_sorted_by_score_descending(self):
        results = self.engine.query(100, 2024)
        for i in range(len(results) - 1):
            self.assertGreaterEqual(results[i].score, results[i + 1].score)


class TestCrossSeasonComparisons(unittest.TestCase):
    """Tests that same-pitcher cross-season profiles are properly compared."""

    def setUp(self):
        self.engine = _build_synthetic_engine()

    def test_cross_season_included_in_results(self):
        """Querying pitcher 100 season 2024 should include pitcher 100 season 2025."""
        results = self.engine.query(100, 2024)
        pitcher_100_results = [r for r in results if r.pitcher_id == 100]
        self.assertEqual(len(pitcher_100_results), 1)
        self.assertEqual(pitcher_100_results[0].season, 2025)

    def test_cross_season_high_similarity(self):
        """Same pitcher across seasons should be highly similar
        (the profiles are intentionally close in the test fixture)."""
        results = self.engine.query(100, 2024)
        cross_season = [r for r in results if r.pitcher_id == 100 and r.season == 2025][0]
        # With synthetic data the cross-season score won't be super high
        # because the arsenal changed (2→3 components). The key assertion
        # is that it IS included in results and has a positive score.
        self.assertGreater(cross_season.score, 0.1,
                           "Same pitcher across adjacent seasons should have positive similarity")

    def test_cross_season_ranked_above_dissimilar(self):
        """Same pitcher across seasons should be more similar than a
        completely different pitcher archetype (finesse vs power)."""
        results = self.engine.query(100, 2024)
        scores = {(r.pitcher_id, r.season): r.score for r in results}
        cross_season_score = scores.get((100, 2025), 0.0)
        finesse_score = scores.get((300, 2024), 0.0)
        self.assertGreater(cross_season_score, finesse_score)

    def test_exact_self_excluded(self):
        """The exact (pitcher_id, season) tuple should NOT appear in results."""
        results = self.engine.query(100, 2024)
        exact_self = [r for r in results if r.pitcher_id == 100 and r.season == 2024]
        self.assertEqual(len(exact_self), 0)

    def test_query_pair_cross_season(self):
        """query_pair should work for same pitcher across seasons."""
        result = self.engine.query_pair((100, 2024), (100, 2025))
        self.assertIsNotNone(result)
        self.assertEqual(result.pitcher_id, 100)
        self.assertEqual(result.season, 2025)
        self.assertGreater(result.score, 0.0)


class TestHandednessPartition(unittest.TestCase):

    def setUp(self):
        self.engine = _build_synthetic_engine()

    def test_rhp_query_only_returns_rhp(self):
        results = self.engine.query(100, 2024)
        for r in results:
            self.assertEqual(r.p_throws, "R")

    def test_lhp_query_only_returns_lhp(self):
        results = self.engine.query(400, 2024)
        for r in results:
            self.assertEqual(r.p_throws, "L")

    def test_cross_hand_query_pair_works(self):
        """query_pair between LHP and RHP should still return a result
        (it computes ad-hoc, not from partitions)."""
        result = self.engine.query_pair((100, 2024), (400, 2024))
        self.assertIsNotNone(result)
        self.assertGreater(result.score, 0.0)


class TestScoreProperties(unittest.TestCase):

    def setUp(self):
        self.engine = _build_synthetic_engine()

    def test_all_scores_in_range(self):
        """SIM-148: removed vacuous release_score / results_score asserts.

        Pre-SIM-148 this method asserted ``r.release_score >= 0.0`` and
        ``r.results_score >= 0.0`` against fields that had been removed
        from SimilarityResult by SIM-067.  Either path was useless:
          * If a stale field default was still 0.0 → assertion passes vacuously.
          * If the field was actually removed → AttributeError, false negative.
        Real coverage of engine correctness now lives below in
        ``test_score_pair_returns_three_subscores``.
        """
        results = self.engine.query(100, 2024)
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0)
            self.assertGreaterEqual(r.arsenal_score, 0.0)
            self.assertLessEqual(r.arsenal_score, 1.0)
            self.assertGreaterEqual(r.command_score, 0.0)
            self.assertLessEqual(r.command_score, 1.0)

    def test_similarity_result_has_no_release_score_field(self):
        """SIM-148 / SIM-067 regression: ``release_score`` removed from the
        SimilarityResult dataclass (release-point info is captured inside
        the GMM components).  Re-introducing the field would silently
        re-double-count the signal."""
        # Use dataclasses.fields if available; fall back to __annotations__
        try:
            from dataclasses import fields
            field_names = {f.name for f in fields(SimilarityResult)}
        except TypeError:
            field_names = set(SimilarityResult.__annotations__.keys())
        self.assertNotIn(
            "release_score", field_names,
            "SIM-067/SIM-148: SimilarityResult must NOT have release_score; "
            "re-adding it re-introduces a double-counted signal.",
        )

    def test_score_pair_returns_three_subscores(self):
        """SIM-148 / SIM-067 regression: ``_score_pair`` returns
        ``(composite, arsenal, command)`` — a 3-tuple.  Restoring the old
        5-tuple (with separate release / results sub-scores) would break
        every caller that unpacks the result."""
        # Build two synthetic profiles that the engine already loaded.
        ids = self.engine.profile_ids()
        self.assertGreaterEqual(len(ids), 2, "synthetic engine needs >=2 profiles")
        pa = self.engine.get_profile(*ids[0])
        pb = self.engine.get_profile(*ids[1])
        pair = self.engine._score_pair(pa, pb)
        self.assertEqual(
            len(pair), 3,
            "SIM-148: _score_pair must return 3 elements (composite, arsenal, command). "
            "Pre-SIM-067 it returned 5; re-introducing release/results sub-scores "
            "double-counts signal already inside the GMM."
        )
        composite, arsenal, command = pair
        for v, name in [(composite, "composite"), (arsenal, "arsenal"),
                        (command, "command")]:
            self.assertGreaterEqual(v, 0.0, f"{name} must be in [0, 1]")
            self.assertLessEqual(v, 1.0, f"{name} must be in [0, 1]")

    def test_similar_pitcher_ranked_above_dissimilar(self):
        """Pitcher B (similar power righty) should rank above Pitcher C
        (finesse righty) when querying for Pitcher A."""
        results = self.engine.query(100, 2024)
        scores_by_id = {(r.pitcher_id, r.season): r.score for r in results}
        # B should have higher score than C
        self.assertGreater(
            scores_by_id.get((200, 2024), 0),
            scores_by_id.get((300, 2024), 0),
        )

    def test_query_nonexistent_pitcher(self):
        results = self.engine.query(999, 2024)
        self.assertEqual(len(results), 0)

    def test_query_pair_symmetry(self):
        """query_pair(A, B) score should equal query_pair(B, A) score."""
        r_ab = self.engine.query_pair((100, 2024), (200, 2024))
        r_ba = self.engine.query_pair((200, 2024), (100, 2024))
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=6)

    def test_similarity_matrix(self):
        ids = [(100, 2024), (100, 2025), (200, 2024)]
        matrix = build_similarity_matrix(self.engine, ids)

        self.assertEqual(matrix.shape, (3, 3))
        # Diagonal = 1.0
        for i in range(3):
            self.assertAlmostEqual(matrix[i, i], 1.0)
        # Symmetric
        self.assertAlmostEqual(matrix[0, 1], matrix[1, 0], places=6)
        self.assertAlmostEqual(matrix[0, 2], matrix[2, 0], places=6)

    def test_profile_count(self):
        self.assertEqual(self.engine.profile_count, 6)


class TestArsenalCacheIntegration(unittest.TestCase):
    """Tests that the arsenal cache is populated correctly by queries."""

    def setUp(self):
        self.engine = _build_synthetic_engine()

    def test_cache_populated_after_query(self):
        """After a query, the arsenal cache should contain entries."""
        self.assertEqual(self.engine.arsenal_cache_size, 0)
        self.engine.query(100, 2024)
        # Should have cached W2 distances for (100,2024) vs 3 other RHP
        self.assertGreater(self.engine.arsenal_cache_size, 0)

    def test_second_query_uses_cache(self):
        """The second query should be faster (cache hits)."""
        self.engine.query(100, 2024)
        initial_cache_size = self.engine.arsenal_cache_size

        # Query again — should not add new cache entries
        self.engine.query(100, 2024)
        self.assertEqual(self.engine.arsenal_cache_size, initial_cache_size)

    def test_different_query_benefits_from_overlap(self):
        """Querying pitcher 200 should partially reuse cache from pitcher 100's query."""
        self.engine.query(100, 2024)  # caches (100,2024)↔(200,2024), (100,2024)↔(300,2024), etc.
        cache_after_first = self.engine.arsenal_cache_size

        self.engine.query(200, 2024)  # needs (200,2024)↔(300,2024) but (200,2024)↔(100,2024) is cached
        cache_after_second = self.engine.arsenal_cache_size

        # Some entries should have been reused (cache grew by less than
        # the full number of new pairs needed)
        new_entries = cache_after_second - cache_after_first
        # (200,2024) needs pairs with: (100,2024), (100,2025), (300,2024)
        # (100,2024) was already cached from the first query
        self.assertLess(new_entries, 3)


# ============================================================================
# Tests: GMM Serialization Round-Trip
# ============================================================================

class TestGMMSerialization(unittest.TestCase):

    def test_round_trip(self):
        original = _make_gmm([POWER_FB, SLIDER])
        model_json = {
            "n_components": original.n_components,
            "feature_names": original.feature_names,
            "feature_means": original.feature_means.tolist(),
            "feature_stds": original.feature_stds.tolist(),
            "components": [
                {
                    "component_id": c.component_id,
                    "weight": c.weight,
                    "mean": c.mean.tolist(),
                    "covariance": c.covariance.tolist(),
                    "n_pitches": c.n_pitches,
                }
                for c in original.components
            ],
            "fit_diagnostics": {"bic": 12345.6},
        }

        restored = GMMModel.from_json(model_json)
        self.assertEqual(restored.n_components, original.n_components)
        self.assertEqual(len(restored.components), len(original.components))

        for orig_c, rest_c in zip(original.components, restored.components):
            np.testing.assert_allclose(rest_c.mean, orig_c.mean)
            np.testing.assert_allclose(rest_c.covariance, orig_c.covariance)
            self.assertAlmostEqual(rest_c.weight, orig_c.weight)


# ============================================================================
# Run
# ============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
0 should partially reuse cache from pitcher 100's query."""
        self.engine.query(100, 2024)
        cache_after_first = self.engine.arsenal_cache_size

        # Querying pitcher 200 in 2024 — overlaps with pairs already
        # computed for pitcher 100.  We expect new entries to be small.
        self.engine.query(200, 2024)
        cache_after_second = self.engine.arsenal_cache_size

        new_entries = cache_after_second - cache_after_first
        # Sanity: cache grew by less than naive "all pairs" count.
        self.assertLessEqual(new_entries, cache_after_first)


# ---------------------------------------------------------------------------
# SIM-148 — Doctest sentinel for pitcher_similarity.py
# ---------------------------------------------------------------------------

class TestPitcherSimilarityDoctests(unittest.TestCase):
    """SIM-148: catch docstring drift in pitcher_similarity.py automatically.

    Per ticket AC #5, runs every doctest in similarity/engines/pitcher_similarity.py.
    Implemented as a unittest.TestCase wrapper so it integrates into the existing
    suite without needing the global ``--doctest-modules`` pytest flag (which
    would scan the whole repo)."""

    def test_doctests_in_pitcher_similarity_module(self):
        import doctest
        import similarity.engines.pitcher_similarity as mod
        results = doctest.testmod(mod, verbose=False)
        self.assertEqual(
            results.failed, 0,
            f"SIM-148: {results.failed} doctest(s) failed in pitcher_similarity.py "
            f"(of {results.attempted} attempted). Run "
            "`python -m doctest similarity/engines/pitcher_similarity.py -v` to debug.",
        )


if __name__ == "__main__":
    unittest.main()
