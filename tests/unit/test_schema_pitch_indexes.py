"""The indexes on ``raw.pitches``, in the canonical schema and in their migrations:
the composite situation index (the situation similarity engine), the
(pitcher, season) clean-row index (the profile computor's hot path), and no
standalone index on the audit-only ``pitch_type`` column."""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]

_PG_SCHEMA_PATH = _ROOT / "db" / "schemas" / "01_postgres_schema.sql"
_MIG_DIR = _ROOT / "db" / "migrations" / "versions"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


class TestSituationIndex:
    """The composite partial index the situation similarity engine reads."""

    def test_index_present_in_canonical_schema(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        assert "idx_pitches_situation" in sql, (
            "SIM-085: idx_pitches_situation missing from 01_postgres_schema.sql. "
            "The situation similarity engine (SIM-070) requires this index."
        )
        # The shape: composite, partial, these columns, this predicate.
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


class TestNoPitchTypeIndex:
    """The audit-only ``pitch_type`` column carries no standalone index: no
    query filters on it, and the index costs every write."""

    def test_canonical_schema_no_pitch_type_index(self) -> None:
        sql = _read(_PG_SCHEMA_PATH)
        # The (pitcher, pitch_type) compound index also names pitch_type and
        # stays for ad-hoc queries; only the standalone index is gone.
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
        """No pipeline SQL filters on ``pitch_type``.

        The test greps the pipeline package for ``WHERE …pitch_type = …`` or
        ``WHERE …pitch_type IN (…)``. A SELECT column or a Python dict key is
        allowed; only a SQL predicate fails.
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


class TestPitcherSeasonCleanIndex:
    """The profile computor's hot path reads (pitcher, season) over clean rows."""

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
