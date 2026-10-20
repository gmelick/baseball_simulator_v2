"""Ask a database whether a table or a column exists.

Code that reads an optional table or column (one a migration adds, or one a
narrow test fixture leaves out) asks the catalog first and builds its query on
the answer. These helpers are that question, one per database driver:

* DuckDB: :func:`duck_columns`, :func:`duck_table_exists` and
  :func:`duck_relation_readable` (a relation of an attached catalog, such as
  ``pg.raw.play_events``).
* psycopg2: :func:`pg_column_exists` and :func:`pg_relation_exists`.
* asyncpg: :func:`pg_column_exists_async`.

Each reads ``information_schema`` (or ``to_regclass``), so a missing table
answers "absent" instead of raising. The names go into the SQL as literals, so
each is checked to be a plain identifier first. A caller that must survive a
connection that cannot answer at all keeps its own ``try``.
"""

from __future__ import annotations

import re
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{name!r} is not a plain SQL identifier")
    return name


def _dotted(name: str) -> str:
    for part in name.split("."):
        _ident(part)
    return name


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------


def duck_columns(conn: Any, schema: str, table: str, *, this_file_only: bool = False) -> set[str]:
    """The column names of ``schema.table``; empty when the table is absent.

    ``this_file_only`` leaves out the catalogs attached to the file (an
    attached Postgres can carry a table of the same schema and name).
    """
    catalog = "table_catalog = current_database() AND " if this_file_only else ""
    sql = (
        "SELECT column_name FROM information_schema.columns "
        f"WHERE {catalog}table_schema = '{_ident(schema)}' AND table_name = '{_ident(table)}'"
    )
    return {str(row[0]) for row in conn.execute(sql).fetchall()}


def duck_table_exists(conn: Any, schema: str, table: str) -> bool:
    """Whether ``schema.table`` exists in the DuckDB catalog."""
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [_ident(schema), _ident(table)],
    ).fetchone()
    return bool(row and row[0])


def duck_relation_readable(conn: Any, relation: str) -> bool:
    """Whether a ``SELECT`` on ``relation`` runs, for example ``pg.raw.play_events``.

    The relation can live in an attached catalog that ``information_schema``
    does not describe, so the probe runs an empty query instead. Any error,
    including an attach that failed, reads as unreadable.
    """
    try:
        conn.execute(f"SELECT 1 FROM {_dotted(relation)} LIMIT 0")
    except Exception:  # noqa: BLE001 -- unreadable for any reason
        return False
    return True


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


def _pg_column_sql(schema: str, table: str, column: str) -> str:
    return (
        "SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema = '{_ident(schema)}' AND table_name = '{_ident(table)}' "
        f"AND column_name = '{_ident(column)}'"
    )


def pg_column_exists(cur: Any, schema: str, table: str, column: str) -> bool:
    """Whether ``schema.table`` has ``column``, on a psycopg2 cursor."""
    cur.execute(_pg_column_sql(schema, table, column))
    return cur.fetchone() is not None


def pg_relation_exists(cur: Any, relation: str) -> bool:
    """Whether ``relation`` (``schema.table``) exists, on a psycopg2 cursor."""
    cur.execute(f"SELECT to_regclass('{_dotted(relation)}')")
    probe = cur.fetchone()
    return bool(probe and probe[0] is not None)


async def pg_column_exists_async(conn: Any, schema: str, table: str, column: str) -> bool:
    """Whether ``schema.table`` has ``column``, on an asyncpg connection or pool."""
    return bool(await conn.fetchval(_pg_column_sql(schema, table, column)))


__all__ = [
    "duck_columns",
    "duck_relation_readable",
    "duck_table_exists",
    "pg_column_exists",
    "pg_column_exists_async",
    "pg_relation_exists",
]
