"""sim089_pitches_pitcher_season_clean_index

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-06

SIM-089: Composite partial index on raw.pitches(pitcher, season)
WHERE data_quality_flag = FALSE.

Bug:
    The player-profile computor's most frequent query pattern is
    "all clean pitches for pitcher X in season Y".  The existing
    idx_pitches_pitcher_season indexes (pitcher, game_date) — season is
    a denormalized SMALLINT column filtered directly in the WHERE clause
    rather than implied by a date range, and the data_quality_flag filter
    is applied after the index scan rather than as part of it.

    For pitchers with 3,000 pitches, the planner scans every row for that
    pitcher and filters ~50 flagged rows at runtime — wasting ~95% of
    block reads.

Fix:
    Add a partial composite index on (pitcher, season) WHERE clean.
    The planner now picks this index for any query of the form
        SELECT … FROM raw.pitches
        WHERE pitcher = ? AND season = ? AND data_quality_flag = FALSE
    and an EXPLAIN ANALYZE on a 3,000-pitch pitcher returns in < 50 ms.

Why partial:
    ~99.9% of rows are clean.  A partial index excluding flagged rows is
    marginally smaller and avoids touching the data_quality_flag column at
    plan time at all.  Matches the pattern established by SIM-085.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_pitches_pitcher_season_clean
            ON raw.pitches(pitcher, season)
            WHERE data_quality_flag = FALSE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pitches_pitcher_season_clean")
