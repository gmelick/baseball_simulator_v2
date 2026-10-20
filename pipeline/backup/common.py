"""pipeline/backup/common.py — the helpers the backup modules share (SIM-525, SIM-568).

The data-store backup (``data_stores``) and the off-site copy (``offsite``) use
them. This module is pure standard library: ``data_stores`` imports duckdb at
module level, and the off-site copy (SIM-568) must never import it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

GIB = 1024**3
CHUNK = 8 * 1024 * 1024

#: A clock returns epoch seconds. A sleep waits. The tests pass fakes of both.
Clock = Callable[[], float]
Sleep = Callable[[float], object]


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


def fsync_path(path: Path) -> None:
    """Flush a file, or a folder's entries, to the disk."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open a folder; there is nothing to flush there
    try:
        os.fsync(fd)
    except OSError:
        pass
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
    ``stale_s``. ``scripts/with_mark.sh`` writes the start epoch into the mark.
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
