"""sim534_swing_board_asof

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-10

SIM-534: the same cutoff stamp on the two swing leaderboards.

WHY THESE TWO STILL MATTER AFTER THE PER-PITCH LOADER
=====================================================
The per-pitch export carries bat speed and swing path from **2024 onward only**.
A 2023 regular-season day returns pitches with every bat-tracking column empty,
even though the leaderboard publishes 2023 season values. So for 2023 the
leaderboard is the only source there is.

That is not a leak. Look-ahead is about the simulation date, not the season: a
2023 full-season aggregate contains nothing that postdates a 2025 cutoff. Only
the season CONTAINING the cutoff has to be truncated.

Stamping every row with the date its data runs through makes that rule
mechanical rather than a matter of remembering it. The profile builder asks for
rows with ``asof_date <= cutoff`` and the arithmetic does the rest: a 2023
full-season row (stamped 31 December 2023) is admissible for a June 2025 cutoff;
a 2025 full-season row (stamped 31 December 2025) is not, and a June 2025
snapshot has to be pulled for it instead.
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_TABLES = ("savant_bat_tracking", "savant_swing_path")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE raw.{table} ADD COLUMN IF NOT EXISTS asof_date DATE;")
        op.execute(
            f"UPDATE raw.{table} SET asof_date = make_date(season, 12, 31) "
            f"WHERE asof_date IS NULL;"
        )
        op.execute(f"ALTER TABLE raw.{table} ALTER COLUMN asof_date SET NOT NULL;")
        op.execute(f"ALTER TABLE raw.{table} DROP CONSTRAINT IF EXISTS {table}_pkey;")
        op.execute(
            f"ALTER TABLE raw.{table} ADD PRIMARY KEY (player_id, season, split, asof_date);"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE raw.{table} DROP CONSTRAINT IF EXISTS {table}_pkey;")
        op.execute(f"ALTER TABLE raw.{table} DROP COLUMN IF EXISTS asof_date;")
        op.execute(f"ALTER TABLE raw.{table} ADD PRIMARY KEY (player_id, season, split);")


__all__ = ["downgrade", "revision", "upgrade"]
