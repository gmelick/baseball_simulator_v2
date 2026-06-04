"""
api/routes/_common.py
======================
Shared route helpers hoisted out of the individual ``api/routes/*`` modules to
remove copy-paste drift (audit ``2026-06-03`` — the ``api/ duplication`` row).

Currently houses the two helpers that were byte-identical across routers:

  * :func:`_get_pool` — the ``app.state.pg_pool`` accessor (503 when unattached),
    previously duplicated in ``games.py`` and ``data_health.py``.
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
