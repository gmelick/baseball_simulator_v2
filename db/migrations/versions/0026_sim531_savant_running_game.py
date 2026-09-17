"""sim531_savant_running_game

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-16

SIM-531: the raw landing tables for the two Savant running-game boards —
Basestealing Run Value (the runner's side) and Pitcher Running Game (the
pitcher's side). Both are loaded by ``pipeline/etl/savant_loader.py`` from the
registry in ``pipeline/etl/savant_boards.py``, at ``n=1`` (every player with
one chance; owner ruling 2026-09-16).

WHAT EACH TABLE FEEDS
=====================
    savant_basestealing          -> derived.baserunner_steal_metrics: the runner's
                                    lead off the bag and his jump on the delivery
                                    (the steal-runner model's Lead sub-score)
    savant_pitcher_running_game  -> derived.pitcher_steal_metrics: the lead and
                                    the jump the pitcher allows (the pitcher-hold
                                    model's Hold sub-score)

THE COLUMNS
===========
Both boards share one shape. ``n_init`` is the number of pitches on which the
runner could have gone (the "initiations" — the lead is measured over these);
``rate_sbx`` is ``(n_sb + n_cs) / n_init``. The three ``r_*_lead`` columns are
feet: the primary lead before the pitch, the secondary lead after the pitcher's
first movement, and their difference (the jump). The ``*_sbx`` versions are the
same leads on the attempted pitches only (noise: they repeat year to year at
0.29–0.32, and are stored, not read). ``n_fb``, ``n_plus``, ``n_minus`` and the
``net_*`` columns are not stored (an unglossed outcome decomposition).

The pitcher table carries two extra columns: ``runs_prevented_on_running_attr``
(the run value attributed to the pitcher) and ``n_pitcher_cs_aa`` (caught
stealings above average).

FOREIGN KEY
===========
Every table references ``raw.players``. Savant publishes a player before our
pitch feed has seen him, so the loader drops and logs unknown players rather
than failing the batch (the 0019 pattern).
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


_TABLES = (
    "savant_basestealing",
    "savant_pitcher_running_game",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_basestealing (
            player_id                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                      INTEGER     NOT NULL,
            n_init                      INTEGER,    -- pitches on which the runner could have gone
            rate_sbx                    FLOAT,      -- (n_sb + n_cs) / n_init
            n_sb                        INTEGER,
            n_cs                        INTEGER,
            n_pk                        INTEGER,    -- picked off
            n_bk                        INTEGER,    -- balks drawn
            runs_stolen_on_running_act  FLOAT,      -- the runner's run value
            r_primary_lead              FLOAT,      -- feet, before the pitch
            r_secondary_lead            FLOAT,      -- feet, after the pitcher's first move
            r_sec_minus_prim_lead       FLOAT,      -- feet, the jump
            r_primary_lead_sbx          FLOAT,      -- the same three on attempted pitches only
            r_secondary_lead_sbx        FLOAT,
            r_sec_minus_prim_lead_sbx   FLOAT,
            scraped_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (player_id, season)
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.savant_pitcher_running_game (
            player_id                       INTEGER     NOT NULL REFERENCES raw.players(player_id),
            season                          INTEGER     NOT NULL,
            n_init                          INTEGER,    -- pitches with a runner who could go
            rate_sbx                        FLOAT,      -- (n_sb + n_cs) / n_init
            n_sb                            INTEGER,
            n_cs                            INTEGER,
            n_pk                            INTEGER,
            n_bk                            INTEGER,
            runs_prevented_on_running_attr  FLOAT,      -- the pitcher's run value
            n_pitcher_cs_aa                 FLOAT,      -- caught stealings above average
            r_primary_lead                  FLOAT,      -- feet, the lead he allows
            r_secondary_lead                FLOAT,
            r_sec_minus_prim_lead           FLOAT,      -- feet, the jump he gives up
            r_primary_lead_sbx              FLOAT,
            r_secondary_lead_sbx            FLOAT,
            r_sec_minus_prim_lead_sbx       FLOAT,
            scraped_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
