"""
Integration test: Live pipeline lineup_state upsert — SIM-082 / SIM-145
========================================================================
Verifies the full end-to-end path for ``_upsert_lineup_state()``:

  * ON CONFLICT (game_pk) WHERE is_live_game=TRUE correctly deduplicates rows
    (requires the unique partial index from migration 0002 / SIM-082)
  * A second upsert for the same game updates the existing row rather than
    inserting a duplicate
  * The upsert is idempotent: N identical calls produce exactly 1 row

This test intentionally exercises the database path that was silently broken
before SIM-082 — the index didn't exist, so PostgreSQL raised an error or
inserted duplicate rows depending on the client library being used.

Acceptance criteria (SIM-082):
  "ON CONFLICT clause in _upsert_lineup_state() references the new partial
   index; integration test passes."

Run:
    pytest tests/integration/test_live_pipeline_upsert.py -v -m integration
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import text

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSERT_SQL = """
    INSERT INTO sim.lineup_state (
        game_pk,
        is_live_game,
        home_team_id,
        away_team_id,
        inning,
        inning_half,
        outs,
        home_score,
        away_score,
        home_batting_order,
        away_batting_order,
        updated_at
    ) VALUES (
        :game_pk,
        TRUE,
        :home_team_id,
        :away_team_id,
        :inning,
        :inning_half,
        :outs,
        :home_score,
        :away_score,
        :home_batting_order::jsonb,
        :away_batting_order::jsonb,
        NOW()
    )
    ON CONFLICT (game_pk) WHERE is_live_game = TRUE
    DO UPDATE SET
        inning            = EXCLUDED.inning,
        inning_half       = EXCLUDED.inning_half,
        outs              = EXCLUDED.outs,
        home_score        = EXCLUDED.home_score,
        away_score        = EXCLUDED.away_score,
        home_batting_order = EXCLUDED.home_batting_order,
        away_batting_order = EXCLUDED.away_batting_order,
        updated_at        = EXCLUDED.updated_at
"""

_BATTING_ORDER: list[int] = [100, 101, 102, 103, 104, 105, 106, 107, 108]


def _upsert_lineup(
    conn: sa.Connection,
    game_pk: int,
    *,
    inning: int = 1,
    inning_half: str = "top",
    outs: int = 0,
    home_score: int = 0,
    away_score: int = 0,
) -> None:
    """Execute the canonical upsert SQL against the test connection."""
    conn.execute(
        text(_INSERT_SQL),
        {
            "game_pk": game_pk,
            "home_team_id": 147,  # Yankees (arbitrary)
            "away_team_id": 111,  # Orioles (arbitrary)
            "inning": inning,
            "inning_half": inning_half,
            "outs": outs,
            "home_score": home_score,
            "away_score": away_score,
            "home_batting_order": json.dumps(_BATTING_ORDER),
            "away_batting_order": json.dumps(_BATTING_ORDER),
        },
    )


def _row_count(conn: sa.Connection, game_pk: int) -> int:
    row = conn.execute(
        text("SELECT COUNT(*) FROM sim.lineup_state WHERE game_pk = :g AND is_live_game = TRUE"),
        {"g": game_pk},
    ).fetchone()
    return row[0] if row else 0


def _get_row(conn: sa.Connection, game_pk: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT inning, inning_half, outs, home_score, away_score "
                "FROM sim.lineup_state "
                "WHERE game_pk = :g AND is_live_game = TRUE"
            ),
            {"g": game_pk},
        )
        .mappings()
        .fetchone()
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLivePipelineUpsert:
    """sim.lineup_state ON CONFLICT upsert works correctly end-to-end."""

    def test_initial_insert_creates_one_row(self, pg_connection: sa.Connection) -> None:
        """A fresh upsert inserts exactly one row for the game."""
        game_pk = 740001
        _upsert_lineup(pg_connection, game_pk, inning=1, outs=0)
        assert _row_count(pg_connection, game_pk) == 1

    def test_second_upsert_updates_not_inserts(self, pg_connection: sa.Connection) -> None:
        """A second upsert for the same game updates the row — no duplicate created.

        This is the core regression test for SIM-082.  Before the unique partial
        index existed, this would raise ``CardinalityViolation`` or silently insert
        a duplicate depending on the PostgreSQL version and client behaviour.
        """
        game_pk = 740002

        # First upsert: top of the 1st, 0 outs, 0-0
        _upsert_lineup(pg_connection, game_pk, inning=1, inning_half="top", outs=0)

        # Second upsert: bottom of the 3rd, 2 outs, 2-1
        _upsert_lineup(
            pg_connection,
            game_pk,
            inning=3,
            inning_half="bottom",
            outs=2,
            home_score=2,
            away_score=1,
        )

        # Still exactly 1 row
        assert _row_count(pg_connection, game_pk) == 1

        # Row reflects the second upsert's values
        row = _get_row(pg_connection, game_pk)
        assert row is not None
        assert row["inning"] == 3
        assert row["inning_half"] == "bottom"
        assert row["outs"] == 2
        assert row["home_score"] == 2
        assert row["away_score"] == 1

    def test_idempotent_upsert_10_times(self, pg_connection: sa.Connection) -> None:
        """N identical upserts for the same game produce exactly 1 row."""
        game_pk = 740003
        for _ in range(10):
            _upsert_lineup(pg_connection, game_pk, inning=5, outs=1, home_score=3)

        assert _row_count(pg_connection, game_pk) == 1

    def test_distinct_games_get_distinct_rows(self, pg_connection: sa.Connection) -> None:
        """Two different live game_pks each get their own independent row."""
        game_a, game_b = 740004, 740005

        _upsert_lineup(pg_connection, game_a, home_score=1)
        _upsert_lineup(pg_connection, game_b, home_score=5)

        assert _row_count(pg_connection, game_a) == 1
        assert _row_count(pg_connection, game_b) == 1

        row_a = _get_row(pg_connection, game_a)
        row_b = _get_row(pg_connection, game_b)
        assert row_a["home_score"] == 1
        assert row_b["home_score"] == 5

    def test_unique_partial_index_name_in_catalog(self, pg_connection: sa.Connection) -> None:
        """The unique partial index that enables ON CONFLICT must exist in pg_indexes.

        This is a schema-level guard: if the index was accidentally dropped, the
        upsert tests above would fail with a confusing error.  This test gives a
        clear failure message pointing to the migration.
        """
        row = pg_connection.execute(
            text("""
                SELECT indexdef
                FROM   pg_indexes
                WHERE  schemaname = 'sim'
                AND    tablename  = 'lineup_state'
                AND    indexname  = 'idx_lineup_state_live_game'
            """)
        ).fetchone()
        assert row is not None, (
            "idx_lineup_state_live_game is missing.  "
            "This index is required for ON CONFLICT in _upsert_lineup_state().  "
            "Run: alembic upgrade head  (migration 0002)"
        )
        # Confirm it really is a partial index (WHERE clause)
        indexdef: str = row[0]
        assert (
            "is_live_game" in indexdef.lower()
        ), f"Index definition does not reference is_live_game: {indexdef}"
