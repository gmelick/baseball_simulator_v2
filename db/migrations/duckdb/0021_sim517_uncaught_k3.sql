-- 0021 — SIM-517 part A: the uncaught-strike-3 receiving columns (schema v20 -> v21)
--
-- WHY
-- ---
-- The catcher RECEIVING profile (owner design 2026-08-29, revised
-- 2026-09-03) carries every "ball got away" skill: framing and the
-- passed-ball/wild-pitch blocking model already live in
-- derived.catcher_season_metrics; the missing read is the strike-3 slice —
-- how often a third strike gets away from THIS catcher. The label is exact:
-- a strikeout PA whose final-pitch description names a wild pitch or passed
-- ball (measured 2026-09-03: 78 such PAs in 2025, with ZERO leakage from
-- mid-PA get-away pitches into the strikeout text).
--
-- The event is rare (~0.2% of strike-3s, ~80 PAs league-wide per season), so
-- a raw per-catcher-season rate is Poisson noise. The columns therefore
-- split: the raw counts carry the `sample_` prefix (the engine-artifact
-- exporter EXCLUDES sample_* columns from the similarity embedding), and the
-- one embedding-visible column is the EB-shrunk rate
-- (prior n = 2000 strike-3s toward the season league rate — a catcher needs
-- roughly three full seasons of strike-3s to move halfway from league).
--
-- Non-destructive: ADD COLUMN IF NOT EXISTS only.

ALTER TABLE derived.catcher_season_metrics
    ADD COLUMN IF NOT EXISTS sample_k3_received INTEGER;
ALTER TABLE derived.catcher_season_metrics
    ADD COLUMN IF NOT EXISTS sample_uncaught_k3 INTEGER;
ALTER TABLE derived.catcher_season_metrics
    ADD COLUMN IF NOT EXISTS uncaught_k3_rate_eb DOUBLE;
