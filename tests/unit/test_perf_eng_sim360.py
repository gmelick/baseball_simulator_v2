"""
test_perf_eng_sim360.py
=======================
Unit tests for SIM-360 -- the **persistent (warm) ProcessPool + shared-memory
lifecycle** for the long-lived API (Phase 5, Sprint 3).

WHAT SIM-360 CHANGES
--------------------
SIM-332's :class:`simulation.batch_runner.BatchRunner._execute` forked a FRESH
``ProcessPoolExecutor`` (``with ProcessPoolExecutor(...) as pool:``) on EVERY
``run()`` call and tore it down at the end -- a one-shot-script lifecycle.  A
long-lived API would then pay the fork + worker-startup + SIM-333 shared-mem
publish/unlink cost per request.  SIM-360 makes the runner create ONE warm pool
lazily on the first pooled ``_execute`` and REUSE it across ``run()`` calls
(publishing the shared-mem segments ONCE), recreating it only on a worker-count
change, and shutting it down in :meth:`close`.

These tests use REAL multiprocessing (a 2-worker pool) driven by the picklable,
no-DB rng factory (the always-on path) so games run with NO live DuckDB/FAISS
and NO Redis.  Iteration counts are tiny (n=4) so the cross-process run is fast.

Coverage (SIM-360 acceptance criteria):
  * a reused runner reuses the SAME underlying ``ProcessPoolExecutor`` across two
    ``run()`` calls (identity assertion -- no new pool on the 2nd call);
  * both runs produce correct SIM-327 summaries;
  * the SIM-333 shared-mem segments are published exactly ONCE for the warm pool;
  * ``close()`` shuts the pool down AND unlinks the segments, and is idempotent;
  * a worker-count change between runs recreates the pool (documented policy);
  * ``reuse_pool=False`` restores the SIM-332 fresh-pool-per-call behaviour;
  * the synchronous ``max_workers <= 1`` in-process path never builds a pool
    (the always-on determinism path is untouched).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import shared_memory

import numpy as np
import pytest

from simulation.batch_runner import (
    BatchRunner,
    GameSpec,
    InMemoryCache,
    NullCache,
    default_max_workers,
)
from simulation.results import GameSimSummary

# The picklable no-DB factory + a lineup so games progress and end (mirrors
# test_backend_sim332.py's always-on path).
FACTORY = "simulation.batch_runner:rng_driven_machine_factory"
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


def _spec() -> GameSpec:
    return GameSpec(
        machine_factory=FACTORY,
        sim_kwargs={
            "away_lineup": AWAY_LINEUP,
            "home_lineup": HOME_LINEUP,
            "season": 2024,
            "pitcher_id": 477132,
            "bat_hand": "R",
        },
    )


def _pool_workers() -> int:
    """A worker count that forces the POOLED path: at least 2 (capped to keep the
    sandbox light), so ``_execute`` builds a real ProcessPoolExecutor."""
    return max(2, min(2, default_max_workers()))


# ===========================================================================
# The warm pool is created ONCE and REUSED across run() calls   (the core AC)
# ===========================================================================


@pytest.mark.slow
class TestWarmPoolReuse:
    def test_same_pool_instance_reused_across_two_runs(self):
        # A 2-worker (pooled) runner; reuse_pool defaults True.
        runner = BatchRunner(max_workers=2, cache=NullCache())
        try:
            assert runner._pool is None  # nothing built before the first run

            res1 = runner.run(_spec(), n_iterations=4, base_seed=11, use_cache=False)
            pool_after_1 = runner._pool
            # First pooled run created the warm pool.
            assert isinstance(pool_after_1, ProcessPoolExecutor)
            assert runner._pool_workers == 2

            res2 = runner.run(_spec(), n_iterations=4, base_seed=22, use_cache=False)
            pool_after_2 = runner._pool
            # SAME executor instance -> the pool was REUSED, not re-forked.
            assert pool_after_2 is pool_after_1

            # Both runs produced correct SIM-327 summaries.
            for res in (res1, res2):
                assert isinstance(res.summary, GameSimSummary)
                assert res.summary.n_iterations == 4
                assert res.max_workers == 2
                assert abs(
                    res.summary.home_win_pct
                    + res.summary.away_win_pct
                    + res.summary.tie_pct
                    - 1.0
                ) < 1e-9
                assert res.summary.home_scores.shape == (4,)
        finally:
            runner.close()

    def test_pool_is_live_after_first_run_and_dead_after_close(self):
        runner = BatchRunner(max_workers=2, cache=NullCache())
        runner.run(_spec(), n_iterations=4, base_seed=1, use_cache=False)
        pool = runner._pool
        assert pool is not None
        # The executor accepts work while the runner is open.
        assert pool.submit(int, "5").result() == 5
        runner.close()
        assert runner._pool is None  # reference dropped
        # After shutdown the executor rejects new work.
        with pytest.raises(RuntimeError):
            pool.submit(int, "5")


# ===========================================================================
# Shared-mem is published ONCE for the warm pool; close() unlinks + idempotent
# ===========================================================================


@pytest.mark.slow
class TestSharedMemPublishedOnceAndClosed:
    def test_segments_published_once_and_close_unlinks(self):
        arrays = {"kd": (np.arange(40, dtype=np.float64).reshape(5, 8) * 2.0)}
        runner = BatchRunner(max_workers=2, cache=NullCache(), shared_arrays=arrays)
        try:
            runner.run(_spec(), n_iterations=4, base_seed=3, use_cache=False)
            # Published exactly once: the registry + the owned segment exist.
            registry = dict(runner._shared_registry)
            assert "kd" in registry
            assert len(runner._owned_segments) == 1
            name = registry["kd"].shm_name
            # The segment is live (re-attachable) while the warm pool owns it.
            probe = shared_memory.SharedMemory(name=name)
            probe.close()

            # A second run REUSES the same published registry (no republish): the
            # owned-segment handle list is unchanged (same single segment object).
            owned_before = runner._owned_segments[0]
            runner.run(_spec(), n_iterations=4, base_seed=4, use_cache=False)
            assert len(runner._owned_segments) == 1
            assert runner._owned_segments[0] is owned_before
            assert runner._pool is not None  # still the warm pool
        finally:
            runner.close()

        # close() shut the pool down AND unlinked the segment.
        assert runner._pool is None
        assert runner._owned_segments == []
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)
        # Idempotent: a second close is a harmless no-op.
        runner.close()

    def test_close_is_idempotent_without_any_run(self):
        # A runner that never ran (no pool, no segments) closes cleanly + twice.
        runner = BatchRunner(max_workers=2, cache=NullCache())
        runner.close()
        runner.close()
        assert runner._pool is None
        assert runner._owned_segments == []


# ===========================================================================
# Worker-count change between runs RECREATES the pool (documented policy)
# ===========================================================================


@pytest.mark.slow
class TestWorkerCountChangeRecreates:
    def test_run_resolved_workers_floor_skips_pool_then_pooled_run_builds_it(self):
        # max_workers override of 2, but the first run has only 1 iteration -> the
        # resolver floors workers to n_iterations=1 (the in-process path: NO pool).
        # A later run with enough iterations resolves to 2 workers and builds the
        # warm pool -- so the pool is only ever created on the genuinely pooled run.
        runner = BatchRunner(max_workers=2, cache=NullCache())
        try:
            runner.run(_spec(), n_iterations=1, base_seed=5, use_cache=False)
            assert runner._pool is None  # 1 worker -> in-process, never pooled

            runner.run(_spec(), n_iterations=4, base_seed=6, use_cache=False)
            assert runner._pool is not None
            assert runner._pool_workers == 2
        finally:
            runner.close()

    def test_get_pool_recreates_on_size_change(self):
        # Direct unit test of the recreate policy on the _get_pool seam: a new
        # worker count drains the old pool and builds a fresh one.
        runner = BatchRunner(max_workers=4, cache=NullCache())
        try:
            p2 = runner._get_pool(2)
            assert runner._pool_workers == 2
            # Same size -> same instance (reuse).
            assert runner._get_pool(2) is p2
            # Different size -> a NEW pool (recreate), old one shut down.
            p3 = runner._get_pool(3)
            assert p3 is not p2
            assert runner._pool_workers == 3
            with pytest.raises(RuntimeError):
                p2.submit(int, "1")  # old pool was shut down
        finally:
            runner.close()


# ===========================================================================
# reuse_pool=False restores the SIM-332 fresh-pool-per-call behaviour
# ===========================================================================


@pytest.mark.slow
class TestTransientPoolOptOut:
    def test_reuse_pool_false_keeps_no_persistent_pool(self):
        runner = BatchRunner(max_workers=2, cache=NullCache(), reuse_pool=False)
        try:
            res = runner.run(_spec(), n_iterations=4, base_seed=7, use_cache=False)
            # The transient ``with ...`` pool was torn down at the end of the call:
            # no persistent pool reference is retained.
            assert runner._pool is None
            assert res.summary.n_iterations == 4
            res2 = runner.run(_spec(), n_iterations=4, base_seed=8, use_cache=False)
            assert runner._pool is None
            assert res2.summary.n_iterations == 4
        finally:
            runner.close()


# ===========================================================================
# The synchronous in-process path never builds a pool (always-on determinism)
# ===========================================================================


class TestInProcessPathNeverPools:
    def test_max_workers_one_never_creates_a_pool(self):
        runner = BatchRunner(max_workers=1, cache=InMemoryCache())
        res = runner.run(_spec(), n_iterations=6, base_seed=99)
        assert res.max_workers == 1
        assert res.summary.n_iterations == 6
        # No pool was ever created for the in-process path.
        assert runner._pool is None
        runner.close()
        assert runner._pool is None

    def test_in_process_determinism_unchanged_by_reuse_pool_flag(self):
        # reuse_pool must not affect the deterministic in-process path.
        a = BatchRunner(max_workers=1, cache=NullCache(), reuse_pool=True).run(
            _spec(), n_iterations=8, base_seed=321, use_cache=False
        )
        b = BatchRunner(max_workers=1, cache=NullCache(), reuse_pool=False).run(
            _spec(), n_iterations=8, base_seed=321, use_cache=False
        )
        assert np.array_equal(a.summary.home_scores, b.summary.home_scores)
        assert np.array_equal(a.summary.away_scores, b.summary.away_scores)


# ===========================================================================
# Context-manager / transient runner cleans up exactly as before (backward-compat)
# ===========================================================================


@pytest.mark.slow
class TestContextManagerCleanup:
    def test_context_manager_shuts_pool_down_on_exit(self):
        with BatchRunner(max_workers=2, cache=NullCache()) as runner:
            runner.run(_spec(), n_iterations=4, base_seed=2, use_cache=False)
            pool = runner._pool
            assert pool is not None
        # __exit__ -> close() -> pool shut down + reference dropped.
        assert runner._pool is None
        with pytest.raises(RuntimeError):
            pool.submit(int, "1")
