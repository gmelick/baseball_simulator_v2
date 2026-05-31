"""
baserunner_similarity.py
========================
Step 2.4 — Baserunner-to-Baserunner Similarity (Extra Base)
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
baserunner-season profiles by combining three sub-score dimensions:

  1. Speed / Athleticism (35%) — the physical capacity to take extra
     bases, anchored by Statcast sprint speed.  This is the most
     reliable and stable feature — it changes very little season to
     season and is the strongest single predictor of extra-base
     advancement success.

  2. Aggression (40%) — how often a runner *attempts* to take an
     extra base across the four key situations (1st→3rd on singles,
     2nd→home on singles, 1st→home on doubles, tag-ups on fly balls).
     Attempt rate is the process metric — it captures the runner's
     decision-making tendencies and risk tolerance independent of
     whether they succeeded.  Also includes stop_rate, the inverse
     of overall attempt rate, stored explicitly for the engine.

  3. Success / Efficiency (25%) — given that a runner attempts,
     how often does he succeed?  This captures execution quality
     (reads, slides, rounding technique) and separates runners who
     attempt aggressively and succeed from those who attempt and get
     thrown out.

Research-Informed Design
------------------------
  Sprint speed is the fastest-stabilizing baserunning metric —
  it's effectively a physical constant within a season. Attempt
  rates stabilize by ~40-50 opportunities.  Success rates are
  noisier (conditional on the attempt, which is conditional on
  the opportunity), so they get the lowest weight.

  The engine does NOT partition by speed tier or any other
  attribute. All runners are compared to all others. The RBF
  kernel naturally handles the fact that a 31 ft/s sprinter
  is very dissimilar from a 25 ft/s slugger.

Dependencies
------------
  pip install duckdb numpy

Usage
-----
  engine = BaserunnerSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])

  results = engine.query(player_id=660271, season=2025)
  results = engine.query(player_id=660271, season=2025, n=20)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import duckdb
import numpy as np
from numpy.typing import NDArray

from similarity.similarity_diagnostics import run_baserunner_diagnostics

if TYPE_CHECKING:
    from similarity.similarity_calibration import CalibrationReport

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("baserunner_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Speed features ---
# Sprint speed is dominant. One feature, extremely reliable.
SPEED_FEATURES = [
    # feature_name,           reliability_weight
    ("sprint_speed", 0.202),  # nearly constant within season
]

# --- Aggression features (attempt rates + stop rate) ---
# How often the runner goes for it in each situation.
# Per-situation attempt rates capture tactical tendency;
# stop_rate is the overall inverse (stored for interpretability but
# redundant with extra_base_attempt_rate — low weight to avoid double-counting).
AGGRESSION_FEATURES = [
    ("extra_base_attempt_rate", 0.202),  # overall attempt rate — most stable
    ("first_to_third_attempt_rate", 0.167),  # specific situations — moderate sample
    ("second_to_home_attempt_rate", 0.100),
    ("first_to_home_attempt_rate", 0.112),  # rarer situation — noisier
    ("tag_up_attempt_rate", 0.100),  # rarest — noisiest
    ("stop_rate", 0.202),  # low weight — redundant with overall
]

# --- Success / efficiency features ---
# Given the runner attempted, how often did he succeed?
# Noisier because it's conditional on the attempt.
SUCCESS_FEATURES = [
    ("extra_base_success_rate", 0.500),  # overall success rate
    ("first_to_third_success_rate", 0.100),
    ("second_to_home_success_rate", 0.100),
    ("first_to_home_success_rate", 0.100),
    ("tag_up_success_rate", 0.100),
]

# --- Sub-score weights (sum to 1.0) ---
WEIGHT_SPEED = 0.35
WEIGHT_AGGRESSION = 0.40
WEIGHT_SUCCESS = 0.25
_TOTAL = WEIGHT_SPEED + WEIGHT_AGGRESSION + WEIGHT_SUCCESS
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters (gamma = 1 / (2σ²))
# Calibrated via similarity_calibration.py; these are initial values.
RBF_SIGMA_SPEED = 0.8171
RBF_SIGMA_AGGRESSION = 1.0478
RBF_SIGMA_SUCCESS = 1.0000

# Empirical Bayes shrinkage prior strength
# At EB_N_PRIOR advancement opportunities, shrinkage weight α = 0.5
EB_N_PRIOR = 15

# Minimum sample for inclusion (matches schema: below_minimum_sample)
MIN_ADVANCEMENT_OPPS = 20


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class BaserunnerProfile:
    """Complete baserunner-season profile for extra-base similarity scoring."""

    player_id: int
    season: int
    sample_advancement_opps: int

    # Feature vectors (raw values — normalized at query time)
    speed_vec: NDArray[np.float64]  # shape (len(SPEED_FEATURES),)
    aggression_vec: NDArray[np.float64]  # shape (len(AGGRESSION_FEATURES),)
    success_vec: NDArray[np.float64]  # shape (len(SUCCESS_FEATURES),)

    # Per-situation sample sizes for confidence weighting
    sample_first_to_third_opps: int = 0
    sample_second_to_home_opps: int = 0
    sample_first_to_home_opps: int = 0
    sample_tag_up_opps: int = 0

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    player_id: int
    season: int
    score: float  # composite [0, 1], 1 = identical
    speed_score: float
    aggression_score: float
    success_score: float
    sample_advancement_opps: int


# ============================================================================
# Reliability-Weighted RBF Kernel
# ============================================================================


class WeightedRBFSimilarity:
    """
    Gaussian RBF kernel with per-feature reliability weights and
    dimensionality-invariant scaling.

    K(x, y) = exp(-γ * Σ_i w_i * (x_i - y_i)²)

    Weights are normalized to sum to 1.0 so the exponent computes the
    weighted *average* per-feature squared distance, making the score
    independent of feature count per sub-score.
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
        diff = x - y
        diff = np.nan_to_num(diff, nan=0.0)
        dist_sq = np.dot(self.weights * diff, diff)
        return float(np.exp(-self.gamma * dist_sq))

    def score_batch(
        self,
        query: NDArray,
        candidates: NDArray,
    ) -> NDArray[np.float64]:
        diff = candidates - query[np.newaxis, :]
        diff = np.nan_to_num(diff, nan=0.0)
        dist_sq = np.sum(self.weights[np.newaxis, :] * diff**2, axis=1)
        return np.exp(-self.gamma * dist_sq)


# ============================================================================
# Empirical Bayes Shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
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
    """Z-score normalizer fit on the population of baserunner profiles."""

    speed_mean: NDArray | None = None
    speed_std: NDArray | None = None
    aggression_mean: NDArray | None = None
    aggression_std: NDArray | None = None
    success_mean: NDArray | None = None
    success_std: NDArray | None = None

    def fit(self, profiles: list[BaserunnerProfile]) -> None:
        if not profiles:
            return

        def _fit_group(vecs: list[NDArray]) -> tuple[NDArray, NDArray]:
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.speed_mean, self.speed_std = _fit_group([p.speed_vec for p in profiles])
        self.aggression_mean, self.aggression_std = _fit_group([p.aggression_vec for p in profiles])
        self.success_mean, self.success_std = _fit_group([p.success_vec for p in profiles])

    def _normalize(self, vec: NDArray, mean: NDArray | None, std: NDArray | None) -> NDArray:
        if mean is None:
            return vec
        normed = (vec - mean) / std
        return np.nan_to_num(normed, nan=0.0)

    def normalize_speed(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.speed_mean, self.speed_std)

    def normalize_aggression(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.aggression_mean, self.aggression_std)

    def normalize_success(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.success_mean, self.success_std)


# ============================================================================
# Scoring Partition — Vectorized Batch Scoring
# ============================================================================


class BaserunnerPartition:
    """
    Stores all baserunner profiles with pre-built normalized feature
    matrices for vectorized batch RBF scoring.
    """

    def __init__(self) -> None:
        self.profiles: list[BaserunnerProfile] = []
        self.keys: list[tuple[int, int]] = []

        self._speed_matrix: NDArray | None = None
        self._aggression_matrix: NDArray | None = None
        self._success_matrix: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(
        self,
        profiles: list[BaserunnerProfile],
        normalizer: FeatureNormalizer,
    ) -> None:
        self.profiles = profiles
        self.keys = [(p.player_id, p.season) for p in profiles]

        if not profiles:
            return

        speed_rows, agg_rows, success_rows = [], [], []
        alphas = []

        for p in profiles:
            speed_rows.append(normalizer.normalize_speed(p.speed_vec))
            agg_rows.append(normalizer.normalize_aggression(p.aggression_vec))
            success_rows.append(normalizer.normalize_success(p.success_vec))
            alphas.append(p.eb_alpha)

        self._speed_matrix = np.array(speed_rows, dtype=np.float64)
        self._aggression_matrix = np.array(agg_rows, dtype=np.float64)
        self._success_matrix = np.array(success_rows, dtype=np.float64)
        self._eb_alphas = np.array(alphas, dtype=np.float64)

    def score_all(
        self,
        query: BaserunnerProfile,
        normalizer: FeatureNormalizer,
        speed_rbf: WeightedRBFSimilarity,
        agg_rbf: WeightedRBFSimilarity,
        success_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        n = len(self.profiles)
        if n == 0:
            return []

        query_key = (query.player_id, query.season)

        # Normalize query vectors
        q_speed = normalizer.normalize_speed(query.speed_vec)
        q_agg = normalizer.normalize_aggression(query.aggression_vec)
        q_success = normalizer.normalize_success(query.success_vec)

        # Batch RBF scores
        speed_scores = speed_rbf.score_batch(q_speed, self._speed_matrix)
        agg_scores = agg_rbf.score_batch(q_agg, self._aggression_matrix)
        success_scores = success_rbf.score_batch(q_success, self._success_matrix)

        # Composite
        composite = (
            WEIGHT_SPEED * speed_scores
            + WEIGHT_AGGRESSION * agg_scores
            + WEIGHT_SUCCESS * success_scores
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
            results.append(
                SimilarityResult(
                    player_id=cand.player_id,
                    season=cand.season,
                    score=float(composite[i]),
                    speed_score=float(speed_scores[i]),
                    aggression_score=float(agg_scores[i]),
                    success_score=float(success_scores[i]),
                    sample_advancement_opps=cand.sample_advancement_opps,
                )
            )

        return results


# ============================================================================
# Main Engine
# ============================================================================


class BaserunnerSimilarityEngine:
    """
    Baserunner-to-Baserunner Extra-Base Similarity Engine.

    Scores computed EXHAUSTIVELY against all baserunner-season profiles.
    Cross-season comparisons are included.

    Usage:
        engine = BaserunnerSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])
        results = engine.query(player_id=660271, season=2025)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], BaserunnerProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "speed": {},
            "aggression": {},
            "success": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = BaserunnerPartition()

        # Build weighted RBF scorers
        self._speed_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_SPEED,
            reliability_weights=np.array([w for _, w in SPEED_FEATURES]),
        )
        self._agg_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_AGGRESSION,
            reliability_weights=np.array([w for _, w in AGGRESSION_FEATURES]),
        )
        self._success_rbf = WeightedRBFSimilarity(
            sigma=RBF_SIGMA_SUCCESS,
            reliability_weights=np.array([w for _, w in SUCCESS_FEATURES]),
        )

    # ------------------------------------------------------------------
    # Calibration wiring (SIM-406)
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """SIM-406: rebuild the speed/aggression/success RBF scorers from a report.

        A sigma (or reliability-weight) field left at its 0.0 / None default keeps
        the engine's current value, so a partial report degrades gracefully. Only
        the query-time scorers are swapped (safe before or after ``build``).
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        def _wts(field: str, default: list[float]) -> NDArray[np.float64]:
            w = getattr(report, field, None)
            return np.asarray(w, dtype=np.float64) if w is not None else np.array(default)

        self._speed_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_baserunner_speed", self._speed_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_baserunner_speed", [w for _, w in SPEED_FEATURES]
            ),
        )
        self._agg_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_baserunner_aggression", self._agg_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_baserunner_aggression", [w for _, w in AGGRESSION_FEATURES]
            ),
        )
        self._success_rbf = WeightedRBFSimilarity(
            sigma=_sig("sigma_baserunner_success", self._success_rbf.sigma),
            reliability_weights=_wts(
                "reliability_weights_baserunner_success", [w for _, w in SUCCESS_FEATURES]
            ),
        )
        log.info(
            "SIM-406: applied calibration to BaserunnerSimilarityEngine "
            "(sigma_speed=%.4f, sigma_agg=%.4f, sigma_suc=%.4f).",
            self._speed_rbf.sigma,
            self._agg_rbf.sigma,
            self._success_rbf.sigma,
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load all baserunner profiles, apply shrinkage, build matrices."""
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
            "BaserunnerSimilarityEngine built: %d profiles in %.2fs.",
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
                WHERE entity_type = 'baserunner'
                  {season_filter}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — no shrinkage fallbacks.")
            return

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw

            self._league_avg["speed"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in SPEED_FEATURES], dtype=np.float64
            )

            self._league_avg["aggression"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in AGGRESSION_FEATURES], dtype=np.float64
            )

            self._league_avg["success"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in SUCCESS_FEATURES], dtype=np.float64
            )

        log.info("Loaded baserunner league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        season_filter = ""
        if seasons:
            sl = ", ".join(str(s) for s in seasons)
            season_filter = f"AND brm.season IN ({sl})"

        rows = conn.execute(f"""
            SELECT
                brm.player_id, brm.season,
                brm.sample_advancement_opps,
                -- Speed (SIM-408: read the column already on the derived table
                -- instead of a pg.raw.sprint_speed join — the profile computor
                -- already materializes sprint_speed here, and the app's
                -- read-only DuckDB connection has no postgres `pg` catalog).
                brm.sprint_speed,
                -- Aggression
                brm.extra_base_attempt_rate,
                brm.first_to_third_attempt_rate,
                brm.second_to_home_attempt_rate,
                brm.first_to_home_attempt_rate,
                brm.tag_up_attempt_rate,
                brm.stop_rate,
                -- Success
                brm.extra_base_success_rate,
                brm.first_to_third_success_rate,
                brm.second_to_home_success_rate,
                brm.first_to_home_success_rate,
                brm.tag_up_success_rate,
                -- Per-situation samples
                brm.sample_first_to_third_opps,
                brm.sample_second_to_home_opps,
                brm.sample_first_to_home_opps,
                brm.sample_tag_up_opps,
                brm.below_minimum_sample
            FROM derived.baserunner_season_metrics brm
            WHERE NOT brm.below_minimum_sample
              {season_filter}
        """).fetchall()

        log.info("Loading %d baserunner profiles from DuckDB …", len(rows))

        for row in rows:
            (
                player_id,
                season,
                sample_adv,
                sprint_speed,
                eb_attempt,
                ftt_attempt,
                sth_attempt,
                fth_attempt,
                tu_attempt,
                stop,
                eb_success,
                ftt_success,
                sth_success,
                fth_success,
                tu_success,
                ftt_opps,
                sth_opps,
                fth_opps,
                tu_opps,
                below_min,
            ) = row

            def _v(vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            self._profiles[(player_id, season)] = BaserunnerProfile(
                player_id=player_id,
                season=season,
                sample_advancement_opps=sample_adv or 0,
                speed_vec=_v([sprint_speed]),
                aggression_vec=_v(
                    [eb_attempt, ftt_attempt, sth_attempt, fth_attempt, tu_attempt, stop]
                ),
                success_vec=_v([eb_success, ftt_success, sth_success, fth_success, tu_success]),
                sample_first_to_third_opps=ftt_opps or 0,
                sample_second_to_home_opps=sth_opps or 0,
                sample_first_to_home_opps=fth_opps or 0,
                sample_tag_up_opps=tu_opps or 0,
                eb_alpha=self._shrinkage.alpha(sample_adv or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        """Apply EB shrinkage to all feature vectors."""
        for _key, p in self._profiles.items():
            s = p.season
            for group, vec_attr in [
                ("speed", "speed_vec"),
                ("aggression", "aggression_vec"),
                ("success", "success_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is not None:
                    setattr(
                        p,
                        vec_attr,
                        self._shrinkage.shrink(
                            getattr(p, vec_attr),
                            avg,
                            p.sample_advancement_opps,
                        ),
                    )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        player_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """
        Score the query baserunner against ALL baserunner-season profiles.

        Parameters
        ----------
        player_id : int
            MLB player ID.
        season : int
            Season of the query runner's profile.
        n : int or None
            Top-N results (sorted by score desc). None = return all.
        """
        query_profile = self._profiles.get((player_id, season))
        if query_profile is None:
            log.warning(
                "Baserunner %d season %d not found. Ensure build() was called.",
                player_id,
                season,
            )
            return []

        results = self._partition.score_all(
            query=query_profile,
            normalizer=self._normalizer,
            speed_rbf=self._speed_rbf,
            agg_rbf=self._agg_rbf,
            success_rbf=self._success_rbf,
        )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        runner_a: tuple[int, int],
        runner_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """Compute similarity between two specific baserunner-seasons."""
        pa = self._profiles.get(runner_a)
        pb = self._profiles.get(runner_b)
        if pa is None or pb is None:
            return None

        scores = self._score_pair(pa, pb)
        return SimilarityResult(
            player_id=pb.player_id,
            season=pb.season,
            score=scores[0],
            speed_score=scores[1],
            aggression_score=scores[2],
            success_score=scores[3],
            sample_advancement_opps=pb.sample_advancement_opps,
        )

    def _score_pair(
        self,
        query: BaserunnerProfile,
        candidate: BaserunnerProfile,
    ) -> tuple[float, float, float, float]:
        """Compute sub-scores and composite between two profiles."""
        speed_q = self._normalizer.normalize_speed(query.speed_vec)
        speed_c = self._normalizer.normalize_speed(candidate.speed_vec)
        speed_s = self._speed_rbf.score(speed_q, speed_c)

        agg_q = self._normalizer.normalize_aggression(query.aggression_vec)
        agg_c = self._normalizer.normalize_aggression(candidate.aggression_vec)
        agg_s = self._agg_rbf.score(agg_q, agg_c)

        suc_q = self._normalizer.normalize_success(query.success_vec)
        suc_c = self._normalizer.normalize_success(candidate.success_vec)
        suc_s = self._success_rbf.score(suc_q, suc_c)

        composite = WEIGHT_SPEED * speed_s + WEIGHT_AGGRESSION * agg_s + WEIGHT_SUCCESS * suc_s

        confidence = min(query.eb_alpha, candidate.eb_alpha)
        composite *= np.sqrt(confidence)

        return (
            float(np.clip(composite, 0.0, 1.0)),
            float(speed_s),
            float(agg_s),
            float(suc_s),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, player_id: int, season: int) -> BaserunnerProfile | None:
        return self._profiles.get((player_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: BaserunnerSimilarityEngine,
    runner_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
    """Build a symmetric N×N similarity matrix."""
    n = len(runner_ids)
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(runner_ids[i], runner_ids[j])
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score
    return matrix


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = BaserunnerSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_baserunner_diagnostics(engine, n_query_samples=50)
    print(report)
