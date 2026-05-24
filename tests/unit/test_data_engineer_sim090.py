"""
Unit tests for SIM-090 (Data Engineer) — connection pool in the ETL loader
==========================================================================

Before SIM-090, ``HistoricalDataLoader._get_conn()`` opened a fresh
``psycopg2.connect(self.dsn)`` and ``conn.close()``-d it on every single DB
round-trip.  Each game load issues many such round-trips (one per FK existence
check, plus the batch insert and the freshness upsert), so a full backfill
churned through thousands of TCP connect/teardown cycles.

SIM-090 replaces that with a lazily-created
``psycopg2.pool.ThreadedConnectionPool``:
  - the pool is created exactly once (on first DB use) and reused for the life
    of the loader, across every game;
  - per round-trip the loader borrows with ``getconn()`` and returns with
    ``putconn()`` rather than opening/closing a raw connection;
  - ``loader.close()`` tears the pool down via ``closeall()``.

These tests assert all three properties with the pool (and psycopg2.connect)
mocked, so they run without a live database.  Mocking style mirrors
tests/unit/test_etl_historical_loader.py — psycopg2 objects are MagicMocks that
simulate context-manager semantics.

Run:
    pytest tests/unit/test_data_engineer_sim090.py -v
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so package imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.etl.etl_historical_loader import (  # noqa: E402
    ETL_DB_POOL_MAX,
    ETL_DB_POOL_MIN,
    HistoricalDataLoader,
)


def _make_pooled_conn() -> MagicMock:
    """A MagicMock psycopg2 connection that honours context-manager use.

    The loader uses ``with conn.cursor() as cur:`` extensively, so the cursor
    must support the context-manager protocol too.
    """
    cur = MagicMock()
    cur.fetchone.return_value = (1,)  # default: "row exists" → short-circuit
    cur.fetchall.return_value = []
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False

    conn = MagicMock()
    conn.cursor.return_value = cur
    # The loader's _get_conn yields the bare connection (it is no longer a
    # context manager itself), but several call-sites also do `with conn:` —
    # support both shapes defensively.
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


def _make_mock_pool() -> MagicMock:
    """A MagicMock standing in for a ThreadedConnectionPool.

    ``getconn()`` hands out a fresh pooled-connection mock each call; ``putconn``
    and ``closeall`` are plain mocks we assert on.
    """
    pool = MagicMock()
    pool.getconn.side_effect = lambda *a, **k: _make_pooled_conn()
    return pool


class TestSim090PoolCreatedOnce:
    """The pool must be constructed exactly once and shared across games."""

    def test_pool_created_once_across_multiple_game_loads(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        mock_pool = _make_mock_pool()
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
            return_value=mock_pool,
        ) as pool_ctor:
            # Drive several DB round-trips spanning what would be multiple games.
            # _game_already_loaded / quality_report both go through _get_conn.
            loader._game_already_loaded(745001)
            loader._game_already_loaded(745002)
            loader._game_already_loaded(745003)
            loader.quality_report(game_pk=745001)

        assert pool_ctor.call_count == 1, (
            "SIM-090: the ThreadedConnectionPool must be created exactly once "
            f"and reused, not per round-trip. Got {pool_ctor.call_count} constructions."
        )

    def test_pool_constructed_with_env_configured_bounds(self):
        """minconn/maxconn must come from the ETL_DB_POOL_* knobs and the DSN."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        mock_pool = _make_mock_pool()
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
            return_value=mock_pool,
        ) as pool_ctor:
            loader._game_already_loaded(745001)

        kwargs = pool_ctor.call_args.kwargs
        assert kwargs.get("minconn") == ETL_DB_POOL_MIN
        assert kwargs.get("maxconn") == ETL_DB_POOL_MAX
        assert kwargs.get("dsn") == "postgresql://test/db"

    def test_no_pool_created_when_db_never_touched(self):
        """Lazy init: constructing a loader must not open a pool by itself."""
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
        ) as pool_ctor:
            HistoricalDataLoader(dsn="postgresql://test/db")
        pool_ctor.assert_not_called()


class TestSim090GetconnPutconnPerRoundTrip:
    """Per round-trip the loader must borrow + return, never connect + close."""

    def test_getconn_putconn_used_not_fresh_connect(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        mock_pool = _make_mock_pool()
        with (
            patch(
                "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
                return_value=mock_pool,
            ),
            patch(
                "pipeline.etl.etl_historical_loader.psycopg2.connect",
            ) as raw_connect,
        ):
            loader._game_already_loaded(745001)
            loader._game_already_loaded(745002)
            loader._game_already_loaded(745003)

        # Each game-existence check borrows and returns exactly one connection.
        assert mock_pool.getconn.call_count == 3
        assert mock_pool.putconn.call_count == 3
        # And critically, the old per-call psycopg2.connect path is gone.
        raw_connect.assert_not_called()

    def test_borrowed_connection_is_returned_even_on_error(self):
        """If a round-trip raises, the connection must still be putconn'd."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        boom_conn = _make_pooled_conn()
        boom_conn.cursor.side_effect = RuntimeError("query exploded")

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = boom_conn

        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
            return_value=mock_pool,
        ):
            with loader._get_conn() as conn:
                try:
                    with conn.cursor():
                        pass
                except RuntimeError:
                    pass

        mock_pool.putconn.assert_called_once_with(boom_conn)


class TestSim090Shutdown:
    """close() must tear the pool down via closeall()."""

    def test_close_calls_closeall(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        mock_pool = _make_mock_pool()
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
            return_value=mock_pool,
        ):
            loader._game_already_loaded(745001)  # forces pool creation
            loader.close()

        mock_pool.closeall.assert_called_once()

    def test_close_is_safe_without_pool(self):
        """close() on a loader that never touched the DB must be a no-op."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        # Should not raise even though no pool was ever created.
        loader.close()

    def test_close_is_idempotent(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")

        mock_pool = _make_mock_pool()
        with patch(
            "pipeline.etl.etl_historical_loader.psycopg2.pool.ThreadedConnectionPool",
            return_value=mock_pool,
        ):
            loader._game_already_loaded(745001)
            loader.close()
            loader.close()  # second call must not blow up or double-closeall

        mock_pool.closeall.assert_called_once()
