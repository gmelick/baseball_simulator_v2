"""
api/errors.py
=============
SIM-416 — app-level exception handling + structured error envelope.

Installs a catch-all handler so an UNHANDLED exception returns a structured
JSON envelope instead of a bare 500 / leaked traceback:

    {"detail": "Internal server error.",
     "error_type": "internal_error",
     "request_id": "<12-hex>"}

The ``request_id`` correlates the client-facing response with the full
server-side log line (logged with the traceback at ERROR). The internal
exception message is NEVER sent to the client (no information leak).

HTTPException and RequestValidationError keep their existing FastAPI shapes
(both already structured; the frontend + tests read ``detail``), so this only
changes the previously-unstructured 500 path. Extracted into its own module so
it is unit-testable against a tiny app without the DB-dependent lifespan.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("api.errors")


def install_exception_handlers(app: FastAPI) -> None:
    """Register the catch-all unhandled-exception handler on ``app``."""

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(  # type: ignore[unused-ignore]
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = uuid.uuid4().hex[:12]
        # Full detail (type, message, traceback) goes to the server log only.
        log.exception(
            "Unhandled exception [request_id=%s] on %s %s",
            request_id,
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "error_type": "internal_error",
                "request_id": request_id,
            },
        )


__all__ = ["install_exception_handlers"]
