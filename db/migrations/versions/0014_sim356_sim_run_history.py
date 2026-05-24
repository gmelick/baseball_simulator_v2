"""sim356_sim_run_history

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-24

SIM-356: durable sim-result history (the Postgres half of the
sim-result + pitch-snapshot persistence pair; the pitch-level play-stream is
the DuckDB half — migration 0008).

Problem
-------
Today a /simulate run's :class:`simulation.results.GameSimSummary` is only ever
held in an ephemeral Redis / ``sim.lineup_state.simulation_results`` JSONB blob
that expires (24h) and is overwritten on the next run for the same matchup.
There is no durable history a consumer can query for "the last N sims of game X"
or "the summary behind run_id R", which is what the Phase-5 /simulate result
lookup + sim-history surface need.

Fix
---
Add ``sim.sim_runs`` — one durable row per Monte-Carlo run:

  * ``run_id``        BIGSERIAL PK — the stable handle a play-stream (DuckDB
                      ``sim.play_stream``) and the API key their lookups on.
  * ``game_pk``       INTEGER NOT NULL — the matchup the run is for.
  * ``n_iterations``  INTEGER NOT NULL — iterations aggregated into the summary.
  * ``base_seed``     BIGINT — the run's base RNG seed (NULL when not seeded).
  * ``summary``       JSONB  NOT NULL — ``to_jsonable(GameSimSummary)`` dict
                      (the full SIM-350 JSON contract: win%s, score arrays, CIs,
                      simulated_at, ...). Read back as the canonical sim result.
  * ``created_at``    TIMESTAMPTZ NOT NULL DEFAULT NOW() — insertion time, the
                      sort key for "latest run for this game".

Index ``idx_sim_runs_game_created`` on (game_pk, created_at DESC) backs the
"latest / last-N runs for game_pk" history query (the dominant read path).

This is purely additive: ``sim.lineup_state`` (the ephemeral live-game blob) is
left untouched; ``sim.sim_runs`` is the durable replacement for the *result
history* use case, not for the live-game working state.

Credentials: none. The DB URL is read from BASEBALL_DB_DSN at runtime by
db/migrations/env.py; nothing is hard-coded here. All DDL is IF NOT EXISTS per
project convention so re-running upgrade() is a no-op.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sim.sim_runs (
            run_id          BIGSERIAL   PRIMARY KEY,
            game_pk         INTEGER     NOT NULL,
            n_iterations    INTEGER     NOT NULL,
            base_seed       BIGINT,
            summary         JSONB       NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # Dominant read path: "latest / last-N runs for this matchup". DESC so the
    # planner can satisfy ORDER BY created_at DESC LIMIT n with an index scan.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sim_runs_game_created
            ON sim.sim_runs(game_pk, created_at DESC)
        """
    )
    op.execute(
        "COMMENT ON TABLE sim.sim_runs IS "
        "'SIM-356: durable per-run GameSimSummary history "
        "(to_jsonable(GameSimSummary) in summary). Backs /simulate result lookup "
        "+ sim history; run_id is the handle the DuckDB sim.play_stream keys on.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sim.idx_sim_runs_game_created")
    op.execute("DROP TABLE IF EXISTS sim.sim_runs")
