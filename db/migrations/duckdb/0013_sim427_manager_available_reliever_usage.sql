-- DuckDB Migration 0013: SIM-427 manager USAGE capstone — available_reliever_usage_rate
-- Applied: 2026-06-03 (Phase 7, manager USAGE refinement)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0013_sim427_manager_available_reliever_usage.sql
--
-- WHY
-- ---
-- The manager USAGE sub-score (SIM-427) was un-gated to derive bullpen tendencies
-- from raw.pitches pitcher-stints. The SIM-433-v2 ingest now records the full
-- per-game roster in raw.game_bullpen_availability, so a manager's bullpen usage
-- can finally be normalized by OPPORTUNITY: of the relievers a manager had
-- AVAILABLE, what fraction did they actually use (separating "chose to hold an arm"
-- from "no arm could pitch"). PlayerProfileComputor._compute_manager_profiles
-- computes it (the `bullpen_opp` CTE) and writes it here.
--
-- Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS (nullable, no default).
-- Keep in sync with db/schemas/02_duckdb_schema.sql.

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS derived;

ALTER TABLE derived.manager_season_metrics
    ADD COLUMN IF NOT EXISTS available_reliever_usage_rate FLOAT;

COMMENT ON COLUMN derived.manager_season_metrics.available_reliever_usage_rate IS
    'SIM-427: relievers used / relievers available per game (the SIM-433-v2 available-but-unused signal), aggregated per season — manager bullpen usage normalized by opportunity. NULL when no availability rows exist for the manager''s games yet.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0013', 'SIM-427: manager available_reliever_usage_rate (usage normalized by SIM-433-v2 availability)');
