-- 0016 — SIM-504 item 3: pitcher disengagement rates (schema v15 -> v16)
--
-- Two new hold-runner columns on derived.pitcher_steal_metrics, computed from
-- raw.play_events (the SIM-502 table) by the profile computor:
--
--   * pickoff_rate — pickoff throws (incl. errant ones) per pitch thrown with
--     a runner on 1B or 2B. The denominator of the pickoff decision: how
--     often does this pitcher throw over?
--   * stepoff_rate — stepoffs per the same denominator. The feed records
--     stepoffs only from 2023 (the pitch-clock disengagement rules), so the
--     rate is a coverage zero before then.
--
-- Both auto-enter the pitcher_steal actor embedding (engine_artifacts
-- discovers numeric columns) and are named in the SIM-474 steal draw's
-- _PITCHER_STEAL_FEATURES. The Step 2.7 pitcher-steal SIMILARITY engine's
-- weight vocabulary is NOT extended here — that is the SIM-476 fit's call.
--
-- NOT DESTRUCTIVE: ADD COLUMN IF NOT EXISTS only; a downgrade drops nothing
-- because nothing is dropped here.

ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS pickoff_rate DOUBLE;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS stepoff_rate DOUBLE;
