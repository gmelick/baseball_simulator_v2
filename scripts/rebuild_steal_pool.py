"""
scripts/rebuild_steal_pool.py — the steal-pool rebuild for the running game on the pitch
(SIM-554), end to end. The steps live in ``scripts/steal_pool_rebuild/``.

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

    W=/c/Users/grego/Documents/baseball_simulator_v2   # the checkout whose code runs
    docker compose stop app
    MSYS_NO_PATHCONV=1 docker compose run -d --rm \\
        -v "$W/scripts:/app/scripts" -v "$W/pipeline:/app/pipeline" \\
        -v "$W/simulation:/app/simulation" -v "$W/db:/app/db" \\
        app python scripts/rebuild_steal_pool.py --apply-migration
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
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# The steps live in scripts/steal_pool_rebuild/. A caller that loads this file by path
# (a test) may not have scripts/ on sys.path.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

from steal_pool_rebuild.bundle import bundle_hashes, export_marker, take_backup  # noqa: E402
from steal_pool_rebuild.checks import (  # noqa: E402
    _log_migration_rolled_back,
    check_classes,
    check_dead_ball_attempts,
    check_identity,
    check_metadata,
    check_new_rows,
    check_pickoff_row_ids,
    check_pickoff_rows,
    check_snapshot_back,
    rollback,
)
from steal_pool_rebuild.common import (  # noqa: E402
    ART_DIR,
    BACKUP_SUFFIX,
    DUCKDB_PATH,
    EXIT_BUNDLE,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_STOPPED,
    EXPECTED_BUILDER,
    MIGRATION,
    _builder,
    _log,
)
from steal_pool_rebuild.export import (  # noqa: E402
    export,
    print_centres,
    print_next_steps,
    print_rollback,
    roundtrip,
)
from steal_pool_rebuild.migration import apply_migration, missing_columns  # noqa: E402
from steal_pool_rebuild.pickoffs import (  # noqa: E402
    count_pickoff_rows,
    log_pickoff_summary,
    pickoff_summary,
    take_snapshot,
)
from steal_pool_rebuild.preflight import _steal_counts, _steal_versions, preflight  # noqa: E402

import pipeline.batch.player_profile_computor as ppc  # noqa: E402
from pipeline.batch.engine_artifacts import last_n_seasons  # noqa: E402
from pipeline.batch.pool_chain import attach_pg  # noqa: E402

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
