"""
Unit tests for SIM-085 → SIM-091 (Data Engineer sprint 2026-05-06)
====================================================================

Permanent regression suite for the six P1 Data-Engineer tickets shipped in
this sprint.  These tests run without a live database — they parse the
canonical schema files, exercise pure-Python code paths, and use mocks
where async DB I/O would otherwise be required.

Coverage:
  SIM-085 — Composite situation index present in canonical schema and
            in the migration.
  SIM-086 — _upsert_game_record() emits None (not 0) when venue missing;
            schema makes raw.games.venue_id nullable;
            VenueBackfillJob skeleton imports cleanly.
  SIM-087 — _validate_row() does NOT warn on a 68 mph pitch (slow curve
            kept as clean data); still warns on a 35 mph (impossible).
            Trigger threshold lowered to < 50 mph in the canonical schema.
  SIM-088 — idx_pitches_pitch_type removed from canonical schema and
            from the create-index list in migration 0001 OR explicitly
            dropped by migration 0008.
  SIM-089 — Composite (pitcher, season) partial index present in canonical
            schema and in the migration.
  SIM-091 — Every derived.* table that has a `season` column appears in the
            _delete_seasons() table list (regression guard against future
            tables being silently skipped on full_rebuild).

Run:
    pytest tests/unit/test_data_engineer_sim085_to_091.py -v
"""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so package imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Source files this suite reasons about
_PG_SCHEMA_PATH      = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_DUCKDB_SCHEMA_PATH  = _ROOT / "db" / "schemas" / "02_duckdb_schema.sql"
_MIG_DIR             = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# SIM-085 — Composite situation index on raw.pitches
# ===========================================================================

class TestSim085SituationIndex:
    """Composite partial index for the situation similarity engine."""

    def test_index_present_in_canonical_schema(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        assert "idx_pitches_situation" in sql, (
            "SIM-085: idx_pitches_situation missing from 01_postgres_schema.sql. "
            "The situation similarity engine (SIM-070) requires this index."
        )
        # Confirm shape: composite + partial + correct columns + correct predicate
        assert re.search(
            r"idx_pitches_situation"
            r".*?\(\s*inning\s*,\s*outs\s*,\s*balls\s*,\s*strikes\s*,"
            r"\s*on_1b\s*,\s*on_2b\s*,\s*on_3b\s*\)"
            r"\s*WHERE\s+data_quality_flag\s*=\s*FALSE",
            sql,
            re.DOTALL | re.IGNORECASE,
        ), "SIM-085: idx_pitches_situation column list or partial predicate is wrong."

    def test_migration_0005_creates_index(self) -> None:
        mig = _read(_MIG_DIR / "0005_sim085_pitches_situation_index.py")
        assert "idx_pitches_situation" in mig
        assert "inning, outs, balls, strikes, on_1b, on_2b, on_3b" in mig
        assert "data_quality_flag = FALSE" in mig
        assert 'down_revision = "0004"' in mig


# ===========================================================================
# SIM-086 — venue_id nullable + None fallback in _upsert_game_record
# ===========================================================================

class TestSim086VenueIdFallback:
    """Live pipeline must not insert venue_id=0 sentinel."""

    def test_canonical_schema_drops_not_null(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        # Find the raw.games CREATE TABLE block
        match = re.search(
            r"CREATE TABLE raw\.games\s*\((.*?)\n\);", sql, re.DOTALL,
        )
        assert match, "raw.games CREATE TABLE block not found in canonical schema"
        body = match.group(1)
        # Find the venue_id line
        venue_line = next(
            (ln for ln in body.splitlines() if re.match(r"\s*venue_id\b", ln)),
            None,
        )
        assert venue_line is not None, "venue_id column missing from raw.games"
        assert "NOT NULL" not in venue_line.upper(), (
            "SIM-086: raw.games.venue_id must be nullable. Current line: "
            f"{venue_line!r}"
        )

    def test_migration_0006_drops_not_null(self) -> None:
        mig = _read(_MIG_DIR / "0006_sim086_games_venue_id_nullable.py")
        assert "ALTER COLUMN venue_id DROP NOT NULL" in mig
        assert 'down_revision = "0005"' in mig

    def test_upsert_game_record_emits_none_when_venue_missing(self) -> None:
        """
        Regression: pre-SIM-086 the fallback was ``.get("id", 0)`` which sent
        venue_id=0 to PostgreSQL and tripped the FK on raw.venues.  After
        SIM-086 the call must pass None.
        """
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        # Build a minimal pipeline instance without invoking __init__
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        # Game dict with NO venue key — the production failure mode
        game = {
            "gamePk":   745001,
            "gameDate": "2026-05-06T18:05:00Z",
            "gameType": "R",
            "status":   {"abstractGameState": "Preview"},
            "teams":    {
                "home": {"team": {"id": 147}},
                "away": {"team": {"id": 111}},
            },
            # 'venue' key intentionally absent
        }

        import asyncio
        asyncio.run(pipeline._upsert_game_record(game))

        # The fifth positional argument to execute() (after the SQL string and
        # game_pk/game_date/game_type/status) is venue_id.  Per SIM-086 it must
        # be None, not 0.
        assert pipeline._db.execute.call_count == 1
        call_args = pipeline._db.execute.call_args
        # call_args.args = (sql, game_pk, game_date, game_type, status, venue_id, home_id, away_id)
        venue_id_arg = call_args.args[5]
        assert venue_id_arg is None, (
            f"SIM-086 regression: venue_id should be None when venue key is missing, got {venue_id_arg!r}. "
            "The previous .get('id', 0) sentinel re-introduced the FK violation."
        )

    def test_upsert_game_record_emits_real_venue_when_present(self) -> None:
        """Sanity: the fallback only kicks in when venue is missing."""
        from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = MagicMock()
        pipeline._db.execute = AsyncMock()

        game = {
            "gamePk":   745002,
            "gameDate": "2026-05-06T18:05:00Z",
            "gameType": "R",
            "status":   {"abstractGameState": "Live"},
            "venue":    {"id": 4169, "name": "Yankee Stadium"},
            "teams":    {
                "home": {"team": {"id": 147}},
                "away": {"team": {"id": 111}},
            },
        }

        import asyncio
        asyncio.run(pipeline._upsert_game_record(game))

        venue_id_arg = pipeline._db.execute.call_args.args[5]
        assert venue_id_arg == 4169

    def test_venue_backfill_job_imports(self) -> None:
        """The new backfill job must import cleanly (catches syntax/import bugs in CI)."""
        from pipeline.etl import venue_backfill_job  # noqa: F401
        from pipeline.etl.venue_backfill_job import (
            VenueBackfillJob,
            schedule_venue_backfill_job,
        )
        # Sanity: class is constructible with a bogus DSN (no connection happens at __init__)
        job = VenueBackfillJob(dsn="postgresql://u:p@localhost/db", dry_run=True)
        assert job._dry_run is True
        assert callable(schedule_venue_backfill_job)


# ===========================================================================
# SIM-087 — release_speed validator threshold lowered
# ===========================================================================

# Minimal pitch row that passes _validate_row()'s HARD ERROR phase so
# individual WARNING checks can be exercised in isolation.  All
# ALWAYS_REQUIRED columns are populated with safe-default values.
# Defined as a module-level constant — pytest's assertion rewriter has
# historically mangled local-variable line attribution inside helper
# functions in this file, so we use dict() copy + update at call sites
# rather than a helper function.
_MINIMAL_CLEAN_PITCH_ROW: dict = {
    "game_pk":         745000,
    "at_bat_number":   1,
    "pitch_number":    1,
    "game_date":       "2024-08-15",
    "venue_id":        4169,
    "pitcher":         100001,
    "p_throws":        "R",
    "batter":          200001,
    "stand":           "R",
    "bat_hand":        "R",
    "inning":          1,
    "inning_topbot":   "Top",
    "balls":           0,
    "strikes":         0,
    "outs":            0,
    "home_score":      0,
    "away_score":      0,
    "type":            "B",          # ball — bypasses IN_PLAY_REQUIRED check
    "release_speed":   95.0,
    "launch_speed":    None,
    "launch_angle":    None,
    "bb_type":         None,
    "break_vertical_induced": None,
}


class TestSim087ReleaseSpeedThreshold:
    """Validator and DB trigger must allow legitimate slow pitches."""

    def test_validate_row_accepts_68_mph(self) -> None:
        """68 mph slow curve should not produce a release_speed warning."""
        from pipeline.etl.etl_historical_loader import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 68.0})
        # Sanity: row passed hard validation
        assert not result.hard_errors, f"unexpected hard errors: {result.hard_errors}"
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings == [], (
            "SIM-087 regression: 68 mph slow curve flagged as bad data. "
            f"Warnings produced: {rs_warnings}"
        )

    def test_validate_row_accepts_60_mph_boundary(self) -> None:
        """The 60 mph boundary itself must be inclusive."""
        from pipeline.etl.etl_historical_loader import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 60.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings == [], (
            f"SIM-087: 60 mph boundary should be inclusive. Warnings: {rs_warnings}"
        )

    def test_validate_row_still_flags_impossible_low(self) -> None:
        """A 35 mph pitch is implausible and must still warn."""
        from pipeline.etl.etl_historical_loader import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 35.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings, "SIM-087: 35 mph still must trigger a validator warning."

    def test_validate_row_still_flags_too_fast(self) -> None:
        """The 102 mph upper bound is unchanged."""
        from pipeline.etl.etl_historical_loader import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 108.0})
        assert not result.hard_errors
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert rs_warnings, "SIM-087: 108 mph still must trigger a validator warning."

    def test_validator_warning_text_advertises_60_mph(self) -> None:
        """Warning text should reflect the new lower bound."""
        from pipeline.etl.etl_historical_loader import _validate_row

        result = _validate_row({**_MINIMAL_CLEAN_PITCH_ROW, "release_speed": 35.0})
        rs_warnings = [w for w in result.warnings if "release_speed" in w]
        assert any("60" in w for w in rs_warnings), (
            "SIM-087: warning text should reference the new 60 mph bound, got: "
            f"{rs_warnings}"
        )

    def test_canonical_schema_trigger_threshold(self) -> None:
        """raw.flag_pitch_quality() must use < 50, not < 60, as the trigger floor."""
        sql = _read(_PG_SCHEMA_PATH)
        # Locate the trigger function body
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


# ===========================================================================
# SIM-088 — Drop idx_pitches_pitch_type
# ===========================================================================

class TestSim088DropPitchTypeIndex:
    """The audit-only pitch_type column should not carry a write-overhead index."""

    def test_canonical_schema_no_pitch_type_index(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        # The (pitcher, pitch_type) compound index also references pitch_type
        # but is intentionally retained for ad-hoc queries; the standalone
        # idx_pitches_pitch_type is the one SIM-088 removes.
        assert not re.search(
            r"CREATE\s+INDEX\s+(IF\s+NOT\s+EXISTS\s+)?idx_pitches_pitch_type\b",
            sql,
            re.IGNORECASE,
        ), "SIM-088: idx_pitches_pitch_type still present in canonical schema."

    def test_migration_0008_drops_index(self) -> None:
        mig = _read(_MIG_DIR / "0008_sim088_drop_pitches_pitch_type_index.py")
        assert "DROP INDEX IF EXISTS idx_pitches_pitch_type" in mig
        assert 'down_revision = "0007"' in mig

    def test_no_sql_where_clause_filters_by_pitch_type(self) -> None:
        """
        SIM-088 acceptance #1: no hot-path SQL filter on pitch_type.

        Greps the pipeline package for ``WHERE …pitch_type = …`` or
        ``WHERE …pitch_type IN (…)``.  Allows pitch_type as a SELECT projection
        column or a Python dict assignment — only flags it as a SQL predicate.

        Audit at SIM-088 ship time confirmed the only pipeline reference to
        ``pitch_type`` is a Python dict assignment in
        etl_historical_loader.py::_extract_play_event() — not a query filter.
        """
        pipeline_dir = _ROOT / "pipeline"
        offenders: list[str] = []
        # Multi-line, dotall: capture the WHERE...predicate region per file.
        where_pitch_type = re.compile(
            r"WHERE[\s\S]{0,400}?\bpitch_type\s*(=|IN\s*\()",
            re.IGNORECASE,
        )
        for path in pipeline_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if where_pitch_type.search(text):
                offenders.append(str(path.relative_to(_ROOT)))

        assert not offenders, (
            "SIM-088 audit: SQL WHERE clause filtering by pitch_type found in "
            f"these files: {offenders}.  Do NOT drop the index without restoring "
            "a CONCURRENTLY-built replacement first."
        )


# ===========================================================================
# SIM-089 — Composite (pitcher, season) partial index
# ===========================================================================

class TestSim089PitcherSeasonCleanIndex:
    """Profile-computor hot path needs (pitcher, season) WHERE clean."""

    def test_index_present_in_canonical_schema(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        assert "idx_pitches_pitcher_season_clean" in sql, (
            "SIM-089: idx_pitches_pitcher_season_clean missing from canonical schema."
        )
        assert re.search(
            r"idx_pitches_pitcher_season_clean"
            r".*?\(\s*pitcher\s*,\s*season\s*\)"
            r"\s*WHERE\s+data_quality_flag\s*=\s*FALSE",
            sql,
            re.DOTALL | re.IGNORECASE,
        ), "SIM-089: index column list or partial predicate is wrong."

    def test_migration_0009_creates_index(self) -> None:
        mig = _read(_MIG_DIR / "0009_sim089_pitches_pitcher_season_clean_index.py")
        assert "idx_pitches_pitcher_season_clean" in mig
        assert "(pitcher, season)" in mig
        assert "data_quality_flag = FALSE" in mig
        assert 'down_revision = "0008"' in mig


# ===========================================================================
# SIM-091 — _delete_seasons() must enumerate every season-keyed derived table
# ===========================================================================

# Tables we intentionally exclude from _delete_seasons():
#   - sim.* pools — each has its own DELETE in the build method that produces it.
#   - derived.run_expectancy_matrix — keyed by season_range (not season).
_EXCLUDED_FROM_DELETE_SEASONS: set[str] = {
    "derived.run_expectancy_matrix",
}


def _parse_derived_tables_with_season() -> set[str]:
    """
    Parse 02_duckdb_schema.sql and return the set of fully-qualified
    derived.* table names that have a `season` column (which is a
    per-season delete-and-rebuild signal).
    """
    sql = _read(_DUCKDB_SCHEMA_PATH)
    tables: set[str] = set()
    # Grab each CREATE TABLE [IF NOT EXISTS] derived.<name> ( ... );  block
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(derived\.\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(sql):
        full_name = m.group(1).lower()
        body = m.group(2)
        if re.search(r"^\s*season\s+(SMALLINT|INTEGER)\b", body, re.MULTILINE | re.IGNORECASE):
            tables.add(full_name)
    return tables


def _parse_delete_seasons_table_list() -> list[str]:
    """Read the table list literal out of player_profile_computor.py::_delete_seasons."""
    path = _ROOT / "pipeline" / "batch" / "player_profile_computor.py"
    text = _read(path)
    m = re.search(
        r"def\s+_delete_seasons\(self,\s*seasons:\s*list\[int\]\)\s*->\s*None:.*?tables\s*=\s*\[(.*?)\]",
        text,
        re.DOTALL,
    )
    assert m, "Could not locate the tables = [...] literal in _delete_seasons()"
    raw = m.group(1)
    names = [
        s.strip().strip('"').strip("'")
        for s in re.findall(r'"([^"]+)"|\'([^\']+)\'', raw)
        for s in [s[0] or s[1]]
    ]
    return [n for n in names if n]


class TestSim091DeleteSeasonsCoverage:
    """Every derived.* table with a `season` column must be deleted on full_rebuild."""

    def test_play_detail_tables_in_delete_list(self) -> None:
        """SIM-091 acceptance #1: per-play detail tables explicitly enumerated."""
        listed = set(_parse_delete_seasons_table_list())
        for required in (
            "derived.outfield_play_detail",
            "derived.infield_play_detail",
            "derived.dp_play_detail",
        ):
            assert required in listed, (
                f"SIM-091: {required} missing from _delete_seasons() table list. "
                "Without it, full_rebuild=True leaves stale defensive metric data."
            )

    def test_all_season_keyed_derived_tables_in_delete_list(self) -> None:
        """
        SIM-091 acceptance #2: regression guard.  When a new derived.* table
        with a season column is added to the schema, this test fails until
        the table is added to _delete_seasons() — preventing silent stale-row
        pollution on the next full_rebuild.
        """
        schema_tables   = _parse_derived_tables_with_season()
        delete_listed   = set(_parse_delete_seasons_table_list())
        expected        = schema_tables - _EXCLUDED_FROM_DELETE_SEASONS

        missing = sorted(expected - delete_listed)
        assert not missing, (
            "SIM-091 regression: the following derived.* tables have a "
            "`season` column but are not in _delete_seasons():\n  "
            + "\n  ".join(missing)
            + "\n\nAdd them to the `tables` list in player_profile_computor.py "
              "or, if intentional, add them to _EXCLUDED_FROM_DELETE_SEASONS "
              "in this test with a comment explaining why."
        )
