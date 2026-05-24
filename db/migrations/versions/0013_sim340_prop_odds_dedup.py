"""sim340_prop_odds_dedup

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-23

SIM-340: Deduplicate raw.prop_odds inserts (prop analogue of SIM-092).

Problem
-------
SIM-340 wires the previously-unused ``_persist_prop_odds()`` into the live
ingestion cadence (``_persist_prop_odds_cycle()`` fires on the normal refresh
loop) and fans every prop out across multiple books × the 7 prop markets ×
every active player.  Without deduplication that path would write a fresh
``raw.prop_odds`` row on every fetch cycle even when the line has not moved —
exactly the duplicate-row explosion SIM-092 fixed for ``raw.game_odds``.

Fix
---
Mirror the SIM-092 strategy on ``raw.prop_odds``:

  1. Add ``odds_hash VARCHAR(64)``: SHA-256 of the prop payload
     (player_id, prop_stat, book, line_type, is_sharp_book, line, over/under).
     Same payload -> same hash -> INSERT ... ON CONFLICT DO NOTHING is a no-op.
  2. Partial UNIQUE INDEX on (game_pk, player_id, source, odds_hash) WHERE
     odds_hash IS NOT NULL — legacy NULL-hash rows (written before SIM-340) do
     not trip the constraint, matching the SIM-092 partial-index pattern.
  3. Application code (LiveIngestionPipeline._persist_prop_odds) computes the
     hash and uses ON CONFLICT (game_pk, player_id, source, odds_hash)
     DO NOTHING.

player_id is part of the unique key (props fan out per player) whereas the
game-odds dedup key is just (game_pk, source, odds_hash).

Backfill
--------
Existing rows keep NULL odds_hash; new rows always populate it.  A follow-up
backfill (out of scope) can fill legacy rows and promote this to a full unique
index, mirroring the SIM-157 / migration 0012 promotion for game_odds.

Credentials: none.  The DB URL is read from BASEBALL_DB_DSN at runtime by
db/migrations/env.py; nothing is hard-coded here.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE raw.prop_odds
            ADD COLUMN IF NOT EXISTS odds_hash VARCHAR(64)
        """
    )
    # Partial unique index: only enforced on rows that have a hash, so legacy
    # NULL-hash rows do not trip the constraint.  Promote to a full unique
    # index in a follow-up once a backfill has populated every legacy row.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_prop_odds_dedup
            ON raw.prop_odds(game_pk, player_id, source, odds_hash)
            WHERE odds_hash IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.idx_prop_odds_dedup")
    op.execute("ALTER TABLE raw.prop_odds DROP COLUMN IF EXISTS odds_hash")
