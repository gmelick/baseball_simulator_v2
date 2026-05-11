"""sim157_game_odds_full_unique

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-13

SIM-157: Promote the SIM-092 partial unique index on raw.game_odds to a full
unique index, now that scripts/backfill_odds_hash.py has populated odds_hash
on every legacy row and collapsed pre-existing duplicates.

Sequence:
    1. SIM-092 (migration 0010) added the column + a *partial* unique index
       (`WHERE odds_hash IS NOT NULL`) so legacy NULL-hash rows did not trip
       the constraint while we deferred the backfill.
    2. SIM-157 ships the backfill script that populates every NULL row and
       de-duplicates `(game_pk, source, odds_hash)` groups down to the
       earliest survivor.
    3. THIS migration drops the partial index and replaces it with a full
       unique index plus a NOT NULL constraint, so future writes can no
       longer regress to NULL-hash and the column is enforced as a true key.

Operator workflow:
    # Apply migrations 0001..0011 first.
    BASEBALL_DB_DSN=... alembic upgrade head
    # Run the backfill (idempotent — safe to re-run).
    BASEBALL_DB_DSN=... python scripts/backfill_odds_hash.py
    # Apply 0012 — fails fast if backfill / dedup is incomplete.
    BASEBALL_DB_DSN=... alembic upgrade head

If 0012 trips the NOT NULL or unique constraint, the backfill did not
complete cleanly — re-run the script and try again.

Downgrade restores the SIM-092 partial-index state so 0010 can still be the
working baseline if 0012 needs to be rolled back without rolling back the
backfill itself.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the SIM-092 partial unique index — about to be replaced.
    op.execute("DROP INDEX IF EXISTS idx_game_odds_dedup")

    # Enforce NOT NULL.  If this fails, the SIM-157 backfill is incomplete;
    # re-run scripts/backfill_odds_hash.py and re-apply this migration.
    op.execute(
        """
        ALTER TABLE raw.game_odds
            ALTER COLUMN odds_hash SET NOT NULL
        """
    )

    # Full unique index on the cleaned key.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_game_odds_dedup_full
            ON raw.game_odds(game_pk, source, odds_hash)
        """
    )


def downgrade() -> None:
    # Restore the SIM-092 partial-index baseline.
    op.execute("DROP INDEX IF EXISTS idx_game_odds_dedup_full")
    op.execute(
        """
        ALTER TABLE raw.game_odds
            ALTER COLUMN odds_hash DROP NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_game_odds_dedup
            ON raw.game_odds(game_pk, source, odds_hash)
            WHERE odds_hash IS NOT NULL
        """
    )
