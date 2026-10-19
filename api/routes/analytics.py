"""
api/routes/analytics.py
=======================
SIM-439 — descriptive summary analytics over raw.pitches for the Data Lab.

Every endpoint is GET, season-bounded (an indexed predicate is mandatory on the
~10⁷-row table), defaults to ``data_quality_flag = FALSE`` (the sim's view, with
an ``include_flagged`` opt-out), and casts computed rates to ``float8`` so the
driver returns floats (not ``Decimal``). Parameterised asyncpg queries only —
this is the curated, safe counterpart to the ad-hoc SQL console.

    GET /api/analytics/kpis
    GET /api/analytics/by-season
    GET /api/analytics/pitch-type-mix?season=&pitcher=&stand=&game_type=
    GET /api/analytics/pa-outcomes?season=&batter=&p_throws=&game_type=
    GET /api/analytics/count-state?season=&pitcher=&batter=&game_type=
    GET /api/analytics/velo-distribution?season=&pitcher=&pitch_type=&game_type=
    GET /api/analytics/zone?season=&pitcher=&game_type=
    GET /api/analytics/leaderboard/pitchers?season=&metric=&min_pa=&limit=&game_type=
    GET /api/analytics/leaderboard/batters?season=&metric=&min_pa=&limit=&vs_hand=&game_type=

A season of ``raw.pitches`` holds the regular season and the postseason. The
seven routes that take ``season`` also take ``game_type`` (the Data Lab
game-type filter, SIM-572). ``R``, the default, reads the regular season alone.
``all`` reads every game of the season, the postseason included. The two
coverage routes (kpis, by-season) describe the whole store and take no game type.

The swing, whiff, called-strike and CSW (called strikes plus whiffs) rates filter
on the ``type`` code. The Data Lab vocabulary fix (SIM-571) made that change. The
code sets live in ``pipeline/statcast_events.py``. ``description`` holds the
feed's display text, and no query reads it. The whiff is Baseball Savant's (the
public Statcast site): a foul tip counts.

Every route reads through the read-only pool, ``app.state.pg_pool_ro`` (SIM-566,
the Data Lab read routes on the read-only pool). The pool's role reads ``raw.*``
and nothing else. Without the pool, every route answers 503; no route falls back
to the main pool. The KPIs route also reads ``pg_class`` for a row estimate; the
read-only role can read that catalog.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.auth import require_auth
from api.routes._common import _get_ro_pool
from pipeline.statcast_events import (
    sql_called_strike,
    sql_csw,
    sql_in_play,
    sql_savant_whiff,
    sql_swing,
)

log = logging.getLogger("api.routes.analytics")

router = APIRouter(prefix="/api/analytics", tags=["data-lab"], dependencies=[Depends(require_auth)])

# The pitch-result filters (the Data Lab vocabulary fix, SIM-571). Every rate
# filters on the `type` code, never on `description`. The loader stores the
# feed's display text in `description`. So a match on a snake_case name (lower
# case, with underscores: "called_strike") finds no row. The fragments render
# from the code sets in pipeline/statcast_events.py. The pool build reads the
# same sets. The rendered predicates:
#   swing          type NOT IN ('B', 'C', 'H', 'P', '*B')
#                  The bat left the shoulder.
#   whiff          type IN ('S', 'W', 'M', 'Q', 'T', 'O')
#                  Savant's whiff: a swing and a miss, or a foul tip.
#   called strike  type IN ('C')
#                  The umpire called a strike on a taken pitch.
#   CSW            (a called strike OR a whiff)
#                  CSW means called strikes plus whiffs. Its denominator is every pitch.
#   in play        type IN ('X', 'D', 'E')
#                  'X' alone is the outs, 65% of balls in play. It drops most hits.
# The unprefixed fragments serve the one-table queries (count-state, zone).
# The "p." fragments serve the two leaderboards, which also read raw.players.
_SWING = sql_swing()
_WHIFF = sql_savant_whiff()
_CALLED_STRIKE = sql_called_strike()
_SWING_P = sql_swing("p.")
_WHIFF_P = sql_savant_whiff("p.")
_CSW_P = sql_csw("p.")
_IN_PLAY_P = sql_in_play("p.")

# The game-type filter (the Data Lab game-type filter, SIM-572). raw.games marks
# the type of each game: 'R' is the regular season, and 'F', 'D', 'L' and 'W'
# are the four postseason rounds. In 2025 the postseason holds 47 games and
# 14,085 of the 723,055 clean pitches. The filter is a semi-join on game_pk: it
# keeps a pitch when its game has the type. A plain join would clash on
# `season`, which both tables hold. 'all' adds no filter.

#: The game types a route reads: the regular season alone, or every game.
GameType = Literal["R", "all"]
#: The code of a regular-season game in ``raw.games.game_type``; the default.
REGULAR_SEASON: GameType = "R"
_GameTypeQuery = Annotated[
    GameType,
    Query(
        description=(
            "R (the default) reads the regular season alone. "
            "all reads every game of the season, the postseason included."
        ),
    ),
]


def _filter_game_type(
    where: list[str], args: list[Any], game_type: GameType, prefix: str = ""
) -> None:
    """Keep only the pitches whose game has ``game_type``; ``all`` keeps every game.

    The filter takes the next query argument. So call it before the route
    appends an argument that the WHERE clause does not read (the leaderboards'
    ``min_pa`` and ``limit``).
    """
    if game_type == "all":
        return
    args.append(game_type)
    where.append(
        f"{prefix}game_pk IN (SELECT g.game_pk FROM raw.games g WHERE g.game_type = ${len(args)})"
    )


async def _fetch(pool, sql: str, *args) -> list[Any]:
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)
    return await pool.fetch(sql, *args)


def _dicts(records: list[Any]) -> list[dict[str, Any]]:
    return [dict(r.items()) for r in (records or [])]


def _quality_clause(include_flagged: bool) -> str:
    return "" if include_flagged else " AND data_quality_flag = FALSE"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class KpisResponse(BaseModel):
    total_pitches: int
    total_pitches_estimated: bool
    total_games: int
    seasons: list[int]
    first_date: str | None
    last_date: str | None


# ---------------------------------------------------------------------------
# KPIs + coverage
# ---------------------------------------------------------------------------


@router.get("/kpis", response_model=KpisResponse, summary="Top-line coverage KPIs")
async def get_kpis(request: Request) -> KpisResponse:
    pool = _get_ro_pool(request)
    # total_pitches: pg_class estimate (instant) instead of a 10^7-row COUNT(*).
    est_rows = await _fetch(
        pool,
        "SELECT reltuples::bigint AS n FROM pg_class WHERE oid = 'raw.pitches'::regclass",
    )
    total_pitches = int(est_rows[0]["n"]) if est_rows and est_rows[0]["n"] is not None else 0

    meta = await _fetch(
        pool,
        """
        SELECT (SELECT COUNT(*) FROM raw.games)                         AS games,
               (SELECT MIN(game_date)::text FROM raw.games)            AS first_date,
               (SELECT MAX(game_date)::text FROM raw.games)            AS last_date
        """,
    )
    m = meta[0] if meta else {}
    season_rows = await _fetch(pool, "SELECT DISTINCT season FROM raw.games ORDER BY season")
    seasons = [int(r["season"]) for r in (season_rows or []) if r["season"] is not None]

    return KpisResponse(
        total_pitches=max(total_pitches, 0),
        total_pitches_estimated=True,
        total_games=int(m["games"]) if m and m["games"] is not None else 0,
        seasons=seasons,
        first_date=(m["first_date"] if m else None),
        last_date=(m["last_date"] if m else None),
    )


@router.get("/by-season", summary="Games + pitches per season")
async def by_season(request: Request) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    rows = await _fetch(
        pool,
        """
        SELECT season,
               COUNT(DISTINCT game_pk) AS games,
               COUNT(*)                AS pitches
          FROM raw.pitches
         GROUP BY season
         ORDER BY season
        """,
    )
    return _dicts(rows)


# ---------------------------------------------------------------------------
# Pitch-mix / outcomes / count-state / velo / zone (dashboard cards)
# ---------------------------------------------------------------------------


@router.get("/pitch-type-mix", summary="Pitch-type usage + stuff (season, optional pitcher/stand)")
async def pitch_type_mix(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    pitcher: int | None = None,
    stand: str | None = Query(None, pattern="^[LRS]$"),
    include_flagged: bool = False,
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    where = ["season = $1", "pitch_type IS NOT NULL"]
    args: list[Any] = [season]
    if pitcher is not None:
        args.append(pitcher)
        where.append(f"pitcher = ${len(args)}")
    if stand:
        args.append(stand)
        where.append(f"stand = ${len(args)}")
    _filter_game_type(where, args, game_type)
    sql = f"""
        SELECT pitch_type,
               COUNT(*)                                            AS n,
               ROUND((100.0 * COUNT(*) / SUM(COUNT(*)) OVER ())::numeric, 1)::float8 AS usage_pct,
               ROUND(AVG(release_speed)::numeric, 1)::float8       AS avg_velo,
               ROUND(AVG(break_vertical_induced)::numeric, 1)::float8 AS avg_ivb,
               ROUND(AVG(break_horizontal)::numeric, 1)::float8    AS avg_hb,
               ROUND(AVG(release_spin_rate)::numeric, 0)::float8   AS avg_spin
          FROM raw.pitches
         WHERE {" AND ".join(where)}{_quality_clause(include_flagged)}
         GROUP BY pitch_type
        HAVING COUNT(*) >= 10
         ORDER BY n DESC
    """
    return _dicts(await _fetch(pool, sql, *args))


@router.get("/pa-outcomes", summary="Plate-appearance outcome distribution")
async def pa_outcomes(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    batter: int | None = None,
    p_throws: str | None = Query(None, pattern="^[LR]$"),
    include_flagged: bool = False,
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    where = ["season = $1", "events IS NOT NULL"]
    args: list[Any] = [season]
    if batter is not None:
        args.append(batter)
        where.append(f"batter = ${len(args)}")
    if p_throws:
        args.append(p_throws)
        where.append(f"p_throws = ${len(args)}")
    _filter_game_type(where, args, game_type)
    sql = f"""
        SELECT events AS event, COUNT(*) AS n
          FROM raw.pitches
         WHERE {" AND ".join(where)}{_quality_clause(include_flagged)}
         GROUP BY events
         ORDER BY n DESC
    """
    return _dicts(await _fetch(pool, sql, *args))


@router.get("/count-state", summary="Swing/whiff by ball-strike count")
async def count_state(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    pitcher: int | None = None,
    batter: int | None = None,
    include_flagged: bool = False,
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    where = ["season = $1", "balls BETWEEN 0 AND 3", "strikes BETWEEN 0 AND 2"]
    args: list[Any] = [season]
    if pitcher is not None:
        args.append(pitcher)
        where.append(f"pitcher = ${len(args)}")
    if batter is not None:
        args.append(batter)
        where.append(f"batter = ${len(args)}")
    _filter_game_type(where, args, game_type)
    sql = f"""
        SELECT balls, strikes,
               COUNT(*) AS pitches,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_SWING}) / NULLIF(COUNT(*), 0))::numeric, 1)::float8 AS swing_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_WHIFF})
                     / NULLIF(COUNT(*) FILTER (WHERE {_SWING}), 0))::numeric, 1)::float8 AS whiff_pct
          FROM raw.pitches
         WHERE {" AND ".join(where)}{_quality_clause(include_flagged)}
         GROUP BY balls, strikes
         ORDER BY balls, strikes
    """
    return _dicts(await _fetch(pool, sql, *args))


@router.get("/velo-distribution", summary="Release-speed histogram (1 mph buckets)")
async def velo_distribution(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    pitcher: int | None = None,
    pitch_type: str | None = Query(None, max_length=5),
    include_flagged: bool = False,
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    where = ["season = $1", "release_speed IS NOT NULL"]
    args: list[Any] = [season]
    if pitcher is not None:
        args.append(pitcher)
        where.append(f"pitcher = ${len(args)}")
    if pitch_type:
        args.append(pitch_type)
        where.append(f"pitch_type = ${len(args)}")
    _filter_game_type(where, args, game_type)
    sql = f"""
        SELECT FLOOR(release_speed)::int AS velo_bucket, COUNT(*) AS n
          FROM raw.pitches
         WHERE {" AND ".join(where)}{_quality_clause(include_flagged)}
         GROUP BY 1
         ORDER BY 1
    """
    return _dicts(await _fetch(pool, sql, *args))


@router.get("/zone", summary="Whiff / called-strike by Statcast zone bucket")
async def zone(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    pitcher: int | None = None,
    include_flagged: bool = False,
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    where = ["season = $1", "zone IS NOT NULL"]
    args: list[Any] = [season]
    if pitcher is not None:
        args.append(pitcher)
        where.append(f"pitcher = ${len(args)}")
    _filter_game_type(where, args, game_type)
    sql = f"""
        SELECT zone,
               COUNT(*) AS pitches,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_WHIFF})
                     / NULLIF(COUNT(*) FILTER (WHERE {_SWING}), 0))::numeric, 1)::float8 AS whiff_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_CALLED_STRIKE})
                     / NULLIF(COUNT(*), 0))::numeric, 1)::float8 AS called_strike_pct
          FROM raw.pitches
         WHERE {" AND ".join(where)}{_quality_clause(include_flagged)}
         GROUP BY zone
         ORDER BY zone
    """
    return _dicts(await _fetch(pool, sql, *args))


# ---------------------------------------------------------------------------
# Leaderboards (deep-link into the Similarity engines)
# ---------------------------------------------------------------------------

_PITCHER_METRICS = {"k_pct", "bb_pct", "csw_pct", "whiff_pct", "pa"}
_BATTER_METRICS = {"hard_hit_pct", "avg_ev", "max_ev", "k_pct", "bb_pct", "hr", "pa"}


@router.get("/leaderboard/pitchers", summary="Pitcher rate-stat leaderboard")
async def pitcher_leaderboard(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    metric: str = Query("k_pct"),
    min_pa: int = Query(200, ge=0, le=5000),
    limit: int = Query(50, ge=1, le=200),
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    order = metric if metric in _PITCHER_METRICS else "k_pct"
    where = ["p.season = $1", "p.data_quality_flag = FALSE"]
    args: list[Any] = [season]
    _filter_game_type(where, args, game_type, "p.")
    args.append(min_pa)
    having_idx = len(args)
    args.append(limit)
    limit_idx = len(args)
    sql = f"""
        SELECT p.pitcher AS player_id,
               pl.full_name AS name,
               COUNT(*) FILTER (WHERE p.events IS NOT NULL) AS pa,
               ROUND((100.0 * COUNT(*) FILTER (WHERE p.events IN ('strikeout','strikeout_double_play'))
                     / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0))::numeric, 1)::float8 AS k_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE p.events = 'walk')
                     / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0))::numeric, 1)::float8 AS bb_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_CSW_P})
                     / NULLIF(COUNT(*), 0))::numeric, 1)::float8 AS csw_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_WHIFF_P})
                     / NULLIF(COUNT(*) FILTER (WHERE {_SWING_P}), 0))::numeric, 1)::float8 AS whiff_pct
          FROM raw.pitches p
          JOIN raw.players pl ON pl.player_id = p.pitcher
         WHERE {" AND ".join(where)}
         GROUP BY p.pitcher, pl.full_name
        HAVING COUNT(*) FILTER (WHERE p.events IS NOT NULL) >= ${having_idx}
         ORDER BY {order} DESC NULLS LAST
         LIMIT ${limit_idx}
    """
    return _dicts(await _fetch(pool, sql, *args))


@router.get("/leaderboard/batters", summary="Batter contact-quality leaderboard")
async def batter_leaderboard(
    request: Request,
    season: int = Query(..., ge=2015, le=2027),
    metric: str = Query("hard_hit_pct"),
    min_pa: int = Query(200, ge=0, le=5000),
    limit: int = Query(50, ge=1, le=200),
    vs_hand: str | None = Query(None, pattern="^[LR]$"),
    game_type: _GameTypeQuery = REGULAR_SEASON,
) -> list[dict[str, Any]]:
    pool = _get_ro_pool(request)
    order = metric if metric in _BATTER_METRICS else "hard_hit_pct"
    where = ["p.season = $1", "p.data_quality_flag = FALSE"]
    args: list[Any] = [season]
    if vs_hand:
        args.append(vs_hand)
        where.append(f"p.p_throws = ${len(args)}")
    _filter_game_type(where, args, game_type, "p.")
    args.append(min_pa)
    having_idx = len(args)
    args.append(limit)
    limit_idx = len(args)
    sql = f"""
        SELECT p.batter AS player_id,
               pl.full_name AS name,
               COUNT(*) FILTER (WHERE p.events IS NOT NULL) AS pa,
               COUNT(*) FILTER (WHERE {_IN_PLAY_P} AND p.launch_speed IS NOT NULL) AS bip,
               ROUND(AVG(p.launch_speed) FILTER (WHERE {_IN_PLAY_P})::numeric, 1)::float8 AS avg_ev,
               MAX(p.launch_speed) FILTER (WHERE {_IN_PLAY_P})::float8 AS max_ev,
               ROUND((100.0 * COUNT(*) FILTER (WHERE {_IN_PLAY_P} AND p.launch_speed >= 95)
                     / NULLIF(COUNT(*) FILTER (WHERE {_IN_PLAY_P} AND p.launch_speed IS NOT NULL), 0))::numeric, 1)::float8 AS hard_hit_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE p.events IN ('strikeout','strikeout_double_play'))
                     / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0))::numeric, 1)::float8 AS k_pct,
               ROUND((100.0 * COUNT(*) FILTER (WHERE p.events = 'walk')
                     / NULLIF(COUNT(*) FILTER (WHERE p.events IS NOT NULL), 0))::numeric, 1)::float8 AS bb_pct,
               COUNT(*) FILTER (WHERE p.events = 'home_run') AS hr
          FROM raw.pitches p
          JOIN raw.players pl ON pl.player_id = p.batter
         WHERE {" AND ".join(where)}
         GROUP BY p.batter, pl.full_name
        HAVING COUNT(*) FILTER (WHERE p.events IS NOT NULL) >= ${having_idx}
         ORDER BY {order} DESC NULLS LAST
         LIMIT ${limit_idx}
    """
    return _dicts(await _fetch(pool, sql, *args))


__all__ = ["router", "KpisResponse", "GameType", "REGULAR_SEASON"]
