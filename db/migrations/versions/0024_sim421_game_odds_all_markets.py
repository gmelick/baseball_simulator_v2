"""sim421_game_odds_all_markets

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-12

SIM-421, the owner's ruling of 2026-09-12: the odds tables carry EVERY market
the book posts on a game, because the simulator can price every one of them.

WHAT CHANGES
============
``raw.game_odds.market_type`` carried a CHECK constraint over three values
(moneyline, runline, total). This migration replaces it with the same
constraint over the 15 values in ``pipeline/odds_provider.py``
``GAME_MARKET_TYPES``: the three full-game markets plus the twelve segment and
team markets BettingPros posts on every game — the first-inning and
first-five-innings moneyline, total and run line; each side's full-game and
first-five team total; the first team to score; and "a run in the first
inning". Every market uses the SAME row layout (the existing columns), and a
unit test keeps this list in step with the vocabulary.

One column is added: ``draw_ml INTEGER NULL``. The first-inning and first-five
moneylines are three-way markets — a segment can end tied — and a two-way
row has nowhere to keep the tie price. ``draw_ml`` stays NULL on every other
market. Adding a nullable column changes no stored row.

THE DOWNGRADE DELETES ROWS
==========================
Postgres cannot restore the three-value constraint while a row holds one of
the twelve new values. The downgrade therefore DELETES every ``raw.game_odds``
row whose ``market_type`` is one of the twelve new markets, drops ``draw_ml``,
then restores the three-value constraint. Those quotes are gone after a
downgrade; re-load them with ``scripts/load_historical_odds.py --game-markets
...`` after the next upgrade.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

#: The three full-game markets (migration 0003).
_ORIGINAL_MARKET_TYPES = ("moneyline", "runline", "total")

#: The twelve segment and team markets. The downgrade deletes their rows.
_NEW_MARKET_TYPES = (
    "f1_moneyline",
    "f5_moneyline",
    "f1_total",
    "f5_total",
    "f1_runline",
    "f5_runline",
    "team_total_home",
    "team_total_away",
    "f5_team_total_home",
    "f5_team_total_away",
    "first_to_score",
    "first_inning_run",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _replace_check(values: tuple[str, ...]) -> str:
    """One idempotent DO block: drop the old constraint, add it over ``values``.

    The constraint keeps its original name (``game_odds_market_type_check``,
    the name Postgres gave the inline CHECK in migration 0003) so every
    tool that looks it up by name still finds it.
    """
    return f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM   pg_constraint
                WHERE  conname   = 'game_odds_market_type_check'
                AND    conrelid  = 'raw.game_odds'::regclass
            ) THEN
                ALTER TABLE raw.game_odds DROP CONSTRAINT game_odds_market_type_check;
            END IF;
            ALTER TABLE raw.game_odds
            ADD CONSTRAINT game_odds_market_type_check
            CHECK (market_type IN ({_sql_list(values)}));
        END;
        $$
    """


def upgrade() -> None:
    op.execute("ALTER TABLE raw.game_odds ADD COLUMN IF NOT EXISTS draw_ml INTEGER")
    op.execute(
        "COMMENT ON COLUMN raw.game_odds.draw_ml IS "
        "'SIM-421: the tie price of a three-way segment moneyline (f1_moneyline, "
        "f5_moneyline). NULL on every two-way market.'"
    )
    op.execute(_replace_check(_ORIGINAL_MARKET_TYPES + _NEW_MARKET_TYPES))


def downgrade() -> None:
    # The rows of the twelve new markets must go first: the three-value
    # constraint cannot be added while they exist (see the module docstring).
    op.execute(f"DELETE FROM raw.game_odds WHERE market_type IN ({_sql_list(_NEW_MARKET_TYPES)})")
    op.execute(_replace_check(_ORIGINAL_MARKET_TYPES))
    op.execute("ALTER TABLE raw.game_odds DROP COLUMN IF EXISTS draw_ml")


__all__ = ["downgrade", "revision", "upgrade"]
