"""
api/routes/sql_runner.py
========================
The Data Lab's SQL console: read-only ad-hoc SQL.

    POST /api/sql/run   { sql, max_rows? }  ->  SqlRunResponse

A thin handler: it leaves all the safety and the execution to
``api.routes.sql_safety.run_read_only_sql``, the path the AI assistant uses
too, so the codebase has one read-only executor. The route requires a login
(a session cookie or an API key).

The errors:
  * 400 — the validator rejected the query (not a SELECT, several statements, a
    write keyword, a dangerous function, too long, a table outside ``raw.*``),
    or Postgres raised (syntax, the statement timeout, the read-only
    transaction). ``detail`` carries the message, so the console's error panel
    shows it as it is.
  * 503 — the read-only pool is not open. The console runs only on
    ``app.state.pg_pool_ro``, the ``BASEBALL_DB_RO_DSN`` pool that the lifespan
    opened and checked; it never falls back to the main pool. The shared
    helper ``_get_ro_pool`` (``api/routes/_common.py``) reads the pool and
    raises the 503.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes._common import _get_ro_pool
from api.routes.sql_safety import (
    DEFAULT_MAX_ROWS,
    DEFAULT_TIMEOUT_MS,
    SqlValidationError,
    run_read_only_sql,
)

log = logging.getLogger("api.routes.sql_runner")

# Every route on this router requires a login (cookie or API key).
router = APIRouter(prefix="/api/sql", tags=["data-lab"], dependencies=[Depends(require_auth)])


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


@router.post(
    "/run",
    response_model=SqlRunResponse,
    summary="Run a read-only SQL query against raw.* (Postgres)",
    description=(
        "Executes a single SELECT / WITH query in a read-only transaction with a "
        "statement timeout and a hard row cap, on the read-only database role. "
        "Every table must be written as raw.<table> (or be a CTE of the query); "
        "any other schema or an unqualified table name is rejected. 400 on a "
        "rejected or failing query; 503 when the read-only pool is not open."
    ),
    dependencies=[Depends(require_auth)],
)
async def run_sql(request: Request, body: SqlRunRequest) -> SqlRunResponse:
    pool = _get_ro_pool(request)
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
