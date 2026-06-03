-- =============================================================================
-- BASEBALL SIMULATION SYSTEM — DuckDB Schema
-- Engine: DuckDB 0.10+
-- Schemas: derived (computed metrics), sim (simulation pools)
--
-- Tables:
--   derived.pitcher_season_metrics       derived.pitcher_gmm_components
--   derived.batter_season_metrics        derived.fielder_season_metrics
--   derived.baserunner_season_metrics    derived.catcher_season_metrics
--   derived.manager_season_metrics       derived.park_factors
--   sim.pitch_pool                       sim.outcome_pool
--   sim.stolen_base_pool
--
-- These tables are READ-ONLY from the simulation engine.
-- All writes come from the nightly ETL job sourced from PostgreSQL.
-- DuckDB does not enforce FKs — referential integrity guaranteed by ETL.
--
-- GMM DESIGN NOTE:
--   Pitcher pitch arsenals are modeled as Gaussian Mixture Models rather than
--   per-pitch-type columns. Each pitcher is fit with K components (2–7,
--   selected by BIC) in an 8-dimensional feature space:
--     [velo, ivb, hb, spin_rate, spin_axis, release_x, release_z, release_ext]
--   This avoids baking in Statcast pitch-type labels and handles unusual
--   arsenals (e.g. pitchers whose slider/cutter are indistinguishable,
--   multi-speed fastballs, etc.) without any classification bias.
--
--   Storage is split across two tables:
--     derived.pitcher_season_metrics — one row per pitcher/season.
--       gmm_model JSON column holds the complete serialized model including
--       all component weights, means, and full covariance matrices.
--       Used when loading a pitcher profile into the similarity engine.
--     derived.pitcher_gmm_components — one row per GMM component per pitcher/season.
--       Queryable without JSON deserialization. Used for fast similarity
--       scoring between pitchers at query time.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS derived;
CREATE SCHEMA IF NOT EXISTS sim;

-- =============================================================================
-- DERIVED.PITCHER_SEASON_METRICS
-- One row per pitcher per season.
-- Pitch arsenal modeled as a GMM over 8 features — no hardcoded pitch types.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.pitcher_season_metrics (
    pitcher_id                  INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    p_throws                    VARCHAR(1)  NOT NULL,       -- L or R

    -- -------------------------------------------------------------------------
    -- Sample size
    -- -------------------------------------------------------------------------
    sample_pitches              INTEGER     NOT NULL DEFAULT 0,

    -- -------------------------------------------------------------------------
    -- GMM Model — full serialized model for loading into the similarity engine
    --
    -- JSON structure:
    -- {
    --   "n_components": 3,             -- K chosen by BIC (range 2–7)
    --   "feature_names": [             -- Fixed order; must match component means
    --     "velo", "ivb", "hb",
    --     "spin_rate", "spin_axis",
    --     "release_x", "release_z", "release_ext"
    --   ],
    --   "feature_means": [...],        -- Global mean vector used for standardization
    --   "feature_stds":  [...],        -- Global std vector used for standardization
    --   "components": [
    --     {
    --       "component_id": 0,
    --       "weight": 0.45,            -- Mixing proportion (all weights sum to 1.0)
    --       "mean": [94.2, 12.1, ...], -- In original (unstandardized) feature units
    --       "covariance": [            -- Full 8x8 covariance matrix (row-major)
    --         [1.2, 0.3, ...],
    --         [0.3, 0.8, ...],
    --         ...
    --       ],
    --       "n_pitches": 1240          -- Pitches assigned to this component (soft count)
    --     },
    --     ...
    --   ],
    --   "fit_diagnostics": {
    --     "bic": 12500.2,              -- BIC score used to select K
    --     "aic": 12400.1,
    --     "log_likelihood": -12345.6,
    --     "n_iter": 45,                -- EM iterations to convergence
    --     "converged": true,
    --     "fit_date": "2025-03-09"
    --   }
    -- }
    -- -------------------------------------------------------------------------
    gmm_model                   JSON        NOT NULL,

    -- -------------------------------------------------------------------------
    -- Command metrics (overall — not per pitch type)
    -- -------------------------------------------------------------------------
    bb_rate                     FLOAT,
    k_rate                      FLOAT,
    csw_rate                    FLOAT,      -- Called Strike + Whiff Rate
    zone_take_rate              FLOAT,
    chase_rate                  FLOAT,      -- O-swing rate
    zone_rate                   FLOAT,
    whiff_rate                  FLOAT,

    -- -------------------------------------------------------------------------
    -- Results
    -- -------------------------------------------------------------------------
    era                         FLOAT,
    fip                         FLOAT,
    xfip                        FLOAT,
    ground_ball_rate            FLOAT,
    fly_ball_rate               FLOAT,
    line_drive_rate             FLOAT,
    hr_per_9                    FLOAT,
    whip                        FLOAT,

    -- -------------------------------------------------------------------------
    -- Confidence flag
    -- -------------------------------------------------------------------------
    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
                                -- TRUE when sample_pitches < 200.
                                -- Similarity engine falls back to league-average GMM.

    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (pitcher_id, season)
);

CREATE INDEX IF NOT EXISTS idx_psm_season         ON derived.pitcher_season_metrics(season);
CREATE INDEX IF NOT EXISTS idx_psm_throws         ON derived.pitcher_season_metrics(p_throws, season);
CREATE INDEX IF NOT EXISTS idx_psm_sample_flag    ON derived.pitcher_season_metrics(below_minimum_sample);

COMMENT ON TABLE  derived.pitcher_season_metrics IS 'Per-pitcher per-season metrics. Pitch arsenal stored as GMM — no hardcoded pitch types.';
COMMENT ON COLUMN derived.pitcher_season_metrics.gmm_model IS 'Full serialized GMM. K components (2-7, BIC-selected) over 8 features: velo, ivb, hb, spin_rate, spin_axis, release_x, release_z, release_ext. Load directly into sklearn GaussianMixture or custom implementation for similarity scoring.';
COMMENT ON COLUMN derived.pitcher_season_metrics.below_minimum_sample IS 'TRUE when pitcher has < 200 pitches. Similarity engine substitutes league-average GMM profile.';

-- =============================================================================
-- DERIVED.PITCHER_GMM_COMPONENTS
-- One row per GMM component per pitcher per season.
-- Companion to pitcher_season_metrics.gmm_model — enables SQL-level similarity
-- queries without deserializing the full JSON model on every lookup.
--
-- Primary use: building similarity score numerators between two pitchers by
-- comparing component centroids (means) and weights without loading Python.
-- The full covariance matrices remain in gmm_model for Python-side scoring.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.pitcher_gmm_components (
    pitcher_id                  INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    component_id                INTEGER     NOT NULL,   -- 0-indexed within pitcher/season

    -- Mixing weight (proportion of pitches assigned to this component)
    weight                      FLOAT       NOT NULL CHECK (weight BETWEEN 0 AND 1),

    -- Component centroid in original feature units (not standardized)
    -- Feature space: [velo, ivb, hb, spin_rate, spin_axis, release_x, release_z, release_ext]
    mean_velo                   FLOAT       NOT NULL,   -- Release speed (mph)
    mean_ivb                    FLOAT       NOT NULL,   -- Induced vertical break (inches)
    mean_hb                     FLOAT       NOT NULL,   -- Horizontal break (inches)
    mean_spin_rate              FLOAT,                  -- RPM; NULL when unreliable
    mean_spin_axis              FLOAT,                  -- Degrees 0-360; NULL when unreliable
    mean_release_x              FLOAT       NOT NULL,   -- Horizontal release point (ft)
    mean_release_z              FLOAT       NOT NULL,   -- Vertical release point (ft)
    mean_release_ext            FLOAT       NOT NULL,   -- Extension toward plate (ft)

    -- Component spread (diagonal of covariance — variance per feature)
    -- Full covariance matrix lives in pitcher_season_metrics.gmm_model
    var_velo                    FLOAT,
    var_ivb                     FLOAT,
    var_hb                      FLOAT,
    var_spin_rate               FLOAT,
    var_spin_axis               FLOAT,
    var_release_x               FLOAT,
    var_release_z               FLOAT,
    var_release_ext             FLOAT,

    -- Approximate pitch count assigned to this component (soft assignment)
    n_pitches_assigned          INTEGER,

    -- Human-readable label assigned post-hoc by the ETL for display purposes only.
    -- NOT used in any similarity calculation.
    -- Examples: "Hard Fastball", "Sweeping Slider", "12-6 Curveball"
    -- NULL when automated labeling confidence is low.
    display_label               VARCHAR(50),
    display_label_confidence    FLOAT,                  -- 0.0–1.0; NULL when unlabeled

    PRIMARY KEY (pitcher_id, season, component_id),
    FOREIGN KEY (pitcher_id, season) REFERENCES derived.pitcher_season_metrics(pitcher_id, season)
);

CREATE INDEX IF NOT EXISTS idx_pgc_season             ON derived.pitcher_gmm_components(season);
CREATE INDEX IF NOT EXISTS idx_pgc_pitcher_season     ON derived.pitcher_gmm_components(pitcher_id, season);
CREATE INDEX IF NOT EXISTS idx_pgc_velo               ON derived.pitcher_gmm_components(mean_velo);
CREATE INDEX IF NOT EXISTS idx_pgc_ivb                ON derived.pitcher_gmm_components(mean_ivb);

COMMENT ON TABLE  derived.pitcher_gmm_components IS 'One row per GMM component per pitcher/season. Enables SQL-level pitcher similarity without JSON deserialization. Full covariance matrices in pitcher_season_metrics.gmm_model.';
COMMENT ON COLUMN derived.pitcher_gmm_components.display_label IS 'Human-readable label for UI display ONLY. Never used in similarity calculations.';
COMMENT ON COLUMN derived.pitcher_gmm_components.mean_ivb IS 'Induced vertical break — gravitational component removed. Puts all pitch types on equal footing for clustering.';

-- =============================================================================
-- DERIVED.BATTER_SEASON_METRICS
-- One row per batter per season. Most-weighted component in Step 3b outcome
-- similarity.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.batter_season_metrics (
    batter_id                   INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    sample_pitches              INTEGER     NOT NULL DEFAULT 0,
    sample_pa                   INTEGER     NOT NULL DEFAULT 0,
    bats                        VARCHAR(1)  NOT NULL,           -- L, R, or S

    -- Plate discipline (overall)
    first_pitch_take_rate       FLOAT,
    o_swing_rate                FLOAT,
    z_swing_rate                FLOAT,
    whiff_rate                  FLOAT,
    contact_rate                FLOAT,
    walk_rate                   FLOAT,
    k_rate                      FLOAT,

    -- Batted ball profile
    gb_rate                     FLOAT,
    fb_rate                     FLOAT,
    pull_rate                   FLOAT,
    oppo_rate                   FLOAT,
    avg_exit_velo               FLOAT,
    max_exit_velo               FLOAT,
    avg_launch_angle            FLOAT,
    hard_hit_rate               FLOAT,
    barrel_rate                 FLOAT,
    hr_rate                     FLOAT,

    -- vs Left-Handed Pitchers
    o_swing_rate_vs_l           FLOAT,
    z_swing_rate_vs_l           FLOAT,
    whiff_rate_vs_l             FLOAT,
    contact_rate_vs_l           FLOAT,
    walk_rate_vs_l              FLOAT,
    k_rate_vs_l                 FLOAT,
    gb_rate_vs_l                FLOAT,
    fb_rate_vs_l                FLOAT,
    pull_rate_vs_l              FLOAT,
    oppo_rate_vs_l              FLOAT,
    avg_exit_velo_vs_l          FLOAT,
    barrel_rate_vs_l            FLOAT,
    sample_pa_vs_l              INTEGER,

    -- vs Right-Handed Pitchers
    o_swing_rate_vs_r           FLOAT,
    z_swing_rate_vs_r           FLOAT,
    whiff_rate_vs_r             FLOAT,
    contact_rate_vs_r           FLOAT,
    walk_rate_vs_r              FLOAT,
    k_rate_vs_r                 FLOAT,
    gb_rate_vs_r                FLOAT,
    fb_rate_vs_r               FLOAT,
    pull_rate_vs_r              FLOAT,
    oppo_rate_vs_r              FLOAT,
    avg_exit_velo_vs_r          FLOAT,
    barrel_rate_vs_r            FLOAT,
    sample_pa_vs_r              INTEGER,

    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (batter_id, season)
);

CREATE INDEX IF NOT EXISTS idx_bsm_season         ON derived.batter_season_metrics(season);
CREATE INDEX IF NOT EXISTS idx_bsm_bats           ON derived.batter_season_metrics(bats, season);
CREATE INDEX IF NOT EXISTS idx_bsm_sample_flag    ON derived.batter_season_metrics(below_minimum_sample);

COMMENT ON TABLE  derived.batter_season_metrics IS 'Per-batter per-season metrics. Most-weighted input to Step 3b outcome similarity.';
COMMENT ON COLUMN derived.batter_season_metrics.below_minimum_sample IS 'TRUE when sample_pa < 100. Similarity engine falls back to league-average batter profile.';

-- =============================================================================
-- DERIVED.FIELDER_SEASON_METRICS
-- One row per player per position per season.
-- Drives Step 4 fielder probability model.
-- Full schema with Tango/Statcast OAA methodology, directional breakdown,
-- star ratings (OF), DP metrics (IF), bunt defense, 1B scooping, and
-- decomposed error rates.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.fielder_season_metrics (
    player_id                   INTEGER     NOT NULL,
    position                    VARCHAR(5)  NOT NULL,   -- 1B, 2B, 3B, SS, LF, CF, RF, C, P
    season                      SMALLINT    NOT NULL,
    innings_played              FLOAT       NOT NULL DEFAULT 0,
    sample_batted_balls         INTEGER     NOT NULL DEFAULT 0,

    -- =========================================================================
    -- RANGE / OAA  (Outfielders: catch probability model; Infielders: intercept model)
    -- =========================================================================
    opportunities               INTEGER     DEFAULT 0,      -- tracked plays assigned
    expected_outs               FLOAT       DEFAULT 0,      -- sum of per-play out probabilities
    actual_outs                 INTEGER     DEFAULT 0,
    outs_above_average          FLOAT,                      -- actual - expected
    expected_catch_pct          FLOAT,                      -- avg out probability of workload
    actual_catch_pct            FLOAT,
    catch_pct_added             FLOAT,                      -- actual - expected (rate stat)

    -- Run conversion: 0.75 runs/out IF, 0.90 runs/out OF (Tango)
    range_runs                  FLOAT,

    -- Directional OAA breakdown (for similarity engine feature vectors)
    oaa_glove_side              FLOAT,      -- plays to fielder's glove side
    oaa_arm_side                FLOAT,      -- plays to fielder's arm side
    oaa_charging                FLOAT,      -- slow rollers / bunts (IF) or coming-in (OF)
    oaa_deep                    FLOAT,      -- range plays requiring backhand or going back

    -- Star ratings (OF only — NULL for IF)
    five_star_opps              INTEGER,    -- 0-25% catch prob opportunities
    five_star_catches           INTEGER,
    four_star_opps              INTEGER,    -- 26-50%
    four_star_catches           INTEGER,
    routine_opps                INTEGER,    -- 76-100%
    routine_catches             INTEGER,

    -- =========================================================================
    -- ERRORS — decomposed (fielding vs throwing are different skills)
    -- =========================================================================
    error_rate                  FLOAT,
    fielding_error_count        INTEGER     DEFAULT 0,
    throwing_error_count        INTEGER     DEFAULT 0,
    fielding_error_rate         FLOAT,
    throwing_error_rate         FLOAT,
    error_oaa_cost              FLOAT,      -- OAA lost specifically to errors

    -- =========================================================================
    -- OUTFIELD ARM  (NULL for infielders)
    -- =========================================================================
    arm_strength                FLOAT,      -- supplemental scrape or proxy
    arm_opportunities           INTEGER,    -- plays with runner advancement opportunity
    arm_holds                   INTEGER,    -- runner did not attempt extra base
    arm_hold_rate               FLOAT,      -- deterrence
    arm_assists                 INTEGER,    -- OF assists (throw-outs)
    arm_thrown_out_rate         FLOAT,      -- of those who attempted, % thrown out
    arm_advancement_prevention  FLOAT,      -- (holds + thrown_out) / opportunities
    of_arm_runs                 FLOAT,      -- RE24-based arm run value

    -- =========================================================================
    -- DOUBLE PLAY  (NULL for outfielders)
    -- =========================================================================
    dp_opportunities            INTEGER,    -- GB with runner on 1B only, <2 outs
    dp_attempts                 INTEGER,    -- DP attempted (threw to 2B first)
    dp_turned                   INTEGER,    -- successful DPs (2+ outs)
    dp_expected                 FLOAT,      -- sum of per-play expected DP probability
    dp_above_expected           FLOAT,      -- dp_turned - dp_expected
    dp_attempt_rate             FLOAT,
    dp_success_rate             FLOAT,
    dp_run_value                FLOAT,      -- RE24-based DP runs

    -- Pivot-specific (2B/SS only; NULL for corner IF)
    dp_pivot_opportunities      INTEGER,
    dp_pivot_completions        INTEGER,
    dp_pivot_expected           FLOAT,
    dp_pivot_above_expected     FLOAT,
    dp_pivot_run_value          FLOAT,

    -- =========================================================================
    -- BUNT DEFENSE  (1B/3B primarily; NULL for others)
    -- =========================================================================
    bunt_opportunities          INTEGER,
    bunt_outs_recorded          INTEGER,
    bunt_fielding_rate          FLOAT,

    -- =========================================================================
    -- 1B SCOOPING  (1B only; NULL for others)
    -- =========================================================================
    scoop_opportunities         INTEGER,    -- difficult throws received
    scoop_success_rate          FLOAT,

    -- =========================================================================
    -- META
    -- =========================================================================
    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (player_id, position, season)
);

CREATE INDEX IF NOT EXISTS idx_fsm_season         ON derived.fielder_season_metrics(season);
CREATE INDEX IF NOT EXISTS idx_fsm_position       ON derived.fielder_season_metrics(position, season);

COMMENT ON TABLE  derived.fielder_season_metrics IS 'Per-fielder per-position per-season metrics. Full OAA model with directional breakdown, DP, arm, error decomposition. Input to Step 4 fielder probability model.';
COMMENT ON COLUMN derived.fielder_season_metrics.outs_above_average IS 'Catch probability OAA: sum of (1-CP) for catches + (-CP) for misses, relative to league average.';
COMMENT ON COLUMN derived.fielder_season_metrics.below_minimum_sample IS 'TRUE when sample_batted_balls < 50. Similarity engine falls back to positional average.';

-- =============================================================================
-- DERIVED.BASERUNNER_SEASON_METRICS
-- One row per player per season.
-- Drives both extra-base advancement (Step 5) and stolen base (Step 1)
-- similarity scoring.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.baserunner_season_metrics (
    player_id                   INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,

    sprint_speed                FLOAT,      -- ft/s from Statcast

        -- =========================================================================
    -- Extra-base advancement — attempt / success split per situation
    -- attempt = runner tried to advance (succeeded OR was thrown out)
    -- success = runner reached the next base safely
    -- =========================================================================

    -- Overall extra-base
    extra_base_attempt_rate     FLOAT,      -- attempts / opportunities
    extra_base_success_rate     FLOAT,      -- successes / attempts

    -- First-to-third on singles
    first_to_third_attempt_rate FLOAT,
    first_to_third_success_rate FLOAT,

    -- Score from second on singles
    second_to_home_attempt_rate FLOAT,
    second_to_home_success_rate FLOAT,

    -- Score from first on doubles
    first_to_home_attempt_rate  FLOAT,
    first_to_home_success_rate  FLOAT,

    -- Tag-up on fly balls (3B→home)
    tag_up_attempt_rate         FLOAT,
    tag_up_success_rate         FLOAT,

    -- Stop rate on close plays: fraction of advancement opportunities where
    -- the runner held up.  Computed as 1 - attempt_rate.  Stored explicitly
    -- for the similarity engine feature vector.
    stop_rate                   FLOAT,

    -- Stolen base
    sb_attempt_rate             FLOAT,
    sb_success_rate             FLOAT,
    cs_rate                     FLOAT,

     -- =========================================================================
    -- Per-situation sample sizes for confidence weighting
    -- =========================================================================
    sample_advancement_opps     INTEGER     NOT NULL DEFAULT 0,
    sample_first_to_third_opps  INTEGER     NOT NULL DEFAULT 0,
    sample_second_to_home_opps  INTEGER     NOT NULL DEFAULT 0,
    sample_first_to_home_opps   INTEGER     NOT NULL DEFAULT 0,
    sample_tag_up_opps          INTEGER     NOT NULL DEFAULT 0,
    sample_sb_attempts          INTEGER     NOT NULL DEFAULT 0,

    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (player_id, season)
);

CREATE INDEX IF NOT EXISTS idx_brm_season         ON derived.baserunner_season_metrics(season);
CREATE INDEX IF NOT EXISTS idx_brm_speed          ON derived.baserunner_season_metrics(sprint_speed);

COMMENT ON TABLE  derived.baserunner_season_metrics IS 'Per-runner per-season metrics with attempt/success splits. Drives Steps 1 (SB decision) and 5 (extra-base advancement). Step 2.4 Baserunner-to-Baserunner similarity engine.';
COMMENT ON COLUMN derived.baserunner_season_metrics.stop_rate IS 'Fraction of advancement opportunities where runner held up. 1 - extra_base_attempt_rate. Captures conservative vs aggressive baserunning tendency.';
COMMENT ON COLUMN derived.baserunner_season_metrics.below_minimum_sample IS 'TRUE when sample_advancement_opps < 20 or sample_sb_attempts < 5. Falls back to speed-bucket average.';

-- =============================================================================
-- DERIVED.BASERUNNER_STEAL_METRICS
-- One row per baserunner per season, stolen-base dimension only.
-- Indexed by the Step 2.5 Baserunner-Steal similarity engine. SIM-408: the
-- engine referenced this table but the computor never built it. Built from
-- raw.pitches SB attempt/success flags. Biomech jump features (reaction_time,
-- burst_distance, break_angle) are intentionally ABSENT — Statcast does not
-- publish them, and the engine's JUMP sub-score was removed accordingly.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.baserunner_steal_metrics (
    player_id                   INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,

    sample_steal_attempts       INTEGER     NOT NULL DEFAULT 0,   -- total SB attempts as lead runner
    sample_first_base_opps      INTEGER     NOT NULL DEFAULT 0,   -- PAs begun on 1B (steal-2B opp denom)

    -- Tendency
    steal_attempt_rate          FLOAT,      -- total attempts / first_base_opps
    steal_attempt_rate_2b       FLOAT,      -- 2B→3B attempts / PAs begun on 2B
    -- Success
    steal_success_rate          FLOAT,      -- successes / attempts (overall)
    steal_success_rate_2b       FLOAT,      -- 2B→3B successes / 2B→3B attempts

    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (player_id, season)
);

CREATE INDEX IF NOT EXISTS idx_bsm_season ON derived.baserunner_steal_metrics(season);

COMMENT ON TABLE derived.baserunner_steal_metrics IS 'SIM-408: per-runner per-season stolen-base tendency/success metrics for the Step 2.5 baserunner-steal engine. Built from raw.pitches SB flags. below_minimum_sample = sample_steal_attempts < 10 (the engine filters these out).';

-- =============================================================================
-- DERIVED.PITCHER_STEAL_METRICS
-- One row per pitcher per season, steal-prevention OUTCOME dimension only.
-- Indexed by the Step 2.7 Pitcher-Steal similarity engine. SIM-408: the engine
-- referenced this table but the computor never built it. Built from raw.pitches.
-- Delivery (biomech timings) + Pickoff (no source columns in raw.pitches) are
-- intentionally ABSENT — Statcast doesn't publish them; the engine's Delivery
-- and Pickoff sub-scores were removed, leaving an outcome-only engine.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.pitcher_steal_metrics (
    pitcher_id                      INTEGER     NOT NULL,
    season                          SMALLINT    NOT NULL,
    throws                          CHAR(1),

    sample_baserunner_events        INTEGER     NOT NULL DEFAULT 0,   -- PAs pitched with a runner on
    sample_steal_attempts_against   INTEGER     NOT NULL DEFAULT 0,

    -- Outcome
    sb_against_per_9                FLOAT,      -- stolen bases allowed per 9 IP
    cs_rate_forced                  FLOAT,      -- CS / attempts against
    steal_attempt_rate_allowed      FLOAT,      -- attempts against / baserunner events

    below_minimum_sample            BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                      TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (pitcher_id, season)
);

CREATE INDEX IF NOT EXISTS idx_pitcher_steal_season ON derived.pitcher_steal_metrics(season);

COMMENT ON TABLE derived.pitcher_steal_metrics IS 'SIM-408: per-pitcher per-season steal-prevention OUTCOME metrics for the Step 2.7 pitcher-steal engine. Built from raw.pitches (SB-against flags + outs for IP). Delivery/pickoff features omitted (not in Statcast). below_minimum_sample = sample_baserunner_events < 30.';

-- =============================================================================
-- DERIVED.CATCHER_SEASON_METRICS
-- One row per catcher per season.
-- Full schema covering framing, blocking, and stolen base prevention.
-- Drives Steps 1/3a (stolen base) and indirect pitch selection via framing.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.catcher_season_metrics (
    player_id                   INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,

    -- =========================================================================
    -- FRAMING
    -- =========================================================================
    pitches_received_total      INTEGER     DEFAULT 0,
    called_pitches              INTEGER     DEFAULT 0,      -- taken pitches (not swung at)
    called_strikes              INTEGER     DEFAULT 0,
    expected_called_strikes     FLOAT,                      -- from framing model
    strikes_above_average       FLOAT,                      -- actual - expected
    framing_runs                FLOAT,                      -- ~0.125 runs per strike above avg

    -- Framing zone breakdown (for similarity engine)
    framing_low                 FLOAT,      -- strikes above avg on low pitches
    framing_high                FLOAT,      -- on high pitches
    framing_inside              FLOAT,      -- on inside-edge pitches
    framing_outside             FLOAT,      -- on outside-edge pitches

    -- SIM-408: zone-framing called-strike rates for the catcher engine
    shadow_zone_strike_rate     FLOAT,      -- called-strike rate just off the edge (framing skill)
    heart_zone_strike_rate      FLOAT,      -- called-strike rate clearly in-zone (low signal)

    -- =========================================================================
    -- BLOCKING
    -- =========================================================================
    expected_pbwp               FLOAT,      -- sum of per-pitch PB/WP probabilities
    actual_pbwp                 INTEGER,    -- actual PB + WP events
    blocks_above_average        FLOAT,      -- expected - actual (positive = good)
    blocking_runs               FLOAT,      -- blocks_above_average * 0.25

    -- Blocking category breakdown
    blocks_aa_bounced           FLOAT,      -- on bounced pitches
    blocks_aa_high              FLOAT,      -- on high pitches (>4 ft)
    blocks_aa_lateral           FLOAT,      -- on pitches far from center

    -- =========================================================================
    -- STOLEN BASE PREVENTION
    -- =========================================================================
    pop_time_mean               FLOAT,      -- 2B pop time (seconds: exchange + throw)
    pop_time_std                FLOAT,
    arm_strength_mean           FLOAT,      -- Throw velocity (mph)

    -- Overall SB prevention
    sb_attempts_faced           INTEGER     DEFAULT 0,
    cs_total                    INTEGER     DEFAULT 0,
    cs_rate                     FLOAT,      -- Caught stealing rate (attempts faced)
    sb_allowed_rate             FLOAT,

    -- By-base breakdown
    sb_attempts_2b              INTEGER     DEFAULT 0,
    cs_rate_2b                  FLOAT,
    sb_attempts_3b              INTEGER     DEFAULT 0,
    cs_rate_3b                  FLOAT,

    -- Deterrence: rate at which opposing runners attempt to steal against this
    -- catcher per PA-level opportunity.  Lower = better arm reputation.
    -- Formula (SIM-073, BA-approved): (SB + CS) / (opp_1B + opp_2B), where
    --   opp_1B = PAs with runner on 1B and 2B empty
    --   opp_2B = PAs with runner on 2B and 3B empty
    -- Min-sample guard: NULL if opportunity denominator < 100 PA opportunities.
    steal_attempt_rate_against  FLOAT,

    -- Pickoffs
    pickoff_attempts            INTEGER     DEFAULT 0,
    pickoff_successes           INTEGER     DEFAULT 0,
    pickoff_rate                FLOAT,

    -- =========================================================================
    -- META
    -- =========================================================================
    below_minimum_sample        BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (player_id, season)
);

CREATE INDEX IF NOT EXISTS idx_csm_season ON derived.catcher_season_metrics(season);

COMMENT ON TABLE  derived.catcher_season_metrics IS 'Per-catcher per-season metrics: framing, blocking, and SB prevention. Input to Steps 1/3a.';
COMMENT ON COLUMN derived.catcher_season_metrics.framing_runs IS '~0.125 runs per strike above average (Statcast methodology).';
COMMENT ON COLUMN derived.catcher_season_metrics.blocking_runs IS 'blocks_above_average * 0.25 runs per block saved.';
COMMENT ON COLUMN derived.catcher_season_metrics.below_minimum_sample IS 'TRUE when sb_attempts_faced < 10. Falls back to league-average catcher profile.';

-- =============================================================================
-- DERIVED.MANAGER_SEASON_METRICS
-- One row per manager per season.
-- Drives Step 7 managerial substitution decisions.
-- =============================================================================

-- SIM-408: schema reconciled to the manager engine's feature vocabulary
-- (USAGE / AGGRESSION / PLATOON sub-scores). The prior vocabulary
-- (sp_pitch_count_mean / ph_rate_* / bullpen_leverage_mapping) was never read by
-- the engine. USAGE columns are GATED on the SIM-427 bullpen-roster build and
-- emitted NULL until it lands; aggression + platoon are computed from the play
-- stream.
CREATE TABLE IF NOT EXISTS derived.manager_season_metrics (
    manager_id                              INTEGER     NOT NULL,
    season                                  SMALLINT    NOT NULL,
    sample_games                            INTEGER     NOT NULL DEFAULT 0,
    sample_starter_decisions                INTEGER     NOT NULL DEFAULT 0,

    -- Usage (SIM-427-gated: NULL until the per-(team,season) bullpen roster exists)
    starter_avg_pitch_count                 FLOAT,
    starter_pull_pct_before_100             FLOAT,
    closer_entry_leverage_index             FLOAT,
    high_leverage_reliever_rate             FLOAT,
    opener_usage_rate                       FLOAT,
    bulk_innings_rate                       FLOAT,
    -- SIM-427 capstone: relievers used / relievers available (the SIM-433-v2
    -- available-but-unused signal) — usage normalized by opportunity.
    available_reliever_usage_rate           FLOAT,

    -- Aggression (computed from the play stream; uncomputable ones NULL)
    steal_order_rate_per_1b_opp             FLOAT,
    hit_and_run_rate_per_opportunity        FLOAT,      -- not flagged in Statcast → NULL
    sac_bunt_rate_high_leverage             FLOAT,
    sac_bunt_rate_low_leverage              FLOAT,
    squeeze_play_rate_per_3b_opp            FLOAT,

    -- Platoon (computed from the play stream; uncomputable ones NULL)
    pinch_hit_rate_vs_same_hand             FLOAT,
    pinch_hit_rate_high_leverage            FLOAT,
    defensive_sub_rate_late_innings         FLOAT,
    double_switch_rate_per_reliever_change  FLOAT,      -- not detectable in Statcast → NULL
    platoon_advantage_exploitation_rate     FLOAT,

    below_minimum_sample                    BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at                              TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (manager_id, season)
);

CREATE INDEX IF NOT EXISTS idx_msm_season ON derived.manager_season_metrics(season);

COMMENT ON TABLE  derived.manager_season_metrics IS 'SIM-408: per-manager per-season tendencies in the manager engine vocabulary (usage/aggression/platoon). Usage gated on SIM-427; aggression+platoon from the play stream. below_minimum_sample = sample_games < 50.';

-- =============================================================================
-- DERIVED.PARK_FACTORS
-- One row per venue per season per factor type.
-- Used in Steps 3b/4 for hit probability adjustments and expected distances.
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.park_factors (
    venue_id                    INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    -- Factor type: HR, 1B, 2B, 3B, R, K, BB, GB, FB
    factor_type                 VARCHAR(5)  NOT NULL,
    factor_overall              FLOAT       NOT NULL,   -- 1.0 = league average
    factor_vs_l                 FLOAT,
    factor_vs_r                 FLOAT,
    sample_pa                   INTEGER     NOT NULL DEFAULT 0,
    -- Bayesian-shrunk toward 1.0 based on sample size
    regressed_factor            FLOAT       NOT NULL,

    PRIMARY KEY (venue_id, season, factor_type)
);

CREATE INDEX IF NOT EXISTS idx_pf_season      ON derived.park_factors(season);
CREATE INDEX IF NOT EXISTS idx_pf_venue       ON derived.park_factors(venue_id, season);

COMMENT ON TABLE  derived.park_factors IS 'Park factors per venue per season. Historical seasons frozen after year-end. Current season rebuilt weekly. SIM-336: factor_vs_l/factor_vs_r are real splits on BATTER handedness (stand); switch-hitter (stand=S) PAs count toward factor_overall only.';
COMMENT ON COLUMN derived.park_factors.factor_overall IS 'SIM-336: venue event rate / league event rate, 1.0 = neutral. Computed via grouped UNPIVOT (overall + L/R splits melted together).';
COMMENT ON COLUMN derived.park_factors.factor_vs_l IS 'SIM-336: venue-vs-LHB rate / league-vs-LHB rate. NULL when the venue had no vs-LHB sample for the factor type.';
COMMENT ON COLUMN derived.park_factors.factor_vs_r IS 'SIM-336: venue-vs-RHB rate / league-vs-RHB rate. NULL when the venue had no vs-RHB sample for the factor type.';
COMMENT ON COLUMN derived.park_factors.regressed_factor IS 'Bayesian-shrunk toward 1.0 based on sample_pa. Use this value in simulation rather than factor_overall. SIM-336 pool-neutralization policy: sim pools are drawn from real (already park-influenced) PAs, so apply park as base_rate * (target_factor / comp_origin_factor) — divide out the comp origin-venue factor before multiplying by the target venue factor, to avoid double-counting.';

-- =============================================================================
-- DERIVED.AT_BAT_SITUATIONS
-- One row per plate appearance, capturing the pre-PA game state (the situation
-- the batter came up to). Indexed by the Step 2.9 Situation-to-Situation KDTree
-- engine (similarity/engines/situation_similarity.py). SIM-408: the engine
-- referenced this table but the computor never built it, so the situation engine
-- indexed 0 rows and normalized a degenerate empty matrix. Materialized from
-- raw.pitches (the first pitch of each PA).
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.at_bat_situations (
    play_id                     VARCHAR     NOT NULL,   -- '<game_pk>-<at_bat_number>'
    game_pk                     INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    venue_id                    INTEGER,                -- joins derived.park_factors(venue_id, season)
    at_bat_number               INTEGER     NOT NULL,

    -- Pre-PA game state (the SituationVector dimensions)
    inning                      SMALLINT,
    top_or_bottom               SMALLINT,               -- 0 = top (away batting), 1 = bottom
    outs_when_up                SMALLINT,
    on_1b                       SMALLINT,               -- 0/1 base-occupied flag
    on_2b                       SMALLINT,
    on_3b                       SMALLINT,
    home_score                  SMALLINT,
    away_score                  SMALLINT,
    leverage_index              FLOAT,                  -- compute_leverage_index(), computed in SQL
    pitcher_pitch_count         INTEGER,                -- current pitcher's pitches entering the PA
    batter_pa_count             INTEGER,                -- batter's PA sequence number in the game

    PRIMARY KEY (game_pk, at_bat_number)
);

CREATE INDEX IF NOT EXISTS idx_abs_season       ON derived.at_bat_situations(season);
CREATE INDEX IF NOT EXISTS idx_abs_venue_season ON derived.at_bat_situations(venue_id, season);

COMMENT ON TABLE derived.at_bat_situations IS 'SIM-408: one row per plate appearance with the pre-PA game state (inning / top-bottom / outs / base-state / score / leverage / pitch-count / PA-count) indexed by the Step 2.9 situation KDTree engine. Built from raw.pitches at the first pitch of each PA; leverage_index replicates pipeline.batch.player_profile_computor.compute_leverage_index() as a SQL expression.';

-- =============================================================================
-- SIM.PITCH_POOL
-- Filtered, indexed copy of pitches used for Step 2 pitch selection similarity.
-- Excludes quality-flagged rows. Contains pre-computed fields needed for fast
-- similarity scoring without joining back to raw.pitches.
-- Rebuilt nightly from PostgreSQL raw.pitches.
-- =============================================================================

CREATE TABLE IF NOT EXISTS sim.pitch_pool (
    pitch_id                    BIGINT      NOT NULL,   -- FK → raw.pitches surrogate (ETL assigned)

    -- Source reference
    game_pk                     INTEGER     NOT NULL,
    at_bat_number               INTEGER     NOT NULL,
    pitch_number                INTEGER     NOT NULL,
    game_date                   DATE        NOT NULL,
    season                      SMALLINT    NOT NULL,

    -- Pitcher context
    pitcher_id                  INTEGER     NOT NULL,
    p_throws                    VARCHAR(1)  NOT NULL,

    -- Batter context
    batter_id                   INTEGER     NOT NULL,
    stand                       VARCHAR(1)  NOT NULL,

    -- -------------------------------------------------------------------------
    -- Core pitch features (pre-computed, used directly in similarity scoring)
    -- -------------------------------------------------------------------------
    velo                        FLOAT,      -- release_speed
    ivb                         FLOAT,      -- break_vertical_induced (gravity-removed)
    hb                          FLOAT,      -- break_horizontal
    spin_rate                   FLOAT,
    spin_axis                   FLOAT,
    release_x                   FLOAT,      -- release_pos_x
    release_z                   FLOAT,      -- release_pos_z
    release_ext                 FLOAT,      -- release_extension
    plate_x                     FLOAT,
    plate_z                     FLOAT,
    zone                        SMALLINT,

    -- -------------------------------------------------------------------------
    -- Situation features
    -- -------------------------------------------------------------------------
    count_balls                 SMALLINT    NOT NULL,
    count_strikes               SMALLINT    NOT NULL,
    outs                        SMALLINT    NOT NULL,
    -- Bitmask encoding: bit0=1B occupied, bit1=2B occupied, bit2=3B occupied
    -- Values: 0=empty, 1=1B, 2=2B, 3=1B+2B, 4=3B, 5=1B+3B, 6=2B+3B, 7=loaded
    runners_state               SMALLINT    NOT NULL DEFAULT 0,
    inning                      SMALLINT    NOT NULL,
    score_diff                  SMALLINT    NOT NULL,   -- bat_score - fld_score

    -- Previous pitch context (for sequence modeling)
    prev_pitch_velo             FLOAT,
    prev_pitch_ivb              FLOAT,
    prev_pitch_hb               FLOAT,
    prev_pitch_outcome          VARCHAR(5), -- B, S, X, C, F

    -- -------------------------------------------------------------------------
    -- Outcome
    -- -------------------------------------------------------------------------
    -- Terminal outcome classification
    outcome_type                VARCHAR(20) NOT NULL,
                                -- ball, called_strike, swinging_strike, foul, in_play
    events                      VARCHAR(50),            -- Non-NULL when outcome_type = in_play

    -- SIM-076: sampling recency weight. 2.0 for the most-recent two seasons,
    -- geometric decay (×0.75/season, floor 0.25) for older data, relative to
    -- the build's reference season. The PlayPoolSampler multiplies a row's
    -- distance-weight by recency_weight so recent form is preferred.
    recency_weight              FLOAT       NOT NULL DEFAULT 1.0,

    PRIMARY KEY (pitch_id)
);

-- SIM-337: index set reconciled to the SIM-111 play-pool query contract (§6.2).
-- The pitch read path pre-filters on pitcher_id + stand (batter handedness)
-- before any FAISS tile is materialized, and otherwise reads by primary key; the
-- situation/feature/outcome columns are read in bulk inside that scoped scan,
-- never via a single-column predicate. So the pitcher/season pre-filter indexes
-- (+ advisory season/game_date) are kept, a stand-bearing composite is added so
-- the pre-filter is fully index-served, and the outcome_type/count indexes (which
-- SIM-115 wrongly kept) are dropped. DuckDB's columnar zone maps cover the
-- bulk-scan columns. See db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql
-- and docs/architecture/2026-05-21-play-pool-query-contracts.md §6.2.
CREATE INDEX IF NOT EXISTS idx_pp_pitcher_season       ON sim.pitch_pool(pitcher_id, season);
CREATE INDEX IF NOT EXISTS idx_pp_pitcher              ON sim.pitch_pool(pitcher_id);
CREATE INDEX IF NOT EXISTS idx_pp_season               ON sim.pitch_pool(season);
CREATE INDEX IF NOT EXISTS idx_pp_game_date            ON sim.pitch_pool(game_date);
CREATE INDEX IF NOT EXISTS idx_pp_pitcher_stand_season ON sim.pitch_pool(pitcher_id, stand, season);

COMMENT ON TABLE  sim.pitch_pool IS 'Pre-filtered pitch pool for Step 2 similarity scoring. Quality-flagged pitches excluded. Rebuilt nightly from PostgreSQL.';
COMMENT ON COLUMN sim.pitch_pool.runners_state IS 'Bitmask: bit0=1B, bit1=2B, bit2=3B. Values 0-7. Encodes baserunner configuration for situational similarity.';
COMMENT ON COLUMN sim.pitch_pool.ivb IS 'Induced vertical break — gravity removed. Pre-computed from break_vertical_induced. Primary pitch movement feature.';

-- =============================================================================
-- SIM.OUTCOME_POOL
-- Subset of pitch_pool where outcome_type = in_play.
-- Adds full batted ball details needed for Steps 3b (outcome type) and
-- Step 4 (fielder probability + baserunner advancement).
-- =============================================================================

CREATE TABLE IF NOT EXISTS sim.outcome_pool (
    pitch_id                    BIGINT      NOT NULL,

    -- All pitch_pool columns duplicated here for self-contained queries
    game_pk                     INTEGER     NOT NULL,
    at_bat_number               INTEGER     NOT NULL,
    pitch_number                INTEGER     NOT NULL,
    game_date                   DATE        NOT NULL,
    season                      SMALLINT    NOT NULL,
    pitcher_id                  INTEGER     NOT NULL,
    p_throws                    VARCHAR(1)  NOT NULL,
    batter_id                   INTEGER     NOT NULL,
    stand                       VARCHAR(1)  NOT NULL,
    velo                        FLOAT,
    ivb                         FLOAT,
    hb                          FLOAT,
    spin_rate                   FLOAT,
    spin_axis                   FLOAT,
    release_x                   FLOAT,
    release_z                   FLOAT,
    release_ext                 FLOAT,
    plate_x                     FLOAT,
    plate_z                     FLOAT,
    zone                        SMALLINT,
    count_balls                 SMALLINT    NOT NULL,
    count_strikes               SMALLINT    NOT NULL,
    outs                        SMALLINT    NOT NULL,
    runners_state               SMALLINT    NOT NULL DEFAULT 0,
    inning                      SMALLINT    NOT NULL,
    score_diff                  SMALLINT    NOT NULL,
    events                      VARCHAR(50),

    -- -------------------------------------------------------------------------
    -- Batted ball details (the additional columns over pitch_pool)
    -- -------------------------------------------------------------------------
    exit_velo                   FLOAT,      -- launch_speed
    launch_angle                FLOAT,
    spray_angle                 FLOAT,                  -- raw spray angle (Statcast)
    -- SIM-051: handedness-corrected spray angle.  Same sign convention for
    -- LHB pull doubles and RHB pull doubles — flips on bat_hand at ETL time.
    -- NULL when bat_hand is 'S' (unresolved switch hitter).
    pull_relative_spray_angle   FLOAT,
    bb_type                     VARCHAR(20),            -- ground_ball, fly_ball, line_drive, popup
    hit_distance                FLOAT,
    hc_x                        FLOAT,
    hc_y                        FLOAT,

    -- Fielding outcome
    fielded_by_position         SMALLINT,               -- 1–9 positional number
    fielding_error_position     SMALLINT,               -- Position where error occurred; NULL = clean

    -- Result
    result_hits                 SMALLINT    NOT NULL DEFAULT 0,     -- 0-4 (0=out, 1=1B, 2=2B, 3=3B, 4=HR)
    result_outs                 SMALLINT    NOT NULL DEFAULT 0,
    result_runs                 SMALLINT    NOT NULL DEFAULT 0,

    -- SIM-076: sampling recency weight (see sim.pitch_pool.recency_weight).
    recency_weight              FLOAT       NOT NULL DEFAULT 1.0,

    -- SIM-411/413/425b realism facts (migration 0012). Appended AFTER recency_weight
    -- so the computor's positional `INSERT ... SELECT * FROM bip` maps identically on
    -- a fresh build and a 0012-migrated DB. Nullable: NULL on rows from a pre-0012 build.
    venue_id                    INTEGER,                -- SIM-411: joins derived.park_factors(venue_id, season)
    fielder_player_id           INTEGER,                -- SIM-425b: raw.pitches.fielded_by (who fielded; NULL when uncredited)

    PRIMARY KEY (pitch_id)
);

-- SIM-337: outcome_pool is read by PK point-lookup (the sampler's payload fetch)
-- and built pre-filtered on season + stand (batter handedness); the batted-ball
-- detail columns are read in bulk inside that scoped scan, never via a
-- single-column predicate (SIM-111 contract §6.2 E/F). The season index
-- (incremental rebuild, SIM-095) is kept and a stand-bearing composite is added
-- so the season + stand pre-filter is fully index-served. Write-overhead indexes
-- on bulk-scan/projected columns dropped.
-- See db/migrations/duckdb/0006_sim337_reconcile_pool_indexes.sql.
CREATE INDEX IF NOT EXISTS idx_op_season       ON sim.outcome_pool(season);
CREATE INDEX IF NOT EXISTS idx_op_stand_season ON sim.outcome_pool(stand, season);

COMMENT ON TABLE  sim.outcome_pool IS 'In-play subset of pitch_pool with full batted ball detail. Input to Steps 3b/4/5.';

-- =============================================================================
-- SIM.STOLEN_BASE_POOL
-- All pitches where an SB was attempted. Used in Step 1 (steal decision) and
-- Step 3a (steal outcome resolution).
-- =============================================================================

CREATE TABLE IF NOT EXISTS sim.stolen_base_pool (
    pitch_id                    BIGINT      NOT NULL,
    game_pk                     INTEGER     NOT NULL,
    at_bat_number               INTEGER     NOT NULL,
    pitch_number                INTEGER     NOT NULL,
    game_date                   DATE        NOT NULL,
    season                      SMALLINT    NOT NULL,

    -- Participants
    runner_id                   INTEGER     NOT NULL,   -- Player attempting the steal
    pitcher_id                  INTEGER     NOT NULL,
    catcher_id                  INTEGER     NOT NULL,   -- fielder_2

    -- Base attempted
    base_attempted              VARCHAR(5)  NOT NULL CHECK (base_attempted IN ('2B','3B','home')),

    -- Situation
    inning                      SMALLINT    NOT NULL,
    outs                        SMALLINT    NOT NULL,
    runners_state               SMALLINT    NOT NULL DEFAULT 0,
    score_diff                  SMALLINT    NOT NULL,
    count_balls                 SMALLINT    NOT NULL,
    count_strikes               SMALLINT    NOT NULL,

    -- Pitch characteristics at time of steal
    velo                        FLOAT,
    ivb                         FLOAT,

    -- Runner / catcher metrics at time of season (denormalized from derived tables
    -- for fast similarity scoring without joins)
    runner_sprint_speed         FLOAT,
    runner_sb_success_rate      FLOAT,
    catcher_pop_time_mean       FLOAT,
    catcher_arm_strength        FLOAT,
    pitcher_sb_allowed_rate     FLOAT,

    -- Outcome
    success                     BOOLEAN     NOT NULL,

    -- SIM-076: sampling recency weight (see sim.pitch_pool.recency_weight).
    recency_weight              FLOAT       NOT NULL DEFAULT 1.0,

    PRIMARY KEY (pitch_id)
);

CREATE INDEX IF NOT EXISTS idx_sbp_runner         ON sim.stolen_base_pool(runner_id);
CREATE INDEX IF NOT EXISTS idx_sbp_catcher        ON sim.stolen_base_pool(catcher_id);
CREATE INDEX IF NOT EXISTS idx_sbp_pitcher        ON sim.stolen_base_pool(pitcher_id);
CREATE INDEX IF NOT EXISTS idx_sbp_season         ON sim.stolen_base_pool(season);
CREATE INDEX IF NOT EXISTS idx_sbp_base           ON sim.stolen_base_pool(base_attempted);
CREATE INDEX IF NOT EXISTS idx_sbp_success        ON sim.stolen_base_pool(success);
CREATE INDEX IF NOT EXISTS idx_sbp_runner_speed   ON sim.stolen_base_pool(runner_sprint_speed);

COMMENT ON TABLE  sim.stolen_base_pool IS 'All pitches with SB attempts. Denormalized runner/catcher metrics enable fast similarity scoring without joins.';
COMMENT ON COLUMN sim.stolen_base_pool.runner_sprint_speed IS 'Denormalized from derived.baserunner_season_metrics at ETL time. Avoids join during simulation.';

-- =============================================================================
-- SIM.POOL_BUILD_METADATA  (SIM-076 / SIM-095)
-- One row per (pool, season) recording the last build. Drives the incremental
-- pool rebuild (SIM-095): a season is skipped when its source watermark has not
-- advanced since the last build. Also records the recency reference season used.
-- =============================================================================

CREATE TABLE IF NOT EXISTS sim.pool_build_metadata (
    pool_name             VARCHAR(20) NOT NULL,   -- 'pitch_pool' | 'outcome_pool' | 'stolen_base_pool'
    season                SMALLINT    NOT NULL,
    row_count             BIGINT      NOT NULL DEFAULT 0,
    source_max_game_date  DATE,                   -- max(game_date) of source rows at build time (watermark)
    -- SIM-345: source row count at build time. Paired with source_max_game_date
    -- so the incremental rebuild can detect a same-game_date late/doubleheader
    -- row (which does NOT advance the watermark). A season is fresh only when the
    -- watermark has not advanced AND this count is unchanged.
    source_row_count      BIGINT,
    recency_ref_season    SMALLINT,               -- reference season used for recency_weight (canonical across pools, SIM-345)
    builder_version       VARCHAR(20),
    built_at              TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (pool_name, season)
);

COMMENT ON TABLE sim.pool_build_metadata IS 'SIM-076/SIM-095/SIM-345: per-(pool,season) build watermark + source row count + canonical recency reference. Enables incremental pool rebuild (skip seasons whose source date AND row count are unchanged).';

-- =============================================================================
-- PER-PLAY DETAIL TABLES
-- Store per-play probabilities from the fitted defensive models.
-- Used for model validation, recalibration, and ad-hoc analysis.
-- Rebuilt nightly by the ETL; can be dropped without losing derived season metrics.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- DERIVED.OUTFIELD_PLAY_DETAIL
-- One row per outfield catch opportunity per play.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS derived.outfield_play_detail (
    pitch_id                    BIGINT      NOT NULL,
    game_pk                     INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    fielder_id                  INTEGER     NOT NULL,
    position                    VARCHAR(5)  NOT NULL,   -- LF, CF, RF

    -- Model inputs
    hang_time                   FLOAT,                  -- seconds (estimated from launch conditions)
    distance_needed             FLOAT,                  -- feet from fielder start to landing spot
    speed_needed                FLOAT,                  -- ft/s required to make the play
    direction_back              BOOLEAN     DEFAULT FALSE,  -- TRUE = going away from home plate
    near_wall                   BOOLEAN     DEFAULT FALSE,

    -- Model output
    catch_probability           FLOAT       NOT NULL,   -- P(catch) from fitted sigmoid
    star_rating                 SMALLINT,               -- 1–5 (5 = most difficult)

    -- Outcome
    caught                      BOOLEAN     NOT NULL,
    oaa_credit                  FLOAT       NOT NULL,   -- (1-CP) if caught, -CP if not caught

    PRIMARY KEY (pitch_id, fielder_id)
);

CREATE INDEX IF NOT EXISTS idx_opd_fielder   ON derived.outfield_play_detail(fielder_id, season);
CREATE INDEX IF NOT EXISTS idx_opd_season    ON derived.outfield_play_detail(season);

COMMENT ON TABLE derived.outfield_play_detail IS 'Per-play OF catch probability detail. Rebuilt nightly. Used for OAA aggregation and model validation.';

-- -----------------------------------------------------------------------------
-- DERIVED.INFIELD_PLAY_DETAIL
-- One row per infield out opportunity per play.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS derived.infield_play_detail (
    pitch_id                    BIGINT      NOT NULL,
    game_pk                     INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,
    fielder_id                  INTEGER     NOT NULL,
    position                    VARCHAR(5)  NOT NULL,   -- 1B, 2B, 3B, SS

    -- Model inputs
    fielder_distance            FLOAT,                  -- feet from fielder start to ball
    throw_distance              FLOAT,                  -- feet from ball location to 1B
    time_margin                 FLOAT,                  -- runner_time_to_1B - defense_time (pos = time to spare)
    runner_speed                FLOAT,                  -- batter-runner sprint speed (ft/s)
    direction_category          VARCHAR(20),            -- glove_side, arm_side, charging, deep

    -- Model output
    out_probability             FLOAT       NOT NULL,   -- P(out) from fitted sigmoid

    -- Outcome
    out_recorded                BOOLEAN     NOT NULL,
    is_error                    BOOLEAN     DEFAULT FALSE,
    error_type                  VARCHAR(20),            -- fielding, throwing, NULL
    oaa_credit                  FLOAT       NOT NULL,

    PRIMARY KEY (pitch_id, fielder_id)
);

CREATE INDEX IF NOT EXISTS idx_ipd_fielder   ON derived.infield_play_detail(fielder_id, season);
CREATE INDEX IF NOT EXISTS idx_ipd_season    ON derived.infield_play_detail(season);

COMMENT ON TABLE derived.infield_play_detail IS 'Per-play infield OAA detail via time-margin model. Rebuilt nightly.';

-- -----------------------------------------------------------------------------
-- DERIVED.DP_PLAY_DETAIL
-- One row per double play opportunity (GB, runner on 1B only, <2 outs).
-- Tracks both initiator credit and pivot credit separately.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS derived.dp_play_detail (
    pitch_id                    BIGINT      NOT NULL,
    game_pk                     INTEGER     NOT NULL,
    season                      SMALLINT    NOT NULL,

    -- Participants
    initiator_id                INTEGER     NOT NULL,   -- fielder who fielded the ball
    initiator_position          VARCHAR(5)  NOT NULL,
    pivot_id                    INTEGER,                -- fielder who turned the relay; NULL if no attempt
    pivot_position              VARCHAR(5),

    -- Model inputs
    time_margin                 FLOAT,                  -- min(runner_time_to_2B, batter_time_to_1B) - defense_time
    runner_speed                FLOAT,                  -- runner on 1B sprint speed (ft/s)
    batter_speed                FLOAT,                  -- batter-runner sprint speed (ft/s)
    fielder_toward_2b           BOOLEAN,                -- TRUE = fielder moving toward 2B bag

    -- Model output
    dp_probability              FLOAT       NOT NULL,   -- P(DP) from fitted sigmoid

    -- Outcome
    dp_turned                   BOOLEAN     NOT NULL,
    outs_recorded               SMALLINT    NOT NULL,   -- 1 or 2

    -- RE24-based run value
    re24_start                  FLOAT,                  -- expected runs at start of play
    re24_end                    FLOAT,                  -- expected runs after play
    runs_scored                 SMALLINT    DEFAULT 0,
    dp_run_value                FLOAT,                  -- re24_start - re24_end (positive = runs prevented)

    PRIMARY KEY (pitch_id)
);

CREATE INDEX IF NOT EXISTS idx_dpd_initiator ON derived.dp_play_detail(initiator_id, season);
CREATE INDEX IF NOT EXISTS idx_dpd_pivot     ON derived.dp_play_detail(pivot_id, season);
CREATE INDEX IF NOT EXISTS idx_dpd_season    ON derived.dp_play_detail(season);

COMMENT ON TABLE derived.dp_play_detail IS 'Per-play DP opportunity detail. Scoped to: runner on 1B only, <2 outs, ground ball. Tango intercept/sigmoid methodology.';

-- =============================================================================
-- DERIVED.RUN_EXPECTANCY_MATRIX
-- Pre-computed 24-state (outs × runners_state) RE matrix.
-- Used for RE24-based run value calculations across all defensive metrics
-- (DP run value, OF arm runs, etc.).
-- =============================================================================

CREATE TABLE IF NOT EXISTS derived.run_expectancy_matrix (
    season_range                VARCHAR(20) NOT NULL,   -- e.g. '2021-2025'
    outs                        SMALLINT    NOT NULL CHECK (outs BETWEEN 0 AND 2),
    -- runners_state bitmask: bit0=1B occupied, bit1=2B occupied, bit2=3B occupied
    runners_state               SMALLINT    NOT NULL CHECK (runners_state BETWEEN 0 AND 7),
    expected_runs               FLOAT       NOT NULL,
    sample_size                 INTEGER     NOT NULL,

    PRIMARY KEY (season_range, outs, runners_state)
);

COMMENT ON TABLE derived.run_expectancy_matrix IS 'Standard 24-state RE matrix built from historical play-by-play. Used for DP run value, OF arm runs, and all RE24 calculations.';
COMMENT ON COLUMN derived.run_expectancy_matrix.runners_state IS 'Bitmask: bit0=1B, bit1=2B, bit2=3B. Matches sim.pitch_pool.runners_state encoding.';
