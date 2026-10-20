"""The sigma fit of the RBF kernel on three engines' feature matrices, and the
FAISS lookup of the two nearest-neighbour engines.

An RBF kernel is a Gaussian kernel: score = exp(-distance² / 2σ²). Its sigma
(σ) sets how fast the score falls with distance. FAISS is a library for fast
nearest-neighbour lookup over a vector index.

  1. The catcher — ``calibrate_sigma`` on a synthetic, well-separated matrix
     the size of the catcher engine's four feature groups. The fit gives a
     finite, positive sigma that puts the median random pair near the project
     target of 0.50, with no collapse.

  2. The pitch-to-pitch engine — its FAISS index returns the query pitch
     itself as the nearest neighbour at about zero L2 (straight-line)
     distance, and the sigma fitted on the engine's normalised matrix puts
     the median random pair near 0.50.

  3. The batted-ball engine — the same two checks on its three features.

The tests run the real ``faiss`` library; the test environment installs it.

What ``calibrate_sigma`` does (``similarity/calibration/fitting.py``):
  * It solves ``target = exp(-gamma * median_dist_sq)`` for sigma. A collapsed
    population (every profile identical, so ``median_dist_sq <= 0``) cannot
    give a sigma: the function logs a warning and returns its degenerate value,
    ``1.0`` by default. A healthy, well-separated population therefore gives a
    finite, positive sigma that differs from 1.0 and reproduces the requested
    median.

Run with:
    pytest tests/unit/test_engine_calibration_regression.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _zscore(mat: np.ndarray) -> np.ndarray:
    """Z-score each column (subtract its mean, divide by its standard
    deviation); a column with no spread divides by 1. The calibrator's
    ``CalibratorBase._zscore_matrix`` (similarity/calibration/base.py) uses
    the same guard."""
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
    """The median RBF score over random pairs at a given sigma, with a seed of
    its own. The tests use it to check that the fit hit its target."""
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
# 1. The catcher — the sigma fit on a catcher-sized matrix
# ===========================================================================


class TestCatcherSigmaCalibration(unittest.TestCase):
    """The sigma fit converges and hits the target band on synthetic,
    well-separated catcher-sized data."""

    def _synthetic_catcher_matrix(self, n: int = 600, seed: int = 150) -> np.ndarray:
        """A well-separated synthetic matrix with one column per feature of the
        catcher engine's four sub-score groups (framing, blocking, throwing,
        deterrence), z-scored as the calibrator expects."""
        from similarity.engines.catcher_similarity import (
            BLOCKING_FEATURES,
            DETERRENCE_FEATURES,
            FRAMING_FEATURES,
            THROWING_FEATURES,
        )

        n_feats = (
            len(FRAMING_FEATURES)
            + len(BLOCKING_FEATURES)
            + len(THROWING_FEATURES)
            + len(DETERRENCE_FEATURES)
        )
        rng = np.random.default_rng(seed)
        # Genuine between-catcher spread (well separated, not collapsed):
        # mixture of latent skill tiers so the population has real variance.
        tier = rng.integers(0, 3, n)[:, None].astype(float)
        base = rng.normal(0.0, 1.0, (n, n_feats))
        raw = base + tier * 0.8  # shift by tier → multi-modal, well separated
        return _zscore(raw)

    def test_catcher_sigma_calibration_converges_to_target_band(self):
        from similarity.calibration.fitting import calibrate_sigma

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
# 2. The pitch-to-pitch engine — the nearest neighbour and the sigma fit
# ===========================================================================


class TestFaissPitchCalibrationRegression(unittest.TestCase):
    """The pitch engine's FAISS index returns the query itself at about zero
    distance, and the sigma fit on its normalised matrix hits the target band."""

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
        from similarity.calibration.fitting import calibrate_sigma

        _engine, _matrix, scaled = self._build()
        X = scaled.astype(np.float64)  # z-scored and weighted by the engine's normalizer
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
# 3. The batted-ball engine — the nearest neighbour and the sigma fit
# ===========================================================================


class TestFaissBattedBallCalibrationRegression(unittest.TestCase):
    """The batted-ball engine's FAISS index returns the query itself at about
    zero distance, and the sigma fit on its normalised matrix hits the target."""

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
        from similarity.calibration.fitting import calibrate_sigma

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
