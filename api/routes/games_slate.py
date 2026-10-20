"""
api/routes/games_slate.py
=========================
The day slate and the status card of one game.

    GET /api/games/{date}             the games on a date (the slate)
    GET /api/games/{game_pk}/status   one game's identity, state and newest summary

The league's schedule says which games exist and what state each one is in.
The database adds the lineup flag, the venue city and the newest simulation run
of each game. The schedule read is cached by date class (20 s today, 10 min
ahead, 24 h past). On a feed error the last good copy serves, else the stored
listing (``raw.games``); ``source`` in the response says which.

The aggregate router in ``api/routes/games.py`` includes this one. Its routes
carry no prefix, tag or dependency of their own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date as _date
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, status

from api.routes._common import _get_pool, _get_sim_cache, _iso, _opt_int, _opt_str, _row_get
from api.routes.games_models import (
    GameCard,
    GameCardAggregateResponse,
    GamesOnDateResponse,
    _sim_summary_lite_from_stored,
)
from api.schemas import GameSimSummaryLite
from db import sim_store
from simulation.batch_runner import POOL_QUERY_TTL_S

log = logging.getLogger("api.routes.games")

router = APIRouter()


# ---------------------------------------------------------------------------
# The game's state and its season records
# ---------------------------------------------------------------------------


class GameStatus(StrEnum):
    """The four states of a game on the status card.

    :data:`_RAW_STATUS_TO_GAME_STATUS` maps the eight ``raw.games.status``
    values to them:

      * ``scheduled`` -- not started yet (Preview, Warmup, Pre-Game)
      * ``live``      -- in progress
      * ``final``     -- over
      * ``postponed`` -- postponed, suspended or cancelled
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


def _team_records_cte(cutoff: str) -> str:
    """The ``team_records`` CTE: each team's season record before a cutoff date.

    The status card and the stored listing share it; only the cutoff differs.
    ``cutoff`` is a trusted SQL fragment that this module chooses, never user
    input: ``"$1"`` or ``"(SELECT game_date FROM game_date_lookup)"``.
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


#: The team, venue and records joins that both record-bearing queries share.
_TEAM_RECORDS_JOIN_TAIL = """      FROM raw.games g
      LEFT JOIN raw.teams    ht  ON ht.team_id  = g.home_team_id AND ht.season  = g.season
      LEFT JOIN raw.teams    at_ ON at_.team_id = g.away_team_id AND at_.season = g.season
      LEFT JOIN raw.venues    v  ON  v.venue_id = g.venue_id     AND  v.season  = g.season
      LEFT JOIN team_records hr  ON hr.team_id  = g.home_team_id AND hr.season  = g.season
      LEFT JOIN team_records ar  ON ar.team_id  = g.away_team_id AND ar.season  = g.season"""


#: One game's identity, names, venue, final score and season records before
#: its own date (``$1`` = game_pk).
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


#: The stored listing of a date, with the same columns (``$1`` = the date).
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
    """Parse a ``YYYY-MM-DD`` path parameter; 422 on a malformed value.

    The parameter is a plain string, so FastAPI does not parse it. The function
    raises 422 on a bad format, the status FastAPI uses for a validation error.
    """
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        # A literal 422: the name of Starlette's constant for it changed
        # between versions (ENTITY -> CONTENT); the number did not.
        raise HTTPException(
            status_code=422,
            detail=f"invalid date {date_str!r}; expected YYYY-MM-DD",
        ) from exc


def _game_card(row: Any) -> GameCard:
    """Map one row of the stored listing (a Record or a dict) to a GameCard.

    The listing's query joins the teams, the venue and the ``team_records``
    CTE, so the row carries the names, the city and the season records. A row
    without those columns (a unit-test stub) leaves them ``None``.
    """
    gd = _row_get(row, "game_date")
    # game_date is a date (asyncpg) or an ISO string (a canned row).
    game_date = gd.isoformat() if hasattr(gd, "isoformat") else str(gd)

    return GameCard(
        game_pk=int(_row_get(row, "game_pk")),
        season=int(_row_get(row, "season", 0) or 0),
        game_date=game_date,
        status=_opt_str(row, "status"),
        home_team_id=_opt_int(row, "home_team_id"),
        away_team_id=_opt_int(row, "away_team_id"),
        venue_id=_opt_int(row, "venue_id"),
        # The names, the venue and the records
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
        # The EXISTS(...) lineup flag; None when the row lacks it.
        lineup_ready=bool(_row_get(row, "lineup_ready"))
        if _row_get(row, "lineup_ready") is not None
        else None,
    )


# ---------------------------------------------------------------------------
# The slate: the league's schedule plus the stored data
# ---------------------------------------------------------------------------

#: The slate's calendar: the league keys games on the local (official) date,
#: and "today" is today in Eastern time, where the league's day turns over.
SLATE_TZ = ZoneInfo("America/New_York")

#: The schedule cache's time to live by date class (the live-slate design, SIM-519).
SLATE_TTL_PAST_S = 24 * 3600
SLATE_TTL_TODAY_S = 20
SLATE_TTL_FUTURE_S = 600
#: The last good schedule of a date, served when the feed errors.
SLATE_LAST_GOOD_TTL_S = 24 * 3600

#: The stored enrichment of the schedule's games, in one query.
_SLATE_ENRICH_SQL = """
    SELECT g.game_pk, g.status, v.city AS venue_city,
           EXISTS(
               SELECT 1 FROM raw.game_lineups gl WHERE gl.game_pk = g.game_pk
           ) AS lineup_ready
      FROM raw.games g
      LEFT JOIN raw.venues v ON v.venue_id = g.venue_id AND v.season = g.season
     WHERE g.game_pk = ANY($1::int[])
"""

#: The same, with the source of the lineup the simulator would read (the
#: ``source`` column, Alembic 0029): the final box first, then a published,
#: then a projected lineup.
_SLATE_ENRICH_SOURCE_SQL = """
    SELECT g.game_pk, g.status, v.city AS venue_city,
           ls.source IS NOT NULL AS lineup_ready,
           ls.source AS lineup_source
      FROM raw.games g
      LEFT JOIN raw.venues v ON v.venue_id = g.venue_id AND v.season = g.season
      LEFT JOIN LATERAL (
            SELECT CASE WHEN bool_or(gl.source = 'box') THEN 'box'
                        WHEN bool_or(gl.source = 'published') THEN 'published'
                        WHEN bool_or(gl.source = 'projected') THEN 'projected' END AS source
              FROM raw.game_lineups gl WHERE gl.game_pk = g.game_pk
      ) ls ON TRUE
     WHERE g.game_pk = ANY($1::int[])
"""

#: The newest stored run of each of the schedule's games, in one query.
_SLATE_RUNS_SQL = """
    SELECT DISTINCT ON (game_pk) game_pk, run_id, n_iterations, summary, created_at
      FROM sim.sim_runs
     WHERE game_pk = ANY($1::int[])
     ORDER BY game_pk, created_at DESC
"""

#: The same, the game's own finished simulation only (Alembic 0030: not a
#: what-if run, and a run with a summary).
_SLATE_PREGAME_RUNS_SQL = """
    SELECT DISTINCT ON (game_pk) game_pk, run_id, n_iterations, summary, created_at
      FROM sim.sim_runs
     WHERE game_pk = ANY($1::int[]) AND kind = 'pregame' AND summary IS NOT NULL
     ORDER BY game_pk, created_at DESC
"""


def _slate_today() -> _date:
    """Today's date in the league's time zone (:data:`SLATE_TZ`)."""
    return datetime.now(SLATE_TZ).date()


def _slate_ttl(day: _date, today: _date) -> int:
    """The schedule cache's TTL for a date: past 24 h, today 20 s, future 10 min."""
    if day < today:
        return SLATE_TTL_PAST_S
    if day == today:
        return SLATE_TTL_TODAY_S
    return SLATE_TTL_FUTURE_S


def _stored_summary(run: Mapping[str, Any] | None) -> GameSimSummaryLite | None:
    """A stored run row's summary as the lite model; None for no run or a bad summary."""
    if run is None:
        return None
    stored = _row_get(run, "summary")
    if isinstance(stored, str | bytes | bytearray):
        try:
            stored = json.loads(stored)
        except ValueError:
            return None
    return _sim_summary_lite_from_stored(stored)


def _schedule_card(g: Any, enrich: Any | None, run: Any | None) -> GameCard:
    """One schedule game and our stored data as a GameCard.

    The records come from the schedule, not from ``raw.games``.
    """
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
        lineup_source=_opt_str(enrich, "lineup_source") if enrich is not None else None,
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
        n_innings=g.n_innings if played else None,
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
    """The stored enrichment and the newest run per game: two queries.

    Each query falls back to its plain form before the migration that adds its
    columns; a failed read logs a warning and leaves that map empty.
    """
    enrich: dict[int, Any] = {}
    runs: dict[int, Any] = {}
    if pool is None or not pks:
        return enrich, runs
    for sql in (_SLATE_ENRICH_SOURCE_SQL, _SLATE_ENRICH_SQL):
        try:
            for r in await pool.fetch(sql, pks) or []:
                enrich[int(_row_get(r, "game_pk"))] = r
            break
        except Exception as exc:  # noqa: BLE001 -- before 0029 the plain read; else none
            log.warning("slate: the stored enrichment read failed: %s", exc)
    for sql in (_SLATE_PREGAME_RUNS_SQL, _SLATE_RUNS_SQL):
        try:
            for r in await pool.fetch(sql, pks) or []:
                runs[int(_row_get(r, "game_pk"))] = r
            break
        except Exception as exc:  # noqa: BLE001 -- before 0030 the plain read; else no sim line
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

    # The stored listing, memoized for POOL_QUERY_TTL_S.
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
            # The Redis cache stores JSON only, so the dates go in as text.
            cache.set(db_key, resp.model_dump(mode="json"), POOL_QUERY_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("listing cache write failed for %s: %s", db_key, exc)
    return resp


async def _schedule_game_for(request: Request, game_date: Any, game_pk: int) -> Any:
    """The schedule entry of one game (the slate's cached read), or None."""
    from pipeline.mlb_schedule import parse_schedule

    try:
        day = game_date if isinstance(game_date, _date) else _parse_date(str(game_date)[:10])
        payload, _source, _err = await _slate_schedule_payload(request, day, use_cache=True)
        if payload is None:
            return None
        return next((g for g in parse_schedule(payload) if g.game_pk == game_pk), None)
    except Exception as exc:  # noqa: BLE001 -- the header degrades to the stored fields
        log.debug("schedule entry unavailable for %s: %s", game_pk, exc)
        return None


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

    # 1. The game's identity row.
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

    # 2. The stored status as one of the four states.
    raw_status = _row_get(row, "status")
    game_status = _RAW_STATUS_TO_GAME_STATUS.get(raw_status or "", GameStatus.SCHEDULED).value

    # 3. The newest stored sim summary; a failed read leaves None.
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

    # 4. The response.
    gd = _row_get(row, "game_date")
    game_date = gd.isoformat() if hasattr(gd, "isoformat") else str(gd)

    # The schedule's view of the game (state, start, probables), from the
    # slate's cached schedule for the date. With none, the stored fields stand.
    sched = await _schedule_game_for(request, gd, int(game_pk))
    extra: dict[str, Any] = {}
    if sched is not None:
        from pipeline.mlb_schedule import card_state

        game_status = card_state(sched)
        played = game_status in ("live", "final")
        extra = {
            "detailed_state": sched.detailed_state or None,
            "start_utc": _iso(sched.start_utc),
            "start_time_tbd": sched.start_time_tbd,
            "game_number": sched.game_number,
            "double_header": sched.double_header,
            "series_description": sched.series_description,
            "away_probable_pitcher_name": sched.away.probable_pitcher.name
            if sched.away.probable_pitcher
            else None,
            "home_probable_pitcher_name": sched.home.probable_pitcher.name
            if sched.home.probable_pitcher
            else None,
            "away_score": sched.away.score if played else None,
            "home_score": sched.home.score if played else None,
        }

    return GameCardAggregateResponse(
        **extra,
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
