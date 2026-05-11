"""sim083_etl_freshness_and_game_odds_to_schema

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05

SIM-083: Move raw.etl_data_freshness DDL into 01_postgres_schema.sql.

Bug:
    FRESHNESS_TABLE_DDL was a module-level string constant in etl_historical_loader.py
    that was never executed.  _log_freshness() blindly ran INSERT INTO
    raw.etl_data_freshness — if the table didn't exist (which it never did),
    every freshness upsert raised UndefinedTable after pitch rows were inserted.
    Data loaded but freshness tracking never worked.

Also covers:
    - raw.game_odds DDL (was GAME_ODDS_DDL string in live_ingestion_pipeline.py;
      never applied separately from LIVE_PIPELINE_DDL).
    - raw.prop_odds and raw.pipeline_run_log (supporting tables for SIM-138).
    - SIM-133 CLV columns on raw.game_odds (book, line_type, market_type, is_sharp_book).
      Applied here rather than a separate migration since raw.game_odds is new.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # raw.etl_data_freshness  (SIM-083)
    # Previously a dead string constant — now authoritative DDL in schema.
    # -----------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.etl_data_freshness (
            entity_type  VARCHAR(10)  NOT NULL,
            entity_id    INTEGER      NOT NULL,
            last_game_pk INTEGER      NOT NULL,
            last_date    DATE         NOT NULL,
            updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            PRIMARY KEY  (entity_type, entity_id)
        )
    """)
    op.execute("""
        COMMENT ON TABLE raw.etl_data_freshness IS
            'Tracks last loaded game per pitcher/batter. Used to detect stale profiles.'
    """)

    # -----------------------------------------------------------------
    # raw.game_odds  (SIM-083 + SIM-133 combined — new table)
    # Was a CREATE IF NOT EXISTS string in live_ingestion_pipeline.py.
    # Now in canonical schema with all four CLV columns included from creation.
    # -----------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.game_odds (
            id              BIGSERIAL       PRIMARY KEY,
            game_pk         INTEGER         NOT NULL REFERENCES raw.games(game_pk),
            fetched_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            source          VARCHAR(50)     NOT NULL DEFAULT 'consensus',
            is_mock         BOOLEAN         NOT NULL DEFAULT TRUE,
            home_ml         INTEGER,
            away_ml         INTEGER,
            home_spread     FLOAT,
            home_spread_ml  INTEGER,
            away_spread     FLOAT,
            away_spread_ml  INTEGER,
            total_line      FLOAT,
            over_ml         INTEGER,
            under_ml        INTEGER,
            book            VARCHAR(50)     NOT NULL DEFAULT 'consensus',
            line_type       VARCHAR(20)     NOT NULL DEFAULT 'current'
                                CHECK (line_type IN ('opening','current','closing','bet_placement')),
            market_type     VARCHAR(20)     NOT NULL DEFAULT 'moneyline'
                                CHECK (market_type IN ('moneyline','runline','total')),
            is_sharp_book   BOOLEAN         NOT NULL DEFAULT FALSE
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_game_odds_game_pk
            ON raw.game_odds(game_pk, fetched_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_game_odds_line_type
            ON raw.game_odds(game_pk, line_type)
    """)

    # -----------------------------------------------------------------
    # raw.prop_odds  (SIM-138 dependency)
    # -----------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw.prop_odds (
            id              BIGSERIAL       PRIMARY KEY,
            game_pk         INTEGER         NOT NULL REFERENCES raw.games(game_pk),
            player_id       INTEGER         NOT NULL REFERENCES raw.players(player_id),
            fetched_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            source          VARCHAR(50)     NOT NULL DEFAULT 'consensus',
            is_mock         BOOLEAN         NOT NULL DEFAULT TRUE,
            prop_type       VARCHAR(30)     NOT NULL,
            line            FLOAT           NOT NULL,
            over_ml         INTEGER,
            under_ml        INTEGER,
            book            VARCHAR(50)     NOT NULL DEFAULT 'consensus',
            line_type       VARCHAR(20)     NOT NULL DEFAULT 'current'
                                CHECK (line_type IN ('opening','current','closing','bet_placement')),
            is_sharp_book   BOOLEAN         NOT NULL DEFAULT FALSE
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_odds_game_player ON raw.prop_odds(game_pk, player_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_odds_line_type   ON raw.prop_odds(game_pk, line_type)"
    )

    # -----------------------------------------------------------------
    # raw.pipeline_run_log  (SIM-138 dependency)
    # -----------------------------------------------------------------
    op.execute("""
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
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_run_log_job ON raw.pipeline_run_log(job_name, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS raw.pipeline_run_log CASCADE")
    op.execute("DROP TABLE IF EXISTS raw.prop_odds CASCADE")
    op.execute("DROP TABLE IF EXISTS raw.game_odds CASCADE")
    op.execute("DROP TABLE IF EXISTS raw.etl_data_freshness CASCADE")
