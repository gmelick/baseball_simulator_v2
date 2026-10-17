"""pipeline/backup/data_stores.py — back up, verify and restore the DuckDB
analytics file and the engine-artifact bundle (SIM-525).

Run it in the jobs container as ``appuser``:

    python -m pipeline.backup.data_stores                 # back up both stores
    python -m pipeline.backup.data_stores --status        # the newest set of each store
    python -m pipeline.backup.data_stores --verify-set latest
    python -m pipeline.backup.data_stores --restore-duckdb <set> --yes
    python -m pipeline.backup.data_stores --restore-bundle <set> --yes

What a backup run does:

1. It refuses (exit 2) when the backup root is missing or cannot be written,
   or when the disk is short of space after pruning (the free-space guard).
2. It waits while the nightly rebuild, the finals load or the weekly refresh
   runs (the marks ``scripts/with_mark.sh`` writes). A rebuild WRITES the
   DuckDB file, and a read-only handle we hold makes its connect fail.
3. The DuckDB store: it opens the file read-only, counts every table, and
   copies the file (and its ``.wal`` sidecar) while it holds the handle. Then
   it opens the copy and requires the same counts. The small files
   (calibration, prop validation, the last run of each ingest job) go into
   the same set.
4. The bundle store: it copies the published bundle version, without the
   app's in-use markers.
5. Each set is written as ``<set>.partial``. The manifest is written last, and
   then the folder is renamed. After a run where every store succeeded, each
   store keeps its newest ``BACKUP_KEEP`` sets.
6. A rerun after a crash (the scheduler's retry) skips a store that already
   has a complete set younger than ``--rerun-window-s`` (1 hour), and it
   reuses that set's id for the other store. So one night makes one set per
   store, and the two stores share one set id. ``--new-set`` turns this off.

Exit codes: 0 every store succeeded; 1 a store failed (the other was still
attempted); 2 a refusal before any write.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from pipeline.backup.common import (  # the shared helpers moved there (SIM-568)
    CHUNK,  # noqa: F401  (re-exported under its old name)
    GIB,
    Clock,
    Sleep,
    copy_file_synced,
    fresh_marks,
    fsync_path,
    human_bytes,
    sha256_file,
)
from pipeline.backup.status import (
    MANIFEST,
    PARTIAL_SUFFIX,
    SET_ID_FORMAT,
    STORES,
    SetInfo,
    age_hours,
    complete_sets,
    newest_complete_set,
    parse_set_id,
)
from pipeline.batch.bundle_versions import (
    IN_USE_PREFIX,
    PUBLISHED_MARKER,
    publish_bundle_version,
    resolve_bundle_dir,
    versions_root,
)

TOOL = "pipeline.backup.data_stores"

#: The stores this module backs up (the db container backs up Postgres).
DATA_STORES = ("duckdb", "bundle")

#: The name of the DuckDB copy inside a set. DuckDB finds its WAL as ``<file>.wal``.
DUCKDB_COPY = "baseball_sim.duckdb"
WAL_SUFFIX = ".wal"

#: The folder of the small files inside a DuckDB set.
DATA_FILES_DIR = "data_files"

#: The marks of the jobs that write the data stores. A backup waits while one runs.
WRITER_MARKS = ("nightly-rebuild", "nightly-finals", "weekly-refresh")

#: The link to the published bundle version, inside the play-pool folder.
BUNDLE_LINK = "engine_artifacts"


#: The default waits, in seconds. Each one has a command-line flag.
DEFAULTS: dict[str, float] = {
    "grace_s": 60.0,
    "wait_s": 7200.0,
    "poll_s": 60.0,
    "stale_s": 21600.0,
    "lock_wait_s": 1800.0,
    "lock_poll_s": 60.0,
    "rerun_window_s": 3600.0,
}

Opener = Callable[..., Any]


class BackupError(RuntimeError):
    """A store's backup, verify or restore failed. The message says why."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    """Print one log line with a UTC stamp."""
    print(f"[backup-data {_iso(datetime.now(UTC))}] {message}", flush=True)


def _files_under(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*") if p.is_file() and p != base / MANIFEST)


def clear_partials(root: Path, stores: Iterable[str]) -> list[Path]:
    """Delete every ``*.partial`` set of the stores. Returns the deleted folders.

    A run never overlaps another (the scheduler's no-overlap), so a partial set
    at the start of a run is always dead: its run crashed or the host rebooted.
    """
    removed: list[Path] = []
    for store in stores:
        store_dir = root / store
        if not store_dir.is_dir():
            continue
        for entry in sorted(store_dir.iterdir()):
            if entry.name.endswith(PARTIAL_SUFFIX):
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry)
                log(f"deleted the dead partial set {store}/{entry.name}")
    return removed


# ---------------------------------------------------------------------------
# The free-space guard and retention
# ---------------------------------------------------------------------------


def free_space_ok(
    root: Path,
    store: str,
    *,
    min_free_gb: float = 10.0,
    factor: float = 1.5,
) -> bool:
    """True when the backup disk has room for a new set of ``store``.

    The room needed is the larger of ``min_free_gb`` GiB and ``factor`` times
    the newest complete set. When the disk is short, the guard deletes the
    oldest complete set while more than two remain, and checks again.
    """
    while True:
        sets = complete_sets(root, store)
        newest = sets[-1].bytes if sets else 0
        needed = max(min_free_gb * GIB, factor * newest)
        free = shutil.disk_usage(root).free
        if free >= needed:
            return True
        if len(sets) <= 2:
            log(
                f"refused: {store} needs {human_bytes(int(needed))} free on {root}, "
                f"{human_bytes(free)} is free, and only {len(sets)} sets remain"
            )
            return False
        oldest = sets[0]
        log(f"short of space: deleting the oldest {store} set {oldest.set_id}")
        shutil.rmtree(oldest.path, ignore_errors=True)


def prune_sets(root: Path, store: str, keep: int, current: str | None) -> list[str]:
    """Keep the newest ``keep`` complete sets of ``store``. Returns the deleted ids.

    The set ``current`` (the one this run made) is never deleted.
    """
    sets = complete_sets(root, store)
    keep_ids = {s.set_id for s in sets[-keep:]} if keep > 0 else set()
    removed: list[str] = []
    for info in sets:
        if info.set_id in keep_ids or info.set_id == current:
            continue
        shutil.rmtree(info.path, ignore_errors=True)
        removed.append(info.set_id)
        log(f"pruned the {store} set {info.set_id}")
    return removed


# ---------------------------------------------------------------------------
# The writer wait and the read-only open
# ---------------------------------------------------------------------------


def _fresh_marks(mark_dir: Path, stale_s: float, now: float) -> list[str]:
    """The writer jobs that run now (the body is ``common.fresh_marks``, SIM-568)."""
    return fresh_marks(mark_dir, WRITER_MARKS, stale_s, now)


def wait_for_marks(
    mark_dir: Path,
    *,
    grace_s: float = DEFAULTS["grace_s"],
    wait_s: float = DEFAULTS["wait_s"],
    poll_s: float = DEFAULTS["poll_s"],
    stale_s: float = DEFAULTS["stale_s"],
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> bool:
    """Wait while a job that writes the data stores runs. False on a timeout.

    A job runs while its ``<name>.running`` mark exists and is younger than
    ``stale_s``; an older mark was left by a job that died. The wait starts with
    ``grace_s``, so a job the scheduler fired in the same second has time to
    write its mark. ``clock`` returns epoch seconds, the unit of the marks.
    """
    sleep(grace_s)
    deadline = clock() + wait_s
    while True:
        running = _fresh_marks(mark_dir, stale_s, clock())
        if not running:
            return True
        if clock() >= deadline:
            log(f"{', '.join(running)} still runs after {wait_s:.0f} s; the wait gives up")
            return False
        log(f"{', '.join(running)} runs; waiting {poll_s:.0f} s")
        sleep(poll_s)


def is_lock_error(exc: BaseException) -> bool:
    """True for the IOException DuckDB raises while another process holds the file."""
    if type(exc).__name__ != "IOException":
        return False
    msg = str(exc).lower()
    return "lock" in msg or "already open" in msg


def open_read_only(
    path: Path,
    *,
    lock_wait_s: float = DEFAULTS["lock_wait_s"],
    lock_poll_s: float = DEFAULTS["lock_poll_s"],
    opener: Opener = duckdb.connect,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> Any | None:
    """Open a DuckDB file read-only. None when a writer still holds it after the wait.

    Any error other than the lock error rises at once.
    """
    started = clock()
    while True:
        try:
            return opener(str(path), read_only=True)
        except Exception as exc:
            if not is_lock_error(exc):
                raise
            if clock() - started >= lock_wait_s:
                log(f"a writer still holds {path} after {lock_wait_s:.0f} s ({exc})")
                return None
            log(f"a writer holds {path} ({exc}); retrying in {lock_poll_s:.0f} s")
            sleep(lock_poll_s)


def table_counts(con: Any) -> dict[str, int]:
    """The row count of every table of the open database, keyed ``schema.table``."""
    rows = con.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = current_database() ORDER BY schema_name, table_name"
    ).fetchall()
    counts: dict[str, int] = {}
    for schema, table in rows:
        quoted = '"{}"."{}"'.format(schema.replace('"', '""'), table.replace('"', '""'))
        row = con.execute(f"SELECT count(*) FROM {quoted}").fetchone()
        counts[f"{schema}.{table}"] = int(row[0])
    return counts


def count_copy(path: Path) -> dict[str, int]:
    """The table counts of a DuckDB copy, opened read-only."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        return table_counts(con)
    finally:
        con.close()


def _count_diff(want: dict[str, int], got: dict[str, int]) -> str:
    parts = []
    for key in sorted(set(want) | set(got)):
        if key not in got:
            parts.append(f"{key} missing table")
        elif key not in want:
            parts.append(f"{key} unexpected table")
        elif want[key] != got[key]:
            parts.append(f"{key} count {got[key]} != {want[key]}")
    return "; ".join(parts) or "none"


# ---------------------------------------------------------------------------
# Writing a set
# ---------------------------------------------------------------------------


def _start_set(root: Path, store: str, set_id: str) -> Path:
    if (root / store / set_id).exists():
        raise BackupError(f"the {store} set {set_id} already exists")
    partial = root / store / f"{set_id}{PARTIAL_SUFFIX}"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    return partial


def _finish_set(
    root: Path,
    store: str,
    set_id: str,
    partial: Path,
    extra: dict[str, Any],
    started: float,
) -> tuple[Path, int, float]:
    """Hash every file, write the manifest last, flush, then rename the set."""
    files = []
    total = 0
    paths = _files_under(partial)
    for path in paths:
        size = path.stat().st_size
        total += size
        files.append(
            {
                "path": path.relative_to(partial).as_posix(),
                "bytes": size,
                "sha256": sha256_file(path),
            }
        )
    seconds = round(time.monotonic() - started, 3)
    manifest: dict[str, Any] = {
        "store": store,
        "set": set_id,
        "created_at": _iso(datetime.now(UTC)),
        "seconds": seconds,
        "bytes": total,
        "host": socket.gethostname(),
        "tool": TOOL,
        "files": files,
    }
    manifest.update(extra)
    for folder in sorted({p.parent for p in paths}):
        fsync_path(folder)
    manifest_path = partial / MANIFEST
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    fsync_path(manifest_path)
    fsync_path(partial)
    final = root / store / set_id
    os.rename(partial, final)
    fsync_path(final.parent)
    return final, total, seconds


def backup_small_files(
    set_dir: Path,
    *,
    calibration: Path,
    prop_validation: Path,
    mark_dir: Path,
) -> list[str]:
    """Copy the small data files into ``<set>/data_files``. Returns their set paths.

    A file that does not exist is skipped. The ``.running`` marks are not copied:
    they describe a job, not data.
    """
    dest_dir = set_dir / DATA_FILES_DIR
    sources = [(calibration, calibration.name), (prop_validation, prop_validation.name)]
    if mark_dir.is_dir():
        sources += [(p, f"ingest_marks/{p.name}") for p in sorted(mark_dir.glob("*.last"))]
    copied: list[str] = []
    for src, name in sources:
        if not src.is_file():
            log(f"no {src}; skipped")
            continue
        copy_file_synced(src, dest_dir / name)
        copied.append(f"{DATA_FILES_DIR}/{name}")
    return copied


def backup_duckdb(
    root: Path,
    set_id: str,
    *,
    duckdb_path: Path,
    calibration: Path,
    prop_validation: Path,
    mark_dir: Path,
    lock_wait_s: float = DEFAULTS["lock_wait_s"],
    lock_poll_s: float = DEFAULTS["lock_poll_s"],
    opener: Opener = duckdb.connect,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> Path:
    """Back up the DuckDB file and the small files into ``duckdb/<set_id>``.

    The copy is made while a read-only handle is held, so no writer can change
    the file during it. The copy must then open and give the same table counts.
    """
    started = time.monotonic()
    if not duckdb_path.is_file():
        raise BackupError(f"no DuckDB file at {duckdb_path}")
    con = open_read_only(
        duckdb_path,
        lock_wait_s=lock_wait_s,
        lock_poll_s=lock_poll_s,
        opener=opener,
        clock=clock,
        sleep=sleep,
    )
    if con is None:
        raise BackupError("a writer held the DuckDB file for the whole lock wait")
    wal = Path(str(duckdb_path) + WAL_SUFFIX)
    try:
        partial = _start_set(root, "duckdb", set_id)
        counts = table_counts(con)
        version = str(con.execute("SELECT version()").fetchone()[0])
        copy_file_synced(duckdb_path, partial / DUCKDB_COPY)
        wal_included = wal.is_file()
        if wal_included:
            copy_file_synced(wal, partial / (DUCKDB_COPY + WAL_SUFFIX))
    finally:
        con.close()
    copy_counts = count_copy(partial / DUCKDB_COPY)
    if copy_counts != counts:
        raise BackupError(
            f"the copy's table counts differ from the source's: {_count_diff(counts, copy_counts)}"
        )
    data_files = backup_small_files(
        partial, calibration=calibration, prop_validation=prop_validation, mark_dir=mark_dir
    )
    final, total, seconds = _finish_set(
        root,
        "duckdb",
        set_id,
        partial,
        {
            "duckdb_version": version,
            "source_path": str(duckdb_path),
            "wal_included": wal_included,
            "counts": counts,
            "data_files": data_files,
        },
        started,
    )
    print(f"BACKUP OK duckdb {set_id} {total} bytes {seconds} s", flush=True)
    return final


def _ignore_in_use(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.startswith(IN_USE_PREFIX)}


def backup_bundle(root: Path, set_id: str, *, play_pool_dir: Path) -> Path:
    """Copy the published bundle version into ``bundle/<set_id>/<build id>``."""
    started = time.monotonic()
    link = play_pool_dir / BUNDLE_LINK
    if not link.is_dir():
        raise BackupError(f"no published bundle at {link}")
    source = Path(resolve_bundle_dir(str(link)))
    build_id = source.name
    partial = _start_set(root, "bundle", set_id)
    shutil.copytree(source, partial / build_id, ignore=_ignore_in_use)
    for path in _files_under(partial):
        fsync_path(path)
    marker = source / PUBLISHED_MARKER
    published_at = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
    final, total, seconds = _finish_set(
        root,
        "bundle",
        set_id,
        partial,
        {"build_id": build_id, "source_path": str(source), "published_at": published_at},
        started,
    )
    print(f"BACKUP OK bundle {set_id} {total} bytes {seconds} s", flush=True)
    return final


# ---------------------------------------------------------------------------
# Reading, verifying and restoring a set
# ---------------------------------------------------------------------------


def find_set(root: Path, store: str, set_id: str) -> SetInfo | None:
    """The complete set ``set_id`` of ``store`` ('latest' = the newest), or None."""
    if set_id == "latest":
        return newest_complete_set(root, store)
    for info in complete_sets(root, store):
        if info.set_id == set_id:
            return info
    return None


def read_manifest(info: SetInfo) -> dict[str, Any]:
    """The manifest of a complete set."""
    loaded = json.loads((info.path / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise BackupError(f"the manifest of {info.store}/{info.set_id} is not an object")
    return loaded


def check_hashes(
    info: SetInfo, manifest: dict[str, Any], only: set[str] | None = None
) -> list[str]:
    """Check every file the manifest lists by size and sha256. Returns the problems."""
    problems: list[str] = []
    for entry in manifest.get("files", []):
        rel = str(entry["path"])
        if only is not None and rel not in only:
            continue
        path = info.path / rel
        if not path.is_file():
            problems.append(f"{rel} is missing")
            continue
        size = path.stat().st_size
        if size != entry.get("bytes"):
            problems.append(f"{rel} size {size} != {entry.get('bytes')}")
            continue
        want = entry.get("sha256")
        if want is not None and sha256_file(path) != want:
            problems.append(f"{rel} sha256 mismatch")
    return problems


def load_bundle(path: Path) -> None:
    """Load a bundle copy with the engine-artifact loader (imported only here)."""
    from pipeline.batch.engine_artifacts import EngineArtifacts

    EngineArtifacts.load(str(path))


def verify_duckdb_set(info: SetInfo) -> list[str]:
    """Check a DuckDB set: every hash, then the copy's counts. Returns the problems."""
    manifest = read_manifest(info)
    problems = check_hashes(info, manifest)
    copy = info.path / DUCKDB_COPY
    if not copy.is_file():
        return [*problems, f"{DUCKDB_COPY} is missing"]
    try:
        got = count_copy(copy)
    except Exception as exc:  # a corrupt copy raises a DuckDB error of any kind
        return [*problems, f"the copy does not open: {exc}"]
    want = {str(k): int(v) for k, v in manifest.get("counts", {}).items()}
    if got != want:
        problems.append(f"table counts differ: {_count_diff(want, got)}")
    return problems


def verify_bundle_set(info: SetInfo) -> list[str]:
    """Check a bundle set: every hash, then a load of the copy. Returns the problems."""
    manifest = read_manifest(info)
    problems = check_hashes(info, manifest)
    build_id = str(manifest.get("build_id") or "")
    build_dir = info.path / build_id
    if not build_id or not build_dir.is_dir():
        return [*problems, "the build id folder is missing"]
    started = time.monotonic()
    try:
        load_bundle(build_dir)
    except Exception as exc:  # the loader raises many kinds of error
        return [*problems, f"the bundle does not load: {type(exc).__name__}: {exc}"]
    log(f"the bundle copy {build_id} loaded in {time.monotonic() - started:.1f} s")
    return problems


def verify_set(root: Path, set_id: str) -> bool:
    """Verify the DuckDB and bundle sets ``set_id`` ('latest' = each store's newest).

    True when at least one set is present and every present set verifies.
    """
    checks = {"duckdb": verify_duckdb_set, "bundle": verify_bundle_set}
    found = 0
    ok = True
    for store, check in checks.items():
        info = find_set(root, store, set_id)
        if info is None:
            log(f"VERIFY {store}: no complete set {set_id}")
            continue
        found += 1
        problems = check(info)
        if problems:
            ok = False
            log(f"VERIFY FAIL {store} {info.set_id}: " + "; ".join(problems))
        else:
            log(f"VERIFY OK {store} {info.set_id} ({human_bytes(info.bytes)})")
    if found == 0:
        log(f"VERIFY FAIL: no DuckDB or bundle set {set_id}")
        return False
    return ok


def restore_duckdb(root: Path, set_id: str, *, duckdb_path: Path) -> None:
    """Put a DuckDB set back onto the live path with ``os.replace``.

    Stop every DuckDB reader first, and restart the app after: a reader that
    stays open keeps reading the old file.
    """
    info = find_set(root, "duckdb", set_id)
    if info is None:
        raise BackupError(f"no complete DuckDB set {set_id}")
    manifest = read_manifest(info)
    src = info.path / DUCKDB_COPY
    src_wal = info.path / (DUCKDB_COPY + WAL_SUFFIX)
    names = {DUCKDB_COPY} | ({src_wal.name} if src_wal.is_file() else set())
    problems = check_hashes(info, manifest, only=names)
    if problems or not src.is_file():
        raise BackupError("the set fails its check: " + ("; ".join(problems) or "no copy"))
    live_wal = Path(str(duckdb_path) + WAL_SUFFIX)
    tmp = Path(str(duckdb_path) + ".restore-tmp")
    tmp_wal = Path(str(live_wal) + ".restore-tmp")
    copy_file_synced(src, tmp)
    if src_wal.is_file():
        copy_file_synced(src_wal, tmp_wal)
    if live_wal.exists():
        # The live WAL belongs to the old file. Replayed onto the restored file, it corrupts it.
        aside = Path(f"{live_wal}.pre-restore-{datetime.now(UTC).strftime(SET_ID_FORMAT)}")
        os.replace(live_wal, aside)
        log(f"moved the live WAL aside to {aside}")
    os.replace(tmp, duckdb_path)
    if src_wal.is_file():
        os.replace(tmp_wal, live_wal)
    fsync_path(duckdb_path.parent)
    log(f"RESTORE OK duckdb {info.set_id} -> {duckdb_path}; restart the app now")


def restore_bundle(root: Path, set_id: str, *, play_pool_dir: Path, force: bool = False) -> Path:
    """Copy a bundle set back as a version and publish it. Returns the version folder."""
    info = find_set(root, "bundle", set_id)
    if info is None:
        raise BackupError(f"no complete bundle set {set_id}")
    manifest = read_manifest(info)
    build_id = str(manifest.get("build_id") or "")
    src = info.path / build_id
    if not build_id or not src.is_dir():
        raise BackupError(f"the bundle set {info.set_id} has no build id folder")
    problems = check_hashes(info, manifest)
    if problems:
        raise BackupError("the set fails its check: " + "; ".join(problems))
    link = play_pool_dir / BUNDLE_LINK
    dest = Path(versions_root(str(link))) / build_id
    if dest.exists() and not force:
        raise BackupError(f"{dest} exists; pass --force to copy over it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)
    publish_bundle_version(str(link), str(dest))
    log(f"RESTORE OK bundle {info.set_id} -> {dest} (published)")
    return dest


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_rows(root: Path, now: datetime | None = None) -> dict[str, dict[str, Any] | None]:
    """The newest complete set of every store, as plain data."""
    now = now or datetime.now(UTC)
    rows: dict[str, dict[str, Any] | None] = {}
    for store in STORES:
        sets = complete_sets(root, store)
        if not sets:
            rows[store] = None
            continue
        info = sets[-1]
        rows[store] = {
            "set": info.set_id,
            "created_at": _iso(info.created_at),
            "age_hours": round(age_hours(info, now), 2),
            "bytes": info.bytes,
            "sets": len(sets),
        }
    return rows


def print_status(root: Path, as_json: bool) -> None:
    """Print the newest set of every store, as text or as JSON."""
    rows = status_rows(root)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    print(f"backups under {root}")
    for store, row in rows.items():
        if row is None:
            print(f"  {store:<8} none")
            continue
        print(
            f"  {store:<8} {row['set']}  created {row['created_at']}  "
            f"age {row['age_hours']:.1f} h  {human_bytes(row['bytes'])}  ({row['sets']} sets)"
        )


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.backup.data_stores",
        description="Back up, verify and restore the DuckDB file and the engine bundle.",
    )
    p.add_argument("--only", action="append", choices=DATA_STORES, help="back up this store only")
    p.add_argument("--backups-root", type=Path, default=None)
    p.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="the data folder; given, it beats the path variables (default /data)",
    )
    for name, default in DEFAULTS.items():
        p.add_argument("--" + name.replace("_", "-"), type=float, default=default)
    p.add_argument("--keep", type=int, default=None)
    p.add_argument(
        "--new-set",
        action="store_true",
        help="back up every store even when a recent run already made its set",
    )
    action = p.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true")
    action.add_argument("--verify-set", metavar="SET")
    action.add_argument("--restore-duckdb", metavar="SET")
    action.add_argument("--restore-bundle", metavar="SET")
    p.add_argument("--json", action="store_true", help="with --status: print JSON")
    p.add_argument("--yes", action="store_true", help="confirm a restore")
    p.add_argument("--force", action="store_true", help="--restore-bundle: copy over a version")
    return p


def _paths(data_root: Path | None) -> dict[str, Path]:
    """The data paths. A ``--data-root`` flag beats the variables; they beat /data."""
    data = data_root or Path("/data")

    def env_path(name: str, default: Path) -> Path:
        value = None if data_root is not None else os.environ.get(name)
        return Path(value) if value else default

    return {
        "duckdb": env_path("BASEBALL_DUCKDB_PATH", data / "baseball_sim.duckdb"),
        "play_pool": env_path("BASEBALL_PLAY_POOL_DIR", data / "play_pool"),
        "marks": env_path("INGEST_MARK_DIR", data / "ingest_marks"),
        "calibration": env_path("CALIBRATION_REPORT_PATH", data / "calibration.json"),
        "prop_validation": env_path("PROP_VALIDATION_PATH", data / "prop_validation.json"),
    }


def recent_sets(
    root: Path, stores: Sequence[str], *, window_s: float, now: float
) -> dict[str, str]:
    """The stores whose newest complete set is younger than ``window_s``.

    The result maps each such store to its set id. A crash retry of the
    nightly run reads it: a store that finished on the first attempt is not
    backed up a second time. A window of 0 or less turns the check off.
    """
    if window_s <= 0:
        return {}
    done: dict[str, str] = {}
    for store in stores:
        info = newest_complete_set(root, store)
        if info is None:
            continue
        # The set id is the run's start time, on the same clock as ``now``.
        started = parse_set_id(info.set_id) or info.created_at
        if 0 <= now - started.timestamp() < window_s:
            done[store] = info.set_id
    return done


def run_backup(
    root: Path,
    stores: Sequence[str],
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    opener: Opener = duckdb.connect,
) -> int:
    """Back up the stores. Returns the exit code (0, 1 or 2)."""
    if not root.is_dir() or not os.access(root, os.W_OK):
        log(f"refused: the backup root {root} is missing or cannot be written")
        return 2
    keep = args.keep if args.keep is not None else int(_env_float("BACKUP_KEEP", 3))
    min_free_gb = _env_float("BACKUP_MIN_FREE_GB", 10.0)
    factor = _env_float("BACKUP_FREE_FACTOR", 1.5)
    clear_partials(root, stores)
    window = 0.0 if getattr(args, "new_set", False) else float(args.rerun_window_s)
    done = recent_sets(root, stores, window_s=window, now=clock())
    for store, done_id in done.items():
        log(f"the {store} set {done_id} is under {window:.0f} s old; the rerun skips the store")
    stores = [s for s in stores if s not in done]
    if not stores:
        log("every store already has its set from this run; nothing to do")
        return 0
    for store in stores:
        if not free_space_ok(root, store, min_free_gb=min_free_gb, factor=factor):
            return 2
    if done:
        set_id = max(done.values())  # the rerun keeps the first attempt's set id
    else:
        set_id = datetime.fromtimestamp(clock(), UTC).strftime(SET_ID_FORMAT)
    log(f"backup run {set_id}: {', '.join(stores)} into {root}")
    failed: list[str] = []
    if "duckdb" in stores:
        if not wait_for_marks(
            paths["marks"],
            grace_s=args.grace_s,
            wait_s=args.wait_s,
            poll_s=args.poll_s,
            stale_s=args.stale_s,
            clock=clock,
            sleep=sleep,
        ):
            log("FAIL duckdb: a writer job still runs; the DuckDB store is skipped")
            failed.append("duckdb")
        else:
            try:
                backup_duckdb(
                    root,
                    set_id,
                    duckdb_path=paths["duckdb"],
                    calibration=paths["calibration"],
                    prop_validation=paths["prop_validation"],
                    mark_dir=paths["marks"],
                    lock_wait_s=args.lock_wait_s,
                    lock_poll_s=args.lock_poll_s,
                    opener=opener,
                    clock=clock,
                    sleep=sleep,
                )
            except Exception as exc:  # one store's failure must not stop the other
                log(f"FAIL duckdb: {type(exc).__name__}: {exc}")
                failed.append("duckdb")
    if "bundle" in stores:
        try:
            backup_bundle(root, set_id, play_pool_dir=paths["play_pool"])
        except Exception as exc:  # one store's failure must not stop the other
            log(f"FAIL bundle: {type(exc).__name__}: {exc}")
            failed.append("bundle")
    if failed:
        log(f"the run failed ({', '.join(failed)}); nothing is pruned")
        return 1
    for store in stores:
        prune_sets(root, store, keep, current=set_id)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    opener: Opener = duckdb.connect,
) -> int:
    """Run the command line. Returns the exit code.

    ``clock``, ``sleep`` and ``opener`` let the tests run the waits instantly.
    """
    args = _parser().parse_args(argv)
    root = args.backups_root or Path(os.environ.get("BACKUP_ROOT") or "/backups")
    paths = _paths(args.data_root)
    if args.status:
        print_status(root, args.json)
        return 0
    try:
        if args.verify_set:
            return 0 if verify_set(root, args.verify_set) else 1
        if args.restore_duckdb or args.restore_bundle:
            if not args.yes:
                log("refused: a restore replaces live data; pass --yes")
                return 2
            if args.restore_duckdb:
                restore_duckdb(root, args.restore_duckdb, duckdb_path=paths["duckdb"])
            else:
                restore_bundle(
                    root, args.restore_bundle, play_pool_dir=paths["play_pool"], force=args.force
                )
            return 0
    except BackupError as exc:
        log(f"FAIL: {exc}")
        return 1
    stores = [s for s in DATA_STORES if not args.only or s in args.only]
    return run_backup(root, stores, paths, args, clock=clock, sleep=sleep, opener=opener)


if __name__ == "__main__":
    sys.exit(main())
