"""The odds routes the API mounts: one game's odds and today's slate of odds."""

from __future__ import annotations

from datetime import date

import aiohttp
from fastapi import Depends
from fastapi.routing import APIRouter

from api.auth import require_auth
from pipeline.live.ingestion.constants import (
    GAME_TYPES,
    HTTP_TIMEOUT_S,
    SCHEDULE_URL,
)
from pipeline.live.ingestion.mock_odds import MockOddsAPI

# FastAPI router — mounts at /api/odds. SIM-443c: every route on it requires a
# login (cookie or API key), in the main app and in the live service's app.
odds_router = APIRouter(prefix="/api/odds", tags=["odds"], dependencies=[Depends(require_auth)])


@odds_router.get("/{game_pk}")
async def get_game_odds(game_pk: int) -> dict:
    """
    Returns mock betting lines for a game.  This handler calls MockOddsAPI
    directly (it predates the SIM-370 seam); the live ingestion path instead
    goes through get_odds_provider(), so ODDS_PROVIDER=bettingpros yields real
    lines (is_mock=False) there.
    """
    return MockOddsAPI.get_odds(game_pk)


@odds_router.get("/today/all")
async def get_todays_odds() -> list[dict]:
    """Returns mock odds for every game scheduled today."""
    async with aiohttp.ClientSession() as session:
        today = date.today().strftime("%Y-%m-%d")
        params = {"sportId": 1, "gameTypes": GAME_TYPES, "date": today}
        async with session.get(
            SCHEDULE_URL, params=params, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
        ) as resp:
            data = await resp.json()
    games = [
        g
        for d in data.get("dates", [])
        for g in d.get("games", [])
        if "rescheduleGameDate" not in g and "resumeGameDate" not in g
    ]
    return [MockOddsAPI.get_odds(g["gamePk"]) for g in games]
