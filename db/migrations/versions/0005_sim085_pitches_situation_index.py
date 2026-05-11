"""sim085_pitches_situation_index

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-06

SIM-085: Composite partial situation index on raw.pitches.

Problem:
    The project plan Step 1.1 explicitly requires a composite index covering the
    full situation vector (count + outs + baserunner state) used by the
    situation-to-situation similarity engine (SIM-070).  The current schema
    has only ``idx_pitches_count_state`` on (balls, strikes, outs) plus
    individual baserunner columns indirectly via other indexes.  No index
    covers the full situation vector, so situation similarity queries fall
    back to a sequential scan over ~700K rows per season.

Fix:
    Create a partial composite index on
    (inning, outs, balls, strikes, on_1b, on_2b, on_3b)
    WHERE data_quality_flag = FALSE.  Partial — flagged rows are excluded
    from the simulation pool anyway, so indexing them wastes write overhead.
    Targets the exact predicate shape used by the engine.

Acceptance gate:
    EXPLAIN ANALYZE on a representative situation lookup must show an
    index scan on idx_pitches_situation, not a sequential scan.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_pitches_situation
            ON raw.pitches(inning, outs, balls, strikes, on_1b, on_2b, on_3b)
            WHERE data_quality_flag = FALSE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pitches_situation")
