-- 0022 — SIM-517 part B: the catcher + got-away columns on the pitch pool
--                        (schema v21 -> v22)
--
-- WHY
-- ---
-- The catcher receiving weight conditions the PITCH draw (owner design
-- 2026-08-29, revised 2026-09-03), so each pitch-pool row must say WHO was
-- catching (`catcher_id` = raw.pitches.fielder_2 — present on every pitch)
-- and whether THAT pitch got away from him (`got_away`):
--
--   * the parser's per-pitch `passed_ball_wild_pitch` flag (validated
--     2026-09-03 at exactly MLB volume, 0.32-0.35/team-game), OR
--   * an uncaught third strike — a strikeout-final pitch whose PA
--     description names a wild pitch / passed ball (the exact label
--     established in migration 0021's rationale).
--
-- The drawn row then IS the play: a got-away strike-3 puts the batter on
-- first when the rule allows; a got-away pitch with runners on advances
-- them. No separate sampling step, no hand-tuned rate.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL
-- until the pool rebuild fills the window seasons; the artifact exporter
-- treats NULL as 0/false.

ALTER TABLE sim.pitch_pool
    ADD COLUMN IF NOT EXISTS catcher_id INTEGER;
ALTER TABLE sim.pitch_pool
    ADD COLUMN IF NOT EXISTS got_away BOOLEAN;
