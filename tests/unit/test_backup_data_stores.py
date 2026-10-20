"""SIM-525 — the backups of the DuckDB file and the engine bundle
(pipeline/backup/data_stores.py).

Every test runs on tmp_path: a real small DuckDB file, a fake bundle tree with
a relative link, and fake clocks, sleeps and openers, so the waits take no time.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import namedtuple
from pathlib import Path

import duckdb
import pytest

from pipeline.backup import data_stores as ds
from pipeline.backup.status import complete_sets

BUILD_ID = "20261014T080000Z"

DiskUsage = namedtuple("DiskUsage", "total used free")


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "BACKUP_ROOT",
        "BACKUP_KEEP",
        "BASEBALL_DUCKDB_PATH",
        "BASEBALL_PLAY_POOL_DIR",
        "INGEST_MARK_DIR",
        "CALIBRATION_REPORT_PATH",
        "PROP_VALIDATION_PATH",
        "BACKUP_MIN_FREE_GB",
        "BACKUP_FREE_FACTOR",
    ):
        monkeypatch.delenv(name, raising=False)
    # The free-space floor is 10 GiB by default; the tests' disk has room unless a test says not.
    monkeypatch.setattr(ds.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 10**15))


def _make_duckdb(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA derived")
    con.execute("CREATE SCHEMA sim")
    con.execute("CREATE TABLE derived.batters AS SELECT range AS id FROM range(7)")
    con.execute("CREATE TABLE sim.pool AS SELECT range AS id FROM range(3)")
    con.execute("CREATE TABLE main.misc (x INTEGER)")
    con.close()


def _make_bundle(play_pool: Path) -> Path:
    version = play_pool / "engine_artifacts.versions" / BUILD_ID
    (version / "pitch_pool").mkdir(parents=True)
    (version / "pitch_pool" / "manifest.json").write_text('{"n": 1}', encoding="utf-8")
    (version / "park_geometry.json").write_text("{}", encoding="utf-8")
    (version / ".published").write_text("2026-10-14T09:20:00+00:00", encoding="utf-8")
    (version / ".in_use.apphost").write_text("x", encoding="utf-8")
    try:
        os.symlink(
            os.path.join("engine_artifacts.versions", BUILD_ID), play_pool / "engine_artifacts"
        )
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symbolic link")
    return version


@pytest.fixture
def data_root(tmp_path) -> Path:
    if os.name == "nt":
        pytest.skip("the bundle link is a POSIX symbolic link (the container runs it)")
    data = tmp_path / "data"
    data.mkdir()
    _make_duckdb(data / "baseball_sim.duckdb")
    _make_bundle(data / "play_pool")
    (data / "calibration.json").write_text('{"cal": 1}', encoding="utf-8")
    (data / "prop_validation.json").write_text('{"pv": 1}', encoding="utf-8")
    marks = data / "ingest_marks"
    marks.mkdir()
    (marks / "nightly-finals.last").write_text("2026-10-14 0 2026-10-14T07:40:00Z\n")
    (marks / "nightly-finals.running").write_text("1\n")  # stale: epoch 1
    (data / "calibration.json.bak_20261001").write_text("old", encoding="utf-8")
    return data


@pytest.fixture
def backups(tmp_path) -> Path:
    root = tmp_path / "backups"
    root.mkdir()
    return root


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


def _run(argv: list[str], backups: Path, data: Path, clock: FakeClock | None = None, **kw) -> int:
    clock = clock or FakeClock()
    return ds.main(
        ["--backups-root", str(backups), "--data-root", str(data), *argv],
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


def _manifest(set_dir: Path) -> dict:
    return json.loads((set_dir / "manifest.json").read_text(encoding="utf-8"))


# --- the backup run ---------------------------------------------------------------


def test_a_backup_run_makes_one_complete_set_per_store(backups, data_root, capsys):
    assert _run([], backups, data_root) == 0
    out = capsys.readouterr().out
    duck = complete_sets(backups, "duckdb")
    bundle = complete_sets(backups, "bundle")
    assert len(duck) == 1 and len(bundle) == 1
    assert duck[0].set_id == bundle[0].set_id  # one set id per run
    assert f"BACKUP OK duckdb {duck[0].set_id} " in out
    assert f"BACKUP OK bundle {bundle[0].set_id} " in out
    assert not list(backups.glob("*/*.partial"))


def test_the_duckdb_copy_round_trips_with_the_manifest_counts(backups, data_root):
    assert _run(["--only", "duckdb"], backups, data_root) == 0
    (info,) = complete_sets(backups, "duckdb")
    manifest = _manifest(info.path)
    assert manifest["store"] == "duckdb"
    assert manifest["tool"] == "pipeline.backup.data_stores"
    assert manifest["counts"] == {"derived.batters": 7, "main.misc": 0, "sim.pool": 3}
    assert manifest["wal_included"] is False
    assert ds.count_copy(info.path / "baseball_sim.duckdb") == manifest["counts"]
    assert manifest["duckdb_version"].startswith("v")
    paths = {f["path"] for f in manifest["files"]}
    assert "baseball_sim.duckdb" in paths
    assert "manifest.json" not in paths
    assert all(f["sha256"] and len(f["sha256"]) == 64 for f in manifest["files"])
    assert manifest["bytes"] == sum(f["bytes"] for f in manifest["files"])
    assert sorted(manifest["data_files"]) == [
        "data_files/calibration.json",
        "data_files/ingest_marks/nightly-finals.last",
        "data_files/prop_validation.json",
    ]
    assert not (info.path / "data_files" / "ingest_marks" / "nightly-finals.running").exists()
    assert not list(info.path.rglob("*.bak_*"))
    assert not complete_sets(backups, "bundle")


def test_the_wal_sidecar_is_copied_when_present(backups, tmp_path, monkeypatch):
    src = tmp_path / "x.duckdb"
    src.write_bytes(b"db")
    (tmp_path / "x.duckdb.wal").write_bytes(b"wal")

    class FakeCon:
        def execute(self, sql):
            class R:
                def fetchone(self):
                    return ["v1.5.6"]

                def fetchall(self):
                    return []

            return R()

        def close(self):
            pass

    monkeypatch.setattr(ds, "count_copy", lambda _p: {})
    final = ds.backup_duckdb(
        backups,
        "20261015T100000Z",
        duckdb_path=src,
        calibration=tmp_path / "none.json",
        prop_validation=tmp_path / "none2.json",
        mark_dir=tmp_path / "no_marks",
        opener=lambda *a, **k: FakeCon(),
    )
    assert (final / "baseball_sim.duckdb").read_bytes() == b"db"
    assert (final / "baseball_sim.duckdb.wal").read_bytes() == b"wal"
    assert _manifest(final)["wal_included"] is True


def test_the_bundle_copy_skips_the_in_use_marker(backups, data_root):
    assert _run(["--only", "bundle"], backups, data_root) == 0
    (info,) = complete_sets(backups, "bundle")
    manifest = _manifest(info.path)
    assert manifest["build_id"] == BUILD_ID
    assert manifest["published_at"] == "2026-10-14T09:20:00+00:00"
    assert manifest["source_path"].endswith(BUILD_ID)
    copy = info.path / BUILD_ID
    assert (copy / "pitch_pool" / "manifest.json").is_file()
    assert (copy / ".published").is_file()
    assert not list(copy.glob(".in_use.*"))
    paths = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert set(paths) == {
        f"{BUILD_ID}/.published",
        f"{BUILD_ID}/park_geometry.json",
        f"{BUILD_ID}/pitch_pool/manifest.json",
    }
    assert paths[f"{BUILD_ID}/park_geometry.json"] == ds.sha256_file(copy / "park_geometry.json")


def test_one_store_failing_still_runs_the_other_and_prunes_nothing(backups, data_root):
    for set_id in ("20261001T100000Z", "20261002T100000Z", "20261003T100000Z"):
        _fake_set(backups, "bundle", set_id)
    (data_root / "baseball_sim.duckdb").unlink()
    assert _run([], backups, data_root) == 1
    assert len(complete_sets(backups, "bundle")) == 4  # the new one, nothing pruned
    assert not complete_sets(backups, "duckdb")


def test_a_missing_root_is_refused(tmp_path, data_root):
    assert _run([], tmp_path / "missing", data_root) == 2
    assert not (tmp_path / "missing").exists()


# --- partial sets and retention ------------------------------------------------------


def _fake_set(root: Path, store: str, set_id: str, size: int = 10) -> Path:
    path = root / store / set_id
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(
        json.dumps({"store": store, "set": set_id, "bytes": size}), encoding="utf-8"
    )
    return path


def test_every_partial_set_is_deleted_at_the_start(backups, data_root):
    (backups / "duckdb" / "20261014T100000Z.partial").mkdir(parents=True)
    (backups / "bundle" / "20261014T100000Z.partial" / "deep").mkdir(parents=True)
    (backups / "postgres" / "20261014T100000Z.partial").mkdir(parents=True)
    assert _run([], backups, data_root) == 0
    assert not list(backups.glob("duckdb/*.partial"))
    assert not list(backups.glob("bundle/*.partial"))
    # The Postgres job runs in another container at the same time; its partial is its own.
    assert (backups / "postgres" / "20261014T100000Z.partial").is_dir()


def test_retention_keeps_the_newest_three_and_never_the_current(backups):
    for day in range(1, 6):
        _fake_set(backups, "duckdb", f"2026100{day}T100000Z")
    removed = ds.prune_sets(backups, "duckdb", 3, current="20261005T100000Z")
    assert removed == ["20261001T100000Z", "20261002T100000Z"]
    assert [s.set_id for s in complete_sets(backups, "duckdb")] == [
        "20261003T100000Z",
        "20261004T100000Z",
        "20261005T100000Z",
    ]
    # Keep 0 still keeps the set the run just made.
    ds.prune_sets(backups, "duckdb", 0, current="20261005T100000Z")
    assert [s.set_id for s in complete_sets(backups, "duckdb")] == ["20261005T100000Z"]


def test_a_successful_run_prunes_to_backup_keep(backups, data_root, monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    for set_id in ("20261001T100000Z", "20261002T100000Z", "20261003T100000Z"):
        _fake_set(backups, "duckdb", set_id)
    assert _run(["--only", "duckdb"], backups, data_root) == 0
    ids = [s.set_id for s in complete_sets(backups, "duckdb")]
    assert len(ids) == 2
    assert ids[0] == "20261003T100000Z"
    # The flag beats the variable.
    for set_id in ("20261004T100000Z",):
        _fake_set(backups, "duckdb", set_id)
    clock = FakeClock(start=1_900_000_000.0)
    assert _run(["--only", "duckdb", "--keep", "1"], backups, data_root, clock=clock) == 0
    assert len(complete_sets(backups, "duckdb")) == 1


# --- the free-space guard ---------------------------------------------------------------


def test_the_guard_passes_with_room(backups):
    _fake_set(backups, "duckdb", "20261001T100000Z", size=4 * ds.GIB)
    assert ds.free_space_ok(backups, "duckdb", min_free_gb=10, factor=1.5)


def test_the_guard_prunes_the_oldest_while_more_than_two_remain(backups, monkeypatch):
    for day in range(1, 5):
        _fake_set(backups, "duckdb", f"2026100{day}T100000Z", size=4 * ds.GIB)

    def disk_usage(_p):
        # Each deleted set frees 4 GiB; the guard needs 10 GiB.
        sets = len(complete_sets(backups, "duckdb"))
        return DiskUsage(0, 0, (8 + (4 - sets) * 4) * ds.GIB)

    monkeypatch.setattr(ds.shutil, "disk_usage", disk_usage)
    assert ds.free_space_ok(backups, "duckdb", min_free_gb=10, factor=1.5)
    assert [s.set_id for s in complete_sets(backups, "duckdb")] == [
        "20261002T100000Z",
        "20261003T100000Z",
        "20261004T100000Z",
    ]


def test_the_guard_refuses_when_two_sets_remain(backups, data_root, monkeypatch):
    for day in range(1, 3):
        _fake_set(backups, "duckdb", f"2026100{day}T100000Z", size=4 * ds.GIB)
    monkeypatch.setattr(ds.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 9 * ds.GIB))
    assert not ds.free_space_ok(backups, "duckdb", min_free_gb=10, factor=1.5)
    assert len(complete_sets(backups, "duckdb")) == 2
    # The run refuses before it writes a set.
    assert _run(["--only", "duckdb"], backups, data_root) == 2
    assert len(complete_sets(backups, "duckdb")) == 2
    assert not list(backups.glob("duckdb/*.partial"))


def test_the_guard_needs_the_factor_times_the_newest_set(backups, monkeypatch):
    _fake_set(backups, "bundle", "20261001T100000Z", size=20 * ds.GIB)
    monkeypatch.setattr(ds.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 25 * ds.GIB))
    assert not ds.free_space_ok(backups, "bundle", min_free_gb=10, factor=1.5)
    monkeypatch.setattr(ds.shutil, "disk_usage", lambda _p: DiskUsage(0, 0, 31 * ds.GIB))
    assert ds.free_space_ok(backups, "bundle", min_free_gb=10, factor=1.5)


# --- the writer wait ---------------------------------------------------------------------


def _mark(mark_dir: Path, name: str, epoch: float) -> None:
    mark_dir.mkdir(parents=True, exist_ok=True)
    (mark_dir / f"{name}.running").write_text(f"{int(epoch)}\n")


def test_no_mark_proceeds_after_the_grace(tmp_path):
    clock = FakeClock()
    assert ds.wait_for_marks(tmp_path, grace_s=60, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [60]


def test_a_fresh_mark_waits_until_it_clears(tmp_path):
    clock = FakeClock()
    _mark(tmp_path, "nightly-rebuild", clock.now)

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 3:
            (tmp_path / "nightly-rebuild.running").unlink()

    assert ds.wait_for_marks(tmp_path, grace_s=60, poll_s=60, clock=clock, sleep=sleep)
    assert clock.sleeps == [60, 60, 60]


def test_a_stale_mark_does_not_hold_the_backup(tmp_path):
    clock = FakeClock()
    _mark(tmp_path, "weekly-refresh", clock.now - 21600 - 1)
    assert ds.wait_for_marks(tmp_path, grace_s=0, stale_s=21600, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [0]


def test_the_wait_gives_up_after_wait_s(tmp_path):
    clock = FakeClock()
    _mark(tmp_path, "nightly-finals", clock.now + 10**6)  # always fresh
    assert not ds.wait_for_marks(
        tmp_path, grace_s=60, wait_s=600, poll_s=60, clock=clock, sleep=clock.sleep
    )
    assert sum(clock.sleeps) == 60 + 600


def test_a_wait_timeout_skips_duckdb_but_backs_up_the_bundle(backups, data_root):
    clock = FakeClock()
    _mark(data_root / "ingest_marks", "nightly-rebuild", clock.now + 10**6)
    assert (
        _run(["--wait-s", "5", "--grace-s", "0", "--poll-s", "1"], backups, data_root, clock) == 1
    )
    assert not complete_sets(backups, "duckdb")
    assert len(complete_sets(backups, "bundle")) == 1


# --- the lock retry ------------------------------------------------------------------------


def test_the_lock_error_is_retried_then_the_open_succeeds(tmp_path):
    clock = FakeClock()
    calls = []

    def opener(path, read_only):
        calls.append((path, read_only))
        if len(calls) <= 2:
            raise duckdb.IOException(f'Could not set lock on file "{path}": Conflicting lock')
        return "con"

    got = ds.open_read_only(
        tmp_path / "x.duckdb", lock_poll_s=60, opener=opener, clock=clock, sleep=clock.sleep
    )
    assert got == "con"
    assert len(calls) == 3
    assert all(ro is True for _p, ro in calls)
    assert clock.sleeps == [60, 60]


def test_the_lock_retry_gives_up_after_lock_wait_s(tmp_path):
    clock = FakeClock()

    def opener(path, read_only):
        raise duckdb.IOException("Could not set lock on file: already open")

    got = ds.open_read_only(
        tmp_path / "x.duckdb",
        lock_wait_s=300,
        lock_poll_s=60,
        opener=opener,
        clock=clock,
        sleep=clock.sleep,
    )
    assert got is None
    assert sum(clock.sleeps) == 300


def test_another_open_error_rises_at_once(tmp_path):
    def opener(path, read_only):
        raise duckdb.IOException("No such file or directory")

    with pytest.raises(duckdb.IOException):
        ds.open_read_only(tmp_path / "x.duckdb", opener=opener, sleep=lambda _s: None)


def test_a_held_lock_skips_the_duckdb_store(backups, data_root):
    def opener(path, read_only):
        raise duckdb.IOException("Could not set lock on file")

    rc = _run(["--lock-wait-s", "120", "--lock-poll-s", "60"], backups, data_root, opener=opener)
    assert rc == 1
    assert not complete_sets(backups, "duckdb")
    assert not list(backups.glob("duckdb/*.partial"))
    assert len(complete_sets(backups, "bundle")) == 1


# --- status --------------------------------------------------------------------------------


def test_status_lists_the_newest_complete_set_of_each_store(backups, capsys):
    _fake_set(backups, "postgres", "20261014T100000Z", size=5 * ds.GIB)
    _fake_set(backups, "postgres", "20261015T100000Z", size=6 * ds.GIB)
    (backups / "postgres" / "20261016T100000Z").mkdir()  # no manifest: incomplete
    _fake_set(backups, "bundle", "20261015T100000Z")
    assert ds.main(["--backups-root", str(backups), "--status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows["postgres"]["set"] == "20261015T100000Z"
    assert rows["postgres"]["bytes"] == 6 * ds.GIB
    assert rows["postgres"]["sets"] == 2
    assert rows["duckdb"] is None
    assert rows["bundle"]["set"] == "20261015T100000Z"
    assert ds.main(["--backups-root", str(backups), "--status"]) == 0
    text = capsys.readouterr().out
    assert "20261015T100000Z" in text and "6.0 GiB" in text
    assert "duckdb   none" in text


def test_status_reads_the_backup_root_variable(backups, monkeypatch, capsys):
    monkeypatch.setenv("BACKUP_ROOT", str(backups))
    _fake_set(backups, "duckdb", "20261015T100000Z")
    assert ds.main(["--status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["duckdb"]["set"] == "20261015T100000Z"


# --- verify ----------------------------------------------------------------------------------


@pytest.fixture
def made(backups, data_root, monkeypatch):
    loaded = []
    monkeypatch.setattr(ds, "load_bundle", lambda path: loaded.append(path))
    assert _run([], backups, data_root) == 0
    return {"loaded": loaded}


def test_verify_latest_passes_on_a_fresh_backup(backups, data_root, made):
    assert ds.main(["--backups-root", str(backups), "--verify-set", "latest"]) == 0
    (path,) = made["loaded"]
    assert path.name == BUILD_ID
    set_id = complete_sets(backups, "duckdb")[0].set_id
    assert ds.main(["--backups-root", str(backups), "--verify-set", set_id]) == 0


def test_verify_detects_a_tampered_file(backups, data_root, made, capsys):
    (info,) = complete_sets(backups, "bundle")
    target = info.path / BUILD_ID / "park_geometry.json"
    data = bytearray(target.read_bytes())
    data[0] ^= 0xFF
    target.write_bytes(bytes(data))
    assert ds.main(["--backups-root", str(backups), "--verify-set", "latest"]) == 1
    assert "sha256 mismatch" in capsys.readouterr().out


def test_verify_detects_a_missing_table(backups, data_root, made, capsys):
    (info,) = complete_sets(backups, "duckdb")
    manifest = _manifest(info.path)
    manifest["counts"]["derived.ghost"] = 5
    (info.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ds.main(["--backups-root", str(backups), "--verify-set", "latest"]) == 1
    assert "derived.ghost missing table" in capsys.readouterr().out


def test_verify_fails_a_bundle_that_does_not_load(backups, data_root, made, monkeypatch):
    def broken(path):
        raise FileNotFoundError("pitch_pool/manifest.json")

    monkeypatch.setattr(ds, "load_bundle", broken)
    assert ds.main(["--backups-root", str(backups), "--verify-set", "latest"]) == 1


def test_verify_with_no_set_fails(backups):
    assert ds.main(["--backups-root", str(backups), "--verify-set", "latest"]) == 1


# --- restore ---------------------------------------------------------------------------------


def test_restore_duckdb_needs_yes(backups, data_root, made):
    live = data_root / "baseball_sim.duckdb"
    before = live.read_bytes()
    argv = ["--backups-root", str(backups), "--data-root", str(data_root)]
    assert ds.main([*argv, "--restore-duckdb", "latest"]) == 2
    assert live.read_bytes() == before


def test_restore_duckdb_replaces_the_live_file(backups, data_root, made):
    live = data_root / "baseball_sim.duckdb"
    con = duckdb.connect(str(live))
    con.execute("DROP TABLE sim.pool")
    con.close()
    (data_root / "baseball_sim.duckdb.wal").write_bytes(b"stale wal")
    old_inode = live.stat().st_ino
    argv = ["--backups-root", str(backups), "--data-root", str(data_root)]
    assert ds.main([*argv, "--restore-duckdb", "latest", "--yes"]) == 0
    assert live.stat().st_ino != old_inode  # a new file moved into place
    (info,) = complete_sets(backups, "duckdb")
    assert live.read_bytes() == (info.path / "baseball_sim.duckdb").read_bytes()
    assert not (data_root / "baseball_sim.duckdb.wal").exists()
    assert list(data_root.glob("baseball_sim.duckdb.wal.pre-restore-*"))
    assert not list(data_root.glob("*.restore-tmp"))
    assert ds.count_copy(live)["sim.pool"] == 3


def test_restore_bundle_publishes_the_restored_version(backups, data_root, made):
    play_pool = data_root / "play_pool"
    shutil.rmtree(play_pool)
    play_pool.mkdir()
    argv = ["--backups-root", str(backups), "--data-root", str(data_root)]
    assert ds.main([*argv, "--restore-bundle", "latest"]) == 2
    assert ds.main([*argv, "--restore-bundle", "latest", "--yes"]) == 0
    link = play_pool / "engine_artifacts"
    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link))
    assert Path(os.path.realpath(link)).name == BUILD_ID
    assert (link / "pitch_pool" / "manifest.json").is_file()
    # A second restore over the same version needs --force.
    assert ds.main([*argv, "--restore-bundle", "latest", "--yes"]) == 1
    assert ds.main([*argv, "--restore-bundle", "latest", "--yes", "--force"]) == 0


# --- the crash retry -------------------------------------------------------------------------


def test_a_retry_skips_the_store_that_finished_and_keeps_its_set_id(backups, data_root):
    clock = FakeClock()
    assert _run(["--only", "duckdb"], backups, data_root, clock) == 0
    (duck,) = complete_sets(backups, "duckdb")
    # The first attempt crashed in the bundle copy; with_retry reruns the whole command.
    (backups / "bundle" / f"{duck.set_id}.partial").mkdir(parents=True)
    clock.now += 300
    assert _run([], backups, data_root, clock) == 0
    assert [s.set_id for s in complete_sets(backups, "duckdb")] == [duck.set_id]
    (bundle,) = complete_sets(backups, "bundle")
    assert bundle.set_id == duck.set_id  # one set id per night
    assert not list(backups.glob("*/*.partial"))


def test_a_retry_after_both_stores_finished_does_nothing(backups, data_root):
    clock = FakeClock()
    assert _run([], backups, data_root, clock) == 0
    clock.now += 60
    assert _run([], backups, data_root, clock) == 0
    assert len(complete_sets(backups, "duckdb")) == 1
    assert len(complete_sets(backups, "bundle")) == 1


def test_the_next_night_and_new_set_make_a_new_set(backups, data_root):
    clock = FakeClock()
    assert _run([], backups, data_root, clock) == 0
    clock.now += 120
    assert _run(["--new-set"], backups, data_root, clock) == 0
    assert len(complete_sets(backups, "duckdb")) == 2
    clock.now += 86400
    assert _run([], backups, data_root, clock) == 0
    assert len(complete_sets(backups, "duckdb")) == 3
    assert len(complete_sets(backups, "bundle")) == 3
