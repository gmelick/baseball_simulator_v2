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
    ) -> None:
        self.a = artifacts
        self.rng = rng if rng is not None else np.random.default_rng()
        self.sit_sigma = float(sit_sigma)
        self.batter_sigma = float(batter_sigma)
        # Per-pool precompute: dense candidate->profile indices for O(1) gathers.
        self._pool_cache: dict[str, dict] = {}
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
        }
        self._pool_cache[hand] = meta
        return meta

    # ---- factor builders --------------------------------------------------
    def _f_pitcher(self, hand: str, pitcher_key: str) -> np.ndarray:
        meta = self._pool_meta(hand)
        sims = self.a.pitcher_sim.get(pitcher_key)
        n_prof = len(self.a.pitcher_sim_index)
        if not sims or n_prof == 0:
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

    def _f_batter(self, hand: str, batter_key: str) -> np.ndarray:
        meta = self._pool_meta(hand)
        bemb = self.a.actor_emb.get("batter")
        if bemb is None or batter_key not in bemb["key_index"]:
            return np.ones(meta["pool"].n, dtype=np.float32)
        mean, std = bemb["mean"], bemb["std"]
        vecs_z = (bemb["vecs"] - mean) / std
        q = vecs_z[bemb["key_index"][batter_key]]
        # RBF affinity per embedding row, then gather to the pool.
        d2 = np.einsum("ij,ij->i", vecs_z - q, vecs_z - q)
        aff = np.exp(-d2 / (2.0 * self.batter_sigma**2 * vecs_z.shape[1])).astype(np.float32)
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
        s = self.a.pools[hand].sit[:, 2:6]
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
        """Per-batter-embedding RBF affinity to the current batter (None if absent)."""
        bemb = self.a.actor_emb.get("batter")
        if bemb is None or batter_key not in bemb["key_index"]:
            return None
        vecs_z = (bemb["vecs"] - bemb["mean"]) / bemb["std"]
        q = vecs_z[bemb["key_index"][batter_key]]
        d2 = np.einsum("ij,ij->i", vecs_z - q, vecs_z - q)
        return np.exp(-d2 / (2.0 * self.batter_sigma**2 * vecs_z.shape[1])).astype(np.float32)

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

    def battedball_new_pa(self, hand: str, batter_key: str, state: np.ndarray) -> None:
        """Assemble the batted-ball weight CDF for the PA (f_batter · f_situation · recency)."""
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
        self._bb_cdf = np.cumsum(f_bat * f_sit * pool.recency, dtype=np.float64)

    def battedball_draw(self) -> tuple[str, int, int, float]:
        """Draw one batted ball -> (event, result_hits, result_outs, launch_angle).

        SIM-425: launch_angle (geom col 1) is returned so the resolver can tell a
        fly out (tag-up eligible) from a ground out for productive-out advancement.
        """
        if self._bb_hand is None or self._bb_cdf is None or self._bb_cdf[-1] <= 0:
            return ("field_out", 0, 1, 0.0)
        pool = self.a.bb_pools[self._bb_hand]
        i = min(
            int(np.searchsorted(self._bb_cdf, self.rng.random() * self._bb_cdf[-1])), pool.n - 1
        )
        return (
            str(pool.event[i]),
            int(pool.result_hits[i]),
            int(pool.result_outs[i]),
            float(pool.geom[i, 1]),
        )

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
