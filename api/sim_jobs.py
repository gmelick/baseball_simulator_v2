"""One simulation run per game: the job registry (SIM-519 Part E).

Before this module, four endpoints each ran their own hidden batch when a panel
asked, nothing was stored on the default stack, a reload lost the pending
state, and a second click started a second batch. A **run** is now a row in
``sim.sim_runs`` with a state:

    queued -> running -> done | failed | cancelled

* **One run per key.** The key is the SHA-256 of the runner's cache key (the
  game spec, the seed, N). A request whose key has a queued or running row gets
  that row back (``existing = True``). A partial unique index (Alembic 0029)
  holds the rule across processes.
* **A queue.** ``SIM_JOB_MAX_CONCURRENT`` jobs (default 1) run on the shared
  runner; the host is core-bound, so one at a time finishes the first sooner.
  The others wait as ``queued`` with their position.
* **Progress and cancel are real.** The runner plays the games in chunks
  (:meth:`simulation.batch_runner.BatchRunner.run_job`); after each chunk the
  job writes ``progress_done`` and checks the cancel flag. A cancelled run keeps
  what it played.
* **Durable results.** A finished run stores its summary, every player's prop
  distributions and the per-game inning grids on the row, and links its
  representative game in the replay file (``replay_run_id``, SIM-561).
* **Restart.** The registry is in-process; the rows outlive it. At boot a
  queued or running row is marked ``failed`` with the error ``restart``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
TERMINAL = ("done", "failed", "cancelled")

_RUN_COLUMNS = """
    run_id, game_pk, status, n_iterations, progress_done, base_seed, summary,
    requested_at, started_at, finished_at, error, lineup_source, bullpen_source,
    replay_run_id, created_at, kind, start_at_bat
"""

_SQL_INSERT = f"""
    INSERT INTO sim.sim_runs (
        game_pk, n_iterations, base_seed, summary, status, requested_at,
        spec_key, requested_by, lineup_source, bullpen_source,
        kind, start_at_bat, changes
    ) VALUES ($1, $2, $3, NULL, 'queued', NOW(), $4, $5, $6, $7, $8, $9, $10::jsonb)
    RETURNING {_RUN_COLUMNS}
"""
_SQL_ACTIVE_BY_KEY = f"""
    SELECT {_RUN_COLUMNS} FROM sim.sim_runs
     WHERE spec_key = $1 AND status IN ('queued', 'running')
     ORDER BY created_at DESC LIMIT 1
"""
_SQL_BY_ID = f"SELECT {_RUN_COLUMNS} FROM sim.sim_runs WHERE run_id = $1 AND game_pk = $2"
_SQL_LATEST = f"""
    SELECT {_RUN_COLUMNS} FROM sim.sim_runs WHERE game_pk = $1 AND kind = 'pregame'
     ORDER BY created_at DESC, run_id DESC LIMIT 1
"""
_SQL_LATEST_DONE_PROPS = """
    SELECT run_id, prop_set, n_iterations, base_seed FROM sim.sim_runs
     WHERE game_pk = $1 AND status = 'done' AND prop_set IS NOT NULL AND kind = 'pregame'
     ORDER BY created_at DESC, run_id DESC LIMIT 1
"""
_SQL_PROPS_BY_ID = """
    SELECT run_id, prop_set, n_iterations, base_seed FROM sim.sim_runs
     WHERE run_id = $1 AND game_pk = $2
"""
_SQL_RUNNING = "UPDATE sim.sim_runs SET status = 'running', started_at = NOW() WHERE run_id = $1"
#: A progress write lands after the next one or after the finish only when it
#: would not move the bar backwards.
_SQL_PROGRESS = """
    UPDATE sim.sim_runs SET progress_done = $2
     WHERE run_id = $1 AND status = 'running' AND progress_done < $2
"""
_SQL_FINISH = """
    UPDATE sim.sim_runs SET
        status = $2, progress_done = $3, summary = $4::jsonb, prop_set = $5::jsonb,
        inning_grids = $6::jsonb, replay_run_id = $7, finished_at = NOW(), error = $8
     WHERE run_id = $1
"""
_SQL_FAIL = """
    UPDATE sim.sim_runs SET status = $2, error = $3, finished_at = NOW()
     WHERE run_id = $1 AND status IN ('queued', 'running')
"""
_SQL_RECOVER = """
    UPDATE sim.sim_runs SET status = 'failed', error = 'restart', finished_at = NOW()
     WHERE status IN ('queued', 'running')
"""


def max_concurrent() -> int:
    """``SIM_JOB_MAX_CONCURRENT`` (default 1, decision D5)."""
    try:
        return max(1, int(os.environ.get("SIM_JOB_MAX_CONCURRENT", "1")))
    except ValueError:
        return 1


def spec_key(runner_cache_key: str) -> str:
    """The run's dedup key: the SHA-256 of the runner's cache key."""
    return hashlib.sha256(runner_cache_key.encode("utf-8")).hexdigest()


def row_dict(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A ``sim.sim_runs`` row as a plain dict, the JSON summary decoded."""
    if row is None:
        return None
    d = dict(row)
    summary = d.get("summary")
    if isinstance(summary, str | bytes | bytearray):
        d["summary"] = json.loads(summary)
    return d


def _json(value: Any) -> str | None:
    if value is None:
        return None
    from api.serialization import to_jsonable

    return json.dumps(to_jsonable(value))


#: The representative game's recorder: ``(base_seed) -> replay_run_id | None``.
RecordGame = Callable[[int | None], Awaitable[int | None]]


@dataclass
class _Job:
    run_id: int
    game_pk: int
    spec: Any
    n_iterations: int
    base_seed: int | None
    runner: Any
    record_game: RecordGame | None


class SimJobRegistry:
    """The app's run jobs (``app.state.sim_jobs``)."""

    def __init__(self, pool: Any, *, concurrency: int | None = None) -> None:
        self._pool = pool
        self._sem = asyncio.Semaphore(concurrency or max_concurrent())
        self._queue: list[int] = []
        self._cancel: set[int] = set()
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ reads

    async def _fetchrow(self, sql: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _execute(self, sql: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def _read(self, sql: str, *args: Any) -> Any:
        """A read that answers None before Alembic 0029 (no job columns yet)."""
        try:
            return await self._fetchrow(sql, *args)
        except Exception as exc:
            if "does not exist" in str(exc):
                return None
            raise

    async def get(self, game_pk: int, run_id: int) -> dict[str, Any] | None:
        return self._with_position(
            row_dict(await self._read(_SQL_BY_ID, int(run_id), int(game_pk)))
        )

    async def latest(self, game_pk: int) -> dict[str, Any] | None:
        return self._with_position(row_dict(await self._read(_SQL_LATEST, int(game_pk))))

    async def prop_set_row(self, game_pk: int, run_id: int | None) -> dict[str, Any] | None:
        """The prop set of a run (``run_id``) or of the game's newest done run."""
        row = (
            await self._read(_SQL_PROPS_BY_ID, int(run_id), int(game_pk))
            if run_id is not None
            else await self._read(_SQL_LATEST_DONE_PROPS, int(game_pk))
        )
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("prop_set"), str | bytes | bytearray):
            d["prop_set"] = json.loads(d["prop_set"])
        return d

    def position(self, run_id: int) -> int | None:
        """1 = next to run; None when the run is not waiting."""
        try:
            return self._queue.index(int(run_id)) + 1
        except ValueError:
            return None

    def _with_position(self, d: dict[str, Any] | None) -> dict[str, Any] | None:
        if d is not None:
            d["position_in_queue"] = (
                self.position(d["run_id"]) if d.get("status") == "queued" else None
            )
        return d

    # ------------------------------------------------------------------ writes

    async def recover_orphans(self) -> int:
        """Mark the queued and running rows of a previous process ``failed('restart')``."""
        try:
            result = await self._execute(_SQL_RECOVER)
        except Exception as exc:  # noqa: BLE001 -- before Alembic 0029 the columns are absent
            log.warning("sim jobs: orphan recovery skipped: %s", exc)
            return 0
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    async def submit(
        self,
        *,
        game_pk: int,
        spec: Any,
        runner: Any,
        n_iterations: int,
        base_seed: int | None,
        lineup_source: str | None = None,
        bullpen_source: str | None = None,
        requested_by: str | None = None,
        record_game: RecordGame | None = None,
        kind: str = "pregame",
        start_at_bat: int | None = None,
        changes: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a run, or return the active run with the same key.

        ``kind`` is ``pregame`` (the game's simulation from first pitch) or
        ``whatif_base`` / ``whatif_change`` (the game page's what-if pair, from
        plate appearance ``start_at_bat``). The game's "latest run" reads only
        see ``pregame`` runs. Returns ``(run, existing)``.
        """
        key = spec_key(f"{kind}:" + runner._cache_key(spec, base_seed, int(n_iterations)))
        held = await self._fetchrow(_SQL_ACTIVE_BY_KEY, key)
        if held is not None:
            return self._with_position(row_dict(held)) or {}, True
        try:
            row = await self._fetchrow(
                _SQL_INSERT,
                int(game_pk),
                int(n_iterations),
                base_seed,
                key,
                requested_by,
                lineup_source,
                bullpen_source,
                kind,
                start_at_bat,
                json.dumps(changes) if changes is not None else None,
            )
        except Exception as exc:  # the unique index: another process queued it first
            if "uq_sim_runs_active_spec" not in str(exc):
                raise
            held = await self._fetchrow(_SQL_ACTIVE_BY_KEY, key)
            return self._with_position(row_dict(held)) or {}, True
        run = row_dict(row) or {}
        job = _Job(
            int(run["run_id"]),
            int(game_pk),
            spec,
            int(n_iterations),
            base_seed,
            runner,
            record_game,
        )
        self._queue.append(job.run_id)
        self._tasks[job.run_id] = asyncio.create_task(self._run(job))
        return self._with_position(run) or run, False

    async def cancel(self, game_pk: int, run_id: int) -> dict[str, Any] | None:
        """Ask a run to stop. Returns the row, or None when the run is unknown.

        A queued run is cancelled at once; a running one stops after its
        current chunk. A finished run is returned unchanged (the route answers
        409)."""
        run = await self.get(game_pk, run_id)
        if run is None or run["status"] in TERMINAL:
            return run
        self._cancel.add(int(run_id))
        if run["status"] == "queued" and int(run_id) in self._queue:
            self._queue.remove(int(run_id))
            await self._execute(_SQL_FAIL, int(run_id), "cancelled", None)
            task = self._tasks.pop(int(run_id), None)
            if task is not None:
                task.cancel()
        return await self.get(game_pk, run_id)

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    # ------------------------------------------------------------------ the job

    async def _run(self, job: _Job) -> None:
        try:
            async with self._sem:
                if job.run_id in self._queue:
                    self._queue.remove(job.run_id)
                if job.run_id in self._cancel:
                    await self._execute(_SQL_FAIL, job.run_id, "cancelled", None)
                    return
                await self._execute(_SQL_RUNNING, job.run_id)
                await self._play(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the row says why
            log.warning("sim job %s failed: %s", job.run_id, exc)
            try:
                await self._execute(
                    _SQL_FAIL, job.run_id, "failed", f"{type(exc).__name__}: {exc}"[:500]
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._tasks.pop(job.run_id, None)
            self._cancel.discard(job.run_id)

    async def _play(self, job: _Job) -> None:
        loop = asyncio.get_running_loop()

        def on_progress(done: int, total: int) -> None:
            asyncio.run_coroutine_threadsafe(
                self._execute(_SQL_PROGRESS, job.run_id, int(done)), loop
            )

        outcome = await asyncio.to_thread(
            job.runner.run_job,
            job.spec,
            n_iterations=job.n_iterations,
            base_seed=job.base_seed,
            on_progress=on_progress,
            should_cancel=lambda: job.run_id in self._cancel,
        )
        replay_run_id = None
        if not outcome.cancelled and job.record_game is not None:
            replay_run_id = await job.record_game(job.base_seed)
        summary = outcome.summary
        grids = getattr(summary, "inning_grids", None) if summary is not None else None
        await self._execute(
            _SQL_FINISH,
            job.run_id,
            "cancelled" if outcome.cancelled else "done",
            int(outcome.n_done),
            _json(summary),
            json.dumps(outcome.prop_set.to_json()) if outcome.prop_set is not None else None,
            _json(grids),
            replay_run_id,
            None,
        )
