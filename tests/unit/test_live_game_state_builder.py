"""The live service's game-state builder: the pitcher role it infers from a box
line, and the play history it keeps across refreshes.

``GameStateBuilder._infer_role`` places an opener (under 4.0 innings with at
least 9 batters faced) between a starter and a multi-inning reliever. The
builder keeps its parsed play history between refreshes and appends only the
new plate appearances; the pipeline keeps one builder per game.
"""

from __future__ import annotations


def _make_play(idx: int, batter_id: int, event: str | None) -> dict:
    """A minimal ``allPlays`` entry with the keys the parser reads."""
    return {
        "about": {
            "atBatIndex": idx,
            "inning": 1,
            "halfInning": "top",
            "isComplete": event is not None,
        },
        "result": {"eventType": event, "description": event or "in progress", "rbi": 0},
        "matchup": {
            "batter": {"id": batter_id, "fullName": f"Batter{batter_id}"},
            "pitcher": {"id": 999, "fullName": "Pitcher999"},
        },
        "playEvents": [],
    }


class TestOpenerRole:
    """The opener slot sits between SP and MRP."""

    def _infer(self, ip: str | float, bf: int) -> str:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        return GameStateBuilder._infer_role(
            {"inningsPitched": str(ip), "battersFaced": bf},
            {},
        )

    def test_starter_remains_sp(self) -> None:
        assert self._infer("5.1", 18) == "SP"

    def test_opener_with_two_innings_nine_bf(self) -> None:
        # 2.0 IP + 9 BF — classic opener pattern
        assert self._infer("2.0", 9) == "Opener"

    def test_short_outing_high_bf_is_opener(self) -> None:
        # 0.2 IP + 10 BF (lots of runners but quick hook) → Opener
        assert self._infer("0.2", 10) == "Opener"

    def test_two_inning_with_six_bf_is_mrp_not_opener(self) -> None:
        # 2.0 IP + 6 BF — multi-inning relief, not an opener
        assert self._infer("2.0", 6) == "MRP"

    def test_one_inning_relief_is_rp(self) -> None:
        assert self._infer("0.2", 3) == "RP"

    def test_missing_battersfaced_falls_back_safely(self) -> None:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        # No battersFaced key: no crash, the innings-only rule decides.
        role = GameStateBuilder._infer_role({"inningsPitched": "2.0"}, {})
        assert role in ("MRP", "RP", "SP")  # graceful, no Opener miss


class TestPlayHistoryCache:
    """The builder caches its play history across refreshes."""

    def test_builder_holds_history_state(self) -> None:
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        b = GameStateBuilder.__new__(GameStateBuilder)
        b._history = []
        b._last_at_bat_index = -1
        b._game_date = None

        # First call with 2 plays
        plays_first = [
            _make_play(idx=0, batter_id=100, event="strikeout"),
            _make_play(idx=1, batter_id=101, event="walk"),
        ]
        h1 = b._parse_play_history(plays_first)
        assert len(h1) == 2
        assert b._last_at_bat_index == 1

        # Second call with all the prior plays + 1 new one — incremental:
        # only the new play should append; existing entries unchanged.
        plays_second = plays_first + [
            _make_play(idx=2, batter_id=102, event="single"),
        ]
        h2 = b._parse_play_history(plays_second)
        assert len(h2) == 3
        assert b._last_at_bat_index == 2

    def test_builder_replaces_in_flight_at_bat(self) -> None:
        """Mid-PA refresh: same atBatIndex with updated event must overwrite
        the last cached entry, not append a duplicate."""
        from pipeline.live.live_ingestion_pipeline import GameStateBuilder

        b = GameStateBuilder.__new__(GameStateBuilder)
        b._history = []
        b._last_at_bat_index = -1
        b._game_date = None

        in_flight = _make_play(idx=0, batter_id=100, event=None)  # PA in-flight
        b._parse_play_history([in_flight])
        assert b._history[-1]["event"] is None

        completed = _make_play(idx=0, batter_id=100, event="single")
        h = b._parse_play_history([completed])

        assert len(h) == 1  # NOT duplicated
        assert h[-1]["event"] == "single"
        assert b._last_at_bat_index == 0

    def test_pipeline_caches_builders_per_game(self) -> None:
        """Two refreshes for the same game_pk return the SAME builder."""
        from pipeline.live.live_ingestion_pipeline import (
            GameStateBuilder,
            LiveIngestionPipeline,
        )

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = None
        pipeline._builders = {}

        b1 = pipeline._get_or_create_builder(745001)
        b2 = pipeline._get_or_create_builder(745001)
        assert b1 is b2
        assert isinstance(b1, GameStateBuilder)

        # Different game = different builder
        b3 = pipeline._get_or_create_builder(745002)
        assert b3 is not b1
