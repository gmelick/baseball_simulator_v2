-- DuckDB Migration 0012: SIM-411/413/425b batted-ball realism columns
-- Applied: 2026-06-03 (Phase 7, realism follow-on)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0012_sim411_413_425b_battedball_realism_cols.sql
--
-- WHY
-- ---
-- Three realism tickets each need one more per-row fact in the batted-ball pool,
-- and they all need the SAME (cheap) outcome-pool rebuild — so they are unblocked
-- by one migration rather than three:
--   * SIM-411 (park factor)           needs venue_id           per batted ball
--                                      (joins derived.park_factors(venue_id, season)).
--   * SIM-413 (pitcher-hand platoon)  needs p_throws           — ALREADY present on
--                                      sim.outcome_pool (no new column); this migration
--                                      only adds the two missing facts below.
--   * SIM-425b (fielder RBF)          needs the FIELDER IDENTITY (fielder_player_id) of
--                                      the row's play so the sampler can look up that
--                                      fielder's defensive quality and nudge out↔hit↔error
--                                      RELATIVE to the current defender at the same
--                                      position. fielded_by_position (1–9) already exists.
--
-- The columns are populated by PlayerProfileComputor._build_outcome_pool from
-- raw.pitches (rp.venue_id, rp.fielded_by), which the outcome-pool build already
-- JOINs — so the rebuild is the existing 3-season outcome-pool pass (minutes), NOT
-- the full ~5.7 h profile recompute.
--
-- Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS (nullable, no default).
-- Keep in sync with db/schemas/02_duckdb_schema.sql (the canonical schema the
-- computor applies on a fresh build) — both append these two columns AFTER
-- recency_weight so the computor's positional ``INSERT ... SELECT * FROM bip``
-- maps identically on a migrated and a fresh database.

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS sim;

-- SIM-411: venue of the batted ball (joins derived.park_factors on (venue_id, season)).
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS venue_id INTEGER;

-- SIM-425b: the player who fielded the ball (raw.pitches.fielded_by). NULL when no
-- fielder is credited (e.g. home runs). fielded_by_position already records WHICH
-- position (1–9); this records WHO, so the sampler can read that fielder's OAA/error
-- rate from the fielder embedding and apply a RELATIVE defensive nudge.
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_player_id INTEGER;

COMMENT ON COLUMN sim.outcome_pool.venue_id IS
    'SIM-411: venue of the play (raw.pitches.venue_id). Joins derived.park_factors(venue_id, season) for the park run-environment multiplier. NULL on legacy rows built before 0012.';
COMMENT ON COLUMN sim.outcome_pool.fielder_player_id IS
    'SIM-425b: player_id of the fielder who fielded the ball (raw.pitches.fielded_by). With fielded_by_position (the position 1–9) it lets the sampler read the pool fielder''s defensive quality and nudge out/hit/error relative to the current defender. NULL when no fielder is credited.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0012', 'SIM-411/413/425b: add sim.outcome_pool.venue_id (park factor) + fielder_player_id (fielder RBF) -- p_throws already present for the platoon split');
