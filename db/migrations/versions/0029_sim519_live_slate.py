"""sim519_live_slate

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-09

SIM-519: the live, schedule-driven game day view. One additive migration for
every column the design (``docs/audit/2026-10-08-sim519-live-slate-tech-design.md``
§12) adds. It changes no existing value: every new column is nullable or takes
a default that describes the rows already stored.

WHAT IT ADDS
============
raw.games (the live service writes them from the schedule; Part B / C)
    start_utc, start_time_tbd, double_header, game_number, detailed_state,
    home_probable_pitcher_id, away_probable_pitcher_id,
    first_pitch_at (the Preview-to-Live instant), schedule_seen_at.

raw.game_lineups (Part B)
    source VARCHAR(16) NOT NULL DEFAULT 'box'   box | published | projected.
                                                 Every stored row is 'box'.
    published_at TIMESTAMPTZ                     when the live service wrote it.

sim.sim_runs (Part E: one durable run per game)
    status VARCHAR(16) NOT NULL DEFAULT 'done'   queued | running | done |
                                                 failed | cancelled. Every
                                                 stored run is 'done'.
    progress_done, requested_at, started_at, finished_at, error, spec_key,
    requested_by, lineup_source, bullpen_source, and the run's panel data:
    prop_set, linescore, decisions, inning_grids (JSONB), and replay_run_id —
    the run's representative game in the replay file (SIM-561 keeps a
    simulated game's play-by-play there, so the row links to it rather than
    copying it; the design's ``play_by_play`` column became this link).
    ``summary`` drops NOT NULL: a queued run has none yet.
    idx_sim_runs_game_status   (game_pk, status, created_at DESC)
    uq_sim_runs_active_spec    UNIQUE (spec_key) WHERE status IN
                               ('queued', 'running'): two app processes cannot
                               start the same run.

raw.game_odds, raw.prop_odds (Part G)
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()   the live writers re-stamp
                                                      a price the book still posts.

DOWNGRADE
=========
Drops every column and index this migration added. ``sim.sim_runs.summary``
gets NOT NULL back only after its NULL rows (runs that never finished) are
deleted. A downgrade loses each run's panel data and every published or
projected lineup row (the downgrade deletes them first, so a later load of
the final box does not collide with them).
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


_GAMES_COLUMNS = (
    ("start_utc", "TIMESTAMPTZ"),
    ("start_time_tbd", "BOOLEAN"),
    ("double_header", "CHAR(1)"),
    ("game_number", "SMALLINT"),
    ("detailed_state", "VARCHAR(40)"),
    ("home_probable_pitcher_id", "INTEGER"),
    ("away_probable_pitcher_id", "INTEGER"),
    ("first_pitch_at", "TIMESTAMPTZ"),
    ("schedule_seen_at", "TIMESTAMPTZ"),
)

_RUN_COLUMNS = (
    ("status", "VARCHAR(16) NOT NULL DEFAULT 'done'"),
    ("progress_done", "INTEGER NOT NULL DEFAULT 0"),
    ("requested_at", "TIMESTAMPTZ"),
    ("started_at", "TIMESTAMPTZ"),
    ("finished_at", "TIMESTAMPTZ"),
    ("error", "TEXT"),
    ("spec_key", "VARCHAR(64)"),
    ("requested_by", "TEXT"),
    ("lineup_source", "VARCHAR(16)"),
    ("bullpen_source", "VARCHAR(16)"),
    ("prop_set", "JSONB"),
    ("linescore", "JSONB"),
    ("decisions", "JSONB"),
    ("replay_run_id", "INTEGER"),
    ("inning_grids", "JSONB"),
)


def upgrade() -> None:
    for name, sql_type in _GAMES_COLUMNS:
        op.execute(f"ALTER TABLE raw.games ADD COLUMN IF NOT EXISTS {name} {sql_type} NULL;")

    op.execute(
        "ALTER TABLE raw.game_lineups "
        "ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'box';"
    )
    op.execute(
        "ALTER TABLE raw.game_lineups ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ NULL;"
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE raw.game_lineups ADD CONSTRAINT ck_game_lineups_source
                CHECK (source IN ('box', 'published', 'projected'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    for name, sql_type in _RUN_COLUMNS:
        op.execute(f"ALTER TABLE sim.sim_runs ADD COLUMN IF NOT EXISTS {name} {sql_type};")
    op.execute("ALTER TABLE sim.sim_runs ALTER COLUMN summary DROP NOT NULL;")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE sim.sim_runs ADD CONSTRAINT ck_sim_runs_status
                CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sim_runs_game_status "
        "ON sim.sim_runs (game_pk, status, created_at DESC);"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sim_runs_active_spec "
        "ON sim.sim_runs (spec_key) WHERE status IN ('queued', 'running');"
    )

    for table in ("raw.game_odds", "raw.prop_odds"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )


def downgrade() -> None:
    for table in ("raw.game_odds", "raw.prop_odds"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS last_seen_at;")

    op.execute("DROP INDEX IF EXISTS sim.uq_sim_runs_active_spec;")
    op.execute("DROP INDEX IF EXISTS sim.idx_sim_runs_game_status;")
    op.execute("ALTER TABLE sim.sim_runs DROP CONSTRAINT IF EXISTS ck_sim_runs_status;")
    op.execute("DELETE FROM sim.sim_runs WHERE summary IS NULL;")
    op.execute("ALTER TABLE sim.sim_runs ALTER COLUMN summary SET NOT NULL;")
    for name, _ in reversed(_RUN_COLUMNS):
        op.execute(f"ALTER TABLE sim.sim_runs DROP COLUMN IF EXISTS {name};")

    op.execute("DELETE FROM raw.game_lineups WHERE source <> 'box';")
    op.execute("ALTER TABLE raw.game_lineups DROP CONSTRAINT IF EXISTS ck_game_lineups_source;")
    op.execute("ALTER TABLE raw.game_lineups DROP COLUMN IF EXISTS published_at;")
    op.execute("ALTER TABLE raw.game_lineups DROP COLUMN IF EXISTS source;")

    for name, _ in reversed(_GAMES_COLUMNS):
        op.execute(f"ALTER TABLE raw.games DROP COLUMN IF EXISTS {name};")
