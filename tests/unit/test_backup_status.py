"""SIM-525 — the reader of the backup sets (pipeline/backup/status.py).

A set is complete only when its manifest exists. The API's backup-age gauge
reads this module, so it must not import duckdb.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pipeline.backup import status


def _make_set(root: Path, store: str, set_id: str, *, complete: bool = True, **manifest) -> Path:
    path = root / store / set_id
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(b"x" * 10)
    if complete:
        body = {"store": store, "set": set_id, "bytes": 10}
        body.update(manifest)
        (path / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return path


def test_only_sets_with_a_manifest_are_complete(tmp_path):
    _make_set(tmp_path, "duckdb", "20261013T100000Z")
    _make_set(tmp_path, "duckdb", "20261014T100000Z")
    _make_set(tmp_path, "duckdb", "20261015T100000Z", complete=False)
    (tmp_path / "duckdb" / "20261016T100000Z.partial").mkdir()
    sets = status.complete_sets(tmp_path, "duckdb")
    assert [s.set_id for s in sets] == ["20261013T100000Z", "20261014T100000Z"]
    newest = status.newest_complete_set(tmp_path, "duckdb")
    assert newest is not None
    assert newest.set_id == "20261014T100000Z"
    assert newest.bytes == 10
    assert newest.created_at == datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def test_the_manifest_created_at_beats_the_set_name(tmp_path):
    _make_set(tmp_path, "bundle", "20261014T100000Z", created_at="2026-10-14T10:05:30Z")
    info = status.newest_complete_set(tmp_path, "bundle")
    assert info is not None
    assert info.created_at == datetime(2026, 10, 14, 10, 5, 30, tzinfo=UTC)


def test_newest_complete_sets_names_every_store(tmp_path):
    _make_set(tmp_path, "postgres", "20261014T100000Z")
    got = status.newest_complete_sets(tmp_path)
    assert set(got) == set(status.STORES)
    assert got["postgres"] is not None
    assert got["duckdb"] is None
    assert got["bundle"] is None


def test_a_missing_root_reads_as_no_sets(tmp_path):
    got = status.newest_complete_sets(tmp_path / "nowhere")
    assert got == {"postgres": None, "duckdb": None, "bundle": None}


def test_age_hours(tmp_path):
    _make_set(tmp_path, "duckdb", "20261014T100000Z")
    info = status.newest_complete_set(tmp_path, "duckdb")
    now = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    assert status.age_hours(info, now) == 18.0
    assert status.age_hours(None, now) == -1.0


def test_status_does_not_import_duckdb():
    code = (
        "import sys; import pipeline.backup.status; sys.exit(1 if 'duckdb' in sys.modules else 0)"
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True)
    assert result.returncode == 0, result.stderr
