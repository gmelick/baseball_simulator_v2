-- DuckDB Migration 0005: prune sim-pool indexes (SIM-115)
-- Applied: 2026-05-21 (SIM-115)
-- DuckDB is columnar with built-in zone maps + Bloom filters, so it benefits far
-- less from secondary indexes than PostgreSQL. The play-pool query path (SIM-111
-- contracts) pre-filters by pitcher before materializing a tile and otherwise
-- reads by primary key, so most sim-pool secondary indexes only add nightly
-- rebuild overhead on 1M+ row tables. This migration drops the write-overhead
-- indexes, keeping only the query-path ones.
--
-- KEEP  sim.pitch_pool : idx_pp_pitcher_season, idx_pp_outcome, idx_pp_count
-- KEEP  sim.outcome_pool: idx_op_season
-- (Primary keys on pitch_id are retained automatically.)
--
-- Idempotent: DROP INDEX IF EXISTS. Index names are schema-qualified (sim.) — an
-- unqualified DROP INDEX silently no-ops because the indexes live in the sim schema.
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0005_sim115_prune_pool_indexes.sql

PRAGMA database_list;

-- sim.pitch_pool — drop 8 write-overhead indexes
DROP INDEX IF EXISTS sim.idx_pp_pitcher;
DROP INDEX IF EXISTS sim.idx_pp_batter;
DROP INDEX IF EXISTS sim.idx_pp_season;
DROP INDEX IF EXISTS sim.idx_pp_game_date;
DROP INDEX IF EXISTS sim.idx_pp_batter_season;
DROP INDEX IF EXISTS sim.idx_pp_runners;
DROP INDEX IF EXISTS sim.idx_pp_velo;
DROP INDEX IF EXISTS sim.idx_pp_ivb;

-- sim.outcome_pool — drop 9 write-overhead indexes (keep idx_op_season)
DROP INDEX IF EXISTS sim.idx_op_pitcher;
DROP INDEX IF EXISTS sim.idx_op_batter;
DROP INDEX IF EXISTS sim.idx_op_bb_type;
DROP INDEX IF EXISTS sim.idx_op_exit_velo;
DROP INDEX IF EXISTS sim.idx_op_launch_angle;
DROP INDEX IF EXISTS sim.idx_op_spray_angle;
DROP INDEX IF EXISTS sim.idx_op_runners;
DROP INDEX IF EXISTS sim.idx_op_result_hits;
DROP INDEX IF EXISTS sim.idx_op_fielded_by;

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0005', 'prune sim-pool indexes (keep query-path, drop write-overhead) SIM-115');
