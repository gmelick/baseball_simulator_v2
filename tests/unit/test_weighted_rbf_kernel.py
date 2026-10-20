"""The shared ``WeightedRBFSimilarity`` kernel, an RBF kernel (radial basis function: a similarity that falls off with the weighted distance between two profiles): an identical pair
scores 1, every score is in [0, 1], the score is symmetric, a batch equals
the single scores, a NaN feature counts as no distance, and the weights sum
to 1."""

from __future__ import annotations

import unittest

import numpy as np


class TestWeightedRBFProperties(unittest.TestCase):
    """The kernel's mathematical properties."""

    def _make_rbf(self, sigma: float = 1.0, n_features: int = 4):
        from similarity.engines.baserunner_steal_similarity import WeightedRBFSimilarity

        weights = np.ones(n_features, dtype=np.float64)
        return WeightedRBFSimilarity(sigma, weights)

    def test_identical_vectors_score_one(self):
        rbf = self._make_rbf()
        x = np.array([0.5, 0.2, 0.8, 0.1])
        self.assertAlmostEqual(rbf.score(x, x.copy()), 1.0, places=10)

    def test_score_in_zero_to_one(self):
        rbf = self._make_rbf()
        rng = np.random.default_rng(0)
        for _ in range(100):
            x = rng.normal(0, 1, 4)
            y = rng.normal(0, 1, 4)
            s = rbf.score(x, y)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0 + 1e-12)

    def test_symmetry(self):
        rbf = self._make_rbf()
        x = np.array([1.0, -0.5, 2.0, 0.0])
        y = np.array([-0.5, 1.5, 0.2, 0.8])
        self.assertAlmostEqual(rbf.score(x, y), rbf.score(y, x), places=12)

    def test_batch_matches_individual(self):
        rbf = self._make_rbf()
        rng = np.random.default_rng(1)
        q = rng.normal(0, 1, 4)
        cands = rng.normal(0, 1, (10, 4))
        batch = rbf.score_batch(q, cands)
        for i in range(10):
            self.assertAlmostEqual(batch[i], rbf.score(q, cands[i]), places=10)

    def test_nan_treated_as_neutral(self):
        """NaN features should be treated as 0 distance (neutral)."""
        rbf = self._make_rbf()
        x = np.array([0.5, np.nan, 0.8, 0.1])
        y = np.array([0.5, 0.5, 0.8, 0.1])  # y has a value where x has NaN
        score_with_nan = rbf.score(x, y)
        # NaN treated as diff=0 → higher score than if the real diff was large
        x_filled = np.array([0.5, 0.5, 0.8, 0.1])  # x with NaN replaced by y's value
        score_no_diff = rbf.score(x_filled, y)
        self.assertAlmostEqual(score_with_nan, score_no_diff, places=10)

    def test_weights_normalized(self):
        """Weights should be normalized to sum to 1.0."""
        from similarity.engines.baserunner_steal_similarity import WeightedRBFSimilarity

        weights = np.array([2.0, 3.0, 1.0, 4.0])
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=weights)
        self.assertAlmostEqual(rbf.weights.sum(), 1.0, places=12)
