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
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

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
from pipeline.odds_provider import (
    BATTER_PROP_STATS,
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    LEGACY_GAME_MARKET_TYPES,
    PITCHER_PROP_STATS,
    PROP_STATS,
    OddsProvider,
    get_odds_provider,
    odds_rows_by_book,
    prop_rows_by_book,
)

# SIM-555: the load guard. Every writer shows it each non-empty row before the
# row is persisted; a refused row is logged, counted and never written.
from pipeline.odds_row_guard import CLOSING_STAMP_GRACE, RefusalTally, check_row

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
#: SIM-555: the schedule poll asks for each game's probable pitchers and posted
#: lineups, so the pre-game odds cycle knows whose props to store before a game
#: has a live feed (``teams.<side>.probablePitcher``, ``lineups.<side>Players``).
SCHEDULE_HYDRATE = "probablePitcher,lineups"

SCHEDULE_POLL_S = 30  # how often to check for newly-live games
#: SIM-519 Part C: the league's day turns over in Eastern time.
_EASTERN = ZoneInfo("America/New_York")
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
# SIM-340: Prop-odds ingestion config (cadence)
# ---------------------------------------------------------------------------
# SIM-555 (2026-09-28): the provider names the books. Each odds cycle asks the
# provider for every book's row of a market (``odds_rows_by_book`` /
# ``prop_rows_by_book``) and persists every row the load guard keeps, each
# under its own label (``'bp:<id>'`` from BettingPros; the mock's one row keeps
# ``'consensus'``). The old fixed book list (PROP_BOOKS) is gone: it wrote one
# price set four times under four names, two of which the vendor never carried.
#
# The provider is selected by ODDS_PROVIDER (via the SIM-370
# get_odds_provider() seam): the deterministic MockOddsAPI is the default, and
# ODDS_PROVIDER=bettingpros switches to the real BettingProsOddsProvider.

# The 15 prop markets live in pipeline/odds_provider.py (PROP_STATS, split into
# PITCHER_PROP_STATS / BATTER_PROP_STATS — SIM-421). This module imports them
# and re-exports PROP_STATS; MockOddsAPI._PROP_CONFIG and the raw.prop_odds
# CHECK constraint (migration 0022) list the same 15 values.

# SIM-421: the role a prop-eligible player plays, as _collect_prop_player_roles()
# reports it. A pitcher is asked only for the pitcher markets and a hitter only
# for the batter markets. "both" is the safe default: a player whose role is
# unknown, or a two-way player who bats AND pitches, is asked for every market.
PROP_ROLE_PITCHER = "pitcher"
PROP_ROLE_BATTER = "batter"
PROP_ROLE_BOTH = "both"

#: SIM-421: boxscore position codes that mark a pitcher (P; some feeds use
#: SP / RP; TWP is a two-way player).
_PITCHER_POSITION_CODES = frozenset({"P", "SP", "RP", "TWP"})

# SIM-340: prop-odds fetch cadence.  Prop lines move far more slowly than the
# WS refresh rate (a WS message fires on every pitch).  Polling every book ×
# player × prop on every WS signal would issue thousands of redundant writes
# per game.  We therefore gate prop fetches to at most once per
# PROP_FETCH_CADENCE_S seconds per game; the dedup hash (migration 0013)
# collapses any identical snapshot that still slips through.  SIM-555: the
# game markets (all fifteen, every book) ride the same cadence.
PROP_FETCH_CADENCE_S = 60

#: SIM-555: the cadence of the pre-game odds cycle, per game. The schedule poll
#: runs every 30 seconds for every game of the day. Pre-game lines move slowly,
#: so a game in the ``Preview`` state reads the vendor at most once every ten
#: minutes. The in-play cycle keeps PROP_FETCH_CADENCE_S.
PREGAME_ODDS_CADENCE_S = 600

#: SIM-546: the near-start window. Closing lines move most in the last minutes,
#: so once the scheduled start is this close (or past, while the game waits in
#: ``Preview``), the pre-game cycle reads the vendor every
#: PREGAME_NEAR_START_CADENCE_S instead. The live marker promotes the last row
#: fetched before first pitch, so that row is then at most a minute old.
PREGAME_NEAR_START_WINDOW_S = 900

#: SIM-546: the pre-game cadence inside the near-start window: the live cadence.
PREGAME_NEAR_START_CADENCE_S = PROP_FETCH_CADENCE_S

# ---------------------------------------------------------------------------
# SIM-555: odds rows, JSON and the database
# ---------------------------------------------------------------------------

#: The by-book row keys that serve the load guard and the logs only. They are
#: never stored and never sent to a browser: the scheduled start (a datetime),
#: the official date and, SIM-555, a made-up game's postponed original start.
_GUARD_ONLY_KEYS = frozenset({"scheduled_start", "game_date", "postponed_start"})

#: The odds fields of a prop row; a row with all three empty is not a quote.
_PROP_ODDS_FIELDS: tuple[str, ...] = ("line", "over_ml", "under_ml")


def _json_default(value: Any) -> str:
    """The ``json.dumps`` fallback: a date or a datetime as ISO-8601 text (SIM-555).

    The BettingPros rows carry ``book_line_at`` as a datetime; a bare
    ``json.dumps`` raises ``TypeError`` on it. Any other unknown type still
    raises, so a real bug stays loud.
    """
    if isinstance(value, date):  # a datetime is a date too
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _jsonable_odds(odds: Mapping[str, Any] | None) -> dict[str, Any]:
    """An odds row made safe for ``json.dumps`` (SIM-555).

    The row loses the guard-only keys (``scheduled_start``, ``game_date``,
    ``postponed_start``) and sends a date or a datetime (``book_line_at``) as
    ISO-8601 text. Every other key passes through unchanged. ``None`` gives an
    empty dict.
    """
    out: dict[str, Any] = {}
    for key, value in (odds or {}).items():
        if key in _GUARD_ONLY_KEYS:
            continue
        out[key] = value.isoformat() if isinstance(value, date) else value
    return out


def _odds_facts_may_be_stale(game: Mapping[str, Any]) -> bool:
    """True when the odds provider's cached facts of a schedule entry's game may be old (SIM-555).

    The provider keeps a found game's schedule facts and vendor event for the
    process lifetime. A game looked up before a postponement keeps its
    original start and event. Three kinds of entry say the facts changed:

      * the original date's entry of a postponed or suspended game
        (``rescheduleGameDate`` / ``resumeGameDate``);
      * a postponed entry not yet rescheduled (``detailedState`` starting
        "Postponed", or ``codedGameState`` "D", as the provider reads it);
      * a made-up or resumed game before its first pitch (``rescheduledFrom`` /
        ``resumedFrom`` while ``Preview``). The poll reads one calendar date's
        schedule (``date.today()``, UTC in the container), so it misses the
        original date's entry of a game postponed after midnight UTC; this
        entry catches that game on its new date.

    The poll drops the cached facts on each such entry. A live game keeps
    them, so the per-pitch refresh reads nothing again.
    """
    if "rescheduleGameDate" in game or "resumeGameDate" in game:
        return True
    status = game.get("status") or {}
    if str(status.get("detailedState") or "").startswith("Postponed"):
        return True
    if status.get("codedGameState") == "D":
        return True
    made_up = "rescheduledFrom" in game or "resumedFrom" in game
    return made_up and status.get("abstractGameState") == "Preview"


def _stamp_param(value: Any) -> datetime | None:
    """A row's ``book_line_at`` as an aware datetime for the TIMESTAMPTZ column (SIM-555).

    A naive datetime is read as UTC (the vendor stamps in UTC); ISO-8601 text is
    parsed; anything else (``None``, a mock row without the key) gives ``None``.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _scheduled_start(game: Mapping[str, Any]) -> datetime | None:
    """SIM-546: a schedule entry's scheduled start as an aware UTC datetime.

    The entry's ``gameDate`` is ISO 8601 UTC text ("2026-10-09T23:05:00Z").
    A missing or unreadable value gives ``None``.
    """
    return _stamp_param(game.get("gameDate"))


def pregame_odds_cadence_s(game: Mapping[str, Any], now: datetime) -> int:
    """PURE (SIM-546): the pre-game cycle's cadence in seconds for one schedule entry.

    Inside PREGAME_NEAR_START_WINDOW_S of the scheduled start, or past the
    start while the game still waits in ``Preview`` (a delay), the cadence is
    PREGAME_NEAR_START_CADENCE_S (60 s). Further out, or with no readable
    start, it is PREGAME_ODDS_CADENCE_S (600 s). ``now`` is an aware datetime.
    """
    start = _scheduled_start(game)
    if start is None:
        return PREGAME_ODDS_CADENCE_S
    if (start - now).total_seconds() <= PREGAME_NEAR_START_WINDOW_S:
        return PREGAME_NEAR_START_CADENCE_S
    return PREGAME_ODDS_CADENCE_S


def _game_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a game-odds row carries at least one price or line."""
    return any(row.get(f) is not None for f in GAME_ODDS_FIELDS)


def _prop_row_has_odds(row: Mapping[str, Any]) -> bool:
    """True when a prop-odds row carries a line or a price."""
    return any(row.get(f) is not None for f in _PROP_ODDS_FIELDS)


#: The raw.game_odds INSERT. SIM-555 adds ``book_line_at``; ``odds_hash`` stays
#: the LAST parameter (two pinned tests read the hash as the final argument).
_GAME_ODDS_INSERT_SQL = """
            INSERT INTO raw.game_odds
                (game_pk, source, is_mock,
                 book, line_type, market_type, is_sharp_book,
                 home_ml, away_ml,
                 home_spread, home_spread_ml, away_spread, away_spread_ml,
                 total_line, over_ml, under_ml,
                 draw_ml, book_line_at, odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            ON CONFLICT (game_pk, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """

#: The raw.prop_odds INSERT. SIM-555 adds ``book_line_at``; ``odds_hash`` stays last.
_PROP_ODDS_INSERT_SQL = """
            INSERT INTO raw.prop_odds
                (game_pk, player_id, source, is_mock,
                 prop_stat, line, over_ml, under_ml,
                 book, line_type, is_sharp_book, book_line_at, odds_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (game_pk, player_id, source, odds_hash)
              WHERE odds_hash IS NOT NULL
              DO NOTHING
            """


def game_odds_insert_args(game_pk: int, odds: Mapping[str, Any]) -> tuple[Any, ...]:
    """The bind values of one raw.game_odds INSERT, in ``_GAME_ODDS_INSERT_SQL`` order.

    SIM-555: ``book_line_at`` (the vendor's stamp on the row's line) sits just
    before ``odds_hash``, which stays LAST: two pinned tests read the hash as
    the final argument. The stamp is not in the hash, so an unchanged line
    re-fetched later still deduplicates. The live pipeline, the historical
    loader and the opening-line job all bind through this one function, so
    every writer sends the hash the live schema requires (``odds_hash`` is
    NOT NULL on raw.game_odds since migration 0012).
    """
    return (
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
        odds.get("draw_ml"),  # SIM-421: the tie price of a three-way segment moneyline
        _stamp_param(odds.get("book_line_at")),  # SIM-555: the vendor's stamp
        LiveIngestionPipeline._odds_hash(dict(odds)),  # stays LAST: two pinned tests read it last
    )


def prop_odds_insert_args(prop: Mapping[str, Any]) -> tuple[Any, ...]:
    """The bind values of one raw.prop_odds INSERT, in ``_PROP_ODDS_INSERT_SQL`` order.

    SIM-555: ``book_line_at`` sits just before ``odds_hash``, which stays last;
    the stamp is not in the hash. Every prop writer binds through this function.
    """
    return (
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
        _stamp_param(prop.get("book_line_at")),  # SIM-555: the vendor's stamp
        LiveIngestionPipeline._prop_odds_hash(dict(prop)),  # stays LAST
    )


async def insert_game_odds_rows(conn: Any, game_pk: int, rows: Sequence[Mapping[str, Any]]) -> int:
    """SIM-555: insert game-odds rows in one ``executemany``, with the dedup.

    ``conn`` is an asyncpg pool or connection. The SQL is the live writer's
    (``odds_hash`` last, ``ON CONFLICT ... DO NOTHING``). Returns the number of
    rows sent (the dedup may insert fewer). An empty list sends nothing.
    """
    if not rows:
        return 0
    await conn.executemany(
        _GAME_ODDS_INSERT_SQL, [game_odds_insert_args(game_pk, row) for row in rows]
    )
    return len(rows)


async def insert_prop_odds_rows(conn: Any, rows: Sequence[Mapping[str, Any]]) -> int:
    """SIM-555: insert prop-odds rows in one ``executemany``, with the dedup.

    The prop analogue of :func:`insert_game_odds_rows`. Returns the rows sent.
    """
    if not rows:
        return 0
    await conn.executemany(_PROP_ODDS_INSERT_SQL, [prop_odds_insert_args(row) for row in rows])
    return len(rows)


# ---------------------------------------------------------------------------
# SIM-546: every live game's closing rows
# ---------------------------------------------------------------------------
# A closing row is the last price a book posted before first pitch. At first
# pitch the live pipeline promotes, per (market, book) for game odds and per
# (player, prop stat, book) for props, the latest pre-pitch row from 'current'
# to 'closing'. The rule (the design's D1):
#
#   * Read one row per key among the rows with line_type 'current' OR
#     'closing' fetched at or before the first-pitch instant. A 'closing' row
#     sorts first: a key that holds one is done, whatever was fetched after
#     it. So a second call promotes nothing, even after a restart mid-game
#     reads up to a later instant and finds in-play 'current' rows.
#   * A key with no closing row reads its latest 'current' row whose stamp
#     the load guard keeps as a closing row: the stamp is no later than the
#     scheduled start plus 15 minutes (CLOSING_STAMP_GRACE; a row with no stamp
#     passes). The read applies the rule, so in a delayed game a book that
#     moved its line during the delay keeps its last line inside the grace
#     (the design's D4). Promote that row when the full guard keeps it.
#   * Rewrite the row's odds_hash to the hash of the same row with line_type
#     'closing'. The nightly loader's identical closing row then deduplicates
#     against it. When a row with that hash already exists under the key of the
#     dedup index, the loader got there first, and the row stays 'current'.

#: SIM-546: one pre-pitch game row per (market, book): its closing row when it
#: has one, else its latest current row stamped no later than ``$3`` (the
#: scheduled start plus the grace; NULL = no stamp check).
_CLOSING_GAME_READ_SQL = """
            SELECT DISTINCT ON (market_type, book)
                   id, source, line_type, book, market_type, is_sharp_book,
                   book_line_at, odds_hash,
                   home_ml, away_ml, draw_ml, home_spread, home_spread_ml,
                   away_spread, away_spread_ml, total_line, over_ml, under_ml
            FROM raw.game_odds
            WHERE game_pk = $1
              AND line_type IN ('current', 'closing')
              AND fetched_at <= $2
              AND (line_type = 'closing' OR $3::timestamptz IS NULL
                   OR book_line_at IS NULL OR book_line_at <= $3::timestamptz)
            ORDER BY market_type, book, (line_type = 'closing') DESC,
                     fetched_at DESC, id DESC
            """

#: SIM-546: the prop analogue: one pre-pitch row per (player, prop stat, book),
#: by the same order and the same stamp bound ``$3``.
_CLOSING_PROP_READ_SQL = """
            SELECT DISTINCT ON (player_id, prop_stat, book)
                   id, source, line_type, player_id, prop_stat, book,
                   is_sharp_book, book_line_at, odds_hash,
                   line, over_ml, under_ml
            FROM raw.prop_odds
            WHERE game_pk = $1
              AND line_type IN ('current', 'closing')
              AND fetched_at <= $2
              AND (line_type = 'closing' OR $3::timestamptz IS NULL
                   OR book_line_at IS NULL OR book_line_at <= $3::timestamptz)
            ORDER BY player_id, prop_stat, book, (line_type = 'closing') DESC,
                     fetched_at DESC, id DESC
            """

#: SIM-546: the new hashes already stored for one (game, source). The dedup
#: index of raw.game_odds is (game_pk, source, odds_hash).
_CLOSING_GAME_EXISTING_SQL = """
            SELECT source, odds_hash
            FROM raw.game_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """

#: SIM-546: the prop analogue. The dedup index of raw.prop_odds is
#: (game_pk, player_id, source, odds_hash); the caller matches the player.
_CLOSING_PROP_EXISTING_SQL = """
            SELECT player_id, source, odds_hash
            FROM raw.prop_odds
            WHERE game_pk = $1 AND source = $2 AND odds_hash = ANY($3::text[])
            """

#: SIM-546: promote the picked rows in one statement. The line_type guard makes
#: a concurrent second call harmless: it skips a row already promoted.
_CLOSING_GAME_UPDATE_SQL = """
            UPDATE raw.game_odds AS g
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  g.id = v.id AND g.line_type = 'current'
            """

#: SIM-546: the prop analogue of the update.
_CLOSING_PROP_UPDATE_SQL = """
            UPDATE raw.prop_odds AS p
            SET    line_type = 'closing', odds_hash = v.odds_hash
            FROM   unnest($1::bigint[], $2::text[]) AS v(id, odds_hash)
            WHERE  p.id = v.id AND p.line_type = 'current'
            """

#: SIM-546: the price columns of a game row and of a prop row. Postgres stores a
#: price as INTEGER. A writer may have hashed it as a float: the BettingPros
#: provider reads every price as a float (-110.0, not -110).
_GAME_PRICE_KEYS: tuple[str, ...] = (
    "home_ml",
    "away_ml",
    "draw_ml",
    "home_spread_ml",
    "away_spread_ml",
    "over_ml",
    "under_ml",
)
_PROP_PRICE_KEYS: tuple[str, ...] = ("over_ml", "under_ml")

#: SIM-546: the line columns. Postgres stores a line as FLOAT. A writer may have
#: hashed a whole line as an int (5, not 5.0).
_GAME_LINE_KEYS: tuple[str, ...] = ("home_spread", "away_spread", "total_line")
_PROP_LINE_KEYS: tuple[str, ...] = ("line",)


def _as_float(value: Any) -> Any:
    """An int price as a float; any other value unchanged."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _as_int(value: Any) -> Any:
    """A whole-number float line as an int; any other value unchanged."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _hash_typings(row: Mapping[str, Any], is_prop: bool) -> list[dict[str, Any]]:
    """The four ways a writer may have typed the row's numbers before it hashed them.

    The hash writes an int as ``-110`` and a float as ``-110.000000``, so one
    price hashes two ways. The first typing is the row as read. The last is the
    BettingPros provider's typing (float prices, float lines): the typing the
    nightly loader writes.
    """
    prices = _PROP_PRICE_KEYS if is_prop else _GAME_PRICE_KEYS
    lines = _PROP_LINE_KEYS if is_prop else _GAME_LINE_KEYS
    as_read = dict(row)
    int_lines = {**as_read, **{k: _as_int(as_read[k]) for k in lines if k in as_read}}
    both = {**int_lines, **{k: _as_float(as_read[k]) for k in prices if k in as_read}}
    float_prices = {**as_read, **{k: _as_float(as_read[k]) for k in prices if k in as_read}}
    return [as_read, int_lines, both, float_prices]


def closing_hash(row: Mapping[str, Any]) -> str:
    """SIM-546: the odds_hash of ``row`` as a closing row.

    The hash is ``LiveIngestionPipeline._odds_hash`` (for a prop row, one with
    ``prop_stat``: ``_prop_odds_hash``) of the row with ``line_type`` set to
    ``'closing'``. The row's numbers are typed the way its writer typed them:
    the typing whose hash at the row's own line type equals the stored
    ``odds_hash``. A row whose stored hash matches no typing (or has no hash)
    takes the BettingPros provider's typing, the one the nightly loader writes.
    """
    is_prop = row.get("prop_stat") is not None
    hasher = LiveIngestionPipeline._prop_odds_hash if is_prop else LiveIngestionPipeline._odds_hash
    typings = _hash_typings(row, is_prop)
    stored = row.get("odds_hash")
    chosen = typings[-1]
    if stored is not None:
        own_type = row.get("line_type", "current")
        for typing in typings:
            if hasher({**typing, "line_type": own_type}) == stored:
                chosen = typing
                break
    return hasher({**chosen, "line_type": "closing"})


def closing_candidates(
    rows: Sequence[Mapping[str, Any]], *, scheduled_start: datetime | None
) -> list[tuple[int, str]]:
    """PURE (SIM-546): the (row id, new odds_hash) of every row to promote.

    ``rows`` are the promotion's read: one pre-pitch row per key, 'current' or
    'closing' (per (market_type, book) for game odds, per (player_id,
    prop_stat, book) for props). The read returns a key's closing row when it
    has one, else its latest current row stamped inside the grace. The
    function picks a row when it is 'current' and the load guard keeps it as a
    closing row with ``scheduled_start`` attached (``check_row``: the prices,
    and the stamp no later than the start plus 15 minutes). A 'closing' row is
    already promoted, so its key is done. The new hash is :func:`closing_hash`.
    """
    picks: list[tuple[int, str]] = []
    for row in rows:
        if row.get("line_type") != "current":
            continue
        probe = {**row, "line_type": "closing", "scheduled_start": scheduled_start}
        if check_row(probe) is not None:
            continue
        picks.append((int(row["id"]), closing_hash(row)))
    return picks


def _updated_count(result: Any) -> int:
    """The row count of an asyncpg status string such as ``'UPDATE 12'`` (else 0)."""
    try:
        return int(str(result).split()[-1])
    except (IndexError, ValueError):
        return 0


async def _promote_closing_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
    is_prop: bool,
) -> int:
    """SIM-546: one read, one existence check per source, then one update."""
    read_sql = _CLOSING_PROP_READ_SQL if is_prop else _CLOSING_GAME_READ_SQL
    stamp_bound = None if scheduled_start is None else scheduled_start + CLOSING_STAMP_GRACE
    rows = [dict(r) for r in await conn.fetch(read_sql, game_pk, first_pitch_at, stamp_bound)]
    picks = closing_candidates(rows, scheduled_start=scheduled_start)
    if not picks:
        return 0
    by_id = {int(r["id"]): r for r in rows}

    def _key(player_id: Any, source: Any, odds_hash: Any) -> tuple[Any, ...]:
        key = (str(source), str(odds_hash))
        return (int(player_id), *key) if is_prop else key

    hashes_by_source: dict[str, list[str]] = {}
    for row_id, new_hash in picks:
        hashes_by_source.setdefault(str(by_id[row_id].get("source")), []).append(new_hash)
    existing_sql = _CLOSING_PROP_EXISTING_SQL if is_prop else _CLOSING_GAME_EXISTING_SQL
    taken: set[tuple[Any, ...]] = set()
    for source, hashes in hashes_by_source.items():
        for hit in await conn.fetch(existing_sql, game_pk, source, hashes):
            taken.add(_key(hit["player_id"] if is_prop else None, hit["source"], hit["odds_hash"]))
    keep = [
        (row_id, new_hash)
        for row_id, new_hash in picks
        if _key(by_id[row_id].get("player_id"), by_id[row_id].get("source"), new_hash) not in taken
    ]
    if not keep:
        return 0
    update_sql = _CLOSING_PROP_UPDATE_SQL if is_prop else _CLOSING_GAME_UPDATE_SQL
    result = await conn.execute(update_sql, [i for i, _ in keep], [h for _, h in keep])
    return _updated_count(result)


async def promote_closing_game_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
) -> int:
    """SIM-546: promote one game's closing rows in raw.game_odds, one per (market, book).

    ``conn`` is an asyncpg pool or connection. ``first_pitch_at`` bounds the rows
    read (``fetched_at`` at or before it). ``scheduled_start`` feeds the load
    guard's closing-stamp rule (``None`` = no stamp check). Returns the rows
    promoted. A second call promotes nothing, even one with a later
    ``first_pitch_at`` (a restart mid-game): a key that holds a closing row is
    done (the rule above).
    """
    return await _promote_closing_rows(
        conn, game_pk, first_pitch_at, scheduled_start=scheduled_start, is_prop=False
    )


async def promote_closing_prop_rows(
    conn: Any,
    game_pk: int,
    first_pitch_at: datetime,
    *,
    scheduled_start: datetime | None,
) -> int:
    """SIM-546: the prop analogue: one closing row per (player, prop stat, book)."""
    return await _promote_closing_rows(
        conn, game_pk, first_pitch_at, scheduled_start=scheduled_start, is_prop=True
    )


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

    This is the deterministic default behind the SIM-370 provider seam
    (get_odds_provider()); set ODDS_PROVIDER=bettingpros to switch the live
    pipeline to the real BettingProsOddsProvider instead.  This class stays as
    the local dev / testing source.

    American odds encoding:
      Favourite:  odds = -(prob / (1-prob)) * 100  (negative integer)
      Underdog:   odds = ((1-prob) / prob) * 100   (positive integer)

    SIM-134: Added get_prop_odds() for the 7 Betting-Analyst-confirmed prop markets.
    SIM-421: the eight markets the book already offers joined them (15 in all).
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
    #   - SIM-421 markets: doubles / triples / stolen bases sit at 0.5 with a
    #     plus-money over (a rare event); outs recorded centres on a 5.2-inning
    #     start (16.5); hits allowed on 4.5; hits+runs+RBIs on 1.5.
    # The keys are exactly PROP_STATS (a SIM-421 unit test enforces it).
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
        # SIM-421: the eight markets the book already offers.
        "singles": (0.5, 0.5, (-120, -105), (-115, -100)),
        "doubles": (0.5, 0.0, (+150, +200), (-250, -190)),
        "triples": (0.5, 0.0, (+600, +900), (-1400, -1000)),
        "runs": (0.5, 0.5, (-125, -105), (-120, -100)),
        "stolen_bases": (0.5, 0.0, (+250, +450), (-650, -350)),
        "hits_runs_rbis": (1.5, 1.0, (-120, -105), (-120, -105)),
        "outs_recorded": (16.5, 1.5, (-120, -105), (-120, -105)),
        "hits_allowed": (4.5, 1.0, (-120, -105), (-120, -105)),
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
        market_type   : any value of ``GAME_MARKET_TYPES`` (``run_line`` is the
                        legacy alias of ``runline``); an unknown value raises
                        ``ValueError`` like the real provider does
        book          : book identifier (e.g. 'consensus', 'pinnacle', 'draftkings')
        is_sharp_book : TRUE for sharp books used as CLV reference (e.g. Pinnacle, Circa)

        SIM-421 (2026-09-12): the three full-game markets keep the original
        shape (one row carries the moneyline, the run line and the total). A
        segment or team market fills only its own fields, the way the real
        provider does — a first-inning moneyline gets a ``draw_ml`` (a segment
        can end tied), a team total a lower line, a yes / no market an over /
        under pair at 0.5.
        """
        canonical = "runline" if market_type == "run_line" else market_type
        if canonical not in GAME_MARKET_KIND:
            known = ", ".join(GAME_MARKET_TYPES)
            raise ValueError(f"Unknown market_type '{market_type}'. Known values: {known}")
        rng = random.Random(game_pk)

        # Home win probability: slight home-field edge built in
        home_win_prob = rng.uniform(0.38, 0.64)
        away_win_prob = 1.0 - home_win_prob

        # SIM-132: Apply realistic book vig so mock lines don't produce zero-
        # overround prices.  Without vig, implied probs sum to exactly 1.0 —
        # no real book prices this way.  Every edge calculation, calibration
        # target, and display component built through Phase 6 would be trained
        # against lines that don't exist in real markets; switching the seam to
        # real lines (ODDS_PROVIDER=bettingpros) shrinks the edge estimates
        # 3–8 pp overnight.
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

        result: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "mock",
            "is_mock": True,
            # SIM-133 CLV columns
            "book": book,
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": is_sharp_book,
        }
        for field in GAME_ODDS_FIELDS:
            result[field] = None
        if canonical in LEGACY_GAME_MARKET_TYPES:
            result.update(
                {
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
            )
            return result

        # A segment / team market: its own deterministic draw, seeded on the
        # game AND the market so two markets of one game never share numbers.
        mrng = random.Random(game_pk * 1_000 + (GAME_MARKET_TYPES.index(canonical) + 1))
        kind = GAME_MARKET_KIND[canonical]
        if kind == "three_way":
            # A first-inning / first-five moneyline: the tie is a real outcome,
            # so the three prices carry the vig between them.
            tie_prob = mrng.uniform(0.18, 0.30)
            side_prob = (1.0 - tie_prob) / 2.0
            edge = mrng.uniform(-0.06, 0.06)
            result["home_ml"] = MockOddsAPI._prob_to_american((side_prob + edge) * (1 + vig / 3))
            result["away_ml"] = MockOddsAPI._prob_to_american((side_prob - edge) * (1 + vig / 3))
            result["draw_ml"] = MockOddsAPI._prob_to_american(tie_prob * (1 + vig / 3))
        elif kind == "moneyline":
            # First team to score: near a coin flip with the book's margin.
            p = mrng.uniform(0.44, 0.56)
            result["home_ml"] = MockOddsAPI._prob_to_american(p * (1 + vig / 2))
            result["away_ml"] = MockOddsAPI._prob_to_american((1.0 - p) * (1 + vig / 2))
        elif kind == "runline":
            spread = mrng.choice([-0.5, 0.5])
            result["home_spread"], result["away_spread"] = spread, -spread
            # SIM-546: every mock price is a valid American price. A raw
            # integer draw put about half of them inside (-100, 100) and
            # some at 0, which the de-vig refuses for both sides.
            p_home = mrng.uniform(0.40, 0.60)
            result["home_spread_ml"] = MockOddsAPI._prob_to_american(p_home * (1 + vig / 2))
            result["away_spread_ml"] = MockOddsAPI._prob_to_american((1.0 - p_home) * (1 + vig / 2))
        elif kind == "total":
            # The line scales with the slice of the game and the side.
            if canonical.startswith("f1_"):
                line = mrng.choice([0.5, 1.5])
            elif canonical.startswith("f5_team"):
                line = mrng.choice([1.5, 2.0, 2.5])
            elif canonical.startswith("f5_"):
                line = mrng.choice([4.0, 4.5, 5.0])
            else:  # a full-game team total
                line = mrng.choice([3.5, 4.0, 4.5, 5.0])
            result["total_line"] = line
            p_over = mrng.uniform(0.40, 0.60)
            result["over_ml"] = MockOddsAPI._prob_to_american(p_over * (1 + vig / 2))
            result["under_ml"] = MockOddsAPI._prob_to_american((1.0 - p_over) * (1 + vig / 2))
        elif kind == "yes_no":
            result["total_line"] = 0.5
            p_yes = mrng.uniform(0.40, 0.60)
            result["over_ml"] = MockOddsAPI._prob_to_american(p_yes * (1 + vig / 2))
            result["under_ml"] = MockOddsAPI._prob_to_american((1.0 - p_yes) * (1 + vig / 2))
        return result

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
        prop_stat     : one of the 15 markets in PROP_STATS (pipeline/odds_provider.py):
                        the pitcher markets 'strikeouts' | 'earned_runs' | 'walks'
                        | 'outs_recorded' | 'hits_allowed' and the batter markets
                        'hits' | 'home_runs' | 'total_bases' | 'rbis' | 'singles'
                        | 'doubles' | 'triples' | 'runs' | 'stolen_bases'
                        | 'hits_runs_rbis'
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
            If prop_stat is not in the 15 known markets.  Prevents silent
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

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """SIM-555: the by-book protocol method. The mock has one book: its one row.

        The row keeps the mock's ``consensus`` label and, for the three
        full-game markets, its all-three-markets shape. The method reads
        ``self.get_odds``, so a subclass that overrides the one-row method (a
        test fake that raises on one market) answers here too. Call it on an
        instance, as the pipeline does.
        """
        return [self.get_odds(game_pk, line_type=line_type, market_type=market_type)]

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """SIM-555: the by-book protocol method. The mock has one book: its one row.

        Reads ``self.get_prop_odds``, as :meth:`get_odds_by_book` reads
        ``self.get_odds``.
        """
        return [self.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)]


# FastAPI router — mounts at /api/odds
odds_router = APIRouter(prefix="/api/odds", tags=["odds"])


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

        SIM-555: a date or a datetime in the payload goes out as ISO-8601 text
        (the by-book odds rows carry ``book_line_at`` as a datetime); the
        refresh loop also strips the odds row before it gets here.
        """
        live_subs = self._subscriptions.get(game_pk, set())
        if not live_subs:
            return
        subs_snapshot = set(live_subs)  # SIM-103: iteration-safe snapshot
        dead: set[WebSocket] = set()
        message = json.dumps(payload, default=_json_default)
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
        # SIM-519 Part C: the browser-message publisher; None = the in-process
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
        # SIM-421 / SIM-555: the same cadence clock for every game market.
        self._last_game_odds_fetch: dict[int, datetime] = {}
        # SIM-555: the pre-game cycle's own clock per game (PREGAME_ODDS_CADENCE_S).
        self._last_pregame_fetch: dict[int, datetime] = {}
        # SIM-555: the load guard's refusals, counted by rule, market and book.
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

        SIM-555: a game that has not started (``Preview``) gets the pre-game
        odds cycle (:meth:`_persist_pregame_odds`), so every book's current
        line is stored before first pitch, not only once the game is live. An
        entry of a postponed, suspended or made-up game first drops the odds
        provider's cached facts of that game (:func:`_odds_facts_may_be_stale`).
        """
        # SIM-519 Part C: yesterday through tomorrow in Eastern time, the
        # league's day. The container's clock is UTC, so date.today() read
        # tomorrow's schedule from 8 pm Eastern while tonight's games were live.
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
                        # SIM-546: the last pre-pitch row per market and book
                        # becomes the closing row BEFORE the watcher starts,
                        # so the live cycle's first in-play row comes after.
                        await self._mark_closing_rows(game_pk, game)
                        await self.on_game_live(game_pk, datetime.now(UTC))
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

                elif status == "Preview":
                    # SIM-555: store every book's current line before first
                    # pitch. The pre-game cycle keeps its own ten-minute clock
                    # per game (PREGAME_ODDS_CADENCE_S), so most polls read
                    # nothing from the vendor. The task upserts the game row
                    # itself before its first odds INSERT (the foreign key).
                    asyncio.create_task(self._persist_pregame_odds(game_pk, game))
                    # SIM-519 Part B: the published lineup and the probable
                    # pitchers, so the game can be simulated before it starts.
                    asyncio.create_task(self._write_preview_lineup(game))

                # SIM-105: skip _upsert_game_record() for games that have
                # already transitioned to Final.  For a 15-game slate with
                # 12 finished games this avoids ~1,080 wasted DB writes per
                # 3-hour afternoon.  Status==Final transitions still fall
                # through (the membership check kicks in next poll).
                if game_pk in self._completed_games and status == "Final":
                    continue

                # Upsert every game (Preview/Live/Final) into raw.games
                asyncio.create_task(self._upsert_game_record(game))
                # SIM-519: a game already Final when first seen (yesterday's,
                # in the three-day window) is upserted once, then skipped.
                if status == "Final":
                    self._completed_games.add(game_pk)

        await self._write_heartbeat(sorted(current_live))

    async def _broadcast_message(self, game_pk: int, payload: dict) -> None:
        """Send a browser-bound message (SIM-519 Part C).

        In the app's process (``LIVE_PIPELINE_ENABLED``) the publisher is the
        in-process ``connection_manager``; in the ``live`` container it is a
        :class:`pipeline.live.broadcast.RedisBroadcaster`, and the app's bridge
        forwards the message to the browsers.
        """
        publisher = getattr(self, "_broadcaster", None) or connection_manager
        await publisher.broadcast(game_pk, payload)

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

    async def on_game_live(self, game_pk: int, first_pitch_at: datetime) -> None:
        """SIM-519 Part C: the Preview-to-Live hook, once per game.

        Records ``raw.games.first_pitch_at`` (Alembic 0029; the first write
        wins, so a restart mid-game keeps the true instant). The closing-row
        promotion (SIM-546) runs just before it in the poll.
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
        """SIM-519 Part C: ``live:heartbeat`` and ``live:watching`` for /ready and the gauge."""
        redis = getattr(self, "_redis", None)
        if redis is None:
            return
        try:
            from pipeline.live.broadcast import write_heartbeat

            await write_heartbeat(redis, live_game_pks)
        except Exception as exc:  # noqa: BLE001
            log.debug("heartbeat not written: %s", exc)

    async def _mark_closing_rows(self, game_pk: int, game: Mapping[str, Any]) -> tuple[int, int]:
        """SIM-546: promote a game's closing rows the first time the poll sees it Live.

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

        SIM-555: the one odds row fetched here goes to the broadcast only. The
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
                # SIM-421 (owner ruling 2026-09-12): every market the book posts
                # goes into the odds table. SIM-555: every book's row of all
                # fifteen game markets, on the prop cadence gate (60 s), never
                # the per-pitch signal. The ``odds`` row above is not persisted.
                await self._persist_game_odds_cycle(game_pk)
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
                # SIM-555: the odds row goes out JSON-safe (no guard-only keys;
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

    def _forget_odds_game(self, game_pk: Any) -> None:
        """Make the odds provider read one game's schedule and event again (SIM-555).

        The BettingPros provider keeps a found game for the process lifetime
        (``forget_game``); a provider without that method keeps nothing, so
        the call is skipped.
        """
        forget = getattr(self._odds_provider(), "forget_game", None)
        if game_pk is not None and callable(forget):
            forget(int(game_pk))

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
        deterministic MockOddsAPI; set ODDS_PROVIDER=bettingpros to use the real
        BettingProsOddsProvider feed without touching this code.

        SIM-555: the refresh loop sends this one row to the browser and no
        longer persists it; :meth:`_persist_game_odds_cycle` stores every
        book's row. With BettingPros the row is the graded book's moneyline.
        """
        return await self._off_loop(self._odds_provider().get_odds, game_pk)

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

    @staticmethod
    def _collect_prop_player_roles(game_state: dict) -> dict[int, str]:
        """
        SIM-421: player_id → role for every player eligible for prop-line
        capture in a built game_state.

        Props are offered on the pitcher on the mound and on the batters in
        both lineups.  We pull:
          * current_pitcher_id → PROP_ROLE_PITCHER
          * every player_id in home_lineup / away_lineup → PROP_ROLE_BATTER,
            read from the entry's boxscore ``position``

        The safe default is PROP_ROLE_BOTH (every market is requested):
          * a lineup entry with no ``position`` (the role is unknown);
          * a lineup entry whose position is a pitcher code — a two-way
            player bats AND pitches;
          * a player seen in two roles (the current pitcher who also bats).

        The dict keeps insertion order, is None-filtered and de-duplicated, so
        a fixed game_state always yields the same ids in the same order (keeps
        the dedup hash and tests stable).
        """
        roles: dict[int, str] = {}
        cp = game_state.get("current_pitcher_id")
        if cp:
            roles[int(cp)] = PROP_ROLE_PITCHER
        for side in ("home_lineup", "away_lineup"):
            for entry in game_state.get(side, []) or []:
                pid = entry.get("player_id")
                if not pid:
                    continue
                pid = int(pid)
                position = str(entry.get("position") or "").upper()
                if not position or position in _PITCHER_POSITION_CODES:
                    role = PROP_ROLE_BOTH
                else:
                    role = PROP_ROLE_BATTER
                if pid in roles and roles[pid] != role:
                    role = PROP_ROLE_BOTH
                roles[pid] = role
        return roles

    @staticmethod
    def _pregame_prop_player_roles(game: Mapping[str, Any]) -> dict[int, str]:
        """
        SIM-555: player_id → role for a game that has not started, read from
        the hydrated schedule entry (see ``SCHEDULE_HYDRATE``).

        A game before first pitch has no live feed, so the roles come from the
        schedule:
          * each side's probable pitcher (``teams.<side>.probablePitcher``)
            → PROP_ROLE_PITCHER;
          * each posted lineup player (``lineups.homePlayers`` /
            ``lineups.awayPlayers``) → PROP_ROLE_BATTER, or PROP_ROLE_BOTH when
            the player's primary position is a pitcher code or is missing.

        A player seen in two roles is PROP_ROLE_BOTH, as in
        :meth:`_collect_prop_player_roles`. Before the lineups are posted the
        map holds the probable pitchers only; it is empty when neither is
        known. The order is fixed (home pitcher, away pitcher, home lineup,
        away lineup), so a fixed entry always gives the same ids.
        """
        roles: dict[int, str] = {}

        def _add(pid: Any, role: str) -> None:
            if not pid:
                return
            pid = int(pid)
            if pid in roles and roles[pid] != role:
                role = PROP_ROLE_BOTH
            roles[pid] = role

        teams = game.get("teams") or {}
        for side in ("home", "away"):
            pitcher = (teams.get(side) or {}).get("probablePitcher") or {}
            _add(pitcher.get("id"), PROP_ROLE_PITCHER)
        lineups = game.get("lineups") or {}
        for key in ("homePlayers", "awayPlayers"):
            for player in lineups.get(key) or []:
                position = str(
                    ((player or {}).get("primaryPosition") or {}).get("abbreviation") or ""
                ).upper()
                role = (
                    PROP_ROLE_BOTH
                    if not position or position in _PITCHER_POSITION_CODES
                    else PROP_ROLE_BATTER
                )
                _add((player or {}).get("id"), role)
        return roles

    @classmethod
    def _collect_prop_player_ids(cls, game_state: dict) -> list[int]:
        """
        SIM-340: Extract the player_ids eligible for prop-line capture from a
        built game_state (the keys of :meth:`_collect_prop_player_roles`).

        Deduplicated, None-filtered, and order-stable so a fixed game_state
        always yields the same id list.
        """
        return list(cls._collect_prop_player_roles(game_state))

    @staticmethod
    def _prop_stats_for_role(role: str, prop_stats: tuple[str, ...]) -> tuple[str, ...]:
        """
        SIM-421: the subset of ``prop_stats`` a player in ``role`` is asked for.

        A pitcher gets the pitcher markets, a hitter the batter markets. Any
        other role (PROP_ROLE_BOTH, or an unknown string) gets every market —
        the safe default, so a mis-labelled player loses no quote.
        """
        if role == PROP_ROLE_PITCHER:
            return tuple(s for s in prop_stats if s in PITCHER_PROP_STATS)
        if role == PROP_ROLE_BATTER:
            return tuple(s for s in prop_stats if s in BATTER_PROP_STATS)
        return prop_stats

    def _prop_offers(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        line_type: str = "current",
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> list[tuple[int, str, list[dict]]]:
        """SIM-555: every book's prop rows, one entry per offer.

        An offer is one (player, prop_stat) at one line type. Each entry is
        ``(player_id, prop_stat, rows)``: the provider's by-book rows through
        ``prop_rows_by_book`` (one row for the mock or a provider with only the
        one-row method), empty rows dropped. An offer with no row is left out.

        SIM-421: ``roles`` (player_id → PROP_ROLE_*) splits the markets by role;
        a player missing from ``roles``, or ``roles=None``, is asked for every
        market in ``prop_stats``. An unknown prop_stat is logged and skipped.
        """
        provider = self._odds_provider()  # SIM-370: env-selected, mock by default
        offers: list[tuple[int, str, list[dict]]] = []
        for player_id in player_ids:
            role = roles.get(player_id, PROP_ROLE_BOTH) if roles else PROP_ROLE_BOTH
            for prop_stat in self._prop_stats_for_role(role, prop_stats):
                try:
                    rows = prop_rows_by_book(
                        provider, game_pk, player_id, prop_stat, line_type=line_type
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
                kept = [row for row in rows if _prop_row_has_odds(row)]
                if kept:
                    offers.append((player_id, prop_stat, kept))
        return offers

    def _fetch_prop_odds(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        line_type: str = "current",
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> list[dict]:
        """
        SIM-340 / SIM-555: a flat list of every book's prop rows for the players.

        SIM-555 (2026-09-28): the provider names the books. The method reads
        every book's row of each (player, prop_stat) through
        ``prop_rows_by_book`` — BettingPros gives one row per book, labelled
        ``'bp:<id>'``; the mock gives its one ``'consensus'`` row. Empty rows
        are dropped; the load guard runs later, in the persisting cycle. The
        old fixed book list (PROP_BOOKS) and its ``books`` argument are gone.

        SIM-421: ``roles`` splits the markets by role (see
        :meth:`_prop_offers`).
        """
        return [
            row
            for _player_id, _prop_stat, rows in self._prop_offers(
                game_pk, player_ids, line_type=line_type, prop_stats=prop_stats, roles=roles
            )
            for row in rows
        ]

    async def _persist_prop_offers(
        self, game_pk: int, offers: list[tuple[int, str, list[dict]]]
    ) -> int:
        """SIM-555: guard and persist each offer's rows in one batch; return the rows sent.

        One ``executemany`` per offer. A failed batch is logged and the next
        offer still runs.
        """
        written = 0
        for player_id, prop_stat, rows in offers:
            kept = self._guard_rows(game_pk, rows)
            if not kept:
                continue
            try:
                written += await self._persist_prop_odds_many(kept)
            except Exception as exc:  # noqa: BLE001 — one offer must not stop the rest
                log.warning(
                    "prop persist failed game %s player %s %s (%d rows): %s",
                    game_pk,
                    player_id,
                    prop_stat,
                    len(kept),
                    exc,
                )
        return written

    def _game_market_offers(
        self, game_pk: int, *, line_type: str = "current"
    ) -> list[tuple[str, list[dict]]]:
        """SIM-555: every book's rows of each ``GAME_MARKET_TYPES`` market, one entry per market.

        Each entry is ``(market_type, rows)``, empty rows dropped; a market with
        no row is left out. An unknown market_type is a programming error and a
        failed fetch is a network error: both are logged, not raised, so one
        bad market cannot stop the cycle.
        """
        provider = self._odds_provider()
        offers: list[tuple[str, list[dict]]] = []
        for market_type in GAME_MARKET_TYPES:
            try:
                rows = odds_rows_by_book(
                    provider, game_pk, line_type=line_type, market_type=market_type
                )
            except ValueError as exc:
                log.warning("skipping game market %r for game %s: %s", market_type, game_pk, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — one market must not stop the rest
                log.warning(
                    "game market %r fetch failed for game %s: %s", market_type, game_pk, exc
                )
                continue
            kept = [row for row in rows if _game_row_has_odds(row)]
            if kept:
                offers.append((market_type, kept))
        return offers

    def _fetch_game_market_odds(self, game_pk: int, *, line_type: str = "current") -> list[dict]:
        """SIM-421 / SIM-555: every book's row of every game market, in market order.

        The rename of the old segment-only fetch: the three full-game markets
        now come through here too (SIM-555), one row per book and market. A
        market the provider cannot resolve (no event, no offer) gives no row,
        so no empty row is ever written.
        """
        return [
            row
            for _market, rows in self._game_market_offers(game_pk, line_type=line_type)
            for row in rows
        ]

    async def _persist_game_odds_cycle(self, game_pk: int) -> int:
        """SIM-421 / SIM-555: persist every book's row of every game market, on the cadence.

        The rename of the old segment cycle. Gated by its own clock
        (``_last_game_odds_fetch``) at ``PROP_FETCH_CADENCE_S``, so the fifteen
        markets are fetched once a minute per game, not on every pitch signal.
        Each market's kept rows go to the database in one batch. Returns the
        rows sent this cycle (0 while the gate is closed).
        """
        now = datetime.now(UTC)
        clock = getattr(self, "_last_game_odds_fetch", None)
        if clock is None:
            clock = {}
            self._last_game_odds_fetch = clock
        last = clock.get(game_pk)
        if last is not None and (now - last).total_seconds() < PROP_FETCH_CADENCE_S:
            return 0
        clock[game_pk] = now
        written = 0
        offers = await self._off_loop(self._game_market_offers, game_pk, line_type="current")
        for market_type, rows in offers:
            kept = self._guard_rows(game_pk, rows)
            if not kept:
                continue
            try:
                written += await self._persist_odds_many(game_pk, kept)
            except Exception as exc:  # noqa: BLE001 — one market must not stop the rest
                log.warning(
                    "game market persist failed game %s %s (%d rows): %s",
                    game_pk,
                    market_type,
                    len(kept),
                    exc,
                )
        if written:
            log.info("SIM-555: persisted %d game-market rows for game %s", written, game_pk)
        return written

    async def _persist_prop_odds_cycle(self, game_pk: int, game_state: dict) -> int:
        """
        SIM-340: Live prop-odds persistence cycle.

        Called from _refresh_game_state() on every WS signal, but self-throttled
        to at most once per PROP_FETCH_CADENCE_S seconds per game (prop lines move
        far more slowly than the per-pitch WS feed).  When the cadence gate opens,
        it:
          1. Collects prop-eligible players + their roles from the game_state
             (SIM-421: pitcher / batter / both).
          2. Fetches every book's rows of each offer via _prop_offers()
             (SIM-555), the pitcher markets for pitchers and the batter markets
             for hitters.
          3. Runs the load guard on each row and persists each offer's kept
             rows in one batch via _persist_prop_odds_many() (SIM-555).

        Returns the number of prop rows sent this cycle (0 when the cadence
        gate is closed or no eligible players were found).
        """
        return await self._persist_prop_roles_cycle(
            game_pk, self._collect_prop_player_roles(game_state)
        )

    async def _persist_prop_roles_cycle(self, game_pk: int, roles: Mapping[int, str]) -> int:
        """SIM-555: the prop cycle for a given role map (player_id → PROP_ROLE_*).

        The body of :meth:`_persist_prop_odds_cycle`, shared with the pre-game
        cycle (:meth:`_persist_pregame_odds`), whose roles come from the
        schedule. The same per-game cadence clock (``_last_prop_fetch``) gates
        both, so a game never pays the prop reads twice in one minute.
        """
        now = datetime.now(UTC)
        clock = getattr(self, "_last_prop_fetch", None)
        if clock is None:
            clock = {}
            self._last_prop_fetch = clock
        last = clock.get(game_pk)
        if last is not None and (now - last).total_seconds() < PROP_FETCH_CADENCE_S:
            # Cadence gate closed — too soon since the last prop fetch.
            return 0

        roles = dict(roles)
        player_ids = list(roles)
        if not player_ids:
            # Nothing to price yet (e.g. lineups not posted) — don't stamp the
            # cadence clock so the next signal retries promptly.
            return 0

        clock[game_pk] = now
        offers = await self._off_loop(
            self._prop_offers, game_pk, player_ids, line_type="current", roles=roles
        )
        written = await self._persist_prop_offers(game_pk, offers)
        if written:
            log.info(
                "SIM-555: persisted %d prop rows from %d offers for game %s (%d players)",
                written,
                len(offers),
                game_pk,
                len(player_ids),
            )
        return written

    async def _persist_pregame_odds(self, game_pk: int, game: Mapping[str, Any]) -> int:
        """SIM-555: store every book's current lines for a game before first pitch.

        ``_sync_live_games`` calls this on every schedule poll (every 30
        seconds) for each game in the ``Preview`` state; ``game`` is the
        game's hydrated schedule entry. The method has its own clock per game
        (``_last_pregame_fetch``): inside PREGAME_ODDS_CADENCE_S (ten minutes)
        of the last pass it returns 0 and calls neither cycle, so a game reads
        the vendor at most once every ten minutes before first pitch. SIM-546:
        inside 15 minutes of the entry's scheduled start the cadence is one
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
        # SIM-546: one minute apart inside 15 minutes of the scheduled start.
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

    async def capture_opening_prop_lines(
        self,
        game_pk: int,
        player_ids: list[int],
        *,
        prop_stats: tuple[str, ...] = PROP_STATS,
        roles: Mapping[int, str] | None = None,
    ) -> int:
        """
        SIM-340 / SIM-138: Opening-line capture hook for player props.

        Writes the raw.prop_odds rows of each (player, prop_stat) with
        line_type='opening'.  This is the prop analogue of the nightly opening
        game-line capture (opening_line_job.py / SIM-138): it records the FIRST
        line posted so opening→closing movement is recoverable for props, not
        just game markets.

        SIM-555: the rows are the provider's by-book opening rows (BettingPros:
        the opener's one row, labelled ``'bp:<id>'``); each passes the load
        guard and each offer's rows go to the database in one batch. The old
        ``books`` argument is gone with PROP_BOOKS.

        SIM-421: pass ``roles`` (player_id → PROP_ROLE_*) to split the markets
        by role; without it every player is asked for every market.

        Idempotent by virtue of the dedup hash (migration 0013): re-running with
        an unchanged opening line is a no-op via ON CONFLICT DO NOTHING.

        Returns the number of opening prop rows sent.
        """
        offers = self._prop_offers(
            game_pk,
            player_ids,
            line_type="opening",
            prop_stats=prop_stats,
            roles=roles,
        )
        written = await self._persist_prop_offers(game_pk, offers)
        if written:
            log.info("SIM-340: captured %d opening prop lines for game %s", written, game_pk)
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

        # SIM-519 Part C: the slate keys games on the league's official date
        # (the local calendar day). gameDate is the UTC start: a West Coast night
        # game's gameDate falls on the next UTC day, so a game created from it
        # sat on the wrong slate until the nightly load corrected it.
        game_date = date.fromisoformat((gd.get("officialDate") or gd["gameDate"])[:10])

        # SIM-438: supply season.  raw.games.season is INTEGER NOT NULL and is
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
            # SIM-086: fall back to None (not 0) when the schedule API omits the
            # venue.  raw.games.venue_id is now nullable; venue_backfill_job.py
            # fills these once the MLB API publishes the venue assignment.
            gd.get("venue", {}).get("id") or None,
            gd.get("teams", {}).get("home", {}).get("team", {}).get("id", 0),
            gd.get("teams", {}).get("away", {}).get("team", {}).get("id", 0),
        )
        await self._upsert_schedule_fields(gd)

    async def _upsert_schedule_fields(self, entry: dict) -> None:
        """SIM-519: the schedule's fields on the game row (Alembic 0029).

        Start time, doubleheader code and number, the detailed state and both
        probable pitchers. Best effort and separate from the INSERT, so a
        database without the 0029 columns keeps every game row it gets today.
        """
        exists = getattr(self, "_schedule_columns_exist", None)
        try:
            if exists is None:
                exists = bool(
                    await self._db.fetchval(
                        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'raw' "
                        "AND table_name = 'games' AND column_name = 'schedule_seen_at'"
                    )
                )
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
        """SIM-519 Part B: make a Preview game simulable.

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
        writer = getattr(self, "_lineup_writer", None)
        if writer is None:
            from pipeline.live.lineup_writer import PublishedLineupWriter

            async def get_json(url: str) -> Any:
                async with self._http.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            writer = PublishedLineupWriter(self._db, get_json)
            self._lineup_writer = writer
        source = await writer.on_preview(game)
        if source is not None:
            await self._on_lineup_published(int(game["gamePk"]), source)

    async def _on_lineup_published(self, game_pk: int, source: str) -> None:
        """SIM-519: tell the game page a lineup is in (the Part C bridge carries it)."""
        broadcast = getattr(self, "_broadcast_message", None)
        if broadcast is None:
            return
        try:
            await broadcast(
                game_pk, {"type": "lineup_published", "game_pk": game_pk, "source": source}
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("lineup_published not broadcast for %s: %s", game_pk, exc)

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
        # SIM-421 (2026-09-12): the tie price of a three-way segment moneyline
        # joins the hash ONLY when it is set. A two-way market's payload is
        # therefore byte-identical to what it was before the column existed,
        # so every hash stored before this change still deduplicates.
        if odds.get("draw_ml") is not None:
            payload += f"|draw_ml={_fmt(odds.get('draw_ml'))}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _odds_params(self, game_pk: int, odds: Mapping[str, Any]) -> tuple[Any, ...]:
        """The bind values of one raw.game_odds INSERT (see :func:`game_odds_insert_args`)."""
        return game_odds_insert_args(game_pk, odds)

    async def _persist_odds(self, game_pk: int, odds: dict) -> None:
        """
        Inserts a fresh odds snapshot into raw.game_odds.

        SIM-133: Writes all four CLV-enabling columns
          (book, line_type, market_type, is_sharp_book).

        SIM-092: Also writes ``odds_hash`` (SHA-256 of the odds payload)
          and uses ON CONFLICT (game_pk, source, odds_hash) DO NOTHING
          so successive identical snapshots are deduplicated at write time.
          Backed by the partial unique index ``idx_game_odds_dedup``.

        SIM-555: also writes ``book_line_at`` (migration 0028). The live cycle
        and the historical loader persist one offer's rows at a time through
        :meth:`_persist_odds_many`; this one-row method stays for single rows.

        The live pipeline always writes line_type='current'.  Opening lines are
        captured by the nightly opening line job (SIM-138).  Closing lines are
        designated by mark_closing_lines(), which the schedule poll calls at
        first pitch (SIM-546).
        """
        await self._db.execute(_GAME_ODDS_INSERT_SQL, *self._odds_params(game_pk, odds))

    async def _persist_odds_many(self, game_pk: int, rows: list[dict]) -> int:
        """SIM-555: insert one offer's game-odds rows in one ``executemany``.

        The same SQL and the same dedup as :meth:`_persist_odds` (the
        ``odds_hash`` ``ON CONFLICT DO NOTHING``), one database round trip for
        the offer's books instead of one per book. Returns the number of rows
        sent (the dedup may insert fewer). An empty list sends nothing.
        """
        return await insert_game_odds_rows(self._db, game_pk, rows)

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
        prop : dict returned by the configured provider's get_prop_odds()
               (MockOddsAPI by default, or BettingProsOddsProvider under
               ODDS_PROVIDER=bettingpros).  Required keys:
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

        SIM-555: also writes ``book_line_at`` (migration 0028). The cycles
        persist one offer's rows at a time through
        :meth:`_persist_prop_odds_many`.

        The DB enforces prop_stat values via CHECK constraint (migration 0004).
        Any unknown prop_stat will fail here with a clear IntegrityError before
        the invalid value reaches the application layer.
        """
        await self._db.execute(_PROP_ODDS_INSERT_SQL, *self._prop_odds_params(prop))

    def _prop_odds_params(self, prop: Mapping[str, Any]) -> tuple[Any, ...]:
        """The bind values of one raw.prop_odds INSERT (see :func:`prop_odds_insert_args`)."""
        return prop_odds_insert_args(prop)

    async def _persist_prop_odds_many(self, rows: list[dict]) -> int:
        """SIM-555: insert one offer's prop rows in one ``executemany``.

        The same SQL and dedup as :meth:`_persist_prop_odds`. Returns the number
        of rows sent (the dedup may insert fewer). An empty list sends nothing.
        """
        return await insert_prop_odds_rows(self._db, rows)

    async def mark_closing_lines(
        self,
        game_pk: int,
        first_pitch_at: datetime,
        *,
        scheduled_start: datetime | None = None,
    ) -> int:
        """SIM-133 / SIM-546: designate a game's closing rows in raw.game_odds.

        SIM-546 rewrote the rule: one closing row per (market, book), not one
        per game. Per key, the latest row fetched at or before
        ``first_pitch_at`` among the 'current' and 'closing' rows is promoted
        when it is 'current' and the load guard keeps its stamp against
        ``scheduled_start`` (``None`` = no stamp check). The promoted row's
        ``odds_hash`` is rewritten to its closing hash, so the nightly loader's
        identical row deduplicates against it. A second call promotes nothing.
        See :func:`promote_closing_game_rows`.

        The schedule poll calls this the first time it sees a game Live
        (:meth:`_mark_closing_rows`). Returns the rows promoted.
        """
        updated = await promote_closing_game_rows(
            self._db, game_pk, first_pitch_at, scheduled_start=scheduled_start
        )
        if updated:
            log.info(
                "Closing lines designated: game %s at %s (%d rows promoted)",
                game_pk,
                first_pitch_at.isoformat(),
                updated,
            )
        return updated

    async def mark_closing_prop_lines(
        self,
        game_pk: int,
        first_pitch_at: datetime,
        *,
        scheduled_start: datetime | None = None,
    ) -> int:
        """SIM-340 / SIM-546: the prop analogue of :meth:`mark_closing_lines`.

        One closing row per (player, prop stat, book), by the same rule, with
        the prop hash rewritten. See :func:`promote_closing_prop_rows`.
        Returns the prop rows promoted.
        """
        updated = await promote_closing_prop_rows(
            self._db, game_pk, first_pitch_at, scheduled_start=scheduled_start
        )
        if updated:
            log.info(
                "Closing prop lines designated: game %s at %s (%d rows promoted)",
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
    # SIM-519 Part C: in the app, the live service's messages arrive on Redis;
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
    """SIM-104 + SIM-153: rate-limited resimulate endpoint + env-var DSN.

    SIM-519 Part C: the ``live`` container runs this app (``python -m
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
