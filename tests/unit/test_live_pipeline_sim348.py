"""
test_live_pipeline_sim348.py
============================
SIM-348 — Live-ingestion-pipeline coverage.

Before this ticket, ``pipeline/live/live_ingestion_pipeline.py`` (~2,160 lines)
was ``omit``-ed from coverage and sat at ~0 %.  This module gives the testable
units real coverage using the established async + mock idiom from
tests/unit/test_live_pipeline_bugs.py and tests/unit/test_data_engineer_sim340.py
(pytest-asyncio with ``asyncio_mode=auto``; AsyncMock for the asyncpg pool /
Redis / aiohttp; no live DB / WS / server).

Units covered:
  * GameStateBuilder.build() — full parse of a feed/live payload into game_state
  * GameStateBuilder incremental play-history caching (SIM-101)
  * GameStateBuilder._infer_role role inference (SIM-102: SP/Opener/MRP/RP)
  * GameStateBuilder._parse_linescore + _parse_roster (lineup/bullpen/bench split)
  * ConnectionManager — connect/disconnect/broadcast snapshot-safety + dead-conn
    cleanup (SIM-103)
  * LiveIngestionPipeline._refresh_game_state — the WS-signal → REST-refetch →
    build → persist → broadcast → resim flow (orchestration)
  * _persist_odds / _persist_prop_odds — INSERT + dedup-hash + ON CONFLICT
  * mark_closing_lines / mark_closing_prop_lines — closing-line designation
  * _collect_prop_player_ids + _persist_prop_odds_cycle cadence gate (SIM-340)
  * _should_resimulate + _signal_resimulation callback dispatch
  * create_app manual /resimulate endpoint cooldown/429 (SIM-104)
  * __init__ runtime guards (SIM-106 sync-callback, SIM-153 missing DSN/Redis)

Owned by QA / DevOps (Agent 9) + Data Engineer (Agent 4).

Run:
    pytest tests/unit/test_live_pipeline_sim348.py -v
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    PROP_BOOKS,
    PROP_STATS,
    RESIM_COOLDOWN_S,
    ConnectionManager,
    GameStateBuilder,
    LiveIngestionPipeline,
    MockOddsAPI,
    create_app,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _bare_pipeline(**attrs) -> LiveIngestionPipeline:
    """Build a LiveIngestionPipeline without running __init__ (no real pool).

    Sets the handful of instance attributes the methods under test read so a
    test can drive a single method in isolation.  Override / add via **attrs.
    """
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = None
    p._redis = None
    p._http = None
    p._sim_cb = None
    p._ws_clients = {}
    p._refresh_locks = {}
    p._last_resim_at_bat = {}
    p._completed_games = set()
    p._builders = {}
    p._last_prop_fetch = {}
    for k, v in attrs.items():
        setattr(p, k, v)
    return p


def _make_async_response(status: int = 200, payload: dict | None = None) -> AsyncMock:
    """An async-context-manager aiohttp response stand-in."""
    resp = AsyncMock()
    resp.status = status
    resp.json.return_value = payload if payload is not None else {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_player(pid: int, name: str, *, batting_order=None, position="", stats=None):
    """Construct a boxscore player entry."""
    entry: dict = {
        "person": {"id": pid, "fullName": name},
        "position": {"abbreviation": position},
        "stats": stats or {},
    }
    if batting_order is not None:
        entry["battingOrder"] = batting_order
    return entry


# ===========================================================================
# GameStateBuilder — build() full parse
# ===========================================================================


class TestGameStateBuilderBuild:
    @pytest.mark.asyncio
    async def test_build_parses_core_state(self, mock_db_pool, sample_feed) -> None:
        builder = GameStateBuilder(mock_db_pool)
        state = await builder.build(sample_feed)

        assert state["game_pk"] == sample_feed["gamePk"]
        assert state["game_status"] == "Live"
        assert state["inning"] == 3
        assert state["half"] == "Top"
        assert state["outs"] == 2
        assert state["balls"] == 2
        assert state["strikes"] == 1
        assert state["home_score"] == 3
        assert state["away_score"] == 1

    @pytest.mark.asyncio
    async def test_build_resolves_batting_and_fielding_team(
        self, mock_db_pool, sample_feed
    ) -> None:
        # half == "Top" → away bats, home fields.
        builder = GameStateBuilder(mock_db_pool)
        state = await builder.build(sample_feed)
        assert state["batting_team_id"] == 111  # away
        assert state["fielding_team_id"] == 147  # home

    @pytest.mark.asyncio
    async def test_build_extracts_baserunners_and_participants(
        self, mock_db_pool, sample_feed
    ) -> None:
        builder = GameStateBuilder(mock_db_pool)
        state = await builder.build(sample_feed)
        assert state["on_1b"] == 301
        assert state["on_2b"] is None
        assert state["on_3b"] is None
        assert state["current_batter_id"] == 101
        # pitcher resolved from currentPlay.matchup.pitcher
        assert state["current_pitcher_id"] == 999

    @pytest.mark.asyncio
    async def test_build_includes_play_history_and_linescore(
        self, mock_db_pool, sample_feed
    ) -> None:
        builder = GameStateBuilder(mock_db_pool)
        state = await builder.build(sample_feed)
        assert len(state["play_history"]) == 1
        entry = state["play_history"][0]
        # atBatIndex 41 → at_bat_number 42 (1-indexed).
        assert entry["at_bat_number"] == 42
        assert entry["event"] == "single"
        assert entry["rbi"] == 1
        assert len(entry["pitches"]) == 1
        assert entry["pitches"][0]["pitch_type"] == "FF"
        assert state["inning_scores"]["home_runs"] == 3
        assert state["inning_scores"]["away_hits"] == 4

    @pytest.mark.asyncio
    async def test_build_pitcher_falls_back_to_linescore_defense(
        self, mock_db_pool, sample_feed
    ) -> None:
        # Drop the currentPlay pitcher so resolution falls through to
        # linescore.defense.pitcher.
        sample_feed["liveData"]["plays"]["currentPlay"]["matchup"].pop("pitcher")
        builder = GameStateBuilder(mock_db_pool)
        state = await builder.build(sample_feed)
        assert state["current_pitcher_id"] == 999  # from linescore.defense

    @pytest.mark.asyncio
    async def test_build_handles_missing_game_date_gracefully(
        self, mock_db_pool, sample_feed
    ) -> None:
        sample_feed["gameData"]["datetime"]["officialDate"] = "not-a-date"
        builder = GameStateBuilder(mock_db_pool)
        # Must not raise — bad date degrades to None.
        state = await builder.build(sample_feed)
        assert state["game_pk"] == sample_feed["gamePk"]


# ===========================================================================
# GameStateBuilder — _parse_roster lineup / bullpen / bench split
# ===========================================================================


class TestGameStateBuilderRoster:
    @pytest.mark.asyncio
    async def test_parse_roster_splits_lineup_bullpen_bench(self, mock_db_pool) -> None:
        boxscore = {
            "teams": {
                "home": {
                    "players": {
                        "ID1": _make_player(
                            1,
                            "Lead Off",
                            batting_order="100",
                            position="CF",
                            stats={"batting": {"summary": "1-2"}},
                        ),
                        "ID2": _make_player(
                            2,
                            "Cleanup",
                            batting_order="400",
                            position="1B",
                            stats={"batting": {"summary": "0-1"}},
                        ),
                        "ID3": _make_player(
                            3,
                            "Reliever",
                            position="P",
                            stats={"pitching": {"pitchesThrown": 0, "inningsPitched": "0.0"}},
                        ),
                        "ID4": _make_player(
                            4,
                            "Bench Bat",
                            position="SS",
                            stats={"batting": {"summary": ""}},
                        ),
                    },
                    "battingOrder": [1, 2],
                }
            }
        }
        builder = GameStateBuilder(mock_db_pool)
        lineup, bullpen, bench = await builder._parse_roster(
            boxscore, game_pk=745000, side="home", team_id=147
        )
        assert [p["player_id"] for p in lineup] == [1, 2]  # sorted by batting_order
        assert lineup[0]["batting_order"] == 1
        assert lineup[1]["batting_order"] == 4
        assert [p["player_id"] for p in bullpen] == [3]
        assert [p["player_id"] for p in bench] == [4]

    @pytest.mark.asyncio
    async def test_parse_roster_fresh_arm_is_available(self, mock_db_pool) -> None:
        boxscore = {
            "teams": {
                "home": {
                    "players": {
                        "ID3": _make_player(
                            3,
                            "Fresh Arm",
                            position="P",
                            stats={"pitching": {"pitchesThrown": 0, "inningsPitched": "0.0"}},
                        ),
                    },
                    "battingOrder": [],
                }
            }
        }
        builder = GameStateBuilder(mock_db_pool)
        _, bullpen, _ = await builder._parse_roster(
            boxscore, game_pk=745000, side="home", team_id=147
        )
        assert bullpen[0]["available"] is True


# ===========================================================================
# GameStateBuilder — incremental play-history caching (SIM-101)
# ===========================================================================


class TestPlayHistoryIncrementalCache:
    def _play(self, idx: int, event: str = "single") -> dict:
        return {
            "about": {"atBatIndex": idx, "inning": 1, "halfInning": "top"},
            "matchup": {
                "batter": {"id": 1, "fullName": "B"},
                "pitcher": {"id": 2, "fullName": "P"},
            },
            "result": {"eventType": event, "description": event, "rbi": 0},
            "playEvents": [],
        }

    def test_incremental_appends_only_new_plays(self, mock_db_pool) -> None:
        builder = GameStateBuilder(mock_db_pool)
        first = builder._parse_play_history([self._play(0), self._play(1)])
        assert len(first) == 2
        assert builder._last_at_bat_index == 1

        # Same two plays + one new play → only the new one is appended.
        second = builder._parse_play_history([self._play(0), self._play(1), self._play(2)])
        assert len(second) == 3
        assert builder._last_at_bat_index == 2

    def test_in_flight_play_is_replaced_not_duplicated(self, mock_db_pool) -> None:
        builder = GameStateBuilder(mock_db_pool)
        builder._parse_play_history([self._play(0, event="strikeout")])
        assert len(builder._history) == 1
        # Same atBatIndex, updated event — refresh the existing entry in place.
        result = builder._parse_play_history([self._play(0, event="single")])
        assert len(result) == 1
        assert result[0]["event"] == "single"

    def test_empty_plays_returns_cached_history(self, mock_db_pool) -> None:
        builder = GameStateBuilder(mock_db_pool)
        builder._parse_play_history([self._play(0)])
        # An empty refresh must not wipe the cache.
        assert len(builder._parse_play_history([])) == 1


# ===========================================================================
# GameStateBuilder — role inference (SIM-102)
# ===========================================================================


class TestInferRole:
    def test_starter_is_sp(self) -> None:
        assert GameStateBuilder._infer_role({"inningsPitched": "5.0"}, {}) == "SP"

    def test_opener_is_opener(self) -> None:
        # IP < 4.0 but BF >= 9 → deliberate first-inning opener.
        assert (
            GameStateBuilder._infer_role({"inningsPitched": "2.0", "battersFaced": 9}, {})
            == "Opener"
        )

    def test_multi_inning_relief_is_mrp(self) -> None:
        assert GameStateBuilder._infer_role({"inningsPitched": "1.2"}, {}) == "MRP"

    def test_one_inning_specialist_is_rp(self) -> None:
        assert GameStateBuilder._infer_role({"inningsPitched": "0.2"}, {}) == "RP"

    def test_malformed_innings_pitched_degrades_to_rp(self) -> None:
        # Bad IP string must not raise.
        assert GameStateBuilder._infer_role({"inningsPitched": "bad"}, {}) == "RP"


# ===========================================================================
# ConnectionManager — broadcast snapshot-safety (SIM-103)
# ===========================================================================


class TestConnectionManager:
    def _ws(self) -> AsyncMock:
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_connect_accepts_and_tracks_subscriber(self) -> None:
        cm = ConnectionManager()
        ws = self._ws()
        await cm.connect(745000, ws)
        ws.accept.assert_awaited_once()
        assert cm.subscriber_count(745000) == 1

    @pytest.mark.asyncio
    async def test_disconnect_removes_subscriber(self) -> None:
        cm = ConnectionManager()
        ws = self._ws()
        await cm.connect(745000, ws)
        cm.disconnect(745000, ws)
        assert cm.subscriber_count(745000) == 0

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_subscribers(self) -> None:
        cm = ConnectionManager()
        a, b = self._ws(), self._ws()
        await cm.connect(745000, a)
        await cm.connect(745000, b)
        await cm.broadcast(745000, {"type": "x", "n": 1})
        for ws in (a, b):
            ws.send_text.assert_awaited_once()
            sent = json.loads(ws.send_text.await_args.args[0])
            assert sent["n"] == 1

    @pytest.mark.asyncio
    async def test_broadcast_no_subscribers_is_noop(self) -> None:
        cm = ConnectionManager()
        # No subscribers — must return without raising.
        await cm.broadcast(999999, {"type": "x"})

    @pytest.mark.asyncio
    async def test_broadcast_prunes_dead_connections(self) -> None:
        cm = ConnectionManager()
        good, dead = self._ws(), self._ws()
        dead.send_text.side_effect = RuntimeError("connection closed")
        await cm.connect(745000, good)
        await cm.connect(745000, dead)
        await cm.broadcast(745000, {"type": "x"})
        # Dead connection is pruned; good one survives.
        assert cm.subscriber_count(745000) == 1

    @pytest.mark.asyncio
    async def test_broadcast_iterates_over_snapshot(self) -> None:
        """SIM-103: a disconnect during the awaiting send must not raise
        'Set changed size during iteration'."""
        cm = ConnectionManager()
        a, b = self._ws(), self._ws()
        await cm.connect(745000, a)
        await cm.connect(745000, b)

        async def _mutate_during_send(_msg):
            # Simulate a concurrent disconnect mid-broadcast.
            cm.disconnect(745000, b)

        a.send_text.side_effect = _mutate_during_send
        # Must not raise despite the set mutating mid-loop.
        await cm.broadcast(745000, {"type": "x"})


# ===========================================================================
# _refresh_game_state — the WS-signal → REST-refetch → build → broadcast flow
# ===========================================================================


class TestRefreshGameState:
    @pytest.mark.asyncio
    async def test_refresh_orchestrates_full_cycle(
        self, mock_db_pool, mock_redis, sample_feed, monkeypatch
    ) -> None:
        import pipeline.live.live_ingestion_pipeline as mod

        # Mock the shared connection_manager broadcast so we don't need real WS.
        broadcast_calls: list = []

        async def _capture_broadcast(game_pk, payload):
            broadcast_calls.append((game_pk, payload))

        monkeypatch.setattr(mod.connection_manager, "broadcast", _capture_broadcast)

        game_pk = sample_feed["gamePk"]
        pipeline = _bare_pipeline(_db=mock_db_pool, _redis=mock_redis)

        # Patch _fetch_feed to return our sample feed (no real HTTP).
        async def _fetch_feed(_pk):
            return sample_feed

        pipeline._fetch_feed = _fetch_feed

        await pipeline._refresh_game_state(game_pk)

        # DB writes happened (lineup_state + odds + props).
        assert mock_db_pool.execute.await_count >= 1
        # Redis cache written.
        assert mock_redis.setex.await_count >= 1
        # Broadcast fired with a game_state_update payload.
        assert broadcast_calls
        gp, payload = broadcast_calls[-1]
        assert gp == game_pk
        assert payload["type"] == "game_state_update"
        assert "game_state" in payload
        assert "odds" in payload
        # The sample feed's currentPlay is complete → resim triggered.
        assert payload["resim_triggered"] is True

    @pytest.mark.asyncio
    async def test_refresh_skips_when_lock_held(self, mock_db_pool, sample_feed) -> None:
        import asyncio

        game_pk = sample_feed["gamePk"]
        held = asyncio.Lock()
        await held.acquire()
        pipeline = _bare_pipeline(_db=mock_db_pool, _refresh_locks={game_pk: held})

        fetch_called = False

        async def _fetch_feed(_pk):
            nonlocal fetch_called
            fetch_called = True
            return sample_feed

        pipeline._fetch_feed = _fetch_feed
        await pipeline._refresh_game_state(game_pk)
        # Lock was held → the signal is dropped, no fetch.
        assert fetch_called is False
        held.release()

    @pytest.mark.asyncio
    async def test_refresh_returns_early_when_feed_unavailable(
        self, mock_db_pool, mock_redis
    ) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool, _redis=mock_redis)

        async def _fetch_feed(_pk):
            return None

        pipeline._fetch_feed = _fetch_feed
        await pipeline._refresh_game_state(745000)
        # No feed → no DB writes.
        mock_db_pool.execute.assert_not_awaited()


# ===========================================================================
# _fetch_feed — REST happy path + Redis fallback
# ===========================================================================


class TestFetchFeed:
    @pytest.mark.asyncio
    async def test_fetch_feed_returns_rest_payload_on_200(self) -> None:
        payload = {"gamePk": 745000, "ok": True}
        resp = _make_async_response(status=200, payload=payload)
        http = MagicMock()
        http.get.return_value = resp
        pipeline = _bare_pipeline(_http=http)
        result = await pipeline._fetch_feed(745000)
        assert result == payload

    @pytest.mark.asyncio
    async def test_fetch_feed_falls_back_to_redis_on_non_200(self) -> None:
        cached = {"gamePk": 745000, "cached": True}
        resp = _make_async_response(status=503, payload={})
        http = MagicMock()
        http.get.return_value = resp
        redis = AsyncMock()
        redis.get.return_value = json.dumps(cached)
        pipeline = _bare_pipeline(_http=http, _redis=redis)
        result = await pipeline._fetch_feed(745000)
        assert result == cached
        redis.get.assert_awaited_once_with("game_feed:745000")

    @pytest.mark.asyncio
    async def test_fetch_feed_returns_none_when_no_cache(self) -> None:
        resp = _make_async_response(status=503, payload={})
        http = MagicMock()
        http.get.return_value = resp
        redis = AsyncMock()
        redis.get.return_value = None
        pipeline = _bare_pipeline(_http=http, _redis=redis)
        assert await pipeline._fetch_feed(745000) is None


# ===========================================================================
# _persist_odds / _persist_prop_odds — INSERT + dedup hash
# ===========================================================================


class TestPersistOdds:
    @pytest.mark.asyncio
    async def test_persist_odds_executes_insert_with_hash(self, mock_db_pool) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        odds = MockOddsAPI.get_odds(745000)
        await pipeline._persist_odds(745000, odds)
        mock_db_pool.execute.assert_awaited_once()
        sql = mock_db_pool.execute.await_args.args[0]
        assert "raw.game_odds" in sql
        assert "ON CONFLICT" in sql
        # Last positional arg is the odds_hash (64-char sha256 hex).
        odds_hash = mock_db_pool.execute.await_args.args[-1]
        assert isinstance(odds_hash, str) and len(odds_hash) == 64

    def test_odds_hash_is_deterministic(self) -> None:
        odds = MockOddsAPI.get_odds(745000)
        assert LiveIngestionPipeline._odds_hash(odds) == LiveIngestionPipeline._odds_hash(odds)

    def test_odds_hash_differs_for_different_book(self) -> None:
        a = MockOddsAPI.get_odds(745000, book="pinnacle")
        b = MockOddsAPI.get_odds(745000, book="draftkings")
        assert LiveIngestionPipeline._odds_hash(a) != LiveIngestionPipeline._odds_hash(b)

    @pytest.mark.asyncio
    async def test_persist_prop_odds_executes_insert(self, mock_db_pool) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        prop = MockOddsAPI.get_prop_odds(745000, 100001, "strikeouts")
        await pipeline._persist_prop_odds(prop)
        mock_db_pool.execute.assert_awaited_once()
        sql = mock_db_pool.execute.await_args.args[0]
        assert "raw.prop_odds" in sql
        assert "ON CONFLICT" in sql

    def test_prop_odds_hash_differs_per_prop_stat(self) -> None:
        a = MockOddsAPI.get_prop_odds(745000, 100001, "strikeouts")
        b = MockOddsAPI.get_prop_odds(745000, 100001, "hits")
        assert LiveIngestionPipeline._prop_odds_hash(a) != LiveIngestionPipeline._prop_odds_hash(b)


# ===========================================================================
# mark_closing_lines / mark_closing_prop_lines
# ===========================================================================


class TestClosingLines:
    @pytest.mark.asyncio
    async def test_mark_closing_lines_returns_update_count(self) -> None:
        db = AsyncMock()
        db.execute.return_value = "UPDATE 1"
        pipeline = _bare_pipeline(_db=db)
        n = await pipeline.mark_closing_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert n == 1
        sql = db.execute.await_args.args[0]
        assert "raw.game_odds" in sql
        assert "closing" in sql

    @pytest.mark.asyncio
    async def test_mark_closing_lines_zero_when_no_rows(self) -> None:
        db = AsyncMock()
        db.execute.return_value = "UPDATE 0"
        pipeline = _bare_pipeline(_db=db)
        n = await pipeline.mark_closing_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert n == 0

    @pytest.mark.asyncio
    async def test_mark_closing_prop_lines_returns_update_count(self) -> None:
        db = AsyncMock()
        db.execute.return_value = "UPDATE 28"
        pipeline = _bare_pipeline(_db=db)
        n = await pipeline.mark_closing_prop_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert n == 28
        sql = db.execute.await_args.args[0]
        assert "raw.prop_odds" in sql
        assert "DISTINCT ON" in sql


# ===========================================================================
# Prop-odds cadence + player-id collection (SIM-340)
# ===========================================================================


class TestPropOddsCycle:
    def test_collect_prop_player_ids_dedupes_and_filters(self, sample_game_state) -> None:
        ids = LiveIngestionPipeline._collect_prop_player_ids(sample_game_state)
        # current_pitcher_id (999) + home/away lineup ids; order-stable, no dupes.
        assert ids[0] == 999
        assert len(ids) == len(set(ids))
        assert None not in ids

    def test_collect_prop_player_ids_empty_when_no_players(self) -> None:
        ids = LiveIngestionPipeline._collect_prop_player_ids(
            {"current_pitcher_id": None, "home_lineup": [], "away_lineup": []}
        )
        assert ids == []

    @pytest.mark.asyncio
    async def test_prop_cycle_persists_quotes_when_gate_open(
        self, mock_db_pool, sample_game_state
    ) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        written = await pipeline._persist_prop_odds_cycle(745000, sample_game_state)
        n_players = len(LiveIngestionPipeline._collect_prop_player_ids(sample_game_state))
        expected = n_players * len(PROP_STATS) * len(PROP_BOOKS)
        assert written == expected
        assert mock_db_pool.execute.await_count == expected

    @pytest.mark.asyncio
    async def test_prop_cycle_throttled_within_cadence(
        self, mock_db_pool, sample_game_state
    ) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        # Pretend we just fetched a moment ago — gate must be closed.
        pipeline._last_prop_fetch[745000] = datetime.now(UTC)
        written = await pipeline._persist_prop_odds_cycle(745000, sample_game_state)
        assert written == 0
        mock_db_pool.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prop_cycle_no_players_does_not_stamp_clock(self, mock_db_pool) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        empty_state = {"current_pitcher_id": None, "home_lineup": [], "away_lineup": []}
        written = await pipeline._persist_prop_odds_cycle(745000, empty_state)
        assert written == 0
        # No eligible players → clock not stamped, so a later signal retries.
        assert 745000 not in pipeline._last_prop_fetch

    @pytest.mark.asyncio
    async def test_capture_opening_prop_lines_writes_opening(self, mock_db_pool) -> None:
        pipeline = _bare_pipeline(_db=mock_db_pool)
        written = await pipeline.capture_opening_prop_lines(745000, [100001])
        expected = 1 * len(PROP_STATS) * len(PROP_BOOKS)
        assert written == expected


# ===========================================================================
# _signal_resimulation — callback dispatch
# ===========================================================================


class TestSignalResimulation:
    @pytest.mark.asyncio
    async def test_signal_invokes_async_callback(self, sample_game_state) -> None:
        seen: list = []

        async def _cb(game_pk, game_state):
            seen.append((game_pk, game_state))

        pipeline = _bare_pipeline(_sim_cb=_cb)
        await pipeline._signal_resimulation(745000, sample_game_state)
        assert seen == [(745000, sample_game_state)]

    @pytest.mark.asyncio
    async def test_signal_logs_when_no_callback(self, sample_game_state) -> None:
        pipeline = _bare_pipeline(_sim_cb=None)
        # Must not raise when there's no callback wired (Phase 5 not yet built).
        await pipeline._signal_resimulation(745000, sample_game_state)

    @pytest.mark.asyncio
    async def test_signal_swallows_callback_exception(self, sample_game_state) -> None:
        async def _cb(game_pk, game_state):
            raise ValueError("callback boom")

        pipeline = _bare_pipeline(_sim_cb=_cb)
        # A failing callback must not propagate out of the pipeline.
        await pipeline._signal_resimulation(745000, sample_game_state)


# ===========================================================================
# __init__ runtime guards (SIM-106 / SIM-153)
# ===========================================================================


class TestPipelineInitGuards:
    def test_sync_callback_raises_typeerror(self) -> None:
        def _sync_cb(game_pk, game_state):  # not async
            return None

        with pytest.raises(TypeError, match="async"):
            LiveIngestionPipeline(
                dsn="postgresql://x", redis_url="redis://x", simulation_callback=_sync_cb
            )

    def test_missing_dsn_raises_runtimeerror(self, monkeypatch) -> None:
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
        with pytest.raises(RuntimeError, match="DSN"):
            LiveIngestionPipeline(dsn=None, redis_url="redis://x")

    def test_missing_redis_raises_runtimeerror(self, monkeypatch) -> None:
        monkeypatch.delenv("REDIS_URL", raising=False)
        with pytest.raises(RuntimeError, match="Redis"):
            LiveIngestionPipeline(dsn="postgresql://x", redis_url=None)

    def test_valid_async_callback_constructs(self) -> None:
        async def _cb(game_pk, game_state):
            return None

        p = LiveIngestionPipeline(
            dsn="postgresql://x", redis_url="redis://x", simulation_callback=_cb
        )
        assert p._sim_cb is _cb
        assert p.live_game_pks == []
        assert p.is_watching(123) is False


# ===========================================================================
# create_app — manual /resimulate cooldown (SIM-104)
# ===========================================================================


class TestResimulateEndpoint:
    def _app_and_pipeline(self, monkeypatch):
        """Build the app and reach into the closure's pipeline via TestClient.

        We construct the app with explicit dsn/redis so __init__ passes, then
        swap the pipeline's _redis / _http for mocks before any request.
        """
        from fastapi.testclient import TestClient

        app = create_app(dsn="postgresql://x", redis_url="redis://x")
        return app, TestClient(app, raise_server_exceptions=True)

    def test_resimulate_rate_limited_returns_429(self, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        import pipeline.live.live_ingestion_pipeline as mod

        app = create_app(dsn="postgresql://x", redis_url="redis://x")

        # Find the pipeline instance the endpoint closes over by patching
        # LiveIngestionPipeline so its _redis is a mock with an active cooldown.
        # The closure already captured the instance; grab it via the route.
        # Simplest: rebuild with a stubbed redis on the captured pipeline.
        # We access it through the app's resimulate handler closure.
        redis = AsyncMock()
        redis.ttl.return_value = 7  # active cooldown
        # The pipeline is captured in create_app's closure; patch via attribute
        # on the route's __closure__ is brittle, so instead exercise the
        # cooldown logic by monkeypatching the class default redis getter.
        # Inject through app.state is not used; instead patch the instance.
        # Locate pipeline: it's referenced by the lifespan + handlers.
        # We hook it by replacing connection_manager-independent state:
        # iterate the app routes to find the bound pipeline.
        target = None
        for route in app.routes:
            ep = getattr(route, "endpoint", None)
            if ep and getattr(ep, "__closure__", None):
                for cell in ep.__closure__:
                    if isinstance(cell.cell_contents, mod.LiveIngestionPipeline):
                        target = cell.cell_contents
                        break
            if target:
                break
        assert target is not None, "could not locate pipeline in app closure"
        target._redis = redis

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/games/745000/resimulate")
        assert resp.status_code == 429
        body = resp.json()
        assert body["status"] == "rate_limited"
        assert body["retry_after_seconds"] == 7

    def test_resimulate_sets_cooldown_and_uses_cache(self, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        import pipeline.live.live_ingestion_pipeline as mod

        cached_state = {"inning": 4, "half": "Top"}
        redis = AsyncMock()
        redis.ttl.return_value = None  # no active cooldown
        redis.get.return_value = json.dumps(cached_state)

        signalled: list = []

        app = create_app(dsn="postgresql://x", redis_url="redis://x")
        target = None
        for route in app.routes:
            ep = getattr(route, "endpoint", None)
            if ep and getattr(ep, "__closure__", None):
                for cell in ep.__closure__:
                    if isinstance(cell.cell_contents, mod.LiveIngestionPipeline):
                        target = cell.cell_contents
                        break
            if target:
                break
        assert target is not None
        target._redis = redis
        target._ws_clients = {}  # not watching → cache path

        async def _signal(game_pk, game_state):
            signalled.append((game_pk, game_state))

        target._signal_resimulation = _signal

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/games/745000/resimulate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "triggered"
        assert body["source"] == "cache"
        # Cooldown was set with the configured TTL.
        redis.setex.assert_awaited()
        setex_args = redis.setex.await_args.args
        assert setex_args[0] == "resim_cooldown:745000"
        assert setex_args[1] == RESIM_COOLDOWN_S
        assert signalled == [(745000, cached_state)]
