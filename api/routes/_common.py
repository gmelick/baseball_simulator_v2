"""
api/routes/_common.py
======================
The small helpers that several route modules share.

  * :func:`_get_pool` — the ``app.state.pg_pool`` accessor (503 when unattached).
    The routes that write or read ``sim.*`` use it: games and betting.
    ``metrics.py`` reads ``app.state.pg_pool`` directly.
  * :func:`_get_ro_pool` — the ``app.state.pg_pool_ro`` accessor (503 when
    unattached). The read-only pool logs in as ``baseball_ro``, a role that can
    read ``raw.*`` and nothing else. The SQL console and the Data Lab read
    routes (schema, analytics, players, data freshness) use it (the Data Lab
    read routes on the read-only pool, SIM-566).
  * :data:`RO_POOL_UNAVAILABLE_DETAIL` — the one 503 text for a missing
    read-only pool. ``sql_safety.py`` re-exports it for the assistant and for
    the console's tests. The constant lives here so the route modules do not
    import ``sqlglot`` through ``sql_safety.py``.
  * :func:`_get_sim_cache` — the optional ``app.state.sim_cache`` accessor.
  * :func:`_row_get` — one column read over an asyncpg ``Record`` OR a plain
    dict (the unit-test stub shape), with a default for a missing or NULL value.
  * :func:`_opt_str`, :func:`_opt_int` and :func:`_iso` — a column as text, as
    an integer or as an ISO-8601 string, or None.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status


def _get_pool(request: Request) -> Any:
    """The asyncpg pool, or 503 if the lifespan has not attached one.

    A route that needs the database fails with a clear 503, not a 500 from an
    attribute error.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database pool unavailable",
        )
    return pool


#: The 503 detail every read-only route returns when the read-only pool is
#: missing (the SQL console, the assistant and the Data Lab read routes). One
#: string, so every route says the same thing.
RO_POOL_UNAVAILABLE_DETAIL = (
    "The read-only database pool is not open. "
    "Set BASEBALL_DB_RO_DSN to the baseball_ro role and restart the app."
)


def _get_ro_pool(request: Request) -> Any:
    """Return the read-only pool, or raise 503 when the lifespan has none.

    The pool logs in as ``baseball_ro``, which can read ``raw.*`` and nothing
    else. There is no fallback to the main pool, because the main pool's role
    can write.
    """
    pool = getattr(request.app.state, "pg_pool_ro", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=RO_POOL_UNAVAILABLE_DETAIL,
        )
    return pool


def _get_sim_cache(request: Request) -> Any:
    """The optional sim cache attached to app.state, or None.

    ``None`` lets ``BatchRunner`` pick its own cache backend. A route never
    needs the cache: it only saves work.
    """
    return getattr(request.app.state, "sim_cache", None)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an asyncpg Record or a plain dict.

    Returns ``default`` when the key is absent OR its value is ``None``. A
    missing key raises ``KeyError`` on a dict, ``IndexError`` or ``KeyError``
    on a Record, and ``TypeError`` on a row that is not subscriptable; each
    reads as ``default``.
    """
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def _opt_str(row: Any, key: str) -> str | None:
    """``str(row[key])``, or ``None`` when the column is absent or NULL."""
    v = _row_get(row, key)
    return None if v is None else str(v)


def _opt_int(row: Any, key: str) -> int | None:
    """``int(row[key])``, or ``None`` when the column is absent or NULL."""
    v = _row_get(row, key)
    return None if v is None else int(v)


def _iso(v: Any) -> str | None:
    """A date or datetime as ISO-8601 text; any other value as ``str``; None stays None."""
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
