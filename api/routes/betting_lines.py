"""
api/routes/betting_lines.py
===========================
The line-movement and CLV reads of a game's stored odds.

    GET /api/betting/games/{game_pk}/line-movement   the opening-to-closing series per side and book
    GET /api/betting/games/{game_pk}/clv             the series that carry an entry-versus-close CLV

CLV (closing-line value) compares the opening price of a bet with its closing
price. Both routes read the stored ``raw.game_odds`` history
(``betting.line_movement``) from the Postgres pool. Only the database has the
series, so with no pool they answer 503; there is no mock. The aggregate router
in ``api/routes/betting.py`` includes this one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routes._common import _get_pool
from api.routes.betting_stored_odds import _VALID_MARKET_TYPES
from api.schemas import LineMovementModel
from betting.line_movement import fetch_line_movement

router = APIRouter()


class LineMovementResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/line-movement`` envelope.

    Carries one :class:`LineMovementModel` per (side, book) present in the stored
    ``raw.game_odds`` history for the (game_pk, market_type[, book]).
    """

    game_pk: int
    market_type: str
    book: str | None = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


class ClvSnapshotResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/clv`` envelope.

    A thin snapshot projection of /line-movement: the entry-vs-close CLV of every
    line-movement series that HAS one. Omitted: a series with < 2 quotes, a pair
    with a missing opposite-side price, and a run-line side whose spread moved
    between the open and the close. Each row carries the side / book identity plus
    the CLV's ``clv_prob`` / ``beat_close`` for a compact "did I beat the close"
    view without the full quote series.
    """

    game_pk: int
    market_type: str
    book: str | None = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


# ===========================================================================
# GET /line-movement
# ===========================================================================


def _validate_market_type(market_type: str) -> str:
    """422 on an unknown market_type for the line-movement / clv reads."""
    if market_type not in _VALID_MARKET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown market_type {market_type!r}; expected one of {list(_VALID_MARKET_TYPES)}"
            ),
        )
    return market_type


async def _fetch_movements(
    request: Request, *, game_pk: int, market_type: str, book: str | None
) -> list[Any]:
    """Run ``fetch_line_movement`` on a connection from the pool (503 with no pool).

    The pool is an asyncpg pool (``async with pool.acquire()``) or a connection
    that exposes ``fetch`` (the mock-pool test idiom).
    """
    pool = _get_pool(request)
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            return await fetch_line_movement(
                conn, game_pk=int(game_pk), market_type=market_type, book=book
            )
    return await fetch_line_movement(pool, game_pk=int(game_pk), market_type=market_type, book=book)


@router.get(
    "/games/{game_pk}/line-movement",
    response_model=LineMovementResponse,
    summary="Opening->closing line-movement time-series per side/book",
    description=(
        "Read the persisted raw.game_odds history for the (game_pk, market_type"
        "[, book]) and build the SIM-368 line-movement time-series: one series per "
        "(side, book) with the ordered quotes, the per-step + opening->closing "
        "deltas, the running implied-prob surface, the steam direction, the "
        "sharp-consensus flag, and the entry-vs-close CLV. A run line (SIM-549) "
        "carries run_line_shape: pair, two_bets or mixed. When either end is two "
        "separate bets, both ends are priced on their own price over the game's "
        "two-way margin. A side whose spread moved gets no CLV; clv_note says why. "
        "SIM-555: only the one-book rows are read (book = bp:<id>); each series and "
        "each quote carries the book's display name in book_name. "
        "numpy-free LineMovementModel list. 503 if no DB pool, 422 on a bad market_type."
    ),
)
async def get_game_line_movement(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: str | None = Query(
        None, description="Restrict to one stored book label, e.g. bp:12 (else all books)"
    ),
) -> LineMovementResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(request, game_pk=int(game_pk), market_type=mt, book=book)
    return LineMovementResponse(
        game_pk=int(game_pk),
        market_type=mt,
        book=book,
        count=len(movements),
        series=[LineMovementModel.from_dataclass(m) for m in movements],
    )


# ===========================================================================
# GET /clv: the series of /line-movement that carry a CLV
# ===========================================================================


@router.get(
    "/games/{game_pk}/clv",
    response_model=ClvSnapshotResponse,
    summary="Entry-vs-close CLV snapshot per side/book",
    description=(
        "A thin projection of /line-movement: the line-movement series that carry "
        "an entry-vs-close CLV (>= 2 quotes and both ends priceable). Each model's "
        "clv.clv_prob / clv.beat_close answers 'did the opening price beat the "
        "close' for that side/book. A run-line series has a CLV only when its "
        "spread was the same at both ends (SIM-549). clv_basis says whether it was "
        "priced as a pair or as two separate bets. SIM-555: the one-book rows only "
        "(book = bp:<id>). numpy-free. 503 if no DB pool, 422 on a bad market_type."
    ),
)
async def get_game_clv(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: str | None = Query(
        None, description="Restrict to one stored book label, e.g. bp:12 (else all books)"
    ),
) -> ClvSnapshotResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(request, game_pk=int(game_pk), market_type=mt, book=book)
    # Only the series that carry a CLV.
    with_clv = [m for m in movements if m.clv is not None]
    return ClvSnapshotResponse(
        game_pk=int(game_pk),
        market_type=mt,
        book=book,
        count=len(with_clv),
        series=[LineMovementModel.from_dataclass(m) for m in with_clv],
    )
