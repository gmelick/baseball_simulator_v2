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

import numpy as np

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool

_OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play")


class FullPoolSampler:
    def __init__(
        self,
        artifacts: EngineArtifacts,
        rng: np.random.Generator | None = None,
        *,
        sit_sigma: float = 2.0,
        batter_sigma: float = 3.0,
        platoon_off_weight: float = 0.6,
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

    def battedball_new_pa(
        self,
        hand: str,
        batter_key: str,
        state: np.ndarray,
        pitcher_throws: str | None = None,
    ) -> None:
        """Assemble the batted-ball weight CDF for the PA (f_batter · f_situation · recency).

        SIM-413: when ``pitcher_throws`` ('L'/'R') is supplied AND the pool carries
        per-row ``p_throws``, softly reweight toward same-hand-matchup rows (opposite
        hand rows ×:attr:`platoon_off_weight`) so the drawn batted ball reflects the
        live platoon matchup. Omitted / legacy pool -> the draw is unchanged."""
        self._bb_hand = hand
        pool = self.a.bb_pools[hand]
        aff = self._batter_aff(batter_key)
        if aff is not None:
            pb = self._bb_pool_bat_idx(hand)
            f_bat = np.where(pb >= 0, aff[np.clip(pb, 0, len(aff) - 1)], np.float32(1.0)).astype(
                np.float32
            )
        else:
            f_bat = np.ones(pool.n, dtype=np.float32)
        diff: np.ndarray = pool.sit - np.asarray(state, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * pool.sit.shape[1])).astype(np.float32)
        w = f_bat * f_sit * pool.recency
        if pitcher_throws and self.platoon_off_weight != 1.0:
            same = self._bb_same_hand_mask(hand, pitcher_throws)
            if same is not None:
                w = w * np.where(same, np.float32(1.0), np.float32(self.platoon_off_weight))
        self._bb_cdf = np.cumsum(w, dtype=np.float64)

    def battedball_draw(self) -> tuple[str, int, int, float]:
        """Draw one batted ball -> (event, result_hits, result_outs, launch_angle).

        SIM-425: launch_angle (geom col 1) is returned so the resolver can tell a
        fly out (tag-up eligible) from a ground out for productive-out advancement.
        """
        if self._bb_hand is None or self._bb_cdf is None or self._bb_cdf[-1] <= 0:
            self._bb_last_i = None
            return ("field_out", 0, 1, 0.0)
        pool = self.a.bb_pools[self._bb_hand]
        i = min(
            int(np.searchsorted(self._bb_cdf, self.rng.random() * self._bb_cdf[-1])), pool.n - 1
        )
        self._bb_last_i = i  # SIM-425b: remember the row for the fielder lookup
        return (
            str(pool.event[i]),
            int(pool.result_hits[i]),
            int(pool.result_outs[i]),
            float(pool.geom[i, 1]),
        )

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
