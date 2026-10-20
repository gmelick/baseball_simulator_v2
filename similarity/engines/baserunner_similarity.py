"""The extra-base runner engine: how alike two runner-seasons are on the bases.

The engine scores a pair of runner-seasons from 0 (unlike) to 1 (identical).
The advancement draws weight their rows by this score (the actor matrix
``runner_adv``). The score blends three sub-scores:

  1. Speed (35%): Statcast sprint speed. It barely moves within a season,
     and it is the strongest single predictor of a successful extra base.

  2. Aggression (40%): how often the runner TRIES for the extra base. The
     group holds the overall attempt rate, the attempt rate in each of four
     situations (first to third on a single, second to home on a single,
     first to home on a double, the tag-up on a fly ball), the stop rate and
     Savant's attempt rate above expectation. An attempt rate shows the
     runner's decisions apart from their result.

  3. Success (25%): how often an attempt succeeds. It shows execution: the
     read, the turn and the slide. It rests on attempts, which rest on
     chances, so it is the noisiest group and carries the lowest weight.

Each sub-score is a Gaussian kernel (RBF): score = exp(-d² / 2σ²), where d²
is the weighted mean squared difference of the group's z-scored features (a
z-score is the value minus the population mean, over the population standard
deviation). Each feature's reliability weight sets its share of d².

Before scoring, each profile shrinks toward its season's league-average row
by empirical-Bayes (EB) shrinkage: the runner keeps the share α = n / (n + 15)
of his own value, where n is his count of extra-base chances. The composite
is then multiplied by the square root of the lower α of the pair, so a thin
profile scores low against everyone.

The engine compares every runner with every other; it does not split runners
by speed tier. The kernel already puts a 31 ft/s runner far from a 25 ft/s one.

This engine keeps its own masked kernel (``WeightedRBFSimilarity`` below): a
feature missing on either side drops out of the pair's distance. The shared
kernel in ``similarity/engines/rbf_kernel.py`` reads a missing difference as
zero instead.

Usage
-----
  engine = BaserunnerSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
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

from db.introspect import duck_columns
from similarity.similarity_diagnostics import run_baserunner_diagnostics

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
log = logging.getLogger("baserunner_similarity")


# ============================================================================
# Features and weights
# ============================================================================

# --- Speed ---
# One feature, so its reliability weight normalizes to 1.0.
SPEED_FEATURES = [
    # feature_name,           reliability_weight
    ("sprint_speed", 0.202),  # nearly constant within a season
]

# --- Aggression: the attempt rates and the stop rate ---
# The per-situation attempt rates show the runner's tendency in each spot.
# stop_rate is 1 minus the overall attempt rate, so it repeats the overall
# rate; both carry 0.202.
AGGRESSION_FEATURES = [
    ("extra_base_attempt_rate", 0.202),  # overall attempt rate: the largest sample
    ("first_to_third_attempt_rate", 0.167),  # a common situation
    ("second_to_home_attempt_rate", 0.100),
    ("first_to_home_attempt_rate", 0.112),  # a rarer situation: noisier
    ("tag_up_attempt_rate", 0.100),  # the rarest situation: the noisiest
    ("stop_rate", 0.202),  # 1 minus the overall attempt rate
    # How often the runner tried for the extra base ABOVE how often a typical
    # runner would have tried in the same chances. Savant's baserunning board
    # supplies the expectation. It repeats year to year at 0.75-0.77, against
    # 0.50-0.59 for the raw rate; the weight is that repeat (the lead-distance
    # work, SIM-531). NaN when the runner has no Savant row: the masked kernel
    # then drops it from the distance and from the weight total.
    ("xb_attempt_rate_above_expected", 0.760),
]

# --- Success ---
# How often an attempt succeeded. Noisier, because it rests on the attempts.
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

# The kernel bandwidths σ (gamma = 1 / (2σ²)). ``make calibrate`` fits them for
# the app's engine (similarity/calibration/). The actor matrix builder does not
# read the calibration report, so the matrices use these values.
RBF_SIGMA_SPEED = 0.8171
RBF_SIGMA_AGGRESSION = 1.0478
RBF_SIGMA_SUCCESS = 1.0000

# The EB prior strength: at EB_N_PRIOR extra-base chances, α = 0.5.
EB_N_PRIOR = 15

#: The features whose NULL loads as NaN (unmeasured), never 0.0, in the profile
#: and in the league-average row. Their NaN survives the shrinkage, and the
#: masked kernel drops them from the pair.
_NAN_WHEN_MISSING = frozenset({"xb_attempt_rate_above_expected"})
_NAN_WHEN_MISSING_MASK = np.array(
    [f in _NAN_WHEN_MISSING for f, _ in AGGRESSION_FEATURES], dtype=bool
)

# The computor's minimum of extra-base chances. The engine does not read this
# constant: the loader filters on the table's below_minimum_sample flag.
MIN_ADVANCEMENT_OPPS = 20


# ============================================================================
# Data structures
# ============================================================================


@dataclass(slots=True)
class BaserunnerProfile:
    """One runner-season's extra-base profile."""

    player_id: int
    season: int
    sample_advancement_opps: int

    # The feature vectors: raw at load, shrunk by ``build``, z-scored when scored.
    speed_vec: NDArray[np.float64]  # shape (len(SPEED_FEATURES),)
    aggression_vec: NDArray[np.float64]  # shape (len(AGGRESSION_FEATURES),)
    success_vec: NDArray[np.float64]  # shape (len(SUCCESS_FEATURES),)

    # The chances in each situation. The engine stores them; no score reads them.
    sample_first_to_third_opps: int = 0
    sample_second_to_home_opps: int = 0
    sample_first_to_home_opps: int = 0
    sample_tag_up_opps: int = 0

    # The EB sample confidence α, over the extra-base chances.
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
# The masked kernel
# ============================================================================


class WeightedRBFSimilarity:
    """A Gaussian kernel (RBF) with per-feature reliability weights and a mask.

        K(x, y) = exp(-γ · Σ_i w_i · (x_i − y_i)² / Σ_i w_i),   γ = 1 / (2σ²)

    The sums run over the features both sides have. A feature missing on
    either side (NaN) drops out of the distance AND of the weight total, so a
    half-measured pair is not pulled toward similarity, as it would be if a
    missing feature read as an exact match. The weights sum to 1.0, so the
    exponent is the weighted AVERAGE squared distance per feature: a
    sub-score's typical score does not depend on how many features it has.

    A pair with no feature in common scores 1.0 (an empty distance). No
    sub-score here reaches that case: every group has a feature that is never
    NaN.
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
        present = np.isfinite(diff)
        d2 = np.where(present, diff * diff, 0.0)
        wsum = float((self.weights * present).sum())
        dist_sq = float((self.weights * d2).sum()) / max(wsum, 1e-12)
        return float(np.exp(-self.gamma * dist_sq))

    def score_batch(
        self,
        query: NDArray,
        candidates: NDArray,
    ) -> NDArray[np.float64]:
        diff = candidates - query[np.newaxis, :]
        present = np.isfinite(diff)
        d2 = np.where(present, diff * diff, 0.0)
        wsum = (self.weights[np.newaxis, :] * present).sum(axis=1)
        dist_sq = (self.weights[np.newaxis, :] * d2).sum(axis=1) / np.maximum(wsum, 1e-12)
        return np.exp(-self.gamma * dist_sq)


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

        A NaN raw value becomes the league mean. A NaN league value leaves the
        raw value alone. ``_apply_shrinkage`` then puts back the NaN of an
        unmeasured Savant feature, so the masked kernel drops it. A runner is
        never scored as exactly average on a feature nobody measured.
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
    """The z-score of each feature, fit on the population of runner profiles."""

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
            # A column no profile measured (the Savant feature on a database
            # without the Savant load) gets mean 0 and standard deviation 1,
            # so the normalizer never emits a NaN statistic.
            measured = np.isfinite(mat).any(axis=0)
            m = np.zeros(mat.shape[1], dtype=np.float64)
            s = np.ones(mat.shape[1], dtype=np.float64)
            if measured.any():
                m[measured] = np.nanmean(mat[:, measured], axis=0)
                s[measured] = np.nanstd(mat[:, measured], axis=0)
            s[s == 0] = 1.0
            return m, s

        self.speed_mean, self.speed_std = _fit_group([p.speed_vec for p in profiles])
        self.aggression_mean, self.aggression_std = _fit_group([p.aggression_vec for p in profiles])
        self.success_mean, self.success_std = _fit_group([p.success_vec for p in profiles])

    def _normalize(self, vec: NDArray, mean: NDArray | None, std: NDArray | None) -> NDArray:
        if mean is None:
            return vec
        # A NaN stays NaN: the masked kernel drops a missing feature from the
        # distance instead of reading it as the mean.
        return (vec - mean) / std

    def normalize_speed(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.speed_mean, self.speed_std)

    def normalize_aggression(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.aggression_mean, self.aggression_std)

    def normalize_success(self, vec: NDArray) -> NDArray:
        return self._normalize(vec, self.success_mean, self.success_std)


# ============================================================================
# Scoring partition
# ============================================================================


class BaserunnerPartition:
    """Every runner profile, with its z-scored feature matrices for scoring a
    query against all of them at once."""

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

        q_speed = normalizer.normalize_speed(query.speed_vec)
        q_agg = normalizer.normalize_aggression(query.aggression_vec)
        q_success = normalizer.normalize_success(query.success_vec)

        speed_scores = speed_rbf.score_batch(q_speed, self._speed_matrix)
        agg_scores = agg_rbf.score_batch(q_agg, self._aggression_matrix)
        success_scores = success_rbf.score_batch(q_success, self._success_matrix)

        composite = (
            WEIGHT_SPEED * speed_scores
            + WEIGHT_AGGRESSION * agg_scores
            + WEIGHT_SUCCESS * success_scores
        )

        # The confidence discount: the square root of the pair's lower α.
        pair_confidence = np.minimum(query.eb_alpha, self._eb_alphas)
        composite *= np.sqrt(pair_confidence)
        composite = np.clip(composite, 0.0, 1.0)

        # Every profile but the query's own.
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
# The engine
# ============================================================================


class BaserunnerSimilarityEngine:
    """The extra-base runner engine.

    A query scores against every loaded runner-season, other seasons included.

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

        # The cutoff (asof_date) every loaded profile declared. None when the
        # table has no asof_date column or every row's value is NULL.
        self._asof_date = None

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
    # Calibration
    # ------------------------------------------------------------------

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the speed, aggression and success kernels from a fitted report.

        A sigma left at its 0.0 default keeps the engine's current sigma. A
        weight list left at None, or one of the wrong length, gives the module's
        weights. The call swaps only the query-time kernels, so it is safe
        before or after ``build``.
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        def _wts(field: str, default: list[float]) -> NDArray[np.float64]:
            w = getattr(report, field, None)
            if w is None:
                return np.array(default)
            arr = np.asarray(w, dtype=np.float64)
            # A report fitted on a different feature list would fail to
            # broadcast on the first query. Keep the module weights until
            # ``make calibrate`` refits the report.
            if arr.shape != (len(default),):
                log.warning(
                    "SIM-531: %s holds %d weights but the engine has %d features; "
                    "keeping the module defaults until the report is refitted.",
                    field,
                    arr.size,
                    len(default),
                )
                return np.array(default)
            return arr

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

    def build(
        self, seasons: list[int] | None = None, *, include_below_minimum: bool = False
    ) -> None:
        """Load the runner profiles, shrink them and build the scoring matrices.

        ``include_below_minimum`` also loads the profiles below the minimum
        sample; their low α shrinks their scores. The actor matrix builder
        sets it, so the matrix covers every runner-season the pools hold."""
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

            # ``xb_attempt_rate_above_expected`` is NaN when the league row lacks
            # it (a database without the Savant load), so the shrinkage leaves
            # the runner's raw value, or his NaN, alone. Every other missing
            # key reads 0.0.
            self._league_avg["aggression"][season] = np.array(
                [
                    (np.nan if pj.get(f) is None else float(pj[f]))
                    if f in _NAN_WHEN_MISSING
                    else (pj.get(f, 0.0) or 0.0)
                    for f, _ in AGGRESSION_FEATURES
                ],
                dtype=np.float64,
            )

            self._league_avg["success"][season] = np.array(
                [pj.get(f, 0.0) or 0.0 for f, _ in SUCCESS_FEATURES], dtype=np.float64
            )

        log.info("Loaded baserunner league averages for %d seasons.", len(rows))

    def _load_profiles(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
        include_below_minimum: bool = False,
    ) -> None:
        season_filter = ""
        if seasons:
            sl = ", ".join(str(s) for s in seasons)
            season_filter = f"AND brm.season IN ({sl})"
        min_filter = "TRUE" if include_below_minimum else "NOT brm.below_minimum_sample"

        # A table without the asof_date column (a database before DuckDB
        # migration 0028) still builds: the cutoff reads NULL on every row.
        _present = duck_columns(conn, "derived", "baserunner_season_metrics")
        _asof_col = "brm.asof_date" if "asof_date" in _present else "NULL AS asof_date"
        # The Savant column arrives with DuckDB migration 0029; an older table
        # reads NULL.
        _xb_col = (
            "brm.xb_attempt_rate_above_expected"
            if "xb_attempt_rate_above_expected" in _present
            else "NULL AS xb_attempt_rate_above_expected"
        )

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
                brm.below_minimum_sample,
                {_asof_col},
                -- SIM-531: the attempt rate above expectation (NULL = no row)
                {_xb_col}
            FROM derived.baserunner_season_metrics brm
            WHERE {min_filter}
              {season_filter}
        """).fetchall()

        log.info("Loading %d baserunner profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff.
        asof_values: set = set()

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
                asof_val,
                xb_above,
            ) = row
            asof_values.add(asof_val)

            def _v(vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            # ``_v`` reads NULL as 0.0. The Savant feature reads NULL as NaN
            # (unmeasured): its 0.0 means "exactly as aggressive as
            # expected", a real reading.
            aggression = np.append(
                _v([eb_attempt, ftt_attempt, sth_attempt, fth_attempt, tu_attempt, stop]),
                np.nan if xb_above is None else float(xb_above),
            )
            self._profiles[(player_id, season)] = BaserunnerProfile(
                player_id=player_id,
                season=season,
                sample_advancement_opps=sample_adv or 0,
                speed_vec=_v([sprint_speed]),
                aggression_vec=aggression,
                success_vec=_v([eb_success, ftt_success, sth_success, fth_success, tu_success]),
                sample_first_to_third_opps=ftt_opps or 0,
                sample_second_to_home_opps=sth_opps or 0,
                sample_first_to_home_opps=fth_opps or 0,
                sample_tag_up_opps=tu_opps or 0,
                eb_alpha=self._shrinkage.alpha(sample_adv or 0),
                below_minimum=bool(below_min),
            )

        # Refuse profiles built at two cutoffs, or one stamped and one not.
        # They are not comparable, and nothing downstream shows the mix.
        if len(asof_values) > 1:
            raise RuntimeError(
                "baserunner profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Baserunner profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Pull every profile toward its season's league-average row on the
        runner's extra-base chances. A season with no league row stays raw.

        An unmeasured ``xb_attempt_rate_above_expected`` (NaN: no Savant row)
        stays NaN, whatever the league row carries. The masked kernel then
        drops it from that runner's pairs, and the normalizer's statistics
        come from the measured runners alone. A measured value shrinks like
        every other feature.
        """
        for _key, p in self._profiles.items():
            s = p.season
            for group, vec_attr in [
                ("speed", "speed_vec"),
                ("aggression", "aggression_vec"),
                ("success", "success_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is None:
                    continue
                raw = getattr(p, vec_attr)
                shrunk = self._shrinkage.shrink(raw, avg, p.sample_advancement_opps)
                if group == "aggression":
                    unmeasured = _NAN_WHEN_MISSING_MASK & np.isnan(raw)
                    if unmeasured.any():
                        shrunk = np.where(unmeasured, np.nan, shrunk)
                setattr(p, vec_attr, shrunk)

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
        Score the query runner against every runner-season profile.

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
        """The score of runner-season ``runner_b`` against ``runner_a``."""
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
        """The composite and the three sub-scores of one pair."""
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
    engine = BaserunnerSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_baserunner_diagnostics(engine, n_query_samples=50)
    print(report)
