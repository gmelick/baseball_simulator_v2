"""
Integration test: Live pipeline lineup_state upsert — SIM-082 / SIM-145
========================================================================
Drives the real ``LiveIngestionPipeline._upsert_lineup_state()``
(pipeline/live/live_ingestion_pipeline.py) against the migrated schema:

  * ON CONFLICT (game_pk) WHERE is_live_game=TRUE correctly deduplicates rows
    (requires the unique partial index from migration 0002 / SIM-082)
  * A second upsert for the same game updates the existing row rather than
    inserting a duplicate — verified by the ``session_id`` staying put
  * The upsert is idempotent: N identical calls produce exactly 1 row

This test intentionally exercises the database path that was silently broken
before SIM-082 — the unique partial index didn't exist, so PostgreSQL rejected
the ON CONFLICT specification outright and live game state was never persisted.

Schema note
-----------
``sim.lineup_state`` (migration 0001) stores the whole game state as a single
``game_state JSONB`` column — there are no per-field ``inning`` / ``outs`` /
``home_score`` columns.  The production upsert writes exactly
``(game_pk, is_live_game, game_state, expires_at)`` and lets the remaining
columns take their DDL defaults.

Isolation note
--------------
The pipeline writes through its own asyncpg pool and each statement commits, so
these tests cannot ride the rolled-back ``pg_connection`` transaction; the
fixture deletes the rows it created on teardown instead.

Acceptance criteria (SIM-082):
  "ON CONFLICT clause in _upsert_lineup_state() references the new partial
   index; integration test passes."

Run:
    pytest tests/integration/test_live_pipeline_upsert.py -v -m integration
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: game_pks owned by this module — removed on fixture teardown.
_TEST_GAME_PKS = (740001, 740002, 740003, 740004, 740005)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_pipeline(asyncpg_pool) -> AsyncGenerator[LiveIngestionPipeline]:
    """Yield a ``LiveIngestionPipeline`` whose ``_db`` is the test asyncpg pool.

    ``__init__`` insists on a DSN + Redis URL and constructs an odds provider,
    none of which the upsert path touches, so we use the repo's standard
    constructor-bypass pattern (``__new__``) and inject only the pool that
    ``start()`` would otherwise create.
    """
    pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    pipeline._db = asyncpg_pool
    try:
        yield pipeline
    finally:
        await asyncpg_pool.execute(
            "DELETE FROM sim.lineup_state WHERE game_pk = ANY($1::int[])",
            list(_TEST_GAME_PKS),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _game_state(
    *,
    inning: int = 1,
    inning_half: str = "top",
    outs: int = 0,
    home_score: int = 0,
    away_score: int = 0,
) -> dict[str, Any]:
    """Build a game_state payload shaped like the one GameStateBuilder emits."""
    return {
        "inning": inning,
        "inning_half": inning_half,
        "outs": outs,
        "home_score": home_score,
        "away_score": away_score,
        "home_team_id": 147,  # Yankees (arbitrary)
        "away_team_id": 111,  # Orioles (arbitrary)
        "home_batting_order": [100, 101, 102, 103, 104, 105, 106, 107, 108],
        "away_batting_order": [200, 201, 202, 203, 204, 205, 206, 207, 208],
    }


async def _row_count(pool, game_pk: int) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM sim.lineup_state WHERE game_pk = $1 AND is_live_game = TRUE",
        game_pk,
    )


async def _get_row(pool, game_pk: int) -> dict[str, Any] | None:
    """Return the live row for ``game_pk`` with ``game_state`` decoded."""
    row = await pool.fetchrow(
        "SELECT session_id, game_state, is_live_game, created_at, updated_at, expires_at "
        "FROM sim.lineup_state WHERE game_pk = $1 AND is_live_game = TRUE",
        game_pk,
    )
    if row is None:
        return None
    out = dict(row)
    # asyncpg hands JSONB back as text unless a codec is registered.
    out["game_state"] = json.loads(out["game_state"])
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLivePipelineUpsert:
    """sim.lineup_state ON CONFLICT upsert works correctly end-to-end."""

    async def test_initial_insert_creates_one_row(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """A fresh upsert inserts exactly one live row carrying the game state."""
        game_pk = 740001
        state = _game_state(inning=1, outs=0)

        await live_pipeline._upsert_lineup_state(game_pk, state)

        assert await _row_count(asyncpg_pool, game_pk) == 1

        row = await _get_row(asyncpg_pool, game_pk)
        assert row is not None
        assert row["is_live_game"] is True
        assert row["game_state"] == state
        # The production upsert stamps a 24h TTL that sim.purge_expired_sessions()
        # later reads; a missing/absent expiry would make live rows immortal.
        assert row["expires_at"] > row["created_at"]

    async def test_second_upsert_updates_not_inserts(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """A second upsert for the same game updates the row — no duplicate created.

        This is the core regression test for SIM-082.  Before the unique partial
        index existed, PostgreSQL rejected the ON CONFLICT specification and the
        row was never written at all.

        The ``session_id`` check is what proves this was an UPDATE: the column
        defaults to a fresh ``uuid_generate_v4()`` on every INSERT, so a stable
        id across the two calls can only mean the existing row was updated.
        """
        game_pk = 740002

        # First upsert: top of the 1st, 0 outs, 0-0
        await live_pipeline._upsert_lineup_state(
            game_pk, _game_state(inning=1, inning_half="top", outs=0)
        )
        first = await _get_row(asyncpg_pool, game_pk)
        assert first is not None

        # Second upsert: bottom of the 3rd, 2 outs, 2-1
        second_state = _game_state(
            inning=3, inning_half="bottom", outs=2, home_score=2, away_score=1
        )
        await live_pipeline._upsert_lineup_state(game_pk, second_state)

        # Still exactly 1 row
        assert await _row_count(asyncpg_pool, game_pk) == 1

        row = await _get_row(asyncpg_pool, game_pk)
        assert row is not None
        assert row["session_id"] == first["session_id"], (
            "session_id changed — the second call INSERTed a new row instead of "
            "updating the existing one"
        )
        assert row["game_state"] == second_state
        assert row["game_state"]["inning"] == 3
        assert row["game_state"]["inning_half"] == "bottom"
        assert row["game_state"]["outs"] == 2
        assert row["game_state"]["home_score"] == 2
        assert row["game_state"]["away_score"] == 1

    async def test_idempotent_upsert_10_times(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """N identical upserts for the same game produce exactly 1 row."""
        game_pk = 740003
        state = _game_state(inning=5, outs=1, home_score=3)

        for _ in range(10):
            await live_pipeline._upsert_lineup_state(game_pk, state)

        assert await _row_count(asyncpg_pool, game_pk) == 1
        row = await _get_row(asyncpg_pool, game_pk)
        assert row is not None
        assert row["game_state"] == state

    async def test_distinct_games_get_distinct_rows(
        self, live_pipeline: LiveIngestionPipeline, asyncpg_pool
    ) -> None:
        """Two different live game_pks each get their own independent row."""
        game_a, game_b = 740004, 740005

        await live_pipeline._upsert_lineup_state(game_a, _game_state(home_score=1))
        await live_pipeline._upsert_lineup_state(game_b, _game_state(home_score=5))

        assert await _row_count(asyncpg_pool, game_a) == 1
        assert await _row_count(asyncpg_pool, game_b) == 1

        row_a = await _get_row(asyncpg_pool, game_a)
        row_b = await _get_row(asyncpg_pool, game_b)
        assert row_a is not None and row_b is not None
        assert row_a["game_state"]["home_score"] == 1
        assert row_b["game_state"]["home_score"] == 5
        assert row_a["session_id"] != row_b["session_id"]

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
        # Confirm it really is a UNIQUE partial index (WHERE clause) — a plain
        # index would not satisfy the ON CONFLICT inference.
        indexdef: str = row[0]
        assert "unique index" in indexdef.lower(), (
            f"idx_lineup_state_live_game must be UNIQUE for ON CONFLICT: {indexdef}"
        )
        assert "is_live_game" in indexdef.lower(), (
            f"Index definition does not reference is_live_game: {indexdef}"
        )
