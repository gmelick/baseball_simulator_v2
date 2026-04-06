-- =============================================================================
-- BASEBALL SIMULATION SYSTEM — PostgreSQL Schema
-- Engine: PostgreSQL 16+
-- Schemas: raw (ingested data), sim (session/operational state)
--
-- Tables:
--   raw.venues          raw.teams           raw.players
--   raw.managers        raw.games           raw.game_lineups
--   raw.pitches
--   sim.lineup_state
--
-- Run order matters — FK dependencies are respected top to bottom.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gin";

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS sim;

-- =============================================================================
-- RAW.VENUES
-- =============================================================================

CREATE TABLE raw.venues (
    venue_id            INTEGER         NOT NULL,
    season              INTEGER         NOT NULL,
    venue_name          VARCHAR(100)    NOT NULL,
    city                VARCHAR(100)    NOT NULL,
    state               VARCHAR(50),
    surface             VARCHAR(20)     NOT NULL CHECK (surface IN ('Turf','Grass')),
    roof_type           VARCHAR(20)     NOT NULL CHECK (roof_type IN ('Open','Retractable','Dome')),
    elevation_ft        FLOAT         NOT NULL DEFAULT 0,
    lf_dist             FLOAT,
    lf_gap_dist         FLOAT,
    cf_dist             FLOAT,
    rf_gap_dist         FLOAT,
    rf_dist             FLOAT,
    lf_wall_ht          FLOAT,
    lf_gap_wall_ht      FLOAT,
    cf_wall_ht          FLOAT,
    rf_gap_wall_ht      FLOAT,
    rf_wall_ht          FLOAT,
    foul_territory      VARCHAR(10)     CHECK (foul_territory IN ('Small','Medium','Large')),
    capacity            INTEGER,
    coordinates_lat     FLOAT,
    coordinates_lon     FLOAT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (venue_id, season)
);

COMMENT ON TABLE  raw.venues IS 'All MLB venues including international sites. Must be seeded before any game data.';
COMMENT ON COLUMN raw.venues.elevation_ft IS 'Feet above sea level. Drives pitch movement and batted ball carry adjustments.';
COMMENT ON COLUMN raw.venues.foul_territory IS 'Qualitative size — affects popup out conversion rates in simulation.';

-- =============================================================================
-- RAW.TEAMS
-- =============================================================================

CREATE TABLE raw.teams (
    team_id             INTEGER         NOT NULL,
    season              INTEGER         NOT NULL,
    team_name           VARCHAR(100)    NOT NULL,
    team_abbrev         VARCHAR(5)      NOT NULL,
    league              CHAR(2)         NOT NULL CHECK (league IN ('AL','NL')),
    division            VARCHAR(10)     NOT NULL
                            CHECK (division IN ('AL East','AL Central','AL West',
                                                'NL East','NL Central','NL West')),
    venue_id            INTEGER         NOT NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (team_id, season),
    FOREIGN KEY (venue_id, season) REFERENCES raw.venues (venue_id, season)
);

CREATE INDEX idx_teams_venue     ON raw.teams(venue_id);
CREATE INDEX idx_teams_league    ON raw.teams(league);
CREATE INDEX idx_teams_division  ON raw.teams(division);

-- =============================================================================
-- RAW.PLAYERS
-- =============================================================================

CREATE TABLE raw.players (
    player_id           INTEGER         PRIMARY KEY,
    full_name           VARCHAR(100)    NOT NULL,
    first_name          VARCHAR(50)     NOT NULL,
    last_name           VARCHAR(50)     NOT NULL,
    birth_date          DATE,
    bats                CHAR(1)         NOT NULL CHECK (bats IN ('L','R','S')),
    throws              CHAR(1)         NOT NULL CHECK (throws IN ('L','R','S')),
    primary_position    VARCHAR(5)      NOT NULL,
    height_inches       INTEGER,
    weight_lbs          INTEGER,
    mlb_debut_date      DATE,
    active              BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_players_last_name      ON raw.players(last_name);
CREATE INDEX idx_players_full_name_trgm ON raw.players USING GIN (full_name gin_trgm_ops);
CREATE INDEX idx_players_active         ON raw.players(active) WHERE active = TRUE;
CREATE INDEX idx_players_position       ON raw.players(primary_position);

COMMENT ON COLUMN raw.players.primary_position IS 'Default position. Actual game position tracked per-game in raw.game_lineups.';

-- =============================================================================
-- RAW.MANAGERS
-- =============================================================================

CREATE TABLE raw.managers (
    manager_id          INTEGER         NOT NULL,
    season              INTEGER         NOT NULL,
    full_name           VARCHAR(100)    NOT NULL,
    team_id             INTEGER         NOT NULL,
    season_start        DATE,
    season_end          DATE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (manager_id, season),
    FOREIGN KEY (team_id, season) REFERENCES raw.teams(team_id, season)
);

CREATE INDEX idx_managers_team   ON raw.managers(team_id);
CREATE INDEX idx_managers_active ON raw.managers(team_id) WHERE season_end IS NULL;

COMMENT ON COLUMN raw.managers.season_end IS 'NULL = currently active with this team.';

-- =============================================================================
-- RAW.GAMES
-- =============================================================================

CREATE TABLE raw.games (
    game_pk                 INTEGER         PRIMARY KEY,
    season                  INTEGER         NOT NULL,
    game_date               DATE            NOT NULL,
    game_type               VARCHAR(2)      NOT NULL
                                CHECK (game_type IN ('R','P','S','A','D','F','L','W')),
    status                  VARCHAR(20)     NOT NULL
                                CHECK (status IN ('Preview','Warmup','Pre-Game','Live',
                                                  'Final','Postponed','Suspended','Cancelled')),
    venue_id                INTEGER         NOT NULL,
    home_team_id            INTEGER         NOT NULL,
    away_team_id            INTEGER         NOT NULL,
    home_manager_id         INTEGER,
    away_manager_id         INTEGER,
    home_score_final        INTEGER,
    away_score_final        INTEGER,
    innings_played          INTEGER,
    home_hits               INTEGER,
    away_hits               INTEGER,
    home_errors             INTEGER,
    away_errors             INTEGER,
    inning_scores           JSONB,
    winning_pitcher_id      INTEGER         REFERENCES raw.players(player_id),
    losing_pitcher_id       INTEGER         REFERENCES raw.players(player_id),
    save_pitcher_id         INTEGER         REFERENCES raw.players(player_id),
    attendance              INTEGER,
    weather_temp            INTEGER,
    weather_condition       VARCHAR(50),
    wind_speed              INTEGER,
    wind_direction          VARCHAR(20),
    game_duration_minutes   INTEGER,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    FOREIGN KEY (venue_id, season)       REFERENCES raw.venues(venue_id, season),
    FOREIGN KEY (home_team_id, season)   REFERENCES raw.teams(team_id, season),
    FOREIGN KEY (away_team_id, season)   REFERENCES raw.teams(team_id, season)
);

CREATE INDEX idx_games_date         ON raw.games(game_date);
CREATE INDEX idx_games_home_team    ON raw.games(home_team_id);
CREATE INDEX idx_games_away_team    ON raw.games(away_team_id);
CREATE INDEX idx_games_status       ON raw.games(status);
CREATE INDEX idx_games_venue        ON raw.games(venue_id);
CREATE INDEX idx_games_date_status  ON raw.games(game_date, status);
CREATE INDEX idx_games_type         ON raw.games(game_type);

COMMENT ON COLUMN raw.games.inning_scores IS 'JSONB linescore. Keys "home"/"away", each a 0-indexed array (index 0 = inning 1). Extra innings appended.';
COMMENT ON COLUMN raw.games.wind_direction IS '"In"=blowing in from outfield. "Out"=blowing out toward fences.';

-- =============================================================================
-- RAW.GAME_LINEUPS
-- =============================================================================

CREATE TABLE raw.game_lineups (
    id                  BIGSERIAL       PRIMARY KEY,
    game_pk             INTEGER         NOT NULL REFERENCES raw.games(game_pk),
    season              INTEGER         NOT NULL,
    team_id             INTEGER         NOT NULL,
    player_id           INTEGER         NOT NULL REFERENCES raw.players(player_id),
    batting_order       INTEGER         CHECK (batting_order BETWEEN 1 AND 9),
    position_code       VARCHAR(5)      NOT NULL,
    is_starter          BOOLEAN         NOT NULL DEFAULT TRUE,
    sequence            INTEGER         NOT NULL DEFAULT 1,
    entered_inning      INTEGER,
    entered_at_bat      INTEGER,
    pinch_role          VARCHAR(5)      CHECK (pinch_role IN ('PH','PR','DEF')),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_game_lineup_slot UNIQUE (game_pk, team_id, player_id, sequence),
    FOREIGN KEY (team_id, season) REFERENCES raw.teams(team_id, season)
);

CREATE INDEX idx_game_lineups_game_team ON raw.game_lineups(game_pk, team_id);
CREATE INDEX idx_game_lineups_player    ON raw.game_lineups(player_id);
CREATE INDEX idx_game_lineups_game      ON raw.game_lineups(game_pk);
CREATE INDEX idx_game_lineups_starters  ON raw.game_lineups(game_pk, team_id) WHERE is_starter = TRUE;
CREATE INDEX idx_game_lineups_pinch     ON raw.game_lineups(game_pk) WHERE pinch_role IS NOT NULL;

COMMENT ON COLUMN raw.game_lineups.sequence   IS '1=original entry. Increments each time a slot changes occupant.';
COMMENT ON COLUMN raw.game_lineups.pinch_role IS 'PH=Pinch Hitter, PR=Pinch Runner, DEF=Defensive replacement.';

-- =============================================================================
-- RAW.PITCHES
-- Direct 1:1 ingestion target for Statcast data. Never modified after write.
-- ~700K rows/season. pitch_type stored for reference only — similarity engine
-- uses GMM components in derived schema.
-- =============================================================================

CREATE TABLE raw.pitches (

    -- Natural key
    game_pk                     INTEGER     NOT NULL REFERENCES raw.games(game_pk),
    at_bat_number               INTEGER     NOT NULL,
    pitch_number                INTEGER     NOT NULL,

    -- Game context (denormalized for query convenience)
    game_date                   DATE        NOT NULL,
    season                      INTEGER     NOT NULL,
    venue_id                    INTEGER     NOT NULL,
    venue                       VARCHAR(100),
    home_id                     INTEGER     NOT NULL,
    home_team                   VARCHAR(10),
    away_id                     INTEGER     NOT NULL,
    away_team                   VARCHAR(10),
    home_manager_id             INTEGER     NOT NULL,
    home_manager_name           VARCHAR(100),
    away_manager_id             INTEGER     NOT NULL,
    away_manager_name           VARCHAR(100),

    -- Inning state
    inning                      SMALLINT    NOT NULL CHECK (inning BETWEEN 1 AND 30),
    inning_topbot               VARCHAR(6)  NOT NULL CHECK (inning_topbot IN ('Top','Bot')),

    -- Pitcher
    pitcher                     INTEGER     NOT NULL REFERENCES raw.players(player_id),
    p_throws                    CHAR(1)     NOT NULL CHECK (p_throws IN ('L','R')),

    -- Batter
    batter                      INTEGER     NOT NULL REFERENCES raw.players(player_id),
    stand                       CHAR(1)     NOT NULL CHECK (stand    IN ('L','R','S')),
    bat_hand                    CHAR(1)     NOT NULL CHECK (bat_hand IN ('L','R','S')),

    -- Score state (pre-pitch)
    home_score                  SMALLINT    NOT NULL DEFAULT 0,
    away_score                  SMALLINT    NOT NULL DEFAULT 0,
    bat_score                   SMALLINT    NOT NULL DEFAULT 0,
    fld_score                   SMALLINT    NOT NULL DEFAULT 0,

    -- Count (pre-pitch)
    balls                       SMALLINT    NOT NULL CHECK (balls   BETWEEN 0 AND 3),
    strikes                     SMALLINT    NOT NULL CHECK (strikes BETWEEN 0 AND 2),
    outs                        SMALLINT    NOT NULL CHECK (outs    BETWEEN 0 AND 2),

    -- Pitch result
    type                        CHAR(2),
    description                 VARCHAR(100),
    pitch_type                  VARCHAR(5),
    pitch_name                  VARCHAR(50),
    events                      VARCHAR(50),

    -- Strike zone
    sz_top                      FLOAT,
    sz_bot                      FLOAT,

    -- Physics
    release_speed               FLOAT,
    end_speed                   FLOAT,
    release_pos_x               FLOAT,
    release_pos_y               FLOAT,
    release_pos_z               FLOAT,
    vx0                         FLOAT,
    vy0                         FLOAT,
    vz0                         FLOAT,
    ax                          FLOAT,
    ay                          FLOAT,
    az                          FLOAT,

    -- Movement
    pfx_x                       FLOAT,
    pfx_z                       FLOAT,
    plate_x                     FLOAT,
    plate_z                     FLOAT,
    x                           FLOAT,
    y                           FLOAT,
    release_spin_rate           INTEGER,
    spin_axis                   INTEGER     CHECK (spin_axis IS NULL OR spin_axis BETWEEN 0 AND 360),
    break_angle                 FLOAT,
    break_length                FLOAT,
    break_y                     FLOAT,
    break_vertical              FLOAT,
    break_vertical_induced      FLOAT,
    break_horizontal            FLOAT,
    zone                        SMALLINT    CHECK (zone IS NULL OR zone BETWEEN 1 AND 14),
    release_extension           FLOAT,

    -- Batted ball
    launch_speed                FLOAT       CHECK (launch_speed IS NULL OR launch_speed BETWEEN 0 AND 130),
    launch_angle                FLOAT       CHECK (launch_angle IS NULL OR launch_angle BETWEEN -90 AND 90),
    hit_distance_sc             FLOAT,
    hc_x                        FLOAT,
    hc_y                        FLOAT,
    spray_angle                 FLOAT,
    bb_type                     VARCHAR(20),
    hit_location                FLOAT,
    des                         TEXT,

    -- Baserunners pre-pitch
    on_1b                       INTEGER,
    on_2b                       INTEGER,
    on_3b                       INTEGER,

    -- Baserunners post-pitch
    post_on_1b                  INTEGER,
    post_on_2b                  INTEGER,
    post_on_3b                  INTEGER,

    -- Fielders
    fielder_2                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_3                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_4                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_5                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_6                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_7                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_8                   INTEGER     NOT NULL REFERENCES raw.players(player_id),
    fielder_9                   INTEGER     NOT NULL REFERENCES raw.players(player_id),

    -- Outcome counts
    runs_on_pitch               SMALLINT    NOT NULL DEFAULT 0,
    outs_on_pitch               SMALLINT    NOT NULL DEFAULT 0,
    rbis_on_pitch               SMALLINT    NOT NULL DEFAULT 0,
    earned_runs_on_pitch        SMALLINT    NOT NULL DEFAULT 0,

    -- Runner scoring flags
    runner_1b_scored            BOOLEAN     NOT NULL DEFAULT FALSE,
    runner_2b_scored            BOOLEAN     NOT NULL DEFAULT FALSE,
    runner_3b_scored            BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Stolen base
    sb_attempt_2b               BOOLEAN     NOT NULL DEFAULT FALSE,
    sb_attempt_3b               BOOLEAN     NOT NULL DEFAULT FALSE,
    sb_attempt_home             BOOLEAN     NOT NULL DEFAULT FALSE,
    sb_success_2b               BOOLEAN     NOT NULL DEFAULT FALSE,
    sb_success_3b               BOOLEAN     NOT NULL DEFAULT FALSE,
    sb_success_home             BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Substitution flags
    passed_ball_wild_pitch      BOOLEAN     NOT NULL DEFAULT FALSE,
    pinch_hitter                BOOLEAN     NOT NULL DEFAULT FALSE,
    pinch_runner                BOOLEAN     NOT NULL DEFAULT FALSE,
    pitcher_sub                 BOOLEAN     NOT NULL DEFAULT FALSE,
    defensive_sub               BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Fielding detail
    fielded_by                  INTEGER,
    fielding_error              INTEGER,
    dropped_ball                INTEGER,
    of_assist                   INTEGER,
    field_assist_1              INTEGER,
    field_assist_2              INTEGER,
    field_assist_3              INTEGER,
    field_assist_4              INTEGER,
    field_assist_5              INTEGER,
    field_putout_1              INTEGER,
    field_putout_2              INTEGER,
    field_putout_3              INTEGER,
    throwing_error_1            INTEGER,
    throwing_error_2            INTEGER,

    -- Quality flag (auto-set by trigger)
    data_quality_flag           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (game_pk, at_bat_number, pitch_number),

    FOREIGN KEY (venue_id, season)          REFERENCES raw.venues(venue_id, season),
    FOREIGN KEY (home_id, season)           REFERENCES raw.teams(team_id, season),
    FOREIGN KEY (away_id, season)           REFERENCES raw.teams(team_id, season),
    FOREIGN KEY (home_manager_id, season)   REFERENCES raw.managers(manager_id, season),
    FOREIGN KEY (away_manager_id, season)   REFERENCES raw.managers(manager_id, season),

    CONSTRAINT chk_pitches_sb_logic CHECK (
        (NOT sb_success_2b   OR sb_attempt_2b)   AND
        (NOT sb_success_3b   OR sb_attempt_3b)   AND
        (NOT sb_success_home OR sb_attempt_home)
    )
);

CREATE INDEX idx_pitches_pitcher        ON raw.pitches(pitcher);
CREATE INDEX idx_pitches_batter         ON raw.pitches(batter);
CREATE INDEX idx_pitches_game_date      ON raw.pitches(game_date);
CREATE INDEX idx_pitches_venue          ON raw.pitches(venue_id);
CREATE INDEX idx_pitches_pitch_type     ON raw.pitches(pitch_type);
CREATE INDEX idx_pitches_events         ON raw.pitches(events) WHERE events IS NOT NULL;
CREATE INDEX idx_pitches_pitcher_season ON raw.pitches(pitcher, game_date);
CREATE INDEX idx_pitches_batter_season  ON raw.pitches(batter, game_date);
CREATE INDEX idx_pitches_pitcher_type   ON raw.pitches(pitcher, pitch_type);
CREATE INDEX idx_pitches_sb_attempts    ON raw.pitches(pitcher, fielder_2)
                                            WHERE sb_attempt_2b OR sb_attempt_3b OR sb_attempt_home;
CREATE INDEX idx_pitches_subs           ON raw.pitches(game_pk)
                                            WHERE pitcher_sub OR pinch_hitter OR pinch_runner OR defensive_sub;
CREATE INDEX idx_pitches_batted_balls   ON raw.pitches(batter, pitcher, game_date) WHERE type = 'X';
CREATE INDEX idx_pitches_quality_clean  ON raw.pitches(game_date) WHERE data_quality_flag = FALSE;
CREATE INDEX idx_pitches_count_state    ON raw.pitches(balls, strikes, outs);

COMMENT ON TABLE  raw.pitches IS 'Direct Statcast ingestion target. Never modified after write. ~700K rows/season.';
COMMENT ON COLUMN raw.pitches.pitch_type IS 'Statcast classification stored for reference/audit only. Similarity engine uses GMM components in derived.pitcher_season_metrics.';
COMMENT ON COLUMN raw.pitches.data_quality_flag IS 'Auto-set by insert trigger. Flagged rows excluded from sim pool but retained for audit.';

-- =============================================================================
-- SIM.LINEUP_STATE
-- =============================================================================

CREATE TABLE sim.lineup_state (
    session_id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    game_pk                 INTEGER,
    is_live_game            BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Required JSON keys: inning, half, outs, balls, strikes,
    --   home_score, away_score, batting_team_id, fielding_team_id,
    --   on_1b, on_2b, on_3b, current_batter_id, current_pitcher_id,
    --   home_lineup, away_lineup, home_bullpen, away_bullpen,
    --   home_bench, away_bench, play_history
    game_state              JSONB       NOT NULL,

    selected_at_bat         INTEGER,
    selected_pitch          INTEGER,

    -- Keys: type (PH|PR|SP|DEF), out_player_id, in_player_id, position, rationale
    pending_substitution    JSONB,

    -- Keys: home_avg_score, away_avg_score, home_win_pct, away_win_pct,
    --   per_player_stats, per_pitcher_stats
    simulation_results      JSONB,
    iterations_run          INTEGER     NOT NULL DEFAULT 0,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours')
);

CREATE INDEX idx_lineup_state_game_pk        ON sim.lineup_state(game_pk) WHERE game_pk IS NOT NULL;
CREATE INDEX idx_lineup_state_live           ON sim.lineup_state(is_live_game, updated_at) WHERE is_live_game = TRUE;
CREATE INDEX idx_lineup_state_expires        ON sim.lineup_state(expires_at);
CREATE INDEX idx_lineup_state_game_state_gin ON sim.lineup_state USING GIN(game_state);

COMMENT ON TABLE  sim.lineup_state IS 'Ephemeral simulation sessions. Expire after 24h unless is_live_game=TRUE.';
COMMENT ON COLUMN sim.lineup_state.selected_at_bat IS 'Set when user clicks historical play. NULL = current/live state.';
COMMENT ON COLUMN sim.lineup_state.pending_substitution IS 'Managerial move staged for what-if simulation. Not committed to game_state until confirmed.';

-- =============================================================================
-- TRIGGERS
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

CREATE TRIGGER trg_venues_updated_at        BEFORE UPDATE ON raw.venues        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_teams_updated_at         BEFORE UPDATE ON raw.teams         FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_players_updated_at       BEFORE UPDATE ON raw.players       FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_managers_updated_at      BEFORE UPDATE ON raw.managers      FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_games_updated_at         BEFORE UPDATE ON raw.games         FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_game_lineups_updated_at  BEFORE UPDATE ON raw.game_lineups  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_lineup_state_updated_at  BEFORE UPDATE ON sim.lineup_state  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE FUNCTION raw.flag_pitch_quality()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (
        (NEW.release_speed IS NOT NULL AND (NEW.release_speed > 102 OR NEW.release_speed < 60)) OR
        (NEW.launch_speed  IS NOT NULL AND NEW.launch_speed > 125) OR
        (NEW.break_vertical_induced IS NOT NULL AND
            (NEW.break_vertical_induced < -25 OR NEW.break_vertical_induced > 25))
    ) THEN
        NEW.data_quality_flag = TRUE;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_pitches_quality_flag
    BEFORE INSERT ON raw.pitches
    FOR EACH ROW EXECUTE FUNCTION raw.flag_pitch_quality();

-- =============================================================================
-- UTILITY FUNCTIONS
-- =============================================================================

CREATE OR REPLACE FUNCTION sim.purge_expired_sessions()
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE deleted_count INTEGER;
BEGIN
    DELETE FROM sim.lineup_state WHERE expires_at < NOW() AND is_live_game = FALSE;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

COMMENT ON FUNCTION sim.purge_expired_sessions IS 'Deletes expired non-live sessions. Schedule nightly.';

-- =============================================================================
-- OPERATIONAL VIEW
-- =============================================================================

CREATE OR REPLACE VIEW sim.live_games AS
SELECT
    g.game_pk, g.game_date, g.status,
    g.home_team_id, g.away_team_id,
    g.home_score_final, g.away_score_final,
    g.venue_id,
    ls.session_id,
    ls.updated_at   AS last_sim_update,
    ls.iterations_run
FROM  raw.games g
LEFT  JOIN sim.lineup_state ls ON ls.game_pk = g.game_pk AND ls.is_live_game = TRUE
WHERE g.status = 'Live' AND g.game_date = CURRENT_DATE;

COMMENT ON VIEW sim.live_games IS 'Currently live games with their active simulation session. Consumed by 30-second polling loop.';
