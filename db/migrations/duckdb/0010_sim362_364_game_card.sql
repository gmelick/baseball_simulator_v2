-- DuckDB Migration 0010: per-game card store (linescore + decisions) (SIM-362/364)
-- Applied: 2026-05-24 (Phase 5, Sprint 4 Wave 2)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0010_sim362_364_game_card.sql
--
-- The DuckDB backing for GET /api/games/{game_pk}/linescore + .../decisions
-- (and the combined .../card). The sibling sim.play_stream (0008) carries one
-- PlayByPlayEntry per pitch and sim.state_snapshots (0009) the per-pitch field
-- state, but neither carries the DERIVED, game-level loop outputs the box-score
-- surface needs: the per-inning linescore (R/H/E grid) and the W/L/Save
-- pitcher decisions. Those derivations (simulation.linescore.linescore_from_plays
-- + simulation.pitcher_decisions.decisions_from_plays) read PlayResult.next_state,
-- which the persisted PlayByPlayEntry rows do NOT carry, so they MUST be computed
-- at RECORD time (from the recorded PlayResult list) and persisted here -- not
-- re-derived at read time.
--
-- WHAT THIS IS
-- ------------
-- ONE row per simulated game (per run), keyed by (run_id) and looked up by
-- (game_pk[, run_id]), holding two numpy-free JSON blobs:
--
--   run_id          the sim.sim_runs.run_id (Postgres) this card belongs to --
--                   the cross-store join key (matches sim.play_stream.run_id and
--                   sim.state_snapshots.run_id). BIGINT to match BIGSERIAL.
--   game_pk         the matchup, denormalized so /linescore + /decisions can
--                   query by game alone.
--   linescore       the simulation.linescore.Linescore serialized via
--                   api.serialization.to_jsonable then json.dumps -- a numpy-free
--                   JSON object the /linescore endpoint reloads into a
--                   LinescoreModel. Stored as VARCHAR (JSON text), mirroring the
--                   sim.state_snapshots.snapshot convention.
--   decisions       the simulation.pitcher_decisions.PitcherDecisions serialized
--                   the same way, reloaded into a PitcherDecisionsModel.
--
-- (run_id) is the PK (one card per run). The (game_pk, run_id) index serves the
-- by-game lookup (and orders the by-game scan when run_id is omitted so the
-- latest run wins -- ORDER BY run_id DESC).
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS per project convention; the
-- table lives in the existing `sim` schema (created in 0001 / 02_duckdb_schema.sql).

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS sim;

CREATE TABLE IF NOT EXISTS sim.game_cards (
    run_id      BIGINT      NOT NULL,
    game_pk     INTEGER     NOT NULL,
    linescore   VARCHAR     NOT NULL,
    decisions   VARCHAR     NOT NULL,
    PRIMARY KEY (run_id)
);

-- /linescore + /decisions resolve a (game_pk[, run_id]) to its card; this index
-- serves that lookup (and orders the by-game scan when run_id is omitted so the
-- latest run's row wins via ORDER BY run_id DESC).
CREATE INDEX IF NOT EXISTS idx_game_cards_game
    ON sim.game_cards(game_pk, run_id);

COMMENT ON TABLE sim.game_cards IS
    'SIM-362/364: per-game derived loop outputs -- one row per run holding the serialized Linescore (R/H/E grid) + PitcherDecisions (W/L/Save) keyed by (run_id), looked up by (game_pk, run_id). Backs GET /linescore + /decisions + /card. run_id joins to Postgres sim.sim_runs and DuckDB sim.play_stream / sim.state_snapshots.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0010', 'per-game card store sim.game_cards (linescore + decisions) keyed by (run_id), looked up by (game_pk, run_id) backing GET /linescore + /decisions + /card (SIM-362/364)');
