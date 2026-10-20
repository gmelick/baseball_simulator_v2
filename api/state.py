"""
api/state.py
=============
The builders of the resources the lifespan (``api/main.py``) attaches to
``app.state``:

  * the similarity engines: ``build_pitcher_engine`` and ``build_all_engines``
    (all eleven, by their :data:`ENGINE_REGISTRY` name), each waiting out a
    DuckDB writer (:func:`retry_on_duckdb_lock`);
  * the calibration: ``load_calibration_report`` and
    ``apply_calibration_to_engines``;
  * ``make_pg_name_resolver``: player ids to names, from ``raw.players``;
  * ``open_redis_cache``: the Redis client and the similarity payload cache;
  * ``open_pg_pool``: an asyncpg pool; ``redact_dsn`` masks a DSN for a log.

Each builder is a top-level function, so a unit test can run it without
booting the app.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import redis.asyncio as aioredis

from simulation.flags import read_float

log = logging.getLogger("api.state")


# ---------------------------------------------------------------------------
# The pitcher similarity engine, and the wait for a DuckDB writer
# ---------------------------------------------------------------------------

DEFAULT_DUCKDB_PATH = "/data/baseball_sim.duckdb"

#: The longest a boot waits for a DuckDB writer (a rebuild) to finish, in seconds.
BOOT_LOCK_WAIT_ENV = "SIM_BOOT_LOCK_WAIT_S"
DEFAULT_BOOT_LOCK_WAIT_S = 1800.0

#: Seconds between two tries while a writer holds the file.
BOOT_LOCK_RETRY_S = 15.0


def _is_duckdb_lock_error(exc: BaseException) -> bool:
    """True for the IOException DuckDB raises while another process holds the file."""
    if type(exc).__name__ != "IOException":
        return False
    msg = str(exc).lower()
    return "lock" in msg or "already open" in msg


def _boot_lock_wait_s() -> float:
    """``SIM_BOOT_LOCK_WAIT_S``, read through the flag registry."""
    return read_float(BOOT_LOCK_WAIT_ENV)


def retry_on_duckdb_lock[T](
    fn: Callable[[], T],
    what: str,
    *,
    max_wait_s: float | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``fn``; while a DuckDB writer holds the file, wait and try again.

    A boot during the nightly rebuild waits for the writer, up to
    ``SIM_BOOT_LOCK_WAIT_S`` (default 30 minutes), then raises the lock error.
    Any other error rises at once (the nightly rebuild that reaches users,
    SIM-563).
    """
    limit = _boot_lock_wait_s() if max_wait_s is None else max_wait_s
    started = clock()
    while True:
        try:
            return fn()
        except Exception as exc:
            if not _is_duckdb_lock_error(exc) or clock() - started >= limit:
                raise
            log.warning(
                "SIM-563: %s waits: a DuckDB writer holds the file (%s). Retrying in %.0f s.",
                what,
                exc,
                BOOT_LOCK_RETRY_S,
            )
            sleep(BOOT_LOCK_RETRY_S)


def build_pitcher_engine(duckdb_path: str | None = None):
    """Construct and build the PitcherSimilarityEngine.

    The call is synchronous and CPU- and I/O-bound: ``engine.build()`` loads
    thousands of profiles from DuckDB and applies the shrinkage and the GMM
    standardization. The lifespan runs it under ``asyncio.to_thread(...)``, so
    the event loop stays free.

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
    Any exception ``PitcherSimilarityEngine.build()`` raises (most often
    ``duckdb.IOException``: the DuckDB file is missing, or a writer still holds
    it after the wait). The lifespan treats it as fatal.
    """
    # A lazy import, so importing this module needs no numpy or the other heavy
    # similarity dependencies.
    from similarity.engines.pitcher_similarity import PitcherSimilarityEngine

    path = duckdb_path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH

    log.info("Building PitcherSimilarityEngine from %s ...", path)
    engine = PitcherSimilarityEngine(duckdb_path=path)
    retry_on_duckdb_lock(engine.build, "the pitcher engine build")
    log.info(
        "PitcherSimilarityEngine ready: %d profiles loaded.",
        len(engine._profiles),  # noqa: SLF001 — the log line's profile count
    )
    return engine


# ---------------------------------------------------------------------------
# All eleven similarity engines
# ---------------------------------------------------------------------------

#: The eleven similarity engines: stable name -> (module path, class name). The
#: name is the contract: ``app.state.engines`` is keyed by it, so a route asks
#: for ``engines["batter"]`` without the import path. Every engine takes
#: ``duckdb_path=`` as its first keyword argument and has ``build()``, so the
#: builder treats them alike. The FAISS engines (situation, pitch_pitch,
#: batted_ball) take the same ``duckdb_path=``; their ``index_kind`` has a
#: working default.
ENGINE_REGISTRY: dict[str, tuple[str, str]] = {
    "pitcher": ("similarity.engines.pitcher_similarity", "PitcherSimilarityEngine"),
    "batter": ("similarity.engines.batter_similarity", "BatterSimilarityEngine"),
    "fielder": ("similarity.engines.fielder_similarity", "FielderSimilarityEngine"),
    "baserunner": ("similarity.engines.baserunner_similarity", "BaserunnerSimilarityEngine"),
    "baserunner_steal": (
        "similarity.engines.baserunner_steal_similarity",
        "BaserunnerStealSimilarityEngine",
    ),
    "catcher": ("similarity.engines.catcher_similarity", "CatcherSimilarityEngine"),
    "pitcher_steal": (
        "similarity.engines.pitcher_steal_similarity",
        "PitcherStealSimilarityEngine",
    ),
    "manager": ("similarity.engines.manager_similarity", "ManagerSimilarityEngine"),
    "situation": ("similarity.engines.situation_similarity", "SituationSimilarityEngine"),
    "pitch_pitch": ("similarity.engines.pitch_pitch_similarity", "PitchPitchSimilarityEngine"),
    "batted_ball": ("similarity.engines.batted_ball_similarity", "BattedBallSimilarityEngine"),
}


def _default_engine_loader(module_path: str, class_name: str):
    """Import ``class_name`` from ``module_path``.

    The import is lazy, so importing this module needs no numpy or faiss. A
    test replaces the whole load step with fakes through ``loader``."""
    import importlib

    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def build_all_engines(
    duckdb_path: str | None = None,
    *,
    registry: dict[str, tuple[str, str]] | None = None,
    loader=None,
):
    """Build all eleven similarity engines, keyed by their registry name.

    Each engine follows :func:`build_pitcher_engine`: the DuckDB path (the
    argument, else ``BASEBALL_DUCKDB_PATH``, else ``DEFAULT_DUCKDB_PATH``), the
    constructor with ``duckdb_path=``, then ``build()``, which loads the
    engine's profiles. The call is synchronous; the lifespan runs it under
    ``asyncio.to_thread(...)``.

    Each engine builds in its own try/except. An engine that fails to construct
    or to build (a missing or empty profile table, no faiss for a FAISS engine)
    is logged at WARNING and left out of the result, so one bad table never
    takes the API down. The result holds only the engines that built (0 to 11).

    The ``registry`` (name -> (module, class)) and the ``loader``
    (``(module, class) -> engine_class``) are parameters, so a unit test feeds
    fake engine classes and never touches DuckDB or the heavy modules.

    Parameters
    ----------
    duckdb_path : str, optional
        DuckDB path; same precedence as :func:`build_pitcher_engine`.
    registry : dict, optional
        Override the default :data:`ENGINE_REGISTRY` (for tests).
    loader : callable, optional
        ``(module_path, class_name) -> engine_class``. Defaults to
        :func:`_default_engine_loader`.

    Returns
    -------
    dict[str, object]
        ``{engine_name: built_engine}`` for every engine that built.
    """
    path = duckdb_path or os.environ.get("BASEBALL_DUCKDB_PATH") or DEFAULT_DUCKDB_PATH
    reg = registry if registry is not None else ENGINE_REGISTRY
    load = loader or _default_engine_loader

    engines: dict[str, Any] = {}
    for name, (module_path, class_name) in reg.items():
        try:
            engine_cls = load(module_path, class_name)
            engine = engine_cls(duckdb_path=path)
            retry_on_duckdb_lock(engine.build, f"the {name} engine build")
            engines[name] = engine
            log.info("Similarity engine '%s' built.", name)
        except Exception as exc:  # noqa: BLE001 — one bad engine must not stop the boot
            log.warning(
                "Skipping similarity engine '%s' — build failed (%s: %s).",
                name,
                type(exc).__name__,
                exc,
            )
    log.info("build_all_engines: %d/%d engines built.", len(engines), len(reg))
    return engines


# ---------------------------------------------------------------------------
# The calibration report
# ---------------------------------------------------------------------------

#: Env var pointing at a persisted CalibrationReport JSON (CalibrationReport.to_json).
CALIBRATION_REPORT_PATH_ENV = "CALIBRATION_REPORT_PATH"


def load_calibration_report(path: str | None = None):
    """Load a ``CalibrationReport`` from a JSON file, or None.

    It returns ``None`` when no path is given (and ``CALIBRATION_REPORT_PATH``
    is unset), when the file does not exist, and when the file is unreadable or
    malformed. Each case logs and none raises, so a missing or corrupt report
    leaves the identity calibration in place instead of stopping the boot.
    """
    report_path = path or os.environ.get(CALIBRATION_REPORT_PATH_ENV)
    if not report_path:
        return None
    if not os.path.exists(report_path):
        log.warning(
            "Calibration report path %s does not exist — using identity calibration.",
            report_path,
        )
        return None
    try:
        from similarity.calibration.report import CalibrationReport

        with open(report_path, encoding="utf-8") as fh:
            report = CalibrationReport.from_json(fh.read())
        log.info("Loaded calibration report from %s.", report_path)
        return report
    except Exception as exc:  # noqa: BLE001 — a bad report must not crash boot
        log.warning(
            "Failed to load calibration report from %s (%s: %s) — using identity calibration.",
            report_path,
            type(exc).__name__,
            exc,
        )
        return None


def apply_calibration_to_engines(engines: dict[str, Any] | None, report: Any) -> list[str]:
    """Apply a fitted ``CalibrationReport`` to every engine that supports it.

    Each engine with ``apply_calibration`` gets the report in its RBF and
    arsenal scorers, so it scores on the fitted curve (median similarity 0.50)
    rather than the module's literals (the calibration fit and apply,
    SIM-406). The FAISS and KD-tree distance engines (``situation``,
    ``pitch_pitch``, ``batted_ball``) have no RBF sigma and no such method;
    they are skipped.

    A failure on one engine logs and moves on, so one bad engine never blocks
    the boot. The function returns the names of the engines it calibrated. A
    ``None`` report (no fitted file) returns ``[]``, and the engines keep
    their module defaults.
    """
    if report is None:
        return []
    engines = engines or {}
    applied: list[str] = []
    for name, engine in engines.items():
        fn = getattr(engine, "apply_calibration", None)
        if not callable(fn):
            continue
        try:
            fn(report)
            applied.append(name)
        except Exception as exc:  # noqa: BLE001 — one bad engine must not block boot
            log.warning(
                "SIM-406: calibration apply failed for engine '%s' (%s: %s) — keeping defaults.",
                name,
                type(exc).__name__,
                exc,
            )
    log.info(
        "SIM-406: calibration applied to %d/%d engines: %s",
        len(applied),
        len(engines),
        ", ".join(sorted(applied)) or "(none)",
    )
    return applied


# ---------------------------------------------------------------------------
# The player name resolver
# ---------------------------------------------------------------------------


class NameResolver(Protocol):
    """An async callable: an iterable of player ids to ``{player_id: full_name}``.

    An unknown id is absent from the result; the caller falls back to a
    placeholder name.
    """

    async def __call__(self, ids: Iterable[int]) -> dict[int, str]: ...


def make_pg_name_resolver(pool: asyncpg.Pool) -> NameResolver:
    """Build a NameResolver over ``raw.players``.

    It matches the ids in one ``ANY($1::int[])`` query, so a similarity query
    that fans out to 2,400 pitchers costs one round trip. The closure is safe to
    attach to ``app.state`` and to call concurrently: an asyncpg pool is.
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
# The similarity payload cache
# ---------------------------------------------------------------------------


CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — per docs/similarity_visualization_spec.md §3


def make_cache_key(pitcher_id: int, season: int, bins: int, top_n: int) -> str:
    """The Redis key of a similarity-distribution payload.

    The spec fixes the format, so an invalidator can target ``simviz:*``.
    """
    return f"simviz:pitcher:{pitcher_id}:{season}:bins={bins}:top_n={top_n}"


class SimilarityCache(Protocol):
    """The async cache interface of the similarity route: ``get_json`` and ``set_json``."""

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl_s: int = CACHE_TTL_SECONDS) -> None: ...


class RedisSimilarityCache:
    """The Redis implementation: JSON strings under :func:`make_cache_key` keys.

    A value that does not decode reads as a miss and logs a WARNING, so a bad
    key never breaks the route.
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
    """Open a Redis client and wrap it in a SimilarityCache.

    It returns the client too, so the caller can close it at shutdown.
    """
    client = aioredis.from_url(redis_url, decode_responses=True)
    # A ping, so a misconfigured Redis fails at boot.
    await client.ping()  # type: ignore[misc]  # redis-py async ping stub typing
    return client, RedisSimilarityCache(client)


# ---------------------------------------------------------------------------
# The Postgres pool
# ---------------------------------------------------------------------------


async def open_pg_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    server_settings: dict[str, str] | None = None,
) -> asyncpg.Pool:
    """Open an asyncpg pool on a PostgreSQL DSN.

    The sizing is the live pipeline's
    (``LiveIngestionPipeline.start()``). ``server_settings`` sets session
    parameters on every connection of the pool: the read-only pool sets
    ``search_path`` to ``raw``.
    """
    pool = await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, server_settings=server_settings
    )
    if pool is None:
        # asyncpg.create_pool returns None only with init=False, which this
        # call never passes.
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool


def redact_dsn(dsn: str) -> str:
    """Return ``dsn`` with its password masked, for a log line.

    The result reads ``scheme://user:***@host:port/db``. Any query string is
    dropped, since it can carry a password too. A DSN that does not parse
    returns ``<redacted>``, so a malformed value never reaches the log.
    """
    try:
        parts = urlsplit(dsn)
        if not parts.scheme or not parts.hostname:
            return "<redacted>"
        user = parts.username or ""
        auth = f"{user}:***@" if parts.password is not None else (f"{user}@" if user else "")
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{auth}{parts.hostname}{port}{parts.path}"
    except Exception:  # noqa: BLE001 — a bad DSN must never raise in a log call
        return "<redacted>"
