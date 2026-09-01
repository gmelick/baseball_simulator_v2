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

_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")

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
        self._cat_idx: dict[str, int] | None = None  # catcher feature-name -> col
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

    def _f_batter(self, hand: str, batter_key: str) -> np.ndarray:
        meta = self._pool_meta(hand)
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
    def new_half_inning(self, hand: str, pitcher_key: str) -> None:
        """Cache the half-inning-constant base (f_pitcher * recency)."""
        self._hand = hand
        pool = self.a.pools[hand]
        self._base = (self._f_pitcher(hand, pitcher_key) * pool.recency).astype(np.float32)

    def new_plate_appearance(self, batter_key: str, base_out: np.ndarray) -> None:
        """Assemble the per-PA matchup weight (base · f_batter · f_situation_baseout)
        and split it into 12 count-bucket CDFs for the per-pitch, count-conditioned
        draw (SIM-429)."""
        assert self._hand is not None and self._base is not None, "call new_half_inning first"
        w = (
            self._base
            * self._f_batter(self._hand, batter_key)
            * self._f_situation_baseout(self._hand, base_out)
        )
        rows = self._pool_meta(self._hand)["bucket_rows"]
        self._bucket_cdf = [(np.cumsum(w[r], dtype=np.float64) if r.size else None) for r in rows]

    def draw(self, balls: int = 0, strikes: int = 0) -> str:
        """Count-conditioned draw of one pitch outcome (SIM-429): restrict to the
        live count's bucket, weighted by the per-PA matchup weights."""
        assert self._bucket_cdf is not None, "call new_plate_appearance first"
        meta = self._pool_cache[self._hand]
        b = min(max(int(balls), 0), 3) * 3 + min(max(int(strikes), 0), 2)
        cdf, rows = self._bucket_cdf[b], meta["bucket_rows"][b]
        if cdf is None or cdf[-1] <= 0:
            return "ball"
        i = int(np.searchsorted(cdf, self.rng.random() * cdf[-1]))
        return str(meta["outcome"][rows[min(i, rows.size - 1)]])

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
        transition columns (a sim510.1+ bundle). False = the legacy draw."""
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
    ) -> None:
        """Assemble the batted-ball weight CDF for the PA (f_batter · f_situation · recency).

        SIM-511: on a transition bundle the draw HARD-filters the exact
        base-out cell (the drawn row must be legal in the live state — that
        is what makes "the drawn row is the play" safe), and the situation
        kernel runs over the remaining soft dims (balls, strikes, inning,
        score_diff). On a legacy bundle the whole-pool soft draw is unchanged.

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
        aff = self._batter_aff(batter_key)
        pb = self._bb_pool_bat_idx(hand) if aff is not None else None
        if meta is not None:
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
                f_bat = np.where(
                    pbr >= 0, aff[np.clip(pbr, 0, len(aff) - 1)], np.float32(1.0)
                ).astype(np.float32)
            else:
                f_bat = np.ones(len(rows), dtype=np.float32)
            diff = meta["soft"][rows] - sv[[0, 1, 4, 5]]
            d2 = np.einsum("ij,ij->i", diff, diff)
            f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * diff.shape[1])).astype(np.float32)
            w = f_bat * f_sit * pool.recency[rows]
            if pitcher_throws and self.platoon_off_weight != 1.0:
                same = self._bb_same_hand_mask(hand, pitcher_throws)
                if same is not None:
                    w = w * np.where(
                        same[rows], np.float32(1.0), np.float32(self.platoon_off_weight)
                    )
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
            if defense_map and self.fielder_sigma > 0.0:
                ff = self._f_live_fielder(hand, rows, defense_map, int(live_season or 0))
                if ff is not None:
                    w = w * ff
            self._bb_rows = rows
            self._bb_cdf = np.cumsum(w, dtype=np.float64)
            return
        # --- legacy bundle: the whole-pool soft draw (unchanged) -----------
        self._bb_rows = None
        if aff is not None and pb is not None:
            f_bat = np.where(pb >= 0, aff[np.clip(pb, 0, len(aff) - 1)], np.float32(1.0)).astype(
                np.float32
            )
        else:
            f_bat = np.ones(pool.n, dtype=np.float32)
        diff = pool.sit - sv
        d2 = np.einsum("ij,ij->i", diff, diff)
        f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * pool.sit.shape[1])).astype(np.float32)
        w = f_bat * f_sit * pool.recency
        if pitcher_throws and self.platoon_off_weight != 1.0:
            same = self._bb_same_hand_mask(hand, pitcher_throws)
            if same is not None:
                w = w * np.where(same, np.float32(1.0), np.float32(self.platoon_off_weight))
        if bat_home is not None and self.home_off_weight != 1.0:
            bh = getattr(pool, "bat_home", None)
            if bh is not None:
                match = (bh > 0) == bool(bat_home)
                w = w * np.where(match, np.float32(1.0), np.float32(self.home_off_weight))
        if park_run_factor is not None and self.park_sigma > 0.0:
            pf = self._bb_park_factors(hand)
            if pf is not None:
                d = pf - np.float32(park_run_factor)
                w = w * np.exp(-(d * d) / (2.0 * self.park_sigma**2)).astype(np.float32)
        if defense_map and self.fielder_sigma > 0.0:
            ff = self._f_live_fielder(hand, np.arange(pool.n), defense_map, int(live_season or 0))
            if ff is not None:
                w = w * ff
        self._bb_cdf = np.cumsum(w, dtype=np.float64)

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
        """SIM-511: the drawn row's full transition, or None (a legacy bundle,
        or no draw yet — the caller then runs the legacy resolution).

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
    def catcher_framing(self, catcher_key: str) -> float:
        """Return the catcher's per-taken-pitch called-strike delta vs expected
        (``strikes_above_average / pitches_received_total``), ~centred across the
        league.  The pitch pool already bakes in average framing, so this centred
        delta nudges the ball/called-strike draw toward THIS catcher.  0.0 when the
        catcher / features are absent."""
        cemb = self.a.actor_emb.get("catcher")
        if cemb is None:
            return 0.0
        feats = cemb.get("features")
        idx = cemb["key_index"].get(catcher_key)
        if feats is None or idx is None:
            return 0.0
        fi = self._cat_feat_idx(feats)
        saa_i, tot_i = fi.get("strikes_above_average"), fi.get("pitches_received_total")
        if saa_i is None or tot_i is None:
            return 0.0
        vec = cemb["vecs"][idx]
        tot = float(vec[tot_i])
        if tot <= 0.0:
            return 0.0
        d = float(vec[saa_i]) / tot
        return d if np.isfinite(d) else 0.0

    def _cat_feat_idx(self, feats: list) -> dict[str, int]:
        if self._cat_idx is None:
            self._cat_idx = {f: i for i, f in enumerate(feats)}
        return self._cat_idx

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
    ) -> np.ndarray | None:
        """Gaussian similarity between the LIVE actor and each pool row's actor
        over the given feature subset; 1.0 (neutral) for rows whose actor is
        absent from the embedding, None when the whole factor is unavailable.
        ``sigma`` overrides the steal bandwidth (the SIM-512 advancement draws
        pass ``adv_sigma``)."""
        if emb_rows_all is None:
            return None
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
                "baserunner", runner_key, meta["runner_rows"], rows, self._RUNNER_STEAL_FEATURES
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
            )
            if f is not None:
                w *= f
        if catcher_key and not _STEAL_ABLATE_CATCHER:
            f = self._steal_actor_factor(
                "catcher", catcher_key, meta["catcher_rows"], rows, self._CATCHER_STEAL_FEATURES
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
        )
        if f is not None:
            w *= f
        if fielder_key:
            f = self._steal_actor_factor(
                "fielder",
                fielder_key,
                meta["fielder_rows"],
                meta["rows"],
                self._FIELDER_ADV_FEATURES,
                sigma=self.adv_sigma,
            )
            if f is not None:
                w *= f
        total = float(w.sum())
        if not np.isfinite(total) or total <= 0.0:
            return None
        cdf = np.cumsum(w, dtype=np.float64)
        i = min(int(np.searchsorted(cdf, self.rng.random() * cdf[-1])), pool.n - 1)
        return (bool(pool.attempted[i]), bool(pool.safe[i]), bool(pool.error_extra[i]))
