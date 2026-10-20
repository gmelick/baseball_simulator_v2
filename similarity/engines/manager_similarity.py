"""Manager-to-manager similarity: one score in [0, 1] between two manager-seasons.

The score mixes three sub-scores. Each sub-score is a weighted Gaussian
kernel (an RBF: score = exp(-distance² / 2σ²)) over z-scored features. A
z-score is a value's distance from the population mean, in standard
deviations. The profile computor derives the features from ``raw.pitches``
and the per-game bullpen availability table. The fielding manager owns the
pitching decisions and the defensive changes; the batting manager owns the
offensive ones.

  1. Pitcher usage (0.40): how long the starter goes, how often he leaves
     before 100 pitches, the leverage when the last reliever enters, the
     relievers' share of high-leverage pitches, short starts, long relief,
     and the share of the available relievers the manager uses.
  2. Offensive aggressiveness (0.35): steal attempts, sacrifice bunts in high-
     and low-leverage spots, and bunts with a runner on third. The
     hit-and-run rate reads 0.0 for every manager: Statcast does not flag it.
  3. Platoon and matchups (0.25): pinch hitters, late defensive changes, and
     the share of plate appearances with the platoon edge. The double-switch
     rate reads 0.0 for every manager: the data cannot detect it.

The composite is the weighted sum of the three, times the pair confidence
(a discount for a thin sample on either side), clipped to [0, 1].

Where the score goes
--------------------
Of this engine's scores, only the usage sub-score reaches the simulator. The
nightly actor-matrix build (``--what actors_sim``) stores ``usage_score`` for
every pair of manager-seasons as the ``manager_usage`` matrix. The
pitching-change draw weights each row of the change pool by the live
manager's usage similarity to the row's manager, raised to its power (each
team's real manager decides the pitching change, SIM-427). The usage
sub-score carries no pair confidence; only the composite does. The build
applies no calibration report, so the module defaults below are the values
the draw sees. The composite and the other two sub-scores serve the
Similarity Explorer.

Design
------
Manager tendencies hold steady from season to season, but they are noisier
than player metrics: every decision depends on the game. Every feature is
therefore a rate per opportunity or an average, never a raw count.
Empirical-Bayes (EB) shrinkage pulls each manager-season toward the league
mean of its season; see EB_N_PRIOR.

Usage
-----
  engine = ManagerSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2023, 2024, 2025, 2026])
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
log = logging.getLogger("manager_similarity")


# ============================================================================
# Config — feature definitions and weights
# ============================================================================

# Each group lists (feature, reliability weight). A reliability weight is the
# feature's share of its group's kernel; a steadier feature gets more pull.
#
# The leverage below is the profile computor's three-step bucket, not the
# leverage index: high = a margin of one run or less in the 7th inning or
# later; low = a margin of four or more, or the 5th inning or earlier; mid =
# the rest.

# --- Pitcher usage (the fielding manager) ---
# Averages and shares, never counts. This group alone weights the pitching-change draw.
USAGE_FEATURES = [
    # feature_name,                             reliability_weight
    ("starter_avg_pitch_count", 0.800),  # mean pitches by the starter; stable
    ("starter_pull_pct_before_100", 0.700),  # share of starts ended under 100 pitches
    ("closer_entry_leverage_index", 0.600),  # mean bucket (0/1/2) as the last reliever enters
    ("high_leverage_reliever_rate", 0.600),  # share of high-bucket pitches thrown by relievers
    ("opener_usage_rate", 0.500),  # share of starts of two innings or fewer
    ("bulk_innings_rate", 0.500),  # share of games with a reliever in 3+ innings
    # Of the relievers available to the manager, the share he used: used /
    # (used + available but held), from the per-game bullpen availability
    # table. It separates "chose to hold an arm" from "no arm could pitch".
    # Weight 0.55, just below the 0.60 bullpen features, because its
    # availability source (a rest and injured-list inference) is noisier than
    # the pitch counts.
    ("available_reliever_usage_rate", 0.550),
]

# --- Offensive aggressiveness (the batting manager) ---
AGGRESSION_FEATURES = [
    ("steal_order_rate_per_1b_opp", 0.700),  # steal attempts / PA with a runner on first
    ("hit_and_run_rate_per_opportunity", 0.600),  # NULL, read as 0.0: Statcast does not flag it
    ("sac_bunt_rate_high_leverage", 0.650),  # sac bunts / high-bucket PA
    ("sac_bunt_rate_low_leverage", 0.500),  # sac bunts / low-bucket PA
    ("squeeze_play_rate_per_3b_opp", 0.400),  # sac bunts / PA with a runner on third
]

# --- Platoon and matchups (pinch hits: the batting manager; defense: the fielding one) ---
PLATOON_FEATURES = [
    ("pinch_hit_rate_vs_same_hand", 0.700),  # share of pinch-hit PA against a same-hand pitcher
    ("pinch_hit_rate_high_leverage", 0.650),  # pinch-hit PA / high-bucket PA
    ("defensive_sub_rate_late_innings", 0.550),  # late (7th+) defensive changes per game
    ("double_switch_rate_per_reliever_change", 0.600),  # NULL, read as 0.0: not detectable
    ("platoon_advantage_exploitation_rate", 0.700),  # share of PA with the platoon edge
]

# --- Sub-score weights (sum to 1.0) ---
# Pitcher usage weighs most: of a manager's decisions, it moves a simulated game
# the most. The weights shape only the composite, which the simulator does not read.
WEIGHT_USAGE = 0.40
WEIGHT_AGGRESSION = 0.35
WEIGHT_PLATOON = 0.25
_TOTAL = WEIGHT_USAGE + WEIGHT_AGGRESSION + WEIGHT_PLATOON
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The kernel bandwidths (σ). `make calibrate` fits each one to a median
# similarity of 0.50.
RBF_SIGMA_USAGE = 1.0000
RBF_SIGMA_AGGRESSION = 1.0500
RBF_SIGMA_PLATOON = 1.0200

# The EB prior strength, in games managed: a manager with EB_N_PRIOR games
# keeps half of his own rates. It is larger than the players' priors because a
# manager's decisions come only when the game offers the chance, so they
# settle more slowly.
EB_N_PRIOR = 30

# The minimum sample, in games managed. The profile computor flags a
# manager-season under it (below_minimum_sample), and the profile load skips it.
MIN_GAMES_MANAGED = 50


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class ManagerProfile:
    """One manager-season's feature vectors, sample size and EB weight."""

    manager_id: int
    season: int
    sample_games: int
    sample_starter_decisions: int  # the computor writes sample_games here (~1 a game)

    # The feature vectors. ``build`` shrinks them toward the league mean in
    # place. The normalizer z-scores them when the partition is built and when
    # a query arrives.
    usage_vec: NDArray[np.float64]  # shape (len(USAGE_FEATURES),)
    aggression_vec: NDArray[np.float64]  # shape (len(AGGRESSION_FEATURES),)
    platoon_vec: NDArray[np.float64]  # shape (len(PLATOON_FEATURES),)

    # Empirical Bayes: the weight the manager's own rates keep, n / (n + prior).
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One candidate in a query's output: the composite and its three sub-scores.

    The sub-scores are the raw kernels. Only ``score`` carries the pair
    confidence; ``usage_score``, the pitching-change draw's weight, does not.
    """

    manager_id: int
    season: int
    score: float
    usage_score: float
    aggression_score: float
    platoon_score: float
    sample_games: int


# ============================================================================
# EmpiricalBayesShrinkage, FeatureNormalizer
# ============================================================================


class EmpiricalBayesShrinkage:
    """Empirical-Bayes shrinkage: pull a rate vector toward the league mean.

    The manager's own values keep the weight α = n / (n + n_prior), with n in
    games managed; the league mean takes the rest. A NaN value takes the
    league mean.
    """

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n: int) -> float:
        return n / (n + self.n_prior)

    def shrink(self, raw: NDArray, avg: NDArray, n: int) -> NDArray:
        a = self.alpha(n)
        return a * np.where(np.isnan(raw), avg, raw) + (1.0 - a) * avg


@dataclass(slots=True)
class FeatureNormalizer:
    """Z-score normalizer fit on the population of manager profiles.

    Each group has its own mean and standard deviation per feature. A NaN
    z-scores to 0, the population mean. A feature with no spread (the two
    features that read 0.0 for everyone) keeps a standard deviation of 1.
    Before ``fit`` runs, a vector passes through unchanged.
    """

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
    """Every manager profile, with its normalized feature matrices, for
    vectorized scoring."""

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
        """Score the query against every profile, its own manager-season left out.

        The composite is the weighted sum of the three sub-scores, times the
        pair confidence sqrt(min(α_query, α_candidate)), clipped to [0, 1].
        """
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
    The manager-to-manager similarity engine.

    A query scores every manager-season above the minimum sample. The
    manager's other seasons are included: the same manager can differ from
    one season to another as his staff changes.

    Usage:
        engine = ManagerSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2023, 2024, 2025, 2026])
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

        # The cutoff every loaded profile declared. None when the table has no
        # asof_date column (a database before migration 0027) or every row's
        # stamp is NULL.
        self._asof_date = None

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the usage, aggression and platoon kernels from a calibration report.

        Only the sigmas change; the reliability weights stay at the module
        defaults. A sigma left at 0.0 keeps the kernel's current sigma. The
        calibration writes 0.0 when a group has no spread to fit, so a partial
        report changes only what it fitted.
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

        log.info(
            "ManagerSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            time.time() - t0,
        )

    def _load_league_averages(self, conn, seasons):
        """Read each season's league means, the shrinkage target.

        A feature the league profile lacks reads 0.0. Without the table, no
        profile is shrunk.
        """
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

        # asof_date is optional: a database before migration 0027 still builds,
        # and every row's stamp reads NULL.
        _present = duck_columns(conn, "derived", "manager_season_metrics")
        _asof_col = "msm.asof_date" if "asof_date" in _present else "NULL AS asof_date"

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
                msm.below_minimum_sample,
                {_asof_col}
            FROM derived.manager_season_metrics msm
            WHERE NOT msm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d manager profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff (checked after the loop).
        asof_values: set = set()

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
                asof_val,
            ) = row
            asof_values.add(asof_val)

            def _v(*vals):
                # A NULL becomes 0.0, not NaN, so the EB shrinkage does not
                # treat it as missing.
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

        # Refuse a mixed set (the point-in-time profiles, SIM-537). Profiles
        # built at two cutoffs, or one stamped and one not, are not comparable,
        # and nothing downstream shows it.
        if len(asof_values) > 1:
            raise RuntimeError(
                "manager profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Manager profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Shrink each profile toward its season's league mean, by games managed.

        A season with no league-average row is not shrunk.
        """
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
        """Every other manager-season, sorted by the composite, descending.

        ``n`` keeps the top ``n``. An unknown manager-season returns an empty
        list.
        """
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
        """Score one manager-season against another; None when either is missing."""
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

    @property
    def asof_date(self):
        """The cutoff every loaded profile declared, or ``None``."""
        return self._asof_date


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
