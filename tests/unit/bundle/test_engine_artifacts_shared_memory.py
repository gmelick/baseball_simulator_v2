"""
tests/unit/bundle/test_engine_artifacts_shared_memory.py
===================================================
The EngineArtifacts shared-memory publish/attach contract.

WHAT THIS COVERS
----------------
The lifespan BatchRunner runs a multi-worker ProcessPool. The big read-only
numpy arrays in :class:`bundle.load_bundle.EngineArtifacts` ride
the ``multiprocessing.shared_memory`` plumbing in :mod:`simulation.batch_runner`
— so each worker ATTACHES the parent's bundle zero-copy instead of re-reading
it from disk.

These tests exercise the EngineArtifacts side of that contract without
spawning a real fork (``test_persistent_process_pool.py`` and
``test_batch_runner_concurrency.py`` cover the fork lifecycle). They cover:

  * the flat-name -> ndarray map :meth:`extract_shared_arrays` returns
    (keys + dtypes + buffer identity), on a tiny in-memory bundle;
  * the worker path on a small bundle on disk: the parent loads it and
    extracts the arrays, :func:`simulation.batch_runner._worker_init`
    attaches the published segments, and ``EngineArtifacts.load(dir,
    shared_views=...)`` takes EVERY extracted array as the shared view;
  * defensive contract: a load ignores unknown keys; an empty views map
    reads every array from disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pytest

from bundle.common import _savez_plain
from bundle.load_bundle import EngineArtifacts
from bundle.worker_pools import (
    _ACTOR_EMB_SHAREABLE_ATTRS,
    _ADV_POOL_SHAREABLE_ATTRS,
    _BB_POOL_SHAREABLE_ATTRS,
    _CHANGE_POOL_SHAREABLE_ATTRS,
    _HAND_POOL_SHAREABLE_ATTRS,
    _STEAL_POOL_SHAREABLE_ATTRS,
    BattedBallPool,
    HandPool,
)
from simulation.batch_runner import (
    _WORKER_SHARED,
    _worker_init,
    publish_shared_arrays,
    unlink_shared_segments,
)

# ===========================================================================
# Fixtures — tiny synthetic EngineArtifacts (no disk, no DuckDB)
# ===========================================================================


def _synthetic_hand_pool(n: int = 6, seed: int = 0) -> HandPool:
    """A tiny HandPool with distinguishable values so a view-vs-copy check is
    unambiguous. Sizes and dtypes mirror the real per-row schema."""
    rng = np.random.default_rng(seed)
    return HandPool(
        geom=rng.standard_normal((n, 10)).astype(np.float32),
        sit=rng.standard_normal((n, 6)).astype(np.float32),
        pitcher_id=np.arange(n, dtype=np.int64) + 600_000,
        batter_id=np.arange(n, dtype=np.int64) + 700_000,
        season=np.full(n, 2024, dtype=np.int64),
        outcome_type=np.asarray(
            ["ball", "called_strike", "swinging_strike", "foul", "in_play", "ball"][:n],
            dtype=object,
        ),
        recency=np.linspace(0.5, 1.0, n, dtype=np.float32),
    )


def _synthetic_bb_pool(n: int = 4, seed: int = 1) -> BattedBallPool:
    rng = np.random.default_rng(seed)
    return BattedBallPool(
        geom=rng.standard_normal((n, 3)).astype(np.float32),
        sit=rng.standard_normal((n, 6)).astype(np.float32),
        batter_id=np.arange(n, dtype=np.int64) + 800_000,
        season=np.full(n, 2023, dtype=np.int64),
        event=np.asarray(["single", "double", "field_out", "home_run"][:n], dtype=object),
        result_hits=np.array([1, 2, 0, 4], dtype=np.int8)[:n],
        result_outs=np.array([0, 0, 1, 0], dtype=np.int8)[:n],
        recency=np.full(n, 0.9, dtype=np.float32),
    )


def _synthetic_actor_emb(n: int = 3, dim: int = 4) -> dict[str, dict]:
    rng = np.random.default_rng(2)
    return {
        "batter": {
            "key_index": {f"{700_000 + i}:2024": i for i in range(n)},
            "vecs": rng.standard_normal((n, dim)).astype(np.float32),
            "mean": rng.standard_normal((dim,)).astype(np.float32),
            "std": np.ones((dim,), dtype=np.float32),
            "features": ["a", "b", "c", "d"][:dim],
        },
        "catcher": {
            "key_index": {f"{900_000 + i}:2024": i for i in range(n)},
            "vecs": rng.standard_normal((n, dim)).astype(np.float32),
            "mean": rng.standard_normal((dim,)).astype(np.float32),
            "std": np.ones((dim,), dtype=np.float32),
            "features": ["x", "y", "z", "w"][:dim],
        },
    }


def _synthetic_artifacts() -> EngineArtifacts:
    return EngineArtifacts(
        pools={"L": _synthetic_hand_pool(seed=10), "R": _synthetic_hand_pool(seed=11)},
        bb_pools={"L": _synthetic_bb_pool(seed=20), "R": _synthetic_bb_pool(seed=21)},
        pitcher_sim_index={f"{600_000 + i}:2024": i for i in range(6)},
        pitcher_sim={f"{600_000}:2024": {f"{600_001}:2024": 0.42}},
        actor_emb=_synthetic_actor_emb(),
    )


# ===========================================================================
# Fixtures — a small bundle on disk with every shareable column
# ===========================================================================

#: The rows of every pool in the disk bundle.
_N = 4


def _parquet(con: duckdb.DuckDBPyConnection, path: Path, columns: str) -> None:
    """Write ``_N`` rows of the SQL select list ``columns`` (``i`` is the row number) to ``path``."""
    con.execute(
        f"COPY (SELECT {columns} FROM range({_N}) t(i)) TO '{path.as_posix()}' (FORMAT parquet)"
    )


def _npy(path: Path, shape: tuple[int, ...], dtype: type = np.float32) -> None:
    """Write a small array of distinct values to ``path``."""
    np.save(path, np.arange(int(np.prod(shape)), dtype=dtype).reshape(shape))


def _write_bundle(root: Path) -> str:
    """A bundle on disk whose every pool carries every shareable column: both
    hands' pitch and batted-ball pools, both steal pools, one advancement pool,
    the change pool, two actor embeddings, the pitcher matrix and one actor
    score matrix. Returns the bundle directory."""
    con = duckdb.connect(":memory:")
    ymd = "20240601::INTEGER AS game_ymd"
    pp = root / "pitch_pool"
    bb = root / "battedball_pool"
    for d in (pp, bb, root / "steal_pool", root / "advancement_pool", root / "manager_pool"):
        d.mkdir(parents=True)
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    for hand in ("L", "R"):
        _npy(pp / f"{hand}.geom.npy", (_N, 10))
        _npy(pp / f"{hand}.sit.npy", (_N, 6))
        _npy(pp / f"{hand}.bb_row.npy", (_N,), np.int64)
        _parquet(
            con,
            pp / f"{hand}.meta.parquet",
            "600000 + i AS pitcher_id, 700000 + i AS batter_id, 2024::BIGINT AS season, "
            "'ball' AS outcome_type, 1.0::FLOAT AS recency_weight, 500000 + i AS catcher_id, "
            "0::TINYINT AS got_away, 1::TINYINT AS bat_home, "
            "50::SMALLINT AS pitcher_pitch_count, 2::TINYINT AS times_through_order, "
            f"5::TINYINT AS zone, {ymd}",
        )
        _npy(bb / f"{hand}.geom.npy", (_N, 3))
        _npy(bb / f"{hand}.sit.npy", (_N, 6))
        _npy(bb / f"{hand}.pgeom.npy", (_N, 10))
        fielders = ", ".join(f"{k}00 + i AS fielder_{k}" for k in range(2, 10))
        _parquet(
            con,
            bb / f"{hand}.meta.parquet",
            "700000 + i AS batter_id, 2024::BIGINT AS season, 'single' AS events, "
            "1::TINYINT AS result_hits, 0::TINYINT AS result_outs, "
            "1.0::FLOAT AS recency_weight, 'R' AS p_throws, 15::BIGINT AS venue_id, "
            "6::TINYINT AS fielded_by_position, 800000 + i AS fielder_player_id, "
            "2::TINYINT AS runner_1b_dest, -1::TINYINT AS runner_2b_dest, "
            "-1::TINYINT AS runner_3b_dest, 1::TINYINT AS batter_dest, 1::TINYINT AS dest_ok, "
            "0::TINYINT AS r1_adv_out, 0::TINYINT AS r2_adv_out, 0::TINYINT AS r3_adv_out, "
            "0::TINYINT AS is_air, 10.0::FLOAT AS spray_raw, 150.0::FLOAT AS hit_dist, "
            "1::TINYINT AS bat_home, 600000 + i AS pitcher_id, 3::TINYINT AS bb_class, "
            f"{fielders}, 32::SMALLINT AS putout_pos_mask, 0::SMALLINT AS assist_pos_mask, {ymd}",
        )
    for target in ("2", "3"):
        _npy(root / "steal_pool" / f"{target}.sit.npy", (_N, 4))
        _parquet(
            con,
            root / "steal_pool" / f"{target}.meta.parquet",
            "100 + i AS runner_id, 600000 + i AS pitcher_id, 500000 + i AS catcher_id, "
            "2024::BIGINT AS season, (i % 2)::TINYINT AS attempted, 0::TINYINT AS success, "
            "1.0::FLOAT AS recency_weight, 0::TINYINT AS pickoff_out, "
            "0::TINYINT AS pickoff_advancing, 0::TINYINT AS pickoff_error, "
            f"'ball' AS pitch_class, 0::TINYINT AS is_pickoff_row, {ymd}",
        )
    _npy(root / "advancement_pool" / "1_1_3.feat.npy", (_N, 5))
    _parquet(
        con,
        root / "advancement_pool" / "1_1_3.meta.parquet",
        "100 + i AS runner_id, 800000 + i AS fielder_id, 9::TINYINT AS fielder_pos, "
        "2024::BIGINT AS season, 1::TINYINT AS attempted, 1::TINYINT AS safe, "
        f"0::TINYINT AS error_extra, 1.0::FLOAT AS recency_weight, {ymd}",
    )
    _npy(root / "manager_pool" / "change.sit.npy", (_N, 6))
    _parquet(
        con,
        root / "manager_pool" / "change.meta.parquet",
        "600000 + i AS pitcher_id, 610000 + i AS incoming_id, 2024::BIGINT AS season, "
        "1::TINYINT AS is_starter, 1::TINYINT AS new_half, (i % 2)::TINYINT AS changed, "
        "1.0::FLOAT AS recency_weight, 7::BIGINT AS manager_id, 5::TINYINT AS in_days_rest, "
        "0::TINYINT AS in_pitched_2d, 0::SMALLINT AS in_pitches_3d, 2::TINYINT AS in_throws, "
        f"{ymd}",
    )
    con.close()
    keys = [f"{600_000 + i}:2024" for i in range(2)]
    _savez_plain(
        str(root / "pitcher_sim.npz"),
        index=json.dumps({k: i for i, k in enumerate(keys)}),
        pitcher_sim_matrix=np.eye(2, dtype=np.float32),
    )
    for actor in ("batter", "catcher"):
        _savez_plain(
            str(root / f"{actor}_emb.npz"),
            keys=json.dumps(keys),
            vecs=np.ones((2, 3), dtype=np.float32),
            mean=np.zeros(3, dtype=np.float32),
            std=np.ones(3, dtype=np.float32),
            features=json.dumps(["a", "b", "c"]),
        )
    (root / "actor_sim").mkdir()
    (root / "actor_sim" / "manifest.json").write_text(
        json.dumps({"sizes": {"batter": 2}}), encoding="utf-8"
    )
    _savez_plain(
        str(root / "actor_sim" / "batter.npz"),
        index=json.dumps({k: i for i, k in enumerate(keys)}),
        matrix=np.eye(2, dtype=np.float32),
    )
    return str(root)


def _expected_keys() -> set[str]:
    """Every flat name the disk bundle publishes: each shareable column of each pool."""
    keys = {f"pool.{h}.{a}" for h in ("L", "R") for a in _HAND_POOL_SHAREABLE_ATTRS}
    keys |= {f"bb_pool.{h}.{a}" for h in ("L", "R") for a in _BB_POOL_SHAREABLE_ATTRS}
    keys |= {
        f"actor_emb.{x}.{a}" for x in ("batter", "catcher") for a in _ACTOR_EMB_SHAREABLE_ATTRS
    }
    keys |= {f"steal_pool.{t}.{a}" for t in ("2", "3") for a in _STEAL_POOL_SHAREABLE_ATTRS}
    keys |= {f"adv_pool.1_1_3.{a}" for a in _ADV_POOL_SHAREABLE_ATTRS}
    keys |= {f"change_pool.{a}" for a in _CHANGE_POOL_SHAREABLE_ATTRS}
    return keys | {"pitcher_sim.matrix", "actor_sim.batter.matrix"}


# ===========================================================================
# extract_shared_arrays() — the flat-name -> ndarray contract
# ===========================================================================


class TestExtractSharedArrays:
    def test_emits_pitch_pool_arrays_for_both_hands(self):
        art = _synthetic_artifacts()
        out = art.extract_shared_arrays()
        for hand in ("L", "R"):
            for attr in ("geom", "sit", "pitcher_id", "batter_id", "season", "recency"):
                assert f"pool.{hand}.{attr}" in out
                assert isinstance(out[f"pool.{hand}.{attr}"], np.ndarray)

    def test_emits_bb_pool_arrays_for_both_hands(self):
        art = _synthetic_artifacts()
        out = art.extract_shared_arrays()
        for hand in ("L", "R"):
            for attr in (
                "geom",
                "sit",
                "batter_id",
                "season",
                "result_hits",
                "result_outs",
                "recency",
            ):
                assert f"bb_pool.{hand}.{attr}" in out

    def test_emits_actor_emb_arrays_per_actor(self):
        art = _synthetic_artifacts()
        out = art.extract_shared_arrays()
        for actor in ("batter", "catcher"):
            for attr in ("vecs", "mean", "std"):
                assert f"actor_emb.{actor}.{attr}" in out

    def test_excludes_object_dtype_arrays(self):
        """outcome_type / event are object-dtype string arrays and cannot live
        in ``multiprocessing.shared_memory`` — they must NOT be in the map."""
        art = _synthetic_artifacts()
        out = art.extract_shared_arrays()
        for key in out:
            assert "outcome_type" not in key
            assert ".event" not in key
            assert "key_index" not in key
            assert "features" not in key

    def test_arrays_are_the_same_buffer_no_copy(self):
        """The map carries the SAME underlying buffer as the source bundle —
        the runner does the single copy into shared memory itself."""
        art = _synthetic_artifacts()
        out = art.extract_shared_arrays()
        # ``a is b`` would be too strict if numpy returns a new view; check the
        # underlying ``base`` / ``data`` pointer matches via tobytes equality
        # plus the original-buffer identity (the canonical "no copy" test).
        assert out["pool.L.geom"] is art.pools["L"].geom
        assert out["bb_pool.R.sit"] is art.bb_pools["R"].sit
        assert out["actor_emb.batter.vecs"] is art.actor_emb["batter"]["vecs"]

    def test_empty_bundle_emits_no_arrays(self):
        art = EngineArtifacts(pools={})
        assert art.extract_shared_arrays() == {}


# ===========================================================================
# The worker path: publish -> _worker_init -> load(shared_views=...)
# ===========================================================================


class TestTheWorkerLoad:
    """The parent loads the bundle and publishes its arrays; the worker
    attaches the segments (:func:`_worker_init`) and loads the bundle with
    them, as :func:`simulation.production_factory` does. No fork: the
    process-pool tests cover the multiprocessing lifecycle."""

    def test_every_published_array_comes_back_as_the_shared_view(self, tmp_path):
        art_dir = _write_bundle(tmp_path)
        parent = EngineArtifacts.load(art_dir)
        payload = parent.extract_shared_arrays()
        assert set(payload) == _expected_keys()
        registry, owned = publish_shared_arrays(payload)
        try:
            _worker_init(registry)
            views = _WORKER_SHARED["views"]
            assert set(views) == set(payload)
            worker = EngineArtifacts.load(art_dir, shared_views=views)
            got = worker.extract_shared_arrays()
            # The same names in the same order, each one the shared view itself.
            assert list(got) == list(payload)
            for key, view in views.items():
                assert got[key] is view, f"{key} was read from disk, not taken from the view"
                assert np.shares_memory(got[key], view)
                np.testing.assert_array_equal(got[key], payload[key])
        finally:
            _worker_init(None)
            unlink_shared_segments(owned)

    def test_the_load_ignores_unknown_keys(self, tmp_path):
        """A stale registry from an earlier build names arrays this bundle
        does not have; the load ignores them and takes the known one."""
        art_dir = _write_bundle(tmp_path)
        geom = np.zeros((_N, 10), dtype=np.float32)
        art = EngineArtifacts.load(
            art_dir,
            shared_views={
                "pool.L.geom": geom,
                "this.does.not.exist": np.ones(5),
                "pool.UNKNOWN_HAND.geom": np.ones(5),
                "actor_emb.UNKNOWN_ACTOR.vecs": np.ones(5),
            },
        )
        assert art.pools["L"].geom is geom
        assert "UNKNOWN_HAND" not in art.pools

    def test_no_registry_reads_every_array_from_disk(self, tmp_path):
        """A None registry (the per-process fallback) leaves the views empty,
        and the load then reads the same arrays from disk."""
        _worker_init(None)
        assert _WORKER_SHARED["views"] == {}
        art_dir = _write_bundle(tmp_path)
        disk = EngineArtifacts.load(art_dir).extract_shared_arrays()
        art = EngineArtifacts.load(art_dir, shared_views=_WORKER_SHARED["views"])
        got = art.extract_shared_arrays()
        assert list(got) == list(disk)
        for key, arr in disk.items():
            np.testing.assert_array_equal(got[key], arr)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
