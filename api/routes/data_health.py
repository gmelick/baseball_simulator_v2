"""
api/routes/data_health.py
=========================
SIM-417 — data-freshness / health API surface for the UI.

Exposes an aggregate view of how current the ingested data is, so the frontend
can show a "data as of …" badge and warn when the store is stale or empty:

    GET /api/data/freshness  ->  DataFreshnessResponse

Reads the asyncpg pool off ``app.state.pg_pool`` (503 if not attached). Pure
aggregate counts + timestamps — no per-player detail and nothing sensitive —
so it sits alongside the ops probes (/health, /ready) without auth. Source:
``raw.etl_data_freshness`` (last-ingest watermark) + ``raw.games`` /
``raw.pitches`` (coverage). An empty store returns zeros / nulls, not an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from api.routes._common import _get_pool, _row_get

router = APIRouter(prefix="/api/data", tags=["data"])


class SeasonCoverage(BaseModel):
    """Per-season ingest coverage."""

    season: int
    games: int
    pitches: int


class DataFreshnessResponse(BaseModel):
    """Aggregate ingest freshness + coverage for the UI."""

    #: Most-recent ETL write (max updated_at on raw.etl_data_freshness), ISO-8601.
    last_ingest_at: str | None = None
    #: Most-recent game DATE that has data (max last_date), ISO YYYY-MM-DD.
    latest_game_date: str | None = None
    #: Distinct entities (pitchers) with a freshness watermark.
    tracked_entities: int = 0
    total_games: int = 0
    total_pitches: int = 0
    #: True when any pitch data exists.
    has_data: bool = False
    #: Per-season coverage, ascending by season.
    seasons: list[SeasonCoverage] = Field(default_factory=list)


_AGG_SQL = """
    SELECT
        (SELECT MAX(updated_at) FROM raw.etl_data_freshness) AS last_ingest_at,
        (SELECT MAX(last_date)  FROM raw.etl_data_freshness) AS latest_game_date,
        (SELECT COUNT(*)        FROM raw.etl_data_freshness) AS tracked_entities,
        (SELECT COUNT(*)        FROM raw.games)              AS total_games,
        (SELECT COUNT(*)        FROM raw.pitches)            AS total_pitches
"""

_PER_SEASON_SQL = """
    SELECT season, COUNT(DISTINCT game_pk) AS games, COUNT(*) AS pitches
      FROM raw.pitches
     GROUP BY season
     ORDER BY season
"""


# ``_get_pool`` + ``_row_get`` are imported from api.routes._common (audit
# 2026-06-03 "api/ duplication").  ``_get_pool`` was byte-identical to games.py's
# copy.  The canonical ``_common._row_get`` is the games.py variant (it takes an
# explicit ``default`` and collapses present-but-None to it).  data_health's old
# local copy never passed a non-None default and wraps every read in ``_iso(...)``
# / ``int(... or 0)``, so the canonical version is behaviour-identical for every
# call site here (the only drift -- None-vs-default collapse -- is unobservable
# without a non-None default).


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@router.get(
    "/freshness",
    response_model=DataFreshnessResponse,
    summary="Ingest freshness + per-season coverage for the UI",
    description=(
        "Aggregate view of the ingested data: the last-ingest watermark "
        "(raw.etl_data_freshness), the most-recent game date with data, total "
        "games/pitches, and per-season coverage. The frontend uses this for a "
        "'data as of …' badge and an empty/stale-store warning. 503 when no DB "
        "pool is attached; an empty store returns zeros (not an error)."
    ),
)
async def get_data_freshness(request: Request) -> DataFreshnessResponse:
    pool = _get_pool(request)
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            agg = await conn.fetchrow(_AGG_SQL)
            season_rows = await conn.fetch(_PER_SEASON_SQL)
    else:
        agg = await pool.fetchrow(_AGG_SQL)
        season_rows = await pool.fetch(_PER_SEASON_SQL)

    total_pitches = int(_row_get(agg, "total_pitches") or 0)

    seasons = [
        SeasonCoverage(
            season=int(_row_get(r, "season")),
            games=int(_row_get(r, "games") or 0),
            pitches=int(_row_get(r, "pitches") or 0),
        )
        for r in (season_rows or [])
    ]

    return DataFreshnessResponse(
        last_ingest_at=_iso(_row_get(agg, "last_ingest_at")),
        latest_game_date=_iso(_row_get(agg, "latest_game_date")),
        tracked_entities=int(_row_get(agg, "tracked_entities") or 0),
        total_games=int(_row_get(agg, "total_games") or 0),
        total_pitches=total_pitches,
        has_data=total_pitches > 0,
        seasons=seasons,
    )


__all__ = ["router", "DataFreshnessResponse", "SeasonCoverage"]
