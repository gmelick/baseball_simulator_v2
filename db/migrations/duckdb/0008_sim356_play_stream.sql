-- DuckDB Migration 0008: pitch-level play-stream / snapshot store (SIM-356)
-- Applied: 2026-05-24 (SIM-356)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0008_sim356_play_stream.sql
--
-- The DuckDB half of the SIM-356 sim-result + pitch-snapshot persistence pair
-- (the Postgres half — durable GameSimSummary history in sim.sim_runs — is
-- Alembic migration 0014).
--
-- WHAT THIS IS
-- ------------
-- One row per pitch of a simulated game, keyed by (game_pk, run_id, sequence).
-- It carries exactly the fields needed to rebuild a
-- simulation.snapshots.PlayByPlayEntry (and therefore a whole PlayByPlay) without
-- replaying the sim — the durable backing for GET .../plays and GET
-- .../state/{at_bat}/{pitch} (SIM-357), replacing the ephemeral Redis / JSONB
-- blob that holds them today.
--
--   run_id          the sim.sim_runs.run_id (Postgres) this stream belongs to —
--                   the cross-store join key. BIGINT to match BIGSERIAL.
--   game_pk         the matchup, denormalized so /plays can query by game alone.
--   sequence        global 0-based pitch index across the game (the order key).
--   at_bat          0-based plate-appearance index (groups pitches into a PA).
--   pitch           1-based pitch number WITHIN the PA.
--   pitch_outcome   ball / called_strike / swinging_strike / foul / in_play.
--   is_contact      ball put in play this pitch.
--   is_pa_end       this pitch resolved the PA (the PA event lives here).
--   event           resolved PA event on the terminal pitch (single/strikeout/…),
--                   NULL on a non-terminal pitch.
--   runs_scored     integer runs that physically scored on the play.
--   outs_recorded   outs recorded by the play.
--   exit_velo       batted-ball exit velocity (NULL on a non-contact pitch).
--   launch_angle    batted-ball launch angle (NULL on a non-contact pitch).
--   spray_angle     batted-ball spray angle (NULL on a non-contact pitch).
--   runs            RE24 / linear-weight run *value* of the play.
--   canonical_event canonical RUN_VALUES key the event resolved to (NULL if none).
--
-- These columns are a 1:1 superset of PlayByPlayEntry's constructor inputs, so a
-- loaded row maps straight back onto PlayByPlayEntry(**row) (minus run_id/game_pk,
-- which are the lookup keys, not entry fields). See db/sim_store.py for the
-- authoritative read/write row schema the SIM-357 endpoints build against.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS per project convention; the
-- table lives in the existing `sim` schema (created in 0001 / 02_duckdb_schema.sql).

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS sim;

CREATE TABLE IF NOT EXISTS sim.play_stream (
    run_id          BIGINT      NOT NULL,
    game_pk         INTEGER     NOT NULL,
    sequence        INTEGER     NOT NULL,
    at_bat          INTEGER     NOT NULL,
    pitch           INTEGER     NOT NULL,
    pitch_outcome   VARCHAR     NOT NULL,
    is_contact      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_pa_end       BOOLEAN     NOT NULL DEFAULT FALSE,
    event           VARCHAR,
    runs_scored     INTEGER     NOT NULL DEFAULT 0,
    outs_recorded   INTEGER     NOT NULL DEFAULT 0,
    exit_velo       DOUBLE,
    launch_angle    DOUBLE,
    spray_angle     DOUBLE,
    runs            DOUBLE      NOT NULL DEFAULT 0.0,
    canonical_event VARCHAR,
    PRIMARY KEY (run_id, sequence)
);

-- /plays + /state read the whole ordered stream for a (game_pk, run_id); the
-- PK already orders within a run, this index serves the by-game lookup that
-- resolves a game_pk to its run(s) and orders the scroll.
CREATE INDEX IF NOT EXISTS idx_play_stream_game_seq
    ON sim.play_stream(game_pk, run_id, sequence);

COMMENT ON TABLE sim.play_stream IS
    'SIM-356: pitch-level play-stream — one row per pitch keyed by (run_id, sequence). Rebuilds PlayByPlayEntry/PlayByPlay for GET /plays + /state (SIM-357). run_id joins to Postgres sim.sim_runs.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0008', 'pitch-level play-stream / snapshot store sim.play_stream keyed by (game_pk, run_id, sequence) backing /plays + /state (SIM-356)');
