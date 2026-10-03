"""
scripts/sim554_rebuild_steal_pool.py — the SIM-554 steal-pool rebuild, end to end.

WHAT IT DOES
============
The running game on the pitch (SIM-554) needs two new columns on the steal
opportunity pool (``sim.steal_opportunity_pool``, migration 0031): the class
of the pitch each row rode (``pitch_class``) and the mark of a pickoff row
(``is_pickoff_row``). A pickoff row is a pickoff outcome thrown before any
pitch of its pair in the plate appearance; the old builder dropped those
(22% of the real pickoff outcomes that fit a pair). The builder ``sim554.1``
writes both. This script rebuilds the pool for the window seasons on that
builder, proves the rebuild changed only what the design says it changes,
exports the steal pool ONLY into the engine-artifact bundle, and proves the
export changed nothing else. The design is
``docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md`` §5.8 and
§8 step 3.

  0. Pre-flight, with no write: the builder version; migration 0031 applied,
     or ``--apply-migration`` passed (else the run stops); the window's
     pitch pool present per season and built by sim553.1 or sim554.1 (the
     class comes from it); the steal pool's build watermark equal to
     ``raw.pitches`` today, per season (else the rebuild would add pitch
     rows, which the checks forbid); the bundle's steal pool whole (every
     file holds its manifest's rows; no stray file but the export's own
     staging files, which a killed export leaves); the backup target.
     Then open DuckDB writable. A held lock stops the run (exit 3).
  1. Snapshot every pool row's ``(pitch_id, attempted, success, pickoff_out,
     pickoff_advancing, pickoff_error)`` into a TEMP table. Count, from the
     play records (``pg.raw.play_events``) and the snapshot's pitch rows and
     NOT from the builder's SQL, the pickoff rows the rebuild must write.
  2. Rebuild the pool for the seasons inside ONE transaction. With
     ``--apply-migration`` the transaction first applies migration 0031,
     AFTER the snapshot, so a failed check or a raise rolls the two columns
     back with the rows (master's pool builder fails on a migrated table).
  3. Check, still inside the transaction:
       a. every snapshot row is back with the same six values;
       b. every new row is a pickoff row (``pitch_id < 0`` and
          ``is_pickoff_row``);
       c. the pickoff rows are exactly the counted ones, one per outcome, each
          with the outcome's runner, thrower, pair, outs, labels and a 0-0
          count, per season;
       d. tagged rows + pickoff rows equal the outcomes that fit a pair, less
          the listed ones (an outcome with no runner or pitcher id, a second
          outcome folded into one tag);
       e. ``pitch_class`` is set on every pitch row and NULL on every pickoff
          row, and equals the pitch pool's class for the same pitch, so the
          class shares per (target, outs, balls, strikes) equal the pitch
          pool's;
       f. no attempted row rides a ball in play or a hit by pitch;
       g. every pickoff row has a game date, a runner and a pitcher;
       h. ``sim.pool_build_metadata`` names the builder for every season.
     A failed check rolls the transaction back and proves the table matches
     the snapshot (exit 2). Then COMMIT.
  4. Copy the bundle's ``steal_pool/`` to a sibling OUTSIDE the bundle
     (``<bundle>.pre_sim554_steal_pool``), or keep an existing whole copy as
     the rollback point. The copy is never deleted.
  5. Export the steal pool ONLY (``build_steal_pool_artifact``). A marker
     file beside the copy (``<copy>.export_started``) says the export began,
     so a re-run after a failed export keeps the copy even when the bundle's
     pool is half-written. A clean round-trip removes the marker.
  6. The round-trip: the exported pool is whole by its manifest; per
     ``pitch_id`` the rows, the six labels, the class and the count match
     DuckDB; every file outside ``steal_pool/`` is byte-identical to before
     (hashed before the rebuild and after the export). A difference fails
     the run (exit 4).
  7. Print the steal band centres the artifact pool gives over PITCH rows
     beside ``tests/acceptance/bands.py``'s, the pickoff outcomes per pitch
     row before and after, the time, the rollback and the next steps.

``--check-only`` writes nothing: it runs the pre-flight and prints the
pickoff rows the rebuild would add. ``--seasons`` defaults to the window and
must hold it. ``--force-backup`` renames aside (never deletes) a copy that
cannot be the rollback point, then copies the bundle's steal pool again.
``--duckdb-path`` and ``--art-dir`` point the run at another database and
bundle.

RUN
===
The run book is ONE command, with ``--apply-migration``. Do NOT apply
migration 0031 separately: the rebuild applies it inside its own
transaction, and a failed rebuild can roll back only columns it added
itself (the run then warns that the columns stay).

The app must be STOPPED: its worker server holds the DuckDB writer lock
(SIM-524). Run docker compose from the MAIN checkout (a compose run from the
worktree is a separate project with empty volumes), with the worktree's code
mounted over the image's. In Git Bash:

    W=/c/Users/grego/Documents/baseball_simulator_v2/.claude/worktrees/sim554
    docker compose stop app
    MSYS_NO_PATHCONV=1 docker compose run -d --rm \\
        -v "$W/scripts:/app/scripts" -v "$W/pipeline:/app/pipeline" \\
        -v "$W/simulation:/app/simulation" -v "$W/db:/app/db" \\
        app python scripts/sim554_rebuild_steal_pool.py --apply-migration
    docker logs -f <the run container>      # read to the COMPLETE / FAILED line
    docker compose up -d app

EXIT CODES
==========
  0  complete.
  1  an unexpected error before the export. The rebuild is undone and the
     bundle is untouched.
  2  a pre-flight or pool check failed, or the bundle copy failed. Nothing
     was exported. The log says whether the DuckDB table was undone.
  3  the DuckDB file is locked: the app, or another reader, is running.
  4  the export or its round-trip failed. The bundle needs the rollback the
     log prints (or a re-run: it keeps the copy).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402

import pipeline.batch.player_profile_computor as ppc  # noqa: E402
from pipeline.batch.engine_artifacts import (  # noqa: E402
    RECENCY_FLOOR_SEASONS,
    STEAL_PITCH_CLASS_CODE,
    STEAL_PITCH_CLASSES,
    build_steal_pool_artifact,
    last_n_seasons,
)
from pipeline.batch.pool_chain import attach_pg  # noqa: E402

#: The builder this rebuild is for. The script refuses to run on any other.
EXPECTED_BUILDER = "sim554.1"
#: The pitch pool builders whose class the steal pool may copy: the SIM-553
#: coding, and this ticket's version bump (the same pitch-pool coding).
PITCH_POOL_BUILDERS: tuple[str, ...] = ("sim553.1", "sim554.1")

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
#: The bundle the app loads at boot: ``$BASEBALL_PLAY_POOL_DIR/engine_artifacts``
#: (``simulation/production_factory.py``), unless the operator names another.
ART_DIR = os.environ.get("BASEBALL_ENGINE_ARTIFACT_DIR") or os.path.join(
    os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool"), "engine_artifacts"
)
#: The copy of the bundle's steal pool, a sibling OUTSIDE the bundle: a
#: directory inside it would change every later comparison of the bundle.
BACKUP_SUFFIX = ".pre_sim554_steal_pool"
MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0031_sim554_steal_pool_pitch_class.sql"
NEW_COLUMNS: tuple[str, ...] = ("pitch_class", "is_pickoff_row")

TARGETS: tuple[str, ...] = ("2", "3")
#: The files a whole steal pool holds, and no other.
STEAL_POOL_FILES: frozenset[str] = frozenset(
    {"manifest.json"} | {f"{t}.{p}" for t in TARGETS for p in ("meta.parquet", "sit.npy")}
)
#: The staging files of the export (``build_steal_pool_artifact``): it writes
#: each target's two files as ``tmp_<target>.*`` and moves them into place at
#: the end. DuckDB's COPY over an existing file writes ``tmp_<name>`` first, so
#: a COPY over a left-over staging file writes ``tmp_tmp_<target>.meta.parquet``.
#: A killed export leaves any of them; the next export overwrites them (and
#: moves its own into place), and the loader reads the pool's files by name.
EXPORT_STAGING_FILES: frozenset[str] = frozenset(
    {f"tmp_{t}.{p}" for t in TARGETS for p in ("meta.parquet", "sit.npy")}
    | {f"tmp_tmp_{t}.meta.parquet" for t in TARGETS}
)
#: The five labels the rebuild must keep on every pitch row (with pitch_id,
#: the six values of the snapshot).
LABELS: tuple[str, ...] = (
    "attempted",
    "success",
    "pickoff_out",
    "pickoff_advancing",
    "pickoff_error",
)
#: The classes on which no real runner ever earns an attempt (design §2.1).
DEAD_BALL_CLASSES: tuple[str, ...] = ("in_play", "hit_by_pitch")
#: Measured 2026-09-30 (design §2.8, §4.6): the pickoff rows the rebuild adds
#: in the pool's games, and the outcomes that fit a pair. Printed beside the
#: counted figures; the per-row check c is the gate, not these.
MEASURED_PICKOFF_ROWS: dict[int, int] = {2023: 91, 2024: 108, 2025: 95, 2026: 100}
MEASURED_FIT = 1_788
#: The steal band centres, when ``tests/acceptance/bands.py`` does not load.
FALLBACK_CENTRES: dict[str, float] = {
    "STEAL_ATT_OPP_2B": 0.0214,
    "STEAL_ATT_OPP_3B": 0.0044,
    "STEAL_SAFE_2B": 0.7982,
}

SNAP = "_sim554_before"
EXPECT = "_sim554_expect"
POOL = "sim.steal_opportunity_pool"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STOPPED = 2
EXIT_LOCKED = 3
EXIT_BUNDLE = 4


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _ok(good: bool) -> str:
    return "OK" if good else "MISMATCH"


def _season_list(seasons: list[int]) -> str:
    return ", ".join(str(int(s)) for s in seasons)


def _builder() -> str:
    """The builder the computor writes. Read at call time, so a test can set it."""
    return str(ppc.POOL_BUILDER_VERSION)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sql_path(path: str) -> str:
    """A file path as DuckDB reads it in SQL (forward slashes on every host)."""
    return str(path).replace("\\", "/")


def _pq(path: str) -> str:
    return f"read_parquet('{_sql_path(path)}')"


def _npy_shape(path: str) -> tuple[int, ...]:
    """The shape of a ``.npy`` file. The memory map fails on a short file."""
    arr = np.load(path, mmap_mode="r")
    shape = tuple(int(d) for d in arr.shape)
    del arr  # release the map (Windows holds the file while a map is open)
    return shape


def _columns(con: Any, path: str) -> list[str]:
    return [str(r[0]) for r in con.execute(f"DESCRIBE SELECT * FROM {_pq(path)}").fetchall()]


def _table_columns(con: Any, schema: str, table: str) -> set[str]:
    """The columns of a table of this DuckDB file (not of the attached Postgres)."""
    return {
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchall()
    }


def _tree_files(root: str) -> dict[str, int]:
    """Every file under ``root``: its path relative to ``root`` (``/``-separated) -> its size."""
    out: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            out[os.path.relpath(path, root).replace(os.sep, "/")] = os.path.getsize(path)
    return out


# ---------------------------------------------------------------------------
# The migration (sim532's statement splitter)
# ---------------------------------------------------------------------------


def migration_statements(text: str) -> list[str]:
    """The statements of a migration file. Drop the comments first (a whole
    comment line, and the tail of a line after ``--``), then split on ';'."""
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def missing_columns(con: Any) -> list[str]:
    """The migration-0031 columns the steal pool does not have yet."""
    have = _table_columns(con, "sim", "steal_opportunity_pool")
    return [c for c in NEW_COLUMNS if c not in have]


def apply_migration(con: Any, migration: Path = MIGRATION) -> int:
    """Apply migration 0031 (idempotent: ADD COLUMN IF NOT EXISTS). Returns the
    statement count."""
    statements = migration_statements(migration.read_text(encoding="utf-8"))
    for stmt in statements:
        con.execute(stmt)
    return len(statements)


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


# ---------------------------------------------------------------------------
# Pre-flight (no write)
# ---------------------------------------------------------------------------


def _steal_counts(con: Any, seasons: list[int]) -> dict[int, int]:
    return {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def _steal_versions(con: Any, seasons: list[int]) -> dict[int, str | None]:
    return {
        int(s): v
        for s, v in con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }


def pitch_pool_problems(con: Any, seasons: list[int]) -> list[str]:
    """The window's pitch pool must be present per season and built by a
    builder whose class the steal pool may copy."""
    sl = _season_list(seasons)
    rows = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM sim.pitch_pool WHERE season IN ({sl}) GROUP BY 1"
        ).fetchall()
    }
    builders = {
        int(s): v
        for s, v in con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'pitch_pool' AND season IN ({sl})"
        ).fetchall()
    }
    problems: list[str] = []
    for s in seasons:
        good = rows.get(s, 0) > 0 and builders.get(s) in PITCH_POOL_BUILDERS
        _log(
            f"  pitch pool {s}: {rows.get(s, 0):,} rows, builder {builders.get(s)}  "
            f"{'OK' if good else 'STOP'}"
        )
        if not good:
            problems.append(
                f"{s}: the pitch pool holds {rows.get(s, 0):,} rows built by "
                f"{builders.get(s)!r}; the steal pool's class comes from a pitch pool built by "
                f"{' or '.join(PITCH_POOL_BUILDERS)}. Rebuild the pitch pool first"
            )
    return problems


def watermark_problems(con: Any, seasons: list[int]) -> list[str]:
    """The steal pool's build watermark must equal ``raw.pitches`` today, per
    season. Otherwise the rebuild adds or drops pitch rows, and check b
    (every new row is a pickoff row) stops it after the work."""
    meta = {
        int(s): (d, n)
        for s, d, n in con.execute(
            "SELECT season, source_max_game_date, source_row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    problems: list[str] = []
    for s in seasons:
        src_max, src_n = ppc._source_state(con, s)
        prev = meta.get(s)
        good = prev is not None and prev[0] == src_max and prev[1] == src_n
        _log(
            f"  steal pool {s}: built from raw.pitches through {prev[0] if prev else None} "
            f"({(prev[1] or 0) if prev else 0:,} rows); raw.pitches now through {src_max} "
            f"({int(src_n or 0):,} rows)  {'OK' if good else 'STOP'}"
        )
        if not good:
            problems.append(
                f"{s}: raw.pitches changed since the steal pool's last build (or the season has "
                "no build record), so the rebuild would add pitch rows the checks forbid. Run the "
                "pool chain for the season first"
            )
    return problems


def bundle_problems(art_dir: str, backup_dir: str, window: list[int], force: bool) -> list[str]:
    """The bundle's steal pool and the rollback point.

    The bundle's pool must be present and writable, with no stray file the
    export does not replace. It must be whole, unless a whole copy is kept as
    the rollback point: then it may be half-written by an earlier run of this
    rebuild, and the export rewrites it. The rollback point must hold the
    window's seasons. The copy's parent must be writable, with room for it.
    """
    pool_dir = os.path.join(art_dir, "steal_pool")
    if not os.path.isdir(pool_dir):
        return [f"the bundle has no steal pool: {pool_dir} is missing"]
    plan = backup_plan(art_dir, backup_dir, force)
    if plan.note:
        _log(f"  backup: {plan.note}")
    problems = list(plan.problems)
    kept = [s for s in _strays(pool_dir) if not _export_replaces(s)]
    if kept:
        problems.append(
            f"the bundle's steal_pool/ holds {kept}, which the export neither writes nor "
            "replaces. Move them out of the bundle first"
        )
    why = steal_pool_problem(pool_dir)
    if why is None:
        _log("  the bundle's steal pool is whole by its own manifest")
    elif plan.action == "keep" and not kept:
        _log(f"  the bundle's steal pool is not whole ({why}); the export rewrites it")
    elif not plan.problems:
        problems.append(f"the bundle's steal pool is not whole ({why}), and no copy is kept")
    source = backup_dir if plan.action == "keep" else pool_dir
    try:
        with open(os.path.join(source, "manifest.json"), encoding="utf-8") as fh:
            seasons = [int(s) for s in json.load(fh).get("seasons", [])]
        if seasons != window:
            problems.append(
                f"{source} holds the seasons {seasons}; the export writes {window}, the same window"
            )
    except (OSError, ValueError) as exc:
        if not plan.problems:
            problems.append(f"{source}/manifest.json does not read ({exc})")
    if not os.access(pool_dir, os.W_OK):
        problems.append(f"no write access to {pool_dir}")
    for name in sorted(os.listdir(pool_dir)):
        p = os.path.join(pool_dir, name)
        if os.path.isfile(p) and not os.access(p, os.W_OK):
            problems.append(f"no write access to {p}")
    parent = os.path.dirname(os.path.abspath(backup_dir))
    if not os.access(parent, os.W_OK):
        problems.append(f"no write access to {parent} (the copy goes there)")
    elif plan.action != "keep":
        size = sum(_tree_files(pool_dir).values())
        free = shutil.disk_usage(parent).free
        if free < 2 * size:
            problems.append(
                f"{free / 1e9:.2f} GB free under {parent}; the copy needs {size / 1e9:.2f} GB"
            )
    return problems


def preflight(
    con: Any,
    seasons: list[int],
    window: list[int],
    art_dir: str,
    backup_dir: str,
    *,
    force: bool,
    apply_migration_flag: bool,
) -> list[str]:
    """Every reason the write path must not start. An empty list means go.

    A missing migration is a problem only without ``--apply-migration``; with
    it, the write path applies the file INSIDE the rebuild's transaction,
    after the snapshot (step 2), so a failed rebuild rolls the two columns
    back with the rows. Never apply 0031 separately: a failed rebuild cannot
    remove columns that were on the table before it began.
    """
    problems: list[str] = []
    if _builder() != EXPECTED_BUILDER:
        problems.append(
            f"POOL_BUILDER_VERSION is {_builder()!r}; this script rebuilds for {EXPECTED_BUILDER!r}"
        )
    _log(f"  window (the {RECENCY_FLOOR_SEASONS} newest pool seasons, the export's): {window}")
    left_out = [s for s in window if s not in seasons]
    if left_out:
        problems.append(
            f"the seasons {seasons} leave out the window seasons {left_out}; the export writes "
            "the whole window, on one builder"
        )
    missing = missing_columns(con)
    if missing and apply_migration_flag:
        _log(
            f"  migration 0031: the pool lacks {missing}; --apply-migration applies it inside "
            "the rebuild's transaction (step 2)"
        )
        if not MIGRATION.is_file():
            problems.append(f"{MIGRATION} is missing; it adds {missing}")
    elif missing:
        problems.append(
            f"migration 0031 is not applied (sim.steal_opportunity_pool lacks {missing}). Pass "
            "--apply-migration: the rebuild applies it inside its transaction, so a failed "
            "rebuild rolls the columns back. Do not apply 0031 separately"
        )
    else:
        _log(
            "  migration 0031: applied before this run (pitch_class and is_pickoff_row are on "
            "the pool); a failed rebuild keeps these columns"
        )
    problems += pitch_pool_problems(con, seasons)
    problems += watermark_problems(con, seasons)
    problems += bundle_problems(art_dir, backup_dir, window, force)
    return problems


# ---------------------------------------------------------------------------
# The snapshot and the counted pickoff rows
# ---------------------------------------------------------------------------


def take_snapshot(con: Any, seasons: list[int]) -> None:
    """Every pool row's six values (and its pair) into a TEMP table. NULL
    labels read FALSE, the export's own reading."""
    has_mark = "is_pickoff_row" in _table_columns(con, "sim", "steal_opportunity_pool")
    mark = "COALESCE(is_pickoff_row, FALSE)" if has_mark else "FALSE"
    labels = ", ".join(f"COALESCE({c}, FALSE) AS {c}" for c in LABELS)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {SNAP} AS "
        f"SELECT pitch_id, season, game_pk, at_bat_number, target_base, {labels}, "
        f"{mark} AS is_pickoff_row "
        f"FROM {POOL} WHERE season IN ({_season_list(seasons)})"
    )


def count_pickoff_rows(con: Any, seasons: list[int]) -> None:
    """Count, into a TEMP table, every pickoff outcome of the play records
    that fits a pair, and whether the snapshot holds a pitch of its pair.

    This restates the design's rule (§1.3, §4.2) in its own SQL; it does NOT
    run the builder's. An outcome is a pickoff out or an errant pickoff throw.
    It fits the target-2 pair when first base is occupied, second is open and
    the throw went to first or second; the target-3 pair when second is
    occupied, third is open and the throw went to second or third. An out at
    the next base (second for the target-2 pair, third for the target-3 pair)
    is a picked-off caught stealing. A pitch of the pair is a snapshot row
    with ``pitch_id > 0`` in the same plate appearance and pair.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {EXPECT} AS
        WITH ev AS (
            SELECT id, game_pk, at_bat_number, game_date, season, base, outs_before,
                   bat_score, fld_score, runner_id, pitcher_id, event_type,
                   (event_type = 'pickoff' AND COALESCE(is_out, FALSE)) AS is_out,
                   (event_type = 'pickoff_error') AS is_err,
                   CASE
                       WHEN (runners_state & 1) = 1 AND (runners_state & 2) = 0
                            AND base IN (1, 2) THEN 2
                       WHEN (runners_state & 2) = 2 AND (runners_state & 4) = 0
                            AND base IN (2, 3) THEN 3
                   END AS pair
            FROM pg.raw.play_events
            WHERE event_type IN ('pickoff', 'pickoff_error')
              AND season IN ({_season_list(seasons)})
        ),
        pitched AS (
            SELECT DISTINCT game_pk, at_bat_number, target_base
            FROM {SNAP} WHERE pitch_id > 0
        )
        SELECT ev.*,
               (ev.is_out AND ev.base = ev.pair) AS is_adv,
               (p.game_pk IS NOT NULL) AS has_pitch,
               (ev.runner_id IS NOT NULL AND ev.pitcher_id IS NOT NULL) AS has_ids
        FROM ev
        LEFT JOIN pitched p
          ON p.game_pk = ev.game_pk AND p.at_bat_number = ev.at_bat_number
         AND p.target_base = ev.pair
        WHERE ev.pair IS NOT NULL AND (ev.is_out OR ev.is_err)
        """
    )


@dataclass
class PickoffCount:
    """The counted pickoff outcomes of one season (:func:`pickoff_summary`)."""

    fit: int = 0  # outcomes that fit a pair
    with_pitch: int = 0  # ... whose pair has a pitch in the plate appearance
    tag_groups: int = 0  # ... distinct (plate appearance, pair): one tag each
    rows: int = 0  # outcomes with no pitch of the pair, a runner and a thrower
    no_id: list[tuple[Any, ...]] = field(default_factory=list)  # the listed ones

    @property
    def folded(self) -> int:
        """Outcomes that share a tag with another in the same plate appearance."""
        return self.with_pitch - self.tag_groups


def pickoff_summary(con: Any, seasons: list[int]) -> dict[int, PickoffCount]:
    """Per season, from the counted table (:func:`count_pickoff_rows`)."""
    out = {int(s): PickoffCount() for s in seasons}
    for s, fit, wp, groups, rows in con.execute(
        f"""
        SELECT season, COUNT(*),
               COUNT(*) FILTER (WHERE has_pitch),
               COUNT(DISTINCT CASE WHEN has_pitch
                     THEN CAST(game_pk AS VARCHAR) || ':' || CAST(at_bat_number AS VARCHAR)
                          || ':' || CAST(pair AS VARCHAR) END),
               COUNT(*) FILTER (WHERE NOT has_pitch AND has_ids)
        FROM {EXPECT} GROUP BY 1
        """
    ).fetchall():
        c = out.setdefault(int(s), PickoffCount())
        c.fit, c.with_pitch, c.tag_groups, c.rows = int(fit), int(wp), int(groups), int(rows)
    for row in con.execute(
        f"SELECT season, id, game_pk, at_bat_number, event_type, runner_id, pitcher_id "
        f"FROM {EXPECT} WHERE NOT has_pitch AND NOT has_ids ORDER BY season, id"
    ).fetchall():
        out.setdefault(int(row[0]), PickoffCount()).no_id.append(tuple(row[1:]))
    return out


def log_pickoff_summary(counts: dict[int, PickoffCount]) -> None:
    for s in sorted(counts):
        c = counts[s]
        measured = MEASURED_PICKOFF_ROWS.get(s)
        _log(
            f"  {s}: outcomes that fit a pair {c.fit:,}; with a pitch of the pair {c.with_pitch:,} "
            f"({c.tag_groups:,} tags, {c.folded:,} folded into another's tag); pickoff rows to "
            f"write {c.rows:,}" + (f" (measured 2026-09-30: {measured})" if measured else "")
        )
        for item in c.no_id:
            _log(f"    listed, no runner or pitcher id (no row possible): {item}")
    total = sum(c.fit for c in counts.values())
    _log(
        f"  outcomes that fit a pair, all seasons: {total:,} (measured 2026-09-30: {MEASURED_FIT:,})"
    )


# ---------------------------------------------------------------------------
# The checks (inside the rebuild's transaction)
# ---------------------------------------------------------------------------


def _examples(con: Any, sql: str) -> str:
    rows = con.execute(sql + " LIMIT 5").fetchall()
    return f" e.g. {rows}" if rows else ""


def check_snapshot_back(con: Any) -> bool:
    """Check a: every snapshot PITCH row is back with the same six values.

    A pickoff row of an earlier run is keyed by ``-raw.play_events.id``, and a
    game reload gives its play records new ids. So the earlier run's pickoff
    rows are reported for information only; check c proves every pickoff row
    of this build against today's play records, and check b proves every new
    row is a pickoff row."""
    diffs = " OR ".join(f"COALESCE(n.{c}, FALSE) IS DISTINCT FROM b.{c}" for c in LABELS)
    # The brackets hold the OR chain together: the example query below adds
    # "AND b.pitch_id > 0", and AND binds before OR.
    base = (
        f"FROM {SNAP} b LEFT JOIN {POOL} n ON n.pitch_id = b.pitch_id "
        f"WHERE (n.pitch_id IS NULL OR {diffs})"
    )
    bad_pitch, bad_pickoff = con.execute(
        "SELECT COUNT(*) FILTER (WHERE b.pitch_id > 0), COUNT(*) FILTER (WHERE b.pitch_id < 0) "
        + base
    ).fetchone()
    total = int(con.execute(f"SELECT COUNT(*) FROM {SNAP} WHERE pitch_id > 0").fetchone()[0])
    good = int(bad_pitch) == 0
    _log(
        f"  a. snapshot pitch rows back with the same six values: {total - bad_pitch:,} "
        f"of {total:,} (missing or changed: {bad_pitch:,})  {_ok(good)}"
        + ("" if good else _examples(con, "SELECT b.pitch_id " + base + " AND b.pitch_id > 0"))
    )
    if int(bad_pickoff):
        _log(
            f"     information: {int(bad_pickoff):,} pickoff rows of an earlier run are not back "
            "under the same id (a reloaded game renumbers its play records); check c proves "
            "this build's pickoff rows"
        )
    return good


def check_new_rows(con: Any, seasons: list[int]) -> bool:
    """Check b: every row the snapshot does not hold is a pickoff row."""
    base = (
        f"FROM {POOL} n LEFT JOIN {SNAP} b ON b.pitch_id = n.pitch_id "
        f"WHERE n.season IN ({_season_list(seasons)}) AND b.pitch_id IS NULL"
    )
    new, bad = con.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT (n.pitch_id < 0 "
        "AND COALESCE(n.is_pickoff_row, FALSE))) " + base
    ).fetchone()
    good = int(bad) == 0
    _log(
        f"  b. new rows {int(new):,}; of those not a pickoff row: {int(bad):,}  {_ok(good)}"
        + (
            ""
            if good
            else _examples(
                con,
                "SELECT n.pitch_id, n.season, n.is_pickoff_row "
                + base
                + " AND NOT (n.pitch_id < 0 AND COALESCE(n.is_pickoff_row, FALSE))",
            )
        )
    )
    return good


#: Check c's comparison of one pickoff row with its outcome (``s`` the row,
#: ``e`` the outcome).
_PICKOFF_ROW_DIFFS = " OR ".join(
    (
        "s.game_pk IS DISTINCT FROM e.game_pk",
        "s.at_bat_number IS DISTINCT FROM e.at_bat_number",
        "s.season IS DISTINCT FROM e.season",
        "s.game_date IS DISTINCT FROM e.game_date",
        "s.runner_id IS DISTINCT FROM e.runner_id",
        "s.pitcher_id IS DISTINCT FROM e.pitcher_id",
        "s.target_base IS DISTINCT FROM e.pair",
        "s.outs IS DISTINCT FROM e.outs_before",
        "s.count_balls IS DISTINCT FROM 0",
        "s.count_strikes IS DISTINCT FROM 0",
        "s.score_diff IS DISTINCT FROM GREATEST(-5, LEAST(5, e.bat_score - e.fld_score))",
        "COALESCE(s.attempted, FALSE)",
        "COALESCE(s.success, FALSE)",
        "COALESCE(s.pickoff_out, FALSE) IS DISTINCT FROM e.is_out",
        "COALESCE(s.pickoff_advancing, FALSE) IS DISTINCT FROM e.is_adv",
        "COALESCE(s.pickoff_error, FALSE) IS DISTINCT FROM (e.is_err AND NOT e.is_out)",
        "s.pitch_class IS NOT NULL",
        "NOT COALESCE(s.is_pickoff_row, FALSE)",
    )
)


def check_pickoff_rows(con: Any, seasons: list[int], counts: dict[int, PickoffCount]) -> bool:
    """Check c: the pickoff rows are exactly the counted outcomes, one row
    each, and each row carries its outcome's facts."""
    sl = _season_list(seasons)
    joined = (
        f"FROM (SELECT * FROM {EXPECT} WHERE NOT has_pitch AND has_ids) e "
        f"FULL OUTER JOIN (SELECT * FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0) s "
        "ON s.pitch_id = -e.id"
    )
    missing, extra, differ = con.execute(
        "SELECT COUNT(*) FILTER (WHERE s.pitch_id IS NULL), "
        "COUNT(*) FILTER (WHERE e.id IS NULL), "
        f"COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND e.id IS NOT NULL "
        f"AND ({_PICKOFF_ROW_DIFFS})) " + joined
    ).fetchone()
    built = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0 "
            "GROUP BY 1"
        ).fetchall()
    }
    ok = int(missing) == 0 and int(extra) == 0 and int(differ) == 0
    for s in seasons:
        want = counts.get(int(s), PickoffCount()).rows
        good = built.get(int(s), 0) == want
        ok = ok and good
        _log(f"  c. {s}: pickoff rows {built.get(int(s), 0):,}; counted {want:,}  {_ok(good)}")
    _log(
        f"  c. counted outcomes with no row {int(missing):,}; rows with no counted outcome "
        f"{int(extra):,}; rows whose runner, thrower, pair, outs, count or labels differ "
        f"{int(differ):,}  {_ok(ok)}"
    )
    if int(missing):
        _log(
            "     "
            + _examples(
                con,
                "SELECT e.id, e.game_pk, e.at_bat_number " + joined + " WHERE s.pitch_id IS NULL",
            )
        )
    if int(extra):
        _log(
            "     "
            + _examples(
                con,
                "SELECT s.pitch_id, s.game_pk, s.at_bat_number " + joined + " WHERE e.id IS NULL",
            )
        )
    if int(differ):
        _log(
            "     "
            + _examples(
                con,
                "SELECT e.id " + joined + f" WHERE s.pitch_id IS NOT NULL AND e.id IS NOT NULL "
                f"AND ({_PICKOFF_ROW_DIFFS})",
            )
        )
    return ok


def check_identity(con: Any, seasons: list[int], counts: dict[int, PickoffCount]) -> bool:
    """Check d: tagged rows + pickoff rows equal the outcomes that fit a
    pair, less the listed ones (no runner or pitcher id; a second outcome
    folded into one tag)."""
    sl = _season_list(seasons)
    tagged = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id > 0 "
            "AND (COALESCE(pickoff_out, FALSE) OR COALESCE(pickoff_error, FALSE)) GROUP BY 1"
        ).fetchall()
    }
    built = {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM {POOL} WHERE season IN ({sl}) AND pitch_id < 0 "
            "GROUP BY 1"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        c = counts.get(int(s), PickoffCount())
        t, p = tagged.get(int(s), 0), built.get(int(s), 0)
        good = t == c.tag_groups and t + p + c.folded + len(c.no_id) == c.fit
        ok = ok and good
        _log(
            f"  d. {s}: tagged rows {t:,} + pickoff rows {p:,} = {t + p:,}; outcomes that fit a "
            f"pair {c.fit:,}, less {c.folded:,} folded and {len(c.no_id):,} listed = "
            f"{c.fit - c.folded - len(c.no_id):,}  {_ok(good)}"
        )
    return ok


def check_classes(con: Any, seasons: list[int]) -> bool:
    """Check e: the class is set on every pitch row and NULL on every pickoff
    row; it equals the pitch pool's class for the same pitch, so the shares
    per (target, outs, balls, strikes) equal the pitch pool's."""
    sl = _season_list(seasons)
    classes = "(" + ", ".join(f"'{c}'" for c in STEAL_PITCH_CLASSES) + ")"
    pitch_null, pitch_marked, odd, po_class, po_unmarked = con.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE pitch_id > 0 AND pitch_class IS NULL),
               COUNT(*) FILTER (WHERE pitch_id > 0 AND COALESCE(is_pickoff_row, FALSE)),
               COUNT(*) FILTER (WHERE pitch_class IS NOT NULL AND pitch_class NOT IN {classes}),
               COUNT(*) FILTER (WHERE pitch_id < 0 AND pitch_class IS NOT NULL),
               COUNT(*) FILTER (WHERE pitch_id < 0 AND NOT COALESCE(is_pickoff_row, FALSE))
        FROM {POOL} WHERE season IN ({sl})
        """
    ).fetchone()
    joined = (
        f"FROM {POOL} s LEFT JOIN sim.pitch_pool pp ON pp.pitch_id = s.pitch_id "
        f"WHERE s.season IN ({sl}) AND s.pitch_id > 0"
    )
    no_pitch, differ = con.execute(
        "SELECT COUNT(*) FILTER (WHERE pp.pitch_id IS NULL), "
        "COUNT(*) FILTER (WHERE s.pitch_class IS DISTINCT FROM pp.outcome_type) " + joined
    ).fetchone()
    worst = con.execute(
        f"""
        WITH j AS (
            SELECT s.target_base AS t, s.outs AS o, s.count_balls AS b, s.count_strikes AS k,
                   COALESCE(s.pitch_class, '<none>') AS ours,
                   COALESCE(pp.outcome_type, '<none>') AS theirs
            {joined}
        ),
        cell AS (SELECT t, o, b, k, COUNT(*) AS n FROM j GROUP BY ALL),
        a AS (SELECT t, o, b, k, ours AS cls, COUNT(*) AS n FROM j GROUP BY ALL),
        p AS (SELECT t, o, b, k, theirs AS cls, COUNT(*) AS n FROM j GROUP BY ALL)
        SELECT COALESCE(MAX(ABS(COALESCE(a.n, 0) - COALESCE(p.n, 0)) / cell.n), 0)
        FROM a FULL OUTER JOIN p USING (t, o, b, k, cls) JOIN cell USING (t, o, b, k)
        """
    ).fetchone()[0]
    good = (
        int(pitch_null) == 0
        and int(pitch_marked) == 0
        and int(odd) == 0
        and int(po_class) == 0
        and int(po_unmarked) == 0
        and int(no_pitch) == 0
        and int(differ) == 0
        and float(worst) == 0.0
    )
    _log(
        f"  e. pitch rows with no class {int(pitch_null):,}, marked as pickoff rows "
        f"{int(pitch_marked):,}; classes outside the six {int(odd):,}; pickoff rows with a class "
        f"{int(po_class):,}, unmarked {int(po_unmarked):,}  {_ok(good)}"
    )
    _log(
        f"  e. pitch rows with no pitch-pool row {int(no_pitch):,}; with a class other than the "
        f"pitch pool's {int(differ):,}; the largest class-share gap in a (target, outs, balls, "
        f"strikes) cell {float(worst):.6f}  {_ok(good)}"
    )
    for t, cls, n_att in con.execute(
        f"SELECT target_base, pitch_class, COUNT(*) FROM {POOL} WHERE season IN ({sl}) "
        "AND pitch_id > 0 AND attempted GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    ).fetchall():
        _log(f"     attempts on target {t}: {cls} {int(n_att):,}")
    return good


def check_dead_ball_attempts(con: Any, seasons: list[int]) -> bool:
    """Check f: no attempted row rides a ball in play or a hit by pitch."""
    dead = "(" + ", ".join(f"'{c}'" for c in DEAD_BALL_CLASSES) + ")"
    n = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            f"AND COALESCE(attempted, FALSE) AND pitch_class IN {dead}"
        ).fetchone()[0]
    )
    good = n == 0
    _log(f"  f. attempted rows on a ball in play or a hit by pitch: {n:,}  {_ok(good)}")
    return good


def check_pickoff_row_ids(con: Any, seasons: list[int]) -> bool:
    """Check g: every pickoff row has a game date, a runner and a pitcher."""
    n = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "AND pitch_id < 0 AND (game_date IS NULL OR runner_id IS NULL OR pitcher_id IS NULL)"
        ).fetchone()[0]
    )
    no_catcher = int(
        con.execute(
            f"SELECT COUNT(*) FROM {POOL} WHERE season IN ({_season_list(seasons)}) "
            "AND pitch_id < 0 AND catcher_id IS NULL"
        ).fetchone()[0]
    )
    good = n == 0
    _log(
        f"  g. pickoff rows with no game date, runner or pitcher: {n:,}  {_ok(good)} "
        f"(with no catcher, a neutral weight: {no_catcher:,})"
    )
    return good


def check_metadata(con: Any, seasons: list[int]) -> bool:
    """Check h: the build metadata names the builder for every season, with
    the table's row count."""
    after = _steal_counts(con, seasons)
    meta = {
        int(s): (v, int(n))
        for s, v, n in con.execute(
            "SELECT season, builder_version, row_count FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'steal_opportunity_pool' AND season IN ({_season_list(seasons)})"
        ).fetchall()
    }
    ok = True
    for s in seasons:
        version, n = meta.get(int(s), (None, -1))
        good = version == _builder() and n == after.get(int(s), 0)
        ok = ok and good
        _log(f"  h. {s}: builder {version}  row_count {n:,}  {_ok(good)}")
    return ok


def matches_snapshot(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """TRUE when the pool holds exactly the snapshot's rows, labels and builders."""
    if _steal_counts(con, seasons) != before or _steal_versions(con, seasons) != versions:
        return False
    diffs = " OR ".join(f"COALESCE(n.{c}, FALSE) IS DISTINCT FROM b.{c}" for c in LABELS)
    differ = con.execute(
        f"SELECT COUNT(*) FROM {SNAP} b LEFT JOIN {POOL} n ON n.pitch_id = b.pitch_id "
        f"WHERE (n.pitch_id IS NULL OR {diffs})"
    ).fetchone()[0]
    return int(differ) == 0


def _log_migration_rolled_back(con: Any, migrated_here: bool) -> None:
    """After a rollback (exit 1 or 2), say what became of migration 0031's columns.

    When this run applied 0031 inside the transaction, the columns go with the
    rows. When they were on the table before this run (an earlier run
    committed them, or someone applied 0031 by hand), the rollback cannot
    remove them, and master's pool builder fails on them until the merge.
    """
    left = [c for c in NEW_COLUMNS if c not in missing_columns(con)]
    if not migrated_here:
        if left:
            _log(
                f"  WARNING: the 0031 columns {left} were on the table before this run, so the "
                "rollback keeps them. Master's pool builder fails on this table after it "
                "deletes the season: merge the SIM-554 code, or hold the nightly and every "
                "`make profile-computor`, or drop the two columns"
            )
        return
    if left:
        _log(
            f"  WARNING: the 0031 columns {left} stayed after the rollback. Master's pool "
            "builder fails on this table after it deletes the season: merge the SIM-554 code, "
            "or hold the nightly and every `make profile-computor`, or drop the two columns"
        )
    else:
        _log(f"  {MIGRATION.name} rolled back with the rows: the table has its old shape")


def rollback(
    con: Any, seasons: list[int], before: dict[int, int], versions: dict[int, str | None]
) -> bool:
    """Roll the rebuild's transaction back and prove the table is as it was."""
    try:
        con.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 - report, then verify what the table holds
        _log(f"  ROLLBACK raised {type(exc).__name__}: {exc}")
    restored = matches_snapshot(con, seasons, before, versions)
    _log(
        "  the transaction is rolled back: sim.steal_opportunity_pool "
        + (
            "holds the snapshot's rows, labels and builders"
            if restored
            else "DOES NOT match the snapshot. Read it before the next pool export"
        )
    )
    return restored


# ---------------------------------------------------------------------------
# The export, the round-trip and the centres
# ---------------------------------------------------------------------------


def export(duckdb_path: str, art_dir: str, window: list[int]) -> dict[str, int]:
    """Step 5: the steal pool ONLY, from a read-only connection."""
    ro = duckdb.connect(duckdb_path, read_only=True)
    try:
        counts = build_steal_pool_artifact(ro, art_dir, window)
    finally:
        ro.close()
    for t in TARGETS:
        _log(f"  steal_pool[{t}]: {int(counts.get(t, 0)):,} rows exported")
    return counts


def _file_order_ids(con: Any, path: str) -> np.ndarray:
    d = con.execute(
        f"SELECT pitch_id FROM read_parquet('{_sql_path(path)}', file_row_number = true) "
        "ORDER BY file_row_number"
    ).fetchnumpy()
    return np.asarray(np.ma.filled(d["pitch_id"], 0), dtype=np.int64)


def roundtrip_target(
    con: Any, pool_dir: str, target: str, window: list[int], n_manifest: int, table: str
) -> bool:
    """One target of the exported pool against DuckDB's ``table``, per pitch_id."""
    meta = os.path.join(pool_dir, f"{target}.meta.parquet")
    cols = set(_columns(con, meta))
    need = {"pitch_id", "pitch_class", "is_pickoff_row", "runner_id", "season", *LABELS}
    if need - cols:
        _log(
            f"  steal_pool[{target}]: the export carries no {sorted(need - cols)}; the "
            "round-trip keys on pitch_id  MISMATCH"
        )
        return False
    where = f"target_base = {int(target)} AND season IN ({_season_list(window)})"
    n_duck = int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])
    n_file, n_ids = con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT pitch_id) FROM {_pq(meta)}"
    ).fetchone()
    sit = np.load(os.path.join(pool_dir, f"{target}.sit.npy"))
    counts_good = n_manifest == n_duck == int(n_file) == int(n_ids) == int(sit.shape[0])
    _log(
        f"  steal_pool[{target}]: manifest {n_manifest:,}, DuckDB {n_duck:,}, parquet "
        f"{int(n_file):,} ({int(n_ids):,} distinct pitch ids), sit {int(sit.shape[0]):,}  "
        f"{_ok(counts_good)}"
    )
    diffs = " OR ".join(
        [f"COALESCE(d.{c}, FALSE) IS DISTINCT FROM COALESCE(p.{c}, FALSE)" for c in LABELS]
        + [
            "COALESCE(d.pitch_class, '') IS DISTINCT FROM p.pitch_class",
            "COALESCE(d.is_pickoff_row, FALSE) IS DISTINCT FROM p.is_pickoff_row",
            "d.runner_id IS DISTINCT FROM p.runner_id",
            "d.season IS DISTINCT FROM p.season",
        ]
    )
    differ = int(
        con.execute(
            f"SELECT COUNT(*) FROM {_pq(meta)} p "
            f"FULL OUTER JOIN (SELECT * FROM {table} WHERE {where}) d ON d.pitch_id = p.pitch_id "
            f"WHERE p.pitch_id IS NULL OR d.pitch_id IS NULL OR {diffs}"
        ).fetchone()[0]
    )
    rows_good = differ == 0
    _log(
        f"  steal_pool[{target}]: rows whose pitch id, six labels, class or mark differ from "
        f"DuckDB: {differ:,}  {_ok(rows_good)}"
    )
    # The situation array is aligned with the parquet by row: row i of sit.npy
    # is the parquet's row i. Read the count and the margin per pitch id.
    ids = _file_order_ids(con, meta)
    d = con.execute(
        f"SELECT pitch_id, count_balls, count_strikes, outs, score_diff FROM {table} "
        f"WHERE {where} ORDER BY pitch_id"
    ).fetchnumpy()
    dids = np.asarray(d["pitch_id"], dtype=np.int64)
    sit_good = ids.size == sit.shape[0]
    if sit_good and dids.size:
        pos = np.clip(np.searchsorted(dids, ids), 0, dids.size - 1)
        found = dids[pos] == ids
        want = np.stack(
            [
                np.asarray(d[c], dtype=np.float32)
                for c in ("count_balls", "count_strikes", "outs", "score_diff")
            ],
            axis=1,
        )[pos]
        bad = (~found) | np.any(want != sit.astype(np.float32), axis=1)
        sit_good = not bool(bad.any())
        n_bad = int(bad.sum())
    else:
        n_bad = int(ids.size)
        sit_good = sit_good and ids.size == 0
    ordered = bool(np.all(np.diff(ids) > 0)) if ids.size > 1 else True
    _log(
        f"  steal_pool[{target}]: situation rows (count, outs, margin) unlike DuckDB's for their "
        f"pitch id: {n_bad:,}  {_ok(sit_good)}; rows in pitch-id order: {ordered}"
    )
    return counts_good and rows_good and sit_good


def roundtrip(
    duckdb_path: str, art_dir: str, window: list[int], hashes_before: dict[str, str]
) -> bool:
    """Step 6: the exported steal pool against DuckDB, then the rest of the bundle."""
    pool_dir = os.path.join(art_dir, "steal_pool")
    why = steal_pool_problem(pool_dir)
    if why is not None:
        _log(f"  the exported steal pool is not whole ({why})  MISMATCH")
        return False
    with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    seasons = [int(s) for s in manifest.get("seasons", [])]
    ok = seasons == window
    _log(f"  the manifest's seasons {seasons}; the window {window}  {_ok(ok)}")
    mem = duckdb.connect()
    try:
        mem.execute(f"ATTACH '{_sql_path(duckdb_path)}' AS x (READ_ONLY)")
        for t in TARGETS:
            ok = (
                roundtrip_target(mem, pool_dir, t, window, int(manifest["counts"][t]), f"x.{POOL}")
                and ok
            )
    finally:
        mem.close()
    t0 = time.time()
    changed = compare_hashes(hashes_before, bundle_hashes(art_dir))
    good = not changed
    _log(
        f"  the bundle outside steal_pool/: {len(hashes_before):,} files, "
        f"{'all byte-identical' if good else f'{len(changed)} differ: {changed[:5]}'} "
        f"({time.time() - t0:.0f} s)  {_ok(good)}"
    )
    return ok and good


def steal_centres(
    attempted: np.ndarray,
    success: np.ndarray,
    recency: np.ndarray,
    pickoff_out: np.ndarray,
    pickoff_error: np.ndarray,
    pitch_class: np.ndarray | None,
) -> dict[str, float]:
    """The steal band centres of one target's pool, over PITCH rows only.

    ``pitch_class`` holds the class codes (1-6; 0 = a pickoff row), or None
    for a pool without classes (every row a pitch row). The attempt rate is
    recency-weighted and the safe share is not, as the steal probe that set
    the band centres computed them (``scripts/archive/sim476_steal_probe.py``).
    The pickoff outcomes count every row: the tags on pitch rows and the
    pickoff rows, over the pitch rows.
    """
    att = np.asarray(attempted).astype(bool)
    suc = np.asarray(success).astype(bool)
    rcy = np.asarray(recency, dtype=np.float64)
    po = np.asarray(pickoff_out).astype(bool) | np.asarray(pickoff_error).astype(bool)
    pitch = np.ones(att.size, dtype=bool) if pitch_class is None else np.asarray(pitch_class) > 0
    a, r, s = att[pitch], rcy[pitch], suc[pitch]
    n_pitch = int(pitch.sum())
    return {
        "rows": float(att.size),
        "pitch_rows": float(n_pitch),
        "pickoff_rows": float(att.size - n_pitch),
        "att_rate": float(a.mean()) if n_pitch else 0.0,
        "att_rate_rcy": float((a * r).sum() / r.sum()) if r.sum() > 0 else 0.0,
        "safe_share": float(s[a].mean()) if a.any() else 0.0,
        "pickoff_outcomes": float(po.sum()),
        "pickoff_per_pitch_row": float(po.sum() / n_pitch) if n_pitch else 0.0,
    }


def class_codes(values: Any) -> np.ndarray:
    """The pool's class strings as codes: 1-6 in ``STEAL_PITCH_CLASSES`` order,
    0 for the empty class of a pickoff row, -1 for a string outside the six."""
    return np.fromiter(
        (
            0 if (v is None or str(v) == "") else STEAL_PITCH_CLASS_CODE.get(str(v), -1)
            for v in values
        ),
        dtype=np.int8,
    )


def read_pool_centres(pool_dir: str) -> dict[str, dict[str, float]]:
    """:func:`steal_centres` per target, read from a steal pool directory."""
    out: dict[str, dict[str, float]] = {}
    mem = duckdb.connect()
    try:
        for t in TARGETS:
            meta = os.path.join(pool_dir, f"{t}.meta.parquet")
            cols = set(_columns(mem, meta))
            pick = ["attempted", "success", "recency_weight", "pickoff_out", "pickoff_error"]
            if "pitch_class" in cols:
                pick.append("pitch_class")
            d = mem.execute(f"SELECT {', '.join(pick)} FROM {_pq(meta)}").fetchnumpy()
            out[t] = steal_centres(
                np.ma.filled(d["attempted"], False),
                np.ma.filled(d["success"], False),
                np.ma.filled(d["recency_weight"], 0.0),
                np.ma.filled(d["pickoff_out"], False),
                np.ma.filled(d["pickoff_error"], False),
                class_codes(np.ma.filled(d["pitch_class"], "")) if "pitch_class" in d else None,
            )
    finally:
        mem.close()
    return out


def band_centres() -> tuple[dict[str, float], str]:
    """The steal band centres of ``tests/acceptance/bands.py`` (pure arithmetic;
    loaded from its file), else the fallback copy."""
    path = _ROOT / "tests" / "acceptance" / "bands.py"
    try:
        spec = importlib.util.spec_from_file_location("_sim554_bands", str(path))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_sim554_bands"] = mod
        spec.loader.exec_module(mod)
        refs = mod.POOL_REFERENCES
        return {k: float(refs[k].centre) for k in FALLBACK_CENTRES}, str(path)
    except Exception as exc:  # noqa: BLE001 - the copy below is the 2026-09 value
        return dict(FALLBACK_CENTRES), f"the script's copy ({type(exc).__name__}: {exc})"


def print_centres(before_dir: str, after_dir: str) -> None:
    """Step 7: the artifact pool's centres over pitch rows, beside the bands'."""
    centres, source = band_centres()
    before = read_pool_centres(before_dir)
    after = read_pool_centres(after_dir)
    _log(f"  the band centres ({source}):")
    names = {"2": ("STEAL_ATT_OPP_2B", "STEAL_SAFE_2B"), "3": ("STEAL_ATT_OPP_3B", None)}
    for t in TARGETS:
        b, a = before[t], after[t]
        att_band, safe_band = names[t]
        _log(
            f"    target {t}: pitch rows {int(b['pitch_rows']):,} -> {int(a['pitch_rows']):,}; "
            f"pickoff rows {int(b['pickoff_rows']):,} -> {int(a['pickoff_rows']):,}"
        )
        _log(
            f"    target {t}: attempts per opportunity (recency-weighted) "
            f"{b['att_rate_rcy']:.4f} -> {a['att_rate_rcy']:.4f}; band {att_band} "
            f"{centres[att_band]:.4f}"
            + (
                "  MOVED beyond the fourth decimal: update bands.py"
                if round(a["att_rate_rcy"], 4) != round(centres[att_band], 4)
                else ""
            )
        )
        safe_note = (
            f"; band {safe_band} {centres[safe_band]:.4f}"
            + (
                "  MOVED: update bands.py"
                if round(a["safe_share"], 4) != round(centres[safe_band], 4)
                else ""
            )
            if safe_band
            else ""
        )
        _log(
            f"    target {t}: safe share {b['safe_share']:.4f} -> {a['safe_share']:.4f}{safe_note}"
        )
    po_b = sum(before[t]["pickoff_outcomes"] for t in TARGETS)
    po_a = sum(after[t]["pickoff_outcomes"] for t in TARGETS)
    n_b = sum(before[t]["pitch_rows"] for t in TARGETS)
    n_a = sum(after[t]["pitch_rows"] for t in TARGETS)
    rate_b = po_b / n_b if n_b else 0.0
    rate_a = po_a / n_a if n_a else 0.0
    _log(
        f"  pickoff outcomes per pitch row: before {rate_b:.6f} ({int(po_b):,} outcomes), after "
        f"{rate_a:.6f} ({int(po_a):,}); ratio {rate_a / rate_b if rate_b else float('nan'):.3f} "
        f"(the design's figure {MEASURED_FIT:,} / 1,394 = {MEASURED_FIT / 1394:.3f})"
    )


def print_rollback(art_dir: str, backup_dir: str) -> None:
    rejected = f"{art_dir}.steal_pool.sim554_rejected.{time.strftime('%Y%m%d-%H%M%S')}"
    _log("ROLLBACK (the bundle's steal pool): stop the app, put the copy back, start the app:")
    _log("    docker compose stop app")
    _log(
        "    MSYS_NO_PATHCONV=1 docker compose run --rm -T app sh -c "
        f"'mv {art_dir}/steal_pool {rejected} && cp -a {backup_dir} {art_dir}/steal_pool'"
    )
    _log("    docker compose up -d app")
    _log(
        "  The app then draws on the old steal pool. It has no class, so the loop runs the "
        "single pre-pitch draw whatever SIM_STEAL_PITCH_CLASS says."
    )
    _log(
        "  sim.steal_opportunity_pool in DuckDB keeps the sim554.1 rows. HOLD every pool export "
        "(the nightly chain, make engine-artifacts, engine_artifacts --what pool or all) until "
        "the table is on the rows you keep: an export ships them again. To undo the pickoff "
        "rows in DuckDB (the app stopped): DELETE FROM sim.steal_opportunity_pool WHERE "
        "is_pickoff_row."
    )


def print_next_steps() -> None:
    _log("NEXT (the run book, design §8), from the MAIN checkout:")
    _log(
        "  4. docker compose up -d app — the boot log reads build_all_engines: 11/11 and the "
        "calibration applied"
    )
    _log(
        "  5. the running-game census, both arms on the balanced 45 games x 20 "
        "(scripts/sim554_running_game_census.py; its docstring has the recipe); a stop rule "
        "it prints stops the run book for a diagnosis"
    )
    _log("  6. the ten-game smoke (scripts/sim_stats.py, 10 x 50): no collapse")
    _log(
        "  7. the lane (45 x 130, tests/acceptance/test_production_config_bands_sim450.py); "
        "update the steal centres in tests/acceptance/bands.py first if the lines above say MOVED"
    )
    _log("  8. close: CHANGES.md, BACKLOG.xlsx, the docs; merge before the next nightly pool build")


# ---------------------------------------------------------------------------
# The two modes
# ---------------------------------------------------------------------------


def _open_writable(path: str) -> Any:
    """Open the DuckDB file writable, or return None when another process holds its lock."""
    try:
        return duckdb.connect(path)
    except duckdb.IOException as exc:
        if "lock" in str(exc).lower() or "already open" in str(exc).lower():
            return None
        raise


def _seasons(args: argparse.Namespace, window: list[int]) -> list[int]:
    return sorted(set(args.seasons)) if args.seasons else list(window)


def _check_only(args: argparse.Namespace) -> int:
    art_dir = args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-554 check-only (read-only; writes nothing): {args.duckdb_path} ===")
    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        seasons = _seasons(args, window)
        _log("=== the write path's pre-flight ===")
        problems = preflight(
            con,
            seasons,
            window,
            art_dir,
            backup_dir,
            force=args.force_backup,
            apply_migration_flag=args.apply_migration,
        )
        for p in problems:
            _log(f"  PRE-FLIGHT: {p}")
        if not problems:
            _log("  the pre-flight is clear (the DuckDB lock is tested only on the write path)")
        _log("=== the pickoff rows the rebuild would write (the play records, counted) ===")
        take_snapshot(con, seasons)
        count_pickoff_rows(con, seasons)
        log_pickoff_summary(pickoff_summary(con, seasons))
    finally:
        con.close()
    _log(f"SIM-554 CHECK-ONLY COMPLETE: pre-flight {'clear' if not problems else 'STOPS'}")
    return EXIT_OK if not problems else EXIT_STOPPED


def _rebuild(args: argparse.Namespace, t0: float) -> int:
    duckdb_path, art_dir = args.duckdb_path, args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-554 steal-pool rebuild: builder {_builder()} ===")
    _log("=== 0. pre-flight ===")
    if _builder() != EXPECTED_BUILDER:
        _log(f"SIM-554 REBUILD FAILED: the builder is {_builder()!r}, not {EXPECTED_BUILDER!r}")
        return EXIT_STOPPED
    if not os.path.isfile(duckdb_path):
        # duckdb.connect would CREATE an empty database at a wrong path.
        _log(f"SIM-554 REBUILD FAILED: {duckdb_path} does not exist")
        return EXIT_STOPPED
    con = _open_writable(duckdb_path)
    if con is None:
        _log(
            "the app is running: stop it first — SIM-524 (the app's worker server holds the DuckDB "
            "writer lock; any other container that reads the DuckDB file holds a lock too)"
        )
        _log("SIM-554 REBUILD FAILED: the DuckDB file is locked")
        return EXIT_LOCKED
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        seasons = _seasons(args, window)
        _log(f"  seasons to rebuild: {seasons}")
        problems = preflight(
            con,
            seasons,
            window,
            art_dir,
            backup_dir,
            force=args.force_backup,
            apply_migration_flag=args.apply_migration,
        )
        if problems:
            for p in problems:
                _log(f"  PRE-FLIGHT: {p}")
            _log("SIM-554 REBUILD FAILED at the pre-flight: nothing was written")
            return EXIT_STOPPED
        t = time.time()
        hashes_before = bundle_hashes(art_dir)
        _log(
            f"  hashed the bundle outside steal_pool/: {len(hashes_before):,} files "
            f"({time.time() - t:.0f} s)"
        )

        _log("=== 1. snapshot, and the pickoff rows counted from the play records ===")
        before = _steal_counts(con, seasons)
        versions = _steal_versions(con, seasons)
        take_snapshot(con, seasons)
        _log(f"  snapshot: {sum(before.values()):,} rows; builders {versions}")
        count_pickoff_rows(con, seasons)
        counts = pickoff_summary(con, seasons)
        log_pickoff_summary(counts)

        _log(f"=== 2. rebuild ({_builder()}), one transaction ===")
        t = time.time()
        con.execute("BEGIN TRANSACTION")
        migrated_here = False
        try:
            # The migration runs INSIDE the transaction, so a failed check or a
            # raise rolls the two columns back with the rows. Master's builder
            # (a positional INSERT of 21 values) would otherwise meet a 23-column
            # table on its next pool build, delete the season, and fail.
            if missing_columns(con):
                n = apply_migration(con)
                still = missing_columns(con)
                migrated_here = True
                _log(
                    f"  applied {MIGRATION.name} ({n} statements) inside the transaction; "
                    f"still missing: {still or 'none'}"
                )
                if still:
                    raise RuntimeError(f"{MIGRATION.name} did not add the columns {still}")
            comp = ppc.PlayerProfileComputor.__new__(ppc.PlayerProfileComputor)
            comp._conn = con
            comp._build_steal_opportunity_pool(seasons, incremental=False)
            _log(f"  rebuilt in {time.time() - t:.0f} s")
            _log("=== 3. checks ===")
            results = {
                "a": check_snapshot_back(con),
                "b": check_new_rows(con, seasons),
                "c": check_pickoff_rows(con, seasons, counts),
                "d": check_identity(con, seasons, counts),
                "e": check_classes(con, seasons),
                "f": check_dead_ball_attempts(con, seasons),
                "g": check_pickoff_row_ids(con, seasons),
                "h": check_metadata(con, seasons),
            }
        except Exception as exc:
            _log("  the rebuild or a check raised; rolling it back")
            if isinstance(exc, duckdb.ConstraintException) and "Duplicate key" in str(exc):
                _log(
                    f"  DuckDB {duckdb.__version__} refused to delete and re-insert a pool row "
                    "in one transaction. An older DuckDB checks the key too early; the SIM-553 "
                    "pitch-pool rebuild ran the same pattern in the container's DuckDB"
                )
            rollback(con, seasons, before, versions)
            _log_migration_rolled_back(con, migrated_here)
            raise
        failed = [k for k, good in results.items() if not good]
        if failed:
            rollback(con, seasons, before, versions)
            _log_migration_rolled_back(con, migrated_here)
            _log(
                f"SIM-554 REBUILD FAILED at check {', '.join(failed)}: rolled back; the bundle is "
                "untouched"
            )
            return EXIT_STOPPED
        con.execute("COMMIT")
        _log("  every check passed: COMMIT")
    finally:
        con.close()

    _log("=== 4. the copy of the bundle's steal pool ===")
    try:
        rollback_dir = take_backup(art_dir, backup_dir, args.force_backup)
    except Exception as exc:  # noqa: BLE001 - the bundle is untouched; say so and stop
        _log(f"  the copy failed: {type(exc).__name__}: {exc}")
        _log(
            "SIM-554 REBUILD FAILED at the copy: sim.steal_opportunity_pool is rebuilt "
            f"(committed); the bundle is untouched. A partial copy may remain at {backup_dir}. "
            "Fix the cause, then re-run with --force-backup (it renames that copy aside, never "
            "deletes it). The re-run's check a compares the pitch rows; check c proves the "
            "pickoff rows against the play records again."
        )
        return EXIT_STOPPED
    _log("=== 5. export the steal pool only ===")
    try:
        t = time.time()
        with open(export_marker(rollback_dir), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        export(duckdb_path, art_dir, window)
        _log(f"  exported in {time.time() - t:.0f} s")
    except Exception:
        traceback.print_exc(file=sys.stdout)
        print_rollback(art_dir, rollback_dir)
        _log(
            "SIM-554 REBUILD FAILED at the export: the bundle's steal_pool/ may be half-written. "
            f"Roll it back (above), or re-run: the re-run keeps {rollback_dir}"
        )
        return EXIT_BUNDLE
    _log("=== 6. round-trip ===")
    try:
        ok = roundtrip(duckdb_path, art_dir, window, hashes_before)
    except Exception:
        traceback.print_exc(file=sys.stdout)
        _log("  the round-trip raised")
        ok = False
    if not ok:
        print_rollback(art_dir, rollback_dir)
        _log("SIM-554 REBUILD FAILED at the round-trip: read the MISMATCH lines, then roll back")
        return EXIT_BUNDLE
    with contextlib.suppress(OSError):
        os.remove(export_marker(rollback_dir))

    _log("=== 7. the centres, the rollback, the next steps ===")
    try:
        print_centres(rollback_dir, os.path.join(art_dir, "steal_pool"))
    except Exception as exc:  # noqa: BLE001 - the centres are a print; the rebuild is done
        _log(f"  the centres did not read ({type(exc).__name__}: {exc})")
    print_rollback(art_dir, rollback_dir)
    print_next_steps()
    _log(f"SIM-554 REBUILD COMPLETE in {(time.time() - t0) / 60:.1f} min")
    return EXIT_OK


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="the pool seasons to rebuild (default: the window; must hold it)",
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="read-only: the pre-flight and the counted pickoff rows; no rebuild, no export",
    )
    ap.add_argument(
        "--apply-migration",
        action="store_true",
        help="apply migration 0031 inside the rebuild's transaction when the pool lacks its "
        "columns (else the run stops); the run book always passes it",
    )
    ap.add_argument(
        "--force-backup",
        action="store_true",
        help=f"when <bundle>{BACKUP_SUFFIX} exists but cannot be the rollback point, rename it "
        "aside (never delete it) and copy the bundle's steal pool again",
    )
    ap.add_argument("--duckdb-path", default=DUCKDB_PATH)
    ap.add_argument("--art-dir", default=ART_DIR, help="the engine-artifact bundle")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    # One stream in one order: the builder's and the exporter's log lines go to
    # stdout beside this script's own lines.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    t0 = time.time()
    try:
        return _check_only(args) if args.check_only else _rebuild(args, t0)
    except Exception as exc:  # noqa: BLE001 - one FAILED line, then the traceback
        traceback.print_exc(file=sys.stdout)
        _log(
            f"SIM-554 {'CHECK-ONLY' if args.check_only else 'REBUILD'} FAILED: "
            f"{type(exc).__name__}: {exc}"
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
