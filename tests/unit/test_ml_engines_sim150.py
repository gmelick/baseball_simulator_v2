"""tests/unit/test_ml_engines_sim150.py — SIM-150
==================================================
ML calibration regression tests.

Three regression tests that lock in the calibrated behaviour of three
engines whose tuning is the project's core IP:

  1. Catcher v2 (RBF) — population sigma calibration on synthetic,
     well-separated catcher features converges (finite, positive sigma)
     and reproduces a median similarity near the project target of
     ~0.50, with NO collapse / no-spread degeneracy.

  2. FAISS pitch-to-pitch — the engine builds a real FAISS index and
     returns *self* as the nearest neighbour at ~zero L2 distance, and
     the population sigma calibrated from the index's normalized feature
     matrix lands the median neighbour similarity near ~0.50.

  3. FAISS batted-ball — same FAISS self-NN + calibration regression for
     the 3-feature batted-ball engine.

These run for real — ``faiss`` and ``ot`` are installed in the test
environment.

Calibration / collapse semantics (from ``similarity_calibration.py``):
  * ``calibrate_sigma`` solves ``target = exp(-gamma * median_dist_sq)``
    for sigma.  A *collapsed* population (all profiles identical →
    ``median_dist_sq <= 0``) makes calibration impossible and the
    function returns the sentinel default ``1.0`` after warning.  A
    healthy, well-separated population therefore returns a finite,
    positive sigma *different from* that fallback and reproduces the
    requested median.

Run with:
    pytest tests/unit/test_ml_engines_sim150.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _zscore(mat: np.ndarray) -> np.ndarray:
    """Z-score normalize columns; zero-variance columns get std=1 (the
    same guard the calibrator's internal _zscore uses)."""
    m = np.nanmean(mat, axis=0)
    s = np.nanstd(mat, axis=0)
    s[s == 0] = 1.0
    return (mat - m) / s


def _empirical_median_score(
    feature_matrix: np.ndarray,
    sigma: float,
    weights: np.ndarray | None = None,
    n_pairs: int = 40_000,
    seed: int = 7,
) -> float:
    """Reproduce the median RBF similarity over random pairs at a given
    sigma — used to assert the calibration actually hit its target."""
    n, d = feature_matrix.shape
    if weights is None:
        weights = np.ones(d) / d
    gamma = 1.0 / (2.0 * sigma**2)
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, n, size=n_pairs)
    ib = rng.integers(0, n, size=n_pairs)
    mask = ia != ib
    ia, ib = ia[mask], ib[mask]
    diff = np.nan_to_num(feature_matrix[ia] - feature_matrix[ib], nan=0.0)
    dist_sq = np.sum(weights[np.newaxis, :] * diff**2, axis=1)
    return float(np.median(np.exp(-gamma * dist_sq)))


# ===========================================================================
# 1. Catcher v2 — population sigma calibration regression
# ===========================================================================


class TestCatcherV2CalibrationRegression(unittest.TestCase):
    """SIM-150 #1 — calibration converges and hits the target band on
    synthetic, well-separated catcher feature data."""

    def _synthetic_catcher_matrix(self, n: int = 600, seed: int = 150) -> np.ndarray:
        """A realistic, well-separated synthetic catcher feature matrix
        spanning every catcher sub-score group (framing/blocking/
        throwing/deterrence/offense), z-scored as the calibrator expects."""
        from similarity.engines.catcher_similarity import (
            BLOCKING_FEATURES,
            DETERRENCE_FEATURES,
            FRAMING_FEATURES,
            OFFENSE_FEATURES,
            THROWING_FEATURES,
        )

        n_feats = (
            len(FRAMING_FEATURES)
            + len(BLOCKING_FEATURES)
            + len(THROWING_FEATURES)
            + len(DETERRENCE_FEATURES)
            + len(OFFENSE_FEATURES)
        )
        rng = np.random.default_rng(seed)
        # Genuine between-catcher spread (well separated, not collapsed):
        # mixture of latent skill tiers so the population has real variance.
        tier = rng.integers(0, 3, n)[:, None].astype(float)
        base = rng.normal(0.0, 1.0, (n, n_feats))
        raw = base + tier * 0.8  # shift by tier → multi-modal, well separated
        return _zscore(raw)

    def test_catcher_sigma_calibration_converges_to_target_band(self):
        from similarity.similarity_calibration import calibrate_sigma

        X = self._synthetic_catcher_matrix()
        target = 0.50
        sigma = calibrate_sigma(X, target_median_score=target, seed=42)

        # --- Convergence: finite, positive, and NOT the collapse fallback. ---
        self.assertTrue(np.isfinite(sigma), "sigma must be finite (no NaN/inf)")
        self.assertGreater(sigma, 0.0, "sigma must be positive")
        # The 1.0 sentinel is only returned on a degenerate/collapsed
        # population; a healthy spread yields something measurably different.
        self.assertNotAlmostEqual(
            sigma,
            1.0,
            places=6,
            msg="sigma == 1.0 exactly is the COLLAPSED/NO_SPREAD fallback sentinel",
        )

        # --- Regression: the calibrated sigma reproduces the target median. ---
        med = _empirical_median_score(X, sigma)
        self.assertAlmostEqual(
            med,
            target,
            delta=0.05,
            msg=f"calibrated median similarity {med:.4f} not within ±0.05 of {target}",
        )
        # Useful discrimination range — median is squarely mid-band, not
        # pinned at 0 (everyone dissimilar) or 1 (everyone identical).
        self.assertGreater(med, 0.30)
        self.assertLess(med, 0.70)


# ===========================================================================
# 2. FAISS pitch-to-pitch — self-NN + calibration regression
# ===========================================================================


class TestFaissPitchCalibrationRegression(unittest.TestCase):
    """SIM-150 #2 — the pitch FAISS index returns self at ~zero distance
    and its normalized feature matrix calibrates to the target band."""

    def _build(self, n: int = 800, seed: int = 41):
        import faiss

        from similarity.engines.pitch_pitch_similarity import (
            FEATURE_DIM,
            NearestPitch,
            PitchNormalizer,
            PitchPitchSimilarityEngine,
        )

        rng = np.random.default_rng(seed)
        matrix = np.column_stack(
            [
                rng.uniform(78.0, 102.0, n),  # velo
                rng.uniform(-12.0, 22.0, n),  # ivb
                rng.uniform(-22.0, 22.0, n),  # hb
                rng.uniform(1800.0, 2900.0, n),  # spin_rate
                rng.uniform(0.0, 359.0, n),  # spin_axis
                rng.uniform(-3.0, 3.0, n),  # release_x
                rng.uniform(4.5, 7.0, n),  # release_z
                rng.uniform(5.5, 7.5, n),  # release_ext
                rng.uniform(-2.5, 2.5, n),  # plate_x
                rng.uniform(0.5, 4.5, n),  # plate_z
            ]
        ).astype(np.float64)

        engine = PitchPitchSimilarityEngine.__new__(PitchPitchSimilarityEngine)
        engine._duckdb_path = ""
        engine._index_kind = "flat"
        engine._normalizer = PitchNormalizer()
        engine._normalizer.fit(matrix)
        scaled = engine._normalizer.normalize_batch(matrix)
        idx = faiss.IndexFlatL2(FEATURE_DIM)
        idx.add(scaled)
        engine._index = idx
        engine._index_meta = [
            NearestPitch(
                pitch_id=1_000_000 + i,
                game_pk=700_000 + (i % 100),
                season=2022 + (i % 3),
                pitcher_id=605000 + (i % 30),
                batter_id=660000 + (i % 30),
                distance=0.0,
                outcome_type="ball",
            )
            for i in range(n)
        ]
        engine._index_size = n
        return engine, matrix, scaled

    def test_self_is_nearest_neighbour_zero_distance(self):
        from similarity.engines.pitch_pitch_similarity import PitchVector

        engine, matrix, _ = self._build()
        idx = 123
        q = PitchVector(*matrix[idx].tolist())
        results = engine.query(q, k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].pitch_id, engine._index_meta[idx].pitch_id)
        self.assertAlmostEqual(results[0].distance, 0.0, places=5)
        # Distances must be non-decreasing (true nearest-neighbour ordering).
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i].distance, results[i + 1].distance + 1e-9)

    def test_pitch_sigma_calibration_hits_target_band(self):
        from similarity.similarity_calibration import calibrate_sigma

        _engine, _matrix, scaled = self._build()
        X = scaled.astype(np.float64)  # already z-scored by the engine normalizer
        target = 0.50
        sigma = calibrate_sigma(X, target_median_score=target, seed=42)
        self.assertTrue(np.isfinite(sigma))
        self.assertGreater(sigma, 0.0)
        self.assertNotAlmostEqual(sigma, 1.0, places=6)
        med = _empirical_median_score(X, sigma)
        self.assertAlmostEqual(
            med,
            target,
            delta=0.05,
            msg=f"pitch calibrated median {med:.4f} not within ±0.05 of {target}",
        )


# ===========================================================================
# 3. FAISS batted-ball — self-NN + calibration regression
# ===========================================================================


class TestFaissBattedBallCalibrationRegression(unittest.TestCase):
    """SIM-150 #3 — the batted-ball FAISS index returns self at ~zero
    distance and its normalized feature matrix calibrates to target."""

    def _build(self, n: int = 800, seed: int = 42):
        import faiss

        from similarity.engines.batted_ball_similarity import (
            FEATURE_DIM,
            BattedBallNormalizer,
            BattedBallSimilarityEngine,
            NearestBattedBall,
        )

        rng = np.random.default_rng(seed)
        matrix = np.column_stack(
            [
                rng.uniform(60.0, 115.0, n),  # exit_velo
                rng.uniform(-25.0, 50.0, n),  # launch_angle
                rng.uniform(-45.0, 45.0, n),  # spray_angle
            ]
        ).astype(np.float64)

        engine = BattedBallSimilarityEngine.__new__(BattedBallSimilarityEngine)
        engine._duckdb_path = ""
        engine._index_kind = "flat"
        engine._normalizer = BattedBallNormalizer()
        engine._normalizer.fit(matrix)
        scaled = engine._normalizer.normalize_batch(matrix)
        idx = faiss.IndexFlatL2(FEATURE_DIM)
        idx.add(scaled)
        engine._index = idx
        engine._index_meta = [
            NearestBattedBall(
                pitch_id=2_000_000 + i,
                game_pk=800_000 + (i % 100),
                season=2022 + (i % 3),
                batter_id=660000 + (i % 30),
                pitcher_id=605000 + (i % 30),
                distance=0.0,
                bb_type="line_drive",
                result_hits=1,
            )
            for i in range(n)
        ]
        engine._index_size = n
        engine._spray_column_used = "spray_angle"
        return engine, matrix, scaled

    def test_self_is_nearest_neighbour_zero_distance(self):
        from similarity.engines.batted_ball_similarity import BattedBallVector

        engine, matrix, _ = self._build()
        idx = 256
        q = BattedBallVector(*matrix[idx].tolist())
        results = engine.query(q, k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].pitch_id, engine._index_meta[idx].pitch_id)
        self.assertAlmostEqual(results[0].distance, 0.0, places=5)
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i].distance, results[i + 1].distance + 1e-9)

    def test_batted_ball_sigma_calibration_hits_target_band(self):
        from similarity.similarity_calibration import calibrate_sigma

        _engine, _matrix, scaled = self._build()
        X = scaled.astype(np.float64)
        target = 0.50
        sigma = calibrate_sigma(X, target_median_score=target, seed=42)
        self.assertTrue(np.isfinite(sigma))
        self.assertGreater(sigma, 0.0)
        self.assertNotAlmostEqual(sigma, 1.0, places=6)
        med = _empirical_median_score(X, sigma)
        self.assertAlmostEqual(
            med,
            target,
            delta=0.05,
            msg=f"batted-ball calibrated median {med:.4f} not within ±0.05 of {target}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
