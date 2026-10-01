-- 0017 — SIM-507: the pickoff channel (schema v16 -> v17)
--
-- Three outcome columns on sim.steal_opportunity_pool, labeled from
-- raw.play_events by the profile computor:
--
--   * pickoff_out       — a pickoff throw retired the held runner.
--   * pickoff_advancing — that out was a picked-off CAUGHT STEALING: the
--     runner was tagged at the NEXT base (MLB Rule 9.07(h) scores it as a
--     CS). A plain pickoff (tagged at his own base) is an out, not a CS.
--   * pickoff_error     — an errant pickoff throw; the runner advances.
--
-- 2023 measurement (this database): 339 pickoff outs (133 advancing),
-- 115 errors, 9,574 throws with no outcome. Outcomes are attributed to ONE
-- opportunity pitch of their plate appearance (the first non-attempted pitch
-- of the matching target pair). CORRECTION (SIM-554, measured 2026-09-30):
-- this did NOT preserve the rates exactly. 22% of the outcomes that fit a
-- pair (394 of 1,788 in 2023-2026) had no pitch of that pair in their plate
-- appearance, because the throw came before the first pitch, and the pool
-- dropped them. Migration 0031 adds each one back as a pickoff row of its
-- own (is_pickoff_row). The SIM-474 steal draw returned these labels beside
-- attempted/success, so ONE similarity-weighted draw answered the whole
-- pre-pitch running-game question. Since SIM-554 the pickoff is its own draw
-- before the pitch and the steal its own draw after it, among real pitches of
-- the same class (SIM_STEAL_PITCH_CLASS; off = the one draw). Out of scope,
-- measured small: pickoffs of
-- a runner held at 3B (~26 outs/season) and steals of home (~16 CS/season),
-- together ~0.009/team-game.
--
-- NOT DESTRUCTIVE: ADD COLUMN IF NOT EXISTS only.

ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_out BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_advancing BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_error BOOLEAN DEFAULT FALSE;
