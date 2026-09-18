-- 0030 — SIM-532: Savant's outs above average and the outfield jump on the fielder profile (schema v29 -> v30)
--
-- WHY
-- ---
-- The fielder model's range group reads five components we compute from an
-- approximation of hang time and distance. Within one position each of them
-- repeats year to year at 0.04 to 0.47. Baseball Savant publishes two things
-- we do not compute. Its official outs above average, pulled once per
-- position, repeats within a position at 0.34 to 0.63 and agrees with ours at
-- only r 0.31: the same skill, measured from full tracking. Its outfield jump
-- splits an outfielder's first three seconds after contact into a reaction, a
-- burst and a route, in feet against the league; the reaction repeats at 0.80
-- to 0.92, the most stable defensive figures the platform holds. The batter
-- hand split the ticket asked about does not repeat (0.04 to 0.14 for
-- outfielders; shortstop alone within the infield), so it stays in the raw
-- table and gets no profile column.
-- Plan: docs/audit/2026-09-17-sim532-fielder-hand-split-and-jump-plan.md §4.2.
--
-- WHAT
-- ----
-- Six columns on derived.fielder_season_metrics.
--
--   derived.fielder_season_metrics (POSITIONAL INSERT — no column list)
--     savant_oaa          — Savant's outs above average AT this position (an integer)
--     savant_oaa_per_100  — savant_oaa * 100 / our opportunities at the position
--     jump_reaction_ft    — feet against the league in the first 1.5 s; outfield rows only
--     jump_burst_ft       — the next 1.5 s; outfield rows only
--     jump_route_ft       — the direction taken; outfield rows only
--     jump_plays          — the plays Savant scored: the jump features' confidence basis
--
-- The six go LAST, after asof_date, in the builder's OAA_JUMP_COLUMN_ORDER.
-- The aggregator's SELECT appends them in the same order, and a unit test
-- holds the live table's tail to FIELDER_TAIL_COLUMNS so the SELECT and the
-- table can never desync.
--
-- A NULL means "no measurement" and every reader treats it as such: an
-- infielder has no jump; an outfielder without a Savant row reads the league
-- mean, never 0. The engine loads NULL as NaN and shrinks it to the league
-- mean.
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only. Existing rows hold NULL
-- until scripts/sim532_fielder_recompute.py rebuilds the fielder table. Apply
-- 0028 FIRST on a database that skipped it: the insert is positional, so
-- asof_date must sit before these six.

-- fielder (positional INSERT: these six stay LAST, in this order)
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS savant_oaa         INTEGER;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS savant_oaa_per_100 FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_reaction_ft   FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_burst_ft      FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_route_ft      FLOAT;
ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS jump_plays         INTEGER;
