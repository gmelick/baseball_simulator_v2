"""
test_sim402_prewarm.py
======================
Unit tests for SIM-402 -- the cold-worker ``/simulate`` SLA fix.

Two structural changes are covered here (the wall-clock SLA itself is verified
live in the container; these lock the CONTRACTS the speed-up rests on):

  1. **Deriver skip on the full-pool path** -- when the full-pool sampler is
     active, :func:`simulation.production_factory.production_machine_factory` must
     NOT build the per-tile :class:`FingerprintDeriver` (it is unused on that path
     but ``_default_deriver_builder`` does three eager disk loads on EVERY seed).
     The per-tile fallback path (the unit-test default, ``SIM_FULL_POOL=0``) must
     STILL build it.

  2. **Worker pre-warm** -- :func:`production_factory.warm_worker_cache` populates
     the per-process full-pool cache, and :meth:`BatchRunner.prewarm` runs it on
     every warm-pool worker up front.  This is the fix for the n>1 cold-fan-out
     stall: a fresh n-iteration batch gives each worker one game, so the per-worker
     cache (which only pays off on the 2nd+ seed) never warms in time -- prewarm
     does it once per worker at startup instead.

All sandbox-runnable: the full-pool build is monkeypatched (no artifacts/DuckDB),
and only the one slow-marked test spawns real worker processes.
"""

from __future__ import annotations

import numpy as np
import pytest

import simulation.production_factory as pf
from simulation.batch_runner import BatchRunner, GameSpec
from simulation.production_factory import (
    production_machine_factory,
    reset_caches,
    warm_worker_cache,
)
from simulation.sim_loop import StateMachine


class _MockSampler:
    """A dependency-light stand-in for :class:`PlayPoolSampler` (holds an ``rng``
    so the ``simulate_game`` re-seed seam finds it; never touches DuckDB/FAISS)."""

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else np.random.default_rng()


class _FakeArt:
    """Minimal stand-in for EngineArtifacts: just the two per-hand pool maps."""

    def __init__(self):
        self.pools = {"R": object(), "L": object()}
        self.bb_pools = {"R": object(), "L": object()}


class _FakeSampler:
    """Records which hands had their lazy per-hand precomputes triggered."""

    def __init__(self, art=None):
        self.a = art if art is not None else _FakeArt()
        self.pool_meta_calls: list[str] = []
        self.bb_idx_calls: list[str] = []

    def _pool_meta(self, hand):
        self.pool_meta_calls.append(hand)
        return {}

    def _bb_pool_bat_idx(self, hand):
        self.bb_idx_calls.append(hand)
        return None


def _spec(**extra) -> GameSpec:
    kw = {
        "pitcher_id": 477132,
        "bat_hand": "R",
        "season": 2024,
        "away_lineup": list(range(101, 110)),
        "home_lineup": list(range(201, 210)),
    }
    kw.update(extra)
    return GameSpec(
        machine_factory="simulation.production_factory:production_machine_factory",
        sim_kwargs=kw,
    )


@pytest.fixture(autouse=True)
def _clean_caches_and_builders():
    """Keep the module-global caches + builder hooks from leaking between tests."""
    reset_caches()
    yield
    reset_caches()
    pf.set_sampler_builder(None)
    pf.set_deriver_builder(None)


# ===========================================================================
# 1. Deriver skip on the full-pool path
# ===========================================================================


class TestDeriverSkippedOnFullPoolPath:
    def test_full_pool_active_skips_deriver_build(self, monkeypatch):
        """When the full-pool sampler builds, the deriver builder is NOT called and
        the machine carries ``fingerprint_deriver=None`` + the full-pool sampler."""
        sentinel = object()
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: sentinel)
        deriver_calls: list[int] = []
        pf.set_deriver_builder(lambda spec: deriver_calls.append(1) or "DERIVER")

        with pf.use_sampler_builder(lambda spec, seed: _MockSampler()):
            machine = production_machine_factory(7, _spec())

        assert isinstance(machine, StateMachine)
        assert machine.full_pool_sampler is sentinel
        assert machine.fingerprint_deriver is None
        assert deriver_calls == []  # the 3-disk-load builder was skipped entirely

    def test_per_tile_path_still_builds_deriver(self, monkeypatch):
        """With no full-pool sampler (the per-tile / unit-test default) the deriver
        builder is STILL invoked -- that path is unchanged by SIM-402."""
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: None)
        pf.set_deriver_builder(lambda spec: "DERIVER")

        with pf.use_sampler_builder(lambda spec, seed: _MockSampler()):
            machine = production_machine_factory(7, _spec())

        assert machine.full_pool_sampler is None
        assert machine.fingerprint_deriver == "DERIVER"

    def test_full_pool_path_still_wires_a_sampler(self, monkeypatch):
        """The per-tile sampler is still wired (the StateMachine's ``_pa`` guard
        needs it) even though the full-pool path never calls it for draws."""
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: object())
        mock = _MockSampler()
        with pf.use_sampler_builder(lambda spec, seed: mock):
            machine = production_machine_factory(7, _spec())
        assert machine.sampler is mock
        assert machine._pa is not None


# ===========================================================================
# 2a. warm_worker_cache + reset_caches
# ===========================================================================


class TestWarmWorkerCache:
    def test_returns_true_when_full_pool_sampler_builds(self, monkeypatch):
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: object())
        assert warm_worker_cache() is True

    def test_returns_false_when_nothing_to_cache(self, monkeypatch):
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: None)
        assert warm_worker_cache() is False

    def test_passes_pool_dir_through(self, monkeypatch):
        seen: list[str | None] = []

        def _spy(spec, seed):
            seen.append(spec.sim_kwargs.get("_pool_dir"))
            return object()

        monkeypatch.setattr(pf, "_build_full_pool_sampler", _spy)
        warm_worker_cache("/data/play_pool")
        assert seen == ["/data/play_pool"]

    def test_never_raises(self, monkeypatch):
        def _boom(spec, seed):
            raise RuntimeError("artifact bundle exploded")

        monkeypatch.setattr(pf, "_build_full_pool_sampler", _boom)
        assert warm_worker_cache() is False  # swallowed, reported as False

    def test_reset_caches_clears_globals(self):
        pf._CACHED_FULL_POOL_SAMPLER = object()
        pf._CACHED_FULL_POOL_ART_DIR = "/somewhere"
        reset_caches()
        assert pf._CACHED_FULL_POOL_SAMPLER is None
        assert pf._CACHED_FULL_POOL_ART_DIR is None


# ===========================================================================
# 2a-bis. _warm_sampler triggers the lazy per-hand precomputes (the FULL warm)
# ===========================================================================


class TestWarmSampler:
    def test_triggers_both_precomputes_for_every_hand(self):
        s = _FakeSampler()
        pf._warm_sampler(s)
        assert sorted(s.pool_meta_calls) == ["L", "R"]
        assert sorted(s.bb_idx_calls) == ["L", "R"]

    def test_defensive_on_bare_object(self):
        pf._warm_sampler(object())  # no .a / no precompute methods -> must not raise

    def test_defensive_when_a_precompute_raises(self):
        s = _FakeSampler()

        def _boom(hand):
            raise RuntimeError("pool_meta blew up")

        s._pool_meta = _boom
        pf._warm_sampler(s)  # the pitch precompute raises; bb still warmed, no raise
        assert sorted(s.bb_idx_calls) == ["L", "R"]

    def test_warm_worker_cache_warms_the_built_sampler(self, monkeypatch):
        s = _FakeSampler()
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: s)
        assert warm_worker_cache() is True
        assert sorted(s.pool_meta_calls) == ["L", "R"]
        assert sorted(s.bb_idx_calls) == ["L", "R"]


# ===========================================================================
# 2b. BatchRunner.prewarm -- in-process + defensive paths (no real fork)
# ===========================================================================


class TestPrewarmInProcess:
    def test_in_process_path_warms_current_process(self, monkeypatch):
        calls: list[str | None] = []
        monkeypatch.setattr(
            pf, "warm_worker_cache", lambda pool_dir=None: calls.append(pool_dir) or True
        )
        runner = BatchRunner(max_workers=1)
        try:
            assert runner.prewarm("/data/play_pool") == 1
            assert calls == ["/data/play_pool"]
        finally:
            runner.close()

    def test_in_process_returns_zero_when_nothing_cached(self, monkeypatch):
        monkeypatch.setattr(pf, "warm_worker_cache", lambda pool_dir=None: False)
        runner = BatchRunner(max_workers=1)
        try:
            assert runner.prewarm() == 0
        finally:
            runner.close()

    def test_reuse_pool_off_warms_in_process_without_a_pool(self, monkeypatch):
        """reuse_pool=False -> a pooled prewarm would be discarded, so warm the
        current process instead (and never spawn a pool)."""
        calls: list[str | None] = []
        monkeypatch.setattr(
            pf, "warm_worker_cache", lambda pool_dir=None: calls.append(pool_dir) or True
        )
        runner = BatchRunner(max_workers=4, reuse_pool=False)
        try:
            assert runner.prewarm() == 1
            assert len(calls) == 1  # warmed once, in-process
            assert runner._pool is None  # no pool was created
        finally:
            runner.close()

    def test_prewarm_swallows_warm_errors(self, monkeypatch):
        def _boom(pool_dir=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(pf, "warm_worker_cache", _boom)
        runner = BatchRunner(max_workers=1)
        try:
            assert runner.prewarm() == 0  # must not raise
        finally:
            runner.close()

    def test_accepts_timeout_kwarg(self, monkeypatch):
        # The bound is a keyword-only knob; the in-process path accepts and ignores it.
        monkeypatch.setattr(pf, "warm_worker_cache", lambda pool_dir=None: True)
        runner = BatchRunner(max_workers=1)
        try:
            assert runner.prewarm(timeout=5) == 1
        finally:
            runner.close()


# ===========================================================================
# 2c. BatchRunner.prewarm -- the pooled path actually spawns every worker
# ===========================================================================


@pytest.mark.slow
@pytest.mark.timeout(60)
def test_prewarm_pooled_warms_workers_bounded():
    """Pooled prewarm warms the pool in bounded-concurrency waves (a semaphore caps
    simultaneous warms so it can't OOM by warming all W at once) and returns a worker
    count without hanging or raising.

    We assert the ROBUST contract (``>= 1``), not exactly W: with real per-worker warm
    latency the executor spawns all W (each task holds a worker for seconds), but the
    no-op test warm (``SIM_FULL_POOL`` unset) returns instantly, so one worker can
    service multiple tasks before the others spawn — a test-only coalescing, not a
    production behaviour."""
    runner = BatchRunner(max_workers=2, reuse_pool=True)
    try:
        warmed = runner.prewarm()
        assert isinstance(warmed, int) and warmed >= 1
    finally:
        runner.close()


@pytest.mark.slow
@pytest.mark.timeout(60)
def test_prewarm_pooled_respects_tiny_timeout():
    """A tiny deadline returns promptly with a partial/zero count instead of
    hanging — the bound that stops a stalled environment from wedging startup."""
    runner = BatchRunner(max_workers=2, reuse_pool=True)
    try:
        warmed = runner.prewarm(timeout=0.001)
        assert isinstance(warmed, int) and warmed >= 0  # bounded, never raises/hangs
    finally:
        runner.close()
