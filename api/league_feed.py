"""The app's reader of the league's public endpoints (SIM-519 Parts A and I).

The app calls the league on the request path for two things only, and caches
both: the schedule of a date (the slate) and one game's live feed (the card
detail). This class owns the one ``aiohttp`` session those reads share.

The lifespan attaches an instance as ``app.state.league_feed``. A test app
attaches a fake with the same two coroutine methods, or none: with no feed the
slate serves the stored listing (``source = "db"``).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from pipeline.mlb_schedule import DEFAULT_HYDRATE, SCHEDULE_URL, schedule_params

GAME_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"


def feed_timeout_s() -> float:
    """The per-request timeout, ``SLATE_FEED_TIMEOUT_S`` (default 5 seconds)."""
    try:
        return float(os.environ.get("SLATE_FEED_TIMEOUT_S", "5"))
    except ValueError:
        return 5.0


class LeagueFeed:
    """Reads the schedule and the per-game feed over one lazy ``aiohttp`` session."""

    def __init__(self, *, timeout_s: float | None = None) -> None:
        self._timeout_s = feed_timeout_s() if timeout_s is None else timeout_s
        self._session: Any = None

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                headers={"User-Agent": "daily-diamond/1.0"},
            )
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def schedule_payload(self, start: date, end: date, hydrate: str = DEFAULT_HYDRATE) -> Any:
        """The raw schedule response for ``start..end``. Raises on any failure."""
        return await self._get_json(SCHEDULE_URL, schedule_params(start, end, hydrate))

    async def game_feed_payload(self, game_pk: int) -> Any:
        """The raw live feed of one game. Raises on any failure."""
        return await self._get_json(GAME_FEED_URL.format(game_pk=int(game_pk)))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
