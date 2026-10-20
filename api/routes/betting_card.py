"""
api/routes/betting_card.py
==========================
The book's pregame lines for a slate card.

    GET /api/betting/games/{game_pk}/card-odds

The route gives the graded book's moneyline, run line and total for one game,
from the stored rows only, never the mock (``betting_stored_odds``): the
closing row once stored, else the latest current row while the game is in
Preview. On a final game, ``settled`` says how each line settled. The
aggregate router in ``api/routes/betting.py`` includes this one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import require_auth
from api.routes.betting_stored_odds import StoredRows, _read_stored_rows
from pipeline.odds_provider import book_display_name

log = logging.getLogger("api.routes.betting")

router = APIRouter()


#: The three full-game markets the slate card shows.
_CARD_MARKETS: tuple[str, ...] = ("moneyline", "runline", "total")

#: The final score of a game, for the settlement (``$1`` = game_pk).
_SQL_CARD_FINAL = """
    SELECT status, home_score_final, away_score_final
    FROM raw.games WHERE game_pk = $1
"""


class CardMoneyline(BaseModel):
    book: str
    line_type: str
    away: float
    home: float


class CardRunLineSide(BaseModel):
    line: float
    price: float


class CardRunLine(BaseModel):
    book: str
    line_type: str
    away: CardRunLineSide
    home: CardRunLineSide


class CardTotal(BaseModel):
    book: str
    line_type: str
    line: float
    over: float
    under: float


class CardSettlement(BaseModel):
    """How each pregame line settled on the final score."""

    away_score: int
    home_score: int
    moneyline: str | None = None  # "away" | "home"
    runline: str | None = None  # "away" | "home" | "push"
    total: str | None = None  # "over" | "under" | "push"


class CardOddsResponse(BaseModel):
    """The book's pregame lines for one slate card (SIM-519 Part I).

    Each market is the graded book's row (``GRADED_BOOK_PREFERENCE``): its
    closing row once stored, else its latest current row while the game is in
    Preview. A market with no stored row is null. Never the mock.
    """

    game_pk: int
    moneyline: CardMoneyline | None = None
    runline: CardRunLine | None = None
    total: CardTotal | None = None
    settled: CardSettlement | None = None


def _card_settlement(odds: CardOddsResponse, away_score: int, home_score: int) -> CardSettlement:
    """(pure) The winner side of each priced market on the final score."""
    out = CardSettlement(away_score=away_score, home_score=home_score)
    if odds.moneyline is not None and away_score != home_score:
        out.moneyline = "away" if away_score > home_score else "home"
    if odds.runline is not None:
        margin = away_score + odds.runline.away.line - home_score
        out.runline = "push" if margin == 0 else ("away" if margin > 0 else "home")
    if odds.total is not None:
        runs = away_score + home_score
        out.total = (
            "push" if runs == odds.total.line else ("over" if runs > odds.total.line else "under")
        )
    return out


def _card_odds_from_rows(game_pk: int, stored: StoredRows) -> CardOddsResponse:
    """(pure) The graded row of each card market → the card's lines."""

    def graded(market: str) -> Mapping[str, Any] | None:
        rows = stored.get(market) or []
        return rows[0] if rows else None

    resp = CardOddsResponse(game_pk=game_pk)
    if (row := graded("moneyline")) is not None:
        resp.moneyline = CardMoneyline(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            away=float(row["away_ml"]),
            home=float(row["home_ml"]),
        )
    if (row := graded("runline")) is not None:
        resp.runline = CardRunLine(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            away=CardRunLineSide(
                line=float(row["away_spread"]), price=float(row["away_spread_ml"])
            ),
            home=CardRunLineSide(
                line=float(row["home_spread"]), price=float(row["home_spread_ml"])
            ),
        )
    if (row := graded("total")) is not None:
        resp.total = CardTotal(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            line=float(row["total_line"]),
            over=float(row["over_ml"]),
            under=float(row["under_ml"]),
        )
    return resp


@router.get(
    "/games/{game_pk}/card-odds",
    response_model=CardOddsResponse,
    summary="The book's pregame lines for a slate card (SIM-519 Part I)",
    dependencies=[Depends(require_auth)],
    description=(
        "The graded book's moneyline, run line and total for one game, from the stored "
        "rows only (never the mock): the closing row once stored, else the latest "
        "current row while the game is in Preview. On a final game `settled` says how "
        "each line settled. 404 when the game has no stored row for any of the three."
    ),
)
async def get_card_odds(game_pk: int, request: Request) -> CardOddsResponse:
    stored = await _read_stored_rows(request, int(game_pk), _CARD_MARKETS)
    odds = _card_odds_from_rows(int(game_pk), stored)
    if odds.moneyline is None and odds.runline is None and odds.total is None:
        raise HTTPException(status_code=404, detail=f"no stored odds for game {game_pk}")
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        try:
            rows = await pool.fetch(_SQL_CARD_FINAL, int(game_pk))
        except Exception as exc:  # noqa: BLE001 -- the lines still serve without a result
            log.warning("card-odds: the final-score read failed for %s: %s", game_pk, exc)
            rows = []
        for raw in rows or []:
            row = raw if isinstance(raw, Mapping) else dict(raw)
            away, home = row.get("away_score_final"), row.get("home_score_final")
            if row.get("status") == "Final" and away is not None and home is not None:
                odds.settled = _card_settlement(odds, int(away), int(home))
    return odds
