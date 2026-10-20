"""
api/routes/players.py
=====================
The player name typeahead and a player's identity, for the Data Lab and the similarity UI.

    GET /api/players/search?q=&limit=      -> [PlayerHit]        (name typeahead)
    GET /api/players/{player_id}           -> PlayerDetail

``raw.players`` carries a ``pg_trgm`` GIN index on ``full_name``
(``idx_players_full_name_trgm``), so the typeahead ranks by ``public.similarity()``;
if the extension is somehow unavailable it degrades to a plain ``ILIKE`` match
(logged once). Every id the rest of the UI shows is resolved to a name here so
users never read raw player ids.

Both routes read the read-only pool, ``app.state.pg_pool_ro`` (the
``baseball_ro`` login). Without that pool each route answers 503. Neither
route falls back to the main pool.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from api.auth import require_auth
from api.routes._common import _get_ro_pool, _row_get

log = logging.getLogger("api.routes.players")

# Every route on this router requires a login (cookie or API key).
router = APIRouter(prefix="/api/players", tags=["data-lab"], dependencies=[Depends(require_auth)])


class PlayerHit(BaseModel):
    player_id: int
    name: str
    bats: str | None = None
    throws: str | None = None
    position: str | None = None


class PlayerDetail(BaseModel):
    player_id: int
    name: str
    bats: str | None = None
    throws: str | None = None
    position: str | None = None
    seasons: list[int] = []


# The query names the function's schema. The read-only pool's session sets
# search_path = raw, and pg_trgm lives in public. On that pool an unqualified
# ``similarity()`` fails. The route then drops to the ILIKE fallback, which
# sorts by name and does not rank.
_SEARCH_TRGM = """
    SELECT player_id, full_name AS name, bats, throws, primary_position AS position
      FROM raw.players
     WHERE full_name ILIKE '%' || $1 || '%'
     ORDER BY public.similarity(full_name, $1) DESC, full_name ASC
     LIMIT $2
"""

_SEARCH_ILIKE = """
    SELECT player_id, full_name AS name, bats, throws, primary_position AS position
      FROM raw.players
     WHERE full_name ILIKE '%' || $1 || '%'
     ORDER BY full_name ASC
     LIMIT $2
"""

_DETAIL = """
    SELECT player_id, full_name AS name, bats, throws, primary_position AS position
      FROM raw.players
     WHERE player_id = $1
"""

_SEASONS = """
    SELECT season FROM raw.pitches WHERE pitcher = $1
    UNION
    SELECT season FROM raw.pitches WHERE batter = $1
    ORDER BY season
"""


async def _fetch(pool, sql: str, *args):
    """Dual-pool fetch (real asyncpg pool with .acquire, or a test stub)."""
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)
    return await pool.fetch(sql, *args)


@router.get(
    "/search",
    response_model=list[PlayerHit],
    summary="Player name typeahead (raw.players, pg_trgm)",
)
async def search_players(
    request: Request,
    q: str = Query(..., min_length=2, max_length=60, description="Name fragment (>= 2 chars)."),
    limit: int = Query(20, ge=1, le=50),
) -> list[PlayerHit]:
    pool = _get_ro_pool(request)
    term = q.strip()
    if len(term) < 2:
        return []
    try:
        rows = await _fetch(pool, _SEARCH_TRGM, term, limit)
    except Exception as exc:  # noqa: BLE001 — pg_trgm may be absent; degrade
        log.warning(
            "pg_trgm similarity() unavailable (%s); falling back to ILIKE.", type(exc).__name__
        )
        rows = await _fetch(pool, _SEARCH_ILIKE, term, limit)

    return [
        PlayerHit(
            player_id=int(_row_get(r, "player_id")),
            name=_row_get(r, "name", ""),
            bats=_row_get(r, "bats"),
            throws=_row_get(r, "throws"),
            position=_row_get(r, "position"),
        )
        for r in (rows or [])
    ]


@router.get(
    "/{player_id}",
    response_model=PlayerDetail,
    summary="Player identity + seasons on file",
)
async def get_player(request: Request, player_id: int) -> PlayerDetail:
    pool = _get_ro_pool(request)
    rows = await _fetch(pool, _DETAIL, player_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player not found")
    row = rows[0]

    season_rows = await _fetch(pool, _SEASONS, player_id)
    seasons = [int(_row_get(r, "season")) for r in (season_rows or [])]

    return PlayerDetail(
        player_id=int(_row_get(row, "player_id")),
        name=_row_get(row, "name", ""),
        bats=_row_get(row, "bats"),
        throws=_row_get(row, "throws"),
        position=_row_get(row, "position"),
        seasons=seasons,
    )


__all__ = ["router", "PlayerHit", "PlayerDetail"]
