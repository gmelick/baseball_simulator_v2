"""sim082_lineup_state_live_game_unique_index

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05

SIM-082: Add unique partial index on sim.lineup_state(game_pk) WHERE is_live_game=TRUE.

Bug:
    _upsert_lineup_state() uses ON CONFLICT (game_pk) WHERE is_live_game=TRUE but no
    matching unique partial index existed.  PostgreSQL raises a constraint error on every
    upsert call, so live game state was NEVER persisted to sim.lineup_state.

Fix:
    Create the unique partial index the ON CONFLICT clause requires.
    Index name matches the one referenced in LIVE_PIPELINE_DDL for consistency.
"""

from alembic import op

# revision identifiers
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is the missing index that caused every _upsert_lineup_state() call
    # to raise a "there is no unique or exclusion constraint matching the
    # ON CONFLICT specification" error.  Without it, is_live_game rows were
    # never written to sim.lineup_state.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lineup_state_live_game
            ON sim.lineup_state(game_pk)
            WHERE is_live_game = TRUE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_lineup_state_live_game")
