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
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import date as _date
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, NamedTuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes._common import _get_pool, _row_get
from api.schemas import (
    BoxscoreCardModel,
    EdgeReportModel,
    GameSimSummaryLite,
    GameSimSummaryModel,
    LinescoreModel,
    LiveGameStateResponse,
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
from simulation.batch_runner import (
    POOL_QUERY_TTL_S,
    BatchRunner,
    GameSpec,
)
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

# SIM-449 re-exports. Nothing in THIS module calls them any more -- every route
# goes through ``_resolved_sim_kwargs`` (SIM-452) -- but importers outside the
# package (``scripts/``, the unit tests) still reach them here under these names.
from simulation.sim_kwargs import (  # noqa: F401 -- kept as a public re-export
    resolve_park_run_factor as _resolve_park_run_factor,
)
from simulation.sim_kwargs import (  # noqa: F401 -- kept as a public re-export
    sim_kwargs_from_state as _sim_kwargs_from_state,
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
#
# ``_get_pool`` (the ``app.state.pg_pool`` accessor) lives in
# ``api.routes._common`` and is imported above -- it was byte-identical to
# data_health.py's copy (audit 2026-06-03 "api/ duplication"); it is re-exported
# here for the existing ``from api.routes.games import _get_pool`` callers
# (betting.py uses the _common import directly).


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


def _get_replay_store(request: Request) -> Any:
    """The replay file's DuckDB connection on app.state, or None (SIM-561).

    The lifespan opens it when ``REPLAY_PERSISTENCE_ENABLED`` is on: a file of its
    own (``REPLAY_DUCKDB_PATH``, default ``/data/replay.duckdb``) that only the app
    writes.  It is NOT the analytics connection (:func:`_get_sim_duckdb`), so the
    app never holds a write lock on the analytics file.  ``None`` means the store
    is off: the replay reads answer 503 and the writes skip.
    """
    return getattr(request.app.state, "replay_duckdb", None)


def _replay_call(con: Any, fn: Any, /, **kwargs: Any) -> Any:
    """Run ``fn(cursor, **kwargs)`` on a cursor of its own, then close it (SIM-561).

    A DuckDB connection is not safe to share across threads; a cursor per call
    is.  The routes run every replay read and write in a worker thread.
    """
    cur = con.cursor()
    try:
        return fn(cur, **kwargs)
    finally:
        cur.close()


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
    """One row of ``GET /api/games/{date}`` -- a scheduled game's identity.

    SIM-383 enrichment adds team names, abbreviations, venue name + city, and
    season-to-date win/loss records (computed through the day before ``game_date``
    from ``raw.games`` final results).  All enrichment fields are ``Optional``
    so existing cached payloads and unit-test stubs that only supply the raw
    ID columns continue to deserialise without error.
    """

    game_pk: int
    season: int
    game_date: str
    status: str | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    venue_id: int | None = None
    # SIM-383 enrichment -------------------------------------------------------
    home_team_name: str | None = None
    home_team_abbrev: str | None = None
    away_team_name: str | None = None
    away_team_abbrev: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    # Season records entering the game (all games with status='Final' AND
    # game_date < this game's date).  0-0 when no prior results exist.
    home_wins: int | None = None
    home_losses: int | None = None
    away_wins: int | None = None
    away_losses: int | None = None
    # SIM-409: True when raw.game_lineups has at least one row for this game;
    # False for scheduled games whose lineups haven't been published yet.
    # None when the caller did not include the lineup check (older code paths).
    lineup_ready: bool | None = None
    # SIM-519 Part A: the schedule-driven card. Every field is optional, so a
    # stored payload, a unit stub or an old mock still deserialises.
    game_status: Literal["scheduled", "live", "final", "postponed"] | None = None
    detailed_state: str | None = None
    reason: str | None = None
    start_utc: str | None = None
    start_time_tbd: bool | None = None
    double_header: str | None = None
    game_number: int | None = None
    game_type: str | None = None
    series_description: str | None = None
    away_score: int | None = None
    home_score: int | None = None
    inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    away_probable_pitcher_id: int | None = None
    away_probable_pitcher_name: str | None = None
    home_probable_pitcher_id: int | None = None
    home_probable_pitcher_name: str | None = None
    rescheduled_to: str | None = None
    rescheduled_from: str | None = None
    lineup_source: str | None = None
    sim_summary: GameSimSummaryLite | None = None
    sim_run_at: str | None = None
    db_known: bool | None = None


class GamesOnDateResponse(BaseModel):
    """The ``GET /api/games/{date}`` envelope: the date echo + its game cards.

    SIM-519 Part A: ``source`` says where the list came from -- ``schedule``
    (the league's schedule, fresh or from the 20-second cache),
    ``schedule_cached`` (the last good schedule after a feed error) or ``db``
    (the stored listing). ``feed_error`` carries the error on a degraded read.
    """

    date: str
    count: int
    games: list[GameCard] = Field(default_factory=list)
    source: Literal["schedule", "schedule_cached", "db"] | None = None
    feed_error: str | None = None
    fetched_at: str | None = None


class SampleGameResponse(BaseModel):
    """The ``POST /api/games/{game_pk}/sample-game`` envelope (SIM-561).

    The stored game's run id and seed.  The page then reads the game through
    /linescore, /decisions and /plays, which serve this run until a newer one.
    """

    game_pk: int
    run_id: int
    base_seed: int


class SimulateResponse(BaseModel):
    """The ``GET /api/games/{game_pk}/simulate`` envelope."""

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    from_cache: bool = False
    summary: GameSimSummaryModel


class SubstitutionSlot(BaseModel):
    """A single targeted player substitution (SIM-388).

    Replaces the player at a specific batting-order slot on one side of the
    lineup, without having to know or pass the other 8 slots.  One or more of
    these can appear in :attr:`RosterOverride.substitutions` to stage a
    multi-sub override queue.

      * ``batting_order`` -- 1-indexed slot (1 = leadoff … 9 = ninth).
      * ``player_id``     -- the replacement player's MLB player ID.
      * ``side``          -- ``"home"`` or ``"away"``.

    Multiple ``SubstitutionSlot`` items for the SAME (side, batting_order) in
    a single request are applied left-to-right, so the last one wins.
    """

    batting_order: int = Field(..., ge=1, le=9, description="1-indexed lineup slot (1–9)")
    player_id: int = Field(..., gt=0, description="MLB player_id of the substitute")
    side: Literal["home", "away"] = Field(..., description="Which side's lineup to modify")


class RosterOverride(BaseModel):
    """The ``POST .../simulate/with_override`` request body (SIM-358 + SIM-388).

    Every field is OPTIONAL; an unset field keeps the resolved (baseline) value.
    At least one field SHOULD be set for the override to differ from baseline,
    but an all-empty body is accepted (it yields an all-zero delta -- a valid,
    if uninteresting, comparison).

      * ``home_lineup`` / ``away_lineup`` -- full 1..9 batting orders (player
        ids) to swap in for that side.  When set, REPLACES the entire lineup
        before any ``substitutions`` are applied.
      * ``substitutions`` -- SIM-388: a list of :class:`SubstitutionSlot`
        targeted single-player changes applied AFTER any full-lineup replacements.
        Use this to stage individual subs without knowing the full batting order.
        Left-to-right application; same-slot conflicts → last entry wins.
      * ``pitcher_id`` -- the pitcher to face the batters for the simulated half.
      * ``bat_hand`` -- the leadoff batter's hand override ('L'/'R'/'S'); the
        sampler pre-filter key.
      * ``description`` -- free text recorded on the returned OverrideDelta.
    """

    home_lineup: list[int] | None = None
    away_lineup: list[int] | None = None
    # SIM-388: targeted substitution list (applied after full-lineup overrides)
    substitutions: list[SubstitutionSlot] | None = None
    pitcher_id: int | None = None
    bat_hand: str | None = Field(default=None, max_length=1)
    description: str | None = None


class WithOverrideResponse(BaseModel):
    """The ``POST .../simulate/with_override`` envelope (SIM-358).

    Carries the BASELINE summary, the OVERRIDE summary (both run at the SAME
    base seed for an apples-to-apples comparison), and the OverrideDelta diff.
    """

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    baseline: GameSimSummaryModel
    override: GameSimSummaryModel
    delta: OverrideDeltaModel


# ---------------------------------------------------------------------------
# SIM-384 -- game-status enum + aggregate card model + query
# ---------------------------------------------------------------------------


class GameStatus(StrEnum):
    """3-state (+ postponed) game status for the Day Summary UI.

    Maps the 8 raw ``raw.games.status`` values to the 4 canonical UI states:
      * ``scheduled`` -- game not yet started (Preview / Warmup / Pre-Game)
      * ``live``      -- game in progress
      * ``final``     -- game completed
      * ``postponed`` -- game postponed, suspended, or cancelled
    """

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"


_RAW_STATUS_TO_GAME_STATUS: dict[str, GameStatus] = {
    "Preview": GameStatus.SCHEDULED,
    "Warmup": GameStatus.SCHEDULED,
    "Pre-Game": GameStatus.SCHEDULED,
    "Live": GameStatus.LIVE,
    "Final": GameStatus.FINAL,
    "Postponed": GameStatus.POSTPONED,
    "Suspended": GameStatus.POSTPONED,
    "Cancelled": GameStatus.POSTPONED,
}


class GameCardAggregateResponse(BaseModel):
    """Response for ``GET /api/games/{game_pk}/card`` (SIM-384).

    Aggregates the identity, status, SIM-383 enrichment, and the most recent
    persisted Monte-Carlo summary into a single payload for the Day Summary
    3-state game cards.  ``sim_summary`` is ``None`` when no sim has been run
    yet (or when Postgres is unavailable).  ``odds`` is reserved for Phase 6
    Sprint 4+ (odds provider integration — SIM-405).
    """

    game_pk: int
    game_status: str  # one of GameStatus values
    game_date: str
    season: int
    # SIM-383 enrichment (all optional -- None when DB join has no matching row)
    home_team_id: int | None = None
    away_team_id: int | None = None
    venue_id: int | None = None
    home_team_name: str | None = None
    home_team_abbrev: str | None = None
    away_team_name: str | None = None
    away_team_abbrev: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    home_wins: int | None = None
    home_losses: int | None = None
    away_wins: int | None = None
    away_losses: int | None = None
    # Final scores (populated when game_status == "final")
    home_score_final: int | None = None
    away_score_final: int | None = None
    # Most-recent Monte-Carlo summary (None when no run has been persisted yet)
    sim_summary: GameSimSummaryLite | None = None
    # Odds (Phase 6 Sprint 4+ -- SIM-405/SIM-395)
    odds: None = None


# ---------------------------------------------------------------------------
# Shared SIM-383 win/loss-records SQL (audit 2026-06-03 "api/ duplication")
# ---------------------------------------------------------------------------
# The ``team_records`` CTE (season-to-date W/L from ``raw.games`` finals strictly
# before a cutoff date) and the team/venue/records JOIN tail were copy-pasted in
# both _GAME_CARD_SQL and _GAMES_ON_DATE_SQL.  They are factored here so any
# change to the records computation happens in ONE place.  The ONLY thing that
# varies between the two callers is the date-cutoff expression, threaded in via
# ``{cutoff}``: a single game_pk lookup vs the listed date param ``$1``.


def _team_records_cte(cutoff: str) -> str:
    """The ``team_records`` CTE body, with the date cutoff expression spliced in.

    ``cutoff`` is a trusted SQL fragment (a literal/sub-select chosen by THIS
    module, never user input) -- e.g. ``"$1"`` or
    ``"(SELECT game_date FROM game_date_lookup)"``.
    """
    return f"""team_records AS (
        -- Season-to-date records: only Final games BEFORE the cutoff date.
        SELECT team_id, season,
               SUM(CASE WHEN (is_home AND home_score_final > away_score_final)
                             OR (NOT is_home AND away_score_final > home_score_final)
                        THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN (is_home AND home_score_final < away_score_final)
                             OR (NOT is_home AND away_score_final < home_score_final)
                        THEN 1 ELSE 0 END) AS losses
          FROM (
                SELECT home_team_id AS team_id, season, TRUE::BOOLEAN AS is_home,
                       home_score_final, away_score_final
                  FROM raw.games
                 WHERE status = 'Final' AND game_date < {cutoff}
                UNION ALL
                SELECT away_team_id AS team_id, season, FALSE::BOOLEAN AS is_home,
                       home_score_final, away_score_final
                  FROM raw.games
                 WHERE status = 'Final' AND game_date < {cutoff}
               ) t
         GROUP BY team_id, season
    )"""


#: The team/venue/records LEFT JOIN tail shared by both record-bearing queries
#: (byte-identical in the original two copies).
_TEAM_RECORDS_JOIN_TAIL = """      FROM raw.games g
      LEFT JOIN raw.teams    ht  ON ht.team_id  = g.home_team_id AND ht.season  = g.season
      LEFT JOIN raw.teams    at_ ON at_.team_id = g.away_team_id AND at_.season = g.season
      LEFT JOIN raw.venues    v  ON  v.venue_id = g.venue_id     AND  v.season  = g.season
      LEFT JOIN team_records hr  ON hr.team_id  = g.home_team_id AND hr.season  = g.season
      LEFT JOIN team_records ar  ON ar.team_id  = g.away_team_id AND ar.season  = g.season"""


#: SQL to fetch the SIM-383-enriched identity + final scores for a single
#: game_pk.  The ``team_records`` CTE computes season-to-date win/loss records
#: from ``raw.games`` final results strictly before the game's own date.
#: Single parameter: ``$1 = game_pk (int)``.
_GAME_CARD_SQL = f"""
    WITH game_date_lookup AS (
        SELECT game_date, season FROM raw.games WHERE game_pk = $1
    ),
    {_team_records_cte("(SELECT game_date FROM game_date_lookup)")}
    SELECT g.game_pk, g.season, g.game_date, g.status,
           g.home_team_id, g.away_team_id, g.venue_id,
           g.home_score_final, g.away_score_final,
           ht.team_name     AS home_team_name,
           ht.team_abbrev   AS home_team_abbrev,
           at_.team_name    AS away_team_name,
           at_.team_abbrev  AS away_team_abbrev,
           v.venue_name,
           v.city           AS venue_city,
           COALESCE(hr.wins,   0) AS home_wins,
           COALESCE(hr.losses, 0) AS home_losses,
           COALESCE(ar.wins,   0) AS away_wins,
           COALESCE(ar.losses, 0) AS away_losses,
           EXISTS(
               SELECT 1 FROM raw.game_lineups gl WHERE gl.game_pk = g.game_pk
           ) AS lineup_ready
{_TEAM_RECORDS_JOIN_TAIL}
     WHERE g.game_pk = $1
"""


def _with_half_width(ci: Any) -> Any:
    """A stored interval dict with ``half_width`` filled from its bounds.

    SIM-546 review: the stored JSON lacks the field (see
    ``_sim_summary_lite_from_stored``). A value that is not such a dict
    passes through unchanged, and the model then rejects it.
    """
    if (
        isinstance(ci, dict)
        and "half_width" not in ci
        and isinstance(ci.get("low"), (int, float))
        and isinstance(ci.get("high"), (int, float))
    ):
        return {**ci, "half_width": (ci["high"] - ci["low"]) / 2.0}
    return ci


def _sim_summary_lite_from_stored(summary: dict | None) -> GameSimSummaryLite | None:
    """Build a ``GameSimSummaryLite`` from a stored (JSONB) summary dict.

    Strips the three raw per-iteration score arrays (``home_scores``,
    ``away_scores``, ``total_scores``) and the per-iteration inning grid
    (``inning_grids``, SIM-546) before passing to ``model_validate``
    because ``GameSimSummaryLite`` does not carry those fields and the base
    ``_ApiModel`` uses ``extra="forbid"``.  Returns ``None`` on any failure
    (missing keys, type errors, unknown format) so callers never see an
    exception from a sim-history read.

    SIM-546 review: the stored summary carries no ``half_width`` on its
    intervals. ``half_width`` is a property of the interval dataclass, and
    ``to_jsonable`` writes fields only. The lite model requires it, so the
    projection fills it from ``(high - low) / 2`` on each ``*_ci`` dict that
    lacks it. Without the fill every stored summary read None.
    """
    if not summary:
        return None
    try:
        lite_dict = {
            k: _with_half_width(v) if k.endswith("_ci") else v
            for k, v in summary.items()
            if k not in ("home_scores", "away_scores", "total_scores", "inning_grids")
        }
        return GameSimSummaryLite.model_validate(lite_dict)
    except Exception:  # noqa: BLE001 -- best-effort deserialization
        return None


# ---------------------------------------------------------------------------
# Helpers -- sim_kwargs assembly from a resolved GameState
#
# SIM-449: ``_sim_kwargs_from_state`` and ``_resolve_park_run_factor`` MOVED to
# ``simulation/sim_kwargs.py``. This module imports them above under their
# original names, so every existing caller and importer is unchanged.
# ``scripts/sim_stats.py`` held a second copy of the builder that dropped the two
# defense maps and the park factor, so SIM_FIELDER_RBF and SIM_PARK_FACTOR ran as
# no-ops under the validation harness. One definition now serves both callers.
#
# SIM-452: SIM-449 unified the BUILDER but not the park-factor RESOLUTION. Five of
# the eight call sites never looked the venue up, so they sent the GameState
# default 1.0 and SIM_PARK_FACTOR was inert for them. ``_resolved_sim_kwargs``
# below is now the ONLY way a route gets sim kwargs, and it always resolves first.
# ---------------------------------------------------------------------------


async def _resolved_sim_kwargs(
    request: Request, pool: Any, state: Any, game_pk: int
) -> dict[str, Any]:
    """Resolve the venue park factor onto ``state``, then build the sim kwargs (SIM-452).

    Every route uses this. ``_resolve_state_or_error`` stamps the unresolved
    sentinel on the state, and ``simulation.sim_kwargs.build_sim_kwargs`` clears the
    sentinel by writing the looked-up factor. A route that called the bare builder
    instead would raise ``UnresolvedParkFactorError``.

    The DuckDB connection comes from ``app.state.sim_duckdb``. When it is absent the
    resolver returns a real neutral ``1.0``, which is a resolved answer, not a
    skipped lookup.
    """
    return await _build_sim_kwargs(
        state,
        pool=pool,
        con=_get_sim_duckdb(request),
        game_pk=int(game_pk),
    )


def _apply_override(base_kwargs: dict[str, Any], override: RosterOverride) -> dict[str, Any]:
    """Return a COPY of ``base_kwargs`` with the override's set fields applied.

    Processing order (SIM-358 + SIM-388):
    1. Full-lineup replacements (``home_lineup`` / ``away_lineup``) are applied first,
       completely replacing that side's batting order.
    2. Targeted substitutions (``substitutions``) are applied left-to-right to the
       CURRENT lineup after step 1 (whether it was replaced or carried from baseline).
       Out-of-range ``batting_order`` values are silently skipped so a malformed slot
       never prevents the rest of the override from running.
    3. Pitcher and bat-hand overrides are applied last.

    Only fields the caller actually set (non-None) replace the baseline value;
    everything else is carried through unchanged so a single-field override
    (e.g. just the pitcher) leaves both lineups intact.
    """
    kw = dict(base_kwargs)

    # Step 1: full-lineup replacements.
    if override.home_lineup is not None:
        kw["home_lineup"] = [int(x) for x in override.home_lineup]
    if override.away_lineup is not None:
        kw["away_lineup"] = [int(x) for x in override.away_lineup]

    # Step 2 (SIM-388): targeted single-player substitutions.
    if override.substitutions:
        home_lineup = list(kw.get("home_lineup") or [])
        away_lineup = list(kw.get("away_lineup") or [])
        for sub in override.substitutions:
            slot = int(sub.batting_order) - 1  # 1-indexed → 0-indexed
            if sub.side == "home" and 0 <= slot < len(home_lineup):
                home_lineup[slot] = int(sub.player_id)
            elif sub.side == "away" and 0 <= slot < len(away_lineup):
                away_lineup[slot] = int(sub.player_id)
            # Out-of-range slot (e.g. lineup shorter than expected): silently skip.
        kw["home_lineup"] = home_lineup
        kw["away_lineup"] = away_lineup

    # Step 3: pitcher + bat-hand.
    if override.pitcher_id is not None:
        kw["pitcher_id"] = int(override.pitcher_id)
    if override.bat_hand is not None:
        kw["bat_hand"] = str(override.bat_hand)
    return kw


async def _resolve_state_or_error(pool: Any, game_pk: int) -> Any:
    """Resolve a game's GameState via SIM-353, mapping failures to HTTP errors.

    A connection is acquired from the pool (asyncpg-style ``async with
    pool.acquire()``); a pool that IS a connection (the mock-pool test idiom)
    is used directly.

    Error mapping (SIM-409, SIM-559):
    * ``LineupNotIngestedError`` (game exists, lineups not yet published by MLB)
      → 503 Service Unavailable with a Retry-After: 900 hint.
    * ``LineupIncompleteError`` (game exists, its lineup rows do not make a
      playable game: a side with no pitcher, an empty batting order)
      → 503 with the same Retry-After hint. The gap is data a later lineup
      publish or the box backfill fills; the game is not missing (SIM-559).
    * ``LineupResolutionError`` (game not found in raw.games) → 404 Not Found.

    SIM-452: every state this factory returns leaves stamped with
    ``UNRESOLVED_PARK_FACTOR``. This is the ONE state factory all eight sim-kwargs
    call sites use, so the stamp reaches all eight. A caller that skips the venue
    lookup now raises ``UnresolvedParkFactorError`` at build time instead of
    running a park-blind simulation that reports success.
    """
    acquire = getattr(pool, "acquire", None)
    try:
        if acquire is not None:
            async with pool.acquire() as conn:
                state = await resolve_game_state(conn, game_pk)
        else:
            # The pool itself exposes fetch/fetchrow (mock-pool / direct-conn path).
            state = await resolve_game_state(pool, game_pk)
        mark_park_factor_unresolved(state)
        return state
    except (LineupNotIngestedError, LineupIncompleteError) as exc:
        # Transient: the lineup is not published yet, or its rows do not yet
        # make a playable game (SIM-559). Suggest a retry in 15 min.
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


def _player_ref_from_jsonable(ref: Any) -> PlayerRef | None:
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
        sequence=(None if snapshot.get("sequence") is None else int(snapshot["sequence"])),
    )
    return StateAtPitchModel.from_dataclass(sap)


# ---------------------------------------------------------------------------
# Record -> persist flow (SIM-357) -- best-effort, never breaks /simulate
# ---------------------------------------------------------------------------


class ReplayArtifacts(NamedTuple):
    """One recorded game, built for the replay store (SIM-357; SIM-561 adds ``result``)."""

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
    """Record one game in THIS process and build its replay artifacts (sync).

    The routes record on the worker pool instead (SIM-561,
    ``BatchRunner.record_game``) and call :func:`_build_replay_artifacts`.
    """
    recorded = record_game(
        factory_ref=factory_ref,
        seed=base_seed,
        sim_kwargs=sim_kwargs,
    )
    return _build_replay_artifacts(recorded, resolved=resolved)


def _build_replay_artifacts(recorded: RecordedGame, *, resolved: Any = None) -> ReplayArtifacts:
    """Build the replay artifacts of ONE recorded game (sync).

    From a :class:`~simulation.play_recorder.RecordedGame` (the game, its plays
    and the state before each), this derives:

      * a :class:`~simulation.snapshots.PlayByPlay` (the /plays scroll),
      * one jsonable :class:`~simulation.snapshots.StateAtPitch` per pitch (the
        /state lookup rows), tagged with each pitch's at_bat / pitch / sequence
        from the matching PlayByPlay entry and built from that ``PlayResult``'s
        committed ``next_state`` (the GameState after the pitch),
      * the jsonable :class:`~simulation.linescore.Linescore` (SIM-362) and
        :class:`~simulation.pitcher_decisions.PitcherDecisions` (SIM-364) DERIVED
        from the recorded ``PlayResult`` list -- computed HERE, at record time,
        because both read ``PlayResult.next_state`` which the persisted
        ``PlayByPlayEntry`` rows drop (so they cannot be re-derived at read time).

    SIM-363 -- POPULATING THE 9 FIELDERS: when a ``resolved``
    :class:`~simulation.lineup_resolver.ResolvedLineup` is supplied, each per-pitch
    StateAtPitch is built with the fielding side's ``defense_positions`` map
    (``build_defense_map_for_state``), so the persisted snapshots carry the 9
    fielders and ``GET /state`` returns them populated.  The map depends only on
    the half (which side is fielding) + substitutions, so it is cached per
    ``(half, fielding-side)`` key across the game.  With no ``resolved`` lineup the
    9 slots stay present-but-empty (the prior behaviour).

    SIM-554 -- A RESULT WITH NO PITCH: a pickoff before the pitch that makes
    the third out returns a result with no pitch thrown (``NO_PITCH``).  The
    play-by-play and the per-pitch snapshots hold the thrown pitches only.  The
    linescore and the decisions read the whole stream, because the pickoff's
    out lives on that result.

    SIM-561 -- WHO, WHEN, THE SCORE: the recorder also captures the state before
    each pitch, so every play-by-play entry carries its inning, half, outs,
    batter, pitcher and the score after it.

    Returns a :class:`ReplayArtifacts` (the play-by-play, the state-snapshot
    dicts, the linescore and decisions JSON, and the recorded game result).
    A pitch whose ``next_state`` is missing is skipped for the state stream (its
    /plays entry still persists).  Pure + sync so the caller can run it in a
    worker thread.
    """
    plays = recorded.plays
    pbp = PlayByPlay.from_play_results(plays, recorded.contexts)
    pitched = thrown_pitches(plays)

    # SIM-362 / SIM-364: derive the game card from the recorded PlayResult list
    # (these read PlayResult.next_state, dropped by the persisted entries, so they
    # MUST be computed here at record time). Both read the WHOLE stream: a
    # no-pitch result carries the pickoff's out (SIM-554).
    linescore = linescore_from_plays(plays)
    decisions = decisions_from_plays(plays)

    # SIM-363: cache the fielding-side defense map per (half, fielding-side) so we
    # build it at most twice per inning rather than per pitch (it changes only on a
    # substitution, which the resolver's "latest occupant" map already reflects).
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

    # The thrown pitches and pbp.entries are 1:1 in order (SIM-554: a no-pitch
    # result makes no entry), so zip pairs each PlayResult with its entry's
    # (at_bat, pitch, sequence) indices. strict=True fails loudly on a slip
    # rather than storing every later snapshot one pitch off.
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
    """Record one game and store it in the replay file; return its run id (SIM-357).

    Records ONE game at ``base_seed`` on the warm worker pool and stores its
    play-stream, per-pitch state snapshots and game card in the replay file
    (SIM-561: a DuckDB file of its own, which numbers its own runs).  ``batch``
    is the /simulate batch: its summary goes to the Postgres sim-run history
    (SIM-356).  A sample game or the projections' first game (``batch`` None)
    writes no history row: a one-game summary is not a Monte-Carlo run.

    Best-effort: a missing replay store returns None at once, and any failure
    logs a warning and returns None.  It never raises, so a store failure never
    breaks /simulate or /boxscore; the sample-game route checks for None.  After
    a store, the game keeps only its newest :data:`db.sim_store.REPLAY_RUNS_KEPT`
    runs.
    """
    con = _get_replay_store(request)
    pool = getattr(request.app.state, "pg_pool", None)

    # Nothing to write the replay stream to -> skip entirely (the replay reads
    # answer 503 "replay store unavailable").
    if con is None:
        return None

    try:
        # SIM-363: resolve the lineup ONCE (best-effort) so the recorded snapshots
        # can carry the 9 fielders for whichever side is fielding.  A resolution
        # failure (e.g. a mock pool with no game_lineups) just leaves the fielder
        # slots empty -- the replay still persists, /state still 200s with empty
        # positions, so this is strictly additive.
        resolved = await _resolve_lineup_best_effort(pool, game_pk)

        # 1) Record the game on the warm worker pool (SIM-561: the API process
        #    never loads the sim bundle), then build the artifacts here.  The
        #    build also derives the SIM-362 linescore + SIM-364 decisions card.
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
    """Keep a /simulate batch's summary in the Postgres sim-run history (SIM-356).

    Best-effort: a failure logs a warning.  Since SIM-561 the history row's id is
    its own; the replay file numbers its runs apart.
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


#: SIM-561: one replay write at a time in this process.  The run id is the file's
#: max + 1, and two prunes of the same game conflict in DuckDB ("Conflict on
#: tuple deletion"), so the id draw, the store and the prune run under it.
_REPLAY_WRITE_LOCK = threading.Lock()


def _store_replay_run(
    cur: Any, *, game_pk: int, built: ReplayArtifacts, base_seed: int | None
) -> int:
    """Write one recorded game to the replay file on ``cur``; return its run id.

    Sync (SIM-561).  The play stream, the snapshots and the game card go in one
    transaction under the file's next run id.  The prune of the game's older runs
    follows; a prune failure logs a warning and keeps the stored game.
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
            # SIM-362/364: the game card (linescore + decisions), derived at record
            # time (they need PlayResult.next_state) and keyed on the same run_id.
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
    """Resolve a game's :class:`ResolvedLineup` (SIM-363), or None on any failure.

    Used by :func:`_persist_replay_artifacts` to populate the 9 FieldSnapshot
    fielders.  Strictly best-effort: a missing pool, an unknown game, or no
    lineup rows simply yields ``None`` (the snapshots are then built with empty
    fielder slots, the prior behaviour) -- a fielder-map failure NEVER breaks the
    replay persist or the /simulate response.
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


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_GAMES_ON_DATE_SQL = f"""
    WITH {_team_records_cte("$1")}
    SELECT g.game_pk, g.season, g.game_date, g.status,
           g.home_team_id, g.away_team_id, g.venue_id,
           ht.team_name     AS home_team_name,
           ht.team_abbrev   AS home_team_abbrev,
           at_.team_name    AS away_team_name,
           at_.team_abbrev  AS away_team_abbrev,
           v.venue_name,
           v.city           AS venue_city,
           COALESCE(hr.wins,   0) AS home_wins,
           COALESCE(hr.losses, 0) AS home_losses,
           COALESCE(ar.wins,   0) AS away_wins,
           COALESCE(ar.losses, 0) AS away_losses,
           EXISTS(
               SELECT 1 FROM raw.game_lineups gl WHERE gl.game_pk = g.game_pk
           ) AS lineup_ready
{_TEAM_RECORDS_JOIN_TAIL}
     WHERE g.game_date = $1
     ORDER BY g.game_pk
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


# ``_row_get`` (uniform Record/dict column read) lives in api.routes._common and
# is imported at the top of this module -- it was the canonical variant the audit
# (2026-06-03 "api/ duplication") chose over data_health.py's near-identical copy.


def _opt_str(row: Any, key: str) -> str | None:
    """``str(row[key])`` or ``None`` when the column is absent/NULL.

    Module-level (takes ``row`` explicitly) so the identical _game_card /
    _enriched_game_card closures share one definition -- audit 2026-06-03
    "api/ duplication".
    """
    v = _row_get(row, key)
    return None if v is None else str(v)


def _opt_int(row: Any, key: str) -> int | None:
    """``int(row[key])`` or ``None`` when the column is absent/NULL (see _opt_str)."""
    v = _row_get(row, key)
    return None if v is None else int(v)


def _game_card(row: Any) -> GameCard:
    """Map one raw.games row (Record or dict) to a GameCard.

    SIM-383: the enriched query joins ``raw.teams`` (×2) + ``raw.venues`` +
    the ``team_records`` CTE so the row carries name, abbreviation, city, and
    season-to-date win/loss fields.  All new fields are optional -- rows that
    don't include them (e.g. old cached payloads, unit-test stubs) silently
    default to ``None``.
    """
    gd = _row_get(row, "game_date")
    # game_date may be a date/datetime (asyncpg) or an ISO string (canned rows).
    game_date = gd.isoformat() if hasattr(gd, "isoformat") else str(gd)

    return GameCard(
        game_pk=int(_row_get(row, "game_pk")),
        season=int(_row_get(row, "season", 0) or 0),
        game_date=game_date,
        status=_opt_str(row, "status"),
        home_team_id=_opt_int(row, "home_team_id"),
        away_team_id=_opt_int(row, "away_team_id"),
        venue_id=_opt_int(row, "venue_id"),
        # SIM-383 enrichment fields (None when the row predates the enriched query)
        home_team_name=_opt_str(row, "home_team_name"),
        home_team_abbrev=_opt_str(row, "home_team_abbrev"),
        away_team_name=_opt_str(row, "away_team_name"),
        away_team_abbrev=_opt_str(row, "away_team_abbrev"),
        venue_name=_opt_str(row, "venue_name"),
        venue_city=_opt_str(row, "venue_city"),
        home_wins=_opt_int(row, "home_wins"),
        home_losses=_opt_int(row, "home_losses"),
        away_wins=_opt_int(row, "away_wins"),
        away_losses=_opt_int(row, "away_losses"),
        # SIM-409: bool from the EXISTS(...) subquery; None when absent.
        lineup_ready=bool(_row_get(row, "lineup_ready"))
        if _row_get(row, "lineup_ready") is not None
        else None,
    )


# ===========================================================================
# SIM-355 -- GET /api/games/{date}
# ===========================================================================


# ---------------------------------------------------------------------------
# SIM-519 Part A -- the schedule-driven slate
# ---------------------------------------------------------------------------

#: The slate's calendar: the league keys games on the local (official) date,
#: and "today" is today in Eastern time, where the league's day turns over.
SLATE_TZ = ZoneInfo("America/New_York")

#: The schedule cache's time to live by date class (SIM-519 §4.3).
SLATE_TTL_PAST_S = 24 * 3600
SLATE_TTL_TODAY_S = 20
SLATE_TTL_FUTURE_S = 600
#: The last good schedule of a date, served when the feed errors.
SLATE_LAST_GOOD_TTL_S = 24 * 3600

#: The stored enrichment of the schedule's games, in one query (SIM-519 §4.3).
_SLATE_ENRICH_SQL = """
    SELECT g.game_pk, g.status, v.city AS venue_city,
           EXISTS(
               SELECT 1 FROM raw.game_lineups gl WHERE gl.game_pk = g.game_pk
           ) AS lineup_ready
      FROM raw.games g
      LEFT JOIN raw.venues v ON v.venue_id = g.venue_id AND v.season = g.season
     WHERE g.game_pk = ANY($1::int[])
"""

#: The newest stored run of each of the schedule's games, in one query.
_SLATE_RUNS_SQL = """
    SELECT DISTINCT ON (game_pk) game_pk, run_id, n_iterations, summary, created_at
      FROM sim.sim_runs
     WHERE game_pk = ANY($1::int[])
     ORDER BY game_pk, created_at DESC
"""


def _slate_today() -> _date:
    return datetime.now(SLATE_TZ).date()


def _slate_ttl(day: _date, today: _date) -> int:
    """The schedule cache's TTL for a date: past 24 h, today 20 s, future 10 min."""
    if day < today:
        return SLATE_TTL_PAST_S
    if day == today:
        return SLATE_TTL_TODAY_S
    return SLATE_TTL_FUTURE_S


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _stored_summary(run: Mapping[str, Any] | None) -> GameSimSummaryLite | None:
    if run is None:
        return None
    stored = _row_get(run, "summary")
    if isinstance(stored, str | bytes | bytearray):
        import json

        try:
            stored = json.loads(stored)
        except ValueError:
            return None
    return _sim_summary_lite_from_stored(stored)


def _schedule_card(g: Any, enrich: Any | None, run: Any | None) -> GameCard:
    """One schedule game + our stored data → a GameCard (D1: the schedule's records)."""
    from pipeline.mlb_schedule import card_state

    state = card_state(g)
    played = state in ("live", "final")
    away_pp, home_pp = g.away.probable_pitcher, g.home.probable_pitcher
    return GameCard(
        game_pk=g.game_pk,
        season=g.season,
        game_date=g.official_date.isoformat(),
        status=g.detailed_state or (_opt_str(enrich, "status") if enrich is not None else None),
        home_team_id=g.home.team_id or None,
        away_team_id=g.away.team_id or None,
        venue_id=g.venue_id,
        home_team_name=g.home.name,
        home_team_abbrev=g.home.abbreviation,
        away_team_name=g.away.name,
        away_team_abbrev=g.away.abbreviation,
        venue_name=g.venue_name,
        venue_city=_opt_str(enrich, "venue_city") if enrich is not None else None,
        home_wins=g.home.wins,
        home_losses=g.home.losses,
        away_wins=g.away.wins,
        away_losses=g.away.losses,
        lineup_ready=bool(_row_get(enrich, "lineup_ready")) if enrich is not None else False,
        game_status=state,
        detailed_state=g.detailed_state or None,
        reason=g.reason,
        start_utc=_iso(g.start_utc),
        start_time_tbd=g.start_time_tbd,
        double_header=g.double_header,
        game_number=g.game_number,
        game_type=g.game_type or None,
        series_description=g.series_description,
        away_score=g.away.score if played else None,
        home_score=g.home.score if played else None,
        inning=g.inning,
        inning_half=g.inning_half,
        outs=g.outs,
        away_probable_pitcher_id=away_pp.player_id if away_pp else None,
        away_probable_pitcher_name=away_pp.name if away_pp else None,
        home_probable_pitcher_id=home_pp.player_id if home_pp else None,
        home_probable_pitcher_name=home_pp.name if home_pp else None,
        rescheduled_to=_iso(g.rescheduled_to),
        rescheduled_from=_iso(g.rescheduled_from),
        sim_summary=_stored_summary(run),
        sim_run_at=_iso(_row_get(run, "created_at")) if run is not None else None,
        db_known=enrich is not None,
    )


async def _slate_schedule_payload(
    request: Request, day: _date, *, use_cache: bool
) -> tuple[Any, str, str | None]:
    """The schedule response for ``day``, with its source and any feed error.

    Order: the cache (``slate:v2:{date}``, TTL by date class), then the feed,
    then the last good copy (``slate:last:{date}``). Returns ``(None, "db",
    error)`` when all three miss; the caller then serves the stored listing.
    """
    feed = getattr(request.app.state, "league_feed", None)
    cache = _get_sim_cache(request)
    key, last_key = f"slate:v2:{day.isoformat()}", f"slate:last:{day.isoformat()}"
    if use_cache and cache is not None:
        try:
            hit = cache.get(key)
        except Exception:  # noqa: BLE001 -- a cache hiccup must not break the read
            hit = None
        if hit is not None:
            return hit, "schedule", None
    if feed is None:
        return None, "db", None
    try:
        payload = await feed.schedule_payload(day, day)
    except Exception as exc:  # noqa: BLE001 -- degrade, never fail (SIM-519 §4.3)
        error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("slate: the league schedule read failed for %s: %s", day, error)
        last = None
        if cache is not None:
            try:
                last = cache.get(last_key)
            except Exception:  # noqa: BLE001
                last = None
        if last is not None:
            return last, "schedule_cached", error
        return None, "db", error
    if cache is not None:
        try:
            cache.set(key, payload, _slate_ttl(day, _slate_today()))
            cache.set(last_key, payload, SLATE_LAST_GOOD_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("slate cache write failed for %s: %s", key, exc)
    return payload, "schedule", None


async def _slate_merge_rows(pool: Any, pks: list[int]) -> tuple[dict[int, Any], dict[int, Any]]:
    """The stored enrichment and the newest run per game: two queries, best effort."""
    enrich: dict[int, Any] = {}
    runs: dict[int, Any] = {}
    if pool is None or not pks:
        return enrich, runs
    try:
        for r in await pool.fetch(_SLATE_ENRICH_SQL, pks) or []:
            enrich[int(_row_get(r, "game_pk"))] = r
    except Exception as exc:  # noqa: BLE001 -- the schedule alone still makes cards
        log.warning("slate: the stored enrichment read failed: %s", exc)
    try:
        for r in await pool.fetch(_SLATE_RUNS_SQL, pks) or []:
            runs[int(_row_get(r, "game_pk"))] = r
    except Exception as exc:  # noqa: BLE001 -- a missing run table means no sim line
        log.warning("slate: the sim-run read failed: %s", exc)
    return enrich, runs


async def _slate_from_db(pool: Any, parsed: _date) -> list[GameCard]:
    """The stored listing, each row mapped through the one status mapper."""
    from pipeline.mlb_schedule import card_state_from_raw

    rows = await pool.fetch(_GAMES_ON_DATE_SQL, parsed)
    games = []
    for r in rows or []:
        card = _game_card(r)
        card.game_status = card_state_from_raw(card.status)  # type: ignore[assignment]
        card.db_known = True
        games.append(card)
    return games


@router.get(
    "/{date}",
    response_model=GamesOnDateResponse,
    summary="Games scheduled on a date",
    description=(
        "List the games on a date (YYYY-MM-DD). SIM-519: the league's schedule says "
        "which games exist and what state they are in; our database adds the lineup "
        "flag and the newest simulation run per game. The schedule is cached 20 s for "
        "today, 10 min for a future date and 24 h for a past one. When the league feed "
        "fails, the endpoint serves the last good schedule, else the stored listing, and "
        "says which in `source`. 422 on a bad date; 503 when the feed is down and no "
        "database pool is attached."
    ),
)
async def get_games_on_date(
    date: str,
    request: Request,
    use_cache: bool = Query(True, description="Consult/populate the schedule cache"),
) -> GamesOnDateResponse:
    from pipeline.mlb_schedule import parse_schedule

    parsed = _parse_date(date)
    payload, source, feed_error = await _slate_schedule_payload(
        request, parsed, use_cache=use_cache
    )
    fetched_at = datetime.now(SLATE_TZ).isoformat()

    if payload is not None:
        # A suspended game appears on its resume day too; keep one card per game.
        sched = list({g.game_pk: g for g in parse_schedule(payload)}.values())
        pool = getattr(request.app.state, "pg_pool", None)
        enrich, runs = await _slate_merge_rows(pool, [g.game_pk for g in sched])
        games = [_schedule_card(g, enrich.get(g.game_pk), runs.get(g.game_pk)) for g in sched]
        return GamesOnDateResponse(
            date=parsed.isoformat(),
            count=len(games),
            games=games,
            source=source,  # type: ignore[arg-type]
            feed_error=feed_error,
            fetched_at=fetched_at,
        )

    # The stored listing, memoized at POOL_QUERY_TTL_S (SIM-359).
    pool = _get_pool(request)
    cache = _get_sim_cache(request)
    db_key = f"games:on_date:{parsed.isoformat()}"
    if use_cache and cache is not None:
        try:
            cached = cache.get(db_key)
        except Exception:  # noqa: BLE001 -- a cache hiccup must not break the read
            cached = None
        if cached is not None:
            return GamesOnDateResponse(**{**cached, "feed_error": feed_error})
    games = await _slate_from_db(pool, parsed)
    resp = GamesOnDateResponse(
        date=parsed.isoformat(),
        count=len(games),
        games=games,
        source="db",
        feed_error=feed_error,
        fetched_at=fetched_at,
    )
    if use_cache and cache is not None:
        try:
            cache.set(db_key, resp.model_dump(), POOL_QUERY_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("listing cache write failed for %s: %s", db_key, exc)
    return resp


# ===========================================================================
# SIM-384 -- GET /api/games/{game_pk}/card
# ===========================================================================


@router.get(
    "/{game_pk}/status",
    response_model=GameCardAggregateResponse,
    summary="Game-status aggregate (identity + 3-state status + sim summary)",
    description=(
        "Return a single game's enriched identity (SIM-383 team names + records + venue), "
        "3-state status enum (scheduled/live/final/postponed), final scores when available, "
        "and the most recently persisted Monte-Carlo sim summary as a lite projection "
        "(no raw score arrays). Intended for the Day Summary 3-state game cards (SIM-391). "
        "``sim_summary`` is None when no sim has been run yet or when Postgres is unavailable. "
        "``odds`` is reserved for the Phase-6 odds-provider integration (SIM-405). "
        "Returns 404 when game_pk is not in raw.games, 503 when no DB pool is attached."
    ),
)
async def get_game_status_card(
    game_pk: int,
    request: Request,
) -> GameCardAggregateResponse:
    pool = _get_pool(request)

    # 1. Fetch the enriched game identity row.
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_GAME_CARD_SQL, int(game_pk))
    else:
        row = await pool.fetchrow(_GAME_CARD_SQL, int(game_pk))

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"game_pk {game_pk} not found",
        )

    # 2. Map raw status → 3-state enum.
    raw_status = _row_get(row, "status")
    game_status = _RAW_STATUS_TO_GAME_STATUS.get(raw_status or "", GameStatus.SCHEDULED).value

    # 3. Load the most-recent persisted sim summary (best-effort; never breaks the response).
    sim_summary: GameSimSummaryLite | None = None
    try:
        if acquire is not None:
            async with pool.acquire() as conn:
                run = await sim_store.load_latest_sim_run(conn, int(game_pk))
        else:
            run = await sim_store.load_latest_sim_run(pool, int(game_pk))
        if run is not None:
            sim_summary = _sim_summary_lite_from_stored(run.get("summary"))
    except Exception:  # noqa: BLE001 -- sim history is best-effort
        pass

    # 4. Build and return the aggregate response.
    gd = _row_get(row, "game_date")
    game_date = gd.isoformat() if hasattr(gd, "isoformat") else str(gd)

    return GameCardAggregateResponse(
        game_pk=int(_row_get(row, "game_pk")),
        game_status=game_status,
        game_date=game_date,
        season=int(_row_get(row, "season", 0) or 0),
        home_team_id=_opt_int(row, "home_team_id"),
        away_team_id=_opt_int(row, "away_team_id"),
        venue_id=_opt_int(row, "venue_id"),
        home_team_name=_opt_str(row, "home_team_name"),
        home_team_abbrev=_opt_str(row, "home_team_abbrev"),
        away_team_name=_opt_str(row, "away_team_name"),
        away_team_abbrev=_opt_str(row, "away_team_abbrev"),
        venue_name=_opt_str(row, "venue_name"),
        venue_city=_opt_str(row, "venue_city"),
        home_wins=_opt_int(row, "home_wins"),
        home_losses=_opt_int(row, "home_losses"),
        away_wins=_opt_int(row, "away_wins"),
        away_losses=_opt_int(row, "away_losses"),
        home_score_final=_opt_int(row, "home_score_final"),
        away_score_final=_opt_int(row, "away_score_final"),
        sim_summary=sim_summary,
        odds=None,
    )


# ---------------------------------------------------------------------------
# SIM-386 — Live in-progress game-state read path
# ---------------------------------------------------------------------------


@router.get(
    "/{game_pk}/live",
    response_model=LiveGameStateResponse,
    summary="Live in-progress game state (SIM-386)",
    description=(
        "Read the live game state from ``sim.lineup_state`` for an in-progress game.  "
        "The live ingestion pipeline (port :8001) writes the game state JSONB on every "
        "MLB WebSocket signal; this endpoint surfaces it on the main API so the frontend "
        "does not need to reach the pipeline app directly.  Fields mirror the "
        "``game_state`` JSONB blob: inning/half/outs/score/baserunners/lineup/roster.  "
        "``updated_at`` (ISO-8601) marks when the pipeline last wrote; the frontend "
        "may treat a gap > 30s as a potential pipeline staleness signal.  "
        "Returns 404 when no live row exists for ``game_pk`` (game not yet live, "
        "already finished, or pipeline has not ingested it yet). "
        "Returns 503 when no DB pool is attached."
    ),
)
async def get_live_game_state(
    game_pk: int,
    request: Request,
) -> LiveGameStateResponse:
    pool = _get_pool(request)
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            live = await sim_store.load_live_game_state(conn, int(game_pk))
    else:
        live = await sim_store.load_live_game_state(pool, int(game_pk))

    if live is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No live game state found for game_pk {game_pk}. "
                "The game may not be in-progress or the pipeline has not "
                "ingested it yet."
            ),
        )

    gs: dict[str, Any] = live.get("game_state") or {}

    def _i(key: str, default: int = 0) -> int:
        v = gs.get(key, default)
        return default if v is None else int(v)

    def _opt_i(key: str) -> int | None:
        v = gs.get(key)
        return None if v is None else int(v)

    def _ids(key: str) -> list[int]:
        v = gs.get(key)
        if not v:
            return []
        return [int(x) for x in v if x is not None]

    return LiveGameStateResponse(
        game_pk=int(live["game_pk"]),
        session_id=str(live["session_id"]),
        inning=_i("inning", 1),
        half=str(gs.get("half", "Top")),
        outs=_i("outs"),
        balls=_i("balls"),
        strikes=_i("strikes"),
        home_score=_i("home_score"),
        away_score=_i("away_score"),
        batting_team_id=_opt_i("batting_team_id"),
        fielding_team_id=_opt_i("fielding_team_id"),
        on_1b=_opt_i("on_1b"),
        on_2b=_opt_i("on_2b"),
        on_3b=_opt_i("on_3b"),
        current_batter_id=_opt_i("current_batter_id"),
        current_pitcher_id=_opt_i("current_pitcher_id"),
        home_lineup=_ids("home_lineup"),
        away_lineup=_ids("away_lineup"),
        home_bullpen=_ids("home_bullpen"),
        away_bullpen=_ids("away_bullpen"),
        home_bench=_ids("home_bench"),
        away_bench=_ids("away_bench"),
        updated_at=live.get("updated_at"),
    )


# ===========================================================================
# SIM-355 -- GET /api/games/{game_pk}/simulate
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
    # SIM-411 / SIM-452: resolve the venue park factor, THEN build the kwargs.
    spec = GameSpec(
        machine_factory=factory_ref,
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    runner = _build_runner(request)

    # The batch is CPU-bound -- offload to a worker thread so the loop stays free.
    _t0 = time.perf_counter()
    batch = await asyncio.to_thread(
        _run_batch,
        runner,
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )
    # SIM-374 metrics: record the batch wall-clock so baseball_sim_latency_seconds
    # reflects real /simulate cost (the gauge previously had no production caller).
    # Best-effort, never breaks the response; skipped on a cache hit (no real sim ran).
    if not batch.from_cache:
        try:
            from api.routes.metrics import record_sim_latency

            record_sim_latency(request.app.state, time.perf_counter() - _t0)
        except Exception:  # noqa: BLE001
            pass

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
# SIM-561 -- POST /api/games/{game_pk}/sample-game
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


# ===========================================================================
# SIM-358 -- POST /api/games/{game_pk}/simulate/with_override
# ===========================================================================


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

    # SIM-411 / SIM-452: one resolved park factor, shared by baseline + override.
    base_kwargs = await _resolved_sim_kwargs(request, pool, state, game_pk)
    override_kwargs = _apply_override(base_kwargs, override)

    baseline_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=base_kwargs)
    override_spec = GameSpec(machine_factory=factory_ref, sim_kwargs=override_kwargs)

    # Both batches at the SAME base seed -> the only difference between the two
    # summaries is the roster change (apples-to-apples comparison).
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
# SIM-561 -- the replay reads: the newest complete run, and the players' names
# ===========================================================================


def _read_newest_plays(
    cur: Any, *, game_pk: int, run_id: int | None = None
) -> tuple[int | None, int | None, list[dict]]:
    """A complete run's id, seed and play stream (sync; SIM-561).

    ``run_id`` None reads the newest complete run.  A run with no game card is
    not complete and reads as empty.
    """
    card = sim_store.load_game_card(cur, game_pk=game_pk, run_id=run_id)
    if card is None:
        return None, None, []
    rows = sim_store.load_play_stream(cur, game_pk=game_pk, run_id=card["run_id"])
    return card["run_id"], card.get("base_seed"), rows


def _read_newest_state(cur: Any, *, game_pk: int, at_bat: int, pitch: int) -> dict | None:
    """One pitch's stored state in the newest complete run (sync; SIM-561)."""
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
    reliever N".  Best-effort like :func:`_player_names`: with no pool, or a
    failed lookup, the real players stay unnamed.
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

    # SIM-561: one complete run only (the newest, unless the caller names one);
    # the store keeps several per game.
    stored_run, base_seed, rows = await asyncio.to_thread(
        _replay_call, con, _read_newest_plays, game_pk=int(game_pk), run_id=run_id
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no persisted plays for game_pk={game_pk}",
        )

    # Build the FULL collection first so n_pitches / n_plate_appearances reflect
    # the whole game regardless of paging (SIM-415).
    pbp = PlayByPlay(entries=[PlayByPlayEntry(**row) for row in rows])
    model = PlayByPlayModel.from_dataclass(pbp)
    model.run_id = stored_run
    model.base_seed = base_seed
    model.names = {
        str(pid): name
        for pid, name in (await _replay_player_names(request, _play_player_ids(rows))).items()
    }

    if limit is None:
        return model  # unchanged full-collection response

    total = len(model.entries)
    model.entries = model.entries[offset : offset + limit]
    model.total_entries = total
    model.page_offset = offset
    model.page_limit = limit
    model.returned_entries = len(model.entries)
    return model


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

    # ``row["snapshot"]`` is the stored, numpy-free jsonable StateAtPitch dict
    # ({at_bat, pitch, field, sequence}).  Rebuild the dataclasses (so the
    # FieldSnapshot's derived occupied_bases / runners_on recompute) and run them
    # through the SAME StateAtPitchModel.from_dataclass converter the live path
    # uses -- no GameState replay needed.
    return _state_at_pitch_model_from_snapshot(row["snapshot"])


# ===========================================================================
# SIM-362/364 -- GET /linescore + /decisions + /card (the persisted game card)
# ===========================================================================


class GameCardResponse(BaseModel):
    """The combined ``GET /api/games/{game_pk}/card`` envelope (SIM-362/364).

    Carries both derived loop outputs for the game's most-recent persisted run:
    the per-inning :class:`~api.schemas.LinescoreModel` (R/H/E grid) and the
    :class:`~api.schemas.PitcherDecisionsModel` (W/L/Save).
    """

    game_pk: int
    linescore: LinescoreModel
    decisions: PitcherDecisionsModel
    #: SIM-561: the stored run and its seed; /plays?run_id= reads the same game.
    run_id: int | None = None
    base_seed: int | None = None


async def _load_game_card_or_error(request: Request, game_pk: int) -> dict:
    """Load the persisted game card, mapping no-store/no-card to 503/404.

    Shared by /linescore, /decisions, and /card: 503 when no DuckDB replay store
    is wired, 404 when nothing has been persisted for the game (no /simulate run,
    or the card persist was skipped).  Returns the parsed
    ``{run_id, game_pk, linescore, decisions}`` dict on success.
    """
    con = _get_replay_store(request)
    if con is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="replay store unavailable",
        )
    # The newest card: the same run /plays and /state read (SIM-561).
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
# SIM-366 -- GET /api/games/{game_pk}/boxscore (per-player prop MEANS card)
# ===========================================================================


def _build_prop_set(
    *,
    runner: BatchRunner,
    spec: GameSpec,
    n_iterations: int,
    base_seed: int | None,
) -> PropDistributionSet:
    """Build the run's :class:`PropDistributionSet` on the batch runner (SIM-560).

    The runner fans the N games out to its worker pool, the same pool
    ``/simulate`` uses, and caches a seeded set.  So the projections card and
    every prop the user then clicks read ONE run.  Before SIM-560 this replayed
    the N games one after another in the API process through the play recorder:
    about 2.5 minutes for the card's 100 games, and again for every click.

    Sync and CPU-bound; the caller offloads it to a worker thread.
    """
    return runner.run_prop_set(spec, n_iterations=n_iterations, base_seed=base_seed)


@dataclasses.dataclass
class _PlayerTag:
    """Where one player sits in the simulated game (SIM-560).

    ``side`` is 'away' or 'home'.  ``lineup_slot`` is the 1-9 batting-order
    slot, or None for a player who does not bat.  ``starting_pitcher`` marks
    each side's starter; a pitcher who also bats keeps his slot.
    """

    side: str
    lineup_slot: int | None = None
    starting_pitcher: bool = False


def _placeholder_pens(sim_kwargs: Mapping[str, Any]) -> dict[int, list[int]]:
    """The factory's placeholder pen for this game, keyed 0 = away and 1 = home.

    A game with no pen in the box (a scheduled game: only the historical loader
    writes ``raw.game_bullpen``) pitches these arms.  Their ids are negative and
    built from each starter's id, so this asks the factory's own builder.
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
    """Tag every player the sim can use with a side, a slot and a role (SIM-560).

    The sim kwargs already name the game's players: the two batting orders,
    the two starters, and each side's pen (``bullpen``, keyed 0 = away and
    1 = home).  A game with no pen in the box pitches the factory's placeholder
    arms instead (:func:`_placeholder_pens`).  The loop pinch-hits nobody, so
    these are every player a box score can hold.
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
    """The players' names, or an empty map when the lookup fails (SIM-560).

    A name is a label.  A failed lookup leaves the card's names empty and the
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
) -> BoxscoreCardModel:
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    # SIM-452: this route was one of the five park-blind callers. It resolves now.
    spec = GameSpec(
        machine_factory=resolve_factory_ref(request),
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    # SIM-561: store the run's first game for the linescore and play-by-play
    # panels, while the pool runs the batch.  Game 0's seed is base_seed itself
    # (derive_seed(base_seed, 0)), so the panels show one of these games.  A store
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
# SIM-390 — Player-prop edge/signal endpoint
# ---------------------------------------------------------------------------

#: All valid prop names (pitcher + batter).  A request for any other prop name
#: is a 422 (validation error) not a 404, because the prop name is a constant
#: from the simulation model, not a dynamic per-game ID.
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
) -> PropEdgeResponse:
    # ---- parameter validation ----
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

    # ---- build prop set (same path as /boxscore; SIM-560: a seeded run is cached) ----
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)
    # SIM-452: this route was one of the five park-blind callers. It resolves now.
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

    # ---- look up the requested player + prop ----
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

    # ---- optional over/under probabilities ----
    p_over_val: float | None = None
    p_under_val: float | None = None
    p_push_val: float | None = None
    if line is not None:
        p_over_val = float(dist.p_over(float(line)))
        p_under_val = float(dist.p_under(float(line)))
        p_push_val = float(dist.p_push(float(line)))

    # ---- optional edge report ----
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
        except Exception as exc:  # noqa: BLE001 -- best-effort; odds can be degenerate
            log.warning("prop_edge_report failed for %d/%s: %s", player_id, prop_upper, exc)

    return PropEdgeResponse(
        player_id=int(dist.player_id),
        prop=str(dist.prop),
        n=int(dist.n),
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
    "GameCard",
    "GamesOnDateResponse",
    "SimulateResponse",
    "RosterOverride",
    "WithOverrideResponse",
    "GameCardResponse",
]
