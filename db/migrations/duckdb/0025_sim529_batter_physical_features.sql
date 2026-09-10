-- 0025 — SIM-529: the physical swing and stance columns on the batter profile
--                 (schema v24 -> v25)
--
-- WHY
-- ---
-- The batter-matching model scores 26 features and only three of them are
-- physical measurements — average exit velocity, maximum exit velocity and
-- average launch angle. Everything else is the rate at which an outcome
-- happened. Baseball Savant now publishes how each batter actually swings and
-- stands, and two measurements taken on our own data say it is worth adding:
--
--   * five of these repeat season to season more reliably than ANY feature the
--     model reads today (swing tilt .903, bat speed .901, stance depth .889,
--     swing length .882 against .825 for whiff rate, our best);
--   * foot separation, stance angle and stance depth overlap everything we
--     already read by only .087 to .123 — they describe a batter our current
--     features cannot tell apart at all.
--
-- The evidence is in docs/audit/2026-09-10-savant-leaderboard-data-audit.md.
-- Columns rejected on that same evidence, and deliberately absent here: whiff
-- per swing (a .983 duplicate of contact rate), the blast and squared-up rates,
-- hard-swing rate (Savant derives it from bat speed) and percent-swings-
-- competitive (year-to-year correlation .003 — noise).
--
-- THREE COLUMNS PER FEATURE
-- -------------------------
-- Owner ruling 2026-09-10: a switch hitter's two sides are separate rows, not
-- one collapsed row. So every feature lands three times — overall, against
-- left-handed pitching, against right-handed pitching. A batter's side is
-- decided by the pitcher's hand, so the pitcher-hand split IS the batting-side
-- split. For a batter who bats one way all three hold the same measurement.
--
-- ⚠ POSITIONAL-INSERT TRAP
-- ------------------------
-- `_compute_batter_profiles` runs INSERT OR REPLACE with NO column list, so
-- DuckDB matches the final SELECT to this table by POSITION. ADD COLUMN appends
-- to the end of the table, so the ORDER BELOW IS LOAD-BEARING. It is generated
-- from `PHYSICAL_COLUMN_ORDER` in pipeline/batch/player_profile_computor.py,
-- the SELECT tail is generated from the same tuple, and
-- tests/unit/test_sim529_batter_physical.py asserts the live table agrees with
-- it. Do not hand-edit one side of that.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL until
-- the batter profiles are rebuilt. Savant publishes none of this before 2023,
-- so seasons before then stay NULL permanently; the model's kernel treats a
-- missing feature as neutral, which is the behaviour we want.

ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS bat_speed                FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS bat_speed_vs_l           FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS bat_speed_vs_r           FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_length             FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_length_vs_l        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_length_vs_r        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_tilt               FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_tilt_vs_l          FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS swing_tilt_vs_r          FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_angle             FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_angle_vs_l        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_angle_vs_r        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_direction         FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_direction_vs_l    FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS attack_direction_vs_r    FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS contact_depth            FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS contact_depth_vs_l       FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS contact_depth_vs_r       FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_foot_sep          FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_foot_sep_vs_l     FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_foot_sep_vs_r     FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_angle             FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_angle_vs_l        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_angle_vs_r        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_depth             FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_depth_vs_l        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_depth_vs_r        FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_off_plate         FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_off_plate_vs_l    FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS stance_off_plate_vs_r    FLOAT;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS physical_swings          INTEGER;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS physical_swings_vs_l     INTEGER;
ALTER TABLE derived.batter_season_metrics ADD COLUMN IF NOT EXISTS physical_swings_vs_r     INTEGER;
