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

from db.introspect import pg_column_exists_async
from pipeline.live.ingestion.game_state_builder import GameStateBuilder
from pipeline.live.ingestion.mlb_websocket import MLBGameWebSocket
from pipeline.odds_provider import (
    OddsProvider,
    get_odds_provider,
)
from pipeline.odds_row_guard import (
    RefusalTally,
    check_row,
)

log = logging.getLogger("live_ingestion")


class PipelineBase:
    """The pipeline's state, declared once so each odds mixin sees it."""

    _dsn: str | None
    _redis_url: str | None
    _odds: OddsProvider
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
        """SIM-519 Part G: whether ``raw.game_odds.last_seen_at`` exists (Alembic 0029), cached."""
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

        SIM-519 Part C: a slow vendor no longer stalls the live refresh. One
        thread keeps the provider's per-process caches single-threaded.
        """
        executor = getattr(self, "_vendor_executor", None)
        if executor is None:
            from concurrent.futures import ThreadPoolExecutor

            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="odds-vendor")
            self._vendor_executor = executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(fn, *args, **kwargs))

    def _odds_provider(self) -> OddsProvider:
        """
        SIM-370: Resolve the active odds provider.

        Normally set in __init__ (env-selected, defaults to MockOddsAPI), but
        pipelines built via __new__ (some tests) skip __init__, so fall back to
        the default provider lazily.  A real provider drops in behind the
        OddsProvider interface with no further changes here.
        """
        provider = getattr(self, "_odds", None)
        if provider is None:
            provider = get_odds_provider()
            self._odds = provider
        return provider

    # ------------------------------------------------------------------
    # SIM-555: the load guard on the live writers
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

    def _guard_rows(self, game_pk: int, rows: list[dict]) -> list[dict]:
        """The rows of one offer that the load guard keeps.

        The caller passes non-empty rows only. Each refused row is logged at
        INFO and counted in :meth:`_refusal_tally` by rule, market and book.
        """
        tally = self._refusal_tally()
        kept: list[dict] = []
        for row in rows:
            tally.offered()
            refusal = check_row(row)
            if refusal is None:
                kept.append(row)
                continue
            prop_stat = row.get("prop_stat")
            market = str(prop_stat if prop_stat is not None else row.get("market_type"))
            book = str(row.get("book"))
            tally.add(refusal, market, book)
            shown = market if prop_stat is None else f"{market} (player {row.get('player_id')})"
            log.info(
                "refused game %s %s/%s %s: %s",
                game_pk,
                row.get("line_type"),
                shown,
                book,
                refusal.message,
            )
        return kept
