"""SIM-519 Part C — the live service: the Redis bridge, the heartbeat, the poll
window, the Preview-to-Live hook and the vendor reads off the loop."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.live import broadcast as bc


class _PubSub:
    def __init__(self, redis: _Redis) -> None:
        self.redis = redis
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.channels: set[str] = set()
        self.closed = False

    async def subscribe(self, name: str) -> None:
        self.channels.add(name)
        self.redis.subs.append(self)

    async def unsubscribe(self, name: str) -> None:
        self.channels.discard(name)

    async def aclose(self) -> None:
        self.closed = True
        if self in self.redis.subs:
            self.redis.subs.remove(self)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0) -> dict | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None


class _Redis:
    def __init__(self) -> None:
        self.kv: dict[str, tuple[str, int | None]] = {}
        self.subs: list[_PubSub] = []
        self.published: list[tuple[str, str]] = []

    async def publish(self, name: str, message: str) -> int:
        self.published.append((name, message))
        n = 0
        for s in list(self.subs):
            if name in s.channels:
                await s.queue.put({"type": "message", "data": message})
                n += 1
        return n

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = (value, ex)

    async def get(self, key: str) -> str | None:
        v = self.kv.get(key)
        return None if v is None else v[0]

    def pubsub(self) -> _PubSub:
        return _PubSub(self)


def test_the_broadcaster_publishes_on_the_game_channel() -> None:
    redis = _Redis()
    asyncio.run(bc.RedisBroadcaster(redis).broadcast(745001, {"type": "x", "n": 1}))
    assert redis.published == [("live:game:745001", json.dumps({"type": "x", "n": 1}))]


def test_the_bridge_forwards_and_stops_with_the_last_watcher() -> None:
    async def scenario() -> tuple[list, int, int]:
        redis = _Redis()
        got: list[tuple[int, dict]] = []

        async def forward(pk: int, payload: dict) -> None:
            got.append((pk, payload))

        bridge = bc.RedisBridge(redis, forward)
        await bridge.retain(7)
        await bridge.retain(7)  # a second browser on the same game
        await asyncio.sleep(0.05)
        assert len(redis.subs) == 1  # one subscription per game
        await bc.RedisBroadcaster(redis).broadcast(7, {"type": "game_state_update"})
        await bc.RedisBroadcaster(redis).broadcast(8, {"type": "other game"})
        await asyncio.sleep(0.05)
        await bridge.release(7)
        still = len(redis.subs)
        await bridge.release(7)
        await asyncio.sleep(0.01)
        return got, still, len(redis.subs)

    got, after_one_left, after_both_left = asyncio.run(scenario())
    assert got == [(7, {"type": "game_state_update"})]
    assert after_one_left == 1
    assert after_both_left == 0


def test_heartbeat_keys_and_status() -> None:
    redis = _Redis()
    asyncio.run(bc.write_heartbeat(redis, [3, 1], now=1000.0))
    assert redis.kv[bc.HEARTBEAT_KEY] == ("1000.0", 90)
    assert redis.kv[bc.WATCHING_KEY] == ("[1, 3]", 90)
    assert asyncio.run(bc.heartbeat_age_s(redis, now=1030.0)) == pytest.approx(30.0)
    with patch("pipeline.live.broadcast.time.time", return_value=1030.0):
        assert asyncio.run(bc.heartbeat_status(redis)) == "ok"
    with patch("pipeline.live.broadcast.time.time", return_value=1200.0):
        assert asyncio.run(bc.heartbeat_status(redis)) == "stale"
    assert asyncio.run(bc.heartbeat_status(_Redis())) == "absent"


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _pipeline() -> Any:
    from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = MagicMock()
    p._db.execute = AsyncMock()
    p._redis = _Redis()
    p._ws_clients = {}
    p._builders = {}
    p._completed_games = set()
    return p


class _Http:
    def __init__(self, schedule: dict) -> None:
        self.schedule = schedule
        self.params: list[dict] = []

    def get(self, url: str, params: dict | None = None) -> Any:
        self.params.append(dict(params or {}))
        sched = self.schedule

        class _R:
            async def json(self) -> dict:
                return sched

            async def __aenter__(self) -> _R:
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

        return _R()


def test_the_poll_reads_yesterday_to_tomorrow_in_eastern_time() -> None:
    from pipeline.live import live_ingestion_pipeline as live

    p = _pipeline()
    p._http = _Http({"dates": []})
    # 01:30 UTC on Oct 10 is still Oct 9 in New York.
    fixed = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)

    class _DT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed.astimezone(tz) if tz else fixed

    with patch.object(live, "datetime", _DT):
        asyncio.run(p._sync_live_games())
    params = p._http.params[0]
    assert (params["startDate"], params["endDate"]) == ("2026-10-08", "2026-10-10")
    assert "date" not in params
    # The heartbeat is written on every poll.
    assert bc.HEARTBEAT_KEY in p._redis.kv


def test_a_game_already_final_is_upserted_once() -> None:
    final = {"gamePk": 5, "status": {"abstractGameState": "Final"}}
    p = _pipeline()
    p._http = _Http({"dates": [{"games": [final]}]})
    p._upsert_game_record = AsyncMock()

    async def two_polls() -> None:
        await p._sync_live_games()
        await p._sync_live_games()
        await asyncio.sleep(0)

    asyncio.run(two_polls())
    assert p._upsert_game_record.await_count == 1
    assert 5 in p._completed_games


def test_the_first_live_poll_records_the_first_pitch() -> None:
    live_game = {"gamePk": 9, "status": {"abstractGameState": "Live"}}
    p = _pipeline()
    p._http = _Http({"dates": [{"games": [live_game]}]})
    p._upsert_game_record = AsyncMock()
    p._mark_closing_rows = AsyncMock(return_value=(0, 0))
    p._start_watching = AsyncMock(side_effect=lambda pk: p._ws_clients.__setitem__(pk, MagicMock()))
    p._refresh_game_state = AsyncMock()

    async def two_polls() -> None:
        await p._sync_live_games()
        await p._sync_live_games()
        await asyncio.sleep(0)

    asyncio.run(two_polls())
    hooks = [c for c in p._db.execute.await_args_list if "first_pitch_at" in c.args[0]]
    assert len(hooks) == 1  # once per game
    assert "COALESCE(first_pitch_at" in hooks[0].args[0]
    assert hooks[0].args[1] == 9


def test_messages_go_through_the_configured_publisher() -> None:
    p = _pipeline()
    redis = _Redis()
    p._broadcaster = bc.RedisBroadcaster(redis)
    asyncio.run(p._broadcast_message(4, {"type": "lineup_published"}))
    assert redis.published[0][0] == "live:game:4"


def test_vendor_reads_run_off_the_event_loop() -> None:
    import threading

    p = _pipeline()
    seen: list[str] = []

    class _Provider:
        def get_odds(self, game_pk: int) -> dict:
            seen.append(threading.current_thread().name)
            return {"game_pk": game_pk}

    p._odds = _Provider()
    out = asyncio.run(p._fetch_odds(12))
    assert out == {"game_pk": 12}
    assert seen and seen[0].startswith("odds-vendor")


def test_the_websocket_retains_and_releases_the_bridge() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pipeline.live.live_ingestion_pipeline import ws_router

    app = FastAPI()
    app.include_router(ws_router)
    bridge = MagicMock()
    bridge.retain = AsyncMock()
    bridge.release = AsyncMock()
    app.state.live_bridge = bridge
    with TestClient(app) as client, client.websocket_connect("/ws/games/77") as ws:
        ws.send_text("ping")
        assert json.loads(ws.receive_text()) == {"type": "pong"}
    bridge.retain.assert_awaited_once_with(77)
    bridge.release.assert_awaited_once_with(77)
