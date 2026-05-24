"""
test_ml_engines_sim322.py
=========================
SIM-322 (P0/P1 LIVE BUG) -- GMM arsenal-similarity covariance
double-standardization in the Wasserstein-2 (Bures) distance.

ROOT CAUSE
----------
The nightly computor (``player_profile_computor._fit_gmm_for_pitcher``) stores
each GMM component's ``mean`` in ORIGINAL feature units but its ``covariance``
in PER-PITCHER STANDARDIZED space::

    cov_std = D_feat^-1 @ cov_orig @ D_feat^-1,   D_feat = diag(feature_stds)

``GMMModel.from_json`` used to load ``mean`` (original) alongside ``covariance``
(standardized), leaving the two on inconsistent scales.  Then
``standardize_gmm`` standardizes BOTH from ORIGINAL units (``z_mean`` and
``z_cov = D^-1 Sigma D^-1``), so the covariance got standardized TWICE while the
mean got standardized ONCE -- corrupting every arsenal W2 distance.

FIX (engine-side, no nightly recompute)
---------------------------------------
``from_json`` now de-standardizes the stored covariance back to ORIGINAL units::

    cov_orig = D_feat @ cov_std @ D_feat

so the in-memory component is internally consistent (mean and cov both in
original units) and ``standardize_gmm`` applies ONE consistent standardization
to both.

These tests use the in-memory ``__new__``-bypass / direct-dataclass-construction
idioms used by the existing pitcher-engine tests (no live DuckDB).
"""

from __future__ import annotations

import unittest

import numpy as np

from similarity.engines.pitcher_similarity import (
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
    ArsenalSimilarity,
    GMMComponent,
    GMMModel,
    standardize_gmm,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bures_sq_closed_form_diag(m1, s1_diag, m2, s2_diag) -> float:
    """Closed-form squared Bures-Wasserstein distance for DIAGONAL covariances.

    For N(m1, diag(s1^2)) and N(m2, diag(s2^2)) the Bures term is separable:

        W2^2 = ||m1 - m2||^2 + sum_k (s1_k - s2_k)^2

    where s_k is the per-feature standard deviation (sqrt of the variance).
    """
    m1 = np.asarray(m1, dtype=np.float64)
    m2 = np.asarray(m2, dtype=np.float64)
    s1 = np.sqrt(np.asarray(s1_diag, dtype=np.float64))
    s2 = np.sqrt(np.asarray(s2_diag, dtype=np.float64))
    return float(np.sum((m1 - m2) ** 2) + np.sum((s1 - s2) ** 2))


def _make_component(cid, weight, mean, cov, n_pitches=200) -> GMMComponent:
    return GMMComponent(
        component_id=cid,
        weight=float(weight),
        mean=np.asarray(mean, dtype=np.float64),
        covariance=np.asarray(cov, dtype=np.float64),
        n_pitches=n_pitches,
    )


def _make_gmm(components, feature_means=None, feature_stds=None) -> GMMModel:
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=(
            np.zeros(GMM_FEATURE_DIM) if feature_means is None else np.asarray(feature_means)
        ),
        feature_stds=(
            np.ones(GMM_FEATURE_DIM) if feature_stds is None else np.asarray(feature_stds)
        ),
        components=components,
    )


def _build_model_json(mean_orig, cov_orig, feature_stds, feature_means=None):
    """Build a model-JSON blob exactly as the nightly computor stores it:

    - ``mean``       : ORIGINAL units
    - ``covariance`` : STANDARDIZED via feature_stds (cov_std = D^-1 cov_orig D^-1)
    """
    mean_orig = np.asarray(mean_orig, dtype=np.float64)
    cov_orig = np.asarray(cov_orig, dtype=np.float64)
    feature_stds = np.asarray(feature_stds, dtype=np.float64)
    if feature_means is None:
        feature_means = np.zeros(GMM_FEATURE_DIM)
    d_inv = np.diag(1.0 / feature_stds)
    cov_std = d_inv @ cov_orig @ d_inv  # what storage actually writes
    return {
        "n_components": 1,
        "feature_names": GMM_FEATURE_NAMES,
        "feature_means": np.asarray(feature_means, dtype=np.float64).tolist(),
        "feature_stds": feature_stds.tolist(),
        "components": [
            {
                "component_id": 0,
                "weight": 1.0,
                "mean": mean_orig.tolist(),
                "mean_std": ((mean_orig - feature_means) / feature_stds).tolist(),
                "covariance": cov_std.tolist(),
                "n_pitches": 300,
            }
        ],
        "fit_diagnostics": {"bic": 0.0},
    }


# ============================================================================
# 1. from_json de-standardizes the covariance back to ORIGINAL units
# ============================================================================


class TestFromJsonDeStandardizesCovariance(unittest.TestCase):
    """The stored covariance is in standardized space; from_json must restore
    it to original units so it is consistent with the original-unit mean."""

    def test_covariance_restored_to_original_units(self):
        rng = np.random.default_rng(322)
        feature_stds = np.array([2.0, 3.0, 1.5, 118.0, 40.0, 0.5, 0.4, 0.3])
        # A genuine original-units covariance (SPD).
        A = rng.standard_normal((GMM_FEATURE_DIM, GMM_FEATURE_DIM))
        cov_orig = A @ A.T + np.eye(GMM_FEATURE_DIM)
        mean_orig = np.array([95.0, 14.0, -7.0, 2300.0, 200.0, -1.5, 6.0, 6.5])

        model_json = _build_model_json(mean_orig, cov_orig, feature_stds)
        gmm = GMMModel.from_json(model_json)

        # The loaded covariance must equal the ORIGINAL covariance, not the
        # standardized one stored in the JSON.
        np.testing.assert_allclose(gmm.components[0].covariance, cov_orig, rtol=1e-9, atol=1e-9)
        np.testing.assert_allclose(gmm.components[0].mean, mean_orig, rtol=1e-12)

    def test_identity_stds_round_trip(self):
        """With feature_stds == 1, de-standardization is the identity, so the
        stored covariance round-trips unchanged (back-compat with old fixtures)."""
        cov = np.diag([2.0, 1.5, 1.0, 3.0, 0.5, 0.8, 0.9, 1.1])
        mean = np.arange(GMM_FEATURE_DIM, dtype=np.float64)
        model_json = _build_model_json(mean, cov, np.ones(GMM_FEATURE_DIM))
        gmm = GMMModel.from_json(model_json)
        np.testing.assert_allclose(gmm.components[0].covariance, cov, rtol=1e-12)


# ============================================================================
# 2. Self-distance is ~0
# ============================================================================


class TestSelfDistanceZero(unittest.TestCase):
    def test_bures_self_distance_zero(self):
        mean = np.array([95.0, 14.0, -7.0, 2300.0, 200.0, -1.5, 6.0, 6.5])
        cov = np.eye(GMM_FEATURE_DIM) * 2.0
        d2 = ArsenalSimilarity.bures_wasserstein_sq(mean, cov, mean, cov)
        self.assertAlmostEqual(d2, 0.0, places=6)

    def test_gmm_self_distance_zero_after_full_load_and_standardize(self):
        """End-to-end: load from JSON (de-standardize) then standardize_gmm;
        a GMM's W2 distance to itself must be ~0 and its score ~1."""
        feature_stds = np.array([2.0, 3.0, 1.5, 118.0, 40.0, 0.5, 0.4, 0.3])
        mean = np.array([95.0, 14.0, -7.0, 2300.0, 200.0, -1.5, 6.0, 6.5])
        cov = np.eye(GMM_FEATURE_DIM) * 2.0
        model_json = _build_model_json(mean, cov, feature_stds)
        gmm = GMMModel.from_json(model_json)

        pop_mean = gmm.components[0].mean.copy()
        pop_std = np.sqrt(np.diag(gmm.components[0].covariance))
        std_gmm = standardize_gmm(gmm, pop_mean, pop_std)

        d = ArsenalSimilarity.distance(std_gmm, std_gmm)
        self.assertAlmostEqual(d, 0.0, places=6)
        self.assertGreater(ArsenalSimilarity.score(std_gmm, std_gmm), 0.999999)


# ============================================================================
# 3. Closed-form analytic W2 between two 1-component Gaussians
# ============================================================================


class TestAnalyticBuresClosedForm(unittest.TestCase):
    def test_diagonal_closed_form(self):
        rng = np.random.default_rng(7)
        for _ in range(10):
            m1 = rng.standard_normal(GMM_FEATURE_DIM) * 5
            m2 = rng.standard_normal(GMM_FEATURE_DIM) * 5
            v1 = rng.uniform(0.5, 4.0, GMM_FEATURE_DIM)
            v2 = rng.uniform(0.5, 4.0, GMM_FEATURE_DIM)
            C1 = np.diag(v1)
            C2 = np.diag(v2)
            d2_engine = ArsenalSimilarity.bures_wasserstein_sq(m1, C1, m2, C2)
            d2_closed = _bures_sq_closed_form_diag(m1, v1, m2, v2)
            self.assertAlmostEqual(d2_engine, d2_closed, places=5)

    def test_single_component_gmm_matches_closed_form(self):
        """A 1-component GMM W2 equals the Bures distance of its lone Gaussian
        (transport plan is trivial), which equals the diagonal closed form."""
        m1 = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        m2 = np.array([0.5, 0.5, 2.5, 2.5, 4.5, 4.5, 6.5, 6.5])
        v1 = np.array([1.0, 2.0, 0.5, 1.5, 3.0, 1.0, 2.0, 0.8])
        v2 = np.array([1.5, 1.0, 1.0, 2.0, 2.0, 1.5, 1.0, 1.2])
        gmm_a = _make_gmm([_make_component(0, 1.0, m1, np.diag(v1))])
        gmm_b = _make_gmm([_make_component(0, 1.0, m2, np.diag(v2))])

        w2 = ArsenalSimilarity.distance(gmm_a, gmm_b)  # this is sqrt(W2^2)
        expected_w2 = np.sqrt(_bures_sq_closed_form_diag(m1, v1, m2, v2))
        self.assertAlmostEqual(w2, expected_w2, places=5)


# ============================================================================
# 4. The fix: no double-standardization (mean & cov on a CONSISTENT scale)
# ============================================================================


class TestNoDoubleStandardization(unittest.TestCase):
    """The crux of SIM-322.

    Two pitchers' GMMs are stored exactly as the nightly computor stores them
    (mean original, covariance standardized).  The W2 distance produced by the
    full engine path (from_json -> standardize_gmm -> distance) must equal the
    W2 computed entirely in a single, self-consistent ORIGINAL-units space.

    Under the OLD buggy code, from_json kept the standardized covariance and
    standardize_gmm re-divided it by pop_std, so the W2 was computed on an
    inconsistent (cov scaled by feature_stds * pop_std, mean scaled only by
    pop_std) basis -- a strictly different number.
    """

    def _full_engine_w2(self, mj_a, mj_b, pop_mean, pop_std):
        ga = standardize_gmm(GMMModel.from_json(mj_a), pop_mean, pop_std)
        gb = standardize_gmm(GMMModel.from_json(mj_b), pop_mean, pop_std)
        return ArsenalSimilarity.distance(ga, gb)

    def test_consistent_with_pure_original_units(self):
        # Two pitchers with their OWN per-pitcher feature_stds (as in prod).
        stds_a = np.array([2.0, 3.0, 1.5, 118.0, 40.0, 0.5, 0.4, 0.3])
        stds_b = np.array([2.5, 2.0, 1.0, 100.0, 35.0, 0.6, 0.5, 0.35])

        mean_a = np.array([96.0, 16.0, -8.0, 2400.0, 210.0, -1.5, 6.2, 6.5])
        mean_b = np.array([88.0, 8.0, -14.0, 1800.0, 220.0, -1.6, 6.1, 5.9])
        cov_a = np.diag([2.0, 1.5, 1.0, 9000.0, 1600.0, 0.25, 0.16, 0.09])
        cov_b = np.diag([2.5, 1.0, 0.8, 4900.0, 900.0, 0.36, 0.25, 0.12])

        mj_a = _build_model_json(mean_a, cov_a, stds_a)
        mj_b = _build_model_json(mean_b, cov_b, stds_b)

        # Reference: do the WHOLE thing in one consistent original-units space.
        # Standardize both pitchers' (mean, cov) by the SAME pop_mean / pop_std.
        pop_mean = 0.5 * (mean_a + mean_b)
        pop_std = np.array([4.0, 4.0, 3.0, 300.0, 30.0, 0.1, 0.1, 0.3])

        d_inv = np.diag(1.0 / pop_std)
        ref_a = _make_gmm(
            [_make_component(0, 1.0, (mean_a - pop_mean) / pop_std, d_inv @ cov_a @ d_inv)]
        )
        ref_b = _make_gmm(
            [_make_component(0, 1.0, (mean_b - pop_mean) / pop_std, d_inv @ cov_b @ d_inv)]
        )
        ref_w2 = ArsenalSimilarity.distance(ref_a, ref_b)

        got_w2 = self._full_engine_w2(mj_a, mj_b, pop_mean, pop_std)
        self.assertAlmostEqual(got_w2, ref_w2, places=6)

    def test_buggy_double_standardization_would_differ(self):
        """Sanity guard: the (now-removed) double-standardized covariance gives
        a DIFFERENT W2, proving this test would catch a regression to the bug."""
        stds_a = np.array([2.0, 3.0, 1.5, 118.0, 40.0, 0.5, 0.4, 0.3])
        mean_a = np.array([96.0, 16.0, -8.0, 2400.0, 210.0, -1.5, 6.2, 6.5])
        mean_b = np.array([88.0, 8.0, -14.0, 1800.0, 220.0, -1.6, 6.1, 5.9])
        cov_a = np.diag([2.0, 1.5, 1.0, 9000.0, 1600.0, 0.25, 0.16, 0.09])
        cov_b = np.diag([2.5, 1.0, 0.8, 4900.0, 900.0, 0.36, 0.25, 0.12])

        mj_a = _build_model_json(mean_a, cov_a, stds_a)
        mj_b = _build_model_json(mean_b, cov_b, stds_a)

        pop_mean = 0.5 * (mean_a + mean_b)
        pop_std = np.array([4.0, 4.0, 3.0, 300.0, 30.0, 0.1, 0.1, 0.3])

        fixed_w2 = self._full_engine_w2(mj_a, mj_b, pop_mean, pop_std)

        # Reconstruct the OLD buggy behavior by hand: keep the STORED
        # standardized covariance, then standardize_gmm divides by pop_std once
        # more -> covariance double-standardized; mean standardized once.
        d_pop_inv = np.diag(1.0 / pop_std)
        d_feat_inv_a = np.diag(1.0 / stds_a)
        cov_a_std = d_feat_inv_a @ cov_a @ d_feat_inv_a  # stored
        cov_b_std = d_feat_inv_a @ cov_b @ d_feat_inv_a
        buggy_a = _make_gmm(
            [
                _make_component(
                    0, 1.0, (mean_a - pop_mean) / pop_std, d_pop_inv @ cov_a_std @ d_pop_inv
                )
            ]
        )
        buggy_b = _make_gmm(
            [
                _make_component(
                    0, 1.0, (mean_b - pop_mean) / pop_std, d_pop_inv @ cov_b_std @ d_pop_inv
                )
            ]
        )
        buggy_w2 = ArsenalSimilarity.distance(buggy_a, buggy_b)

        # The two must be materially different (the bug actually changed the answer).
        self.assertGreater(abs(fixed_w2 - buggy_w2), 1e-3)


# ============================================================================
# 5. Standardizing inputs is idempotent w.r.t. the stored convention
# ============================================================================


class TestNoDoubleApplyOnReStandardize(unittest.TestCase):
    def test_standardize_with_storage_stats_recovers_standardized_space(self):
        """Loading (de-standardizing) then re-standardizing with the SAME
        per-pitcher feature_means/feature_stds must recover EXACTLY the
        originally-stored standardized mean/cov -- proving no double-apply."""
        feature_means = np.array([95.0, 14.0, -7.0, 2300.0, 200.0, -1.5, 6.0, 6.4])
        feature_stds = np.array([2.0, 3.0, 1.5, 118.0, 40.0, 0.5, 0.4, 0.3])
        mean_orig = np.array([96.0, 16.0, -8.0, 2400.0, 210.0, -1.4, 6.2, 6.5])
        cov_orig = np.diag([2.0, 1.5, 1.0, 9000.0, 1600.0, 0.25, 0.16, 0.09])

        mj = _build_model_json(mean_orig, cov_orig, feature_stds, feature_means)
        gmm = GMMModel.from_json(mj)

        # Re-standardize with the SAME stats used at storage time.
        restd = standardize_gmm(gmm, feature_means, feature_stds)

        # Expected = exactly what the computor stored (mean_std, cov_std).
        d_inv = np.diag(1.0 / feature_stds)
        expected_mean_std = (mean_orig - feature_means) / feature_stds
        expected_cov_std = d_inv @ cov_orig @ d_inv

        np.testing.assert_allclose(
            restd.components[0].mean, expected_mean_std, rtol=1e-9, atol=1e-9
        )
        np.testing.assert_allclose(
            restd.components[0].covariance, expected_cov_std, rtol=1e-9, atol=1e-9
        )


if __name__ == "__main__":
    unittest.main()
