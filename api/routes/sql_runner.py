"""
api/routes/sql_runner.py
========================
SIM-439 — read-only ad-hoc SQL execution for the Data Lab SQL Console.

    POST /api/sql/run   { sql, max_rows? }  ->  SqlRunResponse

Thin handler: it delegates ALL safety + execution to
``api.routes.sql_safety.run_read_only_sql`` (the same path the AI assistant
uses), so there is exactly one read-only executor in the codebase. Guarded by
``Depends(require_auth)`` (session cookie or API key), matching the other
expensive/sensitive routes.

Errors:
  * 400 — the query was rejected by the validator (non-SELECT, multi-statement,
    write keyword, dangerous function, too long) OR Postgres raised (syntax,
    statement-timeout, read-only-transaction). The message is surfaced in
    ``detail`` so the console's error panel can render it verbatim.
  * 503 — no database pool attached (lifespan not up).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes._common import _get_pool
from api.routes.sql_safety import (
    DEFAULT_MAX_ROWS,
    DEFAULT_TIMEOUT_MS,
    SqlValidationError,
    run_read_only_sql,
)

log = logging.getLogger("api.routes.sql_runner")

router = APIRouter(prefix="/api/sql", tags=["data-lab"])


class SqlRunRequest(BaseModel):
    sql: str = Field(..., description="A single read-only SELECT / WITH query.")
    max_rows: int = Field(
        DEFAULT_MAX_ROWS,
        ge=1,
        le=5000,
        description="Row cap (hard-limited server-side); results over the cap are truncated.",
    )


class SqlRunResponse(BaseModel):
    columns: list[str]
    rows: list[list] = Field(..., description="Row-major result cells (JSON-safe).")
    row_count: int
    truncated: bool = Field(..., description="True when the result hit the row cap.")
    elapsed_ms: float
    max_rows: int
    timeout_ms: int


def _ro_pool(request: Request):
    """Prefer a dedicated read-only pool if the lifespan attached one, else the
    main pool (still executed inside a read-only transaction)."""
    ro = getattr(request.app.state, "pg_pool_ro", None)
    if ro is not None:
        return ro
    return _get_pool(request)


@router.post(
    "/run",
    response_model=SqlRunResponse,
    summary="Run a read-only SQL query against raw.* (Postgres)",
    description=(
        "Executes a single SELECT / WITH query in a read-only transaction with a "
        "statement timeout and a hard row cap. Only raw.* (Postgres) is reachable; "
        "derived.*/sim.* live in DuckDB and are not on this pool. 400 on a rejected "
        "or failing query; 503 with no pool."
    ),
    dependencies=[Depends(require_auth)],
)
async def run_sql(request: Request, body: SqlRunRequest) -> SqlRunResponse:
    pool = _ro_pool(request)
    try:
        result = await run_read_only_sql(pool, body.sql, max_rows=body.max_rows)
    except SqlValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surface the DB error to the console
        # Postgres syntax / timeout / read-only-write errors land here. asyncpg
        # error messages are user-facing SQL diagnostics — safe + useful to show.
        detail = str(exc).strip() or f"query failed ({type(exc).__name__})"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    return SqlRunResponse(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
        elapsed_ms=result["elapsed_ms"],
        max_rows=body.max_rows,
        timeout_ms=DEFAULT_TIMEOUT_MS,
    )


__all__ = ["router", "SqlRunRequest", "SqlRunResponse"]
