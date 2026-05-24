-- DuckDB Migration 0004: recency_weight + pool_build_metadata
-- Applied: 2026-05-21 (SIM-076)
-- Adds a sampling `recency_weight` column to all three sim pools and creates the
-- sim.pool_build_metadata table that drives the incremental pool rebuild (SIM-095).
--
-- recency_weight semantics (BA/ML-approved, mirrors the engines' 2x-last-2-seasons
-- recency-boost strategy): 2.0 for the most-recent two seasons relative to the
-- build's reference season, then geometric decay ×0.75 per additional season,
-- floored at 0.25. The PlayPoolSampler multiplies a candidate's distance-weight by
-- recency_weight, so recent form is preferred without dropping older comps.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0004_sim076_recency_weight.sql

PRAGMA database_list;

ALTER TABLE sim.pitch_pool        ADD COLUMN IF NOT EXISTS recency_weight FLOAT DEFAULT 1.0;
ALTER TABLE sim.outcome_pool      ADD COLUMN IF NOT EXISTS recency_weight FLOAT DEFAULT 1.0;
ALTER TABLE sim.stolen_base_pool  ADD COLUMN IF NOT EXISTS recency_weight FLOAT DEFAULT 1.0;

COMMENT ON COLUMN sim.pitch_pool.recency_weight IS
    'SIM-076: sampling recency weight. 2.0 for the most-recent two seasons, ×0.75/season decay (floor 0.25) for older data, relative to the build reference season.';

CREATE TABLE IF NOT EXISTS sim.pool_build_metadata (
    pool_name             VARCHAR(20) NOT NULL,
    season                SMALLINT    NOT NULL,
    row_count             BIGINT      NOT NULL DEFAULT 0,
    source_max_game_date  DATE,
    recency_ref_season    SMALLINT,
    builder_version       VARCHAR(20),
    built_at              TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pool_name, season)
);

COMMENT ON TABLE sim.pool_build_metadata IS
    'SIM-076/SIM-095: per-(pool,season) build watermark + recency reference. Enables incremental pool rebuild.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0004', 'recency_weight on sim pools + pool_build_metadata — SIM-076/SIM-095');
