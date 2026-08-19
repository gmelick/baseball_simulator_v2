-- 0018 — SIM-510: transition columns on sim.outcome_pool + the advancement
-- opportunity pool (schema v17 -> v18)
--
-- WHY
-- ---
-- The fielding-transition epic (SIM-510→513) makes the drawn outcome-pool row
-- BE the play: the row must therefore carry the whole base-state transition —
-- who stood where before the pitch, where each body ended, and who was
-- retired. `raw.pitches` already holds this truth (the ETL parser maintains
-- post-play seats per runner movement); this migration copies it onto the
-- pool and adds the derived per-runner DESTINATION encoding the draw applies:
--
--   runner_Xb_dest / batter_dest:  NULL = no runner on that base pre-pitch;
--   4 = scored; 3/2/1 = the post-play base; 0 = retired on the play.
--
-- `dest_outs_consistent` guards the derivation: retired bodies must equal the
-- events-derived `result_outs`. The artifact export excludes inconsistent
-- rows and the SIM-510 validation measures their rate.
--
-- The advancement OPPORTUNITY pool holds one row per discretionary
-- runner-advancement decision (SIM-512), attempted or not — the SIM-468
-- denominator lesson. (scenario, from_base, target_base) name the decision:
--   1 = 1st->3rd on a single      (1, 3)
--   2 = 2nd->home on a single     (2, 4)
--   3 = 1st->home on a double     (1, 4)
--   4 = tag up on a caught ball   (3, 4) / (2, 3) / (1, 2)
--   5 = the batter stretches      (0, 2) on a single / (0, 3) on a double
--
-- ⚠ POSITIONAL-INSERT TRAP: the pool INSERTs carry no column list. The new
-- outcome_pool columns are appended LAST (after fielder_player_id), and the
-- builder SELECT appends them in exactly this order. Keep in sync with
-- db/schemas/02_duckdb_schema.sql.
--
-- NOT DESTRUCTIVE: ADD COLUMN IF NOT EXISTS + CREATE TABLE IF NOT EXISTS only.

-- Pre-pitch runner identities (raw.pitches.on_*)
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS on_1b INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS on_2b INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS on_3b INTEGER;

-- Post-play runner identities (raw.pitches.post_on_*)
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS post_on_1b INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS post_on_2b INTEGER;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS post_on_3b INTEGER;

-- Per-runner scored flags (official scoring — run timing included)
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_1b_scored BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_2b_scored BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_3b_scored BOOLEAN DEFAULT FALSE;

-- Per-runner thrown-out-advancing flags (a tag out taking an extra base,
-- never a force or a doubled-off runner)
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_1b_out_advancing BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_2b_out_advancing BOOLEAN DEFAULT FALSE;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_3b_out_advancing BOOLEAN DEFAULT FALSE;

-- Derived destinations (the encoding the fielding draw applies)
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_1b_dest SMALLINT;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_2b_dest SMALLINT;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS runner_3b_dest SMALLINT;
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS batter_dest SMALLINT;

-- The derivation guard: retired bodies == result_outs
ALTER TABLE sim.outcome_pool ADD COLUMN IF NOT EXISTS dest_outs_consistent BOOLEAN;

CREATE TABLE IF NOT EXISTS sim.advancement_opportunity_pool (
    pitch_id        BIGINT      NOT NULL,
    game_pk         INTEGER     NOT NULL,
    at_bat_number   INTEGER     NOT NULL,
    pitch_number    INTEGER     NOT NULL,
    game_date       DATE        NOT NULL,
    season          SMALLINT    NOT NULL,

    -- The decision this row is a sample of (see the header).
    scenario        SMALLINT    NOT NULL CHECK (scenario BETWEEN 1 AND 5),
    from_base       SMALLINT    NOT NULL CHECK (from_base BETWEEN 0 AND 3),
    target_base     SMALLINT    NOT NULL CHECK (target_base BETWEEN 2 AND 4),

    -- The deciding runner (the batter for scenario 5) and the defender whose
    -- arm the throw comes from.
    runner_id       INTEGER     NOT NULL,
    fielder_id      INTEGER,
    fielder_pos     SMALLINT,

    -- The situation and the batted-ball geometry (the throw context).
    outs            SMALLINT    NOT NULL CHECK (outs BETWEEN 0 AND 2),
    exit_velo       FLOAT,
    launch_angle    FLOAT,
    spray_angle     FLOAT,
    hit_distance    FLOAT,

    -- The denominator and the outcomes. attempted=FALSE rows are the point.
    attempted       BOOLEAN     NOT NULL,
    safe            BOOLEAN     NOT NULL DEFAULT FALSE,
    error_extra     BOOLEAN     NOT NULL DEFAULT FALSE,

    recency_weight  FLOAT       NOT NULL DEFAULT 1.0,

    PRIMARY KEY (pitch_id, scenario, from_base)
);

CREATE INDEX IF NOT EXISTS idx_aop_season   ON sim.advancement_opportunity_pool(season);
CREATE INDEX IF NOT EXISTS idx_aop_decision ON sim.advancement_opportunity_pool(scenario, from_base, target_base);

COMMENT ON TABLE sim.advancement_opportunity_pool IS
'SIM-510: one row per discretionary runner-advancement decision (five scenarios), attempted or not. The SIM-512 advancement draw picks one row and reads attempted/safe/error_extra.';

INSERT OR IGNORE INTO migration_history (migration_id, description)
VALUES ('0018', 'SIM-510: transition columns (pre/post runner identities, scored/out-advancing flags, derived destinations) on sim.outcome_pool + sim.advancement_opportunity_pool (the five-scenario advancement decisions with the non-attempt denominator)');
