"""sim093_etl_errors_table

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-07

SIM-093: Create raw.etl_errors and wire it into the ETL hard-error path.

Bug:
    The ETL pipeline's docstring explicitly says it "logs to etl_errors table"
    but the table did not exist.  Hard validation errors went only to the
    Python logger; skipped rows were lost with no audit trail and no
    reprocessing path.  There was no way to find which pitches were skipped
    without grepping log files — and even then the metadata (which game,
    which pitch, what error) was inconsistently captured.

Fix:
    Create raw.etl_errors with one row per skipped (or otherwise notable)
    pitch.  The application loader writes here in _process_and_insert();
    a new HistoricalDataLoader.reprocess_errored_games(since=) method reads
    here so an operator can identify games that need a re-ingest after a
    bug fix.

Schema decisions:
    * error_type CHECK ('HARD','WARN') — even though only HARD writes today,
      WARN is reserved for future "store every flagged-row reason" callers.
    * error_messages TEXT[] — preserve the multi-message list from
      ValidationResult without losing structure to a join string.
    * No FK to raw.games — etl_errors must work even when the game itself
      failed to insert (eg. FK prereq missing).  We deliberately keep this
      table loose-coupled; deleting a game does NOT cascade-delete its
      error rows (audit trail must outlive recovery actions).
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.etl_errors (
            id              BIGSERIAL       PRIMARY KEY,
            game_pk         INTEGER,
            at_bat_number   INTEGER,
            pitch_number    INTEGER,
            error_type      VARCHAR(10)     NOT NULL
                                CHECK (error_type IN ('HARD','WARN')),
            error_messages  TEXT[]          NOT NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_etl_errors_game_pk
            ON raw.etl_errors(game_pk, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_etl_errors_recent
            ON raw.etl_errors(created_at DESC)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE raw.etl_errors IS
            'One row per pitch skipped by the ETL hard-error path.  Audit '
            'trail for HistoricalDataLoader.reprocess_errored_games().  '
            'Loose-coupled (no FK to raw.games) so an error row survives '
            'even if the game itself never landed.'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS raw.etl_errors CASCADE")
