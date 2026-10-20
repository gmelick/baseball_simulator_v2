"""Doc guard: the Alembic head the operator docs cite is the newest migration on disk.

The operator manual (``WORKFLOW.md``) tells the operator what ``alembic current``
prints. When that prose lags the head, an operator who follows the check reads a
correct head as a failure. ``CLAUDE.md`` and the technical reference cite the head
too. This test reads the newest file under ``db/migrations/versions/`` and fails
when any of those citations lags it, so the next migration ticket updates the
prose in the same commit.

The patterns are deliberately narrow: they match the sentences that state the
CURRENT head, not the historical mentions of the migration that shipped a table
("Alembic 0015 ``raw.game_bullpen_availability``").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO / "db" / "migrations" / "versions"


def _newest_revision_on_disk() -> str:
    """The four-digit prefix of the newest Alembic migration file."""
    revisions = sorted(p.name[:4] for p in _VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert revisions, f"no migration files under {_VERSIONS}"
    return revisions[-1]


def test_newest_revision_is_a_four_digit_number() -> None:
    head = _newest_revision_on_disk()
    assert re.fullmatch(r"\d{4}", head), head


@pytest.mark.parametrize(
    ("doc", "pattern"),
    [
        # WORKFLOW.md: the header line ("Alembic head NNNN") and the summary line
        # ("DuckDB vNN / Alembic NNNN").
        ("WORKFLOW.md", r"Alembic (?:head )?(\d{4})"),
        # WORKFLOW.md: the two operator checks that quote what `alembic current` prints.
        ("WORKFLOW.md", r"`alembic current` print(?:s|ing)[^\n]*?`(\d{4})`"),
        # CLAUDE.md: the status line ("Alembic head **NNNN**") and the repo-map line
        # ("(Alembic, head **NNNN**").
        ("CLAUDE.md", r"Alembic,? head \*\*(\d{4})\*\*"),
        # CLAUDE.md: the architecture diagram ("DuckDB vNN / Alembic NNNN)").
        ("CLAUDE.md", r"/ Alembic (\d{4})\)"),
        # The technical reference's migration-system note.
        ("docs/technical/pipeline-betting-db.md", r"The Alembic head on disk is (\d{4})_"),
    ],
)
def test_docs_cite_the_on_disk_alembic_head(doc: str, pattern: str) -> None:
    text = (_REPO / doc).read_text(encoding="utf-8")
    cited = re.findall(pattern, text)
    assert cited, f"{doc}: nothing matches {pattern!r}; the sentence this guards was reworded"
    head = _newest_revision_on_disk()
    assert set(cited) == {head}, (
        f"{doc} cites Alembic head {sorted(set(cited))} but the newest migration file is "
        f"{head}; update the prose in the same commit as the migration"
    )
