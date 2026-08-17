-- DuckDB Migration 0015: SIM-468 — sim.steal_opportunity_pool
-- Applied: 2026-08-16 (SIM-468)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0015_sim468_steal_opportunity_pool.sql
--
-- WHY
-- ---
-- `sim.stolen_base_pool` holds only ATTEMPTS, so a draw over it attempts a
-- steal 100% of the time — it has no denominator. This table holds one row per
-- pitch where a steal was POSSIBLE (a runner on first with second open, or on
-- second with third open), attempted or not, with an `attempted` flag beside
-- the `success` flag. One draw then answers both "does the runner go" and
-- "safe or caught", and the catcher affects the DECISION, not only the
-- outcome (SIM-474 consumes this pool).
--
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS only. Nothing is dropped.
-- Keep in sync with db/schemas/02_duckdb_schema.sql.

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS sim;

CREATE TABLE IF NOT EXISTS sim.steal_opportunity_pool (
    -- Surrogate id joined from sim.pitch_pool on (game_pk, at_bat_number,
    -- pitch_number), like sim.stolen_base_pool.
    pitch_id        BIGINT      NOT NULL,
    game_pk         INTEGER     NOT NULL,
    at_bat_number   INTEGER     NOT NULL,
    pitch_number    INTEGER     NOT NULL,
    game_date       DATE        NOT NULL,
    season          SMALLINT    NOT NULL,

    -- The stealing CANDIDATE (the lead runner of the open-base pair), the
    -- pitcher holding him, and the catcher whose arm deters him.
    runner_id       INTEGER     NOT NULL,
    pitcher_id      INTEGER     NOT NULL,
    catcher_id      INTEGER,

    -- 2 = runner on 1B with 2B open; 3 = runner on 2B with 3B open. Steals of
    -- home ride on other events and are out of scope (SIM-468 ticket text).
    target_base     SMALLINT    NOT NULL CHECK (target_base IN (2, 3)),

    -- The situation ENTERING the pitch, so a draw can hard-filter its cell.
    inning          SMALLINT    NOT NULL,
    outs            SMALLINT    NOT NULL CHECK (outs BETWEEN 0 AND 2),
    count_balls     SMALLINT    NOT NULL,
    count_strikes   SMALLINT    NOT NULL,
    score_diff      SMALLINT    NOT NULL,  -- batting - fielding, capped ±5

    -- The denominator and the numerators. attempted=FALSE rows are the point
    -- of this table: they let one draw decide whether the runner goes at all.
    attempted       BOOLEAN     NOT NULL,
    success         BOOLEAN     NOT NULL DEFAULT FALSE,

    recency_weight  FLOAT       NOT NULL DEFAULT 1.0,

    PRIMARY KEY (pitch_id)
);

CREATE INDEX IF NOT EXISTS idx_sop_season  ON sim.steal_opportunity_pool(season);
CREATE INDEX IF NOT EXISTS idx_sop_target  ON sim.steal_opportunity_pool(target_base);
CREATE INDEX IF NOT EXISTS idx_sop_runner  ON sim.steal_opportunity_pool(runner_id);
CREATE INDEX IF NOT EXISTS idx_sop_attempt ON sim.steal_opportunity_pool(attempted);

COMMENT ON TABLE sim.steal_opportunity_pool IS
'SIM-468: one row per pitch where a steal was POSSIBLE (runner on 1B with 2B open, or 2B with 3B open), attempted or not. The denominator sim.stolen_base_pool lacks. SIM-474 draws attempt AND outcome from it.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0015', 'SIM-468: sim.steal_opportunity_pool — per-pitch steal opportunities with the attempted flag (the denominator the attempts-only pool lacks)');
