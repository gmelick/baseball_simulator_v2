"""sim545_game_player_stats

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-12

The official per-player box score as the prop ground truth (SIM-545, the
sub-ticket of the unpriced-prop-markets work, SIM-421).

Problem
-------
A player prop is graded by the sportsbook against the OFFICIAL box score: the
batter's hits, runs, RBI, total bases; the pitcher's strikeouts, outs, hits
allowed. The platform today derives its "actuals" from ``raw.pitches`` event
labels (``simulation.prop_validation.real_props_from_pa_events``). That
derivation covers only H / HR / TB and K / BB, and it can drift from the
official line: an intentional walk lives in no pitch row, a pickoff out lives
in no pitch row, and a run credited by the official scorer can sit on a play
the pitch parser attributes differently. Every new prop market (singles,
doubles, triples, runs, stolen bases, hits+runs+RBI, outs recorded, hits
allowed) needs an actual the derivation cannot supply exactly.

Fix
---
Add ``raw.game_player_stats`` — one row per (game_pk, player_id) for every
player who APPEARED in the game, copied verbatim from the MLB Stats API box
score (``/api/v1/game/{game_pk}/boxscore``, the same ``teams`` shape the live
feed carries under ``liveData.boxscore``). The batting block is the box's
batting line; the pitching block is the box's pitching line. A player who
did not play has no row: that is the "did not play, the book voids the bet"
rule, and downstream code skips a player with no actual.

``player_id`` carries NO foreign key to ``raw.players`` on purpose. A box
score can list a player the pitch sweep never wrote (a position player who
pitched in a blowout the sweep skipped, a game loaded before the players
upsert ran), and this table must never fail on him: a missing ground-truth
row silently voids a graded prop, which is worse than a dangling id.

Purely additive: no existing table is modified. All DDL is IF NOT EXISTS per
project convention so re-running upgrade() is a no-op.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.game_player_stats (
            game_pk          INTEGER     NOT NULL REFERENCES raw.games(game_pk),
            player_id        INTEGER     NOT NULL,
            team_id          INTEGER     NOT NULL,
            side             VARCHAR(4)  NOT NULL CHECK (side IN ('home', 'away')),
            season           SMALLINT    NOT NULL,
            game_date        DATE        NOT NULL,
            batting_order    SMALLINT,
            position_code    VARCHAR(5),
            played_bat       BOOLEAN     NOT NULL DEFAULT FALSE,
            played_pitch     BOOLEAN     NOT NULL DEFAULT FALSE,
            -- batting line (the box score's batting block)
            pa               SMALLINT    NOT NULL DEFAULT 0,
            ab               SMALLINT    NOT NULL DEFAULT 0,
            r                SMALLINT    NOT NULL DEFAULT 0,
            h                SMALLINT    NOT NULL DEFAULT 0,
            b2               SMALLINT    NOT NULL DEFAULT 0,
            b3               SMALLINT    NOT NULL DEFAULT 0,
            hr               SMALLINT    NOT NULL DEFAULT 0,
            rbi              SMALLINT    NOT NULL DEFAULT 0,
            sb               SMALLINT    NOT NULL DEFAULT 0,
            cs               SMALLINT    NOT NULL DEFAULT 0,
            bb               SMALLINT    NOT NULL DEFAULT 0,
            k                SMALLINT    NOT NULL DEFAULT 0,
            hbp              SMALLINT    NOT NULL DEFAULT 0,
            sf               SMALLINT    NOT NULL DEFAULT 0,
            tb               SMALLINT    NOT NULL DEFAULT 0,
            -- pitching line (the box score's pitching block)
            p_outs           SMALLINT    NOT NULL DEFAULT 0,
            p_h              SMALLINT    NOT NULL DEFAULT 0,
            p_r              SMALLINT    NOT NULL DEFAULT 0,
            p_er             SMALLINT    NOT NULL DEFAULT 0,
            p_bb             SMALLINT    NOT NULL DEFAULT 0,
            p_k              SMALLINT    NOT NULL DEFAULT 0,
            p_hr             SMALLINT    NOT NULL DEFAULT 0,
            p_pitches        SMALLINT    NOT NULL DEFAULT 0,
            p_batters_faced  SMALLINT    NOT NULL DEFAULT 0,
            p_started        BOOLEAN     NOT NULL DEFAULT FALSE,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (game_pk, player_id)
        )
        """
    )
    # Per-player history ("every game this batter played in 2024").
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gps_player
            ON raw.game_player_stats(player_id)
        """
    )
    # Per-season scans (the audit and the validation lanes read a season at a time).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gps_season
            ON raw.game_player_stats(season)
        """
    )
    op.execute(
        "COMMENT ON TABLE raw.game_player_stats IS "
        "'SIM-545: the official per-player box score, one row per (game_pk, "
        "player_id) for every player who appeared. This is the ground truth that "
        "grades a player prop the way a sportsbook does: the batting block is the "
        "box batting line, the pitching block the box pitching line. A player who "
        "did not play has no row (the book voids his bet). player_id carries no "
        "foreign key to raw.players on purpose: a box score can list a player the "
        "pitch sweep never wrote, and a missing ground-truth row must never come "
        "from a foreign-key failure.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.idx_gps_season")
    op.execute("DROP INDEX IF EXISTS raw.idx_gps_player")
    op.execute("DROP TABLE IF EXISTS raw.game_player_stats")
