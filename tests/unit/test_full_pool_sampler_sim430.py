"""tests/unit/test_full_pool_sampler_sim430.py — SIM-430 hot-path caching.

The full-pool per-PA path was the `/simulate` throughput cost. An in-container
cProfile of `FullPoolSampler` on the real all-seasons bundle showed
`new_plate_appearance` recomputing CONSTANTS on every plate appearance:
  * the z-scored batter-embedding matrix (`vecs_z`) — rebuilt in both `_f_batter`
    and `_batter_aff` every PA though it never changes,
  * the per-batter RBF affinity — computed twice per PA (pitch pool + batted ball),
  * the base-out situation columns — a non-contiguous `pool.sit[:, 2:6]` slice+copy
    over the whole pool every PA.

SIM-430 hoists all three into caches. These tests pin the invariants that make the
optimization SAFE + REAL:
  1. **Behavioral equivalence** — the cached path is pure memoization, so the
     drawn outcome sequence is byte-identical across two independent samplers at
     the same seed.
  2. **The caches actually populate + are reused**, and the pitch-pool /
     batted-ball affinity is shared (so the win is real, not bypassed).

Synthetic bundle only (no DuckDB / disk); HandPool / BattedBallPool / EngineArtifacts
constructed exactly as in test_engine_artifacts_shared_sim403b.
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import BattedBallPool, EngineArtifacts, HandPool
from simulation.full_pool_sampler import FullPoolSampler

_BATTERS = ["200:2024", "201:2024", "202:2024", "203:2024"]
_PITCHER = "100:2024"


def _hand_pool(n: int, seed: int) -> HandPool:
    """A HandPool whose sit columns carry real count/base-out structure so the
    count-bucketing + base-out RBF have something to discriminate on."""
    rng = np.random.default_rng(seed)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 0] = np.arange(n) % 4  # balls 0..3
    sit[:, 1] = np.arange(n) % 3  # strikes 0..2
    sit[:, 2] = np.arange(n) % 3  # outs
    sit[:, 3] = np.arange(n) % 2  # runners
    sit[:, 4] = 5.0  # inning
    sit[:, 5] = (np.arange(n) % 5 - 2).astype(np.float32)  # score_diff
    return HandPool(
        geom=rng.standard_normal((n, 10)).astype(np.float32),
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.tile([200, 201, 202, 203], n // 4 + 1)[:n].astype(np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        outcome_type=np.asarray(
            [
                ["ball", "called_strike", "swinging_strike", "foul", "in_play"][i % 5]
                for i in range(n)
            ],
            dtype=object,
        ),
        recency=np.full(n, 1.0, dtype=np.float32),
    )


def _bb_pool(n: int, seed: int) -> BattedBallPool:
    rng = np.random.default_rng(seed)
    return BattedBallPool(
        geom=rng.standard_normal((n, 3)).astype(np.float32),
        sit=rng.standard_normal((n, 6)).astype(np.float32),
        batter_id=np.tile([200, 201, 202, 203], n // 4 + 1)[:n].astype(np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        event=np.asarray(["single"] * n, dtype=object),
        result_hits=np.ones(n, dtype=np.int8),
        result_outs=np.zeros(n, dtype=np.int8),
        recency=np.full(n, 1.0, dtype=np.float32),
    )


def _toy_artifacts() -> EngineArtifacts:
    """A 2-hand bundle with a NON-trivial batter-embedding mean/std (so the cached
    z-score is a real transform) + a pitcher-sim entry for the pool's pitcher."""
    rng = np.random.default_rng(7)
    actor_emb = {
        "batter": {
            "keys": np.asarray(_BATTERS, dtype=object),
            "key_index": {k: i for i, k in enumerate(_BATTERS)},
            "vecs": rng.standard_normal((4, 8)).astype(np.float32),
            "mean": rng.standard_normal(8).astype(np.float32),
            "std": (np.abs(rng.standard_normal(8)) + 0.5).astype(np.float32),
        }
    }
    return EngineArtifacts(
        pools={"R": _hand_pool(40, 0), "L": _hand_pool(32, 5)},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        actor_emb=actor_emb,
        bb_pools={"R": _bb_pool(28, 1), "L": _bb_pool(24, 2)},
    )


def _draw_sequence(s: FullPoolSampler, seed: int) -> list:
    s.rng = np.random.default_rng(seed)
    out: list = []
    base_out = np.array([1, 1, 5, 0], dtype=np.float32)
    st = np.array([0, 0, 1, 1, 5, 0], dtype=np.float32)
    for _hi in range(4):
        s.new_half_inning("R", _PITCHER)
        for j in range(4):
            s.new_plate_appearance(_BATTERS[j], base_out)
            for b, k in [(0, 0), (1, 0), (2, 1), (3, 2)]:
                out.append(s.draw(b, k))
            s.battedball_new_pa("R", _BATTERS[j], st)
            out.append(s.battedball_draw())
    return out


class TestCachingEquivalence:
    def test_caches_do_not_change_draw_sequence(self):
        # Two independent samplers over the SAME artifacts at the SAME seed must
        # produce the identical draw sequence (the caches are pure memoization).
        art = _toy_artifacts()
        seq_a = _draw_sequence(FullPoolSampler(art, np.random.default_rng(1)), 42)
        seq_b = _draw_sequence(FullPoolSampler(art, np.random.default_rng(1)), 42)
        assert seq_a == seq_b
        assert len(seq_a) == 4 * 4 * 5  # 4 half-innings * 4 PA * (4 pitches + 1 BB)

    def test_repeated_pa_same_batter_is_stable(self):
        # Re-drawing the same (pitcher,batter,state) PA twice at the same rng gives
        # the same draw — the memoized affinity must not corrupt across calls.
        art = _toy_artifacts()
        s = FullPoolSampler(art, np.random.default_rng(0))
        s.new_half_inning("R", _PITCHER)
        base_out = np.array([1, 1, 5, 0], dtype=np.float32)
        s.rng = np.random.default_rng(7)
        s.new_plate_appearance("200:2024", base_out)
        d1 = s.draw(1, 1)
        s.rng = np.random.default_rng(7)
        s.new_plate_appearance("200:2024", base_out)
        d2 = s.draw(1, 1)
        assert d1 == d2


class TestCachesPopulate:
    def test_vecs_z_cached_once(self):
        art = _toy_artifacts()
        s = FullPoolSampler(art, np.random.default_rng(0))
        assert s._vecs_z is None
        s.new_half_inning("R", _PITCHER)
        s.new_plate_appearance("200:2024", np.array([1, 1, 5, 0], dtype=np.float32))
        assert s._vecs_z is not None
        first = s._vecs_z
        # A second PA must REUSE the same cached object, not rebuild it.
        s.new_plate_appearance("201:2024", np.array([0, 0, 5, 0], dtype=np.float32))
        assert s._vecs_z is first

    def test_affinity_memoized_and_shared_pitch_and_battedball(self):
        art = _toy_artifacts()
        s = FullPoolSampler(art, np.random.default_rng(0))
        s.new_half_inning("R", _PITCHER)
        s.new_plate_appearance("202:2024", np.array([1, 1, 5, 0], dtype=np.float32))
        assert "202:2024" in s._aff_cache
        cached = s._aff_cache["202:2024"]
        # The batted-ball factor reuses the SAME affinity array (no recompute).
        assert s._batter_aff("202:2024") is cached

    def test_sit_baseout_is_contiguous_and_cached(self):
        art = _toy_artifacts()
        s = FullPoolSampler(art, np.random.default_rng(0))
        meta = s._pool_meta("R")
        sb = meta["sit_baseout"]
        assert sb.shape == (art.pools["R"].n, 4)
        assert sb.flags["C_CONTIGUOUS"]
        # It must equal the original slice exactly (same numbers, just contiguous).
        np.testing.assert_array_equal(sb, art.pools["R"].sit[:, 2:6])
