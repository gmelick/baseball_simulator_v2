"""sim534_pitch_tracking_and_asof

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-10

SIM-534: make the batter's physical measurements POINT-IN-TIME.

THE PROBLEM
===========
SIM-529 loaded the physical swing and stance features as SEASON aggregates. For
simulating today's games that is right. For backtesting it is look-ahead: a bat
speed averaged over all of 2025 encodes how the batter swung in September, and
using it to simulate an April 2025 game uses information that did not exist.

WHAT THIS MIGRATION ADDS
========================
1. ``raw.savant_pitch_tracking`` — the per-PITCH tracking measurements. Savant's
   pitch-level search export carries nine columns our own feed does not, and
   they are the same measurements the bat-tracking and swing-path leaderboards
   aggregate. Holding them per pitch means the profile builder can aggregate
   them up to any cutoff date, exactly as it already does for every metric
   computed from ``raw.pitches``. The leaderboard stops being the source and
   becomes a cross-check.

   A SEPARATE TABLE, not columns on ``raw.pitches``: that table is documented as
   "Direct 1:1 ingestion target for Statcast data. Never modified after write",
   and this data arrives from a different feed on a different schedule. The two
   join on (game_pk, at_bat_number, pitch_number).

   No foreign key to ``raw.games``. Savant occasionally carries a game our own
   ingest has not reached; the profile join is an inner join to ``raw.pitches``,
   so an orphan row is simply never read.

2. ``asof_date`` on ``raw.savant_batting_stance``. Stance is the one batter
   measurement that is NOT in the pitch-level export — it comes from pose
   tracking and only exists as a leaderboard aggregate. That board does accept a
   date range, so the fix is to store one row per cutoff instead of one per
   season. The primary key gains the cutoff.

   Existing rows are stamped 31 December of their season, which is what a
   full-season pull means: everything that will ever be in that season.
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- 1. the per-pitch tracking measurements ------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_pitch_tracking (
            game_pk             INTEGER     NOT NULL,
            at_bat_number       SMALLINT    NOT NULL,
            pitch_number        SMALLINT    NOT NULL,
            game_date           DATE        NOT NULL,
            season              SMALLINT    NOT NULL,
            batter              INTEGER,
            pitcher             INTEGER,
            -- The bat-tracking family, per pitch.
            bat_speed           FLOAT,      -- mph at contact point
            swing_length        FLOAT,      -- feet of bat-head travel
            attack_angle        FLOAT,      -- degrees, up (+) or down (-)
            attack_direction    FLOAT,      -- degrees, pull (+) or oppo (-)
            swing_path_tilt     FLOAT,      -- degrees, swing-plane incline
            intercept_x         FLOAT,      -- inches, ball minus batter position
            intercept_y         FLOAT,      -- inches, contact depth
            -- Carried because the same row has them and a later ticket wants them.
            arm_angle           FLOAT,      -- the PITCHER's arm angle, degrees
            hyper_speed         FLOAT,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (game_pk, at_bat_number, pitch_number)
        );
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_savant_pitch_tracking_date "
        "ON raw.savant_pitch_tracking(game_date);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_savant_pitch_tracking_batter "
        "ON raw.savant_pitch_tracking(batter, season);"
    )

    # -- 2. the stance cutoff -----------------------------------------------
    # A full-season pull is data "as of" the last day of that season.
    op.execute(
        "ALTER TABLE raw.savant_batting_stance "
        "ADD COLUMN IF NOT EXISTS asof_date DATE;"
    )
    op.execute(
        "UPDATE raw.savant_batting_stance "
        "SET asof_date = make_date(season, 12, 31) WHERE asof_date IS NULL;"
    )
    op.execute(
        "ALTER TABLE raw.savant_batting_stance ALTER COLUMN asof_date SET NOT NULL;"
    )
    op.execute(
        "ALTER TABLE raw.savant_batting_stance DROP CONSTRAINT IF EXISTS savant_batting_stance_pkey;"
    )
    op.execute(
        "ALTER TABLE raw.savant_batting_stance "
        "ADD PRIMARY KEY (player_id, season, bat_side, asof_date);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.idx_savant_pitch_tracking_batter;")
    op.execute("DROP INDEX IF EXISTS raw.idx_savant_pitch_tracking_date;")
    op.execute("DROP TABLE IF EXISTS raw.savant_pitch_tracking;")
    op.execute(
        "ALTER TABLE raw.savant_batting_stance DROP CONSTRAINT IF EXISTS savant_batting_stance_pkey;"
    )
    op.execute("ALTER TABLE raw.savant_batting_stance DROP COLUMN IF EXISTS asof_date;")
    op.execute(
        "ALTER TABLE raw.savant_batting_stance ADD PRIMARY KEY (player_id, season, bat_side);"
    )


__all__ = ["downgrade", "revision", "upgrade"]
