"""
fielder_similarity.py
=====================
Step 2.3 — Fielder-to-Fielder Similarity (Defensive Batted Ball Fielding)
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
fielder-position-season profiles by combining position-appropriate
sub-score dimensions:

  Infielders (1B, 2B, 3B, SS):
    1. Range (45%)         — directional OAA breakdown: ability to make plays
                             to glove side, arm side, charging, and deep. Plus
                             overall catch percentage added.
    2. Double Play (30%)   — DP conversion above expected, attempt rate,
                             success rate. For middle infielders (2B/SS), pivot
                             skill is included as additional features.
    3. Errors (15%)        — decomposed fielding and throwing error rates.
                             These are distinct skills (hands vs arm accuracy).
    4. Specialty (10%)     — position-specific: bunt defense (1B/3B) and
                             1B scooping ability. Neutral for 2B/SS.

  Outfielders (LF, CF, RF):
    1. Range (40%)         — directional OAA: coming-in, going-back, left/right.
                             Plus overall catch percentage added.
    2. Arm (30%)           — deterrence (hold rate), execution (thrown-out rate),
                             combined advancement prevention, and RE24-based
                             arm run value.
    3. Star Plays (15%)    — success rate on difficult opportunities (5-star,
                             4-star) and routine play reliability. Separates
                             elite range from sure-handedness.
    4. Errors (15%)        — fielding and throwing error rate decomposition.

Position Gating
---------------
  Fielders are STRICTLY partitioned by position. A shortstop is never
  compared to a center fielder. Within a position group, all seasons and
  players are scored exhaustively.

  Positions are grouped into two tiers:
    - IF: 1B, 2B, 3B, SS (each in their own partition)
    - OF: LF, CF, RF (each in their own partition)

  This means a 2B is NOT compared to a SS, even though both are infielders.
  The defensive skill profiles at these positions differ substantially
  (e.g. range direction, throw distance, DP role). Cross-position comparisons
  would inject noise rather than signal.

  If a player plays multiple positions across seasons, each position-season
  profile is independent. This is correct — a utility player's 2B defense
  and 3B defense may differ significantly.

Exhaustive Scoring
------------------
  Like pitcher and batter similarity, query() scores against ALL same-position
  profiles and returns the full list. Cross-season comparisons are first-class.

Research-Informed Design
------------------------
  Feature selection and weighting draw from:
    - Tango/Lichtman/Dolphin "The Book" — positional adjustment framework
    - Statcast OAA methodology — catch probability model decomposition
    - Tango's defensive spectrum — different positions weight different skills
    - FanGraphs WAR defensive component — run values per saved out

  Key stabilization points (approximate):
    - OAA / range metrics: ~600-800 innings (~1 full season)
    - Error rates: ~1000+ innings (very noisy)
    - DP rates: ~200+ DP opportunities
    - OF arm: ~100+ advancement opportunities

  Because defensive metrics stabilize slowly, the Empirical Bayes shrinkage
  here uses a larger N_PRIOR than batter/pitcher engines.

Dependencies
------------
  pip install duckdb numpy

Usage
-----
  engine = FielderSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])

  # All similar fielders at SS
  results = engine.query(player_id=660271, position="SS", season=2025)

  # Top-20 only
  results = engine.query(player_id=660271, position="SS", season=2025, n=20)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import duckdb
import numpy as np
from numpy.typing import NDArray

from similarity.similarity_diagnostics import run_fielder_diagnostics

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fielder_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Position groups ---
INFIELD_POSITIONS = {"1B", "2B", "3B", "SS"}
OUTFIELD_POSITIONS = {"LF", "CF", "RF"}
ALL_POSITIONS = INFIELD_POSITIONS | OUTFIELD_POSITIONS

# --- Infield feature definitions ---
# Range: directional OAA breakdown + overall catch pct added
# Directional OAA captures WHERE a fielder excels (glove side range vs.
# charging plays etc.), which is more diagnostic than aggregate OAA alone.
IF_RANGE_FEATURES = [
    # feature_name,          reliability_weight
    ("oaa_glove_side", 0.103),  # stabilizes ~300 opps
    ("oaa_arm_side", 0.107),  # stabilizes ~300 opps
    ("oaa_charging", 0.100),  # stabilizes ~400 opps (fewer opps)
    ("oaa_deep", 0.100),  # stabilizes ~350 opps
    ("catch_pct_added", 0.100),  # stabilizes fastest — aggregate stat
]

# Double Play: conversion skill separates great infielders
# dp_above_expected is the core metric; attempt rate and success rate
# add discrimination for aggressiveness vs reliability.
IF_DP_FEATURES = [
    ("dp_above_expected", 0.400),  # stabilizes ~150 DP opps
    ("dp_attempt_rate", 0.100),  # stabilizes ~100 DP opps
    ("dp_success_rate", 0.500),  # noisier — depends on runner speed
]

# Pivot-specific features (2B/SS only — NULL for corner IF)
# These are APPENDED to the DP vector for middle infielders.
IF_PIVOT_FEATURES = [
    ("dp_pivot_above_expected", 0.517),
]

# Errors: decomposed into fielding (hands/footwork) vs throwing (arm accuracy)
IF_ERROR_FEATURES = [
    ("fielding_error_rate", 0.100),  # stabilizes slowly (~1000 innings)
    ("throwing_error_rate", 0.100),  # slightly more stable — discrete throws
]

# Specialty: position-specific niche skills
# 1B/3B: bunt defense. 1B additionally: scooping throws.
# 2B/SS: neutral (features filled with positional average).
IF_SPECIALTY_FEATURES = [
    ("bunt_fielding_rate", 0.100),  # stabilizes ~80 bunt opps
    ("scoop_success_rate", 0.713),  # 1B only; stabilizes ~100 scoop opps
]

# --- Outfield feature definitions ---
OF_RANGE_FEATURES = [
    ("oaa_glove_side", 0.100),
    ("oaa_arm_side", 0.100),
    ("oaa_charging", 0.100),  # coming-in plays
    ("oaa_deep", 0.100),  # going-back — most discriminating for OF
    ("catch_pct_added", 0.100),
]

# Arm: deterrence + execution + run value
# Hold rate captures deterrence (runners don't even try).
# Thrown-out rate captures arm accuracy on actual attempts.
# of_arm_runs is the RE24-based total value.
OF_ARM_FEATURES = [
    ("arm_hold_rate", 0.500),
    ("arm_thrown_out_rate", 0.500),
    ("arm_advancement_prevention", 0.500),
    ("of_arm_runs", 0.500),
]

# Star Plays: success rates on plays bucketed by difficulty
# Separates "highlight reel range" from "fundamentally sound"
OF_STAR_FEATURES = [
    ("five_star_catch_rate", 0.100),  # very low sample — high noise
    ("four_star_catch_rate", 0.500),
    ("routine_catch_rate", 0.500),  # most stable — high-opportunity bucket
]

OF_ERROR_FEATURES = [
    ("fielding_error_rate", 0.100),
    ("throwing_error_rate", 0.100),
]

# --- Sub-score weights (sum to 1.0 within each position type) ---
# Infield
WEIGHT_IF_RANGE = 0.45
WEIGHT_IF_DP = 0.30
WEIGHT_IF_ERRORS = 0.15
WEIGHT_IF_SPECIALTY = 0.10
_IF_TOTAL = WEIGHT_IF_RANGE + WEIGHT_IF_DP + WEIGHT_IF_ERRORS + WEIGHT_IF_SPECIALTY
assert abs(_IF_TOTAL - 1.0) < 1e-9, "IF sub-score weights must sum to 1.0"

# Outfield
WEIGHT_OF_RANGE = 0.40
WEIGHT_OF_ARM = 0.30
WEIGHT_OF_STARS = 0.15
WEIGHT_OF_ERRORS = 0.15
_OF_TOTAL = WEIGHT_OF_RANGE + WEIGHT_OF_ARM + WEIGHT_OF_STARS + WEIGHT_OF_ERRORS
assert abs(_OF_TOTAL - 1.0) < 1e-9, "OF sub-score weights must sum to 1.0"

# --- RBF bandwidth parameters ---
# Defensive metrics are noisier than batting/pitching, so sigmas are
# slightly wider to avoid over-discriminating on noise.
RBF_SIGMA_IF_RANGE = 1.033
RBF_SIGMA_IF_DP = 0.372
RBF_SIGMA_IF_ERRORS = 1.000
RBF_SIGMA_IF_SPECIALTY = 1.000

RBF_SIGMA_OF_RANGE = 1.027
RBF_SIGMA_OF_ARM = 1.000
RBF_SIGMA_OF_STARS = 0.449
RBF_SIGMA_OF_ERRORS = 1.000

# --- Empirical Bayes ---
# Defensive metrics stabilize MUCH slower than batting/pitching metrics.
# EB_N_PRIOR is larger here, meaning we lean on positional average longer
# before trusting individual data.
EB_N_PRIOR = 15

# Minimum sample for inclusion
MIN_FIELDER_BATTED_BALLS = 50


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class FielderProfile:
    """Complete fielder-position-season profile for similarity scoring."""

    player_id: int
    position: str  # 1B, 2B, 3B, SS, LF, CF, RF
    season: int
    innings_played: float
    sample_batted_balls: int

    # Feature vectors (raw values — normalized at query time)
    range_vec: NDArray[np.float64]
    error_vec: NDArray[np.float64]

    # Position-type-specific vectors
    # Infielders: dp_vec and specialty_vec
    # Outfielders: arm_vec and star_vec
    dp_vec: NDArray[np.float64] | None = None
    specialty_vec: NDArray[np.float64] | None = None
    arm_vec: NDArray[np.float64] | None = None
    star_vec: NDArray[np.float64] | None = None

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    player_id: int
    position: str
    season: int
    score: float  # composite [0, 1], 1 = identical
    range_score: float
    secondary_score: float  # DP (IF) or Arm (OF)
    tertiary_score: float  # Errors (IF) or Stars (OF)
    quaternary_score: float  # Specialty (IF) or Errors (OF)
    sample_batted_balls: int


# ============================================================================
# Reliability-Weighted RBF Kernel (shared implementation)
# ============================================================================


class WeightedRBFSimilarity:
    """
    Gaussian RBF kernel with per-feature reliability weights and
    dimensionality-invariant scaling.

    K(x, y) = exp(-γ * Σ_i w_i * (x_i - y_i)²)

    Weights are normalized to sum to 1.0 so the kernel computes the
    weighted average per-feature squared distance, making the score
    independent of feature count.
    """

    def __init__(
        self,
        sigma: float,
        reliability_weights: NDArray[np.float64],
    ) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma**2)
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
        dist_sq = np.sum(self.weights[np.newaxis, :] * diff**2, axis=1)
        return np.exp(-self.gamma * dist_sq)


# ============================================================================
# Empirical Bayes Shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """Shrinks raw feature vectors toward positional average based on sample size."""

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int) -> float:
        return n_samples / (n_samples + self.n_prior)

    def shrink(
        self,
        raw_vec: NDArray[np.float64],
        avg_vec: NDArray[np.float64],
        n_samples: int,
    ) -> NDArray[np.float64]:
        a = self.alpha(n_samples)
        raw_clean = np.where(np.isnan(raw_vec), avg_vec, raw_vec)
        return a * raw_clean + (1.0 - a) * avg_vec


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class FeatureNormalizer:
    """
    Z-score normalizer fit per position group.

    Each position has its own normalization parameters because the
    distributions differ substantially (e.g. CF range distribution is
    different from 1B range distribution).
    """

    # Keys: position string → (mean_array, std_array)
    range_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    error_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    dp_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    specialty_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    arm_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    star_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)

    def fit(self, profiles_by_position: dict[str, list[FielderProfile]]) -> None:
        """Fit normalization parameters per position from all profiles."""
        for pos, profiles in profiles_by_position.items():
            if not profiles:
                continue

            def _fit_group(vecs: list[NDArray]) -> tuple[NDArray, NDArray]:
                mat = np.array(vecs, dtype=np.float64)
                m = np.nanmean(mat, axis=0)
                s = np.nanstd(mat, axis=0)
                s[s == 0] = 1.0
                return m, s

            self.range_params[pos] = _fit_group([p.range_vec for p in profiles])
            self.error_params[pos] = _fit_group([p.error_vec for p in profiles])

            if pos in INFIELD_POSITIONS:
                dp_vecs = [p.dp_vec for p in profiles if p.dp_vec is not None]
                if dp_vecs:
                    self.dp_params[pos] = _fit_group(dp_vecs)
                spec_vecs = [p.specialty_vec for p in profiles if p.specialty_vec is not None]
                if spec_vecs:
                    self.specialty_params[pos] = _fit_group(spec_vecs)
            else:
                arm_vecs = [p.arm_vec for p in profiles if p.arm_vec is not None]
                if arm_vecs:
                    self.arm_params[pos] = _fit_group(arm_vecs)
                star_vecs = [p.star_vec for p in profiles if p.star_vec is not None]
                if star_vecs:
                    self.star_params[pos] = _fit_group(star_vecs)

    def _normalize(self, vec: NDArray, params: dict[str, tuple], pos: str) -> NDArray:
        if pos not in params:
            return vec
        mean, std = params[pos]
        normed = (vec - mean) / std
        return np.nan_to_num(normed, nan=0.0)

    def normalize_range(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.range_params, pos)

    def normalize_error(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.error_params, pos)

    def normalize_dp(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.dp_params, pos)

    def normalize_specialty(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.specialty_params, pos)

    def normalize_arm(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.arm_params, pos)

    def normalize_star(self, vec: NDArray, pos: str) -> NDArray:
        return self._normalize(vec, self.star_params, pos)


# ============================================================================
# Position Partition — Vectorized Batch Scoring
# ============================================================================


class PositionPartition:
    """
    Stores all fielder profiles for one specific position with pre-built
    normalized feature matrices for vectorized batch RBF scoring.
    """

    def __init__(self, position: str) -> None:
        self.position = position
        self.is_infield = position in INFIELD_POSITIONS
        self.profiles: list[FielderProfile] = []
        self.keys: list[tuple[int, str, int]] = []  # (player_id, position, season)

        # Normalized feature matrices
        self._range_matrix: NDArray | None = None
        self._error_matrix: NDArray | None = None
        self._secondary_matrix: NDArray | None = None  # DP (IF) or Arm (OF)
        self._tertiary_matrix: NDArray | None = None  # Specialty (IF) or Stars (OF)

        self._eb_alphas: NDArray | None = None

    def build(
        self,
        profiles: list[FielderProfile],
        normalizer: FeatureNormalizer,
    ) -> None:
        self.profiles = profiles
        self.keys = [(p.player_id, p.position, p.season) for p in profiles]

        if not profiles:
            return

        pos = self.position
        range_rows, error_rows, sec_rows, tert_rows = [], [], [], []
        alphas = []

        for p in profiles:
            range_rows.append(normalizer.normalize_range(p.range_vec, pos))
            error_rows.append(normalizer.normalize_error(p.error_vec, pos))
            alphas.append(p.eb_alpha)

            if self.is_infield:
                sec_rows.append(
                    normalizer.normalize_dp(
                        p.dp_vec
                        if p.dp_vec is not None
                        else np.zeros(
                            len(IF_DP_FEATURES)
                            + (len(IF_PIVOT_FEATURES) if pos in ("2B", "SS") else 0)
                        ),
                        pos,
                    )
                )
                tert_rows.append(
                    normalizer.normalize_specialty(
                        p.specialty_vec
                        if p.specialty_vec is not None
                        else np.zeros(len(IF_SPECIALTY_FEATURES)),
                        pos,
                    )
                )
            else:
                sec_rows.append(
                    normalizer.normalize_arm(
                        p.arm_vec if p.arm_vec is not None else np.zeros(len(OF_ARM_FEATURES)),
                        pos,
                    )
                )
                tert_rows.append(
                    normalizer.normalize_star(
                        p.star_vec if p.star_vec is not None else np.zeros(len(OF_STAR_FEATURES)),
                        pos,
                    )
                )

        self._range_matrix = np.array(range_rows, dtype=np.float64)
        self._error_matrix = np.array(error_rows, dtype=np.float64)
        self._secondary_matrix = np.array(sec_rows, dtype=np.float64)
        self._tertiary_matrix = np.array(tert_rows, dtype=np.float64)
        self._eb_alphas = np.array(alphas, dtype=np.float64)

    def score_all(
        self,
        query: FielderProfile,
        normalizer: FeatureNormalizer,
        range_rbf: WeightedRBFSimilarity,
        secondary_rbf: WeightedRBFSimilarity,
        tertiary_rbf: WeightedRBFSimilarity,
        error_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        """Score the query against EVERY profile in this partition."""
        if not self.profiles:
            return []

        n = len(self.profiles)
        pos = self.position
        query_key = (query.player_id, query.position, query.season)

        # Vectorized RBF sub-scores
        range_q = normalizer.normalize_range(query.range_vec, pos)
        range_scores = range_rbf.score_batch(range_q, self._range_matrix)

        error_q = normalizer.normalize_error(query.error_vec, pos)
        error_scores = error_rbf.score_batch(error_q, self._error_matrix)

        # Secondary: DP (IF) or Arm (OF)
        if self.is_infield:
            sec_q = normalizer.normalize_dp(
                query.dp_vec
                if query.dp_vec is not None
                else np.zeros(self._secondary_matrix.shape[1]),
                pos,
            )
        else:
            sec_q = normalizer.normalize_arm(
                query.arm_vec
                if query.arm_vec is not None
                else np.zeros(self._secondary_matrix.shape[1]),
                pos,
            )
        secondary_scores = secondary_rbf.score_batch(sec_q, self._secondary_matrix)

        # Tertiary: Specialty (IF) or Stars (OF)
        if self.is_infield:
            tert_q = normalizer.normalize_specialty(
                query.specialty_vec
                if query.specialty_vec is not None
                else np.zeros(self._tertiary_matrix.shape[1]),
                pos,
            )
        else:
            tert_q = normalizer.normalize_star(
                query.star_vec
                if query.star_vec is not None
                else np.zeros(self._tertiary_matrix.shape[1]),
                pos,
            )
        tertiary_scores = tertiary_rbf.score_batch(tert_q, self._tertiary_matrix)

        # Weighted composite
        if self.is_infield:
            composite = (
                WEIGHT_IF_RANGE * range_scores
                + WEIGHT_IF_DP * secondary_scores
                + WEIGHT_IF_ERRORS * error_scores
                + WEIGHT_IF_SPECIALTY * tertiary_scores
            )
        else:
            composite = (
                WEIGHT_OF_RANGE * range_scores
                + WEIGHT_OF_ARM * secondary_scores
                + WEIGHT_OF_STARS * tertiary_scores
                + WEIGHT_OF_ERRORS * error_scores
            )

        # Confidence discount
        pair_confidence = np.minimum(query.eb_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # Build results (exclude exact self-match)
        results = []
        for i in range(n):
            if self.keys[i] == query_key:
                continue
            cand = self.profiles[i]

            if self.is_infield:
                results.append(
                    SimilarityResult(
                        player_id=cand.player_id,
                        position=cand.position,
                        season=cand.season,
                        score=float(composite[i]),
                        range_score=float(range_scores[i]),
                        secondary_score=float(secondary_scores[i]),  # DP
                        tertiary_score=float(error_scores[i]),  # Errors
                        quaternary_score=float(tertiary_scores[i]),  # Specialty
                        sample_batted_balls=cand.sample_batted_balls,
                    )
                )
            else:
                results.append(
                    SimilarityResult(
                        player_id=cand.player_id,
                        position=cand.position,
                        season=cand.season,
                        score=float(composite[i]),
                        range_score=float(range_scores[i]),
                        secondary_score=float(secondary_scores[i]),  # Arm
                        tertiary_score=float(tertiary_scores[i]),  # Stars
                        quaternary_score=float(error_scores[i]),  # Errors
                        sample_batted_balls=cand.sample_batted_balls,
                    )
                )

        return results


# ============================================================================
# Main Engine
# ============================================================================


class FielderSimilarityEngine:
    """
    The Fielder-to-Fielder Similarity Engine.

    Scores are computed EXHAUSTIVELY against all same-position fielder-season
    profiles. Cross-season comparisons are included. Position-gated: each
    position (1B, 2B, 3B, SS, LF, CF, RF) lives in its own partition and
    is never compared cross-position.

    Usage:
        engine = FielderSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])

        # All similar SS
        results = engine.query(player_id=660271, position="SS", season=2025)

        # Top-10 CF
        results = engine.query(player_id=621043, position="CF", season=2025, n=10)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, str, int], FielderProfile] = {}

        # Per-position positional-average vectors for shrinkage
        # Keys: (position, season) → NDArray
        self._pos_avg: dict[str, dict[str, dict[int, NDArray]]] = {
            "range": {},
            "error": {},
            "dp": {},
            "specialty": {},
            "arm": {},
            "star": {},
        }

        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()

        # Per-position partitions
        self._partitions: dict[str, PositionPartition] = {
            pos: PositionPartition(pos) for pos in ALL_POSITIONS
        }

        # Build RBF scorers — separate for IF and OF sub-score types
        self._if_range_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_IF_RANGE,
            reliability_weights=np.array([w for _, w in IF_RANGE_FEATURES]),
        )
        self._if_dp_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_IF_DP,
            reliability_weights=np.array(
                [w for _, w in IF_DP_FEATURES] + [w for _, w in IF_PIVOT_FEATURES]
            ),
        )
        self._if_dp_rbf_corner = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_IF_DP,
            reliability_weights=np.array([w for _, w in IF_DP_FEATURES]),
        )
        self._if_error_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_IF_ERRORS,
            reliability_weights=np.array([w for _, w in IF_ERROR_FEATURES]),
        )
        self._if_specialty_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_IF_SPECIALTY,
            reliability_weights=np.array([w for _, w in IF_SPECIALTY_FEATURES]),
        )

        self._of_range_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_OF_RANGE,
            reliability_weights=np.array([w for _, w in OF_RANGE_FEATURES]),
        )
        self._of_arm_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_OF_ARM,
            reliability_weights=np.array([w for _, w in OF_ARM_FEATURES]),
        )
        self._of_star_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_OF_STARS,
            reliability_weights=np.array([w for _, w in OF_STAR_FEATURES]),
        )
        self._of_error_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_OF_ERRORS,
            reliability_weights=np.array([w for _, w in OF_ERROR_FEATURES]),
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load all fielder profiles, apply shrinkage, build matrices."""
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)

        try:
            self._load_positional_averages(conn, seasons)
            self._load_profiles(conn, seasons)
        finally:
            conn.close()

        self._apply_shrinkage()

        # Group profiles by position for normalization and partition building
        profiles_by_pos: dict[str, list[FielderProfile]] = {pos: [] for pos in ALL_POSITIONS}
        for _key, p in self._profiles.items():
            profiles_by_pos[p.position].append(p)

        self._normalizer.fit(profiles_by_pos)

        for pos, partition in self._partitions.items():
            profs = profiles_by_pos.get(pos, [])
            self._get_rbfs_for_position(pos)
            partition.build(profs, self._normalizer)

        elapsed = time.time() - t0
        log.info(
            "FielderSimilarityEngine built: %d profiles across %d positions in %.2fs.",
            len(self._profiles),
            sum(1 for pos, parts in self._partitions.items() if parts.profiles),
            elapsed,
        )
        for pos in sorted(ALL_POSITIONS):
            n = len(self._partitions[pos].profiles)
            if n > 0:
                log.info("  %s: %d profiles", pos, n)

    # ------------------------------------------------------------------
    # Data Loading
    # ------------------------------------------------------------------

    def _load_positional_averages(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        """
        Load per-position per-season average feature vectors from
        derived.league_averages. Used as shrinkage targets.
        """
        try:
            season_filter = ""
            if seasons:
                sl = ", ".join(str(s) for s in seasons)
                season_filter = f"AND season IN ({sl})"

            rows = conn.execute(f"""
                SELECT season, entity_type, profile_json
                FROM derived.league_averages
                WHERE entity_type LIKE 'fielder_%'
                  {season_filter}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — no shrinkage fallbacks.")
            return

        for season, entity_type, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw
            # entity_type format: "fielder_SS", "fielder_CF", etc.
            pos = entity_type.replace("fielder_", "")
            if pos not in ALL_POSITIONS:
                continue

            if pos not in self._pos_avg["range"]:
                for group in self._pos_avg:
                    self._pos_avg[group][pos] = {}

            if pos in INFIELD_POSITIONS:
                self._pos_avg["range"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in IF_RANGE_FEATURES], dtype=np.float64
                )
                dp_feats = IF_DP_FEATURES + (IF_PIVOT_FEATURES if pos in ("2B", "SS") else [])
                self._pos_avg["dp"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in dp_feats], dtype=np.float64
                )
                self._pos_avg["error"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in IF_ERROR_FEATURES], dtype=np.float64
                )
                self._pos_avg["specialty"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in IF_SPECIALTY_FEATURES], dtype=np.float64
                )
            else:
                self._pos_avg["range"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in OF_RANGE_FEATURES], dtype=np.float64
                )
                self._pos_avg["arm"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in OF_ARM_FEATURES], dtype=np.float64
                )
                self._pos_avg["error"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in OF_ERROR_FEATURES], dtype=np.float64
                )
                self._pos_avg["star"][pos][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in OF_STAR_FEATURES], dtype=np.float64
                )

        log.info("Loaded fielder positional averages for %d season-position combos.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        season_filter = ""
        if seasons:
            sl = ", ".join(str(s) for s in seasons)
            season_filter = f"AND fsm.season IN ({sl})"

        rows = conn.execute(f"""
            SELECT
                fsm.player_id, fsm.position, fsm.season,
                fsm.innings_played, fsm.sample_batted_balls,
                -- Range / OAA (5 features — shared IF/OF)
                fsm.oaa_glove_side, fsm.oaa_arm_side,
                fsm.oaa_charging, fsm.oaa_deep,
                fsm.catch_pct_added,
                -- Errors (2 features — shared IF/OF)
                fsm.fielding_error_rate, fsm.throwing_error_rate,
                -- DP (IF only)
                fsm.dp_above_expected, fsm.dp_attempt_rate, fsm.dp_success_rate,
                fsm.dp_pivot_above_expected,
                -- Specialty (IF only)
                fsm.bunt_fielding_rate, fsm.scoop_success_rate,
                -- Arm (OF only)
                fsm.arm_hold_rate, fsm.arm_thrown_out_rate,
                fsm.arm_advancement_prevention, fsm.of_arm_runs,
                -- Star plays (OF only — compute rates from counts)
                fsm.five_star_opps, fsm.five_star_catches,
                fsm.four_star_opps, fsm.four_star_catches,
                fsm.routine_opps, fsm.routine_catches,
                -- Meta
                fsm.below_minimum_sample
            FROM derived.fielder_season_metrics fsm
            WHERE NOT fsm.below_minimum_sample
              AND fsm.position IN ('1B','2B','3B','SS','LF','CF','RF')
              {season_filter}
        """).fetchall()

        log.info("Loading %d fielder profiles from DuckDB …", len(rows))

        for row in rows:
            (
                player_id,
                position,
                season,
                innings,
                sample_bb,
                # Range (5)
                oaa_gs,
                oaa_as,
                oaa_ch,
                oaa_deep,
                cpct_add,
                # Errors (2)
                f_err_rate,
                t_err_rate,
                # DP (4)
                dp_above,
                dp_att_rate,
                dp_suc_rate,
                dp_pivot_above,
                # Specialty (2)
                bunt_rate,
                scoop_rate,
                # Arm (4)
                arm_hold,
                arm_throw_out,
                arm_adv_prev,
                of_arm_runs,
                # Star play counts (6)
                five_opps,
                five_catches,
                four_opps,
                four_catches,
                routine_opps,
                routine_catches,
                # Meta
                below_min,
            ) = row

            def _v(vals):
                return np.array([v if v is not None else np.nan for v in vals], dtype=np.float64)

            # Compute star play rates from counts
            five_star_rate = (five_catches / five_opps) if five_opps and five_opps > 0 else np.nan
            four_star_rate = (four_catches / four_opps) if four_opps and four_opps > 0 else np.nan
            routine_rate = (
                (routine_catches / routine_opps) if routine_opps and routine_opps > 0 else np.nan
            )

            if position not in ALL_POSITIONS:
                continue

            range_vec = _v([oaa_gs, oaa_as, oaa_ch, oaa_deep, cpct_add])
            error_vec = _v([f_err_rate, t_err_rate])

            dp_vec = None
            specialty_vec = None
            arm_vec = None
            star_vec = None

            if position in INFIELD_POSITIONS:
                dp_feats = [dp_above, dp_att_rate, dp_suc_rate]
                if position in ("2B", "SS"):
                    dp_feats.append(dp_pivot_above)
                dp_vec = _v(dp_feats)
                specialty_vec = _v([bunt_rate, scoop_rate])
            else:
                arm_vec = _v([arm_hold, arm_throw_out, arm_adv_prev, of_arm_runs])
                star_vec = _v([five_star_rate, four_star_rate, routine_rate])

            self._profiles[(player_id, position, season)] = FielderProfile(
                player_id=player_id,
                position=position,
                season=season,
                innings_played=innings or 0.0,
                sample_batted_balls=sample_bb or 0,
                range_vec=range_vec,
                error_vec=error_vec,
                dp_vec=dp_vec,
                specialty_vec=specialty_vec,
                arm_vec=arm_vec,
                star_vec=star_vec,
                eb_alpha=self._shrinkage.alpha(sample_bb or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        """Apply EB shrinkage to all feature vectors using positional averages."""
        for _key, p in self._profiles.items():
            pos = p.position
            s = p.season
            n = p.sample_batted_balls

            # Range
            avg = self._pos_avg["range"].get(pos, {}).get(s)
            if avg is not None:
                p.range_vec = self._shrinkage.shrink(p.range_vec, avg, n)

            # Errors
            avg = self._pos_avg["error"].get(pos, {}).get(s)
            if avg is not None:
                p.error_vec = self._shrinkage.shrink(p.error_vec, avg, n)

            if pos in INFIELD_POSITIONS:
                avg = self._pos_avg["dp"].get(pos, {}).get(s)
                if avg is not None and p.dp_vec is not None:
                    p.dp_vec = self._shrinkage.shrink(p.dp_vec, avg, n)

                avg = self._pos_avg["specialty"].get(pos, {}).get(s)
                if avg is not None and p.specialty_vec is not None:
                    p.specialty_vec = self._shrinkage.shrink(p.specialty_vec, avg, n)
            else:
                avg = self._pos_avg["arm"].get(pos, {}).get(s)
                if avg is not None and p.arm_vec is not None:
                    p.arm_vec = self._shrinkage.shrink(p.arm_vec, avg, n)

                avg = self._pos_avg["star"].get(pos, {}).get(s)
                if avg is not None and p.star_vec is not None:
                    p.star_vec = self._shrinkage.shrink(p.star_vec, avg, n)

    # ------------------------------------------------------------------
    # RBF Selector
    # ------------------------------------------------------------------

    def _get_rbfs_for_position(
        self,
        position: str,
    ) -> tuple[
        WeightedRBFSimilarity, WeightedRBFSimilarity, WeightedRBFSimilarity, WeightedRBFSimilarity
    ]:
        """Return (range_rbf, secondary_rbf, tertiary_rbf, error_rbf) for a position."""
        if position in INFIELD_POSITIONS:
            dp_rbf = self._if_dp_rbf if position in ("2B", "SS") else self._if_dp_rbf_corner
            return (self._if_range_rbf, dp_rbf, self._if_specialty_rbf, self._if_error_rbf)
        else:
            return (self._of_range_rbf, self._of_arm_rbf, self._of_star_rbf, self._of_error_rbf)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        player_id: int,
        position: str,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query fielder against ALL same-position fielder-season
        profiles in the engine.

        Parameters
        ----------
        player_id : int
            MLB player ID.
        position : str
            Defensive position (1B, 2B, 3B, SS, LF, CF, RF).
        season : int
            Season of the query profile.
        n : int or None
            If provided, return only the top-N results. None = return all.

        Returns
        -------
        Exhaustive list of SimilarityResult sorted by score descending.
        The query fielder's exact (player_id, position, season) is excluded,
        but other seasons of the same player ARE included.
        """
        if position not in ALL_POSITIONS:
            log.warning("Invalid position '%s'. Must be one of %s.", position, ALL_POSITIONS)
            return []

        query_profile = self._profiles.get((player_id, position, season))
        if query_profile is None:
            log.warning(
                "Fielder %d at %s season %d not found. Ensure build() was called.",
                player_id,
                position,
                season,
            )
            return []

        partition = self._partitions[position]
        range_rbf, secondary_rbf, tertiary_rbf, error_rbf = self._get_rbfs_for_position(position)

        results = partition.score_all(
            query=query_profile,
            normalizer=self._normalizer,
            range_rbf=range_rbf,
            secondary_rbf=secondary_rbf,
            tertiary_rbf=tertiary_rbf,
            error_rbf=error_rbf,
        )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        fielder_a: tuple[int, str, int],
        fielder_b: tuple[int, str, int],
    ) -> SimilarityResult | None:
        """
        Compute similarity between two specific fielder-position-seasons.

        Parameters
        ----------
        fielder_a : (player_id, position, season)
        fielder_b : (player_id, position, season)

        Returns
        -------
        SimilarityResult or None if either fielder is not in the engine
        or if they are at different positions.
        """
        pa = self._profiles.get(fielder_a)
        pb = self._profiles.get(fielder_b)
        if pa is None or pb is None:
            return None
        if pa.position != pb.position:
            log.warning(
                "Cannot compare cross-position: %s vs %s.",
                pa.position,
                pb.position,
            )
            return None

        scores = self._score_pair(pa, pb)
        return SimilarityResult(
            player_id=pb.player_id,
            position=pb.position,
            season=pb.season,
            score=scores[0],
            range_score=scores[1],
            secondary_score=scores[2],
            tertiary_score=scores[3],
            quaternary_score=scores[4],
            sample_batted_balls=pb.sample_batted_balls,
        )

    def _score_pair(
        self,
        query: FielderProfile,
        candidate: FielderProfile,
    ) -> tuple[float, float, float, float, float]:
        """Compute sub-scores and composite between two profiles."""
        pos = query.position
        range_rbf, secondary_rbf, tertiary_rbf, error_rbf = self._get_rbfs_for_position(pos)

        # Range
        range_q = self._normalizer.normalize_range(query.range_vec, pos)
        range_c = self._normalizer.normalize_range(candidate.range_vec, pos)
        range_s = range_rbf.score(range_q, range_c)

        # Error
        error_q = self._normalizer.normalize_error(query.error_vec, pos)
        error_c = self._normalizer.normalize_error(candidate.error_vec, pos)
        error_s = error_rbf.score(error_q, error_c)

        if pos in INFIELD_POSITIONS:
            # DP
            dp_q_vec = (
                query.dp_vec
                if query.dp_vec is not None
                else np.zeros(
                    self._partitions[pos]._secondary_matrix.shape[1]
                    if self._partitions[pos]._secondary_matrix is not None
                    else len(IF_DP_FEATURES)
                )
            )
            dp_c_vec = candidate.dp_vec if candidate.dp_vec is not None else np.zeros_like(dp_q_vec)
            dp_q = self._normalizer.normalize_dp(dp_q_vec, pos)
            dp_c = self._normalizer.normalize_dp(dp_c_vec, pos)
            secondary_s = secondary_rbf.score(dp_q, dp_c)

            # Specialty
            spec_q_vec = (
                query.specialty_vec
                if query.specialty_vec is not None
                else np.zeros(len(IF_SPECIALTY_FEATURES))
            )
            spec_c_vec = (
                candidate.specialty_vec
                if candidate.specialty_vec is not None
                else np.zeros(len(IF_SPECIALTY_FEATURES))
            )
            spec_q = self._normalizer.normalize_specialty(spec_q_vec, pos)
            spec_c = self._normalizer.normalize_specialty(spec_c_vec, pos)
            tertiary_s = tertiary_rbf.score(spec_q, spec_c)

            composite = (
                WEIGHT_IF_RANGE * range_s
                + WEIGHT_IF_DP * secondary_s
                + WEIGHT_IF_ERRORS * error_s
                + WEIGHT_IF_SPECIALTY * tertiary_s
            )
        else:
            # Arm
            arm_q_vec = (
                query.arm_vec if query.arm_vec is not None else np.zeros(len(OF_ARM_FEATURES))
            )
            arm_c_vec = (
                candidate.arm_vec
                if candidate.arm_vec is not None
                else np.zeros(len(OF_ARM_FEATURES))
            )
            arm_q = self._normalizer.normalize_arm(arm_q_vec, pos)
            arm_c = self._normalizer.normalize_arm(arm_c_vec, pos)
            secondary_s = secondary_rbf.score(arm_q, arm_c)

            # Stars
            star_q_vec = (
                query.star_vec if query.star_vec is not None else np.zeros(len(OF_STAR_FEATURES))
            )
            star_c_vec = (
                candidate.star_vec
                if candidate.star_vec is not None
                else np.zeros(len(OF_STAR_FEATURES))
            )
            star_q = self._normalizer.normalize_star(star_q_vec, pos)
            star_c = self._normalizer.normalize_star(star_c_vec, pos)
            tertiary_s = tertiary_rbf.score(star_q, star_c)

            composite = (
                WEIGHT_OF_RANGE * range_s
                + WEIGHT_OF_ARM * secondary_s
                + WEIGHT_OF_STARS * tertiary_s
                + WEIGHT_OF_ERRORS * error_s
            )

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(range_s),
            float(secondary_s),
            float(tertiary_s),
            float(error_s),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, player_id: int, position: str, season: int) -> FielderProfile | None:
        return self._profiles.get((player_id, position, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, str, int]]:
        """List all (player_id, position, season) triples in the engine."""
        return list(self._profiles.keys())

    def profile_ids_for_position(self, position: str) -> list[tuple[int, str, int]]:
        """List all profile keys for a specific position."""
        return [k for k in self._profiles if k[1] == position]


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: FielderSimilarityEngine,
    fielder_ids: list[tuple[int, str, int]],
) -> NDArray[np.float64]:
    """
    Build a symmetric N×N similarity matrix for the given fielder/position/season
    triples. All entries must be at the same position.
    """
    n = len(fielder_ids)
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(fielder_ids[i], fielder_ids[j])
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score
    return matrix


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = FielderSimilarityEngine(duckdb_path="../../db/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    results = engine.query(592743, "SS", 2018, 25)
    report = run_fielder_diagnostics(engine, n_query_samples=50)
    print(report)
