"""
tests/unit/test_api_ws_schemas_sim385.py — SIM-385

Unit tests for the typed WebSocket event schemas in api/websocket/schemas.py.

Coverage:
  - WsEventType enum completeness
  - GameStateUpdateEvent round-trip (serialise → deserialise)
  - ResimPendingEvent round-trip
  - PingEvent / PongEvent wire format
  - LiveGameState extra fields are forwarded (ConfigDict extra="allow")
  - LiveOdds optional fields default to None
  - GameStateUpdateEvent.resim_triggered default is False
  - model_validate on a raw broadcast_payload dict (mirrors the pipeline call-site)
"""

from __future__ import annotations

import json

from api.websocket.schemas import (
    GameStateUpdateEvent,
    LiveGameState,
    LiveOdds,
    PingEvent,
    PongEvent,
    ResimPendingEvent,
    WsEventType,
)

# ---------------------------------------------------------------------------
# WsEventType
# ---------------------------------------------------------------------------


class TestWsEventType:
    def test_has_all_four_variants(self):
        variants = {e.value for e in WsEventType}
        assert variants == {"game_state_update", "resim_pending", "ping", "pong"}

    def test_string_coercion(self):
        assert WsEventType("game_state_update") is WsEventType.game_state_update


# ---------------------------------------------------------------------------
# GameStateUpdateEvent
# ---------------------------------------------------------------------------


class TestGameStateUpdateEvent:
    def _make_raw(self, **overrides) -> dict:
        base = {
            "type": "game_state_update",
            "game_pk": 745528,
            "game_state": {
                "inning": 5,
                "half": "Top",
                "outs": 2,
                "balls": 3,
                "strikes": 1,
                "home_score": 4,
                "away_score": 2,
                "game_status": "In Progress",
            },
            "odds": {
                "home_ml": -150,
                "away_ml": 130,
                "total_line": 9.0,
            },
            "resim_triggered": False,
        }
        base.update(overrides)
        return base

    def test_model_validate_from_raw_dict(self):
        raw = self._make_raw()
        evt = GameStateUpdateEvent.model_validate(raw)
        assert evt.type is WsEventType.game_state_update
        assert evt.game_pk == 745528
        assert evt.game_state.inning == 5
        assert evt.game_state.half == "Top"
        assert evt.odds.home_ml == -150
        assert evt.resim_triggered is False

    def test_round_trip_json(self):
        raw = self._make_raw(resim_triggered=True)
        evt = GameStateUpdateEvent.model_validate(raw)
        serialised = evt.model_dump()
        assert serialised["type"] == "game_state_update"
        assert serialised["resim_triggered"] is True
        assert serialised["game_state"]["inning"] == 5

    def test_resim_triggered_defaults_to_false(self):
        raw = self._make_raw()
        raw.pop("resim_triggered", None)
        evt = GameStateUpdateEvent.model_validate(raw)
        assert evt.resim_triggered is False

    def test_odds_defaults_to_empty_live_odds(self):
        raw = self._make_raw()
        raw.pop("odds", None)
        evt = GameStateUpdateEvent.model_validate(raw)
        assert isinstance(evt.odds, LiveOdds)
        assert evt.odds.home_ml is None

    def test_extra_fields_on_game_state_are_forwarded(self):
        """Pipeline may add future fields; extra='allow' must pass them through."""
        raw = self._make_raw()
        raw["game_state"]["future_field"] = "some_value"
        evt = GameStateUpdateEvent.model_validate(raw)
        # Extra field preserved on the model
        assert evt.game_state.model_extra.get("future_field") == "some_value"

    def test_wire_format_matches_pipeline_broadcast_payload(self):
        """Matches the exact dict LiveIngestionPipeline builds at line 1370."""
        broadcast_payload = {
            "type": "game_state_update",
            "game_pk": 123456,
            "game_state": {"inning": 1, "half": "Bottom", "outs": 0},
            "odds": {},
            "resim_triggered": True,
        }
        evt = GameStateUpdateEvent.model_validate(broadcast_payload)
        assert evt.game_pk == 123456
        assert evt.resim_triggered is True
        # Serialise as the WS layer would
        wire = json.dumps(evt.model_dump())
        decoded = json.loads(wire)
        assert decoded["type"] == "game_state_update"


# ---------------------------------------------------------------------------
# ResimPendingEvent
# ---------------------------------------------------------------------------


class TestResimPendingEvent:
    def test_model_validate_auto_trigger(self):
        raw = {
            "type": "resim_pending",
            "game_pk": 745528,
            "trigger": "auto",
            "inning": 7,
            "half": "Top",
            "balls": 0,
            "strikes": 0,
            "outs": 3,
        }
        evt = ResimPendingEvent.model_validate(raw)
        assert evt.type is WsEventType.resim_pending
        assert evt.trigger == "auto"
        assert evt.inning == 7
        assert evt.outs == 3

    def test_trigger_defaults_to_auto(self):
        raw = {"type": "resim_pending", "game_pk": 1}
        evt = ResimPendingEvent.model_validate(raw)
        assert evt.trigger == "auto"

    def test_manual_trigger(self):
        """Matches the pipeline broadcast at live_ingestion_pipeline.py:2161."""
        raw = {
            "type": "resim_pending",
            "game_pk": 745528,
            "trigger": "manual",
            "inning": 5,
            "half": "Top",
            "balls": 1,
            "strikes": 2,
            "outs": 1,
        }
        evt = ResimPendingEvent.model_validate(raw)
        assert evt.trigger == "manual"
        assert evt.game_pk == 745528

    def test_round_trip_json(self):
        raw = {
            "type": "resim_pending",
            "game_pk": 999,
            "trigger": "auto",
            "inning": 9,
            "half": "Bottom",
        }
        evt = ResimPendingEvent.model_validate(raw)
        d = evt.model_dump()
        assert d["type"] == "resim_pending"
        assert d["game_pk"] == 999


# ---------------------------------------------------------------------------
# PingEvent / PongEvent
# ---------------------------------------------------------------------------


class TestKeepAliveEvents:
    def test_ping_wire_format(self):
        evt = PingEvent()
        assert evt.model_dump() == {"type": WsEventType.ping}

    def test_pong_wire_format(self):
        evt = PongEvent()
        assert evt.model_dump() == {"type": WsEventType.pong}

    def test_ping_json_serialises_to_string_value(self):
        wire = json.dumps(PingEvent().model_dump())
        assert json.loads(wire) == {"type": "ping"}

    def test_pong_json_serialises_to_string_value(self):
        wire = json.dumps(PongEvent().model_dump())
        assert json.loads(wire) == {"type": "pong"}


# ---------------------------------------------------------------------------
# LiveGameState
# ---------------------------------------------------------------------------


class TestLiveGameState:
    def test_all_fields_optional_empty_dict(self):
        gs = LiveGameState.model_validate({})
        assert gs.inning is None
        assert gs.home_score is None
        assert gs.play_history == []

    def test_full_known_fields(self):
        gs = LiveGameState.model_validate(
            {
                "inning": 3,
                "half": "Bottom",
                "outs": 1,
                "balls": 2,
                "strikes": 2,
                "home_score": 1,
                "away_score": 0,
                "game_status": "In Progress",
                "home_team_id": 119,
                "away_team_id": 147,
                "current_pitcher_id": 543037,
                "current_batter_id": 665742,
            }
        )
        assert gs.inning == 3
        assert gs.game_status == "In Progress"
        assert gs.current_pitcher_id == 543037

    def test_extra_fields_preserved(self):
        gs = LiveGameState.model_validate({"inning": 1, "new_field": "xyz"})
        assert gs.model_extra.get("new_field") == "xyz"


# ---------------------------------------------------------------------------
# LiveOdds
# ---------------------------------------------------------------------------


class TestLiveOdds:
    def test_empty_dict_all_none(self):
        odds = LiveOdds.model_validate({})
        assert odds.home_ml is None
        assert odds.away_ml is None
        assert odds.total_line is None

    def test_partial_fields(self):
        odds = LiveOdds.model_validate({"home_ml": -115, "total_line": 8.5})
        assert odds.home_ml == -115
        assert odds.total_line == 8.5
        assert odds.away_ml is None

    def test_extra_fields_preserved(self):
        odds = LiveOdds.model_validate({"home_ml": -110, "book": "Pinnacle"})
        assert odds.model_extra.get("book") == "Pinnacle"
