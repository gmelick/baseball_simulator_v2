"""
api/main.py
===========
MLB Baseball Simulation Platform — FastAPI Application Entry Point

Current wiring:
  - Lifespan opens the asyncpg pool, the Redis client + cache, and
    builds the PitcherSimilarityEngine (Backend Developer slice of
    Phase 5; spec: docs/similarity_visualization_spec.md).
  - Similarity Explorer router is registered and serves real engine
    output against the live DuckDB profiles.

Routers added here as phases complete:
  Phase 5: simulation runner, game state, managerial override
  Phase 6: frontend serving
  Phase 7: monitoring, deployment hardening
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.auth import LatencyMiddleware, RateLimitMiddleware, resolve_cors_origins
from api.errors import install_exception_handlers
from api.state import (
    apply_calibration_to_engines,
    build_all_engines,
    build_pitcher_engine,
    load_calibration_report,
    make_pg_name_resolver,
    open_pg_pool,
    open_redis_cache,
)

log = logging.getLogger("api.main")

# ---------------------------------------------------------------------------
# Environment validation (SIM-153)
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = [
    "BASEBALL_DB_DSN",
    "REDIS_URL",
]

# SIM-389: auth vars are only required outside development so `make test-unit`
# and local dev boot without ceremony.  In any other ENVIRONMENT value they must
# be set — validate_environment() enforces this.
_REQUIRED_AUTH_ENV_VARS = [
    "SECRET_KEY",  # HMAC signing key for session tokens
    "AUTH_PASSWORD",  # shared browser login password
]


def validate_environment() -> None:
    """
    Asserts all required environment variables are present at startup.
    Raises RuntimeError with a clear message listing what is missing.
    Called inside the lifespan so a misconfigured container fails fast
    rather than silently running with broken connections.

    Auth variables (SECRET_KEY, AUTH_PASSWORD) are only required outside
    the ``development`` environment — local dev + the test suite boot without
    them, falling back to insecure dev sentinels that are logged as warnings.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in the required values."
        )

    env = os.environ.get("ENVIRONMENT", "development").strip().lower()
    if env != "development":
        missing_auth = [v for v in _REQUIRED_AUTH_ENV_VARS if not os.environ.get(v)]
        if missing_auth:
            raise RuntimeError(
                f"Missing required auth variable(s) for ENVIRONMENT={env!r}: "
                f"{', '.join(missing_auth)}. "
                f"Set these before deploying outside development."
            )


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


async def _background_prewarm(runner, pool_dir: str) -> None:
    """SIM-402: warm the sim-runner workers OFF the startup path.

    Scheduled as a background task by the lifespan and NEVER awaited before
    ``yield``, so a slow / stalled / OOM'd warm can't wedge startup (a blocking
    pre-warm, even wrapped in ``asyncio.wait_for``, was observed to hang the
    lifespan — ``wait_for`` can't interrupt a synchronous thread blocked in
    multiprocessing C-calls).  Logs the outcome; any failure leaves the lazy
    per-game warm-up as the fallback.
    """
    try:
        n_warm = await asyncio.to_thread(runner.prewarm, pool_dir)
        log.info("SIM-402: pre-warmed %d sim-runner worker(s).", n_warm)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "SIM-402: background pre-warm failed (%s: %s); workers will warm lazily.",
            type(exc).__name__,
            exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    On startup:
      - Validates environment variables (SIM-153)
      - Opens the asyncpg Postgres pool
      - Opens the aioredis client + Similarity payload cache
      - Builds the PitcherSimilarityEngine (under asyncio.to_thread
        so the event loop stays responsive during the multi-second
        DuckDB load)

    On shutdown:
      - Closes the Redis client and the Postgres pool in reverse order.
    """
    # Validate env at boot; fail fast on misconfiguration
    validate_environment()

    log.info(
        "MLB Simulation Platform starting — environment: %s",
        os.environ.get("ENVIRONMENT", "development"),
    )

    # ----------------------------------------------------------------
    # Similarity Explorer resources (v1: pitcher engine).
    # Spec: docs/similarity_visualization_spec.md.
    #
    # The three attributes set on app.state are the contract consumed
    # by api/routes/similarity.py:
    #   - pitcher_engine          (built PitcherSimilarityEngine)
    #   - player_name_resolver    (async: pitcher_ids -> {id: name})
    #   - similarity_cache        (Redis-backed TTL cache; optional —
    #                              route degrades gracefully if missing)
    #
    # Failures here are fatal — same fail-fast posture as
    # validate_environment(). A misconfigured DuckDB path or
    # unreachable Postgres should crash boot loudly rather than serve
    # 5xxs at request time.
    # ----------------------------------------------------------------
    dsn = os.environ["BASEBALL_DB_DSN"]
    redis_url = os.environ["REDIS_URL"]

    log.info("Opening Postgres pool ...")
    app.state.pg_pool = await open_pg_pool(dsn)
    app.state.player_name_resolver = make_pg_name_resolver(app.state.pg_pool)

    log.info("Opening Redis cache ...")
    app.state.redis_client, app.state.similarity_cache = await open_redis_cache(redis_url)

    # Dev-onboarding escape hatch: SIMILARITY_ENGINE_ENABLED=false skips the
    # engine build entirely so the stack can boot before the DuckDB profile
    # file exists (i.e. before the multi-hour ETL has run). When skipped,
    # ``app.state.pitcher_engine`` is left unset and the similarity route
    # returns 503 "engine warming" — which is exactly its documented
    # contract for that condition.
    engine_enabled = os.environ.get("SIMILARITY_ENGINE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )

    if engine_enabled:
        log.info("Building pitcher similarity engine (this may take a few seconds) ...")
        # The pitcher engine keeps its FAIL-FAST posture: a build failure here
        # crashes boot loudly so the similarity route's contract is unchanged.
        app.state.pitcher_engine = await asyncio.to_thread(build_pitcher_engine)

        # ------------------------------------------------------------
        # SIM-361: build ALL 11 similarity engines and attach them as
        # app.state.engines (keyed by ENGINE_REGISTRY name). build_all_engines
        # is per-engine resilient — an engine with a missing/empty profile table
        # is logged + skipped, so the *additional* engines degrade gracefully and
        # never take down boot the way the pitcher engine (above) intentionally
        # does. app.state.pitcher_engine is preserved (set above) for the
        # existing similarity route's backward-compat; the built dict's "pitcher"
        # entry is the same contract under the new keying.
        # ------------------------------------------------------------
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
        # No engines built; expose an empty dict so consumers can probe safely.
        app.state.engines = {}

    # ----------------------------------------------------------------
    # SIM-361: win-probability calibration map.
    #
    # Load a persisted CalibrationReport from CALIBRATION_REPORT_PATH (if set
    # and readable) and turn its fitted reliability curve into a CalibrationMap
    # for the win_probability(...) transform. Best-effort: a missing / corrupt
    # report, or a report with no fitted curve, degrades to IDENTITY_CALIBRATION
    # (the "well-calibrated until a curve says otherwise" default) — never a boot
    # failure. Always attached so consumers can read app.state.calibration_map
    # unconditionally.
    # ----------------------------------------------------------------
    # SIM-406: load the persisted CalibrationReport ONCE, then both
    #   (a) APPLY it to every similarity engine that supports apply_calibration
    #       (the engine-layer similarity calibration — closes the audit gap that
    #       calibration was wired only on the pitcher engine), and
    #   (b) derive the win-probability CalibrationMap from its fitted reliability
    #       curve (SIM-361).
    # Best-effort throughout: a missing/corrupt report degrades to identity win-prob
    # + uncalibrated engine defaults — never a boot failure.
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

    # ----------------------------------------------------------------
    # Phase 5 (SIM-354): live ingestion pipeline.
    #
    # Gated behind LIVE_PIPELINE_ENABLED (default FALSE) so importing/booting
    # the app — including the test suite and `create_app()` import-time wiring —
    # never requires a live MLB WebSocket, the MLB Stats REST API, or any other
    # external connection. The ws_router / odds_router are ALWAYS registered in
    # create_app() (route registration needs no live connection); only the
    # background poller + WS subscriptions are started here when enabled.
    #
    # The pipeline's simulation_callback is the Phase-5 re-sim seam: it fires
    # at the end of every plate appearance via
    # LiveIngestionPipeline._signal_resimulation(game_pk, game_state). We wire a
    # default async hook that logs the signal and stashes the latest state on
    # app.state.last_resim_signal; the Phase-5 simulation runner replaces this
    # callback with the real re-sim trigger once that slice lands.
    # ----------------------------------------------------------------
    pipeline_enabled = os.environ.get("LIVE_PIPELINE_ENABLED", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )

    if pipeline_enabled:
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        async def _resim_signal(game_pk: int, game_state: dict) -> None:
            # Phase-5 seam: the simulation runner will replace this hook with a
            # real re-sim trigger. Until then, record the signal so /ready and
            # tests can observe that the wiring fired.
            log.info(
                "Re-sim signal received: game %s | inning %s | score %s-%s",
                game_pk,
                game_state.get("inning"),
                game_state.get("home_score"),
                game_state.get("away_score"),
            )
            app.state.last_resim_signal = {"game_pk": game_pk, "game_state": game_state}
            # SIM-410 metrics: the freshness gauge reads last_resim_signal_ts (a unix
            # ts); stamp it here so baseball_sim_pipeline_freshness_seconds reports the
            # real age of the last signal instead of always -1 ("no data").
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

    # ----------------------------------------------------------------
    # Phase 5 (SIM-357): replay-persistence DuckDB connection.
    #
    # Gated behind REPLAY_PERSISTENCE_ENABLED (default FALSE). When enabled,
    # open a writable DuckDB connection (the sim.play_stream / sim.state_snapshots
    # store, schema v9) and attach it as app.state.sim_duckdb so /simulate can
    # persist a representative game's play-by-play + per-pitch snapshots and
    # /plays + /state/{at_bat}/{pitch} can serve them. Best-effort: a failure to
    # open never blocks boot — the replay endpoints simply return 503 and
    # /simulate's persistence step skips silently. Default-off avoids DuckDB's
    # single-writer constraint clashing with the read-only similarity engine
    # until the deployment wires a dedicated replay DuckDB file.
    # ----------------------------------------------------------------
    replay_enabled = os.environ.get("REPLAY_PERSISTENCE_ENABLED", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )

    if replay_enabled:
        try:
            import duckdb

            duckdb_path = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
            log.info("Opening replay-persistence DuckDB at %s ...", duckdb_path)
            app.state.sim_duckdb = await asyncio.to_thread(duckdb.connect, duckdb_path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "REPLAY_PERSISTENCE_ENABLED but DuckDB open failed (%s); "
                "/plays + /state will return 503 and /simulate persistence is skipped.",
                type(exc).__name__,
            )

    # ----------------------------------------------------------------
    # Phase 5 (SIM-360): the long-lived, persistent BatchRunner.
    #
    # Build ONE BatchRunner at startup and attach it as app.state.sim_runner so
    # the /simulate handlers REUSE a single warm ProcessPoolExecutor (and the
    # SIM-333 shared-mem segments, published once) across requests rather than
    # forking + publishing/unlinking per call (the SIM-332 one-shot lifecycle).
    #
    # The runner reuses the cache create_app() already attached
    # (app.state.sim_cache) when present, else makes its own. SIM_RUNNER_WORKERS
    # (env) overrides the worker count; default 1 keeps the in-process,
    # deterministic path on (no fork until a deployment opts into a pooled count),
    # so this is always safe to create -- a BatchRunner with no pooled run yet
    # holds no resources, and close() on shutdown is a no-op in that case. The
    # /simulate handler still offloads the run via asyncio.to_thread.
    # ----------------------------------------------------------------
    from simulation.batch_runner import BatchRunner, default_max_workers, make_cache

    sim_cache = getattr(app.state, "sim_cache", None) or make_cache()
    # SIM-403: SIM_RUNNER_WORKERS unset → None → BatchRunner.resolve_max_workers()
    # uses default_max_workers() = min(cpu_count - 1, 10) at run time, enabling
    # real parallelism.  Explicit "1" keeps the serialised in-process path for
    # environments that can't fork (e.g. some CI runners).
    _workers_env = os.environ.get("SIM_RUNNER_WORKERS")
    try:
        sim_runner_workers: int | None = int(_workers_env) if _workers_env else None
    except (TypeError, ValueError):
        sim_runner_workers = None
    resolved_workers = (
        sim_runner_workers if sim_runner_workers is not None else default_max_workers()
    )

    # SIM-403b: when the full-pool path is enabled AND the engine-artifact bundle
    # is on disk, load it ONCE here and extract the big shareable numpy arrays
    # so BatchRunner can publish them into ``multiprocessing.shared_memory``
    # zero-copy. The worker-side splice happens in
    # :func:`simulation.production_factory._build_full_pool_sampler`. If the
    # bundle isn't present (no-DB tests, sandbox without a built artifact dir),
    # we silently skip — the per-worker disk load still works for the per-tile
    # fallback path and the full-pool path runs on the disk-loaded arrays.
    shared_arrays_payload: dict | None = None
    _full_pool_env = os.environ.get("SIM_FULL_POOL", "").strip().lower()
    if _full_pool_env not in ("", "0", "false", "no", "off"):
        try:
            from pipeline.batch.engine_artifacts import EngineArtifacts

            pool_dir = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
            art_dir = os.path.join(pool_dir, "engine_artifacts")
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
            # Defensive: a missing/corrupt artifact dir must not prevent the
            # API from starting (the per-tile fallback still works). Log and
            # move on without shared arrays.
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
        _workers_env,
        len(shared_arrays_payload) if shared_arrays_payload else 0,
    )
    app.state.sim_runner = BatchRunner(
        cache=sim_cache,
        max_workers=sim_runner_workers,
        shared_arrays=shared_arrays_payload,
    )

    # SIM-402: pre-warm the pool so the first /simulate hits WARM workers (a fresh
    # n-iteration request otherwise spreads its games one-per-worker, so every worker
    # is cold on that first batch and pays the full-pool warm-up on the request path).
    # Run it in the BACKGROUND — never block startup on it. A blocking pre-warm (even
    # wrapped in asyncio.wait_for) was observed to hang the lifespan indefinitely:
    # wait_for cannot interrupt a synchronous thread blocked in multiprocessing
    # C-calls, so a stalled / OOM'd warm wedged startup. As a background task the app
    # yields + serves immediately; workers warm behind it (in bounded-concurrency
    # waves, see BatchRunner.prewarm) and any failure degrades to the lazy per-game
    # warm-up.
    if _full_pool_env not in ("", "0", "false", "no", "off"):
        prewarm_pool_dir = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
        log.info("SIM-402: scheduling background sim-runner pre-warm ...")
        app.state.prewarm_task = asyncio.create_task(
            _background_prewarm(app.state.sim_runner, prewarm_pool_dir)
        )

    yield

    log.info("MLB Simulation Platform shutting down.")

    # SIM-402: stop awaiting the background pre-warm if it's still running. Its worker
    # threads are detached and finish on their own; cancelling just releases our await
    # so shutdown isn't blocked, and close() (below) tears the pool + shm down.
    prewarm_task = getattr(app.state, "prewarm_task", None)
    if prewarm_task is not None and not prewarm_task.done():
        prewarm_task.cancel()
        try:
            await prewarm_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    # Phase 5 (SIM-360): shut the persistent BatchRunner down first (reverse
    # order of acquisition) — close() shutdown(wait=True)s its warm pool and
    # unlinks the SIM-333 shared-mem segments. Idempotent + a no-op if no pooled
    # run ever happened, so this never raises even on a clean (unused) boot.
    if getattr(app.state, "sim_runner", None) is not None:
        try:
            app.state.sim_runner.close()
        except Exception:  # noqa: BLE001
            pass

    # Reverse order of acquisition for shutdown — engine has no
    # explicit close, just drop the reference.
    if hasattr(app.state, "redis_client"):
        await app.state.redis_client.aclose()
    if hasattr(app.state, "pg_pool"):
        await app.state.pg_pool.close()

    # ----------------------------------------------------------------
    # Phase 5 (SIM-354): cleanly stop the pipeline if it was started.
    # ----------------------------------------------------------------
    if hasattr(app.state, "pipeline"):
        await app.state.pipeline.stop()

    # Phase 5 (SIM-357): close the replay-persistence DuckDB connection.
    if getattr(app.state, "sim_duckdb", None) is not None:
        try:
            app.state.sim_duckdb.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="MLB Baseball Simulation Platform",
        description=(
            "Pitch-by-pitch MLB game simulator powered by Statcast data and "
            "multi-dimensional similarity engines. Full API available in Phase 5."
        ),
        version="0.1.0-phase7",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS (SIM-351) — env-driven allowlist. CORS_ORIGINS (comma-separated)
    # wins; else FRONTEND_URL; else a safe localhost default. The "*" wildcard
    # is reachable ONLY under ENVIRONMENT=development, so production is never
    # ["*"] while allow_credentials=True (a spec violation browsers reject).
    # See api/auth.resolve_cors_origins for the precedence rules.
    origins = resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiting (SIM-351) — lightweight in-memory sliding window keyed by
    # API key / client IP. Disabled by default (RATE_LIMIT_PER_MINUTE unset/<=0)
    # so dev + the test suite are unaffected; opt in via RATE_LIMIT_PER_MINUTE.
    # /health, /ready, / are exempt inside the middleware.
    app.add_middleware(RateLimitMiddleware)

    # p95 latency tracking (SIM-410) — rolling 200-request window; stores the
    # computed p95 on app.state.api_p95_seconds so /metrics can expose it to
    # Prometheus/Grafana. /health, /ready, /, /metrics are exempt.
    app.add_middleware(LatencyMiddleware)

    # ----------------------------------------------------------------
    # Routers — registered as phases complete
    # ----------------------------------------------------------------
    # SIM-389: Browser session auth (login / me / logout).
    # Registered FIRST so the /auth/* routes are always resolvable
    # regardless of other router registration order.
    from api.routes.auth import router as auth_router

    app.include_router(auth_router)

    # Similarity Score Explorer (v1: pitcher engine).
    # Spec: docs/similarity_visualization_spec.md. The lifespan above
    # attaches the engine, name resolver, and Redis cache to app.state.
    from api.routes.similarity import router as similarity_router

    app.include_router(similarity_router)

    # Phase 5 (SIM-355/358/359): game simulation router.
    #   GET  /api/games/{date}                            -- games on a date
    #   GET  /api/games/{game_pk}/simulate                -- Monte-Carlo summary
    #   POST /api/games/{game_pk}/simulate/with_override  -- baseline vs override
    # Registered unconditionally -- route registration needs no live DB/Redis.
    # The handlers read app.state.pg_pool + app.state.sim_cache (attached below /
    # in the lifespan); a missing pool returns 503 from the route itself.
    #
    # SIM-359: attach a sim-result cache to app.state at factory time so the
    # BatchRunner the games router builds memoizes summaries (keyed on
    # spec+seed+N at SIM_RESULT_TTL_S) AND the date listing (at POOL_QUERY_TTL_S).
    # make_cache() is no-op-safe: it returns a RedisCache only when a server
    # answers ping(), else an in-process InMemoryCache -- so this never requires
    # Redis and the test suite runs on the in-memory fallback with no server.
    from api.routes.games import router as games_router
    from simulation.batch_runner import make_cache

    app.state.sim_cache = make_cache()
    app.include_router(games_router)

    # Phase 5 (SIM-367/368/369): betting API surface.
    #   GET /api/betting/games/{game_pk}/edges          -- per-market edge reports
    #   GET /api/betting/games/{game_pk}/signals        -- ranked +EV bet signals
    #   GET /api/betting/games/{game_pk}/line-movement  -- opening->closing series
    #   GET /api/betting/games/{game_pk}/clv            -- entry-vs-close snapshot
    # Registered unconditionally -- route registration needs no live DB/Redis. The
    # edge/signal handlers reuse the games router's sim seam (factory ref + the
    # BatchRunner + sim cache attached above); line-movement/clv read raw.game_odds
    # off app.state.pg_pool (503 without a pool). Odds for edges/signals are
    # injected via query params or fall back to the deterministic mock provider.
    from api.routes.betting import router as betting_router

    app.include_router(betting_router)

    # SIM-417: data-freshness / health surface for the UI.
    #   GET /api/data/freshness — ingest watermark + per-season coverage.
    # Reads app.state.pg_pool (503 without a pool); aggregate counts only.
    from api.routes.data_health import router as data_health_router

    app.include_router(data_health_router)

    # Phase 5 (SIM-354): live ingestion routers.
    #   ws_router   — WebSocket /ws/games/{game_pk} (frontend live subscriptions)
    #   odds_router — REST /api/odds/{game_pk}, /api/odds/today/all
    # These are registered unconditionally — route registration requires no live
    # DB/Redis/WS connection. The background pipeline that *feeds* the WS clients
    # is started in lifespan only when LIVE_PIPELINE_ENABLED=true.
    from pipeline.live.live_ingestion_pipeline import odds_router, ws_router

    app.include_router(ws_router)
    app.include_router(odds_router)

    # Phase 5 (SIM-374): Prometheus metrics endpoint.
    #   GET /metrics — application metrics in Prometheus text exposition format
    #                  (text/plain; version=0.0.4) for the deploy/monitoring/
    #                  Prometheus scrape + Grafana dashboards.
    # Registered unconditionally — import-safe and connection-free: the handler
    # reads only app.state scalars (no DB/Redis), and prometheus_client is an
    # OPTIONAL dependency (the route falls back to a hand-rolled exposition when
    # it is not installed), so this never affects boot or the test suite.
    from api.routes.metrics import router as metrics_router

    app.include_router(metrics_router)
    # ----------------------------------------------------------------

    # ---- SIM-381: Serve the Vite-built SPA -----------------------------------
    # In production, nginx serves frontend/dist/ directly (see deploy/nginx/nginx.conf).
    # This mount is a dev/staging convenience: when running uvicorn directly,
    # the built assets are served from FastAPI so the app is fully self-contained.
    #
    # The mount is CONDITIONAL on the dist directory existing so the app boots
    # cleanly before the first `npm run build` (CI / fresh clone).
    #
    # Mount order matters: API routes above are registered first, so the
    # `/{full_path:path}` SPA catch-all only fires when nothing else matches.
    _spa_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if _spa_dist.is_dir():
        _spa_assets = _spa_dist / "assets"
        if _spa_assets.is_dir():
            # Hashed filenames from Vite → safe for immutable caching in prod.
            app.mount(
                "/assets",
                StaticFiles(directory=str(_spa_assets)),
                name="spa-assets",
            )

        @app.get("/{full_path:path}", include_in_schema=False, tags=["frontend"])
        async def _serve_spa(full_path: str) -> FileResponse:  # noqa: RUF029
            """SPA catch-all — returns index.html for all non-API client routes."""
            return FileResponse(str(_spa_dist / "index.html"))

    # ---- Health / readiness -----------------------------------------

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        return {
            "status": "ok",
            "phase": "6",
            "environment": os.environ.get("ENVIRONMENT", "development"),
        }

    @app.get("/ready", tags=["ops"], summary="Readiness probe")
    async def ready() -> JSONResponse:
        """
        Returns 200 when the application is ready to serve traffic.
        Confirms the Postgres pool, the Redis cache, and the pitcher
        similarity engine are all attached and (for DB/Redis) live.

        Returns 503 with a per-component status if any check fails. A
        missing pitcher_engine is reported but does NOT cause /ready to
        fail — the similarity route surfaces that via its own 503.
        """
        checks: dict[str, str] = {}
        all_ok = True

        # Postgres
        pool = getattr(app.state, "pg_pool", None)
        if pool is None:
            checks["postgres"] = "not_initialized"
            all_ok = False
        else:
            try:
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                checks["postgres"] = "ok"
            except Exception as exc:  # noqa: BLE001
                checks["postgres"] = f"error: {type(exc).__name__}"
                all_ok = False

        # Redis
        redis_client = getattr(app.state, "redis_client", None)
        if redis_client is None:
            checks["redis"] = "not_initialized"
            all_ok = False
        else:
            try:
                await redis_client.ping()
            except Exception as exc:  # noqa: BLE001
                checks["redis"] = f"error: {type(exc).__name__}"
                all_ok = False

        # Pitcher engine (informational only — not a readiness blocker)
        checks["pitcher_engine"] = (
            "ok" if getattr(app.state, "pitcher_engine", None) is not None else "not_initialized"
        )

        status_code = 200 if all_ok else 503
        return JSONResponse(
            {"status": "ready" if all_ok else "degraded", "checks": checks},
            status_code=status_code,
        )

    @app.get("/", tags=["ops"], include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "MLB Baseball Simulation Platform",
            "phase": "6 — Frontend Build (in progress)",
            "docs": "/docs",
            "health": "/health",
        }

    # App-level exception handling (SIM-416) — structured 500 envelope.
    install_exception_handlers(app)

    return app


app = create_app()
