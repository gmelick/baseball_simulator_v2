"""The steal-runner engine: how alike two runner-seasons are as base stealers.

The engine scores a pair of runner-seasons from 0 (unlike) to 1 (identical).
The steal draw weights its rows by this score (the actor matrix
``runner_steal``). The engine is apart from the extra-base engine
(``baserunner_similarity.py``) because the skills differ:

  * An extra base is a read made during the play: a fly ball or a line
    drive, then a go or a stop.
  * A steal is planned before the pitch: the runner reads the pitcher, takes
    a lead and goes on the first move. The lead and the jump matter more
    than raw sprint speed.

The score blends three sub-scores. Each weight comes from how well the
group's traits repeat year to year on the qualified rows (owner decision
2026-09-16, the lead-distance work, SIM-531; plan §6.1 of
docs/audit/2026-09-16-sim531-lead-distance-build-plan.md):

  1. Tendency (45%): how often the runner tries to steal. The attempt rate
     per plate appearance begun on first, and the attempt rate at third per
     plate appearance begun on second. Repeats at 0.78.

  2. Lead (45%): the runner's lead off the bag in feet before the pitch
     (repeats at 0.73-0.74), and the extra distance he gains on the
     delivery, the jump (0.80-0.85). Both come from Baseball Savant's
     Basestealing Run Value board. They are close to independent of each
     other (r = 0.02) and of the attempt rate. Reaction time, burst and the
     break angle are not published; the lead and the jump are.

  3. Success (10%): how often an attempt arrives safe. It repeats at only
     0.13-0.26, close to noise, so it carries the smallest weight.

Each sub-score is a Gaussian kernel (RBF, ``similarity/engines/rbf_kernel.py``):
score = exp(-d² / 2σ²) over the group's z-scored features (a z-score is the
value minus the population mean, over the population standard deviation).

The missing-value rule
----------------------
  A runner with no Savant row (a season before 2023, or a runner the board
  never listed) has no lead. His pairs score over the groups BOTH sides have,
  tendency and success with their weights renormalized to sum to one. He is
  never scored against a lead of 0.0 ft, which sits twelve standard
  deviations below the league. A NULL loads as NaN, and the normalizer reads
  a NaN as the population mean.

The confidence basis
--------------------
  Every runner who had a chance carries a profile, including the runners who
  never went. The confidence that multiplies the whole score is the
  empirical-Bayes (EB) share α = chances / (chances + 50). The chances are the
  plate appearances begun on first plus those begun on second, never fewer
  than his attempts (an attempt is a chance). An attempt basis would score a
  zero-attempt runner 0.0 against every row. A first-base basis would do the
  same to a runner whose chances were all on second (the extra-innings
  automatic runner), or whose only attempts were of third or home. Either
  way the steal draw would give that runner no weight at all.

  EB shrinkage pulls each group toward the season's league mean on its own
  denominator: the tendency and the lead on the chances, the success rate
  on the attempts. A runner with no measured lead is not shrunk on the lead:
  his NaN stays NaN. The normalizer's statistics then come from the measured
  runners alone, and the bandwidth applies as fitted.

Usage
-----
  engine = BaserunnerStealSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
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

from db.introspect import duck_columns
from similarity.engines.rbf_kernel import WeightedRBFSimilarity
from similarity.similarity_diagnostics import run_generic_diagnostics

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
log = logging.getLogger("baserunner_steal_similarity")


# ============================================================================
# Features and weights
# ============================================================================

# --- Tendency ---
# How often the runner starts a steal attempt.
TENDENCY_FEATURES = [
    # feature_name,                 reliability_weight
    ("steal_attempt_rate", 0.800),  # attempts per PA begun on first; stable by ~50 chances
    ("steal_attempt_rate_2b", 0.500),  # attempts at third per PA begun on second; sparser
]

# --- Lead ---
# The runner's lead off the bag in feet, from Savant's Basestealing Run Value
# board (derived.baserunner_steal_metrics). Each weight is the trait's
# year-to-year repeat on the qualified rows (plan §2.2).
LEAD_FEATURES = [
    ("lead_primary_ft", 0.735),  # the lead before the pitch
    ("lead_jump_ft", 0.823),  # the extra distance gained on the delivery
]

# --- Success ---
# How often an attempt arrives safe.
SUCCESS_FEATURES = [
    ("steal_success_rate", 0.700),  # over every attempt
    ("steal_success_rate_2b", 0.400),  # attempts at third: sparser, noisier
]

# --- Sub-score weights (sum to 1.0) ---
# Each weight is the group's share of the summed year-to-year repeats on the
# qualified rows, rounded: tendency 0.78, lead 0.78 (primary 0.735, jump
# 0.823), success 0.19 (owner decision 2026-09-16, the lead-distance work,
# SIM-531).
WEIGHT_TENDENCY = 0.45
WEIGHT_LEAD = 0.45
WEIGHT_SUCCESS = 0.10
_TOTAL = WEIGHT_TENDENCY + WEIGHT_LEAD + WEIGHT_SUCCESS
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The kernel bandwidths σ (gamma = 1 / (2σ²)). The target of each is a median
# score near 0.50 across the population.
RBF_SIGMA_TENDENCY = 1.0500
RBF_SIGMA_SUCCESS = 1.0200
# sigma_baserunner_steal_lead as ``make calibrate`` fitted it on 2026-09-16
# over the 481 qualified runner-seasons with a measured lead (2023-2026). The
# value lives here because the actor matrix builder does not read the
# calibration report (plan §3, Finding 4). To refit, copy the new value here.
RBF_SIGMA_LEAD = 0.9836

# The EB prior of the success group, whose denominator is attempts: at
# EB_N_PRIOR attempts, α = 0.5.
EB_N_PRIOR = 20
# The EB prior of the confidence and of the tendency and lead groups, over the
# runner's chances (the attempt rate stabilizes by about 50 of them). At 5
# chances a runner's lead is 91% league mean; at 300 it is 86% his own.
EB_N_PRIOR_OPPS = 50

# The computor's minimum of steal attempts. The engine does not read this
# constant: the loader filters on the table's below_minimum_sample flag.
MIN_STEAL_ATTEMPTS = 10


# ============================================================================
# Data structures
# ============================================================================


def _no_lead() -> NDArray[np.float64]:
    """The lead vector of a runner with no Savant row: NaN, never 0.0."""
    return np.full(len(LEAD_FEATURES), np.nan, dtype=np.float64)


@dataclass(slots=True)
class BaserunnerStealProfile:
    """One runner-season's stolen-base profile."""

    player_id: int
    season: int
    sample_steal_attempts: int  # every steal attempt (of second, third or home)
    sample_first_base_opps: int  # plate appearances begun on first

    # The feature vectors: raw at load, shrunk by ``build``, z-scored when scored.
    tendency_vec: NDArray[np.float64]  # shape (len(TENDENCY_FEATURES),)
    success_vec: NDArray[np.float64]  # shape (len(SUCCESS_FEATURES),)
    # The lead group; NaN when the runner has no Savant row.
    lead_vec: NDArray[np.float64] = field(default_factory=_no_lead)
    # Whether the lead was MEASURED (both features finite at load). A runner
    # without one scores over the groups he has, and his NaN lead is never
    # shrunk (it would otherwise enter the normalizer's statistics).
    has_lead: bool = False
    # Plate appearances begun on second. With the first-base count and the
    # attempts, they give the runner's chances (the confidence basis).
    sample_second_base_opps: int = 0

    # The EB sample confidence α, over the runner's chances.
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
    # The lead kernel between the pair; None when either side has no measured
    # lead (the pair scored over tendency and success alone).
    lead_score: float | None = None


# ============================================================================
# Empirical-Bayes shrinkage
# ============================================================================


class EmpiricalBayesShrinkage:
    """EB shrinkage toward the league mean: the runner keeps the share
    α = n / (n + n_prior) of his own value."""

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

        A NaN raw value becomes the league mean. A NaN league value (a key the
        league row lacks) leaves the raw value alone: a feature shrinks only
        toward a mean that exists.
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
    """The z-score of each feature, fit on the population of runner profiles.
    A NaN z-score reads as 0, the population mean."""

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
            # A column no profile measured (the lead on a database without the
            # Savant load) gets mean 0 and standard deviation 1, so the
            # normalizer never emits a NaN statistic.
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
# Scoring partition
# ============================================================================


class StealPartition:
    """Every steal profile, with its z-scored feature matrices for scoring a
    query against all of them at once."""

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
        """Score ``query`` against every profile but its own.

        A pair scores over the groups BOTH sides have. With a lead on both
        sides the composite blends all three groups. Without one it blends
        tendency and success, their weights renormalized to sum to one. The
        composite is then multiplied by the square root of the pair's lower α.
        ``lead_rbf`` defaults to the module bandwidth, for an engine built
        without one (the no-DB test fixtures).
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
    """The lead kernel at the module bandwidth."""
    return WeightedRBFSimilarity(RBF_SIGMA_LEAD, np.array([w for _, w in LEAD_FEATURES]))


# ============================================================================
# The engine
# ============================================================================


class BaserunnerStealSimilarityEngine:
    """The steal-runner engine.

    A query scores against every loaded runner-season. ``build`` loads the
    profiles at or above the table's minimum sample, or every profile with
    ``include_below_minimum``.

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
        # The success group's shrinkage: over attempts, prior 20.
        self._shrinkage = EmpiricalBayesShrinkage()
        # The confidence and the tendency and lead groups' shrinkage: over
        # the runner's chances (``chances``), prior 50.
        self._opps_shrinkage = EmpiricalBayesShrinkage(EB_N_PRIOR_OPPS)
        self._partition = StealPartition()

        # The cutoff (asof_date) every loaded profile declared. None when the
        # table has no asof_date column or every row's value is NULL.
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
        """The runner's chances: the basis of the confidence and of the
        tendency and lead shrinkage.

        The chances are the plate appearances begun on first plus those begun
        on second, never fewer than his attempts. Every row the computor
        writes carries at least one of the three, so no profile reads
        confidence 0. A zero confidence would score the runner 0 against
        every row and give the steal draw no weight for him."""
        return max(int(first_base_opps or 0) + int(second_base_opps or 0), int(attempts or 0))

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the tendency, success and lead kernels from a fitted report.

        A sigma left at its 0.0 default keeps the engine's current sigma. The
        reliability weights stay the module's.
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
        """Load the steal profiles, shrink them and build the scoring matrices.

        ``include_below_minimum`` also loads the profiles below the minimum
        sample; their low α shrinks their scores. The actor matrix builder
        sets it, so the matrix covers every runner-season in the steal pool."""
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
            # A lead key the row lacks (a database without the Savant load) is
            # NaN, never 0.0 ft, and the shrinkage then leaves the raw lead
            # alone.
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

        # A table without the asof_date column (a database before DuckDB
        # migration 0028) still builds: the cutoff reads NULL on every row.
        _present = duck_columns(conn, "derived", "baserunner_steal_metrics")
        _asof_col = "bss.asof_date" if "asof_date" in _present else "NULL AS asof_date"
        # The lead columns and the second-base count arrive with DuckDB
        # migration 0029; an older table reads NULL.
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

        # Every profile must declare the same cutoff.
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
                # NULL loads as NaN (unmeasured), never 0.0. A success rate of
                # 0.0 for a runner who never went, or a lead of 0.0 ft, is a
                # measurement he never produced.
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
                # The confidence basis is the runner's chances.
                eb_alpha=self._opps_shrinkage.alpha(
                    self.chances(n_opps or 0, n_opps_2b or 0, n_attempts or 0)
                ),
                below_minimum=bool(below_min),
            )

        # Refuse profiles built at two cutoffs, or one stamped and one not.
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

        Each group shrinks on its own denominator. The tendency (attempts per
        plate appearance begun on base) and the lead shrink on the runner's
        chances, prior 50. The success rate (safe per attempt) shrinks on
        attempts, prior 20. A group with no league row stays raw. A NaN
        tendency or success feature becomes the league mean.

        An unmeasured lead (``has_lead`` False) is not shrunk at all. The
        league mean would put every such runner at the exact mean inside the
        normalizer's statistics. That cuts the lead's standard deviation
        (by 24-27% in the lead-distance review, SIM-531) and sharpens the
        kernel past its fitted bandwidth.
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
        Score the query runner against every steal profile.

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
        """The score of runner-season ``runner_b`` against ``runner_a``."""
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
        # The same arithmetic as ``StealPartition.score_all``: the three-group
        # blend when both sides carry a lead, else the two-group blend
        # renormalized. A test holds the two paths equal.
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
        """The cutoff every loaded profile declared, or ``None``."""
        return self._asof_date

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


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
