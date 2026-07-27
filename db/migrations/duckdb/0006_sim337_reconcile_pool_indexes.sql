-- ############################################################################
-- SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27.
-- The header/comment text below states the `bat_hand` vs `stand` semantics
-- EXACTLY BACKWARDS. Ground truth, measured over 2017-2025:
--   * `stand`    = the side actually batted from THIS PA. 'S' on 0 rows.
--   * `bat_hand` = the ROSTER-DECLARED side. 'S' on 10.4-13.3% of rows.
-- The SQL below is left byte-for-byte as applied (it is a historical record);
-- read db/schemas/01_postgres_schema.sql for the canonical definition.
-- ############################################################################

-- DuckDB Migration 0006: reconcile sim-pool indexes to the SIM-111 contract (SIM-337)
-- Applied: 2026-05-22 (SIM-337)
--
-- SIM-115 (migration 0005) pruned the sim-pool indexes but contradicted the
-- SIM-111 play-pool query contract (§6.2), which is the authoritative input it
-- was supposed to act on. The contract states the pitch read path PRE-FILTERS on
-- `pitcher_id` + `stand` (batter handedness) before any FAISS tile is
-- materialized, and otherwise reads by primary key; the situation/feature/outcome
-- columns are read in BULK inside an already-(pitcher_id, season, stand)-scoped
-- scan — never via a single-column WHERE predicate. So:
--
--   * The pitcher / season indexes (pre-filter keys) must be KEPT.
--   * The outcome_type / count indexes (projected or bulk-read) must be DROPPED.
--   * A `stand`-bearing composite must be ADDED — `stand` is half the pitch
--     pre-filter (contract §6.2 note C) yet no `stand` index existed on either
--     pool.
--
-- 0005 got this backwards on sim.pitch_pool: it KEPT idx_pp_outcome + idx_pp_count
-- (contract says DROP) and DROPPED idx_pp_pitcher + idx_pp_season + idx_pp_game_date
-- (contract says KEEP). It also never added a `stand` index on either pool.
--
-- This migration reconciles both pools to §6.2.
--
-- FINAL index set after 0006 (PKs on pitch_id retained automatically):
--   sim.pitch_pool  : idx_pp_pitcher_season (pitcher_id, season)        [KEEP, contract C]
--                     idx_pp_pitcher        (pitcher_id)                [RESTORE, contract: prefix/single-pitcher]
--                     idx_pp_season         (season)                    [RESTORE, advisory: season rebuilds]
--                     idx_pp_game_date      (game_date)                 [RESTORE, advisory: MAX(game_date) watermark]
--                     idx_pp_pitcher_stand_season (pitcher_id, stand, season) [ADD, fully serves C]
--   sim.outcome_pool: idx_op_season         (season)                   [KEEP, contract E/F]
--                     idx_op_stand_season   (stand, season)            [ADD, fully serves E]
--
-- The handedness column on both pools is `stand` (VARCHAR(1) NOT NULL). `bat_hand`
-- appears only in build-side candidate-selection logic / comments, not as a real
-- column. The `stand`/`bat_hand` contract cleanup is SIM-345; this migration just
-- indexes the column that actually exists.
--
-- Idempotent: DROP INDEX IF EXISTS / CREATE INDEX IF NOT EXISTS.
-- ALL DROPs are SCHEMA-QUALIFIED (sim.) — an unqualified DROP INDEX silently
-- no-ops because indexes live in their table's schema.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql

PRAGMA database_list;

-- sim.pitch_pool ------------------------------------------------------------
-- Drop the two indexes 0005 wrongly kept (contract §6.2 says DROP):
--   idx_pp_outcome — outcome_type is projected in query A, never filtered.
--   idx_pp_count   — count/outs are situation columns, bulk-read by the KDTree
--                    (query H), never used as a WHERE predicate.
DROP INDEX IF EXISTS sim.idx_pp_outcome;
DROP INDEX IF EXISTS sim.idx_pp_count;

-- Restore the pre-filter / advisory indexes 0005 wrongly dropped (contract: KEEP):
CREATE INDEX IF NOT EXISTS idx_pp_pitcher_season  ON sim.pitch_pool(pitcher_id, season);  -- serves C (load-bearing)
CREATE INDEX IF NOT EXISTS idx_pp_pitcher         ON sim.pitch_pool(pitcher_id);          -- prefix / single-pitcher scans
CREATE INDEX IF NOT EXISTS idx_pp_season          ON sim.pitch_pool(season);              -- advisory: season-scoped rebuilds
CREATE INDEX IF NOT EXISTS idx_pp_game_date       ON sim.pitch_pool(game_date);           -- advisory: MAX(game_date) watermark

-- Add the stand-bearing composite so the pitch-tile pre-filter (C) is fully
-- index-served (pitcher_id + stand + season) rather than relying on the
-- (pitcher_id, season) prefix plus a residual stand filter.
CREATE INDEX IF NOT EXISTS idx_pp_pitcher_stand_season ON sim.pitch_pool(pitcher_id, stand, season);

-- sim.outcome_pool ----------------------------------------------------------
-- 0005 already kept only idx_op_season (correct per §6.2 E/F). Add the
-- stand-bearing composite so the batted-ball pre-filter (E: season + stand) is
-- fully index-served. (Defensive DROPs of the contract-DROP indexes in case an
-- older DB still carries them — all schema-qualified.)
DROP INDEX IF EXISTS sim.idx_op_pitcher;
DROP INDEX IF EXISTS sim.idx_op_batter;
DROP INDEX IF EXISTS sim.idx_op_bb_type;
DROP INDEX IF EXISTS sim.idx_op_exit_velo;
DROP INDEX IF EXISTS sim.idx_op_launch_angle;
DROP INDEX IF EXISTS sim.idx_op_spray_angle;
DROP INDEX IF EXISTS sim.idx_op_runners;
DROP INDEX IF EXISTS sim.idx_op_result_hits;
DROP INDEX IF EXISTS sim.idx_op_fielded_by;

CREATE INDEX IF NOT EXISTS idx_op_season       ON sim.outcome_pool(season);          -- serves E/F
CREATE INDEX IF NOT EXISTS idx_op_stand_season ON sim.outcome_pool(stand, season);   -- ADD: fully serves E

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0006', 'reconcile sim-pool indexes to SIM-111 contract §6.2 + add stand composites — SIM-337');
