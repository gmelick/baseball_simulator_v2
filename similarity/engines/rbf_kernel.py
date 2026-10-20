"""The weighted Gaussian kernel the profile engines score a sub-score with.

The batter, fielder, catcher, manager and both steal engines share this
class. The baserunner engine keeps its own masked kernel, which drops a
feature missing on either side instead of reading it as an exact match.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


class WeightedRBFSimilarity:
    """A Gaussian kernel (RBF) with per-feature reliability weights.

        K(x, y) = exp(-γ · Σ_i w_i · (x_i − y_i)²),   γ = 1 / (2σ²)

    The reliability weights give more pull to the features that stabilize
    sooner (they carry more true-talent signal). The weights are normalized to
    sum to 1.0, not to the feature count: the exponent is then the weighted
    AVERAGE squared distance per feature, so a sub-score's typical score does
    not depend on how many features it has. Without it a sub-score with many
    features would collapse toward zero and one with few would stay inflated.

    The features arrive z-scored. A missing (NaN) difference counts as zero
    distance.
    """

    def __init__(self, sigma: float, reliability_weights: NDArray[np.float64]) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma**2)
        total = reliability_weights.sum()
        if total > 0:
            self.weights = reliability_weights / total
        else:
            self.weights = np.ones_like(reliability_weights) / len(reliability_weights)

    def score(self, x: NDArray, y: NDArray) -> float:
        """The kernel between two vectors."""
        diff = x - y
        diff = np.nan_to_num(diff, nan=0.0)
        dist_sq = np.dot(self.weights * diff, diff)
        return float(np.exp(-self.gamma * dist_sq))

    def score_batch(self, query: NDArray, candidates: NDArray) -> NDArray[np.float64]:
        """The kernel between one query and each candidate row; shape (n_candidates,)."""
        diff = candidates - query[np.newaxis, :]
        diff = np.nan_to_num(diff, nan=0.0)
        dist_sq = np.sum(self.weights[np.newaxis, :] * diff**2, axis=1)
        return np.exp(-self.gamma * dist_sq)
