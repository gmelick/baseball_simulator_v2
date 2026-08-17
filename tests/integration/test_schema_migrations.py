"""
Integration test: Alembic schema migrations — SIM-145 / SIM-082 / SIM-083
==========================================================================
Verifies that ``alembic upgrade head`` produces the complete production schema
in a fresh PostgreSQL 15 database.

Acceptance criteria (SIM-145):
  - The raw.* and sim.* base tables match the canonical set exactly.
  - The unique partial index required by SIM-082 exists.
  - Alembic's ``alembic_version`` table records the head revision.

The table lists below are the *canonical* set produced by the migration chain
in ``db/migrations/versions/`` and are asserted for exact equality, so the test
guards drift in both directions: a table silently dropped by a migration fails,
and a table added without updating this list fails too (update the list in the
same commit as the migration that adds it).

Note that ``sim.live_games`` is a VIEW, not a base table, so it is deliberately
absent from ``_SIM_TABLES`` — the queries below filter on
``table_type = 'BASE TABLE'``.

These tests run against the session-scoped testcontainers PostgreSQL instance
spun up in conftest.py — migrations are applied once per session before any
test in this suite runs.

Run:
    pytest tests/integration/test_schema_migrations.py -v -m integration
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from tests.integration.conftest import assert_table_exists

pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Expected tables — the canonical set after `alembic upgrade head`
# Each entry is annotated with the migration that creates it.
# ---------------------------------------------------------------------------

_RAW_TABLES = {
    "venues",  # 0001
    "teams",  # 0001
    "players",  # 0001
    "managers",  # 0001
    "games",  # 0001
    "game_lineups",  # 0001
    "sprint_speed",  # 0001
    "pitches",  # 0001 — the pitch-level Statcast table
    "etl_data_freshness",  # 0003 (SIM-083)
    "game_odds",  # 0003 (SIM-083 / SIM-133)
    "prop_odds",  # 0003 (SIM-083), renamed column in 0004
    "pipeline_run_log",  # 0003 (SIM-083 / SIM-138)
    "etl_errors",  # 0011 (SIM-093)
    "game_bullpen_availability",  # 0015 (SIM-433)
    "etl_game_ingest",  # 0017 (SIM-441)
    "play_events",  # 0018 (SIM-502) — non-pitch play events
}

_SIM_TABLES = {
    "lineup_state",  # 0001 (unique partial index added in 0002 / SIM-082)
    "sim_runs",  # 0014 (SIM-356)
}

#: Views are tracked separately — information_schema.tables lists them too, so
#: the base-table assertions must filter them out to stay meaningful.
_SIM_VIEWS = {"live_games"}  # 0001


def _base_tables(conn: sa.Connection, schema: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
        ),
        {"s": schema},
    ).fetchall()
    return {r[0] for r in rows}


class TestSchemaMigrations:
    """All Alembic migrations apply cleanly to a fresh database."""

    def test_raw_schema_tables_exist(self, pg_connection: sa.Connection) -> None:
        """The raw.* base tables must match the canonical set exactly."""
        for table in sorted(_RAW_TABLES):
            assert_table_exists(pg_connection, "raw", table)

        found = _base_tables(pg_connection, "raw")
        assert found == _RAW_TABLES, (
            f"raw.* schema drift — missing: {sorted(_RAW_TABLES - found)}, "
            f"unexpected: {sorted(found - _RAW_TABLES)}. "
            "If a migration intentionally added or removed a table, update "
            "_RAW_TABLES in this file in the same commit."
        )

    def test_sim_schema_tables_exist(self, pg_connection: sa.Connection) -> None:
        """The sim.* base tables must match the canonical set exactly."""
        for table in sorted(_SIM_TABLES):
            assert_table_exists(pg_connection, "sim", table)

        found = _base_tables(pg_connection, "sim")
        assert found == _SIM_TABLES, (
            f"sim.* schema drift — missing: {sorted(_SIM_TABLES - found)}, "
            f"unexpected: {sorted(found - _SIM_TABLES)}. "
            "If a migration intentionally added or removed a table, update "
            "_SIM_TABLES in this file in the same commit."
        )

        # sim.live_games is a view over raw.games + sim.lineup_state; it is not a
        # base table but the API's live-slate read path depends on it existing.
        view_rows = pg_connection.execute(
            text("SELECT table_name FROM information_schema.views WHERE table_schema = 'sim'")
        ).fetchall()
        assert {r[0] for r in view_rows} == _SIM_VIEWS

    def test_alembic_version_is_head(self, pg_connection: sa.Connection) -> None:
        """alembic_version must record the head revision of the migration chain.

        The expected head is read from ``db/migrations/versions/`` via Alembic's
        own ScriptDirectory rather than hard-coded, so this test keeps passing as
        migrations are added — but still fails if the container was migrated to
        something other than head (a broken or branched chain).
        """
        row = pg_connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        assert row is not None, "alembic_version table is empty — did migrations run?"

        expected_head = ScriptDirectory.from_config(
            Config(str(_REPO_ROOT / "alembic.ini"))
        ).get_current_head()
        assert row[0] == expected_head, (
            f"Database is at revision '{row[0]}' but the migration chain head is "
            f"'{expected_head}' — `alembic upgrade head` did not fully apply."
        )

    def test_sim082_unique_partial_index_exists(self, pg_connection: sa.Connection) -> None:
        """SIM-082: The unique partial index on sim.lineup_state must exist.

        Without this index, ON CONFLICT (game_pk) WHERE is_live_game=TRUE silently
        fails or raises an error.  This test is the e2e verification for migration 0002.
        """
        row = pg_connection.execute(
            text("""
                SELECT 1
                FROM   pg_indexes
                WHERE  schemaname = 'sim'
                AND    tablename  = 'lineup_state'
                AND    indexname  = 'idx_lineup_state_live_game'
            """)
        ).fetchone()
        assert row is not None, (
            "idx_lineup_state_live_game not found on sim.lineup_state.  "
            "Migration 0002 (SIM-082) may not have been applied."
        )

    def test_sim083_game_odds_has_clv_columns(self, pg_connection: sa.Connection) -> None:
        """SIM-083 / SIM-133: raw.game_odds must have the four CLV-related columns.

        These columns (book, line_type, market_type, is_sharp_book) are required
        to compute Closing Line Value.  Their absence would silently corrupt all
        betting analytics.
        """
        rows = pg_connection.execute(
            text("""
                SELECT column_name
                FROM   information_schema.columns
                WHERE  table_schema = 'raw'
                AND    table_name   = 'game_odds'
                AND    column_name  IN ('book','line_type','market_type','is_sharp_book')
                ORDER  BY column_name
            """)
        ).fetchall()
        found = {r[0] for r in rows}
        expected = {"book", "line_type", "market_type", "is_sharp_book"}
        missing = expected - found
        assert not missing, (
            f"raw.game_odds is missing CLV columns: {missing}. "
            "Migrations 0003 (SIM-083 / SIM-133) may not have been applied."
        )

    # -- migration 0017 (SIM-441) ------------------------------------------
    #
    # 0017 shipped without any integration coverage. The exact-set table guard
    # above caught the new TABLE a week later, on the next scheduled run — but
    # nothing covered the four column/index changes in the same migration, and a
    # column-level drift is exactly what went undetected here before (the
    # raw.etl_data_freshness failures of 2026-06-29 → 07-20). These close that gap.

    def test_sim441_etl_game_ingest_records_terminal_outcomes(
        self, pg_connection: sa.Connection
    ) -> None:
        """0017: raw.etl_game_ingest must accept exactly the three outcomes.

        This table is what lets the loader tell "never loaded" apart from "loaded
        and legitimately produced zero pitches" — the distinction that stops a
        cancelled game being re-fetched every night forever. A widened or missing
        CHECK would let a typo'd outcome through and silently break that.
        """
        trans = pg_connection.begin_nested()
        try:
            for i, outcome in enumerate(("loaded", "empty", "failed")):
                pg_connection.execute(
                    text(
                        "INSERT INTO raw.etl_game_ingest (game_pk, season, outcome) "
                        "VALUES (:pk, 2024, :o)"
                    ),
                    {"pk": -1000 - i, "o": outcome},
                )
            with pytest.raises(sa.exc.IntegrityError):
                pg_connection.execute(
                    text(
                        "INSERT INTO raw.etl_game_ingest (game_pk, season, outcome) "
                        "VALUES (-1099, 2024, 'partially_loaded')"
                    )
                )
        finally:
            trans.rollback()

    def test_sim441_pitches_has_fielding_overflow_flag(self, pg_connection: sa.Connection) -> None:
        """0017: raw.pitches.field_assist_6_plus marks pitches whose fielding
        credits overflowed the fixed assist/putout slots. Without it, a
        multi-runner rundown looks identical to a clean play."""
        row = pg_connection.execute(
            text("""
                SELECT is_nullable, column_default
                FROM   information_schema.columns
                WHERE  table_schema = 'raw' AND table_name = 'pitches'
                AND    column_name  = 'field_assist_6_plus'
            """)
        ).fetchone()
        assert row is not None, "raw.pitches.field_assist_6_plus is missing (migration 0017)"
        is_nullable, default = row
        assert is_nullable == "NO", "the flag must be NOT NULL — NULL is not 'no overflow'"
        assert default is not None and "false" in default.lower()

    def test_sim441_players_active_column_is_gone(self, pg_connection: sa.Connection) -> None:
        """0017 dropped raw.players.active — it was never populated or read, and a
        column that always says TRUE invites being trusted."""
        row = pg_connection.execute(
            text("""
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'raw' AND table_name = 'players' AND column_name = 'active'
            """)
        ).fetchone()
        assert row is None, "raw.players.active should have been dropped by migration 0017"

        idx = pg_connection.execute(
            text(
                "SELECT 1 FROM pg_indexes WHERE schemaname='raw' AND indexname='idx_players_active'"
            )
        ).fetchone()
        assert idx is None, "idx_players_active should have been dropped with its column"

    def test_sim441_etl_errors_natural_key_rejects_duplicates(
        self, pg_connection: sa.Connection
    ) -> None:
        """0017: the error ledger must be idempotent.

        Re-running a game used to append a fresh copy of every error it hit, so
        the ledger inflated on each retry and could not be counted. Asserted
        BEHAVIOURALLY — a catalogue check would pass on a non-unique index.
        """
        trans = pg_connection.begin_nested()
        try:
            # error_messages is a TEXT[] — one row can carry several messages for
            # the same pitch, which is precisely why the natural key excludes it.
            insert = text(
                "INSERT INTO raw.etl_errors "
                "(game_pk, at_bat_number, pitch_number, error_type, error_messages) "
                "VALUES (-1001, 1, 1, 'HARD', :m)"
            )
            pg_connection.execute(insert, {"m": ["first"]})
            with pytest.raises(sa.exc.IntegrityError):
                pg_connection.execute(insert, {"m": ["different message, same natural key"]})
        finally:
            trans.rollback()

    def test_manager_ids_are_nullable(self, pg_connection: sa.Connection) -> None:
        """raw.pitches.{home,away}_manager_id must accept NULL.

        The loader no longer invents a manager when the feed has none — it used to
        fall back to ``coaches[0]``, i.e. whoever the API happened to list first.
        NOT NULL here would force that guess back.

        ⚠ This is an INVARIANT, not migration-0017 coverage. 0017 contains
        ``ALTER COLUMN ... DROP NOT NULL`` for both columns, but they were already
        nullable at 0015, so those two statements are no-ops — verified by
        migrating a throwaway database to 0015/0016/head and diffing
        ``is_nullable`` (YES at every revision). Do not cite this test as evidence
        that 0017 applied; the four tests above are.
        """
        rows = pg_connection.execute(
            text("""
                SELECT column_name, is_nullable
                FROM   information_schema.columns
                WHERE  table_schema = 'raw' AND table_name = 'pitches'
                AND    column_name IN ('home_manager_id', 'away_manager_id')
                ORDER  BY column_name
            """)
        ).fetchall()
        assert len(rows) == 2, "manager id columns missing from raw.pitches"
        for column_name, is_nullable in rows:
            assert is_nullable == "YES", (
                f"raw.pitches.{column_name} is still NOT NULL — migration 0017 did not apply"
            )

    def test_raw_etl_freshness_table_exists(self, pg_connection: sa.Connection) -> None:
        """SIM-083: raw.etl_data_freshness must exist.

        Previously this table was only created by dead string constant code that
        was never executed.  Migration 0003 creates it authoritatively.
        """
        assert_table_exists(pg_connection, "raw", "etl_data_freshness")

    def test_schemas_are_created(self, pg_connection: sa.Connection) -> None:
        """Both 'raw' and 'sim' schemas must exist."""
        for schema in ("raw", "sim"):
            row = pg_connection.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                {"s": schema},
            ).fetchone()
            assert row is not None, f"Schema '{schema}' does not exist after migration"
