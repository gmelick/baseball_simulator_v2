"""sim088_drop_pitches_pitch_type_index

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-06

SIM-088: Drop idx_pitches_pitch_type — wasted write overhead.

Background:
    The schema comment on raw.pitches.pitch_type explicitly says it is
    "stored for reference/audit only. Similarity engine uses GMM components."
    No hot path in any pipeline file filters by pitch_type as a primary
    predicate, yet the single-column index ``idx_pitches_pitch_type`` adds
    ~15 MB of write overhead per season per ingest.

What we keep:
    The compound (pitcher, pitch_type) index ``idx_pitches_pitcher_type``
    is retained — it supports per-pitcher pitch-type breakdown queries that
    are common in ad-hoc analysis and the cost of carrying it is low.

Re-adding for debugging:
    If a future investigation needs the standalone index, add it
    CONCURRENTLY at runtime — there is no need to commit it back to the
    schema for one-off use.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pitches_pitch_type")


def downgrade() -> None:
    # Re-create the index as it existed before SIM-088.
    op.execute("CREATE INDEX IF NOT EXISTS idx_pitches_pitch_type ON raw.pitches(pitch_type)")
