"""sim421_prop_odds_market_vocabulary

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-12

SIM-421: the prop-bet types the market already offers but the platform did
not price.

WHAT CHANGES
============
``raw.prop_odds.prop_stat`` carries a CHECK constraint, ``ck_prop_odds_prop_stat``
(migration 0004), that listed the seven original markets. This migration
replaces it with the same constraint over 15 values — the seven originals plus
the eight markets BettingPros already quotes:

  pitcher:  outs_recorded, hits_allowed
  batter:   singles, doubles, triples, runs, stolen_bases, hits_runs_rbis

The vocabulary itself lives in ``pipeline/odds_provider.py`` (``PROP_STATS``);
this constraint lists the same 15 strings and a unit test keeps the two in step.
The constraint is dropped and re-added inside one ``DO`` block so a re-run is
idempotent (the migration 0004 style).

THE DOWNGRADE DELETES ROWS
==========================
Postgres cannot restore the seven-value constraint while a row holds one of
the eight new values. The downgrade therefore DELETES every ``raw.prop_odds``
row whose ``prop_stat`` is one of the eight new markets, then restores the
seven-value constraint. Those quotes are gone after a downgrade; re-load them
with ``scripts/load_historical_odds.py --prop-stats ...`` after the next
upgrade.
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

#: The seven original markets (migration 0004).
_ORIGINAL_PROP_STATS = (
    "strikeouts",
    "hits",
    "home_runs",
    "earned_runs",
    "walks",
    "total_bases",
    "rbis",
)

#: The eight SIM-421 markets. The downgrade deletes their rows.
_NEW_PROP_STATS = (
    "singles",
    "doubles",
    "triples",
    "runs",
    "stolen_bases",
    "hits_runs_rbis",
    "outs_recorded",
    "hits_allowed",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _replace_check(values: tuple[str, ...]) -> str:
    """One idempotent DO block: drop the old constraint, add it over ``values``."""
    return f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM   pg_constraint
                WHERE  conname   = 'ck_prop_odds_prop_stat'
                AND    conrelid  = 'raw.prop_odds'::regclass
            ) THEN
                ALTER TABLE raw.prop_odds DROP CONSTRAINT ck_prop_odds_prop_stat;
            END IF;
            ALTER TABLE raw.prop_odds
            ADD CONSTRAINT ck_prop_odds_prop_stat
            CHECK (prop_stat IN ({_sql_list(values)}));
        END;
        $$
    """


def upgrade() -> None:
    op.execute(_replace_check(_ORIGINAL_PROP_STATS + _NEW_PROP_STATS))


def downgrade() -> None:
    # The rows of the eight new markets must go first: the seven-value
    # constraint cannot be added while they exist (see the module docstring).
    op.execute(f"DELETE FROM raw.prop_odds WHERE prop_stat IN ({_sql_list(_NEW_PROP_STATS)})")
    op.execute(_replace_check(_ORIGINAL_PROP_STATS))


__all__ = ["downgrade", "revision", "upgrade"]
