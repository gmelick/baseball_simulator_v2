"""The helpers that the data-store backup and the off-site copy share."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from operations.backup import back_up_data_stores as ds
from operations.backup import common
from operations.backup import copy_offsite as off


class FakeClock:
    """A clock that a sleep moves forward."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_both_jobs_use_the_one_wait_prune_and_stamp() -> None:
    assert ds.wait_while_busy is common.wait_while_busy
    assert off.wait_while_busy is common.wait_while_busy
    assert ds.prune_victims is common.prune_victims
    assert off.prune_victims is common.prune_victims
    assert ds.iso_utc is common.iso_utc
    assert off.iso_utc is common.iso_utc
    assert ds.root_path is common.root_path
    assert off.root_path is common.root_path


def test_iso_utc_writes_utc_with_a_z() -> None:
    eastern = timezone(timedelta(hours=-5))
    assert common.iso_utc(datetime(2026, 11, 24, 5, 6, 7, tzinfo=eastern)) == (
        "2026-11-24T10:06:07Z"
    )


def test_root_path_takes_the_flag_then_the_variable_then_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOME_ROOT", "/from/env")
    assert common.root_path(tmp_path, "SOME_ROOT", "/default") == tmp_path
    assert common.root_path(None, "SOME_ROOT", "/default") == Path("/from/env")
    monkeypatch.setenv("SOME_ROOT", "")
    assert common.root_path(None, "SOME_ROOT", "/default") == Path("/default")


def test_fsync_fd_ignores_a_file_system_that_cannot_fsync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_fd: int) -> None:
        raise OSError("not supported")

    monkeypatch.setattr(common.os, "fsync", refuse)
    common.fsync_fd(0)  # no error


def test_fsync_path_flushes_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flushed: list[int] = []
    monkeypatch.setattr(common.os, "fsync", flushed.append)
    target = tmp_path / "f"
    target.write_text("x")
    common.fsync_path(target)
    assert len(flushed) == 1
    assert os.path.exists(target)


def test_the_wait_returns_at_once_when_nothing_runs() -> None:
    clock = FakeClock()
    logs: list[str] = []
    ok = common.wait_while_busy(
        lambda _now: [],
        grace_s=0,
        wait_s=60,
        poll_s=10,
        clock=clock,
        sleep=clock.sleep,
        log=logs.append,
    )
    assert ok
    assert clock.sleeps == []
    assert logs == []


def test_the_wait_sleeps_the_grace_then_polls_until_quiet() -> None:
    clock = FakeClock()
    quiet_at = clock.now + 5 + 20
    logs: list[str] = []
    ok = common.wait_while_busy(
        lambda now: ["the job"] if now < quiet_at else [],
        grace_s=5,
        wait_s=600,
        poll_s=10,
        clock=clock,
        sleep=clock.sleep,
        log=logs.append,
    )
    assert ok
    assert clock.sleeps == [5, 10, 10]
    assert logs == ["the job runs; waiting 10 s"] * 2


def test_the_wait_gives_up_at_its_limit() -> None:
    clock = FakeClock()
    logs: list[str] = []
    ok = common.wait_while_busy(
        lambda _now: ["a", "b"],
        grace_s=0,
        wait_s=30,
        poll_s=10,
        clock=clock,
        sleep=clock.sleep,
        log=logs.append,
    )
    assert not ok
    assert sum(clock.sleeps) == 30
    assert logs[-1] == "a, b still runs after 30 s; the wait gives up"


def _t(hour: int) -> datetime:
    return datetime(2026, 11, 24, hour, tzinfo=UTC)


def test_prune_keeps_the_newest_and_never_a_protected_set() -> None:
    sets = [(_t(3), "c"), (_t(1), "a"), (_t(2), "b"), (_t(4), "d")]
    assert common.prune_victims(sets, 2, set()) == ["a", "b"]
    assert common.prune_victims(sets, 2, {"a"}) == ["b"]
    assert common.prune_victims(sets, 0, {"d"}) == ["a", "b", "c"]


def test_prune_sets_never_deletes_the_current_set(tmp_path: Path) -> None:
    store = tmp_path / "duckdb"
    for set_id in ("20261101T100000Z", "20261102T100000Z", "20261103T100000Z"):
        (store / set_id).mkdir(parents=True)
        (store / set_id / "manifest.json").write_text("{}", encoding="utf-8")
    removed = ds.prune_sets(tmp_path, "duckdb", 1, current="20261101T100000Z")
    assert removed == ["20261102T100000Z"]
    assert sorted(p.name for p in store.iterdir()) == ["20261101T100000Z", "20261103T100000Z"]
