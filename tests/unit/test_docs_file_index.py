"""Doc guard: the file index lists every source file in its review stage.

``scripts/docs_file_index.py`` writes ``docs/technical/file-index.md``. The page's
first line carries a key: a hash of each file's stage, path and first sentence.
This test recomputes the key from the checkout. It fails when the page lags the
code: a file was added, removed or moved to another stage, or its first sentence
changed. The fix is ``python scripts/docs_file_index.py``.

The stage titles also head the sections of ``docs/technical/code-review-path.md``,
so the review path and the index stay in step.

The test needs ``git`` and a git checkout. It skips without them: the app image
has neither, so the container lane skips it and CI runs it.
"""

from __future__ import annotations

import shutil

import pytest
from tests.script_loader import REPO, load_script

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(),
    reason="the file index reads git ls-files; this run has no git checkout",
)


@pytest.fixture(scope="module")
def index_module():
    return load_script("docs_file_index")


@pytest.fixture(scope="module")
def built(index_module):
    return index_module.build_rows()


def test_every_source_file_has_a_stage(built) -> None:
    _rows, unplaced = built
    assert not unplaced, (
        "no review stage claims these files; add a pattern to STAGES in "
        f"scripts/docs_file_index.py: {unplaced}"
    )


def test_the_index_is_current(index_module, built) -> None:
    rows, _unplaced = built
    text = index_module.INDEX.read_text(encoding="utf-8")
    assert index_module.committed_key(text) == index_module.index_key(rows), (
        "docs/technical/file-index.md is stale: a file was added, removed or moved, or "
        "its first sentence changed. Run: python scripts/docs_file_index.py"
    )


def test_the_review_path_heads_a_section_per_stage(index_module) -> None:
    path = REPO / "docs" / "technical" / "code-review-path.md"
    text = path.read_text(encoding="utf-8")
    for number, title, _summary, _patterns in index_module.STAGES:
        assert f'<a id="stage-{number}"></a>' in text, f"{path.name} has no anchor stage-{number}"
        assert f"## {number}. {title}" in text, (
            f"{path.name} has no heading '## {number}. {title}'; keep it equal to the "
            "stage title in scripts/docs_file_index.py"
        )
