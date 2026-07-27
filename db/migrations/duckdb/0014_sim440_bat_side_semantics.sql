-- DuckDB Migration 0014: correct the pull_relative_spray_angle column comment
-- Applied: 2026-07-27 (SIM-440)
--
-- WHY
-- ---
-- Migration 0003 (SIM-051) shipped this COMMENT ON COLUMN:
--
--     '... Flipped on per-PA bat_hand at ETL time.'
--
-- and its header block asserted:
--
--     `bat_hand` is the per-PA resolved batter handedness on raw.pitches
--     (NOT the roster `bats` value)
--
-- Both statements are BACKWARDS, and because a COMMENT is persisted INTO the
-- database, the wrong definition shipped to every deployment and is served to
-- anyone introspecting the schema — including the SIM-439 SQL console and the
-- AI assistant's schema catalog.
--
-- Ground truth (measured against the live DB, all seasons 2017-2025 —
-- docs/data_quality/2026-05-20-bat-side-coverage.md):
--
--   raw.pitches.stand     = the side actually batted from THIS plate appearance
--                           (feed/live in-game context; MLB has already resolved
--                           switch hitters against the pitcher).
--                           'S' on 0 rows, every season.
--   raw.pitches.bat_hand  = the ROSTER-DECLARED side (/api/v1/people/{id}).
--                           Constant per player, 'S' for every switch hitter.
--                           'S' on 10.4%-13.3% of rows, every season.
--
-- CONSEQUENCE OF THE OLD DEFINITION
-- ---------------------------------
-- `_build_outcome_pool` keyed the sign flip on `bat_hand`, so every switch
-- hitter's batted balls got pull_relative_spray_angle = NULL — and
-- `engine_artifacts.build_battedball_pool_artifact` filters on
-- `pull_relative_spray_angle IS NOT NULL`.  Roughly 1 in 8 batted balls was
-- therefore excluded outright from the production batted-ball draw.  The
-- expression now keys on `stand`.
--
-- SCOPE
-- -----
-- Comment-only.  No column is added, dropped or retyped, and no row is
-- rewritten.  The stored VALUES are still the old bat_hand-keyed ones (NULL for
-- switch hitters) until `make profile-computor` rebuilds sim.outcome_pool.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0014_sim440_bat_side_semantics.sql

PRAGMA database_list;

COMMENT ON COLUMN sim.outcome_pool.pull_relative_spray_angle IS
    'SIM-051/SIM-440: handedness-corrected spray angle. Keyed on `stand` — the side the batter ACTUALLY BATTED FROM this PA (never ''S''), NOT the roster-declared `bat_hand` (''S'' for every switch hitter). stand=''R'': +spray_angle; stand=''L'': -spray_angle. Positive = pull side (LF for RHB, RF for LHB). NULL only when spray_angle is NULL.';

COMMENT ON COLUMN sim.outcome_pool.stand IS
    'SIM-345/SIM-440: the RESOLVED batting side for the plate appearance, copied straight from raw.pitches.stand. Always ''L'' or ''R'' — never ''S''. This is the pool pre-filter / FAISS tile key. Do not confuse with raw.pitches.bat_hand, which is the roster-declared side.';

COMMENT ON COLUMN sim.pitch_pool.stand IS
    'SIM-345/SIM-440: the RESOLVED batting side for the plate appearance, copied straight from raw.pitches.stand. Always ''L'' or ''R'' — never ''S''. Second half of the (season, pitcher_id, stand) pitch tile key.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0014', 'SIM-440: correct the pull_relative_spray_angle / stand column comments — bat_hand is the roster-declared side, stand is the per-PA resolved side (0003 documented them inverted)');
