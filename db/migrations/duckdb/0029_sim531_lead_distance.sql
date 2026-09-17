-- 0029 — SIM-531: lead distance in the steal and baserunning profiles (schema v28 -> v29)
--
-- WHY
-- ---
-- The steal decision compares the live runner, pitcher and catcher to the ones
-- in past plays, and until now those comparisons used results only: how often
-- the runner went, how often he was safe. Baseball Savant publishes two
-- physical measurements the decision has never seen — the runner's lead off
-- the bag before the pitch and the extra distance he gets on the pitcher's
-- delivery (the jump) — and the mirror image for the pitcher (the lead and the
-- jump he allows). On the qualified rows the runner's jump repeats year to
-- year at 0.80–0.85 and the pitcher's jump allowed at 0.89–0.90; the steal
-- success rate the runner model weights at 38% today repeats at 0.13–0.26.
-- For the extra-base decision Savant also publishes how often a runner tried
-- for the extra base against how often a typical runner would have tried in
-- the same chances — the expectation the platform never had (it repeats at
-- 0.75–0.77 against 0.50–0.59 for the raw rate).
-- Plan: docs/audit/2026-09-16-sim531-lead-distance-build-plan.md §4.2.
--
-- WHAT
-- ----
-- Thirteen columns across the three runner-side profile tables.
--
--   derived.baserunner_steal_metrics (explicit-column INSERT: order free)
--     lead_primary_ft, lead_secondary_ft, lead_jump_ft  — feet; NULL = no Savant row
--     savant_steal_opps                                  — n_init, the count the lead was measured over
--     sample_second_base_opps                            — plate appearances begun on second (the
--                                                          steal driver now covers every runner with a
--                                                          chance; the model's confidence basis is his
--                                                          total chances, so a runner whose chances were
--                                                          all on second never reads confidence 0)
--   derived.pitcher_steal_metrics (explicit-column INSERT)
--     lead_allowed_primary_ft, lead_allowed_secondary_ft, lead_allowed_jump_ft, savant_hold_opps
--   derived.baserunner_season_metrics (POSITIONAL INSERT — no column list)
--     xb_opportunities, xb_attempt_rate, xb_expected_attempt_rate,
--     xb_attempt_rate_above_expected — appended LAST, after asof_date, in the
--     builder's XB_COLUMN_ORDER; a unit test holds the live table's tail to
--     BASERUNNER_TAIL_COLUMNS so the SELECT and the table can never desync.
--
-- A NULL means "no measurement" and every reader treats it as such (the
-- engines load NULL as NaN and score a pair over the groups both sides have;
-- a lead of 0.0 ft would sit twelve standard deviations below the league).
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL
-- until scripts/sim531_runner_recompute.py rebuilds the three tables. Apply
-- 0028 FIRST on a database that skipped it (the live one had, 2026-09-16):
-- derived.baserunner_season_metrics is positional, so asof_date must sit
-- before these four. The recompute script applies both, in order.

-- steal runner
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_primary_ft   FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_secondary_ft FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS lead_jump_ft      FLOAT;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS savant_steal_opps INTEGER;
ALTER TABLE derived.baserunner_steal_metrics ADD COLUMN IF NOT EXISTS sample_second_base_opps INTEGER;

-- pitcher hold
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_primary_ft   FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_secondary_ft FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS lead_allowed_jump_ft      FLOAT;
ALTER TABLE derived.pitcher_steal_metrics ADD COLUMN IF NOT EXISTS savant_hold_opps          INTEGER;

-- advancement runner (positional INSERT: these four stay LAST, in this order)
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_opportunities               INTEGER;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_attempt_rate                FLOAT;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_expected_attempt_rate       FLOAT;
ALTER TABLE derived.baserunner_season_metrics ADD COLUMN IF NOT EXISTS xb_attempt_rate_above_expected FLOAT;
