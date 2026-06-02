"""sim433_game_bullpen_availability

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-02

SIM-433 — per-game bullpen availability + IL ingestion.

Problem
-------
``raw.game_lineups`` records who *played*, not who was *available*. A manager's
bullpen-usage tendency (the SIM-427 / SIM-434 manager-profile USAGE sub-score)
needs the available-but-unused signal so it can distinguish "the manager chose
NOT to use reliever X" from "the manager *couldn't* — X was on the IL, or had
just thrown back-to-back and was unavailable for rest". Without this signal a
manager who never had a rested setup man looks identical to one who hoarded a
fresh bullpen, and the USAGE columns are uninterpretable.

The season roster + roles are derivable from ``raw.pitches`` (who pitched, when,
how much), but *availability* — the active 26-man minus IL minus
rest-unavailable — is not in our ingested data: it needs the MLB Stats API
active roster + transactions/IL. This table is the join target for both halves:

  * the rest / recent-workload fields (``days_rest``, ``pitches_last_3d``,
    ``back_to_back``) are derived from ``raw.pitches`` by
    ``PlayerProfileComputor._compute_bullpen_workload`` (code-now, no network);
  * ``available`` / ``reason`` (active / IL / rest / recent_use) come from the
    MLB-API ingest (``pipeline/live/bullpen_availability_ingest.py``).

Fix
---
Add ``raw.game_bullpen_availability`` — one row per (game_pk, team_id,
pitcher_id):

  * ``available``         BOOLEAN     NOT NULL — was this arm available for THIS
                          game (active roster, not on IL, and not rest-blocked).
  * ``reason``            VARCHAR(16) — why: 'active' (available),
                          'IL' / 'rest' / 'recent_use' (unavailable). CHECK-gated.
  * ``days_rest``         INTEGER — days since this pitcher's last appearance
                          (derived from raw.pitches; NULL = no prior appearance
                          known in the window).
  * ``pitches_last_3d``   INTEGER     NOT NULL DEFAULT 0 — pitches thrown in the
                          3 calendar days before this game (workload signal).
  * ``back_to_back``      BOOLEAN     NOT NULL DEFAULT FALSE — pitched on the
                          immediately preceding calendar day.
  * ``is_starter``        BOOLEAN     NOT NULL DEFAULT FALSE — true if this arm is
                          a starter (excluded from the bullpen-availability lens).
  * ``source``            VARCHAR(16) NOT NULL DEFAULT 'mlb_api' — provenance of
                          the availability decision ('mlb_api' / 'workload' /
                          'manual').
  * ``created_at`` / ``updated_at`` TIMESTAMPTZ.

PK is (game_pk, team_id, pitcher_id). FK on game_pk → raw.games and
pitcher_id → raw.players (mirrors raw.game_lineups). Indexes back the two read
paths: per-game availability (game_pk, team_id) and per-pitcher history
(pitcher_id).

Purely additive: no existing table is modified. Credentials: none — the DB URL
is read from BASEBALL_DB_DSN at runtime by db/migrations/env.py. All DDL is
IF NOT EXISTS per project convention so re-running upgrade() is a no-op.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
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
        )
        """
    )
    # Per-game availability read path ("which arms could this team use tonight").
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gba_game_team
            ON raw.game_bullpen_availability(game_pk, team_id)
        """
    )
    # Per-pitcher availability history.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gba_pitcher
            ON raw.game_bullpen_availability(pitcher_id)
        """
    )
    op.execute(
        "COMMENT ON TABLE raw.game_bullpen_availability IS "
        "'SIM-433: per-game bullpen availability (who was AVAILABLE, not just who "
        "played). reason in active/IL/rest/recent_use; rest fields "
        "(days_rest/pitches_last_3d/back_to_back) derived from raw.pitches by the "
        "profile computor, available/reason from the MLB Stats API active roster + "
        "IL. The available-but-unused signal that makes SIM-427/434 manager USAGE "
        "profiles meaningful.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.idx_gba_pitcher")
    op.execute("DROP INDEX IF EXISTS raw.idx_gba_game_team")
    op.execute("DROP TABLE IF EXISTS raw.game_bullpen_availability")
