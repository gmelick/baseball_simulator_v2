"""Doc guard: the DuckDB schema version the prose cites is the one on disk.

Why this test exists. ``CLAUDE.md``, ``WORKFLOW.md``, ``agent_team.md`` and the
Phase-6 handoff all told the reader the DuckDB schema was "v13" for fifteen
migrations after it moved on (the version file read 28 on 2026-09-16). An
operator who follows the ``WORKFLOW.md`` check reads a correct ``28`` as a
mismatch. ``tests/unit/test_docs_alembic_head.py`` guards the Alembic head the
same way; this file guards the DuckDB number so the next DuckDB migration ticket
updates the prose in the same commit as the version bump.

The truth is ``db/schemas/duckdb_schema_version.txt``, and
``tests/unit/test_sim_store.py`` already holds that file equal to the newest
numbered file under ``db/migrations/duckdb/``. This test only asks that the
prose agree with it.

The patterns are deliberately narrow: they match the sentences that state the
CURRENT schema version, not the historical mentions of the migration that
shipped a column ("migration 0023 (schema v23)").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_VERSION_FILE = _REPO / "db" / "schemas" / "duckdb_schema_version.txt"
_MIGRATIONS = _REPO / "db" / "migrations" / "duckdb"


def _version_on_disk() -> int:
    return int(_VERSION_FILE.read_text(encoding="utf-8").strip())


def _newest_migration_number() -> int:
    numbers = sorted(int(p.name[:4]) for p in _MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert numbers, f"no DuckDB migrations under {_MIGRATIONS}"
    return numbers[-1]


def test_version_file_matches_the_newest_migration() -> None:
    assert _version_on_disk() == _newest_migration_number()


@pytest.mark.parametrize(
    ("doc", "pattern"),
    [
        # CLAUDE.md: the status line ("DuckDB schema **v28**, Alembic head ...").
        ("CLAUDE.md", r"DuckDB schema \*\*v(\d+)\*\*"),
        # CLAUDE.md: the architecture diagram ("DuckDB v28 / Alembic 0025").
        ("CLAUDE.md", r"DuckDB v(\d+) / Alembic"),
        # CLAUDE.md: the repo map ("(numbered SQL, schema **v28**)").
        ("CLAUDE.md", r"numbered SQL, schema \*\*v(\d+)\*\*"),
        # WORKFLOW.md: the header line and the phase note ("DuckDB v28").
        ("WORKFLOW.md", r"DuckDB v(\d+)"),
        # WORKFLOW.md: the operator check's expected output.
        ("WORKFLOW.md", r":: Expected: (\d+)"),
        # WORKFLOW.md: the troubleshooting row ("currently `28`").
        ("WORKFLOW.md", r"latest migration \(currently `(\d+)`\)"),
        # agent_team.md: the tech-stack line.
        ("agent_team.md", r"DuckDB v(\d+) \(in-process\)"),
        # The Phase-6 handoff's persistence line.
        ("docs/HANDOFF_PHASE6.md", r"DuckDB v(\d+) /"),
        # The technical reference's migration-system note.
        ("docs/technical/pipeline-betting-db.md", r"duckdb_schema_version\.txt \((\d+) as of"),
    ],
)
def test_docs_cite_the_on_disk_duckdb_version(doc: str, pattern: str) -> None:
    text = (_REPO / doc).read_text(encoding="utf-8")
    cited = re.findall(pattern, text)
    assert cited, f"{doc}: nothing matches {pattern!r}; the sentence this guards was reworded"
    version = _version_on_disk()
    assert {int(c) for c in cited} == {version}, (
        f"{doc} cites DuckDB schema {sorted({int(c) for c in cited})} but "
        f"duckdb_schema_version.txt reads {version}; update the prose in the same "
        "commit as the version bump"
    )


@pytest.mark.parametrize(
    ("doc", "pattern"),
    [
        # WORKFLOW.md: the troubleshooting row names the newest migration file.
        ("WORKFLOW.md", r"through `(\d{4})_\*`"),
        # The technical reference names the newest migration file too.
        ("docs/technical/pipeline-betting-db.md", r"newest file db/migrations/duckdb/(\d{4})_"),
    ],
)
def test_docs_name_the_newest_duckdb_migration(doc: str, pattern: str) -> None:
    text = (_REPO / doc).read_text(encoding="utf-8")
    cited = re.findall(pattern, text)
    assert cited, f"{doc}: nothing matches {pattern!r}; the sentence this guards was reworded"
    newest = _newest_migration_number()
    assert {int(c) for c in cited} == {newest}, (
        f"{doc} names DuckDB migration {sorted({int(c) for c in cited})} but the newest "
        f"file is {newest:04d}; update the prose in the same commit as the migration"
    )
