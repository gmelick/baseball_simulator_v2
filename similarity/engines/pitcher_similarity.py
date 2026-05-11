"""
pitcher_similarity.py
=====================
Step 2.1 — Pitcher-to-Pitcher Similarity (Command & Pitch Profile)
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
pitcher-season profiles by combining:
  1. Arsenal similarity  — GMM-to-GMM distance via Wasserstein-2 optimal
                           transport across mixture components
  2. Command similarity  — Gaussian RBF kernel over plate-discipline and
                           result metrics

Each sub-score is individually normalized to [0, 1] and then combined
using a configurable weighted average (arsenal most-weighted per the
project specification).

CRITICAL DESIGN PRINCIPLE — EXHAUSTIVE SCORING:
  The simulation's pitch-pool weighting requires a similarity score
  for EVERY pitcher-season in the population, not just a pre-filtered
  top-N. The engine therefore scores a query against ALL same-handedness
  profiles and returns the full scored list. Cross-season comparisons
  (e.g. 2025 Scherzer vs. 2024 Scherzer) are first-class — only the
  exact same (pitcher_id, season) pair is excluded.

Architecture
------------
  DuckDB (read-only)
    ├── derived.pitcher_season_metrics   → command/result metrics + GMM JSON
    ├── derived.pitcher_gmm_components   → fast SQL-queryable component centroids
    └── derived.league_averages          → fallback profiles for small-sample pitchers
         │
         ▼
  PitcherSimilarityEngine (this module)
    ├── PitcherProfile          — dataclass holding one pitcher/season profile
    ├── GMMModel                — deserialized GMM with components
    ├── ArsenalSimilarity       — Wasserstein-2 optimal transport scorer
    ├── RBFSimilarity           — vectorized batch RBF kernel scorer
    ├── EmpiricalBayesShrinkage — blends small-sample profiles toward league avg
    └── ArsenalCache            — lazy + optional batch W2 distance cache
         │
         ▼
  query(pitcher_id, season, n=None) → List[SimilarityResult]
                                     (default n=None returns ALL)

Performance Model
-----------------
  RBF sub-scores are vectorized as matrix operations against the
  full handedness partition. For 2,400 RHP profiles this takes <1ms.

  Arsenal W2 is the expensive path (~0.5ms per pair due to 8×8 matrix
  square roots). Two strategies:

    1. Lazy caching: first query for a pitcher computes W2 against all
       same-handedness profiles (~1.2s for 2,400 RHP). Results are
       cached; subsequent queries for the same pitcher are instant.

    2. Batch precomputation: call precompute_arsenal_cache() after build()
       to fill the full pairwise cache (~20min for 2,400 RHP with
       multiprocessing). Can be persisted to disk.

  For simulation: the first iteration pays the ~1s lazy-cache cost per
  pitcher; all 99 subsequent iterations are <1ms.

Key Design Decisions
--------------------
- **Wasserstein-2 (Earth Mover's Distance) for arsenal comparison:**
  The gold-standard distance between two Gaussian Mixture Models. Unlike
  KL divergence it is a true metric (symmetric, satisfies triangle
  inequality), handles mixtures with different numbers of components
  naturally, and has a closed-form solution between individual Gaussian
  components. We solve the optimal transport problem via the POT library
  (Python Optimal Transport).

- **Full covariance Gaussian distance (Bures-Wasserstein):**
  The W2 distance between two individual Gaussians N(m1, C1) and
  N(m2, C2) is:
    W2² = ||m1 - m2||² + Tr(C1 + C2 - 2*(C1^{1/2} C2 C1^{1/2})^{1/2})
  This properly accounts for correlation structure in the pitch profile
  (e.g. velo/spin correlation) rather than treating features as
  independent.

- **Empirical Bayes shrinkage for small-sample pitchers:**
  Pitchers below the minimum sample threshold (200 pitches) have their
  feature vector shrunk toward the league-average profile. The shrinkage
  weight is α = N / (N + N_prior), where N_prior is calibrated so that
  at 200 pitches the prior contributes ~0%.

- **Handedness gating:**
  LHP and RHP live in separate partitions. A LHP is never matched
  to a RHP — their release geometry and batter-facing profiles are
  fundamentally different.

- **Cross-season comparisons are first-class:**
  2025 Scherzer vs. 2024 Scherzer is a valid and useful comparison.
  Only the exact same (pitcher_id, season) tuple is excluded from
  results. This enables the simulation to track pitcher evolution and
  blend multi-year profiles.

Dependencies
------------
  pip install duckdb numpy scipy scikit-learn POT (python-ot)

Usage
-----
  engine = PitcherSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])

  # All similar pitchers (exhaustive)
  results = engine.query(pitcher_id=543037, season=2025)

  # Top-20 only
  results = engine.query(pitcher_id=543037, season=2025, n=20)

  # Optional: pre-warm the full arsenal cache for instant queries
  engine.precompute_arsenal_cache(n_workers=4)
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray
from scipy.linalg import sqrtm

from similarity.similarity_diagnostics import run_pitcher_diagnostics
import ot

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pitcher_similarity")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Feature names inside GMM (must match GMM_FEATURE_NAMES in player_profile_computor)
GMM_FEATURE_NAMES = [
    "velo", "ivb", "hb", "spin_rate", "spin_axis", "release_x", "release_z", "release_ext",
]
GMM_FEATURE_DIM = len(GMM_FEATURE_NAMES)

# Command features used for the RBF command sub-score
COMMAND_FEATURES = [
    "bb_rate", "k_rate", "csw_rate", "zone_take_rate", "chase_rate", "zone_rate", "whiff_rate",
]

# Sub-score weights — arsenal most weighted per project spec
WEIGHT_ARSENAL   = 0.65
WEIGHT_COMMAND   = 0.35
_TOTAL_WEIGHT    = WEIGHT_ARSENAL + WEIGHT_COMMAND
assert abs(_TOTAL_WEIGHT - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters (gamma = 1 / (2 * sigma²))
# Calibrated so that the median MLB pitcher pair gets a score
# around 0.4–0.6 (useful discrimination range).
RBF_SIGMA_COMMAND  = 1.0453   # tighter — command is lower-dimensional

# Arsenal W2 distance → similarity score transform.
#
# Uses a linear exponential:
#   score = exp(-W₂ / ARSENAL_SCALE)
#
# Linear rather than squared because the simulation uses similarity as a
# weight for ALL historical pitches in the pool, not just top-N. The
# linear form keeps the tail (dissimilar pitchers) distinguishable rather
# than crushing them all to ~0, which matters when even moderately
# different pitchers contribute to the weighted sample.
#
# ARSENAL_SCALE = -median_W₂ / ln(target)
# For median W₂ ≈ 2.84 and target 0.50: scale = 2.84 / 0.693 = 4.10
#
# Recalibrate from real data via:
#   from similarity_calibration import calibrate_arsenal_scale
#   dists = engine._arsenal_cache.finite_distances()
#   optimal_scale = calibrate_arsenal_scale(dists, target_median_score=0.50)
ARSENAL_SCALE = 4.25

# Empirical Bayes shrinkage prior strength
# At N_PRIOR pitches, the shrinkage weight α = N / (N + N_PRIOR) = 0.5
# At 200 pitches (the minimum sample threshold), α ≈ 200/(200+50) = 0.80
EB_N_PRIOR = 5

# Minimum cluster size for GMM — components with fewer assigned pitches
# than this are merged into the nearest larger component.
MIN_CLUSTER_SIZE = 30

# Minimum sample for similarity (matches player_profile_computor.py)
MIN_PITCHER_PITCHES = 200

# ============================================================================
# Data Structures
# ============================================================================

@dataclass(frozen=True, slots=True)
class GMMComponent:
    """One component of a pitcher's Gaussian Mixture Model."""
    component_id: int
    weight: float                          # mixing proportion (sums to 1.0)
    mean: NDArray[np.float64]              # shape (8,) — original feature units
    covariance: NDArray[np.float64]        # shape (8, 8) — full covariance
    n_pitches: int


@dataclass(slots=True)
class GMMModel:
    """Full GMM for one pitcher/season."""
    n_components: int
    feature_names: list[str]
    feature_means: NDArray[np.float64]     # global standardization mean
    feature_stds: NDArray[np.float64]      # global standardization std
    components: list[GMMComponent]
    bic: float = 0.0

    @classmethod
    def from_json(cls, model_json: dict) -> GMMModel:
        """Deserialize from the JSON stored in pitcher_season_metrics.gmm_model."""
        components = []
        for c in model_json.get("components", []):
            components.append(GMMComponent(
                component_id=c["component_id"],
                weight=c["weight"],
                mean=np.array(c["mean"], dtype=np.float64),
                covariance=np.array(c["covariance"], dtype=np.float64),
                n_pitches=c.get("n_pitches", 0),
            ))
        diag = model_json.get("fit_diagnostics", {})
        return cls(
            n_components=model_json.get("n_components", 0),
            feature_names=model_json.get("feature_names", GMM_FEATURE_NAMES),
            feature_means=np.array(
                model_json.get("feature_means", [0.0] * GMM_FEATURE_DIM),
                dtype=np.float64,
            ),
            feature_stds=np.array(
                model_json.get("feature_stds", [1.0] * GMM_FEATURE_DIM),
                dtype=np.float64,
            ),
            components=components,
            bic=diag.get("bic", 0.0),
        )


@dataclass(slots=True)
class PitcherProfile:
    """Complete pitcher profile for similarity scoring."""
    pitcher_id: int
    season: int
    p_throws: str                          # "L" or "R"
    sample_pitches: int

    # Sub-models
    gmm: GMMModel | None

    # Command vector (normalized externally before scoring)
    command_vec: NDArray[np.float64]        # shape (len(COMMAND_FEATURES),)

    # Empirical Bayes shrinkage weight (1.0 = fully own data, 0.0 = fully league avg)
    eb_alpha: float = 1.0

    # Below minimum sample flag
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""
    pitcher_id: int
    season: int
    p_throws: str
    score: float                           # composite [0, 1], 1 = identical
    arsenal_score: float
    command_score: float
    sample_pitches: int


# ============================================================================
# Arsenal Similarity — Wasserstein-2 Optimal Transport
# ============================================================================

class ArsenalSimilarity:
    """
    Computes similarity between two pitchers' arsenals modeled as GMMs
    using Wasserstein-2 (Earth Mover's Distance) optimal transport.

    The cost matrix between components is the Bures–Wasserstein distance
    (the W2 distance between individual Gaussian components), which
    accounts for full covariance structure.

    When the POT library is not available, falls back to a greedy
    nearest-component matching that is a reasonable approximation.
    """

    @staticmethod
    def bures_wasserstein_sq(
        m1: NDArray, C1: NDArray,
        m2: NDArray, C2: NDArray,
    ) -> float:
        """
        Squared Bures–Wasserstein distance between N(m1, C1) and N(m2, C2):

            W2² = ||m1 - m2||² + Tr(C1 + C2 - 2 * (C1^½ C2 C1^½)^½)

        This is the closed-form W2 distance for multivariate Gaussians.
        Uses the matrix square root (scipy.linalg.sqrtm).

        Returns a non-negative float. Numerically stable implementation
        clamps small negative eigenvalues that arise from floating-point
        error in the matrix square root.
        """
        mean_diff_sq = np.sum((m1 - m2) ** 2)

        # Compute C1^{1/2}
        C1_sqrt = sqrtm(C1)
        if np.iscomplexobj(C1_sqrt):
            C1_sqrt = C1_sqrt.real

        # Inner product: C1^{1/2} @ C2 @ C1^{1/2}
        inner = C1_sqrt @ C2 @ C1_sqrt

        # Matrix square root of inner product
        inner_sqrt = sqrtm(inner)
        if np.iscomplexobj(inner_sqrt):
            inner_sqrt = inner_sqrt.real

        trace_term = np.trace(C1) + np.trace(C2) - 2.0 * np.trace(inner_sqrt)

        # Clamp to handle floating point noise
        return float(max(mean_diff_sq + trace_term, 0.0))

    @classmethod
    def _build_cost_matrix(
        cls,
        gmm_a: GMMModel,
        gmm_b: GMMModel,
    ) -> NDArray[np.float64]:
        """
        Build the |K_a| × |K_b| cost matrix where entry (i, j) is the
        squared Bures–Wasserstein distance between component i of gmm_a
        and component j of gmm_b.
        """
        n_a = len(gmm_a.components)
        n_b = len(gmm_b.components)
        cost = np.zeros((n_a, n_b), dtype=np.float64)

        for i, ca in enumerate(gmm_a.components):
            for j, cb in enumerate(gmm_b.components):
                cost[i, j] = cls.bures_wasserstein_sq(
                    ca.mean, ca.covariance,
                    cb.mean, cb.covariance,
                )
        return cost

    @classmethod
    def distance(cls, gmm_a: GMMModel, gmm_b: GMMModel) -> float:
        """
        Wasserstein-2 distance between two Gaussian Mixture Models.

        Solves the optimal transport problem:
            min_{T} Σ_{ij} T_{ij} * C_{ij}
            s.t. T @ 1 = weights_a, T.T @ 1 = weights_b

        where C_{ij} is the squared Bures–Wasserstein distance.

        Returns the square root of the transport cost (W2, not W2²).
        """
        if not gmm_a.components or not gmm_b.components:
            return float("inf")

        cost = cls._build_cost_matrix(gmm_a, gmm_b)
        weights_a = np.array([c.weight for c in gmm_a.components], dtype=np.float64)
        weights_b = np.array([c.weight for c in gmm_b.components], dtype=np.float64)

        # Normalize weights (should already sum to 1, but safety)
        weights_a /= weights_a.sum()
        weights_b /= weights_b.sum()

        if ot is not None:
            # Exact Earth Mover's Distance via POT linear programming
            transport_cost = ot.emd2(weights_a, weights_b, cost)
        else:
            # Fallback: greedy nearest-component matching
            transport_cost = cls._greedy_transport(weights_a, weights_b, cost)

        return float(np.sqrt(max(transport_cost, 0.0)))

    @staticmethod
    def _greedy_transport(
        wa: NDArray, wb: NDArray, cost: NDArray,
    ) -> float:
        """
        Greedy approximation to optimal transport when POT is unavailable.

        Iteratively matches the cheapest remaining (i, j) pair, transferring
        as much mass as possible. Not optimal but reasonable for small K.
        """
        wa = wa.copy()
        wb = wb.copy()
        total_cost = 0.0

        n_a, n_b = cost.shape
        entries = []
        for i in range(n_a):
            for j in range(n_b):
                entries.append((cost[i, j], i, j))
        entries.sort()

        for c, i, j in entries:
            if wa[i] <= 0 or wb[j] <= 0:
                continue
            transfer = min(wa[i], wb[j])
            total_cost += transfer * c
            wa[i] -= transfer
            wb[j] -= transfer

        return total_cost

    @classmethod
    def score(cls, gmm_a: GMMModel, gmm_b: GMMModel) -> float:
        """
        Arsenal similarity score in [0, 1].

        Uses the Gaussian RBF form for consistency with all other sub-scores:
            score = exp(-ARSENAL_GAMMA * W₂²)

        A distance of 0 → score 1.0 (identical).
        """
        d = cls.distance(gmm_a, gmm_b)
        if not np.isfinite(d):
            return 0.0
        return float(np.exp(-d / ARSENAL_SCALE))


# ============================================================================
# Command / Result Similarity — Gaussian RBF Kernel
# ============================================================================

class RBFSimilarity:
    """
    Gaussian RBF (Radial Basis Function) kernel similarity with
    dimensionality-invariant scaling.

    K(x, y) = exp(-γ * mean_i[(x_i - y_i)²])

    Features are z-score normalized before computing the distance so
    that each feature contributes roughly equally.
    """

    def __init__(self, sigma: float = 1.0) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma ** 2)

    def score(self, x: NDArray, y: NDArray) -> float:
        """Compute RBF similarity between two vectors. Returns [0, 1]."""
        diff = x - y
        diff = np.nan_to_num(diff, nan=0.0)
        mean_dist_sq = np.dot(diff, diff) / len(diff)
        return float(np.exp(-self.gamma * mean_dist_sq))

    def score_batch(
        self, query: NDArray, candidates: NDArray,
    ) -> NDArray[np.float64]:
        """
        Compute RBF similarity between one query vector and an array of
        candidate vectors. Returns shape (n_candidates,).
        """
        diff = candidates - query[np.newaxis, :]
        diff = np.nan_to_num(diff, nan=0.0)
        n_features = diff.shape[1]
        mean_dist_sq = np.sum(diff ** 2, axis=1) / n_features
        return np.exp(-self.gamma * mean_dist_sq)


# ============================================================================
# Empirical Bayes Shrinkage
# ============================================================================

class EmpiricalBayesShrinkage:
    """
    Shrinks a pitcher's raw feature vector toward the league-average
    profile based on sample size.

    Shrinkage weight: α = N / (N + N_prior)
      - Large N  → α → 1.0 → trust pitcher's own data
      - Small N  → α → 0.0 → rely on league average

    N_prior is calibrated so that at MIN_PITCHER_PITCHES (200), the
    pitcher's own data accounts for ~80% of the blended profile.

    For GMM models specifically, shrinkage is applied to the *command*
    and *result* feature vectors only — the GMM itself is either used
    as-is (if above threshold) or replaced with the league-average GMM
    (if below threshold). Partially shrinking GMM mixture models is not
    meaningful because the component structure changes.
    """

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int) -> float:
        """Shrinkage weight: proportion of own data vs. prior."""
        return n_samples / (n_samples + self.n_prior)

    def shrink(
        self,
        raw_vec: NDArray[np.float64],
        league_avg_vec: NDArray[np.float64],
        n_samples: int,
    ) -> NDArray[np.float64]:
        """
        Returns α * raw_vec + (1 - α) * league_avg_vec.
        Handles NaN in raw_vec by using league_avg in those positions.
        """
        a = self.alpha(n_samples)
        raw_clean = np.where(np.isnan(raw_vec), league_avg_vec, raw_vec)
        return a * raw_clean + (1.0 - a) * league_avg_vec


# ============================================================================
# GMM Post-Processing — Minimum Cluster Size Enforcement
# ============================================================================

def standardize_gmm(
    gmm: GMMModel,
    pop_mean: NDArray[np.float64],
    pop_std: NDArray[np.float64],
) -> GMMModel:
    """
    Transform a GMM's component means and covariances from original
    feature units into z-score (standardized) space.

    This is CRITICAL for making the W2 distance dimensionality-invariant.
    Without standardization, spin_rate (range ~1000 RPM) dominates
    velocity (range ~15 mph) in the distance calculation because W2
    operates on raw Euclidean distances between component centroids.

    Standardized mean:  z_μ = (μ - pop_mean) / pop_std
    Standardized cov:   z_Σ = D⁻¹ Σ D⁻¹   where D = diag(pop_std)

    The covariance transform follows from the linear transformation
    property: if Y = AX, then Cov(Y) = A Cov(X) Aᵀ, where A = D⁻¹.

    Parameters
    ----------
    gmm : GMMModel
        GMM with means/covariances in original feature units.
    pop_mean : (D,) array
        Population mean for each feature (computed across all GMM
        component centroids, weighted by component weight).
    pop_std : (D,) array
        Population std for each feature. Zeros replaced with 1.0.

    Returns
    -------
    New GMMModel with standardized means and covariances. Weights and
    component structure are unchanged.
    """
    safe_std = pop_std.copy()
    safe_std[safe_std == 0] = 1.0
    D_inv = np.diag(1.0 / safe_std)

    new_components = []
    for c in gmm.components:
        z_mean = (c.mean - pop_mean) / safe_std
        z_cov = D_inv @ c.covariance @ D_inv
        new_components.append(GMMComponent(
            component_id=c.component_id,
            weight=c.weight,
            mean=z_mean,
            covariance=z_cov,
            n_pitches=c.n_pitches,
        ))

    return GMMModel(
        n_components=gmm.n_components,
        feature_names=gmm.feature_names,
        feature_means=pop_mean,
        feature_stds=safe_std,
        components=new_components,
        bic=gmm.bic,
    )

def enforce_min_cluster_size(gmm: GMMModel) -> GMMModel:
    """
    Merges GMM components with fewer than MIN_CLUSTER_SIZE assigned pitches
    into the nearest (by centroid Euclidean distance) larger component.

    This resolves the 'extreme archetype' outlier issue noted in the
    project plan Step 2.1 — tiny components (e.g. 10 pitches of an
    experimental pitch) can dominate distance calculations due to their
    unusual location in feature space.

    Returns a new GMMModel with the cleaned components.
    """
    if not gmm.components:
        return gmm

    components = list(gmm.components)

    changed = True
    while changed and len(components) > 1:
        changed = False

        small = [c for c in components if c.n_pitches < MIN_CLUSTER_SIZE]
        if not small:
            break

        small.sort(key=lambda c: c.n_pitches)

        for s in small:
            if s not in components:
                continue

            large = [c for c in components if c is not s]
            if not large:
                break

            dists = [np.linalg.norm(s.mean - c.mean) for c in large]
            nearest = large[int(np.argmin(dists))]

            total_w = s.weight + nearest.weight
            total_n = s.n_pitches + nearest.n_pitches
            frac_s = s.weight / total_w if total_w > 0 else 0.5
            frac_n = nearest.weight / total_w if total_w > 0 else 0.5

            merged_mean = frac_s * s.mean + frac_n * nearest.mean
            diff_s = (s.mean - merged_mean).reshape(-1, 1)
            diff_n = (nearest.mean - merged_mean).reshape(-1, 1)
            merged_cov = (
                frac_s * (s.covariance + diff_s @ diff_s.T)
                + frac_n * (nearest.covariance + diff_n @ diff_n.T)
            )

            merged = GMMComponent(
                component_id=nearest.component_id,
                weight=total_w,
                mean=merged_mean,
                covariance=merged_cov,
                n_pitches=total_n,
            )

            components = [c for c in components if c is not s and c is not nearest]
            components.append(merged)
            changed = True
            break

    # Re-normalize weights
    total_w = sum(c.weight for c in components)
    if total_w > 0:
        components = [
            GMMComponent(
                component_id=i,
                weight=c.weight / total_w,
                mean=c.mean,
                covariance=c.covariance,
                n_pitches=c.n_pitches,
            )
            for i, c in enumerate(components)
        ]

    return GMMModel(
        n_components=len(components),
        feature_names=gmm.feature_names,
        feature_means=gmm.feature_means,
        feature_stds=gmm.feature_stds,
        components=components,
        bic=gmm.bic,
    )


# ============================================================================
# Feature Normalization
# ============================================================================

@dataclass(slots=True)
class FeatureNormalizer:
    """
    Z-score normalizer fit on the population of pitcher profiles.

    Stores per-feature mean and std for command and result feature vectors.
    Missing values (NaN) are filled with the population
    mean (i.e. z-score of 0) so they contribute nothing to distance.
    """
    command_mean: NDArray[np.float64] | None = None
    command_std: NDArray[np.float64] | None = None

    def fit(self, profiles: list[PitcherProfile]) -> None:
        """Compute population statistics from all profiles."""
        if not profiles:
            return

        cmd = np.array([p.command_vec for p in profiles], dtype=np.float64)

        self.command_mean = np.nanmean(cmd, axis=0)
        self.command_std = np.nanstd(cmd, axis=0)
        self.command_std[self.command_std == 0] = 1.0

    def normalize_command(self, vec: NDArray) -> NDArray:
        if self.command_mean is None:
            return vec
        normed = (vec - self.command_mean) / self.command_std
        return np.nan_to_num(normed, nan=0.0)


# ============================================================================
# Arsenal Distance Cache
# ============================================================================

def _cache_key(
    a: tuple[int, int], b: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Canonical symmetric key: always smaller tuple first."""
    return (a, b) if a <= b else (b, a)


class ArsenalCache:
    """
    Lazy + optional-batch cache for pairwise W2 arsenal distances.

    Stores raw W2 distance (not the [0,1] score) since the score is a
    simple monotonic transform that can be applied at query time.

    Usage patterns:
      1. Lazy: call get_or_compute() — computes on miss, caches for reuse.
      2. Batch: call precompute() once to fill the entire cache.
      3. Persist: call save() / load() to avoid re-computation across runs.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}

    def get(
        self, a: tuple[int, int], b: tuple[int, int],
    ) -> float | None:
        """Look up cached distance. Returns None on miss."""
        return self._cache.get(_cache_key(a, b))

    def put(
        self, a: tuple[int, int], b: tuple[int, int], distance: float,
    ) -> None:
        """Store a distance in the cache."""
        self._cache[_cache_key(a, b)] = distance

    def get_or_compute(
        self,
        key_a: tuple[int, int],
        key_b: tuple[int, int],
        gmm_a: GMMModel | None,
        gmm_b: GMMModel | None,
    ) -> float:
        """
        Return cached W2 distance, or compute + cache on miss.

        Returns float('inf') if either GMM is None (missing model).
        Returns 0.0 for self-comparisons.
        """
        if key_a == key_b:
            return 0.0

        cached = self.get(key_a, key_b)
        if cached is not None:
            return cached

        if gmm_a is None or gmm_b is None:
            dist = float("inf")
        else:
            dist = ArsenalSimilarity.distance(gmm_a, gmm_b)

        self.put(key_a, key_b, dist)
        return dist

    def precompute(
        self,
        profiles: list[PitcherProfile],
        n_workers: int = 1,
    ) -> None:
        """
        Batch-fill the cache for all pairs in the given profile list.

        Parameters
        ----------
        profiles : list[PitcherProfile]
            All profiles in one handedness partition.
        n_workers : int
            Number of parallel workers. 1 = serial (safe, no overhead).
            >1 = multiprocessing (faster for large populations).
        """
        pairs_to_compute = []
        for i in range(len(profiles)):
            for j in range(i + 1, len(profiles)):
                pa, pb = profiles[i], profiles[j]
                key_a = (pa.pitcher_id, pa.season)
                key_b = (pb.pitcher_id, pb.season)
                if self.get(key_a, key_b) is None:
                    pairs_to_compute.append((i, j))

        if not pairs_to_compute:
            log.info("  Arsenal cache already complete for %d profiles.", len(profiles))
            return

        log.info(
            "  Precomputing %d arsenal W2 distances (%d workers) …",
            len(pairs_to_compute), n_workers,
        )
        t0 = time.time()

        if n_workers <= 1:
            for idx, (i, j) in enumerate(pairs_to_compute):
                pa, pb = profiles[i], profiles[j]
                key_a = (pa.pitcher_id, pa.season)
                key_b = (pb.pitcher_id, pb.season)
                if pa.gmm is not None and pb.gmm is not None:
                    dist = ArsenalSimilarity.distance(pa.gmm, pb.gmm)
                else:
                    dist = float("inf")
                self.put(key_a, key_b, dist)

                if (idx + 1) % 10000 == 0:
                    elapsed = time.time() - t0
                    rate = (idx + 1) / elapsed
                    remaining = (len(pairs_to_compute) - idx - 1) / rate
                    log.info(
                        "    %d/%d pairs (%.0f/sec, ~%.0fs remaining)",
                        idx + 1, len(pairs_to_compute), rate, remaining,
                    )
        else:
            # Parallel computation via multiprocessing
            gmm_data = {}
            for p in profiles:
                k = (p.pitcher_id, p.season)
                if p.gmm is not None:
                    gmm_data[k] = _serialize_gmm(p.gmm)

            chunk_size = max(1, len(pairs_to_compute) // (n_workers * 4))
            chunks = [
                pairs_to_compute[i:i + chunk_size]
                for i in range(0, len(pairs_to_compute), chunk_size)
            ]

            keys = [(p.pitcher_id, p.season) for p in profiles]

            with ProcessPoolExecutor(max_workers=n_workers) as executor:
                futures = {}
                for chunk in chunks:
                    future = executor.submit(
                        _compute_w2_chunk, chunk, keys, gmm_data,
                    )
                    futures[future] = chunk

                done = 0
                for future in as_completed(futures):
                    results = future.result()
                    for (key_a, key_b), dist in results:
                        self.put(key_a, key_b, dist)
                    done += len(futures[future])

        elapsed = time.time() - t0
        log.info(
            "  Arsenal cache precomputed: %d pairs in %.1fs (%.0f pairs/sec).",
            len(pairs_to_compute), elapsed,
            len(pairs_to_compute) / max(elapsed, 0.001),
        )

    @property
    def size(self) -> int:
        """Number of cached distances."""
        return len(self._cache)

    def finite_distances(self) -> NDArray[np.float64]:
        """
        Return all cached W2 distances as a 1-D numpy array, excluding
        inf values (which represent pairs where one or both GMMs were
        missing).

        SIM-148: docstring updated to use ``calibrate_arsenal_scale``
        (the current API).  ``calibrate_arsenal_gamma`` was renamed during
        SIM-066; pre-SIM-148 docstring still referenced the dead name.

        This is the array you pass to calibrate_arsenal_scale():

            from similarity.similarity_calibration import calibrate_arsenal_scale

            engine.precompute_arsenal_cache()
            dists = engine._arsenal_cache.finite_distances()
            optimal_scale = calibrate_arsenal_scale(dists, target_median_score=0.50)
        """
        all_dists = np.array(list(self._cache.values()), dtype=np.float64)
        return all_dists[np.isfinite(all_dists)]

    def save(self, path: str) -> None:
        """Persist cache to disk (pickle)."""
        with open(path, "wb") as f:
            pickle.dump(self._cache, f, protocol=pickle.HIGHEST_PROTOCOL)
        log.info("Arsenal cache saved: %d entries → %s", len(self._cache), path)

    def load(self, path: str) -> None:
        """Load cache from disk. Merges with existing entries."""
        if not os.path.exists(path):
            log.warning("Cache file not found: %s", path)
            return
        with open(path, "rb") as f:
            loaded = pickle.load(f)
        self._cache.update(loaded)
        log.info("Arsenal cache loaded: %d entries from %s", len(loaded), path)


# --- Multiprocessing helpers (module-level for pickling) ---

def _serialize_gmm(gmm: GMMModel) -> dict:
    """Serialize GMM to a plain dict for cross-process pickling."""
    return {
        "components": [
            {
                "weight": c.weight,
                "mean": c.mean.tolist(),
                "covariance": c.covariance.tolist(),
            }
            for c in gmm.components
        ],
    }


def _deserialize_gmm(data: dict) -> GMMModel:
    """Reconstruct a minimal GMMModel from serialized data."""
    components = [
        GMMComponent(
            component_id=i,
            weight=c["weight"],
            mean=np.array(c["mean"], dtype=np.float64),
            covariance=np.array(c["covariance"], dtype=np.float64),
            n_pitches=0,
        )
        for i, c in enumerate(data["components"])
    ]
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=np.zeros(GMM_FEATURE_DIM),
        feature_stds=np.ones(GMM_FEATURE_DIM),
        components=components,
    )


def _compute_w2_chunk(
    pairs: list[tuple[int, int]],
    keys: list[tuple[int, int]],
    gmm_data: dict[tuple[int, int], dict],
) -> list[tuple[tuple[tuple[int, int], tuple[int, int]], float]]:
    """Worker function for parallel W2 computation."""
    results = []
    for i, j in pairs:
        key_a, key_b = keys[i], keys[j]
        data_a = gmm_data.get(key_a)
        data_b = gmm_data.get(key_b)
        if data_a is not None and data_b is not None:
            gmm_a = _deserialize_gmm(data_a)
            gmm_b = _deserialize_gmm(data_b)
            dist = ArsenalSimilarity.distance(gmm_a, gmm_b)
        else:
            dist = float("inf")
        results.append(((key_a, key_b), dist))
    return results


# ============================================================================
# Handedness Partition — Vectorized RBF Matrices
# ============================================================================

class HandednessPartition:
    """
    Stores all profiles for one handedness (L or R) with pre-built
    normalized feature matrices for vectorized batch RBF scoring.

    The query path:
      1. Command/result RBF scores: single matrix op (< 1ms for 3000 profiles)
      2. Arsenal W2 scores: lookup from ArsenalCache (lazy or precomputed)
      3. Composite: weighted sum with confidence discount
    """

    def __init__(self, hand: str) -> None:
        self.hand = hand
        self.profiles: list[PitcherProfile] = []
        self.keys: list[tuple[int, int]] = []           # parallel to profiles

        # Normalized feature matrices — shape (N, feature_dim)
        self._cmd_matrix: NDArray | None = None

        # Confidence (EB alpha) array — shape (N,)
        self._eb_alphas: NDArray | None = None

    def build(
        self,
        profiles: list[PitcherProfile],
        normalizer: FeatureNormalizer,
    ) -> None:
        """Populate the partition and build vectorized matrices."""
        self.profiles = profiles
        self.keys = [(p.pitcher_id, p.season) for p in profiles]

        if not profiles:
            return

        cmd_rows = []
        alphas = []

        for p in profiles:
            cmd_rows.append(normalizer.normalize_command(p.command_vec))
            alphas.append(p.eb_alpha)

        self._cmd_matrix = np.array(cmd_rows, dtype=np.float64)
        self._eb_alphas = np.array(alphas, dtype=np.float64)

    def score_all(
        self,
        query_profile: PitcherProfile,
        normalizer: FeatureNormalizer,
        arsenal_cache: ArsenalCache,
        command_rbf: RBFSimilarity,
    ) -> list[SimilarityResult]:
        """
        Score the query profile against EVERY profile in this partition.

        Returns a SimilarityResult for each profile EXCEPT the exact
        same (pitcher_id, season) as the query. Cross-season matches
        for the same pitcher_id ARE included.

        RBF sub-scores are computed in a single vectorized pass.
        Arsenal W2 distances are looked up from the cache (computed
        lazily on miss).
        """
        if not self.profiles:
            return []

        n = len(self.profiles)
        query_key = (query_profile.pitcher_id, query_profile.season)

        # --- Vectorized RBF sub-scores (< 1ms for thousands of profiles) ---
        cmd_q = normalizer.normalize_command(query_profile.command_vec)

        command_scores = command_rbf.score_batch(cmd_q, self._cmd_matrix)

        # --- Arsenal W2 scores (from cache, lazy-compute on miss) ---
        arsenal_scores = np.zeros(n, dtype=np.float64)
        query_has_gmm = query_profile.gmm is not None

        for i, cand in enumerate(self.profiles):
            cand_key = self.keys[i]
            if cand_key == query_key:
                # Will be filtered out below; skip expensive W2
                continue

            w2_dist = arsenal_cache.get_or_compute(
                query_key, cand_key,
                query_profile.gmm, cand.gmm,
            )

            if np.isfinite(w2_dist):
                arsenal_scores[i] = np.exp(-w2_dist / ARSENAL_SCALE)
            else:
                arsenal_scores[i] = 0.0

        # --- Composite scores ---
        has_arsenal = np.array(
            [query_has_gmm and p.gmm is not None for p in self.profiles],
            dtype=bool,
        )

        composite = np.zeros(n, dtype=np.float64)

        # Path 1: both GMMs present — full weighted sum
        mask_full = has_arsenal
        composite[mask_full] = (
            WEIGHT_ARSENAL * arsenal_scores[mask_full]
            + WEIGHT_COMMAND * command_scores[mask_full]
        )

        # Path 2: missing GMM — redistribute arsenal weight
        mask_no_arsenal = ~has_arsenal
        remaining = WEIGHT_COMMAND
        composite[mask_no_arsenal] = (
            (WEIGHT_COMMAND / remaining) * command_scores[mask_no_arsenal]
        )

        # Confidence discount: sqrt(min(alpha_query, alpha_candidate))
        query_alpha = query_profile.eb_alpha
        pair_confidence = np.minimum(query_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # --- Build results (exclude exact self-match only) ---
        results = []
        for i in range(n):
            if self.keys[i] == query_key:
                continue
            cand = self.profiles[i]
            results.append(SimilarityResult(
                pitcher_id=cand.pitcher_id,
                season=cand.season,
                p_throws=cand.p_throws,
                score=float(composite[i]),
                arsenal_score=float(arsenal_scores[i]),
                command_score=float(command_scores[i]),
                sample_pitches=cand.sample_pitches,
            ))

        return results


# ============================================================================
# Main Engine
# ============================================================================

class PitcherSimilarityEngine:
    """
    The Pitcher-to-Pitcher Similarity Engine.

    Computes a composite similarity score [0, 1] between any two
    pitcher-season profiles using:
      1. Arsenal similarity (GMM-to-GMM Wasserstein-2 optimal transport)
      2. Command similarity (RBF kernel over discipline metrics)

    Scores are computed EXHAUSTIVELY against all same-handedness
    pitcher-season profiles. Cross-season comparisons (e.g. 2025
    Scherzer vs. 2024 Scherzer) are included — only the exact same
    (pitcher_id, season) tuple is excluded.

    Usage:
        engine = PitcherSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])

        # All similar pitcher-seasons (exhaustive)
        all_results = engine.query(pitcher_id=543037, season=2025)

        # Top-20 only
        top_20 = engine.query(pitcher_id=543037, season=2025, n=20)

        # Optional: pre-warm full cache for instant subsequent queries
        engine.precompute_arsenal_cache(n_workers=4)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], PitcherProfile] = {}
        self._league_avg_command: dict[int, NDArray] = {}

        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._command_rbf = RBFSimilarity(sigma=RBF_SIGMA_COMMAND)

        # Handedness partitions with vectorized matrices
        self._partition_l = HandednessPartition("L")
        self._partition_r = HandednessPartition("R")

        # Arsenal W2 distance cache (shared across both partitions)
        self._arsenal_cache = ArsenalCache()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        seasons: list[int] | None = None,
        arsenal_cache_path: str | None = None,
    ) -> None:
        """
        Load all pitcher profiles from DuckDB, apply Empirical Bayes
        shrinkage, and build vectorized scoring matrices.

        Parameters
        ----------
        seasons : list[int]
            Seasons to include. If None, loads all available seasons.
        arsenal_cache_path : str, optional
            Path to a previously saved arsenal cache file. If provided
            and the file exists, cached W2 distances are loaded to avoid
            re-computation.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)

        try:
            self._load_league_averages(conn, seasons)
            self._load_profiles(conn, seasons)
        finally:
            conn.close()

        # Apply Empirical Bayes shrinkage to all profiles
        self._apply_shrinkage()

        # GMM post-processing: enforce minimum cluster size
        for key, profile in self._profiles.items():
            if profile.gmm is not None:
                self._profiles[key].gmm = enforce_min_cluster_size(profile.gmm)

        # Standardize all GMM component means/covariances into z-score space.
        # This makes the W2 distance dimensionality-invariant — without it,
        # spin_rate (range ~1000) dominates velocity (range ~15) in the
        # distance calculation and arsenal scores collapse to near-zero.
        self._standardize_arsenals()

        # Fit normalizer on the full population
        all_profiles = list(self._profiles.values())
        self._normalizer.fit(all_profiles)

        # Build handedness partitions with vectorized matrices
        profiles_l = [p for p in all_profiles if p.p_throws == "L"]
        profiles_r = [p for p in all_profiles if p.p_throws == "R"]
        self._partition_l.build(profiles_l, self._normalizer)
        self._partition_r.build(profiles_r, self._normalizer)

        # Optionally load a persisted arsenal cache
        if arsenal_cache_path:
            self._arsenal_cache.load(arsenal_cache_path)

        elapsed = time.time() - t0
        log.info(
            "PitcherSimilarityEngine built: %d profiles "
            "(%d LHP, %d RHP) in %.2fs.",
            len(self._profiles),
            len(profiles_l), len(profiles_r),
            elapsed,
        )

    def precompute_arsenal_cache(
        self,
        n_workers: int = 1,
        save_path: str | None = None,
    ) -> None:
        """
        Batch-precompute ALL pairwise arsenal W2 distances.

        This is optional but recommended for production use. After this
        call, all subsequent query() calls will be instant (no lazy W2
        computation needed).

        Parameters
        ----------
        n_workers : int
            Number of parallel workers. 1 = serial.
        save_path : str, optional
            If provided, the cache is persisted to disk after computation.
        """
        log.info("Precomputing arsenal cache (LHP) …")
        self._arsenal_cache.precompute(self._partition_l.profiles, n_workers)
        log.info("Precomputing arsenal cache (RHP) …")
        self._arsenal_cache.precompute(self._partition_r.profiles, n_workers)
        log.info(
            "Arsenal cache complete: %d total cached distances.",
            self._arsenal_cache.size,
        )

        if save_path:
            self._arsenal_cache.save(save_path)

    # ------------------------------------------------------------------
    # Data Loading (private)
    # ------------------------------------------------------------------

    def _load_league_averages(
        self, conn: duckdb.DuckDBPyConnection, seasons: list[int] | None,
    ) -> None:
        """Load league-average profiles from derived.league_averages."""
        try:
            season_filter = ""
            if seasons:
                season_list = ", ".join(str(s) for s in seasons)
                season_filter = f"AND season IN ({season_list})"

            rows = conn.execute(f"""
                SELECT season, profile_json
                FROM derived.league_averages
                WHERE entity_type = 'pitcher'
                  {season_filter}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — no shrinkage fallbacks available.")
            return

        for season, profile_json_raw in rows:
            if isinstance(profile_json_raw, str):
                pj = json.loads(profile_json_raw)
            else:
                pj = profile_json_raw

            self._league_avg_command[season] = np.array([
                pj.get(f, 0.0) or 0.0 for f in COMMAND_FEATURES
            ], dtype=np.float64)

        log.info("Loaded league averages for %d seasons.", len(rows))

    def _load_profiles(
        self, conn: duckdb.DuckDBPyConnection, seasons: list[int] | None,
    ) -> None:
        """Load pitcher profiles from DuckDB."""
        season_filter = ""
        if seasons:
            season_list = ", ".join(str(s) for s in seasons)
            season_filter = f"AND psm.season IN ({season_list})"

        rows = conn.execute(f"""
            SELECT
                psm.pitcher_id,
                psm.season,
                psm.p_throws,
                psm.sample_pitches,
                psm.gmm_model,
                psm.bb_rate,
                psm.k_rate,
                psm.csw_rate,
                psm.chase_rate,
                psm.zone_take_rate,
                psm.zone_rate,
                psm.whiff_rate,
                psm.ground_ball_rate,
                psm.fly_ball_rate,
                psm.line_drive_rate,
                psm.whip,
                psm.hr_per_9,
                psm.below_minimum_sample
            FROM derived.pitcher_season_metrics psm
            WHERE psm.below_minimum_sample = FALSE
              {season_filter}
        """).fetchall()

        log.info("Loading %d pitcher profiles from DuckDB …", len(rows))

        for row in rows:
            (
                pitcher_id, season, p_throws, sample_pitches,
                gmm_model_raw,
                bb_rate, k_rate, csw_rate, zone_take_rate, chase_rate, zone_rate, whiff_rate,
                gb_rate, fb_rate, ld_rate, whip, hr_per_9,
                below_min,
            ) = row

            gmm = None
            if gmm_model_raw:
                if isinstance(gmm_model_raw, str):
                    gmm_dict = json.loads(gmm_model_raw)
                else:
                    gmm_dict = gmm_model_raw
                if gmm_dict.get("n_components", 0) > 0:
                    gmm = GMMModel.from_json(gmm_dict)

            command_vec = np.array([
                bb_rate or 0.0, k_rate or 0.0, csw_rate or 0.0,
                zone_take_rate or 0.0, chase_rate or 0.0, zone_rate or 0.0, whiff_rate or 0.0
            ], dtype=np.float64)

            self._profiles[(pitcher_id, season)] = PitcherProfile(
                pitcher_id=pitcher_id,
                season=season,
                p_throws=p_throws,
                sample_pitches=sample_pitches,
                gmm=gmm,
                command_vec=command_vec,
                eb_alpha=self._shrinkage.alpha(sample_pitches),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        """Apply Empirical Bayes shrinkage to command and result vectors."""
        for key, profile in self._profiles.items():
            season = profile.season
            la_cmd = self._league_avg_command.get(season)

            if la_cmd is not None:
                profile.command_vec = self._shrinkage.shrink(
                    profile.command_vec, la_cmd, profile.sample_pitches,
                )

    def _standardize_arsenals(self) -> None:
        """
        Compute population-level mean/std from all GMM component centroids,
        then standardize every GMM's components into z-score space.

        This makes the W2 distance dimensionality-invariant. Without it,
        features like spin_rate (range ~1000 RPM) dominate velocity
        (range ~15 mph) because W2 uses raw Euclidean distances.

        Population stats are computed as the weight-averaged centroid across
        all components of all profiles (each component contributes its
        centroid weighted by its mixing proportion).
        """
        # Collect all component centroids with their weights
        all_means = []
        all_weights = []
        for profile in self._profiles.values():
            if profile.gmm is not None:
                for c in profile.gmm.components:
                    all_means.append(c.mean)
                    all_weights.append(c.weight)

        if not all_means:
            log.warning("No GMM components found — skipping arsenal standardization.")
            return

        means = np.array(all_means, dtype=np.float64)
        weights = np.array(all_weights, dtype=np.float64)
        weights /= weights.sum()

        # Weighted population mean and std
        pop_mean = np.average(means, axis=0, weights=weights)
        pop_var = np.average((means - pop_mean) ** 2, axis=0, weights=weights)
        pop_std = np.sqrt(pop_var)
        pop_std[pop_std == 0] = 1.0

        log.info(
            "  Arsenal standardization — pop_mean: [%s], pop_std: [%s]",
            ", ".join(f"{v:.1f}" for v in pop_mean),
            ", ".join(f"{v:.1f}" for v in pop_std),
        )

        # Standardize every profile's GMM
        for key, profile in self._profiles.items():
            if profile.gmm is not None:
                self._profiles[key].gmm = standardize_gmm(
                    profile.gmm, pop_mean, pop_std,
                )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        pitcher_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query pitcher against ALL same-handedness pitcher-season
        profiles in the engine.

        Parameters
        ----------
        pitcher_id : int
            MLB player ID of the query pitcher.
        season : int
            Season of the query pitcher's profile.
        n : int or None
            If provided, return only the top-N results (sorted by score
            descending). If None, return ALL results sorted by score
            descending.

        Returns
        -------
        List of SimilarityResult for every pitcher-season in the same
        handedness partition, sorted by composite score descending.
        The query pitcher's exact (pitcher_id, season) is excluded, but
        other seasons of the same pitcher_id ARE included.
        """
        query_profile = self._profiles.get((pitcher_id, season))
        if query_profile is None:
            log.warning(
                "Pitcher %d season %d not found in engine. "
                "Ensure build() was called with that season.",
                pitcher_id, season,
            )
            return []

        partition = (
            self._partition_l if query_profile.p_throws == "L"
            else self._partition_r
        )

        results = partition.score_all(
            query_profile=query_profile,
            normalizer=self._normalizer,
            arsenal_cache=self._arsenal_cache,
            command_rbf=self._command_rbf,
        )

        results.sort(key=lambda r: r.score, reverse=True)

        if n is not None:
            return results[:n]
        return results

    def query_pair(
        self,
        pitcher_a: tuple[int, int],
        pitcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """
        Compute the similarity between two specific pitcher-seasons.

        Parameters
        ----------
        pitcher_a : (pitcher_id, season)
        pitcher_b : (pitcher_id, season)

        Returns
        -------
        SimilarityResult or None if either pitcher is not in the engine.
        """
        pa = self._profiles.get(pitcher_a)
        pb = self._profiles.get(pitcher_b)
        if pa is None or pb is None:
            return None

        composite, arsenal_s, command_s = (
            self._score_pair(pa, pb)
        )

        return SimilarityResult(
            pitcher_id=pb.pitcher_id,
            season=pb.season,
            p_throws=pb.p_throws,
            score=composite,
            arsenal_score=arsenal_s,
            command_score=command_s,
            sample_pitches=pb.sample_pitches,
        )

    def _score_pair(
        self,
        query: PitcherProfile,
        candidate: PitcherProfile,
    ) -> tuple[float, float, float]:
        """
        Compute all sub-scores and the weighted composite between two
        pitcher profiles. Used by query_pair() for ad-hoc comparisons.

        Returns
        -------
        (composite, arsenal, command) — all in [0, 1].

        SIM-148 / SIM-067: pre-SIM-067 this returned a 5-tuple including
        a separate ``release`` and ``results`` sub-score.  SIM-067 removed
        the release sub-score (release-point information is already inside
        the per-component GMM means and was double-counting); ``results``
        was folded into the composite via the empirical-Bayes confidence
        multiplier rather than carried as a separate sub-score.

        Permanent regression test:
            tests/unit/test_pitcher_similarity.py::TestScoreProperties::
            test_score_pair_returns_three_subscores  guards this shape.
        """
        key_q = (query.pitcher_id, query.season)
        key_c = (candidate.pitcher_id, candidate.season)
        w2_dist = self._arsenal_cache.get_or_compute(
            key_q, key_c, query.gmm, candidate.gmm,
        )

        if np.isfinite(w2_dist):
            arsenal_s = float(np.exp(-w2_dist / ARSENAL_SCALE))
        else:
            arsenal_s = 0.0

        cmd_q = self._normalizer.normalize_command(query.command_vec)
        cmd_c = self._normalizer.normalize_command(candidate.command_vec)
        command_s = self._command_rbf.score(cmd_q, cmd_c)

        has_arsenal = query.gmm is not None and candidate.gmm is not None
        if has_arsenal:
            composite = (
                WEIGHT_ARSENAL * arsenal_s
                + WEIGHT_COMMAND * command_s
            )
        else:
            remaining = WEIGHT_COMMAND
            composite = (
                (WEIGHT_COMMAND / remaining) * command_s
            )

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(arsenal_s),
            float(command_s),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, pitcher_id: int, season: int) -> PitcherProfile | None:
        """Retrieve a loaded pitcher profile by ID and season."""
        return self._profiles.get((pitcher_id, season))

    @property
    def profile_count(self) -> int:
        """Number of profiles currently loaded in the engine."""
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        """List all (pitcher_id, season) pairs in the engine."""
        return list(self._profiles.keys())

    @property
    def arsenal_cache_size(self) -> int:
        """Number of cached arsenal W2 distances."""
        return self._arsenal_cache.size


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================

def build_similarity_matrix(
    engine: PitcherSimilarityEngine,
    pitcher_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
    """
    Build a symmetric N×N similarity matrix for the given pitcher/season
    pairs. Useful for visualization and clustering analysis.

    Parameters
    ----------
    engine : PitcherSimilarityEngine
        A built engine (build() must have been called).
    pitcher_ids : list of (pitcher_id, season)
        The pitchers to include.

    Returns
    -------
    N×N numpy array where entry [i, j] is the composite similarity
    score between pitcher_ids[i] and pitcher_ids[j].
    """
    n = len(pitcher_ids)
    matrix = np.eye(n, dtype=np.float64)

    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(pitcher_ids[i], pitcher_ids[j])
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score

    return matrix


# ============================================================================
# Entry Point — CLI for testing
# ============================================================================
if __name__ == "__main__":
    engine = PitcherSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    # engine.precompute_arsenal_cache(n_workers=8)
    # w2_scores = engine._arsenal_cache.finite_distances()
    # gamma = calibrate_arsenal_norm_scale(w2_scores, .5)
    report = run_pitcher_diagnostics(engine, n_query_samples=50)
    print(report)
    results = engine.query(pitcher_id=694973, season=2025, n=20)
    print(results)
