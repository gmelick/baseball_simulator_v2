"""
catcher_similarity.py
=====================
Step 2.6 — Catcher-to-Catcher Similarity
MLB Baseball Simulation Platform

Computes a composite similarity score [0, 1] between any two MLB
catcher-season profiles across five dimensions (SIM-072 v2 split):

  1. Framing (45%) — the most impactful and most measurable catcher skill.
     Pitch framing is worth multiple wins per season for elite framers.
     Anchored by Statcast strike rate vs. expected strike rate (the cleanest
     signal), supplemented by raw runs_saved_framing for magnitude.

  2. Blocking (20%) — dirt-ball pass-through prevention. Measured by
     block success rate (blocked balls / total block opportunities) and
     passed ball rate.  Less discriminating than framing but critical for
     simulation accuracy in wild-pitch-heavy situations.

  3. Throwing — Execution (12%) — pure mechanics of the throw to 2B/3B
     once the runner has decided to go.  Includes pop time, caught
     stealing rate (conditional on attempts), exchange time, and arm
     strength (mph).  These are all execution-side signals: how good a
     catcher is at converting attempts into outs.  Field name
     `throwing_score` retained for backward compatibility.

  4. Throwing — Deterrence (8%) — how *often* runners attempt against
     this catcher per opportunity.  Single-feature sub-score driven by
     `steal_attempt_rate_against` (PA-level rate, SIM-073).  Carries the
     reputational signal: elite-arm catchers rarely get challenged, and
     the execution sub-score alone hides that effect because their CS
     rate denominator collapses with attempts.  Per BA pushback memo
     (sprint 2026-05-06), the v1 throwing dimension over-weighted
     execution mechanics for catchers nobody runs on.

  5. Offensive Profile (15%) — catchers are evaluated on offense
     differently than other positions because their defensive value is
     so much larger.  The offensive sub-score uses a minimal feature set:
     K rate, walk rate, avg exit velocity, and hard-hit rate.  Unlike
     fielder or pitcher engines, power (HR rate) is excluded because
     catcher offensive profiles are diverse enough that a heavy-hitting
     catcher (e.g. Wilson Contreras) and a defense-first catcher
     (e.g. Tucker Barnhart) need to match on defensive fingerprint first.

Research-Informed Design
------------------------
  Statcast framing data (strike_rate_vs_expected) has been available since
  2015 and stabilizes meaningfully by ~1500 pitches received.  Block data
  is sparser — meaningful by ~50 block opportunities.  Pop time stabilizes
  quickly (~20 steal attempts against).  CS rate requires 40+ attempts
  to be reliable.

  EB_N_PRIOR is set to 15 (same as fielder engine) because defensive metrics
  require more sample than offensive ones to stabilize, as established in
  the ML Engineer spec.

  Catchers are NOT partitioned by throws-hand.  All catchers are compared
  across the full population.  The framing and blocking dimensions are
  purely skill-based, not handedness-dependent.

Dependencies
------------
  pip install duckdb numpy

Usage
-----
  engine = CatcherSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2022, 2023, 2024, 2025])
  results = engine.query(catcher_id=572816, season=2025)
  results = engine.query(catcher_id=572816, season=2025, n=20)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray

from similarity.similarity_diagnostics import run_generic_diagnostics

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
# Config — Feature Definitions & Weights
# ============================================================================

# --- Framing features ---
# Statcast strike rate vs. expected is the primary framing signal.
# runs_saved_framing adds magnitude (elite framers have high strike_vs_exp
# AND high volume, giving large runs saved).
FRAMING_FEATURES = [
    # feature_name,                   reliability_weight
    ("strike_rate_vs_expected", 0.900),  # called strike rate - xCSR; strongest signal
    ("runs_saved_framing", 0.600),  # runs above average; volume-adjusted
    ("shadow_zone_strike_rate", 0.700),  # border-pitch framing; most discriminating zone
    ("heart_zone_strike_rate", 0.400),  # heart zone: everyone frames these (less signal)
]

# --- Blocking features ---
BLOCKING_FEATURES = [
    ("block_success_rate", 0.700),  # blocked / total block opps
    ("passed_ball_rate_per_9", 0.600),  # PB per 9 innings caught
    ("wild_pitch_prevention_rate", 0.500),  # WPs saved vs. expected
]

# --- Throwing — Execution features (mechanics of the throw itself) ---
# These four features stay together as the v2 "execution" sub-score
# (field name `throwing_score` retained per SIM-072 acceptance criteria).
THROWING_FEATURES = [
    ("pop_time_avg", 0.900),  # avg seconds release → glove at 2B; most stable
    ("cs_rate", 0.600),  # caught stealing rate; conditional on attempts
    ("exchange_time_avg", 0.700),  # pure exchange speed component
    ("arm_strength_mph", 0.800),  # Statcast throw velocity; very stable (physical)
]

# --- Throwing — Deterrence features (do runners even try?) ---
# Single-feature sub-score (acceptable for v1 per SIM-072 AC #3).  The
# reliability weight is 1.0 because there is no other feature in the
# group to balance against.  Magnitude calibration is handled by
# RBF_SIGMA_DETERRENCE below.
DETERRENCE_FEATURES = [
    ("steal_attempt_rate_against", 1.000),  # PA-level steal-attempt rate (SIM-073)
]

# --- Offensive Profile features ---
# Minimal — used only to distinguish catcher offensive archetypes.
# (Elite hitters like Realmuto vs. defense-first catchers like Trevino)
OFFENSE_FEATURES = [
    ("k_rate", 0.701),
    ("walk_rate", 0.558),
    ("avg_exit_velo", 0.675),
    ("hard_hit_rate", 0.628),
]

# --- Sub-score weights (SIM-072 v2 — must sum to 1.0) ---
# Framing 45 + Blocking 20 + Execution 12 + Deterrence 8 + Offense 15 = 100
WEIGHT_FRAMING = 0.45
WEIGHT_BLOCKING = 0.20
WEIGHT_THROWING = 0.12  # execution mechanics (was 0.20 in v1)
WEIGHT_DETERRENCE = 0.08  # NEW — runners' willingness to challenge
WEIGHT_OFFENSE = 0.15
_TOTAL = WEIGHT_FRAMING + WEIGHT_BLOCKING + WEIGHT_THROWING + WEIGHT_DETERRENCE + WEIGHT_OFFENSE
assert abs(_TOTAL - 1.0) < 1e-9, "Sub-score weights must sum to 1.0"

# RBF bandwidth parameters
RBF_SIGMA_FRAMING = 0.9200
RBF_SIGMA_BLOCKING = 1.0500
RBF_SIGMA_THROWING = 0.9800
RBF_SIGMA_DETERRENCE = 1.0000  # 1σ ≈ ~1 SD of standardized steal_attempt_rate_against
RBF_SIGMA_OFFENSE = 1.1000

# EB_N_PRIOR = 15: defensive metrics stabilize more slowly than offensive ones.
# This is the established value for fielder/catcher engines per the ML Engineer spec.
EB_N_PRIOR = 15

# Minimum pitches received for inclusion
MIN_PITCHES_RECEIVED = 300


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class CatcherProfile:
    """Complete catcher-season profile for similarity scoring."""

    catcher_id: int
    season: int
    sample_pitches_received: int
    sample_block_opps: int
    sample_steal_attempts_against: int

    # Feature vectors (raw — normalized at query time)
    framing_vec: NDArray[np.float64]  # shape (len(FRAMING_FEATURES),)
    blocking_vec: NDArray[np.float64]  # shape (len(BLOCKING_FEATURES),)
    throwing_vec: NDArray[np.float64]  # shape (len(THROWING_FEATURES),)
    offense_vec: NDArray[np.float64]  # shape (len(OFFENSE_FEATURES),)
    # SIM-072 v2: deterrence sub-score (single-feature for v1)
    deterrence_vec: NDArray[np.float64] | None = None  # shape (len(DETERRENCE_FEATURES),)

    # Empirical Bayes — gated on pitches received (defensive volume)
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
    throwing_score: float  # execution mechanics (12% weight in v2)
    deterrence_score: float  # NEW (SIM-072) — 8% weight
    offense_score: float
    sample_pitches_received: int


# Backward-compat alias: SIM-072 spec references CatcherSimilarityResult.
CatcherSimilarityResult = SimilarityResult


# ============================================================================
# WeightedRBFSimilarity, EmpiricalBayesShrinkage, FeatureNormalizer
# (Same structure as other RBF engines — reproduced for module self-containment)
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

    def alpha(self, n_samples: int) -> float:
        return n_samples / (n_samples + self.n_prior)

    def shrink(self, raw: NDArray, avg: NDArray, n: int) -> NDArray:
        a = self.alpha(n)
        return a * np.where(np.isnan(raw), avg, raw) + (1.0 - a) * avg


@dataclass(slots=True)
class FeatureNormalizer:
    framing_mean: NDArray | None = None
    framing_std: NDArray | None = None
    blocking_mean: NDArray | None = None
    blocking_std: NDArray | None = None
    throwing_mean: NDArray | None = None
    throwing_std: NDArray | None = None
    deterrence_mean: NDArray | None = None  # SIM-072
    deterrence_std: NDArray | None = None
    offense_mean: NDArray | None = None
    offense_std: NDArray | None = None

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
        self.offense_mean, self.offense_std = _fit([p.offense_vec for p in profiles])

        # SIM-072: deterrence may be missing on legacy profiles loaded from
        # pre-SIM-073 builds.  Fit only when at least one profile carries it.
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

    def normalize_offense(self, v):
        return self._norm(v, self.offense_mean, self.offense_std)

    def normalize_deterrence(self, v):
        return self._norm(v, self.deterrence_mean, self.deterrence_std)


# ============================================================================
# Scoring Partition
# ============================================================================


class CatcherPartition:
    def __init__(self) -> None:
        self.profiles: list[CatcherProfile] = []
        self.keys: list[tuple[int, int]] = []
        self._framing_mat: NDArray | None = None
        self._blocking_mat: NDArray | None = None
        self._throwing_mat: NDArray | None = None
        self._deterrence_mat: NDArray | None = None  # SIM-072
        self._offense_mat: NDArray | None = None
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
        self._offense_mat = np.array([norm.normalize_offense(p.offense_vec) for p in profiles])
        # SIM-072 deterrence matrix.  Profiles missing the signal contribute a
        # zero-vector in standardized space (i.e. league average) so they
        # neither help nor hurt their matches.
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
        offense_rbf: WeightedRBFSimilarity,
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
        of_scores = offense_rbf.score_batch(
            norm.normalize_offense(query.offense_vec), self._offense_mat
        )

        composite = (
            WEIGHT_FRAMING * fr_scores
            + WEIGHT_BLOCKING * bl_scores
            + WEIGHT_THROWING * th_scores
            + WEIGHT_DETERRENCE * det_scores
            + WEIGHT_OFFENSE * of_scores
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
                offense_score=float(of_scores[i]),
                sample_pitches_received=self.profiles[i].sample_pitches_received,
            )
            for i in range(len(self.profiles))
            if self.keys[i] != query_key
        ]


# ============================================================================
# Main Engine
# ============================================================================


class CatcherSimilarityEngine:
    """
    Catcher-to-Catcher Similarity Engine (Step 2.6).

    Scores computed exhaustively against all catcher-season profiles
    with sufficient pitches-received sample.

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
            "offense": {},
        }
        self._normalizer = FeatureNormalizer()
        self._shrinkage = EmpiricalBayesShrinkage()
        self._partition = CatcherPartition()

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
        self._offense_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_OFFENSE, np.array([w for _, w in OFFENSE_FEATURES])
        )

    def build(self, seasons: list[int] | None = None) -> None:
        """Load all catcher profiles, apply shrinkage, build scoring matrices."""
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
                ("offense", OFFENSE_FEATURES),
            ]:
                self._league_avg[group][season] = np.array(
                    [pj.get(f, 0.0) or 0.0 for f, _ in features], dtype=np.float64
                )

    def _load_profiles(self, conn, seasons):
        sf = f"AND csm.season IN ({', '.join(str(s) for s in seasons)})" if seasons else ""
        rows = conn.execute(f"""
            SELECT
                csm.catcher_id, csm.season,
                csm.sample_pitches_received,
                csm.sample_block_opps,
                csm.sample_steal_attempts_against,
                -- Framing (4)
                csm.strike_rate_vs_expected,
                csm.runs_saved_framing,
                csm.shadow_zone_strike_rate,
                csm.heart_zone_strike_rate,
                -- Blocking (3)
                csm.block_success_rate,
                csm.passed_ball_rate_per_9,
                csm.wild_pitch_prevention_rate,
                -- Throwing — Execution (4)
                csm.pop_time_avg,
                csm.cs_rate,
                csm.exchange_time_avg,
                csm.arm_strength_mph,
                -- Throwing — Deterrence (1, SIM-073)
                csm.steal_attempt_rate_against,
                -- Offense (4)
                csm.k_rate,
                csm.walk_rate,
                csm.avg_exit_velo,
                csm.hard_hit_rate,
                csm.below_minimum_sample
            FROM derived.catcher_season_metrics csm
            WHERE NOT csm.below_minimum_sample
              {sf}
        """).fetchall()

        log.info("Loading %d catcher profiles from DuckDB …", len(rows))

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
                exchange,
                arm_mph,
                steal_attempt_rate_against,
                k_rate,
                walk_rate,
                avg_ev,
                hh_rate,
                below_min,
            ) = row

            def _v(*vals):
                return np.array([v or 0.0 for v in vals], dtype=np.float64)

            # SIM-072: deterrence vector is None when the underlying column is
            # NULL (sub-100-PA min-sample guard from SIM-073) so the engine
            # falls back to league-average behavior in standardized space.
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
                throwing_vec=_v(pop_time, cs_rate, exchange, arm_mph),
                deterrence_vec=deterrence_vec,
                offense_vec=_v(k_rate, walk_rate, avg_ev, hh_rate),
                eb_alpha=self._shrinkage.alpha(n_pitches or 0),
                below_minimum=bool(below_min),
            )

    def _apply_shrinkage(self) -> None:
        for p in self._profiles.values():
            s = p.season
            for group, attr in [
                ("framing", "framing_vec"),
                ("blocking", "blocking_vec"),
                ("throwing", "throwing_vec"),
                ("deterrence", "deterrence_vec"),
                ("offense", "offense_vec"),
            ]:
                avg = self._league_avg[group].get(s)
                if avg is None:
                    continue
                current = getattr(p, attr)
                # SIM-072: deterrence may be missing when the column was NULL
                # (sub-100-PA sample).  Fall back to league average — no
                # shrinkage math needed since `current` would be undefined.
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
        """Score query catcher against ALL catcher profiles."""
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
            self._offense_rbf,
        )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:n] if n is not None else results

    def query_pair(
        self,
        catcher_a: tuple[int, int],
        catcher_b: tuple[int, int],
    ) -> SimilarityResult | None:
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
        of = self._offense_rbf.score(
            norm.normalize_offense(pa.offense_vec), norm.normalize_offense(pb.offense_vec)
        )

        # SIM-072 deterrence sub-score.  Profiles missing the signal contribute
        # the league-average vector (zero in standardized space), so the pair
        # scores as deterrence-similar without injecting noise.
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
            + WEIGHT_OFFENSE * of
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
            offense_score=of,
            sample_pitches_received=pb.sample_pitches_received,
        )

    def get_profile(self, catcher_id: int, season: int) -> CatcherProfile | None:
        return self._profiles.get((catcher_id, season))

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    def profile_ids(self) -> list[tuple[int, int]]:
        return list(self._profiles.keys())


# ============================================================================
# Convenience: Batch Similarity Matrix
# ============================================================================


def build_similarity_matrix(
    engine: CatcherSimilarityEngine,
    catcher_ids: list[tuple[int, int]],
) -> NDArray[np.float64]:
    n = len(catcher_ids)
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            result = engine.query_pair(catcher_ids[i], catcher_ids[j])
            score = result.score if result else 0.0
            matrix[i, j] = score
            matrix[j, i] = score
    return matrix


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
            "offense_score",
        ],
        n_query_samples=50,
        engine_name="Catcher",
    )
    print(report)
