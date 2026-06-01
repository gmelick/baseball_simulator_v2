"""tests/unit/test_sim430_pitcher_sim_matrix.py — SIM-430 fan-out / OOM fix.

The fan-out/OOM half of SIM-430: 10 ProcessPool workers OOM-killed the host because
each held a full PRIVATE copy of the ``pitcher_sim`` dict-of-dicts (~2 GB resident,
NOT shareable through multiprocessing.shared_memory). The fix densifies it into a
small (n_prof x n_prof) float32 ``pitcher_sim_matrix`` that rides the existing
SIM-403b shared-memory seam (one read-only view for all workers) and lets the
~2 GB dict be skipped entirely on load.

These tests pin the invariants that make it SAFE + REAL:
  1. **Byte-identical draw** — the dense-matrix ``_f_pitcher`` reproduces the
     dict-scatter path exactly (same prof_score, same draws).
  2. **The matrix needs NO dict** — with the dict empty, the matrix path still
     scores (proving a worker can drop the 2 GB dict).
  3. **Fallback parity** — a pitcher absent from the index, or an all-zero
     (unscored/empty) row, both fall back to flat weights exactly like ``if not sims``.
  4. **Shared-memory seam** — ``pitcher_sim.matrix`` round-trips through
     extract_shared_arrays / attach_shared_views.

Synthetic bundle only (no DuckDB / disk).
"""

from __future__ import annotations

import numpy as np

from pipeline.batch.engine_artifacts import BattedBallPool, EngineArtifacts, HandPool
from simulation.full_pool_sampler import FullPoolSampler

# 3 same-hand pitchers in the similarity index; the pool is drawn from them.
_INDEX = {"100:2024": 0, "101:2024": 1, "102:2024": 2}
_SIMS = {
    "100:2024": {"100:2024": 1.0, "101:2024": 0.62, "102:2024": 0.18},
    "101:2024": {"101:2024": 1.0, "100:2024": 0.55, "102:2024": 0.40},
    "102:2024": {"102:2024": 1.0, "100:2024": 0.21, "101:2024": 0.47},
}
_BATTERS = ["200:2024", "201:2024", "202:2024", "203:2024"]


def _densify(sims: dict, index: dict) -> np.ndarray:
    """Mirror engine_artifacts.build_pitcher_sim_matrix's dict -> dense conversion."""
    n = len(index)
    m = np.zeros((n, n), dtype=np.float32)
    for qk, row in sims.items():
        i = index.get(qk)
        if i is None:
            continue
        for tk, v in row.items():
            j = index.get(tk)
            if j is not None:
                m[i, j] = v
    return m


def _hand_pool(n: int, seed: int) -> HandPool:
    rng = np.random.default_rng(seed)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 0] = np.arange(n) % 4
    sit[:, 1] = np.arange(n) % 3
    sit[:, 2] = np.arange(n) % 3
    sit[:, 3] = np.arange(n) % 2
    sit[:, 4] = 5.0
    sit[:, 5] = (np.arange(n) % 5 - 2).astype(np.float32)
    # Pool rows are drawn from the 3 indexed pitchers so pool_prof has real entries.
    pitcher_ids = np.array([100, 101, 102], dtype=np.int64)
    return HandPool(
        geom=rng.standard_normal((n, 10)).astype(np.float32),
        sit=sit,
        pitcher_id=pitcher_ids[np.arange(n) % 3],
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


def _actor_emb() -> dict:
    rng = np.random.default_rng(7)
    return {
        "batter": {
            "key_index": {k: i for i, k in enumerate(_BATTERS)},
            "vecs": rng.standard_normal((4, 8)).astype(np.float32),
            "mean": rng.standard_normal(8).astype(np.float32),
            "std": (np.abs(rng.standard_normal(8)) + 0.5).astype(np.float32),
        }
    }


def _bundle(*, with_matrix: bool, with_dict: bool) -> EngineArtifacts:
    return EngineArtifacts(
        pools={"R": _hand_pool(48, 0), "L": _hand_pool(33, 5)},
        pitcher_sim=dict(_SIMS) if with_dict else {},
        pitcher_sim_index=dict(_INDEX),
        actor_emb=_actor_emb(),
        bb_pools={"R": _bb_pool(28, 1), "L": _bb_pool(24, 2)},
        pitcher_sim_matrix=_densify(_SIMS, _INDEX) if with_matrix else None,
    )


def _draw_sequence(s: FullPoolSampler, seed: int, pitcher_key: str) -> list:
    s.rng = np.random.default_rng(seed)
    out: list = []
    base_out = np.array([1, 1, 5, 0], dtype=np.float32)
    st = np.array([0, 0, 1, 1, 5, 0], dtype=np.float32)
    for _hi in range(4):
        s.new_half_inning("R", pitcher_key)
        for j in range(4):
            s.new_plate_appearance(_BATTERS[j], base_out)
            for b, k in [(0, 0), (1, 0), (2, 1), (3, 2)]:
                out.append(s.draw(b, k))
            s.battedball_new_pa("R", _BATTERS[j], st)
            out.append(s.battedball_draw())
    return out


class TestMatrixEquivalence:
    def test_f_pitcher_matches_dict_for_every_indexed_pitcher(self):
        dict_art = _bundle(with_matrix=False, with_dict=True)
        mat_art = _bundle(with_matrix=True, with_dict=False)
        sd = FullPoolSampler(dict_art, np.random.default_rng(0))
        sm = FullPoolSampler(mat_art, np.random.default_rng(0))
        for key in _INDEX:
            for hand in ("R", "L"):
                np.testing.assert_array_equal(sd._f_pitcher(hand, key), sm._f_pitcher(hand, key))

    def test_full_draw_sequence_is_byte_identical(self):
        dict_art = _bundle(with_matrix=False, with_dict=True)
        mat_art = _bundle(with_matrix=True, with_dict=False)
        seq_dict = _draw_sequence(
            FullPoolSampler(dict_art, np.random.default_rng(1)), 42, "101:2024"
        )
        seq_mat = _draw_sequence(FullPoolSampler(mat_art, np.random.default_rng(1)), 42, "101:2024")
        assert seq_dict == seq_mat
        assert len(seq_mat) == 4 * 4 * 5


class TestMatrixNeedsNoDict:
    def test_matrix_path_scores_with_empty_dict(self):
        # The OOM fix: a worker carries the matrix but NOT the 2 GB dict.
        art = _bundle(with_matrix=True, with_dict=False)
        assert art.pitcher_sim == {}
        s = FullPoolSampler(art, np.random.default_rng(0))
        f = s._f_pitcher("R", "100:2024")
        assert f.shape == (art.pools["R"].n,)
        # Not all-ones: a scored pitcher up/down-weights the pool by similarity.
        assert not np.allclose(f, 1.0)


class TestFallbackParity:
    def test_pitcher_absent_from_index_is_flat_ones_both_paths(self):
        dict_art = _bundle(with_matrix=False, with_dict=True)
        mat_art = _bundle(with_matrix=True, with_dict=False)
        sd = FullPoolSampler(dict_art, np.random.default_rng(0))
        sm = FullPoolSampler(mat_art, np.random.default_rng(0))
        for s in (sd, sm):
            f = s._f_pitcher("R", "999:2024")  # not in the index
            np.testing.assert_array_equal(f, np.ones(s.a.pools["R"].n, dtype=np.float32))

    def test_all_zero_row_falls_back_to_flat_ones(self):
        # An indexed pitcher whose row is all-zero (== empty/unscored sims) must
        # match the dict path's `if not sims` flat-ones fallback.
        idx = {"100:2024": 0, "300:2024": 1}
        sims = {"100:2024": {"100:2024": 1.0}}  # 300 has NO sims entry
        m = _densify(sims, idx)
        art = EngineArtifacts(
            pools={"R": _hand_pool(12, 0), "L": _hand_pool(12, 1)},
            pitcher_sim={},
            pitcher_sim_index=idx,
            actor_emb=_actor_emb(),
            bb_pools={"R": _bb_pool(8, 1), "L": _bb_pool(8, 2)},
            pitcher_sim_matrix=m,
        )
        s = FullPoolSampler(art, np.random.default_rng(0))
        np.testing.assert_array_equal(
            s._f_pitcher("R", "300:2024"), np.ones(art.pools["R"].n, dtype=np.float32)
        )


class TestSharedMemorySeam:
    def test_matrix_round_trips_through_extract_and_attach(self):
        src = _bundle(with_matrix=True, with_dict=False)
        shared = src.extract_shared_arrays()
        assert "pitcher_sim.matrix" in shared
        assert shared["pitcher_sim.matrix"].dtype == np.float32

        # A worker bundle that disk-loaded WITHOUT the matrix attaches the view.
        dst = _bundle(with_matrix=False, with_dict=True)
        dst.pitcher_sim_matrix = None
        dst.attach_shared_views({"pitcher_sim.matrix": shared["pitcher_sim.matrix"]})
        assert dst.pitcher_sim_matrix is shared["pitcher_sim.matrix"]

    def test_no_matrix_means_not_published(self):
        legacy = _bundle(with_matrix=False, with_dict=True)
        assert "pitcher_sim.matrix" not in legacy.extract_shared_arrays()
