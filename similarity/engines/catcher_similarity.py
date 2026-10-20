"""The catcher engine: how alike two catcher-seasons are behind the plate.

The engine scores a pair of catcher-seasons from 0 (unlike) to 1 (identical).
The actor matrix ``catcher`` holds the composite. The steal and pickoff draws
weight their rows by the throwing sub-score (the matrix ``catcher_throwing``).
The composite blends four defensive sub-scores:

  1. Framing (≈52.9%): the catcher skill with the largest effect and the best
     measure. Pitch framing is worth several wins a season to an elite
     framer. The group holds the called strikes above the framing model's
     expectation per called pitch (the cleanest signal), the framing runs
     (the size of the effect), and the strike rates in the shadow zone (the
     border of the zone) and in the heart of the zone.

  2. Blocking (≈23.5%): the passed balls and wild pitches the catcher
     prevents. The group holds his blocks above average per expected passed
     ball or wild pitch, the same on bounced pitches alone, and his passed
     balls plus wild pitches per called pitch.

  3. Throwing execution (≈14.1%): the throw once the runner goes. The group
     holds pop time, the caught-stealing rate and arm strength. The result
     field is ``throwing_score``.

  4. Deterrence (≈9.4%): how often runners try against him, as steal
     attempts per plate appearance with a runner on first or second. Runners
     rarely test an elite arm, so his caught-stealing rate rests on few
     attempts, and the execution sub-score alone hides his effect.

The weights keep the ratio 45 : 20 : 12 : 8, divided by 0.85 so they sum to
one. Framing leads because it is the largest and best-measured skill. The
catcher's own batting is not a feature: it does not belong in a defensive
score.

Each sub-score is a Gaussian kernel (RBF, ``similarity/engines/rbf_kernel.py``):
score = exp(-d² / 2σ²) over the group's z-scored features (a z-score is the
value minus the population mean, over the population standard deviation).
The reliability weights follow how fast each measure stabilizes: framing by
about 1,500 pitches received, blocking by about 50 chances, pop time by about
20 steal attempts against, the caught-stealing rate by 40 or more attempts.

Before scoring, each profile shrinks toward its season's league-average row
by empirical-Bayes (EB) shrinkage: the catcher keeps the share
α = n / (n + 15) of his own value, where n is his count of called pitches.
The composite is then multiplied by the square root of the lower α of the
pair.

The engine compares every catcher with every other; it does not split
catchers by throwing hand. Framing and blocking do not depend on the hand.

Usage
-----
  engine = CatcherSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(catcher_id=572816, season=2025, n=20)
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
log = logging.getLogger("catcher_similarity")


# ============================================================================
# Features and weights
# ============================================================================

# --- Framing ---
# The called strikes above expectation lead. The framing runs add the size of
# the effect: an elite framer has a high rate AND a large volume.
FRAMING_FEATURES = [
    # feature_name,                   reliability_weight
    ("strike_rate_vs_expected", 0.900),  # called strikes above the model, per called pitch
    ("runs_saved_framing", 0.600),  # the framing runs
    ("shadow_zone_strike_rate", 0.700),  # the zone's border: the most telling area
    ("heart_zone_strike_rate", 0.400),  # the heart: every catcher gets these (less signal)
]

# --- Blocking ---
# PB is a passed ball, WP a wild pitch. A block above average is one expected
# PB or WP that did not happen.
BLOCKING_FEATURES = [
    ("block_success_rate", 0.700),  # blocks above average per expected PB + WP
    ("passed_ball_rate_per_9", 0.600),  # PB + WP per called pitch (not per 9 innings)
    ("wild_pitch_prevention_rate", 0.500),  # blocks above average on bounced pitches; same divisor
]

# --- Throwing execution: the throw itself ---
# The result field of this sub-score is ``throwing_score``. Exchange time is
# not a feature: the feed does not separate it from pop time.
THROWING_FEATURES = [
    ("pop_time_avg", 0.900),  # seconds from the catch to the glove at second; the most stable
    ("cs_rate", 0.600),  # caught-stealing rate; rests on the attempts
    ("arm_strength_mph", 0.800),  # throw speed; very stable (physical)
]

# --- Deterrence: do runners even try? ---
# One feature, so its reliability weight normalizes to 1.0. The bandwidth
# RBF_SIGMA_DETERRENCE sets the scale.
DETERRENCE_FEATURES = [
    ("steal_attempt_rate_against", 1.000),  # attempts per PA with a runner on first or second
]

# --- Sub-score weights ---
# The ratio 45 : 20 : 12 : 8, divided by 0.85 so the four sum to one.
WEIGHT_FRAMING = 0.45 / 0.85  # ≈ 0.5294
WEIGHT_BLOCKING = 0.20 / 0.85  # ≈ 0.2353
WEIGHT_THROWING = 0.12 / 0.85  # ≈ 0.1412 (the throw itself)
WEIGHT_DETERRENCE = 0.08 / 0.85  # ≈ 0.0941 (how often runners try)
_TOTAL = WEIGHT_FRAMING + WEIGHT_BLOCKING + WEIGHT_THROWING + WEIGHT_DETERRENCE
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# The kernel bandwidths σ (gamma = 1 / (2σ²)).
RBF_SIGMA_FRAMING = 0.9200
RBF_SIGMA_BLOCKING = 1.0500
RBF_SIGMA_THROWING = 0.9800
RBF_SIGMA_DETERRENCE = 1.0000  # one standard deviation in z-score units

# The EB prior strength: at EB_N_PRIOR called pitches, α = 0.5. The value is
# the fielder engine's prior. Over called pitches it shrinks little: α is
# already 0.95 at 300.
EB_N_PRIOR = 15

# The engine does not read this constant. The loader keeps the rows the table
# does not flag below_minimum_sample; the computor flags a catcher who faced
# fewer than 10 steal attempts.
MIN_PITCHES_RECEIVED = 300


# ============================================================================
# Data structures
# ============================================================================


@dataclass(slots=True)
class CatcherProfile:
    """One catcher-season's defensive profile."""

    catcher_id: int
    season: int
    sample_pitches_received: int  # the table's count of called pitches
    sample_block_opps: int  # the loader fills it with the same count
    sample_steal_attempts_against: int

    # The feature vectors: raw at load, shrunk by ``build``, z-scored when scored.
    framing_vec: NDArray[np.float64]  # shape (len(FRAMING_FEATURES),)
    blocking_vec: NDArray[np.float64]  # shape (len(BLOCKING_FEATURES),)
    throwing_vec: NDArray[np.float64]  # shape (len(THROWING_FEATURES),)
    # None when the table's deterrence rate is NULL, until ``_apply_shrinkage``
    # fills it from the league row.
    deterrence_vec: NDArray[np.float64] | None = None  # shape (len(DETERRENCE_FEATURES),)

    # The EB sample confidence α, over the called pitches.
    eb_alpha: float = 1.0
    below_minimum: bool = False


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """One entry in the similarity query output."""

    catcher_id: int
    season: int
    score: float
    framing_score: float
    blocking_score: float
    throwing_score: float  # the throw itself
    deterrence_score: float  # how often runners try
    sample_pitches_received: int


# Another name for SimilarityResult; the tests import it.
CatcherSimilarityResult = SimilarityResult


# ============================================================================
# Empirical-Bayes shrinkage and feature normalization
# ============================================================================


class EmpiricalBayesShrinkage:
    """EB shrinkage toward the league mean: the catcher keeps the share
    α = n / (n + n_prior) of his own value."""

    def __init__(self, n_prior: int = EB_N_PRIOR) -> None:
        self.n_prior = n_prior

    def alpha(self, n_samples: int) -> float:
        return n_samples / (n_samples + self.n_prior)

    def shrink(self, raw: NDArray, avg: NDArray, n: int) -> NDArray:
        """Pull ``raw`` toward ``avg`` by ``alpha(n)``. A NaN raw value
        becomes the league mean."""
        a = self.alpha(n)
        return a * np.where(np.isnan(raw), avg, raw) + (1.0 - a) * avg


@dataclass(slots=True)
class FeatureNormalizer:
    """The z-score of each feature, fit on the population of catcher profiles.
    A NaN z-score reads as 0, the population mean."""

    framing_mean: NDArray | None = None
    framing_std: NDArray | None = None
    blocking_mean: NDArray | None = None
    blocking_std: NDArray | None = None
    throwing_mean: NDArray | None = None
    throwing_std: NDArray | None = None
    deterrence_mean: NDArray | None = None
    deterrence_std: NDArray | None = None

    def fit(self, profiles: list[CatcherProfile]) -> None:
        if not profiles:
            return

        def _fit(vecs):
            mat = np.array(vecs, dtype=np.float64)
            m = np.nanmean(mat, axis=0)
            s = np.nanstd(mat, axis=0)
            s[s == 0] = 1.0
            return m, s

        self.framing_mean, self.framing_std = _fit([p.framing_vec for p in profiles])
        self.blocking_mean, self.blocking_std = _fit([p.blocking_vec for p in profiles])
        self.throwing_mean, self.throwing_std = _fit([p.throwing_vec for p in profiles])

        # A deterrence vector can be None (see ``_apply_shrinkage``). The fit
        # uses the profiles that carry one, and runs only when one does.
        det_vecs = [p.deterrence_vec for p in profiles if p.deterrence_vec is not None]
        if det_vecs:
            self.deterrence_mean, self.deterrence_std = _fit(det_vecs)

    def _norm(self, v, m, s):
        if m is None:
            return v
        return np.nan_to_num((v - m) / s, nan=0.0)

    def normalize_framing(self, v):
        return self._norm(v, self.framing_mean, self.framing_std)

    def normalize_blocking(self, v):
        return self._norm(v, self.blocking_mean, self.blocking_std)

    def normalize_throwing(self, v):
        return self._norm(v, self.throwing_mean, self.throwing_std)

    def normalize_deterrence(self, v):
        return self._norm(v, self.deterrence_mean, self.deterrence_std)


# ============================================================================
# Scoring partition
# ============================================================================


class CatcherPartition:
    """Every catcher profile, with its z-scored feature matrices for scoring a
    query against all of them at once."""

    def __init__(self) -> None:
        self.profiles: list[CatcherProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._framing_mat: NDArray | None = None
        self._blocking_mat: NDArray | None = None
        self._throwing_mat: NDArray | None = None
        self._deterrence_mat: NDArray | None = None
        self._eb_alphas: NDArray | None = None

    @staticmethod
    def _zero_vec(dim: int) -> NDArray[np.float64]:
        return np.zeros(dim, dtype=np.float64)

    def build(self, profiles: list[CatcherProfile], norm: FeatureNormalizer) -> None:
        self.profiles = profiles
        self.keys = [(p.catcher_id, p.season) for p in profiles]
        if not profiles:
            return
        self._framing_mat = np.array([norm.normalize_framing(p.framing_vec) for p in profiles])
        self._blocking_mat = np.array([norm.normalize_blocking(p.blocking_vec) for p in profiles])
        self._throwing_mat = np.array([norm.normalize_throwing(p.throwing_vec) for p in profiles])
        # A profile with no deterrence vector sits at 0 in z-score units: the
        # population mean.
        det_dim = len(DETERRENCE_FEATURES)
        self._deterrence_mat = np.array(
            [
                norm.normalize_deterrence(p.deterrence_vec)
                if p.deterrence_vec is not None
                else self._zero_vec(det_dim)
                for p in profiles
            ]
        )
        self._eb_alphas = np.array([p.eb_alpha for p in profiles], dtype=np.float64)

    def score_all(
        self,
        query: CatcherProfile,
        norm: FeatureNormalizer,
        framing_rbf: WeightedRBFSimilarity,
        blocking_rbf: WeightedRBFSimilarity,
        throwing_rbf: WeightedRBFSimilarity,
        deterrence_rbf: WeightedRBFSimilarity,
    ) -> list[SimilarityResult]:
        if not self.profiles:
            return []

        query_key = (query.catcher_id, query.season)

        det_dim = len(DETERRENCE_FEATURES)
        q_det = (
            norm.normalize_deterrence(query.deterrence_vec)
            if query.deterrence_vec is not None
            else self._zero_vec(det_dim)
        )

        fr_scores = framing_rbf.score_batch(
            norm.normalize_framing(query.framing_vec), self._framing_mat
        )
        bl_scores = blocking_rbf.score_batch(
            norm.normalize_blocking(query.blocking_vec), self._blocking_mat
        )
        th_scores = throwing_rbf.score_batch(
            norm.normalize_throwing(query.throwing_vec), self._throwing_mat
        )
        det_scores = deterrence_rbf.score_batch(q_det, self._deterrence_mat)

        composite = (
            WEIGHT_FRAMING * fr_scores
            + WEIGHT_BLOCKING * bl_scores
            + WEIGHT_THROWING * th_scores
            + WEIGHT_DETERRENCE * det_scores
        )

        pair_conf = np.minimum(query.eb_alpha, self._eb_alphas)
        composite = np.clip(composite * np.sqrt(pair_conf), 0.0, 1.0)

        return [
            SimilarityResult(
                catcher_id=self.profiles[i].catcher_id,
                season=self.profiles[i].season,
                score=float(composite[i]),
                framing_score=float(fr_scores[i]),
                blocking_score=float(bl_scores[i]),
                throwing_score=float(th_scores[i]),
                deterrence_score=float(det_scores[i]),
                sample_pitches_received=self.profiles[i].sample_pitches_received,
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


# ============================================================================
# The engine
# ============================================================================


class CatcherSimilarityEngine:
    """The catcher engine.

    A query scores against every loaded catcher-season. ``build`` loads the
    profiles the table does not flag below its minimum sample.

    Usage:
        engine = CatcherSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2022, 2023, 2024, 2025])
        results = engine.query(catcher_id=572816, season=2025)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._profiles: dict[tuple[int, int], CatcherProfile] = {}
        self._league_avg: dict[str, dict[int, NDArray]] = {
            "framing": {},
            "blocking": {},
            "throwing": {},
            "deterrence": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = CatcherPartition()

        # The cutoff (asof_date) every loaded profile declared. None when the
        # table has no asof_date column or every row's value is NULL.
        self._asof_date = None

        self._framing_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_FRAMING, np.array([w for _, w in FRAMING_FEATURES])
        )
        self._blocking_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_BLOCKING, np.array([w for _, w in BLOCKING_FEATURES])
        )
        self._throwing_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_THROWING, np.array([w for _, w in THROWING_FEATURES])
        )
        self._deterrence_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_DETERRENCE, np.array([w for _, w in DETERRENCE_FEATURES])
        )

    def apply_calibration(self, report: CalibrationReport) -> None:
        """Rebuild the four kernels from a fitted report.

        A sigma left at its 0.0 default keeps the engine's current sigma. The
        reliability weights stay the module's stabilization priors: the report
        fits only the sigmas.
        """

        def _sig(field: str, current: float) -> float:
            v = float(getattr(report, field, 0.0) or 0.0)
            return v if v > 0.0 else current

        self._framing_rbf = WeightedRBFSimilarity(
            _sig("sigma_catcher_framing", self._framing_rbf.sigma),
            np.array([w for _, w in FRAMING_FEATURES]),
        )
        self._blocking_rbf = WeightedRBFSimilarity(
            _sig("sigma_catcher_blocking", self._blocking_rbf.sigma),
            np.array([w for _, w in BLOCKING_FEATURES]),
        )
        self._throwing_rbf = WeightedRBFSimilarity(
            _sig("sigma_catcher_throwing", self._throwing_rbf.sigma),
            np.array([w for _, w in THROWING_FEATURES]),
        )
        self._deterrence_rbf = WeightedRBFSimilarity(
            _sig("sigma_catcher_deterrence", self._deterrence_rbf.sigma),
            np.array([w for _, w in DETERRENCE_FEATURES]),
        )
        log.info(
            "SIM-406: applied calibration to CatcherSimilarityEngine "
            "(sigma_framing=%.4f, sigma_blocking=%.4f, sigma_throwing=%.4f, sigma_deterrence=%.4f).",
            self._framing_rbf.sigma,
            self._blocking_rbf.sigma,
            self._throwing_rbf.sigma,
            self._deterrence_rbf.sigma,
        )

    def build(self, seasons: list[int] | None = None) -> None:
        """Load the catcher profiles, shrink them and build the scoring matrices."""
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
            "CatcherSimilarityEngine built: %d profiles in %.2fs.",
            len(self._profiles),
            time.time() - t0,
        )

    def _load_league_averages(self, conn, seasons):
        try:
            sf = f"AND season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
            rows = conn.execute(f"""
                SELECT season, profile_json FROM derived.league_averages
                WHERE entity_type = 'catcher' {sf}
            """).fetchall()
        except duckdb.CatalogException:
            log.warning("derived.league_averages not found — skipping shrinkage fallbacks.")
            return

        for season, pj_raw in rows:
            pj = json.loads(pj_raw) if isinstance(pj_raw, str) else pj_raw
            for group, features in [
                ("framing", FRAMING_FEATURES),
                ("blocking", BLOCKING_FEATURES),
                ("throwing", THROWING_FEATURES),
                ("deterrence", DETERRENCE_FEATURES),
            ]:
                self._league_avg[group][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in features], dtype=np.float64
                )

    def _load_profiles(self, conn, seasons):
        sf = f"AND csm.season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""

        # A table without the asof_date column (a database before DuckDB
        # migration 0028) still builds: the cutoff reads NULL on every row.
        _present = duck_columns(conn, "derived", "catcher_season_metrics")
        _asof_col = "csm.asof_date" if "asof_date" in _present else "NULL AS asof_date"

        # The query derives the strike rate above expectation and the three
        # blocking rates from the table's counts. It reads every other feature
        # as stored.
        rows = conn.execute(f"""
            SELECT
                csm.player_id                                                 AS catcher_id,
                csm.season,
                COALESCE(csm.pitches_received_total, 0)                       AS sample_pitches_received,
                COALESCE(csm.pitches_received_total, 0)                       AS sample_block_opps,
                COALESCE(csm.sb_attempts_faced, 0)                            AS sample_steal_attempts_against,
                -- Framing (4) — derived from counts + zone rates
                (csm.called_strikes - csm.expected_called_strikes)
                    / NULLIF(csm.called_pitches, 0)                           AS strike_rate_vs_expected,
                csm.framing_runs                                              AS runs_saved_framing,
                csm.shadow_zone_strike_rate,
                csm.heart_zone_strike_rate,
                -- Blocking (3) — derived from the PB/WP block-model outputs
                csm.blocks_above_average / NULLIF(csm.expected_pbwp, 0)       AS block_success_rate,
                -- per-received-pitch PB/WP rate (z-normalized by the RBF; the
                -- '_per_9' name is historical — the signal is a passed-ball rate)
                csm.actual_pbwp * 1.0 / NULLIF(csm.pitches_received_total, 0)  AS passed_ball_rate_per_9,
                COALESCE(csm.blocks_aa_bounced, 0)
                    / NULLIF(csm.expected_pbwp, 0)                            AS wild_pitch_prevention_rate,
                -- Throwing — Execution (3); exchange_time TRIMmed
                csm.pop_time_mean                                             AS pop_time_avg,
                csm.cs_rate,
                csm.arm_strength_mean                                         AS arm_strength_mph,
                -- Throwing — Deterrence (1, SIM-073)
                csm.steal_attempt_rate_against,
                csm.below_minimum_sample,
                {_asof_col}
            FROM derived.catcher_season_metrics csm
            WHERE NOT csm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d catcher profiles from DuckDB …", len(rows))

        # Every profile must declare the same cutoff.
        asof_values: set = set()

        for row in rows:
            (
                cid,
                season,
                n_pitches,
                n_block_opps,
                n_steal_against,
                strike_vs_exp,
                runs_framing,
                shadow_sr,
                heart_sr,
                block_sr,
                pb_rate,
                wp_prev,
                pop_time,
                cs_rate,
                arm_mph,
                steal_attempt_rate_against,
                below_min,
                asof_val,
            ) = row
            asof_values.add(asof_val)

            # NULL reads as 0.0.
            def _v(*vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            # The deterrence vector is None when the rate is NULL: the computor
            # writes NULL below 100 plate appearances with a runner on first
            # or second. ``_apply_shrinkage`` then fills it from the league row.
            deterrence_vec = (
                _v(steal_attempt_rate_against) if steal_attempt_rate_against is not None else None
            )

            self._profiles[(cid, season)] = CatcherProfile(
                catcher_id=cid,
                season=season,
                sample_pitches_received=n_pitches or 0,
                sample_block_opps=n_block_opps or 0,
                sample_steal_attempts_against=n_steal_against or 0,
                framing_vec=_v(strike_vs_exp, runs_framing, shadow_sr, heart_sr),
                blocking_vec=_v(block_sr, pb_rate, wp_prev),
                throwing_vec=_v(pop_time, cs_rate, arm_mph),
                deterrence_vec=deterrence_vec,
                eb_alpha=self._shrinkage.alpha(n_pitches or 0),
                below_minimum=bool(below_min),
            )

        # Refuse profiles built at two cutoffs, or one stamped and one not.
        if len(asof_values) > 1:
            raise RuntimeError(
                "catcher profiles were built at different cutoffs "
                f"({sorted(str(v) for v in asof_values)}). Rebuild them all at one "
                "date before scoring."
            )
        self._asof_date = next(iter(asof_values), None)
        if self._asof_date is not None:
            log.info("Catcher profiles are as of %s.", self._asof_date)

    def _apply_shrinkage(self) -> None:
        """Pull every profile toward its season's league-average row on the
        catcher's called pitches. A season with no league row stays raw."""
        for p in self._profiles.values():
            s = p.season
            for group, attr in [
                ("framing", "framing_vec"),
                ("blocking", "blocking_vec"),
                ("throwing", "throwing_vec"),
                ("deterrence", "deterrence_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is None:
                    continue
                current = getattr(p, attr)
                # A missing deterrence vector takes the league row's value.
                if current is None:
                    setattr(p, attr, avg.copy())
                    continue
                setattr(
                    p,
                    attr,
                    self._shrinkage.shrink(
                        current,
                        avg,
                        p.sample_pitches_received,
                    ),
                )

    def query(
        self,
        catcher_id: int,
        season: int,
        n: int | None = None,
    ) -> list[SimilarityResult]:
        """Score the query catcher against every catcher profile."""
        profile = self._profiles.get((catcher_id, season))
        if profile is None:
            log.warning("Catcher %d season %d not found.", catcher_id, season)
            return []

        results = self._partition.score_all(
            profile,
            self._normalizer,
            self._framing_rbf,
            self._blocking_rbf,
            self._throwing_rbf,
            self._deterrence_rbf,
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        catcher_a: tuple[int, int],
        catcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
        """The score of catcher-season ``catcher_b`` against ``catcher_a``."""
        pa = self._profiles.get(catcher_a)
        pb = self._profiles.get(catcher_b)
        if pa is None or pb is None:
            return None

        norm = self._normalizer
        fr = self._framing_rbf.score(
            norm.normalize_framing(pa.framing_vec), norm.normalize_framing(pb.framing_vec)
        )
        bl = self._blocking_rbf.score(
            norm.normalize_blocking(pa.blocking_vec), norm.normalize_blocking(pb.blocking_vec)
        )
        th = self._throwing_rbf.score(
            norm.normalize_throwing(pa.throwing_vec), norm.normalize_throwing(pb.throwing_vec)
        )

        # A profile with no deterrence vector sits at 0 in z-score units: the
        # population mean.
        det_dim = len(DETERRENCE_FEATURES)
        a_det = (
            norm.normalize_deterrence(pa.deterrence_vec)
            if pa.deterrence_vec is not None
            else np.zeros(det_dim)
        )
        b_det = (
            norm.normalize_deterrence(pb.deterrence_vec)
            if pb.deterrence_vec is not None
            else np.zeros(det_dim)
        )
        det = self._deterrence_rbf.score(a_det, b_det)

        composite = (
            WEIGHT_FRAMING * fr
            + WEIGHT_BLOCKING * bl
            + WEIGHT_THROWING * th
            + WEIGHT_DETERRENCE * det
        )
        composite = float(np.clip(composite * np.sqrt(min(pa.eb_alpha, pb.eb_alpha)), 0.0, 1.0))

        return SimilarityResult(
            catcher_id=pb.catcher_id,
            season=pb.season,
            score=composite,
            framing_score=fr,
            blocking_score=bl,
            throwing_score=th,
            deterrence_score=det,
            sample_pitches_received=pb.sample_pitches_received,
        )

    def get_profile(self, catcher_id: int, season: int) -> CatcherProfile | None:
        return self._profiles.get((catcher_id, season))

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
    engine = CatcherSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    report = run_generic_diagnostics(
        engine,
        sub_score_names=[
            "framing_score",
            "blocking_score",
            "throwing_score",
            "deterrence_score",
        ],
        n_query_samples=50,
        engine_name="Catcher",
    )
    print(report)
