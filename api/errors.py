"""
api/errors.py
=============
The app's exception handling: a structured error envelope for a 500.

Installs a catch-all handler so an UNHANDLED exception returns a structured
JSON envelope instead of a bare 500 / leaked traceback:

    {"detail": "Internal server error.",
     "error_type": "internal_error",
     "request_id": "<12-hex>"}

The ``request_id`` correlates the client-facing response with the full
server-side log line (logged with the traceback at ERROR). The internal
exception message is NEVER sent to the client (no information leak).

HTTPException and RequestValidationError keep FastAPI's own shapes (both are
structured; the frontend and the tests read ``detail``), so only the 500 path
uses the envelope. The module stands alone, so a test runs it against a small
app without the lifespan.
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
