"""The bundle's steal pool: whether it is whole, the rollback copy (step 4) and
the hashes that prove the export touched nothing else."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field

import duckdb

from .common import (
    EXPORT_STAGING_FILES,
    STEAL_POOL_FILES,
    TARGETS,
    _columns,
    _log,
    _npy_shape,
    _pq,
    _sha256,
    _tree_files,
)

# ---------------------------------------------------------------------------
# The bundle's steal pool
# ---------------------------------------------------------------------------


def _strays(pool_dir: str) -> list[str]:
    """The entries of a steal pool directory other than the pool's five files.

    The export's staging files (:data:`EXPORT_STAGING_FILES`) are among them
    when an export was killed: the export removes them itself only when
    Python runs its clean-up, which a blue screen or ``docker stop`` skips.
    :func:`_export_replaces` tells those apart from every other entry.
    """
    if not os.path.isdir(pool_dir):
        return []
    return sorted(set(os.listdir(pool_dir)) - STEAL_POOL_FILES)


def _export_replaces(name: str) -> bool:
    """TRUE for a stray the next export overwrites: one of its staging files."""
    return name in EXPORT_STAGING_FILES


def steal_pool_problem(pool_dir: str) -> str | None:
    """Why the steal pool in ``pool_dir`` is not whole by its OWN manifest, or None.

    Whole means: the manifest reads and names the two targets; each target's
    meta parquet and situation array hold the manifest's rows (the array with
    the manifest's columns); and the directory holds no other file but the
    export's staging files, which a killed export leaves and the loader never
    reads.
    """
    if not os.path.isdir(pool_dir):
        return f"{pool_dir} is missing"
    strays = [s for s in _strays(pool_dir) if not _export_replaces(s)]
    if strays:
        return f"it holds files no export writes: {strays}"
    mem = duckdb.connect()
    try:
        with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        counts = {str(k): int(v) for k, v in manifest["counts"].items()}
        if set(counts) != set(TARGETS):
            return f"its manifest names the targets {sorted(counts)}, not {list(TARGETS)}"
        width = len(manifest.get("sit_cols", [])) or 4
        for t in TARGETS:
            n = counts[t]
            meta = os.path.join(pool_dir, f"{t}.meta.parquet")
            rows = int(mem.execute(f"SELECT COUNT(*) FROM {_pq(meta)}").fetchone()[0])
            shape = _npy_shape(os.path.join(pool_dir, f"{t}.sit.npy"))
            if len(shape) != 2 or shape[1] != width:
                return f"its {t}.sit.npy has shape {shape}, not {n:,} x {width}"
            if rows != n or shape[0] != n:
                return (
                    f"target {t} does not hold the manifest's {n:,} rows "
                    f"(meta {rows:,}, sit {shape[0]:,})"
                )
    except Exception as exc:  # noqa: BLE001 - any read failure means the pool is not whole
        return f"it does not read whole ({type(exc).__name__}: {exc})"
    finally:
        mem.close()
    return None


def _has_classes(pool_dir: str) -> bool:
    """TRUE when the pool's meta parquet carries the pitch class (a post-0031 export)."""
    meta = os.path.join(pool_dir, f"{TARGETS[0]}.meta.parquet")
    if not os.path.isfile(meta):
        return False
    mem = duckdb.connect()
    try:
        return "pitch_class" in _columns(mem, meta)
    except Exception:  # noqa: BLE001 - an unreadable file carries nothing
        return False
    finally:
        mem.close()


def _same_files(a_dir: str, b_dir: str) -> bool:
    a, b = _tree_files(a_dir), _tree_files(b_dir)
    if a != b:
        return False
    return all(_sha256(os.path.join(a_dir, rel)) == _sha256(os.path.join(b_dir, rel)) for rel in a)


@dataclass
class BackupPlan:
    """What step 4 does with the copy: keep it, copy the bundle's steal pool
    to it, or rename a copy aside and copy again (``--force-backup``)."""

    action: str  # "keep" | "copy" | "replace"
    problems: list[str] = field(default_factory=list)
    note: str = ""


def export_marker(backup_dir: str) -> str:
    """The file that says this rebuild started an export after taking the copy.

    It sits beside the copy, outside the bundle. Step 5 writes it before the
    export and step 6 removes it after a clean round-trip, so a re-run after
    a failed export keeps the copy even when the export left the bundle's
    pool half-written.
    """
    return backup_dir + ".export_started"


def backup_plan(art_dir: str, backup_dir: str, force: bool) -> BackupPlan:
    """Decide the rollback point, with no write.

    A whole copy is KEPT when the bundle's steal pool is byte for byte the
    copy, when the bundle's pool carries the class (this rebuild exported it
    before), or when the export marker says an export of this rebuild
    started (:func:`export_marker`). A whole copy the bundle has moved on
    from (the bundle's pool differs, carries no class, and no export of this
    rebuild started: a later export of the old shape) is stale. A stale or
    partial copy stops the run, unless ``--force-backup`` renames it aside;
    the bundle's own pool must then be whole.
    """
    bundle_pool = os.path.join(art_dir, "steal_pool")
    if not os.path.exists(backup_dir):
        why = steal_pool_problem(bundle_pool)
        if why is not None:
            return BackupPlan(
                "copy",
                [
                    f"the bundle's steal pool is not whole ({why}), so it cannot become the "
                    "rollback point, and no copy exists"
                ],
            )
        return BackupPlan("copy", note="the bundle's steal pool is whole: the run copies it")
    why_copy = steal_pool_problem(backup_dir)
    if why_copy is None:
        if (
            os.path.exists(export_marker(backup_dir))
            or _same_files(bundle_pool, backup_dir)
            or _has_classes(bundle_pool)
        ):
            return BackupPlan(
                "keep",
                note=f"the copy {backup_dir} is whole: the run keeps it as the rollback point",
            )
        why_copy = "it is whole, but the bundle's steal pool changed after the copy"
    if not force:
        return BackupPlan(
            "replace",
            [
                f"the copy {backup_dir} cannot be the rollback point ({why_copy}). Pass "
                "--force-backup to rename it aside (never deleted) and copy again"
            ],
        )
    why = steal_pool_problem(bundle_pool)
    if why is not None:
        return BackupPlan(
            "replace",
            [
                f"the copy {backup_dir} cannot be the rollback point ({why_copy}), and the "
                f"bundle's steal pool cannot replace it: it is not whole ({why})"
            ],
        )
    return BackupPlan(
        "replace",
        note=f"--force-backup renames {backup_dir} aside ({why_copy}) and copies again",
    )


def take_backup(art_dir: str, backup_dir: str, force: bool) -> str:
    """Step 4: carry out :func:`backup_plan`. Returns the rollback point."""
    plan = backup_plan(art_dir, backup_dir, force)
    if plan.problems:
        raise RuntimeError("; ".join(plan.problems))
    if plan.action == "keep":
        _log(f"  kept {backup_dir} as the rollback point (whole; never copied over)")
        return backup_dir
    if plan.action == "replace":
        aside = f"{backup_dir}.{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(backup_dir, aside)
        _log(f"  moved {backup_dir} to {aside} (not deleted)")
    shutil.copytree(os.path.join(art_dir, "steal_pool"), backup_dir)
    why = steal_pool_problem(backup_dir)
    if why is not None:
        raise RuntimeError(f"the fresh copy {backup_dir} is not whole ({why})")
    if not _same_files(os.path.join(art_dir, "steal_pool"), backup_dir):
        raise RuntimeError(f"the fresh copy {backup_dir} differs from the bundle's steal pool")
    _log(f"  copied the bundle's steal pool to {backup_dir} (whole, byte for byte)")
    return backup_dir


def bundle_hashes(art_dir: str) -> dict[str, str]:
    """The SHA-256 of every bundle file OUTSIDE ``steal_pool/``. The export
    writes the steal pool only; the round-trip proves the rest unchanged."""
    return {
        rel: _sha256(os.path.join(art_dir, rel))
        for rel in sorted(_tree_files(art_dir))
        if not rel.startswith("steal_pool/")
    }


def compare_hashes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The differences between two :func:`bundle_hashes` reads (empty = identical)."""
    out = [f"only before: {rel}" for rel in sorted(set(before) - set(after))]
    out += [f"only after: {rel}" for rel in sorted(set(after) - set(before))]
    out += [
        f"changed: {rel}" for rel in sorted(set(before) & set(after)) if before[rel] != after[rel]
    ]
    return out
