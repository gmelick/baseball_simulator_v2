"""sim532_savant_oaa_and_jump

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-17

SIM-532: the raw landing tables for two Savant fielding boards — Outs Above
Average (pulled once per position) and Outfield Jump (per player). Both are
loaded by ``pipeline/etl/savant_loader.py`` from the registry in
``pipeline/etl/savant_boards.py``, at ``min=0`` (every fielder with one
chance; owner ruling 2026-09-16).

WHAT EACH TABLE FEEDS
=====================
    savant_outs_above_average  -> derived.fielder_season_metrics: Savant's outs
                                  above average AT the position, per 100 of our
                                  chances (a feature of both range groups of
                                  the fielder model)
    savant_outfield_jump       -> derived.fielder_season_metrics: the three
                                  parts of an outfielder's first three seconds
                                  after contact (reaction, burst, route) and
                                  the plays they were measured over (the
                                  features' own confidence basis)

THE COLUMNS
===========
The outs-above-average table keys on (player, season, POSITION). ``position``
is the position PULLED (``pos=3`` … ``pos=9`` on the query), because the figure
is the fielder's at that position; ``primary_position`` is the board's own
label for the player, kept for the record. Every measurement is a whole number
of outs. ``oaa_vs_rhh`` and ``oaa_vs_lhh`` are the split by the batter's hand:
stored for the record, read by nothing. Their difference repeats year to year
at 0.04 to 0.14 for outfielders and, within a position, only at shortstop
(0.36 to 0.38); a feature that does not repeat only adds noise to a draw
weight (the plan's Finding 1).

The outfield-jump table keys on (player, season). ``n_plays`` is the number of
plays Savant scored; ``outs_above_average`` is on those plays. The four ``*_ft``
columns are feet against the league average: ``reaction_ft`` the first 1.5
seconds, ``burst_ft`` the next 1.5, ``route_ft`` the direction taken and
``jump_ft`` their total. ``feet_covered`` is the unadjusted distance in the
first three seconds. Savant's ``outs_per_play`` is not stored.

FOREIGN KEY
===========
Every table references ``raw.players``. Savant publishes a player before our
pitch feed has seen him, so the loader drops and logs unknown players rather
than failing the batch (the 0019 pattern).
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


_TABLES = (
    "savant_outs_above_average",
    "savant_outfield_jump",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_outs_above_average (
            player_id                  INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                     INTEGER     NOT NULL,
            position                   VARCHAR(2)  NOT NULL,   -- the position PULLED (1B..RF), written from the query
            primary_position           VARCHAR(2),             -- the board's own label for the player
            fielding_runs_prevented    INTEGER,
            outs_above_average         INTEGER,                -- at this position
            oaa_in_front               INTEGER,
            oaa_toward_3b_line         INTEGER,
            oaa_toward_1b_line         INTEGER,
            oaa_behind                 INTEGER,
            oaa_vs_rhh                 INTEGER,                -- the hand split: kept for the record, read by nothing
            oaa_vs_lhh                 INTEGER,
            scraped_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season, position)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_outfield_jump (
            player_id                  INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                     INTEGER     NOT NULL,
            n_plays                    INTEGER,                -- the plays Savant scored
            n_outs                     INTEGER,
            outs_above_average         INTEGER,                -- on those plays
            reaction_ft                FLOAT,                  -- feet against the league, the first 1.5 s
            burst_ft                   FLOAT,                  -- the next 1.5 s
            route_ft                   FLOAT,                  -- the direction taken
            jump_ft                    FLOAT,                  -- the total (Savant's "jump")
            feet_covered               FLOAT,                  -- feet in the first 3 s, unadjusted
            scraped_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
