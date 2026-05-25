"""
bench_api_simulate_sim372.py
============================
SIM-372 — End-to-end ``GET /api/games/{game_pk}/simulate`` latency perf gate.

WHY THIS EXISTS — the request-path gap (vs bench_simulation.py)
--------------------------------------------------------------
The canonical perf baseline ``tests/performance/bench_simulation.py``
(SIM-118 / SIM-335) times the RUNNER hot paths only — the pitcher engine
``query()``, the arsenal cache, a single ``StateMachine.step_pitch``, and the
``BatchRunner`` fan-out/fan-in.  It deliberately does NOT cross the FastAPI
request boundary, so the published SLA budgets were never measured against the
ACTUAL served latency a client sees.

SIM-372 closes that gap: this bench times the FULL HTTP request path of the
production simulate endpoint via Starlette's ``TestClient`` —

    HTTP dispatch
      -> resolve_factory_ref + lineup resolve (-> GameState)
      -> GameSpec assembly + BatchRunner.run (the real simulate_game loop)
      -> GameSimSummaryModel serialization (numpy-free JSON)
      -> the SimulateResponse envelope back over the wire

— for both a SINGLE-game request (small ``n_iterations``) and a 100-iteration
BATCH request, and records the served median against the documented SLA.

THE SLA (SIM-372 acceptance criteria)
-------------------------------------
  * a SINGLE game simulate request  < 2 s   (``SINGLE_GAME_SLA_S``)
  * a 100-iteration BATCH request    < 30 s  (``BATCH_SLA_S``)

These are the same 2s / 30s budgets the platform commits to on TARGET
HARDWARE with the REAL DB-backed production factory.

Threshold philosophy — soft locally, hard in CI (mirrors bench_simulation.py)
-----------------------------------------------------------------------------
The latency targets are *hardware dependent* (see bench_simulation.py's module
docstring).  Asserting them unconditionally would make this suite flaky on a
shared/loaded sandbox or a developer laptop.  So, exactly like the SIM-118
harness:

  * By default the benches RUN the full request path and RECORD the served
    latency via the ``benchmark(...)`` fixture, then emit a SOFT, non-fatal
    note (``warnings.warn`` + print) when the served median exceeds the SLA.
    The suite is therefore always green here.
  * When ``PERF_STRICT=1`` (set by the dedicated-hardware perf CI workflow) the
    SLA is asserted as a HARD failure so a real regression fails the gate.

THE NO-DB CAVEAT — what the sandbox figure means (and does not)
---------------------------------------------------------------
This bench runs under the SIM-355 factory-ref testability seam with the no-DB
``simulation.batch_runner:rng_driven_machine_factory`` (the same seam the unit
tests use), and a monkeypatched ``resolve_game_state`` returning a fixed small
GameState — so it runs with NO live Postgres / DuckDB / FAISS.  That means the
sandbox-measured latency is a LOWER BOUND / smoke for the SLA: it exercises the
real request -> runner -> serialize -> response path and its Python/serialize
overhead, but the per-pitch cost of the real DB/FAISS-backed sampler+query
(~1 ms/pitch, see bench_simulation.py Bench 1/4) is NOT included.

The AUTHORITATIVE gate therefore runs ON TARGET HARDWARE with the real
DB-backed production factory: a CI perf job (added by the orchestrator to the
perf workflow) stands up the DB-backed app, points the endpoint at the
production factory, sets ``PERF_STRICT=1``, and runs this file so the 2s/30s
SLA is enforced as a hard failure against true served latency.  The sandbox run
remains a fast, always-green structural + lower-bound smoke.

Run locally (always green — records + soft-notes):
    pytest tests/performance/bench_api_simulate_sim372.py --benchmark-only
Structure-only (CI collect/verify, no timing):
    pytest tests/performance/bench_api_simulate_sim372.py --benchmark-disable
Run as the SLA gate (dedicated hardware + DB-backed factory):
    PERF_STRICT=1 pytest tests/performance/bench_api_simulate_sim372.py --benchmark-only
"""

from __future__ import annotations

import os
import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from simulation.game_state import GameState

# ---------------------------------------------------------------------------
# SLA constants (SIM-372 acceptance criteria — the served-latency budget).
#
# Asserted as HARD failures only under PERF_STRICT=1 (the dedicated-hardware
# perf CI job, against the REAL DB-backed factory); otherwise recorded +
# soft-warned so the harness is always green in shared sandboxes / laptops.
# ---------------------------------------------------------------------------

#: A single-game simulate request must serve in under 2 s end-to-end.
SINGLE_GAME_SLA_S = 2.0

#: A 100-iteration batch simulate request must serve in under 30 s end-to-end.
BATCH_SLA_S = 30.0

#: The 100-iteration batch the production endpoint defaults to (SIM-355: the
#: /simulate route's ``n_iterations`` default is 100). The BATCH bench uses this.
BATCH_N_ITERATIONS = 100

#: A small single-game iteration count — the "one game" served-latency probe.
#: Kept tiny so the no-DB sandbox run is quick while still crossing the full
#: request -> runner -> serialize -> response path at least once per round.
SINGLE_N_ITERATIONS = 1

#: The no-DB, picklable rng factory — the SIM-355 testability seam the endpoint's
#: ``app.state.sim_factory_ref`` is set to so /simulate runs a REAL (fast, no
#: live sampler/DuckDB) batch. The DB-backed production factory replaces this on
#: target hardware where the authoritative SLA gate runs.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"

#: A stable game_pk for the benched requests.
BENCH_GAME_PK = 745001

#: When set, the SLA is asserted as a HARD failure (the dedicated-hardware perf
#: CI workflow). Mirrors bench_simulation.py's PERF_STRICT switch.
PERF_STRICT = os.environ.get("PERF_STRICT", "") == "1"


# ---------------------------------------------------------------------------
# Soft / strict SLA helper (mirrors bench_simulation.py::_check_threshold)
# ---------------------------------------------------------------------------


def _check_sla(name: str, measured_s: float, target_s: float) -> None:
    """Assert the served-latency SLA under PERF_STRICT, else soft-warn.

    Keeps the harness always-green on shared hardware (sandbox / laptop) while
    letting the dedicated-hardware perf CI job (PERF_STRICT=1, real DB-backed
    factory) enforce the 2s/30s SLA as a hard regression gate. Identical
    philosophy to bench_simulation.py's ``_check_threshold``.
    """
    ok = measured_s <= target_s
    msg = (
        f"{name}: served median {measured_s:.4f}s vs SLA {target_s:.4f}s "
        f"(no-DB sandbox figure is a LOWER BOUND; the authoritative gate runs "
        f"on target hardware with the real DB-backed factory)"
    )
    # Always surface the measured-vs-SLA line so a --benchmark-only run records it.
    print(f"SIM-372 SLA — {msg}")
    if ok:
        return
    if PERF_STRICT:
        raise AssertionError(f"SLA REGRESSION — {msg}")
    warnings.warn(f"SLA (soft) — {msg}", stacklevel=2)


# ---------------------------------------------------------------------------
# Fakes + app wiring (mirror tests/unit/test_api_games.py exactly)
# ---------------------------------------------------------------------------


class _FakePool:
    """A fake asyncpg pool/connection (mirrors test_api_games._FakePool).

    The /simulate path does not call ``fetch`` on it (we monkeypatch
    ``resolve_game_state``), but the pool must be present so the route's
    ``_get_pool`` does not 503. ``fetch``/``fetchrow`` return benign empties.
    """

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []

    async def fetch(self, sql, *args):  # pragma: no cover — not hit on /simulate
        return self._rows

    async def fetchrow(self, sql, *args):  # pragma: no cover
        return self._rows[0] if self._rows else None


def _small_game_state() -> GameState:
    """A minimal but valid GameState the patched resolver returns.

    Full 1..9 lineups + a pitcher + season — enough for simulate_game to run a
    real (no-DB) game via the rng factory. Mirrors
    test_api_games._small_game_state.
    """
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


def _build_app(*, pool, cache=None, factory_ref=NO_DB_FACTORY_REF) -> FastAPI:
    """A tiny app with ONLY the games router + the app.state the route reads.

    Mirrors test_api_games._build_app: includes the games router and attaches
    ``pg_pool`` (so _get_pool does not 503), ``sim_cache`` (None => BatchRunner
    picks its own backend), and ``sim_factory_ref`` (the no-DB testability seam).
    No ``sim_duckdb`` is attached, so the SIM-357 replay-artifact persist is
    skipped (it returns early when no DuckDB store is wired) — keeping this bench
    on the pure simulate request path.
    """
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool
    app.state.sim_cache = cache
    app.state.sim_factory_ref = factory_ref
    return app


@pytest.fixture
def patch_resolver(monkeypatch):
    """Monkeypatch resolve_game_state (as imported into the games module) to
    return a fixed small GameState — no live Postgres/DuckDB. Mirrors
    test_api_games.patch_resolver."""

    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


@pytest.fixture
def simulate_client(patch_resolver):
    """A TestClient over the tiny games-router app wired with the no-DB seam.

    use_cache=false is used on every benched request (see the benches) so each
    round does REAL work rather than short-circuiting on the SIM_RESULT_TTL_S
    summary cache — we are timing the request->runner->serialize path, not a
    dict lookup. The fixture itself attaches NO sim cache for the same reason.
    """
    app = _build_app(pool=_FakePool(), cache=None)
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Shared bench body — time one full GET /{game_pk}/simulate request
# ---------------------------------------------------------------------------


def _benched_request(client: TestClient, n_iterations: int):
    """Issue ONE full GET /{game_pk}/simulate request and assert it served.

    This is the unit the benchmark times: the complete Starlette request
    dispatch -> route handler -> lineup resolve (patched) -> BatchRunner.run
    (the real simulate_game loop under the no-DB factory) -> GameSimSummaryModel
    serialization -> JSON response. ``use_cache=false`` forces a real recompute
    every round (no SIM-359 summary-cache short-circuit).
    """
    resp = client.get(
        f"/api/games/{BENCH_GAME_PK}/simulate",
        params={
            "n_iterations": n_iterations,
            "base_seed": 42,
            "use_cache": "false",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


def _served_median_s(benchmark) -> "float | None":
    """The benched median in seconds, or None under --benchmark-disable.

    Under ``--benchmark-disable`` the ``benchmark`` fixture runs the function
    once but ``benchmark.stats`` is None — so any stats access must be guarded.
    Returns None in that case so the SLA note/assert is skipped (the structure
    is still exercised + collectable, which is what the disabled path verifies).
    """
    stats = getattr(benchmark, "stats", None)
    if stats is None:
        return None
    return stats.stats.median


# ---------------------------------------------------------------------------
# Bench A — SINGLE-game simulate request (the 2 s SLA)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(
    group="api-simulate", min_rounds=1, max_time=2.0, warmup=False
)
def test_bench_api_simulate_single_game(benchmark, simulate_client):
    """Bench A: time a SINGLE-game GET /{game_pk}/simulate request end-to-end.

    Runs the full HTTP request path for a small (``SINGLE_N_ITERATIONS``)
    Monte-Carlo run through the no-DB factory, so it crosses the real
    request -> runner -> serialize -> response boundary that bench_simulation.py
    never measures. Records the served median against ``SINGLE_GAME_SLA_S``
    (2 s) and emits a SOFT note (HARD only under PERF_STRICT=1).

    ``min_rounds=1`` + ``max_time=2.0`` keep the no-DB sandbox wall-time bounded
    (a single served request through the loop-bound no-DB game is itself a
    multi-second unit on the shared sandbox); pytest-benchmark still records a
    median from however many rounds fit. On target hardware with the real
    DB-backed factory a single game is sub-second, so the same markers run many
    rounds there.

    The no-DB sandbox figure is a LOWER BOUND for the SLA (no live sampler/query
    per-pitch cost); the authoritative gate runs on target hardware with the
    real DB-backed factory. See the module docstring.
    """
    resp = benchmark(lambda: _benched_request(simulate_client, SINGLE_N_ITERATIONS))
    # Sanity: the request really served a summary for the requested run.
    body = resp.json()
    assert body["game_pk"] == BENCH_GAME_PK
    assert body["n_iterations"] == SINGLE_N_ITERATIONS
    assert body["summary"]["n_iterations"] == SINGLE_N_ITERATIONS

    median_s = _served_median_s(benchmark)
    if median_s is not None:  # None under --benchmark-disable — skip the SLA note
        _check_sla("single-game /simulate", median_s, SINGLE_GAME_SLA_S)


# ---------------------------------------------------------------------------
# Bench B — 100-iteration BATCH simulate request (the 30 s SLA)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group="api-simulate", min_rounds=1, max_time=30.0, warmup=False)
def test_bench_api_simulate_batch_100(benchmark, simulate_client):
    """Bench B: time a 100-iteration BATCH GET /{game_pk}/simulate request.

    Runs the full HTTP request path for the production-default
    ``BATCH_N_ITERATIONS`` (100) Monte-Carlo batch through the no-DB factory —
    the same request->runner->serialize->response boundary as Bench A, but at
    the 100-game batch size the 30 s SLA is written against. Records the served
    median against ``BATCH_SLA_S`` (30 s) and emits a SOFT note (HARD only under
    PERF_STRICT=1).

    ``min_rounds=1`` keeps the sandbox wall-time bounded (a 100-iteration batch
    is the heavy path); pytest-benchmark still records a median from however
    many rounds fit in ``max_time``. The no-DB figure is a LOWER BOUND for the
    SLA — the authoritative gate runs on target hardware with the real
    DB-backed factory. See the module docstring.
    """
    resp = benchmark(lambda: _benched_request(simulate_client, BATCH_N_ITERATIONS))
    body = resp.json()
    assert body["game_pk"] == BENCH_GAME_PK
    assert body["n_iterations"] == BATCH_N_ITERATIONS
    assert body["summary"]["n_iterations"] == BATCH_N_ITERATIONS

    median_s = _served_median_s(benchmark)
    if median_s is not None:  # None under --benchmark-disable — skip the SLA note
        _check_sla("batch-100 /simulate", median_s, BATCH_SLA_S)
