"""The pitcher-hold engine: how alike two pitcher-seasons are at holding runners.

The engine scores a pair of pitcher-seasons from 0 (unlike) to 1 (identical).
The steal draw weights its rows by this score (the actor matrix
``pitcher_steal``). The engine is apart from the main pitcher engine
(``pitcher_similarity.py``), which scores the arsenal and command. Holding
runners is a skill of its own: some pitchers let a fast runner steal at will,
while a quick one stops the running game whoever catches.

The score blends two sub-scores. Each weight comes from how well the group
repeats year to year (owner decision 2026-09-16, the lead-distance work,
SIM-531; plan §6.1 of docs/audit/2026-09-16-sim531-lead-distance-build-plan.md):

  1. Outcome (35%): the stolen bases allowed per 9 innings, the
     caught-stealing rate when runners go, and the attempts allowed per plate
     appearance with a runner on. The group repeats at 0.35 on average; the
     caught-stealing rate alone at 0.32 / -0.03, which is noise.

  2. Hold (65%): what the pitcher ALLOWS, in feet, from Baseball Savant's
     Pitcher Running Game board. The runner's lead before the pitch repeats
     at 0.43. The extra distance the runner gains on the delivery, the jump,
     repeats at 0.89-0.90: the most stable number in either steal engine.

The feed publishes no delivery times. The table also holds pickoff and
step-off rates; this engine does not read them.

A pitcher with no Savant row (a season before 2023, or a pitcher the board
never listed) has no hold measurement. His pairs score on the outcome group
alone, never against a lead of 0.0 ft. A NULL loads as NaN.

Each sub-score is a Gaussian kernel (RBF, ``similarity/engines/rbf_kernel.py``):
score = exp(-d² / 2σ²) over the group's z-scored features (a z-score is the
value minus the population mean, over the population standard deviation).

Both groups shrink toward the season's league mean by empirical-Bayes (EB)
shrinkage: the pitcher keeps the share α = n / (n + 25) of his own value,
where n is his count of baserunner events (plate appearances with a runner
on). The outcomes are noisy: they rest on attempts, which rest on the
runners' aggression and on the catcher. They stabilize by about 80 events. A
20-event reliever's lead allowed is 56% league mean. The composite is then
multiplied by the square root of the lower α of the pair.

Usage
-----
  engine = PitcherStealSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(pitcher_id=543037, season=2025, n=20)
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
log = logging.getLogger("pitcher_steal_similarity")


# ============================================================================
# Features and weights
# ============================================================================

# --- Outcome: the play-by-play's steal results against the pitcher ---
OUTCOME_FEATURES = [
    ("sb_against_per_9", 0.700),  # stolen bases allowed per 9 innings pitched
    ("cs_rate_forced", 0.500),  # caught-stealing rate when runners go
    ("steal_attempt_rate_allowed", 0.600),  # attempts per PA with a runner on
]

# --- Hold ---
# What the pitcher ALLOWS, in feet, from Savant's Pitcher Running Game board
# (derived.pitcher_steal_metrics). Each weight is the trait's year-to-year
# repeat on the qualified rows.
HOLD_FEATURES = [
    ("lead_allowed_primary_ft", 0.430),  # the lead he allows before the pitch
    ("lead_allowed_jump_ft", 0.895),  # the jump he gives up on the delivery
]

# --- Sub-score weights (sum to 1.0) ---
# Each weight is the group's share of the summed year-to-year repeats, rounded:
# outcome 0.35, hold 0.66 (owner decision 2026-09-16, the lead-distance work,
# SIM-531).
WEIGHT_OUTCOME = 0.35
WEIGHT_HOLD = 0.65
_TOTAL = WEIGHT_OUTCOME + WEIGHT_HOLD
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The kernel bandwidths σ (gamma = 1 / (2σ²)).
RBF_SIGMA_OUTCOME = 1.1000
# sigma_pitcher_steal_hold as ``make calibrate`` fitted it on 2026-09-16 over
# the 2,390 qualified pitcher-seasons with a measured lead allowed
# (2023-2026). The value lives here because the actor matrix builder does not
# read the calibration report (plan §3, Finding 4). To refit, copy the new
# value here.
RBF_SIGMA_HOLD = 0.9897

# The EB prior strength: at EB_N_PRIOR baserunner events, α = 0.5.
EB_N_PRIOR = 25

# The computor's minimum of baserunner events. The engine does not read this
# constant: the loader filters on the table's below_minimum_sample flag.
MIN_BASERUNNER_EVENTS = 30


# ============================================================================
# Data structures
# ============================================================================


def _no_hold() -> NDArray[np.float64]:
    """The hold vector of a pitcher with no Savant row: NaN, never 0.0."""
    return np.full(len(HOLD_FEATURES), np.nan, dtype=np.float64)


@dataclass(slots=True)
class PitcherStealProfile:
    """One pitcher-season's hold profile."""

    pitcher_id: int
    season: int
    throws: str  # "L" or "R"
    sample_baserunner_events: int  # plate appearances with a runner on
    sample_steal_attempts_against: int  # steal attempts against this pitcher

    # The feature vectors: raw at load, shrunk by ``build``, z-scored when scored.
    outcome_vec: NDArray[np.float64]  # shape (len(OUTCOME_FEATURES),)
    # The hold group; NaN when the pitcher has no Savant row.
    hold_vec: NDArray[np.float64] = field(default_factory=_no_hold)
    # Whether the hold was MEASURED (both features finite at load). A pitcher
    # without one scores on the outcome alone, and his NaN hold is never
    # shrunk (it would otherwise enter the normalizer's statistics).
    has_hold: bool = False

    # The EB sample confidence α, over the baserunner events.
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
    # The hold kernel between the pair; None when either side has no measured
    # hold (the pair scored on the outcome group alone).
    hold_score: float | None = None


# ============================================================================
# Empirical-Bayes shrinkage and feature normalization
# ============================================================================


class EmpiricalBayesShrinkage:
    """EB shrinkage toward the league mean: the pitcher keeps the share
    α = n / (n + n_prior) of his own value."""

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n: int) -> float:
        return n / (n + self.n_prior)

    def shrink(self, raw: NDArray, avg: NDArray, n: int) -> NDArray:
        """Pull ``raw`` toward the league mean by ``alpha(n)``. A NaN raw value
        becomes the league mean. A NaN league value (a key the league row
        lacks) leaves the raw value alone."""
        a = self.alpha(n)
        avg_eff = np.where(np.isnan(avg), raw, avg)
        return a * np.where(np.isnan(raw), avg_eff, raw) + (1.0 - a) * avg_eff


@dataclass(slots=True)
class FeatureNormalizer:
    """The z-score of each feature, fit on the population of pitcher profiles.
    A NaN z-score reads as 0, the population mean."""

    outcome_mean: NDArray | None = None
    outcome_std: NDArray | None = None
    hold_mean: NDArray | None = None
    hold_std: NDArray | None = None

    def fit(self, profiles: list[PitcherStealProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs):
            mat = np.array(vecs, dtype=np.float64)
            # A column no profile measured (the hold on a database without the
            # Savant load) gets mean 0 and standard deviation 1.
            measured = np.isfinite(mat).any(axis=0)
            m = np.zeros(mat.shape[1], dtype=np.float64)
            s = np.ones(mat.shape[1], dtype=np.float64)
            if measured.any():
                m[measured] = np.nanmean(mat[:, measured], axis=0)
                s[measured] = np.nanstd(mat[:, measured], axis=0)
            s[s == 0] = 1.0
            return m, s

        self.outcome_mean, self.outcome_std = _fit([p.outcome_vec for p in profiles])
        self.hold_mean, self.hold_std = _fit([p.hold_vec for p in profiles])

    def _norm(self, v, m, s):
        if m is None:
            return v
        return np.nan_to_num((v - m) / s, nan=0.0)

    def normalize_outcome(self, v):
        return self._norm(v, self.outcome_mean, self.outcome_std)

    def normalize_hold(self, v):
        return self._norm(v, self.hold_mean, self.hold_std)


# ============================================================================
# Scoring partition
# ============================================================================


class PitcherStealPartition:
    """Every pitcher-hold profile, with its z-scored feature matrices for
    scoring a query against all of them at once."""

    def __init__(self) -> None:
        self.profiles: list[PitcherStealProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._outcome_mat: NDArray | None = None
        self._hold_mat: NDArray | None = None
        self._hold_has: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    def build(self, profiles: list[PitcherStealProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.pitcher_id, p.season) for p in profiles]
        if not profiles:
            return
        self._outcome_mat = np.array([norm.normalize_outcome(p.outcome_vec) for p in profiles])
        self._hold_mat = np.array([norm.normalize_hold(p.hold_vec) for p in profiles])
        self._hold_has = np.array([bool(p.has_hold) for p in profiles], dtype=bool)
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: PitcherStealProfile,
        norm: FeatureNormalizer,
        out_rbf: WeightedRBFSimilarity,
        hold_rbf: WeightedRBFSimilarity | None = None,
    ) -> list[SimilarityResult]:
        """Score ``query`` against every profile but its own.

        A pair with a measured hold on both sides blends the two groups. A
        pair missing it on either side scores on the outcome alone (the
        outcome group's weight renormalized to one). The composite is then
        multiplied by the square root of the pair's lower α. ``hold_rbf``
        defaults to the module bandwidth, for an engine built without one
        (the no-DB test fixtures).
        """
        if not self.profiles:
            return []
        if hold_rbf is None:
            hold_rbf = _default_hold_rbf()

        query_key = (query.pitcher_id, query.season)
        out_s = out_rbf.score_batch(norm.normalize_outcome(query.outcome_vec), self._outcome_mat)

        hold_has = (
            self._hold_has
            if self._hold_has is not None
            else np.zeros(len(self.profiles), dtype=bool)
        )
        both = hold_has & bool(getattr(query, "has_hold", False))
        if both.any() and self._hold_mat is not None:
            hold_s = hold_rbf.score_batch(norm.normalize_hold(query.hold_vec), self._hold_mat)
            composite = np.where(both, WEIGHT_OUTCOME * out_s + WEIGHT_HOLD * hold_s, out_s)
        else:
            hold_s = None
            composite = out_s
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
                hold_score=(float(hold_s[i]) if hold_s is not None and both[i] else None),
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


def _default_hold_rbf() -> WeightedRBFSimilarity:
    """The hold kernel at the module bandwidth."""
    return WeightedRBFSimilarity(RBF_SIGMA_HOLD, np.array([w for _, w in HOLD_FEATURES]))


# ============================================================================
# The engine
# ============================================================================


class PitcherStealSimilarityEngine:
    """The pitcher-hold engine.

    A query scores against every loaded pitcher-season. ``build`` loads the
    profiles the table does not flag below its minimum sample.

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
            "hold": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = PitcherStealPartition()

        # The cutoff (asof_date) every loaded profile declared. None when the
        # table has no asof_date column or every row's value is NULL.
        self._asof_date = None

        self._out_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
        )
        self._hold_rbf = _default_hold_rbf()

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the outcome and hold kernels from a fitted report.

        A sigma left at its 0.0 default keeps the engine's current sigma. The
        reliability weights stay the module's.
        """
        v = float(getattr(report, "sigma_pitcher_steal_outcome", 0.0) or 0.0)
        sigma = v if v > 0.0 else self._out_rbf.sigma
        self._out_rbf = WeightedRBFSimilarity(sigma, np.array([w for _, w in OUTCOME_FEATURES]))
        hold_rbf = getattr(self, "_hold_rbf", None) or _default_hold_rbf()
        h = float(getattr(report, "sigma_pitcher_steal_hold", 0.0) or 0.0)
        self._hold_rbf = WeightedRBFSimilarity(
            h if h > 0.0 else hold_rbf.sigma, np.array([w for _, w in HOLD_FEATURES])
        )
        log.info(
            "SIM-406: applied calibration to PitcherStealSimilarityEngine "
            "(sigma_outcome=%.4f, sigma_hold=%.4f).",
            self._out_rbf.sigma,
            self._hold_rbf.sigma,
        )

    def build(self, seasons: list[int] | None = None) -> None:
        """Load the pitcher-hold profiles, shrink them and build the scoring
        matrices."""
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
            # A hold key the row lacks is NaN, never 0.0 ft, and the shrinkage
            # then leaves the raw value alone.
            self._league_avg["hold"][season] = np.array(
                [np.nan if pj.get(f) is None else float(pj[f]) for f, _ in HOLD_FEATURES],
                dtype=np.float64,
            )

    def _load_profiles(self, conn, seasons):
        sf = f"AND psm.season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""

        # A table without the asof_date column (a database before DuckDB
        # migration 0028) still builds: the cutoff reads NULL on every row.
        _present = duck_columns(conn, "derived", "pitcher_steal_metrics")
        _asof_col = "psm.asof_date" if "asof_date" in _present else "NULL AS asof_date"
        # The hold columns arrive with DuckDB migration 0029; an older table
        # reads NULL.
        _hold_cols = (
            "psm.lead_allowed_primary_ft, psm.lead_allowed_jump_ft"
            if {"lead_allowed_primary_ft", "lead_allowed_jump_ft"} <= _present
            else "NULL AS lead_allowed_primary_ft, NULL AS lead_allowed_jump_ft"
        )

        rows = conn.execute(f"""
            SELECT
                psm.pitcher_id, psm.season, psm.throws,
                psm.sample_baserunner_events,
                psm.sample_steal_attempts_against,
                -- Outcome (3)
                psm.sb_against_per_9,
                psm.cs_rate_forced,
                psm.steal_attempt_rate_allowed,
                psm.below_minimum_sample,
                {_asof_col},
                -- Hold (SIM-531)
                {_hold_cols}
            FROM derived.pitcher_steal_metrics psm
            WHERE NOT psm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d pitcher-steal profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff.
        asof_values: set = set()

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
                asof_val,
                hold_primary,
                hold_jump,
            ) = row
            asof_values.add(asof_val)

            def _v(*vals):
                # NULL loads as NaN (unmeasured), never 0.0.
                return np.array([np.nan if v is None else float(v) for v in vals], dtype=np.float64)

            hold_vec = _v(hold_primary, hold_jump)
            self._profiles[(pid, season)] = PitcherStealProfile(
                pitcher_id=pid,
                season=season,
                throws=throws or "R",
                sample_baserunner_events=n_br_events or 0,
                sample_steal_attempts_against=n_steal_against or 0,
                outcome_vec=_v(sb_per_9, cs_rate, steal_rate),
                hold_vec=hold_vec,
                has_hold=bool(np.isfinite(hold_vec).all()),
                eb_alpha=self._shrinkage.alpha(n_br_events or 0),
                below_minimum=bool(below_min),
            )

        # Refuse profiles built at two cutoffs, or one stamped and one not.
        if len(asof_values) > 1:
            raise RuntimeError(
                "pitcher-steal profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Pitcher-steal profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Pull both groups toward the season's league mean on the pitcher's
        baserunner events (prior 25). A 20-event reliever's lead allowed is
        56% league mean. A group with no league row stays raw.

        An unmeasured hold (``has_hold`` False) is not shrunk at all. The
        league mean would cut the hold's spread inside the normalizer and
        sharpen the kernel past its fitted bandwidth."""
        for p in self._profiles.values():
            for group, attr in (("outcome", "outcome_vec"), ("hold", "hold_vec")):
                if group == "hold" and not getattr(p, "has_hold", False):
                    continue
                avg = self._league_avg.get(group, {}).get(p.season)
                if avg is not None:
                    setattr(
                        p,
                        attr,
                        self._shrinkage.shrink(getattr(p, attr), avg, p.sample_baserunner_events),
                    )

    def query(
        self,
        pitcher_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """Score the query pitcher against every pitcher-hold profile, best
        first; ``n`` keeps the top n."""
        profile = self._profiles.get((pitcher_id, season))
        if profile is None:
            log.warning("Pitcher-steal %d season %d not found.", pitcher_id, season)
            return []

        results = self._partition.score_all(
            profile,
            self._normalizer,
            self._out_rbf,
            getattr(self, "_hold_rbf", None),
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        pitcher_a: tuple[int, int],
        pitcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """The score of pitcher-season ``pitcher_b`` against ``pitcher_a``."""
        pa = self._profiles.get(pitcher_a)
        pb = self._profiles.get(pitcher_b)
        if pa is None or pb is None:
            return None

        norm = self._normalizer
        out_s = self._out_rbf.score(
            norm.normalize_outcome(pa.outcome_vec), norm.normalize_outcome(pb.outcome_vec)
        )

        # The same arithmetic as ``PitcherStealPartition.score_all``.
        hold_s: float | None = None
        if getattr(pa, "has_hold", False) and getattr(pb, "has_hold", False):
            hold_rbf = getattr(self, "_hold_rbf", None) or _default_hold_rbf()
            hold_s = hold_rbf.score(
                norm.normalize_hold(pa.hold_vec), norm.normalize_hold(pb.hold_vec)
            )
            composite = WEIGHT_OUTCOME * out_s + WEIGHT_HOLD * hold_s
        else:
            composite = out_s
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            pitcher_id=pb.pitcher_id,
            season=pb.season,
            throws=pb.throws,
            score=composite,
            outcome_score=out_s,
            sample_baserunner_events=pb.sample_baserunner_events,
            hold_score=hold_s,
        )

    def get_profile(self, pitcher_id: int, season: int) -> PitcherStealProfile | None:
        return self._profiles.get((pitcher_id, season))

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
    engine = PitcherStealSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_generic_diagnostics(
        engine,
        sub_score_names=["outcome_score", "hold_score"],
        n_query_samples=50,
        engine_name="PitcherSteal",
    )
    print(report)
