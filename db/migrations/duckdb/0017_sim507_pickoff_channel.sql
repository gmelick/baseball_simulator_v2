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
-- of the matching target pair), which preserves per-opportunity-pitch rates
-- exactly. The SIM-474 steal draw returns these labels beside
-- attempted/success, so ONE similarity-weighted draw answers the whole
-- pre-pitch running-game question. Out of scope, measured small: pickoffs of
-- a runner held at 3B (~26 outs/season) and steals of home (~16 CS/season),
-- together ~0.009/team-game.
--
-- NOT DESTRUCTIVE: ADD COLUMN IF NOT EXISTS only.

ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_out BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_advancing BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.steal_opportunity_pool ADD COLUMN IF NOT EXISTS pickoff_error BOOLEAN DEFAULT FALSE;
