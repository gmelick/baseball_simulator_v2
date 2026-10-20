"""SIM-568 — the off-site copy of the backup sets (pipeline/backup/offsite.py).

Every test runs on tmp_path: fake sets with real files and a real manifest
(bytes, sha256, created_at), and a fake clock whose sleep moves time, so the
waits take no time.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from pipeline.backup import offsite as off
from pipeline.backup.common import GIB
from pipeline.backup.status import STORES, complete_sets, parse_set_id

SET_ID = "20261015T100000Z"
FILES = {"payload.bin": b"x" * 10, "sub/part.bin": b"y" * 5}
SET_BYTES = 15

DiskUsage = namedtuple("DiskUsage", "total used free")


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "BACKUP_ROOT",
        "OFFSITE_ROOT",
        "INGEST_MARK_DIR",
        "BACKUP_KEEP",
        "OFFSITE_KEEP",
        "BACKUP_MIN_FREE_GB",
        "BACKUP_FREE_FACTOR",
    ):
        monkeypatch.delenv(name, raising=False)
    # The free-space floor is 10 GiB by default; the tests' disk has room unless a test says not.
    monkeypatch.setattr(off.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 10**15))


@pytest.fixture
def backups(tmp_path) -> Path:
    root = tmp_path / "backups"
    root.mkdir()
    return root


@pytest.fixture
def offsite(tmp_path) -> Path:
    root = tmp_path / "offsite"
    root.mkdir()
    return root


@pytest.fixture
def marks(tmp_path) -> Path:
    path = tmp_path / "marks"
    path.mkdir()
    return path


@pytest.fixture
def copies(monkeypatch) -> list[Path]:
    """The source files copy_and_hash copies (the real copy still runs)."""
    seen: list[Path] = []
    real = off.copy_and_hash

    def counting(src, dest):
        seen.append(src)
        return real(src, dest)

    monkeypatch.setattr(off, "copy_and_hash", counting)
    return seen


class FakeClock:
    """A clock the fake sleep moves forward."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fake_set(root: Path, store: str, set_id: str, files: dict[str, bytes] | None = None) -> Path:
    files = FILES if files is None else files
    path = root / store / set_id
    path.mkdir(parents=True, exist_ok=True)
    entries = []
    for rel, data in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append(
            {"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = {
        "store": store,
        "set": set_id,
        "created_at": parse_set_id(set_id).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bytes": sum(e["bytes"] for e in entries),
        "files": entries,
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


def _run(argv, backups, offsite, clock=None, marks=None, sleep=None) -> int:
    clock = clock or FakeClock()
    mark_dir = marks if marks is not None else backups.parent / "no_marks"
    return off.main(
        [
            "--backups-root",
            str(backups),
            "--offsite-root",
            str(offsite),
            "--mark-dir",
            str(mark_dir),
            *argv,
        ],
        clock=clock,
        sleep=sleep or clock.sleep,
    )


def _ids(root: Path, store: str) -> list[str]:
    return [s.set_id for s in complete_sets(root, store)]


def _mark(mark_dir: Path, name: str, epoch: float) -> None:
    mark_dir.mkdir(parents=True, exist_ok=True)
    (mark_dir / f"{name}.running").write_text(f"{int(epoch)}\n")


def _lock(backups: Path, epoch: float) -> None:
    lock = backups / "postgres" / ".lock"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "owner").write_text(f"4242 {int(epoch)}\n")


# --- the copy -------------------------------------------------------------------


def test_a_run_mirrors_the_newest_set_of_every_store(backups, offsite, capsys):
    for store in STORES:
        _fake_set(backups, store, SET_ID)
    assert _run([], backups, offsite) == 0
    out = capsys.readouterr().out
    assert "[backup-offsite " in out
    for store in STORES:
        assert _ids(offsite, store) == [SET_ID]
        src = backups / store / SET_ID
        dest = offsite / store / SET_ID
        assert (dest / "manifest.json").read_bytes() == (src / "manifest.json").read_bytes()
        assert (dest / "sub" / "part.bin").read_bytes() == FILES["sub/part.bin"]
        assert f"COPY OK {store} {SET_ID} {SET_BYTES} bytes " in out
    assert not list(offsite.rglob("*.tmp"))


def test_the_manifest_file_list_is_the_copy_list(backups, offsite):
    src = _fake_set(backups, "duckdb", SET_ID)
    (src / "stray.bin").write_bytes(b"not listed")
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    dest = offsite / "duckdb" / SET_ID
    got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert got == ["manifest.json", "payload.bin", "sub/part.bin"]


def test_a_second_run_copies_nothing(backups, offsite, capsys, copies):
    for store in STORES:
        _fake_set(backups, store, SET_ID)
    assert _run([], backups, offsite) == 0
    assert len(copies) == 6
    copies.clear()
    capsys.readouterr()
    assert _run([], backups, offsite) == 0
    out = capsys.readouterr().out
    assert copies == []
    assert out.count("COPY SKIP") == 3
    assert "COPY OK" not in out


def test_a_crash_mid_copy_leaves_no_manifest_and_no_tmp(backups, offsite, monkeypatch):
    _fake_set(backups, "bundle", SET_ID)
    real = off.copy_and_hash

    def crash(src, dest):
        if src.name == "part.bin":
            raise OSError("the disk went away")
        return real(src, dest)

    monkeypatch.setattr(off, "copy_and_hash", crash)
    assert _run(["--only", "bundle"], backups, offsite) == 1
    dest = offsite / "bundle" / SET_ID
    assert dest.is_dir()
    assert (dest / "payload.bin").is_file()
    assert not (dest / "manifest.json").exists()
    assert not list(dest.rglob("*.tmp"))
    assert _ids(offsite, "bundle") == []


def test_a_dead_folder_of_a_wanted_set_is_resumed(backups, offsite, copies):
    src = _fake_set(backups, "duckdb", SET_ID)
    dest = offsite / "duckdb" / SET_ID
    (dest / "sub").mkdir(parents=True)
    shutil.copyfile(src / "payload.bin", dest / "payload.bin")  # a good file
    (dest / "sub" / "part.bin").write_bytes(b"y" * 2)  # a truncated file
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    assert [p.name for p in copies] == ["part.bin"]
    assert (dest / "sub" / "part.bin").read_bytes() == FILES["sub/part.bin"]
    assert _ids(offsite, "duckdb") == [SET_ID]


def test_a_damaged_complete_copy_loses_its_manifest_before_any_rewrite(
    backups, offsite, monkeypatch, capsys
):
    # SIM-568: the off-site set has its manifest and one truncated file. The
    # copy must delete the old manifest before it rewrites a file, so a crash
    # in the rewrite leaves a set that reads incomplete, never complete.
    src = _fake_set(backups, "duckdb", SET_ID)
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    dest = offsite / "duckdb" / SET_ID
    (dest / "sub" / "part.bin").write_bytes(b"y" * 2)
    real = off.copy_and_hash
    seen: list[tuple[str, bool]] = []

    def checking(s, d):
        seen.append((s.name, (dest / "manifest.json").exists()))
        return real(s, d)

    monkeypatch.setattr(off, "copy_and_hash", checking)
    capsys.readouterr()
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    assert f"COPY OK duckdb {SET_ID} {SET_BYTES} bytes " in capsys.readouterr().out
    assert seen == [("part.bin", False)]
    assert (dest / "manifest.json").read_bytes() == (src / "manifest.json").read_bytes()
    assert (dest / "sub" / "part.bin").read_bytes() == FILES["sub/part.bin"]
    assert off.status_rows(backups, offsite)["duckdb"]["mirrored"] is True


def test_a_crash_while_a_damaged_complete_copy_is_rewritten_leaves_it_incomplete(
    backups, offsite, monkeypatch
):
    _fake_set(backups, "duckdb", SET_ID)
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    dest = offsite / "duckdb" / SET_ID
    (dest / "sub" / "part.bin").write_bytes(b"y" * 2)

    def crash(_src, _dest):
        raise OSError("the disk went away")

    monkeypatch.setattr(off, "copy_and_hash", crash)
    assert _run(["--only", "duckdb"], backups, offsite) == 1
    assert not (dest / "manifest.json").exists()
    assert _ids(offsite, "duckdb") == []


def test_a_same_size_wrong_file_is_recopied(backups, offsite, copies):
    src = _fake_set(backups, "duckdb", SET_ID)
    dest = offsite / "duckdb" / SET_ID
    dest.mkdir(parents=True)
    (dest / "payload.bin").write_bytes(b"z" * 10)  # the right size, the wrong bytes
    assert _run(["--only", "duckdb"], backups, offsite) == 0
    assert "payload.bin" in [p.name for p in copies]
    assert (dest / "payload.bin").read_bytes() == (src / "payload.bin").read_bytes()


def test_dead_folders_of_unwanted_sets_and_partials_are_deleted(backups, offsite):
    _fake_set(backups, "postgres", SET_ID)
    (offsite / "postgres" / "20261001T100000Z" / "dump").mkdir(parents=True)  # no manifest
    (offsite / "postgres" / f"{SET_ID}.partial").mkdir(parents=True)
    assert _run(["--only", "postgres"], backups, offsite) == 0
    assert sorted(p.name for p in (offsite / "postgres").iterdir()) == [SET_ID]


def test_keep_two_of_five_local_sets_copies_the_newest_two(backups, offsite):
    for day in range(1, 6):
        _fake_set(backups, "duckdb", f"2026100{day}T100000Z")
    assert _run(["--only", "duckdb", "--keep", "2"], backups, offsite) == 0
    assert _ids(offsite, "duckdb") == ["20261004T100000Z", "20261005T100000Z"]


def test_keep_precedence_and_zero_reads_as_one(monkeypatch):
    assert off.resolve_keep(None) == 3
    monkeypatch.setenv("BACKUP_KEEP", "4")
    assert off.resolve_keep(None) == 4
    monkeypatch.setenv("OFFSITE_KEEP", "2")
    assert off.resolve_keep(None) == 2
    assert off.resolve_keep(5) == 5  # the flag beats both variables
    assert off.resolve_keep(0) == 1
    assert off.resolve_keep(-3) == 1
    monkeypatch.setenv("OFFSITE_KEEP", "0")
    assert off.resolve_keep(None) == 1
    monkeypatch.setenv("OFFSITE_KEEP", "many")  # not a number: the next source answers
    assert off.resolve_keep(None) == 4


def test_offsite_keep_beats_backup_keep_in_a_run(backups, offsite, monkeypatch):
    for day in (1, 2, 3):
        _fake_set(backups, "bundle", f"2026100{day}T100000Z")
    monkeypatch.setenv("BACKUP_KEEP", "3")
    monkeypatch.setenv("OFFSITE_KEEP", "1")
    assert _run(["--only", "bundle"], backups, offsite) == 0
    assert _ids(offsite, "bundle") == ["20261003T100000Z"]


def test_only_postgres(backups, offsite):
    for store in STORES:
        _fake_set(backups, store, SET_ID)
    assert _run(["--only", "postgres"], backups, offsite) == 0
    assert _ids(offsite, "postgres") == [SET_ID]
    assert not (offsite / "duckdb").exists()
    assert not (offsite / "bundle").exists()


# --- retention --------------------------------------------------------------------


def test_retention_keeps_the_newest_off_site_sets(backups, offsite):
    for day in (1, 2, 3):
        _fake_set(offsite, "bundle", f"2026100{day}T100000Z")
    _fake_set(backups, "bundle", "20261005T100000Z")
    assert _run(["--only", "bundle", "--keep", "2"], backups, offsite) == 0
    assert _ids(offsite, "bundle") == ["20261003T100000Z", "20261005T100000Z"]
    assert sorted(p.name for p in (offsite / "bundle").iterdir()) == [
        "20261003T100000Z",
        "20261005T100000Z",
    ]


def test_retention_never_prunes_a_set_copied_this_run(backups, offsite):
    _fake_set(offsite, "bundle", "20261009T100000Z")  # newer than any local set
    _fake_set(backups, "bundle", "20261005T100000Z")
    assert _run(["--only", "bundle", "--keep", "1"], backups, offsite) == 0
    assert _ids(offsite, "bundle") == ["20261005T100000Z", "20261009T100000Z"]


def test_retention_unlinks_the_manifest_before_the_folder(backups, offsite, monkeypatch):
    # SIM-568: the order matters for a partial delete. A folder delete that dies
    # with the manifest still in place leaves a set that reads complete with
    # files missing. So every folder delete of a pruned set must find no manifest.
    for day in (1, 2):
        _fake_set(offsite, "bundle", f"2026100{day}T100000Z")
    _fake_set(backups, "bundle", "20261005T100000Z")
    real_rmtree = shutil.rmtree
    calls: list[tuple[str, bool]] = []

    def recording(path, *args, **kwargs):
        path = Path(path)
        calls.append((path.name, (path / "manifest.json").exists()))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(off.shutil, "rmtree", recording)
    assert _run(["--only", "bundle", "--keep", "1"], backups, offsite) == 0
    assert sorted(calls) == [("20261001T100000Z", False), ("20261002T100000Z", False)]
    assert sorted(p.name for p in (offsite / "bundle").iterdir()) == ["20261005T100000Z"]


def test_a_pruned_set_with_a_held_file_stays_incomplete_until_the_next_run(
    backups, offsite, monkeypatch, capsys
):
    for day in (1, 2):
        _fake_set(offsite, "bundle", f"2026100{day}T100000Z")
    _fake_set(backups, "bundle", "20261005T100000Z")
    real_rmtree = shutil.rmtree
    # rmtree(ignore_errors=True) deletes nothing it cannot, and says nothing.
    monkeypatch.setattr(off.shutil, "rmtree", lambda *_a, **_k: None)
    assert _run(["--only", "bundle", "--keep", "1"], backups, offsite) == 0
    assert "a held file keeps its folder" in capsys.readouterr().out
    assert _ids(offsite, "bundle") == ["20261005T100000Z"]
    for day in (1, 2):
        folder = offsite / "bundle" / f"2026100{day}T100000Z"
        assert folder.is_dir()  # the folder stays...
        assert not (folder / "manifest.json").exists()  # ...but no longer reads as complete
    # The next run deletes the two dead folders.
    monkeypatch.setattr(off.shutil, "rmtree", real_rmtree)
    assert _run(["--only", "bundle", "--keep", "1"], backups, offsite) == 0
    assert sorted(p.name for p in (offsite / "bundle").iterdir()) == ["20261005T100000Z"]


def test_an_off_site_set_newer_than_any_local_set_does_not_make_a_wanted_set_flap(
    backups, offsite, copies, capsys
):
    # SIM-568: local lost its newest set (a hand-deleted bad set). Retention must
    # not prune a set the local side still wants, or the next run copies it back.
    local = ["20261001T100000Z", "20261002T100000Z", "20261003T100000Z"]
    for set_id in local:
        _fake_set(backups, "bundle", set_id)
    assert _run(["--only", "bundle"], backups, offsite) == 0
    _fake_set(offsite, "bundle", "20261004T100000Z")
    copies.clear()
    for _night in range(3):
        assert _run(["--only", "bundle"], backups, offsite) == 0
        assert copies == []
        assert _ids(offsite, "bundle") == [*local, "20261004T100000Z"]
    capsys.readouterr()
    assert _run(["--only", "bundle", "--dry-run"], backups, offsite) == 0
    assert "WOULD PRUNE" not in capsys.readouterr().out
    # Once the local side drops its oldest set, that set leaves the off-site side too.
    shutil.rmtree(backups / "bundle" / local[0])
    _fake_set(backups, "bundle", "20261005T100000Z")
    assert _run(["--only", "bundle"], backups, offsite) == 0
    assert _ids(offsite, "bundle") == [
        "20261002T100000Z",
        "20261003T100000Z",
        "20261004T100000Z",
        "20261005T100000Z",
    ]


def test_a_failed_store_prunes_nothing_while_the_others_copy_and_prune(backups, offsite, capsys):
    for store in STORES:
        for day in (1, 2):
            _fake_set(offsite, store, f"2026100{day}T100000Z")
        _fake_set(backups, store, "20261005T100000Z")
    (backups / "duckdb" / "20261005T100000Z" / "payload.bin").write_bytes(b"short")
    assert _run(["--keep", "1"], backups, offsite) == 1
    assert "FAIL duckdb:" in capsys.readouterr().out
    assert _ids(offsite, "duckdb") == ["20261001T100000Z", "20261002T100000Z"]
    assert not (offsite / "duckdb" / "20261005T100000Z").exists()
    for store in ("postgres", "bundle"):
        assert _ids(offsite, store) == ["20261005T100000Z"]


# --- the checks on the bytes --------------------------------------------------------


def test_the_read_back_hash_catches_a_bad_copy_and_no_hash_does_not(
    backups, offsite, monkeypatch, capsys
):
    _fake_set(backups, "duckdb", SET_ID)
    real = off.copy_and_hash

    def flaky(src, dest):
        digest = real(src, dest)
        data = bytearray(dest.read_bytes())
        data[0] ^= 0xFF
        dest.write_bytes(bytes(data))
        return digest

    monkeypatch.setattr(off, "copy_and_hash", flaky)
    assert _run(["--only", "duckdb"], backups, offsite) == 1
    assert "read-back" in capsys.readouterr().out
    assert not (offsite / "duckdb" / SET_ID / "manifest.json").exists()
    shutil.rmtree(offsite / "duckdb")
    assert _run(["--only", "duckdb", "--no-hash"], backups, offsite) == 0
    assert _ids(offsite, "duckdb") == [SET_ID]


def test_a_source_whose_size_differs_from_its_manifest_is_refused_before_any_write(
    backups, offsite, capsys
):
    src = _fake_set(backups, "bundle", SET_ID)
    (src / "payload.bin").write_bytes(b"x" * 9)
    assert _run(["--only", "bundle"], backups, offsite) == 1
    assert "the local set is damaged" in capsys.readouterr().out
    assert not (offsite / "bundle" / SET_ID).exists()


def test_a_source_whose_bytes_differ_from_its_manifest_gets_no_manifest(backups, offsite, capsys):
    src = _fake_set(backups, "bundle", SET_ID)
    (src / "payload.bin").write_bytes(b"w" * 10)  # the right size, the wrong bytes
    assert _run(["--only", "bundle"], backups, offsite) == 1
    assert "does not match its manifest sha256" in capsys.readouterr().out
    assert not (offsite / "bundle" / SET_ID / "manifest.json").exists()


def test_a_manifest_path_outside_the_set_is_refused(backups, offsite):
    path = _fake_set(backups, "bundle", SET_ID)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["files"].append({"path": "../escape.bin", "bytes": 1, "sha256": "0" * 64})
    (path / "manifest.json").write_text(json.dumps(manifest))
    assert _run(["--only", "bundle"], backups, offsite) == 1
    assert not (offsite / "bundle" / SET_ID).exists()
    assert not (offsite / "bundle" / "escape.bin").exists()


# --- the refusals ------------------------------------------------------------------


def test_a_missing_root_is_refused(tmp_path, backups, offsite):
    assert _run([], tmp_path / "missing", offsite) == 2
    assert _run([], backups, tmp_path / "nowhere") == 2
    assert not (tmp_path / "nowhere").exists()


def test_an_off_site_root_that_cannot_be_written_is_refused(backups, offsite, monkeypatch):
    _fake_set(backups, "duckdb", SET_ID)
    monkeypatch.setattr(off.os, "access", lambda *_a, **_k: False)
    assert _run([], backups, offsite) == 2
    assert not (offsite / "duckdb").exists()


def test_the_same_folder_twice_is_refused_and_the_probe_is_gone(backups, monkeypatch, capsys):
    assert _run([], backups, backups) == 2
    # Two mounts of one folder have different paths; the probe file catches them.
    monkeypatch.setattr(off, "_same_by_realpath", lambda _a, _b: False)
    assert _run([], backups, backups) == 2
    assert "one folder" in capsys.readouterr().out
    assert not list(backups.glob("offsite-probe-*"))


def test_the_free_space_guard_refuses_before_any_write(backups, offsite, monkeypatch, capsys):
    _fake_set(backups, "duckdb", SET_ID)
    dead = offsite / "postgres" / "20261001T100000Z"
    dead.mkdir(parents=True)
    monkeypatch.setattr(off.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 9 * GIB))
    assert _run([], backups, offsite) == 2
    assert "not the OneDrive quota" in capsys.readouterr().out
    assert not (offsite / "duckdb").exists()
    assert dead.is_dir()  # the dead folder clean is a write too; it did not run


def test_the_guard_needs_the_factor_times_the_copy(backups, offsite, monkeypatch):
    monkeypatch.setenv("BACKUP_MIN_FREE_GB", "0")
    _fake_set(backups, "duckdb", SET_ID)  # 15 bytes; the factor 1.5 needs 22.5
    monkeypatch.setattr(off.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 22))
    assert _run([], backups, offsite) == 2
    monkeypatch.setattr(off.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 23))
    assert _run([], backups, offsite) == 0
    # Nothing is left to copy, so the guard does not run and a full disk does not refuse.
    monkeypatch.setattr(off.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 0))
    assert _run([], backups, offsite) == 0


# --- the wait ---------------------------------------------------------------------


def test_a_fresh_data_job_mark_holds_the_copy_until_it_goes(backups, marks):
    clock = FakeClock()
    _mark(marks, "nightly-backup", clock.now)

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 3:
            (marks / "nightly-backup.running").unlink()

    assert off.wait_until_quiet(backups, marks, poll_s=60, clock=clock, sleep=sleep)
    assert clock.sleeps == [60, 60, 60]


def test_a_fresh_postgres_lock_holds_the_run_until_it_goes(backups, offsite, marks):
    clock = FakeClock()
    _fake_set(backups, "postgres", SET_ID)
    _lock(backups, clock.now)

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 3:
            shutil.rmtree(backups / "postgres" / ".lock")

    assert _run(["--only", "postgres"], backups, offsite, clock, marks, sleep=sleep) == 0
    assert clock.sleeps == [60, 60, 60]
    assert _ids(offsite, "postgres") == [SET_ID]
    assert not (offsite / "postgres" / ".lock").exists()


def test_stale_marks_stale_locks_and_lone_partials_do_not_hold_the_copy(backups, marks):
    clock = FakeClock()
    _mark(marks, "nightly-backup", clock.now - 21600 - 1)
    _lock(backups, clock.now - 21600 - 1)
    (backups / "postgres" / f"{SET_ID}.partial").mkdir(parents=True)
    _mark(marks, "nightly-rebuild", clock.now)  # not a backup job: the copy does not wait on it
    assert off.wait_until_quiet(backups, marks, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


def test_a_wait_timeout_logs_and_still_copies(backups, offsite, marks, capsys):
    clock = FakeClock()
    _mark(marks, "nightly-backup", clock.now)  # fresh for the whole 300 s wait
    _fake_set(backups, "bundle", SET_ID)
    argv = ["--only", "bundle", "--wait-s", "300", "--poll-s", "60"]
    assert _run(argv, backups, offsite, clock, marks) == 0
    assert "the wait gives up" in capsys.readouterr().out
    assert sum(clock.sleeps) == 300
    assert _ids(offsite, "bundle") == [SET_ID]


# --- the copy lock (SIM-568) -------------------------------------------------------


def _copy_lock(marks: Path, pid: int, epoch: float, host: str | None = None) -> Path:
    lock = marks / "offsite-copy.lock"
    lock.write_text(f"{pid} {int(epoch)} {host or socket.gethostname()}\n")
    return lock


def test_a_live_copy_lock_refuses_a_second_run_before_any_write(backups, offsite, marks, capsys):
    clock = FakeClock()
    _fake_set(backups, "bundle", SET_ID)
    lock = _copy_lock(marks, os.getpid(), clock.now)  # this process: alive
    text = lock.read_text()
    assert _run(["--only", "bundle"], backups, offsite, clock, marks) == 2
    assert "another off-site copy run holds" in capsys.readouterr().out
    assert not (offsite / "bundle").exists()
    assert lock.read_text() == text  # the refused run leaves the holder's lock alone
    # A pid of another host cannot be probed: a fresh lock from there holds too.
    _copy_lock(marks, 999_999, clock.now, host="another-container")
    assert _run(["--only", "bundle"], backups, offsite, clock, marks) == 2
    assert not (offsite / "bundle").exists()
    # A dry run writes nothing: it takes no lock and says a real run would refuse.
    assert _run(["--only", "bundle", "--dry-run"], backups, offsite, clock, marks) == 0
    out = capsys.readouterr().out
    assert "a real run would refuse" in out
    assert f"WOULD COPY bundle {SET_ID}" in out


def test_the_copy_lock_spans_the_wait_and_goes_after_the_run(backups, offsite, marks):
    clock = FakeClock()
    _fake_set(backups, "bundle", SET_ID)
    _mark(marks, "nightly-backup", clock.now)
    lock = marks / "offsite-copy.lock"
    holders: list[str] = []

    def sleep(seconds):
        holders.append(lock.read_text().split()[0])
        clock.sleep(seconds)
        if len(clock.sleeps) == 2:
            (marks / "nightly-backup.running").unlink()

    assert _run(["--only", "bundle"], backups, offsite, clock, marks, sleep=sleep) == 0
    assert holders == [str(os.getpid())] * 2
    assert not lock.exists()
    assert _ids(offsite, "bundle") == [SET_ID]


def test_a_failed_run_releases_the_copy_lock(backups, offsite, marks):
    src = _fake_set(backups, "bundle", SET_ID)
    (src / "payload.bin").write_bytes(b"short")
    assert _run(["--only", "bundle"], backups, offsite, marks=marks) == 1
    assert not (marks / "offsite-copy.lock").exists()


def test_a_stale_copy_lock_and_a_dead_owner_are_taken_over(
    backups, offsite, marks, monkeypatch, capsys
):
    clock = FakeClock()
    _fake_set(backups, "bundle", SET_ID)
    lock = _copy_lock(marks, os.getpid(), clock.now - 21600 - 1)  # alive but too old
    assert _run(["--only", "bundle"], backups, offsite, clock, marks) == 0
    assert "removing a stale copy lock" in capsys.readouterr().out
    assert not lock.exists()
    assert _ids(offsite, "bundle") == [SET_ID]
    # A crashed run on this host left a fresh lock: the rerun of with_retry.sh takes it.
    _copy_lock(marks, 4242, clock.now)
    monkeypatch.setattr(off, "_pid_alive", lambda pid: pid != 4242)
    assert _run(["--only", "bundle"], backups, offsite, clock, marks) == 0
    assert "removing a stale copy lock" in capsys.readouterr().out
    assert not lock.exists()


def test_an_unwritable_mark_folder_runs_the_copy_unlocked(tmp_path, backups, offsite, capsys):
    not_a_folder = tmp_path / "marks_file"
    not_a_folder.write_text("")
    _fake_set(backups, "bundle", SET_ID)
    assert _run(["--only", "bundle"], backups, offsite, marks=not_a_folder) == 0
    assert "the copy runs unlocked" in capsys.readouterr().out
    assert _ids(offsite, "bundle") == [SET_ID]


# --- the manifest move -------------------------------------------------------------


def test_the_manifest_move_retries_on_a_permission_error(tmp_path, monkeypatch):
    dest = tmp_path / "set"
    dest.mkdir()
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("OneDrive holds the folder")
        return real(src, dst)

    monkeypatch.setattr(off.os, "replace", flaky)
    sleeps: list[float] = []
    off.write_manifest(b'{"a": 1}', dest, sleep=sleeps.append)
    assert (dest / "manifest.json").read_bytes() == b'{"a": 1}'
    assert sleeps == [3.0, 3.0]
    assert not (dest / "manifest.json.tmp").exists()


def test_the_manifest_move_gives_up_after_its_tries(tmp_path, monkeypatch):
    dest = tmp_path / "set"
    dest.mkdir()
    calls = []

    def held(src, dst):
        calls.append(dst)
        raise PermissionError("OneDrive holds the folder")

    monkeypatch.setattr(off.os, "replace", held)
    sleeps: list[float] = []
    with pytest.raises(off.OffsiteError):
        off.write_manifest(b"{}", dest, sleep=sleeps.append)
    assert len(calls) == 10
    assert sleeps == [3.0] * 9
    assert not (dest / "manifest.json").exists()
    assert not (dest / "manifest.json.tmp").exists()


# --- status and the dry run ---------------------------------------------------------


def test_status_text_and_json_report_mirrored(backups, offsite, capsys):
    _fake_set(backups, "postgres", SET_ID)
    assert _run(["--only", "postgres"], backups, offsite) == 0
    _fake_set(backups, "duckdb", SET_ID)
    capsys.readouterr()
    argv = ["--backups-root", str(backups), "--offsite-root", str(offsite), "--status"]
    assert off.main([*argv, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows["postgres"]["mirrored"] is True
    assert rows["postgres"]["local"]["set"] == SET_ID
    assert rows["postgres"]["offsite"]["set"] == SET_ID
    assert rows["postgres"]["offsite"]["bytes"] == SET_BYTES
    assert rows["postgres"]["offsite"]["sets"] == 1
    assert rows["duckdb"]["mirrored"] is False
    assert rows["duckdb"]["offsite"] is None
    assert rows["bundle"] == {"local": None, "offsite": None, "mirrored": False}
    assert off.main(argv) == 0
    lines = {ln.split()[0]: ln for ln in capsys.readouterr().out.splitlines()[1:]}
    assert lines["postgres"].endswith("MIRRORED")
    assert lines["duckdb"].endswith("BEHIND")
    assert lines["bundle"].endswith("none")
    # A damaged off-site copy reads BEHIND.
    (offsite / "postgres" / SET_ID / "sub" / "part.bin").unlink()
    assert off.main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["postgres"]["mirrored"] is False


def test_a_dry_run_writes_nothing_and_does_not_wait(backups, offsite, marks, capsys):
    clock = FakeClock()
    _mark(marks, "nightly-backup", clock.now)
    _fake_set(backups, "bundle", SET_ID)
    for day in (1, 2, 3):
        _fake_set(offsite, "bundle", f"2026100{day}T100000Z")
    (offsite / "bundle" / "20261009T100000Z").mkdir()  # a dead folder of an unwanted set
    before = sorted(p.relative_to(offsite).as_posix() for p in offsite.rglob("*"))
    assert _run(["--dry-run"], backups, offsite, clock, marks) == 0
    out = capsys.readouterr().out
    assert f"WOULD COPY bundle {SET_ID} {SET_BYTES} bytes" in out
    assert "WOULD PRUNE bundle 20261001T100000Z" in out
    assert "WOULD PRUNE bundle 20261002T100000Z" not in out
    assert "WOULD DELETE bundle 20261009T100000Z" in out
    assert "a real run would wait" in out
    assert clock.sleeps == []
    assert sorted(p.relative_to(offsite).as_posix() for p in offsite.rglob("*")) == before


def test_a_dry_run_writes_no_probe_file(backups, offsite, monkeypatch, capsys):
    _fake_set(backups, "duckdb", SET_ID)
    # A real run refuses an off-site root it cannot write; a dry run never tries.
    monkeypatch.setattr(off.os, "access", lambda *_a, **_k: False)
    assert _run(["--dry-run"], backups, offsite) == 0
    assert f"WOULD COPY duckdb {SET_ID} {SET_BYTES} bytes" in capsys.readouterr().out
    assert list(offsite.iterdir()) == []
    assert not list(backups.glob("offsite-probe-*"))


# --- the module boundary ------------------------------------------------------------


def test_offsite_and_common_never_import_duckdb():
    code = (
        "import sys; import pipeline.backup.offsite, pipeline.backup.common; "
        "sys.exit(1 if 'duckdb' in sys.modules else 0)"
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_data_stores_reuses_the_common_helpers(tmp_path):
    from pipeline.backup import common
    from pipeline.backup import data_stores as ds

    assert ds.sha256_file is common.sha256_file
    assert ds.GIB == common.GIB
    (tmp_path / "nightly-rebuild.running").write_text("100\n")
    (tmp_path / "nightly-backup.running").write_text("100\n")  # not a writer mark
    assert ds._fresh_marks(tmp_path, 21600, 200.0) == ["nightly-rebuild"]
