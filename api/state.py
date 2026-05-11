"""
api/state.py
=============
Boot-time resource construction for the FastAPI app.

Owns the three resources the lifespan in ``api.main`` attaches to
``app.state`` for the similarity routes (and other Phase-5 routes as
they come online):

  1. ``pitcher_engine``        — a fully-built PitcherSimilarityEngine
  2. ``player_name_resolver``  — async callable: pitcher_ids → names
  3. ``similarity_cache``      — Redis-backed TTL cache for endpoint payloads

Each builder is exported as a top-level function so unit tests can
exercise it without booting the whole app. The route handler in
``api/routes/similarity.py`` consumes these via FastAPI ``Depends``.

Owner: Backend Developer (Agent 5).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from typing import Any, Protocol

import asyncpg
import redis.asyncio as aioredis

log = logging.getLogger("api.state")


# ---------------------------------------------------------------------------
# Pitcher similarity engine
# ---------------------------------------------------------------------------

DEFAULT_DUCKDB_PATH = "/data/baseball_sim.duckdb"


def build_pitcher_engine(duckdb_path: str | None = None):
    """
    Construct and build the PitcherSimilarityEngine.

    This is a synchronous, CPU+I/O bound call (engine.build() loads
    thousands of profiles from DuckDB and applies shrinkage + GMM
    standardization). Designed to be invoked from the FastAPI lifespan
    under ``asyncio.to_thread(...)`` so it does not block the event loop.

    Parameters
    ----------
    duckdb_path : str, optional
        Path to the DuckDB analytical database. Defaults to the
        ``BASEBALL_DUCKDB_PATH`` env var, then to
        ``/data/baseball_sim.duckdb`` (the project's docker convention).

    Returns
    -------
    PitcherSimilarityEngine
        A fully warmed engine — ``build()`` has been called.

    Raises
    ------
    Any exception raised by ``PitcherSimilarityEngine.build()`` (most
    commonly ``duckdb.IOException`` when the DuckDB file does not exist
    or is locked by the nightly profile job). The lifespan treats this
    as fatal, matching the fail-fast posture of ``validate_environment``.
    """
    # Import lazily so importing this module in a numpy-free environment
    # (e.g. ruff lint workers) doesn't require the heavy similarity deps.
    from similarity.engines.pitcher_similarity import PitcherSimilarityEngine

    path = duckdb_path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH

    log.info("Building PitcherSimilarityEngine from %s ...", path)
    engine = PitcherSimilarityEngine(duckdb_path=path)
    engine.build()
    log.info(
        "PitcherSimilarityEngine ready: %d profiles loaded.",
        len(engine._profiles),  # noqa: SLF001 — internal counter for log line
    )
    return engine


# ---------------------------------------------------------------------------
# Player name resolver
# ---------------------------------------------------------------------------


class NameResolver(Protocol):
    """
    Async callable that maps an iterable of player_ids to a
    ``{player_id: full_name}`` dict. Missing ids are simply absent
    from the returned dict — callers fall back to a placeholder name.
    """

    async def __call__(self, ids: Iterable[int]) -> dict[int, str]: ...


def make_pg_name_resolver(pool: asyncpg.Pool) -> NameResolver:
    """
    Build a Postgres-backed NameResolver. Reads ``raw.players``,
    matching pitcher_ids in a single ``ANY($1::int[])`` query so a
    similarity query that fans out to 2,400 pitchers costs exactly one
    round trip.

    Returned closure is safe to attach to ``app.state`` and call
    concurrently — asyncpg pools are themselves concurrency-safe.
    """

    async def _resolve(ids: Iterable[int]) -> dict[int, str]:
        id_list = sorted({int(i) for i in ids})
        if not id_list:
            return {}
        rows = await pool.fetch(
            "SELECT player_id, full_name FROM   raw.players WHERE  player_id = ANY($1::int[])",
            id_list,
        )
        return {int(r["player_id"]): r["full_name"] for r in rows}

    return _resolve


# ---------------------------------------------------------------------------
# Similarity payload cache
# ---------------------------------------------------------------------------


CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — per docs/similarity_visualization_spec.md §3


def make_cache_key(pitcher_id: int, season: int, bins: int, top_n: int) -> str:
    """
    Build the canonical Redis cache key for a similarity-distribution
    payload. Format is pinned by the spec so the eventual nightly
    invalidator can target ``simviz:*`` cleanly.
    """
    return f"simviz:pitcher:{pitcher_id}:{season}:bins={bins}:top_n={top_n}"


class SimilarityCache(Protocol):
    """
    Minimal async cache interface used by the similarity route. Two
    methods only: ``get_json`` and ``set_json``. Anything more elaborate
    belongs in a dedicated caching layer when more routes need it.
    """

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl_s: int = CACHE_TTL_SECONDS) -> None: ...


class RedisSimilarityCache:
    """
    Redis-backed implementation. Stores values as JSON strings under
    keys produced by :func:`make_cache_key`. A decode failure is treated
    as a cache miss (with a WARNING log) so a poisoned key never breaks
    the route.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._r = redis_client

    async def get_json(self, key: str) -> Any | None:
        raw = await self._r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            log.warning("Discarding poisoned cache value at %s", key)
            return None

    async def set_json(
        self,
        key: str,
        value: Any,
        ttl_s: int = CACHE_TTL_SECONDS,
    ) -> None:
        await self._r.set(key, json.dumps(value, default=str), ex=ttl_s)


async def open_redis_cache(redis_url: str) -> tuple[aioredis.Redis, RedisSimilarityCache]:
    """
    Open a Redis client and wrap it in a SimilarityCache. Returns
    the raw client too so the caller can close it on shutdown.
    """
    client = aioredis.from_url(redis_url, decode_responses=True)
    # Probe the connection so a misconfigured Redis fails fast at boot
    # (matches the validate_environment philosophy in api.main).
    await client.ping()
    return client, RedisSimilarityCache(client)


# ---------------------------------------------------------------------------
# Postgres pool
# ---------------------------------------------------------------------------


async def open_pg_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """
    Open an asyncpg pool against the project's PostgreSQL DSN.

    Mirrors the connection-pool sizing used by
    ``pipeline.live.live_ingestion_pipeline.LiveIngestionPipeline.start()``.
    Kept here as a small helper so the lifespan in ``api.main`` stays
    declarative and the sizing knobs live in one place.
    """
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    if pool is None:
        # asyncpg.create_pool returns None only if init=False is passed;
        # we never pass it. This branch is defensive only.
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool
