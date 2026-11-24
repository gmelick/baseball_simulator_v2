"""operations/backup/common.py — the helpers the two backup jobs share.

The data-store backup and the off-site copy use them. The module is pure
standard library, because the off-site copy must never import duckdb.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

GIB = 1024**3
CHUNK = 8 * 1024 * 1024

#: A clock returns epoch seconds; a sleep waits. The tests pass fakes of both.
Clock = Callable[[], float]
Sleep = Callable[[float], object]


def iso_utc(dt: datetime) -> str:
    """Return a time as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def root_path(flag: Path | None, env: str, default: str) -> Path:
    """Return the flag's path, else the variable ``env``, else ``default``."""
    return flag or Path(os.environ.get(env) or default)


def env_float(name: str, default: float) -> float:
    """Return the variable ``name`` as a float, or ``default`` when unset or bad."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def human_bytes(n: int) -> str:
    """Return a size as text, in the largest unit up to GiB that keeps it at 1 or more."""
    size = float(n)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            break
    return f"{size:.1f} {unit}"


def sha256_file(path: Path) -> str:
    """Return a file's sha256, read in chunks because the DuckDB file is gigabytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_fd(fd: int) -> None:
    """Flush an open file to the disk.

    A file system that cannot fsync must not stop a backup, so the error is
    ignored. The read-back hash checks the copy.
    """
    try:
        os.fsync(fd)
    except OSError:
        pass


def fsync_path(path: Path) -> None:
    """Flush a file, or a folder's entries, to the disk."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open a folder; there is nothing to flush there
    try:
        fsync_fd(fd)
    finally:
        os.close(fd)


def copy_file_synced(src: Path, dest: Path) -> None:
    """Copy one file and flush the copy to the disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    fsync_path(dest)


def fresh_marks(mark_dir: Path, names: Iterable[str], stale_s: float, now: float) -> list[str]:
    """Return the jobs among ``names`` that run now.

    A job runs while its ``<name>.running`` mark (written by
    ``operations/jobs/with_mark.sh``) holds a start epoch younger than
    ``stale_s``. An older mark is one a dead job left.
    """
    fresh: list[str] = []
    for name in names:
        path = mark_dir / f"{name}.running"
        try:
            started = float(path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            continue
        if now - started < stale_s:
            fresh.append(name)
    return fresh


def wait_while_busy(
    busy: Callable[[float], list[str]],
    *,
    grace_s: float,
    wait_s: float,
    poll_s: float,
    clock: Clock,
    sleep: Sleep,
    log: Callable[[str], None],
) -> bool:
    """Wait while ``busy(now)`` names a running job. Return False on a timeout.

    The wait starts with ``grace_s``, so a job fired in the same second has
    time to write its mark.
    """
    if grace_s > 0:
        sleep(grace_s)
    deadline = clock() + wait_s
    while True:
        running = busy(clock())
        if not running:
            return True
        if clock() >= deadline:
            log(f"{', '.join(running)} still runs after {wait_s:.0f} s; the wait gives up")
            return False
        log(f"{', '.join(running)} runs; waiting {poll_s:.0f} s")
        sleep(poll_s)


def prune_victims(sets: list[tuple[datetime, str]], keep: int, protect: set[str]) -> list[str]:
    """Return the set ids to prune: all but the newest ``keep``, never one in ``protect``."""
    ordered = sorted(sets)
    keep_ids = {set_id for _, set_id in ordered[-keep:]} if keep > 0 else set()
    return [set_id for _, set_id in ordered if set_id not in keep_ids and set_id not in protect]
