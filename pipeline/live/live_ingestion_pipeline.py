"""The live ingestion pipeline: the day's games, their live state and their odds.

``LiveIngestionPipeline`` polls the MLB schedule, watches each live game's
socket, rebuilds the game state on every change, writes the game rows and the
lineups, stores the odds and signals a re-simulation at the end of every plate
appearance. ``create_app`` serves it on its own (the ``live`` service, which
publishes to Redis); the API mounts ``ws_router`` and ``odds_router``. The
parts live in ``pipeline/live/ingestion/``: the settings, the odds rows and
closing rows, the mock odds, the odds routes, the game-state builder, the
WebSocket parts and the game-odds and prop-odds mixins.

The flow
--------
  MLB Schedule API (30s poll)
       │
       ▼  new game_pk discovered
  MLBGameWebSocket ──────────────────────────────────────────────────┐
       │  any WS message = "something changed"                        │
       ▼                                                               │
  _refresh_game_state()                                               │
       │                                                               │
       ├── aiohttp → feed/live REST API                               │
       ├── GameStateBuilder.build()  → game_state JSONB               │
       ├── _upsert_lineup_state()   → sim.lineup_state                │
       ├── _upsert_game_record()    → raw.games                       │
       ├── _cache_to_redis()        → 60s TTL (rate-limit fallback)   │
       │                                                               │
       └── _should_resimulate()?                                       │
               │ fires at end of every plate appearance (PA complete)  │
               ▼                                                        │
       simulation_requested signal  ─────────────────────────────────┘
       (the simulation callback, when one is set)

       └── _broadcast_message()  → ws://host/ws/games/{game_pk}
               (the browsers watching this game)

Usage
-----
  python -m pipeline.live.run_live      # the live service (create_app)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRouter

# The odds routes and the live WebSocket require a login. api.auth
# imports only FastAPI and Starlette, so this import makes no cycle.
from api.auth import authenticate_websocket
from db.introspect import pg_column_exists_async
from pipeline.live.ingestion.base import PipelineBase
from pipeline.live.ingestion.closing_rows import (
    closing_candidates as closing_candidates,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.closing_rows import (
    closing_hash as closing_hash,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.closing_rows import (
    promote_closing_game_rows as promote_closing_game_rows,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.closing_rows import (
    promote_closing_prop_rows as promote_closing_prop_rows,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    _EASTERN,
    GAME_TYPES,
    HTTP_TIMEOUT_S,
    MLB_BASE,
    REDIS_TTL_DONE_S,
    REDIS_TTL_LIVE_S,
    RESIM_COOLDOWN_S,
    SCHEDULE_HYDRATE,
    SCHEDULE_POLL_S,
    SCHEDULE_URL,
)
from pipeline.live.ingestion.constants import (
    PREGAME_NEAR_START_CADENCE_S as PREGAME_NEAR_START_CADENCE_S,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    PREGAME_NEAR_START_WINDOW_S as PREGAME_NEAR_START_WINDOW_S,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    PROP_FETCH_CADENCE_S as PROP_FETCH_CADENCE_S,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    PROP_ROLE_BATTER as PROP_ROLE_BATTER,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    PROP_ROLE_BOTH as PROP_ROLE_BOTH,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.constants import (
    PROP_ROLE_PITCHER as PROP_ROLE_PITCHER,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.game_odds import GameOddsMixin
from pipeline.live.ingestion.game_state_builder import GameStateBuilder
from pipeline.live.ingestion.mlb_websocket import (
    ConnectionManager,
    MLBGameWebSocket,
)
from pipeline.live.ingestion.mock_odds import (
    MockOddsAPI as MockOddsAPI,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.odds_routes import odds_router
from pipeline.live.ingestion.odds_rows import (
    _GAME_ODDS_INSERT_SQL as _GAME_ODDS_INSERT_SQL,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.odds_rows import (
    _GAME_ODDS_RESTAMP_SQL as _GAME_ODDS_RESTAMP_SQL,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.odds_rows import (
    _PROP_ODDS_RESTAMP_SQL as _PROP_ODDS_RESTAMP_SQL,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.odds_rows import (
    _json_default,
    _jsonable_odds,
    _odds_facts_may_be_stale,
    _scheduled_start,
    pregame_odds_cadence_s,
)
from pipeline.live.ingestion.odds_rows import (
    insert_game_odds_rows as insert_game_odds_rows,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.odds_rows import (
    insert_prop_odds_rows as insert_prop_odds_rows,  # re-export: removed in the integration pass
)
from pipeline.live.ingestion.prop_odds import PropOddsMixin

# Odds/prop provider seam.  The pipeline obtains its odds source via
# get_odds_provider() (env-selected, defaults to MockOddsAPI) instead of
# hard-instantiating MockOddsAPI, so a real provider can drop in behind the
# OddsProvider interface without touching ingestion/CLV/persistence code.
from pipeline.odds_provider import (
    OddsProvider,
    get_odds_provider,
)
from pipeline.odds_row_guard import (
    CLOSING_STAMP_GRACE as CLOSING_STAMP_GRACE,  # re-export: removed in the integration pass
)

# The load guard. Every writer shows it each non-empty row before the
# row is persisted; a refused row is logged, counted and never written.
from pipeline.odds_row_guard import RefusalTally

# The type of the simulation callback. It MUST be an async function: a sync
# function would raise TypeError when the pipeline awaits it, or never run if
# its result is not awaited. LiveIngestionPipeline.__init__ checks it, so a
# misuse fails at construction, not at the first re-simulation signal.
SimulationCallback = Callable[[int, dict], Coroutine[Any, Any, None]]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("live_ingestion")


# Re-simulation is triggered automatically at the end of every plate appearance.
# A manual endpoint also allows the frontend to request a re-sim mid at-bat.


# ---------------------------------------------------------------------------
# The odds sources
# ---------------------------------------------------------------------------
# The provider names the books (one row per book, SIM-555). Each odds cycle
# asks the provider for every book's row of a market (``odds_rows_by_book`` /
# ``prop_rows_by_book``) and stores every row the load guard keeps, each under
# its own label (``'bp:<id>'`` from BettingPros; the mock's one row keeps
# ``'consensus'``).
#
# ODDS_PROVIDER selects the provider (``get_odds_provider()``): the
# deterministic MockOddsAPI is the default, and ODDS_PROVIDER=bettingpros
# switches to the real BettingProsOddsProvider.
#
# The 15 prop markets live in pipeline/odds_provider.py (PROP_STATS, split into
# PITCHER_PROP_STATS / BATTER_PROP_STATS, the prop-market work of SIM-421);
# MockOddsAPI._PROP_CONFIG and the raw.prop_odds CHECK constraint (migration
# 0022) list the same 15 values.


# Singleton shared across the FastAPI app
connection_manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Live Ingestion Pipeline
# ---------------------------------------------------------------------------


class LiveIngestionPipeline(GameOddsMixin, PropOddsMixin, PipelineBase):
    """
    Orchestrates the full live data ingestion loop.

    Lifecycle:
      start() → schedule poller discovers live games
               → MLBGameWebSocket spawned per game
               → each WS message triggers _refresh_game_state()
               → state written to DB + Redis + broadcast to frontend WS
      stop()  → cancel all tasks, close connections
    """

    def __init__(
        self,
        dsn: str | None = None,
        redis_url: str | None = None,
        simulation_callback: SimulationCallback | None = None,
        odds_provider: OddsProvider | None = None,
    ) -> None:
        """
        Parameters
        ----------
        dsn
            asyncpg-compatible PostgreSQL DSN.
        redis_url
            Redis connection URL (e.g. "redis://localhost:6379").
        simulation_callback
            Optional ``async def callback(game_pk: int, game_state: dict) -> None``
            called when a re-simulation is due. If None, the signal is logged.

        ``simulation_callback`` MUST be an async function.  Passing a
        plain sync function would either raise ``TypeError`` when the pipeline
        awaits it (best case) or silently never run if the returned value
        isn't awaited.  Both modes are hard to diagnose, so we check at
        __init__ time and raise immediately with a clear message.
        """
        # Fail fast on a sync callback.
        if simulation_callback is not None and not asyncio.iscoroutinefunction(simulation_callback):
            raise TypeError(
                "LiveIngestionPipeline.simulation_callback must be an async "
                "function (defined with `async def`).  Got "
                f"{type(simulation_callback).__name__}={simulation_callback!r}.  "
                "Phase 5 wiring tip: wrap a sync runner as `async def runner(game_pk, "
                "state): result = await asyncio.to_thread(sync_runner, game_pk, state); ...`"
            )

        # dsn and redis_url default to their environment variables.
        # Tests pass explicit values; production uses .env / docker-compose env.
        self._dsn = dsn or os.environ.get("BASEBALL_DB_DSN")
        self._redis_url = redis_url or os.environ.get("REDIS_URL")
        if not self._dsn:
            raise RuntimeError(
                "LiveIngestionPipeline: no DSN provided and BASEBALL_DB_DSN env "
                "var is not set.  Pass dsn=… or copy .env.example to .env."
            )
        if not self._redis_url:
            raise RuntimeError(
                "LiveIngestionPipeline: no Redis URL provided and REDIS_URL env "
                "var is not set.  Pass redis_url=… or copy .env.example to .env."
            )
        self._sim_cb: SimulationCallback | None = simulation_callback

        # Odds source.  Defaults to the env-selected provider
        # (MockOddsAPI unless ODDS_PROVIDER is set); tests inject a fake here.
        self._odds: OddsProvider = odds_provider or get_odds_provider()

        self._db: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        # The browser-message publisher; None = the in-process
        # connection_manager (see _broadcast_message).
        self._broadcaster: Any = None
        self._http: aiohttp.ClientSession | None = None

        # game_pk -> MLBGameWebSocket
        self._ws_clients: dict[int, MLBGameWebSocket] = {}
        # game_pk -> asyncio.Lock (prevents concurrent refreshes for same game)
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        # game_pk -> at_bat_number of the last PA that triggered a re-simulation.
        # Prevents re-simulating the same completed PA twice if multiple WS
        # messages arrive before the lock clears (e.g. a substitution event
        # immediately after the final pitch of a PA).
        self._last_resim_at_bat: dict[int, int] = {}
        # The game_pks of the games that have reached Final.
        # _sync_live_games() consults this set to short-circuit redundant
        # _upsert_game_record() calls for the rest of the day's polling.
        # Pre-populated on pipeline start from raw.games to survive restarts
        # without an upsert storm.
        self._completed_games: set[int] = set()
        # Per-game cached GameStateBuilder.  Built lazily on first
        # refresh; preserves last-processed at-bat index across refreshes so
        # _parse_play_history() only walks new plays.
        self._builders: dict[int, GameStateBuilder] = {}
        # Per-game timestamp of the last prop-odds fetch cycle.  The
        # WS feed signals on every pitch; prop lines move far more slowly, so
        # _persist_prop_odds_cycle() consults this map and skips the fetch
        # unless PROP_FETCH_CADENCE_S seconds have elapsed for this game.
        self._last_prop_fetch: dict[int, datetime] = {}
        # The same cadence clock for every game market.
        self._last_game_odds_fetch: dict[int, datetime] = {}
        # The pre-game cycle's own clock per game (PREGAME_ODDS_CADENCE_S).
        self._last_pregame_fetch: dict[int, datetime] = {}
        # The load guard's refusals, counted by rule, market and book.
        self._odds_refusals: RefusalTally = RefusalTally()

        self._schedule_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("Starting live ingestion pipeline …")
        self._db = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S))
        # Pre-populate _completed_games from today's already-Final
        # games so a pipeline restart mid-afternoon doesn't trigger an
        # upsert storm for every game finished earlier in the day.
        await self._hydrate_completed_games()
        self._running = True
        self._schedule_task = asyncio.create_task(self._schedule_poller(), name="schedule-poller")
        log.info("Pipeline started.")

    async def _hydrate_completed_games(self) -> None:
        """Load today's Final game_pks into _completed_games on boot."""
        if self._db is None:
            return
        try:
            rows = await self._db.fetch(
                """
                SELECT game_pk
                FROM   raw.games
                WHERE  game_date = CURRENT_DATE
                  AND  status    = 'Final'
                """,
            )
            for r in rows:
                self._completed_games.add(int(r["game_pk"]))
            if self._completed_games:
                log.info(
                    "SIM-105: hydrated %d already-final game_pks; will skip "
                    "their _upsert_game_record() polls.",
                    len(self._completed_games),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "SIM-105: could not hydrate _completed_games on boot: %s "
                "(restart will trigger one-time upsert storm)",
                exc,
            )

    async def stop(self) -> None:
        log.info("Stopping live ingestion pipeline …")
        self._running = False
        if self._schedule_task:
            self._schedule_task.cancel()
        for ws in self._ws_clients.values():
            ws.stop()
        self._ws_clients.clear()
        if self._http and not self._http.closed:
            await self._http.close()
        if self._redis:
            await self._redis.aclose()  # type: ignore[attr-defined]
        if self._db:
            await self._db.close()
        executor = getattr(self, "_vendor_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        log.info("Pipeline stopped.")

    # ------------------------------------------------------------------
    # Schedule poller — discovers newly-live games
    # ------------------------------------------------------------------

    async def _schedule_poller(self) -> None:
        """
        Polls the MLB schedule every SCHEDULE_POLL_S seconds.
        Spins up a WS subscription for any game that transitions to Live
        and tears it down when a game reaches Final.
        """
        while self._running:
            try:
                await self._sync_live_games()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.error("Schedule poll error: %s", exc)
            await asyncio.sleep(SCHEDULE_POLL_S)

    async def _sync_live_games(self) -> None:
        """Poll today's schedule: watch the live games, close the finished ones.

        A game that has not started (``Preview``) gets the pre-game
        odds cycle (:meth:`_persist_pregame_odds`), so every book's current
        line is stored before first pitch, not only once the game is live. An
        entry of a postponed, suspended or made-up game first drops the odds
        provider's cached facts of that game (:func:`_odds_facts_may_be_stale`).
        """
        # Yesterday through tomorrow in Eastern time, the league's day. The
        # container's clock is UTC, so date.today() would read tomorrow's
        # schedule from 8 pm Eastern while tonight's games are live.
        # Each entry is acted on by its own state; tomorrow's Preview games get
        # their lineups and pregame odds early.
        today = datetime.now(_EASTERN).date()
        params = {
            "sportId": 1,
            "gameTypes": GAME_TYPES,
            "startDate": (today - timedelta(days=1)).isoformat(),
            "endDate": (today + timedelta(days=1)).isoformat(),
            "hydrate": SCHEDULE_HYDRATE,
        }

        async with self._http.get(SCHEDULE_URL, params=params) as resp:
            data = await resp.json()

        current_live: set[int] = set()

        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                if _odds_facts_may_be_stale(game):
                    self._forget_odds_game(game.get("gamePk"))
                if "rescheduleGameDate" in game or "resumeGameDate" in game:
                    continue

                game_pk = game["gamePk"]
                status = game.get("status", {}).get("abstractGameState", "")

                if status == "Live":
                    current_live.add(game_pk)
                    if game_pk not in self._ws_clients:
                        log.info("New live game detected: %s", game_pk)
                        # The last pre-pitch row per market and book
                        # becomes the closing row BEFORE the watcher starts,
                        # so the live cycle's first in-play row comes after.
                        await self._mark_closing_rows(game_pk, game)
                        await self.on_game_live(game_pk, datetime.now(UTC))
                        await self._start_watching(game_pk)
                        # Immediately fetch initial state (don't wait for first WS msg)
                        asyncio.create_task(self._refresh_game_state(game_pk))
                    # A game that started with no stored lineup gets its
                    # starting rows from the feed (once; later polls are free).
                    asyncio.create_task(self._write_live_lineup(game))

                elif status == "Final":
                    if game_pk in self._ws_clients:
                        log.info("Game %s finished — closing WS", game_pk)
                        self._ws_clients[game_pk].stop()
                        del self._ws_clients[game_pk]
                        # Tear down the cached builder when its game
                        # ends — keeps _builders bounded by today's live slate.
                        self._builders.pop(game_pk, None)
                        # Do one final state refresh to capture the finished state
                        asyncio.create_task(self._refresh_game_state(game_pk))
                        # Mark the game completed AFTER the final
                        # upsert fires.  All future polls will skip this game.
                        self._completed_games.add(game_pk)

                elif status == "Preview":
                    # Store every book's current line before first
                    # pitch. The pre-game cycle keeps its own ten-minute clock
                    # per game (PREGAME_ODDS_CADENCE_S), so most polls read
                    # nothing from the vendor. The task upserts the game row
                    # itself before its first odds INSERT (the foreign key).
                    asyncio.create_task(self._persist_pregame_odds(game_pk, game))
                    # The published lineup and the probable
                    # pitchers, so the game can be simulated before it starts.
                    asyncio.create_task(self._write_preview_lineup(game))

                # Skip _upsert_game_record() for games that have
                # already transitioned to Final.  For a 15-game slate with
                # 12 finished games this avoids ~1,080 wasted DB writes per
                # 3-hour afternoon.  Status==Final transitions still fall
                # through (the membership check kicks in next poll).
                if game_pk in self._completed_games and status == "Final":
                    continue

                # Upsert every game (Preview/Live/Final) into raw.games
                asyncio.create_task(self._upsert_game_record(game))
                # A game already Final when first seen (yesterday's,
                # in the three-day window) is upserted once, then skipped.
                if status == "Final":
                    self._completed_games.add(game_pk)

        await self._write_heartbeat(sorted(current_live))

    async def _broadcast_message(self, game_pk: int, payload: dict) -> None:
        """Send a browser-bound message.

        In the app's process (``LIVE_PIPELINE_ENABLED``) the publisher is the
        in-process ``connection_manager``; in the ``live`` container it is a
        :class:`pipeline.live.broadcast.RedisBroadcaster`, and the app's bridge
        forwards the message to the browsers.
        """
        publisher = getattr(self, "_broadcaster", None) or connection_manager
        await publisher.broadcast(game_pk, payload)

    async def on_game_live(self, game_pk: int, first_pitch_at: datetime) -> None:
        """The Preview-to-Live hook, once per game.

        Records ``raw.games.first_pitch_at`` (Alembic 0029; the first write
        wins, so a restart mid-game keeps the true instant). The closing-row
        promotion runs just before it in the poll.
        """
        if getattr(self, "_db", None) is None:
            return
        try:
            await self._db.execute(
                "UPDATE raw.games SET first_pitch_at = COALESCE(first_pitch_at, $2) WHERE game_pk = $1",
                int(game_pk),
                first_pitch_at,
            )
        except Exception as exc:  # noqa: BLE001 -- before 0029 the column is absent
            log.debug("first_pitch_at not recorded for game %s: %s", game_pk, exc)

    async def _write_heartbeat(self, live_game_pks: list[int]) -> None:
        """Write ``live:heartbeat`` and ``live:watching`` for /ready and the gauge."""
        redis = getattr(self, "_redis", None)
        if redis is None:
            return
        try:
            from pipeline.live.broadcast import write_heartbeat

            await write_heartbeat(redis, live_game_pks)
        except Exception as exc:  # noqa: BLE001
            log.debug("heartbeat not written: %s", exc)

    async def _mark_closing_rows(self, game_pk: int, game: Mapping[str, Any]) -> tuple[int, int]:
        """Promote a game's closing rows the first time the poll sees it Live.

        The first-pitch instant is the poll's own time (UTC now): the poll runs
        every 30 seconds, so the instant is at most 30 seconds after MLB
        flipped the status. The scheduled start is the entry's ``gameDate``.
        The method calls :meth:`mark_closing_lines` and
        :meth:`mark_closing_prop_lines`, logs the two counts and never raises:
        a failed promotion is logged, and the nightly closing pass repairs it.
        A restart mid-game calls it again, with a later instant. A key that
        already holds a closing row is done, so the call leaves it alone. One
        limit: a key with no closing row yet (a book first posted in play, or a
        failed first promotion) can then take an in-play row with no stamp or
        a stamp inside the grace.
        Returns (game rows promoted, prop rows promoted).
        """
        first_pitch_at = datetime.now(UTC)
        scheduled_start = _scheduled_start(game)
        if getattr(self, "_db", None) is None:
            log.warning("closing rows not promoted for game %s: no database pool", game_pk)
            return 0, 0
        n_game = n_prop = 0
        try:
            n_game = await self.mark_closing_lines(
                game_pk, first_pitch_at, scheduled_start=scheduled_start
            )
        except Exception as exc:  # noqa: BLE001 — the poll must not stop on a failed promotion
            log.warning("closing game rows not promoted for game %s: %s", game_pk, exc)
        try:
            n_prop = await self.mark_closing_prop_lines(
                game_pk, first_pitch_at, scheduled_start=scheduled_start
            )
        except Exception as exc:  # noqa: BLE001 — the poll must not stop on a failed promotion
            log.warning("closing prop rows not promoted for game %s: %s", game_pk, exc)
        log.info(
            "closing rows promoted: game %s, %d game rows, %d prop rows",
            game_pk,
            n_game,
            n_prop,
        )
        return n_game, n_prop

    async def _start_watching(self, game_pk: int) -> None:
        self._refresh_locks[game_pk] = asyncio.Lock()
        # Pre-create the cached builder so the first refresh doesn't
        # take a (small) hit constructing one on the hot path.
        self._get_or_create_builder(game_pk)
        ws = MLBGameWebSocket(game_pk, on_update=self._refresh_game_state)
        self._ws_clients[game_pk] = ws
        ws.start()

    def _get_or_create_builder(self, game_pk: int) -> GameStateBuilder:
        """Cache one GameStateBuilder per game.  Disposed when the
        game transitions to Final (see _sync_live_games)."""
        builder = self._builders.get(game_pk)
        if builder is None:
            builder = GameStateBuilder(self._db)
            self._builders[game_pk] = builder
        return builder

    # ------------------------------------------------------------------
    # State refresh — called on every WS signal
    # ------------------------------------------------------------------

    async def _refresh_game_state(self, game_pk: int) -> None:
        """
        Core update cycle for a single game:
          1. Fetch latest feed/live from MLB API  (Redis as fallback)
          2. Build game_state JSONB
          3. Upsert sim.lineup_state
          4. Cache to Redis
          5. Fetch odds
          6. Broadcast to frontend WS clients
          7. Signal re-simulation if warranted

        The one odds row fetched here goes to the broadcast only. The
        stored odds come from :meth:`_persist_game_odds_cycle` (every book's
        row of every game market, on the cadence) and
        :meth:`_persist_prop_odds_cycle`. Before first pitch the same two
        cycles run from the schedule poll (:meth:`_persist_pregame_odds`).
        """
        lock = self._refresh_locks.setdefault(game_pk, asyncio.Lock())
        if lock.locked():
            # A refresh is already in flight for this game; skip this signal
            return

        async with lock:
            try:
                feed = await self._fetch_feed(game_pk)
                if feed is None:
                    return

                # Reuse the per-game builder so play history accumulates
                # incrementally instead of being rebuilt from scratch every WS msg.
                builder = self._get_or_create_builder(game_pk)
                game_state = await builder.build(feed)
                odds = await self._fetch_odds(game_pk)

                await self._upsert_lineup_state(game_pk, game_state)
                # Pass both feed (for _fetch_feed fallback) and
                # game_state (for resimulate endpoint) — see _cache_to_redis.
                await self._cache_to_redis(
                    game_pk,
                    feed,
                    game_state,
                    feed["gameData"]["status"]["abstractGameState"],
                )
                # Every market the book posts goes into the odds table (owner
                # ruling 2026-09-12): every book's row of all fifteen game
                # markets, on the prop cadence gate (60 s), never the per-pitch
                # signal. The ``odds`` row above is not stored.
                await self._persist_game_odds_cycle(game_pk)
                # The player-prop odds, on the same refresh cycle.
                # _persist_prop_odds_cycle() throttles itself to
                # PROP_FETCH_CADENCE_S per game, so it does not fire on every
                # pitch signal of the socket.
                await self._persist_prop_odds_cycle(game_pk, game_state)

                # Determine whether this update marks the end of a plate
                # appearance before broadcasting, so the frontend knows whether
                # to expect incoming simulation results.
                pa_ended, completed_at_bat = self._should_resimulate(game_pk, feed)

                # Broadcast full payload to all frontend subscribers.
                # resim_triggered=True tells the frontend to show a loading
                # indicator while it waits for new simulation results.
                # The odds row goes out JSON-safe (no guard-only keys;
                # the stamp as ISO-8601 text).
                broadcast_payload = {
                    "type": "game_state_update",
                    "game_pk": game_pk,
                    "game_state": game_state,
                    "odds": _jsonable_odds(odds),
                    "resim_triggered": pa_ended,
                }
                await self._broadcast_message(game_pk, broadcast_payload)

                if pa_ended:
                    self._last_resim_at_bat[game_pk] = completed_at_bat
                    await self._signal_resimulation(game_pk, game_state)

            except Exception as exc:
                log.error("Error refreshing game %s: %s", game_pk, exc, exc_info=True)

    async def _fetch_feed(self, game_pk: int) -> dict | None:
        """
        Fetches feed/live from MLB Stats API.
        Falls back to cached Redis state if the API call fails, per the
        rate-limiting mitigation in the risk register.
        """
        url = f"{MLB_BASE}/api/v1.1/game/{game_pk}/feed/live"
        try:
            async with self._http.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("feed/live returned %s for game %s", resp.status, game_pk)
        except aiohttp.ClientError as exc:
            log.warning("feed/live fetch failed game %s: %s — trying cache", game_pk, exc)

        # Redis fallback
        cached = await self._redis.get(f"game_feed:{game_pk}")
        if cached:
            log.info("Using cached state for game %s", game_pk)
            return json.loads(cached)

        log.error("No state available for game %s", game_pk)
        return None

    async def _persist_pregame_odds(self, game_pk: int, game: Mapping[str, Any]) -> int:
        """Store every book's current lines for a game before first pitch.

        ``_sync_live_games`` calls this on every schedule poll (every 30
        seconds) for each game in the ``Preview`` state; ``game`` is the
        game's hydrated schedule entry. The method has its own clock per game
        (``_last_pregame_fetch``): inside PREGAME_ODDS_CADENCE_S (ten minutes)
        of the last pass it returns 0 and calls neither cycle, so a game reads
        the vendor at most once every ten minutes before first pitch. Inside
        15 minutes of the entry's scheduled start the cadence is one
        minute (:func:`pregame_odds_cadence_s`), so the row the live marker
        promotes to closing at first pitch is fresh. The
        clock is set before the cycles run, so a failed pass also waits the
        full cadence, and a second poll that arrives while a pass is still
        running reads nothing.

        When the gate opens, the method runs the two cycles the live refresh
        runs: :meth:`_persist_game_odds_cycle` (every book's row of every game
        market) and the prop cycle, whose players come from
        :meth:`_pregame_prop_player_roles` (the probable pitchers, then the
        posted lineups). The rows are ``line_type='current'``, exactly as the
        live cycle writes them.

        The method runs as a detached task, so it logs a failure instead of
        raising it. Returns the rows sent (0 while the gate is closed).

        Review fix (2026-09-28): once the gate opens, the method first upserts
        the game's ``raw.games`` row and waits for it. ``raw.game_odds.game_pk``
        references ``raw.games``, and the schedule poll starts this task
        before its own upsert task, so on a game's first poll of the day the
        odds INSERT reached Postgres before the game row and failed the
        foreign key, and the ten-minute clock kept the moneyline from a retry.
        A failed upsert is logged and the cycles still run (the game row may
        exist already).
        """
        now = datetime.now(UTC)
        # Created lazily: the tests build the pipeline with __new__.
        clock = getattr(self, "_last_pregame_fetch", None)
        if clock is None:
            clock = {}
            self._last_pregame_fetch = clock
        last = clock.get(game_pk)
        # One minute apart inside 15 minutes of the scheduled start.
        cadence = pregame_odds_cadence_s(game, now)
        if last is not None and (now - last).total_seconds() < cadence:
            return 0
        clock[game_pk] = now
        if game.get("gamePk") is not None:
            try:
                await self._upsert_game_record(dict(game))
            except Exception as exc:  # noqa: BLE001 — a detached task must not raise
                log.warning("pre-game game-row upsert failed for game %s: %s", game_pk, exc)
        written = 0
        try:
            written += await self._persist_game_odds_cycle(game_pk)
            written += await self._persist_prop_roles_cycle(
                game_pk, self._pregame_prop_player_roles(game)
            )
        except Exception as exc:  # noqa: BLE001 — a detached task must not raise
            log.warning("pre-game odds cycle failed for game %s: %s", game_pk, exc)
        return written

    def _should_resimulate(self, game_pk: int, feed: dict) -> tuple[bool, int]:
        """
        Returns (should_resim: bool, at_bat_number: int).

        Re-simulation fires at the end of every plate appearance — detected by
        checking whether the current play's `about.isComplete` flag is True in
        the feed/live response.  We also guard against double-triggering: if
        the same at_bat_number already fired a resim this game (tracked in
        self._last_resim_at_bat), we skip it even if isComplete is still True
        (which it will be until the next PA starts).

        This means:
          - Every walk, strikeout, hit, HBP, fielder's choice, sac fly, etc.
            triggers a resim the moment the play completes.
          - Multiple WS messages during the same PA (mid-count updates, mound
            visits, pickoff attempts) do NOT trigger a resim.
          - The first message after a new PA begins will not trigger a resim
            until that PA itself completes.
        """
        game_status = feed.get("gameData", {}).get("status", {}).get("abstractGameState", "")
        if game_status not in ("Live",):
            return False, -1

        live_data = feed.get("liveData", {})
        current_play = live_data.get("plays", {}).get("currentPlay", {})
        about = current_play.get("about", {})

        is_complete = about.get("isComplete", False)
        at_bat_number = about.get("atBatIndex", -1) + 1  # 0-indexed in API, 1-indexed here

        if not is_complete:
            return False, at_bat_number

        # Guard: don't re-trigger for the same completed PA
        if self._last_resim_at_bat.get(game_pk) == at_bat_number:
            return False, at_bat_number

        return True, at_bat_number

    async def _signal_resimulation(self, game_pk: int, game_state: dict) -> None:
        """Call the simulation callback with the game state; with no callback
        set, log the signal."""
        if self._sim_cb:
            try:
                await self._sim_cb(game_pk, game_state)
            except Exception as exc:
                log.error("Simulation callback failed game %s: %s", game_pk, exc)
        else:
            log.info(
                "Re-sim signal (PA complete): game %s | at_bat %s | inning %s | score %s-%s",
                game_pk,
                game_state.get("play_history", [{}])[-1].get("at_bat_number", "?")
                if game_state.get("play_history")
                else "?",
                game_state.get("inning"),
                game_state.get("home_score"),
                game_state.get("away_score"),
            )

    # ------------------------------------------------------------------
    # Database writes
    # ------------------------------------------------------------------

    async def _upsert_lineup_state(self, game_pk: int, game_state: dict) -> None:
        """
        Upserts sim.lineup_state for this game.  Creates a new live session
        if one doesn't exist; updates the existing one otherwise.
        """
        game_state_json = json.dumps(game_state)
        await self._db.execute(
            """
            INSERT INTO sim.lineup_state
                (game_pk, is_live_game, game_state, expires_at)
            VALUES
                ($1, TRUE, $2::jsonb, NOW() + INTERVAL '24 hours')
            ON CONFLICT (game_pk) WHERE is_live_game = TRUE
            DO UPDATE SET
                game_state = EXCLUDED.game_state,
                updated_at = NOW(),
                expires_at = NOW() + INTERVAL '24 hours'
            """,
            game_pk,
            game_state_json,
        )

    async def _upsert_game_record(self, game: dict) -> None:
        """
        Upserts a row in raw.games for the given game dict from the schedule API.
        Handles Preview → Live → Final transitions.
        """
        gd = game
        status_map = {
            "Preview": "Preview",
            "Live": "Live",
            "Final": "Final",
            "Postponed": "Postponed",
            "Cancelled": "Cancelled",
            "Suspended": "Suspended",
        }
        status = status_map.get(gd.get("status", {}).get("abstractGameState", "Preview"), "Preview")

        # The slate keys games on the league's official date (the local
        # calendar day). gameDate is the UTC start: a West Coast night game's
        # gameDate falls on the next UTC day.
        game_date = date.fromisoformat((gd.get("officialDate") or gd["gameDate"])[:10])

        # Supply season.  raw.games.season is INTEGER NOT NULL and is
        # half of all three composite FKs — (venue_id, season) -> raw.venues and
        # (home/away_team_id, season) -> raw.teams — but it was never included in
        # the INSERT, so EVERY game this pipeline had not seen before failed with
        # NotNullViolationError.  The failure was silent because the call site is
        # fire-and-forget (asyncio.create_task), and the ON CONFLICT DO UPDATE
        # path kept working for games the ETL had already loaded — so status
        # transitions looked healthy while no new game was ever created.
        #
        # The schedule API returns season as a STRING ("2024"), hence the int().
        # Fall back to the game-date year when the key is absent/unparseable so a
        # thin payload can never re-break the insert on a NOT NULL column.
        try:
            season = int(gd["season"])
        except (KeyError, TypeError, ValueError):
            season = game_date.year

        await self._db.execute(
            """
            INSERT INTO raw.games (
                game_pk, season, game_date, game_type, status,
                venue_id, home_team_id, away_team_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (game_pk) DO UPDATE SET
                status     = EXCLUDED.status,
                updated_at = NOW()
            """,
            gd["gamePk"],
            season,
            game_date,
            gd.get("gameType", "R"),
            status,
            # Fall back to None (not 0) when the schedule API omits the
            # venue.  raw.games.venue_id is now nullable; venue_backfill_job.py
            # fills these once the MLB API publishes the venue assignment.
            gd.get("venue", {}).get("id") or None,
            gd.get("teams", {}).get("home", {}).get("team", {}).get("id", 0),
            gd.get("teams", {}).get("away", {}).get("team", {}).get("id", 0),
        )
        await self._upsert_schedule_fields(gd)

    async def _upsert_schedule_fields(self, entry: dict) -> None:
        """The schedule's fields on the game row (Alembic 0029).

        Start time, doubleheader code and number, the detailed state and both
        probable pitchers. Best effort and separate from the INSERT, so a
        database without the 0029 columns keeps every game row it gets today.
        """
        exists = getattr(self, "_schedule_columns_exist", None)
        try:
            if exists is None:
                exists = await pg_column_exists_async(self._db, "raw", "games", "schedule_seen_at")
                self._schedule_columns_exist = exists
            if not exists:
                return
            from pipeline.mlb_schedule import parse_game

            g = parse_game(entry)
            await self._db.execute(
                """
                UPDATE raw.games SET
                    start_utc                = $2,
                    start_time_tbd           = $3,
                    double_header            = $4,
                    game_number              = $5,
                    detailed_state           = $6,
                    home_probable_pitcher_id = $7,
                    away_probable_pitcher_id = $8,
                    schedule_seen_at         = NOW()
                 WHERE game_pk = $1
                """,
                g.game_pk,
                g.start_utc,
                g.start_time_tbd,
                g.double_header[:1] or None,
                g.game_number,
                (g.detailed_state or None) and g.detailed_state[:40],
                g.home.probable_pitcher.player_id if g.home.probable_pitcher else None,
                g.away.probable_pitcher.player_id if g.away.probable_pitcher else None,
            )
        except Exception as exc:  # noqa: BLE001 -- never cost the game row its upsert
            log.debug("schedule fields not written for game %s: %s", entry.get("gamePk"), exc)

    async def _write_preview_lineup(self, game: dict) -> None:
        """Make a Preview game simulable.

        The game row first and awaited (``raw.game_lineups.game_pk`` is a
        foreign key), then the published-lineup writer
        (:class:`pipeline.live.lineup_writer.PublishedLineupWriter`), which reads
        the game's feed only when the schedule's lineups or probables changed.
        """
        try:
            await self._upsert_game_record(game)
        except Exception as exc:  # noqa: BLE001
            log.warning("lineup: game row not upserted for %s: %s", game.get("gamePk"), exc)
            return
        source = await self._get_lineup_writer().on_preview(game)
        if source is not None:
            await self._on_lineup_published(int(game["gamePk"]), source)

    async def _write_live_lineup(self, game: dict) -> None:
        """Make a game that started with no stored lineup simulable (from its feed's box)."""
        source = await self._get_lineup_writer().on_live(game)
        if source is not None:
            await self._on_lineup_published(int(game["gamePk"]), source)

    def _get_lineup_writer(self) -> Any:
        """The one published-lineup writer of this pipeline (built on first use)."""
        writer = getattr(self, "_lineup_writer", None)
        if writer is None:
            from pipeline.live.lineup_writer import PublishedLineupWriter

            async def get_json(url: str) -> Any:
                async with self._http.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            writer = PublishedLineupWriter(self._db, get_json)
            self._lineup_writer = writer
        return writer

    async def _on_lineup_published(self, game_pk: int, source: str) -> None:
        """Tell the game page a lineup is in (the Redis bridge carries it)."""
        broadcast = getattr(self, "_broadcast_message", None)
        if broadcast is None:
            return
        try:
            await broadcast(
                game_pk, {"type": "lineup_published", "game_pk": game_pk, "source": source}
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("lineup_published not broadcast for %s: %s", game_pk, exc)

    # ------------------------------------------------------------------
    # Redis cache
    # ------------------------------------------------------------------

    async def _cache_to_redis(
        self, game_pk: int, feed: dict, game_state: dict, status: str
    ) -> None:
        """
        Writes two Redis keys per game refresh:

        1. ``game_feed:{game_pk}``   — raw MLB Stats API feed/live dict.
           Used by ``_fetch_feed()`` as a rate-limit fallback: when a
           subsequent HTTP call returns 429/error, the pipeline returns the
           last known feed rather than dropping the refresh cycle entirely.

        2. ``game_state:{game_pk}``  — built game_state JSONB.
           Used by the manual resimulate endpoint
           (POST /api/games/{game_pk}/resimulate) to serve the last known
           state without hitting the DB.

        ``_fetch_feed()`` reads ``game_feed:{game_pk}`` as its rate-limit
        fallback, so both keys must be written.
        """
        if self._redis is None:
            return
        ttl_secs = REDIS_TTL_DONE_S if status == "Final" else REDIS_TTL_LIVE_S
        try:
            await self._redis.setex("game_feed:" + str(game_pk), ttl_secs, json.dumps(feed))
            await self._redis.setex("game_state:" + str(game_pk), ttl_secs, json.dumps(game_state))
        except Exception as exc:
            log.warning("Redis cache write failed for game %s: %s", game_pk, exc)

    @property
    def live_game_pks(self) -> list:
        return list(self._ws_clients.keys())

    def is_watching(self, game_pk: int) -> bool:
        return game_pk in self._ws_clients


ws_router = APIRouter(prefix="/ws", tags=["live"])


@ws_router.websocket("/games/{game_pk}")
async def game_state_ws(websocket: WebSocket, game_pk: int) -> None:
    # require_auth takes a Request, and FastAPI does not supply one
    # in a WebSocket scope, so on ws_router it would fail every handshake with
    # a TypeError. ws_router therefore carries no router dependency, and the
    # handler checks the handshake's cookie or key first. A refused handshake
    # is closed with code 1008 before it is accepted.
    if await authenticate_websocket(websocket) is None:
        return
    await connection_manager.connect(game_pk, websocket)
    # In the app, the live service's messages arrive on Redis;
    # the bridge subscribes to this game's channel while anyone watches it.
    bridge = getattr(websocket.app.state, "live_bridge", None)
    if bridge is not None:
        await bridge.retain(game_pk)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data.strip().lower() == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(game_pk, websocket)
        if bridge is not None:
            await bridge.release(game_pk)


def create_app(
    dsn=None, redis_url=None, simulation_callback=None, *, publish_to_redis: bool = False
) -> FastAPI:
    """The live service's app: the pipeline, the WebSocket and odds routes, a
    rate-limited resimulate endpoint and ``/health``.

    The ``live`` container runs this app (``python -m
    pipeline.live.run_live``) with ``publish_to_redis=True``: every
    browser-bound message goes to the game's Redis channel, which the main
    app's bridge forwards to the browsers. ``/health`` answers with the age of
    the last schedule poll.
    """
    pipeline = LiveIngestionPipeline(
        dsn=dsn,
        redis_url=redis_url,
        simulation_callback=simulation_callback,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pipeline.start()
        if publish_to_redis and pipeline._redis is not None:
            from pipeline.live.broadcast import RedisBroadcaster

            pipeline._broadcaster = RedisBroadcaster(pipeline._redis, default=_json_default)
        yield
        await pipeline.stop()

    app = FastAPI(title="MLB Live Ingestion", lifespan=lifespan)
    app.include_router(ws_router)
    app.include_router(odds_router)

    @app.get("/health")
    async def live_health(response: Response):
        from pipeline.live.broadcast import HEARTBEAT_STALE_S, heartbeat_age_s

        age = None
        if pipeline._redis is not None:
            try:
                age = await heartbeat_age_s(pipeline._redis)
            except Exception:  # noqa: BLE001
                age = None
        # Before the first poll completes the service is starting, not failing.
        ok = age is not None and age <= HEARTBEAT_STALE_S
        if not ok:
            response.status_code = 503
        return {"status": "ok" if ok else "stale", "heartbeat_age_s": age}

    @app.get("/api/pipeline/status")
    async def pipeline_status():
        return {
            "live_games": pipeline.live_game_pks,
            "live_game_count": len(pipeline.live_game_pks),
        }

    @app.post("/api/games/{game_pk}/resimulate")
    async def manual_resimulate(game_pk: int, response: Response):
        # Per-game Redis cooldown (resim_cooldown:<game_pk>).
        if pipeline._redis:
            cooldown_key = "resim_cooldown:" + str(game_pk)
            ttl = await pipeline._redis.ttl(cooldown_key)
            if ttl is not None and ttl > 0:
                response.status_code = 429
                return {
                    "status": "rate_limited",
                    "retry_after_seconds": int(ttl),
                    "detail": (
                        "Manual re-simulation for game "
                        + str(game_pk)
                        + " is on cooldown. Try again in "
                        + str(int(ttl))
                        + "s."
                    ),
                }
            await pipeline._redis.setex(cooldown_key, RESIM_COOLDOWN_S, "1")

        if not pipeline.is_watching(game_pk):
            cached = (
                await pipeline._redis.get("game_state:" + str(game_pk)) if pipeline._redis else None
            )
            if not cached:
                return {
                    "status": "error",
                    "detail": "game " + str(game_pk) + " is not live and has no cached state",
                }
            game_state = json.loads(cached)
            await pipeline._signal_resimulation(game_pk, game_state)
            return {"status": "triggered", "source": "cache"}

        feed = await pipeline._fetch_feed(game_pk)
        if feed is None:
            return {"status": "error", "detail": "could not fetch game state"}

        builder = pipeline._get_or_create_builder(game_pk)
        game_state = await builder.build(feed)
        await pipeline._signal_resimulation(game_pk, game_state)
        await pipeline._broadcast_message(
            game_pk,
            {
                "type": "resim_pending",
                "game_pk": game_pk,
                "trigger": "manual",
                "inning": game_state.get("inning"),
                "half": game_state.get("half"),
                "balls": game_state.get("balls"),
                "strikes": game_state.get("strikes"),
                "outs": game_state.get("outs"),
            },
        )
        return {"status": "triggered", "source": "live"}

    return app


def run(dsn=None, redis_url=None, simulation_callback=None, host="0.0.0.0", port=8001) -> None:
    import uvicorn

    app = create_app(dsn=dsn, redis_url=redis_url, simulation_callback=simulation_callback)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
