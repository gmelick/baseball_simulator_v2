"""
live_ingestion_pipeline.py
==========================
Step 1.3 — Live Data Ingestion Pipeline
MLB Baseball Simulation Platform

Architecture
------------
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
               │ inning >= 7  OR  |score_diff| <= 2                   │
               ▼                                                        │
       simulation_requested signal  ─────────────────────────────────┘
       (consumed by Phase 5 runner)

       └── _broadcast_to_clients()  → ws://host/ws/games/{game_pk}
               (frontend WebSocket clients subscribed to this game)

MockOddsAPI
  GET /api/odds/{game_pk}
  Deterministic mock seeded on game_pk.  Drop-in replacement:
  swap _fetch_odds() in LiveIngestionPipeline to call a real provider.

Additional DDL (run once):
  raw.game_odds — stores odds snapshots per game

Dependencies
------------
  pip install fastapi uvicorn websockets aiohttp asyncpg redis

Usage
-----
  # Standalone (no FastAPI — useful for testing the pipeline alone):
  python live_ingestion_pipeline.py

  # Integrated with FastAPI app (in your main app.py):
  from live_ingestion_pipeline import create_app
  app = create_app(dsn="postgresql://...", redis_url="redis://localhost")

  # Or mount into existing app:
  from live_ingestion_pipeline import (
      lifespan, ws_router, odds_router, connection_manager
  )
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import aiohttp
import asyncpg
import redis.asyncio as aioredis
import websockets
import websockets.exceptions
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRouter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("live_ingestion")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MLB_BASE          = "https://statsapi.mlb.com"
MLB_WS_TEMPLATE   = "wss://ws.statsapi.mlb.com/api/v1/game/push/subscribe/gameday/{game_pk}"
SCHEDULE_URL      = f"{MLB_BASE}/api/v1/schedule"

SCHEDULE_POLL_S   = 30      # how often to check for newly-live games
WS_RECONNECT_BASE = 2.0     # base seconds for WS reconnect backoff
WS_RECONNECT_MAX  = 60.0    # cap on WS reconnect backoff
HTTP_TIMEOUT_S    = 10      # aiohttp request timeout
REDIS_TTL_LIVE_S  = 60      # cache TTL for live game states
REDIS_TTL_DONE_S  = 3600    # cache TTL for completed game states

# Re-simulation is triggered automatically at the end of every plate appearance.
# A manual endpoint also allows the frontend to request a re-sim mid at-bat.

GAME_TYPES = ["R", "F", "D", "L", "W", "C", "P"]

# ---------------------------------------------------------------------------
# DDL helpers  (run once against your database before starting the pipeline)
# ---------------------------------------------------------------------------

GAME_ODDS_DDL = """
CREATE TABLE IF NOT EXISTS raw.game_odds (
    id              BIGSERIAL       PRIMARY KEY,
    game_pk         INTEGER         NOT NULL REFERENCES raw.games(game_pk),
    fetched_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    source          VARCHAR(50)     NOT NULL DEFAULT 'mock',
    is_mock         BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Moneyline (American odds: negative = favourite)
    home_ml         INTEGER,        -- e.g. -150
    away_ml         INTEGER,        -- e.g. +130

    -- Run line / spread
    home_spread     FLOAT,          -- e.g. -1.5
    home_spread_ml  INTEGER,        -- e.g. -110
    away_spread     FLOAT,          -- e.g. +1.5
    away_spread_ml  INTEGER,        -- e.g. -110

    -- Total (over/under)
    total_line      FLOAT,          -- e.g.  8.5
    over_ml         INTEGER,        -- e.g. -110
    under_ml        INTEGER         -- e.g. -110
);

CREATE INDEX IF NOT EXISTS idx_game_odds_game_pk
    ON raw.game_odds(game_pk, fetched_at DESC);

COMMENT ON TABLE raw.game_odds IS
    'Odds snapshots per game. source=mock until Phase 7 real provider integration.';
"""


# ---------------------------------------------------------------------------
# Mock Odds API
# ---------------------------------------------------------------------------

class MockOddsAPI:
    """
    Deterministic mock that generates plausible MLB betting lines seeded on
    game_pk.  All values are consistent within a session (same game_pk always
    returns the same lines) so the frontend never sees odds flicker.

    To swap in a real provider in Phase 7:
      1. Replace `_fetch_odds()` in LiveIngestionPipeline with a real HTTP call.
      2. Keep this class for local dev / testing.

    American odds encoding:
      Favourite:  odds = -(prob / (1-prob)) * 100  (negative integer)
      Underdog:   odds = ((1-prob) / prob) * 100   (positive integer)
    """

    @staticmethod
    def _prob_to_american(prob: float) -> int:
        prob = max(0.01, min(0.99, prob))
        if prob >= 0.5:
            return round(-(prob / (1.0 - prob)) * 100)
        else:
            return round(((1.0 - prob) / prob) * 100)

    @staticmethod
    def get_odds(game_pk: int) -> dict[str, Any]:
        rng = random.Random(game_pk)

        # Home win probability: slight home-field edge built in
        home_win_prob = rng.uniform(0.38, 0.64)
        away_win_prob = 1.0 - home_win_prob

        home_ml = MockOddsAPI._prob_to_american(home_win_prob)
        away_ml = MockOddsAPI._prob_to_american(away_win_prob)

        # Run line: home -1.5 if heavy fav, else pick randomly
        if home_win_prob > 0.58:
            home_spread, away_spread = -1.5, +1.5
        elif home_win_prob < 0.42:
            home_spread, away_spread = +1.5, -1.5
        else:
            home_spread = rng.choice([-1.5, +1.5])
            away_spread = -home_spread

        # Standard -110 juice on spread with slight variance
        spread_juice = rng.randint(-115, -105)

        # Total: center around 8.5, range 7.5–10.5
        total_line = round(rng.uniform(7.5, 10.5) * 2) / 2  # nearest 0.5
        total_juice = rng.randint(-115, -105)

        return {
            "game_pk":        game_pk,
            "source":         "mock",
            "is_mock":        True,
            "home_ml":        home_ml,
            "away_ml":        away_ml,
            "home_spread":    home_spread,
            "home_spread_ml": spread_juice,
            "away_spread":    away_spread,
            "away_spread_ml": spread_juice,
            "total_line":     total_line,
            "over_ml":        total_juice,
            "under_ml":       total_juice,
        }


# FastAPI router — mounts at /api/odds
odds_router = APIRouter(prefix="/api/odds", tags=["odds"])

@odds_router.get("/{game_pk}")
async def get_game_odds(game_pk: int) -> dict:
    """
    Returns mock betting lines for a game.  In Phase 7, replace this handler
    with a call to a real odds provider and flip is_mock=False.
    """
    return MockOddsAPI.get_odds(game_pk)

@odds_router.get("/today/all")
async def get_todays_odds() -> list[dict]:
    """Returns mock odds for every game scheduled today."""
    async with aiohttp.ClientSession() as session:
        today = date.today().strftime("%Y-%m-%d")
        params = {"sportId": 1, "gameTypes": GAME_TYPES, "date": today}
        async with session.get(SCHEDULE_URL, params=params, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as resp:
            data = await resp.json()
    games = [
        g for d in data.get("dates", []) for g in d.get("games", [])
        if "rescheduleGameDate" not in g and "resumeGameDate" not in g
    ]
    return [MockOddsAPI.get_odds(g["gamePk"]) for g in games]


# ---------------------------------------------------------------------------
# Game State Builder
# ---------------------------------------------------------------------------

class GameStateBuilder:
    """
    Transforms the raw MLB Stats API `feed/live` JSON into the game_state
    JSONB structure required by sim.lineup_state.

    Required top-level keys (from schema comment):
      inning, half, outs, balls, strikes,
      home_score, away_score, batting_team_id, fielding_team_id,
      on_1b, on_2b, on_3b, current_batter_id, current_pitcher_id,
      home_lineup, away_lineup, home_bullpen, away_bullpen,
      home_bench, away_bench, play_history
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._db = db_pool

    async def build(self, feed: dict[str, Any]) -> dict[str, Any]:
        """
        Main entry point.  `feed` is the parsed JSON from
        GET /api/v1.1/game/{game_pk}/feed/live
        """
        game_data = feed["gameData"]
        live_data = feed["liveData"]

        game_pk      = feed["gamePk"]
        game_status  = game_data["status"]["abstractGameState"]   # Live / Final / Preview
        linescore    = live_data.get("linescore", {})
        boxscore     = live_data.get("boxscore", {})
        current_play = live_data["plays"].get("currentPlay", {})

        home_team_id = game_data["teams"]["home"]["id"]
        away_team_id = game_data["teams"]["away"]["id"]

        # ---- Inning state --------------------------------------------------
        inning       = linescore.get("currentInning", 1)
        half         = linescore.get("inningHalf", "Top")          # "Top" | "Bottom"
        outs         = linescore.get("outs", 0)
        balls        = current_play.get("count", {}).get("balls", 0)
        strikes      = current_play.get("count", {}).get("strikes", 0)
        home_score   = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        away_score   = linescore.get("teams", {}).get("away", {}).get("runs", 0)

        batting_team_id  = away_team_id if half == "Top" else home_team_id
        fielding_team_id = home_team_id if half == "Top" else away_team_id

        # ---- Baserunners ---------------------------------------------------
        offense  = current_play.get("matchup", {})
        runners  = linescore.get("offense", {})
        on_1b = runners.get("first",  {}).get("id")
        on_2b = runners.get("second", {}).get("id")
        on_3b = runners.get("third",  {}).get("id")

        # ---- Current participants ------------------------------------------
        current_batter_id  = offense.get("batter",  {}).get("id")
        current_pitcher_id = offense.get("pitcher", {}).get("id")
        # Note: matchup is batter-centric; pitcher is the defending pitcher
        current_pitcher_id = (
            current_play.get("matchup", {}).get("pitcher", {}).get("id")
            or live_data.get("linescore", {}).get("defense", {}).get("pitcher", {}).get("id")
        )

        # ---- Lineups -------------------------------------------------------
        home_lineup, home_bullpen, home_bench = await self._parse_roster(
            boxscore, game_pk, side="home", team_id=home_team_id
        )
        away_lineup, away_bullpen, away_bench = await self._parse_roster(
            boxscore, game_pk, side="away", team_id=away_team_id
        )

        # ---- Play history (at-bat level summary) ---------------------------
        play_history = self._parse_play_history(live_data["plays"].get("allPlays", []))

        # ---- Linescore snapshot (stored for frontend convenience) ----------
        inning_scores = self._parse_linescore(linescore)

        return {
            # Core state
            "game_pk":            game_pk,
            "game_status":        game_status,
            "inning":             inning,
            "half":               half,
            "outs":               outs,
            "balls":              balls,
            "strikes":            strikes,
            "home_score":         home_score,
            "away_score":         away_score,
            "batting_team_id":    batting_team_id,
            "fielding_team_id":   fielding_team_id,
            "on_1b":              on_1b,
            "on_2b":              on_2b,
            "on_3b":              on_3b,
            "current_batter_id":  current_batter_id,
            "current_pitcher_id": current_pitcher_id,
            # Rosters
            "home_lineup":   home_lineup,
            "away_lineup":   away_lineup,
            "home_bullpen":  home_bullpen,
            "away_bullpen":  away_bullpen,
            "home_bench":    home_bench,
            "away_bench":    away_bench,
            # History / display
            "play_history":    play_history,
            "inning_scores":   inning_scores,
            "last_updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _parse_roster(
        self,
        boxscore: dict,
        game_pk: int,
        side: str,
        team_id: int,
    ) -> tuple[list, list, list]:
        """
        Returns (lineup, bullpen, bench) for one team.

        lineup  — [{player_id, batting_order, position, name, stats_today}]
        bullpen — [{player_id, name, pitch_count_today, days_rest, role}]
        bench   — [{player_id, name, position}]
        """
        team_box = boxscore.get("teams", {}).get(side, {})
        players  = team_box.get("players", {})          # keyed "ID{player_id}"
        batting_order = team_box.get("battingOrder", [])

        lineup:  list[dict] = []
        bullpen: list[dict] = []
        bench:   list[dict] = []

        # Collect pitcher IDs already used in this game (for pitch counts)
        used_pitcher_ids: set[int] = set()

        for key, player_data in players.items():
            pid        = player_data["person"]["id"]
            name       = player_data["person"]["fullName"]
            position   = player_data.get("position", {}).get("abbreviation", "")
            stats      = player_data.get("stats", {})
            game_stats = player_data.get("gameStats", {})
            seq        = player_data.get("battingOrder")         # "100", "200" … or None

            # Batting lineup
            if seq is not None:
                batting_pos = int(seq) // 100   # "100" → 1
                batting_stats = stats.get("batting", {}).get("summary", "")
                lineup.append({
                    "player_id":     pid,
                    "name":          name,
                    "batting_order": batting_pos,
                    "position":      position,
                    "stats_today":   batting_stats,
                    "is_current":    pid in batting_order,
                })
                continue

            # Pitchers not in batting order → bullpen or bench
            if position == "P":
                pitching_stats = stats.get("pitching", {})
                pitch_count_today = pitching_stats.get("pitchesThrown", 0)
                if pitch_count_today > 0:
                    used_pitcher_ids.add(pid)

                days_rest = await self._get_days_rest(pid, game_pk)
                role      = self._infer_role(pitching_stats, game_stats)

                bullpen.append({
                    "player_id":         pid,
                    "name":              name,
                    "pitch_count_today": pitch_count_today,
                    "days_rest":         days_rest,
                    "role":              role,
                    "era":               pitching_stats.get("era", ""),
                    "available":         pitch_count_today == 0,
                })
            else:
                bench.append({
                    "player_id": pid,
                    "name":      name,
                    "position":  position,
                    "stats_today": stats.get("batting", {}).get("summary", ""),
                })

        lineup.sort(key=lambda x: x["batting_order"])
        return lineup, bullpen, bench

    async def _get_days_rest(self, player_id: int, current_game_pk: int) -> int | None:
        """
        Queries raw.pitches to find the last game_date this pitcher appeared,
        then returns the number of rest days relative to today.
        Returns None if no historical record found.
        """
        try:
            row = await self._db.fetchrow(
                """
                SELECT MAX(game_date) AS last_date
                FROM raw.pitches
                WHERE pitcher = $1
                  AND game_pk != $2
                """,
                player_id, current_game_pk,
            )
            if row and row["last_date"]:
                delta = date.today() - row["last_date"]
                return delta.days
        except Exception as exc:
            log.warning("days_rest query failed for player %s: %s", player_id, exc)
        return None

    @staticmethod
    def _infer_role(pitching_stats: dict, game_stats: dict) -> str:
        """
        Best-effort role inference from in-game stats.
        Proper role taxonomy comes from derived.pitcher_season_metrics in Phase 2.
        """
        ip = pitching_stats.get("inningsPitched", "0")
        try:
            ip_float = float(ip.replace(".1", ".33").replace(".2", ".67"))
        except (ValueError, AttributeError):
            ip_float = 0.0
        if ip_float >= 4.0:
            return "SP"
        if ip_float >= 1.0:
            return "MRP"
        return "RP"

    @staticmethod
    def _parse_play_history(all_plays: list) -> list[dict]:
        """
        Converts allPlays into a lightweight at-bat-level log for the frontend
        play-by-play panel and for simulation replay.
        """
        history: list[dict] = []
        for play in all_plays:
            about  = play.get("about", {})
            result = play.get("result", {})
            matchup = play.get("matchup", {})
            pitches = [
                {
                    "pitch_number":  e.get("pitchNumber"),
                    "pitch_type":    e.get("details", {}).get("type", {}).get("code"),
                    "pitch_name":    e.get("details", {}).get("type", {}).get("description"),
                    "description":   e.get("details", {}).get("description"),
                    "speed":         e.get("pitchData", {}).get("startSpeed"),
                    "balls":         e.get("count", {}).get("balls"),
                    "strikes":       e.get("count", {}).get("strikes"),
                    "outs":          e.get("count", {}).get("outs"),
                }
                for e in play.get("playEvents", [])
                if e.get("isPitch")
            ]
            history.append({
                "at_bat_number": about.get("atBatIndex", 0) + 1,
                "inning":        about.get("inning"),
                "half":          about.get("halfInning", "").capitalize(),
                "batter_id":     matchup.get("batter", {}).get("id"),
                "batter_name":   matchup.get("batter", {}).get("fullName"),
                "pitcher_id":    matchup.get("pitcher", {}).get("id"),
                "pitcher_name":  matchup.get("pitcher", {}).get("fullName"),
                "event":         result.get("eventType"),
                "description":   result.get("description"),
                "rbi":           result.get("rbi", 0),
                "pitches":       pitches,
            })
        return history

    @staticmethod
    def _parse_linescore(linescore: dict) -> dict:
        """Extracts inning-by-inning scoring in the shape the frontend expects."""
        innings_raw = linescore.get("innings", [])
        home_innings = [i.get("home", {}).get("runs") for i in innings_raw]
        away_innings = [i.get("away", {}).get("runs") for i in innings_raw]
        return {
            "home": home_innings,
            "away": away_innings,
            "home_runs":   linescore.get("teams", {}).get("home", {}).get("runs", 0),
            "home_hits":   linescore.get("teams", {}).get("home", {}).get("hits", 0),
            "home_errors": linescore.get("teams", {}).get("home", {}).get("errors", 0),
            "away_runs":   linescore.get("teams", {}).get("away", {}).get("runs", 0),
            "away_hits":   linescore.get("teams", {}).get("away", {}).get("hits", 0),
            "away_errors": linescore.get("teams", {}).get("away", {}).get("errors", 0),
        }


# ---------------------------------------------------------------------------
# MLB Game WebSocket Client
# ---------------------------------------------------------------------------

class MLBGameWebSocket:
    """
    Subscribes to the MLB gameday push feed for a single game_pk.

    The MLB WebSocket sends a message whenever the game state changes
    (new pitch, substitution, scoring play, etc.). The message itself
    is treated as a pure signal — the actual state is always fetched
    from the REST API on receipt, ensuring we never depend on the WS
    payload format being stable.

    Reconnect strategy: exponential backoff starting at WS_RECONNECT_BASE
    seconds, capped at WS_RECONNECT_MAX seconds.
    """

    def __init__(
        self,
        game_pk: int,
        on_update: callable,   # async callback(game_pk: int) -> None
    ) -> None:
        self.game_pk   = game_pk
        self.on_update = on_update
        self._stop     = asyncio.Event()
        self._task:  asyncio.Task | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"ws-game-{self.game_pk}"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        url     = MLB_WS_TEMPLATE.format(game_pk=self.game_pk)
        backoff = WS_RECONNECT_BASE

        while not self._stop.is_set():
            try:
                log.info("WS connecting: game %s", self.game_pk)
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    log.info("WS connected:  game %s", self.game_pk)
                    backoff = WS_RECONNECT_BASE   # reset on successful connect
                    async for message in ws:
                        if self._stop.is_set():
                            break
                        log.debug("WS message: game %s — %s", self.game_pk, message[:120])
                        # Any message is a change signal — fire the update callback
                        asyncio.create_task(self.on_update(self.game_pk))

            except asyncio.CancelledError:
                log.info("WS cancelled: game %s", self.game_pk)
                return
            except websockets.exceptions.ConnectionClosedOK:
                log.info("WS closed normally: game %s", self.game_pk)
                return   # game probably ended
            except Exception as exc:
                if self._stop.is_set():
                    return
                log.warning(
                    "WS error game %s (%s) — reconnecting in %.1fs",
                    self.game_pk, exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RECONNECT_MAX)


# ---------------------------------------------------------------------------
# Frontend WebSocket Connection Manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """
    Manages frontend WebSocket clients subscribed to individual game channels.
    Extends the existing dashboard's WS infrastructure.

    Clients connect to:  ws://host/ws/games/{game_pk}
    """

    def __init__(self) -> None:
        # game_pk → set of connected WebSocket objects
        self._subscriptions: dict[int, set[WebSocket]] = {}

    async def connect(self, game_pk: int, ws: WebSocket) -> None:
        await ws.accept()
        self._subscriptions.setdefault(game_pk, set()).add(ws)
        log.info("Frontend WS connected: game %s (%d subscribers)",
                 game_pk, len(self._subscriptions[game_pk]))

    def disconnect(self, game_pk: int, ws: WebSocket) -> None:
        subs = self._subscriptions.get(game_pk, set())
        subs.discard(ws)
        log.info("Frontend WS disconnected: game %s (%d remaining)",
                 game_pk, len(subs))

    async def broadcast(self, game_pk: int, payload: dict) -> None:
        """Send a JSON payload to all clients watching this game."""
        subs = self._subscriptions.get(game_pk, set())
        if not subs:
            return
        dead: set[WebSocket] = set()
        message = json.dumps(payload)
        for ws in subs:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            subs.discard(ws)

    def subscriber_count(self, game_pk: int) -> int:
        return len(self._subscriptions.get(game_pk, set()))


# Singleton shared across the FastAPI app
connection_manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Live Ingestion Pipeline
# ---------------------------------------------------------------------------

class LiveIngestionPipeline:
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
        dsn:       str,
        redis_url: str,
        simulation_callback: callable | None = None,
    ) -> None:
        """
        Parameters
        ----------
        dsn
            asyncpg-compatible PostgreSQL DSN.
        redis_url
            Redis connection URL (e.g. "redis://localhost:6379").
        simulation_callback
            Optional async callback(game_pk: int, game_state: dict) -> None
            called when re-simulation should be triggered (Phase 5 integration
            point). If None, a log message is emitted instead.
        """
        self._dsn    = dsn
        self._redis_url = redis_url
        self._sim_cb = simulation_callback

        self._db:    asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self._http:  aiohttp.ClientSession | None = None

        # game_pk -> MLBGameWebSocket
        self._ws_clients: dict[int, MLBGameWebSocket] = {}
        # game_pk -> asyncio.Lock (prevents concurrent refreshes for same game)
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        # game_pk -> at_bat_number of the last PA that triggered a re-simulation.
        # Prevents re-simulating the same completed PA twice if multiple WS
        # messages arrive before the lock clears (e.g. a substitution event
        # immediately after the final pitch of a PA).
        self._last_resim_at_bat: dict[int, int] = {}

        self._schedule_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("Starting live ingestion pipeline …")
        self._db    = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._http  = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
        )
        self._running = True
        self._schedule_task = asyncio.create_task(
            self._schedule_poller(), name="schedule-poller"
        )
        log.info("Pipeline started.")

    async def stop(self) -> None:
        log.info("Stopping live ingestion pipeline …")
        self._running = False
        if self._schedule_task:
            self._schedule_task.cancel()
        for ws in self._ws_clients.values():
            ws.stop()
        self._ws_clients.clear()
        if self._http  and not self._http.closed:
            await self._http.close()
        if self._redis:
            await self._redis.aclose()
        if self._db:
            await self._db.close()
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
        today  = date.today().strftime("%Y-%m-%d")
        params = {"sportId": 1, "gameTypes": GAME_TYPES, "date": today}

        async with self._http.get(SCHEDULE_URL, params=params) as resp:
            data = await resp.json()

        current_live: set[int] = set()

        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                if "rescheduleGameDate" in game or "resumeGameDate" in game:
                    continue

                game_pk = game["gamePk"]
                status  = game.get("status", {}).get("abstractGameState", "")

                if status == "Live":
                    current_live.add(game_pk)
                    if game_pk not in self._ws_clients:
                        log.info("New live game detected: %s", game_pk)
                        await self._start_watching(game_pk)
                        # Immediately fetch initial state (don't wait for first WS msg)
                        asyncio.create_task(self._refresh_game_state(game_pk))

                elif status == "Final":
                    if game_pk in self._ws_clients:
                        log.info("Game %s finished — closing WS", game_pk)
                        self._ws_clients[game_pk].stop()
                        del self._ws_clients[game_pk]
                        # Do one final state refresh to capture the finished state
                        asyncio.create_task(self._refresh_game_state(game_pk))

                # Upsert every game (Preview/Live/Final) into raw.games
                asyncio.create_task(self._upsert_game_record(game))

    async def _start_watching(self, game_pk: int) -> None:
        self._refresh_locks[game_pk] = asyncio.Lock()
        ws = MLBGameWebSocket(game_pk, on_update=self._refresh_game_state)
        self._ws_clients[game_pk] = ws
        ws.start()

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

                builder    = GameStateBuilder(self._db)
                game_state = await builder.build(feed)
                odds       = await self._fetch_odds(game_pk)

                await self._upsert_lineup_state(game_pk, game_state)
                await self._cache_to_redis(game_pk, game_state, feed["gameData"]["status"]["abstractGameState"])
                await self._persist_odds(game_pk, odds)

                # Determine whether this update marks the end of a plate
                # appearance before broadcasting, so the frontend knows whether
                # to expect incoming simulation results.
                pa_ended, completed_at_bat = self._should_resimulate(game_pk, feed)

                # Broadcast full payload to all frontend subscribers.
                # resim_triggered=True tells the frontend to show a loading
                # indicator while it waits for new simulation results.
                broadcast_payload = {
                    "type":            "game_state_update",
                    "game_pk":         game_pk,
                    "game_state":      game_state,
                    "odds":            odds,
                    "resim_triggered": pa_ended,
                }
                await connection_manager.broadcast(game_pk, broadcast_payload)

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

    async def _fetch_odds(self, game_pk: int) -> dict:
        """
        Returns mock odds.  In Phase 7, replace this method body with an
        aiohttp call to your real odds provider (e.g. The Odds API):

            async with self._http.get(
                "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
                params={"apiKey": ODDS_API_KEY, "eventIds": game_pk, ...}
            ) as resp:
                return await resp.json()
        """
        return MockOddsAPI.get_odds(game_pk)

    def _should_resimulate(
        self, game_pk: int, feed: dict
    ) -> tuple[bool, int]:
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

        live_data    = feed.get("liveData", {})
        current_play = live_data.get("plays", {}).get("currentPlay", {})
        about        = current_play.get("about", {})

        is_complete   = about.get("isComplete", False)
        at_bat_number = about.get("atBatIndex", -1) + 1   # 0-indexed in API, 1-indexed here

        if not is_complete:
            return False, at_bat_number

        # Guard: don't re-trigger for the same completed PA
        if self._last_resim_at_bat.get(game_pk) == at_bat_number:
            return False, at_bat_number

        return True, at_bat_number

    async def _signal_resimulation(self, game_pk: int, game_state: dict) -> None:
        """
        Phase 5 integration point.  When the simulation runner (Phase 5) is
        built, set simulation_callback on the pipeline to trigger it here.
        Until then, this logs the signal so you can verify the trigger logic
        is firing correctly during development.
        """
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
                    if game_state.get("play_history") else "?",
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
            "Preview":   "Preview",
            "Live":      "Live",
            "Final":     "Final",
            "Postponed": "Postponed",
            "Cancelled": "Cancelled",
            "Suspended": "Suspended",
        }
        status = status_map.get(
            gd.get("status", {}).get("abstractGameState", "Preview"), "Preview"
        )

        await self._db.execute(
            """
            INSERT INTO raw.games (
                game_pk, game_date, game_type, status,
                venue_id, home_team_id, away_team_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (game_pk) DO UPDATE SET
                status     = EXCLUDED.status,
                updated_at = NOW()
            """,
            gd["gamePk"],
            date.fromisoformat(gd["gameDate"][:10]),
            gd.get("gameType", "R"),
            status,
            gd.get("venue", {}).get("id", 0),
            gd.get("teams", {}).get("home", {}).get("team", {}).get("id", 0),
            gd.get("teams", {}).get("away", {}).get("team", {}).get("id", 0),
        )

    async def _persist_odds(self, game_pk: int, odds: dict) -> None:
        """Inserts a fresh odds snapshot into raw.game_odds."""
        await self._db.execute(
            """
            INSERT INTO raw.game_odds
                (game_pk, source, is_mock,
                 home_ml, away_ml,
                 home_spread, home_spread_ml, away_spread, away_spread_ml,
                 total_line, over_ml, under_ml)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            game_pk,
            odds.get("source", "mock"),
            odds.get("is_mock", True),
            odds.get("home_ml"),
            odds.get("away_ml"),
            odds.get("home_spread"),
            odds.get("home_spread_ml"),
            odds.get("away_spread"),
            odds.get("away_spread_ml"),
            odds.get("total_line"),
            odds.get("over_ml"),
            odds.get("under_ml"),
        )

    # ------------------------------------------------------------------
    # Redis cache
    # ------------------------------------------------------------------

    async def _cache_to_redis(
        self, game_pk: int, game_state: dict, status: str
    ) -> None:
        """
        Caches the raw feed (not the built game_state) so _fetch_feed can fall
        back to it if the MLB API rate-limits us mid-game.
        """
        ttl = REDIS_TTL_DONE_S if status == "Final" else REDIS_TTL_LIVE_S
        await self._redis.setex(
            f"game_state:{game_pk}",
            ttl,
            json.dumps(game_state),
        )

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    @property
    def live_game_pks(self) -> list[int]:
        return list(self._ws_clients.keys())

    def is_watching(self, game_pk: int) -> bool:
        return game_pk in self._ws_clients


# ---------------------------------------------------------------------------
# FastAPI WebSocket endpoint
# ---------------------------------------------------------------------------

ws_router = APIRouter(tags=["websocket"])

@ws_router.websocket("/ws/games/{game_pk}")
async def game_websocket(websocket: WebSocket, game_pk: int) -> None:
    """
    Frontend clients connect here to receive live game state pushes.

    Message types clients will receive:
      {"type": "game_state_update", "game_pk": ..., "game_state": {...}, "odds": {...}}
      {"type": "ping"}

    The connection is kept alive with a periodic ping.  Clients should
    reconnect automatically on disconnect.
    """
    await connection_manager.connect(game_pk, websocket)
    try:
        while True:
            # Keep-alive: send a ping every 25 seconds and wait for any
            # client message (clients can send "ping" to confirm alive)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=25.0)
                if data.strip().lower() == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        connection_manager.disconnect(game_pk, websocket)


# ---------------------------------------------------------------------------
# FastAPI app factory + lifespan
# ---------------------------------------------------------------------------

def create_app(
    dsn:                 str,
    redis_url:           str = "redis://localhost:6379",
    simulation_callback: callable | None = None,
) -> FastAPI:
    """
    Creates a FastAPI application with the live ingestion pipeline wired in.

    To mount into your existing dashboard app instead of creating a new one:

        from live_ingestion_pipeline import (
            lifespan_factory, ws_router, odds_router
        )
        app = FastAPI(lifespan=lifespan_factory(dsn, redis_url))
        app.include_router(ws_router)
        app.include_router(odds_router)
    """

    pipeline = LiveIngestionPipeline(
        dsn=dsn,
        redis_url=redis_url,
        simulation_callback=simulation_callback,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pipeline.start()
        yield
        await pipeline.stop()

    app = FastAPI(
        title="MLB Live Ingestion",
        description="Step 1.3 — Live Data Ingestion Pipeline",
        lifespan=lifespan,
    )
    app.include_router(ws_router)
    app.include_router(odds_router)

    # ---- Status endpoints --------------------------------------------------

    @app.get("/api/pipeline/status")
    async def pipeline_status():
        return {
            "live_games":       pipeline.live_game_pks,
            "live_game_count":  len(pipeline.live_game_pks),
            "ws_clients":       {
                gp: connection_manager.subscriber_count(gp)
                for gp in pipeline.live_game_pks
            },
        }

    @app.post("/api/games/{game_pk}/resimulate")
    async def manual_resimulate(game_pk: int):
        """
        Manual re-simulation trigger for the frontend mid-at-bat button.

        Called when the user wants to see how the current count affects likely
        outcomes without waiting for the plate appearance to end.  Fetches the
        latest game state from Redis (fast, avoids an extra MLB API call) and
        fires the simulation callback directly.

        Frontend usage:
          POST /api/games/{game_pk}/resimulate
          → server responds immediately with {"status": "triggered"}
          → simulation results arrive shortly after via the WS channel as:
            {"type": "simulation_results", "game_pk": ..., "results": {...}}

        Rate note: this endpoint is intentionally not debounced server-side.
        If you want to prevent users from spamming it, add a short TTL check
        in Redis (e.g. one manual resim per game per 10 seconds).
        """
        if not pipeline.is_watching(game_pk):
            # Game is not currently live — pull state from Redis if available
            cached = await pipeline._redis.get(f"game_state:{game_pk}") if pipeline._redis else None
            if not cached:
                return {"status": "error", "detail": f"game {game_pk} is not live and has no cached state"}
            game_state = json.loads(cached)
            await pipeline._signal_resimulation(game_pk, game_state)
            return {"status": "triggered", "source": "cache"}

        # Game is live — do a fresh state fetch so the sim runs on the latest count
        feed = await pipeline._fetch_feed(game_pk)
        if feed is None:
            return {"status": "error", "detail": "could not fetch game state"}

        builder    = GameStateBuilder(pipeline._db)
        game_state = await builder.build(feed)

        await pipeline._signal_resimulation(game_pk, game_state)

        # Broadcast a "resim_pending" notice so the frontend can show a spinner
        # while it waits for results over the WS channel
        await connection_manager.broadcast(game_pk, {
            "type":     "resim_pending",
            "game_pk":  game_pk,
            "trigger":  "manual",
            "inning":   game_state.get("inning"),
            "half":     game_state.get("half"),
            "balls":    game_state.get("balls"),
            "strikes":  game_state.get("strikes"),
            "outs":     game_state.get("outs"),
        })

        return {"status": "triggered", "source": "live"}

    @app.get("/api/games/today")
    async def games_today():
        """Returns today's schedule with current state from Redis cache."""
        today  = date.today().strftime("%Y-%m-%d")
        params = {"sportId": 1, "gameTypes": GAME_TYPES, "date": today}
        async with aiohttp.ClientSession() as s:
            async with s.get(SCHEDULE_URL, params=params) as resp:
                data = await resp.json()

        games = []
        redis_client = pipeline._redis
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                gp = game["gamePk"]
                cached = await redis_client.get(f"game_state:{gp}") if redis_client else None
                games.append({
                    "game_pk":    gp,
                    "status":     game.get("status", {}).get("abstractGameState"),
                    "home_team":  game.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation"),
                    "away_team":  game.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation"),
                    "venue":      game.get("venue", {}).get("name"),
                    "game_state": json.loads(cached) if cached else None,
                    "odds":       MockOddsAPI.get_odds(gp),
                })
        return games

    return app


def lifespan_factory(
    dsn:                 str,
    redis_url:           str = "redis://localhost:6379",
    simulation_callback: callable | None = None,
):
    """
    Returns a lifespan context manager for mounting the pipeline into an
    existing FastAPI app without creating a new one.

    Usage in your existing app.py:
        from live_ingestion_pipeline import lifespan_factory, ws_router, odds_router

        app = FastAPI(lifespan=lifespan_factory(
            dsn="postgresql://user:pass@localhost/baseball",
            redis_url="redis://localhost",
        ))
        app.include_router(ws_router)
        app.include_router(odds_router)
    """
    pipeline = LiveIngestionPipeline(dsn, redis_url, simulation_callback)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pipeline.start()
        yield
        await pipeline.stop()

    return lifespan


# ---------------------------------------------------------------------------
# Supporting DDL — run once before starting the pipeline
# ---------------------------------------------------------------------------

LIVE_PIPELINE_DDL = """
-- Run this once against your database before starting the pipeline.
-- (raw.game_odds only — sim.lineup_state already exists in 01_postgres_schema.sql)

""" + GAME_ODDS_DDL + """

-- Partial unique index that enforces one active live session per game.
-- Needed for the ON CONFLICT clause in _upsert_lineup_state().
CREATE UNIQUE INDEX IF NOT EXISTS uq_lineup_state_live_game
    ON sim.lineup_state(game_pk)
    WHERE is_live_game = TRUE;
"""


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="MLB Live Ingestion Pipeline")
    parser.add_argument(
        "--dsn",
        default="postgresql://postgres:password@localhost:5432/baseball",
        help="asyncpg PostgreSQL DSN",
    )
    parser.add_argument(
        "--redis",
        default="redis://localhost:6379",
        help="Redis URL",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Uvicorn host",
    )
    parser.add_argument(
        "--port", type=int, default=8001,
        help="Uvicorn port (use 8000 if merging with existing dashboard)",
    )
    parser.add_argument(
        "--print-ddl", action="store_true",
        help="Print the required DDL and exit",
    )
    args = parser.parse_args()

    if args.print_ddl:
        print(LIVE_PIPELINE_DDL)
    else:
        app = create_app(dsn=args.dsn, redis_url=args.redis)
        uvicorn.run(app, host=args.host, port=args.port)
