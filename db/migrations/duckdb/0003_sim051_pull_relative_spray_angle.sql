-- ############################################################################
-- SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27.
-- The header/comment text below states the `bat_hand` vs `stand` semantics
-- EXACTLY BACKWARDS. Ground truth, measured over 2017-2025:
--   * `stand`    = the side actually batted from THIS PA. 'S' on 0 rows.
--   * `bat_hand` = the ROSTER-DECLARED side. 'S' on 10.4-13.3% of rows.
-- The SQL below is left byte-for-byte as applied (it is a historical record);
-- read db/schemas/01_postgres_schema.sql for the canonical definition.
-- ############################################################################

-- DuckDB Migration 0003: pull_relative_spray_angle
-- Applied: 2026-05-20 (SIM-051)
-- Adds the `pull_relative_spray_angle` column to sim.outcome_pool.
-- This is the handedness-corrected spray angle consumed by the batted-ball
-- similarity engine (SIM-042) via `_select_spray_column`, so that "pull" is
-- always positive regardless of batter handedness.  Without it the loader
-- falls back to the raw `spray_angle`, which is biased by handedness.
--
-- Formula (handedness flip; BA-approved):
--   pull_relative_spray_angle =
--       CASE
--           WHEN bat_hand = 'R' THEN  spray_angle   -- pull = LF, already positive
--           WHEN bat_hand = 'L' THEN -spray_angle   -- pull = RF, flip sign
--           ELSE NULL                               -- unresolved switch hitter ('S')
--       END
--   where:
--     `bat_hand` is the per-PA resolved batter handedness on raw.pitches
--     (NOT the roster `bats` value), so a switch hitter is flipped using the
--     hand he actually batted with for that plate appearance.
--   Sign convention: positive = pull side (LF for RHB, RF for LHB).
--
-- NULL handling: rows with bat_hand='S' (unresolved switch hitter) or a NULL
-- spray_angle stay NULL.  Switch-hitter NULLs are gate-controlled to ≤ 1 %
-- per season by SIM-160.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0003_sim051_pull_relative_spray_angle.sql

PRAGMA database_list;

-- Idempotent: DuckDB raises a duplicate column error if re-applied without
-- IF NOT EXISTS.  Wrap in a defensive block.
ALTER TABLE sim.outcome_pool
    ADD COLUMN IF NOT EXISTS pull_relative_spray_angle FLOAT;

COMMENT ON COLUMN sim.outcome_pool.pull_relative_spray_angle IS
    'SIM-051: handedness-corrected spray angle. R: +spray_angle; L: -spray_angle; S/NULL: NULL. Positive = pull side (LF for RHB, RF for LHB). Flipped on per-PA bat_hand at ETL time.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0003', 'pull_relative_spray_angle — SIM-051: handedness-corrected spray angle for batted-ball engine');
