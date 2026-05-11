"""
Integration test: ETL → raw.etl_data_freshness flow — SIM-083 / SIM-145
========================================================================
Verifies the end-to-end write path for the ETL freshness tracking system:

  1. ``raw.etl_data_freshness`` exists and has the correct schema
     (previously this table only existed as a dead string constant — SIM-083)
  2. Writing a freshness record succeeds (no UndefinedTable at runtime)
  3. Freshness records survive a second write for the same source (upsert)
  4. ``raw.pipeline_run_log`` captures pipeline execution metadata

These tests mirror what ``etl_historical_loader.py:_log_freshness()`` does at
runtime, ensuring the DDL migration (0003) and the write path stay in sync.

Acceptance criteria (SIM-083):
  "Integration test writes a freshness record and reads it back without error."

Run:
    pytest tests/integration/test_etl_flow.py -v -m integration
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from tests.integration.conftest import assert_table_exists

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UPSERT_FRESHNESS_SQL = """
    INSERT INTO raw.etl_data_freshness (
        source_name,
        last_successful_load_at,
        last_game_date_loaded,
        rows_loaded_last_run,
        pipeline_version,
        notes
    ) VALUES (
        :source_name,
        :last_successful_load_at,
        :last_game_date_loaded,
        :rows_loaded_last_run,
        :pipeline_version,
        :notes
    )
    ON CONFLICT (source_name)
    DO UPDATE SET
        last_successful_load_at = EXCLUDED.last_successful_load_at,
        last_game_date_loaded   = EXCLUDED.last_game_date_loaded,
        rows_loaded_last_run    = EXCLUDED.rows_loaded_last_run,
        pipeline_version        = EXCLUDED.pipeline_version,
        notes                   = EXCLUDED.notes
"""

_INSERT_RUN_LOG_SQL = """
    INSERT INTO raw.pipeline_run_log (
        run_id,
        pipeline_name,
        started_at,
        finished_at,
        status,
        rows_inserted,
        rows_updated,
        error_message
    ) VALUES (
        :run_id,
        :pipeline_name,
        :started_at,
        :finished_at,
        :status,
        :rows_inserted,
        :rows_updated,
        :error_message
    )
"""


def _write_freshness(
    conn: sa.Connection,
    source_name: str,
    *,
    rows: int = 1000,
    version: str = "test-1.0",
    notes: str | None = None,
) -> None:
    conn.execute(
        text(_UPSERT_FRESHNESS_SQL),
        {
            "source_name": source_name,
            "last_successful_load_at": datetime.datetime.utcnow(),
            "last_game_date_loaded": datetime.date(2025, 9, 28),
            "rows_loaded_last_run": rows,
            "pipeline_version": version,
            "notes": notes,
        },
    )


def _read_freshness(conn: sa.Connection, source_name: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT source_name, rows_loaded_last_run, pipeline_version, notes "
                "FROM raw.etl_data_freshness WHERE source_name = :s"
            ),
            {"s": source_name},
        )
        .mappings()
        .fetchone()
    )
    return dict(row) if row else None


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
        """Verify the column set of raw.etl_data_freshness matches the schema spec."""
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
        required = {
            "source_name",
            "last_successful_load_at",
            "last_game_date_loaded",
            "rows_loaded_last_run",
            "pipeline_version",
        }
        missing = required - found
        assert not missing, f"raw.etl_data_freshness is missing columns: {missing}"


class TestEtlFreshnessWritePath:
    """Write path from etl_historical_loader._log_freshness() works end-to-end."""

    def test_write_freshness_record(self, pg_connection: sa.Connection) -> None:
        """Writing a freshness record must not raise any database error.

        This was broken before SIM-083: _log_freshness() was silently a no-op
        or raised UndefinedTable because the DDL was never executed.
        """
        _write_freshness(pg_connection, "statcast_events_test", rows=5000)
        row = _read_freshness(pg_connection, "statcast_events_test")
        assert row is not None
        assert row["rows_loaded_last_run"] == 5000
        assert row["pipeline_version"] == "test-1.0"

    def test_upsert_updates_existing_record(self, pg_connection: sa.Connection) -> None:
        """A second write for the same source_name updates, not duplicates, the row."""
        source = "sprint_speed_test"

        _write_freshness(pg_connection, source, rows=1000, version="v1")
        _write_freshness(pg_connection, source, rows=2500, version="v2")

        row = _read_freshness(pg_connection, source)
        assert row is not None
        assert row["rows_loaded_last_run"] == 2500
        assert row["pipeline_version"] == "v2"

        # Confirm there's still only one row for this source
        count = pg_connection.execute(
            text("SELECT COUNT(*) FROM raw.etl_data_freshness WHERE source_name = :s"),
            {"s": source},
        ).scalar()
        assert count == 1

    def test_freshness_notes_field_accepts_null(self, pg_connection: sa.Connection) -> None:
        """notes is an optional free-text column; NULL must be accepted."""
        _write_freshness(pg_connection, "venues_test", notes=None)
        row = _read_freshness(pg_connection, "venues_test")
        assert row is not None
        assert row["notes"] is None

    def test_freshness_notes_field_accepts_text(self, pg_connection: sa.Connection) -> None:
        """notes can carry a human-readable description of the run."""
        note = "Backfill 2024 full season — 162 game slate"
        _write_freshness(pg_connection, "games_test", notes=note)
        row = _read_freshness(pg_connection, "games_test")
        assert row is not None
        assert row["notes"] == note


class TestPipelineRunLog:
    """raw.pipeline_run_log captures pipeline execution records."""

    def test_pipeline_run_log_table_exists(self, pg_connection: sa.Connection) -> None:
        assert_table_exists(pg_connection, "raw", "pipeline_run_log")

    def test_write_successful_run_log(self, pg_connection: sa.Connection) -> None:
        """A completed pipeline run can be recorded without error."""
        run_id = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        pg_connection.execute(
            text(_INSERT_RUN_LOG_SQL),
            {
                "run_id": run_id,
                "pipeline_name": "historical_loader_test",
                "started_at": now - datetime.timedelta(seconds=42),
                "finished_at": now,
                "status": "success",
                "rows_inserted": 12345,
                "rows_updated": 0,
                "error_message": None,
            },
        )
        row = (
            pg_connection.execute(
                text(
                    "SELECT pipeline_name, status, rows_inserted "
                    "FROM raw.pipeline_run_log WHERE run_id = :r"
                ),
                {"r": run_id},
            )
            .mappings()
            .fetchone()
        )
        assert row is not None
        assert row["pipeline_name"] == "historical_loader_test"
        assert row["status"] == "success"
        assert row["rows_inserted"] == 12345

    def test_write_failed_run_log(self, pg_connection: sa.Connection) -> None:
        """A failed pipeline run with an error message can be recorded."""
        run_id = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        error = "ConnectionError: MLB Stats API returned 503 after 3 retries"
        pg_connection.execute(
            text(_INSERT_RUN_LOG_SQL),
            {
                "run_id": run_id,
                "pipeline_name": "live_ingestion_test",
                "started_at": now - datetime.timedelta(seconds=5),
                "finished_at": now,
                "status": "error",
                "rows_inserted": 0,
                "rows_updated": 0,
                "error_message": error,
            },
        )
        row = (
            pg_connection.execute(
                text("SELECT status, error_message FROM raw.pipeline_run_log WHERE run_id = :r"),
                {"r": run_id},
            )
            .mappings()
            .fetchone()
        )
        assert row is not None
        assert row["status"] == "error"
        assert row["error_message"] == error
