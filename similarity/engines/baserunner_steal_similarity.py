"""
baserunner_steal_similarity.py
==============================
Step 2.5 — Baserunner-to-Baserunner Similarity (Stolen Base)
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
baserunner-season profiles specifically along the stolen-base dimension.
This engine is intentionally separate from the extra-base engine (Step 2.4)
because the skill sets involved are meaningfully different:

  * Extra-base advancement is primarily about reading fly balls and line
    drives in real time, then making go/no-go decisions mid-play.
  * Stolen base attempts are pre-meditated: the runner reads pitcher
    tendencies before the pitch, takes a lead, and commits on first
    movement.  The dominant skills here are jump quality and lead size,
    not just raw sprint speed.

Sub-score dimensions (SIM-408: the original Jump / First-Step sub-score was
removed — reaction time, burst distance, and break angle are biomech signals
Statcast does not publish per runner-season, so they cannot be computed; its
35% weight was renormalized into the two surviving sub-scores):

  1. Steal Tendency (~62%) — how often does the runner attempt to steal?
     Measured as attempt rate per first-base opportunity, plus the 2B→3B
     attempt rate.

  2. Success Efficiency (~38%) — given the runner attempted, how often did
     he arrive safe?  Noisy because it's conditional on the attempt AND on
     pitcher/catcher matchup quality, but necessary to distinguish efficient
     stealers from volume traders.

Research-Informed Design
------------------------
  Attempt rate stabilizes by ~50 first-base opportunities.  Success rate is
  the noisiest — it requires conditional sample sizes (attempts, not
  opportunities) and is heavily influenced by catcher pop time in any given
  season.  Hence the weight ordering: tendency > success.

Dependencies
------------
  pip install duckdb numpy scipy

Usage
-----
  engine = BaserunnerStealSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])

  results = engine.query(player_id=660271, season=2025)
  results = engine.query(player_id=660271, season=2025, n=20)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import duckdb
import numpy as np
from numpy.typing import NDArray

from similarity.similarity_diagnostics import run_generic_diagnostics

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
log = logging.getLogger("baserunner_steal_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Steal Tendency features ---
# How frequently and aggressively the runner initiates steal attempts.
# SIM-408: lead_distance_tendency + disengagement_response_rate were dropped —
# neither is derivable from historical Statcast (lead distance / disengagement-
# response are tracking/biomech signals the feed does not publish per
# runner-season), so derived.baserunner_steal_metrics can't supply them.
TENDENCY_FEATURES = [
    # feature_name,                 reliability_weight
    ("steal_attempt_rate", 0.800),  # attempts / first_base_opps; stabilizes ~50 opps
    ("steal_attempt_rate_2b", 0.500),  # 2B→3B steal attempt rate; sparser
]

# --- Success / Efficiency features ---
# Given the runner attempted, how often did he arrive safe?
SUCCESS_FEATURES = [
    ("steal_success_rate", 0.700),  # overall success rate
    ("steal_success_rate_2b", 0.400),  # 2B→3B success; sparser, noisier
]

# --- Sub-score weights (sum to 1.0) ---
# SIM-408: the Jump / First-Step sub-score (reaction_time_ms / burst_distance_ft
# / break_angle_deg) was removed — those are biomech features Statcast does not
# publish, so they cannot be computed.  The original 0.40 / 0.35 / 0.25 split is
# renormalized over the two surviving sub-scores, preserving their 0.40 : 0.25
# relative emphasis (tendency > success).
WEIGHT_TENDENCY = 0.40 / 0.65  # ≈ 0.6154
WEIGHT_SUCCESS = 0.25 / 0.65  # ≈ 0.3846
_TOTAL = WEIGHT_TENDENCY + WEIGHT_SUCCESS
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters (gamma = 1 / (2σ²))
# Calibrated to keep median similarity score near 0.50 across the population.
RBF_SIGMA_TENDENCY = 1.0500
RBF_SIGMA_SUCCESS = 1.0200

# Empirical Bayes shrinkage: at EB_N_PRIOR steal attempts, α = 0.5
EB_N_PRIOR = 20

# Minimum steal attempts for inclusion in the index
MIN_STEAL_ATTEMPTS = 10


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class BaserunnerStealProfile:
    """Complete baserunner-season profile for stolen-base similarity scoring."""

    player_id: int
    season: int
    sample_steal_attempts: int  # total steal attempts (1B→2B + 2B→3B)
    sample_first_base_opps: int  # first-base opportunities (denominator for tendency)

    # Feature vectors (raw — normalized at query time)
    tendency_vec: NDArray[np.float64]  # shape (len(TENDENCY_FEATURES),)
    success_vec: NDArray[np.float64]  # shape (len(SUCCESS_FEATURES),)

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    player_id: int
    season: int
    score: float  # composite [0, 1], 1 = identical
    tendency_score: float
    success_score: float
    sample_steal_attempts: int


# ============================================================================
# Reliability-Weighted RBF Kernel
# ============================================================================


class WeightedRBFSimilarity:
    """
    Gaussian RBF kernel with per-feature reliability weights.

    K(x, y) = exp(-γ * Σ_i w_i * (x_i - y_i)²)

    Weights normalized to sum to 1.0 for dimensionality invariance.
    """

    def __init__(self, sigma: float, reliability_weights: NDArray[np.float64]) -> None:
        self.sigma = sigma
        self.gamma = 1.0 / (2.0 * sigma**2)
        total = reliability_weights.sum()
        self.weights = (
            reliability_weights / total
            if total > 0
            else (np.ones_like(reliability_weights) / len(reliability_weights))
        )

    def score(self, x: NDArray, y: NDArray) -> float:
        diff = np.nan_to_num(x - y, nan=0.0)
        return float(np.exp(-self.gamma * np.dot(self.weights * diff, diff)))

    def score_batch(self, query: NDArray, candidates: NDArray) -> NDArray[np.float64]:
        diff = np.nan_to_num(candidates - query[np.newaxis, :], nan=0.0)
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
    tendency_mean: NDArray | None = None
    tendency_std: NDArray | None = None
    success_mean: NDArray | None = None
    success_std: NDArray | None = None

    def fit(self, profiles: list[BaserunnerStealProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs: list[NDArray]) -> tuple[NDArray, NDArray]:
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.tendency_mean, self.tendency_std = _fit([p.tendency_vec for p in profiles])
        self.success_mean, self.success_std = _fit([p.success_vec for p in profiles])

    def _norm(self, vec: NDArray, mean: NDArray | None, std: NDArray | None) -> NDArray:
        if mean is None:
            return vec
        return np.nan_to_num((vec - mean) / std, nan=0.0)

    def normalize_tendency(self, v: NDArray) -> NDArray:
        return self._norm(v, self.tendency_mean, self.tendency_std)

    def normalize_success(self, v: NDArray) -> NDArray:
        return self._norm(v, self.success_mean, self.success_std)


# ============================================================================
# Scoring Partition
# ============================================================================


class StealPartition:
    """Vectorized batch scoring across all steal profiles."""

    def __init__(self) -> None:
        self.profiles: list[BaserunnerStealProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._tendency_mat: NDArray | None = None
        self._success_mat: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(self, profiles: list[BaserunnerStealProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.player_id, p.season) for p in profiles]
        if not profiles:
            return
        self._tendency_mat = np.array([norm.normalize_tendency(p.tendency_vec) for p in profiles])
        self._success_mat = np.array([norm.normalize_success(p.success_vec) for p in profiles])
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: BaserunnerStealProfile,
        norm: FeatureNormalizer,
        tend_rbf: WeightedRBFSimilarity,
        succ_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        if not self.profiles:
            return []

        query_key = (query.player_id, query.season)
        q_tend = norm.normalize_tendency(query.tendency_vec)
        q_succ = norm.normalize_success(query.success_vec)

        tend_scores = tend_rbf.score_batch(q_tend, self._tendency_mat)
        succ_scores = succ_rbf.score_batch(q_succ, self._success_mat)

        composite = WEIGHT_TENDENCY * tend_scores + WEIGHT_SUCCESS * succ_scores

        pair_conf = np.minimum(query.eb_alpha, self._eb_alphas)
        composite = np.clip(composite * np.sqrt(pair_conf), 0.0, 1.0)

        return [
            SimilarityResult(
                player_id=self.profiles[i].player_id,
                season=self.profiles[i].season,
                score=float(composite[i]),
                tendency_score=float(tend_scores[i]),
                success_score=float(succ_scores[i]),
                sample_steal_attempts=self.profiles[i].sample_steal_attempts,
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


# ============================================================================
# Main Engine
# ============================================================================


class BaserunnerStealSimilarityEngine:
    """
    Baserunner Stolen-Base Similarity Engine (Step 2.5).

    Scores computed exhaustively against all baserunner-season profiles
    with sufficient steal attempt sample size.

    Usage:
        engine = BaserunnerStealSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])
        results = engine.query(player_id=660271, season=2025)
        results = engine.query(player_id=660271, season=2025, n=20)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], BaserunnerStealProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "tendency": {},
            "success": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = StealPartition()

        self._tend_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_TENDENCY,
            np.array([w for _, w in TENDENCY_FEATURES]),
        )
        self._succ_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SUCCESS,
            np.array([w for _, w in SUCCESS_FEATURES]),
        )

    # ------------------------------------------------------------------
    # Calibration wiring (SIM-406)
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """SIM-406: rebuild the tendency + success RBF scorers from a fitted report.

        A sigma field left at its 0.0 default keeps the engine's current value, so
        a partial report degrades gracefully.
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        self._tend_rbf = WeightedRBFSimilarity(
            _sig("sigma_baserunner_steal_tendency", self._tend_rbf.sigma),
            np.array([w for _, w in TENDENCY_FEATURES]),
        )
        self._succ_rbf = WeightedRBFSimilarity(
            _sig("sigma_baserunner_steal_success", self._succ_rbf.sigma),
            np.array([w for _, w in SUCCESS_FEATURES]),
        )
        log.info(
            "SIM-406: applied calibration to BaserunnerStealSimilarityEngine "
            "(sigma_tendency=%.4f, sigma_success=%.4f).",
            self._tend_rbf.sigma,
            self._succ_rbf.sigma,
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load all steal profiles, apply shrinkage, build scoring matrices."""
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

        log.info(
            "BaserunnerStealSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            time.time() - t0,
        )

    def _load_league_averages(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        try:
            sf = ""
            if seasons:
                sf = f"AND season IN ({', '.join(str(s) for s in seasons)})"
            rows = conn.execute(f"""
                SELECT season, profile_json
                FROM derived.league_averages
                WHERE entity_type = 'baserunner_steal'
                  {sf}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — skipping shrinkage fallbacks.")
            return

        import json

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw
            self._league_avg["tendency"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in TENDENCY_FEATURES], dtype=np.float64
            )
            self._league_avg["success"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in SUCCESS_FEATURES], dtype=np.float64
            )
        log.info("Loaded steal league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> None:
        sf = ""
        if seasons:
            sf = f"AND bss.season IN ({', '.join(str(s) for s in seasons)})"

        rows = conn.execute(f"""
            SELECT
                bss.player_id,
                bss.season,
                bss.sample_steal_attempts,
                bss.sample_first_base_opps,
                -- Tendency
                bss.steal_attempt_rate,
                bss.steal_attempt_rate_2b,
                -- Success
                bss.steal_success_rate,
                bss.steal_success_rate_2b,
                bss.below_minimum_sample
            FROM derived.baserunner_steal_metrics bss
            WHERE NOT bss.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d steal profiles from DuckDB …", len(rows))

        for row in rows:
            (
                pid,
                season,
                n_attempts,
                n_opps,
                sar,
                sar2,
                suc,
                suc2,
                below_min,
            ) = row

            def _v(*vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            self._profiles[(pid, season)] = BaserunnerStealProfile(
                player_id=pid,
                season=season,
                sample_steal_attempts=n_attempts or 0,
                sample_first_base_opps=n_opps or 0,
                tendency_vec=_v(sar, sar2),
                success_vec=_v(suc, suc2),
                eb_alpha=self._shrinkage.alpha(n_attempts or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        for p in self._profiles.values():
            s = p.season
            for group, attr in [
                ("tendency", "tendency_vec"),
                ("success", "success_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is not None:
                    setattr(
                        p,
                        attr,
                        self._shrinkage.shrink(
                            getattr(p, attr),
                            avg,
                            p.sample_steal_attempts,
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
        Score the query runner against ALL steal profiles.

        Parameters
        ----------
        player_id : int   MLB player ID.
        season : int      Season of the query runner's profile.
        n : int or None   Top-N results. None = return all.
        """
        profile = self._profiles.get((player_id, season))
        if profile is None:
            log.warning(
                "Steal runner %d season %d not found. Ensure build() was called.",
                player_id,
                season,
            )
            return []

        results = self._partition.score_all(
            profile,
            self._normalizer,
            self._tend_rbf,
            self._succ_rbf,
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        runner_a: tuple[int, int],
        runner_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """Compute similarity between two specific baserunner-steal-seasons."""
        pa = self._profiles.get(runner_a)
        pb = self._profiles.get(runner_b)
        if pa is None or pb is None:
            return None

        norm = self._normalizer
        tend_s = self._tend_rbf.score(
            norm.normalize_tendency(pa.tendency_vec),
            norm.normalize_tendency(pb.tendency_vec),
        )
        succ_s = self._succ_rbf.score(
            norm.normalize_success(pa.success_vec),
            norm.normalize_success(pb.success_vec),
        )
        composite = WEIGHT_TENDENCY * tend_s + WEIGHT_SUCCESS * succ_s
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            player_id=pb.player_id,
            season=pb.season,
            score=composite,
            tendency_score=tend_s,
            success_score=succ_s,
            sample_steal_attempts=pb.sample_steal_attempts,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, player_id: int, season: int) -> BaserunnerStealProfile | None:
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
    engine: BaserunnerStealSimilarityEngine,
    runner_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
    """Build a symmetric N×N similarity matrix for the given runner-seasons."""
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
    engine = BaserunnerStealSimilarityEngine(
        duckdb_path="../../db/schemas/baseball_simulator.duckdb"
    )
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_generic_diagnostics(
        engine,
        sub_score_names=["tendency_score", "success_score"],
        n_query_samples=50,
        engine_name="BaserunnerSteal",
    )
    print(report)
