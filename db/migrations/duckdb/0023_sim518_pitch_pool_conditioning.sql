-- 0023 — SIM-518: the draw-conditioning columns on the pitch pool
--                  (schema v22 -> v23)
--
-- WHY
-- ---
-- The pitch draw cannot see three facts the batted-ball draw and the manager
-- model already read: which side was batting, how tired the pitcher was, and
-- how many times he had been through the order. SIM-518 (owner consolidation
-- 2026-08-29) lands them as pool columns first, then as env-gated draw
-- WEIGHTS (byte-identical off), then fits them the SIM-476 way:
--
--   * bat_home            — SIM-464's pitch-pool half. 'Bot' = the home team
--                           bats (raw.pitches.inning_topbot, the 0019 rule).
--                           Consumers: the SIM_PITCH_HOME_OFF_WEIGHT weight now,
--                           the SIM-467 cell index's batting-side dimension next.
--   * pitcher_pitch_count — SIM-465. Pitches this pitcher threw in this game
--                           BEFORE this plate appearance began. "Before the
--                           PA" (not before the pitch) because the live loop
--                           snapshots fatigue once per PA — the count inside
--                           the PA is the draw's own count bucket.
--   * times_through_order — SIM-465. 1 + (batters faced before this PA) // 9,
--                           the live helper simulation.sim_loop
--                           .times_through_order, one definition on both sides.
--
-- ⚠ POSITIONAL-INSERT TRAP: the pool INSERT carries no column list. The three
-- columns are appended LAST (after got_away), and the builder SELECT appends
-- them in exactly this order. Keep in sync with db/schemas/02_duckdb_schema.sql.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL
-- until the pool rebuild fills the window seasons; the artifact exporter
-- writes NULL as unknown (-1 / -1 / 0) and every consumer is neutral on it.

ALTER TABLE sim.pitch_pool ADD COLUMN IF NOT EXISTS bat_home            BOOLEAN;
ALTER TABLE sim.pitch_pool ADD COLUMN IF NOT EXISTS pitcher_pitch_count SMALLINT;
ALTER TABLE sim.pitch_pool ADD COLUMN IF NOT EXISTS times_through_order SMALLINT;

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0023', 'SIM-518: bat_home + pitcher_pitch_count + times_through_order on sim.pitch_pool — the draw-conditioning columns (SIM-464 pitch half, SIM-465)');
