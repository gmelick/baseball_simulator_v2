-- 0024 — SIM-523 part G: the data additions the play-picker redesign asked for
--                  (schema v23 -> v24)
--
-- WHY
-- ---
-- The redesign's fielding draw (plan step 6) reads "the fielders on the chain,
-- each against the live defender at that position", and its batter kernel
-- reads sprint speed. Neither datum reached a profile or a pool:
--
--   * sprint_speed on derived.fielder_season_metrics — the fielder profile's
--     speed (the plan's "one join": raw.sprint_speed already carries Savant's
--     per-season sprint speed; the baserunner profile had the column and the
--     join but the raw table was EMPTY until part G ran the loader).
--   * fielder_2 .. fielder_9 on sim.outcome_pool — the defensive ALIGNMENT on
--     the play (raw.pitches.fielder_2..9, the player at each position), so a
--     factor can compare the live defender at ANY position with the row's.
--   * putout_pos_mask / assist_pos_mask on sim.outcome_pool — the CHAIN: bit
--     k set (1 << k, k = 1..9) when the position credited a putout / an
--     assist on the play (raw.pitches.field_putout_1..3 / field_assist_1..5
--     matched to the alignment; bit 1 = the pitcher). 0 = no such credit
--     (every hit; an uncredited out).
--
-- ⚠ POSITIONAL-INSERT TRAP: the pool INSERT carries no column list. The ten
-- pool columns are appended LAST (after bat_home), and the builder's final
-- SELECT appends them in exactly this order. Keep in sync with
-- db/schemas/02_duckdb_schema.sql.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL
-- until the window seasons are rebuilt; the artifact exporter writes the
-- chain only when the columns exist, and the loader is None without them.

ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS sprint_speed DOUBLE;

ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_2 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_3 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_4 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_5 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_6 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_7 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_8 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS fielder_9 INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS putout_pos_mask SMALLINT;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS assist_pos_mask SMALLINT;
