"""sim427_game_bullpen

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-13

SIM-427, the owner's decision of 2026-09-13: the simulator's bullpen for a game
is the set of arms the MLB API lists for that game — the box-score feed's
``bullpen`` list (the pitchers on the roster who did NOT pitch that day) and
its ``pitchers`` list (those who did, the starter first). This table stores
those lists, one row per (game, pitcher), written by the box-score ingest
alongside ``raw.game_player_stats``.

``listed`` says which list the arm came from: ``'bullpen'`` (sat), ``'pitched'``
(a reliever who appeared), ``'started'`` (the side's starting pitcher, the
first entry of ``pitchers``). The rotation's other starters appear under
``'bullpen'`` on their off days — the simulator's pen resolver removes them
by the box score's ``p_started`` history, not here.

``pitcher_id`` carries no foreign key to ``raw.players`` on purpose, the
SIM-545 precedent: a box can list an arm the player table has not seen yet.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.game_bullpen (
            game_pk     INTEGER     NOT NULL REFERENCES raw.games(game_pk),
            team_id     INTEGER     NOT NULL,
            pitcher_id  INTEGER     NOT NULL,
            side        VARCHAR(4)  NOT NULL CHECK (side IN ('home', 'away')),
            listed      VARCHAR(8)  NOT NULL CHECK (listed IN ('bullpen', 'pitched', 'started')),
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (game_pk, pitcher_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_bullpen_team ON raw.game_bullpen(team_id, game_pk)"
    )
    op.execute(
        "COMMENT ON TABLE raw.game_bullpen IS "
        "'SIM-427: the arms the MLB box-score feed lists for a game per side — bullpen "
        "(did not pitch), pitched (a reliever who appeared), started (the starter). "
        "The simulator''s pen for the game; rotation starters on their off day are "
        "listed under bullpen and removed by the resolver.'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS raw.game_bullpen")
