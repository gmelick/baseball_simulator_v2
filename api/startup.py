"""
api/startup.py
==============
The startup and shutdown steps of the app's lifespan (``api/main.py``) that no
test patches.

The lifespan calls each step in turn. A step attaches its resource to
``app.state``; a step that fails degrades and logs, except the pitcher engine,
whose failure stops the boot. The steps log under ``api.main``, the logger of
the lifespan, so the boot reads as one log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from api.state import (
    apply_calibration_to_engines,
    build_all_engines,
    build_pitcher_engine,
    load_calibration_report,
)
from simulation.flags import read_int

log = logging.getLogger("api.main")


async def build_engines(app: FastAPI) -> None:
    """Build the pitcher engine and all eleven similarity engines.

    ``app.state.pitcher_engine`` serves the similarity route; a failure to
    build it stops the boot, so the route's contract holds.
    ``app.state.engines`` holds every engine by its registry name; an engine
    with a missing or empty profile table is logged and skipped.
    ``SIMILARITY_ENGINE_ENABLED=false`` skips both builds, so the stack can
    boot before the DuckDB profile file exists; the similarity routes then
    answer 503 and ``app.state.engines`` is empty.
    """
    engine_enabled = os.environ.get("SIMILARITY_ENGINE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )

    if engine_enabled:
        log.info("Building pitcher similarity engine (this may take a few seconds) ...")
        # A failure to build the pitcher engine stops the boot.
        app.state.pitcher_engine = await asyncio.to_thread(build_pitcher_engine)

        # All eleven engines, keyed by their ENGINE_REGISTRY name. An engine
        # with a missing or empty profile table is logged and skipped; its
        # failure never stops the boot.
        log.info("Building all similarity engines (SIM-361) ...")
        app.state.engines = await asyncio.to_thread(build_all_engines)
        log.info(
            "Similarity engines attached: %s",
            sorted(app.state.engines.keys()),
        )
    else:
        log.warning(
            "SIMILARITY_ENGINE_ENABLED is false — skipping engine build. "
            "The /api/similarity/* routes will return 503 until this is "
            "re-enabled and the DuckDB profile file exists."
        )
        # No engines: an empty dict, so a consumer can probe it safely.
        app.state.engines = {}


async def load_calibration(app: FastAPI) -> None:
    """Load the calibration report and apply it.

    The report (``CALIBRATION_REPORT_PATH``) loads once. It calibrates every
    engine that supports ``apply_calibration``, and its fitted reliability curve
    becomes the win-probability map (``app.state.calibration_map``). A missing or
    corrupt report leaves the engines on their defaults and the map at
    ``IDENTITY_CALIBRATION``; it never stops the boot.
    """
    from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap

    calibration_report = await asyncio.to_thread(load_calibration_report)
    app.state.calibration_report = calibration_report
    if calibration_report is not None:
        applied = apply_calibration_to_engines(app.state.engines, calibration_report)
        app.state.calibration_map = CalibrationMap.from_report(calibration_report)
        log.info(
            "SIM-406: applied fitted calibration to %d engines; win-prob map: %s",
            len(applied),
            app.state.calibration_map.name,
        )
    else:
        app.state.calibration_map = IDENTITY_CALIBRATION
        log.info(
            "No CalibrationReport found (CALIBRATION_REPORT_PATH unset/missing) — "
            "engines on locked defaults, win-prob map: %s",
            app.state.calibration_map.name,
        )


def open_assistant_client(app: FastAPI) -> None:
    """Create the Data Lab assistant's client when ``ANTHROPIC_API_KEY`` is set.

    Without the key, or when the client fails to start,
    ``app.state.anthropic_client`` stays None: the assistant route answers 503
    and ``/api/assistant/status`` reports ``configured: false``.
    """
    app.state.anthropic_client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            app.state.anthropic_client = anthropic.AsyncAnthropic()
            log.info(
                "SIM-439: AI assistant enabled (model=%s).",
                os.environ.get("ASSISTANT_MODEL", "claude-sonnet-5"),
            )
        except Exception as exc:  # noqa: BLE001 — a bad SDK/key must not crash boot
            log.warning(
                "SIM-439: ANTHROPIC_API_KEY set but client init failed (%s: %s); assistant disabled.",
                type(exc).__name__,
                exc,
            )
    else:
        log.info(
            "SIM-439: ANTHROPIC_API_KEY unset — AI assistant disabled "
            "(SQL console + analytics still work)."
        )


async def open_read_only_pool(app: FastAPI, open_pg_pool: Callable[..., Awaitable[Any]]) -> None:
    """Open the read-only pool, check its role, and keep it only when the role is safe.

    The pool (``BASEBALL_DB_RO_DSN``, the ``baseball_ro`` role) serves the SQL
    console, the assistant and the Data Lab read routes: schema, analytics,
    players and data freshness. No route falls back to the main pool. The
    lifespan passes its own ``open_pg_pool``. A superuser, a role that can
    write, or a failed open or check leaves ``app.state.pg_pool_ro`` None, and
    those routes answer 503.
    """
    from api.routes.sql_safety import ReadOnlyRoleCheck, check_read_only_role
    from api.state import redact_dsn

    app.state.pg_pool_ro = None
    app.state.sql_console_role = None
    ro_dsn = os.environ.get("BASEBALL_DB_RO_DSN", "").strip()
    if not ro_dsn:
        log.warning(
            "SIM-443e: BASEBALL_DB_RO_DSN is unset; the SQL console, the assistant and the "
            "Data Lab read routes (schema, analytics, players, data freshness) answer 503."
        )
    else:
        ro_pool = None
        role_check: ReadOnlyRoleCheck | None = None
        try:
            ro_pool = await open_pg_pool(ro_dsn, server_settings={"search_path": "raw"})
            role_check = await check_read_only_role(ro_pool)
        except Exception as exc:  # noqa: BLE001 — a driver message can echo the DSN
            log.error(
                "SIM-443e: the read-only pool failed to open or to check its role "
                "(%s); the SQL console, the assistant and the Data Lab read routes "
                "(schema, analytics, players, data freshness) answer 503.",
                type(exc).__name__,
            )
            role_check = None
        if role_check is not None and not role_check.acceptable:
            log.error(
                "SIM-443e: refused the read-only pool: role %s is not read-only "
                "(%s); the SQL console, the assistant and the Data Lab read routes "
                "(schema, analytics, players, data freshness) answer 503.",
                role_check.role,
                role_check.flags(),
            )
        if role_check is None or not role_check.acceptable:
            if ro_pool is not None:
                try:
                    await ro_pool.close()
                except Exception:  # noqa: BLE001
                    pass
        else:
            app.state.pg_pool_ro = ro_pool
            app.state.sql_console_role = role_check.role
            log.info(
                "SIM-443e: read-only pool open as %s (superuser=False) at %s; it serves "
                "the SQL console, the assistant and the Data Lab read routes "
                "(schema, analytics, players, data freshness)",
                role_check.role,
                redact_dsn(ro_dsn),
            )
            if not role_check.raw_select:
                log.warning(
                    "SIM-443e: role %s cannot SELECT raw.pitches; console queries "
                    "will fail until the grants are applied (make db-roles).",
                    role_check.role,
                )


async def start_live_feed(app: FastAPI, *, dsn: str, redis_url: str) -> None:
    """Start the live pipeline in this process, or bridge the live service's messages.

    ``LIVE_PIPELINE_ENABLED`` (default off) starts the live ingestion pipeline
    here; its ``simulation_callback`` records each plate appearance's signal on
    ``app.state.last_resim_signal``. On the compose stack the flag stays off:
    the ``live`` service runs the pipeline in its own container and publishes
    on Redis, and the bridge (``app.state.live_bridge``) forwards a game's
    messages to the browsers watching it. The WebSocket and odds routers are
    mounted either way (``create_app``).
    """
    pipeline_enabled = os.environ.get("LIVE_PIPELINE_ENABLED", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )

    if pipeline_enabled:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        async def _resim_signal(game_pk: int, game_state: dict) -> None:
            # The re-sim hook records the signal; no simulation runs from it.
            log.info(
                "Re-sim signal received: game %s | inning %s | score %s-%s",
                game_pk,
                game_state.get("inning"),
                game_state.get("home_score"),
                game_state.get("away_score"),
            )
            app.state.last_resim_signal = {"game_pk": game_pk, "game_state": game_state}
            # The freshness gauge (baseball_sim_pipeline_freshness_seconds)
            # reads this Unix time as the age of the last signal.
            app.state.last_resim_signal_ts = time.time()

        log.info("Starting live ingestion pipeline (LIVE_PIPELINE_ENABLED=true) ...")
        pipeline = LiveIngestionPipeline(
            dsn=dsn,
            redis_url=redis_url,
            simulation_callback=_resim_signal,
        )
        await pipeline.start()
        app.state.pipeline = pipeline
    else:
        log.info(
            "LIVE_PIPELINE_ENABLED is false — ws_router/odds_router are mounted "
            "but the background live ingestion pipeline is NOT started."
        )
        # The live service runs in its own container and publishes on Redis;
        # this bridge forwards a game's messages to the browsers watching it.
        # Never set LIVE_PIPELINE_ENABLED on the compose stack: the `live`
        # service is the one publisher.
        from pipeline.live.broadcast import RedisBridge
        from pipeline.live.live_ingestion_pipeline import connection_manager

        app.state.live_bridge = RedisBridge(app.state.redis_client, connection_manager.broadcast)


async def build_sim_runner(app: FastAPI) -> None:
    """Build the one long-lived BatchRunner, ``app.state.sim_runner``.

    The routes reuse its warm worker pool and its shared-memory arrays across
    requests. It reuses ``app.state.sim_cache`` when ``create_app`` attached
    one. ``SIM_RUNNER_WORKERS`` sets the worker count; unset, the runner uses
    ``default_max_workers()``, and ``1`` keeps the run in this process.

    The engine-artifact bundle's version is pinned before the pool starts (the
    forkserver's workers inherit the environment), so a worker that respawns
    after a rebuild reads the same version as the shared arrays. When the bundle
    loads, its large arrays are published to shared memory once; when it does
    not, each worker loads the bundle itself and fails at its first game with
    no bundle (there is no fallback simulator).
    """
    from simulation.batch_runner import BatchRunner, default_max_workers, make_cache

    sim_cache = getattr(app.state, "sim_cache", None) or make_cache()
    # SIM_RUNNER_WORKERS unset gives None: BatchRunner.resolve_max_workers()
    # then uses default_max_workers() = min(cpu_count - 1, 10) at run time. An
    # explicit "1" keeps the run in this process, for a host that cannot fork.
    sim_runner_workers = read_int("SIM_RUNNER_WORKERS")
    resolved_workers = (
        sim_runner_workers if sim_runner_workers is not None else default_max_workers()
    )

    # The bundle loads once here, and its large arrays go to the BatchRunner,
    # which publishes them to shared memory without a copy; each worker splices
    # them in (simulation.production_factory._build_full_pool_sampler). With no
    # bundle (a no-DB test, a host with no built bundle) nothing is published,
    # and each worker loads the bundle itself, failing at its first game when
    # there is none.
    #
    # The bundle is a link to a published version. The version this boot loads
    # is pinned before the worker pool starts (the forkserver's workers inherit
    # the environment), so a worker that respawns after a rebuild reads the
    # same version as the shared arrays. The shutdown undoes the pin.
    from api.bundle_watch import PINNED_ENV, bundle_link, pin_bundle_version

    app.state.bundle_pin_prev_env = os.environ.get(PINNED_ENV)
    app.state.bundle_version = None
    app.state.bundle_in_use_marker = None
    if os.path.isdir(bundle_link()):
        app.state.bundle_version, app.state.bundle_in_use_marker = pin_bundle_version()
    shared_arrays_payload: dict | None = None
    try:
        from pipeline.batch.engine_artifacts import EngineArtifacts

        art_dir = app.state.bundle_version or bundle_link()
        log.info(
            "SIM-403b: loading engine artifacts for shared-memory publish (%s) ...",
            art_dir,
        )
        art = await asyncio.to_thread(EngineArtifacts.load, art_dir)
        shared_arrays_payload = art.extract_shared_arrays()
        total_mb = sum(a.nbytes for a in shared_arrays_payload.values()) / (1024 * 1024)
        log.info(
            "SIM-403b: publishing %d engine-artifact arrays (%.1f MB) into shared memory",
            len(shared_arrays_payload),
            total_mb,
        )
    except Exception as exc:  # noqa: BLE001
        # A missing or corrupt bundle never stops the boot (the health and
        # data routes still serve); the runner starts without shared arrays.
        log.warning(
            "SIM-403b: engine-artifact preload failed (%s: %s); "
            "workers will disk-load per process.",
            type(exc).__name__,
            exc,
        )
        shared_arrays_payload = None

    log.info(
        "Building persistent BatchRunner (workers=%s, SIM_RUNNER_WORKERS=%r, shared_arrays=%d) ...",
        resolved_workers,
        sim_runner_workers,
        len(shared_arrays_payload) if shared_arrays_payload else 0,
    )
    app.state.sim_runner = BatchRunner(
        cache=sim_cache,
        max_workers=sim_runner_workers,
        shared_arrays=shared_arrays_payload,
    )


def start_bundle_watch(app: FastAPI) -> None:
    """Restart the app when the nightly rebuild publishes a new bundle version.

    With ``SIM_RESTART_ON_NEW_BUNDLE`` on and a pinned version, a task watches
    the bundle link (``api.bundle_watch.watch_for_new_bundle``).
    """
    from api.bundle_watch import bundle_link, restart_enabled, watch_for_new_bundle

    app.state.bundle_watch_task = None
    if restart_enabled() and app.state.bundle_version:
        jobs = getattr(app.state, "sim_jobs", None)
        app.state.bundle_watch_task = asyncio.create_task(
            watch_for_new_bundle(
                link=bundle_link(),
                loaded=app.state.bundle_version,
                active_jobs=(lambda: jobs.active_count()) if jobs is not None else (lambda: 0),
            )
        )
        log.info("SIM-563: watching %s for a new bundle version.", bundle_link())


async def close_resources(app: FastAPI) -> None:
    """Close what the startup steps opened, in reverse order of acquisition.

    The lifespan closes the park-factor source after this. Each close is
    guarded, so one failure does not stop the rest.
    """
    # Stop the bundle watch; release this boot's version and its pin.
    watch_task = getattr(app.state, "bundle_watch_task", None)
    if watch_task is not None and not watch_task.done():
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    from api.bundle_watch import PINNED_ENV as _PINNED_ENV
    from pipeline.batch.bundle_versions import unmark_in_use

    unmark_in_use(getattr(app.state, "bundle_in_use_marker", None))
    if getattr(app.state, "bundle_version", None):
        prev = getattr(app.state, "bundle_pin_prev_env", None)
        if prev is None:
            os.environ.pop(_PINNED_ENV, None)
        else:
            os.environ[_PINNED_ENV] = prev

    # Stop awaiting the background pre-warm if it still runs. Its worker
    # threads finish on their own; the cancel only releases the await, so the
    # shutdown does not wait, and close() (below) tears the pool and the shared
    # memory down.
    prewarm_task = getattr(app.state, "prewarm_task", None)
    if prewarm_task is not None and not prewarm_task.done():
        prewarm_task.cancel()
        try:
            await prewarm_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    # The shared BatchRunner: close() shuts its warm pool down (wait=True) and
    # unlinks its shared-memory segments. It is idempotent and a no-op when no
    # pooled run happened.
    if getattr(app.state, "sim_runner", None) is not None:
        try:
            app.state.sim_runner.close()
        except Exception:  # noqa: BLE001
            pass

    # The run jobs (the next boot recovers their rows).
    if getattr(app.state, "sim_jobs", None) is not None:
        try:
            await app.state.sim_jobs.close()
        except Exception:  # noqa: BLE001
            pass

    # The live bridge's subscriptions.
    if getattr(app.state, "live_bridge", None) is not None:
        try:
            await app.state.live_bridge.close()
        except Exception:  # noqa: BLE001
            pass

    # The league reader's HTTP session.
    if getattr(app.state, "league_feed", None) is not None:
        try:
            await app.state.league_feed.close()
        except Exception:  # noqa: BLE001
            pass

    # The Redis client, then the pools. The engines need no close.
    if hasattr(app.state, "redis_client"):
        await app.state.redis_client.aclose()
    # The read-only pool before the main pool.
    if getattr(app.state, "pg_pool_ro", None) is not None:
        try:
            await app.state.pg_pool_ro.close()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(app.state, "pg_pool"):
        await app.state.pg_pool.close()

    # The in-process live pipeline, when one started.
    if hasattr(app.state, "pipeline"):
        await app.state.pipeline.stop()

    # The replay store.
    if getattr(app.state, "replay_duckdb", None) is not None:
        try:
            app.state.replay_duckdb.close()
        except Exception:  # noqa: BLE001
            pass
