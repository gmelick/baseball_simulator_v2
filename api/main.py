"""
api/main.py
===========
MLB Baseball Simulation Platform — FastAPI Application Entry Point

Phase status: stub.  Full implementation in Phase 5 (SIM-Backend Developer tickets).
This file exists so that:
  - Dockerfile CMD (uvicorn api.main:app) resolves without ImportError
  - CI docker-build job can run: python -c 'import api.main'
  - make dev starts a live server with clear "coming in Phase 5" messaging

Routers added here as phases complete:
  Phase 5: simulation runner, game state, managerial override
  Phase 6: frontend serving
  Phase 7: monitoring, deployment hardening
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
      - Will initialize DB pool, Redis, and live pipeline in Phase 5

    On shutdown:
      - Will cleanly close all connections in Phase 5
    """
    # Validate env at boot; fail fast on misconfiguration
    validate_environment()

    log.info(
        "MLB Simulation Platform starting — environment: %s",
        os.environ.get("ENVIRONMENT", "development"),
    )

    # ----------------------------------------------------------------
    # Phase 5: Uncomment to wire in the live ingestion pipeline
    # ----------------------------------------------------------------
    # from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline
    # pipeline = LiveIngestionPipeline(
    #     dsn=os.environ["BASEBALL_DB_DSN"],
    #     redis_url=os.environ["REDIS_URL"],
    # )
    # await pipeline.start()
    # app.state.pipeline = pipeline
    # ----------------------------------------------------------------

    yield

    log.info("MLB Simulation Platform shutting down.")

    # ----------------------------------------------------------------
    # Phase 5: Uncomment to cleanly stop the pipeline
    # ----------------------------------------------------------------
    # if hasattr(app.state, "pipeline"):
    #     await app.state.pipeline.stop()
    # ----------------------------------------------------------------


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

    # CORS — tighten in production via ENVIRONMENT check
    origins = (
        ["*"]
        if os.environ.get("ENVIRONMENT", "development") == "development"
        else [os.environ.get("FRONTEND_URL", "http://localhost:3000")]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------------
    # Routers — registered as phases complete
    # ----------------------------------------------------------------
    # Phase 5:
    # from pipeline.live.live_ingestion_pipeline import ws_router, odds_router
    # app.include_router(ws_router)
    # app.include_router(odds_router)
    # ----------------------------------------------------------------

    # ---- Health / readiness -----------------------------------------

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        return {"status": "ok", "phase": "2", "environment": os.environ.get("ENVIRONMENT", "development")}

    @app.get("/ready", tags=["ops"], summary="Readiness probe")
    async def ready() -> JSONResponse:
        """
        Returns 200 when the application is ready to serve traffic.
        Phase 5: will also check DB pool and Redis connectivity.
        """
        return JSONResponse({"status": "ready"})

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
