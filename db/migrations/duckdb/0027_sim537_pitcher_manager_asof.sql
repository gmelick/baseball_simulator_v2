-- 0027 — SIM-537: the cutoff stamp on pitcher and manager profiles (schema v26 -> v27)
--
-- WHY
-- ---
-- SIM-534 (migration 0026) added the cutoff stamp to the batter profile so a
-- backtest of a past game cannot draw on a batter's data recorded after that
-- game. This extends the same stamp to derived.pitcher_season_metrics and
-- derived.manager_season_metrics, the two groupings whose data is entirely
-- our own (raw.pitches / raw.play_events / raw.game_bullpen_availability) and
-- so needs no Savant-style prior-season fallback — see
-- docs/audit/2026-09-10-savant-point-in-time-data.md §6.
--
-- Both INSERTs carry an explicit column list, unlike the batter one, so there
-- is no positional-order trap here: asof_date is named directly in both.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL,
-- which reads as "built before this was tracked" and is refused by each
-- engine's mixed-cutoff guard until the profiles are rebuilt.

ALTER TABLE derived.pitcher_season_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
ALTER TABLE derived.manager_season_metrics ADD COLUMN IF NOT EXISTS asof_date DATE;
