"""sim528_savant_raw_tables

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-10

SIM-528: the raw landing tables for the Baseball Savant leaderboards.

WHY EIGHT TYPED TABLES AND NOT ONE GENERIC ONE
==============================================
A single ``raw.savant_leaderboard(board, season, player_id, payload JSONB)``
would need one migration instead of eight. It was rejected for two reasons.
Type coercion would move into the nightly profile builder, where a bad cast is
far harder to see than at the ingest boundary — the builder's SQL is thousands
of lines and a silently-wrong number there surfaces as a model drift, not an
error. And every downstream join would carry JSON extraction, which DuckDB
handles but which makes the profile queries much harder to read.

Typed tables cost more lines here and are cheaper everywhere else.

WHAT EACH TABLE FEEDS
=====================
    savant_bat_tracking          -> batter profile: bat speed, swing length
    savant_swing_path            -> batter profile: attack angle, tilt, contact depth
    savant_batting_stance        -> batter profile: foot separation, stance angle, box position
    savant_arm_strength          -> fielder + catcher profile: throw velocity
    savant_baserunning           -> fielder profile: the whole outfield arm block
    savant_poptime               -> catcher profile: arm strength, exchange time
    savant_catcher_throwing      -> catcher profile: arm strength fallback
    savant_first_base_receiving  -> fielder profile: scoop success

THE SPLIT COLUMN
================
Two tables carry a third key column, because one player-season has more than one
row.

``savant_batting_stance.bat_side`` is Savant's own: it publishes a row per
batting side, so a switch hitter already has two.

``savant_bat_tracking.split`` and ``savant_swing_path.split`` hold ``all``,
``vs_l`` or ``vs_r`` — the pitcher's hand the swings were taken against. Those
boards report one row per batter labelled with his majority side, so the split
has to come from the query. Owner ruling 2026-09-10: a switch hitter's two sides
are separate rows, not one collapsed row, and a batter's side is decided by the
pitcher's hand — so filtering on the pitcher's hand IS the batting-side split.

FOREIGN KEY
===========
Every table references ``raw.players``. Savant publishes a player before our
pitch feed has seen him, so the loader drops and logs unknown players rather
than failing the batch.
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


_TABLES = (
    "savant_bat_tracking",
    "savant_swing_path",
    "savant_batting_stance",
    "savant_arm_strength",
    "savant_baserunning",
    "savant_poptime",
    "savant_catcher_throwing",
    "savant_first_base_receiving",
)


def upgrade() -> None:
    # -- SIM-529: the physical swing and stance boards ----------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_bat_tracking (
            player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season              INTEGER     NOT NULL,
            split               VARCHAR(8)  NOT NULL,   -- all | vs_l | vs_r (pitcher hand)
            avg_bat_speed       FLOAT,                  -- mph
            swing_length        FLOAT,                  -- feet of bat head travel
            competitive_swings  INTEGER,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season, split)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_swing_path (
            player_id               INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                  INTEGER     NOT NULL,
            split                   VARCHAR(8)  NOT NULL,
            bat_side                VARCHAR(1),          -- Savant's label for the row
            swing_tilt              FLOAT,               -- degrees; swing-plane incline
            attack_angle            FLOAT,               -- degrees; up/down at contact
            attack_direction        FLOAT,               -- degrees; pull/oppo at contact
            ideal_attack_angle_rate FLOAT,
            intercept_y_vs_plate    FLOAT,               -- inches in front of the plate
            intercept_y_vs_batter   FLOAT,
            competitive_swings      INTEGER,
            scraped_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season, split)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_batting_stance (
            player_id               INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                  INTEGER     NOT NULL,
            bat_side                VARCHAR(1)  NOT NULL,  -- L or R; a switch hitter has both
            foot_sep                FLOAT,                 -- inches between the feet
            stance_angle            FLOAT,                 -- degrees; open (+) or closed (-)
            batter_y_position       FLOAT,                 -- depth in the box
            batter_x_position       FLOAT,                 -- distance off the plate
            intercept_y_vs_plate    FLOAT,
            intercept_y_vs_batter   FLOAT,
            scraped_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season, bat_side)
        );
    """)

    # -- SIM-530: the boards that fill the empty measurement blocks ----------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_arm_strength (
            player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season              INTEGER     NOT NULL,
            total_throws        INTEGER,
            max_arm_strength    FLOAT,      -- mph, best throw
            arm_overall         FLOAT,      -- mph, all positions
            arm_inf             FLOAT,
            arm_of              FLOAT,
            arm_1b              FLOAT,
            arm_2b              FLOAT,
            arm_3b              FLOAT,
            arm_ss              FLOAT,
            arm_lf              FLOAT,
            arm_cf              FLOAT,
            arm_rf              FLOAT,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_baserunning (
            player_id                       INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                          INTEGER     NOT NULL,
            fielder_runs                    FLOAT,      -- total arm run value
            fielder_runs_advances           FLOAT,
            fielder_runs_thrown_out         FLOAT,
            fielder_runs_hold               FLOAT,
            runner_runs                     FLOAT,
            n_opp_xb                        INTEGER,    -- chances to take an extra base
            n_att_xb                        INTEGER,    -- attempts
            rate_att_xb                     FLOAT,      -- attempts / chances
            est_rate_att_generic_fielder    FLOAT,      -- the baseline our data lacks
            est_rate_att_generic_runner     FLOAT,
            n_out                           INTEGER,
            n_safe                          INTEGER,
            scraped_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_poptime (
            player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season              INTEGER     NOT NULL,
            arm_strength        FLOAT,      -- mph on steal attempts
            exchange_time       FLOAT,      -- seconds, glove to release
            pop_time_2b         FLOAT,      -- seconds, release to the bag
            pop_time_2b_count   INTEGER,
            pop_time_3b         FLOAT,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_catcher_throwing (
            player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season              INTEGER     NOT NULL,
            arm_strength        FLOAT,
            pop_time            FLOAT,
            exchange_time       FLOAT,
            est_cs_pct          FLOAT,
            cs_aa_per_throw     FLOAT,
            sb_attempts         INTEGER,
            n_cs                INTEGER,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_first_base_receiving (
            player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season              INTEGER     NOT NULL,
            height_in_inches    INTEGER,
            n_plays             INTEGER,
            n_outs              INTEGER,
            total_oaa           FLOAT,
            n_scoop             INTEGER,
            outs_scoop          INTEGER,
            oaa_scoop           FLOAT,
            scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)

    for table in _TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_season ON raw.{table}(season);")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS raw.idx_{table}_season;")
        op.execute(f"DROP TABLE IF EXISTS raw.{table};")


__all__ = ["downgrade", "revision", "upgrade"]
