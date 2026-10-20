"""The state every part of the historical loader shares."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import ClassVar

import psycopg2
import psycopg2.extras
import psycopg2.pool

# Connection pool sizing.  A historical backfill issues many short
# DB round-trips per game (one per FK-existence check, plus inserts and the
# freshness upsert).  Re-connecting for every one of those is wasteful, so we
# borrow from a pool instead.  Sized via env so ops can tune for the box.
ETL_DB_POOL_MIN = int(os.environ.get("ETL_DB_POOL_MIN", "1"))
ETL_DB_POOL_MAX = int(os.environ.get("ETL_DB_POOL_MAX", "8"))


class LoaderBase:
    """The loader's state, declared once so each mixin sees it: the DSN, the
    connection pool and its lock, the circuit-breaker count and the pitch insert.
    """

    dsn: str
    write_csv: bool
    csv_output_dir: str
    _pool: psycopg2.pool.ThreadedConnectionPool | None
    _pool_lock: threading.Lock
    _consecutive_failures: int
    INSERT_SQL: ClassVar[str]

    def _ensure_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Create the pool exactly once, on first DB access.

        ThreadedConnectionPool is used (over SimpleConnectionPool) so the loader
        stays safe if a caller ever fans games out across threads; for the
        single-threaded backfill it behaves identically.  Pool bounds come from
        ETL_DB_POOL_MIN / ETL_DB_POOL_MAX (resolved at import time).
        """
        # Double-checked locking. The bare ``if self._pool is None``
        # was a check-then-set race — two threads could each build a pool, and
        # the loser's connections would leak (never returned, never closed)
        # because ``close()`` only ever shuts down the pool it can see. Harmless
        # while the loader is driven single-threaded, but the class advertises
        # ThreadedConnectionPool precisely so a caller MAY fan out.
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
        """Borrow a pooled connection for the duration of the ``with`` block.

        The connection is returned to the pool (``putconn``) on exit rather than
        closed, so the underlying socket is reused by the next round-trip.
        Existing transaction semantics are unchanged: callers still own
        commit/rollback exactly as before.
        """
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    def close(self) -> None:
        """Shut the pool down, closing every pooled connection.

        Safe to call multiple times and on a loader that never touched the DB.
        Call this once a backfill / load run is complete so file descriptors
        aren't held open.
        """
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
