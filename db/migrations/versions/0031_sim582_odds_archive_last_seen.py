"""sim582_odds_archive_last_seen

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-13

SIM-582: the two odds archives hold every live odds column again. Migration 0028
created the archives as copies of the live tables' columns. Migration 0029 then
added ``last_seen_at`` to the live tables only. The retirement script
(``scripts/probes/retire_consensus_odds.py``) checks that an archive holds every
live column before it copies a season's rows. Since 0029 that check fails, so the
script stops with an error (exit 3) and copies nothing.

WHAT IT ADDS
============
    raw.game_odds_archive.last_seen_at   TIMESTAMPTZ, nullable, no default. It
    raw.prop_odds_archive.last_seen_at   lands last, where 0029 put it in the
                                         live tables, so each archive keeps the
                                         live column order. A row archived
                                         before 0031 holds NULL: its last-seen
                                         time was never kept. The script copies
                                         every live column by name, so a row it
                                         archives keeps its own value and the
                                         archive needs no default.

The column changes only the catalog: a nullable column with no default rewrites
no row.

DOWNGRADE
=========
Drops the column from both archives. The rows archived after 0031 lose their
last-seen time.
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("game_odds", "prop_odds"):
        op.execute(
            f"ALTER TABLE raw.{table}_archive ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NULL;"
        )


def downgrade() -> None:
    for table in ("game_odds", "prop_odds"):
        op.execute(f"ALTER TABLE raw.{table}_archive DROP COLUMN IF EXISTS last_seen_at;")


__all__ = ["downgrade", "revision", "upgrade"]
