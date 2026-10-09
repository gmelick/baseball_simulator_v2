"""whatif_runs

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-09

The game page's "what if": a user picks a plate appearance of the real game,
makes managerial changes, and the page simulates the rest of the game twice
from that point (as it stood, and with the changes). Both are ordinary run
jobs on ``sim.sim_runs``; this migration tells them apart from the game's own
simulation.

WHAT IT ADDS
============
sim.sim_runs.kind          VARCHAR(16) NOT NULL DEFAULT 'pregame':
                           pregame | whatif_base | whatif_change. Every stored
                           run is 'pregame'. The "latest run" reads (the
                           Simulation card, the slate's sim line, the
                           projections) see only 'pregame'.
sim.sim_runs.start_at_bat  INTEGER: the plate appearance (the feed's
                           atBatIndex) a what-if starts at; NULL = first pitch.
sim.sim_runs.changes       JSONB: the managerial changes of a 'whatif_change' run.
idx_sim_runs_game_kind     (game_pk, kind, created_at DESC).

DOWNGRADE
=========
Deletes the what-if runs, then drops the index and the three columns.
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sim.sim_runs ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'pregame';"
    )
    op.execute("ALTER TABLE sim.sim_runs ADD COLUMN IF NOT EXISTS start_at_bat INTEGER;")
    op.execute("ALTER TABLE sim.sim_runs ADD COLUMN IF NOT EXISTS changes JSONB;")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE sim.sim_runs ADD CONSTRAINT ck_sim_runs_kind
                CHECK (kind IN ('pregame', 'whatif_base', 'whatif_change'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sim_runs_game_kind ON sim.sim_runs (game_pk, kind, created_at DESC);"
    )


def downgrade() -> None:
    op.execute("DELETE FROM sim.sim_runs WHERE kind <> 'pregame';")
    op.execute("DROP INDEX IF EXISTS sim.idx_sim_runs_game_kind;")
    op.execute("ALTER TABLE sim.sim_runs DROP CONSTRAINT IF EXISTS ck_sim_runs_kind;")
    op.execute("ALTER TABLE sim.sim_runs DROP COLUMN IF EXISTS changes;")
    op.execute("ALTER TABLE sim.sim_runs DROP COLUMN IF EXISTS start_at_bat;")
    op.execute("ALTER TABLE sim.sim_runs DROP COLUMN IF EXISTS kind;")
