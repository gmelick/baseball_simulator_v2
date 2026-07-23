"""
Integration test: ETL → raw.etl_data_freshness flow — SIM-083 / SIM-145
========================================================================
Verifies the end-to-end write path for the ETL freshness tracking system
against the *canonical* schema created by Alembic migration 0003:

    raw.etl_data_freshness (
        entity_type  VARCHAR(10) NOT NULL,   -- 'pitcher' | 'batter'
        entity_id    INTEGER     NOT NULL,   -- MLBAM player id
        last_game_pk INTEGER     NOT NULL,
        last_date    DATE        NOT NULL,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (entity_type, entity_id)
    )

Coverage:

  1. ``raw.etl_data_freshness`` exists and carries exactly the columns above
     (previously this table only existed as a dead string constant — SIM-083)
  2. The production writer records one row per (entity_type, entity_id)
  3. A later game for the same entity upserts in place — no duplicate row
  4. The upsert's monotonicity guard holds: an *older* game must never
     overwrite a newer one
  5. ``raw.pipeline_run_log`` captures the running → finished job lifecycle

Rather than re-implementing the ETL's SQL, the write-path tests drive the real
production code:

  * ``HistoricalDataLoader._log_freshness()`` (pipeline/etl/etl_historical_loader.py)
    — the sole writer of raw.etl_data_freshness.
  * ``OpeningLineJob._start_log_row()`` / ``._finish_log_row()``
    (pipeline/etl/opening_line_job.py) — a real writer of raw.pipeline_run_log.

That keeps the DDL and the production upserts provably in sync: if either drifts,
these tests fail.

Isolation note
--------------
Both production writers own their own connection (a psycopg2 pool and an asyncpg
pool respectively) and COMMIT, so they cannot ride the rolled-back
``pg_connection`` transaction.  The fixtures below delete the rows these tests
create on teardown instead.

Acceptance criteria (SIM-083):
  "Integration test writes a freshness record and reads it back without error."

Run:
    pytest tests/integration/test_etl_flow.py -v -m integration
"""

from __future__ import annotations

import datetime
from collections.abc import Generator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from pipeline.etl.etl_historical_loader import HistoricalDataLoader
from pipeline.etl.opening_line_job import OpeningLineJob
from tests.integration.conftest import assert_table_exists

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Synthetic MLBAM ids used by these tests.  Far above any real player id so the
#: teardown DELETE can target them without touching anything else.
_TEST_ENTITY_ID_BASE = 9_900_000

#: The exact column set migration 0003 creates for raw.etl_data_freshness.
_FRESHNESS_COLUMNS = {
    "entity_type",
    "entity_id",
    "last_game_pk",
    "last_date",
    "updated_at",
}

#: job_name values written by the pipeline_run_log tests (cleaned up on teardown).
_TEST_JOB_NAMES = ("opening_line_job",)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def etl_loader(pg_container, pg_engine: sa.Engine) -> Generator[HistoricalDataLoader]:
    """Yield a real ``HistoricalDataLoader`` bound to the test container.

    ``_log_freshness()`` opens its own psycopg2 pool from ``self.dsn`` and
    commits, so the loader needs a plain ``postgresql://`` DSN (testcontainers
    hands back the SQLAlchemy ``postgresql+psycopg2://`` form).
    """
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
    loader = HistoricalDataLoader(dsn=dsn)
    try:
        yield loader
    finally:
        loader.close()
        with pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM raw.etl_data_freshness WHERE entity_id >= :base"),
                {"base": _TEST_ENTITY_ID_BASE},
            )


@pytest.fixture()
def opening_line_job(asyncpg_pool) -> Generator[OpeningLineJob]:
    """Yield an ``OpeningLineJob`` whose ``_db`` is the test asyncpg pool.

    ``__init__`` only stores config (it does not connect), so we construct it
    normally and inject the pool that ``_connect()`` would otherwise build.
    """
    job = OpeningLineJob(dsn="postgresql://unused")
    job._db = asyncpg_pool
    yield job


@pytest.fixture(autouse=True)
def _purge_run_log(pg_engine: sa.Engine) -> Generator[None]:
    """Remove pipeline_run_log rows written by this module after each test."""
    yield
    with pg_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM raw.pipeline_run_log WHERE job_name = ANY(:names)"),
            {"names": list(_TEST_JOB_NAMES)},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pitch_rows(
    game_date: datetime.date,
    *,
    pitcher_ids: tuple[int, ...],
    batter_ids: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Build the minimal Statcast-shaped rows ``_log_freshness()`` reads.

    The production method only touches ``game_date``, ``pitcher`` and ``batter``;
    everything else in a real pitch row is irrelevant to freshness tracking.
    """
    rows: list[dict[str, Any]] = []
    for i, pitcher in enumerate(pitcher_ids):
        rows.append(
            {
                "game_date": game_date,
                "pitcher": pitcher,
                "batter": batter_ids[i % len(batter_ids)],
            }
        )
    return rows


def _read_freshness(conn: sa.Connection, entity_type: str, entity_id: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT entity_type, entity_id, last_game_pk, last_date, updated_at "
                "FROM raw.etl_data_freshness "
                "WHERE entity_type = :t AND entity_id = :i"
            ),
            {"t": entity_type, "i": entity_id},
        )
        .mappings()
        .fetchone()
    )
    return dict(row) if row else None


def _freshness_row_count(conn: sa.Connection, entity_type: str, entity_id: int) -> int:
    return (
        conn.execute(
            text(
                "SELECT COUNT(*) FROM raw.etl_data_freshness "
                "WHERE entity_type = :t AND entity_id = :i"
            ),
            {"t": entity_type, "i": entity_id},
        ).scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEtlFreshnessTable:
    """raw.etl_data_freshness DDL (migration 0003 / SIM-083) is correct."""

    def test_freshness_table_exists(self, pg_connection: sa.Connection) -> None:
        """raw.etl_data_freshness must exist after migrations.

        Before SIM-083 this table only existed as the dead string constant
        FRESHNESS_TABLE_DDL which was defined but never executed.
        """
        assert_table_exists(pg_connection, "raw", "etl_data_freshness")

    def test_freshness_table_has_expected_columns(self, pg_connection: sa.Connection) -> None:
        """The column set must match migration 0003 exactly, and the PK must be composite.

        Exact equality (not just "contains") is deliberate: an extra or renamed
        column here would silently desynchronise ``_log_freshness()``'s INSERT
        column list from the table.
        """
        rows = pg_connection.execute(
            text("""
                SELECT column_name
                FROM   information_schema.columns
                WHERE  table_schema = 'raw'
                AND    table_name   = 'etl_data_freshness'
                ORDER  BY ordinal_position
            """)
        ).fetchall()
        found = {r[0] for r in rows}
        assert found == _FRESHNESS_COLUMNS, (
            f"raw.etl_data_freshness column drift — "
            f"missing: {_FRESHNESS_COLUMNS - found or '{}'}, "
            f"unexpected: {found - _FRESHNESS_COLUMNS or '{}'}"
        )

        # The (entity_type, entity_id) composite PK is what _log_freshness()'s
        # ON CONFLICT target relies on; without it the upsert cannot resolve.
        pk_cols = pg_connection.execute(
            text("""
                SELECT     kcu.column_name
                FROM       information_schema.table_constraints tc
                JOIN       information_schema.key_column_usage  kcu
                       ON  kcu.constraint_name  = tc.constraint_name
                      AND  kcu.constraint_schema = tc.constraint_schema
                WHERE      tc.table_schema    = 'raw'
                AND        tc.table_name      = 'etl_data_freshness'
                AND        tc.constraint_type = 'PRIMARY KEY'
                ORDER  BY  kcu.ordinal_position
            """)
        ).fetchall()
        assert [r[0] for r in pk_cols] == ["entity_type", "entity_id"], (
            "raw.etl_data_freshness PRIMARY KEY must be (entity_type, entity_id) — "
            "it is the ON CONFLICT target in _log_freshness()"
        )


class TestEtlFreshnessWritePath:
    """The real ``HistoricalDataLoader._log_freshness()`` works end-to-end."""

    def test_write_freshness_record(
        self, etl_loader: HistoricalDataLoader, pg_connection: sa.Connection
    ) -> None:
        """The production writer records one row per pitcher and per batter.

        This was broken before SIM-083: _log_freshness() ran INSERT INTO
        raw.etl_data_freshness against a table that was never created, so every
        call raised UndefinedTable after the pitch rows had already been inserted.
        """
        pitcher = _TEST_ENTITY_ID_BASE + 1
        batter = _TEST_ENTITY_ID_BASE + 2
        game_pk = 745001
        game_date = datetime.date(2025, 9, 28)

        etl_loader._log_freshness(
            game_pk,
            _pitch_rows(game_date, pitcher_ids=(pitcher,), batter_ids=(batter,)),
        )

        pitcher_row = _read_freshness(pg_connection, "pitcher", pitcher)
        assert pitcher_row is not None, "no 'pitcher' freshness row was written"
        assert pitcher_row["last_game_pk"] == game_pk
        assert pitcher_row["last_date"] == game_date

        batter_row = _read_freshness(pg_connection, "batter", batter)
        assert batter_row is not None, "no 'batter' freshness row was written"
        assert batter_row["last_game_pk"] == game_pk
        assert batter_row["last_date"] == game_date

    def test_upsert_updates_existing_record(
        self, etl_loader: HistoricalDataLoader, pg_connection: sa.Connection
    ) -> None:
        """A later game for the same entity updates the row rather than duplicating it."""
        pitcher = _TEST_ENTITY_ID_BASE + 10
        batter = _TEST_ENTITY_ID_BASE + 11

        etl_loader._log_freshness(
            745010,
            _pitch_rows(datetime.date(2025, 4, 1), pitcher_ids=(pitcher,), batter_ids=(batter,)),
        )
        etl_loader._log_freshness(
            745011,
            _pitch_rows(datetime.date(2025, 9, 28), pitcher_ids=(pitcher,), batter_ids=(batter,)),
        )

        row = _read_freshness(pg_connection, "pitcher", pitcher)
        assert row is not None
        assert row["last_game_pk"] == 745011
        assert row["last_date"] == datetime.date(2025, 9, 28)

        # The composite PK means there can only ever be one row per entity.
        assert _freshness_row_count(pg_connection, "pitcher", pitcher) == 1

    def test_older_game_does_not_overwrite_newer(
        self, etl_loader: HistoricalDataLoader, pg_connection: sa.Connection
    ) -> None:
        """The upsert's monotonicity guard must reject out-of-order backfills.

        ``_log_freshness()`` ends its ON CONFLICT clause with
        ``WHERE EXCLUDED.last_date > raw.etl_data_freshness.last_date``.  A
        backfill that replays an *older* game must therefore leave the newer
        record untouched — otherwise "last loaded game" would go backwards and
        stale-profile detection would silently re-load everything.
        """
        pitcher = _TEST_ENTITY_ID_BASE + 20
        batter = _TEST_ENTITY_ID_BASE + 21
        newer = datetime.date(2025, 9, 28)
        older = datetime.date(2025, 4, 1)

        etl_loader._log_freshness(
            745020, _pitch_rows(newer, pitcher_ids=(pitcher,), batter_ids=(batter,))
        )
        etl_loader._log_freshness(
            745019, _pitch_rows(older, pitcher_ids=(pitcher,), batter_ids=(batter,))
        )

        row = _read_freshness(pg_connection, "pitcher", pitcher)
        assert row is not None
        assert row["last_date"] == newer, "an older game overwrote a newer freshness record"
        assert row["last_game_pk"] == 745020

    def test_pitcher_and_batter_are_tracked_independently(
        self, etl_loader: HistoricalDataLoader, pg_connection: sa.Connection
    ) -> None:
        """entity_type partitions the key space: the same id can be both roles.

        Two-way players (and, more commonly, id collisions across the two
        namespaces) must not clobber each other — that is exactly what the
        composite (entity_type, entity_id) PK buys.
        """
        shared_id = _TEST_ENTITY_ID_BASE + 30

        # Same id appears as the pitcher in one game and the batter in a later one.
        etl_loader._log_freshness(
            745030,
            _pitch_rows(
                datetime.date(2025, 5, 1),
                pitcher_ids=(shared_id,),
                batter_ids=(_TEST_ENTITY_ID_BASE + 31,),
            ),
        )
        etl_loader._log_freshness(
            745031,
            _pitch_rows(
                datetime.date(2025, 6, 1),
                pitcher_ids=(_TEST_ENTITY_ID_BASE + 32,),
                batter_ids=(shared_id,),
            ),
        )

        pitcher_row = _read_freshness(pg_connection, "pitcher", shared_id)
        batter_row = _read_freshness(pg_connection, "batter", shared_id)
        assert pitcher_row is not None and batter_row is not None
        assert pitcher_row["last_game_pk"] == 745030
        assert batter_row["last_game_pk"] == 745031


class TestPipelineRunLog:
    """raw.pipeline_run_log captures pipeline execution records."""

    def test_pipeline_run_log_table_exists(self, pg_connection: sa.Connection) -> None:
        assert_table_exists(pg_connection, "raw", "pipeline_run_log")

    async def test_write_successful_run_log(
        self, opening_line_job: OpeningLineJob, asyncpg_pool
    ) -> None:
        """The real start→finish lifecycle records a completed run.

        Drives ``OpeningLineJob._start_log_row()`` / ``._finish_log_row()``,
        the production writers, so the run-log DDL and their column lists stay
        in sync.
        """
        run_id = await opening_line_job._start_log_row()
        assert isinstance(run_id, int)

        # A freshly started run is 'running' with no finish timestamp.
        started = await asyncpg_pool.fetchrow(
            "SELECT job_name, status, started_at, finished_at "
            "FROM raw.pipeline_run_log WHERE id = $1",
            run_id,
        )
        assert started["job_name"] == "opening_line_job"
        assert started["status"] == "running"
        assert started["started_at"] is not None
        assert started["finished_at"] is None

        await opening_line_job._finish_log_row(
            run_id,
            "success",
            {"opening_line_games_captured": 15, "opening_prop_lines_captured": 120},
        )

        finished = await asyncpg_pool.fetchrow(
            "SELECT status, finished_at, opening_line_games_captured, "
            "       opening_prop_lines_captured, error_message "
            "FROM raw.pipeline_run_log WHERE id = $1",
            run_id,
        )
        assert finished["status"] == "success"
        assert finished["finished_at"] is not None
        assert finished["opening_line_games_captured"] == 15
        assert finished["opening_prop_lines_captured"] == 120
        assert finished["error_message"] is None

    async def test_write_failed_run_log(
        self, opening_line_job: OpeningLineJob, asyncpg_pool
    ) -> None:
        """A failed run records status='error' plus the error message.

        Also pins the status CHECK constraint: only the four documented states
        are storable, so a typo'd status fails loudly instead of being persisted.
        """
        import asyncpg

        error = "ConnectionError: MLB Stats API returned 503 after 3 retries"

        run_id = await opening_line_job._start_log_row()
        await opening_line_job._finish_log_row(run_id, "error", {}, error_message=error)

        row = await asyncpg_pool.fetchrow(
            "SELECT status, error_message, opening_line_games_captured "
            "FROM raw.pipeline_run_log WHERE id = $1",
            run_id,
        )
        assert row["status"] == "error"
        assert row["error_message"] == error
        # An empty summary dict falls back to the documented 0 counts.
        assert row["opening_line_games_captured"] == 0

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await asyncpg_pool.execute(
                "UPDATE raw.pipeline_run_log SET status = 'exploded' WHERE id = $1",
                run_id,
            )
