"""
manager_similarity.py
=====================
Step 2.8 — Manager-to-Manager Similarity
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
manager-season profiles.  Manager behavior is a meaningful independent
signal in game simulation: two identical teams simulated with different
managers will produce different run distributions, win probabilities,
and proposition bet outcomes because of differences in:

  - When starters are pulled (starter tolerance)
  - How the bullpen is structured and deployed (closer usage, leverage)
  - How aggressively the offense runs and bunts (small ball vs. three-true)
  - How platoon matchups are exploited (pinch-hit rate vs. handed pitchers)

This engine allows the simulation to find managers with similar historical
behavioral fingerprints and use their tendencies to parameterize manager
decision logic in the Phase 4 simulation loop.

Sub-score dimensions:

  1. Pitcher Usage (40%) — the single highest-impact managerial decision
     dimension for simulation accuracy.  Includes starter tolerance (pitch
     count threshold at which the manager pulls a starter), closer leverage
     entry point, bulk-inning reliever usage, and opener usage rate.

  2. Offensive Aggressiveness (35%) — how often the manager initiates
     running plays (steal rate, hit-and-run rate), uses sacrifice bunts in
     different situations, and employs squeeze plays.  Run environment
     awareness: whether the manager adjusts small-ball tendencies in
     high-leverage situations.

  3. Platoon / Matchup Management (25%) — pinch-hit rate vs. same-handed
     pitchers, defensive substitution frequency in late innings, and
     double-switch usage rate.  Managers vary significantly on whether they
     exploit platoon advantages or ride their lineup.

Research-Informed Design
------------------------
  Manager behavioral tendencies are relatively stable season-to-season
  after the first 100 games of a tenure.  They're noisier than player
  metrics because every decision is situationally dependent — the same
  manager will pull a starter in inning 5 when the team is up 8 runs and
  leave him in during a 2-0 gem.  For this reason, all features use
  rate-based metrics (decisions per opportunity) rather than raw counts,
  and only situations meeting minimum context requirements are included
  (e.g. only high-leverage plate appearances for pinch-hit rate).

  EB_N_PRIOR = 30: managers require a larger sample to stabilize because
  their decisions are opportunity-gated.  New managers in their first
  half-season receive significant shrinkage toward the league average.

Dependencies
------------
  pip install duckdb numpy

Usage
-----
  engine = ManagerSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(manager_id=4061, season=2025)
  results = engine.query(manager_id=4061, season=2025, n=20)
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
log = logging.getLogger("manager_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Pitcher Usage features ---
# Rate-based metrics; all normalized per 9 innings or per opportunity.
USAGE_FEATURES = [
    # feature_name,                             reliability_weight
    ("starter_avg_pitch_count", 0.800),  # avg pitches before first hook; stable
    ("starter_pull_pct_before_100", 0.700),  # % starts ended before 100 pitches
    ("closer_entry_leverage_index", 0.600),  # avg LI when closer enters; role usage
    ("high_leverage_reliever_rate", 0.600),  # % high-LI PA handled by top 2 relievers
    ("opener_usage_rate", 0.500),  # openers used / total starts
    ("bulk_innings_rate", 0.500),  # games with bulk-inning reliever after opener
    # SIM-427 capstone: of the relievers the manager had AVAILABLE, the fraction
    # actually used per game (opportunity-normalized; from the SIM-433-v2 full-roster
    # availability table). An orthogonal "bullpen-depletion" axis — how aggressively a
    # manager empties the pen relative to what's available, separating "chose to hold
    # an arm" from "no arm could pitch". Weight 0.55: just below the 0.60 bullpen-
    # reliance features because its availability source (rest/IL inference) is modestly
    # noisier than the pure pitch-count anchors. 100% manager-season coverage on the
    # 2017-2026 ingest (no NULL-coercion bias in practice).
    ("available_reliever_usage_rate", 0.550),
]

# --- Offensive Aggressiveness features ---
AGGRESSION_FEATURES = [
    ("steal_order_rate_per_1b_opp", 0.700),  # steal signs given / 1B opportunity
    ("hit_and_run_rate_per_opportunity", 0.600),  # H&R per 1B+runner opportunity
    ("sac_bunt_rate_high_leverage", 0.650),  # bunts in high-LI situations (> 1.5 LI)
    ("sac_bunt_rate_low_leverage", 0.500),  # bunts in low-LI (small-ball vs. not)
    ("squeeze_play_rate_per_3b_opp", 0.400),  # squeeze attempts / runner-on-3rd-<2-out
]

# --- Platoon / Matchup Management features ---
PLATOON_FEATURES = [
    ("pinch_hit_rate_vs_same_hand", 0.700),  # PH rate when platoon disadvantage exists
    ("pinch_hit_rate_high_leverage", 0.650),  # PH rate in high-LI at-bats
    ("defensive_sub_rate_late_innings", 0.550),  # defensive replacement rate in 7th+ innings
    ("double_switch_rate_per_reliever_change", 0.600),  # double switches / reliever change event
    ("platoon_advantage_exploitation_rate", 0.700),  # matchup changes made when adv. available
]

# --- Sub-score weights (sum to 1.0) ---
WEIGHT_USAGE = 0.40
WEIGHT_AGGRESSION = 0.35
WEIGHT_PLATOON = 0.25
_TOTAL = WEIGHT_USAGE + WEIGHT_AGGRESSION + WEIGHT_PLATOON
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters
RBF_SIGMA_USAGE = 1.0000
RBF_SIGMA_AGGRESSION = 1.0500
RBF_SIGMA_PLATOON = 1.0200

# EB_N_PRIOR = 30: manager decisions stabilize more slowly than player stats
EB_N_PRIOR = 30

# Minimum games managed for inclusion
MIN_GAMES_MANAGED = 50


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class ManagerProfile:
    """Manager-season profile for similarity scoring."""

    manager_id: int
    season: int
    sample_games: int
    sample_starter_decisions: int  # number of starts with pull decision recorded

    # Feature vectors (raw — normalized at query time)
    usage_vec: NDArray[np.float64]  # shape (len(USAGE_FEATURES),)
    aggression_vec: NDArray[np.float64]  # shape (len(AGGRESSION_FEATURES),)
    platoon_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    manager_id: int
    season: int
    score: float
    usage_score: float
    aggression_score: float
    platoon_score: float
    sample_games: int


# ============================================================================
# WeightedRBFSimilarity, EmpiricalBayesShrinkage, FeatureNormalizer
# ============================================================================


class WeightedRBFSimilarity:
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
        return np.exp(-self.gamma * np.sum(self.weights[np.newaxis, :] * diff**2, axis=1))


class EmpiricalBayesShrinkage:
    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n: int) -> float:
        return n / (n + self.n_prior)

    def shrink(self, raw: NDArray, avg: NDArray, n: int) -> NDArray:
        a = self.alpha(n)
        return a * np.where(np.isnan(raw), avg, raw) + (1.0 - a) * avg


@dataclass(slots=True)
class FeatureNormalizer:
    usage_mean: NDArray | None = None
    usage_std: NDArray | None = None
    aggression_mean: NDArray | None = None
    aggression_std: NDArray | None = None
    platoon_mean: NDArray | None = None
    platoon_std: NDArray | None = None

    def fit(self, profiles: list[ManagerProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs):
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.usage_mean, self.usage_std = _fit([p.usage_vec for p in profiles])
        self.aggression_mean, self.aggression_std = _fit([p.aggression_vec for p in profiles])
        self.platoon_mean, self.platoon_std = _fit([p.platoon_vec for p in profiles])

    def _norm(self, v, m, s):
        if m is None:
            return v
        return np.nan_to_num((v - m) / s, nan=0.0)

    def normalize_usage(self, v):
        return self._norm(v, self.usage_mean, self.usage_std)

    def normalize_aggression(self, v):
        return self._norm(v, self.aggression_mean, self.aggression_std)

    def normalize_platoon(self, v):
        return self._norm(v, self.platoon_mean, self.platoon_std)


# ============================================================================
# Scoring Partition
# ============================================================================


class ManagerPartition:
    def __init__(self) -> None:
        self.profiles: list[ManagerProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._usage_mat: NDArray | None = None
        self._aggression_mat: NDArray | None = None
        self._platoon_mat: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(self, profiles: list[ManagerProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.manager_id, p.season) for p in profiles]
        if not profiles:
            return
        self._usage_mat = np.array([norm.normalize_usage(p.usage_vec) for p in profiles])
        self._aggression_mat = np.array(
            [norm.normalize_aggression(p.aggression_vec) for p in profiles]
        )
        self._platoon_mat = np.array([norm.normalize_platoon(p.platoon_vec) for p in profiles])
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: ManagerProfile,
        norm: FeatureNormalizer,
        usage_rbf: WeightedRBFSimilarity,
        agg_rbf: WeightedRBFSimilarity,
        plat_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        if not self.profiles:
            return []

        query_key = (query.manager_id, query.season)
        u_s = usage_rbf.score_batch(norm.normalize_usage(query.usage_vec), self._usage_mat)
        a_s = agg_rbf.score_batch(
            norm.normalize_aggression(query.aggression_vec), self._aggression_mat
        )
        p_s = plat_rbf.score_batch(norm.normalize_platoon(query.platoon_vec), self._platoon_mat)

        composite = WEIGHT_USAGE * u_s + WEIGHT_AGGRESSION * a_s + WEIGHT_PLATOON * p_s
        pair_conf = np.minimum(query.eb_alpha, self._eb_alphas)
        composite = np.clip(composite * np.sqrt(pair_conf), 0.0, 1.0)

        return [
            SimilarityResult(
                manager_id=self.profiles[i].manager_id,
                season=self.profiles[i].season,
                score=float(composite[i]),
                usage_score=float(u_s[i]),
                aggression_score=float(a_s[i]),
                platoon_score=float(p_s[i]),
                sample_games=self.profiles[i].sample_games,
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


# ============================================================================
# Main Engine
# ============================================================================


class ManagerSimilarityEngine:
    """
    Manager-to-Manager Similarity Engine (Step 2.8).

    Scores computed exhaustively across all manager-seasons with
    sufficient sample size.  Cross-season comparisons are included
    (the same manager in 2022 vs. 2024 may differ after personnel changes).

    Usage:
        engine = ManagerSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])
        results = engine.query(manager_id=4061, season=2025)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], ManagerProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "usage": {},
            "aggression": {},
            "platoon": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = ManagerPartition()

        self._usage_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_USAGE, np.array([w for _, w in USAGE_FEATURES])
        )
        self._agg_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_AGGRESSION, np.array([w for _, w in AGGRESSION_FEATURES])
        )
        self._plat_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_PLATOON, np.array([w for _, w in PLATOON_FEATURES])
        )

    def apply_calibration(self, report: CalibrationReport) -> None:
        """SIM-406: rebuild the usage / aggression / platoon RBF scorers from a report.

        A sigma field left at its 0.0 default keeps the engine's current value, so a
        partial report degrades gracefully (e.g. a DuckDB whose USAGE columns have not
        been repopulated yields a degenerate, no-variance fit → the 0.0 keep-default
        sentinel → the module-default RBF_SIGMA_USAGE).
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        self._usage_rbf = WeightedRBFSimilarity(
            _sig("sigma_manager_usage", self._usage_rbf.sigma),
            np.array([w for _, w in USAGE_FEATURES]),
        )
        self._agg_rbf = WeightedRBFSimilarity(
            _sig("sigma_manager_aggression", self._agg_rbf.sigma),
            np.array([w for _, w in AGGRESSION_FEATURES]),
        )
        self._plat_rbf = WeightedRBFSimilarity(
            _sig("sigma_manager_platoon", self._plat_rbf.sigma),
            np.array([w for _, w in PLATOON_FEATURES]),
        )
        log.info(
            "SIM-406: applied calibration to ManagerSimilarityEngine "
            "(sigma_usage=%.4f, sigma_aggression=%.4f, sigma_platoon=%.4f).",
            self._usage_rbf.sigma,
            self._agg_rbf.sigma,
            self._plat_rbf.sigma,
        )

    def build(self, seasons: list[int] | None = None) -> None:
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
            "ManagerSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            time.time() - t0,
        )

    def _load_league_averages(self, conn, seasons):
        try:
            sf = f"AND season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
            rows = conn.execute(f"""
                SELECT season, profile_json FROM derived.league_averages
                WHERE entity_type = 'manager' {sf}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found.")
            return

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw
            for group, features in [
                ("usage", USAGE_FEATURES),
                ("aggression", AGGRESSION_FEATURES),
                ("platoon", PLATOON_FEATURES),
            ]:
                self._league_avg[group][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in features], dtype=np.float64
                )

    def _load_profiles(self, conn, seasons):
        sf = f"AND msm.season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
        rows = conn.execute(f"""
            SELECT
                msm.manager_id, msm.season,
                msm.sample_games, msm.sample_starter_decisions,
                -- Usage (7)
                msm.starter_avg_pitch_count,
                msm.starter_pull_pct_before_100,
                msm.closer_entry_leverage_index,
                msm.high_leverage_reliever_rate,
                msm.opener_usage_rate,
                msm.bulk_innings_rate,
                msm.available_reliever_usage_rate,
                -- Aggression (5)
                msm.steal_order_rate_per_1b_opp,
                msm.hit_and_run_rate_per_opportunity,
                msm.sac_bunt_rate_high_leverage,
                msm.sac_bunt_rate_low_leverage,
                msm.squeeze_play_rate_per_3b_opp,
                -- Platoon (5)
                msm.pinch_hit_rate_vs_same_hand,
                msm.pinch_hit_rate_high_leverage,
                msm.defensive_sub_rate_late_innings,
                msm.double_switch_rate_per_reliever_change,
                msm.platoon_advantage_exploitation_rate,
                msm.below_minimum_sample
            FROM derived.manager_season_metrics msm
            WHERE NOT msm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d manager profiles from DuckDB …", len(rows))

        for row in rows:
            (
                mid,
                season,
                n_games,
                n_start_dec,
                avg_pc,
                pull_b100,
                closer_li,
                hl_rate,
                opener_rate,
                bulk_rate,
                avail_rel_usage,
                steal_rate,
                hnr_rate,
                bunt_hl,
                bunt_ll,
                squeeze_rate,
                ph_sh,
                ph_hl,
                def_sub,
                dbl_sw,
                plat_exploit,
                below_min,
            ) = row

            def _v(*vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            self._profiles[(mid, season)] = ManagerProfile(
                manager_id=mid,
                season=season,
                sample_games=n_games or 0,
                sample_starter_decisions=n_start_dec or 0,
                usage_vec=_v(
                    avg_pc, pull_b100, closer_li, hl_rate, opener_rate, bulk_rate, avail_rel_usage
                ),
                aggression_vec=_v(steal_rate, hnr_rate, bunt_hl, bunt_ll, squeeze_rate),
                platoon_vec=_v(ph_sh, ph_hl, def_sub, dbl_sw, plat_exploit),
                eb_alpha=self._shrinkage.alpha(n_games or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        for p in self._profiles.values():
            s = p.season
            for group, attr in [
                ("usage", "usage_vec"),
                ("aggression", "aggression_vec"),
                ("platoon", "platoon_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is not None:
                    setattr(
                        p,
                        attr,
                        self._shrinkage.shrink(
                            getattr(p, attr),
                            avg,
                            p.sample_games,
                        ),
                    )

    def query(
        self,
        manager_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        profile = self._profiles.get((manager_id, season))
        if profile is None:
            log.warning("Manager %d season %d not found.", manager_id, season)
            return []

        results = self._partition.score_all(
            profile,
            self._normalizer,
            self._usage_rbf,
            self._agg_rbf,
            self._plat_rbf,
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        mgr_a: tuple[int, int],
        mgr_b: tuple[int, int],
    ) -> SimilarityResult | None:
        pa = self._profiles.get(mgr_a)
        pb = self._profiles.get(mgr_b)
        if pa is None or pb is None:
            return None

        norm = self._normalizer
        u = self._usage_rbf.score(
            norm.normalize_usage(pa.usage_vec), norm.normalize_usage(pb.usage_vec)
        )
        a = self._agg_rbf.score(
            norm.normalize_aggression(pa.aggression_vec),
            norm.normalize_aggression(pb.aggression_vec),
        )
        p = self._plat_rbf.score(
            norm.normalize_platoon(pa.platoon_vec), norm.normalize_platoon(pb.platoon_vec)
        )

        composite = WEIGHT_USAGE * u + WEIGHT_AGGRESSION * a + WEIGHT_PLATOON * p
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            manager_id=pb.manager_id,
            season=pb.season,
            score=composite,
            usage_score=u,
            aggression_score=a,
            platoon_score=p,
            sample_games=pb.sample_games,
        )

    def get_profile(self, manager_id: int, season: int) -> ManagerProfile | None:
        return self._profiles.get((manager_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: ManagerSimilarityEngine,
    manager_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
    n = len(manager_ids)
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(manager_ids[i], manager_ids[j])
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score
    return matrix


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = ManagerSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_generic_diagnostics(
        engine,
        sub_score_names=["usage_score", "aggression_score", "platoon_score"],
        n_query_samples=50,
        engine_name="Manager",
    )
    print(report)
