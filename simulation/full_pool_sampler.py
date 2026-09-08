"""
simulation/full_pool_sampler.py
===============================
SIM-423 — full-pool similarity-weighted pitch sampler core.

Scores the ENTIRE bat_hand pool by the applicable similarity engines and draws
from that full weighted distribution — no top-K, no hard filter except the
batter's hand (pitcher hand self-zeroes via the pitcher engine). The weight
factorizes so the cost amortizes (perf gate: ~6 s/game naive -> ~1.1-1.5 s/game):

    w_i = f_pitcher[pid:season_i] · f_batter[bid:season_i] · f_situation(state, sit_i)
          · recency_i

  * f_pitcher  — per-(pitcher) gather from the SIM-075 pitcher×pitcher sim;
                 CONSTANT for a half-inning (cached in :meth:`new_half_inning`).
  * f_batter   — RBF over the batter season-metrics embedding; CONSTANT for a PA.
  * f_situation— RBF over the candidate's own situation vector (in the pool);
                 recomputed per PA.
  * recency    — pool constant.

A fresh weight vector + CDF is assembled once per PA; each pitch is then an O(1)
searchsorted draw. Missing artifacts/keys degrade to a neutral (1.0) factor, so
the sampler runs with a partial bundle (e.g. before the pitcher-sim nightly build).
"""

from __future__ import annotations

import os

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
from simulation.filter_cells import (
    DEFAULT_MIN_CELL,
    N_BAND,
    N_BASE,
    N_COUNT,
    N_OUTS,
    score_band,
    score_band_array,
)

_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")

#: SIM-523 part B: the pitch-to-pitch engine's feature weights, in the pitch
#: pool's ``_GEOM_COLS`` order (velo, ivb, hb, spin_rate, spin_axis, release_x,
#: release_z, release_ext, plate_x, plate_z). Copied so the sampler never
#: imports FAISS; a unit test pins them to the engine's ``FEATURE_WEIGHTS``.
_PITCH_FEATURE_WEIGHTS = np.array(
    [1.20, 1.10, 1.00, 0.70, 0.50, 0.60, 0.60, 0.40, 0.90, 0.90], dtype=np.float32
)


def _repower(w: np.ndarray, f: np.ndarray, power: float) -> np.ndarray:
    """SIM-523 part B: ``w`` already carries the factor ``f`` once; return
    ``w`` with that factor raised to ``power`` instead (× f^(power-1)). A
    zero factor stays zero at any power (its row has zero weight already);
    power 1.0 returns ``w`` itself, untouched."""
    if power == 1.0:
        return w
    adj = np.where(f > 0.0, np.power(np.maximum(f, 1e-30), np.float32(power - 1.0)), 0.0)
    return (w * adj).astype(np.float32)


#: SIM-476 diagnostics (2026-08-17): skip ONE similarity factor in the steal
#: draw to locate the source of the safe/caught-split inflation (certified
#: 88.1% vs MLB ~77.6%). The catcher arm REFUTED its suspect (ablating it made
#: the split WORSE, 0.869 -> 0.916); the runner arm tests the attempt-
#: composition theory (the runner kernel may concentrate attempted-row weight
#: on elite-stealer-like rows more sharply than real attempt composition).
#: Default OFF; never set in production.
_STEAL_ABLATE_CATCHER = os.environ.get("SIM_STEAL_ABLATE_CATCHER", "0") == "1"
_STEAL_ABLATE_RUNNER = os.environ.get("SIM_STEAL_ABLATE_RUNNER", "0") == "1"
_STEAL_ABLATE_PITCHER = os.environ.get("SIM_STEAL_ABLATE_PITCHER", "0") == "1"


#: SIM-512: positional number -> the fielder-embedding position name. Keep in
#: sync with ``simulation.sim_loop._POS_NUM_TO_STR`` — the fielder embedding
#: keys are ``"{player_id}:{position}:{season}"`` with these names.
_POS_NUM_TO_NAME: dict[int, str] = {
    1: "P",
    2: "C",
    3: "1B",
    4: "2B",
    5: "3B",
    6: "SS",
    7: "LF",
    8: "CF",
    9: "RF",
}


class FullPoolSampler:
    def __init__(
        self,
        artifacts: EngineArtifacts,
        rng: np.random.Generator | None = None,
        *,
        sit_sigma: float = 2.0,
        batter_sigma: float = 3.0,
        platoon_off_weight: float = 0.6,
        home_off_weight: float = 1.0,
    ) -> None:
        self.a = artifacts
        self.rng = rng if rng is not None else np.random.default_rng()
        self.sit_sigma = float(sit_sigma)
        self.batter_sigma = float(batter_sigma)
        # SIM-413: the relative weight given to OPPOSITE-hand batted-ball pool rows
        # when the platoon reweight is active (same-hand rows keep weight 1.0). <1
        # softly conditions the batted-ball draw on the live pitcher hand so the
        # drawn outcome reflects the platoon matchup; 1.0 disables the reweight.
        self.platoon_off_weight = float(platoon_off_weight)
        # SIM-491 (the SIM-412 rebuild): the relative weight given to batted-ball
        # rows whose BATTING SIDE mismatches the live one (matched rows keep
        # 1.0). <1 pulls the draw toward rows hit in the same half (home rows
        # for the home offense), so home advantage emerges from the pool's real
        # rates. 1.0 (the default) disables the reweight EXACTLY — no weight
        # multiplication runs, so the draw is byte-identical to pre-SIM-491.
        # The value is a SIM-476 fit target (calibrate to the +0.13 R/g edge).
        self.home_off_weight = float(home_off_weight)
        # SIM-491 part 2 (the SIM-411 rebuild): the park kernel. A Gaussian on
        # |park_run_factor(live venue) − park_run_factor(row venue)| pulls the
        # batted-ball draw toward rows hit in run-environment-similar parks.
        # ``venue_run_factors`` maps (venue_id, season) -> regressed run factor
        # (derived.park_factors, factor_type='R'); the factory loads it when
        # the kernel is enabled. ``park_sigma`` 0.0 (the default) disables the
        # kernel EXACTLY — no weight multiplication runs. The bandwidth is a
        # SIM-476 fit target (the factor range is ~0.87-1.13).
        self.park_sigma = 0.0
        self.venue_run_factors: dict[tuple[int, int], float] | None = None
        #: Per-hand cache of the per-row park factor (1.0 for unknown venues).
        self._bb_park: dict[str, np.ndarray] = {}
        # SIM-491 part 3 (the SIM-425b rebuild): the fielder-quality kernel.
        # Weight each batted-ball row by the similarity between the LIVE
        # defender at the row's position and the ROW's own fielder, over the
        # OAA-centred feature set — a good live shortstop pulls the draw toward
        # rows where good shortstops made plays. 0.0 (the default) disables
        # the kernel EXACTLY. The bandwidth is a SIM-476 fit target.
        self.fielder_sigma = 0.0
        #: Per-hand cache of each row's fielder-embedding index (-1 = absent).
        self._bb_fielder_emb: dict[str, np.ndarray] = {}
        # SIM-517: the catcher RECEIVING kernel — ONE multiplicative weight on
        # the pitch draw, with an ANISOTROPIC metric: the framing dims (the
        # two zone-strike rates) and the blocking dims (the block rate + the
        # EB uncaught-K3 rate) each carry their OWN bandwidth, because the
        # part-E ladder measured they need different ones (framing conditions
        # correctly at ~0.25 and DEGRADES tighter — twin-selection noise;
        # blocking closes only at ~0.05 — soft-kernel shrinkage toward the
        # dense centre). A poor receiver behind the plate pulls the draw
        # toward pitches caught by poor receivers at the pool's own
        # conditional rates. A group's sigma 0.0 (the default) removes that
        # group from the metric; both 0.0 disables the kernel EXACTLY. The
        # factor is normalized to a MEAN of 1 within each COUNT BUCKET (the
        # SIM-476 per-partition lesson): it may only shift WHICH pitch is
        # drawn at a count, never the count structure; missing-identity rows
        # are exactly neutral.
        self.catcher_framing_sigma = 0.0
        self.catcher_block_sigma = 0.0
        #: Lazy receiving data: (key_index, z-matrix) or None when unavailable.
        self._recv_data: tuple[dict[str, int], np.ndarray] | None | bool = False
        #: Per-hand cache of each pitch row's receiving-matrix index (-1 absent).
        self._pp_recv_idx: dict[str, np.ndarray] = {}
        #: (hand, catcher_key) -> the normalized per-row receiving factor.
        self._recv_factor_cache: dict[tuple[str, str], np.ndarray | None] = {}
        #: The catcher key new_half_inning staged for the receiving factor.
        self._catcher_key: str | None = None
        #: The last drawn pitch-pool row (global index; None before any draw
        #: or after an empty-bucket fallback) — the got-away accessor reads it.
        self._pp_last_i: int | None = None
        # SIM-518 — the draw-conditioning weights. Every one is OFF by
        # default (a 0.0 sigma / a 1.0 weight disables it EXACTLY — no
        # multiplication runs), reads a column that is None on a pre-0023
        # bundle (then it stays neutral), and is a SIM-476-style fit target.
        #   * fatigue (SIM-465): Gaussians on |live − row| pitch count and
        #     times-through-the-order over the PITCH draw, normalized to a
        #     mean of 1 within each count bucket; a row with an unknown value
        #     is exactly neutral. Costs one full-pool pass per PA until the
        #     SIM-467 cell index restricts it to the live cell.
        #   * the batting side (SIM-464's pitch half): a row whose side
        #     mismatches the live one × ``pitch_home_off_weight`` (0.0 = a
        #     hard match, the owner's fielding-draw ruling; 1.0 = off).
        #   * pitch similarity (SIM-472): a Gaussian on the z-scored 10-dim
        #     distance between the DRAWN pitch's geometry and each batted-ball
        #     row's own pitch, inside the SIM-511 base-out cell — the pitch
        #     and the contact must agree.
        self.fatigue_pc_sigma = 0.0
        self.fatigue_tto_sigma = 0.0
        self.pitch_home_off_weight = 1.0
        self.bb_pitch_sigma = 0.0
        # SIM-467 — the pitch-draw CELL INDEX. Off (the default) the per-PA
        # weight is assembled over the WHOLE pool and split into the 12 count
        # buckets, exactly as before. On, each plate appearance selects its
        # hard-filter cell — (runners, outs, score band, batting side), the
        # SIM-451 definition — and assembles the weight over that cell's 12
        # count sub-cells only. The weights INSIDE the cell are today's weights
        # bit for bit (the same 4-dim situation kernel over the same rows), so
        # the only change is a zero weight outside the cell; per-PA work drops
        # from ~N rows to ~N/240. Thin sub-cells widen in decision #19's fixed
        # order (band → side → count) below ``pitch_min_cell`` rows; every
        # widening is counted per draw in ``widen_counts``. A pre-0023 bundle
        # (no ``bat_home``) has no side dimension: 1,440 cells instead of 2,880.
        # Plan: docs/audit/2026-09-04-sim467-518-plan.md §5.
        self.pitch_cell_index = False
        self.pitch_min_cell = DEFAULT_MIN_CELL
        #: Per-hand cell index: the row order sorted by sub-cell + offsets.
        self._cell_cache: dict[str, dict] = {}
        #: The current PA's per-count sub-cell rows / widening levels (cell path
        #: only; None on the whole-pool path — ``draw`` reads them when set).
        self._pa_rows: list[np.ndarray] | None = None
        self._pa_levels: list[int] | None = None
        #: Draws per widening level 0..3 (the lane reports the shares).
        self.widen_counts = np.zeros(4, dtype=np.int64)
        # SIM-523 part A — the actor SCORE MATRICES. Off (the default) every
        # actor factor is today's bell-curve kernel over raw profile numbers.
        # On, each factor is its engine's composite 0-to-1 score, read from the
        # nightly matrix (``EngineArtifacts.actor_sim``) by one row lookup and
        # one gather, raised to a fitted POWER (``actor_power[name]``, 1.0 until
        # part F fits it). A matrix the bundle lacks falls back to the kernel;
        # a live actor the matrix lacks is neutral (1.0), as is a pool row whose
        # actor the matrix lacks. The redesign's ordering rule (pitcher first,
        # catcher receiving last) is enforced by the fitted powers, not here.
        self.actor_matrices = False
        self.actor_power: dict[str, float] = {}
        #: (matrix name) -> int64 array mapping the actor EMBEDDING's rows to
        #: matrix columns (-1 = unscored); built once per matrix per process.
        self._emb_to_mat_cache: dict[str, np.ndarray] = {}
        # SIM-523 part B — the PITCH / PITCH-RESULT split (plan §3, steps 3
        # and 4). Off (the default) one draw picks the pitch AND its result,
        # as today. On, ``draw`` first picks the PITCH thrown from the per-PA
        # weight (pitcher, batter, recency, situation, the gated extras — the
        # batter factor re-raised to ``pitch_batter_power``), then picks the
        # RESULT among the same rows: the same per-PA weight TIMES the
        # pitch-to-pitch score to the drawn pitch (a Gaussian on the pitch
        # engine's own weighted, z-scored metric, bandwidth
        # ``result_pitch_sigma``; 0 = the result is the pitch row itself),
        # the pitcher factor re-raised to ``result_pitcher_power`` and the
        # batter factor to ``result_batter_power``. Every power is 1.0 until
        # part F fits it (then the pitch draw is exactly today's draw and the
        # result draw is the same weight, conditioned on the pitch). The
        # RESULT row is the play: its outcome, its got-away fact and, when in
        # play, its own batted ball (``last_born_batted_ball`` through the
        # artifact's pitch-id join ``HandPool.bb_row``); the thrown pitch's
        # geometry stays readable as ``last_pitch_geom``.
        self.pitch_result_split = False
        self.result_pitch_sigma = 1.0
        self.result_pitcher_power = 1.0
        self.result_batter_power = 1.0
        self.pitch_batter_power = 1.0
        # The DENSITY CORRECTION on the result draw. A kernel estimate of
        # "the result given the pitch" leans toward where the candidate rows
        # are dense — the strike zone — and the 2026-09-08 probe measured that
        # lean at neutral powers: the ball share fell 7% and walks 37%. The
        # standard correction divides every candidate by its own local
        # density under the same kernel (estimated against a fixed random
        # reference subset of the candidate rows, cached per sub-cell); the
        # split then reproduces the single draw's marginals at neutral powers
        # and conditions on the pitch on top. ``result_density_power`` is the
        # exponent on the inverse density (1.0 = the full correction; 0.0 =
        # off) — a part-F fit target with the bandwidth.
        self.result_density_power = 1.0
        #: The current PA's per-count sub-cell keys (the density cache key).
        self._pa_keys: list[tuple] | None = None
        # SIM-523 part B: the BORN batted ball on the fielding draw — a
        # Gaussian on the z-scored (exit velocity, launch angle, spray,
        # distance) distance between the result row's batted ball and each
        # cell row's, normalized to a mean of 1 over the rows with complete
        # data (part C's step 6 grows from it). 0.0 (the default) = off.
        self.bb_born_sigma = 0.0
        #: Per-count raw per-PA weights and the pitcher / batter factors,
        #: kept only while the split is on (the result draw re-weights them).
        self._bucket_w: list[np.ndarray | None] | None = None
        self._bucket_fp: list[np.ndarray | None] | None = None
        self._bucket_fb: list[np.ndarray | None] | None = None
        #: The half-inning's pitcher factor (the split re-raises it).
        self._f_pitcher_vec: np.ndarray | None = None
        #: The last PITCH-draw row (the pitch thrown); equal to ``_pp_last_i``
        #: while the split is off.
        self._pp_pitch_i: int | None = None
        #: Per-hand z-stats (mean, std, complete-row mask) of the pitch pool's
        #: geometry and of the batted-ball pool's batted-ball features.
        self._pp_geom_stats: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        self._bb_born_stats: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        #: Per-hand cache of the batted-ball pool's pitch-geometry z-stats
        #: (mean, std, all-finite row mask) — constant once the bundle loads.
        self._bb_pgeom_stats: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        # Per-pool precompute: dense candidate->profile indices for O(1) gathers.
        self._pool_cache: dict[str, dict] = {}
        # SIM-430 hot-path caches (all hold CONSTANTS that the original code
        # recomputed every PA — the per-game profiler's top costs):
        #   * _vecs_z: the z-scored batter-embedding matrix, recomputed in both
        #     _f_batter and _batter_aff every PA (~0.5 s/game) though it never
        #     changes once the artifacts are loaded.
        #   * _aff_cache: the per-batter RBF affinity vector keyed by batter_key,
        #     so the pitch-pool draw and the batted-ball draw in the SAME PA (and
        #     the same batter across PAs) reuse one einsum+exp pass.
        # Both are pure memoization — identical numeric output, just hoisted out
        # of the per-PA loop.
        self._vecs_z: np.ndarray | None = None
        self._aff_cache: dict[str, np.ndarray] = {}
        # State across the matchup.
        self._hand: str | None = None
        self._base: np.ndarray | None = None  # f_pitcher * recency  (half-inning)
        self._bucket_cdf: list | None = None  # per-PA, per-count-bucket CDFs (SIM-429)
        # SIM-425 batted-ball state
        self._bb_hand: str | None = None
        self._bb_cdf: np.ndarray | None = None
        self._bb_pool_bat: dict[str, np.ndarray] = {}
        self._br_idx: dict[str, int] | None = None  # baserunner feature-name -> col
        # SIM-413: per-hand cached mask of pool rows pitched by a RHP (p_throws=='R'),
        # so the platoon reweight is one cheap boolean select per PA instead of an
        # object-array compare over the whole pool.
        self._bb_throws_r: dict[str, np.ndarray] = {}
        # SIM-425b: index of the row the last battedball_draw returned, so the
        # resolver can read that pool play's fielder identity/position.
        self._bb_last_i: int | None = None
        self._fld_idx: dict[str, int] | None = None  # fielder feature-name -> col
        # SIM-474: steal-opportunity-pool per-target precompute (lazy, permanent):
        # cell index by (outs, balls, strikes), per-row embedding-row gathers for
        # runner/pitcher/catcher, and the z-scored steal-feature matrices.
        self._steal_meta_cache: dict[str, dict] = {}
        self._steal_emb_z: dict[str, np.ndarray] = {}
        # SIM-511: per-hand transition precompute (the base-out cell index over
        # consistent rows) + the current PA's cell rows (None = legacy path).
        self._bb_meta_cache: dict[str, dict] = {}
        self._bb_rows: np.ndarray | None = None
        # SIM-512: per-decision advancement-pool precompute.
        self._adv_meta_cache: dict[str, dict] = {}
        #: SIM-512 kernel bandwidths — the same hyperparameter class as
        #: ``steal_sigma`` and SIM-476 fit targets. ``adv_sigma`` conditions on
        #: the actors (runner legs/decisions, fielder arm); ``adv_feat_sigma``
        #: on the z-scored throw geometry (EV, LA, spray, distance, outs).
        self.adv_sigma = 1.0
        self.adv_feat_sigma = 1.0
        #: SIM-474 kernel bandwidths — the same hyperparameter class as
        #: ``sit_sigma``/``batter_sigma`` and a SIM-476 temperature-fit target.
        #: 1.0 conditions HARD on the actor (a maximally-different runner keeps
        #: ~0.14 weight over the 4 steal features): attempt rates vary more by
        #: runner than any other factor here, so the runner kernel must bite.
        self.steal_sigma = 1.0
        self.steal_score_sigma = 2.0

    # ---- per-pool one-time precompute ------------------------------------
    def _pool_meta(self, hand: str) -> dict:
        if hand in self._pool_cache:
            return self._pool_cache[hand]
        pool: HandPool = self.a.pools[hand]
        n = pool.n
        # candidate (pitcher_id:season) -> dense profile index in the pitcher-sim list
        pidx = self.a.pitcher_sim_index
        pool_prof = np.fromiter(
            (
                pidx.get(f"{int(p)}:{int(s)}", -1)
                for p, s in zip(pool.pitcher_id, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=n,
        )
        # candidate (batter_id:season) -> dense batter-embedding row
        bemb = self.a.actor_emb.get("batter")
        if bemb is not None:
            bkidx = bemb["key_index"]
            pool_bat = np.fromiter(
                (
                    bkidx.get(f"{int(b)}:{int(s)}", -1)
                    for b, s in zip(pool.batter_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=n,
            )
        else:
            pool_bat = np.full(n, -1, dtype=np.int64)
        # SIM-429: count bucket per row (balls*3 + strikes, 12 buckets) + the row
        # indices per bucket, so the pitch draw conditions on the LIVE count
        # (ball-rate swings 37%@0-0 -> 23%@3-2; a count-blind draw 2.8x-inflates).
        balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
        strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
        cbucket = balls * 3 + strikes
        bucket_rows = [np.nonzero(cbucket == b)[0] for b in range(12)]
        meta = {
            "pool": pool,
            "pool_prof": pool_prof,
            "pool_bat": pool_bat,
            "outcome": np.asarray(pool.outcome_type, dtype=object),
            "bucket_rows": bucket_rows,
            # SIM-430: the base-out columns as a CONTIGUOUS copy. _f_situation_baseout
            # ran ``pool.sit[:, 2:6]`` (a non-contiguous 4-col slice + copy over the
            # whole ~935K-row pool) on EVERY PA — the profiler's single biggest cost
            # (~0.67 s/game). Materialising it once here makes the per-PA RBF a plain
            # contiguous subtract.
            "sit_baseout": np.ascontiguousarray(pool.sit[:, 2:6]),
        }
        self._pool_cache[hand] = meta
        return meta

    # ---- factor builders --------------------------------------------------
    def _f_pitcher(self, hand: str, pitcher_key: str) -> np.ndarray:
        meta = self._pool_meta(hand)
        n_prof = len(self.a.pitcher_sim_index)
        if n_prof == 0:
            return np.ones(meta["pool"].n, dtype=np.float32)
        matrix = getattr(self.a, "pitcher_sim_matrix", None)
        if matrix is not None:
            # SIM-430: dense fast path — one contiguous (shared) row instead of
            # scattering the ~2 GB pitcher_sim dict. Byte-identical to the dict
            # path: a key absent from the index, or an unscored/empty query (an
            # all-zero row — a populated query always has >0 same-hand scores),
            # both fall back to the flat-ones weighting exactly as ``if not sims``.
            i = self.a.pitcher_sim_index.get(pitcher_key)
            if i is None:
                return np.ones(meta["pool"].n, dtype=np.float32)
            prof_score = matrix[i]
            if not prof_score.any():
                return np.ones(meta["pool"].n, dtype=np.float32)
        else:
            sims = self.a.pitcher_sim.get(pitcher_key)
            if not sims:
                return np.ones(meta["pool"].n, dtype=np.float32)
            prof_score = np.full(n_prof, 0.0, dtype=np.float32)
            idx = self.a.pitcher_sim_index
            for k, v in sims.items():
                j = idx.get(k)
                if j is not None:
                    prof_score[j] = v
        pp = meta["pool_prof"]
        out = np.where(pp >= 0, prof_score[np.clip(pp, 0, n_prof - 1)], np.float32(1.0))
        return out.astype(np.float32)

    def _batter_vecs_z(self, bemb: dict) -> np.ndarray:
        """The z-scored batter-embedding matrix (SIM-430: cached — constant across
        the whole sampler life, but the original recomputed it per PA)."""
        if self._vecs_z is None:
            self._vecs_z = ((bemb["vecs"] - bemb["mean"]) / bemb["std"]).astype(np.float32)
        return self._vecs_z

    def _batter_affinity(self, batter_key: str) -> np.ndarray | None:
        """Per-embedding-row RBF affinity to ``batter_key`` (None if absent),
        memoized by batter_key (SIM-430). Shared by the pitch-pool factor
        (:meth:`_f_batter`) and the batted-ball factor (:meth:`_batter_aff`), so a
        batter's affinity is computed at most once — not twice per PA, every PA."""
        cached = self._aff_cache.get(batter_key)
        if cached is not None:
            return cached
        bemb = self.a.actor_emb.get("batter")
        if bemb is None or batter_key not in bemb["key_index"]:
            return None
        vecs_z = self._batter_vecs_z(bemb)
        q = vecs_z[bemb["key_index"][batter_key]]
        diff = vecs_z - q
        d2 = np.einsum("ij,ij->i", diff, diff)
        aff = np.exp(-d2 / (2.0 * self.batter_sigma**2 * vecs_z.shape[1])).astype(np.float32)
        self._aff_cache[batter_key] = aff
        return aff

    # ---- SIM-523 part A: the actor score-matrix lookups --------------------
    #: Which actor embedding each matrix's keys index (fielder matrices are
    #: per position and share the fielder embedding).
    _MATRIX_EMBEDDING = {
        "batter": "batter",
        "catcher": "catcher",
        "catcher_throwing": "catcher",
        "runner_steal": "baserunner",
        "runner_adv": "baserunner",
        "pitcher_steal": "pitcher_steal",
    }

    def _actor_matrix(self, name: str) -> dict | None:
        """The named score matrix entry ({"index", "matrix"}) or None."""
        return getattr(self.a, "actor_sim", {}).get(name)

    def _emb_to_mat(self, name: str) -> np.ndarray | None:
        """Embedding row -> matrix column for ``name`` (-1 = unscored)."""
        cached = self._emb_to_mat_cache.get(name)
        if cached is not None:
            return cached
        entry = self._actor_matrix(name)
        actor = self._MATRIX_EMBEDDING.get(name, "fielder" if name.startswith("fielder_") else None)
        emb = self.a.actor_emb.get(actor) if actor else None
        if entry is None or emb is None:
            return None
        index = entry["index"]
        keys = emb.get("keys")
        if keys is None:
            keys = [k for k, _ in sorted(emb["key_index"].items(), key=lambda kv: kv[1])]
        out = np.fromiter((index.get(str(k), -1) for k in keys), dtype=np.int64, count=len(keys))
        self._emb_to_mat_cache[name] = out
        return out

    def _matrix_gather(self, name: str, live_key: str, emb_rows: np.ndarray) -> np.ndarray | None:
        """The live actor's matrix row gathered onto ``emb_rows`` (embedding
        indices per pool row, -1 = absent), raised to the factor's power.
        Neutral (1.0) where either side is unscored. None when the bundle has
        no such matrix (the caller falls back to its kernel)."""
        entry = self._actor_matrix(name)
        e2m = self._emb_to_mat(name)
        if entry is None or e2m is None:
            return None
        live = entry["index"].get(live_key)
        out = np.ones(len(emb_rows), dtype=np.float32)
        if live is None:
            return out
        cols = np.where(emb_rows >= 0, e2m[np.clip(emb_rows, 0, len(e2m) - 1)], -1)
        valid = cols >= 0
        if not valid.any():
            return out
        scores = entry["matrix"][live, cols[valid]].astype(np.float32)
        scores = np.where(np.isfinite(scores), scores, np.float32(1.0))
        power = float(self.actor_power.get(name, 1.0))
        if power != 1.0:
            scores = np.power(np.clip(scores, 0.0, None), np.float32(power)).astype(np.float32)
        out[valid] = scores
        return out

    def _f_batter(self, hand: str, batter_key: str) -> np.ndarray:
        meta = self._pool_meta(hand)
        if self.actor_matrices:
            f = self._matrix_gather("batter", batter_key, meta["pool_bat"])
            if f is not None:
                return f
        aff = self._batter_affinity(batter_key)
        if aff is None:
            return np.ones(meta["pool"].n, dtype=np.float32)
        pb = meta["pool_bat"]
        return np.where(pb >= 0, aff[np.clip(pb, 0, len(aff) - 1)], np.float32(1.0)).astype(
            np.float32
        )

    def _f_situation(self, hand: str, state: np.ndarray) -> np.ndarray:
        pool = self.a.pools[hand]
        diff: np.ndarray = pool.sit - np.asarray(state, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        return np.exp(-d2 / (2.0 * self.sit_sigma**2 * pool.sit.shape[1])).astype(np.float32)

    def _f_situation_baseout(self, hand: str, base_out: np.ndarray) -> np.ndarray:
        """RBF over the base-out dims only (outs, runners, inning, score_diff) —
        count is handled by the per-pitch bucket, not this factor (SIM-429)."""
        s = self._pool_meta(hand)["sit_baseout"]  # SIM-430: contiguous, cached
        diff: np.ndarray = s - np.asarray(base_out, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        return np.exp(-d2 / (2.0 * self.sit_sigma**2 * s.shape[1])).astype(np.float32)

    # ---- matchup lifecycle ------------------------------------------------
    def new_half_inning(self, hand: str, pitcher_key: str, catcher_key: str | None = None) -> None:
        """Cache the half-inning-constant base (f_pitcher * recency) and stage
        the fielding catcher for the SIM-517 receiving factor (a no-op at
        both receiving sigmas 0)."""
        self._hand = hand
        self._catcher_key = catcher_key
        pool = self.a.pools[hand]
        f_pitcher = self._f_pitcher(hand, pitcher_key)
        self._f_pitcher_vec = f_pitcher  # SIM-523 part B: the split re-raises it
        self._base = (f_pitcher * pool.recency).astype(np.float32)

    def new_plate_appearance(
        self,
        batter_key: str,
        base_out: np.ndarray,
        *,
        pitch_count: int | None = None,
        tto: int | None = None,
        bat_home: bool | None = None,
    ) -> None:
        """Assemble the per-PA matchup weight (base · f_batter · f_situation_baseout
        [· f_catcher_receiving — SIM-517] [· f_fatigue · the batting-side weight —
        SIM-518]) and split it into 12 count-bucket CDFs for the per-pitch,
        count-conditioned draw (SIM-429).

        SIM-518: ``pitch_count`` (pitches the live pitcher threw BEFORE this
        PA) and ``tto`` (his times through the order entering it) feed the
        fatigue kernel; ``bat_home`` feeds the batting-side weight. Each is
        read only when its sigma / weight is ON and the pool carries the
        column; omitted or off, the weight is exactly today's."""
        assert self._hand is not None and self._base is not None, "call new_half_inning first"
        if self.pitch_cell_index:
            self._new_plate_appearance_cell(
                batter_key, base_out, pitch_count=pitch_count, tto=tto, bat_home=bat_home
            )
            return
        self._pa_rows = None
        self._pa_levels = None
        self._pa_keys = None
        f_bat = self._f_batter(self._hand, batter_key)
        w = self._base * f_bat * self._f_situation_baseout(self._hand, base_out)
        if (
            self.catcher_framing_sigma > 0.0 or self.catcher_block_sigma > 0.0
        ) and self._catcher_key is not None:
            f_recv = self._f_catcher_receiving(self._hand, self._catcher_key)
            if f_recv is not None:
                w = w * f_recv
        # --- SIM-518: the conditioning weights (each exactly absent when off)
        if (self.fatigue_pc_sigma > 0.0 or self.fatigue_tto_sigma > 0.0) and (
            pitch_count is not None or tto is not None
        ):
            f_fat = self._f_fatigue(self._hand, pitch_count, tto)
            if f_fat is not None:
                w = w * f_fat
        if bat_home is not None and self.pitch_home_off_weight != 1.0:
            bh = getattr(self.a.pools[self._hand], "bat_home", None)
            if bh is not None:
                # An unknown-side row (-1) stays neutral; a mismatch × the weight.
                mismatch = (bh >= 0) & ((bh > 0) != bool(bat_home))
                w = w * np.where(mismatch, np.float32(self.pitch_home_off_weight), np.float32(1.0))
        rows = self._pool_meta(self._hand)["bucket_rows"]
        # SIM-523 part B: the split keeps the raw weight per count for the
        # result draw; off, nothing is kept and ``w`` is untouched.
        if self.pitch_result_split:
            fpv = self._f_pitcher_vec
            self._bucket_w = [(w[r] if r.size else None) for r in rows]
            self._bucket_fb = [(f_bat[r] if r.size else None) for r in rows]
            self._bucket_fp = [(fpv[r] if (r.size and fpv is not None) else None) for r in rows]
            w = _repower(w, f_bat, self.pitch_batter_power)
        else:
            self._bucket_w = self._bucket_fb = self._bucket_fp = None
        self._bucket_cdf = [(np.cumsum(w[r], dtype=np.float64) if r.size else None) for r in rows]

    # ---- SIM-467: the cell index -------------------------------------------
    def _cell_meta(self, hand: str) -> dict:
        """Per-hand one-time precompute of the cell index: every row's sub-cell
        id — ((runners, outs, band, side) × count) — a stable row order sorted
        by it, and the offsets that make each sub-cell one contiguous slice.
        The side axis has THREE values when the pool carries ``bat_home``
        (away / home / unknown, so an unknown-side row joins no live side's
        cell but every side union) and ONE when it does not (a pre-0023
        bundle). ~N int32 + a small offsets array; ~0.2 s per hand."""
        meta = self._cell_cache.get(hand)
        if meta is not None:
            return meta
        pool = self.a.pools[hand]
        sit = pool.sit
        rs = sit[:, 3].astype(np.int64) & 0b111
        outs = np.clip(sit[:, 2].astype(np.int64), 0, N_OUTS - 1)
        cb = np.clip(sit[:, 0].astype(np.int64), 0, 3) * 3 + np.clip(
            sit[:, 1].astype(np.int64), 0, 2
        )
        band = score_band_array(sit[:, 5])
        bh = getattr(pool, "bat_home", None)
        # A column that is UNKNOWN on every row (a migration-0023 pool exported
        # before its rebuild filled it) has no side dimension either: every
        # live side's cell would be empty and every draw would widen past the
        # score band. Treat it exactly like an absent column.
        if bh is None or not bool((bh >= 0).any()):
            n_side = 1
            side = np.zeros(pool.n, dtype=np.int64)
        else:
            n_side = 3
            side = np.where(bh > 0, 1, np.where(bh == 0, 0, 2)).astype(np.int64)
        sub = (((rs * N_OUTS + outs) * N_BAND + band) * n_side + side) * N_COUNT + cb
        n_sub = N_BASE * N_OUTS * N_BAND * n_side * N_COUNT
        order = np.argsort(sub, kind="stable").astype(np.int32)
        offsets = np.zeros(n_sub + 1, dtype=np.int64)
        offsets[1:] = np.cumsum(np.bincount(sub, minlength=n_sub))
        meta = {"n_side": n_side, "order": order, "offsets": offsets, "widened": {}}
        self._cell_cache[hand] = meta
        return meta

    def _subcell_rows(
        self, hand: str, rs: int, outs: int, band: int, side: int | None, cb: int
    ) -> tuple[np.ndarray, int]:
        """The rows of one count sub-cell of the live PA cell, WIDENED by
        decision #19's fixed ladder when the sub-cell holds fewer than
        ``pitch_min_cell`` rows: level 1 unions the five score bands, level 2
        the batting sides too, level 3 the twelve counts too. ``side`` None
        means the live side is unknown — level 0 already unions the sides.
        Returns ``(rows, level)``; cached per sub-cell (occupancy is a pool
        fact, not a live-state fact). An empty level-3 cell raises: the
        base-out cell is essential (the SIM-511 wording)."""
        meta = self._cell_meta(hand)
        n_side = meta["n_side"]
        order, off = meta["order"], meta["offsets"]
        key = (rs, outs, band, side, cb)
        cached = meta["widened"].get(key)
        if cached is not None:
            return cached

        def sl(b: int, s: int, c: int) -> np.ndarray:
            sub = (((rs * N_OUTS + outs) * N_BAND + b) * n_side + s) * N_COUNT + c
            return order[off[sub] : off[sub + 1]]

        sides = [side] if side is not None else list(range(n_side))
        need = max(int(self.pitch_min_cell), 1)
        parts = [sl(band, s, cb) for s in sides]
        rows = parts[0] if len(parts) == 1 else np.concatenate(parts)
        level = 0
        if rows.size < need:
            rows = np.concatenate([sl(b, s, cb) for b in range(N_BAND) for s in sides])
            level = 1
            if rows.size < need:
                rows = np.concatenate([sl(b, s, cb) for b in range(N_BAND) for s in range(n_side)])
                level = 2
                if rows.size < need:
                    rows = np.concatenate(
                        [
                            sl(b, s, c)
                            for b in range(N_BAND)
                            for s in range(n_side)
                            for c in range(N_COUNT)
                        ]
                    )
                    level = 3
                    if rows.size == 0:
                        raise RuntimeError(
                            f"SIM-467: base-out cell (runners_state={rs}, outs={outs}) is "
                            f"EMPTY in the {hand}-hand pitch pool — a data defect. The "
                            "base-out cell is essential and the ladder ends there; rebuild "
                            "the pool and investigate."
                        )
        result = (rows, level)
        meta["widened"][key] = result
        return result

    def _new_plate_appearance_cell(
        self,
        batter_key: str,
        base_out: np.ndarray,
        *,
        pitch_count: int | None,
        tto: int | None,
        bat_home: bool | None,
    ) -> None:
        """The SIM-467 cell path of :meth:`new_plate_appearance`: the same
        factors as the whole-pool path, evaluated on the live cell's rows only
        and multiplied in the same order, so every in-cell weight is bit-
        identical to the whole-pool path's weight for that row."""
        hand = self._hand
        assert hand is not None and self._base is not None
        meta = self._pool_meta(hand)
        cmeta = self._cell_meta(hand)
        bo = np.asarray(base_out, dtype=np.float32)
        rs = int(bo[1]) & 0b111
        outs = min(max(int(bo[0]), 0), N_OUTS - 1)
        band = score_band(int(bo[3]))
        side: int | None
        if cmeta["n_side"] == 1 or bat_home is None:
            side = None if cmeta["n_side"] > 1 else 0
        else:
            side = 1 if bat_home else 0
        base = self._base
        aff = self._batter_affinity(batter_key)
        pb = meta["pool_bat"]
        sitb = meta["sit_baseout"]
        recv: np.ndarray | None = None
        if (
            self.catcher_framing_sigma > 0.0 or self.catcher_block_sigma > 0.0
        ) and self._catcher_key is not None:
            recv = self._f_catcher_receiving(hand, self._catcher_key)
        fatigue_on = (self.fatigue_pc_sigma > 0.0 or self.fatigue_tto_sigma > 0.0) and (
            pitch_count is not None or tto is not None
        )
        bh = getattr(self.a.pools[hand], "bat_home", None)
        home_on = bat_home is not None and self.pitch_home_off_weight != 1.0 and bh is not None
        pa_rows: list[np.ndarray] = []
        pa_levels: list[int] = []
        pa_keys: list[tuple] = []
        cdfs: list[np.ndarray | None] = []
        split = self.pitch_result_split
        fpv = self._f_pitcher_vec
        b_w: list[np.ndarray | None] = []
        b_fb: list[np.ndarray | None] = []
        b_fp: list[np.ndarray | None] = []
        for cb in range(N_COUNT):
            rows, level = self._subcell_rows(hand, rs, outs, band, side, cb)
            pa_rows.append(rows)
            pa_levels.append(level)
            pa_keys.append((rs, outs, band, -1 if side is None else side, cb))
            if rows.size == 0:
                cdfs.append(None)
                b_w.append(None)
                b_fb.append(None)
                b_fp.append(None)
                continue
            if aff is not None:
                pbr = pb[rows]
                f_bat = np.where(
                    pbr >= 0, aff[np.clip(pbr, 0, len(aff) - 1)], np.float32(1.0)
                ).astype(np.float32)
            else:
                f_bat = np.ones(rows.size, dtype=np.float32)
            diff: np.ndarray = sitb[rows] - bo
            d2 = np.einsum("ij,ij->i", diff, diff)
            f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * sitb.shape[1])).astype(np.float32)
            w = base[rows] * f_bat * f_sit
            if recv is not None:
                w = w * recv[rows]
            if fatigue_on:
                f_fat = self._fatigue_rows(hand, pitch_count, tto, rows)
                if f_fat is not None:
                    w = w * f_fat
            if home_on:
                assert bh is not None and bat_home is not None
                bhr = bh[rows]
                mismatch = (bhr >= 0) & ((bhr > 0) != bool(bat_home))
                w = w * np.where(mismatch, np.float32(self.pitch_home_off_weight), np.float32(1.0))
            if split:
                # SIM-523 part B: keep the raw weight + factors for the result
                # draw; the pitch draw's batter power applies here only.
                b_w.append(w)
                b_fb.append(f_bat)
                b_fp.append(fpv[rows] if fpv is not None else None)
                w = _repower(w, f_bat, self.pitch_batter_power)
            cdfs.append(np.cumsum(w, dtype=np.float64))
        self._pa_rows = pa_rows
        self._pa_levels = pa_levels
        self._pa_keys = pa_keys
        self._bucket_cdf = cdfs
        if split:
            self._bucket_w, self._bucket_fb, self._bucket_fp = b_w, b_fb, b_fp
        else:
            self._bucket_w = self._bucket_fb = self._bucket_fp = None

    def _fatigue_rows(
        self, hand: str, pitch_count: int | None, tto: int | None, rows: np.ndarray
    ) -> np.ndarray | None:
        """:meth:`_f_fatigue` restricted to ``rows`` (one count sub-cell of the
        live cell): the same Gaussians, normalized to a mean of 1 over the
        sub-cell's valid rows; unknown rows exactly neutral."""
        pool = self.a.pools[hand]
        pc_col = getattr(pool, "pitch_count", None)
        tto_col = getattr(pool, "tto", None)
        out = np.ones(rows.size, dtype=np.float32)
        valid = np.zeros(rows.size, dtype=bool)
        touched = False
        if self.fatigue_pc_sigma > 0.0 and pitch_count is not None and pc_col is not None:
            pcr = pc_col[rows]
            ok = pcr >= 0
            d = pcr.astype(np.float32) - np.float32(pitch_count)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_pc_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if self.fatigue_tto_sigma > 0.0 and tto is not None and tto_col is not None:
            ttr = tto_col[rows]
            ok = ttr > 0
            d = ttr.astype(np.float32) - np.float32(tto)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_tto_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if not touched:
            return None
        if valid.any():
            mean_w = float(out[valid].mean())
            out[valid] = out[valid] / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def cell_index_stats(self) -> dict:
        """SIM-467: draws per widening level (0 = the exact cell) and the
        index shape per hand, for the lane's report."""
        return {
            "enabled": bool(self.pitch_cell_index),
            "min_cell": int(self.pitch_min_cell),
            "draws_by_level": [int(x) for x in self.widen_counts],
            "n_side": {h: int(m["n_side"]) for h, m in self._cell_cache.items()},
        }

    def _f_fatigue(self, hand: str, pitch_count: int | None, tto: int | None) -> np.ndarray | None:
        """SIM-518 (SIM-465): the per-row fatigue factor for the live pitcher's
        pitch count and times through the order — a Gaussian on each |live −
        row| distance (a sigma of 0.0 removes that term), NORMALIZED to a mean
        of 1 within each COUNT BUCKET over the rows that carry a value. A row
        with an unknown value (pitch count -1 / times-through 0, a pre-rebuild
        row) is exactly neutral. None when the pool carries neither column."""
        pool = self.a.pools[hand]
        pc_col = getattr(pool, "pitch_count", None)
        tto_col = getattr(pool, "tto", None)
        out = np.ones(pool.n, dtype=np.float32)
        valid = np.zeros(pool.n, dtype=bool)
        touched = False
        if self.fatigue_pc_sigma > 0.0 and pitch_count is not None and pc_col is not None:
            ok = pc_col >= 0
            d = pc_col.astype(np.float32) - np.float32(pitch_count)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_pc_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if self.fatigue_tto_sigma > 0.0 and tto is not None and tto_col is not None:
            ok = tto_col > 0
            d = tto_col.astype(np.float32) - np.float32(tto)
            f = np.exp(-(d * d) / (2.0 * self.fatigue_tto_sigma**2)).astype(np.float32)
            out = np.where(ok, out * f, out).astype(np.float32)
            valid |= ok
            touched = True
        if not touched:
            return None
        # Per-COUNT-BUCKET normalization over the valid rows (the SIM-517
        # pattern): the factor shifts WHICH pitch is drawn at a count; a
        # fully underflowed bucket goes neutral rather than starving.
        for r in self._pool_meta(hand)["bucket_rows"]:
            if r.size == 0:
                continue
            m = valid[r]
            if not m.any():
                continue
            sel = r[m]
            mean_w = float(out[sel].mean())
            if mean_w > 0.0:
                out[sel] = out[sel] / np.float32(mean_w)
            else:
                out[sel] = np.float32(1.0)
        return out

    def draw(self, balls: int = 0, strikes: int = 0) -> str:
        """Count-conditioned draw of one pitch outcome (SIM-429): restrict to the
        live count's bucket, weighted by the per-PA matchup weights."""
        assert self._bucket_cdf is not None, "call new_plate_appearance first"
        meta = self._pool_cache[self._hand]
        b = min(max(int(balls), 0), 3) * 3 + min(max(int(strikes), 0), 2)
        cdf = self._bucket_cdf[b]
        # SIM-467: on the cell path the bucket's rows are the live cell's
        # sub-cell (global indices, so the got-away / geometry reads below
        # are unchanged); count the draw's widening level.
        if self._pa_rows is not None:
            rows = self._pa_rows[b]
            if self._pa_levels is not None:
                self.widen_counts[self._pa_levels[b]] += 1
        else:
            rows = meta["bucket_rows"][b]
        if cdf is None or cdf[-1] <= 0:
            self._pp_last_i = None
            self._pp_pitch_i = None
            return "ball"
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        gi = int(rows[min(i, rows.size - 1)])
        self._pp_pitch_i = gi  # SIM-523 part B: the pitch thrown
        if self.pitch_result_split and self._bucket_w is not None:
            gi = self._result_draw(b, rows, gi)
        self._pp_last_i = gi  # the RESULT row: the got-away / born-ball reads
        return str(meta["outcome"][gi])

    # ---- SIM-523 part B: the pitch-result draw -----------------------------
    def _pp_geom_z_stats(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per-column mean / std of the pitch pool's geometry over its
        complete rows (all finite, a real velocity), plus that row mask.
        Cached per hand; one pass over the pool."""
        cached = self._pp_geom_stats.get(hand)
        if cached is not None:
            return cached
        g = self.a.pools[hand].geom
        valid = np.isfinite(g).all(axis=1) & (g[:, 0] > 0.0)
        if valid.any():
            mean = g[valid].mean(axis=0).astype(np.float32)
            std = g[valid].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(g.shape[1], dtype=np.float32)
            std = np.ones(g.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._pp_geom_stats[hand] = stats
        return stats

    def _f_result_pitch(self, hand: str, rows: np.ndarray, pitch_gi: int) -> np.ndarray | None:
        """The pitch-to-pitch factor of every candidate row against the drawn
        pitch: a Gaussian on the pitch engine's own metric (z-scored geometry,
        each dimension weighted by the engine's feature weight), bandwidth
        ``result_pitch_sigma`` in per-feature standard deviations, normalized
        to a mean of 1 over the rows with complete geometry (an incomplete row
        is exactly neutral). None when the drawn pitch itself has no complete
        geometry or the bandwidth is 0 — the result is then the pitch row."""
        sigma = float(self.result_pitch_sigma)
        if sigma <= 0.0:
            return None
        mean, std, valid_all = self._pp_geom_z_stats(hand)
        if not bool(valid_all[pitch_gi]):
            return None
        g = self.a.pools[hand].geom
        live = (g[pitch_gi] - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            diff = (g[rows[valid]] - mean) / std - live
            d2 = np.einsum("ij,ij->i", diff * _PITCH_FEATURE_WEIGHTS, diff)
            scale = 2.0 * sigma * sigma * float(_PITCH_FEATURE_WEIGHTS.sum())
            f = np.exp(-d2 / scale).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    #: Reference rows per candidate set for the density estimate.
    _RESULT_DENSITY_REFS = 256

    def _result_inv_density(self, hand: str, key: tuple, rows: np.ndarray) -> np.ndarray:
        """The inverse local density of every candidate row under the result
        kernel — each row's mean kernel value against a fixed random subset
        of the candidate rows (a pool fact: cached per candidate set and
        bandwidth; the subset is drawn from a generator seeded by the key, so
        the sampler's own stream is untouched), raised to
        ``result_density_power``. Floored at 5% of the median density so a
        lone outlier gains at most 20× at power 1, normalized to a mean of 1;
        a row without complete geometry is exactly neutral."""
        meta = self._cell_meta(hand) if key[0] >= 0 else self._pool_meta(hand)
        cache = meta.setdefault("density", {})
        ckey = (key, float(self.result_pitch_sigma), float(self.result_density_power))
        hit = cache.get(ckey)
        if hit is not None:
            return hit
        mean, std, valid_all = self._pp_geom_z_stats(hand)
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        vr = rows[valid]
        if vr.size:
            g = self.a.pools[hand].geom
            sw = np.sqrt(_PITCH_FEATURE_WEIGHTS)
            z = ((g[vr] - mean) / std) * sw  # weighted metric: plain squared distance
            m = int(min(self._RESULT_DENSITY_REFS, vr.size))
            if m < vr.size:
                seed = abs(hash((int(key[0]), int(key[1]), *[int(k) for k in key[2:]]))) % (2**32)
                pick = np.random.default_rng(seed).choice(vr.size, size=m, replace=False)
                ref = z[pick]
            else:
                ref = z
            scale = 2.0 * float(self.result_pitch_sigma) ** 2 * float(_PITCH_FEATURE_WEIGHTS.sum())
            ref_sq = np.einsum("ij,ij->i", ref, ref)
            dens = np.empty(vr.size, dtype=np.float64)
            for start in range(0, vr.size, 8192):
                zb = z[start : start + 8192]
                d2 = np.einsum("ij,ij->i", zb, zb)[:, None] + ref_sq[None, :] - 2.0 * (zb @ ref.T)
                dens[start : start + 8192] = np.exp(-np.maximum(d2, 0.0) / scale).mean(axis=1)
            floor = 0.05 * float(np.median(dens))
            inv = np.power(np.maximum(dens, max(floor, 1e-30)), -float(self.result_density_power))
            inv = inv / inv.mean()
            out[valid] = inv.astype(np.float32)
        cache[ckey] = out
        return out

    def _result_draw(self, b: int, rows: np.ndarray, pitch_gi: int) -> int:
        """Step 4: draw the RESULT row among the pitch draw's candidate rows
        (the same count sub-cell), weighted by the raw per-PA weight × the
        pitch-to-pitch factor × the re-raised pitcher / batter factors. Falls
        back to the pitch row itself when nothing can condition the draw."""
        assert self._bucket_w is not None and self._hand is not None
        w = self._bucket_w[b]
        if w is None or w.size == 0:
            return pitch_gi
        f = self._f_result_pitch(self._hand, rows, pitch_gi)
        if f is None:
            return pitch_gi
        wr = w * f
        if self.result_density_power != 0.0:
            key = self._pa_keys[b] if self._pa_keys is not None else (-1, b)
            wr = wr * self._result_inv_density(self._hand, key, rows)
        if self.result_pitcher_power != 1.0 and self._bucket_fp is not None:
            fp = self._bucket_fp[b]
            if fp is not None:
                wr = _repower(wr, fp, self.result_pitcher_power)
        if self.result_batter_power != 1.0 and self._bucket_fb is not None:
            fb = self._bucket_fb[b]
            if fb is not None:
                wr = _repower(wr, fb, self.result_batter_power)
        cdf = np.cumsum(wr, dtype=np.float64)
        total = float(cdf[-1])
        if not np.isfinite(total) or total <= 0.0:
            return pitch_gi
        j = int(np.searchsorted(cdf, self.rng.random() * total))
        return int(rows[min(j, rows.size - 1)])

    def last_result_row(self) -> int | None:
        """SIM-523 part B: the global pitch-pool index of the last RESULT row
        (the play), or None before any draw / after an empty-bucket fallback.
        Equal to the pitch row while the split is off."""
        return self._pp_last_i

    def last_born_batted_ball(self) -> dict | None:
        """SIM-523 part B: the last result row's OWN batted ball, read through
        the artifact's pitch-id join — ``row`` (the batted-ball pool index),
        ``ev``, ``la``, ``spray`` (pull-relative), ``spray_raw``, ``dist``,
        ``is_air``. None when the last result was not in play, the bundle
        carries no join, or the join has no batted ball for that pitch."""
        i = self._pp_last_i
        if i is None or self._hand is None:
            return None
        br = getattr(self.a.pools[self._hand], "bb_row", None)
        if br is None:
            return None
        j = int(br[i])
        pool = self.a.bb_pools.get(self._hand)
        if j < 0 or pool is None or j >= pool.n:
            return None
        return {
            "row": j,
            "ev": float(pool.geom[j, 0]),
            "la": float(pool.geom[j, 1]),
            "spray": float(pool.geom[j, 2]),
            "spray_raw": (float(pool.spray_raw[j]) if pool.spray_raw is not None else None),
            "dist": (float(pool.hit_dist[j]) if pool.hit_dist is not None else None),
            "is_air": (bool(pool.is_air[j]) if pool.is_air is not None else None),
        }

    def last_pitch_got_away(self) -> bool:
        """SIM-517: did the LAST drawn pitch row get away from its catcher (a
        passed ball / wild pitch, incl. an uncaught third strike)? The drawn
        row IS the play — this flag is its got-away fact. False before any
        draw, after an empty-bucket fallback, or on a pre-0022 bundle."""
        i = self._pp_last_i
        if i is None or self._hand is None:
            return False
        ga = self.a.pools[self._hand].got_away
        if ga is None:
            return False
        return bool(ga[i])

    def last_pitch_geom(self) -> np.ndarray | None:
        """SIM-518 (SIM-472): the LAST drawn pitch row's raw geometry (the ten
        pitch-pool columns, ``_GEOM_COLS`` order), so the batted-ball draw can
        condition on the pitch that produced it. None before any draw, after
        an empty-bucket fallback, or when the row's velocity is missing (the
        export writes a NULL as 0.0, and no real pitch reads 0 mph). SIM-523
        part B: the PITCH-draw row — the pitch thrown — which is the result
        row while the split is off."""
        i = self._pp_pitch_i if self._pp_pitch_i is not None else self._pp_last_i
        if i is None or self._hand is None:
            return None
        g = self.a.pools[self._hand].geom[i]
        if not (float(g[0]) > 0.0) or not bool(np.isfinite(g).all()):
            return None
        return g

    # ---- SIM-517: the catcher RECEIVING factor ----------------------------
    #: The receiving skill, read as rates: the two zone-framing rates, the
    #: block rate (got-aways per pitch received — derived here because the
    #: metrics table stores the count), and the EB uncaught-strike-3 rate.
    _RECV_RATE_FEATURES = ("shadow_zone_strike_rate", "heart_zone_strike_rate")

    def _catcher_receiving_data(self) -> tuple[dict[str, int], np.ndarray] | None:
        """Lazy (key_index, z-matrix) of the derived receiving features over
        every catcher-season in the embedding. None when the embedding or any
        required column is absent (the kernel then stays neutral)."""
        if self._recv_data is not False:
            return self._recv_data  # type: ignore[return-value]
        cemb = self.a.actor_emb.get("catcher")
        out: tuple[dict[str, int], np.ndarray] | None = None
        if cemb is not None:
            feats = list(cemb.get("features") or [])
            fi = {f: i for i, f in enumerate(feats)}
            need = (*self._RECV_RATE_FEATURES, "actual_pbwp", "pitches_received_total")
            if all(f in fi for f in need):
                vecs = np.asarray(cemb["vecs"], dtype=np.float64)
                cols = [vecs[:, fi[f]] for f in self._RECV_RATE_FEATURES]
                # The block rate: got-aways per pitch received.
                tot = vecs[:, fi["pitches_received_total"]]
                with np.errstate(divide="ignore", invalid="ignore"):
                    cols.append(np.where(tot > 0, vecs[:, fi["actual_pbwp"]] / tot, np.nan))
                # The EB uncaught-K3 rate (part A; optional on an older export).
                if "uncaught_k3_rate_eb" in fi:
                    cols.append(vecs[:, fi["uncaught_k3_rate_eb"]])
                mat = np.stack(cols, axis=1)
                mean = np.nan_to_num(np.nanmean(mat, axis=0))
                std = np.nan_to_num(np.nanstd(mat, axis=0))
                std[std <= 0] = 1.0
                z = (mat - mean) / std
                out = (dict(cemb["key_index"]), z)
        self._recv_data = out
        return out

    def _pp_catcher_recv_idx(self, hand: str) -> np.ndarray | None:
        """Each pitch-pool row's receiving-matrix index, keyed
        ``catcher_id:row_season`` (-1 when absent). None on a pre-0022 bundle
        or with no receiving data."""
        cached = self._pp_recv_idx.get(hand)
        if cached is not None:
            return cached
        pool = self.a.pools[hand]
        if pool.catcher_id is None:
            return None
        data = self._catcher_receiving_data()
        if data is None:
            return None
        ki = data[0]
        idx = np.fromiter(
            (
                ki.get(f"{int(c)}:{int(s)}", -1)
                for c, s in zip(pool.catcher_id, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=pool.n,
        )
        self._pp_recv_idx[hand] = idx
        return idx

    def _f_catcher_receiving(self, hand: str, catcher_key: str) -> np.ndarray | None:
        """The per-row receiving factor for the LIVE catcher: a Gaussian on
        the z-distance between the live catcher's receiving vector and each
        row catcher's, NORMALIZED to a mean of 1 within each COUNT BUCKET.

        The SIM-476 lessons, applied from day one: the factor may shift only
        WHICH pitch is drawn at a count — never the count-bucket mass (the
        cross-partition redistribution that redded the lane) — and a row with
        no embedded catcher (or a NaN receiving row, or a bucket whose every
        weight underflows) is exactly neutral, never favored or starved.
        None when the data is unavailable — the draw is then unweighted."""
        cache_key = (hand, catcher_key)
        if cache_key in self._recv_factor_cache:
            return self._recv_factor_cache[cache_key]
        out: np.ndarray | None = None
        data = self._catcher_receiving_data()
        rows_idx = self._pp_catcher_recv_idx(hand)
        if data is not None and rows_idx is not None:
            ki, z = data
            live = ki.get(catcher_key, -1)
            if live >= 0 and np.isfinite(z[live]).all():
                row_z = z[np.clip(rows_idx, 0, len(z) - 1)]
                valid = (rows_idx >= 0) & np.isfinite(row_z).all(axis=1)
                out = np.ones(len(rows_idx), dtype=np.float32)
                if valid.any():
                    diff = row_z[valid] - z[live]
                    # Anisotropic metric: framing dims (0,1) and blocking dims
                    # (2..) under their own bandwidths; a group with sigma 0 is
                    # excluded (OFF), never an accidental hard filter.
                    expo = np.zeros(diff.shape[0], dtype=np.float64)
                    if self.catcher_framing_sigma > 0.0:
                        df = diff[:, :2]
                        expo += np.einsum("ij,ij->i", df, df) / (
                            2.0 * self.catcher_framing_sigma**2 * df.shape[1]
                        )
                    if self.catcher_block_sigma > 0.0 and diff.shape[1] > 2:
                        db = diff[:, 2:]
                        expo += np.einsum("ij,ij->i", db, db) / (
                            2.0 * self.catcher_block_sigma**2 * db.shape[1]
                        )
                    out[valid] = np.exp(-expo).astype(np.float32)
                    # Per-COUNT-BUCKET normalization: mean factor 1 among the
                    # valid rows of each bucket; a fully underflowed bucket
                    # goes neutral rather than starving of draws.
                    for r in self._pool_meta(hand)["bucket_rows"]:
                        if r.size == 0:
                            continue
                        m = valid[r]
                        if not m.any():
                            continue
                        sel = r[m]
                        mean_w = float(out[sel].mean())
                        if mean_w > 0.0:
                            out[sel] = out[sel] / np.float32(mean_w)
                        else:
                            out[sel] = np.float32(1.0)
        self._recv_factor_cache[cache_key] = out
        return out

    # ---- SIM-425: batted-ball draw (step 5) -------------------------------
    def _batter_aff(self, batter_key: str) -> np.ndarray | None:
        """Per-batter-embedding RBF affinity to the current batter (None if absent).

        SIM-430: delegates to the memoized :meth:`_batter_affinity` so the
        batted-ball draw reuses the affinity the pitch-pool factor already computed
        for this batter (was a duplicate full einsum+exp + a per-call vecs_z
        recompute)."""
        return self._batter_affinity(batter_key)

    def _bb_pool_bat_idx(self, hand: str) -> np.ndarray:
        if hand in self._bb_pool_bat:
            return self._bb_pool_bat[hand]
        pool = self.a.bb_pools[hand]
        bemb = self.a.actor_emb.get("batter")
        if bemb is None:
            pb = np.full(pool.n, -1, dtype=np.int64)
        else:
            ki = bemb["key_index"]
            pb = np.fromiter(
                (
                    ki.get(f"{int(b)}:{int(s)}", -1)
                    for b, s in zip(pool.batter_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        self._bb_pool_bat[hand] = pb
        return pb

    def _bb_park_factors(self, hand: str) -> np.ndarray | None:
        """SIM-491 part 2 (SIM-411): the per-row park run factor for the hand's
        batted-ball pool, from ``venue_run_factors`` keyed (venue_id, row season)
        with a venue-only mean fallback; 1.0 for unknown venues. None when the
        pool carries no per-row ``venue_id`` (a pre-0012 bundle) or no factor
        map is loaded — the caller then leaves the draw unweighted."""
        cached = self._bb_park.get(hand)
        if cached is not None:
            return cached
        vf = self.venue_run_factors
        pool = self.a.bb_pools[hand]
        vid = getattr(pool, "venue_id", None)
        if not vf or vid is None:
            return None
        # Venue-only mean fallback for (venue, season) pairs the map lacks.
        by_venue: dict[int, list[float]] = {}
        for (v, _s), f in vf.items():
            by_venue.setdefault(int(v), []).append(float(f))
        venue_mean = {v: sum(fs) / len(fs) for v, fs in by_venue.items()}
        out = np.fromiter(
            (
                vf.get((int(v), int(s)), venue_mean.get(int(v), 1.0))
                for v, s in zip(vid, pool.season, strict=False)
            ),
            dtype=np.float32,
            count=pool.n,
        )
        self._bb_park[hand] = out
        return out

    #: SIM-491 part 3: the OAA-centred feature subset of the fielder embedding
    #: (derived.fielder_season_metrics). v1 is the range-quality core; the
    #: SIM-476 fit may widen it toward the arm features the advancement draws
    #: already use.
    _FIELDER_BB_FEATURES = ("outs_above_average",)

    def _bb_fielder_emb_rows(self, hand: str) -> np.ndarray | None:
        """SIM-491 part 3: each batted-ball row's fielder-embedding index, keyed
        ``fielder_id:POS:row_season`` (the row's CONTEMPORANEOUS season — the
        SIM-425b survivorship lesson); -1 when absent. None when the pool has no
        fielder columns (a pre-0012 bundle) or no fielder embedding is loaded."""
        cached = self._bb_fielder_emb.get(hand)
        if cached is not None:
            return cached
        pool = self.a.bb_pools[hand]
        pos = getattr(pool, "fielder_pos", None)
        fid = getattr(pool, "fielder_id", None)
        emb = self.a.actor_emb.get("fielder")
        if pos is None or fid is None or emb is None:
            return None
        ki = emb["key_index"]
        out = np.fromiter(
            (
                ki.get(f"{int(f)}:{_POS_NUM_TO_NAME.get(int(p), '?')}:{int(s)}", -1)
                for f, p, s in zip(fid, pos, pool.season, strict=False)
            ),
            dtype=np.int64,
            count=pool.n,
        )
        self._bb_fielder_emb[hand] = out
        return out

    def _fielder_matrices_on(self) -> bool:
        """SIM-523 part A: the matrix path applies whenever the switch is on
        and the bundle carries at least one per-position fielder matrix."""
        return self.actor_matrices and any(
            self._actor_matrix(f"fielder_{n}") is not None for n in _POS_NUM_TO_NAME.values()
        )

    def _f_live_fielder(
        self, hand: str, rows: np.ndarray, defense_map: dict[str, int], season: int
    ) -> np.ndarray | None:
        """SIM-491 part 3: per-row Gaussian similarity between the LIVE defender
        at the row's position and the row's own fielder, over
        :data:`_FIELDER_BB_FEATURES`. 1.0 (neutral) for rows where either side
        is absent from the embedding; None when the factor is unavailable."""
        z = self._emb_z("fielder")
        emb = self.a.actor_emb.get("fielder")
        cols = self._steal_feat_cols("fielder", self._FIELDER_BB_FEATURES)
        row_emb = self._bb_fielder_emb_rows(hand)
        if z is None or emb is None or cols is None or row_emb is None:
            return None
        ki = emb["key_index"]
        # The live defender's embedding index per position NUMBER (the row's
        # fielder_pos vocabulary), -1 when the defense map / embedding lacks it.
        live_by_pos = np.full(10, -1, dtype=np.int64)
        for p, name in _POS_NUM_TO_NAME.items():
            pid = defense_map.get(name)
            if pid:
                live_by_pos[p] = ki.get(f"{int(pid)}:{name}:{int(season)}", -1)
        pool = self.a.bb_pools[hand]
        pos = np.clip(np.asarray(pool.fielder_pos)[rows].astype(np.int64), 0, 9)
        live_idx = live_by_pos[pos]
        row_idx = row_emb[rows]
        valid = (live_idx >= 0) & (row_idx >= 0)
        out = np.ones(len(rows), dtype=np.float32)
        if not valid.any():
            return out
        if self._fielder_matrices_on():
            # SIM-523 part A: the fielder engine's per-position score matrix,
            # gathered per position; rows at a position without a matrix or a
            # live key stay neutral. The per-position mean-1 rescale below still
            # applies (a factor must never move balls between positions).
            valid = np.zeros(len(rows), dtype=bool)
            for p, name in _POS_NUM_TO_NAME.items():
                at = pos == p
                if not at.any():
                    continue
                pid = defense_map.get(name)
                if not pid:
                    continue
                f = self._matrix_gather(
                    f"fielder_{name}", f"{int(pid)}:{name}:{int(season)}", row_idx[at]
                )
                if f is None:
                    continue
                out[at] = f
                valid |= at & (row_idx >= 0)
        else:
            diff = z[row_idx[valid]][:, cols] - z[live_idx[valid]][:, cols]
            d2 = np.einsum("ij,ij->i", diff, diff)
            out[valid] = np.exp(-d2 / (2.0 * self.fielder_sigma**2 * len(cols))).astype(np.float32)
        # SIM-476: the factor must not move batted balls BETWEEN positions.
        # Where a ball goes is batted-ball physics (the batter/situation
        # kernels); the fielder factor's job is to pick WHICH play at that
        # position, given the live defender. Raw Gaussian weights break that:
        # a position whose live defender sits near the middle of the OAA
        # distribution outweighs a position with an extreme defender, so the
        # draw redistributes balls toward well-matched positions (measured:
        # the OF share of drawn balls rose 52.7% -> 57.4% at sigma=0.5, which
        # alone inflates hits ~+6% — the 2026-09-01 lane red). Normalizing to
        # a MEAN of 1 within each position keeps the within-position
        # discrimination and kills the cross-position shift; it also makes
        # missing-identity rows (weight 1.0) exactly draw-neutral.
        for p in np.unique(pos[valid]):
            m = valid & (pos == p)
            mean_w = float(out[m].mean())
            if mean_w > 0.0:
                out[m] = out[m] / np.float32(mean_w)
            else:
                # Every weight at this position underflowed (an extreme live
                # defender in float32): the factor has no usable discrimination
                # here, so it goes NEUTRAL — never zero, which would starve the
                # position of batted balls entirely.
                out[m] = np.float32(1.0)
        return out

    def _bb_same_hand_mask(self, hand: str, pitcher_throws: str) -> np.ndarray | None:
        """SIM-413: boolean mask of batted-ball pool rows whose pitcher threw the
        SAME hand as ``pitcher_throws``. None when the pool carries no per-row
        ``p_throws`` (a legacy bundle) so the caller leaves the draw unweighted."""
        pool = self.a.bb_pools[hand]
        pt = getattr(pool, "p_throws", None)
        if pt is None:
            return None
        tr = self._bb_throws_r.get(hand)
        if tr is None:
            tr = np.asarray(pt, dtype=object) == "R"
            self._bb_throws_r[hand] = tr
        return tr if pitcher_throws == "R" else ~tr

    def has_transition(self, hand: str) -> bool:
        """SIM-511: True when this hand's batted-ball pool carries the SIM-510
        transition columns (a sim510.1+ bundle). False = a data defect: SIM-486
        deleted the legacy soft draw, so the batted-ball draw raises."""
        pool = self.a.bb_pools.get(hand)
        return (
            pool is not None
            and getattr(pool, "r1_dest", None) is not None
            and getattr(pool, "dest_ok", None) is not None
            and getattr(pool, "is_air", None) is not None
        )

    def _transition_meta(self, hand: str) -> dict | None:
        """SIM-511 per-hand one-time precompute: the base-out cell index over
        CONSISTENT transition rows (``dest_ok`` — SIM-510's outs-accounting
        guard) plus the soft-kernel situation columns.

        The hard filter is the base-out cell ALONE — 8 runner configurations
        × 3 out states = 24 cells, all common (owner ruling 2026-08-19). The
        filter is essential, so it never relaxes; the count stays SOFT
        conditioning; an EMPTY cell raises as a data defect. Never widen.
        """
        meta = self._bb_meta_cache.get(hand)
        if meta is not None:
            return meta
        if not self.has_transition(hand):
            return None
        pool = self.a.bb_pools[hand]
        ok = pool.dest_ok.astype(bool)
        outs = pool.sit[:, 2].astype(np.int64)
        rs = pool.sit[:, 3].astype(np.int64)
        cells: dict[tuple[int, int], np.ndarray] = {}
        for rstate in range(8):
            for o in range(3):
                rows = np.nonzero(ok & (rs == rstate) & (outs == o))[0]
                if len(rows):
                    cells[(rstate, o)] = rows
        # An empty cell raises AT DRAW TIME (battedball_new_pa), where the
        # live state names the hole — the defect surfaces on first contact.
        # The soft situation kernel runs over the NON-exact dims only:
        # balls, strikes, inning, score_diff (sit cols 0, 1, 4, 5).
        meta = {"cells": cells, "soft": np.ascontiguousarray(pool.sit[:, [0, 1, 4, 5]])}
        self._bb_meta_cache[hand] = meta
        return meta

    def battedball_new_pa(
        self,
        hand: str,
        batter_key: str,
        state: np.ndarray,
        pitcher_throws: str | None = None,
        bat_home: bool | None = None,
        park_run_factor: float | None = None,
        defense_map: dict[str, int] | None = None,
        live_season: int | None = None,
        pitch_geom: np.ndarray | None = None,
        born_bb: dict | None = None,
    ) -> None:
        """Assemble the batted-ball weight CDF for the PA (f_batter · f_situation · recency).

        SIM-523 part B: when ``born_bb`` (the result row's own batted ball,
        :meth:`last_born_batted_ball`) is supplied AND :attr:`bb_born_sigma`
        > 0, a Gaussian on the z-scored (exit velocity, launch angle, spray,
        distance) distance between that ball and each cell row's pulls the
        draw toward plays on similar batted balls — the ball born in the
        pitch-result draw is what gets fielded. Omitted / sigma 0 -> unchanged.

        SIM-518 (SIM-472): when ``pitch_geom`` (the DRAWN pitch's ten geometry
        values, :meth:`last_pitch_geom`) is supplied AND :attr:`bb_pitch_sigma`
        > 0 AND the pool carries ``pgeom``, a Gaussian on the z-scored distance
        between that pitch and each row's own pitch pulls the draw toward
        batted balls hit off similar pitches — the pitch and the contact
        agree. Omitted / sigma 0 / a pre-sim518 export -> the draw is unchanged.

        SIM-511: the draw HARD-filters the exact base-out cell (the drawn row
        must be legal in the live state — that is what makes "the drawn row is
        the play" safe), and the situation kernel runs over the remaining soft
        dims (balls, strikes, inning, score_diff). A bundle without the
        transition columns raises (SIM-486 deleted the legacy soft draw).

        SIM-413: when ``pitcher_throws`` ('L'/'R') is supplied AND the pool carries
        per-row ``p_throws``, softly reweight toward same-hand-matchup rows (opposite
        hand rows ×:attr:`platoon_off_weight`) so the drawn batted ball reflects the
        live platoon matchup. Omitted / legacy pool -> the draw is unchanged.

        SIM-491: when ``bat_home`` is supplied AND the pool carries per-row
        ``bat_home`` (migration 0019), softly reweight toward rows whose batting
        side matches the live one (mismatched rows ×:attr:`home_off_weight`) —
        the SIM-412 home-field advantage as a draw weight. Omitted / weight 1.0 /
        legacy pool -> the draw is unchanged.

        SIM-491 part 2 (SIM-411): when ``park_run_factor`` is supplied,
        :attr:`park_sigma` > 0 AND a venue-factor map is loaded, a Gaussian on
        the |live − row| park-factor delta pulls the draw toward
        run-environment-similar parks. Omitted / sigma 0 / no map -> the draw
        is unchanged.

        SIM-491 part 3 (SIM-425b): when ``defense_map`` (position name ->
        live defender id) is supplied AND :attr:`fielder_sigma` > 0, each row
        is weighted by the similarity between the LIVE defender at the row's
        position and the row's own fielder (:data:`_FIELDER_BB_FEATURES`).
        Omitted / sigma 0 / a pre-0012 bundle -> the draw is unchanged."""
        self._bb_hand = hand
        pool = self.a.bb_pools[hand]
        sv = np.asarray(state, dtype=np.float32)
        meta = self._transition_meta(hand)
        if meta is None:
            raise RuntimeError(
                f"SIM-486: the {hand}-hand batted-ball pool carries no transition "
                "columns (r1_dest / dest_ok / is_air) — a pre-sim510.1 bundle. Rebuild "
                "the engine artifacts; there is no legacy soft-draw path."
            )
        aff = self._batter_aff(batter_key)
        pb = self._bb_pool_bat_idx(hand) if aff is not None else None
        # --- SIM-511: the hard base-out cell ---------------------------
        rstate = int(sv[3]) & 0b111
        o = min(max(int(sv[2]), 0), 2)
        rows = meta["cells"].get((rstate, o))
        if rows is None:
            raise RuntimeError(
                f"SIM-511: base-out cell (runners_state={rstate}, outs={o}) is "
                f"EMPTY in the {hand}-hand batted-ball pool — a data defect. "
                "The base-out filter is essential and never widens (owner "
                "ruling 2026-08-19); rebuild the pool and investigate."
            )
        if aff is not None and pb is not None:
            pbr = pb[rows]
            f_bat = np.where(pbr >= 0, aff[np.clip(pbr, 0, len(aff) - 1)], np.float32(1.0)).astype(
                np.float32
            )
        else:
            f_bat = np.ones(len(rows), dtype=np.float32)
        diff = meta["soft"][rows] - sv[[0, 1, 4, 5]]
        d2 = np.einsum("ij,ij->i", diff, diff)
        f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * diff.shape[1])).astype(np.float32)
        w = f_bat * f_sit * pool.recency[rows]
        if pitcher_throws and self.platoon_off_weight != 1.0:
            same = self._bb_same_hand_mask(hand, pitcher_throws)
            if same is not None:
                w = w * np.where(same[rows], np.float32(1.0), np.float32(self.platoon_off_weight))
        if bat_home is not None and self.home_off_weight != 1.0:
            bh = getattr(pool, "bat_home", None)
            if bh is not None:
                match = (bh[rows] > 0) == bool(bat_home)
                w = w * np.where(match, np.float32(1.0), np.float32(self.home_off_weight))
        if park_run_factor is not None and self.park_sigma > 0.0:
            pf = self._bb_park_factors(hand)
            if pf is not None:
                d = pf[rows] - np.float32(park_run_factor)
                w = w * np.exp(-(d * d) / (2.0 * self.park_sigma**2)).astype(np.float32)
        if defense_map and (self.fielder_sigma > 0.0 or self._fielder_matrices_on()):
            ff = self._f_live_fielder(hand, rows, defense_map, int(live_season or 0))
            if ff is not None:
                w = w * ff
        if pitch_geom is not None and self.bb_pitch_sigma > 0.0:
            fpg = self._f_pitch_similarity(hand, rows, pitch_geom)
            if fpg is not None:
                w = w * fpg
        if born_bb is not None and self.bb_born_sigma > 0.0:
            fbb = self._f_born_similarity(hand, rows, born_bb)
            if fbb is not None:
                w = w * fbb
        self._bb_rows = rows
        self._bb_cdf = np.cumsum(w, dtype=np.float64)

    # ---- SIM-523 part B: the born batted ball on the fielding draw ---------
    def _bb_born_features(self, hand: str) -> np.ndarray:
        """The batted-ball pool's (exit velocity, launch angle, spray[,
        distance]) matrix — distance only when the pool carries it."""
        pool = self.a.bb_pools[hand]
        if pool.hit_dist is not None:
            return np.column_stack([pool.geom[:, :3], pool.hit_dist]).astype(np.float32)
        return np.ascontiguousarray(pool.geom[:, :3], dtype=np.float32)

    def _bb_born_z_stats(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cached = self._bb_born_stats.get(hand)
        if cached is not None:
            return cached
        x = self._bb_born_features(hand)
        valid = np.isfinite(x).all(axis=1) & (x[:, 0] > 0.0)
        if valid.any():
            mean = x[valid].mean(axis=0).astype(np.float32)
            std = x[valid].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(x.shape[1], dtype=np.float32)
            std = np.ones(x.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._bb_born_stats[hand] = stats
        return stats

    def _f_born_similarity(self, hand: str, rows: np.ndarray, born: dict) -> np.ndarray | None:
        """The batted-ball similarity factor over the cell ``rows`` against the
        born ball: a Gaussian on the z-scored feature distance, normalized to
        a mean of 1 over the rows with complete data (an incomplete row is
        exactly neutral). None when the born ball is incomplete."""
        mean, std, valid_all = self._bb_born_z_stats(hand)
        x = self._bb_born_features(hand)
        live_vals = [born.get("ev"), born.get("la"), born.get("spray")]
        if x.shape[1] == 4:
            live_vals.append(born.get("dist"))
        if any(v is None for v in live_vals):
            return None
        live_arr = np.asarray(live_vals, dtype=np.float32)
        if not bool(np.isfinite(live_arr).all()) or not (float(live_arr[0]) > 0.0):
            return None
        live = (live_arr - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            diff = (x[rows[valid]] - mean) / std - live
            d2 = np.einsum("ij,ij->i", diff, diff)
            f = np.exp(-d2 / (2.0 * self.bb_born_sigma**2 * diff.shape[1])).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def _bb_pgeom_z_stats(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """SIM-518 (SIM-472): per-column mean/std of the batted-ball pool's
        producing-pitch geometry over its all-finite rows, plus that row mask.
        Cached per hand; None when the pool carries no ``pgeom``."""
        cached = self._bb_pgeom_stats.get(hand)
        if cached is not None:
            return cached
        pg = getattr(self.a.bb_pools[hand], "pgeom", None)
        if pg is None:
            return None
        valid = np.isfinite(pg).all(axis=1)
        if valid.any():
            mean = pg[valid].mean(axis=0).astype(np.float32)
            std = pg[valid].std(axis=0).astype(np.float32)
        else:
            mean = np.zeros(pg.shape[1], dtype=np.float32)
            std = np.ones(pg.shape[1], dtype=np.float32)
        std = np.where(std > 1e-6, std, np.float32(1.0)).astype(np.float32)
        stats = (mean, std, valid)
        self._bb_pgeom_stats[hand] = stats
        return stats

    def _f_pitch_similarity(
        self, hand: str, rows: np.ndarray, pitch_geom: np.ndarray
    ) -> np.ndarray | None:
        """SIM-518 (SIM-472): the per-row pitch-similarity factor over the
        cell ``rows`` — a Gaussian on the z-scored 10-dim distance between
        the drawn pitch and each row's producing pitch, NORMALIZED to a mean
        of 1 over the rows with complete geometry; a row missing any value is
        exactly neutral. None when the pool carries no ``pgeom``."""
        stats = self._bb_pgeom_z_stats(hand)
        if stats is None:
            return None
        mean, std, valid_all = stats
        pg = self.a.bb_pools[hand].pgeom
        assert pg is not None
        live = (np.asarray(pitch_geom, dtype=np.float32) - mean) / std
        valid = valid_all[rows]
        out = np.ones(len(rows), dtype=np.float32)
        if valid.any():
            z = (pg[rows[valid]] - mean) / std
            diff = z - live
            d2 = np.einsum("ij,ij->i", diff, diff)
            f = np.exp(-d2 / (2.0 * self.bb_pitch_sigma**2 * diff.shape[1])).astype(np.float32)
            mean_w = float(f.mean())
            out[valid] = f / np.float32(mean_w) if mean_w > 0.0 else np.float32(1.0)
        return out

    def battedball_draw(self) -> tuple[str, int, int, float]:
        """Draw one batted ball -> (event, result_hits, result_outs, launch_angle).

        SIM-425: launch_angle (geom col 1) is returned so the resolver can tell a
        fly out (tag-up eligible) from a ground out for productive-out advancement.
        SIM-511: on a transition bundle the drawn index maps through the PA's
        base-out cell; read the row's transition via :meth:`last_transition`.
        """
        if self._bb_hand is None or self._bb_cdf is None or self._bb_cdf[-1] <= 0:
            self._bb_last_i = None
            return ("field_out", 0, 1, 0.0)
        pool = self.a.bb_pools[self._bb_hand]
        i = int(np.searchsorted(self._bb_cdf, self.rng.random() * self._bb_cdf[-1]))
        if self._bb_rows is not None:
            i = int(self._bb_rows[min(i, len(self._bb_rows) - 1)])
        else:
            i = min(i, pool.n - 1)
        self._bb_last_i = i  # SIM-425b: remember the row for the fielder lookup
        return (
            str(pool.event[i]),
            int(pool.result_hits[i]),
            int(pool.result_outs[i]),
            float(pool.geom[i, 1]),
        )

    def last_transition(self) -> dict | None:
        """SIM-511: the drawn row's full transition, or None when no draw has
        happened yet.

        Keys: ``r1``/``r2``/``r3`` — the destination of the pre-pitch runner
        on that base (-1 = no runner; 4 = scored; 3/2/1 = the post base;
        0 = retired); ``batter`` — the batter-runner's destination (0 = out);
        ``adv1``/``adv2``/``adv3`` — thrown-out-advancing flags (a
        discretionary out, never a force or a doubled-off runner);
        ``is_air`` — a caught-ball row (the tag-up shape); ``ev``/``spray``/
        ``dist`` — the throw geometry for the SIM-512 advancement kernel.
        """
        i = self._bb_last_i
        if i is None or self._bb_hand is None or not self.has_transition(self._bb_hand):
            return None
        pool = self.a.bb_pools[self._bb_hand]
        if pool.r1_adv_out is None or pool.spray_raw is None or pool.hit_dist is None:
            return None
        return {
            "r1": int(pool.r1_dest[i]),
            "r2": int(pool.r2_dest[i]),
            "r3": int(pool.r3_dest[i]),
            "batter": int(pool.batter_dest[i]),
            "adv1": bool(pool.r1_adv_out[i]),
            "adv2": bool(pool.r2_adv_out[i]),
            "adv3": bool(pool.r3_adv_out[i]),
            "is_air": bool(pool.is_air[i]),
            "ev": float(pool.geom[i, 0]),
            "spray": float(pool.spray_raw[i]),
            "dist": float(pool.hit_dist[i]),
        }

    def last_battedball_fielder(self) -> tuple[int, int, int] | None:
        """SIM-425b: ``(fielded_by_position, fielder_player_id, season)`` of the row
        the last :meth:`battedball_draw` returned, or None when the pool lacks the
        fielder columns (a legacy bundle) or no draw has happened.

        The ``season`` is the pool ROW's own season, so the caller scores that
        fielder by their CONTEMPORANEOUS OAA — not the game season, which would drop
        any pool fielder lacking a game-season row (a survivorship filter that biases
        the live-vs-pool comparison)."""
        i = self._bb_last_i
        if i is None or self._bb_hand is None:
            return None
        pool = self.a.bb_pools[self._bb_hand]
        pos, fid = getattr(pool, "fielder_pos", None), getattr(pool, "fielder_id", None)
        if pos is None or fid is None:
            return None
        return (int(pos[i]), int(fid[i]), int(pool.season[i]))

    def fielder_quality(self, fielder_id: int, position: str, season: int) -> float | None:
        """SIM-425b: the fielder's outs-above-average (per the fielder embedding,
        keyed ``player_id:position:season``), or None when the fielder / feature is
        absent. A higher value == a better defender at that position; the resolver
        nudges out↔hit by the delta between the live defender and the pool play's
        fielder. Returns None (-> neutral) on a legacy bundle or an unknown fielder."""
        femb = self.a.actor_emb.get("fielder")
        if femb is None or not fielder_id:
            return None
        feats = femb.get("features")
        idx = femb["key_index"].get(f"{int(fielder_id)}:{position}:{int(season)}")
        if feats is None or idx is None:
            return None
        if self._fld_idx is None:
            self._fld_idx = {f: i for i, f in enumerate(feats)}
        oaa_i = self._fld_idx.get("outs_above_average")
        if oaa_i is None:
            return None
        v = float(femb["vecs"][idx][oaa_i])
        return v if np.isfinite(v) else None

    def has_battedball(self) -> bool:
        return bool(self.a.bb_pools)

    # ---- SIM-425: engine-backed baserunner advancement rates --------------
    def runner_rate(self, runner_key: str, name: str) -> float | None:
        """Return a baserunner's raw advancement rate (e.g. ``second_to_home_attempt_rate``,
        ``tag_up_attempt_rate``) from the baserunner embedding, or None when the
        runner / feature is absent so the caller can fall back to a league constant."""
        bemb = self.a.actor_emb.get("baserunner")
        if bemb is None:
            return None
        feats = bemb.get("features")
        idx = bemb["key_index"].get(runner_key)
        if feats is None or idx is None or name not in self._br_feat_idx(feats):
            return None
        v = float(bemb["vecs"][idx][self._br_feat_idx(feats)[name]])
        return v if np.isfinite(v) else None

    def _br_feat_idx(self, feats: list) -> dict[str, int]:
        if self._br_idx is None:
            self._br_idx = {f: i for i, f in enumerate(feats)}
        return self._br_idx

    # ---- SIM-428: catcher framing -----------------------------------------

    # ---- SIM-474: the steal draw over the opportunity pool -----------------

    #: The steal-relevant feature subsets. Each actor's similarity is a gaussian
    #: kernel over THESE z-scored columns only, so a runner is "similar" by how
    #: he runs, not by how he tags up.
    _RUNNER_STEAL_FEATURES = ("sprint_speed", "sb_attempt_rate", "sb_success_rate", "cs_rate")
    _CATCHER_STEAL_FEATURES = (
        "pop_time_mean",
        "arm_strength_mean",
        "cs_rate",
        "steal_attempt_rate_against",
    )
    #: SIM-504 item 3 added pickoff_rate/stepoff_rate (raw.play_events
    #: disengagements). `_steal_feat_cols` skips names a legacy artifact lacks,
    #: so an old bundle degrades to the 3-feature kernel instead of failing.
    _PITCHER_STEAL_FEATURES = (
        "sb_against_per_9",
        "cs_rate_forced",
        "steal_attempt_rate_allowed",
        "pickoff_rate",
        "stepoff_rate",
    )

    def has_steal_pool(self) -> bool:
        return bool(self.a.steal_pools)

    def _emb_z(self, actor: str) -> np.ndarray | None:
        """The z-scored embedding matrix for ``actor`` (memoized)."""
        z = self._steal_emb_z.get(actor)
        if z is not None:
            return z
        emb = self.a.actor_emb.get(actor)
        if emb is None:
            return None
        vecs, mean, std = emb.get("vecs"), emb.get("mean"), emb.get("std")
        if vecs is None or mean is None or std is None:
            return None
        z = ((vecs - mean) / std).astype(np.float32)
        self._steal_emb_z[actor] = z
        return z

    def _steal_meta(self, target: str) -> dict | None:
        """Per-target one-time precompute: the (outs, balls, strikes) cell index
        plus per-row embedding-row gathers for the three actors."""
        meta = self._steal_meta_cache.get(target)
        if meta is not None:
            return meta
        pool = self.a.steal_pools.get(target)
        if pool is None or pool.n == 0:
            return None
        sit = pool.sit  # cols: count_balls, count_strikes, outs, score_diff
        cells: dict[tuple[int, int, int], np.ndarray] = {}
        key = (sit[:, 2].astype(np.int64) * 100 + sit[:, 0].astype(np.int64) * 10) + sit[
            :, 1
        ].astype(np.int64)
        order = np.argsort(key, kind="stable")
        sorted_keys = key[order]
        bounds = np.searchsorted(sorted_keys, np.unique(sorted_keys))
        uniq = np.unique(sorted_keys)
        for i, k in enumerate(uniq):
            lo = bounds[i]
            hi = bounds[i + 1] if i + 1 < len(bounds) else len(order)
            cells[(int(k) // 100, (int(k) % 100) // 10, int(k) % 10)] = order[lo:hi]

        def _rows(actor: str, ids: np.ndarray) -> np.ndarray | None:
            emb = self.a.actor_emb.get(actor)
            if emb is None:
                return None
            kidx = emb["key_index"]
            return np.fromiter(
                (
                    kidx.get(f"{int(a)}:{int(s)}", -1)
                    for a, s in zip(ids, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )

        meta = {
            "cells": cells,
            "runner_rows": _rows("baserunner", pool.runner_id),
            "pitcher_rows": _rows("pitcher_steal", pool.pitcher_id),
            "catcher_rows": _rows("catcher", pool.catcher_id),
        }
        self._steal_meta_cache[target] = meta
        return meta

    def _steal_feat_cols(self, actor: str, names: tuple[str, ...]) -> np.ndarray | None:
        emb = self.a.actor_emb.get(actor)
        if emb is None:
            return None
        feats = emb.get("features")
        if feats is None:
            return None
        fmap = {f: i for i, f in enumerate(feats)}
        cols = [fmap[n] for n in names if n in fmap]
        return np.asarray(cols, dtype=np.int64) if cols else None

    def _steal_actor_factor(
        self,
        actor: str,
        live_key: str,
        emb_rows_all: np.ndarray | None,
        rows: np.ndarray,
        feat_names: tuple[str, ...],
        sigma: float | None = None,
        matrix: str | None = None,
    ) -> np.ndarray | None:
        """Gaussian similarity between the LIVE actor and each pool row's actor
        over the given feature subset; 1.0 (neutral) for rows whose actor is
        absent from the embedding, None when the whole factor is unavailable.
        ``sigma`` overrides the steal bandwidth (the SIM-512 advancement draws
        pass ``adv_sigma``). SIM-523 part A: with ``actor_matrices`` on and a
        ``matrix`` name the bundle carries, the factor is that engine's score
        matrix gathered onto the rows instead (the kernel is the fallback)."""
        if emb_rows_all is None:
            return None
        if self.actor_matrices and matrix is not None:
            f = self._matrix_gather(matrix, live_key, emb_rows_all[rows])
            if f is not None:
                return f
        z = self._emb_z(actor)
        emb = self.a.actor_emb.get(actor)
        if z is None or emb is None:
            return None
        live_idx = emb["key_index"].get(live_key)
        cols = self._steal_feat_cols(actor, feat_names)
        if live_idx is None or cols is None:
            return None
        s = float(sigma) if sigma is not None else self.steal_sigma
        live = z[live_idx][cols]
        row_idx = emb_rows_all[rows]
        valid = row_idx >= 0
        out = np.ones(len(rows), dtype=np.float32)
        if not valid.any():
            return out
        sub = z[row_idx[valid]][:, cols]
        diff = sub - live
        d2 = np.einsum("ij,ij->i", diff, diff)
        out[valid] = np.exp(-d2 / (2.0 * s**2 * len(cols))).astype(np.float32)
        return out

    def steal_draw(
        self,
        target_base: int,
        runner_key: str,
        pitcher_key: str,
        catcher_key: str | None,
        *,
        outs: int,
        balls: int,
        strikes: int,
        score_diff: int,
        aggression: float = 1.0,
    ) -> tuple[bool, bool, bool, bool, bool] | None:
        """Draw ONE steal-opportunity row -> (attempted, success, pickoff_out,
        pickoff_advancing, pickoff_error), or None when the pool/cell is
        absent (the caller then stages nothing).

        The owner's rule (2026-08-10): every sim decision is a similarity-
        weighted draw from a hard-filtered pool, never a hand-tuned formula.
        Hard filter: target base + the exact (outs, balls, strikes) cell.
        Weights: runner similarity (how he runs), pitcher hold similarity,
        catcher-arm similarity — so a strong arm DETERS the attempt, because
        rows against similar catchers carry fewer attempts — a soft score-diff
        kernel (blowout damping emerges from the pool, not a constant), manager
        aggression as a multiplier on ATTEMPTED rows (a weight, never a gate —
        SIM-474), and recency. The drawn row answers the whole pre-pitch
        running-game question at once: `attempted` says whether the runner
        goes, `success` says safe or caught, and the SIM-507 pickoff labels
        say whether a pickoff retired the runner (`pickoff_advancing` marks a
        picked-off caught stealing) or an errant throw advanced him. On a
        pre-0017 bundle the pickoff labels are all-zero and nothing changes.
        """
        pool = self.a.steal_pools.get(str(int(target_base)))
        meta = self._steal_meta(str(int(target_base)))
        if pool is None or meta is None:
            return None
        rows = meta["cells"].get((int(outs), int(balls), int(strikes)))
        if rows is None or len(rows) == 0:
            return None
        w = pool.recency[rows].astype(np.float32).copy()
        sd = pool.sit[rows, 3] - np.float32(score_diff)
        w *= np.exp(-(sd * sd) / (2.0 * self.steal_score_sigma**2)).astype(np.float32)
        if not _STEAL_ABLATE_RUNNER:
            f = self._steal_actor_factor(
                "baserunner",
                runner_key,
                meta["runner_rows"],
                rows,
                self._RUNNER_STEAL_FEATURES,
                matrix="runner_steal",
            )
            if f is not None:
                w *= f
        if not _STEAL_ABLATE_PITCHER:
            f = self._steal_actor_factor(
                "pitcher_steal",
                pitcher_key,
                meta["pitcher_rows"],
                rows,
                self._PITCHER_STEAL_FEATURES,
                matrix="pitcher_steal",
            )
            if f is not None:
                w *= f
        if catcher_key and not _STEAL_ABLATE_CATCHER:
            f = self._steal_actor_factor(
                "catcher",
                catcher_key,
                meta["catcher_rows"],
                rows,
                self._CATCHER_STEAL_FEATURES,
                matrix="catcher_throwing",
            )
            if f is not None:
                w *= f
        if aggression != 1.0:
            att = pool.attempted[rows].astype(bool)
            w = np.where(att, w * np.float32(aggression), w)
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        cdf = np.cumsum(w, dtype=np.float64)
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        i = min(i, len(rows) - 1)
        r = rows[i]
        return (
            bool(pool.attempted[r]),
            bool(pool.success[r]),
            bool(pool.pickoff_out[r]),
            bool(pool.pickoff_advancing[r]),
            bool(pool.pickoff_error[r]),
        )

    # ---- SIM-512: the five-scenario advancement draw -----------------------

    #: The runner kernel: how he runs AND how he decides. All columns of
    #: derived.baserunner_season_metrics (the baserunner embedding).
    _RUNNER_ADV_FEATURES = (
        "sprint_speed",
        "extra_base_attempt_rate",
        "extra_base_success_rate",
        "first_to_third_attempt_rate",
        "second_to_home_attempt_rate",
        "first_to_home_attempt_rate",
        "tag_up_attempt_rate",
    )
    #: The fielder-arm kernel: a strong arm DETERS the send, because rows
    #: against similar arms carry fewer attempts. Columns of
    #: derived.fielder_season_metrics (the fielder embedding).
    _FIELDER_ADV_FEATURES = (
        "arm_strength",
        "arm_hold_rate",
        "arm_thrown_out_rate",
        "arm_advancement_prevention",
    )

    def has_advancement(self) -> bool:
        """SIM-512: True when the bundle carries the advancement pools."""
        return bool(self.a.adv_pools)

    def _adv_meta(self, key: str) -> dict | None:
        """Per-decision one-time precompute: the z-scored throw-geometry
        matrix + per-row embedding-row gathers for the runner and fielder."""
        meta = self._adv_meta_cache.get(key)
        if meta is not None:
            return meta
        pool = self.a.adv_pools.get(key)
        if pool is None or pool.n == 0:
            return None
        feat = pool.feat.astype(np.float32)
        mean = feat.mean(axis=0)
        std = feat.std(axis=0)
        std[std < 1e-6] = 1.0
        zfeat = (feat - mean) / std

        runner_rows = None
        bemb = self.a.actor_emb.get("baserunner")
        if bemb is not None:
            kidx = bemb["key_index"]
            runner_rows = np.fromiter(
                (
                    kidx.get(f"{int(r)}:{int(s)}", -1)
                    for r, s in zip(pool.runner_id, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        fielder_rows = None
        femb = self.a.actor_emb.get("fielder")
        if femb is not None:
            kidx = femb["key_index"]
            fielder_rows = np.fromiter(
                (
                    kidx.get(f"{int(f)}:{_POS_NUM_TO_NAME.get(int(p), '?')}:{int(s)}", -1)
                    for f, p, s in zip(pool.fielder_id, pool.fielder_pos, pool.season, strict=False)
                ),
                dtype=np.int64,
                count=pool.n,
            )
        meta = {
            "zfeat": zfeat,
            "mean": mean,
            "std": std,
            "rows": np.arange(pool.n, dtype=np.int64),
            "runner_rows": runner_rows,
            "fielder_rows": fielder_rows,
        }
        self._adv_meta_cache[key] = meta
        return meta

    def advancement_draw(
        self,
        scenario: int,
        from_base: int,
        target_base: int,
        runner_key: str,
        fielder_key: str | None,
        *,
        outs: int,
        exit_velo: float,
        launch_angle: float,
        spray_angle: float,
        hit_distance: float,
    ) -> tuple[bool, bool, bool] | None:
        """SIM-512: draw ONE advancement-opportunity row -> (attempted, safe,
        error_extra), or None when the pool/decision is absent (a legacy
        bundle — the caller then stages no discretionary advancement).

        The owner's rule (2026-08-10): every sim decision is a similarity-
        weighted draw from a hard-filtered pool, never a hand-tuned formula.
        Hard filter: the decision itself — (scenario, from_base, target_base)
        is its own sub-pool. Weights: runner similarity (his legs AND his
        decisions), the LIVE fielder's arm against each row fielder's arm
        (a strong arm deters the send because rows against similar arms carry
        fewer attempts), a z-scored kernel over the throw geometry (EV, LA,
        spray, distance, outs), and recency. The drawn row answers the whole
        question at once: ``attempted`` says whether he goes, ``safe`` says
        the throw's outcome, ``error_extra`` the extra base on a bad throw.
        """
        key = f"{int(scenario)}_{int(from_base)}_{int(target_base)}"
        pool = self.a.adv_pools.get(key)
        meta = self._adv_meta(key)
        if pool is None or meta is None:
            return None
        w = pool.recency.astype(np.float32).copy()
        live = (
            np.asarray([exit_velo, launch_angle, spray_angle, hit_distance, outs], dtype=np.float32)
            - meta["mean"]
        ) / meta["std"]
        diff = meta["zfeat"] - live
        d2 = np.einsum("ij,ij->i", diff, diff)
        w *= np.exp(-d2 / (2.0 * self.adv_feat_sigma**2 * diff.shape[1])).astype(np.float32)
        f = self._steal_actor_factor(
            "baserunner",
            runner_key,
            meta["runner_rows"],
            meta["rows"],
            self._RUNNER_ADV_FEATURES,
            sigma=self.adv_sigma,
            matrix="runner_adv",
        )
        if f is not None:
            w *= f
        if fielder_key:
            # The live fielder key is "id:POS:season"; the fielder matrices are
            # per position, so the matrix name follows the key's position.
            parts = fielder_key.split(":")
            f = self._steal_actor_factor(
                "fielder",
                fielder_key,
                meta["fielder_rows"],
                meta["rows"],
                self._FIELDER_ADV_FEATURES,
                sigma=self.adv_sigma,
                matrix=(f"fielder_{parts[1]}" if len(parts) == 3 else None),
            )
            if f is not None:
                w *= f
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        cdf = np.cumsum(w, dtype=np.float64)
        i = min(int(np.searchsorted(cdf, self.rng.random() * cdf[-1])), pool.n - 1)
        return (bool(pool.attempted[i]), bool(pool.safe[i]), bool(pool.error_extra[i]))
