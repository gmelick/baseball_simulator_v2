"""The state every part of the live pipeline shares."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import partial
from typing import Any

import aiohttp
import asyncpg
import redis.asyncio as aioredis

from betting.bettingpros.fetch_odds import BettingProsOddsProvider
from betting.load import odds_source
from betting.validate import RefusalTally
from db.introspect import pg_column_exists_async
from pipeline.live.ingestion.game_state_builder import GameStateBuilder
from pipeline.live.ingestion.mlb_websocket import MLBGameWebSocket

log = logging.getLogger("live_ingestion")


class PipelineBase:
    """The pipeline's state, declared once so each odds mixin sees it."""

    _dsn: str | None
    _redis_url: str | None
    _odds: BettingProsOddsProvider
    _db: asyncpg.Pool | None
    _redis: aioredis.Redis | None
    _broadcaster: Any
    _http: aiohttp.ClientSession | None
    _ws_clients: dict[int, MLBGameWebSocket]
    _refresh_locks: dict[int, asyncio.Lock]
    _last_resim_at_bat: dict[int, int]
    _completed_games: set[int]
    _builders: dict[int, GameStateBuilder]
    _last_prop_fetch: dict[int, datetime]
    _last_game_odds_fetch: dict[int, datetime]
    _last_pregame_fetch: dict[int, datetime]
    _odds_refusals: RefusalTally
    _schedule_task: asyncio.Task | None
    _running: bool

    async def _last_seen_available(self) -> bool:
        """Whether ``raw.game_odds.last_seen_at`` exists (Alembic 0029), cached."""
        known = getattr(self, "_last_seen_column", None)
        if known is not None:
            return bool(known)
        try:
            known = await pg_column_exists_async(self._db, "raw", "game_odds", "last_seen_at")
        except Exception:  # noqa: BLE001 -- unknown means the plain insert
            return False
        self._last_seen_column = known
        return known

    async def _off_loop(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous vendor read on the pipeline's one worker thread.

        A slow vendor does not stall the live refresh, and one thread keeps
        the provider's per-process caches single-threaded.
        """
        executor = getattr(self, "_vendor_executor", None)
        if executor is None:
            from concurrent.futures import ThreadPoolExecutor

            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="odds-vendor")
            self._vendor_executor = executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(fn, *args, **kwargs))

    def _odds_provider(self) -> BettingProsOddsProvider:
        """The odds provider: the one set in ``__init__``, else a new BettingPros provider.

        A pipeline built with ``__new__`` (some tests) skips ``__init__``, so the
        provider is made on first use.
        """
        provider = getattr(self, "_odds", None)
        if provider is None:
            provider = odds_source()
            self._odds = provider
        return provider

    # ------------------------------------------------------------------
    # The load guard on the live writers
    # ------------------------------------------------------------------

    def _refusal_tally(self) -> RefusalTally:
        """The pipeline's refusal tally, created on first use.

        Tests build the pipeline with ``__new__`` (no ``__init__``), so the
        tally cannot rely on the constructor.
        """
        tally = getattr(self, "_odds_refusals", None)
        if tally is None:
            tally = RefusalTally()
            self._odds_refusals = tally
        return tally
