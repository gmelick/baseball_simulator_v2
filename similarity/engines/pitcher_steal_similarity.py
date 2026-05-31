"""
pitcher_steal_similarity.py
===========================
Step 2.7 — Pitcher Hold-Runner / Steal-Prevention Similarity
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
pitcher-season profiles specifically along the stolen-base prevention
dimension.  This engine is intentionally separate from the main pitcher
similarity engine (Step 2.1), which focuses on arsenal quality and command.

A pitcher's ability to hold runners is a meaningful independent skill: some
pitchers let elite base stealers succeed at will, while quick-twitch athletes
make stealing essentially impossible regardless of the catcher.  The simulation
needs this signal to correctly resolve stolen-base attempts.

SIM-408 — scope reduced to outcomes only
-----------------------------------------
  The engine was originally designed with three sub-scores: Delivery Speed
  (50%), Pickoff / Disengagement Tendency (30%), and Steal-Prevention Outcomes
  (20%).  The first two require data Statcast does not publish historically:
    * Delivery — set-position / stretch / first-to-home times, quick-pitch rate
      (biomech timings, never in the public feed).
    * Pickoff — pickoff-attempt / disengagement / slide-step rates (raw.pitches
      has NO pickoff or disengagement columns at all).
  Neither can be computed for derived.pitcher_steal_metrics, so both sub-scores
  were removed.  What remains — and IS computable from the play-by-play — is the
  steal-prevention OUTCOME profile: stolen bases allowed per 9 IP, caught-
  stealing rate when challenged, and how often runners even attempted against
  this pitcher.  The engine now scores similarity on that single dimension
  (weight 1.0); two pitchers are "similar" when runners fare similarly against
  them.

Research-Informed Design
------------------------
  Steal-against outcomes are noisy (conditional on attempts, which are
  conditional on opponent aggressiveness and catcher quality), so the EB prior
  is kept and stabilizes by ~80 baserunner events.

Dependencies
------------
  pip install duckdb numpy

Usage
-----
  engine = PitcherStealSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(pitcher_id=543037, season=2025)
  results = engine.query(pitcher_id=543037, season=2025, n=20)
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
log = logging.getLogger("pitcher_steal_similarity")


# ============================================================================
# Config — Feature Definitions & Weights
# ============================================================================

# --- Steal Prevention Outcome features ---
# SIM-408: the Delivery (biomech timing) and Pickoff/Disengagement sub-scores
# were removed — neither is derivable from historical Statcast (raw.pitches has
# no delivery-time or pickoff/disengagement columns). The outcome profile is the
# only steal-prevention signal computable from the play-by-play.
OUTCOME_FEATURES = [
    ("sb_against_per_9", 0.700),  # stolen bases allowed per 9 innings pitched
    ("cs_rate_forced", 0.500),  # CS rate when runners challenged this pitcher
    ("steal_attempt_rate_allowed", 0.600),  # how often runners even tried against this pitcher
]

# --- Sub-score weight ---
# SIM-408: outcome is now the sole sub-score (Delivery + Pickoff removed).
WEIGHT_OUTCOME = 1.0
_TOTAL = WEIGHT_OUTCOME
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameter
RBF_SIGMA_OUTCOME = 1.1000

# EB_N_PRIOR — holding runners stabilizes by ~80 baserunner events
EB_N_PRIOR = 25

# Minimum innings with runners on for inclusion
MIN_BASERUNNER_EVENTS = 30


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class PitcherStealProfile:
    """Pitcher-season profile for steal-prevention similarity scoring."""

    pitcher_id: int
    season: int
    throws: str  # "L" or "R"
    sample_baserunner_events: int  # PA with runner on base (denominator)
    sample_steal_attempts_against: int  # steal attempts against this pitcher

    # Feature vector (raw — normalized at query time)
    outcome_vec: NDArray[np.float64]  # shape (len(OUTCOME_FEATURES),)

    # Empirical Bayes
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    pitcher_id: int
    season: int
    throws: str
    score: float
    outcome_score: float
    sample_baserunner_events: int


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
    outcome_mean: NDArray | None = None
    outcome_std: NDArray | None = None

    def fit(self, profiles: list[PitcherStealProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs):
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.outcome_mean, self.outcome_std = _fit([p.outcome_vec for p in profiles])

    def _norm(self, v, m, s):
        if m is None:
            return v
        return np.nan_to_num((v - m) / s, nan=0.0)

    def normalize_outcome(self, v):
        return self._norm(v, self.outcome_mean, self.outcome_std)


# ============================================================================
# Scoring Partition
# ============================================================================


class PitcherStealPartition:
    def __init__(self) -> None:
        self.profiles: list[PitcherStealProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._outcome_mat: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(self, profiles: list[PitcherStealProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.pitcher_id, p.season) for p in profiles]
        if not profiles:
            return
        self._outcome_mat = np.array([norm.normalize_outcome(p.outcome_vec) for p in profiles])
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: PitcherStealProfile,
        norm: FeatureNormalizer,
        out_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        if not self.profiles:
            return []

        query_key = (query.pitcher_id, query.season)
        out_s = out_rbf.score_batch(norm.normalize_outcome(query.outcome_vec), self._outcome_mat)

        composite = WEIGHT_OUTCOME * out_s
        pair_conf = np.minimum(query.eb_alpha, self._eb_alphas)
        composite = np.clip(composite * np.sqrt(pair_conf), 0.0, 1.0)

        return [
            SimilarityResult(
                pitcher_id=self.profiles[i].pitcher_id,
                season=self.profiles[i].season,
                throws=self.profiles[i].throws,
                score=float(composite[i]),
                outcome_score=float(out_s[i]),
                sample_baserunner_events=self.profiles[i].sample_baserunner_events,
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


# ============================================================================
# Main Engine
# ============================================================================


class PitcherStealSimilarityEngine:
    """
    Pitcher Hold-Runner / Steal-Prevention Similarity Engine (Step 2.7).

    Usage:
        engine = PitcherStealSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])
        results = engine.query(pitcher_id=543037, season=2025)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], PitcherStealProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "outcome": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = PitcherStealPartition()

        self._out_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
        )

    def apply_calibration(self, report: CalibrationReport) -> None:
        """SIM-406: rebuild the outcome RBF scorer from a fitted report.

        A sigma field left at its 0.0 default keeps the engine's current value.
        """
        v = float(getattr(report, "sigma_pitcher_steal_outcome", 0.0) or 0.0)
        sigma = v if v > 0.0 else self._out_rbf.sigma
        self._out_rbf = WeightedRBFSimilarity(sigma, np.array([w for _, w in OUTCOME_FEATURES]))
        log.info(
            "SIM-406: applied calibration to PitcherStealSimilarityEngine (sigma_outcome=%.4f).",
            self._out_rbf.sigma,
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
            "PitcherStealSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            time.time() - t0,
        )

    def _load_league_averages(self, conn, seasons):
        try:
            sf = f"AND season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
            rows = conn.execute(f"""
                SELECT season, profile_json FROM derived.league_averages
                WHERE entity_type = 'pitcher_steal' {sf}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found.")
            return

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw
            self._league_avg["outcome"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in OUTCOME_FEATURES], dtype=np.float64
            )

    def _load_profiles(self, conn, seasons):
        sf = f"AND psm.season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
        rows = conn.execute(f"""
            SELECT
                psm.pitcher_id, psm.season, psm.throws,
                psm.sample_baserunner_events,
                psm.sample_steal_attempts_against,
                -- Outcome (3)
                psm.sb_against_per_9,
                psm.cs_rate_forced,
                psm.steal_attempt_rate_allowed,
                psm.below_minimum_sample
            FROM derived.pitcher_steal_metrics psm
            WHERE NOT psm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d pitcher-steal profiles from DuckDB …", len(rows))

        for row in rows:
            (
                pid,
                season,
                throws,
                n_br_events,
                n_steal_against,
                sb_per_9,
                cs_rate,
                steal_rate,
                below_min,
            ) = row

            def _v(*vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            self._profiles[(pid, season)] = PitcherStealProfile(
                pitcher_id=pid,
                season=season,
                throws=throws or "R",
                sample_baserunner_events=n_br_events or 0,
                sample_steal_attempts_against=n_steal_against or 0,
                outcome_vec=_v(sb_per_9, cs_rate, steal_rate),
                eb_alpha=self._shrinkage.alpha(n_br_events or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        for p in self._profiles.values():
            avg = self._league_avg["outcome"].get(p.season)
            if avg is not None:
                p.outcome_vec = self._shrinkage.shrink(
                    p.outcome_vec, avg, p.sample_baserunner_events
                )

    def query(
        self,
        pitcher_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        profile = self._profiles.get((pitcher_id, season))
        if profile is None:
            log.warning("Pitcher-steal %d season %d not found.", pitcher_id, season)
            return []

        results = self._partition.score_all(
            profile,
            self._normalizer,
            self._out_rbf,
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        pitcher_a: tuple[int, int],
        pitcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
        pa = self._profiles.get(pitcher_a)
        pb = self._profiles.get(pitcher_b)
        if pa is None or pb is None:
            return None

        norm = self._normalizer
        out_s = self._out_rbf.score(
            norm.normalize_outcome(pa.outcome_vec), norm.normalize_outcome(pb.outcome_vec)
        )

        composite = WEIGHT_OUTCOME * out_s
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            pitcher_id=pb.pitcher_id,
            season=pb.season,
            throws=pb.throws,
            score=composite,
            outcome_score=out_s,
            sample_baserunner_events=pb.sample_baserunner_events,
        )

    def get_profile(self, pitcher_id: int, season: int) -> PitcherStealProfile | None:
        return self._profiles.get((pitcher_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: PitcherStealSimilarityEngine,
    pitcher_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
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
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = PitcherStealSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_generic_diagnostics(
        engine,
        sub_score_names=["outcome_score"],
        n_query_samples=50,
        engine_name="PitcherSteal",
    )
    print(report)
