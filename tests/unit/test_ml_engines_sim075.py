"""tests/unit/test_ml_engines_sim075.py — SIM-075
==================================================
Regression tests for the vectorized arsenal W2 cache lookup.

SIM-075 replaced the O(N) per-candidate Python dict loop in
``HandednessPartition.score_all`` (which called
``ArsenalCache.get_or_compute`` once per candidate) with a single NumPy
matrix **row slice**.  ``ArsenalCache`` now carries a dense symmetric
``(n_pitchers, n_pitchers)`` distance matrix plus an ``id -> row index``
map alongside the existing sparse dict store.  This is a pure layout
optimization: the returned distances/scores MUST be numerically identical
to the old per-pair computation.

These tests build a small synthetic pitcher engine via the
``__new__``-bypass pattern (see ``tests/unit/test_pitcher_similarity.py``)
and assert:

  (a) the vectorized all-vs-one lookup returns the SAME W2 values as
      computing each pair directly with ``ArsenalSimilarity.distance``;
  (b) self-distance is 0.0 (matrix diagonal);
  (c) symmetry  d[i, j] == d[j, i];
  (d) NaN / missing-GMM handling is preserved (matrix stores NaN; the
      public lookup maps it back to inf, exactly as the old loop did);
  (e) (perf) the precomputed row-slice path is no slower than — and in
      practice faster than — re-running the per-pair loop.

Run with:
    pytest tests/unit/test_ml_engines_sim075.py -v
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from similarity.engines.pitcher_similarity import (
    ARSENAL_SCALE,
    GMM_FEATURE_DIM,
    GMM_FEATURE_NAMES,
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
)

# ---------------------------------------------------------------------------
# Synthetic-fixture helpers (mirrors test_pitcher_similarity.py)
# ---------------------------------------------------------------------------


def _make_component(
    cid: int,
    weight: float,
    mean: list[float],
    var_diag: float = 1.0,
    n_pitches: int = 200,
) -> GMMComponent:
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
) -> PitcherProfile:
    return PitcherProfile(
        pitcher_id=pitcher_id,
        season=season,
        p_throws=p_throws,
        sample_pitches=500,
        gmm=gmm,
        command_vec=np.array(
            command or [0.08, 0.22, 0.30, 0.45, 0.28], dtype=np.float64
        ),
    )


def _build_rhp_profiles(include_missing_gmm: bool = True) -> list[PitcherProfile]:
    """A handful of distinct RHP profiles in one handedness partition.

    The last profile (id 999) deliberately has ``gmm=None`` to exercise the
    missing-GMM -> NaN/inf path when ``include_missing_gmm`` is True.
    """
    power_fb = _make_component(0, 0.55, [96.0, 16.0, -8.0, 2400, 210, -1.5, 6.2, 6.5], 2.0)
    slider = _make_component(1, 0.30, [86.0, -2.0, 6.0, 2500, 140, -1.3, 6.0, 6.2], 1.5)
    changeup = _make_component(2, 0.15, [88.0, 8.0, -14.0, 1800, 220, -1.6, 6.1, 5.9], 1.0)

    gmm_a = _make_gmm([power_fb, slider])
    gmm_b = _make_gmm([power_fb, changeup])
    gmm_c = _make_gmm([slider, changeup])
    gmm_d = _make_gmm(
        [
            _make_component(0, 0.4, [89.0, 11.0, -5.0, 2100, 200, -1.8, 5.9, 6.0]),
            _make_component(1, 0.35, [77.0, -18.0, 3.0, 2700, 340, -1.6, 5.7, 5.6]),
            _make_component(2, 0.25, [82.0, 6.0, -10.0, 1700, 230, -1.7, 5.8, 5.7]),
        ]
    )

    profiles = [
        _make_profile(100, 2024, "R", gmm_a, command=[0.07, 0.28, 0.32, 0.47, 0.30]),
        _make_profile(100, 2025, "R", gmm_b, command=[0.08, 0.26, 0.31, 0.46, 0.29]),
        _make_profile(200, 2024, "R", gmm_c, command=[0.06, 0.20, 0.28, 0.42, 0.25]),
        _make_profile(300, 2024, "R", gmm_d, command=[0.09, 0.24, 0.30, 0.44, 0.27]),
    ]
    if include_missing_gmm:
        profiles.append(
            _make_profile(999, 2024, "R", gmm=None, command=[0.08, 0.22, 0.30, 0.45, 0.28])
        )
    return profiles


def _build_engine(profiles: list[PitcherProfile]) -> PitcherSimilarityEngine:
    """Construct an engine without DuckDB, replicating build()'s pipeline."""
    engine = PitcherSimilarityEngine.__new__(PitcherSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {}
    engine._league_avg_command = {}
    engine._normalizer = FeatureNormalizer()
    engine._shrinkage = EmpiricalBayesShrinkage()
    engine._command_rbf = RBFSimilarity(sigma=0.8)
    engine._partition_l = HandednessPartition("L")
    engine._partition_r = HandednessPartition("R")
    engine._arsenal_cache = ArsenalCache()

    for p in profiles:
        engine._profiles[(p.pitcher_id, p.season)] = p

    engine._standardize_arsenals()
    all_profiles = list(engine._profiles.values())
    engine._normalizer.fit(all_profiles)
    profiles_l = [p for p in all_profiles if p.p_throws == "L"]
    profiles_r = [p for p in all_profiles if p.p_throws == "R"]
    engine._partition_l.build(profiles_l, engine._normalizer)
    engine._partition_r.build(profiles_r, engine._normalizer)
    return engine


# ===========================================================================
# Tests
# ===========================================================================


class TestVectorizedArsenalLookup(unittest.TestCase):
    """SIM-075 — the matrix-backed lookup matches the per-pair computation."""

    def setUp(self):
        self.engine = _build_engine(_build_rhp_profiles())
        self.partition = self.engine._partition_r
        self.cache = self.engine._arsenal_cache
        self.profiles = self.partition.profiles
        # The post-standardization GMMs live on the partition profiles; use
        # those (not the pre-standardization fixtures) as ground truth.
        self.by_key = {(p.pitcher_id, p.season): p for p in self.profiles}

    # --- (a) vectorized == direct per-pair computation -------------------

    def test_row_slice_matches_direct_pairwise_w2(self):
        """row_distances(query) must equal computing each W2 pair directly."""
        for query in self.profiles:
            qkey = (query.pitcher_id, query.season)
            # Fresh cache each query so we measure the vectorized path's
            # output, not residual state.
            self.cache._cache.clear()
            self.cache._matrix = None
            self.cache._matrix_index = {}

            row = self.cache.row_distances(query, self.profiles)

            expected = np.empty(len(self.profiles), dtype=np.float64)
            for i, cand in enumerate(self.profiles):
                ckey = (cand.pitcher_id, cand.season)
                if ckey == qkey:
                    expected[i] = 0.0
                elif query.gmm is None or cand.gmm is None:
                    expected[i] = np.inf
                else:
                    expected[i] = ArsenalSimilarity.distance(query.gmm, cand.gmm)

            np.testing.assert_allclose(row, expected, rtol=0, atol=1e-12)

    def test_precomputed_matrix_row_equals_lazy_row(self):
        """The precomputed matrix row-slice path equals the lazy build path."""
        # Lazy path (no matrix): build the row directly.
        self.cache._cache.clear()
        self.cache._matrix = None
        self.cache._matrix_index = {}
        query = self.profiles[0]
        lazy_row = self.cache.row_distances(query, self.profiles)

        # Precompute the whole partition -> dense matrix exists -> fast path.
        self.cache._cache.clear()
        self.cache._matrix = None
        self.cache._matrix_index = {}
        self.cache.precompute(self.profiles, n_workers=1)
        self.assertTrue(self.cache.has_matrix(self.profiles))
        fast_row = self.cache.row_distances(query, self.profiles)

        np.testing.assert_allclose(fast_row, lazy_row, rtol=0, atol=1e-12)

    def test_score_all_identical_to_per_pair_get_or_compute(self):
        """End-to-end: composite/arsenal scores from the vectorized score_all
        match a manual per-pair recomputation (the legacy code path)."""
        query = self.by_key[(100, 2024)]
        results = self.partition.score_all(
            query_profile=query,
            normalizer=self.engine._normalizer,
            arsenal_cache=self.cache,
            command_rbf=self.engine._command_rbf,
        )
        res_by_key = {(r.pitcher_id, r.season): r for r in results}

        # Recompute each pair's arsenal score the old way.
        fresh = ArsenalCache()
        for cand in self.profiles:
            ckey = (cand.pitcher_id, cand.season)
            if ckey == (query.pitcher_id, query.season):
                continue
            w2 = fresh.get_or_compute(
                (query.pitcher_id, query.season), ckey, query.gmm, cand.gmm
            )
            expected_arsenal = (
                float(np.exp(-w2 / ARSENAL_SCALE)) if np.isfinite(w2) else 0.0
            )
            self.assertAlmostEqual(
                res_by_key[ckey].arsenal_score, expected_arsenal, places=10
            )

    # --- (b) self-distance is 0 -----------------------------------------

    def test_self_distance_is_zero(self):
        self.cache.build_matrix(self.profiles)
        mat = self.cache._matrix
        diag = np.diag(mat)
        np.testing.assert_array_equal(diag, np.zeros(len(self.profiles)))

        # And through the public lookup: the query's own slot reads 0.0.
        for query in self.profiles:
            row = self.cache.row_distances(query, self.profiles)
            idx = self.cache._matrix_index[(query.pitcher_id, query.season)]
            self.assertEqual(row[idx], 0.0)

    # --- (c) symmetry ---------------------------------------------------

    def test_matrix_is_symmetric(self):
        self.cache.build_matrix(self.profiles)
        mat = self.cache._matrix
        n = mat.shape[0]
        for i in range(n):
            for j in range(n):
                a, b = mat[i, j], mat[j, i]
                if np.isnan(a) or np.isnan(b):
                    # NaN slots must be NaN on both sides.
                    self.assertTrue(np.isnan(a) and np.isnan(b))
                else:
                    self.assertEqual(a, b)

    # --- (d) NaN / missing-GMM handling preserved ------------------------

    def test_missing_gmm_is_nan_in_matrix_and_inf_in_lookup(self):
        self.cache.build_matrix(self.profiles)
        mat = self.cache._matrix
        idx = self.cache._matrix_index

        missing_row = idx[(999, 2024)]  # this profile has gmm=None
        # Every off-diagonal entry in the missing pitcher's row/col is NaN.
        for k, j in idx.items():
            if k == (999, 2024):
                self.assertEqual(mat[missing_row, j], 0.0)  # self-distance
            else:
                self.assertTrue(np.isnan(mat[missing_row, j]))
                self.assertTrue(np.isnan(mat[j, missing_row]))

        # The public lookup maps NaN -> inf (parity with the old loop, which
        # returned float('inf') when a GMM was None).
        missing_profile = self.by_key[(999, 2024)]
        row = self.cache.row_distances(missing_profile, self.profiles)
        for k, j in idx.items():
            if k == (999, 2024):
                self.assertEqual(row[j], 0.0)
            else:
                self.assertTrue(np.isinf(row[j]))
        self.assertFalse(np.any(np.isnan(row)), "lookup output must not contain NaN")

    def test_finite_distances_unaffected_drops_inf(self):
        """finite_distances() still excludes inf (missing-GMM) pairs."""
        self.cache.precompute(self.profiles, n_workers=1)
        finite = self.cache.finite_distances()
        self.assertTrue(np.all(np.isfinite(finite)))
        # With a missing-GMM pitcher present, some cached values are inf and
        # must NOT appear in the finite array.
        raw = np.array(list(self.cache._cache.values()), dtype=np.float64)
        self.assertTrue(np.any(np.isinf(raw)), "fixture should have inf pairs")
        self.assertEqual(len(finite), int(np.sum(np.isfinite(raw))))

    # --- (e) performance: row slice no slower than the loop --------------

    def test_vectorized_row_slice_is_faster(self):
        """With the matrix precomputed, repeated all-vs-one lookups (pure row
        slices) should beat repeatedly re-running the per-pair dict loop."""
        # Larger population so the O(N) loop has something to chew on.
        big = []
        rng = np.random.default_rng(7)
        base = _build_rhp_profiles(include_missing_gmm=False)
        for i in range(60):
            comps = [
                _make_component(
                    0, 0.6, (rng.normal(0, 1, GMM_FEATURE_DIM)).tolist(), 1.2
                ),
                _make_component(
                    1, 0.4, (rng.normal(0, 1, GMM_FEATURE_DIM)).tolist(), 1.0
                ),
            ]
            big.append(_make_profile(10_000 + i, 2024, "R", _make_gmm(comps)))
        profiles = base + big

        engine = _build_engine(profiles)
        cache = engine._arsenal_cache
        part_profiles = engine._partition_r.profiles
        query = part_profiles[0]

        # Baseline: per-pair dict loop on a cold cache, repeated.
        def loop_lookup(c: ArsenalCache) -> np.ndarray:
            qk = (query.pitcher_id, query.season)
            out = np.empty(len(part_profiles), dtype=np.float64)
            for i, cand in enumerate(part_profiles):
                ck = (cand.pitcher_id, cand.season)
                if ck == qk:
                    out[i] = 0.0
                    continue
                d = c.get_or_compute(qk, ck, query.gmm, cand.gmm)
                out[i] = d
            return out

        # Warm a separate cache for the loop baseline (fair: both warm).
        loop_cache = ArsenalCache()
        loop_lookup(loop_cache)  # warm
        t0 = time.perf_counter()
        for _ in range(2000):
            loop_lookup(loop_cache)
        loop_t = time.perf_counter() - t0

        # Vectorized: precompute matrix, then pure row slices.
        cache.precompute(part_profiles, n_workers=1)
        cache.row_distances(query, part_profiles)  # warm
        t0 = time.perf_counter()
        for _ in range(2000):
            cache.row_distances(query, part_profiles)
        vec_t = time.perf_counter() - t0

        self.assertLess(
            vec_t,
            loop_t,
            f"vectorized row slice ({vec_t:.4f}s) should beat dict loop ({loop_t:.4f}s)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
