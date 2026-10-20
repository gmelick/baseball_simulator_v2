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
    """Open the app's resources at startup and close them at shutdown.

    Startup, in order: check the environment, open the Postgres pool, the run
    registry and the Redis cache, attach the league reader, build the
    similarity engines, load the calibration, open the assistant's client and
    the read-only pool, start the live feed, open the replay store, prime the
    park-factor source, build the shared BatchRunner, schedule its pre-warm and
    watch for a new bundle. A failure to open the Postgres pool or the Redis
    cache, or to build the pitcher engine, stops the boot; every other step
    degrades and logs.

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

    # SIM-519 Part E: the run jobs. A queued or running row left by a previous
    # process can never finish, so it is marked failed('restart') at boot.
    from api.sim_jobs import SimJobRegistry

    app.state.sim_jobs = SimJobRegistry(app.state.pg_pool)
    orphans = await app.state.sim_jobs.recover_orphans()
    if orphans:
        log.info("SIM-519: %d unfinished simulation run(s) marked failed('restart')", orphans)

    log.info("Opening Redis cache ...")
    app.state.redis_client, app.state.similarity_cache = await open_redis_cache(redis_url)

    # SIM-519 Part A: the league reader the slate (schedule) and the card detail
    # (per-game feed) share. SLATE_SCHEDULE_ENABLED=0 leaves it off, and the
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
    # SIM-563: with a pinned version the workers find the bundle through the pin,
    # so the pre-warm passes no pool dir (an explicit one would bypass the pin and
    # cache the sampler under a different key than the real games use).
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

    # SIM-453 — tell the caller whether the park factor was real. An unavailable
    # source used to be indistinguishable from a neutral park, in the logs and in
    # the response alike. Now every response says which one it was.
    app.add_middleware(ParkFactorSourceMiddleware)

    # ----------------------------------------------------------------
    # Routers — registered as phases complete
    # ----------------------------------------------------------------
    # SIM-389: Browser session auth (login / me / logout).
    # Registered FIRST so the /auth/* routes are always resolvable
    # regardless of other router registration order.
    #
    # SIM-443c: every data router below carries require_auth at the router
    # level (its APIRouter(dependencies=...)), so a new route on one of them
    # is gated without a per-route line. Two routers are deliberately open:
    # auth_router (the login itself) and ws_router: require_auth takes a
    # Request, and FastAPI does not supply one in a WebSocket scope, so on
    # ws_router it would fail every handshake with a TypeError. ws_router
    # therefore carries no router dependency, and the handler calls
    # authenticate_websocket first. tests/unit/test_route_auth_coverage.py walks app.routes
    # and fails on any ungated data route.
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
    # The route reads app.state.pg_pool_ro, the read-only pool (SIM-566, the
    # Data Lab read routes on the read-only pool). It answers 503 without that
    # pool and never falls back to the main pool. Aggregate counts only.
    from api.routes.data_health import router as data_health_router

    app.include_router(data_health_router)

    # Phase 5 (SIM-354): live ingestion routers.
    #   ws_router   — WebSocket /ws/games/{game_pk} (frontend live subscriptions)
    #   odds_router — REST /api/odds/{game_pk}, /api/odds/today/all
    # These are registered unconditionally — route registration requires no live
    # DB/Redis/WS connection. The background pipeline that *feeds* the WS clients
    # is started in lifespan only when LIVE_PIPELINE_ENABLED=true.
    # SIM-443c: odds_router carries require_auth; the WebSocket handler checks
    # the handshake's cookie or key itself and closes with 1008 on a failure.
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

    # SIM-439: Data Lab (raw.pitches explorer) + generalized Similarity Explorer.
    #   POST /api/sql/run           — read-only SQL console executor
    #   GET  /api/analytics/*       — summary analytics over raw.pitches
    #   GET  /api/players/*         — name typeahead + player identity
    #   GET  /api/schema            — raw.* schema tree for the console/assistant
    #   GET  /api/similarity/*      — generalized comps across all engines
    #   *    /api/assistant/*       — optional AI assistant (gated on ANTHROPIC_API_KEY)
    # Registered unconditionally (route registration needs no live DB/engine); the
    # handlers read app.state and 503 gracefully when a dependency is missing.
    # The SQL console, the assistant, analytics, players and schema read ONLY
    # app.state.pg_pool_ro, the read-only pool (SIM-443e; SIM-566 for the three
    # Data Lab read routers). Without it they answer 503; none falls back to the
    # main pool. The similarity explorer reads the engines, not Postgres.
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
    # ----------------------------------------------------------------

    # ---- Health / readiness -----------------------------------------

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        return {
            "status": "ok",
            "phase": "6",
            "environment": os.environ.get("ENVIRONMENT", "development"),
            # SIM-443c: false only under the AUTH_DISABLED=1 opt-out.
            "auth_enforced": not _auth_disabled(),
            # SIM-443e: whether the SQL console's read-only pool is open, and as
            # which database role. No DSN, no password.
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

        # SIM-519 Part C: the live service's heartbeat (informational only).
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

    # ---- SIM-381: Serve the Vite-built SPA -----------------------------------
    # In production, nginx serves frontend/dist/ directly (see deploy/nginx/nginx.conf).
    # This mount is a dev/staging convenience: when running uvicorn directly,
    # the built assets are served from FastAPI so the app is fully self-contained.
    #
    # The mount is CONDITIONAL on the dist directory existing so the app boots
    # cleanly before the first `npm run build` (CI / fresh clone).
    #
    # Mount order matters: FastAPI matches routes in registration order, so the
    # API routes and /health and /ready come first. The `/{full_path:path}`
    # catch-all then fires only when nothing else matches. It stays before "/"
    # so a built frontend serves index.html at the root.
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
