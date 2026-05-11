"""
test_api_state.py
=================
Unit tests for api/state.py — the boot-time resource builders that the
FastAPI lifespan attaches to ``app.state``.

These tests exercise the pieces that don't require a live DuckDB / live
Postgres / live Redis:

  - make_cache_key — pinned format the nightly invalidator will rely on
  - make_pg_name_resolver — SQL shape and result handling against a
    stubbed asyncpg pool
  - RedisSimilarityCache — get/set semantics against an in-memory fake
  - build_pitcher_engine env-var precedence (mocked engine constructor)

Owned by Backend Developer (Agent 5).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from api.state import (
    CACHE_TTL_SECONDS,
    RedisSimilarityCache,
    make_cache_key,
    make_pg_name_resolver,
)


# ---------------------------------------------------------------------------
# make_cache_key
# ---------------------------------------------------------------------------


class TestCacheKey:

    def test_format_matches_spec(self):
        # Spec: docs/similarity_visualization_spec.md §3 caching.
        assert (
            make_cache_key(694973, 2025, 20, 20)
            == "simviz:pitcher:694973:2025:bins=20:top_n=20"
        )

    def test_different_params_yield_different_keys(self):
        keys = {
            make_cache_key(1, 2024, 20, 20),
            make_cache_key(1, 2025, 20, 20),   # season diff
            make_cache_key(2, 2024, 20, 20),   # pitcher diff
            make_cache_key(1, 2024, 40, 20),   # bins diff
            make_cache_key(1, 2024, 20, 50),   # top_n diff
        }
        assert len(keys) == 5

    def test_prefix_is_simviz_pitcher(self):
        # The nightly invalidator (Data Engineer follow-on) will DEL
        # ``simviz:*`` — confirm the prefix never drifts.
        key = make_cache_key(10, 2024, 20, 20)
        assert key.startswith("simviz:pitcher:")


# ---------------------------------------------------------------------------
# make_pg_name_resolver
# ---------------------------------------------------------------------------


class _StubPool:
    """Asyncpg pool stand-in. Records the SQL + args it was called with."""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql: str | None = None
        self.last_args: tuple | None = None

    async def fetch(self, sql, *args):
        self.last_sql = sql
        self.last_args = args
        return self._rows


@pytest.mark.asyncio
async def test_name_resolver_returns_empty_for_empty_input():
    pool = _StubPool(rows=[])
    resolve = make_pg_name_resolver(pool)
    assert await resolve([]) == {}
    # No DB round-trip for an empty input.
    assert pool.last_sql is None


@pytest.mark.asyncio
async def test_name_resolver_dedupes_and_casts_ids():
    """
    The resolver must dedupe and integer-cast input ids before issuing
    the query — protects against duplicate ids from set() iteration
    order and against str ids leaking in from a JSON layer.
    """
    pool = _StubPool(rows=[
        {"player_id": 100, "full_name": "Alice"},
        {"player_id": 200, "full_name": "Bob"},
    ])
    resolve = make_pg_name_resolver(pool)
    result = await resolve([100, 100, "200", 200])
    assert result == {100: "Alice", 200: "Bob"}
    # Single round-trip; deduped + sorted id list passed as $1.
    assert pool.last_args == ([100, 200],)


@pytest.mark.asyncio
async def test_name_resolver_handles_missing_ids():
    """
    Postgres returns fewer rows than requested when some ids are
    missing — those ids are simply absent from the result dict.
    The route falls back to a placeholder name.
    """
    pool = _StubPool(rows=[
        {"player_id": 100, "full_name": "Alice"},
        # 200 deliberately absent
    ])
    resolve = make_pg_name_resolver(pool)
    result = await resolve([100, 200])
    assert result == {100: "Alice"}
    assert 200 not in result


@pytest.mark.asyncio
async def test_name_resolver_sql_uses_array_filter():
    """Pin the query shape so it stays index-friendly on raw.players."""
    pool = _StubPool(rows=[])
    resolve = make_pg_name_resolver(pool)
    await resolve([1, 2, 3])
    assert "raw.players" in pool.last_sql
    assert "player_id = ANY($1::int[])" in pool.last_sql


# ---------------------------------------------------------------------------
# RedisSimilarityCache
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory Redis stand-in covering the two methods the cache uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))


@pytest.mark.asyncio
async def test_cache_set_then_get_roundtrip():
    redis = _FakeRedis()
    cache = RedisSimilarityCache(redis)
    payload = {"hello": "world", "n": 42}

    await cache.set_json("k", payload)
    result = await cache.get_json("k")

    assert result == payload
    # Default TTL is the spec-defined 24h.
    assert redis.set_calls[-1][2] == CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_cache_get_returns_none_for_missing_key():
    cache = RedisSimilarityCache(_FakeRedis())
    assert await cache.get_json("nope") is None


@pytest.mark.asyncio
async def test_cache_get_treats_poisoned_value_as_miss():
    """
    A JSON-decode failure must not raise — the route would 500 on a
    poisoned cache value, which is worse than just re-computing.
    """
    redis = _FakeRedis()
    redis.store["bad"] = "not-valid-json{{"
    cache = RedisSimilarityCache(redis)
    assert await cache.get_json("bad") is None


@pytest.mark.asyncio
async def test_cache_custom_ttl_is_respected():
    redis = _FakeRedis()
    cache = RedisSimilarityCache(redis)
    await cache.set_json("k", {"x": 1}, ttl_s=60)
    assert redis.set_calls[-1][2] == 60


@pytest.mark.asyncio
async def test_cache_serializes_with_default_str_fallback():
    """
    Anything the JSON encoder can't handle natively (e.g. a datetime)
    falls back to str(). Confirms the cache doesn't raise on payloads
    that mix Pydantic-serialized primitives with stray objects.
    """
    redis = _FakeRedis()
    cache = RedisSimilarityCache(redis)

    class _Custom:
        def __str__(self) -> str:
            return "custom-value"

    await cache.set_json("k", {"obj": _Custom()})
    raw = redis.store["k"]
    # Confirm it round-tripped via the default=str path.
    assert json.loads(raw)["obj"] == "custom-value"


# ---------------------------------------------------------------------------
# build_pitcher_engine — env-var precedence
# ---------------------------------------------------------------------------


def test_build_pitcher_engine_uses_arg_over_env(monkeypatch):
    """
    Explicit ``duckdb_path`` argument wins over the env var.
    We mock the actual engine constructor since we can't instantiate
    against a real DuckDB file in a unit test.
    """
    import api.state as state_mod
    monkeypatch.setenv("BASEBALL_DUCKDB_PATH", "/from/env.duckdb")

    seen = {}

    class _StubEngine:
        def __init__(self, duckdb_path):
            seen["path"] = duckdb_path
            self._profiles = {}

        def build(self):
            seen["built"] = True

    # Patch the lazy import target by injecting a fake module.
    import sys, types
    fake_mod = types.ModuleType("similarity.engines.pitcher_similarity")
    fake_mod.PitcherSimilarityEngine = _StubEngine
    monkeypatch.setitem(sys.modules, "similarity.engines.pitcher_similarity", fake_mod)

    state_mod.build_pitcher_engine(duckdb_path="/explicit/path.duckdb")
    assert seen["path"] == "/explicit/path.duckdb"
    assert seen["built"] is True


def test_build_pitcher_engine_falls_back_to_env(monkeypatch):
    import api.state as state_mod
    monkeypatch.setenv("BASEBALL_DUCKDB_PATH", "/from/env.duckdb")
    seen = {}

    class _StubEngine:
        def __init__(self, duckdb_path):
            seen["path"] = duckdb_path
            self._profiles = {}

        def build(self):
            pass

    import sys, types
    fake_mod = types.ModuleType("similarity.engines.pitcher_similarity")
    fake_mod.PitcherSimilarityEngine = _StubEngine
    monkeypatch.setitem(sys.modules, "similarity.engines.pitcher_similarity", fake_mod)

    state_mod.build_pitcher_engine()
    assert seen["path"] == "/from/env.duckdb"


def test_build_pitcher_engine_default_path_when_no_env(monkeypatch):
    import api.state as state_mod
    monkeypatch.delenv("BASEBALL_DUCKDB_PATH", raising=False)
    seen = {}

    class _StubEngine:
        def __init__(self, duckdb_path):
            seen["path"] = duckdb_path
            self._profiles = {}

        def build(self):
            pass

    import sys, types
    fake_mod = types.ModuleType("similarity.engines.pitcher_similarity")
    fake_mod.PitcherSimilarityEngine = _StubEngine
    monkeypatch.setitem(sys.modules, "similarity.engines.pitcher_similarity", fake_mod)

    state_mod.build_pitcher_engine()
    assert seen["path"] == state_mod.DEFAULT_DUCKDB_PATH


# ---------------------------------------------------------------------------
# AsyncMock sanity (catch wiring drift early)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_is_awaitable():
    """A regression guard: the route awaits the resolver — confirm
    make_pg_name_resolver returns an awaitable, not a sync callable."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    resolve = make_pg_name_resolver(pool)
    coro = resolve([1])
    # Must be awaitable (a coroutine), not a dict.
    assert hasattr(coro, "__await__")
    await coro
