"""Each game's replay and scoring, in one process or across a pool of worker
processes.

THE PER-GAME PIPELINE
---------------------
:func:`_score_one_game` is the whole unit of work, and both execution paths call
it: read the game's closing odds, resolve its lineup into a GameState, resolve
the venue park factor and the point-in-time cutoff onto the state, replay the game
``iterations`` times through the production factory, build the summary, the win
probability (through the calibration map) and the prop distributions, score the
markets, and attach the best bettable price to each record.

ACROSS-GAMES PARALLELISM
------------------------
A full season is feasible because the run fans out over GAMES, not over one
game's iterations. A game's cost is the per-plate-appearance full-pool scoring and
the host is bound by its cores, so one game cannot run much faster; several games
at once multiply the throughput. ``--workers N`` (default :data:`DEFAULT_WORKERS`)
maps each whole game onto a ``forkserver`` worker that runs it serially. Each
worker holds only its own full-pool sampler cache, and the parent loads no engine
artifacts, so the workers never inherit a large parent. ``--workers 1`` is the
serial in-process path: the debug mode and the reference for a byte-identical
check. Both paths give the same records for the same (game set, base seed,
iterations): each game is independent and seeded per iteration, so only the
completion order differs, and it changes no record.

A BAD GAME AGAINST A BROKEN PROCESS (SIM-454)
---------------------------------------------
A game with bad data (no ingested lineup, a degenerate box score) is skipped and
counted. A broken process (no event loop, no Postgres pool, no park-factor source,
a dead Postgres) stops the run with a named cause (:func:`_is_process_broken`,
:class:`WorkerInitError`). Booking it as one more skipped game would repeat on
every remaining game and turn a broken worker into a season of "unresolved" rows
that exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from simulation import flags
from simulation.game_market_distributions import SegmentRuns
from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap

from .grading import (
    SEGMENT_MARKET_TYPES,
    _fetch_final_score,
    _fetch_official_grid,
    _fetch_prop_ground_truth,
    score_game_accuracy,
    score_prop_accuracy,
    score_segment_market_accuracy,
)
from .pricing import _fetch_game_odds, _fetch_prop_odds, attach_best_prices
from .records import GROUND_TRUTH_EVENT_LABEL, GROUND_TRUTH_OFFICIAL, AccuracyRecord

log = logging.getLogger("accuracy_comparison")

#: No default password (SIM-443b): a blank value is resolved in the command
#: line's ``main`` through ``pipeline.db_dsn.require_dsn``, which names the missing
#: variable.
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
#: The fitted reliability curve the live API applies at boot (the default of
#: ``scripts/validate_props.py``).
DEFAULT_CALIBRATION_PATH = os.environ.get("CALIBRATION_REPORT_PATH", "/data/calibration.json")

#: The production machine factory (a dotted ref), the one the API serves with.
_FACTORY_REF = "simulation.production_factory:production_machine_factory"

#: The default worker count: six games at once. Each worker holds its own sampler
#: cache, so the count is a memory budget as well as a core budget.
DEFAULT_WORKERS = 6

#: The pool's start method, as in ``simulation.batch_runner``. ``forkserver`` forks
#: workers from a lean server, so they never inherit what the parent holds; each
#: worker loads its own bundle. Set by ``SIM_MP_START_METHOD``.
_MP_START_METHOD = flags.read_str("SIM_MP_START_METHOD").strip().lower()


def _pool_mp_context() -> Any:
    """The multiprocessing context of the across-games pool.

    ``forkserver`` by default; the platform default when the requested method is
    unavailable (a host without forkserver). The same rule as
    :func:`simulation.batch_runner._pool_mp_context`.
    """
    import multiprocessing

    try:
        if _MP_START_METHOD in multiprocessing.get_all_start_methods():
            return multiprocessing.get_context(_MP_START_METHOD)
    except Exception:  # noqa: BLE001 — any oddity → platform default
        pass
    return multiprocessing.get_context()


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def _replay_game(state, n_iter: int, base_seed: int | None) -> tuple[list, list]:
    """Replay one resolved game ``n_iter`` times. Returns the per-iteration results
    and the per-iteration inning grids ``(home_by_inning, away_by_inning)`` the
    segment markets are priced from.

    It is the ``record_game_plays`` seam of ``scripts/validate_props.py``: it
    builds the live machine from the production factory ref and the state's sim
    kwargs, and runs ``simulate_game`` at each derived seed
    (``derive_seed(base_seed, i)``), keeping each iteration's ``GameSimResult``
    (its ``.boxscore`` and the score). Both execution paths use it, so a game's
    sims are the same in the parent (``--workers 1``) and in a worker. It is sync
    and CPU-bound; the async caller runs it through ``asyncio.to_thread``.

    ``state`` must arrive with its venue park factor resolved:
    :func:`_score_one_game` resolves it, because the lookup is async and this body
    runs in a thread with no event loop. A state that skipped the lookup still
    carries ``UNRESOLVED_PARK_FACTOR``, and ``sim_kwargs_from_state`` raises on it
    instead of replaying park-blind (SIM-452).
    """
    from api.routes.games import _sim_kwargs_from_state
    from simulation.batch_runner import derive_seed
    from simulation.linescore import linescore_from_plays
    from simulation.play_recorder import record_game_plays

    sim_kwargs = _sim_kwargs_from_state(state)
    results = []
    grids = []
    for i in range(int(n_iter)):
        seed = derive_seed(base_seed, i)
        result, plays = record_game_plays(
            factory_ref=_FACTORY_REF, seed=seed, sim_kwargs=sim_kwargs
        )
        results.append(result)
        # The per-inning run cells (the light form; the plays are dropped).
        ls = linescore_from_plays(plays)
        grids.append((list(ls.home_by_inning), list(ls.away_by_inning)))
    return results, grids


# ---------------------------------------------------------------------------
# The per-game unit of work, shared by the serial and the parallel path
# ---------------------------------------------------------------------------


async def _score_one_game(
    pool: Any,
    game_pk: int,
    *,
    duck: Any,
    do_game: bool,
    do_props: bool,
    iterations: int,
    base_seed: int,
    calibration_map: Any = IDENTITY_CALIBRATION,
    benchmark_book: str | None = None,
) -> tuple[list[AccuracyRecord], str, float, str | None]:
    """Resolve, replay and score ONE game; return ``(accuracy_records, status,
    park_factor, ground_truth_source)``.

    The steps, the same in both execution paths:

      1. read the game's closing odds: the graded row of each market (the first
         book on the preference list, or ``benchmark_book``) and every
         sportsbook's closing row beside it;
      2. resolve the :class:`GameState` (lineup → state);
      3. resolve the venue park factor, the managers' profiles and the
         point-in-time cutoff onto the state;
      4. replay ``iterations`` sims through :func:`_replay_game` (seed
         ``derive_seed(base_seed, i)``, deterministic per game);
      5. build the :class:`GameSimSummary`, a :class:`WinProbability` through
         ``calibration_map``, and the :class:`PropDistributionSet` for props;
      6. score the game markets and the props, as ``do_game`` / ``do_props`` say;
      7. put the best bettable closing price on each record
         (:func:`attach_best_prices`). The scores do not change.

    ``status`` is ``"scored"``, ``"no_odds"``, ``"unresolved"`` or ``"empty"``, for
    the run counters. A game with bad data gives no records and a status other
    than ``"scored"``; it never raises out of here, EXCEPT an
    :class:`UnresolvedParkFactorError` (the code itself regressed and must not be
    tallied as one skipped game) or a process-level failure
    (:func:`_is_process_broken`).

    ``park_factor`` is the factor the game ran with; the caller counts the games
    that got a non-neutral one, so an operator sees whether the park nudge was
    live. ``ground_truth_source`` names the record that graded the game's props
    (:data:`GROUND_TRUTH_OFFICIAL` or :data:`GROUND_TRUTH_EVENT_LABEL`), or ``None``
    when the game scored no props. The only randomness is the per-iteration seed,
    so the records are the same wherever this runs.
    """
    from api.routes.games import _resolve_state_or_error
    from simulation.prop_distributions import PropDistributionSet
    from simulation.results import GameSimSummary
    from simulation.sim_kwargs import (
        resolve_asof_ymd,
        resolve_manager_profiles_onto_state,
        resolve_park_factor_onto_state,
    )
    from simulation.win_probability import win_probability

    # Read the odds first: a game with no odds rows is skipped as 'no_odds'.
    game_odds = (
        await _fetch_game_odds(pool, game_pk, benchmark_book=benchmark_book) if do_game else {}
    )
    prop_odds = (
        await _fetch_prop_odds(pool, game_pk, benchmark_book=benchmark_book) if do_props else {}
    )
    if not game_odds and not prop_odds:
        return [], "no_odds", 1.0, None

    try:
        state = await _resolve_state_or_error(pool, game_pk)
    except Exception as exc:  # noqa: BLE001 — skip games with UNRESOLVABLE DATA
        # A missing lineup is one bad game. A dead Postgres is every game, so it
        # must not be booked as one.
        if _is_process_broken(exc):
            log.error("game %s: process-level failure while resolving state", game_pk)
            raise
        log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
        return [], "unresolved", 1.0, None

    # The park factor resolves HERE, where the connections live. The replay runs
    # in a thread with no event loop, and the builder rejects an unresolved state,
    # so an edit that drops this line fails loudly instead of replaying park-blind.
    park_factor = await resolve_park_factor_onto_state(state, pool, duck, int(game_pk))
    # Each side's manager profile and the league means, the read the API's builder
    # makes (the steal weight's inputs; empty and neutral when the DuckDB is closed
    # or the manager is unknown).
    resolve_manager_profiles_onto_state(state, duck)
    # The point-in-time cutoff: the day before the game, so the replay never draws
    # on a pool row or a player profile dated after the game it predicts.
    # sim_kwargs_from_state reads state.asof_ymd and carries it into the simulator
    # as "_asof_ymd" (simulation/sim_kwargs.py).
    #
    # resolve_asof_ymd returns None for a live game (no cutoff needed) and on a
    # lookup failure (a database hiccup, or a game_pk not in raw.games). Every game
    # here is a completed one, so None means a failed lookup: the game is skipped.
    # A replay anyway would draw from the whole pool, this game's own real plays
    # included, the leak the cutoff exists to close.
    asof_ymd = await resolve_asof_ymd(pool, int(game_pk))
    if asof_ymd is None:
        log.info("skip game %s (point-in-time cutoff unresolved)", game_pk)
        return [], "unresolved", park_factor, None
    state.asof_ymd = asof_ymd

    results, grids = await asyncio.to_thread(_replay_game, state, iterations, base_seed)
    if not results:
        return [], "empty", park_factor, None

    summary = GameSimSummary.from_results(results)
    wp = win_probability(summary, calibration_map=calibration_map)

    accuracy: list[AccuracyRecord] = []
    pset = PropDistributionSet.from_results(results) if (do_props and prop_odds) else None

    if do_game and game_odds:
        final_score = await _fetch_final_score(pool, game_pk)
        if final_score is not None:
            home_score, away_score = final_score
            accuracy.extend(
                score_game_accuracy(game_pk, wp, summary, game_odds, home_score, away_score)
            )
        # The twelve segment and team markets, priced from the per-iteration
        # linescore and graded on the official inning grid.
        if grids and any(m in game_odds for m in SEGMENT_MARKET_TYPES):
            official_grid = await _fetch_official_grid(pool, game_pk)
            accuracy.extend(
                score_segment_market_accuracy(
                    game_pk, SegmentRuns.from_inning_grids(grids), game_odds, official_grid
                )
            )
    ground_truth_source: str | None = None
    if do_props and prop_odds and pset is not None:
        # The official box score when the game has rows, else the event-label
        # fallback. The source's own prop set bounds what is graded.
        truth = await _fetch_prop_ground_truth(pool, game_pk)
        ground_truth_source = truth.source
        accuracy.extend(
            score_prop_accuracy(
                game_pk,
                pset,
                prop_odds,
                truth.batter_actuals,
                truth.pitcher_actuals,
                scorable_props=truth.scorable_props,
            )
        )

    # The best price a bettor could take at each graded line.
    accuracy = attach_best_prices(accuracy, game_odds, prop_odds)
    return accuracy, "scored", park_factor, ground_truth_source


# ---------------------------------------------------------------------------
# The across-games pool: a picklable module-level worker and its one-time setup
# ---------------------------------------------------------------------------


class WorkerInitError(RuntimeError):
    """A worker process could not build the resources every game needs.

    This is not one bad game: the PROCESS is broken (no event loop, no Postgres
    pool or no sim DuckDB). Every game the worker is handed fails the same way, so
    :func:`_process_one_game` lets it out and :func:`_run_parallel` stops the run on
    it.
    """


def _is_process_broken(exc: BaseException) -> bool:
    """Whether ``exc`` means "this PROCESS is broken", not "this game is bad".

    The per-game handlers swallow bad DATA (a game with no ingested lineup, a
    degenerate box score) and must NOT swallow broken INFRASTRUCTURE: a swallowed
    infrastructure failure repeats on every remaining game, and the run books a
    season of "unresolved" rows, prints an empty table and exits 0. ``True`` here
    makes the caller re-raise, which stops the run with a non-zero exit and a named
    cause.
    """
    if isinstance(exc, WorkerInitError | ImportError | MemoryError):
        return True
    # A dead or unreachable Postgres, a closed pool, an exhausted connection limit.
    if isinstance(exc, OSError):
        return True
    try:
        import asyncpg
    except Exception:  # noqa: BLE001 — asyncpg absent → nothing more to classify
        return False
    # A server under load reports exhaustion and shutdown as SERVER errors, not as
    # connection errors, so `PostgresConnectionError` (class 08) alone misses them.
    # `InsufficientResourcesError` is class 53: too many connections (53300), out of
    # memory (53200). `OperatorInterventionError` is class 57: admin shutdown
    # (57P01), crash shutdown (57P02), cannot connect now (57P03). Each breaks every
    # remaining game, not this one.
    return isinstance(
        exc,
        asyncpg.InterfaceError
        | asyncpg.PostgresConnectionError
        | asyncpg.exceptions.InsufficientResourcesError
        | asyncpg.exceptions.OperatorInterventionError,
    )


#: The per-worker process globals, one set per forkserver worker. ``_WORKER_INITED``
#: guards the one-time setup; ``_WORKER_LOOP`` is the worker's own event loop and
#: ``_WORKER_POOL`` its one asyncpg pool. Every game the worker handles shares them.
_WORKER_INITED: bool = False
_WORKER_LOOP: Any = None
_WORKER_POOL: Any = None
_WORKER_DSN: str = DEFAULT_DSN
#: The worker's own read-only sim-DuckDB connection, for the park-factor lookup. A
#: DuckDB connection is not fork-safe, so each worker opens its own.
_WORKER_DUCK: Any = None
#: The worker's own win-probability calibration map, loaded once. A missing or
#: corrupt report falls back to IDENTITY_CALIBRATION, as at API boot, instead of
#: failing the worker.
_WORKER_CALIBRATION_MAP: Any = IDENTITY_CALIBRATION
#: The LATCHED setup failure. Once the setup fails, the worker is broken for good,
#: so the next call re-raises this instead of building a loop and a pool again
#: (each retry would leak one of each).
_WORKER_INIT_ERROR: BaseException | None = None


def _close_worker_resources(loop: Any, pool: Any, duck: Any) -> None:
    """Release whatever a failed setup built.

    The pool closes on the loop that created it, so the loop closes last. Every
    step is best-effort: a cleanup that raises would mask the setup failure the
    caller is about to report.
    """
    if pool is not None and loop is not None:
        try:
            loop.run_until_complete(pool.close())
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: pool.close() failed", exc_info=True)
    if duck is not None:
        try:
            duck.close()
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: duckdb.close() failed", exc_info=True)
    if loop is not None:
        try:
            asyncio.set_event_loop(None)
            loop.close()
        except Exception:  # noqa: BLE001 — best-effort release
            log.debug("worker init cleanup: loop.close() failed", exc_info=True)


def _worker_lazy_init(
    dsn: str,
    duckdb_path: str,
    *,
    allow_neutral_parks: bool = False,
    calibration_path: str | None = DEFAULT_CALIBRATION_PATH,
) -> None:
    """The one-time setup of a worker (on its first call; a no-op after).

    It builds what every game of the worker shares:

      * ``(a)`` the worker's full-pool sampler cache, warmed once through
        :func:`simulation.production_factory.warm_worker_cache`, so the first game
        is a warm-cache hit (best-effort: without artifacts the cache warms
        lazily);
      * ``(b)`` one asyncio loop and one asyncpg pool (each worker reads odds and
        resolves states on its own connection);
      * ``(c)`` one read-only sim-DuckDB connection, which the park-factor lookup
        reads. It is required unless the operator passed
        ``--allow-neutral-parks``, the escape hatch :func:`run` honours;
      * ``(d)`` the worker's own win-probability calibration map, best-effort: a
        missing or corrupt report falls back to :data:`IDENTITY_CALIBRATION`, as
        ``api/main.py`` does at boot.

    The setup is ATOMIC. It builds the loop, the pool and the DuckDB connection
    into locals and publishes them to the module globals only when all three
    succeed. Any failure releases everything it built and raises
    :class:`WorkerInitError`, so a failed setup leaks no loop and no pool.

    The failure is LATCHED: a worker that failed once cannot succeed later, so
    the next call re-raises at once and builds nothing.
    """
    global _WORKER_INITED, _WORKER_LOOP, _WORKER_POOL, _WORKER_DSN, _WORKER_DUCK
    global _WORKER_INIT_ERROR, _WORKER_CALIBRATION_MAP
    if _WORKER_INITED:
        return
    if _WORKER_INIT_ERROR is not None:
        # Latched: the worker is broken for good. Build nothing, leak nothing.
        raise _WORKER_INIT_ERROR

    loop: Any = None
    pool: Any = None
    duck: Any = None
    calib_map: Any = IDENTITY_CALIBRATION
    try:
        # (a) warm the full-pool sampler cache once for this worker (best-effort).
        try:
            from simulation.production_factory import warm_worker_cache

            warm_worker_cache(None)
        except Exception:  # noqa: BLE001 — full-pool off / no artifacts → lazy warm
            pass

        # (b) one event loop + one asyncpg pool, owned by this worker for its lifetime.
        import asyncpg

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        pool = loop.run_until_complete(asyncpg.create_pool(dsn, min_size=1, max_size=2))

        # (c) this worker's own read-only sim DuckDB.
        from simulation.sim_kwargs import open_sim_duckdb

        duck = open_sim_duckdb(duckdb_path)
        if duck is None and not allow_neutral_parks:
            raise WorkerInitError(
                f"SIM-452: worker could not open the sim DuckDB at {duckdb_path!r}, so every "
                "park factor would silently fall back to a neutral 1.0. Fix --duckdb, or "
                "pass --allow-neutral-parks to accept a park-neutral run."
            )

        # (d) this worker's own calibration map (best-effort, never fatal).
        try:
            from api.state import load_calibration_report

            report = load_calibration_report(calibration_path)
            calib_map = (
                CalibrationMap.from_report(report) if report is not None else (IDENTITY_CALIBRATION)
            )
        except Exception as exc:  # noqa: BLE001 — a bad report must not sink the worker
            log.warning(
                "SIM-538: worker could not load calibration from %r (%s) — using identity.",
                calibration_path,
                exc,
            )
            calib_map = IDENTITY_CALIBRATION
    except BaseException as exc:
        # Release EVERYTHING this attempt built, whatever went wrong. Building into
        # locals makes this the one place that owns the half-built state.
        _close_worker_resources(loop, pool, duck)
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        if isinstance(exc, WorkerInitError):
            _WORKER_INIT_ERROR = exc
            raise
        err = WorkerInitError(f"worker init failed: {type(exc).__name__}: {exc}")
        _WORKER_INIT_ERROR = err
        raise err from exc

    # Publish only now: either every resource exists, or none of them does.
    _WORKER_DSN = dsn
    _WORKER_LOOP = loop
    _WORKER_POOL = pool
    _WORKER_DUCK = duck
    _WORKER_CALIBRATION_MAP = calib_map
    _WORKER_INITED = True
    atexit.register(_worker_shutdown)


def _worker_shutdown() -> None:
    """Release the worker's loop, pool and DuckDB at process exit.

    Registered with :mod:`atexit` after a successful setup, so a worker hands its
    Postgres connections back when the pool shuts down instead of holding them
    until the OS reaps the process.
    """
    global _WORKER_INITED, _WORKER_LOOP, _WORKER_POOL, _WORKER_DUCK
    if not _WORKER_INITED:
        return
    _WORKER_INITED = False
    _close_worker_resources(_WORKER_LOOP, _WORKER_POOL, _WORKER_DUCK)
    _WORKER_LOOP = _WORKER_POOL = _WORKER_DUCK = None


def _process_one_game(game_pk: int, params: dict[str, Any]) -> dict[str, Any]:
    """The picklable across-games worker: score ONE game end to end.

    The function the ``ProcessPoolExecutor`` maps over the slate's games.
    Its first call in a worker runs :func:`_worker_lazy_init`; every call then
    drives :func:`_score_one_game` on the worker's own loop and pool.

    Returns a picklable payload ``{"status": str, "accuracy_records": list[dict],
    "park_run_factor": float, "ground_truth_source": str | None}``, each record
    as ``AccuracyRecord.to_jsonable()``. The parent turns the dicts back into
    records and folds ``status`` and ``ground_truth_source`` into the run
    counters. Plain dicts keep the process boundary independent of how the
    package is named in the worker. A failed game's payload carries no
    ``ground_truth_source``: it graded nothing.

    A game with BAD DATA logs and gives no records (status ``"unresolved"``); it
    never raises out, so one bad game never sinks the run. A BROKEN PROCESS
    propagates:

      * :class:`UnresolvedParkFactorError` says the code skipped the park-factor
        lookup: a defect in the script, not a bad game;
      * :class:`WorkerInitError` says the worker has no loop, no pool or no
        park-factor source, so every game it is handed fails the same way;
      * anything :func:`_is_process_broken` recognises (a dead Postgres, an
        exhausted connection limit, an ``ImportError``), for the same reason.
    """
    from simulation.sim_kwargs import UnresolvedParkFactorError

    dsn = str(params.get("dsn", DEFAULT_DSN))
    duckdb_path = str(params.get("duckdb", DEFAULT_DUCKDB_PATH))
    allow_neutral = bool(params.get("allow_neutral_parks", False))
    calibration_path = params.get("calibration_path", DEFAULT_CALIBRATION_PATH)
    try:
        _worker_lazy_init(
            dsn,
            duckdb_path,
            allow_neutral_parks=allow_neutral,
            calibration_path=calibration_path,
        )
        accuracy_records, status, park_factor, ground_truth_source = (
            _WORKER_LOOP.run_until_complete(
                _score_one_game(
                    _WORKER_POOL,
                    int(game_pk),
                    duck=_WORKER_DUCK,
                    do_game=bool(params["do_game"]),
                    do_props=bool(params["do_props"]),
                    iterations=int(params["iterations"]),
                    base_seed=int(params["base_seed"]),
                    calibration_map=_WORKER_CALIBRATION_MAP,
                    benchmark_book=params.get("benchmark_book"),
                )
            )
        )
        return {
            "status": status,
            "accuracy_records": [a.to_jsonable() for a in accuracy_records],
            "park_run_factor": float(park_factor),
            "ground_truth_source": ground_truth_source,
        }
    except UnresolvedParkFactorError:
        raise
    except Exception as exc:  # noqa: BLE001 — one BAD GAME never sinks the run
        if _is_process_broken(exc):
            # The worker is broken, not this game. Let it out so the run stops
            # with a named cause instead of booking every remaining game.
            log.error("worker: process-level failure on game %s — stopping the run", game_pk)
            raise
        log.warning("worker: game %s failed (%s) — no records", game_pk, type(exc).__name__)
        return {"status": "unresolved", "accuracy_records": [], "park_run_factor": 1.0}


# ---------------------------------------------------------------------------
# The two execution paths
# ---------------------------------------------------------------------------


@dataclass
class _Counters:
    """The running tallies of the run summary."""

    games_attempted: int = 0
    games_scored: int = 0
    games_no_odds: int = 0
    games_unresolved: int = 0
    #: The raw observations of the accuracy comparison.
    accuracy_records: list[AccuracyRecord] = field(default_factory=list)
    #: The scored games whose park factor the lookup moved off 1.0. A scored run
    #: with none means the park nudge did nothing all run, and the operator sees it.
    games_park_nonneutral: int = 0
    #: The scored games whose props were graded on the official box score
    #: (``raw.game_player_stats``) and on the event-label fallback
    #: (``raw.pitches.events``). The fallback sees five props, the official record
    #: every priced market. The two add up to the games that scored a prop.
    games_official_boxscore: int = 0
    games_event_label: int = 0


def _tally(
    counters: _Counters,
    status: str,
    park_factor: float = 1.0,
    ground_truth_source: str | None = None,
) -> None:
    """Fold one game's status into the run counters (both paths)."""
    counters.games_attempted += 1
    if status == "scored" and abs(float(park_factor) - 1.0) > 1e-9:
        counters.games_park_nonneutral += 1
    if status == "scored" and ground_truth_source == GROUND_TRUTH_OFFICIAL:
        counters.games_official_boxscore += 1
    elif status == "scored" and ground_truth_source == GROUND_TRUTH_EVENT_LABEL:
        counters.games_event_label += 1
    if status == "no_odds":
        counters.games_no_odds += 1
    elif status == "unresolved":
        counters.games_unresolved += 1
    elif status == "scored":
        counters.games_scored += 1
    # "empty": attempted, with no results; it counts in no scored or skip bucket.


async def _run_serial(
    game_pks: list[int],
    *,
    duck: Any,
    do_game: bool,
    do_props: bool,
    calibration_map: Any,
    args: argparse.Namespace,
) -> _Counters:
    """The serial in-process path (``--workers 1``).

    It opens ONE asyncpg pool in this process and scores each game in turn through
    :func:`_score_one_game`, the pipeline the workers run: the no-pool debug mode
    and the reference of a byte-identical check. ``duck`` is the parent's read-only
    sim-DuckDB connection (the park factor comes from it). ``calibration_map`` is
    the one map the whole run scores win probabilities through, loaded once by the
    command line; in the parallel path each worker loads its own.
    """
    import asyncpg

    counters = _Counters()
    pool = None
    try:
        if game_pks:
            pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        for game_pk in game_pks:
            accuracy_records, status, park_factor, ground_truth_source = await _score_one_game(
                pool,
                game_pk,
                duck=duck,
                do_game=do_game,
                do_props=do_props,
                iterations=args.iterations,
                base_seed=args.base_seed,
                calibration_map=calibration_map,
                benchmark_book=getattr(args, "benchmark_book", None),
            )
            _tally(counters, status, park_factor, ground_truth_source)
            counters.accuracy_records.extend(accuracy_records)
            if counters.games_scored and counters.games_scored % 25 == 0:
                log.info(
                    "  scored %d/%d games (%d accuracy rows) ...",
                    counters.games_scored,
                    len(game_pks),
                    len(counters.accuracy_records),
                )
    finally:
        if pool is not None:
            await pool.close()
    return counters


def _run_parallel(
    game_pks: list[int],
    *,
    workers: int,
    do_game: bool,
    do_props: bool,
    calibration_path: str,
    args: argparse.Namespace,
) -> _Counters:
    """The across-games parallel path (``--workers > 1``).

    It builds a ``ProcessPoolExecutor`` with the ``forkserver`` context and maps
    :func:`_process_one_game` over the games; ``workers`` games run at
    once, each worker running a WHOLE game serially. The records are collected as
    the games complete. The parent loads no engine artifacts, so the forkserver
    workers never inherit a large parent. The records equal the serial path's for
    the same (game set, base seed, iterations).
    """
    from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, as_completed

    from simulation.sim_kwargs import UnresolvedParkFactorError

    worker_params: dict[str, Any] = {
        "do_game": do_game,
        "do_props": do_props,
        "iterations": int(args.iterations),
        "base_seed": int(args.base_seed),
        "dsn": args.dsn,
        # Each worker opens this path read-only for the park-factor lookup.
        "duckdb": args.duckdb,
        # The workers honour the escape hatch the parent honours.
        "allow_neutral_parks": bool(getattr(args, "allow_neutral_parks", False)),
        # A path, not a loaded map, so the parameters stay picklable: each worker
        # loads its own map once (_worker_lazy_init step (d)).
        "calibration_path": calibration_path,
        # A book label, or None for the preference list.
        "benchmark_book": getattr(args, "benchmark_book", None),
    }
    counters = _Counters()
    if not game_pks:
        return counters

    log.info(
        "ACROSS-GAMES parallel: %d games over %d forkserver workers (~373 MB each).",
        len(game_pks),
        workers,
    )
    with ProcessPoolExecutor(max_workers=workers, mp_context=_pool_mp_context()) as pool:
        futures = {
            pool.submit(_process_one_game, int(pk), worker_params): int(pk) for pk in game_pks
        }
        done = 0
        for fut in as_completed(futures):
            game_pk = futures[fut]
            try:
                payload = fut.result()
            except Exception as exc:  # noqa: BLE001 — split into fatal vs one bad game
                # A defect, a broken worker or a dead pool is NOT one bad game. One
                # classifier (_is_process_broken) decides with the two fatal types
                # named here, so this branch and the worker's cannot drift apart.
                fatal = isinstance(
                    exc, UnresolvedParkFactorError | BrokenExecutor
                ) or _is_process_broken(exc)
                if fatal:
                    # Cancel the queue FIRST: the `with` block's exit waits for
                    # every pending future, so without this the run would feed games
                    # to broken workers for the rest of the season before the error
                    # surfaced.
                    log.error(
                        "STOPPING the run at game %s — %s: %s",
                        game_pk,
                        type(exc).__name__,
                        exc,
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                log.warning("game %s worker crashed (%s)", game_pk, type(exc).__name__)
                _tally(counters, "unresolved")
                continue
            _tally(
                counters,
                str(payload.get("status", "unresolved")),
                float(payload.get("park_run_factor", 1.0)),
                payload.get("ground_truth_source"),
            )
            counters.accuracy_records.extend(
                AccuracyRecord.from_jsonable(d) for d in payload.get("accuracy_records", [])
            )
            done += 1
            if done % 25 == 0:
                log.info(
                    "  completed %d/%d games (%d accuracy rows) ...",
                    done,
                    len(game_pks),
                    len(counters.accuracy_records),
                )
    return counters
