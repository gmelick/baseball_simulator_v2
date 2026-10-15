"""pipeline/backup/status.py — which backup sets exist, and how old they are (SIM-525).

The backup root holds one folder per store (``postgres``, ``duckdb``,
``bundle``). Each store folder holds one folder per SET, named by the UTC time
the run started (``%Y%m%dT%H%M%SZ``). A set is COMPLETE only when its
``manifest.json`` exists: the backup writes the manifest last. A folder that
ends in ``.partial`` is a set in progress, or one whose run died.

This module is pure standard library. The API imports it for the backup-age
gauge, so it must never import duckdb.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: The three stores, in the order the reports list them.
STORES = ("postgres", "duckdb", "bundle")

#: The name of the file that marks a set as complete.
MANIFEST = "manifest.json"

#: The suffix of a set in progress.
PARTIAL_SUFFIX = ".partial"

#: The format of a set id (the UTC start time of the run).
SET_ID_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class SetInfo:
    """One complete backup set of one store."""

    store: str
    set_id: str
    path: Path
    created_at: datetime
    bytes: int


def parse_set_id(set_id: str) -> datetime | None:
    """The UTC time a set id names, or None when the name is not a set id."""
    try:
        return datetime.strptime(set_id, SET_ID_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_set(store: str, path: Path) -> SetInfo | None:
    manifest_path = path / MANIFEST
    if not manifest_path.is_file():
        return None
    manifest: dict[str, object] = {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded
    except (OSError, ValueError):
        manifest = {}
    created_at = _parse_created_at(manifest.get("created_at")) or parse_set_id(path.name)
    if created_at is None:
        return None
    size = manifest.get("bytes")
    return SetInfo(
        store=store,
        set_id=path.name,
        path=path,
        created_at=created_at,
        bytes=int(size) if isinstance(size, int | float) else 0,
    )


def complete_sets(root: Path, store: str) -> list[SetInfo]:
    """Every complete set of ``store`` under ``root``, the oldest first."""
    store_dir = Path(root) / store
    try:
        entries = list(store_dir.iterdir())
    except OSError:
        return []
    sets: list[SetInfo] = []
    for entry in entries:
        if entry.name.endswith(PARTIAL_SUFFIX) or not entry.is_dir():
            continue
        info = _read_set(store, entry)
        if info is not None:
            sets.append(info)
    sets.sort(key=lambda s: (s.created_at, s.set_id))
    return sets


def newest_complete_set(root: Path, store: str) -> SetInfo | None:
    """The newest complete set of ``store``, or None when there is none."""
    sets = complete_sets(root, store)
    return sets[-1] if sets else None


def newest_complete_sets(root: Path) -> dict[str, SetInfo | None]:
    """The newest complete set of every store (None for a store with none)."""
    return {store: newest_complete_set(root, store) for store in STORES}


def age_hours(info: SetInfo | None, now: datetime) -> float:
    """Hours from the set's creation to ``now``; -1.0 when there is no set."""
    if info is None:
        return -1.0
    return (now - info.created_at).total_seconds() / 3600.0
