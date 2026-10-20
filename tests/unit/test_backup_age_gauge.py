"""
tests/unit/test_backup_age_gauge.py
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

The off-site copy (SIM-568) adds a second gauge,
``baseball_sim_offsite_backup_age_hours{store=...}``. A daemon thread reads
``OFFSITE_ROOT`` at most every 600 seconds, one read at a time, and never on
the event loop. The scrape renders only the stored reading, aged by the hours
since the read, or -1 for every store before the first read.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# The off-site copy (SIM-568): the gauge baseball_sim_offsite_backup_age_hours.
#
# The scrape never reads the off-site folder. A background task reads
# OFFSITE_ROOT on a worker thread at most every 600 seconds and stores the
# reading on app.state.offsite_backup_ages; the scrape renders that reading,
# or -1 for every store before the first read.
# ---------------------------------------------------------------------------

_OFFSITE_LINE = re.compile(
    r'^baseball_sim_offsite_backup_age_hours\{store="(\w+)"\} (\S+)$', re.MULTILINE
)


def _parse_offsite(body: str) -> dict[str, float]:
    """Read the per-store off-site gauge values out of an exposition body."""
    return {store: float(value) for store, value in _OFFSITE_LINE.findall(body)}


_STORED = {"postgres": 1.5, "duckdb": 26.25, "bundle": -1.0}


def test_the_offsite_reader_reads_offsite_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_status_module()
    root = tmp_path / "offsite"
    created = datetime.now(UTC) - timedelta(hours=3)
    _make_set(root, "duckdb", created.strftime("%Y%m%dT%H%M%SZ"), created)
    monkeypatch.setenv("OFFSITE_ROOT", str(root))
    # The local root holds nothing: the off-site reader must not read it.
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_local"))
    ages = metrics_mod._read_offsite_ages()  # noqa: SLF001
    assert set(ages) == set(STORES)
    assert 2.9 <= ages["duckdb"] <= 3.2
    assert ages["postgres"] == -1
    assert ages["bundle"] == -1
    assert metrics_mod._read_backup_ages() == dict.fromkeys(STORES, -1.0)  # noqa: SLF001


def test_a_missing_offsite_root_reads_minus_one_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFFSITE_ROOT", str(tmp_path / "no_such_dir"))
    assert metrics_mod._read_offsite_ages() == dict.fromkeys(STORES, -1.0)  # noqa: SLF001


def test_the_fallback_carries_the_offsite_help_and_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    body = metrics_mod._render_fallback(_fresh_state(), 1)  # noqa: SLF001
    assert (
        "# HELP baseball_sim_offsite_backup_age_hours Age in hours of the store's newest "
        "backup set that is complete off-site; -1 when none." in body
    )
    assert "# TYPE baseball_sim_offsite_backup_age_hours gauge" in body
    # Nothing stored yet: -1 for every store.
    assert _parse_offsite(body) == dict.fromkeys(STORES, -1.0)


def test_the_fallback_renders_the_stored_offsite_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    state = SimpleNamespace(offsite_backup_ages=dict(_STORED))
    body = metrics_mod._render_fallback(state, 1)  # noqa: SLF001
    assert _parse_offsite(body) == _STORED


def test_the_endpoint_renders_the_stored_offsite_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    # An off-site root with a fresh set: the scrape must render the stored
    # reading, not this folder.
    root = tmp_path / "offsite"
    created = datetime.now(UTC) - timedelta(hours=3)
    for store in STORES:
        _make_set(root, store, created.strftime("%Y%m%dT%H%M%SZ"), created)
    monkeypatch.setenv("OFFSITE_ROOT", str(root))
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.offsite_backup_ages = dict(_STORED)
    # A check time of now holds the background refresh back.
    app.state.offsite_checked_at = metrics_mod.time.time()
    body = TestClient(app).get("/metrics").text
    assert _parse_offsite(body) == _STORED


def test_the_endpoint_reads_minus_one_before_the_first_offsite_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    monkeypatch.setenv("OFFSITE_ROOT", str(tmp_path / "no_offsite"))
    app = FastAPI()
    app.include_router(metrics_router)
    body = TestClient(app).get("/metrics").text
    assert _parse_offsite(body) == dict.fromkeys(STORES, -1.0)


def test_the_scrape_never_reads_the_offsite_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))

    def must_not_run() -> dict[str, float]:
        raise AssertionError("the scrape read the off-site folder")

    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", must_not_run)
    state = SimpleNamespace(offsite_backup_ages=dict(_STORED))
    # No running loop here, so no background task either.
    body = metrics_mod._render_fallback(state, 1)  # noqa: SLF001
    assert _parse_offsite(body) == _STORED


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Poll ``predicate`` on the running loop until it holds, or fail after ``timeout_s``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the off-site read")
        await asyncio.sleep(0.01)


def test_the_offsite_refresh_runs_once_per_600_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    threads: list[threading.Thread] = []

    def fake_read() -> dict[str, float]:
        calls.append(1)
        threads.append(threading.current_thread())
        return {"postgres": float(len(calls)), "duckdb": -1.0, "bundle": -1.0}

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", fake_read)
    monkeypatch.setattr(metrics_mod.time, "time", lambda: clock["now"])
    state = _fresh_state()
    loop_threads: list[threading.Thread] = []

    async def scrape() -> None:
        loop_threads.append(threading.current_thread())
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        task = getattr(state, "offsite_refresh_task", None)
        if task is not None:
            await task

    asyncio.run(scrape())
    assert state.offsite_checked_at == 1_000_000.0
    assert state.offsite_backup_ages["postgres"] == 1.0
    assert state.offsite_backup_read_at == 1_000_000.0
    assert len(calls) == 1
    # The read never runs on the event loop, and never on the loop's default
    # executor: it runs on a daemon thread of its own (the off-site copy, SIM-568).
    assert threads[0] is not loop_threads[0]
    assert threads[0].daemon is True
    assert threads[0].name == metrics_mod.OFFSITE_READ_THREAD_NAME

    state.offsite_refresh_task = None
    clock["now"] += metrics_mod.OFFSITE_AGE_REFRESH_S - 1
    asyncio.run(scrape())
    assert state.offsite_checked_at == 1_000_000.0
    assert len(calls) == 1

    clock["now"] += 1
    asyncio.run(scrape())
    assert state.offsite_checked_at == 1_000_000.0 + metrics_mod.OFFSITE_AGE_REFRESH_S
    assert state.offsite_backup_ages["postgres"] == 2.0
    assert len(calls) == 2
    assert metrics_mod.OFFSITE_AGE_REFRESH_S == 600


def test_the_offsite_refresh_needs_a_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run() -> dict[str, float]:
        raise AssertionError("the off-site folder was read without a loop")

    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", must_not_run)
    state = _fresh_state()
    metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
    assert not hasattr(state, "offsite_checked_at")
    assert not hasattr(state, "offsite_refresh_task")
    assert not hasattr(state, "offsite_backup_ages")


def test_a_failed_offsite_read_keeps_the_app_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, float]:
        raise OSError("OneDrive offline")

    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", boom)
    state = _fresh_state()
    asyncio.run(metrics_mod._refresh_offsite_ages(state))  # noqa: SLF001
    assert not hasattr(state, "offsite_backup_ages")
    assert not hasattr(state, "offsite_backup_read_at")
    assert metrics_mod._offsite_ages(state) == dict.fromkeys(STORES, -1.0)  # noqa: SLF001


def test_one_offsite_read_runs_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled read holds back every later scrape's read (the off-site copy, SIM-568)."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    threads: list[threading.Thread] = []

    def fake_read() -> dict[str, float]:
        calls.append(1)
        threads.append(threading.current_thread())
        if len(calls) == 1:
            started.set()
            release.wait(10)
        return {"postgres": float(len(calls)), "duckdb": -1.0, "bundle": -1.0}

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", fake_read)
    monkeypatch.setattr(metrics_mod.time, "time", lambda: clock["now"])
    state = _fresh_state()

    async def scenario() -> None:
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        first = state.offsite_refresh_task
        await _wait_until(started.is_set)
        # Three scrapes 600 s apart while the first read stalls: none starts a read.
        for _ in range(3):
            clock["now"] += metrics_mod.OFFSITE_AGE_REFRESH_S
            metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
            assert state.offsite_refresh_task is first
        assert len(calls) == 1
        assert sum(t.is_alive() for t in threads) == 1
        assert state.offsite_checked_at == 1_000_000.0
        release.set()
        await first
        assert state.offsite_backup_ages["postgres"] == 1.0
        # The read ended and the check is due: the next scrape starts the next read.
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        second = state.offsite_refresh_task
        assert second is not first
        await second
        assert len(calls) == 2
        assert state.offsite_backup_ages["postgres"] == 2.0

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_a_stalled_offsite_read_lets_the_age_grow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scrape ages the last reading, so a stall never reads fresh (SIM-568)."""
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def fake_read() -> dict[str, float]:
        calls.append(1)
        if len(calls) == 1:
            return {"postgres": 3.0, "duckdb": -1.0, "bundle": 3.0}
        started.set()
        release.wait(10)
        return {"postgres": 0.5, "duckdb": -1.0, "bundle": 0.5}

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", fake_read)
    monkeypatch.setattr(metrics_mod.time, "time", lambda: clock["now"])
    state = _fresh_state()

    def ages() -> dict[str, float]:
        return metrics_mod._offsite_ages(state)  # noqa: SLF001

    async def scenario() -> None:
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        await state.offsite_refresh_task
        assert ages() == {"postgres": 3.0, "duckdb": -1.0, "bundle": 3.0}
        # The second read starts and stalls.
        clock["now"] += metrics_mod.OFFSITE_AGE_REFRESH_S
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        await _wait_until(started.is_set)
        assert ages()["postgres"] == pytest.approx(3.0 + 600 / 3600)
        # Forty minutes later the age has grown with the clock.
        clock["now"] += 40 * 60
        assert ages()["postgres"] == pytest.approx(3.0 + 3000 / 3600)
        assert ages()["duckdb"] == -1.0
        # Ten hours later the gauge itself reads the grown age.
        clock["now"] += 10 * 3600
        rendered = _parse_offsite(metrics_mod._render_fallback(state, 1))  # noqa: SLF001
        assert rendered["bundle"] == pytest.approx(3.0 + 39_000 / 3600, abs=1e-3)
        assert rendered["duckdb"] == -1.0
        # The stalled read ends: its own reading replaces the aged one.
        release.set()
        await state.offsite_refresh_task
        assert ages() == {"postgres": 0.5, "duckdb": -1.0, "bundle": 0.5}

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_the_endpoint_answers_while_the_offsite_read_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /metrics keeps answering at once with -1 while the read is blocked (SIM-568)."""
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    reading = {"postgres": 5.0, "duckdb": -1.0, "bundle": 2.0}

    def blocking_read() -> dict[str, float]:
        calls.append(1)
        started.set()
        release.wait(10)
        return dict(reading)

    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", blocking_read)
    app = FastAPI()
    app.include_router(metrics_router)

    def timed_scrape(client: TestClient) -> tuple[str, float]:
        begin = time.monotonic()
        body = client.get("/metrics").text
        return body, time.monotonic() - begin

    try:
        with TestClient(app) as client:
            body, elapsed = timed_scrape(client)
            assert started.wait(5), "the scrape never started the off-site read"
            assert elapsed < 5.0
            assert _parse_offsite(body) == dict.fromkeys(STORES, -1.0)
            # A second scrape while the read is blocked: a read on the event loop
            # would hold this request until the read ends.
            body, elapsed = timed_scrape(client)
            assert not release.is_set()
            assert elapsed < 5.0
            assert _parse_offsite(body) == dict.fromkeys(STORES, -1.0)
            assert len(calls) == 1
            # The read ends; the next scrape renders its reading, aged by a moment.
            release.set()
            deadline = time.monotonic() + 5.0
            while getattr(app.state, "offsite_backup_ages", None) is None:
                assert time.monotonic() < deadline, "the off-site reading never arrived"
                time.sleep(0.01)
            rendered = _parse_offsite(client.get("/metrics").text)
            assert rendered["postgres"] == pytest.approx(5.0, abs=0.01)
            assert rendered["bundle"] == pytest.approx(2.0, abs=0.01)
            assert rendered["duckdb"] == -1.0
    finally:
        release.set()


def test_a_read_that_outlives_its_loop_is_dropped_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that ends after its loop closed raises nothing and stores nothing (SIM-568)."""
    errors: list[object] = []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    started = threading.Event()
    release = threading.Event()
    threads: list[threading.Thread] = []

    def blocking_read() -> dict[str, float]:
        threads.append(threading.current_thread())
        started.set()
        release.wait(10)
        return {"postgres": 1.0, "duckdb": 1.0, "bundle": 1.0}

    monkeypatch.setattr(metrics_mod, "_read_offsite_ages", blocking_read)
    state = _fresh_state()

    async def scenario() -> None:
        metrics_mod._schedule_finals_refresh(state)  # noqa: SLF001
        await _wait_until(started.is_set)

    try:
        # The run ends with the read in flight: asyncio.run cancels the task.
        asyncio.run(scenario())
        assert state.offsite_refresh_task.cancelled()
    finally:
        release.set()
    threads[0].join(5)
    assert not threads[0].is_alive()
    assert errors == []
    assert not hasattr(state, "offsite_backup_ages")


def test_the_local_line_regex_still_finds_three_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKUPS_ROOT", str(tmp_path / "no_such_dir"))
    state = SimpleNamespace(offsite_backup_ages=dict(_STORED))
    body = metrics_mod._render_fallback(state, 1)  # noqa: SLF001
    assert len(_LINE.findall(body)) == 3
    assert len(_OFFSITE_LINE.findall(body)) == 3
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.offsite_backup_ages = dict(_STORED)
    app.state.offsite_checked_at = metrics_mod.time.time()
    body = TestClient(app).get("/metrics").text
    assert len(_LINE.findall(body)) == 3
    assert len(_OFFSITE_LINE.findall(body)) == 3
