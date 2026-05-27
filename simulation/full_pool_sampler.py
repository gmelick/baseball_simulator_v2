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
        self._cdf: np.ndarray | None = None  # per-PA cumulative weights
        self._outcome_codes: dict[str, np.ndarray] = {}
        # SIM-425 batted-ball state
        self._bb_hand: str | None = None
        self._bb_cdf: np.ndarray | None = None
        self._bb_pool_bat: dict[str, np.ndarray] = {}

    # ---- per-pool one-time precompute ------------------------------------
    def _pool_meta(self, hand: str) -> dict:
        if hand in self._pool_cache:
            return self._pool_cache[hand]
        pool: HandPool = self.a.pools[hand]
        n = pool.n
        # candidate (pitcher_id:season) -> dense profile index in the pitcher-sim list
        pidx = self.a.pitcher_sim_index
        pool_prof = np.fromiter(
            (pidx.get(f"{int(p)}:{int(s)}", -1) for p, s in zip(pool.pitcher_id, pool.season, strict=False)),
            dtype=np.int64, count=n,
        )
        # candidate (batter_id:season) -> dense batter-embedding row
        bemb = self.a.actor_emb.get("batter")
        if bemb is not None:
            bkidx = bemb["key_index"]
            pool_bat = np.fromiter(
                (bkidx.get(f"{int(b)}:{int(s)}", -1) for b, s in zip(pool.batter_id, pool.season, strict=False)),
                dtype=np.int64, count=n,
            )
        else:
            pool_bat = np.full(n, -1, dtype=np.int64)
        meta = {"pool": pool, "pool_prof": pool_prof, "pool_bat": pool_bat,
                "outcome": np.asarray(pool.outcome_type, dtype=object)}
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
        return np.where(pb >= 0, aff[np.clip(pb, 0, len(aff) - 1)], np.float32(1.0)).astype(np.float32)

    def _f_situation(self, hand: str, state: np.ndarray) -> np.ndarray:
        pool = self.a.pools[hand]
        diff = pool.sit - np.asarray(state, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        return np.exp(-d2 / (2.0 * self.sit_sigma**2 * pool.sit.shape[1])).astype(np.float32)

    # ---- matchup lifecycle ------------------------------------------------
    def new_half_inning(self, hand: str, pitcher_key: str) -> None:
        """Cache the half-inning-constant base (f_pitcher * recency)."""
        self._hand = hand
        pool = self.a.pools[hand]
        self._base = (self._f_pitcher(hand, pitcher_key) * pool.recency).astype(np.float32)

    def new_plate_appearance(self, batter_key: str, state: np.ndarray) -> None:
        """Assemble the per-PA weight vector + its CDF (the alias-draw stand-in)."""
        assert self._hand is not None and self._base is not None, "call new_half_inning first"
        w = self._base * self._f_batter(self._hand, batter_key) * self._f_situation(self._hand, state)
        self._cdf = np.cumsum(w, dtype=np.float64)

    def draw(self) -> str:
        """O(1) draw of one pitch outcome from the current PA's weighted pool."""
        assert self._cdf is not None, "call new_plate_appearance first"
        total = self._cdf[-1]
        if total <= 0:
            return "ball"
        r = self.rng.random() * total
        i = int(np.searchsorted(self._cdf, r))
        meta = self._pool_cache[self._hand]
        return str(meta["outcome"][min(i, len(meta["outcome"]) - 1)])

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
                (ki.get(f"{int(b)}:{int(s)}", -1) for b, s in zip(pool.batter_id, pool.season, strict=False)),
                dtype=np.int64, count=pool.n,
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
            f_bat = np.where(pb >= 0, aff[np.clip(pb, 0, len(aff) - 1)], np.float32(1.0)).astype(np.float32)
        else:
            f_bat = np.ones(pool.n, dtype=np.float32)
        diff = pool.sit - np.asarray(state, dtype=np.float32)
        d2 = np.einsum("ij,ij->i", diff, diff)
        f_sit = np.exp(-d2 / (2.0 * self.sit_sigma**2 * pool.sit.shape[1])).astype(np.float32)
        self._bb_cdf = np.cumsum(f_bat * f_sit * pool.recency, dtype=np.float64)

    def battedball_draw(self) -> tuple[str, int, int]:
        """Draw one batted ball -> (event, result_hits, result_outs)."""
        if self._bb_hand is None or self._bb_cdf is None or self._bb_cdf[-1] <= 0:
            return ("field_out", 0, 1)
        pool = self.a.bb_pools[self._bb_hand]
        i = min(int(np.searchsorted(self._bb_cdf, self.rng.random() * self._bb_cdf[-1])), pool.n - 1)
        return (str(pool.event[i]), int(pool.result_hits[i]), int(pool.result_outs[i]))

    def has_battedball(self) -> bool:
        return bool(self.a.bb_pools)
