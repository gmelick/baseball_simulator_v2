-- DuckDB Migration 0011: SIM-408 engine ↔ DuckDB schema reconciliation
-- Applied: 2026-05-29 (Phase 7, live bring-up)
-- Run via: duckdb baseball_simulator.duckdb < db/migrations/duckdb/0011_sim408_engine_schema_reconciliation.sql
--
-- WHY
-- ---
-- The first live 11-engine build (SIM-408) booted only 7/11 engines: the
-- situation, baserunner_steal, pitcher_steal, catcher, and manager engines query
-- tables/columns the profile computor + 02_duckdb_schema.sql never produced
-- (diagnosis: docs/audit/2026-05-29-sim408-engine-schema-divergence.md; plan:
-- docs/audit/2026-05-29-sim408-reconciliation-plan.md). This migration adds the
-- missing tables/columns so the computor's output matches each engine's SELECT.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS; ALTER TABLE ... ADD COLUMN IF
-- NOT EXISTS. Keep in sync with db/schemas/02_duckdb_schema.sql (the canonical
-- schema the computor applies on a fresh build).

PRAGMA database_list;  -- confirm connection before applying

CREATE SCHEMA IF NOT EXISTS derived;

-- ---------------------------------------------------------------------------
-- 1. derived.at_bat_situations (situation engine — Step 2.9 KDTree)
--    One row per PA with the pre-PA game state. Built by
--    PlayerProfileComputor._build_at_bat_situations from raw.pitches.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived.at_bat_situations (
    play_id                     VARCHAR     NOT NULL,   -- '<game_pk>-<at_bat_number>'
    game_pk                     INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    venue_id                    INTEGER,                -- joins derived.park_factors(venue_id, season)
    at_bat_number               INTEGER     NOT NULL,
    inning                      SMALLINT,
    top_or_bottom               SMALLINT,               -- 0 = top, 1 = bottom
    outs_when_up                SMALLINT,
    on_1b                       SMALLINT,               -- 0/1 base-occupied flag
    on_2b                       SMALLINT,
    on_3b                       SMALLINT,
    home_score                  SMALLINT,
    away_score                  SMALLINT,
    leverage_index              FLOAT,
    pitcher_pitch_count         INTEGER,
    batter_pa_count             INTEGER,
    PRIMARY KEY (game_pk, at_bat_number)
);

CREATE INDEX IF NOT EXISTS idx_abs_season       ON derived.at_bat_situations(season);
CREATE INDEX IF NOT EXISTS idx_abs_venue_season ON derived.at_bat_situations(venue_id, season);

-- ---------------------------------------------------------------------------
-- 2. derived.baserunner_steal_metrics (Step 2.5 baserunner-steal engine)
--    Built by PlayerProfileComputor._build_baserunner_steal_metrics from
--    raw.pitches SB flags. Biomech jump features are intentionally absent
--    (Statcast doesn't publish them; the engine's JUMP sub-score was removed).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived.baserunner_steal_metrics (
    player_id                   INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    sample_steal_attempts       INTEGER     NOT NULL DEFAULT 0,
    sample_first_base_opps      INTEGER     NOT NULL DEFAULT 0,
    steal_attempt_rate          FLOAT,
    steal_attempt_rate_2b       FLOAT,
    steal_success_rate          FLOAT,
    steal_success_rate_2b       FLOAT,
    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (player_id, season)
);

CREATE INDEX IF NOT EXISTS idx_bsm_season ON derived.baserunner_steal_metrics(season);

-- ---------------------------------------------------------------------------
-- 3. derived.pitcher_steal_metrics (Step 2.7 pitcher-steal engine, outcome-only)
--    Built by PlayerProfileComputor._build_pitcher_steal_metrics from
--    raw.pitches (SB-against flags + outs for IP). Delivery + pickoff features
--    are intentionally absent (not in Statcast; the engine's Delivery + Pickoff
--    sub-scores were removed, leaving an outcome-only engine).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived.pitcher_steal_metrics (
    pitcher_id                      INTEGER     NOT NULL,
    season                          SMALLINT    NOT NULL,
    throws                          CHAR(1),
    sample_baserunner_events        INTEGER     NOT NULL DEFAULT 0,
    sample_steal_attempts_against   INTEGER     NOT NULL DEFAULT 0,
    sb_against_per_9                FLOAT,
    cs_rate_forced                  FLOAT,
    steal_attempt_rate_allowed      FLOAT,
    below_minimum_sample            BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                      TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pitcher_id, season)
);

CREATE INDEX IF NOT EXISTS idx_pitcher_steal_season ON derived.pitcher_steal_metrics(season);

-- ---------------------------------------------------------------------------
-- 4. derived.catcher_season_metrics — zone-framing called-strike rates
--    The catcher engine's FRAMING sub-score needs shadow/heart zone strike
--    rates; the framing pass now emits them. (All other engine-expected catcher
--    columns are derived in the engine's load SELECT from existing counts; the
--    Offense sub-score was TRIMmed.) ADD COLUMN IF NOT EXISTS for existing DBs.
-- ---------------------------------------------------------------------------
ALTER TABLE derived.catcher_season_metrics ADD COLUMN IF NOT EXISTS shadow_zone_strike_rate FLOAT;
ALTER TABLE derived.catcher_season_metrics ADD COLUMN IF NOT EXISTS heart_zone_strike_rate FLOAT;

-- ---------------------------------------------------------------------------
-- 5. derived.manager_season_metrics — reconcile to the engine vocabulary
--    The prior columns (sp_pitch_count_mean / ph_rate_* / bullpen_leverage_mapping)
--    are NOT read by the engine; the manager engine needs the
--    usage/aggression/platoon vocabulary. This is a derived table fully rebuilt
--    by the computor, so we DROP + recreate (disjoint column sets). USAGE cols
--    are SIM-427-gated (NULL until the bullpen roster exists).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS derived.manager_season_metrics;
CREATE TABLE IF NOT EXISTS derived.manager_season_metrics (
    manager_id                              INTEGER     NOT NULL,
    season                                  SMALLINT    NOT NULL,
    sample_games                            INTEGER     NOT NULL DEFAULT 0,
    sample_starter_decisions                INTEGER     NOT NULL DEFAULT 0,
    starter_avg_pitch_count                 FLOAT,
    starter_pull_pct_before_100             FLOAT,
    closer_entry_leverage_index             FLOAT,
    high_leverage_reliever_rate             FLOAT,
    opener_usage_rate                       FLOAT,
    bulk_innings_rate                       FLOAT,
    steal_order_rate_per_1b_opp             FLOAT,
    hit_and_run_rate_per_opportunity        FLOAT,
    sac_bunt_rate_high_leverage             FLOAT,
    sac_bunt_rate_low_leverage              FLOAT,
    squeeze_play_rate_per_3b_opp            FLOAT,
    pinch_hit_rate_vs_same_hand             FLOAT,
    pinch_hit_rate_high_leverage            FLOAT,
    defensive_sub_rate_late_innings         FLOAT,
    double_switch_rate_per_reliever_change  FLOAT,
    platoon_advantage_exploitation_rate     FLOAT,
    below_minimum_sample                    BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                              TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (manager_id, season)
);
CREATE INDEX IF NOT EXISTS idx_msm_season ON derived.manager_season_metrics(season);

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0011', 'SIM-408 engine/DuckDB schema reconciliation: derived.at_bat_situations (situation engine) + (appended) baserunner_steal_metrics / pitcher_steal_metrics + catcher/manager engine-vocabulary columns');
