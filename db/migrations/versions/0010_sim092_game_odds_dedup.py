"""sim092_game_odds_dedup

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-07

SIM-092: Deduplicate raw.game_odds inserts.

Problem:
    _persist_odds() always INSERTs a new row with no ON CONFLICT clause.  The
    live pipeline refreshes a game every 30 seconds, so a 3-hour game produces
    ~360 odds rows per game.  Almost every snapshot is identical to the
    previous one (book lines move infrequently relative to refresh cadence).
    Over a full 162-game season × 30 games/day × 3 hours = millions of
    duplicate rows accumulate, blowing up storage and slowing every CLV query.

Fix:
    1. Add ``odds_hash VARCHAR(64)``: SHA-256 of the concatenated odds payload
       (home_ml, away_ml, spreads, total, line_type, market_type, ...).
       Same payload → same hash → INSERT … ON CONFLICT DO NOTHING is a no-op.
    2. UNIQUE INDEX on (game_pk, source, odds_hash).
    3. Application code computes odds_hash before insert and uses
       ON CONFLICT (game_pk, source, odds_hash) DO NOTHING.

Backfill:
    Existing rows have NULL odds_hash.  Filling them retroactively is a
    non-trivial pass over potentially millions of rows; we punt that to a
    follow-up backfill job if anyone needs CLV continuity across pre-/post-
    SIM-092 odds rows.  New rows always populate the hash.

    Pre-existing duplicates remain in the table — this migration prevents
    further accumulation but does not deduplicate history.  A separate
    cleanup script (out of scope here) can DELETE oldest-of-duplicates after
    backfilling odds_hash.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE raw.game_odds
            ADD COLUMN IF NOT EXISTS odds_hash VARCHAR(64)
        """
    )
    # Partial unique index: only enforce on rows that have a hash, so legacy
    # NULL-hash rows don't trip the constraint.  After backfill, this can be
    # promoted to a full unique index in a follow-up migration.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_game_odds_dedup
            ON raw.game_odds(game_pk, source, odds_hash)
            WHERE odds_hash IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_game_odds_dedup")
    op.execute("ALTER TABLE raw.game_odds DROP COLUMN IF EXISTS odds_hash")
