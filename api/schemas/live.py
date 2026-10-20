"""
api/schemas/live.py
===================
The live service's stored game state (``GET /api/games/{game_pk}/live``).
"""

from __future__ import annotations

from pydantic import Field

from api.schemas._base import _ApiModel


class LiveGameStateResponse(_ApiModel):
    """SIM-386 response: live in-progress game state from ``sim.lineup_state``.

    Fields are extracted from the ``game_state`` JSONB blob written by the live
    ingestion pipeline (:class:`~pipeline.live.live_ingestion_pipeline.GameStateBuilder`).
    All fields default to neutral / empty values so a partially-built game_state
    (e.g. a game that just went Live with no pitches yet) never breaks
    deserialization -- the frontend handles sparse state gracefully.

    ``updated_at`` (ISO-8601 string) is the pipeline's last-write timestamp; the
    frontend can use it to detect staleness (> ~30s without an update may mean
    the pipeline lost the WS signal and is polling-fallback).
    """

    game_pk: int
    session_id: str  # UUID of the sim.lineup_state row
    # The inning
    inning: int = 1
    half: str = "Top"  # "Top" | "Bottom"
    outs: int = 0
    balls: int = 0
    strikes: int = 0
    # The score
    home_score: int = 0
    away_score: int = 0
    # The teams
    batting_team_id: int | None = None
    fielding_team_id: int | None = None
    # The runners
    on_1b: int | None = None  # player_id of runner on first, or None
    on_2b: int | None = None
    on_3b: int | None = None
    # The batter and the pitcher
    current_batter_id: int | None = None
    current_pitcher_id: int | None = None
    # The lineups and the rosters
    home_lineup: list[int] = Field(default_factory=list)
    away_lineup: list[int] = Field(default_factory=list)
    home_bullpen: list[int] = Field(default_factory=list)
    away_bullpen: list[int] = Field(default_factory=list)
    home_bench: list[int] = Field(default_factory=list)
    away_bench: list[int] = Field(default_factory=list)
    # The pipeline's last write
    updated_at: str | None = None  # ISO-8601 timestamp of last pipeline write
