-- 0019 — SIM-491 (the SIM-412 rebuild): the batting side on sim.outcome_pool
-- (schema v18 -> v19)
--
-- WHY
-- ---
-- The SIM-412 home-field advantage worked by FLIPPING the drawn event
-- post-draw. On the SIM-511 transition architecture a flip contradicts the
-- drawn row's own movement, so the flip is INERT on the production path and
-- home_win_pct reads 0.4735 (centre 0.5428). The rebuild moves the effect
-- INTO the fielding draw's weights: up-weight pool rows whose batting side
-- matches the live batting side, so home advantage emerges from real rows at
-- the pool's real rates.
--
-- This migration adds the one fact the pool does not carry: which side was
-- batting. `raw.pitches.inning_topbot` names it exactly — 'Bot' = the home
-- team bats.
--
-- ⚠ POSITIONAL-INSERT TRAP: the pool INSERT carries no column list. The new
-- column is appended LAST (after dest_outs_consistent), and the builder
-- SELECT appends it in exactly this order. Keep in sync with
-- db/schemas/02_duckdb_schema.sql.
--
-- NOT DESTRUCTIVE: ADD COLUMN IF NOT EXISTS only.

ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS bat_home BOOLEAN;

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0019', 'SIM-491: bat_home (the batting side, from raw.pitches.inning_topbot) on sim.outcome_pool — the SIM-412 home-field rebuild as a fielding-draw weight');
