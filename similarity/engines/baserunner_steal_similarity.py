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

Sub-score dimensions (SIM-531, owner decision 2026-09-16; the weights are
set from how well each trait repeats year to year on the qualified rows —
plan §6.1 of docs/audit/2026-09-16-sim531-lead-distance-build-plan.md):

  1. Steal Tendency (45%) — how often does the runner attempt to steal?
     Measured as attempt rate per first-base opportunity, plus the 2B→3B
     attempt rate. Repeats at 0.78.

  2. Lead (45%) — the runner's lead off the bag in feet before the pitch
     (repeats at 0.73-0.74) and the extra distance he gets on the pitcher's
     delivery, the "jump" (0.80-0.85). Both come from Baseball Savant's
     Basestealing Run Value board; they are close to independent of each
     other (r = 0.02) and of the attempt rate. This group fills the hole the
     removed Jump / First-Step sub-score (SIM-408) left: reaction time, burst
     and break angle were never published, but the lead and the jump are.

  3. Success Efficiency (10%) — given the runner attempted, how often did
     he arrive safe?  Repeats at only 0.13-0.26: noise carrying a large
     weight until 2026-09-16, when it carried 38%.

The missing-value rule
----------------------
  A runner with no Savant row (a season before 2023, or a runner the board
  never listed) has no lead. The pair is then scored over the groups BOTH
  sides have — tendency and success, renormalized — never against a lead of
  0.0 ft, which would sit twelve standard deviations below the league. A NULL
  loads as NaN, and the normalizer treats NaN as neutral.

The confidence basis
--------------------
  Every runner who HAD a chance carries a profile (SIM-531 widened the
  builder's driver), including the ones who never went. The confidence that
  multiplies the whole score is therefore ``chances / (chances + 50)`` over
  his CHANCES — plate appearances begun on first plus those begun on second,
  and never below his attempts (an attempt is a chance by definition). An
  attempt-based confidence would score a zero-attempt runner 0.0 against
  every row; a first-base-only basis would do the same to a runner whose
  chances were all on second (the extra-innings automatic runner) or whose
  only attempts were of third or home. Either way the steal draw would then
  have no weight at all — the trap the review of 2026-09-16 caught. The
  tendency and the lead shrink toward the league mean on the same basis (the
  rate's own denominator); the success rate shrinks on attempts, its own
  denominator. A runner with no measured lead is not shrunk on the lead at
  all: his NaN stays NaN, so the normalizer's statistics come from the
  measured runners alone and the fitted bandwidth applies as fitted.

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
from dataclasses import dataclass, field
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

# --- Lead features (SIM-531) ---
# The runner's lead off the bag in feet, from Savant's Basestealing Run Value
# board (derived.baserunner_steal_metrics.lead_primary_ft / lead_jump_ft). The
# weight is the trait's year-to-year repeat on the qualified rows (plan §2.2).
LEAD_FEATURES = [
    ("lead_primary_ft", 0.735),  # the lead before the pitch
    ("lead_jump_ft", 0.823),  # the extra distance on the pitcher's delivery
]

# --- Success / Efficiency features ---
# Given the runner attempted, how often did he arrive safe?
SUCCESS_FEATURES = [
    ("steal_success_rate", 0.700),  # overall success rate
    ("steal_success_rate_2b", 0.400),  # 2B→3B success; sparser, noisier
]

# --- Sub-score weights (sum to 1.0) ---
# SIM-531 (owner decision 2026-09-16): the measured split. Each weight is the
# group's share of the summed year-to-year repeats on the qualified rows —
# tendency 0.78, lead 0.78 (primary 0.735, jump 0.823), success 0.19. The
# 2026-05 renormalization (0.6154 / 0.3846, after SIM-408 removed the
# never-computable Jump sub-score) is superseded; the alternative not taken was
# the original 0.40 / 0.35 / 0.25.
WEIGHT_TENDENCY = 0.45
WEIGHT_LEAD = 0.45
WEIGHT_SUCCESS = 0.10
_TOTAL = WEIGHT_TENDENCY + WEIGHT_LEAD + WEIGHT_SUCCESS
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters (gamma = 1 / (2σ²))
# Calibrated to keep median similarity score near 0.50 across the population.
RBF_SIGMA_TENDENCY = 1.0500
RBF_SIGMA_SUCCESS = 1.0200
# SIM-531: sigma_baserunner_steal_lead as ``make calibrate`` fitted it on
# 2026-09-16 over the 481 qualified runner-seasons with a measured lead
# (2023-2026). Copied here because the matrix builder does not read the
# calibration report (plan §3, Finding 4); refit = copy the new value.
RBF_SIGMA_LEAD = 0.9836

# Empirical Bayes shrinkage: at EB_N_PRIOR steal attempts, α = 0.5 — the
# success group's basis (its denominator is attempts).
EB_N_PRIOR = 20
# SIM-531: the confidence basis, and the tendency and lead groups' shrinkage
# basis — the runner's chances (first-base plus second-base plate-appearance
# starts, never below his attempts; the attempt rate stabilizes by ~50 of
# them). At 5 chances a runner's lead is 91% league mean; at 300 it is 86%
# his own.
EB_N_PRIOR_OPPS = 50

# Minimum steal attempts for inclusion in the index
MIN_STEAL_ATTEMPTS = 10


# ============================================================================
# Data Structures
# ============================================================================


def _no_lead() -> NDArray[np.float64]:
    """The lead vector of a runner with no Savant row: NaN, never 0.0."""
    return np.full(len(LEAD_FEATURES), np.nan, dtype=np.float64)


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
    # SIM-531: the lead group; NaN when the runner has no Savant row.
    lead_vec: NDArray[np.float64] = field(default_factory=_no_lead)
    # SIM-531: whether the lead was MEASURED (both features finite at load).
    # A runner without one is scored over the groups he has, and his NaN lead
    # is never shrunk (it would otherwise enter the normalizer's statistics).
    has_lead: bool = False
    # SIM-531: plate appearances begun on second — with the first-base count
    # and the attempts, the runner's CHANCES (the confidence basis).
    sample_second_base_opps: int = 0

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
    # SIM-531: the lead kernel between the pair; None when either side has no
    # measured lead (the pair was scored over tendency and success alone).
    lead_score: float | None = None


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
        """Pull ``raw_vec`` toward the league mean by ``alpha(n_samples)``.

        A NaN raw value becomes the league mean. SIM-531: a NaN LEAGUE value
        (a key the league row lacks) leaves the raw value alone — a feature
        can be shrunk only toward a mean that exists.
        """
        a = self.alpha(n_samples)
        avg = np.where(np.isnan(league_avg_vec), raw_vec, league_avg_vec)
        raw_clean = np.where(np.isnan(raw_vec), avg, raw_vec)
        return a * raw_clean + (1.0 - a) * avg


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class FeatureNormalizer:
    tendency_mean: NDArray | None = None
    tendency_std: NDArray | None = None
    success_mean: NDArray | None = None
    success_std: NDArray | None = None
    lead_mean: NDArray | None = None
    lead_std: NDArray | None = None

    def fit(self, profiles: list[BaserunnerStealProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs: list[NDArray]) -> tuple[NDArray, NDArray]:
            mat = np.array(vecs, dtype=np.float64)
            # SIM-531: a column no profile measured (the lead on a database
            # built before the Savant load) has no mean; z-score it to 0 / 1
            # so the normalizer never emits NaN statistics.
            measured = np.isfinite(mat).any(axis=0)
            m = np.zeros(mat.shape[1], dtype=np.float64)
            s = np.ones(mat.shape[1], dtype=np.float64)
            if measured.any():
                m[measured] = np.nanmean(mat[:, measured], axis=0)
                s[measured] = np.nanstd(mat[:, measured], axis=0)
            s[s == 0] = 1.0
            return m, s

        self.tendency_mean, self.tendency_std = _fit([p.tendency_vec for p in profiles])
        self.success_mean, self.success_std = _fit([p.success_vec for p in profiles])
        self.lead_mean, self.lead_std = _fit([p.lead_vec for p in profiles])

    def _norm(self, vec: NDArray, mean: NDArray | None, std: NDArray | None) -> NDArray:
        if mean is None:
            return vec
        return np.nan_to_num((vec - mean) / std, nan=0.0)

    def normalize_tendency(self, v: NDArray) -> NDArray:
        return self._norm(v, self.tendency_mean, self.tendency_std)

    def normalize_success(self, v: NDArray) -> NDArray:
        return self._norm(v, self.success_mean, self.success_std)

    def normalize_lead(self, v: NDArray) -> NDArray:
        return self._norm(v, self.lead_mean, self.lead_std)


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
        self._lead_mat: NDArray | None = None
        self._lead_has: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(self, profiles: list[BaserunnerStealProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.player_id, p.season) for p in profiles]
        if not profiles:
            return
        self._tendency_mat = np.array([norm.normalize_tendency(p.tendency_vec) for p in profiles])
        self._success_mat = np.array([norm.normalize_success(p.success_vec) for p in profiles])
        self._lead_mat = np.array([norm.normalize_lead(p.lead_vec) for p in profiles])
        self._lead_has = np.array([bool(p.has_lead) for p in profiles], dtype=bool)
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: BaserunnerStealProfile,
        norm: FeatureNormalizer,
        tend_rbf: WeightedRBFSimilarity,
        succ_rbf: WeightedRBFSimilarity,
        lead_rbf: WeightedRBFSimilarity | None = None,
    ) -> list[SimilarityResult]:
        """Score ``query`` against every profile.

        SIM-531: a pair scores over the groups BOTH sides have. With a lead
        on both sides the composite is the three-way blend; without one it is
        the tendency + success blend renormalized to sum to one. ``lead_rbf``
        defaults to the module bandwidth so a caller built without one (the
        no-DB fixtures) keeps working.
        """
        if not self.profiles:
            return []
        if lead_rbf is None:
            lead_rbf = _default_lead_rbf()

        query_key = (query.player_id, query.season)
        q_tend = norm.normalize_tendency(query.tendency_vec)
        q_succ = norm.normalize_success(query.success_vec)

        tend_scores = tend_rbf.score_batch(q_tend, self._tendency_mat)
        succ_scores = succ_rbf.score_batch(q_succ, self._success_mat)

        without_lead = (WEIGHT_TENDENCY * tend_scores + WEIGHT_SUCCESS * succ_scores) / (
            WEIGHT_TENDENCY + WEIGHT_SUCCESS
        )
        lead_has = (
            self._lead_has
            if self._lead_has is not None
            else np.zeros(len(self.profiles), dtype=bool)
        )
        both = lead_has & bool(getattr(query, "has_lead", False))
        if both.any() and self._lead_mat is not None:
            q_lead = norm.normalize_lead(query.lead_vec)
            lead_scores = lead_rbf.score_batch(q_lead, self._lead_mat)
            full = (
                WEIGHT_TENDENCY * tend_scores
                + WEIGHT_LEAD * lead_scores
                + WEIGHT_SUCCESS * succ_scores
            )
            composite = np.where(both, full, without_lead)
        else:
            lead_scores = None
            composite = without_lead

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
                lead_score=(float(lead_scores[i]) if lead_scores is not None and both[i] else None),
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


def _default_lead_rbf() -> WeightedRBFSimilarity:
    """The lead kernel at the module bandwidth (SIM-531)."""
    return WeightedRBFSimilarity(RBF_SIGMA_LEAD, np.array([w for _, w in LEAD_FEATURES]))


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
            "lead": {},
        }
        self._normalizer = FeatureNormalizer()
        # The success group's shrinkage (attempts, prior 20).
        self._shrinkage = EmpiricalBayesShrinkage()
        # SIM-531: the confidence basis and the tendency / lead groups'
        # shrinkage — first-base opportunities, prior 50.
        self._opps_shrinkage = EmpiricalBayesShrinkage(EB_N_PRIOR_OPPS)
        self._partition = StealPartition()

        # SIM-537: the cutoff every loaded profile declared, or None when the
        # source has no asof_date column yet (a pre-migration-0028 database)
        # or every row's column is NULL (built before the cutoff was tracked).
        self._asof_date = None

        self._tend_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_TENDENCY,
            np.array([w for _, w in TENDENCY_FEATURES]),
        )
        self._succ_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SUCCESS,
            np.array([w for _, w in SUCCESS_FEATURES]),
        )
        self._lead_rbf = _default_lead_rbf()

    @staticmethod
    def chances(first_base_opps: int, second_base_opps: int = 0, attempts: int = 0) -> int:
        """SIM-531: the runner's chances — the confidence basis and the
        tendency / lead shrinkage basis. First-base plus second-base
        plate-appearance starts, never below his attempts: every row the
        builder's driver admits carries at least one of the three, so no
        profile reads confidence 0 (a zero score against every row would give
        the steal draw no weight at all)."""
        return max(int(first_base_opps or 0) + int(second_base_opps or 0), int(attempts or 0))

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
        lead_rbf = getattr(self, "_lead_rbf", None) or _default_lead_rbf()
        self._lead_rbf = WeightedRBFSimilarity(
            _sig("sigma_baserunner_steal_lead", lead_rbf.sigma),
            np.array([w for _, w in LEAD_FEATURES]),
        )
        log.info(
            "SIM-406: applied calibration to BaserunnerStealSimilarityEngine "
            "(sigma_tendency=%.4f, sigma_success=%.4f, sigma_lead=%.4f).",
            self._tend_rbf.sigma,
            self._succ_rbf.sigma,
            self._lead_rbf.sigma,
        )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self, seasons: list[int] | None = None, *, include_below_minimum: bool = False
    ) -> None:
        """Load all steal profiles, apply shrinkage, build scoring matrices.

        SIM-523 (the kernel retirement): ``include_below_minimum`` loads the
        thin profiles too (their sample confidence shrinks their scores), so
        the score matrix covers every runner-season in the steal pool."""
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            self._load_league_averages(conn, seasons)
            self._load_profiles(conn, seasons, include_below_minimum)
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
            # SIM-531: a lead key the row lacks (a league row written before
            # the Savant load) is NaN — never 0.0 ft — and the shrinkage then
            # leaves the raw lead alone.
            self._league_avg["lead"][season] = np.array(
                [np.nan if pj.get(f) is None else float(pj[f]) for f, _ in LEAD_FEATURES],
                dtype=np.float64,
            )
        log.info("Loaded steal league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
        include_below_minimum: bool = False,
    ) -> None:
        sf = ""
        if seasons:
            sf = f"AND bss.season IN ({', '.join(str(s) for s in seasons)})"
        min_filter = "TRUE" if include_below_minimum else "NOT bss.below_minimum_sample"

        # SIM-537: graceful-optional column — a database that has not run
        # migration 0028 yet still builds; asof_date comes back NULL.
        _present = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'derived' AND table_name = 'baserunner_steal_metrics'"
            ).fetchall()
        }
        _asof_col = "bss.asof_date" if "asof_date" in _present else "NULL AS asof_date"
        # SIM-531: the lead columns exist only after migration 0029.
        _lead_cols = (
            "bss.lead_primary_ft, bss.lead_jump_ft"
            if {"lead_primary_ft", "lead_jump_ft"} <= _present
            else "NULL AS lead_primary_ft, NULL AS lead_jump_ft"
        )
        _opps2_col = (
            "bss.sample_second_base_opps"
            if "sample_second_base_opps" in _present
            else "NULL AS sample_second_base_opps"
        )

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
                bss.below_minimum_sample,
                {_asof_col},
                -- Lead (SIM-531)
                {_lead_cols},
                {_opps2_col}
            FROM derived.baserunner_steal_metrics bss
            WHERE {min_filter}
              {sf}
        """).fetchall()

        log.info("Loading %d steal profiles from DuckDB …", len(rows))

        # SIM-537: every profile must declare the same cutoff.
        asof_values: set = set()

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
                asof_val,
                lead_primary,
                lead_jump,
                n_opps_2b,
            ) = row
            asof_values.add(asof_val)

            def _v(*vals):
                # SIM-531: NULL loads as NaN (unmeasured), never 0.0 — a
                # success rate of 0.0 for a runner who never went, or a lead
                # of 0.0 ft, would be a measurement he never produced.
                return np.array([np.nan if v is None else float(v) for v in vals], dtype=np.float64)

            lead_vec = _v(lead_primary, lead_jump)
            self._profiles[(pid, season)] = BaserunnerStealProfile(
                player_id=pid,
                season=season,
                sample_steal_attempts=n_attempts or 0,
                sample_first_base_opps=n_opps or 0,
                tendency_vec=_v(sar, sar2),
                success_vec=_v(suc, suc2),
                lead_vec=lead_vec,
                has_lead=bool(np.isfinite(lead_vec).all()),
                sample_second_base_opps=n_opps_2b or 0,
                # SIM-531: the confidence basis is the runner's chances.
                eb_alpha=self._opps_shrinkage.alpha(
                    self.chances(n_opps or 0, n_opps_2b or 0, n_attempts or 0)
                ),
                below_minimum=bool(below_min),
            )

        # SIM-537: refuse a mixed set.
        if len(asof_values) > 1:
            raise RuntimeError(
                "baserunner-steal profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Baserunner-steal profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Pull every profile toward its season's league mean.

        Each group shrinks on the denominator its features are measured
        over: the tendency (attempts per chance) and the lead (measured on
        the runner's chances) on his chances, prior 50; the success rate
        (safe per attempt) on attempts, prior 20. A group with no league row
        is left raw; a NaN tendency or success feature becomes the league
        mean. An UNMEASURED lead (``has_lead`` False) is not shrunk at all:
        filling it with the league mean would put every such runner at the
        exact mean inside the normalizer's statistics, deflate the lead's
        spread and sharpen the kernel past its fitted bandwidth (the review
        of 2026-09-16 measured −24 to −27% on the standard deviation).
        """
        for p in self._profiles.values():
            s = p.season
            n_chances = self.chances(
                p.sample_first_base_opps,
                getattr(p, "sample_second_base_opps", 0),
                p.sample_steal_attempts,
            )
            for group, attr, shrinkage, n in (
                ("tendency", "tendency_vec", self._opps_shrinkage, n_chances),
                ("lead", "lead_vec", self._opps_shrinkage, n_chances),
                ("success", "success_vec", self._shrinkage, p.sample_steal_attempts),
            ):
                if group == "lead" and not getattr(p, "has_lead", False):
                    continue
                avg = self._league_avg.get(group, {}).get(s)
                if avg is not None:
                    setattr(p, attr, shrinkage.shrink(getattr(p, attr), avg, n))

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
            getattr(self, "_lead_rbf", None),
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
        # SIM-531: the same arithmetic as ``StealPartition.score_all`` — the
        # three-way blend when both sides carry a lead, else the two-way blend
        # renormalized (a test holds the two paths equal).
        lead_s: float | None = None
        if getattr(pa, "has_lead", False) and getattr(pb, "has_lead", False):
            lead_rbf = getattr(self, "_lead_rbf", None) or _default_lead_rbf()
            lead_s = lead_rbf.score(
                norm.normalize_lead(pa.lead_vec),
                norm.normalize_lead(pb.lead_vec),
            )
            composite = WEIGHT_TENDENCY * tend_s + WEIGHT_LEAD * lead_s + WEIGHT_SUCCESS * succ_s
        else:
            composite = (WEIGHT_TENDENCY * tend_s + WEIGHT_SUCCESS * succ_s) / (
                WEIGHT_TENDENCY + WEIGHT_SUCCESS
            )
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            player_id=pb.player_id,
            season=pb.season,
            score=composite,
            tendency_score=tend_s,
            success_score=succ_s,
            sample_steal_attempts=pb.sample_steal_attempts,
            lead_score=lead_s,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_profile(self, player_id: int, season: int) -> BaserunnerStealProfile | None:
        return self._profiles.get((player_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    @property
    def asof_date(self):
        """The cutoff every loaded profile declared, or ``None`` (SIM-537)."""
        return self._asof_date

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
        sub_score_names=["tendency_score", "lead_score", "success_score"],
        n_query_samples=50,
        engine_name="BaserunnerSteal",
    )
    print(report)
