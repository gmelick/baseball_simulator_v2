"""
api/routes/games.py
===================
The game routes (prefix ``/api/games``) and the simulation seam they share.

This module holds the aggregate ``router`` (a login on every route) and every
route that runs a simulation:

    GET  /{game_pk}/simulate                  an N-game Monte-Carlo summary
    POST /{game_pk}/simulate                  queue a durable run (a sim.sim_runs row)
    POST /{game_pk}/what-if                   two runs from a plate appearance, as played and changed
    POST /{game_pk}/sample-game               simulate and store one game for the game page
    POST /{game_pk}/simulate/with_override    a baseline run and an override run at one seed
    GET  /{game_pk}/boxscore                  every player's prop means over N games
    GET  /{game_pk}/props/{player_id}/{prop}  one player's prop distribution and edge

It also holds the reads of the replay store, one stored game per run:
``/plays``, ``/state/{at_bat}/{pitch}``, ``/linescore``, ``/decisions`` and
``/card``.

The routers it includes add the rest: the slate (``games_slate``), the real
game (``games_feed``) and the run reads (``games_runs``). Their models live in
``games_models``. Each sub-router is a bare ``APIRouter()``: the aggregate's
prefix, tag and login apply to its routes, so the OpenAPI contract does not
depend on the module a route lives in.

THE FACTORY SEAM
----------------
A simulated game needs a ``StateMachine`` factory. The production factory
builds a sampler over the engine-artifact bundle, which a unit test does not
have. So :func:`resolve_factory_ref` picks the dotted factory ref in this order:

    1. ``request.app.state.sim_factory_ref``   (an override on the app)
    2. ``$SIM_MACHINE_FACTORY_REF``            (an override in the environment)
    3. :data:`PRODUCTION_FACTORY_REF`           (the production default)

A unit test sets the first, or patches the third, to the no-DB factory
``simulation.batch_runner:rng_driven_machine_factory``.

EVERY ROUTE RESOLVES THE PARK FACTOR
------------------------------------
:func:`_resolve_state_or_error` marks the park factor of every game state as
unresolved, and :func:`_resolved_sim_kwargs` looks the venue's factor up before
it builds the sim kwargs. A route that skipped the lookup would raise at build
time instead of running a park-blind simulation (the park-factor resolution,
SIM-452).

CACHING
-------
``BatchRunner`` memoizes a summary by (spec, seed, N) for ``SIM_RESULT_TTL_S``,
in the app's cache (``app.state.sim_cache``) when one is attached.
``?use_cache=false`` skips the cache. The cache only saves work; no route needs
it.

THE REPLAY STORE
----------------
The replay store is a DuckDB file of its own (``app.state.replay_duckdb``; the
replay store, SIM-561). It holds one recorded game per run: the play stream,
the state after each pitch, the linescore and the pitcher decisions. A route
records the game on the worker pool and writes it under the file's next run id.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.auth import require_auth
from api.routes import games_feed, games_runs, games_slate
from api.routes._common import _get_pool, _get_sim_cache, _row_get
from api.routes.games_feed import _feed_lineup_rows, _league_game_payload, _start_state
from api.routes.games_models import (
    GameCardResponse,
    RosterOverride,
    SampleGameResponse,
    SimRunModel,
    SimRunRequest,
    SimulateResponse,
    WhatIfRequest,
    WhatIfStarted,
    WithOverrideResponse,
    _sim_summary_lite_from_stored,  # noqa: F401 # re-export: removed in the integration pass
    _with_half_width,  # noqa: F401 # re-export: removed in the integration pass
)
from api.routes.games_runs import _get_sim_jobs, _run_model, _stored_prop_set
from api.schemas import (
    BoxscoreCardModel,
    EdgeReportModel,
    GameSimSummaryModel,
    LinescoreModel,
    OverrideDeltaModel,
    PitcherDecisionsModel,
    PlayByPlayModel,
    PropEdgeResponse,
    StateAtPitchModel,
)
from api.serialization import to_jsonable
from betting import MarketSide, OddsQuote, TwoWayMarket
from betting import prop_edge_report as _prop_edge_report
from db import sim_store
from simulation.batch_runner import BatchRunner, GameSpec
from simulation.flags import read_str
from simulation.linescore import linescore_from_plays
from simulation.lineup_resolver import (
    LineupIncompleteError,
    LineupNotIngestedError,
    LineupResolutionError,
    build_defense_map_for_state,
    resolve_game_state,
    resolve_lineup,
)
from simulation.pitcher_decisions import decisions_from_plays
from simulation.play_recorder import RecordedGame, record_game
from simulation.production_factory import (
    _default_bullpen_for_spec,
    placeholder_reliever_number,
)
from simulation.prop_distributions import ALL_PROPS, PropDistributionSet
from simulation.sim_kwargs import (
    build_sim_kwargs as _build_sim_kwargs,
)
from simulation.sim_kwargs import (
    mark_park_factor_unresolved,
)
from simulation.sim_kwargs import (
    resolve_park_run_factor as _resolve_park_run_factor,  # noqa: F401 # re-export: removed in the integration pass
)
from simulation.sim_kwargs import (
    sim_kwargs_from_state as _sim_kwargs_from_state,  # noqa: F401 # re-export: removed in the integration pass
)
from simulation.snapshots import (
    FieldSnapshot,
    OverrideDelta,
    PlayByPlay,
    PlayByPlayEntry,
    PlayerRef,
    StateAtPitch,
    thrown_pitches,
)

log = logging.getLogger("api.routes.games")

#: The aggregate router: every route under /api/games requires a login (cookie
#: or API key). The three sub-routers join it here, ahead of this module's own
#: routes.
router = APIRouter(prefix="/api/games", tags=["games"], dependencies=[Depends(require_auth)])
router.include_router(games_slate.router)
router.include_router(games_feed.router)
router.include_router(games_runs.router)


# ---------------------------------------------------------------------------
# The factory seam and the app.state accessors
# ---------------------------------------------------------------------------

#: The production machine factory's dotted ref. It builds a sampler over the
#: engine-artifact bundle, which a unit test does not have; a test patches this
#: default, or sets ``app.state.sim_factory_ref``, to the no-DB factory
#: ``"simulation.batch_runner:rng_driven_machine_factory"``. The string is part
#: of the result and prop-set cache keys.
PRODUCTION_FACTORY_REF = "simulation.production_factory:production_machine_factory"


def resolve_factory_ref(request: Request) -> str:
    """The machine factory's dotted ref for this request.

    The order: ``app.state.sim_factory_ref``, then ``$SIM_MACHINE_FACTORY_REF``,
    then :data:`PRODUCTION_FACTORY_REF` (the module docstring, THE FACTORY SEAM).
    """
    override = getattr(request.app.state, "sim_factory_ref", None)
    if override:
        return str(override)
    env_ref = read_str("SIM_MACHINE_FACTORY_REF")
    if env_ref:
        return env_ref
    return PRODUCTION_FACTORY_REF


def _get_sim_duckdb(request: Request) -> Any:
    """The analytics DuckDB connection on ``app.state.sim_duckdb``, or None.

    The park-factor lookup (:func:`_resolved_sim_kwargs`) reads the venue's run
    factor through it. The lifespan never attaches one: with None the lookup
    reads the snapshot that the lifespan primed (``prime_park_factor_source``),
    so the app never holds the analytics file open. A test attaches a fake
    connection to feed the lookup.
    """
    return getattr(request.app.state, "sim_duckdb", None)


def _get_replay_store(request: Request) -> Any:
    """The replay file's DuckDB connection on app.state, or None.

    The lifespan opens it when ``REPLAY_PERSISTENCE_ENABLED`` is on: a file of its
    own (``REPLAY_DUCKDB_PATH``, default ``/data/replay.duckdb``) that only the app
    writes. It is not the analytics connection (:func:`_get_sim_duckdb`), so the
    app never holds a write lock on the analytics file. ``None`` means the store
    is off: the replay reads answer 503 and the writes skip.
    """
    return getattr(request.app.state, "replay_duckdb", None)


def _replay_call(con: Any, fn: Any, /, **kwargs: Any) -> Any:
    """Run ``fn(cursor, **kwargs)`` on a cursor of its own, then close it.

    A DuckDB connection is not safe to share across threads; a cursor per call
    is. The routes run every replay read and write in a worker thread.
    """
    cur = con.cursor()
    try:
        return fn(cur, **kwargs)
    finally:
        cur.close()


def _build_runner(request: Request) -> BatchRunner:
    """The BatchRunner for this request: the app's shared runner when attached.

    The lifespan attaches one long-lived runner as ``app.state.sim_runner``. It
    keeps one warm worker pool and the shared-memory arrays across requests, so
    a request pays no fork and no publish. The caller runs the batch in a worker
    thread (``asyncio.to_thread``).

    With no shared runner (a unit test that builds the app without the
    lifespan), the route builds a runner of its own on the app's sim cache, else
    on the runner's default cache. ``max_workers=1`` runs the batch in this
    process: no fork and no pickling.
    """
    shared = getattr(request.app.state, "sim_runner", None)
    if shared is not None:
        return shared
    cache = _get_sim_cache(request)
    return BatchRunner(cache=cache, max_workers=1)


# ---------------------------------------------------------------------------
# The game state and its sim kwargs
#
# ``_resolved_sim_kwargs`` is the one way a route gets sim kwargs, and it looks
# the venue's park factor up first (the park-factor resolution, SIM-452). The
# builder itself lives in ``simulation/sim_kwargs.py``.
# ---------------------------------------------------------------------------


async def _resolved_sim_kwargs(
    request: Request, pool: Any, state: Any, game_pk: int
) -> dict[str, Any]:
    """Resolve the venue's park factor onto ``state``, then build the sim kwargs.

    Every route uses this. ``_resolve_state_or_error`` marks the factor as
    unresolved, and ``simulation.sim_kwargs.build_sim_kwargs`` replaces the mark
    with the looked-up factor. A route that called the bare builder instead
    would raise ``UnresolvedParkFactorError``.

    The lookup reads ``app.state.sim_duckdb`` (:func:`_get_sim_duckdb`), else the
    primed snapshot. With neither, the resolver returns a neutral ``1.0``, which
    is a resolved answer, not a skipped lookup.
    """
    return await _build_sim_kwargs(
        state,
        pool=pool,
        con=_get_sim_duckdb(request),
        game_pk=int(game_pk),
    )


def _apply_override(base_kwargs: dict[str, Any], override: RosterOverride) -> dict[str, Any]:
    """Return a copy of ``base_kwargs`` with the override's set fields applied.

    The order:

    1. A full lineup (``home_lineup`` / ``away_lineup``) replaces that side's
       batting order.
    2. The ``substitutions`` apply left to right to the lineup that step 1
       left. A ``batting_order`` outside the lineup is skipped, so one bad slot
       does not stop the rest of the override.
    3. The pitcher and the bat hand apply last.

    Only a field the caller set (not None) replaces the baseline value, so an
    override of the pitcher alone leaves both lineups as they were.
    """
    kw = dict(base_kwargs)

    # Step 1: full-lineup replacements.
    if override.home_lineup is not None:
        kw["home_lineup"] = [int(x) for x in override.home_lineup]
    if override.away_lineup is not None:
        kw["away_lineup"] = [int(x) for x in override.away_lineup]

    # Step 2: the single-player substitutions.
    if override.substitutions:
        home_lineup = list(kw.get("home_lineup") or [])
        away_lineup = list(kw.get("away_lineup") or [])
        for sub in override.substitutions:
            slot = int(sub.batting_order) - 1  # 1-indexed → 0-indexed
            if sub.side == "home" and 0 <= slot < len(home_lineup):
                home_lineup[slot] = int(sub.player_id)
            elif sub.side == "away" and 0 <= slot < len(away_lineup):
                away_lineup[slot] = int(sub.player_id)
            # A slot outside the lineup is skipped.
        kw["home_lineup"] = home_lineup
        kw["away_lineup"] = away_lineup

    # Step 3: the pitcher and the bat hand.
    if override.pitcher_id is not None:
        kw["pitcher_id"] = int(override.pitcher_id)
    if override.bat_hand is not None:
        kw["bat_hand"] = str(override.bat_hand)
    return kw


async def _resolve_state_or_error(
    pool: Any, game_pk: int, *, fallback_rows: list[dict[str, Any]] | None = None
) -> Any:
    """Resolve a game's GameState from its lineup; map a failure to an HTTP error.

    The function acquires a connection from the pool (asyncpg's ``async with
    pool.acquire()``); a pool that IS a connection (the mock-pool test idiom) is
    used directly. ``fallback_rows`` are lineup rows to use when the table has
    none (the what-if builds them from the league feed).

    The errors:

    * ``LineupNotIngestedError`` (the game exists, its lineup is not published
      yet) is a 503 with ``Retry-After: 900``.
    * ``LineupIncompleteError`` (the game exists, its lineup rows do not make a
      playable game: a side with no pitcher, an empty batting order) is the
      same 503. A later lineup publish or the box backfill fills the gap; the
      game is not missing (the incomplete-lineup answer, SIM-559).
    * ``LineupResolutionError`` (the game is not in ``raw.games``) is a 404.

    Every state it returns carries the unresolved park-factor mark. Every
    sim-kwargs call site gets its state here, so a caller that skips the venue
    lookup raises ``UnresolvedParkFactorError`` at build time instead of running
    a park-blind simulation that reports success.
    """
    acquire = getattr(pool, "acquire", None)
    try:
        if acquire is not None:
            async with pool.acquire() as conn:
                state = await resolve_game_state(conn, game_pk, fallback_rows=fallback_rows)
        else:
            # The pool itself exposes fetch / fetchrow (a mock pool or a connection).
            state = await resolve_game_state(pool, game_pk, fallback_rows=fallback_rows)
        mark_park_factor_unresolved(state)
        return state
    except (LineupNotIngestedError, LineupIncompleteError) as exc:
        # Transient: the lineup is not published yet, or its rows do not yet
        # make a playable game. Suggest a retry in 15 minutes.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "900"},
        ) from exc
    except LineupResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


def _run_batch(
    runner: BatchRunner,
    spec: GameSpec,
    *,
    n_iterations: int,
    base_seed: int | None,
    use_cache: bool,
):
    """Run the Monte-Carlo batch (sync; the route runs it in a worker thread).

    The wrapper lets the route ``await asyncio.to_thread(...)`` the batch, so
    the event loop stays free while the games run.
    """
    return runner.run(
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )


# ---------------------------------------------------------------------------
# A stored pitch state back to its response model
# ---------------------------------------------------------------------------


def _player_ref_from_jsonable(ref: Any) -> PlayerRef | None:
    """Rebuild an Optional[PlayerRef] from its jsonable dict (None == empty)."""
    if ref is None:
        return None
    return PlayerRef(
        player_id=int(ref["player_id"]),
        label=None if ref.get("label") is None else str(ref["label"]),
    )


def _state_at_pitch_model_from_snapshot(snapshot: Mapping[str, Any]) -> StateAtPitchModel:
    """Rebuild a :class:`StateAtPitchModel` from a stored StateAtPitch.

    The stored ``snapshot`` is ``to_jsonable(StateAtPitch)``: a dict
    ``{at_bat, pitch, sequence, field}`` whose ``field`` holds the
    :class:`~simulation.snapshots.FieldSnapshot` dataclass fields only;
    ``to_jsonable`` does not write the derived ``occupied_bases`` and
    ``runners_on`` properties. So the function rebuilds the dataclasses, which
    recompute them, and converts through ``StateAtPitchModel.from_dataclass``,
    the converter of a fresh snapshot. The wire shape is the same either way.
    """
    f = snapshot["field"]
    field_snap = FieldSnapshot(
        positions={
            str(pos): _player_ref_from_jsonable(ref)
            for pos, ref in (f.get("positions") or {}).items()
        },
        batter=_player_ref_from_jsonable(f.get("batter")),
        baserunners={
            str(base): _player_ref_from_jsonable(ref)
            for base, ref in (f.get("baserunners") or {}).items()
        },
        balls=int(f["balls"]),
        strikes=int(f["strikes"]),
        outs=int(f["outs"]),
        inning=int(f["inning"]),
        half=str(f["half"]),
        home_score=int(f["home_score"]),
        away_score=int(f["away_score"]),
        runners_state=int(f.get("runners_state", 0)),
    )
    sap = StateAtPitch(
        at_bat=int(snapshot["at_bat"]),
        pitch=int(snapshot["pitch"]),
        field=field_snap,
        sequence=(None if snapshot.get("sequence") is None else int(snapshot["sequence"])),
    )
    return StateAtPitchModel.from_dataclass(sap)


# ---------------------------------------------------------------------------
# Record one game and store it in the replay file (a store failure never
# breaks a route)
# ---------------------------------------------------------------------------


class ReplayArtifacts(NamedTuple):
    """One recorded game, built for the replay store."""

    play_by_play: PlayByPlay
    snapshots: list[dict]
    linescore: dict
    decisions: dict
    result: Any  # the recorded GameSimResult


def _record_and_build(
    *,
    factory_ref: str,
    base_seed: int | None,
    sim_kwargs: dict[str, Any],
    resolved: Any = None,
) -> ReplayArtifacts:
    """Record one game in this process and build its replay artifacts (sync).

    No route calls this: the routes record on the worker pool
    (``BatchRunner.record_game``) and call :func:`_build_replay_artifacts`. A
    test drives the builder through it with ``record_game`` patched, and two
    tests patch ``record_game`` here to prove no route records in the API
    process.
    """
    recorded = record_game(
        factory_ref=factory_ref,
        seed=base_seed,
        sim_kwargs=sim_kwargs,
    )
    return _build_replay_artifacts(recorded, resolved=resolved)


def _build_replay_artifacts(recorded: RecordedGame, *, resolved: Any = None) -> ReplayArtifacts:
    """Build the replay artifacts of one recorded game (pure and sync).

    From a :class:`~simulation.play_recorder.RecordedGame` (the game, its plays
    and the state before each), the function derives:

      * a :class:`~simulation.snapshots.PlayByPlay` (the /plays scroll); each
        entry carries its inning, half, outs, batter, pitcher and the score
        after it;
      * one stored :class:`~simulation.snapshots.StateAtPitch` per pitch (the
        /state rows), tagged with the pitch's at_bat, pitch and sequence from
        its play-by-play entry and built from the result's ``next_state`` (the
        GameState after the pitch);
      * the :class:`~simulation.linescore.Linescore` and the
        :class:`~simulation.pitcher_decisions.PitcherDecisions`. Both read
        ``PlayResult.next_state``, which the stored entries drop, so they are
        built here, at record time.

    With a ``resolved`` :class:`~simulation.lineup_resolver.ResolvedLineup`,
    each pitch's state carries the fielding side's nine fielders
    (``build_defense_map_for_state``); without one the nine slots are empty.

    A pickoff before the pitch that makes the third out returns a result with
    no pitch (``NO_PITCH``). The play-by-play and the states hold the thrown
    pitches only; the linescore and the decisions read the whole stream,
    because the pickoff's out lives on that result. A pitch with no
    ``next_state`` gets no state row; its play-by-play entry stays.
    """
    plays = recorded.plays
    pbp = PlayByPlay.from_play_results(plays, recorded.contexts)
    pitched = thrown_pitches(plays)

    # Both read the whole stream: a no-pitch result carries the pickoff's out.
    linescore = linescore_from_plays(plays)
    decisions = decisions_from_plays(plays)

    # The defense map changes only with the half (which side fields) and a
    # substitution, which the resolver's "latest occupant" map already holds.
    # So it is built once per half, not per pitch.
    defense_cache: dict[Any, dict[str, int] | None] = {}

    def _defense_for(next_state: Any) -> dict[str, int] | None:
        if resolved is None:
            return None
        key = getattr(next_state, "half", None)
        if key not in defense_cache:
            try:
                defense_cache[key] = build_defense_map_for_state(resolved, next_state)
            except Exception:  # noqa: BLE001 -- a bad lineup must not break replay
                defense_cache[key] = None
        return defense_cache[key]

    # The thrown pitches and pbp.entries pair one to one, in order (a no-pitch
    # result makes no entry), so zip pairs each PlayResult with its entry's
    # (at_bat, pitch, sequence). strict=True fails loudly on a slip rather than
    # storing every later state one pitch off.
    snapshots: list[dict] = []
    for play, entry in zip(pitched, pbp.entries, strict=True):
        next_state = getattr(play, "next_state", None)
        if next_state is None:
            continue
        sap = StateAtPitch.from_game_state(
            next_state,
            at_bat=entry.at_bat,
            pitch=entry.pitch,
            sequence=entry.sequence,
            defense_positions=_defense_for(next_state),
        )
        snapshots.append(to_jsonable(sap))
    return ReplayArtifacts(
        play_by_play=pbp,
        snapshots=snapshots,
        linescore=to_jsonable(linescore),
        decisions=to_jsonable(decisions),
        result=recorded.result,
    )


async def _persist_replay_artifacts(
    request: Request,
    *,
    game_pk: int,
    factory_ref: str,
    base_seed: int | None,
    sim_kwargs: dict[str, Any],
    batch: Any = None,
) -> int | None:
    """Record one game and store it in the replay file; return its run id.

    The function records one game at ``base_seed`` on the warm worker pool and
    stores its play stream, its per-pitch states and its game card in the
    replay file, which numbers its own runs. ``batch`` is the /simulate batch:
    its summary goes to the Postgres sim-run history. A sample game or the
    projections' first game (``batch`` None) writes no history row: a one-game
    summary is not a Monte-Carlo run.

    It never raises. A missing replay store returns None at once, and any
    failure logs a warning and returns None, so a store failure never breaks
    /simulate or /boxscore; the sample-game route checks for None. After a
    store, the game keeps only its newest :data:`db.sim_store.REPLAY_RUNS_KEPT`
    runs.
    """
    con = _get_replay_store(request)
    pool = getattr(request.app.state, "pg_pool", None)

    # Nothing to write the replay stream to: the replay reads answer 503
    # "replay store unavailable". A /simulate batch still keeps its summary in
    # the Postgres history, which the slate card's sim line reads.
    if con is None:
        if batch is not None and pool is not None:
            await _store_history_run(pool, game_pk=game_pk, batch=batch, base_seed=base_seed)
        return None

    try:
        # The lineup gives the stored states their nine fielders. A failed
        # resolve (a mock pool with no lineup rows) leaves the slots empty; the
        # game is still stored and /state still answers.
        resolved = await _resolve_lineup_best_effort(pool, game_pk)

        # 1) Record the game on the warm worker pool (the API process never
        #    loads the sim bundle), then build the artifacts here.
        runner = _build_runner(request)
        spec = GameSpec(machine_factory=factory_ref, sim_kwargs=sim_kwargs)
        recorded = await asyncio.to_thread(runner.record_game, spec, base_seed)
        built = await asyncio.to_thread(_build_replay_artifacts, recorded, resolved=resolved)

        # 2) Store the game in the replay file under the file's own next run id.
        run_id = await asyncio.to_thread(
            _replay_call, con, _store_replay_run, game_pk=game_pk, built=built, base_seed=base_seed
        )

        # 3) A /simulate batch also keeps its summary in the Postgres history.
        if batch is not None and pool is not None:
            await _store_history_run(pool, game_pk=game_pk, batch=batch, base_seed=base_seed)
        return run_id
    except Exception as exc:  # noqa: BLE001 -- persistence must never break /simulate
        log.warning("replay-artifact persist failed for game %s: %s", game_pk, exc)
        return None


async def _store_history_run(pool: Any, *, game_pk: int, batch: Any, base_seed: int | None) -> None:
    """Keep a /simulate batch's summary in the Postgres sim-run history.

    A failure logs a warning and returns. The history row's id is its own; the
    replay file numbers its runs apart.
    """
    try:
        summary_json = to_jsonable(batch.summary)
        acquire = getattr(pool, "acquire", None)
        if acquire is not None:
            async with pool.acquire() as conn:
                await sim_store.store_sim_run(
                    conn,
                    game_pk=game_pk,
                    summary=summary_json,
                    n_iterations=int(batch.n_iterations),
                    base_seed=base_seed,
                )
        else:
            await sim_store.store_sim_run(
                pool,
                game_pk=game_pk,
                summary=summary_json,
                n_iterations=int(batch.n_iterations),
                base_seed=base_seed,
            )
    except Exception as exc:  # noqa: BLE001 -- history write is optional
        log.warning("sim-run history persist failed for game %s: %s", game_pk, exc)


#: One replay write at a time in this process. The run id is the file's max + 1,
#: and two prunes of the same game conflict in DuckDB ("Conflict on tuple
#: deletion"), so the id draw, the store and the prune run under this lock.
_REPLAY_WRITE_LOCK = threading.Lock()


def _store_replay_run(
    cur: Any, *, game_pk: int, built: ReplayArtifacts, base_seed: int | None
) -> int:
    """Write one recorded game to the replay file on ``cur``; return its run id (sync).

    The play stream, the states and the game card go in one transaction under
    the file's next run id. The prune of the game's older runs follows; a
    prune failure logs a warning and keeps the stored game.
    """
    play_rows = [dataclasses.asdict(e) for e in built.play_by_play.entries]
    with _REPLAY_WRITE_LOCK:
        run_id = sim_store.next_replay_run_id(cur)
        cur.execute("BEGIN TRANSACTION")
        try:
            sim_store.store_play_stream(cur, game_pk=game_pk, run_id=run_id, play_entries=play_rows)
            sim_store.store_state_snapshots(
                cur, game_pk=game_pk, run_id=run_id, snapshots=built.snapshots
            )
            # The game card (linescore and decisions), built at record time and
            # keyed on the same run_id.
            sim_store.store_game_card(
                cur,
                game_pk=game_pk,
                run_id=run_id,
                linescore=built.linescore,
                decisions=built.decisions,
                base_seed=base_seed,
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        try:
            sim_store.prune_replay_runs(cur, game_pk=game_pk)
        except Exception as exc:  # noqa: BLE001 -- the game is stored; pruning can wait
            log.warning("replay prune failed for game %s: %s", game_pk, exc)
    return run_id


async def _resolve_lineup_best_effort(pool: Any, game_pk: int) -> Any:
    """Resolve a game's :class:`ResolvedLineup`, or None on any failure.

    :func:`_persist_replay_artifacts` uses it to fill the nine fielders of each
    stored state. A missing pool, an unknown game or no lineup rows gives None:
    the states then have empty fielder slots, and the store goes on.
    """
    if pool is None:
        return None
    try:
        acquire = getattr(pool, "acquire", None)
        if acquire is not None:
            async with pool.acquire() as conn:
                return await resolve_lineup(conn, int(game_pk))
        return await resolve_lineup(pool, int(game_pk))
    except Exception as exc:  # noqa: BLE001 -- fielder map is best-effort
        log.warning("defense-map lineup resolve failed for game %s: %s", game_pk, exc)
        return None


# ===========================================================================
# GET /simulate: an N-game Monte-Carlo summary
# ===========================================================================


@router.get(
    "/{game_pk}/simulate",
    response_model=SimulateResponse,
    summary="Simulate a game (Monte-Carlo)",
    dependencies=[Depends(require_auth)],
    description=(
        "Resolve the game's lineup into a GameState (SIM-353), run an "
        "N-iteration Monte-Carlo batch (SIM-332), and return the GameSimSummary "
        "(numpy-free JSON, SIM-350). The machine factory is the testability seam "
        "(see resolve_factory_ref). Summary results are cached at "
        "SIM_RESULT_TTL_S (60s) keyed on (spec + seed + N) -- SIM-359."
    ),
)
async def simulate_game_endpoint(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(100, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(None, description="Reproducibility seed for the whole batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
) -> SimulateResponse:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    # The venue's park factor resolves before the kwargs build.
    spec = GameSpec(
        machine_factory=factory_ref,
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    runner = _build_runner(request)

    # The batch is CPU-bound: a worker thread runs it, so the event loop stays free.
    _t0 = time.perf_counter()
    batch = await asyncio.to_thread(
        _run_batch,
        runner,
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )
    # The batch's wall-clock time feeds the baseball_sim_latency_seconds gauge.
    # A cache hit ran no games, so it records nothing; a failure never breaks
    # the response.
    if not batch.from_cache:
        try:
            from api.routes.metrics import record_sim_latency

            record_sim_latency(request.app.state, time.perf_counter() - _t0)
        except Exception:  # noqa: BLE001
            pass

    # Store one game at the run's base_seed for /plays and /state, and the
    # batch's summary in the sim-run history. A store failure never breaks the
    # response.
    await _persist_replay_artifacts(
        request,
        game_pk=int(game_pk),
        factory_ref=factory_ref,
        base_seed=base_seed,
        sim_kwargs=spec.sim_kwargs,
        batch=batch,
    )

    return SimulateResponse(
        game_pk=int(game_pk),
        n_iterations=batch.n_iterations,
        base_seed=base_seed,
        from_cache=bool(batch.from_cache),
        summary=GameSimSummaryModel.from_dataclass(batch.summary),
    )


# ===========================================================================
# The run jobs: POST /simulate and POST /what-if
# ===========================================================================


#: The source of the lineup the simulator reads for a game: the box, else a
#: published, else a projected lineup (the ``source`` column, Alembic 0029).
#: None before 0029 or with no row.
_SQL_LINEUP_SOURCE = """
    SELECT CASE WHEN bool_or(source = 'box') THEN 'box'
                WHEN bool_or(source = 'published') THEN 'published'
                WHEN bool_or(source = 'projected') THEN 'projected' END
      FROM raw.game_lineups WHERE game_pk = $1
"""


async def _lineup_source(pool: Any, game_pk: int) -> str | None:
    """The source of the game's lineup (:data:`_SQL_LINEUP_SOURCE`), or None."""
    try:
        return await pool.fetchval(_SQL_LINEUP_SOURCE, int(game_pk))
    except Exception:  # noqa: BLE001 -- before 0029 there is no source column
        return None


@router.post(
    "/{game_pk}/simulate",
    response_model=SimRunModel,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a simulation run (SIM-519 Part E)",
    dependencies=[Depends(require_auth)],
    description=(
        "Queue one Monte-Carlo run of the game: N iterations from first pitch. The run "
        "is a durable row; poll GET /simulate/runs/{run_id} for its state and progress. "
        "A run with the same game, seed and N that is queued or running is returned "
        "instead (existing=true). When done, the row holds the summary, every player's "
        "prop distributions and the inning grids, and the panels read it. 503 with "
        "Retry-After when the lineup is not yet published; 404 for an unknown game."
    ),
)
async def start_simulation_run(game_pk: int, request: Request, body: SimRunRequest) -> SimRunModel:
    jobs = _get_sim_jobs(request)
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)
    factory_ref = resolve_factory_ref(request)
    spec = GameSpec(
        machine_factory=factory_ref,
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    runner = _build_runner(request)

    async def record_game(seed: int | None) -> int | None:
        # The run's representative game, stored in the replay file: the
        # linescore and play-by-play panels show it.
        return await _persist_replay_artifacts(
            request,
            game_pk=int(game_pk),
            factory_ref=factory_ref,
            base_seed=seed,
            sim_kwargs=spec.sim_kwargs,
        )

    try:
        run, existing = await jobs.submit(
            game_pk=int(game_pk),
            spec=spec,
            runner=runner,
            n_iterations=body.n_iterations,
            base_seed=body.base_seed,
            lineup_source=await _lineup_source(pool, game_pk),
            bullpen_source=getattr(state, "bullpen_source", None),
            record_game=record_game,
        )
    except Exception as exc:
        # Before Alembic 0029 the run table has no job columns.
        if "does not exist" in str(exc) and "column" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="simulation runs need database migration 0029 (make migrate)",
            ) from exc
        raise
    return _run_model(run, existing=existing)


@router.post(
    "/{game_pk}/what-if",
    response_model=WhatIfStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Simulate the rest of the game from a plate appearance, with managerial changes",
    dependencies=[Depends(require_auth)],
    description=(
        "Builds the real game's state at the start of `at_bat` (or first pitch), applies "
        "the changes (pinch-hit, reliever, pinch-run, defense), and queues TWO runs with "
        "the same seed and N: as the game really stood, and with the changes. Poll "
        "GET /what-if/{base_run_id}/{change_run_id}. 422 for an illegal change."
    ),
)
async def start_what_if(game_pk: int, request: Request, body: WhatIfRequest) -> WhatIfStarted:
    from simulation.whatif import IllegalChange, apply_changes, describe_changes

    jobs = _get_sim_jobs(request)
    base_ss = await _start_state(request, game_pk, body.at_bat)
    changes = body.changes.model_dump(exclude_none=True)
    try:
        changed_ss = apply_changes(base_ss, changes)
    except IllegalChange as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    pool = _get_pool(request)
    try:
        state = await _resolve_state_or_error(pool, game_pk)
    except HTTPException as exc:
        # A game that started before its lineup was stored: the base state
        # comes from the feed's starting lineup (the start state then replaces
        # the lineups, pitchers, defense and hands anyway).
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        payload, _source, _error = await _league_game_payload(request, game_pk)
        rows = _feed_lineup_rows(payload) if payload is not None else []
        if not rows:
            raise
        state = await _resolve_state_or_error(pool, game_pk, fallback_rows=rows)
    factory_ref = resolve_factory_ref(request)
    kwargs = await _resolved_sim_kwargs(request, pool, state, game_pk)
    runner = _build_runner(request)
    seed = secrets.randbelow(2**31 - 1)

    def strip(ss: Mapping[str, Any]) -> dict[str, Any]:
        # Only the simulator's keys travel to the workers.
        return {
            k: v
            for k, v in ss.items()
            if k not in ("eligible", "used", "names", "batting_side", "at_bat")
        }

    runs = []
    for kind, ss in (("whatif_base", base_ss), ("whatif_change", changed_ss)):
        spec = GameSpec(
            machine_factory=factory_ref, sim_kwargs={**kwargs, "start_state": strip(ss)}
        )
        try:
            run, _existing = await jobs.submit(
                game_pk=int(game_pk),
                spec=spec,
                runner=runner,
                n_iterations=body.n_iterations,
                base_seed=seed,
                bullpen_source=getattr(state, "bullpen_source", None),
                kind=kind,
                start_at_bat=body.at_bat,
                changes=changes if kind == "whatif_change" else None,
            )
        except Exception as exc:
            if "does not exist" in str(exc) and "column" in str(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="what-if runs need database migration 0030 (make migrate)",
                ) from exc
            raise
        runs.append(int(run["run_id"]))
    return WhatIfStarted(
        game_pk=int(game_pk),
        at_bat=body.at_bat,
        base_run_id=runs[0],
        change_run_id=runs[1],
        base_seed=seed,
        changes=describe_changes(changes, base_ss.get("names") or {}),
    )


# ===========================================================================
# POST /sample-game and POST /simulate/with_override
# ===========================================================================


@router.post(
    "/{game_pk}/sample-game",
    response_model=SampleGameResponse,
    summary="Simulate and store one game for the game page",
    dependencies=[Depends(require_auth)],
    description=(
        "Resolve the game's lineup, simulate ONE game at ``base_seed`` (a random "
        "seed when omitted), and store it in the replay file: its play-by-play, "
        "per-pitch states, linescore and pitcher decisions (SIM-561). /linescore, "
        "/decisions, /card, /plays and /state then serve this game. Returns the "
        "stored run's id and seed. 503 if the replay store is off or the lineup "
        "is not yet usable (Retry-After); 404 if the game is unknown; 500 if the "
        "game could not be stored (the app log says why)."
    ),
)
async def simulate_sample_game(
    game_pk: int,
    request: Request,
    base_seed: int | None = Query(None, description="The game's seed; random when omitted"),
) -> SampleGameResponse:
    if _get_replay_store(request) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)
    sim_kwargs = await _resolved_sim_kwargs(request, pool, state, game_pk)
    seed = int(base_seed) if base_seed is not None else secrets.randbelow(1_000_000_000)

    run_id = await _persist_replay_artifacts(
        request,
        game_pk=int(game_pk),
        factory_ref=resolve_factory_ref(request),
        base_seed=seed,
        sim_kwargs=sim_kwargs,
    )
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the simulated game was not stored; the app log says why",
        )
    return SampleGameResponse(game_pk=int(game_pk), run_id=int(run_id), base_seed=seed)


@router.post(
    "/{game_pk}/simulate/with_override",
    response_model=WithOverrideResponse,
    summary="Simulate a game with a roster override (baseline vs override)",
    dependencies=[Depends(require_auth)],
    description=(
        "Run a BASELINE sim (resolved lineup) and an OVERRIDE sim (the modified "
        "roster) at the SAME base seed, then return both summaries plus the "
        "baseline-vs-override OverrideDelta (SIM-331). The factory + caching "
        "seams are shared with /simulate (SIM-355/SIM-359)."
    ),
)
async def simulate_with_override_endpoint(
    game_pk: int,
    override: RosterOverride,
    request: Request,
    n_iterations: int = Query(100, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(0, description="Shared seed for baseline + override"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
) -> WithOverrideResponse:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    runner = _build_runner(request)

    # One resolved park factor serves the baseline and the override.
    base_kwargs = await _resolved_sim_kwargs(request, pool, state, game_pk)
    override_kwargs = _apply_override(base_kwargs, override)

    baseline_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=base_kwargs)
    override_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=override_kwargs)

    # Both batches run at the same base seed, so the roster change is the only
    # difference between the two summaries.
    baseline_batch = await asyncio.to_thread(
        _run_batch,
        runner,
        baseline_spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )
    override_batch = await asyncio.to_thread(
        _run_batch,
        runner,
        override_spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    delta = OverrideDelta.from_summaries(
        baseline_batch.summary,
        override_batch.summary,
        description=override.description,
    )

    return WithOverrideResponse(
        game_pk=int(game_pk),
        n_iterations=n_iterations,
        base_seed=base_seed,
        baseline=GameSimSummaryModel.from_dataclass(baseline_batch.summary),
        override=GameSimSummaryModel.from_dataclass(override_batch.summary),
        delta=OverrideDeltaModel.from_dataclass(delta),
    )


# ===========================================================================
# The replay reads: the newest complete run, and the players' names
# ===========================================================================


def _read_newest_plays(
    cur: Any, *, game_pk: int, run_id: int | None = None
) -> tuple[int | None, int | None, list[dict]]:
    """A complete run's id, seed and play stream (sync).

    ``run_id`` None reads the newest complete run. A run with no game card is
    not complete and reads as empty.
    """
    card = sim_store.load_game_card(cur, game_pk=game_pk, run_id=run_id)
    if card is None:
        return None, None, []
    rows = sim_store.load_play_stream(cur, game_pk=game_pk, run_id=card["run_id"])
    return card["run_id"], card.get("base_seed"), rows


def _read_newest_state(cur: Any, *, game_pk: int, at_bat: int, pitch: int) -> dict | None:
    """One pitch's stored state in the newest complete run (sync)."""
    run_id = sim_store.latest_replay_run_id(cur, game_pk=game_pk)
    if run_id is None:
        return None
    return sim_store.load_state_at(cur, game_pk=game_pk, at_bat=at_bat, pitch=pitch, run_id=run_id)


def _play_player_ids(rows: list[dict]) -> list[int]:
    """Every batter and pitcher id in a stored play stream, sorted."""
    ids = {row.get(key) for row in rows for key in ("batter_id", "pitcher_id")}
    return sorted(int(pid) for pid in ids if pid is not None)


async def _replay_player_names(request: Request, player_ids: list[int]) -> dict[int, str]:
    """Names for the ids in a replay: ``raw.players`` and the placeholder arms.

    A placeholder reliever (a game with no pen in the box) reads "Generic
    reliever N". As in :func:`_player_names`, a missing pool or a failed lookup
    leaves the real players unnamed.
    """
    names = {
        pid: f"Generic reliever {n}"
        for pid in player_ids
        if (n := placeholder_reliever_number(pid)) is not None
    }
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        names.update(await _player_names(pool, [pid for pid in player_ids if pid > 0]))
    return names


# ===========================================================================
# GET /plays and GET /state/{at_bat}/{pitch}
# ===========================================================================


@router.get(
    "/{game_pk}/plays",
    response_model=PlayByPlayModel,
    summary="Play-by-play scroll for a simulated game",
    description=(
        "Return the persisted pitch-level play-by-play (one entry per pitch, the "
        "resolved PA event on the terminal pitch) for the game's most-recent "
        "persisted run -- the durable backing populated by /simulate (SIM-357). "
        "Served straight from the DuckDB play-stream store (SIM-356); numpy-free "
        "JSON (SIM-350). SIM-415: optional ``limit``/``offset`` page the entries "
        "(a full game is ~300 pitches); ``n_pitches``/``n_plate_appearances`` stay "
        "full-game totals and ``total_entries``/``page_*`` describe the slice. "
        "Omitting ``limit`` returns the whole stream (unchanged shape). SIM-561: "
        "the entries come from the newest stored run only (``run_id``), each "
        "carries its inning, half, outs, batter, pitcher and the score after it, "
        "and ``names`` maps every batter and pitcher id to a name. 404 if "
        "nothing has been persisted for the game, 503 if no replay store is wired."
    ),
)
async def get_game_plays(
    game_pk: int,
    request: Request,
    limit: int | None = Query(
        None, ge=1, le=2000, description="Page size; omit to return all entries"
    ),
    offset: int = Query(0, ge=0, description="0-based offset of the entry slice"),
    run_id: int | None = Query(
        None, description="SIM-561: the stored run to read; omit for the newest"
    ),
) -> PlayByPlayModel:
    con = _get_replay_store(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )

    # One complete run only (the newest, unless the caller names one); the
    # store keeps several per game.
    stored_run, base_seed, rows = await asyncio.to_thread(
        _replay_call, con, _read_newest_plays, game_pk=int(game_pk), run_id=run_id
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no persisted plays for game_pk={game_pk}",
        )

    # The whole stream builds first, so n_pitches and n_plate_appearances count
    # the whole game whatever the page.
    pbp = PlayByPlay(entries=[PlayByPlayEntry(**row) for row in rows])
    model = PlayByPlayModel.from_dataclass(pbp)
    model.run_id = stored_run
    model.base_seed = base_seed
    model.names = {
        str(pid): name
        for pid, name in (await _replay_player_names(request, _play_player_ids(rows))).items()
    }

    if limit is None:
        return model  # the whole stream, unpaged

    total = len(model.entries)
    model.entries = model.entries[offset : offset + limit]
    model.total_entries = total
    model.page_offset = offset
    model.page_limit = limit
    model.returned_entries = len(model.entries)
    return model


@router.get(
    "/{game_pk}/state/{at_bat}/{pitch}",
    response_model=StateAtPitchModel,
    summary="Field/state snapshot as of a given at-bat/pitch",
    description=(
        "Return the point-in-time field/baserunner/count snapshot as of the "
        "given (at_bat, pitch) for the game's most-recent persisted run -- the "
        "StateAtPitch persisted by /simulate (SIM-357), served from the DuckDB "
        "state-snapshot store. numpy-free JSON (SIM-350). 404 if no snapshot was "
        "persisted for that pitch, 503 if no replay store is wired."
    ),
)
async def get_game_state_at_pitch(
    game_pk: int,
    at_bat: int,
    pitch: int,
    request: Request,
) -> StateAtPitchModel:
    con = _get_replay_store(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )

    row = await asyncio.to_thread(
        _replay_call,
        con,
        _read_newest_state,
        game_pk=int(game_pk),
        at_bat=int(at_bat),
        pitch=int(pitch),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"no persisted state for game_pk={game_pk} at_bat={at_bat} pitch={pitch}"),
        )

    # ``row["snapshot"]`` is the stored StateAtPitch dict; the rebuild needs no
    # replay of the game.
    return _state_at_pitch_model_from_snapshot(row["snapshot"])


# ===========================================================================
# GET /linescore, /decisions and /card: the stored game card
# ===========================================================================


async def _load_game_card_or_error(request: Request, game_pk: int) -> dict:
    """Load the newest stored game card; 503 with no store, 404 with no card.

    /linescore, /decisions and /card share it. The 404 means nothing is stored
    for the game (no run, or the store skipped the card). Returns the parsed
    ``{run_id, game_pk, linescore, decisions, base_seed}`` dict.
    """
    con = _get_replay_store(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )
    # The newest card: the same run that /plays and /state read.
    card = await asyncio.to_thread(
        _replay_call, con, sim_store.load_game_card, game_pk=int(game_pk)
    )
    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no persisted game card for game_pk={game_pk}",
        )
    return card


@router.get(
    "/{game_pk}/linescore",
    response_model=LinescoreModel,
    summary="Per-inning linescore (R/H/E grid) for a simulated game",
    description=(
        "Return the persisted per-inning linescore (the away/home run grid plus "
        "each team's Runs/Hits/Errors totals, SIM-362) for the game's most-recent "
        "persisted run -- derived at record time from the recorded PlayResult "
        "stream by /simulate and served from the DuckDB game-card store. "
        "numpy-free JSON (SIM-350). 404 if no card has been persisted, 503 if no "
        "replay store is wired."
    ),
)
async def get_game_linescore(
    game_pk: int,
    request: Request,
) -> LinescoreModel:
    card = await _load_game_card_or_error(request, game_pk)
    return LinescoreModel.from_jsonable(card["linescore"])


@router.get(
    "/{game_pk}/decisions",
    response_model=PitcherDecisionsModel,
    summary="Winning/losing/save pitcher decisions for a simulated game",
    description=(
        "Return the persisted W/L/Save pitcher decisions (SIM-364) for the game's "
        "most-recent persisted run -- derived at record time from the recorded "
        "PlayResult stream by /simulate and served from the DuckDB game-card "
        "store. All three pitcher ids are null on a tie / no-decision. numpy-free "
        "JSON. 404 if no card has been persisted, 503 if no replay store is wired."
    ),
)
async def get_game_decisions(
    game_pk: int,
    request: Request,
) -> PitcherDecisionsModel:
    card = await _load_game_card_or_error(request, game_pk)
    return PitcherDecisionsModel.from_jsonable(card["decisions"])


@router.get(
    "/{game_pk}/card",
    response_model=GameCardResponse,
    summary="Combined linescore + pitcher decisions for a simulated game",
    description=(
        "Return BOTH the per-inning linescore (SIM-362) and the W/L/Save pitcher "
        "decisions (SIM-364) for the game's most-recent persisted run in one "
        "payload, from the DuckDB game-card store. 404 if no card has been "
        "persisted, 503 if no replay store is wired."
    ),
)
async def get_game_card(
    game_pk: int,
    request: Request,
) -> GameCardResponse:
    card = await _load_game_card_or_error(request, game_pk)
    return GameCardResponse(
        game_pk=int(game_pk),
        linescore=LinescoreModel.from_jsonable(card["linescore"]),
        decisions=PitcherDecisionsModel.from_jsonable(card["decisions"]),
        run_id=card.get("run_id"),
        base_seed=card.get("base_seed"),
    )


# ===========================================================================
# GET /boxscore: every player's prop means over N games
# ===========================================================================


def _build_prop_set(
    *,
    runner: BatchRunner,
    spec: GameSpec,
    n_iterations: int,
    base_seed: int | None,
) -> PropDistributionSet:
    """Build the run's :class:`PropDistributionSet` on the batch runner.

    The runner fans the N games out to its worker pool, the pool ``/simulate``
    uses, and caches a seeded set. So the projections card and every prop the
    user then clicks read one run (the projections on the worker pool,
    SIM-560). Sync and CPU-bound; the caller runs it in a worker thread.
    """
    return runner.run_prop_set(spec, n_iterations=n_iterations, base_seed=base_seed)


@dataclasses.dataclass
class _PlayerTag:
    """Where one player sits in the simulated game.

    ``side`` is 'away' or 'home'. ``lineup_slot`` is the 1-9 batting-order
    slot, or None for a player who does not bat. ``starting_pitcher`` marks
    each side's starter; a pitcher who also bats keeps his slot.
    """

    side: str
    lineup_slot: int | None = None
    starting_pitcher: bool = False


def _placeholder_pens(sim_kwargs: Mapping[str, Any]) -> dict[int, list[int]]:
    """The factory's placeholder pen for this game, keyed 0 = away and 1 = home.

    A game with no pen in the box (a scheduled game: only the historical loader
    writes ``raw.game_bullpen``) pitches these arms. Their ids are negative and
    built from each starter's id, so the function asks the factory's own builder.
    """
    return _default_bullpen_for_spec(GameSpec(sim_kwargs=dict(sim_kwargs)))


def _placeholder_names(sim_kwargs: Mapping[str, Any]) -> dict[int, str]:
    """A label for each placeholder arm: 'Generic reliever 1' to '6' per side."""
    return {
        pid: f"Generic reliever {n}"
        for pen in _placeholder_pens(sim_kwargs).values()
        for n, pid in enumerate(pen, start=1)
    }


def _player_tags(sim_kwargs: Mapping[str, Any]) -> dict[int, _PlayerTag]:
    """Tag every player the sim can use with a side, a slot and a role.

    The sim kwargs name the game's players: the two batting orders, the two
    starters, and each side's pen (``bullpen``, keyed 0 = away and 1 = home). A
    game with no pen in the box pitches the factory's placeholder arms instead
    (:func:`_placeholder_pens`). The loop pinch-hits nobody, so these are every
    player a box score can hold.
    """
    tags: dict[int, _PlayerTag] = {}

    def _tag(pid: Any, side: str) -> _PlayerTag | None:
        if pid is None:
            return None
        return tags.setdefault(int(pid), _PlayerTag(side=side))

    for side in ("away", "home"):
        for slot, pid in enumerate(sim_kwargs.get(f"{side}_lineup") or [], start=1):
            tag = _tag(pid, side)
            if tag is not None:
                tag.lineup_slot = slot
        starter = _tag(sim_kwargs.get(f"{side}_pitcher_id"), side)
        if starter is not None:
            starter.starting_pitcher = True
    pens = list((sim_kwargs.get("bullpen") or {}).items())
    pens += list(_placeholder_pens(sim_kwargs).items())
    for team, pen in pens:
        side = "home" if int(team) == 1 else "away"
        for pid in pen or []:
            _tag(pid, side)
    return tags


_PLAYER_NAMES_SQL = """
    SELECT player_id, full_name
    FROM   raw.players
    WHERE  player_id = ANY($1::int[])
"""


async def _query_player_names(pool: Any, player_ids: list[int]) -> dict[int, str]:
    """Read each player's full name from ``raw.players``."""
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_PLAYER_NAMES_SQL, player_ids)
    else:
        rows = await pool.fetch(_PLAYER_NAMES_SQL, player_ids)
    names: dict[int, str] = {}
    for row in rows:
        name = _row_get(row, "full_name")
        if name:
            names[int(_row_get(row, "player_id"))] = str(name)
    return names


async def _player_names(pool: Any, player_ids: list[int]) -> dict[int, str]:
    """The players' names, or an empty map when the lookup fails.

    A name is a label. A failed lookup leaves the card's names empty and the
    panel shows the player id instead; it never fails the card.
    """
    if not player_ids:
        return {}
    try:
        return await _query_player_names(pool, player_ids)
    except Exception as exc:  # noqa: BLE001 -- a label must not break the card
        log.warning("player-name lookup failed: %s", exc)
        return {}


@router.get(
    "/{game_pk}/boxscore",
    response_model=BoxscoreCardModel,
    summary="Per-player boxscore-average card (prop means over N iterations)",
    description=(
        "Resolve the game's lineup, run an N-iteration boxscore batch, and return "
        "each player's prop MEANS as a boxscore card (SIM-366): for a batter the "
        "H/HR/RBI/TB/1B/2B/3B/R/SB/HRR means, for a pitcher the "
        "K/BB/ER/OUTS/H_ALLOWED means (SIM-421 added the market's other lines) "
        "-- the means-only projection of the run's PropDistributionSet (SIM-329). "
        "Each row also carries the player's name, side, batting-order slot and "
        "starting-pitcher flag (SIM-560). The games run on the worker pool, and a "
        "seeded run is cached, so /props with the same seed and N reads this run. "
        "With the replay store on, the run's first game is stored for /linescore "
        "and /plays (SIM-561). "
        "numpy-free JSON "
        "(SIM-350). 503 if no DB pool is attached or the game's lineup is not yet "
        "usable (Retry-After); 404 if the game is unknown."
    ),
)
async def get_game_boxscore(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(100, ge=1, le=2000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(None, description="Reproducibility seed for the batch"),
    run_id: int | None = Query(None, description="SIM-519: read this stored run, no batch"),
    latest_run: bool = Query(False, description="SIM-519: read the game's newest done run"),
) -> BoxscoreCardModel:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    spec = GameSpec(
        machine_factory=resolve_factory_ref(request),
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    if run_id is not None or latest_run:
        # The panel reads the stored run; no second batch.
        stored, row = await _stored_prop_set(request, int(game_pk), run_id)
        names = _placeholder_names(spec.sim_kwargs)
        names.update(await _player_names(pool, sorted(pid for pid in stored.by_player if pid > 0)))
        return BoxscoreCardModel.from_prop_set(
            stored,
            base_seed=row.get("base_seed"),
            names=names,
            tags={
                pid: dataclasses.asdict(tag) for pid, tag in _player_tags(spec.sim_kwargs).items()
            },
        )
    # Store the run's first game for the linescore and play-by-play panels
    # while the pool runs the batch. Game 0's seed is base_seed itself
    # (derive_seed(base_seed, 0)), so the panels show one of these games. A store
    # failure never breaks the card; with the replay store off it returns at once.
    pset, _run_id = await asyncio.gather(
        asyncio.to_thread(
            _build_prop_set,
            runner=_build_runner(request),
            spec=spec,
            n_iterations=n_iterations,
            base_seed=base_seed,
        ),
        _persist_replay_artifacts(
            request,
            game_pk=int(game_pk),
            factory_ref=str(spec.machine_factory),
            base_seed=base_seed,
            sim_kwargs=spec.sim_kwargs,
        ),
    )
    names = _placeholder_names(spec.sim_kwargs)
    names.update(await _player_names(pool, sorted(pid for pid in pset.by_player if pid > 0)))
    return BoxscoreCardModel.from_prop_set(
        pset,
        base_seed=base_seed,
        names=names,
        tags={pid: dataclasses.asdict(tag) for pid, tag in _player_tags(spec.sim_kwargs).items()},
    )


# ---------------------------------------------------------------------------
# GET /props/{player_id}/{prop}: one player's prop distribution and edge
# ---------------------------------------------------------------------------

#: Every valid prop name (pitcher and batter). Any other name is a 422, not a
#: 404: the prop name is a constant of the model, not a per-game id.
_VALID_PROP_NAMES: frozenset[str] = frozenset(ALL_PROPS)


@router.get(
    "/{game_pk}/props/{player_id}/{prop}",
    response_model=PropEdgeResponse,
    summary="Player prop PMF + optional edge report (SIM-390)",
    description=(
        "Run an N-iteration Monte-Carlo batch for the game, look up one player's "
        "prop PMF (e.g. K, BB, H, HR), and return the full integer-support PMF.  "
        "Supply ``line`` to also get ``p_over``/``p_under``/``p_push``; supply "
        "``line``, ``over_ml``, and ``under_ml`` to also get a full "
        "``edge_report`` via the CLV engine (SIM-339).  ``bet_side`` controls "
        "which side's edge is computed (``'over'`` or ``'under'``, default "
        "``'over'``).  Valid props (SIM-421 added the market's other lines): "
        "K, BB, ER, OUTS, H_ALLOWED (pitcher); H, HR, RBI, TB, 1B, 2B, 3B, R, "
        "SB, HRR (batter; HRR = hits + runs + RBI as one per-game sum).  503 if "
        "no DB pool is attached; 404 if the lineup or player cannot be resolved."
    ),
)
async def get_player_prop_edge(
    game_pk: int,
    player_id: int,
    prop: str,
    request: Request,
    n_iterations: int = Query(100, ge=1, le=2000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(None, description="Reproducibility seed for the batch"),
    line: float | None = Query(None, description="Prop line for over/under calculation"),
    over_ml: float | None = Query(None, description="American odds for the OVER side"),
    under_ml: float | None = Query(None, description="American odds for the UNDER side"),
    bet_side: str = Query("over", description="Side to compute edge for ('over' or 'under')"),
    run_id: int | None = Query(None, description="SIM-519: read this stored run, no batch"),
    latest_run: bool = Query(False, description="SIM-519: read the game's newest done run"),
) -> PropEdgeResponse:
    # ---- the parameters ----
    prop_upper = prop.upper()
    if prop_upper not in _VALID_PROP_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Unknown prop '{prop}'. Valid props: " + ", ".join(sorted(_VALID_PROP_NAMES))),
        )
    if bet_side not in ("over", "under"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bet_side must be 'over' or 'under'",
        )
    if (over_ml is not None or under_ml is not None) and line is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="over_ml/under_ml require a line to be supplied",
        )

    # ---- the prop set (the /boxscore path; a seeded run is cached) ----
    if run_id is not None or latest_run:
        # The stored run's distributions; no batch.
        pset, _row = await _stored_prop_set(request, int(game_pk), run_id)
    else:
        pool = _get_pool(request)
        state = await _resolve_state_or_error(pool, game_pk)
        spec = GameSpec(
            machine_factory=resolve_factory_ref(request),
            sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
        )
        pset = await asyncio.to_thread(
            _build_prop_set,
            runner=_build_runner(request),
            spec=spec,
            n_iterations=n_iterations,
            base_seed=base_seed,
        )

    # ---- the requested player and prop ----
    dist = pset.get(int(player_id), prop_upper)
    if dist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"player_id {player_id} did not appear in the sim run "
                f"or has no '{prop_upper}' prop (check that the player "
                f"actually pitched/batted in this game's lineup)."
            ),
        )

    # ---- the over / under probabilities, when a line is given ----
    p_over_val: float | None = None
    p_under_val: float | None = None
    p_push_val: float | None = None
    if line is not None:
        p_over_val = float(dist.p_over(float(line)))
        p_under_val = float(dist.p_under(float(line)))
        p_push_val = float(dist.p_push(float(line)))

    # ---- the edge report, when the line and both prices are given ----
    edge_rpt: EdgeReportModel | None = None
    if line is not None and over_ml is not None and under_ml is not None:
        mkt_side = MarketSide.OVER if bet_side == "over" else MarketSide.UNDER
        market = TwoWayMarket(
            side=mkt_side,
            entry=OddsQuote(
                side=float(over_ml) if bet_side == "over" else float(under_ml),
                other=float(under_ml) if bet_side == "over" else float(over_ml),
                line=float(line),
            ),
        )
        try:
            raw_report = _prop_edge_report(dist, market, side=mkt_side)
            edge_rpt = EdgeReportModel.from_dataclass(raw_report)
        except Exception as exc:  # noqa: BLE001 -- degenerate odds give no report
            log.warning("prop_edge_report failed for %d/%s: %s", player_id, prop_upper, exc)

    # The player's name for the chart header; a failed lookup leaves None.
    player_name = None
    if int(player_id) > 0:
        name_pool = getattr(request.app.state, "pg_pool", None)
        if name_pool is not None:
            player_name = (await _player_names(name_pool, [int(player_id)])).get(int(player_id))

    return PropEdgeResponse(
        player_id=int(dist.player_id),
        prop=str(dist.prop),
        n=int(dist.n),
        player_name=player_name,
        support=[int(v) for v in dist.support.tolist()],
        probabilities=[float(p) for p in dist.probabilities.tolist()],
        mean=float(dist.mean),
        median=float(dist.median),
        std=float(dist.std),
        pmf={str(int(v)): float(p) for v, p in dist.pmf().items()},
        line=None if line is None else float(line),
        p_over=p_over_val,
        p_under=p_under_val,
        p_push=p_push_val,
        edge_report=edge_rpt,
    )


__all__ = [
    "router",
    "PRODUCTION_FACTORY_REF",
    "resolve_factory_ref",
]
