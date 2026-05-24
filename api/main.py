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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.auth import RateLimitMiddleware, resolve_cors_origins
from api.state import (
    build_pitcher_engine,
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


def validate_environment() -> None:
    """
    Asserts all required environment variables are present at startup.
    Raises RuntimeError with a clear message listing what is missing.
    Called inside the lifespan so a misconfigured container fails fast
    rather than silently running with broken connections.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in the required values."
        )


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


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
    engine_enabled = os.environ.get(
        "SIMILARITY_ENGINE_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no")

    if engine_enabled:
        log.info("Building pitcher similarity engine (this may take a few seconds) ...")
        app.state.pitcher_engine = await asyncio.to_thread(build_pitcher_engine)
    else:
        log.warning(
            "SIMILARITY_ENGINE_ENABLED is false — skipping engine build. "
            "The /api/similarity/* routes will return 503 until this is "
            "re-enabled and the DuckDB profile file exists."
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
    pipeline_enabled = os.environ.get(
        "LIVE_PIPELINE_ENABLED", "false"
    ).strip().lower() not in ("0", "false", "no", "")

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

    yield

    log.info("MLB Simulation Platform shutting down.")

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
        version="0.1.0-phase2",
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

    # ----------------------------------------------------------------
    # Routers — registered as phases complete
    # ----------------------------------------------------------------
    # Similarity Score Explorer (v1: pitcher engine).
    # Spec: docs/similarity_visualization_spec.md. The lifespan above
    # attaches the engine, name resolver, and Redis cache to app.state.
    from api.routes.similarity import router as similarity_router

    app.include_router(similarity_router)

    # Phase 5 (SIM-354): live ingestion routers.
    #   ws_router   — WebSocket /ws/games/{game_pk} (frontend live subscriptions)
    #   odds_router — REST /api/odds/{game_pk}, /api/odds/today/all
    # These are registered unconditionally — route registration requires no live
    # DB/Redis/WS connection. The background pipeline that *feeds* the WS clients
    # is started in lifespan only when LIVE_PIPELINE_ENABLED=true.
    from pipeline.live.live_ingestion_pipeline import odds_router, ws_router

    app.include_router(ws_router)
    app.include_router(odds_router)
    # ----------------------------------------------------------------

    # ---- Health / readiness -----------------------------------------

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        return {
            "status": "ok",
            "phase": "2",
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
                checks["redis"] = "ok"
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
            "phase": "2 — Similarity Engine Suite (in progress)",
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
