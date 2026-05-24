"""
api/routes/games.py
===================
Phase-5 Game Simulation endpoints (Sprint 2).

Routes (prefix ``/api/games``):

    GET  /api/games/{date}                          (SIM-355)
        list the games scheduled on a date (YYYY-MM-DD), read from raw.games.

    GET  /api/games/{game_pk}/simulate              (SIM-355)
        resolve the game's lineup -> GameState -> run an N-iteration
        Monte-Carlo batch -> return the GameSimSummary (numpy-free JSON).

    POST /api/games/{game_pk}/simulate/with_override (SIM-358)
        run a BASELINE sim (resolved lineup) AND an OVERRIDE sim (a modified
        roster) at the SAME base seed, and return both summaries plus the
        baseline-vs-override OverrideDelta.

DESIGN -- thin handlers over existing seams
-------------------------------------------
This router is intentionally thin and mirrors ``api/routes/similarity.py``:
it reads resources off ``request.app.state`` (the asyncpg pool + an optional
sim cache), delegates lineup resolution to ``simulation.lineup_resolver``
(SIM-353), the Monte-Carlo run to ``simulation.batch_runner.BatchRunner``
(SIM-332), the override diff to ``simulation.snapshots.OverrideDelta`` (SIM-331),
and JSON serialization to ``api.schemas`` (SIM-350) -- so NO numpy ever reaches
the wire and this file owns only the HTTP contract.

THE FACTORY-REF TESTABILITY SEAM (SIM-355)
------------------------------------------
A simulated game needs a ``StateMachine`` factory.  The PRODUCTION factory
builds a sampler over a live DuckDB we do not have in unit tests, so the dotted
factory ref is **overridable** via :func:`resolve_factory_ref`, in precedence:

    1. ``request.app.state.sim_factory_ref``      (explicit per-app override)
    2. ``$SIM_MACHINE_FACTORY_REF``               (env override)
    3. module-level :data:`PRODUCTION_FACTORY_REF` (the production default)

A unit test monkeypatches :data:`PRODUCTION_FACTORY_REF` (or sets
``app.state.sim_factory_ref``) to the no-DB
``simulation.batch_runner:rng_driven_machine_factory`` so ``/simulate`` runs a
real (fast, no-DB) batch with no live sampler.

CACHING (SIM-359)
-----------------
Two layers, both no-op-safe optimizations (never a correctness requirement):
  * ``BatchRunner``'s OWN SimCache memoizes a summary keyed on
    (spec + base seed + N) at SIM_RESULT_TTL_S (60s).  The runner is built with
    the app's cache (``request.app.state.sim_cache``) when present, else its
    default ``make_cache()`` (Redis-if-reachable, else in-memory).
  * the date listing (a pool read) is memoized at POOL_QUERY_TTL_S (300s) when a
    sim cache is attached.
Caching is disabled for a request via ``?use_cache=false`` (tests use this to
prove a second call still works on both the cached and uncached path).

Owner: Backend Developer (SIM-355 / SIM-358 / SIM-359).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from datetime import date as _date
from datetime import datetime
from typing import Any, Mapping, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.schemas import (
    GameSimSummaryModel,
    OverrideDeltaModel,
    PlayByPlayModel,
    StateAtPitchModel,
)
from api.serialization import to_jsonable
from db import sim_store
from simulation.batch_runner import (
    POOL_QUERY_TTL_S,
    BatchRunner,
    GameSpec,
)
from simulation.lineup_resolver import (
    LineupResolutionError,
    resolve_game_state,
)
from simulation.play_recorder import record_game_plays
from simulation.snapshots import (
    FieldSnapshot,
    OverrideDelta,
    PlayByPlay,
    PlayByPlayEntry,
    PlayerRef,
    StateAtPitch,
)

log = logging.getLogger("api.routes.games")

router = APIRouter(prefix="/api/games", tags=["games"])


# ---------------------------------------------------------------------------
# The factory-ref testability seam (SIM-355)
# ---------------------------------------------------------------------------

#: The PRODUCTION machine-factory dotted ref.  Builds a sampler over the live
#: DuckDB -- NOT available in unit tests.  Tests monkeypatch this module-level
#: default (or set ``app.state.sim_factory_ref``) to the no-DB rng factory
#: ``"simulation.batch_runner:rng_driven_machine_factory"``.
PRODUCTION_FACTORY_REF = "simulation.production_factory:production_machine_factory"


def resolve_factory_ref(request: Request) -> str:
    """The machine-factory dotted ref for this request (the testability seam).

    Precedence: ``app.state.sim_factory_ref`` -> ``$SIM_MACHINE_FACTORY_REF`` ->
    the module-level :data:`PRODUCTION_FACTORY_REF`.  See the module docstring.
    """
    override = getattr(request.app.state, "sim_factory_ref", None)
    if override:
        return str(override)
    env_ref = os.environ.get("SIM_MACHINE_FACTORY_REF")
    if env_ref:
        return env_ref
    return PRODUCTION_FACTORY_REF


# ---------------------------------------------------------------------------
# app.state accessors (mirror similarity.py's get_* dependencies)
# ---------------------------------------------------------------------------


def _get_pool(request: Request) -> Any:
    """The asyncpg pool, or 503 if the lifespan has not attached one.

    Mirrors ``similarity.get_pitcher_engine``'s 503-on-missing posture: a route
    that needs the DB fails loudly rather than 500-ing on an attribute error.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database pool unavailable",
        )
    return pool


def _get_sim_cache(request: Request) -> Any:
    """The optional sim cache attached to app.state, or None.

    ``None`` means "let BatchRunner pick its own cache backend"; the route never
    hard-depends on a cache (SIM-359: caching is a no-op-safe optimization).
    """
    return getattr(request.app.state, "sim_cache", None)


def _get_sim_duckdb(request: Request) -> Any:
    """The optional sim DuckDB connection on app.state, or None (SIM-357).

    The play-stream + state-snapshot stores (``db.sim_store``) are DuckDB-backed;
    the lifespan attaches a live ``duckdb`` connection as ``app.state.sim_duckdb``
    when available.  ``None`` means "no replay store wired" -> persistence is
    skipped silently and the /plays + /state reads 404.  Like the cache, this is
    a best-effort optimization: a missing store never breaks /simulate.
    """
    return getattr(request.app.state, "sim_duckdb", None)


def _build_runner(request: Request) -> BatchRunner:
    """The BatchRunner for this request -- the shared one if present (SIM-360).

    SIM-360: when the lifespan has attached a long-lived ``app.state.sim_runner``
    (a persistent BatchRunner that REUSES one warm ProcessPoolExecutor + the
    SIM-333 shared-mem segments across requests), reuse it rather than building a
    fresh runner per request -- so a long-lived API does not pay the fork +
    publish/unlink cost on every ``/simulate``.  The runner's OWN SimCache still
    memoizes summaries at SIM_RESULT_TTL_S, and the heavy run is offloaded to a
    worker thread by the caller via ``asyncio.to_thread``.

    FALLBACK (SIM-359): if no shared runner is attached (e.g. a unit test that
    builds the app without entering the lifespan), build a transient one wired
    with the app's sim cache when present (so a repeat matchup/seed/N hits the
    memoized summary), else its own ``make_cache()``.  ``max_workers=1`` runs the
    batch synchronously in THIS process -- no fork, no pickling -- the fast,
    deterministic test path.
    """
    shared = getattr(request.app.state, "sim_runner", None)
    if shared is not None:
        return shared
    cache = _get_sim_cache(request)
    return BatchRunner(cache=cache, max_workers=1)


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class GameCard(BaseModel):
    """One row of ``GET /api/games/{date}`` -- a scheduled game's identity."""

    game_pk: int
    season: int
    game_date: str
    status: Optional[str] = None
    home_team_id: Optional[int] = None
    away_team_id: Optional[int] = None
    venue_id: Optional[int] = None


class GamesOnDateResponse(BaseModel):
    """The ``GET /api/games/{date}`` envelope: the date echo + its game cards."""

    date: str
    count: int
    games: list[GameCard] = Field(default_factory=list)


class SimulateResponse(BaseModel):
    """The ``GET /api/games/{game_pk}/simulate`` envelope."""

    game_pk: int
    n_iterations: int
    base_seed: Optional[int] = None
    from_cache: bool = False
    summary: GameSimSummaryModel


class RosterOverride(BaseModel):
    """The ``POST .../simulate/with_override`` request body (SIM-358).

    Every field is OPTIONAL; an unset field keeps the resolved (baseline) value.
    At least one field SHOULD be set for the override to differ from baseline,
    but an all-empty body is accepted (it yields an all-zero delta -- a valid,
    if uninteresting, comparison).

      * ``home_lineup`` / ``away_lineup`` -- full 1..9 batting orders (player
        ids) to swap in for that side.
      * ``pitcher_id`` -- the pitcher to face the batters for the simulated half.
      * ``bat_hand`` -- the leadoff batter's hand override ('L'/'R'/'S'); the
        sampler pre-filter key.
      * ``description`` -- free text recorded on the returned OverrideDelta.
    """

    home_lineup: Optional[list[int]] = None
    away_lineup: Optional[list[int]] = None
    pitcher_id: Optional[int] = None
    bat_hand: Optional[str] = Field(default=None, max_length=1)
    description: Optional[str] = None


class WithOverrideResponse(BaseModel):
    """The ``POST .../simulate/with_override`` envelope (SIM-358).

    Carries the BASELINE summary, the OVERRIDE summary (both run at the SAME
    base seed for an apples-to-apples comparison), and the OverrideDelta diff.
    """

    game_pk: int
    n_iterations: int
    base_seed: Optional[int] = None
    baseline: GameSimSummaryModel
    override: GameSimSummaryModel
    delta: OverrideDeltaModel


# ---------------------------------------------------------------------------
# Helpers -- sim_kwargs assembly from a resolved GameState
# ---------------------------------------------------------------------------


def _sim_kwargs_from_state(state: Any) -> dict[str, Any]:
    """Build the ``simulate_game`` kwargs from a resolved GameState (SIM-353).

    Pulls the lineup contract off the GameState's public fields (the same fields
    ``lineup_resolver.build_game_state`` populated) into the ``sim_kwargs`` dict
    the BatchRunner / ``simulate_game`` consume.
    """
    return {
        "away_lineup": list(getattr(state, "away_lineup", []) or []),
        "home_lineup": list(getattr(state, "home_lineup", []) or []),
        "season": int(getattr(state, "season", 2024)),
        "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
        "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
        "k": int(getattr(state, "k", 25) or 25) if hasattr(state, "k") else 25,
        "max_innings": 12,
    }


def _apply_override(base_kwargs: dict[str, Any], override: RosterOverride) -> dict[str, Any]:
    """Return a COPY of ``base_kwargs`` with the override's set fields applied.

    Only fields the caller actually set (non-None) replace the baseline value;
    everything else is carried through unchanged so a single-field override
    (e.g. just the pitcher) leaves both lineups intact.
    """
    kw = dict(base_kwargs)
    if override.home_lineup is not None:
        kw["home_lineup"] = [int(x) for x in override.home_lineup]
    if override.away_lineup is not None:
        kw["away_lineup"] = [int(x) for x in override.away_lineup]
    if override.pitcher_id is not None:
        kw["pitcher_id"] = int(override.pitcher_id)
    if override.bat_hand is not None:
        kw["bat_hand"] = str(override.bat_hand)
    return kw


async def _resolve_state_or_error(pool: Any, game_pk: int) -> Any:
    """Resolve a game's GameState via SIM-353, mapping failures to HTTP errors.

    A connection is acquired from the pool (asyncpg-style ``async with
    pool.acquire()``); a pool that IS a connection (the mock-pool test idiom)
    is used directly.  A ``LineupResolutionError`` (unknown game / no lineup) is
    surfaced as 404, not 500.
    """
    acquire = getattr(pool, "acquire", None)
    try:
        if acquire is not None:
            async with pool.acquire() as conn:
                return await resolve_game_state(conn, game_pk)
        # The pool itself exposes fetch/fetchrow (mock-pool / direct-conn path).
        return await resolve_game_state(pool, game_pk)
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
    base_seed: Optional[int],
    use_cache: bool,
):
    """Run the Monte-Carlo batch (a sync call -- offloaded to a worker thread).

    Wraps ``BatchRunner.run`` so the route can ``await asyncio.to_thread(...)``
    it, keeping the event loop responsive during the CPU-bound game loop.
    """
    return runner.run(
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )


# ---------------------------------------------------------------------------
# StateAtPitch rebuild (SIM-357) -- stored jsonable dict -> response model
# ---------------------------------------------------------------------------


def _player_ref_from_jsonable(ref: Any) -> Optional[PlayerRef]:
    """Rebuild an Optional[PlayerRef] from its jsonable dict (None == empty)."""
    if ref is None:
        return None
    return PlayerRef(
        player_id=int(ref["player_id"]),
        label=None if ref.get("label") is None else str(ref["label"]),
    )


def _state_at_pitch_model_from_snapshot(snapshot: Mapping[str, Any]) -> StateAtPitchModel:
    """Rebuild a :class:`StateAtPitchModel` from a stored jsonable StateAtPitch.

    The stored ``snapshot`` is ``to_jsonable(StateAtPitch)`` -- a dict
    ``{at_bat, pitch, sequence, field}`` whose ``field`` mirrors the
    :class:`~simulation.snapshots.FieldSnapshot` dataclass FIELDS only (the
    derived ``occupied_bases`` / ``runners_on`` properties are NOT serialized by
    ``to_jsonable``).  So we reconstruct the real dataclasses (which recompute
    those properties) and round-trip through ``StateAtPitchModel.from_dataclass``
    -- the same converter the live snapshot path uses -- guaranteeing the wire
    shape is identical whether served fresh or from the store.
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
        sequence=(
            None if snapshot.get("sequence") is None else int(snapshot["sequence"])
        ),
    )
    return StateAtPitchModel.from_dataclass(sap)


# ---------------------------------------------------------------------------
# Record -> persist flow (SIM-357) -- best-effort, never breaks /simulate
# ---------------------------------------------------------------------------


def _record_and_build(
    *,
    factory_ref: str,
    base_seed: Optional[int],
    sim_kwargs: dict[str, Any],
) -> "tuple[PlayByPlay, list[dict]]":
    """Record ONE representative game and build the replay artifacts (sync).

    Replays a single game at the run's ``base_seed`` via
    :func:`simulation.play_recorder.record_game_plays` (the no-DB rng path under
    the test/factory seam), then derives:

      * a :class:`~simulation.snapshots.PlayByPlay` (the /plays scroll), and
      * one jsonable :class:`~simulation.snapshots.StateAtPitch` per pitch (the
        /state lookup rows), tagged with each pitch's at_bat / pitch / sequence
        from the matching PlayByPlay entry and built from that ``PlayResult``'s
        committed ``next_state`` (the GameState after the pitch).

    Returns ``(play_by_play, state_snapshot_dicts)``.  A pitch whose
    ``next_state`` is missing is skipped for the state stream (its /plays entry
    still persists) -- the snapshot is best-effort and a missing one just 404s.
    Pure + sync so the caller can run it in the same worker thread as the batch.
    """
    _result, plays = record_game_plays(
        factory_ref=factory_ref,
        seed=base_seed,
        sim_kwargs=sim_kwargs,
    )
    pbp = PlayByPlay.from_play_results(plays)
    # plays and pbp.entries are 1:1 in order, so zip pairs each PlayResult with
    # its entry's (at_bat, pitch, sequence) indices.
    snapshots: list[dict] = []
    for play, entry in zip(plays, pbp.entries):
        next_state = getattr(play, "next_state", None)
        if next_state is None:
            continue
        sap = StateAtPitch.from_game_state(
            next_state,
            at_bat=entry.at_bat,
            pitch=entry.pitch,
            sequence=entry.sequence,
        )
        snapshots.append(to_jsonable(sap))
    return pbp, snapshots


async def _persist_replay_artifacts(
    request: Request,
    *,
    game_pk: int,
    factory_ref: str,
    base_seed: Optional[int],
    sim_kwargs: dict[str, Any],
    batch: Any,
) -> None:
    """Best-effort persist of the /plays + /state replay artifacts (SIM-357).

    After a batch runs, this records ONE representative game (at the run's
    ``base_seed``), persists its play-stream + per-pitch state snapshots to the
    DuckDB store, and persists the run's GameSimSummary to the Postgres sim-run
    history (SIM-356) so /plays + /state have something to read.

    EVERYTHING here is wrapped so a persistence failure NEVER breaks the
    /simulate response: a missing DuckDB store skips the DuckDB writes silently,
    a missing pg pool skips the history write, and any exception is swallowed
    (logged at warning).  The cross-store ``run_id`` is taken from the Postgres
    insert when available; without a pool we fall back to a synthetic run_id so
    the DuckDB stream is still self-consistent and queryable by game_pk.
    """
    con = _get_sim_duckdb(request)
    pool = getattr(request.app.state, "pg_pool", None)

    # Nothing to write the replay stream to -> skip entirely (the /plays + /state
    # reads will 404, which is the documented "nothing persisted" behaviour).
    if con is None:
        return

    try:
        # 1) Record the representative game + build artifacts (CPU-bound: thread).
        pbp, snapshots = await asyncio.to_thread(
            _record_and_build,
            factory_ref=factory_ref,
            base_seed=base_seed,
            sim_kwargs=sim_kwargs,
        )

        # 2) Persist the run summary to Postgres (SIM-356) to get a durable
        #    run_id; without a pool, use a synthetic run_id so the DuckDB stream
        #    is still grouped + queryable by game_pk.
        run_id: int
        if pool is not None:
            try:
                summary_json = to_jsonable(batch.summary)
                acquire = getattr(pool, "acquire", None)
                if acquire is not None:
                    async with pool.acquire() as conn:
                        run_id = await sim_store.store_sim_run(
                            conn,
                            game_pk=game_pk,
                            summary=summary_json,
                            n_iterations=int(batch.n_iterations),
                            base_seed=base_seed,
                        )
                else:
                    run_id = await sim_store.store_sim_run(
                        pool,
                        game_pk=game_pk,
                        summary=summary_json,
                        n_iterations=int(batch.n_iterations),
                        base_seed=base_seed,
                    )
            except Exception as exc:  # noqa: BLE001 -- history write is optional
                log.warning("sim-run history persist failed for game %s: %s", game_pk, exc)
                run_id = 0 if base_seed is None else int(base_seed)
        else:
            run_id = 0 if base_seed is None else int(base_seed)

        # 3) Persist the DuckDB play-stream + state snapshots (sync calls).
        play_rows = [dataclasses.asdict(e) for e in pbp.entries]
        sim_store.store_play_stream(
            con, game_pk=game_pk, run_id=run_id, play_entries=play_rows
        )
        sim_store.store_state_snapshots(
            con, game_pk=game_pk, run_id=run_id, snapshots=snapshots
        )
    except Exception as exc:  # noqa: BLE001 -- persistence must never break /simulate
        log.warning("replay-artifact persist failed for game %s: %s", game_pk, exc)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_GAMES_ON_DATE_SQL = """
    SELECT game_pk, season, game_date, status,
           home_team_id, away_team_id, venue_id
    FROM   raw.games
    WHERE  game_date = $1
    ORDER  BY game_pk
"""


def _parse_date(date_str: str) -> _date:
    """Parse a ``YYYY-MM-DD`` path param, 422 on a malformed value.

    FastAPI does not coerce a path str to a date for us here (the param is a
    plain str so the same path can also match a numeric game_pk on the sibling
    routes), so we validate explicitly and raise 422 on a bad format -- the same
    status FastAPI uses for a validation error.
    """
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        # 422 (the FastAPI validation-error status). Used as a literal int rather
        # than status.HTTP_422_* because that constant's name changed across
        # Starlette versions (ENTITY -> CONTENT); the numeric code is stable.
        raise HTTPException(
            status_code=422,
            detail=f"invalid date {date_str!r}; expected YYYY-MM-DD",
        ) from exc


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an asyncpg Record or a plain dict uniformly."""
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def _game_card(row: Any) -> GameCard:
    """Map one raw.games row (Record or dict) to a GameCard."""
    gd = _row_get(row, "game_date")
    # game_date may be a date/datetime (asyncpg) or an ISO string (canned rows).
    game_date = gd.isoformat() if hasattr(gd, "isoformat") else str(gd)
    return GameCard(
        game_pk=int(_row_get(row, "game_pk")),
        season=int(_row_get(row, "season", 0) or 0),
        game_date=game_date,
        status=(None if _row_get(row, "status") is None else str(_row_get(row, "status"))),
        home_team_id=(
            None if _row_get(row, "home_team_id") is None else int(_row_get(row, "home_team_id"))
        ),
        away_team_id=(
            None if _row_get(row, "away_team_id") is None else int(_row_get(row, "away_team_id"))
        ),
        venue_id=(
            None if _row_get(row, "venue_id") is None else int(_row_get(row, "venue_id"))
        ),
    )


# ===========================================================================
# SIM-355 -- GET /api/games/{date}
# ===========================================================================


@router.get(
    "/{date}",
    response_model=GamesOnDateResponse,
    summary="Games scheduled on a date",
    description=(
        "List the games on a date (YYYY-MM-DD) from raw.games. The result is "
        "memoized at POOL_QUERY_TTL_S (300s) when a sim cache is attached "
        "(SIM-359). Returns 503 if no DB pool is attached, 422 on a bad date."
    ),
)
async def get_games_on_date(
    date: str,
    request: Request,
    use_cache: bool = Query(True, description="Consult/populate the listing cache"),
) -> GamesOnDateResponse:
    parsed = _parse_date(date)
    pool = _get_pool(request)
    cache = _get_sim_cache(request)
    cache_key = f"games:on_date:{parsed.isoformat()}"

    # ---- listing cache (POOL_QUERY_TTL_S) -- fast path ----
    if use_cache and cache is not None:
        try:
            cached = cache.get(cache_key)
        except Exception:  # noqa: BLE001 -- a cache hiccup must not break the read
            cached = None
        if cached is not None:
            return GamesOnDateResponse(**cached)

    rows = await pool.fetch(_GAMES_ON_DATE_SQL, parsed)
    games = [_game_card(r) for r in (rows or [])]
    payload = GamesOnDateResponse(date=parsed.isoformat(), count=len(games), games=games)

    # ---- listing cache write-through ----
    if use_cache and cache is not None:
        try:
            cache.set(cache_key, payload.model_dump(), POOL_QUERY_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("listing cache write failed for %s: %s", cache_key, exc)

    return payload


# ===========================================================================
# SIM-355 -- GET /api/games/{game_pk}/simulate
# ===========================================================================


@router.get(
    "/{game_pk}/simulate",
    response_model=SimulateResponse,
    summary="Simulate a game (Monte-Carlo)",
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
    base_seed: Optional[int] = Query(None, description="Reproducibility seed for the whole batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
) -> SimulateResponse:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    spec = GameSpec(
        machine_factory=factory_ref,
        sim_kwargs=_sim_kwargs_from_state(state),
    )
    runner = _build_runner(request)

    # The batch is CPU-bound -- offload to a worker thread so the loop stays free.
    batch = await asyncio.to_thread(
        _run_batch,
        runner,
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    # SIM-357: persist the /plays + /state replay artifacts (record ONE game at
    # the run's base_seed -> play-stream + per-pitch state snapshots + sim-run
    # history).  Best-effort: a persistence failure NEVER breaks this response.
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
# SIM-358 -- POST /api/games/{game_pk}/simulate/with_override
# ===========================================================================


@router.post(
    "/{game_pk}/simulate/with_override",
    response_model=WithOverrideResponse,
    summary="Simulate a game with a roster override (baseline vs override)",
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
    base_seed: Optional[int] = Query(0, description="Shared seed for baseline + override"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
) -> WithOverrideResponse:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    runner = _build_runner(request)

    base_kwargs = _sim_kwargs_from_state(state)
    override_kwargs = _apply_override(base_kwargs, override)

    baseline_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=base_kwargs)
    override_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=override_kwargs)

    # Both batches at the SAME base seed -> the only difference between the two
    # summaries is the roster change (apples-to-apples comparison).
    baseline_batch = await asyncio.to_thread(
        _run_batch, runner, baseline_spec,
        n_iterations=n_iterations, base_seed=base_seed, use_cache=use_cache,
    )
    override_batch = await asyncio.to_thread(
        _run_batch, runner, override_spec,
        n_iterations=n_iterations, base_seed=base_seed, use_cache=use_cache,
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
# SIM-357 -- GET /api/games/{game_pk}/plays
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
        "JSON (SIM-350). 404 if nothing has been persisted for the game, 503 if "
        "no replay store is wired."
    ),
)
async def get_game_plays(
    game_pk: int,
    request: Request,
) -> PlayByPlayModel:
    con = _get_sim_duckdb(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )

    rows = await asyncio.to_thread(
        sim_store.load_play_stream, con, game_pk=int(game_pk)
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no persisted plays for game_pk={game_pk}",
        )

    pbp = PlayByPlay(entries=[PlayByPlayEntry(**row) for row in rows])
    return PlayByPlayModel.from_dataclass(pbp)


# ===========================================================================
# SIM-357 -- GET /api/games/{game_pk}/state/{at_bat}/{pitch}
# ===========================================================================


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
    con = _get_sim_duckdb(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )

    row = await asyncio.to_thread(
        sim_store.load_state_at,
        con,
        game_pk=int(game_pk),
        at_bat=int(at_bat),
        pitch=int(pitch),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no persisted state for game_pk={game_pk} "
                f"at_bat={at_bat} pitch={pitch}"
            ),
        )

    # ``row["snapshot"]`` is the stored, numpy-free jsonable StateAtPitch dict
    # ({at_bat, pitch, field, sequence}).  Rebuild the dataclasses (so the
    # FieldSnapshot's derived occupied_bases / runners_on recompute) and run them
    # through the SAME StateAtPitchModel.from_dataclass converter the live path
    # uses -- no GameState replay needed.
    return _state_at_pitch_model_from_snapshot(row["snapshot"])


__all__ = [
    "router",
    "PRODUCTION_FACTORY_REF",
    "resolve_factory_ref",
    "GameCard",
    "GamesOnDateResponse",
    "SimulateResponse",
    "RosterOverride",
    "WithOverrideResponse",
]
