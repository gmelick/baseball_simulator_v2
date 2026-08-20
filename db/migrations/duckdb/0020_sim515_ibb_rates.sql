-- 0020 — SIM-515: the measured IBB rate table (schema v19 -> v20)
--
-- WHY
-- ---
-- The sim issued 0.3233 intentional walks per team-game vs MLB's 0.1224
-- (2.64x) — `_should_issue_ibb` was a hand-tuned formula (tendency x leverage
-- vs an rng roll) re-rolled PER PITCH, so it compounded toward certainty in
-- every qualifying spot. The architecture rule (2026-08-10) says every
-- decision is a draw from real data. This table holds that data: for each
-- hard-filtered cell (the full pre-PA base-out state x outs x late x close),
-- how many real plate appearances entered the cell and how many were
-- intentionally walked. The loop draws ONCE per PA at the cell's real rate.
--
-- The numerator comes from raw.play_events `intent_walk` rows (which carry
-- the pre-play cell directly); the denominator adds the pitched PAs from
-- raw.pitches (a no-pitch IBB has no pitch rows, so the union is the true
-- PA count). Both sides are built over the SAME season window as the pool
-- (the owner's 2026-08-20 window ruling: the last three completed seasons
-- plus the current one).
--
-- NOT DESTRUCTIVE: CREATE TABLE IF NOT EXISTS only.

CREATE TABLE IF NOT EXISTS sim.ibb_rates (
    -- The pre-PA cell. runners_state is the standard bitmask
    -- (bit0=1B, bit1=2B, bit2=3B); is_late = inning >= 7;
    -- is_close = |bat_score - fld_score| <= 1.
    runners_state   SMALLINT    NOT NULL CHECK (runners_state BETWEEN 0 AND 7),
    outs            SMALLINT    NOT NULL CHECK (outs BETWEEN 0 AND 2),
    is_late         BOOLEAN     NOT NULL,
    is_close        BOOLEAN     NOT NULL,

    -- The denominator (every PA that entered the cell) and the numerator.
    opportunities   BIGINT      NOT NULL,
    issued          BIGINT      NOT NULL,

    PRIMARY KEY (runners_state, outs, is_late, is_close)
);

COMMENT ON TABLE sim.ibb_rates IS
'SIM-515: real IBB rates per hard-filtered cell. The loop draws once per PA at issued/opportunities — never a hand-tuned formula.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0020', 'SIM-515: sim.ibb_rates — the measured intentional-walk rate per (runners_state, outs, late, close) cell; replaces the hand-tuned per-pitch IBB formula');
