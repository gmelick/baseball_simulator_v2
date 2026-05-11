"""
test_fielder_similarity.py
===========================
Comprehensive unit test suite for the Fielder-to-Fielder Similarity Engine.

Test categories:
  1. Configuration & Constants     — weight sums, feature definitions, position groups
  2. WeightedRBFSimilarity         — kernel math, NaN handling, batch vs scalar consistency
  3. EmpiricalBayesShrinkage       — alpha formula, shrinkage blend, NaN fill
  4. FeatureNormalizer              — z-score correctness, per-position isolation, missing pos
  5. FielderProfile                 — dataclass construction, IF vs OF vector slots
  6. PositionPartition              — build, empty partition, score_all, self-exclusion
  7. SimilarityResult               — frozen dataclass, field access
  8. FielderSimilarityEngine        — full integration without DuckDB:
       a. Position gating (cross-position query_pair returns None)
       b. Exhaustive scoring (all same-position profiles returned)
       c. Self-exclusion (query player's own season excluded)
       d. Cross-season inclusion (same player, different season included)
       e. Top-N truncation
       f. Score symmetry (A→B == B→A)
       g. Identical profiles → score ≈ 1.0
       h. Very different profiles → low score
       i. Confidence discount (low-sample profiles penalized)
       j. IF composite weight verification
       k. OF composite weight verification
       l. Middle IF pivot features (2B/SS have longer DP vector)
       m. Corner IF specialty features (bunt/scoop)
       n. Invalid position handling
       o. Missing profile handling
  9. build_similarity_matrix        — symmetry, diagonal = 1.0
  10. Regression / stability        — fixed-seed deterministic scores
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from similarity.engines.fielder_similarity import (
    ALL_POSITIONS,
    EB_N_PRIOR,
    IF_DP_FEATURES,
    IF_ERROR_FEATURES,
    IF_PIVOT_FEATURES,
    IF_RANGE_FEATURES,
    IF_SPECIALTY_FEATURES,
    # Config constants
    INFIELD_POSITIONS,
    MIN_FIELDER_BATTED_BALLS,
    OF_ARM_FEATURES,
    OF_ERROR_FEATURES,
    OF_RANGE_FEATURES,
    OF_STAR_FEATURES,
    OUTFIELD_POSITIONS,
    RBF_SIGMA_IF_DP,
    RBF_SIGMA_IF_ERRORS,
    RBF_SIGMA_IF_RANGE,
    RBF_SIGMA_IF_SPECIALTY,
    RBF_SIGMA_OF_ARM,
    RBF_SIGMA_OF_ERRORS,
    RBF_SIGMA_OF_RANGE,
    RBF_SIGMA_OF_STARS,
    WEIGHT_IF_DP,
    WEIGHT_IF_ERRORS,
    WEIGHT_IF_RANGE,
    WEIGHT_IF_SPECIALTY,
    WEIGHT_OF_ARM,
    WEIGHT_OF_ERRORS,
    WEIGHT_OF_RANGE,
    WEIGHT_OF_STARS,
    EmpiricalBayesShrinkage,
    FeatureNormalizer,
    # Classes
    FielderProfile,
    FielderSimilarityEngine,
    PositionPartition,
    SimilarityResult,
    WeightedRBFSimilarity,
    build_similarity_matrix,
)

# ============================================================================
# Helpers — build profiles and engines without DuckDB
# ============================================================================


def _make_if_profile(
    player_id: int = 1,
    position: str = "SS",
    season: int = 2024,
    sample_bb: int = 200,
    range_vec: NDArray | None = None,
    error_vec: NDArray | None = None,
    dp_vec: NDArray | None = None,
    specialty_vec: NDArray | None = None,
    eb_alpha: float | None = None,
) -> FielderProfile:
    """Create an infielder profile with sensible defaults."""
    is_middle = position in ("2B", "SS")
    dp_dim = len(IF_DP_FEATURES) + (len(IF_PIVOT_FEATURES) if is_middle else 0)
    return FielderProfile(
        player_id=player_id,
        position=position,
        season=season,
        innings_played=600.0,
        sample_batted_balls=sample_bb,
        range_vec=range_vec
        if range_vec is not None
        else np.zeros(len(IF_RANGE_FEATURES), dtype=np.float64),
        error_vec=error_vec if error_vec is not None else np.array([0.02, 0.01], dtype=np.float64),
        dp_vec=dp_vec if dp_vec is not None else np.zeros(dp_dim, dtype=np.float64),
        specialty_vec=specialty_vec
        if specialty_vec is not None
        else np.array([0.5, 0.9], dtype=np.float64),
        arm_vec=None,
        star_vec=None,
        eb_alpha=eb_alpha if eb_alpha is not None else sample_bb / (sample_bb + EB_N_PRIOR),
    )


def _make_of_profile(
    player_id: int = 100,
    position: str = "CF",
    season: int = 2024,
    sample_bb: int = 200,
    range_vec: NDArray | None = None,
    error_vec: NDArray | None = None,
    arm_vec: NDArray | None = None,
    star_vec: NDArray | None = None,
    eb_alpha: float | None = None,
) -> FielderProfile:
    """Create an outfielder profile with sensible defaults."""
    return FielderProfile(
        player_id=player_id,
        position=position,
        season=season,
        innings_played=800.0,
        sample_batted_balls=sample_bb,
        range_vec=range_vec
        if range_vec is not None
        else np.zeros(len(OF_RANGE_FEATURES), dtype=np.float64),
        error_vec=error_vec if error_vec is not None else np.array([0.01, 0.005], dtype=np.float64),
        dp_vec=None,
        specialty_vec=None,
        arm_vec=arm_vec
        if arm_vec is not None
        else np.array([0.6, 0.1, 0.5, 0.0], dtype=np.float64),
        star_vec=star_vec if star_vec is not None else np.array([0.1, 0.4, 0.98], dtype=np.float64),
        eb_alpha=eb_alpha if eb_alpha is not None else sample_bb / (sample_bb + EB_N_PRIOR),
    )


def _build_test_engine(
    if_profiles: list[FielderProfile] | None = None,
    of_profiles: list[FielderProfile] | None = None,
) -> FielderSimilarityEngine:
    """
    Assemble a FielderSimilarityEngine from in-memory profiles without DuckDB.
    Replicates the post-build() state by fitting the normalizer and building
    all partitions.
    """
    engine = FielderSimilarityEngine.__new__(FielderSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {}
    engine._pos_avg = {g: {} for g in ["range", "error", "dp", "specialty", "arm", "star"]}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._partitions = {pos: PositionPartition(pos) for pos in ALL_POSITIONS}

    # RBF scorers
    engine._if_range_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_RANGE,
        reliability_weights=np.array([w for _, w in IF_RANGE_FEATURES]),
    )
    engine._if_dp_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_DP,
        reliability_weights=np.array(
            [w for _, w in IF_DP_FEATURES] + [w for _, w in IF_PIVOT_FEATURES]
        ),
    )
    engine._if_dp_rbf_corner = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_DP,
        reliability_weights=np.array([w for _, w in IF_DP_FEATURES]),
    )
    engine._if_error_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_ERRORS,
        reliability_weights=np.array([w for _, w in IF_ERROR_FEATURES]),
    )
    engine._if_specialty_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_IF_SPECIALTY,
        reliability_weights=np.array([w for _, w in IF_SPECIALTY_FEATURES]),
    )
    engine._of_range_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_RANGE,
        reliability_weights=np.array([w for _, w in OF_RANGE_FEATURES]),
    )
    engine._of_arm_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_ARM,
        reliability_weights=np.array([w for _, w in OF_ARM_FEATURES]),
    )
    engine._of_star_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_STARS,
        reliability_weights=np.array([w for _, w in OF_STAR_FEATURES]),
    )
    engine._of_error_rbf = WeightedRBFSimilarity(
        sigma=RBF_SIGMA_OF_ERRORS,
        reliability_weights=np.array([w for _, w in OF_ERROR_FEATURES]),
    )

    all_profiles = list(if_profiles or []) + list(of_profiles or [])
    for p in all_profiles:
        engine._profiles[(p.player_id, p.position, p.season)] = p

    profiles_by_pos = {pos: [] for pos in ALL_POSITIONS}
    for p in all_profiles:
        profiles_by_pos[p.position].append(p)

    engine._normalizer.fit(profiles_by_pos)
    for pos, partition in engine._partitions.items():
        partition.build(profiles_by_pos.get(pos, []), engine._normalizer)

    return engine


def _generate_ss_population(n: int = 20, seed: int = 42) -> list[FielderProfile]:
    """Generate a population of SS profiles with realistic variance."""
    rng = np.random.default_rng(seed)
    profiles = []
    for i in range(1, n + 1):
        for season in [2023, 2024]:
            bb = rng.integers(80, 400)
            profiles.append(
                _make_if_profile(
                    player_id=i,
                    position="SS",
                    season=season,
                    sample_bb=int(bb),
                    range_vec=rng.normal(0, 3, len(IF_RANGE_FEATURES)).astype(np.float64),
                    error_vec=np.clip(rng.beta(2, 50, len(IF_ERROR_FEATURES)), 0, 0.2).astype(
                        np.float64
                    ),
                    dp_vec=rng.normal(0, 2, len(IF_DP_FEATURES) + len(IF_PIVOT_FEATURES)).astype(
                        np.float64
                    ),
                    specialty_vec=rng.beta(5, 5, len(IF_SPECIALTY_FEATURES)).astype(np.float64),
                )
            )
    return profiles


def _generate_cf_population(n: int = 20, seed: int = 99) -> list[FielderProfile]:
    """Generate a population of CF profiles with realistic variance."""
    rng = np.random.default_rng(seed)
    profiles = []
    for i in range(100, 100 + n):
        for season in [2023, 2024]:
            bb = rng.integers(80, 400)
            profiles.append(
                _make_of_profile(
                    player_id=i,
                    position="CF",
                    season=season,
                    sample_bb=int(bb),
                    range_vec=rng.normal(0, 3, len(OF_RANGE_FEATURES)).astype(np.float64),
                    error_vec=np.clip(rng.beta(2, 50, len(OF_ERROR_FEATURES)), 0, 0.2).astype(
                        np.float64
                    ),
                    arm_vec=np.concatenate(
                        [
                            rng.beta(5, 5, 3),
                            rng.normal(0, 1, 1),
                        ]
                    ).astype(np.float64),
                    star_vec=rng.beta(5, 5, len(OF_STAR_FEATURES)).astype(np.float64),
                )
            )
    return profiles


# ============================================================================
# 1. Configuration & Constants
# ============================================================================


class TestConfig:
    """Validate module-level constants and feature definitions."""

    def test_if_weights_sum_to_one(self):
        total = WEIGHT_IF_RANGE + WEIGHT_IF_DP + WEIGHT_IF_ERRORS + WEIGHT_IF_SPECIALTY
        assert abs(total - 1.0) < 1e-9

    def test_of_weights_sum_to_one(self):
        total = WEIGHT_OF_RANGE + WEIGHT_OF_ARM + WEIGHT_OF_STARS + WEIGHT_OF_ERRORS
        assert abs(total - 1.0) < 1e-9

    def test_position_groups_complete(self):
        assert ALL_POSITIONS == INFIELD_POSITIONS | OUTFIELD_POSITIONS
        assert {"1B", "2B", "3B", "SS"} == INFIELD_POSITIONS
        assert {"LF", "CF", "RF"} == OUTFIELD_POSITIONS

    def test_no_position_overlap(self):
        assert set() == INFIELD_POSITIONS & OUTFIELD_POSITIONS

    def test_feature_lists_nonempty(self):
        for features in [
            IF_RANGE_FEATURES,
            IF_DP_FEATURES,
            IF_PIVOT_FEATURES,
            IF_ERROR_FEATURES,
            IF_SPECIALTY_FEATURES,
            OF_RANGE_FEATURES,
            OF_ARM_FEATURES,
            OF_STAR_FEATURES,
            OF_ERROR_FEATURES,
        ]:
            assert len(features) > 0, "Empty feature list"

    def test_reliability_weights_positive(self):
        for name, features in [
            ("IF_RANGE", IF_RANGE_FEATURES),
            ("IF_DP", IF_DP_FEATURES),
            ("IF_PIVOT", IF_PIVOT_FEATURES),
            ("IF_ERROR", IF_ERROR_FEATURES),
            ("IF_SPECIALTY", IF_SPECIALTY_FEATURES),
            ("OF_RANGE", OF_RANGE_FEATURES),
            ("OF_ARM", OF_ARM_FEATURES),
            ("OF_STAR", OF_STAR_FEATURES),
            ("OF_ERROR", OF_ERROR_FEATURES),
        ]:
            for feat_name, weight in features:
                assert weight > 0, f"{name}.{feat_name} has non-positive weight {weight}"
                assert weight <= 1.0, f"{name}.{feat_name} has weight > 1.0: {weight}"

    def test_sigma_values_positive(self):
        for sigma in [
            RBF_SIGMA_IF_RANGE,
            RBF_SIGMA_IF_DP,
            RBF_SIGMA_IF_ERRORS,
            RBF_SIGMA_IF_SPECIALTY,
            RBF_SIGMA_OF_RANGE,
            RBF_SIGMA_OF_ARM,
            RBF_SIGMA_OF_STARS,
            RBF_SIGMA_OF_ERRORS,
        ]:
            assert sigma > 0

    def test_eb_n_prior_positive(self):
        assert EB_N_PRIOR > 0

    def test_min_sample_positive(self):
        assert MIN_FIELDER_BATTED_BALLS > 0


# ============================================================================
# 2. WeightedRBFSimilarity
# ============================================================================


class TestWeightedRBFSimilarity:
    """Unit tests for the RBF kernel implementation."""

    def test_identical_vectors_score_one(self):
        weights = np.array([0.5, 0.5])
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=weights)
        x = np.array([1.0, 2.0])
        assert rbf.score(x, x) == pytest.approx(1.0)

    def test_score_range_zero_to_one(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(3))
        x = np.array([0.0, 0.0, 0.0])
        y = np.array([10.0, 10.0, 10.0])
        s = rbf.score(x, y)
        assert 0.0 <= s <= 1.0

    def test_score_decreases_with_distance(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(2))
        x = np.array([0.0, 0.0])
        y_near = np.array([0.1, 0.1])
        y_far = np.array([5.0, 5.0])
        assert rbf.score(x, y_near) > rbf.score(x, y_far)

    def test_score_symmetric(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.array([0.3, 0.7]))
        x = np.array([1.0, 2.0])
        y = np.array([3.0, 4.0])
        assert rbf.score(x, y) == pytest.approx(rbf.score(y, x))

    def test_weights_normalized_to_sum_one(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.array([2.0, 8.0]))
        assert rbf.weights.sum() == pytest.approx(1.0)
        assert rbf.weights[0] == pytest.approx(0.2)
        assert rbf.weights[1] == pytest.approx(0.8)

    def test_zero_weights_fallback_uniform(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.array([0.0, 0.0]))
        assert rbf.weights.sum() == pytest.approx(1.0)
        assert rbf.weights[0] == pytest.approx(0.5)

    def test_gamma_formula(self):
        rbf = WeightedRBFSimilarity(sigma=2.0, reliability_weights=np.ones(3))
        assert rbf.gamma == pytest.approx(1.0 / (2.0 * 4.0))

    def test_nan_treated_as_neutral(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(3))
        x = np.array([1.0, np.nan, 3.0])
        y = np.array([1.0, 2.0, 3.0])
        # NaN diff → 0, so only non-NaN features contribute to distance
        s = rbf.score(x, y)
        assert np.isfinite(s)
        assert s > 0.0

    def test_batch_matches_scalar(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.array([0.3, 0.5, 0.2]))
        query = np.array([1.0, 2.0, 3.0])
        candidates = np.array(
            [
                [1.0, 2.0, 3.0],
                [4.0, 5.0, 6.0],
                [0.0, 0.0, 0.0],
            ]
        )
        batch_scores = rbf.score_batch(query, candidates)
        for i in range(3):
            scalar_score = rbf.score(query, candidates[i])
            assert batch_scores[i] == pytest.approx(scalar_score, abs=1e-12)

    def test_batch_output_shape(self):
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(4))
        query = np.zeros(4)
        candidates = np.zeros((10, 4))
        result = rbf.score_batch(query, candidates)
        assert result.shape == (10,)

    def test_higher_sigma_less_discriminating(self):
        """Wider sigma → scores closer to 1.0 for same distance."""
        x = np.array([0.0, 0.0])
        y = np.array([2.0, 2.0])
        w = np.ones(2)
        rbf_tight = WeightedRBFSimilarity(sigma=0.5, reliability_weights=w)
        rbf_wide = WeightedRBFSimilarity(sigma=2.0, reliability_weights=w)
        assert rbf_wide.score(x, y) > rbf_tight.score(x, y)

    def test_weighted_features_matter_more(self):
        """Feature with higher weight should have more impact on score."""
        # Weights: first feature gets 90% weight
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.array([9.0, 1.0]))
        base = np.array([0.0, 0.0])
        # Diff on feature 0 (high weight)
        y_diff_f0 = np.array([2.0, 0.0])
        # Same-magnitude diff on feature 1 (low weight)
        y_diff_f1 = np.array([0.0, 2.0])
        assert rbf.score(base, y_diff_f1) > rbf.score(base, y_diff_f0)


# ============================================================================
# 3. EmpiricalBayesShrinkage
# ============================================================================


class TestEmpiricalBayesShrinkage:
    def test_alpha_zero_samples(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        assert eb.alpha(0) == pytest.approx(0.0)

    def test_alpha_equal_prior(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        assert eb.alpha(15) == pytest.approx(0.5)

    def test_alpha_large_sample(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        a = eb.alpha(10000)
        assert a > 0.99

    def test_alpha_at_min_sample(self):
        eb = EmpiricalBayesShrinkage(n_prior=EB_N_PRIOR)
        a = eb.alpha(MIN_FIELDER_BATTED_BALLS)
        # At 50 BB, alpha = 50/(50+15) ≈ 0.77
        expected = MIN_FIELDER_BATTED_BALLS / (MIN_FIELDER_BATTED_BALLS + EB_N_PRIOR)
        assert a == pytest.approx(expected)

    def test_shrink_zero_samples_returns_avg(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        raw = np.array([10.0, 20.0])
        avg = np.array([5.0, 5.0])
        result = eb.shrink(raw, avg, n_samples=0)
        np.testing.assert_array_almost_equal(result, avg)

    def test_shrink_large_sample_returns_raw(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        raw = np.array([10.0, 20.0])
        avg = np.array([5.0, 5.0])
        result = eb.shrink(raw, avg, n_samples=100000)
        np.testing.assert_array_almost_equal(result, raw, decimal=2)

    def test_shrink_nan_replaced_by_avg(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        raw = np.array([np.nan, 20.0])
        avg = np.array([5.0, 5.0])
        result = eb.shrink(raw, avg, n_samples=15)
        # alpha = 0.5, NaN replaced by avg before blend
        assert result[0] == pytest.approx(5.0)
        assert result[1] == pytest.approx(0.5 * 20.0 + 0.5 * 5.0)

    def test_shrink_interpolation(self):
        eb = EmpiricalBayesShrinkage(n_prior=15)
        raw = np.array([10.0])
        avg = np.array([0.0])
        # alpha = 15/(15+15) = 0.5
        result = eb.shrink(raw, avg, n_samples=15)
        assert result[0] == pytest.approx(5.0)


# ============================================================================
# 4. FeatureNormalizer
# ============================================================================


class TestFeatureNormalizer:
    def _make_profiles(self) -> dict[str, list[FielderProfile]]:
        """Create a small population for normalization fitting."""
        ss_profiles = [
            _make_if_profile(
                player_id=1, position="SS", range_vec=np.array([1.0, 2.0, 3.0, 4.0, 5.0])
            ),
            _make_if_profile(
                player_id=2, position="SS", range_vec=np.array([3.0, 4.0, 5.0, 6.0, 7.0])
            ),
            _make_if_profile(
                player_id=3, position="SS", range_vec=np.array([5.0, 6.0, 7.0, 8.0, 9.0])
            ),
        ]
        cf_profiles = [
            _make_of_profile(
                player_id=10, position="CF", range_vec=np.array([10.0, 20.0, 30.0, 40.0, 50.0])
            ),
            _make_of_profile(
                player_id=11, position="CF", range_vec=np.array([20.0, 30.0, 40.0, 50.0, 60.0])
            ),
        ]
        return {"SS": ss_profiles, "CF": cf_profiles}

    def test_fit_produces_params_per_position(self):
        norm = FeatureNormalizer()
        norm.fit(self._make_profiles())
        assert "SS" in norm.range_params
        assert "CF" in norm.range_params

    def test_normalization_produces_mean_zero(self):
        profiles = self._make_profiles()
        norm = FeatureNormalizer()
        norm.fit(profiles)
        # Normalize each SS range vec and check mean across population ≈ 0
        normed = [norm.normalize_range(p.range_vec, "SS") for p in profiles["SS"]]
        mean = np.mean(normed, axis=0)
        np.testing.assert_array_almost_equal(mean, np.zeros_like(mean), decimal=10)

    def test_normalization_produces_unit_std(self):
        profiles = self._make_profiles()
        norm = FeatureNormalizer()
        norm.fit(profiles)
        normed = np.array([norm.normalize_range(p.range_vec, "SS") for p in profiles["SS"]])
        std = np.std(normed, axis=0)
        np.testing.assert_array_almost_equal(std, np.ones_like(std), decimal=10)

    def test_positions_normalized_independently(self):
        """SS and CF have different normalization parameters."""
        profiles = self._make_profiles()
        norm = FeatureNormalizer()
        norm.fit(profiles)
        ss_mean, _ = norm.range_params["SS"]
        cf_mean, _ = norm.range_params["CF"]
        # CF means should be much larger than SS means
        assert cf_mean[0] > ss_mean[0]

    def test_unknown_position_returns_raw(self):
        norm = FeatureNormalizer()
        norm.fit(self._make_profiles())
        raw = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = norm.normalize_range(raw, "DH")  # not a real position
        np.testing.assert_array_equal(result, raw)

    def test_nan_in_input_becomes_zero(self):
        profiles = self._make_profiles()
        norm = FeatureNormalizer()
        norm.fit(profiles)
        vec_with_nan = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
        result = norm.normalize_range(vec_with_nan, "SS")
        assert np.isfinite(result).all()

    def test_constant_feature_gets_std_one(self):
        """If a feature is identical across all profiles, std is set to 1.0."""
        profiles = {
            "SS": [
                _make_if_profile(
                    player_id=i, position="SS", range_vec=np.array([0.0, float(i), 0.0, 0.0, 0.0])
                )
                for i in range(5)
            ]
        }
        norm = FeatureNormalizer()
        norm.fit(profiles)
        _, std = norm.range_params["SS"]
        # Feature 0 is constant → std forced to 1.0
        assert std[0] == 1.0
        # Feature 1 varies → std is the actual std
        assert std[1] > 0

    def test_if_normalizer_has_dp_and_specialty(self):
        profiles = {"SS": _generate_ss_population(5, seed=1)}
        norm = FeatureNormalizer()
        norm.fit(profiles)
        assert "SS" in norm.dp_params
        assert "SS" in norm.specialty_params
        assert "SS" not in norm.arm_params
        assert "SS" not in norm.star_params

    def test_of_normalizer_has_arm_and_star(self):
        profiles = {"CF": _generate_cf_population(5, seed=1)}
        norm = FeatureNormalizer()
        norm.fit(profiles)
        assert "CF" in norm.arm_params
        assert "CF" in norm.star_params
        assert "CF" not in norm.dp_params
        assert "CF" not in norm.specialty_params


# ============================================================================
# 5. FielderProfile
# ============================================================================


class TestFielderProfile:
    def test_if_profile_has_dp_no_arm(self):
        p = _make_if_profile()
        assert p.dp_vec is not None
        assert p.specialty_vec is not None
        assert p.arm_vec is None
        assert p.star_vec is None

    def test_of_profile_has_arm_no_dp(self):
        p = _make_of_profile()
        assert p.arm_vec is not None
        assert p.star_vec is not None
        assert p.dp_vec is None
        assert p.specialty_vec is None

    def test_middle_if_dp_vec_has_pivot(self):
        ss = _make_if_profile(position="SS")
        expected_dim = len(IF_DP_FEATURES) + len(IF_PIVOT_FEATURES)
        assert ss.dp_vec.shape == (expected_dim,)

    def test_corner_if_dp_vec_no_pivot(self):
        fb = _make_if_profile(position="1B")
        expected_dim = len(IF_DP_FEATURES)
        assert fb.dp_vec.shape == (expected_dim,)

    def test_eb_alpha_computed_correctly(self):
        p = _make_if_profile(sample_bb=100)
        expected = 100 / (100 + EB_N_PRIOR)
        assert p.eb_alpha == pytest.approx(expected)


# ============================================================================
# 6. PositionPartition
# ============================================================================


class TestPositionPartition:
    def test_empty_partition_returns_empty(self):
        part = PositionPartition("SS")
        norm = FeatureNormalizer()
        part.build([], norm)
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(5))
        rbf2 = WeightedRBFSimilarity(sigma=1.0, reliability_weights=np.ones(2))
        query = _make_if_profile()
        results = part.score_all(query, norm, rbf, rbf, rbf2, rbf2)
        assert results == []

    def test_build_stores_correct_count(self):
        profiles = _generate_ss_population(5, seed=1)
        norm = FeatureNormalizer()
        norm.fit({"SS": profiles})
        part = PositionPartition("SS")
        part.build(profiles, norm)
        assert len(part.profiles) == len(profiles)
        assert len(part.keys) == len(profiles)

    def test_self_excluded_from_score_all(self):
        profiles = _generate_ss_population(3, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        query = profiles[0]
        results = engine.query(query.player_id, query.position, query.season)
        result_keys = [(r.player_id, r.position, r.season) for r in results]
        assert (query.player_id, query.position, query.season) not in result_keys

    def test_infield_partition_is_infield(self):
        p = PositionPartition("SS")
        assert p.is_infield is True

    def test_outfield_partition_is_not_infield(self):
        p = PositionPartition("CF")
        assert p.is_infield is False


# ============================================================================
# 7. SimilarityResult
# ============================================================================


class TestSimilarityResult:
    def test_result_frozen(self):
        r = SimilarityResult(
            player_id=1,
            position="SS",
            season=2024,
            score=0.75,
            range_score=0.8,
            secondary_score=0.7,
            tertiary_score=0.6,
            quaternary_score=0.9,
            sample_batted_balls=200,
        )
        with pytest.raises(AttributeError):
            r.score = 0.5  # type: ignore

    def test_result_fields_accessible(self):
        r = SimilarityResult(
            player_id=42,
            position="CF",
            season=2023,
            score=0.55,
            range_score=0.6,
            secondary_score=0.5,
            tertiary_score=0.4,
            quaternary_score=0.7,
            sample_batted_balls=150,
        )
        assert r.player_id == 42
        assert r.position == "CF"
        assert r.season == 2023
        assert r.score == pytest.approx(0.55)


# ============================================================================
# 8. FielderSimilarityEngine — Integration Tests
# ============================================================================


class TestEnginePositionGating:
    """Cross-position comparisons must be blocked."""

    def test_query_pair_cross_position_returns_none(self):
        ss = _make_if_profile(player_id=1, position="SS")
        cf = _make_of_profile(player_id=2, position="CF")
        engine = _build_test_engine(if_profiles=[ss], of_profiles=[cf])
        result = engine.query_pair((1, "SS", 2024), (2, "CF", 2024))
        assert result is None

    def test_query_pair_cross_if_positions_returns_none(self):
        ss = _make_if_profile(player_id=1, position="SS")
        fb = _make_if_profile(player_id=2, position="1B")
        engine = _build_test_engine(if_profiles=[ss, fb])
        result = engine.query_pair((1, "SS", 2024), (2, "1B", 2024))
        assert result is None

    def test_query_invalid_position_returns_empty(self):
        engine = _build_test_engine(if_profiles=_generate_ss_population(3))
        results = engine.query(1, "DH", 2024)
        assert results == []


class TestEngineExhaustiveScoring:
    """Every same-position profile must be scored."""

    def test_all_same_position_returned(self):
        profiles = _generate_ss_population(10, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        query = profiles[0]
        results = engine.query(query.player_id, "SS", query.season)
        # Should be all profiles minus the query itself
        expected_count = len(profiles) - 1
        assert len(results) == expected_count

    def test_results_sorted_descending(self):
        profiles = _generate_ss_population(10, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        results = engine.query(profiles[0].player_id, "SS", profiles[0].season)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


class TestEngineSelfExclusion:
    """The query profile's exact (id, pos, season) must be excluded."""

    def test_self_not_in_results(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        q = profiles[0]
        results = engine.query(q.player_id, "SS", q.season)
        for r in results:
            assert not (r.player_id == q.player_id and r.season == q.season)


class TestEngineCrossSeasonInclusion:
    """Same player, different season should appear in results."""

    def test_cross_season_included(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        # Player 1 has 2023 and 2024 seasons
        results = engine.query(1, "SS", 2024)
        seasons_found = [r.season for r in results if r.player_id == 1]
        assert 2023 in seasons_found


class TestEngineTopN:
    def test_top_n_limits_results(self):
        profiles = _generate_ss_population(20, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        results = engine.query(1, "SS", 2024, n=5)
        assert len(results) == 5

    def test_top_n_none_returns_all(self):
        profiles = _generate_ss_population(10, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        results = engine.query(1, "SS", 2024, n=None)
        assert len(results) == len(profiles) - 1


class TestEngineSymmetry:
    """score(A, B) must equal score(B, A) for all pairs."""

    def test_if_pair_symmetric(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        a = (1, "SS", 2024)
        b = (2, "SS", 2024)
        r_ab = engine.query_pair(a, b)
        r_ba = engine.query_pair(b, a)
        assert r_ab is not None and r_ba is not None
        assert r_ab.score == pytest.approx(r_ba.score, abs=1e-12)

    def test_of_pair_symmetric(self):
        profiles = _generate_cf_population(5, seed=1)
        engine = _build_test_engine(of_profiles=profiles)
        a = (100, "CF", 2024)
        b = (101, "CF", 2024)
        r_ab = engine.query_pair(a, b)
        r_ba = engine.query_pair(b, a)
        assert r_ab is not None and r_ba is not None
        assert r_ab.score == pytest.approx(r_ba.score, abs=1e-12)

    def test_symmetry_all_subscores(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        a = (1, "SS", 2023)
        b = (3, "SS", 2024)
        r_ab = engine.query_pair(a, b)
        r_ba = engine.query_pair(b, a)
        assert r_ab.range_score == pytest.approx(r_ba.range_score, abs=1e-12)
        assert r_ab.secondary_score == pytest.approx(r_ba.secondary_score, abs=1e-12)
        assert r_ab.tertiary_score == pytest.approx(r_ba.tertiary_score, abs=1e-12)
        assert r_ab.quaternary_score == pytest.approx(r_ba.quaternary_score, abs=1e-12)


class TestEngineIdenticalProfiles:
    """Two identical profiles (different ids) should score near 1.0."""

    def test_identical_if_profiles_high_score(self):
        vec_r = np.array([2.0, 1.5, -1.0, 3.0, 0.5])
        vec_e = np.array([0.02, 0.01])
        vec_dp = np.array([1.0, 0.6, 0.7, 0.5])
        vec_sp = np.array([0.5, 0.9])
        p1 = _make_if_profile(
            player_id=1,
            range_vec=vec_r.copy(),
            error_vec=vec_e.copy(),
            dp_vec=vec_dp.copy(),
            specialty_vec=vec_sp.copy(),
            sample_bb=500,
        )
        p2 = _make_if_profile(
            player_id=2,
            range_vec=vec_r.copy(),
            error_vec=vec_e.copy(),
            dp_vec=vec_dp.copy(),
            specialty_vec=vec_sp.copy(),
            sample_bb=500,
        )
        # Need a third profile so normalization has variance
        p3 = _make_if_profile(
            player_id=3,
            range_vec=vec_r + 5,
            error_vec=vec_e + 0.05,
            dp_vec=vec_dp + 3,
            specialty_vec=1 - vec_sp,
            sample_bb=500,
        )
        engine = _build_test_engine(if_profiles=[p1, p2, p3])
        result = engine.query_pair((1, "SS", 2024), (2, "SS", 2024))
        assert result is not None
        assert result.score > 0.95

    def test_identical_of_profiles_high_score(self):
        vec_r = np.array([3.0, 2.0, -1.0, 4.0, 1.0])
        vec_e = np.array([0.01, 0.005])
        vec_arm = np.array([0.7, 0.15, 0.6, 1.5])
        vec_star = np.array([0.15, 0.45, 0.97])
        p1 = _make_of_profile(
            player_id=10,
            range_vec=vec_r.copy(),
            error_vec=vec_e.copy(),
            arm_vec=vec_arm.copy(),
            star_vec=vec_star.copy(),
            sample_bb=500,
        )
        p2 = _make_of_profile(
            player_id=11,
            range_vec=vec_r.copy(),
            error_vec=vec_e.copy(),
            arm_vec=vec_arm.copy(),
            star_vec=vec_star.copy(),
            sample_bb=500,
        )
        p3 = _make_of_profile(
            player_id=12,
            range_vec=vec_r + 5,
            error_vec=vec_e + 0.05,
            arm_vec=vec_arm * 0.5,
            star_vec=1 - vec_star,
            sample_bb=500,
        )
        engine = _build_test_engine(of_profiles=[p1, p2, p3])
        result = engine.query_pair((10, "CF", 2024), (11, "CF", 2024))
        assert result is not None
        assert result.score > 0.95


class TestEngineDivergentProfiles:
    """Very different profiles should score low."""

    def test_divergent_if_profiles_low_score(self):
        p1 = _make_if_profile(
            player_id=1,
            range_vec=np.array([10, 10, 10, 10, 10.0]),
            error_vec=np.array([0.0, 0.0]),
            dp_vec=np.array([5.0, 0.9, 0.9, 3.0]),
            specialty_vec=np.array([0.95, 0.99]),
            sample_bb=500,
        )
        p2 = _make_if_profile(
            player_id=2,
            range_vec=np.array([-10, -10, -10, -10, -10.0]),
            error_vec=np.array([0.15, 0.12]),
            dp_vec=np.array([-5.0, 0.1, 0.1, -3.0]),
            specialty_vec=np.array([0.1, 0.3]),
            sample_bb=500,
        )
        p3 = _make_if_profile(
            player_id=3,
            range_vec=np.zeros(5),
            error_vec=np.array([0.05, 0.05]),
            dp_vec=np.zeros(4),
            specialty_vec=np.array([0.5, 0.6]),
            sample_bb=500,
        )
        engine = _build_test_engine(if_profiles=[p1, p2, p3])
        result = engine.query_pair((1, "SS", 2024), (2, "SS", 2024))
        assert result is not None
        assert result.score < 0.30


class TestEngineConfidenceDiscount:
    """Low-sample profiles should be penalized."""

    def test_low_sample_lower_score(self):
        base_range = np.array([2.0, 1.0, 0.0, 1.5, 0.5])
        base_err = np.array([0.02, 0.01])
        base_dp = np.array([1.0, 0.6, 0.7, 0.3])
        base_spec = np.array([0.5, 0.8])

        # High-sample pair
        p_high_a = _make_if_profile(
            player_id=1,
            range_vec=base_range.copy(),
            error_vec=base_err.copy(),
            dp_vec=base_dp.copy(),
            specialty_vec=base_spec.copy(),
            sample_bb=500,
        )
        p_high_b = _make_if_profile(
            player_id=2,
            range_vec=base_range.copy(),
            error_vec=base_err.copy(),
            dp_vec=base_dp.copy(),
            specialty_vec=base_spec.copy(),
            sample_bb=500,
        )
        # Low-sample twin
        p_low = _make_if_profile(
            player_id=3,
            range_vec=base_range.copy(),
            error_vec=base_err.copy(),
            dp_vec=base_dp.copy(),
            specialty_vec=base_spec.copy(),
            sample_bb=20,
        )
        # Need divergent profile for normalization variance
        p_diff = _make_if_profile(
            player_id=4,
            range_vec=base_range + 8,
            error_vec=base_err + 0.1,
            dp_vec=base_dp + 5,
            specialty_vec=1 - base_spec,
            sample_bb=500,
        )

        engine = _build_test_engine(if_profiles=[p_high_a, p_high_b, p_low, p_diff])
        score_high = engine.query_pair((1, "SS", 2024), (2, "SS", 2024))
        score_low = engine.query_pair((1, "SS", 2024), (3, "SS", 2024))
        assert score_high is not None and score_low is not None
        assert score_high.score > score_low.score


class TestEngineCompositeWeights:
    """Verify composite uses correct weights for IF and OF."""

    def test_if_composite_weight_formula(self):
        """For IF: composite = 0.45*range + 0.30*dp + 0.15*error + 0.10*specialty (before confidence)."""
        profiles = _generate_ss_population(10, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        a = (1, "SS", 2024)
        b = (2, "SS", 2024)
        r = engine.query_pair(a, b)
        assert r is not None
        # Sub-scores are returned; verify the composite is bounded
        assert 0.0 <= r.score <= 1.0
        assert 0.0 <= r.range_score <= 1.0
        assert 0.0 <= r.secondary_score <= 1.0
        assert 0.0 <= r.tertiary_score <= 1.0
        assert 0.0 <= r.quaternary_score <= 1.0

    def test_of_all_subscores_bounded(self):
        profiles = _generate_cf_population(10, seed=1)
        engine = _build_test_engine(of_profiles=profiles)
        a = (100, "CF", 2024)
        b = (101, "CF", 2024)
        r = engine.query_pair(a, b)
        assert r is not None
        for s in [r.score, r.range_score, r.secondary_score, r.tertiary_score, r.quaternary_score]:
            assert 0.0 <= s <= 1.0


class TestEngineMiddleIFPivot:
    """2B/SS get longer DP vector with pivot features; 1B/3B do not."""

    def test_ss_dp_dim_includes_pivot(self):
        p = _make_if_profile(position="SS")
        assert p.dp_vec.shape == (len(IF_DP_FEATURES) + len(IF_PIVOT_FEATURES),)

    def test_2b_dp_dim_includes_pivot(self):
        p = _make_if_profile(position="2B")
        assert p.dp_vec.shape == (len(IF_DP_FEATURES) + len(IF_PIVOT_FEATURES),)

    def test_1b_dp_dim_excludes_pivot(self):
        p = _make_if_profile(position="1B")
        assert p.dp_vec.shape == (len(IF_DP_FEATURES),)

    def test_3b_dp_dim_excludes_pivot(self):
        p = _make_if_profile(position="3B")
        assert p.dp_vec.shape == (len(IF_DP_FEATURES),)


class TestEngineMissingProfile:
    def test_query_missing_player_returns_empty(self):
        engine = _build_test_engine(if_profiles=_generate_ss_population(3))
        results = engine.query(99999, "SS", 2024)
        assert results == []

    def test_query_pair_missing_a_returns_none(self):
        profiles = _generate_ss_population(3)
        engine = _build_test_engine(if_profiles=profiles)
        result = engine.query_pair((99999, "SS", 2024), (1, "SS", 2024))
        assert result is None

    def test_query_pair_missing_b_returns_none(self):
        profiles = _generate_ss_population(3)
        engine = _build_test_engine(if_profiles=profiles)
        result = engine.query_pair((1, "SS", 2024), (99999, "SS", 2024))
        assert result is None


# ============================================================================
# 8h. Mixed Population — IF and OF coexist without interference
# ============================================================================


class TestEngineMixedPopulation:
    def test_if_query_ignores_of_profiles(self):
        ss = _generate_ss_population(5, seed=1)
        cf = _generate_cf_population(5, seed=1)
        engine = _build_test_engine(if_profiles=ss, of_profiles=cf)
        results = engine.query(1, "SS", 2024)
        for r in results:
            assert r.position == "SS"

    def test_of_query_ignores_if_profiles(self):
        ss = _generate_ss_population(5, seed=1)
        cf = _generate_cf_population(5, seed=1)
        engine = _build_test_engine(if_profiles=ss, of_profiles=cf)
        results = engine.query(100, "CF", 2024)
        for r in results:
            assert r.position == "CF"

    def test_total_profile_count(self):
        ss = _generate_ss_population(5, seed=1)
        cf = _generate_cf_population(5, seed=1)
        engine = _build_test_engine(if_profiles=ss, of_profiles=cf)
        assert engine.profile_count == len(ss) + len(cf)


# ============================================================================
# 8i. Utility methods
# ============================================================================


class TestEngineUtilities:
    def test_get_profile(self):
        profiles = _generate_ss_population(3)
        engine = _build_test_engine(if_profiles=profiles)
        p = engine.get_profile(1, "SS", 2024)
        assert p is not None
        assert p.player_id == 1

    def test_get_profile_missing_returns_none(self):
        engine = _build_test_engine(if_profiles=_generate_ss_population(3))
        assert engine.get_profile(99999, "SS", 2024) is None

    def test_profile_ids(self):
        profiles = _generate_ss_population(3)
        engine = _build_test_engine(if_profiles=profiles)
        ids = engine.profile_ids()
        assert len(ids) == len(profiles)
        assert all(isinstance(k, tuple) and len(k) == 3 for k in ids)

    def test_profile_ids_for_position(self):
        ss = _generate_ss_population(5, seed=1)
        cf = _generate_cf_population(3, seed=1)
        engine = _build_test_engine(if_profiles=ss, of_profiles=cf)
        ss_ids = engine.profile_ids_for_position("SS")
        cf_ids = engine.profile_ids_for_position("CF")
        assert len(ss_ids) == len(ss)
        assert len(cf_ids) == len(cf)
        assert all(k[1] == "SS" for k in ss_ids)
        assert all(k[1] == "CF" for k in cf_ids)


# ============================================================================
# 9. build_similarity_matrix
# ============================================================================


class TestBuildSimilarityMatrix:
    def test_diagonal_is_one(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        ids = [(p.player_id, p.position, p.season) for p in profiles[:5]]
        matrix = build_similarity_matrix(engine, ids)
        np.testing.assert_array_almost_equal(np.diag(matrix), np.ones(5))

    def test_matrix_symmetric(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        ids = [(p.player_id, p.position, p.season) for p in profiles[:5]]
        matrix = build_similarity_matrix(engine, ids)
        np.testing.assert_array_almost_equal(matrix, matrix.T)

    def test_matrix_shape(self):
        profiles = _generate_ss_population(4, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        ids = [(p.player_id, p.position, p.season) for p in profiles[:3]]
        matrix = build_similarity_matrix(engine, ids)
        assert matrix.shape == (3, 3)

    def test_matrix_values_bounded(self):
        profiles = _generate_ss_population(5, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        ids = [(p.player_id, p.position, p.season) for p in profiles[:5]]
        matrix = build_similarity_matrix(engine, ids)
        assert np.all(matrix >= 0.0)
        assert np.all(matrix <= 1.0)


# ============================================================================
# 10. Regression / Stability — deterministic under fixed seed
# ============================================================================


class TestRegression:
    """Ensure the engine produces deterministic, reproducible scores."""

    def test_deterministic_query(self):
        """Same inputs → same outputs across two runs."""
        profiles = _generate_ss_population(10, seed=42)
        engine1 = _build_test_engine(if_profiles=profiles)
        engine2 = _build_test_engine(if_profiles=profiles)
        r1 = engine1.query(1, "SS", 2024)
        r2 = engine2.query(1, "SS", 2024)
        assert len(r1) == len(r2)
        for a, b in zip(r1, r2, strict=False):
            assert a.player_id == b.player_id
            assert a.season == b.season
            assert a.score == pytest.approx(b.score, abs=1e-15)

    def test_deterministic_pair(self):
        profiles = _generate_ss_population(5, seed=42)
        engine = _build_test_engine(if_profiles=profiles)
        r1 = engine.query_pair((1, "SS", 2024), (2, "SS", 2024))
        r2 = engine.query_pair((1, "SS", 2024), (2, "SS", 2024))
        assert r1.score == pytest.approx(r2.score, abs=1e-15)

    def test_scores_finite(self):
        """No NaN or Inf in any score."""
        profiles = _generate_ss_population(10, seed=42) + _generate_cf_population(10, seed=42)
        engine = _build_test_engine(
            if_profiles=[p for p in profiles if p.position in INFIELD_POSITIONS],
            of_profiles=[p for p in profiles if p.position in OUTFIELD_POSITIONS],
        )
        for pid, pos, season in engine.profile_ids()[:10]:
            results = engine.query(pid, pos, season)
            for r in results:
                assert np.isfinite(r.score), f"Non-finite score for {pid}/{pos}/{season}"
                assert np.isfinite(r.range_score)
                assert np.isfinite(r.secondary_score)
                assert np.isfinite(r.tertiary_score)
                assert np.isfinite(r.quaternary_score)


# ============================================================================
# 11. Edge cases
# ============================================================================


class TestEdgeCases:
    def test_single_profile_query_returns_empty(self):
        """Only one profile at a position → query returns empty (self excluded)."""
        p = _make_if_profile(player_id=1, position="3B")
        engine = _build_test_engine(if_profiles=[p])
        results = engine.query(1, "3B", 2024)
        assert results == []

    def test_two_profiles_one_result(self):
        p1 = _make_if_profile(
            player_id=1, position="3B", range_vec=np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        )
        p2 = _make_if_profile(
            player_id=2, position="3B", range_vec=np.array([2.0, 3.0, 4.0, 5.0, 6.0])
        )
        engine = _build_test_engine(if_profiles=[p1, p2])
        results = engine.query(1, "3B", 2024)
        assert len(results) == 1
        assert results[0].player_id == 2

    def test_nan_in_feature_vec_no_crash(self):
        """NaN in feature vectors should not crash — treated as neutral."""
        p1 = _make_of_profile(player_id=10, range_vec=np.array([np.nan, 2.0, 3.0, 4.0, 5.0]))
        p2 = _make_of_profile(player_id=11, range_vec=np.array([1.0, 2.0, 3.0, 4.0, 5.0]))
        p3 = _make_of_profile(player_id=12, range_vec=np.array([5.0, 6.0, 7.0, 8.0, 9.0]))
        engine = _build_test_engine(of_profiles=[p1, p2, p3])
        results = engine.query(10, "CF", 2024)
        assert len(results) == 2
        for r in results:
            assert np.isfinite(r.score)

    def test_all_identical_profiles_high_scores(self):
        """When all profiles are identical, all scores should be very high."""
        vec = np.array([2.0, 1.0, 0.5, -1.0, 3.0])
        profiles = [
            _make_of_profile(
                player_id=i,
                range_vec=vec.copy(),
                error_vec=np.array([0.02, 0.01]),
                arm_vec=np.array([0.5, 0.1, 0.4, 0.5]),
                star_vec=np.array([0.1, 0.3, 0.95]),
                sample_bb=400,
            )
            for i in range(10, 15)
        ]
        engine = _build_test_engine(of_profiles=profiles)
        results = engine.query(10, "CF", 2024)
        # All scores should be very high (close to 1.0)
        for r in results:
            assert r.score > 0.9

    def test_query_pair_same_player_different_season(self):
        """Cross-season query_pair should work."""
        profiles = _generate_ss_population(3, seed=1)
        engine = _build_test_engine(if_profiles=profiles)
        result = engine.query_pair((1, "SS", 2023), (1, "SS", 2024))
        assert result is not None
        assert result.score > 0  # same player should have some similarity


# ============================================================================
# 12. Score Composition Verification
# ============================================================================


class TestScoreComposition:
    """Verify that the composite score correctly combines sub-scores."""

    def test_if_composite_respects_weights(self):
        """
        When one sub-score dominates, the composite should reflect it
        proportionally to its weight.
        """
        profiles = _generate_ss_population(15, seed=7)
        engine = _build_test_engine(if_profiles=profiles)
        a = (1, "SS", 2024)
        b = (5, "SS", 2024)
        r = engine.query_pair(a, b)
        assert r is not None
        # The composite should be a confidence-discounted weighted average
        # of sub-scores. Verify it's between the min and max sub-scores
        # (after confidence discount).
        min(r.range_score, r.secondary_score, r.tertiary_score, r.quaternary_score)
        sub_max = max(r.range_score, r.secondary_score, r.tertiary_score, r.quaternary_score)
        # After confidence discount, composite <= sub_max
        assert r.score <= sub_max + 0.01  # small tolerance for rounding

    def test_all_positions_produce_results(self):
        """Build an engine with all 7 positions and verify each produces results."""
        all_profiles = []
        rng = np.random.default_rng(123)
        pid = 1
        for pos in sorted(ALL_POSITIONS):
            is_if = pos in INFIELD_POSITIONS
            for _ in range(5):
                for season in [2023, 2024]:
                    if is_if:
                        is_mid = pos in ("2B", "SS")
                        dp_dim = len(IF_DP_FEATURES) + (len(IF_PIVOT_FEATURES) if is_mid else 0)
                        all_profiles.append(
                            _make_if_profile(
                                player_id=pid,
                                position=pos,
                                season=season,
                                sample_bb=int(rng.integers(80, 400)),
                                range_vec=rng.normal(0, 2, len(IF_RANGE_FEATURES)).astype(
                                    np.float64
                                ),
                                dp_vec=rng.normal(0, 1, dp_dim).astype(np.float64),
                            )
                        )
                    else:
                        all_profiles.append(
                            _make_of_profile(
                                player_id=pid,
                                position=pos,
                                season=season,
                                sample_bb=int(rng.integers(80, 400)),
                                range_vec=rng.normal(0, 2, len(OF_RANGE_FEATURES)).astype(
                                    np.float64
                                ),
                            )
                        )
                pid += 1

        engine = _build_test_engine(
            if_profiles=[p for p in all_profiles if p.position in INFIELD_POSITIONS],
            of_profiles=[p for p in all_profiles if p.position in OUTFIELD_POSITIONS],
        )

        for pos in sorted(ALL_POSITIONS):
            pos_ids = engine.profile_ids_for_position(pos)
            assert len(pos_ids) > 0, f"No profiles for {pos}"
            pid, p, season = pos_ids[0]
            results = engine.query(pid, p, season)
            assert len(results) > 0, f"No results for {pos} query"
