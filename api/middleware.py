"""
api/middleware.py
=================
The app's own response middleware.

:class:`ParkFactorSourceMiddleware` tells the API caller whether the park
factors behind a simulation were real. The other middleware the app adds (CORS,
the rate limit and the latency window) live in ``api/auth.py`` and Starlette.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class ParkFactorSourceMiddleware(BaseHTTPMiddleware):
    """Stamp ``X-Park-Factor-Source`` on every response (the park-factor source, SIM-453).

    A WARNING in the container log tells the operator that the park factor is
    unavailable. It tells the API caller nothing, and the caller holds the
    numbers. This header carries the same fact to the caller:

    * ``duckdb:<n>`` — the source is open and holds ``n`` rows in
      ``derived.park_factors``. Simulations use real venue factors.
    * ``unavailable`` — nothing answered the lookup. Every simulation in this
      response ran at a neutral park factor of 1.0.
    * ``unknown`` — the lifespan has not run (a TestClient built without it).

    A client that ignores the header is not affected, and no response body
    changes shape.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # The module's live verdict first, so the header follows a re-read (a
        # boot during a rebuild recovers its park factors later).
        from simulation.sim_kwargs import park_factor_source_status

        source = park_factor_source_status() or getattr(
            request.app.state, "park_factor_source", None
        )
        try:
            response.headers["X-Park-Factor-Source"] = (
                source.header_value() if source is not None else "unknown"
            )
        except Exception:  # noqa: BLE001 -- a header never breaks a response
            pass
        return response
