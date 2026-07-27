-- =============================================================================
-- BASEBALL SIMULATION SYSTEM — PostgreSQL Schema
-- Engine: PostgreSQL 16+
-- Schemas: raw (ingested data), sim (session/operational state)
--
-- Snapshot authoritative only through migration 0013; Alembic
-- (db/migrations/versions/) is the source of truth.
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
    -- SIM-441: `active` was dropped (migration 0017). It was NOT NULL DEFAULT TRUE
    -- with a partial index, never written and never updated — so it read TRUE for
    -- every player ever ingested, the "partial" index covered 100% of rows, and a
    -- SQL-console user filtering `WHERE active = TRUE` silently got retired players.
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_players_last_name      ON raw.players(last_name);
CREATE INDEX idx_players_full_name_trgm ON raw.players USING GIN (full_name gin_trgm_ops);
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
    -- SIM-086: Nullable.  The live pipeline upserts game rows from the schedule API
    -- before venue assignment is published (international/spring-training games).
    -- Pre-SIM-086 the live pipeline silently dropped these games because venue_id=0
    -- raised an FK violation.  pipeline/etl/venue_backfill_job.py fills NULL rows
    -- once the MLB API publishes the venue.
    venue_id                INTEGER,
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
-- RAW.SPRINT_SPEED
-- =============================================================================

CREATE TABLE raw.sprint_speed (
    player_id           INTEGER     NOT NULL REFERENCES raw.players(player_id),
    season              INTEGER     NOT NULL,
    sprint_speed        FLOAT       NOT NULL,     -- ft/s, Savant's 2-best-run avg
    competitive_runs    INTEGER     NOT NULL,     -- Savant's sample count
    bolts               INTEGER,                  -- runs ≥ 30 ft/s (optional)
    hp_to_1b            FLOAT,                    -- home-to-first avg (optional)
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, season)
);

CREATE INDEX idx_sprint_speed_season ON raw.sprint_speed(season);

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
    -- SIM-441: nullable (migration 0017), matching raw.games. The loader no
    -- longer fabricates a manager when a coaches roster carries no
    -- NTRM/MNGR/COAB job code; NOT NULL here turned "we could not identify the
    -- manager" into a NotNullViolation that rolled back the whole game's pitch
    -- insert. Losing a full game of pitch-by-pitch data over two denormalised
    -- convenience columns is the worse outcome. The composite FKs below are
    -- unaffected — Postgres does not enforce an FK when part of the key is NULL.
    home_manager_id             INTEGER,
    home_manager_name           VARCHAR(100),
    away_manager_id             INTEGER,
    away_manager_name           VARCHAR(100),

    -- Inning state
    inning                      SMALLINT    NOT NULL CHECK (inning BETWEEN 1 AND 30),
    inning_topbot               VARCHAR(6)  NOT NULL CHECK (inning_topbot IN ('Top','Bot')),

    -- Pitcher
    pitcher                     INTEGER     NOT NULL REFERENCES raw.players(player_id),
    p_throws                    CHAR(1)     NOT NULL CHECK (p_throws IN ('L','R')),

    -- Batter
    --
    -- SIM-440 — CANONICAL DEFINITION of the two handedness columns. Read this
    -- before using either. Several places in this repo documented them the
    -- WRONG WAY ROUND for years; the statement below is measured, not assumed
    -- (docs/data_quality/2026-05-20-bat-side-coverage.md, live DB, 2017-2025).
    --
    --   stand    = the side the batter ACTUALLY BATTED FROM in this plate
    --              appearance. Parsed from the feed/live in-game context
    --              (playEvents[].offense.batter.batSide.code), which MLB has
    --              already resolved against the pitcher for switch hitters.
    --              MEASURED: 'S' on 0 rows in every season. Use THIS whenever
    --              you need "which side did he swing from" — spray-angle sign,
    --              platoon splits, pool pre-filter keys, tile keys.
    --
    --   bat_hand = the batter's ROSTER-DECLARED side, from /api/v1/people/{id}
    --              batSide.code. Constant for a player across his whole career,
    --              and 'S' for every switch hitter. MEASURED: 'S' on 10.4-13.3%
    --              of rows in EVERY season. It is NOT per-PA and NOT resolved.
    --
    -- The CHECK below still allows 'S' on `stand` only because the constraint
    -- predates the measurement; nothing has ever written it.
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

    -- Runner out advancing (thrown out attempting extra base, NOT stolen bases)
    -- Parsed from play description during ETL.  Required to split
    -- baserunner attempt rate vs success rate in derived metrics.
    runner_1b_out_advancing     BOOLEAN     NOT NULL DEFAULT FALSE,
    runner_2b_out_advancing     BOOLEAN     NOT NULL DEFAULT FALSE,
    runner_3b_out_advancing     BOOLEAN     NOT NULL DEFAULT FALSE,

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
    -- SIM-441: TRUE when a pitch recorded MORE fielding credits than the slots
    -- above can hold, so at least one was dropped. Practically only reachable on
    -- a multi-runner rundown. Modelling the 6th+ assist is not worth a column
    -- each, but the overflow must be visible rather than silent so a consumer can
    -- exclude or measure those plays instead of under-counting assists.
    field_assist_6_plus         BOOLEAN     NOT NULL DEFAULT FALSE,

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
-- SIM-088: idx_pitches_pitch_type intentionally absent.
-- pitch_type is stored for reference/audit only — the similarity engine uses
-- GMM components in derived.pitcher_season_metrics, not the Statcast string.
-- No hot path filters by pitch_type, so a single-column index added ~15 MB of
-- write overhead per season for zero query benefit.  Re-add CONCURRENTLY for
-- ad-hoc debugging if ever needed; (pitcher, pitch_type) compound stays.
CREATE INDEX idx_pitches_events         ON raw.pitches(events) WHERE events IS NOT NULL;
CREATE INDEX idx_pitches_pitcher_season ON raw.pitches(pitcher, game_date);
-- SIM-089: Composite partial index for the player-profile computor's hot path
-- (one query per pitcher/season filtered by data_quality_flag).  Without this,
-- the planner picks idx_pitches_pitcher_season above and applies the
-- data_quality_flag filter post-scan, which scans every pitch for the pitcher
-- across all seasons.  With this index a 3,000-pitch pitcher's season fetch is
-- < 50 ms.
CREATE INDEX idx_pitches_pitcher_season_clean
    ON raw.pitches(pitcher, season)
    WHERE data_quality_flag = FALSE;
CREATE INDEX idx_pitches_batter_season  ON raw.pitches(batter, game_date);
CREATE INDEX idx_pitches_pitcher_type   ON raw.pitches(pitcher, pitch_type);
CREATE INDEX idx_pitches_sb_attempts    ON raw.pitches(pitcher, fielder_2)
                                            WHERE sb_attempt_2b OR sb_attempt_3b OR sb_attempt_home;
CREATE INDEX idx_pitches_subs           ON raw.pitches(game_pk)
                                            WHERE pitcher_sub OR pinch_hitter OR pinch_runner OR defensive_sub;
CREATE INDEX idx_pitches_batted_balls   ON raw.pitches(batter, pitcher, game_date) WHERE type = 'X';
CREATE INDEX idx_pitches_quality_clean  ON raw.pitches(game_date) WHERE data_quality_flag = FALSE;
CREATE INDEX idx_pitches_count_state    ON raw.pitches(balls, strikes, outs);
-- SIM-085: Composite situation index for the situation-to-situation engine (SIM-070).
-- Partial — flagged rows are excluded from the simulation pool anyway.
-- Without this index, situation similarity queries fall back to a sequential scan
-- over ~700K rows per season.
CREATE INDEX idx_pitches_situation
    ON raw.pitches(inning, outs, balls, strikes, on_1b, on_2b, on_3b)
    WHERE data_quality_flag = FALSE;

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

-- SIM-082: Unique partial index required for ON CONFLICT (game_pk) WHERE is_live_game=TRUE
-- in _upsert_lineup_state().  Without this index PostgreSQL raises a constraint error and
-- live game state is never persisted.
CREATE UNIQUE INDEX idx_lineup_state_live_game
    ON sim.lineup_state(game_pk)
    WHERE is_live_game = TRUE;

COMMENT ON TABLE  sim.lineup_state IS 'Ephemeral simulation sessions. Expire after 24h unless is_live_game=TRUE.';
COMMENT ON COLUMN sim.lineup_state.selected_at_bat IS 'Set when user clicks historical play. NULL = current/live state.';
COMMENT ON COLUMN sim.lineup_state.pending_substitution IS 'Managerial move staged for what-if simulation. Not committed to game_state until confirmed.';

-- =============================================================================
-- RAW.ETL_DATA_FRESHNESS
-- SIM-083: Moved from FRESHNESS_TABLE_DDL string constant in etl_historical_loader.py.
-- Tracks the last loaded game per pitcher/batter so stale player profiles can
-- be detected without scanning all of raw.pitches.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.etl_data_freshness (
    entity_type  VARCHAR(10)  NOT NULL,   -- 'pitcher' or 'batter'
    entity_id    INTEGER      NOT NULL,
    last_game_pk INTEGER      NOT NULL,
    last_date    DATE         NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY  (entity_type, entity_id)
);

COMMENT ON TABLE raw.etl_data_freshness IS
    'Tracks last loaded game per pitcher/batter. Used to detect stale profiles.';

-- =============================================================================
-- RAW.GAME_ODDS
-- SIM-083: Moved from GAME_ODDS_DDL string constant in live_ingestion_pipeline.py.
-- Odds snapshots per game.  See SIM-133 for the CLV-enabling column extensions
-- (book, line_type, market_type, is_sharp_book) applied as migration 0003.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.game_odds (
    id              BIGSERIAL       PRIMARY KEY,
    game_pk         INTEGER         NOT NULL REFERENCES raw.games(game_pk),
    fetched_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    source          VARCHAR(50)     NOT NULL DEFAULT 'consensus',
    is_mock         BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Moneyline (American odds: negative = favourite)
    home_ml         INTEGER,        -- e.g. -150
    away_ml         INTEGER,        -- e.g. +130

    -- Run line / spread
    home_spread     FLOAT,          -- e.g. -1.5
    home_spread_ml  INTEGER,        -- e.g. -110
    away_spread     FLOAT,          -- e.g. +1.5
    away_spread_ml  INTEGER,        -- e.g. -110

    -- Total (over/under)
    total_line      FLOAT,          -- e.g.  8.5
    over_ml         INTEGER,        -- e.g. -110
    under_ml        INTEGER,        -- e.g. -110

    -- SIM-133: CLV columns (added via migration 0003)
    book            VARCHAR(50)     NOT NULL DEFAULT 'consensus',
    line_type       VARCHAR(20)     NOT NULL DEFAULT 'current'
                        CHECK (line_type IN ('opening','current','closing','bet_placement')),
    market_type     VARCHAR(20)     NOT NULL DEFAULT 'moneyline'
                        CHECK (market_type IN ('moneyline','runline','total')),
    is_sharp_book   BOOLEAN         NOT NULL DEFAULT FALSE,
    -- SIM-092: SHA-256 of the concatenated odds payload.  Application code
    -- (_persist_odds) computes this and uses
    --   ON CONFLICT (game_pk, source, odds_hash) DO NOTHING
    -- so identical successive snapshots are no-ops.  Pre-SIM-092 rows have
    -- NULL hashes; backfill is left to a future cleanup ticket.
    odds_hash       VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_game_odds_game_pk
    ON raw.game_odds(game_pk, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_game_odds_line_type
    ON raw.game_odds(game_pk, line_type);

-- SIM-092: Partial unique index on hashed payload — only enforced on rows
-- with a non-NULL hash so legacy NULL-hash rows do not violate it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_game_odds_dedup
    ON raw.game_odds(game_pk, source, odds_hash)
    WHERE odds_hash IS NOT NULL;

COMMENT ON TABLE raw.game_odds IS
    'Odds snapshots per game. source=mock until Phase 7 real provider integration.';
COMMENT ON COLUMN raw.game_odds.line_type IS
    'opening=first line posted; current=live snapshot; closing=final line before first_pitch; bet_placement=line at time a bet was placed.';
COMMENT ON COLUMN raw.game_odds.is_sharp_book IS
    'TRUE for sharp/respected books (Pinnacle, Circa) whose closing lines are used as CLV reference.';

-- =============================================================================
-- RAW.PROP_ODDS
-- Player prop odds snapshots.  Parallel to raw.game_odds.
-- line_type='opening' captured by nightly job (SIM-138) when starter announced.
--
-- SIM-134: prop_type renamed to prop_stat with CHECK constraint.
--          Betting Analyst (Agent 8) confirmed 7-value scope — see CHANGES.md.
--          Migration 0004 applies these changes to live databases.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.prop_odds (
    id              BIGSERIAL       PRIMARY KEY,
    game_pk         INTEGER         NOT NULL REFERENCES raw.games(game_pk),
    player_id       INTEGER         NOT NULL REFERENCES raw.players(player_id),
    fetched_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    source          VARCHAR(50)     NOT NULL DEFAULT 'consensus',
    is_mock         BOOLEAN         NOT NULL DEFAULT TRUE,
    -- SIM-134: renamed from prop_type; CHECK constraint enforces known markets.
    -- 7 values confirmed by Betting Analyst (Agent 8).
    -- innings_pitched deferred to Phase 4 (requires pitch-count model).
    prop_stat       VARCHAR(30)     NOT NULL
                        CHECK (prop_stat IN (
                            'strikeouts',
                            'hits',
                            'home_runs',
                            'earned_runs',
                            'walks',
                            'total_bases',
                            'rbis'
                        )),
    line            FLOAT           NOT NULL,
    over_ml         INTEGER,
    under_ml        INTEGER,
    book            VARCHAR(50)     NOT NULL DEFAULT 'consensus',
    line_type       VARCHAR(20)     NOT NULL DEFAULT 'current'
                        CHECK (line_type IN ('opening','current','closing','bet_placement')),
    is_sharp_book   BOOLEAN         NOT NULL DEFAULT FALSE,
    -- SIM-340: SHA-256 fingerprint of the prop payload for write-time dedup.
    -- Applied via Alembic migration 0013. Mirrors raw.game_odds.odds_hash.
    odds_hash       VARCHAR(64)
);

-- SIM-134: compound index supports per-player-per-prop time-series queries
-- and efficient CLV lookups (opening vs. closing for same game/player/stat).
-- Replaces the weaker (game_pk, player_id)-only index from migration 0003.
CREATE INDEX IF NOT EXISTS idx_prop_odds_game_player
    ON raw.prop_odds(game_pk, player_id, prop_stat, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_prop_odds_line_type
    ON raw.prop_odds(game_pk, line_type);
-- SIM-340: write-time dedup index (partial; legacy NULL-hash rows tolerated).
-- Applied via Alembic migration 0013. Mirrors idx_game_odds_dedup (SIM-092).
CREATE UNIQUE INDEX IF NOT EXISTS idx_prop_odds_dedup
    ON raw.prop_odds(game_pk, player_id, source, odds_hash)
    WHERE odds_hash IS NOT NULL;

COMMENT ON TABLE raw.prop_odds IS
    'Player prop odds snapshots. Opening lines captured nightly when starter is announced (SIM-138). prop_stat CHECK constraint enforces 7 known markets (SIM-134).';

-- =============================================================================
-- RAW.ETL_ERRORS
-- SIM-093: Audit trail for the ETL hard-error path.  Originally referenced
-- by an etl_historical_loader.py docstring as the destination for skipped
-- rows, but the table did not exist before SIM-093.
--
-- Loose-coupled by design: NO FK to raw.games so an error row survives
-- when the game itself never landed (eg. FK prereq missing).
--
-- error_type='HARD'  → row was skipped entirely (validator rejected it)
-- error_type='WARN'  → row inserted with data_quality_flag=TRUE, but the
--                       reason set is captured here for full provenance.
--                       Reserved for future use; current loader writes HARD only.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.etl_errors (
    id              BIGSERIAL       PRIMARY KEY,
    game_pk         INTEGER,
    at_bat_number   INTEGER,
    pitch_number    INTEGER,
    error_type      VARCHAR(10)     NOT NULL
                        CHECK (error_type IN ('HARD','WARN')),
    error_messages  TEXT[]          NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- SIM-441: the ledger insert had no conflict clause and the table only a
-- surrogate key, so a game whose rows ALL hard-error (and therefore writes no
-- pitch rows, so `_game_already_loaded` stays False) was re-processed every
-- night, appending a full duplicate error set each time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_etl_errors_natural_key
    ON raw.etl_errors (game_pk, at_bat_number, pitch_number, error_type);

-- SIM-441: terminal per-game ETL outcome. `_game_already_loaded` keys on pitch
-- rows existing, which cannot tell "never loaded" from "loaded and legitimately
-- produced zero pitches" — so a pitch-less or totally-failing game was re-fetched
-- forever. outcome='empty' stops that. reload_game() / reload=True ignore this
-- table by design: that is the escape hatch after a parser fix.
CREATE TABLE IF NOT EXISTS raw.etl_game_ingest (
    game_pk         INTEGER         PRIMARY KEY,
    season          SMALLINT        NOT NULL,
    outcome         VARCHAR(16)     NOT NULL
                        CHECK (outcome IN ('loaded','empty','failed')),
    pitch_rows      INTEGER         NOT NULL DEFAULT 0,
    skipped_rows    INTEGER         NOT NULL DEFAULT 0,
    attempts        INTEGER         NOT NULL DEFAULT 1,
    last_error      TEXT,
    first_seen_at   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_etl_errors_game_pk
    ON raw.etl_errors(game_pk, created_at);

CREATE INDEX IF NOT EXISTS idx_etl_errors_recent
    ON raw.etl_errors(created_at DESC);

COMMENT ON TABLE raw.etl_errors IS
    'One row per pitch skipped by the ETL hard-error path. Audit trail for HistoricalDataLoader.reprocess_errored_games(). Loose-coupled (no FK to raw.games) so an error row survives even if the game itself never landed.';

-- =============================================================================
-- RAW.PIPELINE_RUN_LOG
-- ETL/pipeline job run log referenced by SIM-138 for opening_line_games_captured.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.pipeline_run_log (
    id                          BIGSERIAL       PRIMARY KEY,
    job_name                    VARCHAR(100)    NOT NULL,
    started_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    finished_at                 TIMESTAMPTZ,
    status                      VARCHAR(20)     NOT NULL DEFAULT 'running'
                                    CHECK (status IN ('running','success','partial','error')),
    games_processed             INTEGER         NOT NULL DEFAULT 0,
    pitches_inserted            INTEGER         NOT NULL DEFAULT 0,
    pitches_skipped             INTEGER         NOT NULL DEFAULT 0,
    opening_line_games_captured INTEGER         NOT NULL DEFAULT 0,
    opening_prop_lines_captured INTEGER         NOT NULL DEFAULT 0,
    error_message               TEXT,
    metadata                    JSONB
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_log_job ON raw.pipeline_run_log(job_name, started_at DESC);

COMMENT ON TABLE raw.pipeline_run_log IS
    'One row per ETL/pipeline job run. opening_line_games_captured logged by nightly opening line job (SIM-138).';

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

-- SIM-087 / SIM-440: plausibility band is release_speed [50, 110] and
-- launch_speed <= 125.  This MUST match etl_historical_loader._validate_row
-- exactly: that validator force-sets data_quality_flag=TRUE on its own warnings
-- and this trigger only ever SETS the flag (never clears it), so whichever of
-- the two layers is stricter is silently authoritative.  A narrower Python band
-- is how SIM-087's widened 50 mph floor stayed unreachable for five years.
-- 102 was never a physical ceiling — Statcast logs 102-105 mph pitches every
-- season, and flagging them stripped the whole high-velocity tail out of
-- sim.pitch_pool and the arsenal GMMs.  Migrations 0007 + 0017 apply this to
-- live DBs.
CREATE OR REPLACE FUNCTION raw.flag_pitch_quality()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (
        (NEW.release_speed IS NOT NULL AND (NEW.release_speed > 110 OR NEW.release_speed < 50)) OR
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

-- =============================================================================
-- SIM.SIM_RUNS
-- SIM-356 (Alembic migration 0014): durable per-run GameSimSummary history.
-- The Postgres half of the sim-result + pitch-snapshot persistence pair (the
-- pitch-level play-stream is the DuckDB half).  One row per Monte-Carlo run;
-- run_id is the stable handle a DuckDB sim.play_stream and the API key their
-- lookups on.  Purely additive — sim.lineup_state (the ephemeral live blob) is
-- left untouched.
-- =============================================================================

CREATE TABLE IF NOT EXISTS sim.sim_runs (
    run_id          BIGSERIAL   PRIMARY KEY,
    game_pk         INTEGER     NOT NULL,
    n_iterations    INTEGER     NOT NULL,
    base_seed       BIGINT,
    summary         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dominant read path: "latest / last-N runs for this matchup". DESC so the
-- planner can satisfy ORDER BY created_at DESC LIMIT n with an index scan.
CREATE INDEX IF NOT EXISTS idx_sim_runs_game_created
    ON sim.sim_runs(game_pk, created_at DESC);

COMMENT ON TABLE sim.sim_runs IS
    'SIM-356: durable per-run GameSimSummary history (to_jsonable(GameSimSummary) in summary). Backs /simulate result lookup + sim history; run_id is the handle the DuckDB sim.play_stream keys on.';

-- =============================================================================
-- RAW.GAME_BULLPEN_AVAILABILITY
-- SIM-433 (Alembic migration 0015): per-game bullpen availability + IL signal.
-- raw.game_lineups records who PLAYED, not who was AVAILABLE.  This table is the
-- available-but-unused signal that makes the SIM-427/SIM-434 manager USAGE
-- sub-score meaningful — one row per (game_pk, team_id, pitcher_id).  The rest /
-- recent-workload fields (days_rest, pitches_last_3d, back_to_back) are derived
-- from raw.pitches by the profile computor; available/reason come from the MLB
-- Stats API active roster + IL.  Purely additive.
-- =============================================================================

CREATE TABLE IF NOT EXISTS raw.game_bullpen_availability (
    game_pk         INTEGER     NOT NULL REFERENCES raw.games(game_pk),
    team_id         INTEGER     NOT NULL,
    pitcher_id      INTEGER     NOT NULL REFERENCES raw.players(player_id),
    available       BOOLEAN     NOT NULL,
    reason          VARCHAR(16)
                        CHECK (reason IN ('active','IL','rest','recent_use')),
    days_rest       INTEGER,
    pitches_last_3d INTEGER     NOT NULL DEFAULT 0,
    back_to_back    BOOLEAN     NOT NULL DEFAULT FALSE,
    is_starter      BOOLEAN     NOT NULL DEFAULT FALSE,
    source          VARCHAR(16) NOT NULL DEFAULT 'mlb_api',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (game_pk, team_id, pitcher_id)
);

-- Per-game availability read path ("which arms could this team use tonight").
CREATE INDEX IF NOT EXISTS idx_gba_game_team
    ON raw.game_bullpen_availability(game_pk, team_id);
-- Per-pitcher availability history.
CREATE INDEX IF NOT EXISTS idx_gba_pitcher
    ON raw.game_bullpen_availability(pitcher_id);

COMMENT ON TABLE raw.game_bullpen_availability IS
    'SIM-433: per-game bullpen availability (who was AVAILABLE, not just who played). reason in active/IL/rest/recent_use; rest fields (days_rest/pitches_last_3d/back_to_back) derived from raw.pitches by the profile computor, available/reason from the MLB Stats API active roster + IL. The available-but-unused signal that makes SIM-427/434 manager USAGE profiles meaningful.';
