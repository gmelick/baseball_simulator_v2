"""The release-speed bounds: the historical loader's validator and the database
trigger both accept 50 to 110 mph (a slow curve, an eephus, the real
high-velocity tail) and flag anything outside it."""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]

_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# A pitch row that passes _validate_row()'s hard-error phase, so each warning
# check runs alone. Every ALWAYS_REQUIRED column holds a safe default. The
# tests copy it with ``{**row, ...}``.
_MINIMAL_CLEAN_PITCH_ROW: dict = {
    "game_pk": 745000,
    "at_bat_number": 1,
    "pitch_number": 1,
    "game_date": "2024-08-15",
    "venue_id": 4169,
    "pitcher": 100001,
    "p_throws": "R",
    "batter": 200001,
    "stand": "R",
    "bat_hand": "R",
    "inning": 1,
    "inning_topbot": "Top",
    "balls": 0,
    "strikes": 0,
    "outs": 0,
    "home_score": 0,
    "away_score": 0,
    "type": "B",  # ball — bypasses IN_PLAY_REQUIRED check
    "release_speed": 95.0,
    "launch_speed": None,
    "launch_angle": None,
    "bb_type": None,
    "break_vertical_induced": None,
}


class TestReleaseSpeedBounds:
    """The validator and the database trigger accept every real pitch speed."""

    def test_validate_row_accepts_68_mph(self) -> None:
        """A 68 mph slow curve draws no release_speed warning."""
        from pipeline.etl.historical.rows import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 68.0})
        # The row passes the hard validation.
        assert not result.hard_errors, f"unexpected hard errors: {result.hard_errors}"
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings == [], (
            "SIM-087 regression: 68 mph slow curve flagged as bad data. "
            f"Warnings produced: {rs_warnings}"
        )

    def test_validate_row_accepts_60_mph_boundary(self) -> None:
        """60 mph draws no warning."""
        from pipeline.etl.historical.rows import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 60.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings == [], (
            f"SIM-087: 60 mph boundary should be inclusive. Warnings: {rs_warnings}"
        )

    def test_validate_row_still_flags_impossible_low(self) -> None:
        """A 35 mph pitch is impossible and warns."""
        from pipeline.etl.historical.rows import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 35.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings, "SIM-087: 35 mph still must trigger a validator warning."

    def test_validate_row_still_flags_too_fast(self) -> None:
        """A 112 mph pitch is above the 110 mph ceiling and warns."""
        from pipeline.etl.historical.rows import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 112.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings, "SIM-440: 112 mph must still trigger a validator warning."

    def test_validate_row_accepts_real_high_velocity_tail(self) -> None:
        """102 to 110 mph is a real, recorded velocity band and draws no warning.

        Statcast records thousands of 102-105 mph pitches every season (the
        maximum is about 105.8). A lower ceiling removes that tail from
        ``sim.pitch_pool`` and the arsenal GMMs (Gaussian mixture models).
        """
        from pipeline.etl.historical.rows import _validate_row

        for velo in (102.5, 104.0, 105.8, 110.0):
            result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": velo})
            rs_warnings = [w for w in result.warnings if "release_speed" in w]
            assert not rs_warnings, f"SIM-440: {velo} mph must NOT be flagged, got {rs_warnings}"

    def test_validate_row_accepts_eephus_floor(self) -> None:
        """The validator's floor is 50 mph, the same as the trigger's.

        A validator warning sets data_quality_flag=TRUE before the row reaches
        the database, so a stricter Python floor wins on every row. Both
        layers state 50, so a pitch in [50, 60) stays clean.
        """
        from pipeline.etl.historical.rows import _validate_row

        for velo in (50.0, 55.0, 59.9):
            result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": velo})
            rs_warnings = [w for w in result.warnings if "release_speed" in w]
            assert not rs_warnings, f"SIM-440: {velo} mph must NOT be flagged, got {rs_warnings}"

    def test_validator_warning_text_advertises_actual_bounds(self) -> None:
        """The warning text states the band the code enforces."""
        from pipeline.etl.historical.rows import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 35.0})
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert any("50" in w and "110" in w for w in rs_warnings), (
            f"SIM-440: warning text must reference the 50-110 mph band, got: {rs_warnings}"
        )

    def test_canonical_schema_trigger_threshold(self) -> None:
        """raw.flag_pitch_quality() uses < 50, not < 60, as its floor."""
        sql = _read(_PG_SCHEMA_PATH)
        # The trigger function's body.
        m = re.search(
            r"CREATE OR REPLACE FUNCTION raw\.flag_pitch_quality\(\).*?LANGUAGE\s+plpgsql\s+AS\s+\$\$(.*?)\$\$;",
            sql,
            re.DOTALL,
        )
        assert m, "flag_pitch_quality function not found in canonical schema"
        body = m.group(1)
        assert "release_speed < 50" in body, (
            "SIM-087: raw.flag_pitch_quality() must use < 50 mph as the impossible floor."
        )
        assert "release_speed < 60" not in body, (
            "SIM-087: stale 60 mph check still present in flag_pitch_quality()."
        )

    def test_migration_0007_updates_trigger(self) -> None:
        mig = _read(_MIG_DIR / "0007_sim087_release_speed_threshold.py")
        assert "release_speed < 50" in mig
        assert 'down_revision = "0006"' in mig
