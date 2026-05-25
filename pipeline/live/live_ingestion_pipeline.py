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
               │ fires at end of every plate appearance (PA complete)  │
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
import os
import random
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

import aiohttp
import asyncpg
import redis.asyncio as aioredis
import websockets
import websockets.exceptions
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRouter

# SIM-370: odds/prop provider seam.  The pipeline obtains its odds source via
# get_odds_provider() (env-selected, defaults to MockOddsAPI) instead of
# hard-instantiating MockOddsAPI, so a real provider can drop in behind the
# OddsProvider interface without touching ingestion/CLV/persistence code.
from pipeline.odds_provider import OddsProvider, get_odds_provider

# SIM-106: Type alias for the simulation callback. It MUST be an async
# function — passing a sync function would either raise TypeError when the
# pipeline awaits it, or silently no-op if the coroutine isn't awaited.
# The runtime check in LiveIngestionPipeline.__init__ catches misuse at
# construction time so the failure surfaces immediately during Phase 5
# wiring rather than during the first re-sim signal in production.
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MLB_BASE = "https://statsapi.mlb.com"
MLB_WS_TEMPLATE = "wss://ws.statsapi.mlb.com/api/v1/game/push/subscribe/gameday/{game_pk}"
SCHEDULE_URL = f"{MLB_BASE}/api/v1/schedule"

SCHEDULE_POLL_S = 30  # how often to check for newly-live games
WS_RECONNECT_BASE = 2.0  # base seconds for WS reconnect backoff
WS_RECONNECT_MAX = 60.0  # cap on WS reconnect backoff
HTTP_TIMEOUT_S = 10  # aiohttp request timeout
REDIS_TTL_LIVE_S = 60  # cache TTL for live game states
REDIS_TTL_DONE_S = 3600  # cache TTL for completed game states

# SIM-104: per-game cooldown applied to the manual /resimulate endpoint.
# 10 seconds is generous enough to avoid frustrating legitimate users
# (sample one resim, see results, sample again) but tight enough to keep
# spam from queueing up dozens of 100-iteration sim runs in Phase 5.
RESIM_COOLDOWN_S = 10

# Re-simulation is triggered automatically at the end of every plate appearance.
# A manual endpoint also allows the frontend to request a re-sim mid at-bat.

GAME_TYPES = ["R", "F", "D", "L", "W", "C", "P"]

# ---------------------------------------------------------------------------
# SIM-340: Prop-odds ingestion config (multi-book, sharp flag, cadence)
# ---------------------------------------------------------------------------
# PROP_BOOKS — the set of books polled for player props on every prop-fetch
# cycle.  Each entry is (book_name, is_sharp_book).  Sharp books (Pinnacle,
# Circa) provide the CLV reference line; soft books (DraftKings, FanDuel)
# capture the line the public actually bets into.  Capturing both lets the
# CLV engine (SIM-339) measure soft-vs-sharp divergence per prop.
#
# Phase 7 swap: replace MockOddsAPI.get_prop_odds() in _fetch_prop_odds() with
# a real provider that returns one quote per (book, player, prop_stat); the
# is_sharp_book classification stays here so it survives the provider swap.
PROP_BOOKS: list[tuple[str, bool]] = [
    ("pinnacle", True),  # sharp — CLV reference
    ("circa", True),  # sharp — CLV reference
    ("draftkings", False),  # soft — retail line
    ("fanduel", False),  # soft — retail line
]

# The 7 Betting-Analyst-confirmed prop markets (mirror MockOddsAPI._PROP_CONFIG
# and the raw.prop_odds CHECK constraint).  Kept as a tuple so callers cannot
# mutate the canonical ordering.
PROP_STATS: tuple[str, ...] = (
    "strikeouts",
    "hits",
    "home_runs",
    "earned_runs",
    "walks",
    "total_bases",
    "rbis",
)

# SIM-340: prop-odds fetch cadence.  Prop lines move far more slowly than the
# WS refresh rate (a WS message fires on every pitch).  Polling every book ×
# player × prop on every WS signal would issue thousands of redundant writes
# per game.  We therefore gate prop fetches to at most once per
# PROP_FETCH_CADENCE_S seconds per game; the dedup hash (migration 0013)
# collapses any identical snapshot that still slips through.
PROP_FETCH_CADENCE_S = 60

# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------
# NOTE (SIM-083): GAME_ODDS_DDL has been removed from this file.
# raw.game_odds DDL (including SIM-133 CLV columns) now lives in:
#   db/schemas/01_postgres_schema.sql
# Applied via Alembic migration 0003 (db/migrations/versions/0003_*.py).
# ---------------------------------------------------------------------------


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
      2. Replace `get_prop_odds()` with a real provider call.
      3. Keep this class for local dev / testing.

    American odds encoding:
      Favourite:  odds = -(prob / (1-prob)) * 100  (negative integer)
      Underdog:   odds = ((1-prob) / prob) * 100   (positive integer)

    SIM-134: Added get_prop_odds() for all 7 Betting-Analyst-confirmed prop markets.
    """

    # ------------------------------------------------------------------
    # SIM-134: Prop line configuration
    # Betting Analyst (Agent 8) approved centers and vig ranges.
    # Format: (line_center, half_spread, over_vig_range, under_vig_range)
    #   line_center:  base prop line (snapped to nearest 0.5 by get_prop_odds)
    #   half_spread:  ± RNG window added to line_center before snapping
    #   over/under vig: (min, max) tuple for American odds juice
    #
    # Design notes:
    #   - Strikeouts: widest window; pitcher quality variance is large.
    #   - Home runs / RBIs set at 0.5; these are binary-feel props.
    #   - Juice asymmetry on HR (books shade the under) reflects sharp-book
    #     behaviour Betting Analyst observed in real markets.
    # ------------------------------------------------------------------
    _PROP_CONFIG: dict[str, tuple[float, float, tuple[int, int], tuple[int, int]]] = {
        #  prop_stat       center  ±spread  over_vig        under_vig
        "strikeouts": (5.5, 1.0, (-125, -105), (-125, -105)),
        "hits": (0.5, 0.5, (-115, -105), (-115, -105)),
        "home_runs": (0.5, 0.0, (-130, -110), (+100, +110)),
        "earned_runs": (3.5, 1.0, (-115, -105), (-115, -105)),
        "walks": (2.5, 0.5, (-115, -105), (-115, -105)),
        "total_bases": (1.5, 0.5, (-120, -105), (-115, -105)),
        "rbis": (0.5, 0.5, (-120, -110), (-110, -100)),
    }

    @staticmethod
    def _prob_to_american(prob: float) -> int:
        prob = max(0.01, min(0.99, prob))
        if prob >= 0.5:
            return round(-(prob / (1.0 - prob)) * 100)
        else:
            return round(((1.0 - prob) / prob) * 100)

    @staticmethod
    def get_odds(
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """
        Returns mock betting lines for a game.

        SIM-133: Added book, line_type, market_type, is_sharp_book fields so all
        raw.game_odds inserts populate the four CLV-enabling columns.

        Parameters
        ----------
        game_pk       : MLB game identifier (seeds the RNG for determinism)
        line_type     : 'opening' | 'current' | 'closing' | 'bet_placement'
        market_type   : 'moneyline' | 'runline' | 'total'
        book          : book identifier (e.g. 'consensus', 'pinnacle', 'draftkings')
        is_sharp_book : TRUE for sharp books used as CLV reference (e.g. Pinnacle, Circa)
        """
        rng = random.Random(game_pk)

        # Home win probability: slight home-field edge built in
        home_win_prob = rng.uniform(0.38, 0.64)
        away_win_prob = 1.0 - home_win_prob

        # SIM-132: Apply realistic book vig so mock lines don't produce zero-
        # overround prices.  Without vig, implied probs sum to exactly 1.0 —
        # no real book prices this way.  Every edge calculation, calibration
        # target, and display component built through Phase 6 would be trained
        # against lines that don't exist in real markets; when Phase 7 swaps
        # in real lines the edge estimates shrink 3–8 pp overnight.
        #
        # vig is the total overround, split evenly between home and away:
        #   home_inflated = home_win_prob * (1 + vig/2)
        #   away_inflated = away_win_prob * (1 + vig/2)
        #   sum of inflated probs = 1 + vig/2  ∈ [1.03, 1.05]
        #
        # Range chosen so (home_implied + away_implied) > 1.03 for every
        # game_pk — the acceptance-criteria assertion in test_live_pipeline.py.
        # Real sharp-book MLB overround is ~3–5 %; soft-book ~6–8 %.
        vig = rng.uniform(0.06, 0.10)
        home_ml = MockOddsAPI._prob_to_american(home_win_prob * (1 + vig / 2))
        away_ml = MockOddsAPI._prob_to_american(away_win_prob * (1 + vig / 2))

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
            "game_pk": game_pk,
            "source": "mock",
            "is_mock": True,
            # SIM-133 CLV columns
            "book": book,
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": is_sharp_book,
            # Odds values
            "home_ml": home_ml,
            "away_ml": away_ml,
            "home_spread": home_spread,
            "home_spread_ml": spread_juice,
            "away_spread": away_spread,
            "away_spread_ml": spread_juice,
            "total_line": total_line,
            "over_ml": total_juice,
            "under_ml": total_juice,
        }

    @staticmethod
    def get_prop_odds(
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """
        SIM-134: Returns a deterministic mock prop line for a player.

        The RNG is seeded on (game_pk, player_id, prop_stat) so the same
        combination always returns the same line — frontend and tests never
        see flicker.

        Parameters
        ----------
        game_pk       : MLB game identifier
        player_id     : MLB player identifier
        prop_stat     : one of the 7 Betting-Analyst-confirmed markets:
                        'strikeouts' | 'hits' | 'home_runs' | 'earned_runs'
                        | 'walks' | 'total_bases' | 'rbis'
        line_type     : 'opening' | 'current' | 'closing' | 'bet_placement'
        book          : book identifier (default 'consensus')
        is_sharp_book : True for sharp reference books (Pinnacle, Circa)

        Returns
        -------
        dict with all columns required by a raw.prop_odds INSERT:
          game_pk, player_id, prop_stat, line,
          over_ml, under_ml, book, line_type, is_sharp_book,
          source, is_mock

        Raises
        ------
        ValueError
            If prop_stat is not in the 7 known markets.  Prevents silent
            schema violations before the DB CHECK constraint catches them.
        """
        if prop_stat not in MockOddsAPI._PROP_CONFIG:
            known = ", ".join(sorted(MockOddsAPI._PROP_CONFIG))
            raise ValueError(f"Unknown prop_stat '{prop_stat}'. Known values: {known}")

        center, half_spread, over_vig_range, under_vig_range = MockOddsAPI._PROP_CONFIG[prop_stat]

        # Deterministic RNG: same game + player + prop always → same line
        rng = random.Random(game_pk * 1_000_000 + player_id + hash(prop_stat))

        # Line: add jitter to center then snap to nearest 0.5
        raw_line = center + rng.uniform(-half_spread, half_spread)
        line = round(raw_line * 2) / 2  # snap to nearest 0.5

        # Vig: independent random draws within Betting-Analyst-approved ranges
        over_ml = rng.randint(*over_vig_range)
        under_ml = rng.randint(*under_vig_range)

        return {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": line,
            "over_ml": over_ml,
            "under_ml": under_ml,
            "book": book,
            "line_type": line_type,
            "is_sharp_book": is_sharp_book,
            "source": "mock",
            "is_mock": True,
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

    SIM-101 — Per-game caching:
      One builder is now stored per ``game_pk`` on the pipeline
      (LiveIngestionPipeline._builders).  The builder caches:

        * ``_history``          — running list of at-bat-level summaries
        * ``_last_at_bat_index``— the highest atBatIndex parsed so far
        * ``_game_date``        — the game's officialDate (for replay-safe
          days_rest, see SIM-100)

      On each refresh, ``_parse_play_history()`` only walks plays whose
      ``atBatIndex > _last_at_bat_index``, then appends to ``_history``.
      Pre-SIM-101 every refresh rebuilt the entire history from scratch —
      O(N) parse + JSON serialize on every WS signal.  By the 9th inning
      with ~80 plays, that fired 20+ times per inning.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._db = db_pool
        # SIM-101: per-game caches.  Reset implicitly each time a fresh
        # builder is constructed (one per game_pk).
        self._history: list[dict] = []
        self._last_at_bat_index: int = -1  # -1 = nothing seen yet
        self._game_date: date | None = None

    async def build(self, feed: dict[str, Any]) -> dict[str, Any]:
        """
        Main entry point.  `feed` is the parsed JSON from
        GET /api/v1.1/game/{game_pk}/feed/live
        """
        game_data = feed["gameData"]
        live_data = feed["liveData"]

        game_pk = feed["gamePk"]
        game_status = game_data["status"]["abstractGameState"]  # Live / Final / Preview
        linescore = live_data.get("linescore", {})
        boxscore = live_data.get("boxscore", {})
        current_play = live_data["plays"].get("currentPlay", {})

        home_team_id = game_data["teams"]["home"]["id"]
        away_team_id = game_data["teams"]["away"]["id"]

        # SIM-100: extract game_date for replay-safe days_rest calculation.
        # officialDate is "YYYY-MM-DD"; None-safe for edge cases.
        _game_date_str = game_data.get("datetime", {}).get("officialDate")
        try:
            _game_date: date | None = date.fromisoformat(_game_date_str) if _game_date_str else None
        except ValueError:
            _game_date = None

        # ---- Inning state --------------------------------------------------
        inning = linescore.get("currentInning", 1)
        half = linescore.get("inningHalf", "Top")  # "Top" | "Bottom"
        outs = linescore.get("outs", 0)
        balls = current_play.get("count", {}).get("balls", 0)
        strikes = current_play.get("count", {}).get("strikes", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)

        batting_team_id = away_team_id if half == "Top" else home_team_id
        fielding_team_id = home_team_id if half == "Top" else away_team_id

        # ---- Baserunners ---------------------------------------------------
        offense = current_play.get("matchup", {})
        runners = linescore.get("offense", {})
        on_1b = runners.get("first", {}).get("id")
        on_2b = runners.get("second", {}).get("id")
        on_3b = runners.get("third", {}).get("id")

        # ---- Current participants ------------------------------------------
        current_batter_id = offense.get("batter", {}).get("id")

        # SIM-099: removed dead first assignment from offense dict (matchup is
        # batter-centric; the "pitcher" key there was silently overwritten by
        # the lookup chain below).
        #
        # Resolution order (most authoritative → least):
        #   1. currentPlay.matchup.pitcher  — set mid-PA, most reliable
        #   2. linescore.defense.pitcher    — set between PAs
        #   3. allPlays[-1].matchup.pitcher — final fallback from last recorded play
        #   4. None → WARNING logged; simulation layer handles gracefully
        _all_plays = live_data.get("plays", {}).get("allPlays", [])
        _last_play_pitcher = (
            _all_plays[-1].get("matchup", {}).get("pitcher", {}).get("id") if _all_plays else None
        )
        current_pitcher_id = (
            current_play.get("matchup", {}).get("pitcher", {}).get("id")
            or live_data.get("linescore", {}).get("defense", {}).get("pitcher", {}).get("id")
            or _last_play_pitcher
        )
        if current_pitcher_id is None:
            log.warning(
                "game %s: could not resolve current_pitcher_id from matchup, "
                "linescore.defense, or allPlays — game state may be incomplete",
                game_pk,
            )

        # ---- Lineups -------------------------------------------------------
        # SIM-100: pass game_date so days_rest is anchored to the game date,
        # not date.today() — required for correct historical replay behaviour.
        home_lineup, home_bullpen, home_bench = await self._parse_roster(
            boxscore, game_pk, side="home", team_id=home_team_id, game_date=_game_date
        )
        away_lineup, away_bullpen, away_bench = await self._parse_roster(
            boxscore, game_pk, side="away", team_id=away_team_id, game_date=_game_date
        )

        # ---- Play history (at-bat level summary) ---------------------------
        # SIM-101: cache game_date on first refresh.
        if self._game_date is None:
            self._game_date = _game_date
        # SIM-101: incremental parse — only walk plays beyond what we've
        # already processed.  Returns the cached running history.
        play_history = self._parse_play_history(live_data["plays"].get("allPlays", []))

        # ---- Linescore snapshot (stored for frontend convenience) ----------
        inning_scores = self._parse_linescore(linescore)

        return {
            # Core state
            "game_pk": game_pk,
            "game_status": game_status,
            "inning": inning,
            "half": half,
            "outs": outs,
            "balls": balls,
            "strikes": strikes,
            "home_score": home_score,
            "away_score": away_score,
            "batting_team_id": batting_team_id,
            "fielding_team_id": fielding_team_id,
            "on_1b": on_1b,
            "on_2b": on_2b,
            "on_3b": on_3b,
            "current_batter_id": current_batter_id,
            "current_pitcher_id": current_pitcher_id,
            # Rosters
            "home_lineup": home_lineup,
            "away_lineup": away_lineup,
            "home_bullpen": home_bullpen,
            "away_bullpen": away_bullpen,
            "home_bench": home_bench,
            "away_bench": away_bench,
            # History / display
            "play_history": play_history,
            "inning_scores": inning_scores,
            "last_updated_at": datetime.now(UTC).isoformat(),
        }

    async def _parse_roster(
        self,
        boxscore: dict,
        game_pk: int,
        side: str,
        team_id: int,
        *,
        game_date: date | None = None,
    ) -> tuple[list, list, list]:
        """
        Returns (lineup, bullpen, bench) for one team.

        lineup  — [{player_id, batting_order, position, name, stats_today}]
        bullpen — [{player_id, name, pitch_count_today, days_rest, role, available}]
        bench   — [{player_id, name, position}]

        SIM-100 fixes applied here:
          - N+1 query eliminated: all bullpen pitcher days_rest fetched in one
            batch query via ``_batch_days_rest()`` rather than one query per pitcher.
          - ``used_pitcher_ids`` dead-code set removed entirely.
          - Availability corrected: a pitcher is available if they haven't thrown
            today OR if they've rested ≥1 day and thrown fewer than 30 pitches
            (single-inning appearances by fresh arms).
          - ``game_date`` passed through to ``_batch_days_rest()`` so historical
            replay calculates rest relative to game_date, not date.today().
        """
        team_box = boxscore.get("teams", {}).get(side, {})
        players = team_box.get("players", {})  # keyed "ID{player_id}"
        batting_order = team_box.get("battingOrder", [])

        lineup: list[dict] = []
        bench: list[dict] = []

        # SIM-100: two-pass approach — collect pitchers first, then batch-query
        # days_rest for all of them in a single DB round-trip.
        bullpen_candidates: list[tuple] = []  # (pid, name, pitching_stats, game_stats, pitch_count)

        for _key, player_data in players.items():
            pid = player_data["person"]["id"]
            name = player_data["person"]["fullName"]
            position = player_data.get("position", {}).get("abbreviation", "")
            stats = player_data.get("stats", {})
            game_stats = player_data.get("gameStats", {})
            seq = player_data.get("battingOrder")  # "100", "200" … or None

            # Batting lineup
            if seq is not None:
                batting_pos = int(seq) // 100  # "100" → 1
                batting_stats = stats.get("batting", {}).get("summary", "")
                lineup.append(
                    {
                        "player_id": pid,
                        "name": name,
                        "batting_order": batting_pos,
                        "position": position,
                        "stats_today": batting_stats,
                        "is_current": pid in batting_order,
                    }
                )
                continue

            if position == "P":
                pitching_stats = stats.get("pitching", {})
                pitch_count_today = pitching_stats.get("pitchesThrown", 0)
                # SIM-100: removed used_pitcher_ids.add(pid) — set was populated
                # but never read anywhere; dead code confirmed by full-file grep.
                bullpen_candidates.append(
                    (pid, name, pitching_stats, game_stats, pitch_count_today)
                )
            else:
                bench.append(
                    {
                        "player_id": pid,
                        "name": name,
                        "position": position,
                        "stats_today": stats.get("batting", {}).get("summary", ""),
                    }
                )

        # SIM-100: single batch query replaces N per-pitcher queries
        pitcher_ids = [c[0] for c in bullpen_candidates]
        days_rest_cache = await self._batch_days_rest(pitcher_ids, game_pk, as_of_date=game_date)

        bullpen: list[dict] = []
        for pid, name, pitching_stats, game_stats, pitch_count_today in bullpen_candidates:
            days_rest = days_rest_cache.get(pid)  # int | None
            role = self._infer_role(pitching_stats, game_stats)

            # SIM-100: corrected availability logic.
            # Old (wrong): pitch_count_today == 0
            #   → marks every pitcher who has thrown even 1 pitch as unavailable,
            #     so most of the bullpen is unavailable mid-game.
            # New (correct): fresh arm OR rested arm with light usage
            #   pitch_count_today == 0                           → fully fresh
            #   days_rest >= 1 AND pitch_count_today < 30        → 1+ days rest,
            #     light usage (single short outing) → manager can bring back
            available = pitch_count_today == 0 or (
                days_rest is not None and days_rest >= 1 and pitch_count_today < 30
            )

            bullpen.append(
                {
                    "player_id": pid,
                    "name": name,
                    "pitch_count_today": pitch_count_today,
                    "days_rest": days_rest,
                    "role": role,
                    "era": pitching_stats.get("era", ""),
                    "available": available,
                }
            )

        lineup.sort(key=lambda x: x["batting_order"])
        return lineup, bullpen, bench

    async def _batch_days_rest(
        self,
        pitcher_ids: list[int],
        current_game_pk: int | None = None,
        *,
        game_pk: int | None = None,
        as_of_date: date | None = None,
    ) -> dict[int, int | None]:
        # ``game_pk`` accepted as alias for legacy callers (tests + SIM-100).
        if current_game_pk is None:
            current_game_pk = game_pk
        """
        SIM-100: Batch replacement for the old per-pitcher ``_get_days_rest()``.

        Issues a *single* DB query for all bullpen pitchers on a team instead of
        one query per pitcher.  With a 13-man bullpen, this reduces DB round-trips
        per WS refresh from 26 (2 teams × 13) to 2 (one per team).

        Parameters
        ----------
        pitcher_ids
            Player IDs for all pitchers on one team's roster.
        current_game_pk
            Excluded from the look-back so today's game doesn't count as a rest day.
        as_of_date
            Date to calculate rest relative to.  Pass ``game_date`` for historical
            replay; defaults to ``date.today()`` for live games.
            (SIM-100: old code always used date.today(), breaking replay.)

        Returns
        -------
        dict mapping player_id → days_rest (int) or None if no prior appearance.
        """
        if not pitcher_ids:
            return {}

        anchor = as_of_date or date.today()

        try:
            rows = await self._db.fetch(
                """
                SELECT pitcher, MAX(game_date) AS last_date
                FROM   raw.pitches
                WHERE  pitcher = ANY($1)
                  AND  game_pk  != $2
                GROUP  BY pitcher
                """,
                pitcher_ids,
                current_game_pk,
            )
        except Exception as exc:
            log.warning("batch days_rest query failed: %s", exc)
            return {}

        result: dict[int, int | None] = {}
        for row in rows:
            if row["last_date"]:
                result[row["pitcher"]] = (anchor - row["last_date"]).days
        return result

    @staticmethod
    def _infer_role(pitching_stats: dict, game_stats: dict) -> str:
        """
        Best-effort role inference from in-game stats.

        SIM-102: Adds an Opener bucket between SP and MRP.  Pre-SIM-102 logic
        only used IP, so a pitcher who threw 2.0 IP and faced 9 batters was
        labelled MRP — exactly the misclassification that breaks the Phase 4
        manager engine, which then treats the opener as a middle reliever
        available for high-leverage re-use.

        Decision order (most specific → least):
          1. SP      — IP ≥ 4.0   (full starter outing)
          2. Opener  — IP < 4.0 AND BF ≥ 9   (deliberate first-inning role)
          3. MRP     — IP ≥ 1.0   (multi-inning relief)
          4. RP      — otherwise  (one-inning specialist)

        Temporary heuristic until SIM-057 lands a proper season-level
        opener_rate column on derived.pitcher_season_metrics.
        """
        ip = pitching_stats.get("inningsPitched", "0")
        try:
            ip_float = float(str(ip).replace(".1", ".33").replace(".2", ".67"))
        except (ValueError, AttributeError):
            ip_float = 0.0

        # batters_faced lives on game_stats.pitching.battersFaced for live feeds;
        # default to 0 so missing data degrades to the previous IP-only behavior.
        bf = 0
        try:
            bf = int(pitching_stats.get("battersFaced", 0))
        except (TypeError, ValueError):
            bf = 0

        if ip_float >= 4.0:
            return "SP"
        if ip_float < 4.0 and bf >= 9:
            return "Opener"
        if ip_float >= 1.0:
            return "MRP"
        return "RP"

    def _parse_play_history(self, all_plays: list) -> list[dict]:
        """
        Converts allPlays into a lightweight at-bat-level log for the frontend
        play-by-play panel and for simulation replay.

        SIM-101 — Incremental parse:
          Pre-SIM-101 this was a static method that walked every play in
          ``all_plays`` on every WS signal.  By the 9th inning of a 10-inning
          game with ~90 plays, this fired 20+ times per inning (one per WS
          message), each time re-parsing every play and re-serializing the
          full history into JSONB for the upsert.

          Now we cache a per-builder history list and only walk plays whose
          ``about.atBatIndex`` is strictly greater than the highest index
          we've already processed.  The terminal at-bat (currentPlay) is
          re-parsed each refresh as long as it hasn't completed (its
          atBatIndex equals _last_at_bat_index until isComplete flips it
          forward), so mid-PA pitch-by-pitch updates still surface in the
          history without duplication.
        """
        if not all_plays:
            return list(self._history)

        # The currentPlay (last in allPlays) may still be in-flight; replay
        # it every refresh until its atBatIndex advances past the cached
        # last-processed index.  This means: while atBatIndex == cache, we
        # update the LAST entry in self._history with the latest pitch
        # detail; once atBatIndex advances, we append the new at-bat.
        new_appended = 0
        replaced_current = False

        for play in all_plays:
            about_idx = play.get("about", {}).get("atBatIndex")
            if about_idx is None:
                continue

            entry = self._build_history_entry(play)

            if about_idx > self._last_at_bat_index:
                # Brand-new at-bat — append.
                self._history.append(entry)
                self._last_at_bat_index = about_idx
                new_appended += 1
            elif about_idx == self._last_at_bat_index and self._history:
                # Same at-bat as last refresh — refresh the in-flight entry's
                # pitch list / description / event in case the PA updated.
                self._history[-1] = entry
                replaced_current = True
            # about_idx < cache: already permanently captured — skip.

        if new_appended:
            log.debug(
                "SIM-101: parsed %d new plays (cache had %d); replaced_current=%s",
                new_appended,
                len(self._history) - new_appended,
                replaced_current,
            )

        return list(self._history)

    @staticmethod
    def _build_history_entry(play: dict) -> dict:
        """SIM-101: extracted from the old parser so both the incremental
        path and any consumer that wants to reformat a single play stay in
        sync."""
        about = play.get("about", {})
        result = play.get("result", {})
        matchup = play.get("matchup", {})
        pitches = [
            {
                "pitch_number": e.get("pitchNumber"),
                "pitch_type": e.get("details", {}).get("type", {}).get("code"),
                "pitch_name": e.get("details", {}).get("type", {}).get("description"),
                "description": e.get("details", {}).get("description"),
                "speed": e.get("pitchData", {}).get("startSpeed"),
                "balls": e.get("count", {}).get("balls"),
                "strikes": e.get("count", {}).get("strikes"),
                "outs": e.get("count", {}).get("outs"),
            }
            for e in play.get("playEvents", [])
            if e.get("isPitch")
        ]
        return {
            "at_bat_number": about.get("atBatIndex", 0) + 1,
            "inning": about.get("inning"),
            "half": about.get("halfInning", "").capitalize(),
            "batter_id": matchup.get("batter", {}).get("id"),
            "batter_name": matchup.get("batter", {}).get("fullName"),
            "pitcher_id": matchup.get("pitcher", {}).get("id"),
            "pitcher_name": matchup.get("pitcher", {}).get("fullName"),
            "event": result.get("eventType"),
            "description": result.get("description"),
            "rbi": result.get("rbi", 0),
            "pitches": pitches,
        }

    @staticmethod
    def _parse_linescore(linescore: dict) -> dict:
        """Extracts inning-by-inning scoring in the shape the frontend expects."""
        innings_raw = linescore.get("innings", [])
        home_innings = [i.get("home", {}).get("runs") for i in innings_raw]
        away_innings = [i.get("away", {}).get("runs") for i in innings_raw]
        return {
            "home": home_innings,
            "away": away_innings,
            "home_runs": linescore.get("teams", {}).get("home", {}).get("runs", 0),
            "home_hits": linescore.get("teams", {}).get("home", {}).get("hits", 0),
            "home_errors": linescore.get("teams", {}).get("home", {}).get("errors", 0),
            "away_runs": linescore.get("teams", {}).get("away", {}).get("runs", 0),
            "away_hits": linescore.get("teams", {}).get("away", {}).get("hits", 0),
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
        on_update: Callable[
            [int], Coroutine[Any, Any, None]
        ],  # async callback(game_pk: int) -> None
    ) -> None:
        self.game_pk = game_pk
        self.on_update = on_update
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"ws-game-{self.game_pk}")

    def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        url = MLB_WS_TEMPLATE.format(game_pk=self.game_pk)
        backoff = WS_RECONNECT_BASE

        while not self._stop.is_set():
            try:
                log.info("WS connecting: game %s", self.game_pk)
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    log.info("WS connected:  game %s", self.game_pk)
                    backoff = WS_RECONNECT_BASE  # reset on successful connect
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
                return  # game probably ended
            except Exception as exc:
                if self._stop.is_set():
                    return
                log.warning(
                    "WS error game %s (%s) — reconnecting in %.1fs", self.game_pk, exc, backoff
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
        log.info(
            "Frontend WS connected: game %s (%d subscribers)",
            game_pk,
            len(self._subscriptions[game_pk]),
        )

    def disconnect(self, game_pk: int, ws: WebSocket) -> None:
        subs = self._subscriptions.get(game_pk, set())
        subs.discard(ws)
        log.info("Frontend WS disconnected: game %s (%d remaining)", game_pk, len(subs))

    async def broadcast(self, game_pk: int, payload: dict) -> None:
        """Send a JSON payload to all clients watching this game.

        SIM-103: Iterate over a SHALLOW COPY of the subscription set.
        ``await ws.send_text()`` yields control, so a concurrent connect()
        or disconnect() on the same game_pk would mutate the original set
        mid-loop and raise ``RuntimeError: Set changed size during iteration``.
        Copy is O(N); the receive set rarely exceeds a few dozen connections.
        Dead-connection cleanup still applies to the live underlying set.
        """
        live_subs = self._subscriptions.get(game_pk, set())
        if not live_subs:
            return
        subs_snapshot = set(live_subs)  # SIM-103: iteration-safe snapshot
        dead: set[WebSocket] = set()
        message = json.dumps(payload)
        for ws in subs_snapshot:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        # Apply cleanup to the live set (which may have changed during the
        # broadcast).  discard() is idempotent and safe even if a disconnect
        # already removed the ws.
        for ws in dead:
            live_subs.discard(ws)

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
            called when re-simulation should be triggered (Phase 5 integration
            point). If None, a log message is emitted instead.

        SIM-106: simulation_callback MUST be an async function.  Passing a
        plain sync function would either raise ``TypeError`` when the pipeline
        awaits it (best case) or silently never run if the returned value
        isn't awaited.  Both modes are hard to diagnose, so we check at
        __init__ time and raise immediately with a clear message.
        """
        # SIM-106 runtime guard — fail fast on misuse.
        if simulation_callback is not None and not asyncio.iscoroutinefunction(simulation_callback):
            raise TypeError(
                "LiveIngestionPipeline.simulation_callback must be an async "
                "function (defined with `async def`).  Got "
                f"{type(simulation_callback).__name__}={simulation_callback!r}.  "
                "Phase 5 wiring tip: wrap a sync runner as `async def runner(game_pk, "
                "state): result = await asyncio.to_thread(sync_runner, game_pk, state); ...`"
            )

        # SIM-153: dsn / redis_url default to env vars when omitted.
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

        # SIM-370: odds source.  Defaults to the env-selected provider
        # (MockOddsAPI unless ODDS_PROVIDER is set); tests inject a fake here.
        self._odds: OddsProvider = odds_provider or get_odds_provider()

        self._db: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
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
        # SIM-105: game_pks of games that have transitioned to Final.
        # _sync_live_games() consults this set to short-circuit redundant
        # _upsert_game_record() calls for the rest of the day's polling.
        # Pre-populated on pipeline start from raw.games to survive restarts
        # without an upsert storm.
        self._completed_games: set[int] = set()
        # SIM-101: per-game cached GameStateBuilder.  Built lazily on first
        # refresh; preserves last-processed at-bat index across refreshes so
        # _parse_play_history() only walks new plays.
        self._builders: dict[int, GameStateBuilder] = {}
        # SIM-340: per-game timestamp of the last prop-odds fetch cycle.  The
        # WS feed signals on every pitch; prop lines move far more slowly, so
        # _persist_prop_odds_cycle() consults this map and skips the fetch
        # unless PROP_FETCH_CADENCE_S seconds have elapsed for this game.
        self._last_prop_fetch: dict[int, datetime] = {}

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
        # SIM-105: pre-populate _completed_games from today's already-Final
        # games so a pipeline restart mid-afternoon doesn't trigger an
        # upsert storm for every game finished earlier in the day.
        await self._hydrate_completed_games()
        self._running = True
        self._schedule_task = asyncio.create_task(self._schedule_poller(), name="schedule-poller")
        log.info("Pipeline started.")

    async def _hydrate_completed_games(self) -> None:
        """SIM-105: load today's Final game_pks into _completed_games on boot."""
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
        today = date.today().strftime("%Y-%m-%d")
        params = {"sportId": 1, "gameTypes": GAME_TYPES, "date": today}

        async with self._http.get(SCHEDULE_URL, params=params) as resp:
            data = await resp.json()

        current_live: set[int] = set()

        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                if "rescheduleGameDate" in game or "resumeGameDate" in game:
                    continue

                game_pk = game["gamePk"]
                status = game.get("status", {}).get("abstractGameState", "")

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
                        # SIM-101: tear down the cached builder when its game
                        # ends — keeps _builders bounded by today's live slate.
                        self._builders.pop(game_pk, None)
                        # Do one final state refresh to capture the finished state
                        asyncio.create_task(self._refresh_game_state(game_pk))
                        # SIM-105: mark the game completed AFTER the final
                        # upsert fires.  All future polls will skip this game.
                        self._completed_games.add(game_pk)

                # SIM-105: skip _upsert_game_record() for games that have
                # already transitioned to Final.  For a 15-game slate with
                # 12 finished games this avoids ~1,080 wasted DB writes per
                # 3-hour afternoon.  Status==Final transitions still fall
                # through (the membership check kicks in next poll).
                if game_pk in self._completed_games and status == "Final":
                    continue

                # Upsert every game (Preview/Live/Final) into raw.games
                asyncio.create_task(self._upsert_game_record(game))

    async def _start_watching(self, game_pk: int) -> None:
        self._refresh_locks[game_pk] = asyncio.Lock()
        # SIM-101: pre-create the cached builder so the first refresh doesn't
        # take a (small) hit constructing one on the hot path.
        self._get_or_create_builder(game_pk)
        ws = MLBGameWebSocket(game_pk, on_update=self._refresh_game_state)
        self._ws_clients[game_pk] = ws
        ws.start()

    def _get_or_create_builder(self, game_pk: int) -> GameStateBuilder:
        """SIM-101: cache one GameStateBuilder per game.  Disposed when the
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

                # SIM-101: reuse the per-game builder so play history accumulates
                # incrementally instead of being rebuilt from scratch every WS msg.
                builder = self._get_or_create_builder(game_pk)
                game_state = await builder.build(feed)
                odds = await self._fetch_odds(game_pk)

                await self._upsert_lineup_state(game_pk, game_state)
                # SIM-099: pass both feed (for _fetch_feed fallback) and
                # game_state (for resimulate endpoint) — see _cache_to_redis.
                await self._cache_to_redis(
                    game_pk,
                    feed,
                    game_state,
                    feed["gameData"]["status"]["abstractGameState"],
                )
                await self._persist_odds(game_pk, odds)
                # SIM-340: persist player-prop odds on the same refresh cycle.
                # _persist_prop_odds_cycle() self-throttles to PROP_FETCH_CADENCE_S
                # per game so it does not fire on every WS pitch signal.  This is
                # the live wiring that finally invokes _persist_prop_odds (which
                # had been defined but never called before SIM-340).
                await self._persist_prop_odds_cycle(game_pk, game_state)

                # Determine whether this update marks the end of a plate
                # appearance before broadcasting, so the frontend knows whether
                # to expect incoming simulation results.
                pa_ended, completed_at_bat = self._should_resimulate(game_pk, feed)

                # Broadcast full payload to all frontend subscribers.
                # resim_triggered=True tells the frontend to show a loading
                # indicator while it waits for new simulation results.
                broadcast_payload = {
                    "type": "game_state_update",
                    "game_pk": game_pk,
                    "game_state": game_state,
                    "odds": odds,
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

    async def _fetch_odds(self, game_pk: int) -> dict:
        """
        Returns odds from the configured provider (SIM-370).  Defaults to the
        deterministic MockOddsAPI; set ODDS_PROVIDER (and implement
        RealOddsAPIProvider) to swap in a real feed without touching this code.
        """
        return self._odds_provider().get_odds(game_pk)

    @staticmethod
    def _collect_prop_player_ids(game_state: dict) -> list[int]:
        """
        SIM-340: Extract the player_ids eligible for prop-line capture from a
        built game_state.

        Props are offered on the two probable/active starting pitchers and on
        the batters in both lineups.  We pull:
          * current_pitcher_id (the pitcher on the mound right now)
          * every player_id in home_lineup / away_lineup (the hitters)

        Deduplicated, None-filtered, and order-stable so a fixed game_state
        always yields the same id list (keeps the dedup hash and tests stable).
        """
        ids: list[int] = []
        cp = game_state.get("current_pitcher_id")
        if cp:
            ids.append(int(cp))
        for side in ("home_lineup", "away_lineup"):
            for entry in game_state.get(side, []) or []:
                pid = entry.get("player_id")
                if pid:
                    ids.append(int(pid))
        # Order-stable dedup.
        seen: set[int] = set()
        unique: list[int] = []
        for pid in ids:
            if pid not in seen:
                seen.add(pid)
                unique.append(pid)
        return unique

    def _fetch_prop_odds(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        line_type: str = "current",
        books: list[tuple[str, bool]] | None = None,
        prop_stats: tuple[str, ...] = PROP_STATS,
    ) -> list[dict]:
        """
        SIM-340: Returns a flat list of prop-odds quotes for the given players.

        Multi-book + sharp flag: iterates over PROP_BOOKS (each a
        ``(book, is_sharp_book)`` pair) so every prop is captured at every book,
        with the sharp-book flag carried through to raw.prop_odds.  Capturing
        both sharp (Pinnacle, Circa) and soft (DraftKings, FanDuel) quotes lets
        the CLV engine (SIM-339) measure soft-vs-sharp divergence per prop.

        In Phase 7, replace the MockOddsAPI.get_prop_odds() call with a real
        provider that returns one quote per (book, player, prop_stat).  The book
        list and is_sharp_book classification live in PROP_BOOKS so they survive
        the provider swap untouched.
        """
        books = books if books is not None else PROP_BOOKS
        provider = self._odds_provider()  # SIM-370: env-selected, mock by default
        quotes: list[dict] = []
        for player_id in player_ids:
            for prop_stat in prop_stats:
                for book, is_sharp in books:
                    try:
                        quote = provider.get_prop_odds(
                            game_pk,
                            player_id,
                            prop_stat,
                            line_type=line_type,
                            book=book,
                            is_sharp_book=is_sharp,
                        )
                    except ValueError:
                        # Unknown prop_stat — skip rather than abort the cycle.
                        log.warning(
                            "skipping unknown prop_stat '%s' for player %s game %s",
                            prop_stat,
                            player_id,
                            game_pk,
                        )
                        continue
                    quotes.append(quote)
        return quotes

    async def _persist_prop_odds_cycle(self, game_pk: int, game_state: dict) -> int:
        """
        SIM-340: Live prop-odds persistence cycle.

        Called from _refresh_game_state() on every WS signal, but self-throttled
        to at most once per PROP_FETCH_CADENCE_S seconds per game (prop lines move
        far more slowly than the per-pitch WS feed).  When the cadence gate opens,
        it:
          1. Collects prop-eligible player_ids from the game_state.
          2. Fetches multi-book prop quotes (sharp + soft) via _fetch_prop_odds().
          3. Persists each quote via _persist_prop_odds() (the previously-unwired
             method this ticket activates).

        Returns the number of prop rows written this cycle (0 when the cadence
        gate is closed or no eligible players were found).
        """
        now = datetime.now(UTC)
        last = self._last_prop_fetch.get(game_pk)
        if last is not None and (now - last).total_seconds() < PROP_FETCH_CADENCE_S:
            # Cadence gate closed — too soon since the last prop fetch.
            return 0

        player_ids = self._collect_prop_player_ids(game_state)
        if not player_ids:
            # Nothing to price yet (e.g. lineups not posted) — don't stamp the
            # cadence clock so the next signal retries promptly.
            return 0

        self._last_prop_fetch[game_pk] = now
        quotes = self._fetch_prop_odds(game_pk, player_ids, line_type="current")
        written = 0
        for quote in quotes:
            try:
                await self._persist_prop_odds(quote)
                written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "prop persist failed game %s player %s %s @ %s: %s",
                    game_pk,
                    quote.get("player_id"),
                    quote.get("prop_stat"),
                    quote.get("book"),
                    exc,
                )
        if written:
            log.info(
                "SIM-340: persisted %d prop quotes for game %s (%d players × %d stats × %d books)",
                written,
                game_pk,
                len(player_ids),
                len(PROP_STATS),
                len(PROP_BOOKS),
            )
        return written

    async def capture_opening_prop_lines(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        books: list[tuple[str, bool]] | None = None,
        prop_stats: tuple[str, ...] = PROP_STATS,
    ) -> int:
        """
        SIM-340 / SIM-138: Opening-line capture hook for player props.

        Writes one raw.prop_odds row per (player, prop_stat, book) with
        line_type='opening'.  This is the prop analogue of the nightly opening
        game-line capture (opening_line_job.py / SIM-138): it records the FIRST
        line posted so opening→closing movement (and therefore CLV) is
        recoverable for props, not just game markets.

        Idempotent by virtue of the dedup hash (migration 0013): re-running with
        an unchanged opening line is a no-op via ON CONFLICT DO NOTHING.

        Returns the number of opening prop rows written.
        """
        quotes = self._fetch_prop_odds(
            game_pk,
            player_ids,
            line_type="opening",
            books=books,
            prop_stats=prop_stats,
        )
        written = 0
        for quote in quotes:
            try:
                await self._persist_prop_odds(quote)
                written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "opening prop persist failed game %s player %s %s: %s",
                    game_pk,
                    quote.get("player_id"),
                    quote.get("prop_stat"),
                    exc,
                )
        if written:
            log.info(
                "SIM-340: captured %d opening prop lines for game %s", written, game_pk
            )
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
            # SIM-086: fall back to None (not 0) when the schedule API omits the
            # venue.  raw.games.venue_id is now nullable; venue_backfill_job.py
            # fills these once the MLB API publishes the venue assignment.
            gd.get("venue", {}).get("id") or None,
            gd.get("teams", {}).get("home", {}).get("team", {}).get("id", 0),
            gd.get("teams", {}).get("away", {}).get("team", {}).get("id", 0),
        )

    @staticmethod
    def _odds_hash(odds: dict) -> str:
        """
        SIM-092: Deterministic SHA-256 fingerprint of an odds payload.

        Hashing rules:
          * Stable key order regardless of dict iteration order.
          * Numeric values are formatted with full precision so 1.5 and 1.50
            collide (they should — same line).
          * book / line_type / market_type / is_sharp_book are part of the
            hash because two books at the same price are still two distinct
            quotes.  source is NOT in the hash (it lives outside the unique
            index, on the table itself, paired with odds_hash by the index).

        Identical payloads produce identical hashes; an INSERT … ON CONFLICT
        (game_pk, source, odds_hash) DO NOTHING is therefore a no-op for any
        snapshot the pipeline has already written.
        """
        import hashlib

        # Keys in fixed order — do NOT change order without bumping the hash
        # space (would invalidate every previously stored hash, but the
        # partial unique index tolerates NULL so a re-fingerprint is safe).
        ordered_keys = (
            "book",
            "line_type",
            "market_type",
            "is_sharp_book",
            "home_ml",
            "away_ml",
            "home_spread",
            "home_spread_ml",
            "away_spread",
            "away_spread_ml",
            "total_line",
            "over_ml",
            "under_ml",
        )

        def _fmt(v: object) -> str:
            # Normalize numeric types so 1.5 and 1.50 hash identically;
            # bool / None / strings stringify directly.
            if v is None:
                return "∅"
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, float):
                # 6-decimal precision is plenty for American odds and lines.
                return f"{v:.6f}"
            return str(v)

        payload = "|".join(f"{k}={_fmt(odds.get(k))}" for k in ordered_keys)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _persist_odds(self, game_pk: int, odds: dict) -> None:
        """
        Inserts a fresh odds snapshot into raw.game_odds.

        SIM-133: Writes all four CLV-enabling columns
          (book, line_type, market_type, is_sharp_book).

        SIM-092: Also writes ``odds_hash`` (SHA-256 of the odds payload)
          and uses ON CONFLICT (game_pk, source, odds_hash) DO NOTHING
          so successive identical snapshots are deduplicated at write time.
          Backed by the partial unique index ``idx_game_odds_dedup``.

        The live pipeline always writes line_type='current'.  Opening lines are
        captured by the nightly opening line job (SIM-138).  Closing lines are
        designated by mark_closing_lines() which runs post-game.
        """
        odds_hash = self._odds_hash(odds)
        await self._db.execute(
            """
            INSERT INTO raw.game_odds
                (game_pk, source, is_mock,
                 book, line_type, market_type, is_sharp_book,
                 home_ml, away_ml,
                 home_spread, home_spread_ml, away_spread, away_spread_ml,
                 total_line, over_ml, under_ml,
                 odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            ON CONFLICT (game_pk, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """,
            game_pk,
            odds.get("source", "mock"),
            odds.get("is_mock", True),
            odds.get("book", "consensus"),
            odds.get("line_type", "current"),
            odds.get("market_type", "moneyline"),
            odds.get("is_sharp_book", False),
            odds.get("home_ml"),
            odds.get("away_ml"),
            odds.get("home_spread"),
            odds.get("home_spread_ml"),
            odds.get("away_spread"),
            odds.get("away_spread_ml"),
            odds.get("total_line"),
            odds.get("over_ml"),
            odds.get("under_ml"),
            odds_hash,
        )

    @staticmethod
    def _prop_odds_hash(prop: dict) -> str:
        """
        SIM-340: Deterministic SHA-256 fingerprint of a prop-odds payload —
        the prop analogue of _odds_hash() (SIM-092).

        Identical prop snapshots produce identical hashes, so an
        INSERT … ON CONFLICT (game_pk, player_id, source, odds_hash) DO NOTHING
        is a no-op for any snapshot already written.  This is what makes the
        live cadence safe: even if the cadence gate is loosened, the dedup hash
        collapses unchanged lines instead of accumulating a row per WS signal.

        prop_stat / book / line_type / is_sharp_book are part of the hash because
        two books (or the over vs the under, or current vs opening) at the same
        line are still distinct quotes.  source lives outside the hash, paired
        with it in the unique index.
        """
        import hashlib

        ordered_keys = (
            "player_id",
            "prop_stat",
            "book",
            "line_type",
            "is_sharp_book",
            "line",
            "over_ml",
            "under_ml",
        )

        def _fmt(v: object) -> str:
            if v is None:
                return "∅"
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, float):
                return f"{v:.6f}"
            return str(v)

        payload = "|".join(f"{k}={_fmt(prop.get(k))}" for k in ordered_keys)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _persist_prop_odds(self, prop: dict) -> None:
        """
        SIM-134: Inserts a fresh player prop odds snapshot into raw.prop_odds.

        Parameters
        ----------
        prop : dict returned by MockOddsAPI.get_prop_odds() (or a real provider
               in Phase 7).  Required keys:
                 game_pk, player_id, prop_stat, line,
                 over_ml, under_ml, book, line_type, is_sharp_book,
                 source, is_mock

        Notes
        -----
        The live pipeline writes line_type='current' during active game windows
        via _persist_prop_odds_cycle() (SIM-340 wiring).  Opening lines are
        captured by capture_opening_prop_lines() and the nightly opening_line_job
        (SIM-138).  Closing lines are designated by mark_closing_prop_lines().

        SIM-340: also writes ``odds_hash`` (SHA-256 of the prop payload) and uses
        ON CONFLICT (game_pk, player_id, source, odds_hash) DO NOTHING so the
        live cadence never accumulates duplicate rows for an unchanged line.
        Backed by the partial unique index ``idx_prop_odds_dedup`` (migration
        0013).

        The DB enforces prop_stat values via CHECK constraint (migration 0004).
        Any unknown prop_stat will fail here with a clear IntegrityError before
        the invalid value reaches the application layer.
        """
        odds_hash = self._prop_odds_hash(prop)
        await self._db.execute(
            """
            INSERT INTO raw.prop_odds
                (game_pk, player_id, source, is_mock,
                 prop_stat, line, over_ml, under_ml,
                 book, line_type, is_sharp_book, odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (game_pk, player_id, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """,
            prop["game_pk"],
            prop["player_id"],
            prop.get("source", "mock"),
            prop.get("is_mock", True),
            prop["prop_stat"],
            prop["line"],
            prop.get("over_ml"),
            prop.get("under_ml"),
            prop.get("book", "consensus"),
            prop.get("line_type", "current"),
            prop.get("is_sharp_book", False),
            odds_hash,
        )

    async def mark_closing_lines(self, game_pk: int, first_pitch_at: datetime) -> int:
        """
        SIM-133: Closing line designation job.

        Finds the most recent raw.game_odds snapshot with line_type='current'
        that was fetched before first_pitch_at and updates its line_type to
        'closing'.  This row becomes the reference line for CLV calculation.

        Should be called once per game immediately after first pitch is detected
        (when feed/live status transitions from 'Preview' to 'Live').

        Returns the number of rows updated (0 or 1 per market_type).
        """
        result = await self._db.execute(
            """
            WITH last_pre_pitch AS (
                SELECT id
                FROM raw.game_odds
                WHERE game_pk = $1
                  AND line_type = 'current'
                  AND fetched_at <= $2
                ORDER BY fetched_at DESC
                LIMIT 1
            )
            UPDATE raw.game_odds
               SET line_type = 'closing'
             WHERE id IN (SELECT id FROM last_pre_pitch)
            """,
            game_pk,
            first_pitch_at,
        )
        updated = int(result.split()[-1]) if result else 0
        if updated:
            log.info(
                "Closing line designated: game %s at %s (%d row updated)",
                game_pk,
                first_pitch_at.isoformat(),
                updated,
            )
        return updated

    async def mark_closing_prop_lines(self, game_pk: int, first_pitch_at: datetime) -> int:
        """
        SIM-340: Closing prop-line designation job — the prop analogue of
        mark_closing_lines() (SIM-133).

        For every distinct (player_id, prop_stat, book) on this game, finds the
        most recent raw.prop_odds snapshot with line_type='current' that was
        fetched at or before first_pitch_at and stamps it line_type='closing'.
        Each stamped row becomes the per-prop CLV reference line (SIM-339).

        Unlike game odds — which has one closing line per market_type — props
        fan out per player × stat × book, so this stamps one closing row per
        such combination via a DISTINCT ON window rather than a single LIMIT 1.

        Should be called once per game immediately after first pitch is detected
        (status transitions 'Preview' -> 'Live'), the same trigger point used
        for mark_closing_lines().

        Returns the number of prop rows updated to 'closing'.
        """
        result = await self._db.execute(
            """
            WITH last_pre_pitch AS (
                SELECT DISTINCT ON (player_id, prop_stat, book) id
                FROM raw.prop_odds
                WHERE game_pk = $1
                  AND line_type = 'current'
                  AND fetched_at <= $2
                ORDER BY player_id, prop_stat, book, fetched_at DESC
            )
            UPDATE raw.prop_odds
               SET line_type = 'closing'
             WHERE id IN (SELECT id FROM last_pre_pitch)
            """,
            game_pk,
            first_pitch_at,
        )
        updated = int(result.split()[-1]) if result else 0
        if updated:
            log.info(
                "Closing prop lines designated: game %s at %s (%d rows updated)",
                game_pk,
                first_pitch_at.isoformat(),
                updated,
            )
        return updated

    # ------------------------------------------------------------------
    # Redis cache
    # ------------------------------------------------------------------

    async def _cache_to_redis(
        self, game_pk: int, feed: dict, game_state: dict, status: str
    ) -> None:
        """
        SIM-099: Writes two Redis keys per game refresh:

        1. ``game_feed:{game_pk}``   — raw MLB Stats API feed/live dict.
           Used by ``_fetch_feed()`` as a rate-limit fallback: when a
           subsequent HTTP call returns 429/error, the pipeline returns the
           last known feed rather than dropping the refresh cycle entirely.

        2. ``game_state:{game_pk}``  — built game_state JSONB.
           Used by the manual resimulate endpoint
           (POST /api/games/{game_pk}/resimulate) to serve the last known
           state without hitting the DB.

        Root cause of SIM-099: previously only ``game_state:{game_pk}`` was
        written, but ``_fetch_feed()`` read ``game_feed:{game_pk}`` — a key
        that was never written.  The rate-limit fallback silently returned
        None for every live game, dropping every refresh cycle under load.
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
    await connection_manager.connect(game_pk, websocket)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data.strip().lower() == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        connection_manager.disconnect(game_pk, websocket)


def create_app(dsn=None, redis_url=None, simulation_callback=None) -> FastAPI:
    """SIM-104 + SIM-153: rate-limited resimulate endpoint + env-var DSN."""
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

    app = FastAPI(title="MLB Live Ingestion", lifespan=lifespan)
    app.include_router(ws_router)
    app.include_router(odds_router)

    @app.get("/api/pipeline/status")
    async def pipeline_status():
        return {
            "live_games": pipeline.live_game_pks,
            "live_game_count": len(pipeline.live_game_pks),
        }

    @app.post("/api/games/{game_pk}/resimulate")
    async def manual_resimulate(game_pk: int, response: Response):
        # SIM-104: per-game Redis cooldown (resim_cooldown:<game_pk>).
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

        builder = pipeline._get_or_create_builder(game_pk)  # SIM-101
        game_state = await builder.build(feed)
        await pipeline._signal_resimulation(game_pk, game_state)
        await connection_manager.broadcast(
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
