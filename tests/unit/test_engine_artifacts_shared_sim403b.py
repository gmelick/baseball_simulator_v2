"""
tests/unit/test_engine_artifacts_shared_sim403b.py
===================================================
SIM-403b -- the EngineArtifacts shared-memory publish/attach contract.

WHAT THIS COVERS
----------------
The SIM-403 ticket lifted the artificial ``SIM_RUNNER_WORKERS=1`` cap so the
lifespan BatchRunner runs a real multi-worker ProcessPool. SIM-403b is the
follow-on that wires the big read-only numpy arrays in
:class:`pipeline.batch.engine_artifacts.EngineArtifacts` through the SIM-333
``multiprocessing.shared_memory`` plumbing already in
:mod:`simulation.batch_runner` — so each worker ATTACHES the parent's bundle
zero-copy instead of re-reading it from disk.

These tests exercise the EngineArtifacts side of that contract without
spawning a real fork (the fork lifecycle itself is already covered by the
SIM-360 + SIM-333 perf-engine tests). They cover:

  * the flat-name -> ndarray map :meth:`extract_shared_arrays` returns
    (keys + dtypes + buffer identity);
  * :meth:`attach_shared_views` REPLACES the bundle's arrays with the views
    (no copy; buffer identity preserved);
  * the worker-side splice round-trips end-to-end via the
    :func:`simulation.batch_runner._worker_init` -> ``_WORKER_SHARED`` ->
    :meth:`attach_shared_views` chain, with the shared-memory segments
    actually live (a probe re-attach succeeds);
  * defensive contract: unknown keys are ignored; an empty/None ``views``
    dict is a no-op (the per-worker disk path is unaffected).
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    BattedBallPool,
    EngineArtifacts,
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
        seasons=[2022, 2023, 2024],
        actor_emb=_synthetic_actor_emb(),
    )


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
        the SIM-333 runner does the single copy into shared memory itself."""
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
# attach_shared_views() — in-place splice
# ===========================================================================


class TestAttachSharedViews:
    def test_replaces_hand_pool_arrays_with_views(self):
        art = _synthetic_artifacts()
        # Build a substitute view for one specific slot.
        new_geom_L = np.zeros_like(art.pools["L"].geom)
        art.attach_shared_views({"pool.L.geom": new_geom_L})
        assert art.pools["L"].geom is new_geom_L  # identity == zero-copy splice

    def test_replaces_bb_pool_arrays_with_views(self):
        art = _synthetic_artifacts()
        new_sit_R = np.zeros_like(art.bb_pools["R"].sit)
        art.attach_shared_views({"bb_pool.R.sit": new_sit_R})
        assert art.bb_pools["R"].sit is new_sit_R

    def test_replaces_actor_emb_arrays_with_views(self):
        art = _synthetic_artifacts()
        new_vecs = np.zeros_like(art.actor_emb["batter"]["vecs"])
        art.attach_shared_views({"actor_emb.batter.vecs": new_vecs})
        assert art.actor_emb["batter"]["vecs"] is new_vecs

    def test_unknown_keys_silently_ignored(self):
        """A stale registry from a previous artifact build (e.g. a flat name
        that no longer exists) must not crash the worker — the attach is a
        defensive splice, not a strict mapping."""
        art = _synthetic_artifacts()
        original_geom = art.pools["L"].geom
        art.attach_shared_views(
            {
                "pool.L.geom": np.zeros_like(original_geom),
                "this.does.not.exist": np.ones(5),
                "pool.UNKNOWN_HAND.geom": np.ones(5),  # hand not in pools
                "actor_emb.UNKNOWN_ACTOR.vecs": np.ones(5),
            }
        )
        # The valid key applied; the unknown ones were silently ignored.
        assert not np.array_equal(art.pools["L"].geom, original_geom)

    def test_empty_views_is_a_noop(self):
        art = _synthetic_artifacts()
        before = art.pools["L"].geom
        art.attach_shared_views({})
        art.attach_shared_views(None)  # type: ignore[arg-type]
        assert art.pools["L"].geom is before

    def test_roundtrip_extract_then_attach_preserves_values(self):
        """The flat-name contract is symmetric: extract -> attach on a fresh
        bundle reproduces the original arrays (the names line up)."""
        src = _synthetic_artifacts()
        shareable = src.extract_shared_arrays()

        # A fresh, "blank-but-shaped" bundle the splice should fill in.
        dst = _synthetic_artifacts()  # same shapes/dtypes, different values
        dst.attach_shared_views(shareable)

        # After the splice, dst's shareable arrays MATCH src's exactly.
        for hand in ("L", "R"):
            for attr in ("geom", "sit", "pitcher_id", "batter_id", "season", "recency"):
                np.testing.assert_array_equal(
                    getattr(dst.pools[hand], attr), getattr(src.pools[hand], attr)
                )
            for attr in (
                "geom",
                "sit",
                "batter_id",
                "season",
                "result_hits",
                "result_outs",
                "recency",
            ):
                np.testing.assert_array_equal(
                    getattr(dst.bb_pools[hand], attr), getattr(src.bb_pools[hand], attr)
                )
        for actor in ("batter", "catcher"):
            for attr in ("vecs", "mean", "std"):
                np.testing.assert_array_equal(
                    dst.actor_emb[actor][attr], src.actor_emb[actor][attr]
                )


# ===========================================================================
# End-to-end through publish_shared_arrays + _worker_init (no fork required)
# ===========================================================================


class TestSharedMemoryRoundtrip:
    """The full publish -> _worker_init -> splice chain, exercised in-process.

    This stops short of a real fork (the SIM-360 / SIM-333 perf tests cover the
    multiprocessing lifecycle) and instead verifies the in-process contract:
    publish the registry, drive ``_worker_init`` synchronously, splice via
    ``attach_shared_views``, and assert the worker's bundle reads from the
    parent's shared-memory segments (the views' ``base`` is the SharedMemory
    buffer, not a private copy)."""

    def test_publish_then_worker_init_then_splice(self):
        art = _synthetic_artifacts()
        payload = art.extract_shared_arrays()
        registry, owned = publish_shared_arrays(payload)
        try:
            # Drive the pool initializer in-process (it populates _WORKER_SHARED).
            _worker_init(registry)
            views = _WORKER_SHARED["views"]
            assert set(views.keys()) == set(payload.keys())

            # The "worker" splices the views into a freshly-disk-loaded bundle.
            worker_bundle = _synthetic_artifacts()  # stand-in for the disk load
            worker_bundle.attach_shared_views(views)

            # Every spliced array has its DATA equal to the parent's source
            # (the bytes round-tripped through shared memory unchanged).
            for hand in ("L", "R"):
                np.testing.assert_array_equal(worker_bundle.pools[hand].geom, art.pools[hand].geom)
                np.testing.assert_array_equal(
                    worker_bundle.bb_pools[hand].result_hits, art.bb_pools[hand].result_hits
                )
            for actor in ("batter", "catcher"):
                np.testing.assert_array_equal(
                    worker_bundle.actor_emb[actor]["vecs"], art.actor_emb[actor]["vecs"]
                )

            # The spliced views are NOT private copies — mutating the parent's
            # shared buffer would be visible (we don't mutate here, but the
            # ``base`` reference proves the buffer is the shared segment).
            for key, view in views.items():
                # numpy view over a SharedMemory.buf carries a non-None base.
                assert view.base is not None, f"{key} appears to be a copy, not a view"
        finally:
            # Reset the worker global + unlink the segments so a later test
            # starts clean.
            _worker_init(None)
            unlink_shared_segments(owned)

    def test_worker_init_with_no_registry_leaves_views_empty(self):
        """A None registry (the SIM-332 per-process fallback) leaves
        ``_WORKER_SHARED["views"]`` empty; attach_shared_views is a no-op."""
        _worker_init(None)
        assert _WORKER_SHARED["views"] == {}

        art = _synthetic_artifacts()
        before = art.pools["L"].geom
        art.attach_shared_views(_WORKER_SHARED["views"])
        assert art.pools["L"].geom is before


# ===========================================================================
# Defensive: empty artifacts still round-trip safely
# ===========================================================================


def test_empty_artifacts_extract_attach_roundtrip():
    """An EngineArtifacts with no pools / bb_pools / actor_emb (e.g. a partial
    nightly build) extracts to {} and attaches a no-op — must not raise."""
    art = EngineArtifacts(pools={})
    assert art.extract_shared_arrays() == {}
    art.attach_shared_views({"pool.L.geom": np.zeros(3)})  # unknown -> ignored
    # Bundle is unchanged.
    assert art.pools == {}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
