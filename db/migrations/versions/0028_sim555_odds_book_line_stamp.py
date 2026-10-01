"""sim555_odds_book_line_stamp

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-28

SIM-555: every odds row holds ONE book's prices and names the book. The
provider now returns one row per book (``book = 'bp:<id>'``); this migration
adds what the new rows and their readers need. It changes no existing row.

WHAT IT ADDS
============
    raw.game_odds.book_line_at   the vendor's stamp on the row's line: the
    raw.prop_odds.book_line_at   opener's ``created`` on an opening row, the
                                 newest ``updated`` of the row's sides
                                 otherwise. TIMESTAMPTZ, nullable (every old
                                 row stays NULL). The dedup hash does not take
                                 it, so an unchanged line re-loaded later still
                                 deduplicates.
    idx_game_odds_market_book    (game_pk, market_type, line_type, book): the
    idx_prop_odds_market_book    (game_pk, player_id, prop_stat, line_type, book):
                                 the graded-row and best-price reads, one row
                                 per book.
    raw.game_odds_archive        empty copies of the two odds tables' columns
    raw.prop_odds_archive        and defaults. The retirement script
                                 (``scripts/sim555_retire_consensus_rows.py``)
                                 moves the old ``consensus`` rows here, season by
                                 season, once the census on the new rows passes.

The archives copy the column defaults (``LIKE ... INCLUDING DEFAULTS``), which
would include the live table's ``id`` sequence; each archive then drops that
default, so an archive never draws ids from the live table's sequence. An
archived row keeps its original ``id``.

No constraint on ``book``: the label ``bp:<id>`` fits ``VARCHAR(50)``. No kind
column: a book's kind (sportsbook, blend, daily fantasy, exchange, prediction
market) is a lookup on its id in ``pipeline/odds_provider.py``.

DOWNGRADE
=========
Drops everything this migration created: the two archive tables (copies of
retired rows — a downgrade after the retirement loses those copies), the two
indexes and the two stamp columns.
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE raw.game_odds ADD COLUMN IF NOT EXISTS book_line_at TIMESTAMPTZ NULL;")
    op.execute("ALTER TABLE raw.prop_odds ADD COLUMN IF NOT EXISTS book_line_at TIMESTAMPTZ NULL;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_odds_market_book "
        "ON raw.game_odds (game_pk, market_type, line_type, book);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_odds_market_book "
        "ON raw.prop_odds (game_pk, player_id, prop_stat, line_type, book);"
    )
    for table in ("game_odds", "prop_odds"):
        op.execute(
            f"CREATE TABLE IF NOT EXISTS raw.{table}_archive (LIKE raw.{table} INCLUDING DEFAULTS);"
        )
        # An archive never draws from the live table's id sequence.
        op.execute(f"ALTER TABLE raw.{table}_archive ALTER COLUMN id DROP DEFAULT;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS raw.prop_odds_archive;")
    op.execute("DROP TABLE IF EXISTS raw.game_odds_archive;")
    op.execute("DROP INDEX IF EXISTS raw.idx_prop_odds_market_book;")
    op.execute("DROP INDEX IF EXISTS raw.idx_game_odds_market_book;")
    op.execute("ALTER TABLE raw.prop_odds DROP COLUMN IF EXISTS book_line_at;")
    op.execute("ALTER TABLE raw.game_odds DROP COLUMN IF EXISTS book_line_at;")


__all__ = ["downgrade", "revision", "upgrade"]
