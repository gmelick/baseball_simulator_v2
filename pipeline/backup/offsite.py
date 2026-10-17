"""pipeline/backup/offsite.py — the off-site copy of the backup sets (SIM-568).

Run it in the jobs container as ``appuser``:

    python -m pipeline.backup.offsite              # copy the newest sets off-site
    python -m pipeline.backup.offsite --status     # the newest local and off-site sets
    python -m pipeline.backup.offsite --dry-run    # what a run would copy and prune

The off-site root (``/offsite``) is the owner's OneDrive folder, bind-mounted.
The OneDrive client uploads whatever lands there, so the cloud copy is the
off-machine copy. This module copies the sets; it cannot see the upload.

What a run does:

1. It refuses (exit 2) when either root is missing, when the off-site root
   cannot be written, or when the two roots are one folder.
2. It takes the copy lock (``offsite-copy.lock`` in the mark folder): one
   copy run at a time. A second run refuses (exit 2) while a live run holds
   the lock, so a ``make backup-offsite`` never writes the set folders that
   the scheduled run writes.
3. It waits while the data job of the scheduled backups runs (its
   ``nightly-backup.running`` mark) or while the Postgres job holds its lock
   (``postgres/.lock/owner``). After ``--wait-s`` it logs and copies anyway:
   a complete set is always safe to copy.
4. For each store it takes the newest ``keep`` complete local sets (the
   wanted sets). The free-space guard checks the disk once, before any
   write, for the bytes of every set that needs a copy. Then, per store, the
   run deletes the dead off-site folders that no wanted set needs and copies
   every wanted set that is not mirrored, the oldest first.
5. A set is copied file by file into ``<store>/<set id>/``, with no
   ``.partial`` folder and no rename: OneDrive holds new files open while it
   uploads them, and a Windows folder rename fails then. The manifest goes
   last, so a folder with no ``manifest.json`` reads as incomplete to every
   reader. A rerun after a crash keeps each file that already matches and
   copies the rest.
6. After a store copied cleanly, the store keeps its newest ``keep`` complete
   off-site sets, and never prunes a set that the local side still wants. A
   pruned set loses its manifest first, then its folder.

A set is MIRRORED when its off-site manifest equals the local one byte for
byte and every file the manifest lists has its size off-site.

This module is pure standard library: it must never import duckdb.

Exit codes: 0 every store copied or already in sync; 1 a store failed (the
others were still attempted); 2 a refusal before any write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline.backup.common import (
    CHUNK,
    GIB,
    Clock,
    Sleep,
    fresh_marks,
    fsync_path,
    human_bytes,
    sha256_file,
)
from pipeline.backup.status import (
    MANIFEST,
    PARTIAL_SUFFIX,
    STORES,
    SetInfo,
    age_hours,
    complete_sets,
    newest_complete_set,
)

#: The mark of the data job of the scheduled backups (SIM-525). The copy waits while it runs.
DATA_JOB_MARK = "nightly-backup"

#: The Postgres job's lock file under the backup root. It holds "<pid> <start epoch>".
POSTGRES_LOCK_OWNER = Path("postgres") / ".lock" / "owner"

#: The copy lock in the mark folder (SIM-568): one copy run at a time.
#: It holds "<pid> <start epoch> <host>".
COPY_LOCK = "offsite-copy.lock"

#: The probe file that tells whether the two roots are one folder.
#: No leading dot: OneDrive refuses some dot names.
PROBE_PREFIX = "offsite-probe-"

#: The manifest is written here first, then moved over ``manifest.json``.
MANIFEST_TMP = MANIFEST + ".tmp"

#: The manifest move retries while OneDrive holds the folder.
REPLACE_TRIES = 10
REPLACE_DELAY_S = 3.0

#: The off-site sets kept per store when no flag or variable says otherwise.
DEFAULT_KEEP = 3

#: The default waits, in seconds. Each one has a command-line flag.
#: ``lock_stale_s`` is the age at which the Postgres job's lock and the copy
#: lock read as stale.
DEFAULTS: dict[str, float] = {
    "grace_s": 0.0,
    "wait_s": 7200.0,
    "poll_s": 60.0,
    "stale_s": 21600.0,
    "lock_stale_s": 21600.0,
}


class OffsiteError(RuntimeError):
    """A store's off-site copy failed. The message says why."""


@dataclass(frozen=True)
class FileEntry:
    """One file of a set, as its manifest lists it."""

    rel: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class CopyResult:
    """What one set copy did."""

    copied: int
    kept: int
    size: int
    seconds: float


@dataclass(frozen=True)
class StorePlan:
    """The wanted sets of one store, and the ones among them that need a copy."""

    store: str
    wanted: list[SetInfo]
    work: list[SetInfo]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    """Print one log line with a UTC stamp."""
    print(f"[backup-offsite {_iso(datetime.now(UTC))}] {message}", flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_fd(fd: int) -> None:
    # The read-back hash is the check of the copy; a file system that cannot
    # fsync must not stop the run (the same rule as common.fsync_path).
    try:
        os.fsync(fd)
    except OSError:
        pass


def resolve_keep(flag: int | None) -> int:
    """The off-site sets kept per store: ``--keep``, else OFFSITE_KEEP, else BACKUP_KEEP, else 3.

    A value of 0 or less reads as 1: the copy always keeps the newest set.
    """
    value = flag
    if value is None:
        for name in ("OFFSITE_KEEP", "BACKUP_KEEP"):
            raw = os.environ.get(name, "").strip()
            if not raw:
                continue
            try:
                value = int(float(raw))
            except ValueError:
                log(f"ignored {name}={raw!r}: not a number")
                continue
            break
    if value is None:
        value = DEFAULT_KEEP
    return max(1, value)


# ---------------------------------------------------------------------------
# Manifests and the mirrored check
# ---------------------------------------------------------------------------


def read_manifest_bytes(set_dir: Path) -> tuple[bytes, list[FileEntry]]:
    """The manifest of a set folder: its bytes and its file list.

    Raises OSError when the manifest cannot be read, and OffsiteError when it
    does not parse or lists a path outside the set.
    """
    raw = (set_dir / MANIFEST).read_bytes()
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OffsiteError(f"the manifest of {set_dir} does not parse: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), list):
        raise OffsiteError(f"the manifest of {set_dir} has no file list")
    entries: list[FileEntry] = []
    for item in loaded["files"]:
        rel = item.get("path") if isinstance(item, dict) else None
        size = item.get("bytes") if isinstance(item, dict) else None
        if (
            not isinstance(rel, str)
            or not rel
            or isinstance(size, bool)
            or not isinstance(size, int)
        ):
            raise OffsiteError(f"the manifest of {set_dir} has a bad file entry: {item!r}")
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or rel in (MANIFEST, MANIFEST_TMP):
            raise OffsiteError(f"the manifest of {set_dir} lists a path outside the set: {rel}")
        sha = item.get("sha256")
        entries.append(FileEntry(rel=rel, size=size, sha256=sha if isinstance(sha, str) else None))
    return raw, entries


def is_mirrored(info: SetInfo, dest: Path) -> bool:
    """True when ``dest`` holds a complete copy of the local set ``info``.

    The off-site manifest must equal the local one byte for byte, and every
    listed file must have its size. A stat never downloads an online-only
    OneDrive file, so the check stays cheap on a later night.
    """
    try:
        want, entries = read_manifest_bytes(info.path)
        got = (dest / MANIFEST).read_bytes()
    except (OSError, OffsiteError):
        return False
    if got != want:
        return False
    for entry in entries:
        try:
            st = (dest / entry.rel).stat()
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry.size:
            return False
    return True


# ---------------------------------------------------------------------------
# The refusals, the wait and the free-space guard
# ---------------------------------------------------------------------------


def _same_by_realpath(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def check_roots(backups_root: Path, offsite_root: Path, *, dry_run: bool = False) -> str | None:
    """None when the run may go on; otherwise the reason for the refusal.

    Two mounts of one folder have different paths, so the check writes a probe
    file off-site and looks for it under the backup root. A dry run writes
    nothing and compares the real paths only.
    """
    if not backups_root.is_dir():
        return f"the backup root {backups_root} is missing"
    if not offsite_root.is_dir():
        return f"the off-site root {offsite_root} is missing"
    if _same_by_realpath(backups_root, offsite_root):
        return f"the two roots are one folder ({backups_root})"
    if dry_run:
        return None
    if not os.access(offsite_root, os.W_OK):
        return f"the off-site root {offsite_root} cannot be written"
    probe = offsite_root / f"{PROBE_PREFIX}{os.getpid()}.tmp"
    try:
        probe.write_bytes(b"")
    except OSError as exc:
        return f"the off-site root {offsite_root} cannot be written ({exc})"
    try:
        if (backups_root / probe.name).exists():
            return (
                f"the two roots are one folder (the probe {probe.name} shows under {backups_root})"
            )
    finally:
        _unlink_quiet(probe)
    return None


def postgres_lock_epoch(backups_root: Path) -> float | None:
    """The start epoch in the Postgres job's lock, or None when there is no readable lock."""
    try:
        fields = (backups_root / POSTGRES_LOCK_OWNER).read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        return float(fields[1])
    except ValueError:
        return None


def busy_reasons(
    backups_root: Path,
    mark_dir: Path,
    *,
    stale_s: float,
    lock_stale_s: float,
    now: float,
) -> list[str]:
    """The backup jobs that run now. An empty list means the copy may start.

    A ``postgres/*.partial`` folder with no lock is a dead run (the job's exit
    trap removes the lock and leaves the partial), so the copy never waits on one.
    """
    reasons = [f"the {name} job" for name in fresh_marks(mark_dir, (DATA_JOB_MARK,), stale_s, now)]
    epoch = postgres_lock_epoch(backups_root)
    if epoch is not None and now - epoch < lock_stale_s:
        reasons.append("the Postgres backup (postgres/.lock)")
    return reasons


def wait_until_quiet(
    backups_root: Path,
    mark_dir: Path,
    *,
    grace_s: float = DEFAULTS["grace_s"],
    wait_s: float = DEFAULTS["wait_s"],
    poll_s: float = DEFAULTS["poll_s"],
    stale_s: float = DEFAULTS["stale_s"],
    lock_stale_s: float = DEFAULTS["lock_stale_s"],
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> bool:
    """Wait while a backup job runs. False on a timeout.

    ``clock`` returns epoch seconds, the unit of the mark and of the lock.
    """
    if grace_s > 0:
        sleep(grace_s)
    deadline = clock() + wait_s
    while True:
        busy = busy_reasons(
            backups_root, mark_dir, stale_s=stale_s, lock_stale_s=lock_stale_s, now=clock()
        )
        if not busy:
            return True
        if clock() >= deadline:
            log(f"{', '.join(busy)} still runs after {wait_s:.0f} s; the wait gives up")
            return False
        log(f"{', '.join(busy)} runs; waiting {poll_s:.0f} s")
        sleep(poll_s)


def free_space_ok(
    offsite_root: Path, needed_bytes: int, *, min_free_gb: float, factor: float
) -> bool:
    """True when the off-site disk has room for ``needed_bytes``.

    The room needed is the larger of ``min_free_gb`` GiB and ``factor`` times
    the bytes to copy. The check reads the local disk under the mount, not the
    OneDrive quota: the container cannot see the quota.
    """
    needed = max(min_free_gb * GIB, factor * needed_bytes)
    free = shutil.disk_usage(offsite_root).free
    log(
        f"the copy needs {human_bytes(int(needed))} free and {human_bytes(free)} is free "
        f"under {offsite_root} (the local disk, not the OneDrive quota)"
    )
    return free >= needed


# ---------------------------------------------------------------------------
# The copy lock (SIM-568)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid runs here. Only POSIX can probe a pid."""
    if os.name != "posix":
        return True  # on Windows os.kill(pid, 0) sends a signal; the age decides there
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # the process exists but belongs to another user
    return True


def lock_holder(lock: Path, *, lock_stale_s: float, now: float) -> str | None:
    """Who holds the copy lock, or None when no live run holds it.

    A lock is stale when it is older than ``lock_stale_s``, or when this host
    took it and its pid is gone. The second rule lets the crash rerun of
    ``with_retry.sh`` take the lock that the crashed run left. A pid of
    another host (another container) cannot be probed, so its age decides.
    """
    try:
        text = lock.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"the lock cannot be read ({exc})"
    fields = text.split()
    try:
        pid: int | None = int(fields[0])
    except (IndexError, ValueError):
        pid = None
    try:
        epoch = float(fields[1])
    except (IndexError, ValueError):
        try:
            epoch = lock.stat().st_mtime
        except OSError:
            return None
    host = fields[2] if len(fields) > 2 else None
    age_s = now - epoch
    if age_s >= lock_stale_s:
        return None
    if pid is not None and host == socket.gethostname() and not _pid_alive(pid):
        return None
    owner = f"pid {pid}" if pid is not None else "an unknown pid"
    return f"{owner} on {host or 'an unknown host'}, {age_s:.0f} s old"


def take_lock(mark_dir: Path, *, lock_stale_s: float, now: float) -> tuple[Path | None, str | None]:
    """Take the copy lock. Returns (the lock, None), or (None, the refusal reason).

    The lock is created with O_EXCL, so two runs cannot both create it. A
    stale lock is removed and taken once more. When the mark folder cannot be
    written, the run goes on unlocked and says so, as ``with_mark.sh`` does.
    """
    lock = mark_dir / COPY_LOCK
    try:
        mark_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"cannot write the mark folder {mark_dir} ({exc}); the copy runs unlocked")
        return None, None
    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = lock_holder(lock, lock_stale_s=lock_stale_s, now=now)
            if holder is not None:
                return None, f"another off-site copy run holds {lock} ({holder})"
            log(f"removing a stale copy lock {lock}")
            _unlink_quiet(lock)
            continue
        except OSError as exc:
            log(f"cannot create the copy lock {lock} ({exc}); the copy runs unlocked")
            return None, None
        try:
            os.write(fd, f"{os.getpid()} {int(now)} {socket.gethostname()}\n".encode())
        finally:
            os.close(fd)
        return lock, None
    return None, f"another off-site copy run took {lock}"


def release_lock(lock: Path) -> None:
    """Delete the copy lock when this run still owns it."""
    try:
        fields = lock.read_text(encoding="utf-8").split()
    except OSError:
        return
    if fields and fields[0] == str(os.getpid()):
        _unlink_quiet(lock)


# ---------------------------------------------------------------------------
# Copying a set
# ---------------------------------------------------------------------------


def copy_and_hash(src: Path, dest: Path) -> str:
    """Copy ``src`` to ``dest`` in CHUNK reads. Returns the source's sha256.

    The digest comes from the bytes as they are read, so the source is read
    once. The copy is flushed through its own handle. Plain reads and writes,
    not ``shutil.copyfile``: its ``sendfile`` path over the Docker Desktop
    bind mount is unpredictable.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        while chunk := fin.read(CHUNK):
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        _fsync_fd(fout.fileno())
    return digest.hexdigest()


def _file_matches(path: Path, entry: FileEntry, use_hash: bool) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != entry.size:
        return False
    if not use_hash or entry.sha256 is None:
        return True
    try:
        return sha256_file(path) == entry.sha256
    except OSError:
        return False


def check_source(info: SetInfo, entries: list[FileEntry]) -> None:
    """Refuse a local set whose files are missing or differ in size from its manifest."""
    for entry in entries:
        name = f"{info.store}/{info.set_id}/{entry.rel}"
        try:
            size = (info.path / entry.rel).stat().st_size
        except OSError as exc:
            raise OffsiteError(f"the local set is damaged: {name} cannot be read ({exc})") from exc
        if size != entry.size:
            raise OffsiteError(
                f"the local set is damaged: {name} has {size} bytes, its manifest says {entry.size}"
            )


def write_manifest(
    manifest_bytes: bytes,
    dest: Path,
    *,
    tries: int = REPLACE_TRIES,
    delay_s: float = REPLACE_DELAY_S,
    sleep: Sleep = time.sleep,
) -> None:
    """Write the source manifest's bytes verbatim as ``dest/manifest.json``.

    The bytes go to ``manifest.json.tmp`` first, then ``os.replace`` moves them
    into place. The move retries on PermissionError, which OneDrive raises while
    it holds the folder. On give-up the ``.tmp`` is deleted and the set stays
    incomplete. Verbatim matters: a half-written manifest would read as
    complete, and the byte compare of the mirrored check needs the same bytes.
    """
    tmp = dest / MANIFEST_TMP
    final = dest / MANIFEST
    try:
        with open(tmp, "wb") as fh:
            fh.write(manifest_bytes)
            fh.flush()
            _fsync_fd(fh.fileno())
        for attempt in range(1, tries + 1):
            try:
                os.replace(tmp, final)
            except PermissionError as exc:
                if attempt >= tries:
                    raise OffsiteError(
                        f"the manifest move in {dest} failed {tries} times: {exc}"
                    ) from exc
                log(f"the manifest move in {dest} failed ({exc}); retrying in {delay_s:.0f} s")
                sleep(delay_s)
                continue
            fsync_path(dest)
            return
    except BaseException:
        _unlink_quiet(tmp)
        raise


def copy_set(
    info: SetInfo,
    offsite_root: Path,
    *,
    use_hash: bool = True,
    sleep: Sleep = time.sleep,
) -> CopyResult:
    """Copy one local set into ``offsite_root/<store>/<set id>``. The manifest goes last.

    The source manifest's file list is the set: a file it does not list is not
    copied. A destination file that already matches (its size, and its sha256
    unless ``use_hash`` is off) is kept, so a rerun resumes a crashed copy.
    """
    started = time.monotonic()
    manifest_bytes, entries = read_manifest_bytes(info.path)
    check_source(info, entries)
    dest = offsite_root / info.store / info.set_id
    dest.mkdir(parents=True, exist_ok=True)
    # An older manifest goes first, so the folder reads as incomplete during the copy.
    (dest / MANIFEST).unlink(missing_ok=True)
    copied = kept = 0
    for entry in entries:
        name = f"{info.store}/{info.set_id}/{entry.rel}"
        target = dest / entry.rel
        if _file_matches(target, entry, use_hash):
            kept += 1
            continue
        digest = copy_and_hash(info.path / entry.rel, target)
        if entry.sha256 is not None and digest != entry.sha256:
            raise OffsiteError(
                f"the local set is damaged: {name} does not match its manifest sha256"
            )
        if use_hash and sha256_file(target) != digest:
            raise OffsiteError(f"the read-back of {name} does not match the source")
        copied += 1
    for folder in sorted({(dest / e.rel).parent for e in entries}):
        fsync_path(folder)
    write_manifest(manifest_bytes, dest, sleep=sleep)
    return CopyResult(
        copied=copied, kept=kept, size=info.bytes, seconds=round(time.monotonic() - started, 3)
    )


# ---------------------------------------------------------------------------
# Dead folders and retention
# ---------------------------------------------------------------------------


def dead_folders(offsite_root: Path, store: str, wanted: set[str]) -> list[Path]:
    """The dead off-site folders of ``store`` that no wanted set needs.

    A folder is dead when its name ends in ``.partial`` or it has no manifest.
    A dead folder of a wanted set is not listed: the copy resumes it.
    """
    try:
        entries = sorted((offsite_root / store).iterdir())
    except OSError:
        return []
    dead: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or entry.name in wanted:
            continue
        if entry.name.endswith(PARTIAL_SUFFIX) or not (entry / MANIFEST).is_file():
            dead.append(entry)
    return dead


def clean_dead_folders(offsite_root: Path, store: str, wanted: set[str]) -> list[str]:
    """Delete the dead off-site folders of ``store`` that no wanted set needs."""
    removed: list[str] = []
    for entry in dead_folders(offsite_root, store, wanted):
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
        if entry.exists():
            log(f"the dead off-site folder {store}/{entry.name} stays: a file is held")
        else:
            log(f"deleted the dead off-site folder {store}/{entry.name}")
    return removed


def prune_victims(sets: list[tuple[datetime, str]], keep: int, protect: set[str]) -> list[str]:
    """The set ids to prune: all but the newest ``keep``, never one in ``protect``."""
    ordered = sorted(sets)
    keep_ids = {set_id for _, set_id in ordered[-keep:]} if keep > 0 else set()
    return [set_id for _, set_id in ordered if set_id not in keep_ids and set_id not in protect]


def prune_offsite(offsite_root: Path, store: str, keep: int, protect: set[str]) -> list[str]:
    """Keep the newest ``keep`` complete off-site sets of ``store``. Returns the pruned ids.

    A set in ``protect`` (a set the local side still wants) is never pruned:
    a pruned wanted set would be copied again the next night and pruned again
    the night after. The manifest goes first, so a set that is half deleted
    never reads as complete; a file OneDrive still holds then stays until the
    next run's dead-folder clean.
    """
    sets = complete_sets(offsite_root, store)
    by_id = {s.set_id: s for s in sets}
    removed: list[str] = []
    for set_id in prune_victims([(s.created_at, s.set_id) for s in sets], keep, protect):
        info = by_id[set_id]
        try:
            (info.path / MANIFEST).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log(f"could not prune the off-site {store} set {set_id} ({exc}); the next run retries")
            continue
        shutil.rmtree(info.path, ignore_errors=True)
        removed.append(set_id)
        if info.path.exists():
            log(
                f"pruned the off-site {store} set {set_id}; a held file keeps its folder "
                "until the next run's dead-folder clean"
            )
        else:
            log(f"pruned the off-site {store} set {set_id}")
    return removed


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def plan_store(backups_root: Path, offsite_root: Path, store: str, keep: int) -> StorePlan:
    """The newest ``keep`` complete local sets of ``store`` and the ones not yet mirrored."""
    wanted = complete_sets(backups_root, store)[-keep:]
    work = [i for i in wanted if not is_mirrored(i, offsite_root / store / i.set_id)]
    return StorePlan(store=store, wanted=wanted, work=work)


def mirror_store(
    plan: StorePlan, offsite_root: Path, keep: int, *, use_hash: bool, sleep: Sleep
) -> list[str]:
    """Copy the store's work list, the oldest first, then prune. Returns the copied ids."""
    wanted_ids = {i.set_id for i in plan.wanted}
    work_ids = {i.set_id for i in plan.work}
    clean_dead_folders(offsite_root, plan.store, wanted_ids)
    if not plan.wanted:
        log(f"no complete local {plan.store} set; nothing to copy")
    copied: list[str] = []
    for info in plan.wanted:
        if info.set_id not in work_ids:
            print(f"COPY SKIP {plan.store} {info.set_id} (already off-site)", flush=True)
            continue
        result = copy_set(info, offsite_root, use_hash=use_hash, sleep=sleep)
        print(
            f"COPY OK {plan.store} {info.set_id} {result.size} bytes {result.seconds} s",
            flush=True,
        )
        copied.append(info.set_id)
    # SIM-568: protect every wanted set, not only the ones copied now. An
    # off-site set newer than any local one would otherwise push a wanted set
    # out, and the next run would copy it back.
    prune_offsite(offsite_root, plan.store, keep, protect=wanted_ids)
    return copied


def print_dry_run(offsite_root: Path, plans: list[StorePlan], keep: int) -> None:
    """Print the folders a run would delete, the sets it would copy and prune."""
    for plan in plans:
        wanted_ids = {i.set_id for i in plan.wanted}
        work_ids = {i.set_id for i in plan.work}
        for entry in dead_folders(offsite_root, plan.store, wanted_ids):
            print(f"WOULD DELETE {plan.store} {entry.name} (dead folder)")
        for info in plan.wanted:
            if info.set_id in work_ids:
                print(f"WOULD COPY {plan.store} {info.set_id} {info.bytes} bytes")
            else:
                print(f"WOULD SKIP {plan.store} {info.set_id} (already off-site)")
        existing = [
            (s.created_at, s.set_id)
            for s in complete_sets(offsite_root, plan.store)
            if s.set_id not in work_ids
        ]
        future = existing + [(i.created_at, i.set_id) for i in plan.work]
        for set_id in prune_victims(future, keep, wanted_ids):
            print(f"WOULD PRUNE {plan.store} {set_id}")


def _plans_with_room(
    backups_root: Path, offsite_root: Path, stores: list[str], keep: int
) -> list[StorePlan] | None:
    """The store plans, or None when the free-space guard refuses them."""
    plans = [plan_store(backups_root, offsite_root, store, keep) for store in stores]
    needed = sum(info.bytes for plan in plans for info in plan.work)
    if needed > 0 and not free_space_ok(
        offsite_root,
        needed,
        min_free_gb=_env_float("BACKUP_MIN_FREE_GB", 10.0),
        factor=_env_float("BACKUP_FREE_FACTOR", 1.5),
    ):
        log(f"refused: the off-site disk is short of space for {human_bytes(needed)}")
        return None
    return plans


def run_copy(
    backups_root: Path,
    offsite_root: Path,
    mark_dir: Path,
    stores: list[str],
    keep: int,
    args: argparse.Namespace,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> int:
    """Copy the stores off-site. Returns the exit code (0, 1 or 2)."""
    reason = check_roots(backups_root, offsite_root, dry_run=args.dry_run)
    if reason is not None:
        log(f"refused: {reason}")
        return 2
    waits = {name: float(getattr(args, name)) for name in DEFAULTS}
    if args.dry_run:
        # A dry run writes nothing, so it takes no lock and does not wait.
        now = clock()
        busy = busy_reasons(
            backups_root,
            mark_dir,
            stale_s=waits["stale_s"],
            lock_stale_s=waits["lock_stale_s"],
            now=now,
        )
        if busy:
            log(f"{', '.join(busy)} runs; a real run would wait")
        holder = lock_holder(mark_dir / COPY_LOCK, lock_stale_s=waits["lock_stale_s"], now=now)
        if holder is not None:
            log(f"another off-site copy run holds the lock ({holder}); a real run would refuse")
        plans = _plans_with_room(backups_root, offsite_root, stores, keep)
        if plans is None:
            return 2
        print_dry_run(offsite_root, plans, keep)
        return 0
    # SIM-568: one copy run at a time. The lock spans the wait and the copy.
    lock, refusal = take_lock(mark_dir, lock_stale_s=waits["lock_stale_s"], now=clock())
    if refusal is not None:
        log(f"refused: {refusal}")
        return 2
    try:
        return _copy_locked(
            backups_root, offsite_root, mark_dir, stores, keep, args, waits, clock, sleep
        )
    finally:
        if lock is not None:
            release_lock(lock)


def _copy_locked(
    backups_root: Path,
    offsite_root: Path,
    mark_dir: Path,
    stores: list[str],
    keep: int,
    args: argparse.Namespace,
    waits: dict[str, float],
    clock: Clock,
    sleep: Sleep,
) -> int:
    """The wait and the copy of a run that holds the copy lock."""
    if not wait_until_quiet(
        backups_root,
        mark_dir,
        grace_s=waits["grace_s"],
        wait_s=waits["wait_s"],
        poll_s=waits["poll_s"],
        stale_s=waits["stale_s"],
        lock_stale_s=waits["lock_stale_s"],
        clock=clock,
        sleep=sleep,
    ):
        log("the copy goes on: a complete set is always safe to copy; --status shows BEHIND")
    plans = _plans_with_room(backups_root, offsite_root, stores, keep)
    if plans is None:
        return 2
    log(f"off-site copy of {', '.join(stores)} from {backups_root} to {offsite_root}, keep {keep}")
    failed: list[str] = []
    for plan in plans:
        try:
            mirror_store(plan, offsite_root, keep, use_hash=not args.no_hash, sleep=sleep)
        except Exception as exc:  # one store's failure must not stop the others
            log(f"FAIL {plan.store}: {type(exc).__name__}: {exc}")
            failed.append(plan.store)
    if failed:
        log(f"the run failed ({', '.join(failed)}); a failed store prunes nothing")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_rows(
    backups_root: Path, offsite_root: Path, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Per store: the newest local set, the newest off-site set, and whether it is mirrored."""
    now = now or datetime.now(UTC)
    rows: dict[str, dict[str, Any]] = {}
    for store in STORES:
        local = newest_complete_set(backups_root, store)
        off_sets = complete_sets(offsite_root, store)
        newest = off_sets[-1] if off_sets else None
        mirrored = local is not None and is_mirrored(local, offsite_root / store / local.set_id)
        rows[store] = {
            "local": None
            if local is None
            else {"set": local.set_id, "created_at": _iso(local.created_at)},
            "offsite": None
            if newest is None
            else {
                "set": newest.set_id,
                "created_at": _iso(newest.created_at),
                "age_hours": round(age_hours(newest, now), 2),
                "bytes": newest.bytes,
                "sets": len(off_sets),
            },
            "mirrored": mirrored,
        }
    return rows


def print_status(backups_root: Path, offsite_root: Path, as_json: bool) -> None:
    """Print the status rows as text or as JSON."""
    rows = status_rows(backups_root, offsite_root)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    print(f"off-site copies under {offsite_root} (local sets under {backups_root})")
    for store, row in rows.items():
        local, off = row["local"], row["offsite"]
        local_text = local["set"] if local else "-"
        off_text = (
            f"{off['set']}  age {off['age_hours']:.1f} h  {human_bytes(off['bytes'])}  "
            f"({off['sets']} sets)"
            if off
            else "-"
        )
        verdict = "none" if local is None else ("MIRRORED" if row["mirrored"] else "BEHIND")
        print(f"  {store:<8} local {local_text}  off-site {off_text}  {verdict}")


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.backup.offsite",
        description="Copy the newest complete backup sets to the off-site folder (OneDrive).",
    )
    p.add_argument(
        "--backups-root", type=Path, default=None, help="default $BACKUP_ROOT or /backups"
    )
    p.add_argument(
        "--offsite-root", type=Path, default=None, help="default $OFFSITE_ROOT or /offsite"
    )
    p.add_argument(
        "--mark-dir", type=Path, default=None, help="default $INGEST_MARK_DIR or /data/ingest_marks"
    )
    p.add_argument("--only", action="append", choices=STORES, help="copy this store only")
    p.add_argument(
        "--keep", type=int, default=None, help="sets kept per store (OFFSITE_KEEP, BACKUP_KEEP, 3)"
    )
    p.add_argument("--no-hash", action="store_true", help="skip the sha256 read-back of each copy")
    p.add_argument(
        "--dry-run", action="store_true", help="print what a run would do; write nothing"
    )
    p.add_argument("--status", action="store_true", help="the newest local and off-site sets")
    p.add_argument("--json", action="store_true", help="with --status: print JSON")
    for name, default in DEFAULTS.items():
        p.add_argument("--" + name.replace("_", "-"), type=float, default=default)
    return p


def _root(flag: Path | None, env: str, default: str) -> Path:
    return flag or Path(os.environ.get(env) or default)


def main(
    argv: list[str] | None = None,
    *,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
) -> int:
    """Run the command line. Returns the exit code.

    ``clock`` and ``sleep`` let the tests run the waits instantly.
    """
    args = _parser().parse_args(argv)
    backups_root = _root(args.backups_root, "BACKUP_ROOT", "/backups")
    offsite_root = _root(args.offsite_root, "OFFSITE_ROOT", "/offsite")
    mark_dir = _root(args.mark_dir, "INGEST_MARK_DIR", "/data/ingest_marks")
    if args.status:
        print_status(backups_root, offsite_root, args.json)
        return 0
    stores = [s for s in STORES if not args.only or s in args.only]
    keep = resolve_keep(args.keep)
    return run_copy(
        backups_root, offsite_root, mark_dir, stores, keep, args, clock=clock, sleep=sleep
    )


if __name__ == "__main__":
    sys.exit(main())
