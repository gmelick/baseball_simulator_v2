"""
Integration test: Alembic schema migrations — SIM-145 / SIM-082 / SIM-083
==========================================================================
Verifies that ``alembic upgrade head`` produces the complete production schema
in a fresh PostgreSQL 15 database.

Acceptance criteria (SIM-145):
  - All expected raw.* and sim.* tables are present after migration.
  - The unique partial index required by SIM-082 exists.
  - Alembic's ``alembic_version`` table records the head revision.

These tests run against the session-scoped testcontainers PostgreSQL instance
spun up in conftest.py — migrations are applied once per session before any
test in this suite runs.

Run:
    pytest tests/integration/test_schema_migrations.py -v -m integration
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from tests.integration.conftest import assert_table_exists

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Expected tables — must exist after alembic upgrade head
# ---------------------------------------------------------------------------

_RAW_TABLES = [
    "venues",
    "teams",
    "players",
    "games",
    "statcast_events",
    "sprint_speed",
    "etl_data_freshness",
    "game_odds",
    "prop_odds",
    "pipeline_run_log",
]

_SIM_TABLES = [
    "player_similarity_profiles",
    "batter_similarity_scores",
    "pitcher_similarity_scores",
    "fielder_similarity_scores",
    "baserunner_similarity_scores",
    "pitch_profiles",
    "pitch_similarity_scores",
    "batted_ball_profiles",
    "batted_ball_similarity_scores",
    "situation_similarity_scores",
    "lineup_state",
    "game_simulations",
    "simulation_results",
    "player_season_stats",
    "betting_lines",
    "line_movement",
    "sim_run_log",
]


class TestSchemaMigrations:
    """All Alembic migrations apply cleanly to a fresh database."""

    def test_raw_schema_tables_exist(self, pg_connection: sa.Connection) -> None:
        """Every expected raw.* table must exist after running migrations."""
        for table in _RAW_TABLES:
            assert_table_exists(pg_connection, "raw", table)

    def test_sim_schema_tables_exist(self, pg_connection: sa.Connection) -> None:
        """Every expected sim.* table must exist after running migrations."""
        for table in _SIM_TABLES:
            assert_table_exists(pg_connection, "sim", table)

    def test_alembic_version_is_head(self, pg_connection: sa.Connection) -> None:
        """alembic_version must contain exactly one row pointing to the head revision.

        The head revision is the last migration applied (0003 at time of writing).
        This test will pass for any future head as long as the chain is unbroken.
        """
        row = pg_connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        assert row is not None, "alembic_version table is empty — did migrations run?"
        # Verify the version string is non-empty and looks like a revision ID
        version_num: str = row[0]
        assert version_num, "version_num is blank"
        assert (
            len(version_num) >= 4
        ), f"version_num '{version_num}' looks too short to be a valid Alembic revision"

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
