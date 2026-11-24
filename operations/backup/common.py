"""operations/backup/common.py — the helpers the backup modules share.

The data-store backup (``back_up_data_stores``) and the off-site copy
(``copy_offsite``) use them. This module is pure standard library:
``back_up_data_stores`` imports duckdb at module level, and the off-site copy must
never import it.
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

#: A clock returns epoch seconds. A sleep waits. The tests pass fakes of both.
Clock = Callable[[], float]
Sleep = Callable[[float], object]


def iso_utc(dt: datetime) -> str:
    """A time as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def root_path(flag: Path | None, env: str, default: str) -> Path:
    """The flag's path; else the environment variable ``env``; else ``default``."""
    return flag or Path(os.environ.get(env) or default)


def env_float(name: str, default: float) -> float:
    """The environment variable ``name`` as a float; ``default`` when it is unset
    or does not parse."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def human_bytes(n: int) -> str:
    """A size in the largest unit that keeps the number at 1 or more (up to GiB)."""
    size = float(n)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            break
    return f"{size:.1f} {unit}"


def sha256_file(path: Path) -> str:
    """The sha256 of a file, read in chunks (the DuckDB file is 3.6 GB)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_fd(fd: int) -> None:
    """Flush an open file to the disk; a file system that cannot fsync is skipped."""
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
    """The jobs among ``names`` that run now.

    A job runs while its ``<name>.running`` mark exists and is younger than
    ``stale_s``. ``operations/jobs/with_mark.sh`` writes the start epoch into the mark.
    An older mark was left by a job that died.
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
    """Wait while ``busy(now)`` names a running job. False on a timeout.

    The wait starts with ``grace_s``, so a job fired in the same second has
    time to write its mark. ``clock`` returns epoch seconds.
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
    """The set ids to prune: all but the newest ``keep``, never one in ``protect``."""
    ordered = sorted(sets)
    keep_ids = {set_id for _, set_id in ordered[-keep:]} if keep > 0 else set()
    return [set_id for _, set_id in ordered if set_id not in keep_ids and set_id not in protect]
