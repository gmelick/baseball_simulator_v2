"""
tests/unit/test_sim525_backup_gauge.py
======================================
The backup-age gauge on /metrics (the scheduled backups, SIM-525).

The gauge ``baseball_sim_backup_age_hours{store=...}`` reports the hours since
the newest complete backup set of each store: postgres, duckdb and bundle.
A store with no complete set reads -1. A missing backup root reads -1 for
every store and never raises. The reading is cached on ``app.state`` for
600 seconds.

The tests build real backup sets under ``tmp_path``: a set is complete when
its ``manifest.json`` exists. The reader module (``pipeline.backup.status``)
is built in parallel; a test that needs it xfails when it cannot import.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import metrics as metrics_mod
from api.routes.metrics import router as metrics_router

STORES = ("postgres", "duckdb", "bundle")
_LINE = re.compile(r'^baseball_sim_backup_age_hours\{store="(\w+)"\} (\S+)$', re.MULTILINE)


def _need_status_module() -> None:
    """Xfail the calling test when the reader module is not importable yet."""
    try:
        import pipeline.backup.status  # noqa: F401
    except ImportError as exc:
        pytest.xfail(
            f"pipeline.backup.status is not built yet ({exc}); the integration stage reruns"
        )


def _make_set(root: Path, store: str, set_id: str, created_at: datetime) -> Path:
    """Write one complete backup set: a data file and its manifest.json."""
    set_dir = root / store / set_id
    set_dir.mkdir(parents=True)
    (set_dir / "data.bin").write_bytes(b"x" * 10)
    manifest = {
        "store": store,
        "set": set_id,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": 1.0,
        "bytes": 10,
        "host": "test",
        "tool": "db/backup/pg_backup.sh",
        "files": [{"path": "data.bin", "bytes": 10, "sha256": None}],
    }
    (set_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return set_dir


def _parse(body: str) -> dict[str, float]:
    """Read the per-store gauge values out of an exposition body."""
    return {store: float(value) for store, value in _LINE.findall(body)}


def _fresh_state() -> SimpleNamespace:
    return SimpleNamespace()


@pytest.fixture()
def postgres_only_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A backup root with one complete postgres set three hours old, and no other store."""
    root = tmp_path / "backups"
    created = datetime.now(UTC) - timedelta(hours=3)
    _make_set(root, "postgres", created.strftime("%Y%m%dT%H%M%SZ"), created)
    # A set in progress (no manifest) under duckdb must not count.
    (root / "duckdb" / "20261015T100000Z.partial").mkdir(parents=True)
    monkeypatch.setenv("BACKUPS_ROOT", str(root))
    return root


def test_the_fallback_renderer_reads_one_complete_set(postgres_only_root: Path) -> None:
    _need_status_module()
    body = metrics_mod._render_fallback(_fresh_state(), 1)  # noqa: SLF001
    assert "# HELP baseball_sim_backup_age_hours Hours since the newest complete backup set" in body
    assert "# TYPE baseball_sim_backup_age_hours gauge" in body
    ages = _parse(body)
    assert set(ages) == set(STORES)
    assert 2.9 <= ages["postgres"] <= 3.2
    assert ages["duckdb"] == -1
    assert ages["bundle"] == -1


def test_the_endpoint_reads_one_complete_set(postgres_only_root: Path) -> None:
    _need_status_module()
    app = FastAPI()
    app.include_router(metrics_router)
    body = TestClient(app).get("/metrics").text
    ages = _parse(body)
    assert set(ages) == set(STORES)
    assert 2.9 <= ages["postgres"] <= 3.2
    assert ages["duckdb"] == -1
    assert ages["bundle"] == -1


def test_a_missing_root_reads_minus_one_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    ages = _parse(metrics_mod._render_fallback(_fresh_state(), 1))  # noqa: SLF001
    assert ages == dict.fromkeys(STORES, -1.0)
    app = FastAPI()
    app.include_router(metrics_router)
    ages = _parse(TestClient(app).get("/metrics").text)
    assert ages == dict.fromkeys(STORES, -1.0)


def test_the_reading_is_cached_for_600_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_read() -> dict[str, float]:
        calls.append(1)
        return {"postgres": float(len(calls)), "duckdb": -1.0, "bundle": -1.0}

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(metrics_mod, "_read_backup_ages", fake_read)
    monkeypatch.setattr(metrics_mod.time, "time", lambda: clock["now"])
    state = _fresh_state()

    assert metrics_mod._backup_ages(state)["postgres"] == 1.0  # noqa: SLF001
    assert state.backup_ages_checked_at == 1_000_000.0
    clock["now"] += metrics_mod.BACKUP_AGE_REFRESH_S - 1
    assert metrics_mod._backup_ages(state)["postgres"] == 1.0  # noqa: SLF001
    assert len(calls) == 1
    clock["now"] += 1
    assert metrics_mod._backup_ages(state)["postgres"] == 2.0  # noqa: SLF001
    assert len(calls) == 2
    assert state.backup_ages["postgres"] == 2.0
    assert metrics_mod.BACKUP_AGE_REFRESH_S == 600


def test_a_reader_failure_reads_minus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _need_status_module()
    import pipeline.backup.status as status

    def boom(root: Path) -> dict[str, object]:
        raise OSError("disk gone")

    monkeypatch.setattr(status, "newest_complete_sets", boom)
    assert metrics_mod._read_backup_ages() == dict.fromkeys(STORES, -1.0)  # noqa: SLF001
