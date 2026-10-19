"""
api/routes/_common.py
======================
Shared route helpers hoisted out of the individual ``api/routes/*`` modules to
remove copy-paste drift (audit ``2026-06-03`` — the ``api/ duplication`` row).

The module holds three helpers and one constant:

  * :func:`_get_pool` — the ``app.state.pg_pool`` accessor (503 when unattached).
    The routes that write or read ``sim.*`` use it: games and betting.
    ``metrics.py`` reads ``app.state.pg_pool`` directly.
  * :func:`_get_ro_pool` — the ``app.state.pg_pool_ro`` accessor (503 when
    unattached). The read-only pool logs in as ``baseball_ro``, a role that can
    read ``raw.*`` and nothing else. The SQL console and the Data Lab read
    routes (schema, analytics, players, data freshness) use it (SIM-566, the
    Data Lab read routes on the read-only pool).
  * :data:`RO_POOL_UNAVAILABLE_DETAIL` — the one 503 text for a missing
    read-only pool. ``sql_safety.py`` re-exports it for the assistant and for
    the console's tests. The constant lives here so the route modules do not
    import ``sqlglot`` through ``sql_safety.py``.
  * :func:`_row_get` — uniform column read over an asyncpg ``Record`` OR a plain
    dict (the unit-test stub shape).  The canonical implementation is the
    ``games.py`` variant, which additionally takes an explicit ``default`` and
    collapses a present-but-``None`` value to that default.

      Drift note: ``data_health.py`` previously carried a near-identical
      ``_row_get`` that branched on ``isinstance(row, dict)`` and did NOT
      collapse ``None`` to a default.  That difference is only observable when a
      *non-None* default is supplied — which ``data_health`` never did (it always
      called with the implicit ``None`` default, and every read is wrapped in
      ``_iso(...)`` or ``int(... or 0)``), so this canonical version is
      behaviour-identical for every existing ``data_health`` call site.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status


def _get_pool(request: Request) -> Any:
    """The asyncpg pool, or 503 if the lifespan has not attached one.

    Mirrors ``similarity.get_pitcher_engine``'s 503-on-missing posture: a route
    that needs the DB fails loudly rather than 500-ing on an attribute error.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database pool unavailable",
        )
    return pool


#: The 503 detail every read-only route returns when the read-only pool is
#: missing (the SQL console and the assistant since SIM-443e; the Data Lab read
#: routes since SIM-566). One string, so every route says the same thing.
RO_POOL_UNAVAILABLE_DETAIL = (
    "The read-only database pool is not open. "
    "Set BASEBALL_DB_RO_DSN to the baseball_ro role and restart the app."
)


def _get_ro_pool(request: Request) -> Any:
    """Return the read-only pool, or raise 503 when the lifespan has none.

    SIM-566 (the Data Lab read routes on the read-only pool): the pool logs in
    as ``baseball_ro``, which can read ``raw.*`` and nothing else. There is no
    fallback to the main pool, because the main pool's role can write.
    """
    pool = getattr(request.app.state, "pg_pool_ro", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=RO_POOL_UNAVAILABLE_DETAIL,
        )
    return pool


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an asyncpg Record or a plain dict uniformly.

    Returns ``default`` when the key is absent OR present-but-``None`` (a missing
    key on a dict raises ``KeyError``; on a Record an ``IndexError``/``KeyError``;
    a non-subscriptable row a ``TypeError`` — all collapse to ``default``).
    """
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val
