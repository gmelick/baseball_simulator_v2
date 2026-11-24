"""The state every part of the historical loader shares: the DSN, the connection pool and
the table probes.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.pool

from db.introspect import pg_column_exists, pg_relation_exists

log = logging.getLogger(__name__)

# A backfill makes many short database round trips per game, so the loader
# borrows its connections from a pool. The environment sets the pool's bounds.
ETL_DB_POOL_MIN = int(os.environ.get("ETL_DB_POOL_MIN", "1"))
ETL_DB_POOL_MAX = int(os.environ.get("ETL_DB_POOL_MAX", "8"))


class LoaderBase:
    """The loader's state, declared once so each mixin sees it.

    The state is the DSN, the connection pool, the pool's lock and the count of
    games that failed in a row.
    """

    dsn: str
    _pool: psycopg2.pool.ThreadedConnectionPool | None
    _pool_lock: threading.Lock
    _consecutive_failures: int

    def _ensure_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Create the pool once, on the first database access, and return it.

        The pool is a ``ThreadedConnectionPool``, so a caller may load games on
        several threads. ``ETL_DB_POOL_MIN`` and ``ETL_DB_POOL_MAX`` set its bounds
        when the module loads.
        """
        # Double-checked locking. Without the lock, two threads can each build a
        # pool, and the connections of the pool that loses leak: close() sees
        # only the pool it holds.
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=ETL_DB_POOL_MIN,
                        maxconn=ETL_DB_POOL_MAX,
                        dsn=self.dsn,
                    )
        return self._pool

    @contextmanager
    def _get_conn(self):
        """Yield a pooled connection, and return it to the pool when the block ends.

        The connection goes back to the pool open, so the next round trip reuses
        its socket. The caller owns the commit and the rollback.
        """
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    def _exists_once(
        self,
        cur: Any,
        cache_attr: str,
        relation: str,
        *,
        column: str | None = None,
        absent_message: str | None = None,
        level: int = logging.WARNING,
    ) -> bool:
        """Return whether ``relation`` (``schema.table``), or its ``column``, exists.

        The loader asks the catalogue once and caches the answer on the attribute
        ``cache_attr`` (``None`` means not yet asked). A sweep of thousands of games
        then runs one lookup, not one per game. The cache assumes that no migration
        runs during a sweep. When the table or column is absent, the loader logs
        ``absent_message`` (if given) at ``level``, once.
        """
        cached = getattr(self, cache_attr)
        if cached is None:
            if column is None:
                cached = pg_relation_exists(cur, relation)
            else:
                schema, table = relation.split(".", 1)
                cached = pg_column_exists(cur, schema, table, column)
            setattr(self, cache_attr, cached)
            if not cached and absent_message:
                log.log(level, absent_message)
        return bool(cached)

    def close(self) -> None:
        """Close every pooled connection and drop the pool.

        Call it when a load run ends, so the loader holds no open sockets. A second
        call, or a call on a loader that never used the database, does nothing.
        """
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
