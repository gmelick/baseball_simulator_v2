"""
test_api_data_health_sim417.py
==============================
Unit tests for the SIM-417 data-freshness API. Builds a tiny app with only the
data_health router and a fake asyncpg pool that returns canned aggregate +
per-season rows — no live DB.

The route reads the read-only pool, ``app.state.pg_pool_ro`` (SIM-566: the Data
Lab read routes run on the ``baseball_ro`` login). The main pool is a sentinel
that fails the test on any use, so a fallback to it cannot pass.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes._common import RO_POOL_UNAVAILABLE_DETAIL
from api.routes.data_health import router as data_health_router


class _FakePool:
    """Fake pool: fetchrow → the aggregate row, fetch → per-season rows."""

    def __init__(self, *, agg=None, seasons=None):
        self._agg = agg
        self._seasons = seasons if seasons is not None else []

    async def fetchrow(self, sql, *args):
        return self._agg

    async def fetch(self, sql, *args):
        return self._seasons


class _SentinelPool:
    """The main pool. Any use of it fails the test."""

    def acquire(self):
        raise AssertionError("the freshness route must never use the main pool")

    async def fetchrow(self, sql, *args):
        raise AssertionError("the freshness route must never use the main pool")

    async def fetch(self, sql, *args):
        raise AssertionError("the freshness route must never use the main pool")


def _build_app(pool) -> FastAPI:
    app = FastAPI()
    app.include_router(data_health_router)
    app.state.pg_pool_ro = pool
    app.state.pg_pool = _SentinelPool()
    return app


_AGG = {
    "last_ingest_at": datetime(2026, 5, 26, 6, 46, 39),
    "latest_game_date": date(2024, 8, 15),
    "tracked_entities": 51,
    "total_games": 360,
    "total_pitches": 105609,
}
_SEASONS = [
    {"season": 2017, "games": 359, "pitches": 105184},
    {"season": 2024, "games": 1, "pitches": 425},
]


def test_freshness_returns_aggregate_and_seasons():
    app = _build_app(_FakePool(agg=_AGG, seasons=_SEASONS))
    client = TestClient(app)
    resp = client.get("/api/data/freshness")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["last_ingest_at"].startswith("2026-05-26T06:46:39")
    assert body["latest_game_date"] == "2024-08-15"
    assert body["tracked_entities"] == 51
    assert body["total_games"] == 360
    assert body["total_pitches"] == 105609
    assert body["has_data"] is True
    assert len(body["seasons"]) == 2
    assert body["seasons"][0] == {"season": 2017, "games": 359, "pitches": 105184}


def test_empty_store_returns_zeros_not_error():
    empty_agg = {
        "last_ingest_at": None,
        "latest_game_date": None,
        "tracked_entities": 0,
        "total_games": 0,
        "total_pitches": 0,
    }
    app = _build_app(_FakePool(agg=empty_agg, seasons=[]))
    client = TestClient(app)
    resp = client.get("/api/data/freshness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_data"] is False
    assert body["total_pitches"] == 0
    assert body["last_ingest_at"] is None
    assert body["seasons"] == []


def test_no_pool_returns_503():
    # A usable main pool is present: the route must still refuse, not fall back.
    app = FastAPI()
    app.include_router(data_health_router)
    app.state.pg_pool_ro = None
    app.state.pg_pool = _FakePool(agg=_AGG, seasons=_SEASONS)
    client = TestClient(app)
    resp = client.get("/api/data/freshness")
    assert resp.status_code == 503
    assert resp.json()["detail"] == RO_POOL_UNAVAILABLE_DETAIL


def test_response_is_json_serializable():
    app = _build_app(_FakePool(agg=_AGG, seasons=_SEASONS))
    client = TestClient(app)
    import json

    json.dumps(client.get("/api/data/freshness").json())
