-- ############################################################################
-- SUPERSEDED IN PART BY DuckDB MIGRATION 0014 (SIM-440), 2026-07-27.
-- The header/comment text below states the `bat_hand` vs `stand` semantics
-- EXACTLY BACKWARDS. Ground truth, measured over 2017-2025:
--   * `stand`    = the side actually batted from THIS PA. 'S' on 0 rows.
--   * `bat_hand` = the ROSTER-DECLARED side. 'S' on 10.4-13.3% of rows.
-- The SQL below is left byte-for-byte as applied (it is a historical record);
-- read db/schemas/01_postgres_schema.sql for the canonical definition.
-- ############################################################################

-- DuckDB Migration 0007: park-factor splits + data-layer fixes (SIM-336 / SIM-345)
-- Applied: 2026-05-23 (SIM-336 + SIM-345)
--
-- Covers the schema half of two bug tickets that share pipeline/batch/
-- player_profile_computor.py:
--
-- SIM-336 (park factors)
--   * derived.park_factors already carries factor_vs_l / factor_vs_r (created in
--     the 0001 baseline from 02_duckdb_schema.sql). The computor previously wrote
--     them as hard-coded NULL; 0007 only re-documents them and defensively ADDs
--     the columns IF NOT EXISTS so a DB that predates them is brought to parity.
--     The factor_overall UNPIVOT-ordering fix and the real L/R splits are code
--     changes in the computor, not schema changes.
--
-- SIM-345 (data layer)
--   * sim.pool_build_metadata gains source_row_count — paired with
--     source_max_game_date so the incremental rebuild detects a same-game_date
--     late/doubleheader row (which does NOT advance the watermark). The computor's
--     freshness test becomes "watermark not advanced AND row count unchanged".
--   * recency_weight NOT NULL parity: migration 0004 added the column as
--     `FLOAT DEFAULT 1.0` (NULLABLE), but 02_duckdb_schema.sql declares it
--     `FLOAT NOT NULL DEFAULT 1.0`. 0007 backfills any NULLs to 1.0 and enforces
--     NOT NULL on all three pools so schema and migrations agree.
--   * recency_ref_season is now canonical across pools (computor change); no
--     schema change required — documented on the metadata table.
--   * `stand` vs `bat_hand` pool contract: the pool `stand` column is now the
--     RESOLVED batting side (bat_hand when L/R, else declared stand). Pure code
--     change (no column rename), so existing rows remain valid; documented here.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; NULL backfill before SET NOT NULL;
-- SET NOT NULL guarded so re-running is a no-op (it is already NOT NULL).
-- No indexes are dropped by this migration.
--
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0007_sim336_sim345_park_factors_data_layer.sql

PRAGMA database_list;

-- SIM-336: defensively ensure the park-factor L/R split columns exist ----------
ALTER TABLE derived.park_factors ADD COLUMN IF NOT EXISTS factor_vs_l FLOAT;
ALTER TABLE derived.park_factors ADD COLUMN IF NOT EXISTS factor_vs_r FLOAT;

COMMENT ON COLUMN derived.park_factors.factor_vs_l IS
    'SIM-336: venue-vs-LHB rate / league-vs-LHB rate (split on batter stand). NULL when the venue had no vs-LHB sample for the factor type.';
COMMENT ON COLUMN derived.park_factors.factor_vs_r IS
    'SIM-336: venue-vs-RHB rate / league-vs-RHB rate (split on batter stand). NULL when the venue had no vs-RHB sample for the factor type.';

-- SIM-345: source_row_count watermark guard on pool_build_metadata ------------
ALTER TABLE sim.pool_build_metadata ADD COLUMN IF NOT EXISTS source_row_count BIGINT;

COMMENT ON COLUMN sim.pool_build_metadata.source_row_count IS
    'SIM-345: source row count at build time. Paired with source_max_game_date so a same-game_date late/doubleheader row (which does not advance the watermark) is still detected. A season is fresh only when watermark not advanced AND this count unchanged.';

-- SIM-345: recency_weight NOT NULL parity with 02_duckdb_schema.sql -----------
-- Backfill any pre-existing NULLs to the league-neutral default before enforcing.
UPDATE sim.pitch_pool       SET recency_weight = 1.0 WHERE recency_weight IS NULL;
UPDATE sim.outcome_pool     SET recency_weight = 1.0 WHERE recency_weight IS NULL;
UPDATE sim.stolen_base_pool SET recency_weight = 1.0 WHERE recency_weight IS NULL;

ALTER TABLE sim.pitch_pool       ALTER COLUMN recency_weight SET NOT NULL;
ALTER TABLE sim.outcome_pool     ALTER COLUMN recency_weight SET NOT NULL;
ALTER TABLE sim.stolen_base_pool ALTER COLUMN recency_weight SET NOT NULL;

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0007', 'park-factor L/R splits + factor_overall fix (SIM-336); source_row_count watermark guard + recency_weight NOT NULL parity + canonical recency_ref_season + stand/bat_hand pool contract (SIM-345)');
