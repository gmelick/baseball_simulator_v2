"""
test_sim402_prewarm.py
======================
Unit tests for SIM-402 -- the cold-worker ``/simulate`` SLA fix.

Two contracts are covered here (the wall-clock SLA itself is verified live in
the container; these lock the CONTRACTS the speed-up rests on):

  1. **The factory has no fallback (SIM-486)** -- with no engine-artifact bundle
     on disk, :func:`simulation.production_factory.production_machine_factory`
     raises loudly and :func:`warm_worker_cache` reports ``False``; the per-tile
     fallback and its fingerprint deriver are gone.

  2. **Worker pre-warm** -- :func:`production_factory.warm_worker_cache` populates
     the per-process full-pool cache, and :meth:`BatchRunner.prewarm` runs it on
     every warm-pool worker up front.  This is the fix for the n>1 cold-fan-out
     stall: a fresh n-iteration batch gives each worker one game, so the per-worker
     cache (which only pays off on the 2nd+ seed) never warms in time -- prewarm
     does it once per worker at startup instead.

All sandbox-runnable: the full-pool build is monkeypatched (no artifacts/DuckDB),
and only the slow-marked tests spawn real worker processes.
"""

from __future__ import annotations

import pytest

import simulation.production_factory as pf
from simulation.batch_runner import BatchRunner, GameSpec
from simulation.production_factory import (
    production_machine_factory,
    reset_caches,
    warm_worker_cache,
)
from simulation.sim_loop import StateMachine


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


# ===========================================================================
# 1. No fallback (SIM-486): the factory raises without a bundle
# ===========================================================================


class TestNoFallback:
    def test_dotted_ref_resolves_to_the_production_factory(self):
        from simulation.batch_runner import _resolve_dotted

        ref = "simulation.production_factory:production_machine_factory"
        assert _resolve_dotted(ref) is production_machine_factory

    def test_factory_raises_loudly_without_a_bundle(self, tmp_path):
        spec = _spec(_pool_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="SIM-486"):
            production_machine_factory(1, spec)

    def test_warm_reports_false_without_a_bundle(self, tmp_path):
        assert warm_worker_cache(str(tmp_path)) is False

    def test_factory_wires_the_built_sampler(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(pf, "_build_full_pool_sampler", lambda spec, seed: sentinel)
        machine = production_machine_factory(7, _spec())
        assert isinstance(machine, StateMachine)
        assert machine.full_pool_sampler is sentinel


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
    no-op test warm (no bundle on disk) returns instantly, so one worker can
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
