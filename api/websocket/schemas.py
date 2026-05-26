"""
api/websocket/schemas.py — SIM-385
===================================
Typed Pydantic v2 models for every WebSocket event the server broadcasts
to frontend clients on /ws/games/{game_pk}.

Event flow
----------
Server → Client (broadcast from LiveIngestionPipeline._broadcast_to_clients):
  GameStateUpdateEvent   — emitted after every live-game state refresh
  ResimPendingEvent      — emitted when a plate-appearance ends and a re-sim is queued
  PingEvent              — server keep-alive (if no message received in 30s)

Server → Client (unicast to connecting client):
  PongEvent              — response to a client "ping" text frame

Client → Server (text frames, no model needed):
  "ping"                 — client keep-alive probe; server responds with PongEvent

Usage
-----
Import from here on both sides:

  from api.websocket.schemas import (
      GameStateUpdateEvent,
      ResimPendingEvent,
      WsEventType,
  )

  # Deserialise an incoming raw dict
  evt = GameStateUpdateEvent.model_validate(raw_payload)

  # Serialise for broadcast
  payload = evt.model_dump()
  await websocket.send_text(json.dumps(payload))

On the TypeScript side, the discriminated union is driven by the ``type`` field.
See frontend/src/api/ws.ts for the client-side type definitions that mirror this.

Notes
-----
- All models use ``model_config = ConfigDict(extra="allow")`` so that future
  fields added to the live payload are forwarded to the client without a
  server-side schema change causing a validation error.
- ``game_state`` is a structured but open-ended dict built by
  ``pipeline.live.live_ingestion_pipeline.GameStateBuilder.build()``.  The inner
  model ``LiveGameState`` documents the known fields; ``extra="allow"`` passes
  through any additional fields from the live feed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Event type discriminator
# ---------------------------------------------------------------------------


class WsEventType(StrEnum):
    """Discriminant for the WebSocket event union."""

    game_state_update = "game_state_update"
    resim_pending = "resim_pending"
    ping = "ping"
    pong = "pong"


# ---------------------------------------------------------------------------
# Inner model: live game state
# ---------------------------------------------------------------------------


class LiveGameState(BaseModel):
    """
    Structured view of the game-state dict built by
    ``GameStateBuilder.build()`` in the live-ingestion pipeline.

    All fields are Optional because the live-feed fields arrive incrementally
    and may be absent at the very start of a game. ``extra="allow"`` forwards
    any additional fields the pipeline adds later without a schema change.
    """

    model_config = ConfigDict(extra="allow")

    # Count / situation
    inning: int | None = None
    half: str | None = None  # "Top" | "Bottom"
    outs: int | None = None
    balls: int | None = None
    strikes: int | None = None

    # Score
    home_score: int | None = None
    away_score: int | None = None

    # Teams
    home_team_id: int | None = None
    away_team_id: int | None = None
    batting_team_id: int | None = None

    # Current participants
    current_pitcher_id: int | None = None
    current_batter_id: int | None = None

    # Runners (base-state bitmask or nullable IDs: 1b, 2b, 3b)
    runner_on_first: int | None = None
    runner_on_second: int | None = None
    runner_on_third: int | None = None

    # Game status (e.g. "In Progress", "Final", "Scheduled")
    game_status: str | None = None

    # Play history — list of plate-appearance summaries (grows during the game)
    play_history: list[dict[str, Any]] = Field(default_factory=list)

    # Lineups — ordered lists of {player_id, position, batting_order, ...}
    home_lineup: list[dict[str, Any]] = Field(default_factory=list)
    away_lineup: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Inner model: live odds snapshot
# ---------------------------------------------------------------------------


class LiveOdds(BaseModel):
    """
    Odds snapshot included in GameStateUpdateEvent.

    Sourced from ``pipeline.odds_provider`` (real or mock).  All fields are
    Optional — the odds pipeline may be disabled in dev mode.
    """

    model_config = ConfigDict(extra="allow")

    # Moneyline
    home_ml: int | None = None
    away_ml: int | None = None

    # Run line (spread)
    home_rl_line: float | None = None
    home_rl_price: int | None = None
    away_rl_line: float | None = None
    away_rl_price: int | None = None

    # Total (over/under)
    total_line: float | None = None
    over_price: int | None = None
    under_price: int | None = None


# ---------------------------------------------------------------------------
# GameStateUpdateEvent
# ---------------------------------------------------------------------------


class GameStateUpdateEvent(BaseModel):
    """
    Broadcast after every live-game state refresh (end of each live feed tick
    that contains meaningful state). This is the highest-frequency event on
    the wire; a typical in-progress game emits one every 30–120 seconds.

    ``resim_triggered=True`` means the plate appearance just ended and a new
    Monte-Carlo simulation is being queued. The frontend should show a
    "re-simulating…" loading indicator until the next simulation result arrives
    via the REST API or the sim result is pushed separately.

    Wire format::

        {
          "type": "game_state_update",
          "game_pk": 745528,
          "game_state": { "inning": 5, "half": "Top", "outs": 2, ... },
          "odds": { "home_ml": -150, "away_ml": +130, ... },
          "resim_triggered": false
        }
    """

    model_config = ConfigDict(extra="allow")

    type: WsEventType = WsEventType.game_state_update
    game_pk: int
    game_state: LiveGameState
    odds: LiveOdds = Field(default_factory=LiveOdds)
    resim_triggered: bool = False


# ---------------------------------------------------------------------------
# ResimPendingEvent
# ---------------------------------------------------------------------------


class ResimPendingEvent(BaseModel):
    """
    Broadcast when a plate-appearance ends and a Monte-Carlo re-simulation
    has been queued by the pipeline. This event is a *signal* only — the
    simulation result itself is delivered via the REST /simulate endpoint
    (the frontend polls or receives a push notification through a separate
    channel once the 100-game batch completes).

    ``trigger`` is a freeform string identifying what caused the re-sim
    (e.g. "auto" from the pipeline, "manual" from the /resim REST endpoint).

    Wire format::

        {
          "type": "resim_pending",
          "game_pk": 745528,
          "trigger": "auto",
          "inning": 5,
          "half": "Top",
          "balls": 0,
          "strikes": 0,
          "outs": 3
        }
    """

    model_config = ConfigDict(extra="allow")

    type: WsEventType = WsEventType.resim_pending
    game_pk: int
    trigger: str = "auto"  # "auto" | "manual"
    inning: int | None = None
    half: str | None = None
    balls: int | None = None
    strikes: int | None = None
    outs: int | None = None


# ---------------------------------------------------------------------------
# Keep-alive events
# ---------------------------------------------------------------------------


class PingEvent(BaseModel):
    """
    Server-initiated keep-alive probe.
    Sent if no inbound data from the client in 30 seconds.
    The client should respond with the text frame "ping" (which the server
    echoes back as a PongEvent).

    Wire format::

        { "type": "ping" }
    """

    type: WsEventType = WsEventType.ping


class PongEvent(BaseModel):
    """
    Server response to a client "ping" text frame.

    Wire format::

        { "type": "pong" }
    """

    type: WsEventType = WsEventType.pong


# ---------------------------------------------------------------------------
# Discriminated union — exhaustive type for use with isinstance() or
# Annotated[..., Discriminator("type")] in consuming code.
# ---------------------------------------------------------------------------

WsEvent = GameStateUpdateEvent | ResimPendingEvent | PingEvent | PongEvent

__all__ = [
    "GameStateUpdateEvent",
    "LiveGameState",
    "LiveOdds",
    "PingEvent",
    "PongEvent",
    "ResimPendingEvent",
    "WsEvent",
    "WsEventType",
]
