"""Batter-to-batter similarity: one score in [0, 1] between two batter-seasons.

The score mixes five sub-scores. Each sub-score is a weighted Gaussian kernel
(an RBF: score = exp(-distance² / 2σ²)) over z-scored features. A z-score is a
value's distance from the population mean, in standard deviations.

  1. Plate discipline (0.32): first-pitch take rate, chase rate (O-swing),
     zone swing rate (Z-swing), contact rate, whiff rate, strikeout rate and
     walk rate.
  2. Batted ball (0.28): ground-ball and fly-ball rates, pull and
     opposite-field rates, average exit velocity and launch angle, hard-hit
     rate and barrel rate.
  3. Platoon (0.12): discipline rates plus the ground-ball and barrel rates,
     split by pitcher hand and compared side for side.
  4. Power (0.08): home-run rate, xBA, xSLG and maximum exit velocity.
  5. Physical swing and stance (0.20; the batter's physical swing features,
     SIM-529): how the batter swings and stands, not what his swings
     produced. Statcast measures them and Baseball Savant publishes them from
     2023 on. Six of the ten repeat from season to season better than any
     other feature here. Each is held overall and per pitcher hand, so a
     switch hitter's two sides stay apart.

The composite is the weighted sum of the five, times a handedness penalty
(the bats penalty), times the pair confidence (a discount for a thin sample on
either side), clipped to [0, 1].

Where the score goes
--------------------
The nightly actor-matrix build (``--what actors_sim``) scores every
batter-season against every other with ``query`` and no pitcher hand. The
pitch draw, the pitch-result draw and the fielding draw use that matrix as a
draw weight. The build applies no calibration report, so the module defaults
below are the values the draws see. The app applies the calibration report to
its own copy at boot, and the Similarity Explorer reads that copy.

Design
------
- Process comes before outcome. The discipline and batted-ball groups (what
  the batter does) outweigh the power group (what happened), because outcome
  rates such as BABIP carry more luck.
- Batters share one partition. A switch hitter must compare against everyone,
  and a left-handed and a right-handed batter can share an approach. A mild
  handedness penalty keeps the two sides apart instead.
- The search is exhaustive. ``query`` scores every batter-season, the
  batter's other seasons included.
- Empirical-Bayes (EB) shrinkage pulls each rate toward the league mean. A
  small sample moves further.

Usage
-----
  engine = BatterSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2023, 2024, 2025, 2026])
  results = engine.query(batter_id=665742, season=2025)  # every batter-season
  results = engine.query(batter_id=665742, season=2025, n=20)  # the top 20
  results = engine.query(batter_id=665742, season=2025, vs_hand="R")  # against a RHP
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import duckdb
import numpy as np
from numpy.typing import NDArray

from db.introspect import duck_columns
from similarity.engines.rbf_kernel import WeightedRBFSimilarity
from similarity.similarity_diagnostics import run_batter_diagnostics

if TYPE_CHECKING:
    from similarity.calibration.report import CalibrationReport

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
# Config — feature definitions and weights
# ============================================================================

# Each group lists (feature, reliability weight). A reliability weight is the
# feature's share of its group's kernel: a feature that repeats better from
# season to season carries more true talent and gets more pull. The values here
# are the module defaults. `make calibrate` fits each one as the feature's
# season-to-season correlation, and the app applies that fit at boot. The
# "stabilizes" notes give the published sample at which each rate becomes
# reliable (Carleton; FanGraphs).

# --- Plate discipline (stabilizes in 60-200 PA) ---
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

# --- Batted ball (stabilizes in 50-150 balls in play) ---
# The pull and opposite-field rates read relative to the batter's own side.
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

# --- Power and results (noisier; 300+ PA) ---
# The profile computor writes no xba or xslg, so both read 0.0 for every batter
# (see the optional columns in the profile load).
POWER_FEATURES = [
    ("hr_rate", 0.470),  # stabilizes ~300 PA
    ("xba", 0.596),  # expected stats — moderate noise
    ("xslg", 0.554),  # expected stats — moderate noise
    ("max_exit_velo", 0.690),  # very stable — physical attribute
]

# --- Platoon splits (compared side for side: vs L with vs L, vs R with vs R) ---
# Mostly approach and discipline by pitcher hand. The ground-ball and barrel
# rates add a little contact shape; the batted-ball group holds the rest.
PLATOON_FEATURES = [
    ("o_swing_rate", 0.649),
    ("z_swing_rate", 0.482),
    ("whiff_rate", 0.734),
    ("walk_rate", 0.503),
    ("k_rate", 0.656),
    ("gb_rate", 0.496),
    ("barrel_rate", 0.239),
]

# --- Physical swing and stance (the batter's physical swing features, SIM-529) ---
# How the batter swings and stands, not what his swings produced. Every other
# group in this engine is a rate of outcomes; this one is a measurement.
#
# Each reliability weight IS the feature's measured year-to-year correlation,
# averaged over 2023->2024 and 2024->2025 (owner ruling 2026-09-10: weight these
# in proportion to reliability). The most stable feature in the other groups is
# whiff rate at a measured 0.825, and six of the ten below beat it. Evidence:
# docs/audit/2026-09-10-savant-leaderboard-data-audit.md
#
# Each feature is stored three times: overall, against left-handed pitching and
# against right-handed pitching. A switch hitter swings and stands differently
# from each side, and one row per batter would lose that.
PHYSICAL_FEATURES = [
    # feature_name,        reliability weight = measured year-to-year r
    ("swing_tilt", 0.896),  # steepness of the swing plane
    ("bat_speed", 0.897),
    ("stance_depth", 0.879),  # how deep in the box he stands
    ("swing_length", 0.885),
    ("stance_off_plate", 0.857),  # how far off the plate he stands
    ("stance_foot_sep", 0.843),
    ("stance_angle", 0.809),  # open or closed
    ("contact_depth", 0.802),  # how far in front of the plate he meets the ball
    ("attack_angle", 0.737),  # up or down at contact
    ("attack_direction", 0.643),  # pull-side or oppo at contact
]

# --- Sub-score weights (sum to 1.0) ---
# The physical group takes 0.20. The other four keep their relative sizes:
# 0.32 : 0.28 : 0.12 : 0.08 is the four-group split 0.40 : 0.35 : 0.15 : 0.10
# scaled by 0.8.
#
# Why 0.20 and not more. Averaged over its features, the physical group repeats
# best of the five (r 0.824, against 0.779 for discipline, the next best). A
# split in proportion to reliability gives it about 0.28. The group is the least
# tested in this simulator, so it holds a fifth of the score instead. This is
# the one number to move if the certifying lane shows the physical group
# pulling too hard or too little.
WEIGHT_DISCIPLINE = 0.32
WEIGHT_BATTED_BALL = 0.28
WEIGHT_PLATOON = 0.12
WEIGHT_POWER = 0.08
WEIGHT_PHYSICAL = 0.20
_TOTAL = WEIGHT_DISCIPLINE + WEIGHT_BATTED_BALL + WEIGHT_PLATOON + WEIGHT_POWER + WEIGHT_PHYSICAL
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The platoon-context weights. A query that names a pitcher hand, for a batter
# with a real sample against it, cares more about the matchup than about the
# overall profile, so the platoon group takes the most weight. The physical
# group keeps its 0.20; the other four are the four-group platoon split
# (0.30 : 0.25 : 0.35 : 0.10) scaled by 0.8.
WEIGHT_DISCIPLINE_PLATOON = 0.24
WEIGHT_BATTED_BALL_PLATOON = 0.20
WEIGHT_PLATOON_PLATOON = 0.28  # platoon becomes the dominant signal
WEIGHT_POWER_PLATOON = 0.08
WEIGHT_PHYSICAL_PLATOON = 0.20
_TOTAL_PLATOON = (
    WEIGHT_DISCIPLINE_PLATOON
    + WEIGHT_BATTED_BALL_PLATOON
    + WEIGHT_PLATOON_PLATOON
    + WEIGHT_POWER_PLATOON
    + WEIGHT_PHYSICAL_PLATOON
)
assert abs(_TOTAL_PLATOON - 1.0) < 1e-9, "Platoon-context weights must sum to 1.0"

# The kernel bandwidths (σ; gamma = 1 / (2σ²)). `make calibrate` fits each one
# to a median similarity of 0.50.
RBF_SIGMA_DISCIPLINE = 1.0389
RBF_SIGMA_BATTED_BALL = 1.0828
RBF_SIGMA_PLATOON = 1.0866
RBF_SIGMA_POWER = 0.9591
#: A starting value in line with the other four.
RBF_SIGMA_PHYSICAL = 1.0000

#: The four-group split, (discipline, batted ball, platoon, power), used when no
#: batter in the population has physical data.
#:
#: Why it exists. With no physical data every physical sub-score is a perfect
#: 1.0, and a 0.20 weight would turn every composite into 0.8 × (the four-group
#: score) + 0.20. That compresses the spread. The score is a draw weight, so a
#: compressed spread makes every batter look more alike and weakens the batter
#: factor in the draws. With these weights the physical group gets 0.0, and the
#: composite is the four-group score.
_LEGACY_WEIGHTS = (0.40, 0.35, 0.15, 0.10)
_LEGACY_WEIGHTS_PLATOON = (0.30, 0.25, 0.35, 0.10)


def sub_score_weights(
    *, platoon_context: bool, physical_available: bool
) -> tuple[float, float, float, float, float]:
    """(discipline, batted ball, platoon, power, physical), summing to 1.0.

    ``physical_available`` is False when no batter in the built population has
    physical data: a season set before 2023 (Savant publishes no swing tracking
    earlier), or a database without the physical columns (migration 0025). The
    physical weight is then 0.0, and the other four take the four-group split.
    """
    if not physical_available:
        legacy = _LEGACY_WEIGHTS_PLATOON if platoon_context else _LEGACY_WEIGHTS
        return (*legacy, 0.0)
    if platoon_context:
        return (
            WEIGHT_DISCIPLINE_PLATOON,
            WEIGHT_BATTED_BALL_PLATOON,
            WEIGHT_PLATOON_PLATOON,
            WEIGHT_POWER_PLATOON,
            WEIGHT_PHYSICAL_PLATOON,
        )
    return (
        WEIGHT_DISCIPLINE,
        WEIGHT_BATTED_BALL,
        WEIGHT_PLATOON,
        WEIGHT_POWER,
        WEIGHT_PHYSICAL,
    )


# The bats penalty multiplies the composite when the two batters bat from
# different sides. The pull and opposite-field rates read relative to the
# batter's own side, so a lefty and a righty with the same rates hit to opposite
# fields. A switch hitter shares a side with everyone, so his penalty is milder.
BATS_PENALTY_OPPOSITE = 0.92  # L↔R
BATS_PENALTY_SWITCH = 0.97  # L↔S or R↔S
BATS_PENALTY_SAME = 1.00  # L↔L, R↔R, S↔S

# The EB prior strength, in plate appearances. A batter with EB_N_PRIOR PA keeps
# half of his own rate (α = 0.5). At the 100-PA minimum α is already 0.95, so
# the shrinkage is light.
EB_N_PRIOR = 5

# The minimum sample, in plate appearances. The profile computor flags a
# batter-season under it (below_minimum_sample), and the profile load skips it.
MIN_BATTER_PA = 100

#: How many columns the profile query returns BEFORE the physical block.
#: Computed, not typed, so a feature added to any group above moves the boundary
#: instead of shifting the physical tail by one column.
#: 5 identity columns + the four feature groups + a sample count per platoon
#: side + below_minimum_sample.
_N_BASE_COLUMNS = (
    5
    + len(DISCIPLINE_FEATURES)
    + len(BATTED_BALL_FEATURES)
    + len(POWER_FEATURES)
    + 2 * (len(PLATOON_FEATURES) + 1)
    + 1
)


# ============================================================================
# Data Structures
# ============================================================================


def _no_physical() -> NDArray[np.float64]:
    """A physical vector meaning "Savant has no row for this batter-season".

    Typed as the vector rather than as ``Field`` because that is what every
    reader of the attribute gets; ``dataclasses`` swaps the descriptor out at
    class-construction time.
    """
    return cast(
        "NDArray[np.float64]",
        field(default_factory=lambda: np.full(len(PHYSICAL_FEATURES), np.nan)),
    )


@dataclass(slots=True)
class BatterProfile:
    """One batter-season's feature vectors, sample sizes and EB weight."""

    batter_id: int
    season: int
    bats: str  # "L", "R", or "S"
    sample_pa: int
    sample_pitches: int

    # The rate vectors. ``build`` shrinks them toward the league mean in place.
    # The normalizer z-scores them when the partition is built and when a query
    # arrives.
    discipline_vec: NDArray[np.float64]  # shape (len(DISCIPLINE_FEATURES),)
    batted_ball_vec: NDArray[np.float64]  # shape (len(BATTED_BALL_FEATURES),)
    power_vec: NDArray[np.float64]  # shape (len(POWER_FEATURES),)

    # Platoon split vectors
    platoon_vs_l_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)
    platoon_vs_r_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)

    # Physical swing and stance, overall and per pitcher hand, so a switch
    # hitter's two sides stay apart. A query with no pitcher hand reads the
    # overall vector; otherwise it reads the matching side. These vectors are
    # not shrunk.
    #
    # The default is all-NaN, not all-zero, and the choice matters: zeros would
    # read as the slowest swing and the flattest stance in the league. Once the
    # normalizer is fitted, a NaN z-scores to 0, the population mean. With no
    # physical data in the population, the kernel reads a NaN difference as zero
    # distance. Either way, two batters with no Savant row are a perfect physical
    # match.
    physical_vec: NDArray[np.float64] = _no_physical()  # (len(PHYSICAL_FEATURES),)
    physical_vs_l_vec: NDArray[np.float64] = _no_physical()
    physical_vs_r_vec: NDArray[np.float64] = _no_physical()

    sample_pa_vs_l: int = 0
    sample_pa_vs_r: int = 0

    # Empirical Bayes: the weight the batter's own rates keep, n / (n + prior).
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One candidate in a query's output: the composite and its five sub-scores.

    The sub-scores are the raw kernels. Only ``score`` carries the bats penalty
    and the pair confidence.
    """

    batter_id: int
    season: int
    bats: str
    score: float  # composite [0, 1], 1 = identical
    discipline_score: float
    batted_ball_score: float
    platoon_score: float
    power_score: float
    physical_score: float
    sample_pa: int


# ============================================================================
# Empirical Bayes Shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """Empirical-Bayes shrinkage: pull a rate vector toward the league mean.

    The batter's own values keep the weight α = n / (n + n_prior); the league
    mean takes the rest. A NaN value takes the league mean.
    """

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

    Each group has its own mean and standard deviation per feature. A NaN
    z-scores to 0, the population mean. A feature with no spread keeps a
    standard deviation of 1. Before ``fit`` runs, a vector passes through
    unchanged.
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
    # One set of parameters for all three physical vectors. The overall vector
    # and the two sides measure the SAME quantities in the same units, so
    # separate parameters would make a switch hitter's left-side stance
    # incomparable to a left-handed batter's stance.
    physical_mean: NDArray | None = None
    physical_std: NDArray | None = None

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
        # The platoon groups fit only over the batters with 30 or more PA on
        # that side.
        vs_l_vecs = [p.platoon_vs_l_vec for p in profiles if p.sample_pa_vs_l >= 30]
        vs_r_vecs = [p.platoon_vs_r_vec for p in profiles if p.sample_pa_vs_r >= 30]
        if vs_l_vecs:
            self.platoon_l_mean, self.platoon_l_std = _fit_group(vs_l_vecs)
        if vs_r_vecs:
            self.platoon_r_mean, self.platoon_r_std = _fit_group(vs_r_vecs)
        # The physical group fits on the overall vectors and serves both sides.
        # It fits only when some batter has physical data: an all-NaN population
        # gives a NaN mean and a numpy warning. Left unset, the parameters pass
        # every vector through unchanged, and the kernel reads the NaNs as zero
        # distance.
        phys_vecs = [p.physical_vec for p in profiles]
        if phys_vecs and np.isfinite(np.asarray(phys_vecs, dtype=np.float64)).any():
            self.physical_mean, self.physical_std = _fit_group(phys_vecs)

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

    def normalize_physical(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.physical_mean, self.physical_std)

    def normalize_platoon_l(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.platoon_l_mean, self.platoon_l_std)

    def normalize_platoon_r(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.platoon_r_mean, self.platoon_r_std)


# ============================================================================
# Bats-Mismatch Penalty
# ============================================================================


def bats_penalty(bats_a: str, bats_b: str) -> float:
    """The bats penalty between two batters' sides (see BATS_PENALTY_OPPOSITE).

    The same side scores 1.0. A switch hitter against anyone scores the milder
    switch penalty. A left-handed against a right-handed batter scores the
    full penalty, though their discipline and power can still match closely.
    """
    if bats_a == bats_b:
        return BATS_PENALTY_SAME
    if "S" in (bats_a, bats_b):
        return BATS_PENALTY_SWITCH
    return BATS_PENALTY_OPPOSITE


def bats_penalty_vector(query_bats: str, all_bats: list[str]) -> NDArray[np.float64]:
    """The bats penalty of the query against each candidate."""
    penalties = np.ones(len(all_bats), dtype=np.float64)
    for i, b in enumerate(all_bats):
        penalties[i] = bats_penalty(query_bats, b)
    return penalties


# ============================================================================
# Scoring Partition — Vectorized Batch Scoring
# ============================================================================


class BatterPartition:
    """Every batter profile, with its normalized feature matrices, for
    vectorized scoring.

    Pitchers split by hand; batters share this one partition. The bats
    penalty keeps the sides apart.
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
        # One physical matrix per context (overall, vs L, vs R), all three
        # normalized with the same parameters so they stay comparable.
        self._physical_matrix: NDArray | None = None
        self._physical_l_matrix: NDArray | None = None
        self._physical_r_matrix: NDArray | None = None

        self._eb_alphas: NDArray | None = None
        self._platoon_l_valid: NDArray | None = None  # bool mask: 30+ PA vs L
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
        phys_rows, phys_l_rows, phys_r_rows = [], [], []
        alphas = []
        pl_valid, pr_valid = [], []

        for p in profiles:
            disc_rows.append(normalizer.normalize_discipline(p.discipline_vec))
            bb_rows.append(normalizer.normalize_batted_ball(p.batted_ball_vec))
            power_rows.append(normalizer.normalize_power(p.power_vec))
            pl_rows.append(normalizer.normalize_platoon_l(p.platoon_vs_l_vec))
            pr_rows.append(normalizer.normalize_platoon_r(p.platoon_vs_r_vec))
            phys_rows.append(normalizer.normalize_physical(p.physical_vec))
            phys_l_rows.append(normalizer.normalize_physical(p.physical_vs_l_vec))
            phys_r_rows.append(normalizer.normalize_physical(p.physical_vs_r_vec))
            alphas.append(p.eb_alpha)
            pl_valid.append(p.sample_pa_vs_l >= 30)
            pr_valid.append(p.sample_pa_vs_r >= 30)

        self._disc_matrix = np.array(disc_rows, dtype=np.float64)
        self._bb_matrix = np.array(bb_rows, dtype=np.float64)
        self._power_matrix = np.array(power_rows, dtype=np.float64)
        self._platoon_l_matrix = np.array(pl_rows, dtype=np.float64)
        self._platoon_r_matrix = np.array(pr_rows, dtype=np.float64)
        self._physical_matrix = np.array(phys_rows, dtype=np.float64)
        self._physical_l_matrix = np.array(phys_l_rows, dtype=np.float64)
        self._physical_r_matrix = np.array(phys_r_rows, dtype=np.float64)
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
        physical_rbf: WeightedRBFSimilarity,
        vs_hand: str | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query against EVERY profile in this partition.

        The composite is the weighted sum of the five sub-scores, times the
        bats penalty, times the pair confidence sqrt(min(α_query, α_candidate)),
        clipped to [0, 1]. The query's own batter-season is left out; its
        other seasons stay in.

        Parameters
        ----------
        vs_hand : "L", "R", or None
            The pitcher hand. When given, the platoon sub-score compares that
            split alone, and the physical sub-score compares the side the
            batter uses against that hand. If the query batter also has 30 or
            more PA against that hand, the platoon-context weights apply. The
            nightly actor matrix passes None; the Similarity Explorer passes a
            hand.
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

        # --- Physical swing and stance, per platoon context ---
        # With a pitcher hand in play, compare the side the batter uses against
        # it. Without one, compare the overall swing.
        phys_query_vec, phys_matrix = {
            "L": (query.physical_vs_l_vec, self._physical_l_matrix),
            "R": (query.physical_vs_r_vec, self._physical_r_matrix),
        }.get(vs_hand, (query.physical_vec, self._physical_matrix))
        phys_q = normalizer.normalize_physical(phys_query_vec)
        physical_scores = physical_rbf.score_batch(phys_q, phys_matrix)

        # --- Platoon sub-scores ---
        # A split counts for a candidate only with 30 or more PA on that side;
        # otherwise the candidate takes a neutral 0.5. With vs_hand, compare
        # that split alone. Without it, average the two splits equally when
        # both count, take the one that counts, or take 0.5 when neither does.
        platoon_scores = np.zeros(n, dtype=np.float64)

        if vs_hand == "L" or vs_hand is None:
            pl_q = normalizer.normalize_platoon_l(query.platoon_vs_l_vec)
            pl_scores = platoon_rbf.score_batch(pl_q, self._platoon_l_matrix)
        if vs_hand == "R" or vs_hand is None:
            pr_q = normalizer.normalize_platoon_r(query.platoon_vs_r_vec)
            pr_scores = platoon_rbf.score_batch(pr_q, self._platoon_r_matrix)

        if vs_hand == "L":
            # The vs-L split alone; a candidate under 30 PA vs L scores 0.5.
            platoon_scores = pl_scores
            platoon_scores[~self._platoon_l_valid] = 0.5  # neutral score
            query_platoon_valid = query.sample_pa_vs_l >= 30
        elif vs_hand == "R":
            platoon_scores = pr_scores
            platoon_scores[~self._platoon_r_valid] = 0.5
            query_platoon_valid = query.sample_pa_vs_r >= 30
        else:
            # Average the splits that count for each candidate.
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
        w_disc, w_bb, w_plat, w_pow, w_phys = sub_score_weights(
            platoon_context=vs_hand is not None and query_platoon_valid,
            physical_available=normalizer.physical_mean is not None,
        )

        # --- Composite score ---
        composite = (
            w_disc * disc_scores
            + w_bb * bb_scores
            + w_plat * platoon_scores
            + w_pow * power_scores
            + w_phys * physical_scores
        )

        # Bats-mismatch penalty
        penalties = bats_penalty_vector(query.bats, self.bats_list)
        composite *= penalties

        # The pair confidence: a thin sample on either side lowers the score.
        pair_confidence = np.minimum(query.eb_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # --- Build results (leave out the query's own batter-season) ---
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
                    physical_score=float(physical_scores[i]),
                    sample_pa=cand.sample_pa,
                )
            )

        return results


# ============================================================================
# Main Engine
# ============================================================================


class BatterSimilarityEngine:
    """
    The batter-to-batter similarity engine.

    A query scores EVERY batter-season profile, the batter's other seasons
    included. A query that names a pitcher hand compares that platoon split
    and, for a batter with a real sample against it, gives the platoon group
    the most weight.

    Usage:
        engine = BatterSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2023, 2024, 2025, 2026])

        # Every similar batter-season
        results = engine.query(batter_id=665742, season=2025)

        # The top 20 against a right-handed pitcher
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
        #: The date every loaded profile's data runs through. None when the
        #: table has no asof_date column or every row's stamp is NULL.
        self._asof_date = None

        # One weighted kernel per sub-score, at the module defaults.
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
        self._physical_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_PHYSICAL,
            reliability_weights=np.array([w for _, w in PHYSICAL_FEATURES]),
        )

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the five sub-score kernels from a calibration report.

        The report's sigmas are fitted to a median similarity of 0.50 over the
        population. A sigma left at 0.0 keeps the kernel's current sigma.
        Reliability weights left at None take the module defaults. A partial
        report therefore changes only what it fitted. Only the query-time
        kernels change, so the call is safe before or after ``build``. The EB
        prior acts at build time, and this method does not re-apply it.
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        def _wts(field: str, default: list[float]) -> NDArray[np.float64]:
            w = getattr(report, field, None)
            return np.asarray(w, dtype=np.float64) if w is not None else np.array(default)

        self._disc_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_discipline", self._disc_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_discipline", [w for _, w in DISCIPLINE_FEATURES]
            ),
        )
        self._bb_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_batted_ball", self._bb_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_batted_ball", [w for _, w in BATTED_BALL_FEATURES]
            ),
        )
        self._platoon_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_platoon", self._platoon_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_platoon", [w for _, w in PLATOON_FEATURES]
            ),
        )
        self._power_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_power", self._power_rbf.sigma),
            reliability_weights=_wts("reliability_weights_power", [w for _, w in POWER_FEATURES]),
        )
        self._physical_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_physical", self._physical_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_physical", [w for _, w in PHYSICAL_FEATURES]
            ),
        )
        log.info(
            "SIM-406: applied calibration to BatterSimilarityEngine "
            "(sigma_disc=%.4f, sigma_bb=%.4f, sigma_plat=%.4f, sigma_pow=%.4f, "
            "sigma_phys=%.4f).",
            self._disc_rbf.sigma,
            self._bb_rbf.sigma,
            self._platoon_rbf.sigma,
            self._power_rbf.sigma,
            self._physical_rbf.sigma,
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load the profiles, shrink them, fit the normalizer and build the partition."""
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

            # Both splits shrink toward the overall league rates of the same
            # names, not toward a mean per pitcher hand.
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

        # An optional column the table lacks is selected as NULL, so the build
        # does not fail on it. The optional columns are xba, xslg, the physical
        # block and asof_date. The profile computor writes no xba or xslg, and
        # the _v helper turns their NULLs into 0.0 for every batter. A feature
        # with no spread adds no distance, but it keeps its share of the power
        # group's weights.
        _present = duck_columns(conn, "derived", "batter_season_metrics")

        def _opt(name: str) -> str:
            return f"bsm.{name}" if name in _present else f"NULL AS {name}"

        def _phys_cols(suffix: str) -> str:
            return ", ".join(_opt(f"{name}{suffix}") for name, _ in PHYSICAL_FEATURES)

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
                bsm.below_minimum_sample,
                -- SIM-529: physical swing and stance, 10 features x 3 contexts.
                -- Every one goes through _opt, so this engine still builds
                -- against a database that has not run migration 0025 yet — the
                -- columns come back NULL and the group scores neutrally.
                {_phys_cols("")},
                {_phys_cols("_vs_l")},
                {_phys_cols("_vs_r")},
                {_opt("asof_date")}
            FROM derived.batter_season_metrics bsm
            WHERE NOT bsm.below_minimum_sample
              {season_filter}
        """).fetchall()

        log.info("Loading %d batter profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff (checked after the loop).
        asof_values: set = set()

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
            ) = row[:_N_BASE_COLUMNS]

            # The physical block is a fixed-width tail (3 contexts x 10
            # features), read by position instead of by 30 more names in the
            # unpack above. The last column is asof_date.
            phys = row[_N_BASE_COLUMNS:-1]
            asof_values.add(row[-1])
            n_phys = len(PHYSICAL_FEATURES)
            if len(phys) != 3 * n_phys:
                raise RuntimeError(
                    f"batter profile row has {len(phys)} physical columns, expected {3 * n_phys}"
                )

            def _v(vals):
                # A NULL rate becomes 0.0, not NaN, so the EB shrinkage does not
                # treat it as missing.
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            def _phys_v(vals):
                # NaN, not 0.0: a zero bat speed would read as the slowest swing
                # in the league. A NaN reads as the league mean once z-scored.
                return np.array([np.nan if v is None else float(v) for v in vals], dtype=np.float64)

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
                physical_vec=_phys_v(phys[:n_phys]),
                physical_vs_l_vec=_phys_v(phys[n_phys : 2 * n_phys]),
                physical_vs_r_vec=_phys_v(phys[2 * n_phys :]),
                sample_pa_vs_l=pa_l or 0,
                sample_pa_vs_r=pa_r or 0,
                eb_alpha=self._shrinkage.alpha(sample_pa),
                below_minimum=bool(below_min),
            )

        # Refuse a mixed set (the point-in-time profiles, SIM-534). Profiles
        # built at two cutoffs are not comparable: one batter's season is
        # truncated, another's complete, and nothing downstream shows it. This
        # load is the cheapest place to catch a half-finished rebuild.
        if len(asof_values) > 1:
            raise RuntimeError(
                "batter profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Batter profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Shrink each profile's rate vectors toward the season's league mean.

        A season with no league-average row is not shrunk. The physical
        vectors are not shrunk.
        """
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

            # Each platoon vector shrinks by its own split sample.
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
            The pitcher hand (see ``BatterPartition.score_all``). The nightly
            actor matrix passes None; the Similarity Explorer passes a hand.

        Returns
        -------
        Every other batter-season, sorted by score, descending (the top
        ``n`` when ``n`` is given). An empty list when the query batter-season
        has no profile.
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
            physical_rbf=self._physical_rbf,
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
        """Score one batter-season against another; None when either is missing."""
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
            physical_score=scores[5],
            sample_pa=pb.sample_pa,
        )

    def _score_pair(
        self,
        query: BatterProfile,
        candidate: BatterProfile,
        vs_hand: str | None = None,
    ) -> tuple[float, float, float, float, float, float]:
        """The composite and the five sub-scores between two profiles.

        ⚠ This must agree with ``BatterPartition.score_all`` on every pair.
        ``score_all`` is the vectorized path the nightly actor matrix uses;
        this one serves ``query_pair``. ``test_batter_physical_features.py``
        compares them, because the two drifting apart is silent. The test's
        batters all have 30 or more PA against each hand. Below that the two
        paths differ: this one applies neither the neutral 0.5 platoon score
        nor the same rule for the platoon-context weights.
        """
        disc_q = self._normalizer.normalize_discipline(query.discipline_vec)
        disc_c = self._normalizer.normalize_discipline(candidate.discipline_vec)
        disc_s = self._disc_rbf.score(disc_q, disc_c)

        bb_q = self._normalizer.normalize_batted_ball(query.batted_ball_vec)
        bb_c = self._normalizer.normalize_batted_ball(candidate.batted_ball_vec)
        bb_s = self._bb_rbf.score(bb_q, bb_c)

        power_q = self._normalizer.normalize_power(query.power_vec)
        power_c = self._normalizer.normalize_power(candidate.power_vec)
        power_s = self._power_rbf.score(power_q, power_c)

        # Physical swing and stance, from the side the batter uses against this
        # pitcher hand.
        phys_attr = {
            "L": "physical_vs_l_vec",
            "R": "physical_vs_r_vec",
        }.get(vs_hand, "physical_vec")
        phys_q = self._normalizer.normalize_physical(getattr(query, phys_attr))
        phys_c = self._normalizer.normalize_physical(getattr(candidate, phys_attr))
        physical_s = self._physical_rbf.score(phys_q, phys_c)

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
        w_d, w_b, w_p, w_pw, w_ph = sub_score_weights(
            platoon_context=vs_hand is not None and query_platoon_valid,
            physical_available=self._normalizer.physical_mean is not None,
        )

        composite = w_d * disc_s + w_b * bb_s + w_p * platoon_s + w_pw * power_s + w_ph * physical_s
        composite *= bats_penalty(query.bats, candidate.bats)

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(disc_s),
            float(bb_s),
            float(platoon_s),
            float(power_s),
            float(physical_s),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, batter_id: int, season: int) -> BatterProfile | None:
        return self._profiles.get((batter_id, season))

    @property
    def asof_date(self):
        """The date every loaded profile's data runs through, or None."""
        return self._asof_date

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = BatterSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_batter_diagnostics(engine, n_query_samples=50)
    print(report)
    results = engine.query(592450, 2025)
