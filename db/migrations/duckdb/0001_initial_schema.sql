-- DuckDB Migration 0001: initial_schema
-- Applied: 2026-05-05
-- Mirrors the DuckDB-side schema as defined in db/schemas/02_duckdb_schema.sql.
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0001_initial_schema.sql
--
-- This file documents the DuckDB schema state at Phase 1 completion.
-- Future DuckDB schema changes must be added as 0002_*.sql, 0003_*.sql, etc.
-- Update db/schemas/duckdb_schema_version.txt to match the latest migration number.

-- Source the canonical schema file (idempotent — all statements use IF NOT EXISTS)
-- Note: DuckDB does not support native migration tooling; apply these files in order.

PRAGMA database_list;  -- confirm connection before applying

-- Schema version tracking table (DuckDB equivalent of alembic_version)
CREATE TABLE IF NOT EXISTS migration_history (
    migration_id    VARCHAR     PRIMARY KEY,
    applied_at      TIMESTAMP   NOT NULL DEFAULT now(),
    description     VARCHAR     NOT NULL
);

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0001', 'initial_schema — Phase 1 DuckDB schema baseline');
