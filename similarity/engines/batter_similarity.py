"""
batter_similarity.py
====================
Step 2.2 — Batter-to-Batter Similarity
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
batter-season profiles by combining four sub-score dimensions:

  1. Plate Discipline (40%)  — process-driven approach metrics that
     stabilize fastest and most reliably signal batter identity:
     first-pitch take rate, O-swing%, Z-swing%, whiff%, contact%,
     walk rate, strikeout rate.

  2. Batted Ball Profile (35%) — contact quality and direction metrics
     that characterize what happens when a batter makes contact:
     GB%, FB%, LD%, IFFB%, pull%, center%, oppo%, avg exit velo,
     avg launch angle, hard-hit%, barrel%.

  3. Platoon Profile (15%) — vs-LHP and vs-RHP splits compared
     separately, critical for matchup-dependent simulation. The
     simulation often queries "find batters like X when facing a RHP"
     which shifts weight toward the relevant platoon split.

  4. Power / Results (10%) — outcome-based metrics that are noisier
     but add discrimination for power profiles: HR rate, xBA, xSLG,
     max exit velo.

Research-Informed Design
------------------------
  Feature weighting within each sub-score is informed by metric
  stabilization research (Carleton 2007, Pavlidis/Dolinar updates,
  FanGraphs reliability studies):

    - K%, contact%, swing% stabilize by ~100-150 PA → high weight
    - Walk rate, GB%, FB% stabilize by ~200-250 PA → moderate weight
    - HR rate stabilizes by ~300 PA → moderate weight
    - LD%, BABIP, BA stabilize by 500+ PA → low weight / excluded

  Process-driven metrics (what the batter *does*) are prioritized over
  outcome metrics (what *happened*), following the PECOTA philosophy
  and the FanGraphs PCA batter similarity approach. The simulation
  cares about HOW a batter approaches an at-bat, not whether he got
  lucky on BABIP this season.

  Platoon splits are included because the simulation runs in the
  context of a specific pitcher handedness (Step 3b: pitch outcome
  determination). A batter's vs-RHP profile can differ dramatically
  from their vs-LHP profile, and matching on the wrong split would
  poison the outcome sampling.

Handedness Design
-----------------
  Unlike pitcher similarity, batters are NOT partitioned by bat hand.
  Switch hitters exist and need to be compared to everyone. Even L/R
  batters can be meaningfully similar (similar approach, similar batted
  ball profile). Instead, a mild bats-mismatch penalty is applied when
  comparing batters with different handedness, as their spray angle
  distributions will be mirrored.

Exhaustive Scoring
------------------
  Like pitcher similarity, query() scores against ALL batter-season
  profiles in the engine and returns the full list. Cross-season
  comparisons (e.g. 2025 Soto vs. 2024 Soto) are first-class.

Dependencies
------------
  pip install duckdb numpy scipy

Usage
-----
  engine = BatterSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])

  # All similar batter-seasons (exhaustive)
  results = engine.query(batter_id=665742, season=2025)

  # Top-20 only
  results = engine.query(batter_id=665742, season=2025, n=20)

  # Platoon-aware: find batters similar to Soto when facing a RHP
  results = engine.query(batter_id=665742, season=2025, vs_hand="R")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray

from similarity.similarity_diagnostics import run_batter_diagnostics

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("batter_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Plate Discipline features (stabilize 50-200 PA) ---
# Ordered by reliability (fastest-stabilizing first).
# Per-feature reliability weights within this sub-score, derived from
# stabilization research: features that stabilize faster get higher weight
# because they carry more true-talent signal per PA.
DISCIPLINE_FEATURES = [
    # feature_name,           reliability_weight
    ("first_pitch_take_rate", 0.680),  # stabilizes ~60 PA (swing% family)
    ("o_swing_rate", 0.689),  # stabilizes ~80 PA
    ("z_swing_rate", 0.533),  # stabilizes ~80 PA
    ("contact_rate", 0.776),  # stabilizes ~100 PA
    ("whiff_rate", 0.776),  # stabilizes ~100 PA
    ("k_rate", 0.701),  # stabilizes ~150 PA
    ("walk_rate", 0.558),  # stabilizes ~200 PA
]

# --- Batted Ball Profile features (stabilize 100-300 BIP) ---
BATTED_BALL_FEATURES = [
    ("gb_rate", 0.563),  # stabilizes ~100 BIP (fastest BB metric)
    ("fb_rate", 0.544),  # stabilizes ~150 BIP
    ("pull_rate", 0.760),  # spray direction — moderate
    ("oppo_rate", 0.792),
    ("avg_exit_velo", 0.675),  # stabilizes ~50 BBE (Statcast-era reliable)
    ("avg_launch_angle", 0.662),  # stabilizes ~100 BBE
    ("hard_hit_rate", 0.628),  # stabilizes ~50 BBE
    ("barrel_rate", 0.333),  # stabilizes ~150 BBE
]

# --- Power / Results features (noisier, 300+ PA) ---
POWER_FEATURES = [
    ("hr_rate", 0.470),  # stabilizes ~300 PA
    ("xba", 0.596),  # expected stats — moderate noise
    ("xslg", 0.554),  # expected stats — moderate noise
    ("max_exit_velo", 0.690),  # very stable — physical attribute
]

# --- Platoon split features (compared per-hand: vs_L and vs_R) ---
# Focus on approach/discipline differences by hand, not contact quality
# which is already captured in the batted_ball sub-score.
PLATOON_FEATURES = [
    ("o_swing_rate", 0.649),
    ("z_swing_rate", 0.482),
    ("whiff_rate", 0.734),
    ("walk_rate", 0.503),
    ("k_rate", 0.656),
    ("gb_rate", 0.496),
    ("barrel_rate", 0.239),
]

# --- Sub-score weights (sum to 1.0) ---
WEIGHT_DISCIPLINE = 0.40
WEIGHT_BATTED_BALL = 0.35
WEIGHT_PLATOON = 0.15
WEIGHT_POWER = 0.10
_TOTAL = WEIGHT_DISCIPLINE + WEIGHT_BATTED_BALL + WEIGHT_PLATOON + WEIGHT_POWER
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# When vs_hand is specified, platoon weight is boosted and discipline/BB
# weights are partially shifted to reflect that the matchup context matters
# more than the overall profile.
WEIGHT_DISCIPLINE_PLATOON = 0.30
WEIGHT_BATTED_BALL_PLATOON = 0.25
WEIGHT_PLATOON_PLATOON = 0.35  # platoon becomes the dominant signal
WEIGHT_POWER_PLATOON = 0.10

# RBF bandwidth parameters (gamma = 1 / (2σ²))
RBF_SIGMA_DISCIPLINE = 1.0389
RBF_SIGMA_BATTED_BALL = 1.0828
RBF_SIGMA_PLATOON = 1.0866
RBF_SIGMA_POWER = 0.9591

# Bats-mismatch penalty: when comparing L-to-R or R-to-L batters,
# spray angle distributions are mirrored. This multiplicative penalty
# accounts for that. L-to-S or R-to-S gets a milder penalty.
BATS_PENALTY_OPPOSITE = 0.92  # L↔R
BATS_PENALTY_SWITCH = 0.97  # L↔S or R↔S
BATS_PENALTY_SAME = 1.00  # L↔L, R↔R, S↔S

# Empirical Bayes shrinkage prior strength
# At EB_N_PRIOR PA, shrinkage weight α = 0.5
EB_N_PRIOR = 5

# Minimum sample for inclusion (matches schema: below_minimum_sample when PA < 100)
MIN_BATTER_PA = 100


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class BatterProfile:
    """Complete batter-season profile for similarity scoring."""

    batter_id: int
    season: int
    bats: str  # "L", "R", or "S"
    sample_pa: int
    sample_pitches: int

    # Feature vectors (raw values — normalized at query time)
    discipline_vec: NDArray[np.float64]  # shape (len(DISCIPLINE_FEATURES),)
    batted_ball_vec: NDArray[np.float64]  # shape (len(BATTED_BALL_FEATURES),)
    power_vec: NDArray[np.float64]  # shape (len(POWER_FEATURES),)

    # Platoon split vectors
    platoon_vs_l_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)
    platoon_vs_r_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)
    sample_pa_vs_l: int = 0
    sample_pa_vs_r: int = 0

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    batter_id: int
    season: int
    bats: str
    score: float  # composite [0, 1], 1 = identical
    discipline_score: float
    batted_ball_score: float
    platoon_score: float
    power_score: float
    sample_pa: int


# ============================================================================
# Reliability-Weighted RBF Kernel
# ============================================================================


class WeightedRBFSimilarity:
    """
    Gaussian RBF kernel with per-feature reliability weights and
    dimensionality-invariant scaling.

    K(x, y) = exp(-γ * Σ_i w_i * (x_i - y_i)²)

    where w_i are reliability weights that give more influence to
    features that stabilize faster (i.e. carry more true-talent signal).

    CRITICAL: weights are normalized to sum to 1.0, not to n_features.
    This means the exponent computes the weighted *average* per-feature
    squared distance, making the score independent of how many features
    each sub-score contains. Without this, sub-scores with more features
    (e.g. 11 batted-ball features) would systematically collapse toward
    zero while sub-scores with fewer features (e.g. 4 power features)
    would remain inflated — the curse of dimensionality in RBF kernels.

    Features are z-score normalized before computing the distance.
    Missing (NaN) features are treated as neutral (0 distance).
    """

    def __init__(
        self,
        sigma: float,
        reliability_weights: NDArray[np.float64],
    ) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma**2)
        # Normalize weights to sum to 1.0 so the kernel computes
        # the weighted-average per-feature distance (dimensionality-invariant)
        total = reliability_weights.sum()
        if total > 0:
            self.weights = reliability_weights / total
        else:
            self.weights = np.ones_like(reliability_weights) / len(reliability_weights)

    def score(self, x: NDArray, y: NDArray) -> float:
        """Compute weighted RBF similarity between two vectors."""
        diff = x - y
        diff = np.nan_to_num(diff, nan=0.0)
        dist_sq = np.dot(self.weights * diff, diff)
        return float(np.exp(-self.gamma * dist_sq))

    def score_batch(
        self,
        query: NDArray,
        candidates: NDArray,
    ) -> NDArray[np.float64]:
        """
        Compute weighted RBF between one query and an array of candidates.
        Returns shape (n_candidates,).
        """
        diff = candidates - query[np.newaxis, :]
        diff = np.nan_to_num(diff, nan=0.0)
        # weighted squared distances: sum_i w_i * (x_i - y_i)^2
        dist_sq = np.sum(self.weights[np.newaxis, :] * diff**2, axis=1)
        return np.exp(-self.gamma * dist_sq)


# ============================================================================
# Empirical Bayes Shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """Shrinks raw feature vectors toward league average based on sample size."""

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int) -> float:
        return n_samples / (n_samples + self.n_prior)

    def shrink(
        self,
        raw_vec: NDArray[np.float64],
        league_avg_vec: NDArray[np.float64],
        n_samples: int,
    ) -> NDArray[np.float64]:
        a = self.alpha(n_samples)
        raw_clean = np.where(np.isnan(raw_vec), league_avg_vec, raw_vec)
        return a * raw_clean + (1.0 - a) * league_avg_vec


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class FeatureNormalizer:
    """
    Z-score normalizer fit on the population of batter profiles.

    Separate normalization parameters for each feature group to ensure
    each feature has mean 0 and std 1 within its group. NaN is treated
    as neutral (z-score of 0).
    """

    discipline_mean: NDArray | None = None
    discipline_std: NDArray | None = None
    batted_ball_mean: NDArray | None = None
    batted_ball_std: NDArray | None = None
    power_mean: NDArray | None = None
    power_std: NDArray | None = None
    platoon_l_mean: NDArray | None = None
    platoon_l_std: NDArray | None = None
    platoon_r_mean: NDArray | None = None
    platoon_r_std: NDArray | None = None

    def fit(self, profiles: list[BatterProfile]) -> None:
        if not profiles:
            return

        def _fit_group(vecs: list[NDArray]) -> tuple[NDArray, NDArray]:
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.discipline_mean, self.discipline_std = _fit_group([p.discipline_vec for p in profiles])
        self.batted_ball_mean, self.batted_ball_std = _fit_group(
            [p.batted_ball_vec for p in profiles]
        )
        self.power_mean, self.power_std = _fit_group([p.power_vec for p in profiles])
        # Platoon — fit on profiles with sufficient platoon sample
        vs_l_vecs = [p.platoon_vs_l_vec for p in profiles if p.sample_pa_vs_l >= 30]
        vs_r_vecs = [p.platoon_vs_r_vec for p in profiles if p.sample_pa_vs_r >= 30]
        if vs_l_vecs:
            self.platoon_l_mean, self.platoon_l_std = _fit_group(vs_l_vecs)
        if vs_r_vecs:
            self.platoon_r_mean, self.platoon_r_std = _fit_group(vs_r_vecs)

    def _normalize(self, vec: NDArray, mean: NDArray | None, std: NDArray | None) -> NDArray:
        if mean is None:
            return vec
        normed = (vec - mean) / std
        return np.nan_to_num(normed, nan=0.0)

    def normalize_discipline(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.discipline_mean, self.discipline_std)

    def normalize_batted_ball(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.batted_ball_mean, self.batted_ball_std)

    def normalize_power(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.power_mean, self.power_std)

    def normalize_platoon_l(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.platoon_l_mean, self.platoon_l_std)

    def normalize_platoon_r(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.platoon_r_mean, self.platoon_r_std)


# ============================================================================
# Bats-Mismatch Penalty
# ============================================================================


def bats_penalty(bats_a: str, bats_b: str) -> float:
    """
    Multiplicative penalty for comparing batters with different handedness.

    Switch hitters (S) are compatible with both sides; the penalty is mild.
    L↔R comparison gets a larger penalty because spray distributions are
    mirrored (pull/oppo swap sides), though the discipline and power
    profiles can still be very similar.
    """
    if bats_a == bats_b:
        return BATS_PENALTY_SAME
    if "S" in (bats_a, bats_b):
        return BATS_PENALTY_SWITCH
    return BATS_PENALTY_OPPOSITE


def bats_penalty_vector(query_bats: str, all_bats: list[str]) -> NDArray[np.float64]:
    """Vectorized version for batch scoring."""
    penalties = np.ones(len(all_bats), dtype=np.float64)
    for i, b in enumerate(all_bats):
        penalties[i] = bats_penalty(query_bats, b)
    return penalties


# ============================================================================
# Scoring Partition — Vectorized Batch Scoring
# ============================================================================


class BatterPartition:
    """
    Stores all batter profiles with pre-built normalized feature matrices
    for vectorized batch RBF scoring.

    Unlike pitchers, batters are NOT partitioned by handedness — all
    batters live in a single partition. Handedness mismatch is handled
    via a multiplicative penalty.
    """

    def __init__(self) -> None:
        self.profiles: list[BatterProfile] = []
        self.keys: list[tuple[int, int]] = []
        self.bats_list: list[str] = []

        # Normalized feature matrices — shape (N, feature_dim)
        self._disc_matrix: NDArray | None = None
        self._bb_matrix: NDArray | None = None
        self._power_matrix: NDArray | None = None
        self._platoon_l_matrix: NDArray | None = None
        self._platoon_r_matrix: NDArray | None = None

        self._eb_alphas: NDArray | None = None
        self._platoon_l_valid: NDArray | None = None  # bool mask: has sufficient vs-L data
        self._platoon_r_valid: NDArray | None = None

    def build(
        self,
        profiles: list[BatterProfile],
        normalizer: FeatureNormalizer,
    ) -> None:
        self.profiles = profiles
        self.keys = [(p.batter_id, p.season) for p in profiles]
        self.bats_list = [p.bats for p in profiles]

        if not profiles:
            return

        disc_rows, bb_rows, power_rows = [], [], []
        pl_rows, pr_rows = [], []
        alphas = []
        pl_valid, pr_valid = [], []

        for p in profiles:
            disc_rows.append(normalizer.normalize_discipline(p.discipline_vec))
            bb_rows.append(normalizer.normalize_batted_ball(p.batted_ball_vec))
            power_rows.append(normalizer.normalize_power(p.power_vec))
            pl_rows.append(normalizer.normalize_platoon_l(p.platoon_vs_l_vec))
            pr_rows.append(normalizer.normalize_platoon_r(p.platoon_vs_r_vec))
            alphas.append(p.eb_alpha)
            pl_valid.append(p.sample_pa_vs_l >= 30)
            pr_valid.append(p.sample_pa_vs_r >= 30)

        self._disc_matrix = np.array(disc_rows, dtype=np.float64)
        self._bb_matrix = np.array(bb_rows, dtype=np.float64)
        self._power_matrix = np.array(power_rows, dtype=np.float64)
        self._platoon_l_matrix = np.array(pl_rows, dtype=np.float64)
        self._platoon_r_matrix = np.array(pr_rows, dtype=np.float64)
        self._eb_alphas = np.array(alphas, dtype=np.float64)
        self._platoon_l_valid = np.array(pl_valid, dtype=bool)
        self._platoon_r_valid = np.array(pr_valid, dtype=bool)

    def score_all(
        self,
        query: BatterProfile,
        normalizer: FeatureNormalizer,
        disc_rbf: WeightedRBFSimilarity,
        bb_rbf: WeightedRBFSimilarity,
        platoon_rbf: WeightedRBFSimilarity,
        power_rbf: WeightedRBFSimilarity,
        vs_hand: str | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query against EVERY profile in this partition.

        Parameters
        ----------
        vs_hand : "L", "R", or None
            If specified, shift weights to emphasize the relevant platoon
            split. Used when the simulation is querying in the context of
            a specific pitcher handedness.
        """
        if not self.profiles:
            return []

        n = len(self.profiles)
        query_key = (query.batter_id, query.season)

        # --- Vectorized RBF sub-scores ---
        disc_q = normalizer.normalize_discipline(query.discipline_vec)
        bb_q = normalizer.normalize_batted_ball(query.batted_ball_vec)
        power_q = normalizer.normalize_power(query.power_vec)

        disc_scores = disc_rbf.score_batch(disc_q, self._disc_matrix)
        bb_scores = bb_rbf.score_batch(bb_q, self._bb_matrix)
        power_scores = power_rbf.score_batch(power_q, self._power_matrix)

        # --- Platoon sub-scores ---
        # When vs_hand is specified, compare only the relevant split.
        # When vs_hand is None, average both splits (weighted by sample size).
        platoon_scores = np.zeros(n, dtype=np.float64)

        if vs_hand == "L" or vs_hand is None:
            pl_q = normalizer.normalize_platoon_l(query.platoon_vs_l_vec)
            pl_scores = platoon_rbf.score_batch(pl_q, self._platoon_l_matrix)
        if vs_hand == "R" or vs_hand is None:
            pr_q = normalizer.normalize_platoon_r(query.platoon_vs_r_vec)
            pr_scores = platoon_rbf.score_batch(pr_q, self._platoon_r_matrix)

        if vs_hand == "L":
            # Use vs-L split only; mask candidates without sufficient vs-L data
            platoon_scores = pl_scores
            platoon_scores[~self._platoon_l_valid] = 0.5  # neutral score
            query_platoon_valid = query.sample_pa_vs_l >= 30
        elif vs_hand == "R":
            platoon_scores = pr_scores
            platoon_scores[~self._platoon_r_valid] = 0.5
            query_platoon_valid = query.sample_pa_vs_r >= 30
        else:
            # Average both splits, weighted by which ones are valid
            both_valid = self._platoon_l_valid & self._platoon_r_valid
            l_only = self._platoon_l_valid & ~self._platoon_r_valid
            r_only = ~self._platoon_l_valid & self._platoon_r_valid
            neither = ~self._platoon_l_valid & ~self._platoon_r_valid

            platoon_scores[both_valid] = 0.5 * pl_scores[both_valid] + 0.5 * pr_scores[both_valid]
            platoon_scores[l_only] = pl_scores[l_only]
            platoon_scores[r_only] = pr_scores[r_only]
            platoon_scores[neither] = 0.5  # neutral
            query_platoon_valid = query.sample_pa_vs_l >= 30 or query.sample_pa_vs_r >= 30

        # --- Select weights based on vs_hand context ---
        if vs_hand is not None and query_platoon_valid:
            w_disc = WEIGHT_DISCIPLINE_PLATOON
            w_bb = WEIGHT_BATTED_BALL_PLATOON
            w_plat = WEIGHT_PLATOON_PLATOON
            w_pow = WEIGHT_POWER_PLATOON
        else:
            w_disc = WEIGHT_DISCIPLINE
            w_bb = WEIGHT_BATTED_BALL
            w_plat = WEIGHT_PLATOON
            w_pow = WEIGHT_POWER

        # --- Composite score ---
        composite = (
            w_disc * disc_scores + w_bb * bb_scores + w_plat * platoon_scores + w_pow * power_scores
        )

        # Bats-mismatch penalty
        penalties = bats_penalty_vector(query.bats, self.bats_list)
        composite *= penalties

        # Confidence discount: sqrt(min(alpha_query, alpha_candidate))
        pair_confidence = np.minimum(query.eb_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # --- Build results (exclude exact self-match) ---
        results = []
        for i in range(n):
            if self.keys[i] == query_key:
                continue
            cand = self.profiles[i]
            results.append(
                SimilarityResult(
                    batter_id=cand.batter_id,
                    season=cand.season,
                    bats=cand.bats,
                    score=float(composite[i]),
                    discipline_score=float(disc_scores[i]),
                    batted_ball_score=float(bb_scores[i]),
                    platoon_score=float(platoon_scores[i]),
                    power_score=float(power_scores[i]),
                    sample_pa=cand.sample_pa,
                )
            )

        return results


# ============================================================================
# Main Engine
# ============================================================================


class BatterSimilarityEngine:
    """
    The Batter-to-Batter Similarity Engine.

    Scores are computed EXHAUSTIVELY against all batter-season profiles.
    Cross-season comparisons are included. Platoon-aware querying
    shifts weights to emphasize the relevant split when a pitcher
    handedness context is provided.

    Usage:
        engine = BatterSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])

        # All similar batters
        results = engine.query(batter_id=665742, season=2025)

        # Top-20 when facing a RHP
        results = engine.query(batter_id=665742, season=2025, n=20, vs_hand="R")
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], BatterProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "discipline": {},
            "batted_ball": {},
            "power": {},
            "platoon_l": {},
            "platoon_r": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = BatterPartition()

        # Build weighted RBF scorers
        self._disc_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_DISCIPLINE,
            reliability_weights=np.array([w for _, w in DISCIPLINE_FEATURES]),
        )
        self._bb_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_BATTED_BALL,
            reliability_weights=np.array([w for _, w in BATTED_BALL_FEATURES]),
        )
        self._platoon_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_PLATOON,
            reliability_weights=np.array([w for _, w in PLATOON_FEATURES]),
        )
        self._power_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_POWER,
            reliability_weights=np.array([w for _, w in POWER_FEATURES]),
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load all batter profiles, apply shrinkage, build matrices."""
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)

        try:
            self._load_league_averages(conn, seasons)
            self._load_profiles(conn, seasons)
        finally:
            conn.close()

        self._apply_shrinkage()

        all_profiles = list(self._profiles.values())
        self._normalizer.fit(all_profiles)
        self._partition.build(all_profiles, self._normalizer)

        elapsed = time.time() - t0
        log.info(
            "BatterSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            elapsed,
        )

    # ------------------------------------------------------------------
    # Data Loading
    # ------------------------------------------------------------------

    def _load_league_averages(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        try:
            season_filter = ""
            if seasons:
                sl = ", ".join(str(s) for s in seasons)
                season_filter = f"AND season IN ({sl})"

            rows = conn.execute(f"""
                SELECT season, profile_json
                FROM derived.league_averages
                WHERE entity_type = 'batter'
                  {season_filter}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — no shrinkage fallbacks.")
            return

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw

            self._league_avg["discipline"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in DISCIPLINE_FEATURES], dtype=np.float64
            )

            self._league_avg["batted_ball"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in BATTED_BALL_FEATURES], dtype=np.float64
            )

            self._league_avg["power"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in POWER_FEATURES], dtype=np.float64
            )

            platoon_avg = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in PLATOON_FEATURES], dtype=np.float64
            )
            self._league_avg["platoon_l"][season] = platoon_avg
            self._league_avg["platoon_r"][season] = platoon_avg.copy()

        log.info("Loaded batter league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        season_filter = ""
        if seasons:
            sl = ", ".join(str(s) for s in seasons)
            season_filter = f"AND bsm.season IN ({sl})"

        # SIM-408: graceful-optional feature columns. xba/xslg are expected-stats
        # the profile computor does not yet produce; rather than hard-fail the
        # whole engine build on a missing column, select NULL for any absent
        # optional column (the _v() helper coerces NULL -> 0.0, so the feature is
        # effectively dropped). Re-enabled automatically once the computor adds it.
        _present = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'derived' AND table_name = 'batter_season_metrics'"
            ).fetchall()
        }

        def _opt(name: str) -> str:
            return f"bsm.{name}" if name in _present else f"NULL AS {name}"

        rows = conn.execute(f"""
            SELECT
                bsm.batter_id, bsm.season, bsm.bats,
                bsm.sample_pa, bsm.sample_pitches,
                -- Discipline
                bsm.first_pitch_take_rate, bsm.o_swing_rate, bsm.z_swing_rate,
                bsm.contact_rate, bsm.whiff_rate, bsm.k_rate, bsm.walk_rate,
                -- Batted ball
                bsm.gb_rate, bsm.fb_rate,
                bsm.pull_rate, bsm.oppo_rate,
                bsm.avg_exit_velo, bsm.avg_launch_angle,
                bsm.hard_hit_rate, bsm.barrel_rate,
                -- Power
                bsm.hr_rate, {_opt("xba")}, {_opt("xslg")}, bsm.max_exit_velo,
                -- Platoon vs L
                bsm.o_swing_rate_vs_l, bsm.z_swing_rate_vs_l,
                bsm.whiff_rate_vs_l, bsm.walk_rate_vs_l, bsm.k_rate_vs_l,
                bsm.gb_rate_vs_l, bsm.barrel_rate_vs_l,
                bsm.sample_pa_vs_l,
                -- Platoon vs R
                bsm.o_swing_rate_vs_r, bsm.z_swing_rate_vs_r,
                bsm.whiff_rate_vs_r, bsm.walk_rate_vs_r, bsm.k_rate_vs_r,
                bsm.gb_rate_vs_r, bsm.barrel_rate_vs_r,
                bsm.sample_pa_vs_r,
                bsm.below_minimum_sample
            FROM derived.batter_season_metrics bsm
            WHERE NOT bsm.below_minimum_sample
              {season_filter}
        """).fetchall()

        log.info("Loading %d batter profiles from DuckDB …", len(rows))

        for row in rows:
            (
                batter_id,
                season,
                bats,
                sample_pa,
                sample_pitches,
                # Discipline (7)
                fptr,
                o_swing,
                z_swing,
                contact,
                whiff,
                k_rate,
                walk_rate,
                # Batted ball (8)
                gb,
                fb,
                pull,
                oppo,
                avg_ev,
                avg_la,
                hh,
                barrel,
                # Power (4)
                hr_rate,
                xba,
                xslg,
                max_ev,
                # Platoon vs L (7 + sample)
                o_sw_l,
                z_sw_l,
                whiff_l,
                walk_l,
                k_l,
                gb_l,
                barrel_l,
                pa_l,
                # Platoon vs R (7 + sample)
                o_sw_r,
                z_sw_r,
                whiff_r,
                walk_r,
                k_r,
                gb_r,
                barrel_r,
                pa_r,
                below_min,
            ) = row

            def _v(vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            self._profiles[(batter_id, season)] = BatterProfile(
                batter_id=batter_id,
                season=season,
                bats=bats,
                sample_pa=sample_pa,
                sample_pitches=sample_pitches,
                discipline_vec=_v([fptr, o_swing, z_swing, contact, whiff, k_rate, walk_rate]),
                batted_ball_vec=_v([gb, fb, pull, oppo, avg_ev, avg_la, hh, barrel]),
                power_vec=_v([hr_rate, xba, xslg, max_ev]),
                platoon_vs_l_vec=_v([o_sw_l, z_sw_l, whiff_l, walk_l, k_l, gb_l, barrel_l]),
                platoon_vs_r_vec=_v([o_sw_r, z_sw_r, whiff_r, walk_r, k_r, gb_r, barrel_r]),
                sample_pa_vs_l=pa_l or 0,
                sample_pa_vs_r=pa_r or 0,
                eb_alpha=self._shrinkage.alpha(sample_pa),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        """Apply EB shrinkage to all feature vectors."""
        for _key, p in self._profiles.items():
            s = p.season
            for group, vec_attr in [
                ("discipline", "discipline_vec"),
                ("batted_ball", "batted_ball_vec"),
                ("power", "power_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is not None:
                    setattr(
                        p,
                        vec_attr,
                        self._shrinkage.shrink(
                            getattr(p, vec_attr),
                            avg,
                            p.sample_pa,
                        ),
                    )

            # Platoon vectors shrunk with their own sample sizes
            avg_l = self._league_avg["platoon_l"].get(s)
            avg_r = self._league_avg["platoon_r"].get(s)
            if avg_l is not None:
                p.platoon_vs_l_vec = self._shrinkage.shrink(
                    p.platoon_vs_l_vec,
                    avg_l,
                    p.sample_pa_vs_l,
                )
            if avg_r is not None:
                p.platoon_vs_r_vec = self._shrinkage.shrink(
                    p.platoon_vs_r_vec,
                    avg_r,
                    p.sample_pa_vs_r,
                )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        batter_id: int,
        season: int,
        n: int | None = None,
        vs_hand: str | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query batter against ALL batter-season profiles.

        Parameters
        ----------
        batter_id : int
            MLB player ID.
        season : int
            Season of the query batter's profile.
        n : int or None
            Top-N results (sorted by score desc). None = return all.
        vs_hand : "L", "R", or None
            Pitcher handedness context. When specified, shifts weights to
            emphasize the relevant platoon split. The simulation uses
            this when querying for similar batters in the context of a
            specific pitcher.

        Returns
        -------
        Exhaustive list of SimilarityResult sorted by score descending.
        """
        query_profile = self._profiles.get((batter_id, season))
        if query_profile is None:
            log.warning(
                "Batter %d season %d not found. Ensure build() was called.",
                batter_id,
                season,
            )
            return []

        results = self._partition.score_all(
            query=query_profile,
            normalizer=self._normalizer,
            disc_rbf=self._disc_rbf,
            bb_rbf=self._bb_rbf,
            platoon_rbf=self._platoon_rbf,
            power_rbf=self._power_rbf,
            vs_hand=vs_hand,
        )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        batter_a: tuple[int, int],
        batter_b: tuple[int, int],
        vs_hand: str | None = None,
    ) -> SimilarityResult | None:
        """Compute similarity between two specific batter-seasons."""
        pa = self._profiles.get(batter_a)
        pb = self._profiles.get(batter_b)
        if pa is None or pb is None:
            return None

        scores = self._score_pair(pa, pb, vs_hand)
        return SimilarityResult(
            batter_id=pb.batter_id,
            season=pb.season,
            bats=pb.bats,
            score=scores[0],
            discipline_score=scores[1],
            batted_ball_score=scores[2],
            platoon_score=scores[3],
            power_score=scores[4],
            sample_pa=pb.sample_pa,
        )

    def _score_pair(
        self,
        query: BatterProfile,
        candidate: BatterProfile,
        vs_hand: str | None = None,
    ) -> tuple[float, float, float, float, float]:
        """Compute sub-scores and composite between two profiles."""
        disc_q = self._normalizer.normalize_discipline(query.discipline_vec)
        disc_c = self._normalizer.normalize_discipline(candidate.discipline_vec)
        disc_s = self._disc_rbf.score(disc_q, disc_c)

        bb_q = self._normalizer.normalize_batted_ball(query.batted_ball_vec)
        bb_c = self._normalizer.normalize_batted_ball(candidate.batted_ball_vec)
        bb_s = self._bb_rbf.score(bb_q, bb_c)

        power_q = self._normalizer.normalize_power(query.power_vec)
        power_c = self._normalizer.normalize_power(candidate.power_vec)
        power_s = self._power_rbf.score(power_q, power_c)

        # Platoon
        if vs_hand == "L":
            pl_q = self._normalizer.normalize_platoon_l(query.platoon_vs_l_vec)
            pl_c = self._normalizer.normalize_platoon_l(candidate.platoon_vs_l_vec)
            platoon_s = self._platoon_rbf.score(pl_q, pl_c)
        elif vs_hand == "R":
            pr_q = self._normalizer.normalize_platoon_r(query.platoon_vs_r_vec)
            pr_c = self._normalizer.normalize_platoon_r(candidate.platoon_vs_r_vec)
            platoon_s = self._platoon_rbf.score(pr_q, pr_c)
        else:
            pl_q = self._normalizer.normalize_platoon_l(query.platoon_vs_l_vec)
            pl_c = self._normalizer.normalize_platoon_l(candidate.platoon_vs_l_vec)
            pr_q = self._normalizer.normalize_platoon_r(query.platoon_vs_r_vec)
            pr_c = self._normalizer.normalize_platoon_r(candidate.platoon_vs_r_vec)
            platoon_s = 0.5 * self._platoon_rbf.score(pl_q, pl_c) + 0.5 * self._platoon_rbf.score(
                pr_q, pr_c
            )

        # Weights
        query_platoon_valid = (
            (vs_hand == "L" and query.sample_pa_vs_l >= 30)
            or (vs_hand == "R" and query.sample_pa_vs_r >= 30)
            or (vs_hand is not None and (query.sample_pa_vs_l >= 30 or query.sample_pa_vs_r >= 30))
        )
        if vs_hand is not None and query_platoon_valid:
            w_d, w_b, w_p, w_pw = (
                WEIGHT_DISCIPLINE_PLATOON,
                WEIGHT_BATTED_BALL_PLATOON,
                WEIGHT_PLATOON_PLATOON,
                WEIGHT_POWER_PLATOON,
            )
        else:
            w_d, w_b, w_p, w_pw = (
                WEIGHT_DISCIPLINE,
                WEIGHT_BATTED_BALL,
                WEIGHT_PLATOON,
                WEIGHT_POWER,
            )

        composite = w_d * disc_s + w_b * bb_s + w_p * platoon_s + w_pw * power_s
        composite *= bats_penalty(query.bats, candidate.bats)

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(disc_s),
            float(bb_s),
            float(platoon_s),
            float(power_s),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, batter_id: int, season: int) -> BatterProfile | None:
        return self._profiles.get((batter_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: BatterSimilarityEngine,
    batter_ids: list[tuple[int, int]],
    vs_hand: str | None = None,
) -> NDArray[np.float64]:
    """Build a symmetric N×N similarity matrix."""
    n = len(batter_ids)
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(batter_ids[i], batter_ids[j], vs_hand)
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score
    return matrix


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = BatterSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_batter_diagnostics(engine, n_query_samples=50)
    print(report)
    results = engine.query(592450, 2025)
