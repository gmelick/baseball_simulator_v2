-- 0032 — SIM-561: who, when and the score on each replayed pitch (schema v31 -> v32)
--
-- WHY
-- ---
-- The game page's play-by-play shows one simulated game, pitch by pitch. A
-- stored pitch carried its outcome and the plate appearance's event, but not
-- the inning, the batter, the pitcher or the score. The page could only print
-- "PA 12: single", so the owner could not review a simulated game.
--
-- WHAT
-- ----
-- Seven nullable columns on sim.play_stream. The play recorder captures the
-- first four from the game state BEFORE the pitch; the pitcher comes from the
-- play itself (the arm that threw, after any pitching change); the score is the
-- one AFTER the pitch.
--
--   inning       INTEGER  — the inning the pitch was thrown in (1-based).
--   half         VARCHAR  — 'top' or 'bottom'.
--   outs_before  INTEGER  — the outs before the pitch (0-2).
--   batter_id    INTEGER  — the batter at the plate.
--   pitcher_id   INTEGER  — the pitcher who threw it.
--   away_score   INTEGER  — the away team's runs after the pitch.
--   home_score   INTEGER  — the home team's runs after the pitch.
--
-- A row an older writer stored keeps NULL in all seven; the page then shows the
-- row without them.
--
-- And one nullable column on sim.game_cards:
--
--   base_seed    BIGINT   — the seed the stored game was played at, so the page
--                           can say which game it shows and the game can be
--                           replayed.
--
-- Since SIM-561 the replay file numbers its own runs (max + 1), and only a
-- /simulate run writes a Postgres sim.sim_runs row.
--
-- WHERE IT APPLIES
-- ----------------
-- The replay tables (0008 play stream, 0009 state snapshots, 0010 game cards)
-- live in their own file since SIM-561, /data/replay.duckdb, which only the app
-- writes; db/sim_store.py ensure_replay_schema applies 0008, 0009, 0010 and this
-- file to it at start. The analytics file holds no sim.play_stream, so the
-- ALTER is guarded with IF EXISTS and is a no-op there.
-- Plan: the SIM-561 entry in CHANGES.md.

PRAGMA database_list;  -- confirm connection before applying

ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS inning INTEGER;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS half VARCHAR;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS outs_before INTEGER;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS batter_id INTEGER;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS pitcher_id INTEGER;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS away_score INTEGER;
ALTER TABLE IF EXISTS sim.play_stream ADD COLUMN IF NOT EXISTS home_score INTEGER;
ALTER TABLE IF EXISTS sim.game_cards ADD COLUMN IF NOT EXISTS base_seed BIGINT;

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0032', 'the inning, half, outs before, batter, pitcher and score after on each sim.play_stream pitch, and the seed on each sim.game_cards run, for the game page (SIM-561)');
