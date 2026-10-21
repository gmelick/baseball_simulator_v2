"""Doc guard: the DuckDB schema version the living docs cite is the one on disk.

The living docs state the current version with one phrase, ``DuckDB schema
**vNN**``. The operator manual (``WORKFLOW.md``) also shows what its version check
prints. When that prose lags the version file, an operator who follows the check
reads a correct version as a mismatch. ``tests/unit/test_docs_alembic_head.py``
guards the Alembic head the same way; this file guards the DuckDB number so the
next DuckDB migration ticket updates the prose in the same commit as the bump.

The truth is ``db/schemas/duckdb_schema_version.txt``. The first test holds that
file equal to the newest numbered file under ``db/migrations/duckdb/``
(``tests/unit/test_sim_store.py`` checks it too). The rest ask that the prose agree.

The patterns are deliberately narrow: they match the phrases that state the
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
        # The phrase every living doc uses for the current version.
        ("CLAUDE.md", r"DuckDB schema \*\*v(\d+)\*\*"),
        ("WORKFLOW.md", r"DuckDB schema \*\*v(\d+)\*\*"),
        ("docs/technical/architecture.md", r"DuckDB schema \*\*v(\d+)\*\*"),
        ("docs/technical/database.md", r"DuckDB schema \*\*v(\d+)\*\*"),
        # WORKFLOW.md: what the operator's version check prints.
        ("WORKFLOW.md", r"(?m)^:: Expected: (\d+)$"),
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
