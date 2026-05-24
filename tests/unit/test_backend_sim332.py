"""
test_backend_sim332.py
======================
Unit tests for SIM-332 -- the **parallel 100-iteration batch runner**
:mod:`simulation.batch_runner` (Phase 4, Sprint 3).

These run with NO live DuckDB/FAISS and NO Redis server: the runner is driven by
the picklable, rng-driven no-DB machine factory (the always-on path) and the
in-memory cache fallback.  Heavy / real-parallel runs are marked ``slow``.

Coverage (the SIM-332 acceptance criteria):
  * a small batch returns a valid SIM-327 :class:`GameSimSummary`;
  * a fixed base seed REPRODUCES the summary exactly (determinism, §6.3);
  * distinct per-iteration seeds produce VARIATION across games;
  * ``derive_seed`` derives a distinct deterministic seed per iteration;
  * ``default_max_workers`` == ``min(cpu - 1, 10)`` (the SIM-281 ceiling);
  * the cache returns the MEMOIZED summary on the second call (in-memory
    fallback), and ``make_cache(prefer_redis=False)`` never needs a server.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.results import GameSimSummary
from simulation.batch_runner import (
    MAX_WORKER_CEILING,
    POOL_QUERY_TTL_S,
    SIM_RESULT_TTL_S,
    BatchRunner,
    GameSpec,
    InMemoryCache,
    NullCache,
    default_max_workers,
    derive_seed,
    make_cache,
)

# The picklable no-DB factory + a lineup so games progress and end.
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


def _runner(**kw) -> BatchRunner:
    # workers=1 -> synchronous in-process (fast + deterministic) by default.
    kw.setdefault("max_workers", 1)
    kw.setdefault("cache", InMemoryCache())
    return BatchRunner(**kw)


# ===========================================================================
# derive_seed -- per-game seed isolation (AC #2)
# ===========================================================================


class TestDeriveSeed:
    def test_distinct_deterministic_seed_per_iteration(self):
        seeds = [derive_seed(1000, i) for i in range(8)]
        assert seeds == [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007]
        # All distinct (each game differs) and a pure function of the base.
        assert len(set(seeds)) == 8

    def test_none_base_seed_yields_none(self):
        assert derive_seed(None, 5) is None

    def test_pure_function_of_base(self):
        # Same base -> identical derivation (reproducible batch).
        assert [derive_seed(7, i) for i in range(4)] == \
               [derive_seed(7, i) for i in range(4)]


# ===========================================================================
# default_max_workers -- the SIM-281 ceiling min(cpu-1, 10)  (AC #5)
# ===========================================================================


class TestMaxWorkers:
    def test_formula_is_min_cpu_minus_one_and_ten(self):
        assert default_max_workers(cpu_count=4) == 3
        assert default_max_workers(cpu_count=8) == 7
        # The 10-worker ceiling holds on big hosts (SIM-281 D1 / SIM-280 §4).
        assert default_max_workers(cpu_count=16) == MAX_WORKER_CEILING == 10
        assert default_max_workers(cpu_count=64) == 10

    def test_floored_at_one_on_single_core(self):
        assert default_max_workers(cpu_count=1) == 1
        assert default_max_workers(cpu_count=2) == 1

    def test_runner_never_exceeds_iterations(self):
        r = BatchRunner(max_workers=8, cache=NullCache())
        # Only 3 iterations -> at most 3 workers (no idle workers).
        assert r.resolve_max_workers(3) == 3
        assert r.resolve_max_workers(100) == 8


# ===========================================================================
# A small batch returns a valid SIM-327 summary  (AC #1/#5)
# ===========================================================================


class TestSmallBatch:
    def test_returns_a_valid_gamesimsummary(self):
        res = _runner().run(_spec(), n_iterations=8, base_seed=42)
        s = res.summary
        assert isinstance(s, GameSimSummary)
        assert s.n_iterations == 8
        assert res.n_iterations == 8
        # Win rates partition into [0, 1] and sum to 1.0.
        assert abs(s.home_win_pct + s.away_win_pct + s.tie_pct - 1.0) < 1e-9
        # RAW per-iteration arrays preserved at length N.
        assert s.home_scores.shape == (8,)
        assert s.away_scores.shape == (8,)
        assert s.total_scores.shape == (8,)
        # Scores are non-negative.
        assert (s.home_scores >= 0).all() and (s.away_scores >= 0).all()

    def test_workers_one_is_in_process_fallback(self):
        # max_workers=1 must work with no pool / no pickling (the test path).
        res = _runner(max_workers=1).run(_spec(), n_iterations=6, base_seed=1)
        assert res.max_workers == 1
        assert res.summary.n_iterations == 6


# ===========================================================================
# Determinism -- a fixed base seed reproduces the summary  (AC #2)
# ===========================================================================


class TestDeterminism:
    def test_fixed_base_seed_reproduces_summary(self):
        a = _runner().run(_spec(), n_iterations=8, base_seed=123, use_cache=False)
        b = _runner().run(_spec(), n_iterations=8, base_seed=123, use_cache=False)
        # Identical raw per-iteration arrays -> identical aggregate.
        assert np.array_equal(a.summary.home_scores, b.summary.home_scores)
        assert np.array_equal(a.summary.away_scores, b.summary.away_scores)
        assert a.summary.home_win_pct == b.summary.home_win_pct
        assert a.summary.total_score_mean == b.summary.total_score_mean

    def test_different_base_seed_changes_summary(self):
        a = _runner().run(_spec(), n_iterations=8, base_seed=1, use_cache=False)
        b = _runner().run(_spec(), n_iterations=8, base_seed=999, use_cache=False)
        # Overwhelmingly likely to differ in at least one game's score.
        assert not np.array_equal(a.summary.home_scores, b.summary.home_scores) \
            or not np.array_equal(a.summary.away_scores, b.summary.away_scores)


# ===========================================================================
# Variation -- distinct per-iteration seeds produce different games  (AC #2)
# ===========================================================================


class TestVariation:
    def test_per_iteration_seeds_produce_variation(self):
        res = _runner().run(_spec(), n_iterations=12, base_seed=7, use_cache=False)
        s = res.summary
        # With distinct per-game seeds the games are not all identical: the raw
        # score arrays must show more than one distinct (home, away) pairing.
        pairs = set(zip(s.home_scores.tolist(), s.away_scores.tolist()))
        assert len(pairs) > 1, "all iterations produced the identical game"


# ===========================================================================
# Redis-optional TTL cache -- memoized summary on the 2nd call  (AC #4)
# ===========================================================================


class TestCache:
    def test_second_call_returns_memoized_summary(self):
        cache = InMemoryCache()
        runner = BatchRunner(max_workers=1, cache=cache)
        spec = _spec()
        first = runner.run(spec, n_iterations=6, base_seed=55)
        second = runner.run(spec, n_iterations=6, base_seed=55)
        assert first.from_cache is False
        assert second.from_cache is True
        # The memoized object IS the cached summary (same identity).
        assert second.summary is first.summary

    def test_distinct_key_per_seed_and_n(self):
        cache = InMemoryCache()
        runner = BatchRunner(max_workers=1, cache=cache)
        spec = _spec()
        runner.run(spec, n_iterations=6, base_seed=1)
        # Different base seed -> a miss (distinct key), not the prior summary.
        other = runner.run(spec, n_iterations=6, base_seed=2)
        assert other.from_cache is False
        # Different N -> also a distinct key.
        diff_n = runner.run(spec, n_iterations=7, base_seed=1)
        assert diff_n.from_cache is False

    def test_use_cache_false_never_memoizes(self):
        cache = InMemoryCache()
        runner = BatchRunner(max_workers=1, cache=cache)
        spec = _spec()
        runner.run(spec, n_iterations=4, base_seed=9, use_cache=False)
        # Nothing stored -> a cached run still recomputes (miss).
        again = runner.run(spec, n_iterations=4, base_seed=9, use_cache=True)
        assert again.from_cache is False

    def test_inmemory_cache_honors_ttl(self):
        clock = {"t": 0.0}
        cache = InMemoryCache(clock=lambda: clock["t"])
        cache.set("k", "v", ttl_s=60)
        assert cache.get("k") == "v"
        clock["t"] = 61.0  # past the 60s TTL.
        assert cache.get("k") is None

    def test_make_cache_no_redis_falls_back_in_memory(self):
        # prefer_redis=False MUST never need a server (the sandbox has none).
        c = make_cache(prefer_redis=False)
        assert isinstance(c, InMemoryCache)

    def test_make_cache_redis_unreachable_falls_back(self):
        # A client whose ping() raises -> graceful in-memory fallback, no raise.
        class _DeadClient:
            def ping(self):
                raise ConnectionError("no server")

        c = make_cache(prefer_redis=True, client=_DeadClient())
        assert isinstance(c, InMemoryCache)

    def test_null_cache_is_a_noop(self):
        c = NullCache()
        c.set("k", "v", ttl_s=60)
        assert c.get("k") is None

    def test_ttl_constants_match_spec(self):
        assert SIM_RESULT_TTL_S == 60
        assert POOL_QUERY_TTL_S == 300


# ===========================================================================
# GameSpec -- picklable + a stable cache key (the cross-process contract)
# ===========================================================================


class TestGameSpecPicklable:
    def test_spec_is_picklable(self):
        import pickle

        spec = _spec()
        round_tripped = pickle.loads(pickle.dumps(spec))
        assert round_tripped.machine_factory == spec.machine_factory
        assert round_tripped.sim_kwargs == spec.sim_kwargs

    def test_cache_key_is_order_independent(self):
        a = GameSpec(machine_factory=FACTORY, sim_kwargs={"x": 1, "y": [1, 2]})
        b = GameSpec(machine_factory=FACTORY, sim_kwargs={"y": [1, 2], "x": 1})
        assert a.cache_key_fields() == b.cache_key_fields()


# ===========================================================================
# Heavy / real-parallel runs (NOT in the always-on suite)
# ===========================================================================


@pytest.mark.slow
class TestSlowParallel:
    def test_full_100_game_batch_across_processes(self):
        # The real SIM-281 path: 100 games across a process pool. Slow + forks.
        runner = BatchRunner(max_workers=4, cache=NullCache())
        res = runner.run(_spec(), n_iterations=100, base_seed=2024)
        assert res.summary.n_iterations == 100
        assert res.max_workers == 4

    def test_pooled_matches_in_process_for_same_base_seed(self):
        # Determinism must hold across the process boundary too: pooled (workers>1)
        # and in-process (workers=1) give the same per-iteration scores.
        spec = _spec()
        seq = BatchRunner(max_workers=1, cache=NullCache()).run(
            spec, n_iterations=8, base_seed=321
        )
        par = BatchRunner(max_workers=4, cache=NullCache()).run(
            spec, n_iterations=8, base_seed=321
        )
        assert np.array_equal(seq.summary.home_scores, par.summary.home_scores)
        assert np.array_equal(seq.summary.away_scores, par.summary.away_scores)
