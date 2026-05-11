"""sim087_release_speed_threshold

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-06

SIM-087: Lower the raw.flag_pitch_quality() trigger's release_speed floor from
60 mph to 50 mph so legitimate slow curveballs and eephus pitches are NOT
flagged as bad data.

Bug:
    The original trigger flagged any pitch with release_speed < 60 mph as
    data_quality_flag=TRUE, which excluded those rows from sim.pitch_pool.
    Slow curveballs (60–65 mph) and eephus pitches are legitimate pitch
    types — flagging them biases the sim pool toward hard-throwing pitchers.

Fix:
    Two-tier: the Python ETL validator (etl_historical_loader._validate_row)
    warns on release_speed outside [60, 102] but does NOT hard-fail; the DB
    trigger only flags the impossible (< 50 OR > 102).  This matches the
    pattern used for launch_speed where the validator and trigger thresholds
    differ to give the ETL log richer detail without losing usable data.

The trigger function is replaced via CREATE OR REPLACE.  No table data is
touched.  Existing rows with data_quality_flag=TRUE retain their flag — a
backfill pass to revisit historical flags is out of scope (would require
re-running the trigger via UPDATE … SET data_quality_flag = FALSE for the
specific rows that the new trigger would NOT flag, which is a separate
ticket if anyone needs it).
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


_NEW_TRIGGER_BODY = """
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

# Pre-SIM-087 body for downgrade.
_OLD_TRIGGER_BODY = """
CREATE OR REPLACE FUNCTION raw.flag_pitch_quality()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (
        (NEW.release_speed IS NOT NULL AND (NEW.release_speed > 102 OR NEW.release_speed < 60)) OR
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
