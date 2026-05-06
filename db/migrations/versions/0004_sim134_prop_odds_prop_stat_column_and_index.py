"""prop_odds: rename prop_type->prop_stat, add CHECK constraint, compound index

SIM-134 — Create raw.prop_odds table and extend MockOddsAPI to generate player prop lines

Revision ID: 0004
Revises:     0003
Create Date: 2026-05-05

Changes
-------
1. Rename column  raw.prop_odds.prop_type  →  raw.prop_odds.prop_stat
2. Add CHECK constraint enforcing the 7 known prop markets (Betting Analyst confirmed).
3. Drop the weak (game_pk, player_id) index from migration 0003.
4. Create a compound (game_pk, player_id, prop_stat, fetched_at DESC) index that
   supports per-player-per-stat time-series queries and CLV line lookups.

Betting Analyst (Agent 8) scope decision
-----------------------------------------
The backlog AC proposed 6 values.  Agent 8 confirmed 7 are required:

  strikeouts   — pitcher prop; most liquid market; direct whiff/chase signal
  hits         — batter prop; BABIP + batted-ball profile driven
  home_runs    — batter prop; exit velo + launch angle + park HR factor
  earned_runs  — pitcher prop; high-variance, addressable with pitch profile
  walks        — pitcher prop; BB/9 driven; good edge available in market
  total_bases  — batter prop; XBH value beyond hits; useful for power hitters
  rbis         — batter prop; lineup-context prop; explicitly in BA role spec
                 and already wired into opening_line_job.py (SIM-138) —
                 omitting it from the CHECK would immediately break existing
                 INSERT paths

Deferred:
  innings_pitched — meaningful starter prop but requires pitch-count model
                    (Phase 4 dependency). Add in migration 0005 when ready.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision      = "0004"
down_revision = "0003"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Rename column prop_type → prop_stat
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE raw.prop_odds RENAME COLUMN prop_type TO prop_stat"
    )

    # ------------------------------------------------------------------
    # 2. Add CHECK constraint on prop_stat
    #    (Use IF NOT EXISTS guard via DO block for idempotency on re-runs)
    # ------------------------------------------------------------------
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM   pg_constraint
                WHERE  conname   = 'ck_prop_odds_prop_stat'
                AND    conrelid  = 'raw.prop_odds'::regclass
            ) THEN
                ALTER TABLE raw.prop_odds
                ADD CONSTRAINT ck_prop_odds_prop_stat
                CHECK (prop_stat IN (
                    'strikeouts',
                    'hits',
                    'home_runs',
                    'earned_runs',
                    'walks',
                    'total_bases',
                    'rbis'
                ));
            END IF;
        END;
        $$
    """)

    # ------------------------------------------------------------------
    # 3. Drop the weak (game_pk, player_id) index from migration 0003
    # ------------------------------------------------------------------
    op.execute(
        "DROP INDEX IF EXISTS raw.idx_prop_odds_game_player"
    )

    # ------------------------------------------------------------------
    # 4. Create compound index for per-player-per-stat time-series queries
    #    and CLV lookups (opening vs. closing same game/player/stat)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_prop_odds_game_player
            ON raw.prop_odds(game_pk, player_id, prop_stat, fetched_at DESC)
    """)


def downgrade() -> None:
    # ------------------------------------------------------------------
    # 4 (reverse). Drop compound index
    # ------------------------------------------------------------------
    op.execute(
        "DROP INDEX IF EXISTS raw.idx_prop_odds_game_player"
    )

    # ------------------------------------------------------------------
    # 3 (reverse). Re-create the old weak index
    # ------------------------------------------------------------------
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_prop_odds_game_player
            ON raw.prop_odds(game_pk, player_id)
    """)

    # ------------------------------------------------------------------
    # 2 (reverse). Drop CHECK constraint
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE raw.prop_odds DROP CONSTRAINT IF EXISTS ck_prop_odds_prop_stat"
    )

    # ------------------------------------------------------------------
    # 1 (reverse). Rename column prop_stat → prop_type
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE raw.prop_odds RENAME COLUMN prop_stat TO prop_type"
    )
