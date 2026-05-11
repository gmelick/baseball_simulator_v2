-- DuckDB Migration 0002: catcher_attempt_rate_against
-- Applied: 2026-05-08 (SIM-073)
-- Adds the `steal_attempt_rate_against` column to derived.catcher_season_metrics.
-- This is the deterrence signal consumed by the catcher similarity engine v2
-- (SIM-072) — the rate at which opposing runners attempt to steal against
-- this catcher per PA-level opportunity.
--
-- Formula (BA-approved):
--   steal_attempt_rate_against = (SB + CS) / (opp_1B + opp_2B)
--   where:
--     opp_1B = count of plate appearances where runner on 1B and 2B is empty
--     opp_2B = count of plate appearances where runner on 2B and 3B is empty
--   Opportunities are counted at the PA level (de-duplicated across pitches),
--   and PAs where the runner was forced to advance are excluded by requiring
--   the next base to be empty.
--
-- Min-sample guard: NULL if opportunity denominator < 100 PA opportunities.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0002_catcher_attempt_rate_against.sql

PRAGMA database_list;

-- Idempotent: DuckDB raises a duplicate column error if re-applied without
-- IF NOT EXISTS.  Wrap in a defensive block.
ALTER TABLE derived.catcher_season_metrics
    ADD COLUMN IF NOT EXISTS steal_attempt_rate_against FLOAT;

COMMENT ON COLUMN derived.catcher_season_metrics.steal_attempt_rate_against IS
    'SIM-073: PA-level rate of steal attempts against this catcher. (SB+CS)/(opp_1B+opp_2B). NULL when opportunity denominator < 100 PAs.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0002', 'catcher_attempt_rate_against — SIM-073: deterrence column for catcher engine v2');
