"""sim440_pitch_quality_velocity_band

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26

SIM-440: widen the raw.flag_pitch_quality() trigger's plausibility band to match
the ETL validator — release_speed [50, 110]. launch_speed stays at 125.

Problem
-------
The trigger and the Python validator (``etl_historical_loader._validate_row``)
are two independent gates that both write the SAME column, and the trigger only
ever SETS ``data_quality_flag`` TRUE — it never clears it.  Whichever layer is
stricter is therefore silently authoritative.

Two consequences had been live since the original schema:

1. **The 102 mph ceiling is not a physical limit.**  Statcast records four-figure
   counts of 102-105 mph pitches every season (max ~105.8).  Both layers flagged
   them, so ``data_quality_flag = TRUE`` removed the entire high-velocity tail
   from ``sim.pitch_pool`` and from the per-pitcher arsenal GMMs — exactly the
   high-leverage relievers whose stuff drives strikeout props.

2. **SIM-087 was only half-delivered.**  Migration 0007 widened the trigger's
   floor 60 -> 50 mph so eephus and position-player pitches would stop being
   flagged, but left the Python validator warning at < 60 — and a validator
   warning force-sets ``data_quality_flag = True`` before the row is ever sent to
   the database.  The stricter Python floor won on every row, so no pitch in
   [50, 60) was ever actually rescued.  The validator has now been widened to
   50 mph; this migration brings the trigger's ceiling up to meet it so the two
   layers state the same band.

Branch note
-----------
``wave1-remediation`` also carries a ``0016`` (``sim448_game_odds_closing_provenance``)
whose ``down_revision`` is likewise ``"0015"``.  Only one of the two can keep that
number: whichever lands second must be renumbered and re-pointed at the other, or
Alembic sees two children of 0015, reports multiple heads, and applies NOTHING —
which would mean this trigger change silently never lands.
``tests/unit/test_sim440_reload_game.py::TestAlembicHistoryIsLinear`` fails loudly
if that happens.

Fix
---
CREATE OR REPLACE the trigger function with release_speed outside [50, 110] and
launch_speed > 130 as the impossible bounds.  ``break_vertical_induced`` is
unchanged.

Scope
-----
No table data is touched.  The trigger is BEFORE INSERT, so this only affects
rows written from here on.  **Existing rows keep their historical flag** — the
~102-110 mph and 50-60 mph rows already in ``raw.pitches`` stay flagged until
they are re-ingested.  Clearing them is deliberately NOT done here: the correct
repair is ``HistoricalDataLoader.refresh_seasons(reload=True)``, which re-parses
and rewrites the rows so the flag is recomputed from the current rules rather
than being blind-cleared by an UPDATE that cannot know why a row was flagged.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


_NEW_TRIGGER_BODY = """
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
"""

# Post-SIM-087 / pre-SIM-440 body for downgrade.
_OLD_TRIGGER_BODY = """
CREATE OR REPLACE FUNCTION raw.flag_pitch_quality()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (
        (NEW.release_speed IS NOT NULL AND (NEW.release_speed > 102 OR NEW.release_speed < 50)) OR
        (NEW.launch_speed  IS NOT NULL AND NEW.launch_speed > 125) OR
        (NEW.break_vertical_induced IS NOT NULL AND
            (NEW.break_vertical_induced < -25 OR NEW.break_vertical_induced > 25))
    ) THEN
        NEW.data_quality_flag = TRUE;
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_NEW_TRIGGER_BODY)


def downgrade() -> None:
    op.execute(_OLD_TRIGGER_BODY)
