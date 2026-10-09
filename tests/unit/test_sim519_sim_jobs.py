"""SIM-519 Part E — one simulation run per game.

The runner's chunked job (progress, cancel) runs real games on the synthetic
bundle. The registry runs against an in-memory stand-in for ``sim.sim_runs``
that answers the module's own SQL constants.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
import api.sim_jobs as sj
from api.auth import require_auth
from simulation.batch_runner import BatchRunner, GameSpec, NullCache
from simulation.game_state import GameState
from simulation.prop_distributions import PropDistributionSet
from simulation.sim_kwargs import sim_kwargs_from_state

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


def _state() -> GameState:
    state = GameState(pitcher_id=401, bat_hand="R", season=2024)
    state.away_lineup = [101 + i for i in range(9)]
    state.home_lineup = [201 + i for i in range(9)]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    state.away_pitcher_id = 301
    state.home_pitcher_id = 401
    state.manager.bullpen_available = {0: [302, 303], 1: [402, 403]}
    return state


def _spec() -> GameSpec:
    return GameSpec(machine_factory=NO_DB_FACTORY_REF, sim_kwargs=sim_kwargs_from_state(_state()))


def _runner() -> BatchRunner:
    # Two "workers" in the chunk arithmetic (chunk = 4), one process in fact.
    r = BatchRunner(cache=NullCache(), max_workers=1)
    r.resolve_max_workers = lambda n: 2  # type: ignore[method-assign]
    r._execute = lambda spec, seeds, w: BatchRunner._execute(r, spec, seeds, 1)  # type: ignore[method-assign]
    return r


# ---------------------------------------------------------------------------
# The runner's job
# ---------------------------------------------------------------------------


def test_run_job_reports_progress_per_chunk_and_builds_both_outputs() -> None:
    seen: list[tuple[int, int]] = []
    out = _runner().run_job(
        _spec(), n_iterations=10, base_seed=7, on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(4, 10), (8, 10), (10, 10)]
    assert out.n_done == 10 and not out.cancelled
    assert out.summary is not None and out.summary.n_iterations == 10
    assert out.prop_set is not None and out.prop_set.n_iterations == 10


def test_run_job_cancels_between_chunks() -> None:
    calls = itertools.count()
    out = _runner().run_job(
        _spec(), n_iterations=12, base_seed=7, should_cancel=lambda: next(calls) >= 1
    )
    assert out.cancelled
    assert out.n_done == 8  # two chunks played, the third never started
    assert out.summary is not None and out.summary.n_iterations == 8


def test_run_job_matches_run_prop_set() -> None:
    a = _runner().run_job(_spec(), n_iterations=6, base_seed=3).prop_set
    b = BatchRunner(cache=NullCache(), max_workers=1).run_prop_set(
        _spec(), n_iterations=6, base_seed=3
    )
    assert a is not None
    assert a.to_json() == b.to_json()


def test_prop_set_json_round_trip() -> None:
    ps = _runner().run_job(_spec(), n_iterations=5, base_seed=1).prop_set
    assert ps is not None
    back = PropDistributionSet.from_json(json.loads(json.dumps(ps.to_json())))
    assert back.to_json() == ps.to_json()
    pid = ps.player_ids()[0]
    assert back.get(pid) is not None


# ---------------------------------------------------------------------------
# The registry, over an in-memory sim.sim_runs
# ---------------------------------------------------------------------------


class _Table:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.ids = itertools.count(1)
        self.progress_writes: list[int] = []

    def _active(self, key: str) -> dict | None:
        rows = [r for r in self.rows.values() if r["spec_key"] == key and r["status"] in sj.ACTIVE]
        return rows[-1] if rows else None


class _Conn:
    def __init__(self, t: _Table) -> None:
        self.t = t

    async def fetchrow(self, sql: str, *a: Any) -> dict | None:
        t = self.t
        if sql is sj._SQL_INSERT:
            if t._active(a[3]) is not None:
                raise RuntimeError(
                    'duplicate key value violates unique constraint "uq_sim_runs_active_spec"'
                )
            rid = next(t.ids)
            t.rows[rid] = {
                "run_id": rid,
                "game_pk": a[0],
                "n_iterations": a[1],
                "base_seed": a[2],
                "summary": None,
                "status": "queued",
                "requested_at": datetime.now(UTC),
                "spec_key": a[3],
                "requested_by": a[4],
                "lineup_source": a[5],
                "bullpen_source": a[6],
                "progress_done": 0,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "replay_run_id": None,
                "created_at": datetime.now(UTC),
                "prop_set": None,
            }
            return dict(t.rows[rid])
        if sql is sj._SQL_ACTIVE_BY_KEY:
            return t._active(a[0])
        if sql is sj._SQL_BY_ID:
            r = t.rows.get(a[0])
            return dict(r) if r and r["game_pk"] == a[1] else None
        if sql is sj._SQL_LATEST:
            rows = [r for r in t.rows.values() if r["game_pk"] == a[0]]
            return dict(rows[-1]) if rows else None
        if sql is sj._SQL_LATEST_DONE_PROPS:
            rows = [
                r
                for r in t.rows.values()
                if r["game_pk"] == a[0] and r["status"] == "done" and r["prop_set"]
            ]
            return dict(rows[-1]) if rows else None
        if sql is sj._SQL_PROPS_BY_ID:
            r = t.rows.get(a[0])
            return dict(r) if r and r["game_pk"] == a[1] else None
        raise AssertionError(sql)

    async def execute(self, sql: str, *a: Any) -> str:
        t = self.t
        if sql is sj._SQL_RUNNING:
            t.rows[a[0]].update(status="running", started_at=datetime.now(UTC))
        elif sql is sj._SQL_PROGRESS:
            r = t.rows[a[0]]
            if r["status"] == "running" and r["progress_done"] < a[1]:
                r["progress_done"] = a[1]
                t.progress_writes.append(a[1])
        elif sql is sj._SQL_FINISH:
            t.rows[a[0]].update(
                status=a[1],
                progress_done=a[2],
                summary=a[3],
                prop_set=a[4],
                inning_grids=a[5],
                replay_run_id=a[6],
                error=a[7],
                finished_at=datetime.now(UTC),
            )
        elif sql is sj._SQL_FAIL:
            r = t.rows[a[0]]
            if r["status"] in sj.ACTIVE:
                r.update(status=a[1], error=a[2], finished_at=datetime.now(UTC))
        elif sql is sj._SQL_RECOVER:
            n = 0
            for r in t.rows.values():
                if r["status"] in sj.ACTIVE:
                    r.update(status="failed", error="restart")
                    n += 1
            return f"UPDATE {n}"
        else:
            raise AssertionError(sql)
        return "UPDATE 1"


class _Pool:
    def __init__(self) -> None:
        self.t = _Table()

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self.t)


async def _wait_terminal(reg: sj.SimJobRegistry, pk: int, rid: int) -> dict:
    for _ in range(400):
        run = await reg.get(pk, rid)
        if run and run["status"] in sj.TERMINAL:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError("the run never finished")


def test_a_run_goes_queued_running_done_and_stores_its_outputs() -> None:
    async def scenario() -> tuple[dict, dict, _Pool]:
        pool = _Pool()
        reg = sj.SimJobRegistry(pool, concurrency=1)

        async def record(seed: int | None) -> int | None:
            return 55

        run, existing = await reg.submit(
            game_pk=1,
            spec=_spec(),
            runner=_runner(),
            n_iterations=6,
            base_seed=9,
            lineup_source="published",
            record_game=record,
        )
        done = await _wait_terminal(reg, 1, run["run_id"])
        return run, done, pool

    run, done, pool = asyncio.run(scenario())
    assert run["status"] == "queued"
    assert done["status"] == "done"
    assert done["progress_done"] == 6
    assert done["replay_run_id"] == 55
    assert done["lineup_source"] == "published"
    assert json.loads(pool.t.rows[run["run_id"]]["prop_set"])["n_iterations"] == 6
    assert done["summary"]["n_iterations"] == 6


def test_the_same_key_returns_the_active_run() -> None:
    async def scenario() -> tuple[dict, dict, bool]:
        reg = sj.SimJobRegistry(_Pool(), concurrency=1)
        slow = _runner()
        a, _ = await reg.submit(game_pk=2, spec=_spec(), runner=slow, n_iterations=8, base_seed=1)
        b, existing = await reg.submit(
            game_pk=2, spec=_spec(), runner=slow, n_iterations=8, base_seed=1
        )
        await _wait_terminal(reg, 2, a["run_id"])
        return a, b, existing

    a, b, existing = asyncio.run(scenario())
    assert existing is True
    assert a["run_id"] == b["run_id"]


def test_a_queued_run_has_a_position_and_cancels_at_once() -> None:
    async def scenario() -> tuple[int | None, dict, dict]:
        reg = sj.SimJobRegistry(_Pool(), concurrency=1)
        first, _ = await reg.submit(
            game_pk=3, spec=_spec(), runner=_runner(), n_iterations=8, base_seed=1
        )
        await asyncio.sleep(0.01)  # the first run leaves the queue and starts
        second, _ = await reg.submit(
            game_pk=3, spec=_spec(), runner=_runner(), n_iterations=8, base_seed=2
        )
        pos = second["position_in_queue"]
        cancelled = await reg.cancel(3, second["run_id"])
        done = await _wait_terminal(reg, 3, first["run_id"])
        return pos, cancelled or {}, done

    pos, cancelled, done = asyncio.run(scenario())
    assert pos == 1
    assert cancelled["status"] == "cancelled"
    assert done["status"] == "done"


def test_a_failing_run_records_its_error() -> None:
    class _Boom:
        def _cache_key(self, *a: Any) -> str:
            return "k"

        def run_job(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("worker died")

    async def scenario() -> dict:
        reg = sj.SimJobRegistry(_Pool(), concurrency=1)
        run, _ = await reg.submit(
            game_pk=4, spec=_spec(), runner=_Boom(), n_iterations=5, base_seed=None
        )
        return await _wait_terminal(reg, 4, run["run_id"])

    run = asyncio.run(scenario())
    assert run["status"] == "failed"
    assert "worker died" in run["error"]


def test_restart_recovery_marks_orphans_failed() -> None:
    pool = _Pool()
    pool.t.rows[1] = {"run_id": 1, "game_pk": 9, "status": "running", "spec_key": "x"}
    pool.t.rows[2] = {"run_id": 2, "game_pk": 9, "status": "done", "spec_key": "y"}
    n = asyncio.run(sj.SimJobRegistry(pool).recover_orphans())
    assert n == 1
    assert pool.t.rows[1]["status"] == "failed" and pool.t.rows[1]["error"] == "restart"
    assert pool.t.rows[2]["status"] == "done"


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.fixture
def app_and_pool(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _Pool]:
    pool = _Pool()

    async def fake_resolve(conn: Any, game_pk: int, **kw: Any) -> GameState:
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", fake_resolve)

    class _PgPool:
        async def fetchval(self, *a: Any) -> None:
            return None

        async def fetch(self, *a: Any) -> list:
            return []

        @asynccontextmanager
        async def acquire(self):
            yield self

        async def fetchrow(self, *a: Any) -> None:
            return None

    app = FastAPI()
    app.include_router(games_mod.router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.pg_pool = _PgPool()
    app.state.sim_cache = None
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    app.state.sim_jobs = sj.SimJobRegistry(pool, concurrency=1)
    return app, pool


def test_post_returns_202_then_the_run_finishes_and_the_panels_read_it(app_and_pool) -> None:
    app, pool = app_and_pool
    import time

    with TestClient(app) as client:
        resp = client.post("/api/games/745001/simulate", json={"n_iterations": 4, "base_seed": 5})
        assert resp.status_code == 202, resp.text
        run = resp.json()
        assert run["status"] in ("queued", "running")
        for _ in range(300):
            body = client.get(f"/api/games/745001/simulate/runs/{run['run_id']}").json()
            if body["status"] in sj.TERMINAL:
                break
            time.sleep(0.02)
        assert body["status"] == "done", body
        assert body["progress_done"] == 4
        assert (
            client.get("/api/games/745001/simulate/runs/latest").json()["run_id"] == run["run_id"]
        )
        card = client.get("/api/games/745001/boxscore?latest_run=true").json()
        assert card["n_iterations"] == 4
        assert client.delete(f"/api/games/745001/simulate/runs/{run['run_id']}").status_code == 409


def test_no_run_reads_404_with_a_hint(app_and_pool) -> None:
    app, _ = app_and_pool
    with TestClient(app) as client:
        resp = client.get("/api/games/1/simulate/runs/latest")
        assert resp.status_code == 404
        assert "POST /simulate" in resp.text
        assert client.get("/api/games/1/boxscore?latest_run=true").status_code == 404
