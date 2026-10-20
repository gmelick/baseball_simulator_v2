"""The fielder engine: how alike two fielders are at one position.

The engine scores two fielder-position-season profiles from 0 to 1 (1 = the
same). The score is a weighted sum of four sub-scores. Each sub-score is an
RBF (a Gaussian kernel, score = exp(-distance² / 2σ²)) over z-scored features
with a reliability weight per feature (``similarity/engines/rbf_kernel.py``).
A z-score is the value minus the position's mean, over its standard deviation.

Infielders (1B, 2B, 3B, SS):
  1. Range (45%): our outs above average (OAA, the plays a fielder makes
     beyond an average fielder) to the glove side, to the arm side, charging
     and going deep; the catch percentage added; and Savant's OAA at the
     position per 100 of our chances. Six features.
  2. Double play (30%): the conversions above expected, the attempt rate and
     the success rate. 2B and SS add the pivot above expected.
  3. Errors (15%): the fielding error rate (hands) and the throwing error
     rate (arm accuracy). These are two separate skills.
  4. Specialty (10%): the bunt defense (1B, 3B) and the scoop rate (1B).
     2B and SS read as neutral.

Outfielders (LF, CF, RF):
  1. Range (40%): the same six features, then the three parts of Savant's
     outfield jump (reaction, burst, route). Nine features. The six shrink on
     the batted balls; the three jump parts shrink on their own plays.
  2. Arm (30%): the throw velocity (Savant's arm-strength board), the
     advancement prevention and the thrown-out rate. The two rates come from
     our advancement opportunity pool and shrink on the arm's own chances.
  3. Star plays (15%): the catch rates on 5-star, 4-star and routine chances.
     They separate elite range from sure hands.
  4. Errors (15%): the fielding and the throwing error rates.

Each position is a partition of its own. The engine never compares two
positions, not even 2B with SS: the skills differ (the range direction, the
throw distance, the double-play role), so a cross-position score adds noise.
A player's seasons at two positions are two independent profiles. A query
scores the query profile against every profile at its position, every season
included.

The feature choice follows "The Book" (Tango, Lichtman, Dolphin), Statcast's
OAA model and the defensive spectrum (each position weights different
skills). Defensive metrics stabilize slowly, so the EB shrinkage (empirical
Bayes: a pull toward the position's league mean, by sample size) uses a
larger prior than the batter engine. Approximate stabilization points:
  - OAA and the range metrics: 600 to 800 innings (one full season)
  - the error rates: 1,000 innings or more (noisy)
  - the double-play rates: about 200 double-play chances
  - the outfield arm: about 100 advancement chances

Usage:
  engine = FielderSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(player_id=660271, position="SS", season=2025)  # every SS
  results = engine.query(player_id=660271, position="SS", season=2025, n=20)  # top 20
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import duckdb
import numpy as np
from numpy.typing import NDArray

from db.introspect import duck_columns
from similarity.engines.rbf_kernel import WeightedRBFSimilarity
from similarity.similarity_diagnostics import run_fielder_diagnostics

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
log = logging.getLogger("fielder_similarity")


# ============================================================================
# Feature definitions and weights
# ============================================================================

# --- Position groups ---
INFIELD_POSITIONS = {"1B", "2B", "3B", "SS"}
OUTFIELD_POSITIONS = {"LF", "CF", "RF"}
ALL_POSITIONS = INFIELD_POSITIONS | OUTFIELD_POSITIONS

# --- Infield feature definitions ---
# Range: our directional OAA, the catch percentage added and Savant's OAA.
# The directional parts show WHERE a fielder excels (the glove side, the
# charge), which says more than one total OAA.
# Each weight is the feature's year-to-year repeat as `make calibrate` fitted
# it (the Savant fielding data, SIM-532): consecutive seasons of one player at
# ONE position, 2017 to 2026, 50 or more batted balls. The actor score-matrix
# build reads these defaults, not the calibration report, so the run book
# copies the fitted values here.
IF_RANGE_FEATURES = [
    # feature_name,          reliability_weight
    ("oaa_glove_side", 0.620),
    ("oaa_arm_side", 0.548),
    ("oaa_charging", 0.452),
    ("oaa_deep", 0.535),
    ("catch_pct_added", 0.529),
    # Savant's OAA at this position per 100 of our chances (an owner
    # decision, SIM-532). Within a position it repeats year to year at 0.34
    # to 0.63 (the seasons since the shift ban); the fit over all ten seasons
    # reads 0.356. It shrinks on the batted balls, as our five do.
    ("savant_oaa_per_100", 0.356),
]

# Double play: the conversion skill separates the great infielders. The
# conversions above expected carry the skill; the attempt rate (aggression)
# and the success rate (reliability) separate two styles.
IF_DP_FEATURES = [
    ("dp_above_expected", 0.400),  # stabilizes ~150 DP opps
    ("dp_attempt_rate", 0.100),  # stabilizes ~100 DP opps
    ("dp_success_rate", 0.500),  # noisier — depends on runner speed
]

# The pivot (2B and SS only; a corner infielder has none). The engine
# appends it to a middle infielder's double-play vector.
IF_PIVOT_FEATURES = [
    ("dp_pivot_above_expected", 0.517),
]

# Errors: the fielding error (hands, footwork) and the throwing error (arm
# accuracy) are two separate skills.
IF_ERROR_FEATURES = [
    ("fielding_error_rate", 0.100),  # stabilizes slowly (~1000 innings)
    ("throwing_error_rate", 0.100),  # slightly more stable — discrete throws
]

# Specialty: the niche skills of one position. 1B and 3B: the bunt defense.
# 1B also: the scoop of a low throw. 2B and SS read as neutral: a missing
# value takes the position's mean.
IF_SPECIALTY_FEATURES = [
    ("bunt_fielding_rate", 0.100),  # stabilizes ~80 bunt opps
    ("scoop_success_rate", 0.713),  # 1B only; stabilizes ~100 scoop opps
]

# --- Outfield feature definitions ---
# Range: our five components, Savant's OAA at this position, then the three
# parts of Savant's outfield jump (the Savant fielding data, SIM-532). Each
# weight is the feature's fitted year-to-year repeat within a position;
# `make calibrate` refits them and the run book copies the values here. The
# first six shrink on the batted balls; the three jump parts shrink on their
# own plays (JUMP_ALPHA_PRIOR_PLAYS). Reaction and route pull against each
# other by Savant's construction (r -0.79: a fast first step and a straight
# line trade off), so the pair carries about a third less than two
# independent features would; the reliability fit sees that.
OF_RANGE_FEATURES = [  # weight = the fitted year-to-year repeat (make calibrate)
    ("oaa_glove_side", 0.344),
    ("oaa_arm_side", 0.373),
    ("oaa_charging", 0.677),  # coming-in plays
    ("oaa_deep", 0.594),  # going-back
    ("catch_pct_added", 0.203),
    # Savant's OAA at this position per 100 of our chances. It repeats at
    # 0.38 to 0.61 within a position; fitted 0.472.
    ("savant_oaa_per_100", 0.472),
    # Feet gained against the league in the first 1.5 s after contact. It
    # repeats at 0.80 to 0.92 on 25 or more plays; fitted 0.775 over pairs
    # with 25 or more plays in both seasons.
    ("jump_reaction_ft", 0.775),
    # Feet gained in the next 1.5 s. It repeats at 0.64 to 0.82. It tracks
    # sprint speed in part (r 0.53 with the speed the profile stores).
    ("jump_burst_ft", 0.658),
    # Feet gained by the direction taken. It repeats at 0.71 to 0.90. It
    # pulls against the reaction (r -0.79).
    ("jump_route_ft", 0.743),
]

# The outfield range entries that shrink on the batted balls: our five
# components and Savant's OAA. The three jump parts after them shrink on their
# own plays.
OF_RANGE_BASE_COUNT = 6

# The jump parts' shrinkage prior, in plays. At this count the reaction's
# year-to-year repeat passes 0.8; about 80 outfielders a season clear it, and
# the median outfielder-season has 31 plays. The profile computor carries the
# same constant. Alpha (the share of his own reading) is 0.29 at 10 plays
# (two thirds of the way to the league mean), 0.55 at 31 and 0.72 at 64.
JUMP_ALPHA_PRIOR_PLAYS = 25

# The range keys whose absence from a league row means "not measured" (NaN),
# never 0.0. Our five components are centred on 0 by construction, so 0.0 is
# their mean, and their league rows carry no key. These four are real
# measurements that an older league row lacks; the shrinkage then leaves the
# raw value alone (see ``EmpiricalBayesShrinkage.shrink``).
_RANGE_KEYS_NAN_WHEN_ABSENT = frozenset(
    {"savant_oaa_per_100", "jump_reaction_ft", "jump_burst_ft", "jump_route_ft"}
)

# Arm (the outfield arm block, owner decisions, SIM-550): three features.
# Each weight is the year-to-year repeat `make calibrate` fitted over pairs of
# consecutive seasons of one player at ONE position, 50 or more chances in
# both.
#   * arm_strength: the throw velocity in mph from Savant's arm-strength
#     board, per position. It repeats at 0.844 and is nearly independent of
#     the prevention.
#   * arm_advancement_prevention: expected minus actual attempts per chance,
#     from sim.advancement_opportunity_pool. The expectation is the pool's
#     rate per season x decision x outs x position, so 0 is average at every
#     position. It repeats at 0.137; within a position the repeat is 0.1 to
#     0.3. A repeat read across positions is inflated: runners challenge a
#     left fielder less, so the position label repeats, not the arm. The
#     engine scores within a position, so the within-position figure counts.
#   * arm_thrown_out_rate: the runners thrown out per attempt against him.
#     It is thin (about five assists a season) and repeats at 0.240.
# Two columns stay out of the group. The raw hold rate is the prevention
# without the situation adjustment (r = 0.90). The run value (of_arm_runs) is
# an outcome summary that repeats at only 0.38 to 0.49.
# `make calibrate` refits these weights for the API; the run book copies the
# fitted values back here.
OF_ARM_FEATURES = [  # weight = the fitted year-to-year repeat (make calibrate)
    ("arm_strength", 0.844),  # throw velocity, mph (Savant, per position)
    ("arm_advancement_prevention", 0.137),  # expected - actual attempts per chance, from our pool
    ("arm_thrown_out_rate", 0.240),  # thrown out / attempts against him
]

# The arm rates' shrinkage prior, in chances: near this count the
# prevention's year-to-year repeat reaches about 0.5. The profile computor
# carries the same constant. Alpha is 0.33 at 25 chances (two thirds of the
# way to the league mean) and 0.80 at 200. It applies to the two pool rates
# only. The velocity is Savant's, measured over 50 or more throws; a missing
# velocity takes the league mean.
ARM_ALPHA_PRIOR_CHANCES = 50

# Star plays: the catch rates on chances bucketed by difficulty. They
# separate highlight-reel range from sure hands.
OF_STAR_FEATURES = [
    ("five_star_catch_rate", 0.100),  # very low sample — high noise
    ("four_star_catch_rate", 0.500),
    ("routine_catch_rate", 0.500),  # most stable — high-opportunity bucket
]

OF_ERROR_FEATURES = [
    ("fielding_error_rate", 0.100),
    ("throwing_error_rate", 0.100),
]

# --- Sub-score weights (each group sums to 1.0) ---
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

# --- RBF bandwidths (sigma), in z-score units ---
# The two range sigmas and the arm sigma are the values `make calibrate`
# fitted (the Savant fielding data, SIM-532; the outfield arm block, SIM-550)
# on the rows that carry every feature of the group. The run book copies them
# here because the actor score-matrix build does not read the calibration
# report.
RBF_SIGMA_IF_RANGE = 1.0549
RBF_SIGMA_IF_DP = 0.372
RBF_SIGMA_IF_ERRORS = 1.000
RBF_SIGMA_IF_SPECIALTY = 1.000

RBF_SIGMA_OF_RANGE = 1.0614
RBF_SIGMA_OF_ARM = 0.9912
RBF_SIGMA_OF_STARS = 0.449
RBF_SIGMA_OF_ERRORS = 1.000

# --- EB shrinkage ---
# Defensive metrics stabilize much more slowly than batting or pitching
# metrics. The prior is larger here than in the batter engine (5), so a
# fielder's vectors lean on the position's league mean longer. The prior
# counts batted balls.
EB_N_PRIOR = 15

# The profile computor's minimum sample (MIN_FIELDER_PLAYS, 50). The computor
# flags a smaller profile below_minimum_sample, and the load skips it. No
# engine code reads this constant; the tests do.
MIN_FIELDER_BATTED_BALLS = 50


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class FielderProfile:
    """One fielder-position-season profile, as the engine scores it."""

    player_id: int
    position: str  # 1B, 2B, 3B, SS, LF, CF, RF
    season: int
    innings_played: float
    sample_batted_balls: int

    # The feature vectors: shrunk at build time, z-scored when scored.
    range_vec: NDArray[np.float64]
    error_vec: NDArray[np.float64]

    # The vectors of one position group.
    # Infielders: dp_vec and specialty_vec
    # Outfielders: arm_vec and star_vec
    dp_vec: NDArray[np.float64] | None = None
    specialty_vec: NDArray[np.float64] | None = None
    arm_vec: NDArray[np.float64] | None = None
    star_vec: NDArray[np.float64] | None = None

    # EB shrinkage: alpha is the share of his own reading on the batted balls.
    eb_alpha: float = 1.0
    below_minimum: bool = False

    # The arm group's own sample: the chances to advance on a ball he fielded
    # at this position (arm_opportunities; 0 when NULL). The two pool rates of
    # the arm shrink on this count, never on the batted balls. Outfielders
    # only; 0 for an infielder.
    sample_arm_chances: int = 0

    # The jump group's own sample: the plays Savant scored for his jump this
    # season (jump_plays; 0 when NULL). The three jump parts of the range
    # vector shrink on this count at JUMP_ALPHA_PRIOR_PLAYS, never on the
    # batted balls. Outfielders only; 0 for an infielder.
    sample_jump_plays: int = 0


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One candidate's score against the query fielder."""

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
# EB shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """EB shrinkage: pull a raw vector toward the position's league mean by sample size."""

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int, n_prior: int | None = None) -> float:
        """The share of his own reading: ``n_samples / (n_samples + prior)``.

        ``n_prior`` overrides the instance's prior for one call.
        """
        prior = self.n_prior if n_prior is None else n_prior
        return n_samples / (n_samples + prior)

    def shrink(
        self,
        raw_vec: NDArray[np.float64],
        avg_vec: NDArray[np.float64],
        n_samples: int,
        n_prior: int | None = None,
    ) -> NDArray[np.float64]:
        """Pull ``raw_vec`` toward ``avg_vec``; the raw value keeps the share ``alpha``.

        ``n_prior`` overrides the instance's prior for one call: the outfield
        arm rates and jump parts shrink on their own counts. ``None`` keeps the
        instance's prior.

        A NaN raw value becomes the league mean. A NaN league value (a key the
        league row lacks) leaves the raw value alone: a feature can shrink only
        toward a mean that exists. The steal engine uses the same rule
        (SIM-531). When both are NaN, the result is NaN. The error, DP,
        specialty and star league vectors never carry NaN: their loader maps an
        absent key to 0.0. The arm vector and the range vector's Savant and
        jump entries can, because an older league row lacks their keys.
        """
        a = self.alpha(n_samples, n_prior)
        avg = np.where(np.isnan(avg_vec), raw_vec, avg_vec)
        raw_clean = np.where(np.isnan(raw_vec), avg, raw_vec)
        return a * raw_clean + (1.0 - a) * avg


# ============================================================================
# Feature normalization
# ============================================================================


@dataclass(slots=True)
class FeatureNormalizer:
    """The z-score of each feature group, fitted per position.

    Each position has its own means and standard deviations, because the
    distributions differ (a centre fielder's range is not a first baseman's).
    A NaN after z-scoring becomes 0, the position's mean. A position with no
    fit passes its vectors through unchanged. A zero standard deviation reads
    as 1.
    """

    # Keys: position string → (mean_array, std_array)
    range_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    error_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    dp_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    specialty_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    arm_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)
    star_params: dict[str, tuple[NDArray, NDArray]] = field(default_factory=dict)

    def fit(self, profiles_by_position: dict[str, list[FielderProfile]]) -> None:
        """Fit each position's means and standard deviations from its profiles."""
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
# Position partition: batch scoring
# ============================================================================


class PositionPartition:
    """Every profile at one position, with its z-scored feature matrices.

    The matrices let one query score every profile at the position in one
    vector step per sub-score.
    """

    def __init__(self, position: str) -> None:
        self.position = position
        self.is_infield = position in INFIELD_POSITIONS
        self.profiles: list[FielderProfile] = []
        self.keys: list[tuple[int, str, int]] = []  # (player_id, position, season)

        # The z-scored feature matrices, one row per profile.
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
        """Z-score every profile's vectors into the partition's matrices.

        A missing group vector reads as zeros before the z-score.
        """
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
        """Score the query against every profile in this partition but its own.

        The composite is the weighted sum of the four sub-scores, times the
        square root of the lower EB alpha of the pair: a thin profile on
        either side lowers the score.
        """
        if not self.profiles:
            return []

        n = len(self.profiles)
        pos = self.position
        query_key = (query.player_id, query.position, query.season)

        # The sub-scores, one vector step each.
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

        # Discount by the thinner profile's confidence.
        pair_confidence = np.minimum(query.eb_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # The results, without the query's own profile.
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
# The engine
# ============================================================================


def _range_league_vector(pj: dict, features: list[tuple[str, float]]) -> NDArray[np.float64]:
    """One league row's range vector, in ``features``' order.

    Our five components fall back to 0.0: the league row never carries their
    keys, and they are centred on 0 by construction, so 0.0 is their mean.
    Savant's OAA and the three jump parts (``_RANGE_KEYS_NAN_WHEN_ABSENT``)
    read NaN when an older row lacks them, and ``shrink`` then leaves the raw
    value alone.
    """
    return np.array(
        [
            (np.nan if pj.get(f) is None else float(pj[f]))
            if f in _RANGE_KEYS_NAN_WHEN_ABSENT
            else (pj.get(f, 0.0) or 0.0)
            for f, _ in features
        ],
        dtype=np.float64,
    )


class FielderSimilarityEngine:
    """The fielder-to-fielder similarity engine.

    A query scores the query profile against every profile at its position,
    every season included. Each position (1B, 2B, 3B, SS, LF, CF, RF) is a
    partition of its own; the engine never compares two positions.

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

        # The shrinkage targets: the league-mean vector per feature group,
        # position and season. Keys: group → position → season → NDArray.
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

        # The cutoff (asof_date) every loaded profile declared. None when the
        # table has no asof_date column (a database before DuckDB migration
        # 0028) or every row's value is NULL.
        self._asof_date = None

        # One partition per position.
        self._partitions: dict[str, PositionPartition] = {
            pos: PositionPartition(pos) for pos in ALL_POSITIONS
        }

        # One kernel per sub-score, apart for the infield and the outfield. The
        # corner infield's double-play kernel has no pivot weight.
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
    # Calibration
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the nine sub-score kernels from a fitted calibration report.

        Each sub-score's sigma, and its reliability weights when present,
        replace the current values. A field left at 0.0 or None keeps the
        current value, so a partial report still applies what it has. The two
        double-play kernels share ``sigma_if_dp``. The middle-infield kernel
        joins the double-play and pivot weights, as the constructor does; the
        corner kernel takes the double-play weights alone. Only the kernels
        change: the partitions' z-scored matrices stay as built.
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        def _wts(field: str, default: list[float]) -> NDArray[np.float64]:
            w = getattr(report, field, None)
            if w is None:
                return np.array(default)
            arr = np.asarray(w, dtype=np.float64)
            # A report fitted on another feature list carries the wrong number
            # of weights. Applying it would fail on the first query: the
            # weights would not broadcast against the feature vector. Keep the
            # module defaults until ``make calibrate`` refits the report. The
            # baserunner engine follows the same rule.
            if arr.shape != (len(default),):
                log.warning(
                    "SIM-550/532: %s holds %d weights but the engine has %d features; "
                    "keeping the module defaults until the report is refitted.",
                    field,
                    arr.size,
                    len(default),
                )
                return np.array(default)
            return arr

        self._if_range_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_if_range", self._if_range_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_if_range", [w for _, w in IF_RANGE_FEATURES]
            ),
        )
        # The middle-infield kernel joins the double-play and pivot weights.
        # Each part falls back to its own module default, so a report that
        # carries only the double-play weights still feeds them to both the
        # middle (`_if_dp_rbf`) and the corner (`_if_dp_rbf_corner`) kernels.
        dp_part = _wts("reliability_weights_if_dp", [w for _, w in IF_DP_FEATURES])
        pivot_part = _wts("reliability_weights_if_pivot", [w for _, w in IF_PIVOT_FEATURES])
        self._if_dp_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_if_dp", self._if_dp_rbf.sigma),
            reliability_weights=np.concatenate([dp_part, pivot_part]),
        )
        self._if_dp_rbf_corner = WeightedRBFSimilarity(
            sigma=_sig("sigma_if_dp", self._if_dp_rbf_corner.sigma),
            reliability_weights=_wts("reliability_weights_if_dp", [w for _, w in IF_DP_FEATURES]),
        )
        self._if_error_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_if_errors", self._if_error_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_if_error", [w for _, w in IF_ERROR_FEATURES]
            ),
        )
        self._if_specialty_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_if_specialty", self._if_specialty_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_if_specialty", [w for _, w in IF_SPECIALTY_FEATURES]
            ),
        )
        self._of_range_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_of_range", self._of_range_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_of_range", [w for _, w in OF_RANGE_FEATURES]
            ),
        )
        self._of_arm_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_of_arm", self._of_arm_rbf.sigma),
            reliability_weights=_wts("reliability_weights_of_arm", [w for _, w in OF_ARM_FEATURES]),
        )
        self._of_star_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_of_stars", self._of_star_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_of_star", [w for _, w in OF_STAR_FEATURES]
            ),
        )
        self._of_error_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_of_errors", self._of_error_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_of_error", [w for _, w in OF_ERROR_FEATURES]
            ),
        )
        log.info(
            "SIM-406: applied calibration to FielderSimilarityEngine "
            "(IF range/dp/err/spec=%.3f/%.3f/%.3f/%.3f, OF range/arm/star/err=%.3f/%.3f/%.3f/%.3f).",
            self._if_range_rbf.sigma,
            self._if_dp_rbf.sigma,
            self._if_error_rbf.sigma,
            self._if_specialty_rbf.sigma,
            self._of_range_rbf.sigma,
            self._of_arm_rbf.sigma,
            self._of_star_rbf.sigma,
            self._of_error_rbf.sigma,
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load the league means and the profiles, shrink them, and build the partitions."""
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)

        try:
            self._load_positional_averages(conn, seasons)
            self._load_profiles(conn, seasons)
        finally:
            conn.close()

        self._apply_shrinkage()

        # Group the profiles by position for the z-score fit and the partitions.
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
    # Data loading
    # ------------------------------------------------------------------

    def _load_positional_averages(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        """Load each position's league-mean vectors per season, the shrinkage targets.

        The source is derived.league_averages. Without that table the engine
        builds with no shrinkage targets.
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
                self._pos_avg["range"][pos][season] = _range_league_vector(pj, IF_RANGE_FEATURES)
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
                self._pos_avg["range"][pos][season] = _range_league_vector(pj, OF_RANGE_FEATURES)
                # An arm key the league row lacks (an older row) is NaN, never
                # 0.0: 0.0 mph or a 0.0 thrown-out rate is the weakest arm in
                # the league. The shrinkage then leaves the raw value alone.
                self._pos_avg["arm"][pos][season] = np.array(
                    [np.nan if pj.get(f) is None else float(pj[f]) for f, _ in OF_ARM_FEATURES],
                    dtype=np.float64,
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
        """Load every fielder-position-season profile that meets the minimum sample.

        Raise when the profiles declare more than one cutoff (asof_date).
        """
        season_filter = ""
        if seasons:
            sl = ", ".join(str(s) for s in seasons)
            season_filter = f"AND fsm.season IN ({sl})"

        # The asof_date column is optional: a database before DuckDB migration
        # 0028 still builds, and asof_date reads NULL.
        _present = duck_columns(conn, "derived", "fielder_season_metrics")
        _asof_col = "fsm.asof_date" if "asof_date" in _present else "NULL AS asof_date"

        # The arm block's four columns, guarded the same way: an older table
        # still loads, and the arm reads NULL (NaN below). The order is
        # OF_ARM_FEATURES' order, then the chance count the arm shrinks on.
        _arm_cols = ", ".join(
            f"fsm.{col}" if col in _present else f"NULL AS {col}"
            for col in (
                "arm_strength",
                "arm_advancement_prevention",
                "arm_thrown_out_rate",
                "arm_opportunities",
            )
        )

        # Savant's OAA, the three jump parts and the jump's play count, guarded
        # the same way: a table before DuckDB migration 0030 still loads, and
        # they read NULL (NaN below). They sit right after catch_pct_added, so
        # the range block stays contiguous: Savant's OAA completes the infield
        # range vector (six), and the jump parts complete the outfield one
        # (nine).
        _oaa_jump_cols = ", ".join(
            f"fsm.{col}" if col in _present else f"NULL AS {col}"
            for col in (
                "savant_oaa_per_100",
                "jump_reaction_ft",
                "jump_burst_ft",
                "jump_route_ft",
                "jump_plays",
            )
        )

        rows = conn.execute(f"""
            SELECT
                fsm.player_id, fsm.position, fsm.season,
                fsm.innings_played, fsm.sample_batted_balls,
                -- Range / OAA (5 features — shared IF/OF)
                fsm.oaa_glove_side, fsm.oaa_arm_side,
                fsm.oaa_charging, fsm.oaa_deep,
                fsm.catch_pct_added,
                -- Range, continued (SIM-532: Savant's figure for both groups;
                -- the three jump parts and their play count for the outfield)
                {_oaa_jump_cols},
                -- Errors (2 features — shared IF/OF)
                fsm.fielding_error_rate, fsm.throwing_error_rate,
                -- DP (IF only)
                fsm.dp_above_expected, fsm.dp_attempt_rate, fsm.dp_success_rate,
                fsm.dp_pivot_above_expected,
                -- Specialty (IF only)
                fsm.bunt_fielding_rate, fsm.scoop_success_rate,
                -- Arm (OF only; SIM-550: velocity, prevention, thrown-out, chances)
                {_arm_cols},
                -- Star plays (OF only — compute rates from counts)
                fsm.five_star_opps, fsm.five_star_catches,
                fsm.four_star_opps, fsm.four_star_catches,
                fsm.routine_opps, fsm.routine_catches,
                -- Meta
                fsm.below_minimum_sample,
                {_asof_col}
            FROM derived.fielder_season_metrics fsm
            WHERE NOT fsm.below_minimum_sample
              AND fsm.position IN ('1B','2B','3B','SS','LF','CF','RF')
              {season_filter}
        """).fetchall()

        log.info("Loading %d fielder profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff (asof_date).
        asof_values: set = set()

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
                # Range, continued: 4 features and the jump's play count
                savant_per_100,
                jump_reaction,
                jump_burst,
                jump_route,
                jump_plays,
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
                # Arm: 3 features and the chance count
                arm_strength,
                arm_adv_prev,
                arm_throw_out,
                arm_opps,
                # Star play counts (6)
                five_opps,
                five_catches,
                four_opps,
                four_catches,
                routine_opps,
                routine_catches,
                # Meta
                below_min,
                asof_val,
            ) = row
            asof_values.add(asof_val)

            def _v(vals):
                return np.array([v if v is not None else np.nan for v in vals], dtype=np.float64)

            # The star-play catch rates from the counts.
            five_star_rate = (five_catches / five_opps) if five_opps and five_opps > 0 else np.nan
            four_star_rate = (four_catches / four_opps) if four_opps and four_opps > 0 else np.nan
            routine_rate = (
                (routine_catches / routine_opps) if routine_opps and routine_opps > 0 else np.nan
            )

            if position not in ALL_POSITIONS:
                continue

            # The range vector in the group's feature order. The infield reads
            # our five plus Savant's OAA (six); the outfield adds the three
            # jump parts (nine). NULL loads as NaN (unmeasured), never 0.0: a
            # 0.0 reaction would be a league-average first step he never
            # showed.
            our_five = [oaa_gs, oaa_as, oaa_ch, oaa_deep, cpct_add]
            if position in INFIELD_POSITIONS:
                range_vec = _v([*our_five, savant_per_100])
            else:
                range_vec = _v([*our_five, savant_per_100, jump_reaction, jump_burst, jump_route])
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
                # OF_ARM_FEATURES' order. NULL loads as NaN (unmeasured), never
                # 0.0: a 0.0 mph velocity or a 0.0 thrown-out rate would be a
                # measurement he never produced.
                arm_vec = _v([arm_strength, arm_adv_prev, arm_throw_out])
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
                # The arm group's own sample (0 for an infielder).
                sample_arm_chances=int(arm_opps or 0),
                # The jump group's own sample (0 for an infielder).
                sample_jump_plays=int(jump_plays or 0) if position in OUTFIELD_POSITIONS else 0,
            )

        # Refuse profiles built at two cutoffs.
        if len(asof_values) > 1:
            raise RuntimeError(
                "fielder profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Fielder profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Shrink every profile's vectors toward the position's league mean.

        Every group shrinks on the profile's batted balls at ``EB_N_PRIOR``,
        with two exceptions in the outfield. The arm's two pool rates shrink
        on the arm's own chances at ``ARM_ALPHA_PRIOR_CHANCES``; the velocity
        stays as read, and a missing one takes the league mean. The three jump
        parts of the range vector shrink on the jump's own plays at
        ``JUMP_ALPHA_PRIOR_PLAYS``. A profile whose season has no league row
        keeps its raw vectors.
        """
        for _key, p in self._profiles.items():
            pos = p.position
            s = p.season
            n = p.sample_batted_balls

            # Range
            avg = self._pos_avg["range"].get(pos, {}).get(s)
            if avg is not None:
                if pos in INFIELD_POSITIONS:
                    # Our five components and Savant's OAA, all on the
                    # batted balls.
                    p.range_vec = self._shrinkage.shrink(p.range_vec, avg, n)
                else:
                    # The first six entries (our five and Savant's OAA)
                    # shrink on the batted balls. The three jump parts shrink
                    # on the jump's OWN plays: a ten-play outfielder's
                    # reaction is noise, yet he may have 600 batted balls.
                    # Alpha is 0.29 at 10 plays, 0.55 at 31 and 0.72 at 64.
                    # A NaN raw jump (no jump row) becomes the league mean
                    # inside ``shrink``. When the league mean is NaN too (an
                    # older league row), the NaN stays, and the normalizer
                    # reads it as the position mean (nan_to_num after the
                    # z-score).
                    base = self._shrinkage.shrink(
                        p.range_vec[:OF_RANGE_BASE_COUNT], avg[:OF_RANGE_BASE_COUNT], n
                    )
                    jump = self._shrinkage.shrink(
                        p.range_vec[OF_RANGE_BASE_COUNT:],
                        avg[OF_RANGE_BASE_COUNT:],
                        p.sample_jump_plays,
                        n_prior=JUMP_ALPHA_PRIOR_PLAYS,
                    )
                    p.range_vec = np.concatenate([base, jump])

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
                    # The two rates (prevention, thrown-out) shrink on the
                    # arm's OWN chances. An everyday outfielder has 600 batted
                    # balls but only 25 to 200 arm chances; shrunk on the
                    # batted balls, a 25-chance arm would read at face value.
                    # A NaN league value leaves the raw rate alone (see
                    # ``shrink``). Alpha is 0.33 at 25 chances and 0.80 at 200.
                    rates = self._shrinkage.shrink(
                        p.arm_vec[1:],
                        avg[1:],
                        p.sample_arm_chances,
                        n_prior=ARM_ALPHA_PRIOR_CHANCES,
                    )
                    # The velocity is Savant's, measured over 50 or more
                    # throws, so it stays as read. A missing one takes the
                    # league mean, never 0 mph.
                    velocity = np.where(np.isnan(p.arm_vec[:1]), avg[:1], p.arm_vec[:1])
                    p.arm_vec = np.concatenate([velocity, rates])

                avg = self._pos_avg["star"].get(pos, {}).get(s)
                if avg is not None and p.star_vec is not None:
                    p.star_vec = self._shrinkage.shrink(p.star_vec, avg, n)

    # ------------------------------------------------------------------
    # Kernel selector
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
        """Score the query fielder against every profile at the same position.

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
        Every SimilarityResult, sorted by score, highest first. The query's own
        (player_id, position, season) is left out; his other seasons at the
        position are in. An unknown position or profile returns an empty list.
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
        """Score fielder B against fielder A at one position.

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
        """Score two profiles at one position.

        Returns (composite, range, secondary, tertiary, error). Secondary is
        the double play (IF) or the arm (OF); tertiary is the specialty (IF)
        or the star plays (OF).
        """
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

        # Discount by the thinner profile's confidence.
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

    @property
    def asof_date(self):
        """The cutoff (asof_date) every loaded profile declared, or ``None``."""
        return self._asof_date

    def profile_ids(self) -> list[tuple[int, str, int]]:
        """Every (player_id, position, season) key in the engine."""
        return list(self._profiles.keys())

    def profile_ids_for_position(self, position: str) -> list[tuple[int, str, int]]:
        """Every profile key at one position."""
        return [k for k in self._profiles if k[1] == position]


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = FielderSimilarityEngine(duckdb_path="../../db/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    results = engine.query(592743, "SS", 2018, 25)
    report = run_fielder_diagnostics(engine, n_query_samples=50)
    print(report)
