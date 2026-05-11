"""sim086_games_venue_id_nullable

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-06

SIM-086: Allow NULL venue_id on raw.games to fix live pipeline FK violation.

Bug:
    _upsert_game_record() in live_ingestion_pipeline.py inserts venue_id=0
    when the schedule API response is missing the venue key (notably for
    international/spring-training games before the venue is assigned).
    No raw.venues row exists for venue_id=0, so the FK constraint
    raises a violation that's caught by the outer except block —
    the game row is never inserted and the error is silently logged.

Fix:
    Make raw.games.venue_id nullable so the live pipeline can insert
    a game record without a known venue.  A scheduled backfill job
    (pipeline/etl/venue_backfill_job.py) finds NULL venue_id rows and
    re-fetches from the MLB API once venue assignment is published.

    Pre-existing rows with venue_id=0 (if any) are not touched here —
    that data was already lost to the silent failure.

Note:
    This migration only relaxes the NOT NULL constraint; the FOREIGN KEY
    constraint stays as-is.  PostgreSQL allows NULL through a FK without
    requiring a matching parent row, which is exactly the behavior we want.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE raw.games ALTER COLUMN venue_id DROP NOT NULL")


def downgrade() -> None:
    # Backfill any NULL rows to 0 before re-imposing NOT NULL.
    # 0 will violate the FK on the next FK validation pass — this downgrade
    # is therefore unsafe in a populated database and exists for symmetry only.
    op.execute("UPDATE raw.games SET venue_id = 0 WHERE venue_id IS NULL")
    op.execute("ALTER TABLE raw.games ALTER COLUMN venue_id SET NOT NULL")
