"""
api/routes/schema_introspect.py
===============================
The read-only schema tree for the Data Lab's schema browser and the AI assistant.

    GET /api/schema   ->  SchemaResponse   (raw.* tables + columns)

Lists only the ``raw`` schema — the Postgres tables reachable by the SQL Console
and the AI assistant. ``derived.*`` / ``sim.*`` live in the in-process DuckDB and
are NOT on this pool, so exposing them here would invite cross-database joins
that cannot run. It returns the structure only, no row data, but it still
needs a login: the table and column names are not public. The tree is cached on
``app.state`` for the life of the process (the schema is fixed between deploys).

The route reads ``information_schema.columns`` through the read-only pool,
``app.state.pg_pool_ro``. The read-only role sees every ``raw`` column there. Without the pool, the
route answers 503; it never falls back to the main pool.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.auth import require_auth
from api.routes._common import _get_ro_pool, _row_get

# Every route on this router requires a login (cookie or API key).
router = APIRouter(prefix="/api/schema", tags=["data-lab"], dependencies=[Depends(require_auth)])


class ColumnInfo(BaseModel):
    name: str
    data_type: str


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnInfo]


class SchemaInfo(BaseModel):
    name: str
    tables: list[TableInfo]


class SchemaResponse(BaseModel):
    schemas: list[SchemaInfo]


_COLUMNS_SQL = """
    SELECT table_name, column_name, data_type
      FROM information_schema.columns
     WHERE table_schema = 'raw'
     ORDER BY table_name, ordinal_position
"""


async def _fetch(pool, sql: str):
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            return await conn.fetch(sql)
    return await pool.fetch(sql)


@router.get(
    "",
    response_model=SchemaResponse,
    summary="raw.* schema tree (tables + columns) for the SQL console / assistant",
)
async def get_schema(request: Request) -> SchemaResponse:
    cached = getattr(request.app.state, "_schema_cache", None)
    if cached is not None:
        return cached

    pool = _get_ro_pool(request)
    rows = await _fetch(pool, _COLUMNS_SQL)

    by_table: dict[str, list[ColumnInfo]] = {}
    for r in rows or []:
        tbl = _row_get(r, "table_name", "")
        by_table.setdefault(tbl, []).append(
            ColumnInfo(name=_row_get(r, "column_name", ""), data_type=_row_get(r, "data_type", ""))
        )

    tables = [TableInfo(name=t, columns=cols) for t, cols in sorted(by_table.items())]
    resp = SchemaResponse(schemas=[SchemaInfo(name="raw", tables=tables)])

    # Cache for the process lifetime (schema is static between deploys).
    request.app.state._schema_cache = resp
    return resp


__all__ = ["router", "SchemaResponse", "SchemaInfo", "TableInfo", "ColumnInfo"]
