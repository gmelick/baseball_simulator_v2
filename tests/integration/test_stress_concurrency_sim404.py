"""
tests/integration/test_stress_concurrency_sim404.py
====================================================
SIM-404 — stress / concurrency / leak suite for the persistent BatchRunner and
the /simulate request path.

WHAT THIS COVERS
----------------
SIM-403 lifted the artificial ``SIM_RUNNER_WORKERS=1`` cap so the lifespan
BatchRunner spawns a real multi-worker ProcessPool. SIM-404 is the suite that
exercises that machinery under load and asserts the invariants a long-lived
production API needs:

1. **Warm-pool stability** — N sequential requests share the SAME warm
   ``ProcessPoolExecutor`` (SIM-360 AC). No re-fork per request.
2. **No worker / subprocess leak** — the live child count is stable across a
   batch of requests; the pool doesn't drift upward over many runs.
3. **No FD leak** — Python file descriptor count is stable across a batch.
4. **Concurrent /simulate requests** — N concurrent requests (each running a
   small Monte-Carlo batch) all return 200 with self-consistent summaries; no
   request is starved or returns stale data from another's cache slot.
5. **Direct BatchRunner concurrency** — N threads calling ``runner.run(...)``
   in parallel on the warm pool all succeed; the pool serialises gracefully
   without dropping work.
6. **Cache-key safety under contention** — concurrent requests with the SAME
   (matchup, seed, N) all return the same canonical summary (the cache is
   either coherent or a race produces equal results, never a corrupted one).

WHAT IT IS NOT
--------------
These are sandbox-runnable in-process tests using FastAPI's ``TestClient`` +
``ThreadPoolExecutor`` and the SIM-355 no-DB ``rng_driven_machine_factory``.
They do NOT require Postgres / DuckDB / FAISS / Redis. They are timing-aware
but NOT a perf gate (the 2 s / 30 s SLA lives in
``tests/performance/bench_api_simulate_sim372.py``). They CATCH leaks and
concurrency bugs that the existing per-request unit tests can't reach because
those tests run a single request through ``max_workers=1`` (in-process).

The pool sizes here are intentionally MODEST (2-4 workers, 30 logical
requests, small ``n_iterations``) so the suite is bounded; the real
production validation is the SIM-372 perf gate + the SIM-404 numbers under
``PERF_STRICT``-style load runs on target hardware.

The whole class is ``@pytest.mark.slow``: spawning a real ProcessPool +
running tens of full simulate requests across worker subprocesses puts these
well outside the fast unit lane's 30 s timeout. CI runs them in the
SIM-418 slow lane.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from simulation.batch_runner import BatchRunner, GameSpec, InMemoryCache
from simulation.game_state import GameState

# ---------------------------------------------------------------------------
# Knobs — intentionally modest so the suite is sandbox-bounded.
# ---------------------------------------------------------------------------

#: Number of concurrent /simulate requests for the concurrency test.
CONCURRENT_REQUESTS = 30

#: ``n_iterations`` per request — small to keep the wall time bounded; the
#: invariant we're checking (pool stability + correctness under contention)
#: doesn't need a full 100-iter batch per request.
PER_REQUEST_ITERATIONS = 4

#: Number of sequential requests for the warm-pool stability test.
SEQUENTIAL_REQUESTS = 30

#: ProcessPool size — keep modest so the sandbox doesn't fork an army.
POOL_WORKERS = 2

#: The no-DB picklable factory the BatchRunner uses across worker processes.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"

#: A stable game_pk for the test requests.
TEST_GAME_PK = 745001

#: Lineups + season — enough for ``simulate_game`` to run a real (no-DB) game.
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


# ---------------------------------------------------------------------------
# Fakes + app wiring (mirror tests/unit/test_api_games.py + bench_api_simulate)
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal asyncpg pool/connection stand-in (the /simulate path doesn't
    actually call fetch — we monkeypatch resolve_game_state — but the route's
    ``_get_pool`` must find SOMETHING on app.state so it doesn't 503)."""

    async def fetch(self, sql, *args):  # pragma: no cover — not hit
        return []

    async def fetchrow(self, sql, *args):  # pragma: no cover
        return None


def _small_game_state() -> GameState:
    """A minimal but valid GameState ``resolve_game_state`` returns — enough
    for the no-DB rng factory to play a full game."""
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = list(AWAY_LINEUP)
    state.home_lineup = list(HOME_LINEUP)
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = AWAY_LINEUP[0]
    state.throw_hand = "R"
    return state


def _spec() -> GameSpec:
    """A picklable GameSpec the BatchRunner can fan out across workers."""
    return GameSpec(
        machine_factory=NO_DB_FACTORY_REF,
        sim_kwargs={
            "away_lineup": list(AWAY_LINEUP),
            "home_lineup": list(HOME_LINEUP),
            "season": 2024,
            "pitcher_id": 600001,
            "bat_hand": "R",
        },
    )


def _build_app(runner: BatchRunner, cache: InMemoryCache | None = None) -> FastAPI:
    """Tiny FastAPI app with the real games router and a shared BatchRunner
    attached as ``app.state.sim_runner`` — the SIM-360 long-lived-runner path
    /simulate exercises in production."""
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = _FakePool()
    app.state.sim_cache = cache
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    app.state.sim_runner = runner
    return app


@pytest.fixture
def patch_resolver(monkeypatch):
    """Monkeypatch ``resolve_game_state`` (as imported into ``games_mod``) so
    every /simulate request resolves to a fixed small GameState — no live
    Postgres / DuckDB."""

    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)


# ---------------------------------------------------------------------------
# OS-resource helpers (FD count + child subprocess count)
# ---------------------------------------------------------------------------


def _open_fd_count() -> int | None:
    """The current process's open file-descriptor count on Linux/Mac, or None
    on platforms where ``/proc/self/fd`` is unavailable (e.g. Windows host).

    Used as a coarse leak indicator: count before vs. after a batch of
    requests should be ~equal (modulo small fluctuations from logging /
    timing). Returns None on Windows so the assertion is skipped there.
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None


def _child_process_count(executor) -> int:
    """How many worker subprocesses the ProcessPoolExecutor currently has.

    Reads the executor's internal ``_processes`` dict (a documented
    implementation detail of ``concurrent.futures.ProcessPoolExecutor``).
    Returns 0 if the dict isn't there (the executor isn't yet built).
    """
    procs = getattr(executor, "_processes", None)
    return len(procs) if procs is not None else 0


# ===========================================================================
# Test 1 — Warm-pool stability across N sequential requests
# ===========================================================================


@pytest.mark.slow
class TestWarmPoolStability:
    """N sequential /simulate requests share the same warm pool (SIM-360 AC).

    Asserts the persistent BatchRunner exposed at ``app.state.sim_runner``
    creates ONE ``ProcessPoolExecutor`` on first use and that no later request
    re-forks it — the production long-lived-API contract.
    """

    def test_pool_identity_stable_across_sequential_requests(self, patch_resolver):
        runner = BatchRunner(max_workers=POOL_WORKERS, cache=InMemoryCache())
        try:
            app = _build_app(runner)
            with TestClient(app) as client:
                # First request creates the pool.
                resp = client.get(
                    f"/api/games/{TEST_GAME_PK}/simulate",
                    params={
                        "n_iterations": PER_REQUEST_ITERATIONS,
                        "base_seed": 0,
                        "use_cache": "false",
                    },
                )
                assert resp.status_code == 200, resp.text
                pool_after_first = runner._pool
                assert pool_after_first is not None
                workers_after_first = _child_process_count(pool_after_first)
                assert workers_after_first == POOL_WORKERS

                # Many subsequent requests must reuse the SAME executor.
                for i in range(1, SEQUENTIAL_REQUESTS):
                    r = client.get(
                        f"/api/games/{TEST_GAME_PK}/simulate",
                        params={
                            "n_iterations": PER_REQUEST_ITERATIONS,
                            "base_seed": i,
                            "use_cache": "false",
                        },
                    )
                    assert r.status_code == 200, r.text
                    assert runner._pool is pool_after_first, (
                        f"pool was re-forked between request 0 and request {i}"
                    )
                    # Child count must not drift upward (workers don't multiply).
                    assert _child_process_count(pool_after_first) == workers_after_first
        finally:
            runner.close()
            assert runner._pool is None


# ===========================================================================
# Test 2 — No FD / subprocess leak across many requests
# ===========================================================================


@pytest.mark.slow
class TestNoResourceLeak:
    """The persistent BatchRunner must not leak FDs or subprocesses across
    requests. We warm the pool with one request, snapshot the counts, run
    many more, and assert the deltas are bounded."""

    #: A coarse upper bound on FD fluctuation across the batch. The exact
    #: number is implementation-noisy (logging, GC, transient sockets); we
    #: just guarantee it's bounded — not silently growing per request.
    FD_DELTA_TOLERANCE = 16

    def test_fd_and_subprocess_count_are_stable(self, patch_resolver):
        runner = BatchRunner(max_workers=POOL_WORKERS, cache=InMemoryCache())
        try:
            app = _build_app(runner)
            with TestClient(app) as client:
                # Warm the pool with one request so the pool + segments exist.
                client.get(
                    f"/api/games/{TEST_GAME_PK}/simulate",
                    params={
                        "n_iterations": PER_REQUEST_ITERATIONS,
                        "base_seed": 0,
                        "use_cache": "false",
                    },
                )
                pool = runner._pool
                assert pool is not None
                baseline_workers = _child_process_count(pool)
                baseline_fds = _open_fd_count()

                # Hammer it with N more requests; nothing should drift up.
                for i in range(1, SEQUENTIAL_REQUESTS):
                    r = client.get(
                        f"/api/games/{TEST_GAME_PK}/simulate",
                        params={
                            "n_iterations": PER_REQUEST_ITERATIONS,
                            "base_seed": i,
                            "use_cache": "false",
                        },
                    )
                    assert r.status_code == 200

                final_workers = _child_process_count(pool)
                final_fds = _open_fd_count()

            # Subprocess count never grows: the pool is fixed-size by design.
            assert final_workers == baseline_workers, (
                f"subprocess count drifted: {baseline_workers} -> {final_workers}"
            )
            # FD count is stable within a small tolerance (or unobservable on
            # the host — Windows /proc isn't available).
            if baseline_fds is not None and final_fds is not None:
                delta = final_fds - baseline_fds
                assert abs(delta) <= self.FD_DELTA_TOLERANCE, (
                    f"FD count drifted by {delta} (baseline {baseline_fds} -> "
                    f"final {final_fds}); leak threshold {self.FD_DELTA_TOLERANCE}"
                )
        finally:
            runner.close()


# ===========================================================================
# Test 3 — Concurrent /simulate requests all serve coherent summaries
# ===========================================================================


@pytest.mark.slow
class TestConcurrentSimulateRequests:
    """N concurrent /simulate requests against the shared runner must all
    serve 200s with self-consistent SIM-327 summaries. No request is starved,
    no request returns truncated/null data, no race on the shared cache.
    """

    def test_all_concurrent_requests_return_self_consistent_summaries(self, patch_resolver):
        runner = BatchRunner(max_workers=POOL_WORKERS, cache=InMemoryCache())
        try:
            app = _build_app(runner)
            with TestClient(app) as client:

                def _do_request(seed: int) -> dict:
                    r = client.get(
                        f"/api/games/{TEST_GAME_PK}/simulate",
                        params={
                            "n_iterations": PER_REQUEST_ITERATIONS,
                            "base_seed": seed,
                            "use_cache": "false",
                        },
                    )
                    assert r.status_code == 200, r.text
                    return r.json()

                # Fan out CONCURRENT_REQUESTS thread workers. The FastAPI
                # TestClient is thread-safe for independent calls; the routes
                # offload the heavy run via asyncio.to_thread anyway.
                with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
                    futures = [ex.submit(_do_request, seed) for seed in range(CONCURRENT_REQUESTS)]
                    bodies = [f.result(timeout=120) for f in as_completed(futures)]

            assert len(bodies) == CONCURRENT_REQUESTS
            for body in bodies:
                # Self-consistency: every response must carry the requested
                # game_pk + iteration count + a well-formed summary.
                assert body["game_pk"] == TEST_GAME_PK
                assert body["n_iterations"] == PER_REQUEST_ITERATIONS
                summary = body["summary"]
                assert summary["n_iterations"] == PER_REQUEST_ITERATIONS
                pct_sum = summary["home_win_pct"] + summary["away_win_pct"] + summary["tie_pct"]
                assert abs(pct_sum - 1.0) < 1e-6, f"win-pct triple does not sum to 1.0: {summary}"
        finally:
            runner.close()


# ===========================================================================
# Test 4 — Direct BatchRunner concurrency (the runner.run() seam itself)
# ===========================================================================


@pytest.mark.slow
class TestDirectRunnerConcurrency:
    """N threads calling ``runner.run(spec, ...)`` against the same warm pool
    all succeed. This isolates the runner from the FastAPI / serialization
    overhead and catches races on the warm-pool / shared-mem / cache state
    that the request-path test could mask."""

    def test_thirty_parallel_runs_all_succeed(self):
        runner = BatchRunner(max_workers=POOL_WORKERS, cache=InMemoryCache())
        try:

            def _do_run(seed: int):
                return runner.run(
                    _spec(),
                    n_iterations=PER_REQUEST_ITERATIONS,
                    base_seed=seed,
                    use_cache=False,
                )

            with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
                futures = [ex.submit(_do_run, s) for s in range(CONCURRENT_REQUESTS)]
                results = [f.result(timeout=120) for f in as_completed(futures)]

            assert len(results) == CONCURRENT_REQUESTS
            for res in results:
                assert res.summary.n_iterations == PER_REQUEST_ITERATIONS
                # The win-pct triple must sum to 1.0 on every run.
                pct_sum = res.summary.home_win_pct + res.summary.away_win_pct + res.summary.tie_pct
                assert abs(pct_sum - 1.0) < 1e-9
        finally:
            runner.close()


# ===========================================================================
# Test 5 — Cache-key safety under contention
# ===========================================================================


@pytest.mark.slow
class TestCacheRaceSafety:
    """Concurrent requests with the SAME (matchup, seed, n_iterations) and
    ``use_cache=true`` must either all hit the cache for the same canonical
    summary, OR the first-in-flight produces and others race to the same key
    — either way every response carries the SAME summary stats (no torn /
    interleaved data)."""

    def test_concurrent_same_key_requests_return_equal_summaries(self, patch_resolver):
        runner = BatchRunner(max_workers=POOL_WORKERS, cache=InMemoryCache())
        try:
            app = _build_app(runner)
            with TestClient(app) as client:
                # Warm the cache + pool with one request at the canonical key.
                resp = client.get(
                    f"/api/games/{TEST_GAME_PK}/simulate",
                    params={
                        "n_iterations": PER_REQUEST_ITERATIONS,
                        "base_seed": 999,
                        "use_cache": "true",
                    },
                )
                assert resp.status_code == 200
                canonical = resp.json()["summary"]

                def _hit_same_key() -> dict:
                    r = client.get(
                        f"/api/games/{TEST_GAME_PK}/simulate",
                        params={
                            "n_iterations": PER_REQUEST_ITERATIONS,
                            "base_seed": 999,
                            "use_cache": "true",
                        },
                    )
                    assert r.status_code == 200, r.text
                    return r.json()["summary"]

                with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as ex:
                    futures = [ex.submit(_hit_same_key) for _ in range(CONCURRENT_REQUESTS)]
                    summaries = [f.result(timeout=60) for f in as_completed(futures)]

            # Every concurrent same-key response must match the canonical one
            # bit-for-bit on the deterministic fields (the seed makes the
            # batch deterministic; the cache (if hit) returns the same dict).
            for s in summaries:
                assert s["n_iterations"] == canonical["n_iterations"]
                assert s["home_win_pct"] == canonical["home_win_pct"]
                assert s["away_win_pct"] == canonical["away_win_pct"]
                assert s["tie_pct"] == canonical["tie_pct"]
                # Score arrays are the raw signal — they must match too.
                assert s["home_scores"] == canonical["home_scores"]
                assert s["away_scores"] == canonical["away_scores"]
        finally:
            runner.close()
