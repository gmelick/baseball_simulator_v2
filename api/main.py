"""
api/main.py
===========
The FastAPI app of the MLB Baseball Simulation Platform: the app factory and its
lifespan.

:func:`create_app` adds the middleware, includes every router and adds the
health, readiness and SPA routes. It opens no connection, so a test or the
OpenAPI export can build the app. :func:`lifespan` opens the app's resources at
startup and closes them at shutdown; its steps that no test patches live in
:mod:`api.startup`, the environment check in :mod:`api.env_check` and the
park-factor header in :mod:`api.middleware`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api import startup
from api.auth import (
    LatencyMiddleware,
    RateLimitMiddleware,
    _auth_disabled,
    resolve_cors_origins,
)
from api.env_check import validate_environment
from api.errors import install_exception_handlers
from api.middleware import ParkFactorSourceMiddleware
from api.state import make_pg_name_resolver, open_pg_pool, open_redis_cache

log = logging.getLogger("api.main")


async def _background_prewarm(runner, pool_dir: str) -> None:
    """Warm the sim runner's workers, off the startup path.

    The lifespan schedules it as a background task and never awaits it before
    ``yield``, so a slow, stalled or out-of-memory warm-up cannot hold the boot.
    It logs the outcome; on a failure each worker warms on its first game.
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
    """Open the app's resources at startup and close them at shutdown.

    Startup, in order: check the environment, open the Postgres pool, the run
    registry and the Redis cache, attach the league reader, build the
    similarity engines, load the calibration, open the assistant's client and
    the read-only pool, start the live feed, open the replay store, prime the
    park-factor source, build the shared BatchRunner, schedule its pre-warm and
    watch for a new bundle. The Postgres pool, the Redis cache and the
    pitcher engine are required: a failure there stops the boot. The optional
    resources (the assistant, the read-only pool, the replay store, the
    park-factor source, the bundle preload) degrade and log.

    Shutdown closes the resources in reverse order
    (:func:`api.startup.close_resources`), then the park-factor source.
    """
    validate_environment()

    log.info(
        "MLB Simulation Platform starting — environment: %s",
        os.environ.get("ENVIRONMENT", "development"),
    )

    dsn = os.environ["BASEBALL_DB_DSN"]
    redis_url = os.environ["REDIS_URL"]

    log.info("Opening Postgres pool ...")
    app.state.pg_pool = await open_pg_pool(dsn)
    app.state.player_name_resolver = make_pg_name_resolver(app.state.pg_pool)

    # The run registry. A queued or running row that a previous process left
    # can never finish, so the boot marks it failed('restart').
    from api.sim_jobs import SimJobRegistry

    app.state.sim_jobs = SimJobRegistry(app.state.pg_pool)
    orphans = await app.state.sim_jobs.recover_orphans()
    if orphans:
        log.info("SIM-519: %d unfinished simulation run(s) marked failed('restart')", orphans)

    log.info("Opening Redis cache ...")
    app.state.redis_client, app.state.similarity_cache = await open_redis_cache(redis_url)

    # The league reader that the slate (the schedule) and the card detail (the
    # per-game feed) share. SLATE_SCHEDULE_ENABLED=0 leaves it off, and the
    # slate then serves the stored listing (source "db").
    if os.environ.get("SLATE_SCHEDULE_ENABLED", "1") != "0":
        from api.league_feed import LeagueFeed

        app.state.league_feed = LeagueFeed()

    await startup.build_engines(app)
    await startup.load_calibration(app)
    startup.open_assistant_client(app)
    await startup.open_read_only_pool(app, open_pg_pool)
    await startup.start_live_feed(app, dsn=dsn, redis_url=redis_url)

    # The replay store: a DuckDB file of its own (REPLAY_DUCKDB_PATH, default
    # /data/replay.duckdb), opened writable when REPLAY_PERSISTENCE_ENABLED is
    # on (compose sets it). open_replay_store creates its tables. The routes
    # that store a game write it; /plays, /state, /linescore, /decisions and
    # /card read it. Only the app opens this file, and the analytics file stays
    # read-only. A failed open never stops the boot: the replay reads answer
    # 503 and the stores skip.
    replay_enabled = os.environ.get("REPLAY_PERSISTENCE_ENABLED", "false").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )

    if replay_enabled:
        replay_path = os.environ.get("REPLAY_DUCKDB_PATH", "/data/replay.duckdb")
        try:
            from db.sim_store import open_replay_store

            log.info("SIM-561: opening the replay store at %s ...", replay_path)
            app.state.replay_duckdb = await asyncio.to_thread(open_replay_store, replay_path)
            log.info("SIM-561: replay store OPEN at %s.", replay_path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "REPLAY_PERSISTENCE_ENABLED but the replay store at %s did not open (%s: %s); "
                "the replay reads return 503 and the stores skip.",
                replay_path,
                type(exc).__name__,
                exc,
            )

    # The park-factor source, primed on its own: reading the park factors is a
    # read, the replay store a write, so the prime is not gated on the replay
    # flag. The prime reads the factors into memory and closes the file
    # (simulation/sim_kwargs.py holds the reasoning).
    from simulation.sim_kwargs import (
        close_park_factor_source,
        prime_park_factor_source,
    )

    park_source = await asyncio.to_thread(prime_park_factor_source)
    app.state.park_factor_source = park_source
    if park_source.available:
        log.info(
            "SIM-453: park-factor source READ at %s — %d rows in "
            "derived.park_factors, held in memory; the file is closed (SIM-524).",
            park_source.path,
            park_source.n_rows,
        )
    else:
        log.warning(
            "SIM-453: park-factor source UNAVAILABLE at %s — %s. Every /simulate "
            "runs PARK-BLIND at a neutral 1.0, and every response carries "
            "X-Park-Factor-Source: unavailable. This is reported, not hidden.",
            park_source.path,
            park_source.detail,
        )

    await startup.build_sim_runner(app)

    # Warm the runner's workers in the background, so the first /simulate meets
    # warm workers. The boot never waits on it: asyncio.wait_for cannot stop a
    # thread blocked in a multiprocessing call, so a blocking warm-up could hang
    # the boot. A failed warm-up leaves each worker to warm on its first game.
    # With a pinned version the workers find the bundle through the pin, so the
    # pre-warm passes no pool dir: an explicit one would bypass the pin and cache
    # the sampler under a different key than the real games use.
    prewarm_pool_dir = (
        None
        if app.state.bundle_version
        else os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
    )
    log.info("SIM-402: scheduling background sim-runner pre-warm ...")
    app.state.prewarm_task = asyncio.create_task(
        _background_prewarm(app.state.sim_runner, prewarm_pool_dir)
    )

    startup.start_bundle_watch(app)

    yield

    log.info("MLB Simulation Platform shutting down.")
    await startup.close_resources(app)
    close_park_factor_source()


# ---------------------------------------------------------------------------
# The app factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the app: the middleware, every router and the health, readiness and SPA routes.

    It opens no connection; the lifespan does that when the server starts.
    """
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

    # CORS: the allowed origins come from CORS_ORIGINS (comma-separated), else
    # FRONTEND_URL, else a localhost default (api/auth.resolve_cors_origins).
    # No default is the wildcard "*", which browsers refuse with credentials.
    origins = resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # The rate limit: an in-memory sliding window per API key or client
    # address. Off by default (RATE_LIMIT_PER_MINUTE unset or <= 0), so a dev
    # stack and the tests are unaffected. /health, /ready and / are exempt.
    app.add_middleware(RateLimitMiddleware)

    # The p95 latency over a rolling 200-request window, kept on
    # app.state.api_p95_seconds for /metrics. /health, /ready, / and /metrics
    # are exempt.
    app.add_middleware(LatencyMiddleware)

    # Every response says whether the park factors were real
    # (X-Park-Factor-Source), so a caller can tell an unavailable source from a
    # neutral park.
    app.add_middleware(ParkFactorSourceMiddleware)

    # ----------------------------------------------------------------
    # The routers. Registering a router needs no live connection; a route
    # whose resource is missing answers 503 itself.
    # ----------------------------------------------------------------
    # The browser session (login, me, logout), registered first.
    #
    # Every data router below carries require_auth at the router level (its
    # APIRouter(dependencies=...)), so a new route on one of them is gated
    # without a line of its own (the login on every route, SIM-443c). Two
    # routers are open on purpose: auth_router (the login itself) and
    # ws_router. require_auth takes a Request, and FastAPI supplies none in a
    # WebSocket scope, so on ws_router it would fail every handshake; the
    # WebSocket handler calls authenticate_websocket first instead.
    # tests/unit/test_route_auth_coverage.py walks app.routes and fails on any
    # ungated data route.
    from api.routes.auth import router as auth_router

    app.include_router(auth_router)

    # The pitcher similarity explorer (docs/similarity_visualization_spec.md).
    # The lifespan attaches its engine, name resolver and Redis cache.
    from api.routes.similarity import router as similarity_router

    app.include_router(similarity_router)

    # The game routes (/api/games): the slate, the real game, the simulations
    # and the replay reads. The sim cache attaches here, at factory time, so
    # the routes' BatchRunner memoizes summaries and the slate its listing.
    # make_cache() returns a RedisCache only when a server answers ping(), else
    # an in-process InMemoryCache, so the tests need no Redis.
    from api.routes.games import router as games_router
    from simulation.batch_runner import make_cache

    app.state.sim_cache = make_cache()
    app.include_router(games_router)

    # The betting routes (/api/betting): the edges, the signals, the line
    # movement, the CLV and the slate card's lines. /edges and /signals use the
    # game routes' simulation seam.
    from api.routes.betting import router as betting_router

    app.include_router(betting_router)

    # GET /api/data/freshness: the ingest watermark and each season's coverage,
    # aggregate counts only. It reads the read-only pool and answers 503
    # without it.
    from api.routes.data_health import router as data_health_router

    app.include_router(data_health_router)

    # The live routers: ws_router (the WebSocket /ws/games/{game_pk}, the
    # frontend's live subscriptions) and odds_router (/api/odds/{game_pk},
    # /api/odds/today/all). The messages come from the live service through
    # the Redis bridge, or from an in-process pipeline when
    # LIVE_PIPELINE_ENABLED is on. odds_router carries require_auth; the
    # WebSocket handler checks the handshake's cookie or key itself and closes
    # with 1008 on a failure.
    from pipeline.live.live_ingestion_pipeline import odds_router, ws_router

    app.include_router(ws_router)
    app.include_router(odds_router)

    # GET /metrics: the app's metrics in the Prometheus text format, for the
    # Prometheus scrape and the Grafana dashboards (deploy/monitoring/).
    from api.routes.metrics import router as metrics_router

    app.include_router(metrics_router)

    # The Data Lab and the similarity explorer:
    #   POST /api/sql/run           the read-only SQL console
    #   GET  /api/analytics/*       summary analytics over raw.pitches
    #   GET  /api/players/*         the name typeahead and a player's identity
    #   GET  /api/schema            the raw.* schema tree for the console and the assistant
    #   GET  /api/similarity/*      comparisons across all the engines
    #   *    /api/assistant/*       the AI assistant (needs ANTHROPIC_API_KEY)
    # The SQL console, the assistant, analytics, players and schema read only
    # the read-only pool; without it they answer 503, and none falls back to
    # the main pool. The similarity explorer reads the engines, not Postgres.
    from api.routes.ai_assistant import router as ai_assistant_router
    from api.routes.analytics import router as analytics_router
    from api.routes.players import router as players_router
    from api.routes.schema_introspect import router as schema_router
    from api.routes.similarity_explorer import router as similarity_explorer_router
    from api.routes.sql_runner import router as sql_runner_router

    app.include_router(sql_runner_router)
    app.include_router(analytics_router)
    app.include_router(players_router)
    app.include_router(schema_router)
    app.include_router(similarity_explorer_router)
    app.include_router(ai_assistant_router)

    # ---- The health and readiness probes ------------------------------

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        return {
            "status": "ok",
            "phase": "6",
            "environment": os.environ.get("ENVIRONMENT", "development"),
            # False only under the AUTH_DISABLED=1 opt-out.
            "auth_enforced": not _auth_disabled(),
            # Whether the SQL console's read-only pool is open, and as which
            # database role. No DSN, no password.
            "sql_console": {
                "ro_pool": getattr(app.state, "pg_pool_ro", None) is not None,
                "role": getattr(app.state, "sql_console_role", None),
            },
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

        # The live service's heartbeat (informational only).
        if redis_client is not None:
            from pipeline.live.broadcast import heartbeat_status

            checks["live_service"] = await heartbeat_status(redis_client)

        # Pitcher engine (informational only — not a readiness blocker)
        checks["pitcher_engine"] = (
            "ok" if getattr(app.state, "pitcher_engine", None) is not None else "not_initialized"
        )

        status_code = 200 if all_ok else 503
        return JSONResponse(
            {"status": "ready" if all_ok else "degraded", "checks": checks},
            status_code=status_code,
        )

    # ---- The built frontend (the SPA) ----------------------------------------
    # In production nginx serves frontend/dist/ (deploy/nginx/nginx.conf). This
    # mount serves the built assets from FastAPI when uvicorn runs on its own.
    # It needs the dist directory, so the app boots before the first
    # `npm run build`.
    #
    # Mount order matters: FastAPI matches routes in registration order, so the
    # API routes and /health and /ready come first. The `/{full_path:path}`
    # catch-all then fires only when nothing else matches. It stays before "/"
    # so a built frontend serves index.html at the root.
    _spa_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if _spa_dist.is_dir():
        _spa_assets = _spa_dist / "assets"
        if _spa_assets.is_dir():
            # Vite hashes the file names, so the assets can be cached forever.
            app.mount(
                "/assets",
                StaticFiles(directory=str(_spa_assets)),
                name="spa-assets",
            )

        @app.get("/{full_path:path}", include_in_schema=False, tags=["frontend"])
        async def _serve_spa(full_path: str) -> FileResponse:  # noqa: RUF029
            """SPA catch-all — returns index.html for all non-API client routes."""
            return FileResponse(str(_spa_dist / "index.html"))

    @app.get("/", tags=["ops"], include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "MLB Baseball Simulation Platform",
            "phase": "6 — Frontend Build (in progress)",
            "docs": "/docs",
            "health": "/health",
        }

    # The app's exception handlers: a structured 500 envelope (api/errors.py).
    install_exception_handlers(app)

    return app


app = create_app()
