-- 0028 — SIM-537: the cutoff stamp on the last five profile tables (schema v27 -> v28)
--
-- WHY
-- ---
-- Migrations 0026 and 0027 stamped the batter, pitcher, and manager profiles
-- with the date each row's data runs through, so a backtest of a past game
-- cannot draw on data recorded after that game. This migration extends the
-- same stamp to the five remaining profile tables: baserunner, baserunner
-- steal, pitcher steal, catcher, and fielder.
--
-- These five mix our own data (raw.pitches, raw.play_events — a plain
-- game_date <= cutoff filter) with Savant leaderboard tables that carry NO
-- date column of their own (raw.sprint_speed, raw.savant_poptime,
-- raw.savant_catcher_throwing, raw.savant_arm_strength,
-- raw.savant_baserunning — only a season number). For those, the builder
-- substitutes the PRIOR season's row when the season being built IS the one
-- containing the cutoff, rather than the still-accumulating current-season
-- row that could leak. See docs/audit/2026-09-10-savant-point-in-time-data.md
-- §5 for the measured year-to-year correlations behind that choice (sprint
-- speed 0.910, catcher arm strength 0.894, fielder arm strength 0.857, pop
-- time 0.726, extra-base-attempt rate against a fielder 0.546) and the one
-- exception: fielder arm run value (of_arm_runs) correlates at only 0.254,
-- so it is left NULL for the cutoff season instead of substituted.
--
-- derived.baserunner_season_metrics and derived.fielder_season_metrics both
-- carry a positional INSERT (no column list) — asof_date must stay the LAST
-- column in each, appended after their existing trailing column
-- (updated_at, and sprint_speed respectively). The other three tables carry
-- an explicit column list, so there is no positional-order trap for them.
--
-- derived.run_expectancy_matrix gets NO new column: it is keyed by
-- season_range (a string spanning multiple seasons), not by an individual
-- season, so a cutoff build and the live production build of the same
-- seasons would share one key and overwrite each other. Rather than widen
-- that key, a cutoff build skips persisting to that table entirely and
-- keeps its run-expectancy matrix in memory for the duration of that run —
-- see build_run_expectancy_matrix in pipeline/batch/player_profile_computor.py.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL,
-- which reads as "built before this was tracked" and is refused by each
-- engine's mixed-cutoff guard until the profiles are rebuilt.

ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
ALTER TABLE derived.catcher_season_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
