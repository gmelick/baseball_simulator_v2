"""sim441_etl_hardening

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-27

SIM-441: schema support for the ETL hardening batch.

Four independent changes, all cheap and all non-destructive except the
deliberate `raw.players.active` drop.

1. raw.pitches.field_assist_6_plus (new BOOLEAN)
   ------------------------------------------------
   The parser records fielding credits into `field_assist_1..5`,
   `field_putout_1..3` and `throwing_error_1..2`. Credits past those slots were
   silently discarded — no warning, no ledger row. Multi-runner rundowns are
   rare enough that modelling the 6th+ assist is not worth a column each, but
   the *fact* of an overflow should be visible rather than invisible, so a
   consumer can exclude those plays or measure how often it happens.

2. raw.players.active (DROPPED)
   ------------------------------------------------
   Declared NOT NULL DEFAULT TRUE with a partial index
   (`idx_players_active ... WHERE active = TRUE`), never written by any code
   path, and never updated — so it read TRUE for every player ever ingested and
   the "partial" index covered 100% of rows. It is served to the SIM-439 SQL
   console, where a user filtering `WHERE active = TRUE` silently gets retired
   players. Nothing reads it. Dropped rather than fixed.

3. raw.etl_errors — unique key + ON CONFLICT support
   ------------------------------------------------
   The ledger insert had no conflict clause and the table had only a surrogate
   key. When EVERY row of a game hard-errors, no pitch rows are written, so
   `_game_already_loaded` stays False and the nightly job re-fetches and
   re-processes that game every night, appending a full duplicate error set each
   time (~54,000 identical rows per season for one game). A unique key makes the
   insert idempotent.

4. raw.etl_game_ingest (new table)
   ------------------------------------------------
   The game-level outcome record. `_game_already_loaded` keys on pitch rows
   existing, which cannot distinguish "not yet loaded" from "loaded and
   legitimately produced zero pitch rows". This table records the terminal
   outcome so a zero-pitch game is not re-attempted forever. `reload=True`
   ignores it by design — that is the escape hatch after a parser fix.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 0. raw.pitches manager ids become nullable ------------------------
    # SIM-441 removed the "no job-code match -> install coaches[0]" fallback,
    # because that fabricated a manager identity (typically a pitching coach),
    # propagated it to ~300 pitch rows and raw.games, and made _ensure_managers
    # close out the REAL manager's stint. The loader now leaves the id empty.
    #
    # But raw.pitches.home/away_manager_id were INTEGER NOT NULL, while the SAME
    # columns on raw.games are nullable — so an empty id turned a survivable
    # "we could not identify the manager" into a NotNullViolation that rolled
    # back the entire game's pitch insert. Losing a full game of pitch-by-pitch
    # data because a coaches roster was unusual is a far worse outcome than a
    # NULL in two denormalised convenience columns. Aligning with raw.games.
    #
    # The composite FKs to raw.managers are unaffected: Postgres does not
    # enforce a foreign key when any column of the key is NULL.
    op.execute("ALTER TABLE raw.pitches ALTER COLUMN home_manager_id DROP NOT NULL")
    op.execute("ALTER TABLE raw.pitches ALTER COLUMN away_manager_id DROP NOT NULL")

    # --- 1. fielding-credit overflow marker --------------------------------
    op.execute(
        """
        ALTER TABLE raw.pitches
            ADD COLUMN IF NOT EXISTS field_assist_6_plus BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN raw.pitches.field_assist_6_plus IS
            'SIM-441: TRUE when this pitch recorded more fielding credits than the '
            'field_assist_1..5 / field_putout_1..3 / throwing_error_1..2 slots can '
            'hold, so at least one credit was dropped. Practically only reachable on '
            'a multi-runner rundown. Lets a consumer exclude or measure those plays '
            'instead of silently under-counting assists.'
        """
    )

    # --- 2. drop the dead `active` flag ------------------------------------
    op.execute("DROP INDEX IF EXISTS raw.idx_players_active")
    op.execute("ALTER TABLE raw.players DROP COLUMN IF EXISTS active")

    # --- 3. make the ETL error ledger idempotent ---------------------------
    # Collapse any duplicates the old unguarded INSERT already produced, keeping
    # the earliest row per natural key, so the unique index can be created.
    op.execute(
        """
        DELETE FROM raw.etl_errors a
              USING raw.etl_errors b
              WHERE a.id > b.id
                AND a.game_pk       IS NOT DISTINCT FROM b.game_pk
                AND a.at_bat_number IS NOT DISTINCT FROM b.at_bat_number
                AND a.pitch_number  IS NOT DISTINCT FROM b.pitch_number
                AND a.error_type    =  b.error_type
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_etl_errors_natural_key
            ON raw.etl_errors (game_pk, at_bat_number, pitch_number, error_type)
        """
    )

    # --- 4. game-level ingest outcome --------------------------------------
    op.create_table(
        "etl_game_ingest",
        sa.Column("game_pk", sa.Integer(), primary_key=True),
        sa.Column("season", sa.SmallInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("pitch_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('loaded','empty','failed')", name="ck_etl_game_ingest_outcome"
        ),
        schema="raw",
    )
    op.execute(
        """
        COMMENT ON TABLE raw.etl_game_ingest IS
            'SIM-441: terminal per-game ETL outcome. `_game_already_loaded` keys on '
            'pitch rows existing, which cannot tell "never loaded" from "loaded and '
            'legitimately produced zero pitches" — so a totally-failing or pitch-less '
            'game was re-fetched and re-processed every night forever. outcome=''empty'' '
            'stops that. reload_game()/reload=True ignore this table by design: that is '
            'the escape hatch after a parser fix.'
        """
    )


def downgrade() -> None:
    op.drop_table("etl_game_ingest", schema="raw")
    op.execute("DROP INDEX IF EXISTS raw.uq_etl_errors_natural_key")
    op.execute(
        """
        ALTER TABLE raw.players
            ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_players_active ON raw.players(active) WHERE active = TRUE"
    )
    op.execute("ALTER TABLE raw.pitches DROP COLUMN IF EXISTS field_assist_6_plus")
    # NOTE: the manager-id NOT NULL constraints are deliberately NOT restored.
    # Re-adding them would fail on any row this migration allowed through with a
    # NULL manager, and the original NOT NULL was itself the defect.
