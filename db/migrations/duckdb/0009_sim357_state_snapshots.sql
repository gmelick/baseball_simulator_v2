-- DuckDB Migration 0009: pitch-level field/state snapshot store (SIM-357)
-- Applied: 2026-05-24 (SIM-357)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0009_sim357_state_snapshots.sql
--
-- The DuckDB backing for GET /api/games/{game_pk}/state/{at_bat}/{pitch}
-- (SIM-357). The sibling sim.play_stream (0008) carries one PlayByPlayEntry per
-- pitch — enough to rebuild the /plays scroll — but it does NOT carry the
-- field/baserunner/count state AS OF a pitch. This table does: one row per
-- pitch holding the serialized simulation.snapshots.StateAtPitch (a
-- FieldSnapshot tagged with at_bat/pitch), so /state can return the point-in-
-- time snapshot without replaying the sim.
--
-- WHAT THIS IS
-- ------------
-- One row per pitch of a simulated game, keyed by (run_id, sequence) like
-- sim.play_stream, and looked up by (game_pk[, run_id], at_bat, pitch).
--
--   run_id          the sim.sim_runs.run_id (Postgres) this stream belongs to —
--                   the cross-store join key (matches sim.play_stream.run_id).
--                   BIGINT to match BIGSERIAL.
--   game_pk         the matchup, denormalized so /state can query by game alone.
--   at_bat          0-based plate-appearance index this snapshot is as-of.
--   pitch           1-based pitch number WITHIN the PA this snapshot is as-of.
--   sequence        global 0-based pitch index across the game (order key / PK).
--   snapshot        the StateAtPitch serialized via api.serialization.to_jsonable
--                   then json.dumps — a numpy-free JSON object the /state
--                   endpoint reloads into a StateAtPitchModel. Stored as VARCHAR
--                   (JSON text) so the row shape stays engine-agnostic and the
--                   reader owns parsing (mirrors the sim.sim_runs JSONB summary
--                   convention on the Postgres side).
--
-- The (game_pk, run_id, at_bat, pitch) tuple is the natural /state lookup key.
-- (run_id, sequence) is the PK (one snapshot per pitch per run, ordered).
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS per project convention; the
-- table lives in the existing `sim` schema (created in 0001 / 02_duckdb_schema.sql).

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS sim;

CREATE TABLE IF NOT EXISTS sim.state_snapshots (
    run_id      BIGINT      NOT NULL,
    game_pk     INTEGER     NOT NULL,
    at_bat      INTEGER     NOT NULL,
    pitch       INTEGER     NOT NULL,
    sequence    INTEGER     NOT NULL,
    snapshot    VARCHAR     NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

-- /state resolves a (game_pk[, run_id], at_bat, pitch) to its snapshot; this
-- index serves that point lookup (and orders the by-game scan when run_id is
-- omitted so the latest run's rows stay grouped).
CREATE INDEX IF NOT EXISTS idx_state_snapshots_game_atbat_pitch
    ON sim.state_snapshots(game_pk, run_id, at_bat, pitch);

COMMENT ON TABLE sim.state_snapshots IS
    'SIM-357: pitch-level field/state snapshots — one serialized StateAtPitch per pitch keyed by (run_id, sequence), looked up by (game_pk, run_id, at_bat, pitch). Backs GET /state/{at_bat}/{pitch}. run_id joins to Postgres sim.sim_runs and sim.play_stream.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0009', 'pitch-level field/state snapshot store sim.state_snapshots keyed by (run_id, sequence), looked up by (game_pk, run_id, at_bat, pitch) backing GET /state (SIM-357)');
