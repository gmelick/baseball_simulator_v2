"""
test_perf_eng_sim333.py
=======================
Unit tests for SIM-333 -- the **shared-memory zero-copy ATTACH** of the read-only
payload (situation KDTree / RBF matrices / FAISS-tile backing arrays) into worker
processes (Phase 4, Sprint 4).  Fills the SIM-332 seam:
:func:`simulation.batch_runner.publish_shared_arrays` (parent) +
:func:`~simulation.batch_runner._worker_init` (worker attach) +
:meth:`simulation.play_pool_sampler.PlayPoolSampler.attach_shared_tile`.

These run with NO live DuckDB/FAISS-on-disk and NO Redis.  The always-on tests use
an IN-PROCESS attach (deterministic, fast) plus ONE single spawned worker; heavy
multi-process pools are ``@pytest.mark.slow``.

Coverage (SIM-333 acceptance criteria):
  * a segment created in the parent attaches + reads IDENTICALLY in a worker (same
    bytes) -- both in-process and across a spawned worker;
  * the attach is the SAME physical buffer, not a copy (write-through proof);
  * the ``{name: SharedArrayDescriptor}`` registry round-trips (picklable);
  * the NO-segments fallback leaves the worker global empty (SIM-332 path intact);
  * lifecycle: workers attach + close but NEVER unlink; the parent owns unlink and
    leaves no ``/dev/shm`` leak;
  * the sampler attaches a tile zero-copy (rowids share memory) and samples over
    the shared buffer with no disk read.
"""

from __future__ import annotations

import pickle
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import shared_memory

import numpy as np
import pytest

import simulation.batch_runner as br
from simulation.batch_runner import (
    BatchRunner,
    SharedArrayDescriptor,
    get_shared_view,
    publish_shared_arrays,
    unlink_shared_segments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload():
    """A small read-only payload standing in for the KDTree data + rowids."""
    kd = np.arange(60, dtype=np.float64).reshape(5, 12) * 1.5
    rowids = np.array([10, 20, 30, 40, 50], dtype=np.int64)
    rbf = np.linspace(-1.0, 1.0, 24, dtype=np.float32).reshape(4, 6)
    return {"kd": kd, "rowids": rowids, "rbf": rbf}


# Module-level (picklable) worker probe for the spawned-worker tests.
def _probe_views(_ignored):
    """Read every attached view in a worker; return checkable scalars + a flag."""
    kd = get_shared_view("kd")
    rowids = get_shared_view("rowids")
    rbf = get_shared_view("rbf")
    return {
        "kd_sum": float(kd.sum()),
        "rowids": tuple(int(x) for x in rowids.tolist()),
        "rbf_sum": float(rbf.sum()),
        "kd_writeable": bool(kd.flags.writeable),
        "missing_is_none": get_shared_view("does_not_exist") is None,
    }


def _probe_fallback(_ignored):
    """A worker started with NO registry: every view must be absent."""
    return {
        "kd": get_shared_view("kd") is None,
        "views_empty": br._WORKER_SHARED.get("views") == {},
    }


# ===========================================================================
# Registry round-trip (picklable {name: SharedArrayDescriptor})  (AC #1/#4)
# ===========================================================================


class TestRegistryRoundTrip:
    def test_publish_returns_descriptor_registry(self):
        arrays = _payload()
        registry, owned = publish_shared_arrays(arrays)
        try:
            assert set(registry) == set(arrays)
            for name, desc in registry.items():
                assert isinstance(desc, SharedArrayDescriptor)
                assert desc.shape == tuple(arrays[name].shape)
                assert desc.dtype == str(arrays[name].dtype)
                assert isinstance(desc.shm_name, str) and desc.shm_name
        finally:
            unlink_shared_segments(owned)

    def test_registry_is_picklable(self):
        arrays = _payload()
        registry, owned = publish_shared_arrays(arrays)
        try:
            # The registry crosses the process boundary via initargs -> must pickle.
            round_tripped = pickle.loads(pickle.dumps(registry))
            assert round_tripped == registry
        finally:
            unlink_shared_segments(owned)


# ===========================================================================
# In-process attach: identical bytes + SAME buffer (not a copy)  (AC #1/#2)
# ===========================================================================


class TestInProcessAttach:
    def test_worker_init_attaches_identical_bytes(self):
        arrays = _payload()
        registry, owned = publish_shared_arrays(arrays)
        try:
            br._worker_init(registry)  # simulate the pool initializer in-process
            for name, src in arrays.items():
                view = get_shared_view(name)
                assert view is not None
                assert view.shape == src.shape
                assert view.dtype == src.dtype
                assert np.array_equal(view, src)  # identical bytes
                assert view.flags.writeable is False  # read-only contract
            # Cleanup the worker-side handles, then the parent unlinks.
            for shm in br._WORKER_SHARED["_handles"]:
                shm.close()
        finally:
            unlink_shared_segments(owned)
            br._worker_init(None)  # reset the process-global

    def test_attached_view_is_same_buffer_not_a_copy(self):
        # Write-through proof: mutate via the parent-owned segment; an independently
        # attached view (what a worker builds) must SEE the change -> same physical
        # memory, no copy.
        a = np.arange(8, dtype=np.float64)
        registry, owned = publish_shared_arrays({"a": a})
        try:
            desc = registry["a"]
            attached = shared_memory.SharedMemory(name=desc.shm_name)
            try:
                view = np.ndarray(desc.shape, dtype=np.dtype(desc.dtype), buffer=attached.buf)
                owner_view = np.ndarray(desc.shape, dtype=np.dtype(desc.dtype), buffer=owned[0].buf)
                owner_view[0] = 999.0
                assert view[0] == 999.0  # saw the parent's write -> shared buffer
            finally:
                attached.close()
        finally:
            unlink_shared_segments(owned)


# ===========================================================================
# No-segments fallback (the SIM-332 per-process path stays intact)  (AC #3)
# ===========================================================================


class TestFallback:
    def test_worker_init_none_leaves_global_empty(self):
        br._worker_init(None)
        assert br._WORKER_SHARED["views"] == {}
        assert br._WORKER_SHARED["_handles"] == []
        assert get_shared_view("anything") is None

    def test_pool_kwargs_passes_none_when_no_shared_arrays(self):
        runner = BatchRunner(max_workers=2)  # no shared_arrays
        kw = runner._pool_kwargs()
        assert kw["initializer"] is br._worker_init
        assert kw["initargs"] == (None,)  # fallback -> empty registry
        runner.close()

    def test_pool_kwargs_passes_registry_when_shared_arrays_set(self):
        runner = BatchRunner(max_workers=2, shared_arrays={"kd": _payload()["kd"]})
        try:
            kw = runner._pool_kwargs()
            registry = kw["initargs"][0]
            assert registry is not None
            assert "kd" in registry
            assert isinstance(registry["kd"], SharedArrayDescriptor)
        finally:
            runner.close()


# ===========================================================================
# Lifecycle: parent owns unlink; no /dev/shm leak                 (AC #2/#4)
# ===========================================================================


class TestLifecycle:
    def test_close_unlinks_and_is_idempotent(self):
        runner = BatchRunner(max_workers=2, shared_arrays={"kd": _payload()["kd"]})
        registry = runner.shared_registry  # forces publish
        name = registry["kd"].shm_name
        # The segment exists (re-attachable) while owned.
        probe = shared_memory.SharedMemory(name=name)
        probe.close()
        runner.close()
        # After close -> unlinked: re-attaching by name must fail.
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)
        # Idempotent: a second close is a harmless no-op.
        runner.close()

    def test_context_manager_unlinks_on_exit(self):
        with BatchRunner(max_workers=2, shared_arrays={"kd": _payload()["kd"]}) as runner:
            name = runner.shared_registry["kd"].shm_name
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)

    def test_worker_attaches_then_closes_without_unlinking(self):
        # The worker side closes its OWN handle but must not unlink; the parent's
        # segment stays alive + re-attachable afterwards.
        arrays = {"kd": _payload()["kd"]}
        registry, owned = publish_shared_arrays(arrays)
        try:
            br._worker_init(registry)
            for shm in br._WORKER_SHARED["_handles"]:
                shm.close()  # worker close -- NOT unlink
            br._worker_init(None)
            # Parent segment still alive: re-attach succeeds (worker never unlinked).
            again = shared_memory.SharedMemory(name=registry["kd"].shm_name)
            again.close()
        finally:
            unlink_shared_segments(owned)


# ===========================================================================
# A single spawned worker attaches + reads identically            (AC #1/#4)
# ===========================================================================
# One real cross-process attach kept in the always-on suite (a single worker, one
# task) -- fast + deterministic; the heavy pool runs are @slow below.


class TestSingleSpawnedWorker:
    def test_one_worker_reads_identical_shared_bytes(self):
        arrays = _payload()
        registry, owned = publish_shared_arrays(arrays)
        try:
            with ProcessPoolExecutor(
                max_workers=1,
                initializer=br._worker_init,
                initargs=(registry,),
            ) as pool:
                got = list(pool.map(_probe_views, range(1)))[0]
            assert got["kd_sum"] == float(arrays["kd"].sum())
            assert got["rowids"] == tuple(arrays["rowids"].tolist())
            assert got["rbf_sum"] == pytest.approx(float(arrays["rbf"].sum()))
            assert got["kd_writeable"] is False  # read-only in the worker
            assert got["missing_is_none"] is True
        finally:
            unlink_shared_segments(owned)

    def test_one_worker_no_registry_has_empty_global(self):
        with ProcessPoolExecutor(
            max_workers=1,
            initializer=br._worker_init,
            initargs=(None,),
        ) as pool:
            got = list(pool.map(_probe_fallback, range(1)))[0]
        assert got["kd"] is True
        assert got["views_empty"] is True


# ===========================================================================
# Sampler shared-tile attach is zero-copy + samples over shared mem (AC #1/#2)
# ===========================================================================


class TestSamplerSharedTileAttach:
    def test_attach_shared_tile_rowids_zero_copy_and_samples(self):
        pytest.importorskip("faiss")  # guarded like the sampler itself
        from simulation.play_pool_sampler import POOL_PITCH, PlayPoolSampler

        rng = np.random.default_rng(7)
        vecs = rng.standard_normal((40, 10)).astype(np.float32)
        rowids = (np.arange(40) + 1000).astype(np.int64)
        registry, owned = publish_shared_arrays({"pv": vecs, "pr": rowids})
        shm_v = shared_memory.SharedMemory(name=registry["pv"].shm_name)
        shm_r = shared_memory.SharedMemory(name=registry["pr"].shm_name)
        try:
            vview = np.ndarray(registry["pv"].shape, dtype=np.float32, buffer=shm_v.buf)
            rview = np.ndarray(registry["pr"].shape, dtype=np.int64, buffer=shm_r.buf)
            samp = PlayPoolSampler(
                pool_dir="/nonexistent",
                outcome_fetch=lambda pool, ids: dict.fromkeys(ids, "single"),
            )
            handle = samp.attach_shared_tile(
                POOL_PITCH,
                2024,
                "R",
                vectors=vview,
                rowids=rview,
                pitcher_id=12345,
                meta={"season": 2024},
            )
            # rowids on the handle is the SAME shared buffer (zero-copy).
            assert np.shares_memory(handle.rowids, rview)
            assert handle.n_vectors == 40
            # A real k-NN sample resolves with NO disk read (the attached tile is
            # served from the LRU, fall-back disk probe never fires).
            res = samp.sample_pitch(12345, "R", 2024, vecs[0], k=5)
            assert res["row_id"] in rowids.tolist()
            assert res["pitch_outcome"] == "single"
            assert res["tile"] == "2024/12345/R"
            samp.close()
        finally:
            shm_v.close()
            shm_r.close()
            unlink_shared_segments(owned)


# ===========================================================================
# Heavy / real multi-process pool runs (NOT in the always-on suite)
# ===========================================================================


@pytest.mark.slow
class TestSlowMultiWorker:
    def test_multiple_workers_all_read_identical_shared_bytes(self):
        arrays = _payload()
        registry, owned = publish_shared_arrays(arrays)
        try:
            with ProcessPoolExecutor(
                max_workers=4,
                initializer=br._worker_init,
                initargs=(registry,),
            ) as pool:
                results = list(pool.map(_probe_views, range(16)))
            kd_sum = float(arrays["kd"].sum())
            assert all(r["kd_sum"] == kd_sum for r in results)
            assert all(r["rowids"] == tuple(arrays["rowids"].tolist()) for r in results)
            assert all(r["kd_writeable"] is False for r in results)
        finally:
            unlink_shared_segments(owned)
