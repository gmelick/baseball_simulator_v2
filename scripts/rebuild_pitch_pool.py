"""
scripts/rebuild_pitch_pool.py — the pitch-pool rebuild on the foul-tip coding (SIM-553),
end to end. The steps live in ``scripts/pitch_pool_rebuild/``.

WHAT IT DOES
============
The pool build codes a two-strike foul tip or foul bunt (Gameday ``T``,
``L``, ``O``) as strike three: class ``swinging_strike``. It also codes ``O``,
``Q``, ``R``, ``P`` and ``H`` explicitly and has no ELSE
(``pipeline/batch/player_profile_computor.py`` ``SQL_OUTCOME_TYPE``; builder
``sim553.1``). This script rebuilds ``sim.pitch_pool`` on that coding. It
proves the rebuild moved exactly the rows the coding says it moves. It exports
the pitch pool ONLY into the engine-artifact bundle, and it proves the export
changed nothing else. The design is
``docs/audit/2026-09-23-foul-tip-strike-three-pool-coding-plan.md`` §7.

  0. Pre-flight, with no write: the builder version, the bundle, the backup
     target, write access, the window, the source rows, the unknown codes,
     and a fresh batted-ball join that must reproduce the rollback point's
     own. The rollback point is an existing WHOLE copy (kept), else the
     bundle, whose own pitch pool must then be whole (no stray file, every
     file with its manifest's rows). The rollback point's pitch pool must
     match DuckDB's window pitch by pitch: the same pitch ids per batting
     side; the same classes, or on a re-run after a committed rebuild
     classes that differ by exactly the expected moves; and the same
     geometry and situation rows. Any other difference would fail the
     round-trip after the COMMIT, so it stops the run here. It also prints a
     DRIFT line, for information only: DuckDB's window rows against the
     bundle's manifests for the batted-ball, steal and advancement pools.
     This rebuild does not export those pools. Refreshing
     them is a separate owner decision; the next nightly ``--what pool`` or
     ``make engine-artifacts`` export would carry the refresh.
     Then open DuckDB writable. A held lock stops the run (exit 3).
  1. Snapshot ``(pitch_id, season, outcome_type)`` of the seasons into a TEMP
     table.
  2. Rebuild ``sim.pitch_pool`` for the seasons (default 2017-2026) inside ONE
     transaction.
  3. Check, still inside the transaction:
       a. the rows per season equal the snapshot's; no NULL class;
       b. the moved rows are exactly the expected rows, code by code;
       c. no moved foul tip or foul bunt (T / L / O) carries the got-away
          flag (a moved 'Q' is a real swing and miss; its flag is reported);
       d. the count chain of the window pool's plate appearances against
          real plate appearances (``pipeline/batch/pool_chain.py``
          ``chain_rates(..., pa_only=True)`` and ``label_check``): the two
          sides count the same plate appearances (``pa_group_count`` equals
          ``real_pa_rates``' ``n_pa``) and every channel is within 0.5%. The
          full-pool chain, the band centres, prints for the record;
       e. ``sim.pool_build_metadata`` reads the builder version for every season.
     A failed check rolls the transaction back, so the table keeps its old
     coding and the bundle is untouched (exit 2). Then COMMIT. The ten-season
     COMMIT takes about 195 s (peak memory 4.2 GB, measured), so with
     the checks, the copy and the export the app is down about ten to fifteen
     minutes.
  4. Copy the bundle aside (``<bundle>.pre_sim553``), or keep an existing
     whole copy as the rollback point. A copy is whole when it holds the
     bundle's files outside ``pitch_pool/`` byte for byte and its own pitch
     pool is whole by its own manifest; its pitch pool is never compared
     with the bundle's, which a failed run may have half-written. A fresh
     copy must pass the same test. Export the pitch pool ONLY
     (``build_pitch_pool_artifact``), then recompute
     ``pitch_pool/{hand}.bb_row.npy`` (each pitch's row in the batted-ball
     pool) exactly as ``build_battedball_pool_artifact`` computes it, against
     the bundle's untouched ``battedball_pool/{hand}.pitch_id.npy``. The
     batted-ball, steal and advancement pools are not written.
  5. The round-trip, graded on the export alone, so a re-run passes too:
     the exported pitch pool is whole by its own manifest; every exported
     class equals DuckDB's class for the same pitch; every class change from
     the copy to the export is that pitch's expected move from its raw code;
     the copy reads one coding (the old or the new, never a mix); each pitch
     keeps its batted ball, its geometry and its situation rows (aligned on
     ``pitch_id``); the batted-ball, steal and advancement files are
     byte-identical to the copy's. A difference fails the run (exit 4):
     roll the bundle back.
  6. Print the rollback (with the coding the copy holds, read from the copy)
     and the next run-book steps.

``--check-only`` writes nothing. It runs the pre-flight (with the drift line),
check d on the pool as it stands, and the class of every ``T`` / ``L`` / ``O``
/ ``Q`` / ``R`` / ``H`` pitch at each strike count, beside the class the new
coding gives. It exits 0 when the label check passes and 2 when it fails.

``--no-transaction`` is the fallback if the one-transaction rebuild runs out
of memory (a killed process leaves the table as it was). It rebuilds in
autocommit (the pattern of the fatigue-fit rebuild, SIM-518), and a failed check puts the snapshot's
classes and builders back. Its weak point: an error in the middle of the
builder's INSERT leaves the seasons deleted. The pre-flight's source-row and
unknown-code checks guard that.

``--seasons`` must hold the whole window. ``--force-backup`` renames aside (it
never deletes) an existing copy that is NOT whole (a partial copy, or one the
bundle has moved on from), then copies the bundle again. It refuses when the
bundle's own pitch pool is not whole (a stray file such as a killed COPY's
``tmp_L.meta.parquet``, or files whose rows disagree with the manifest): such
a bundle never becomes the rollback point. A whole copy is always kept, with
or without the flag. ``--duckdb-path`` and ``--art-dir`` point the run at
another database and bundle.

RUN
===
The app must be STOPPED: the export rewrites, in place, the bundle version
the app's workers read. (The app holds no DuckDB handle; another writer's
lock stops the run with exit 3.) From the repo root (``scripts/`` is NOT bind-mounted):

    docker compose stop app
    MSYS_NO_PATHCONV=1 docker compose run -d --rm \\
        -v "$PWD:/app" app python scripts/rebuild_pitch_pool.py
    docker logs -f <the run container>      # read to the COMPLETE / FAILED line
    docker compose up -d app

EXIT CODES
==========
  0  complete.
  1  an unexpected error before the export. The rebuild is undone, and the
     bundle is untouched.
  2  a pre-flight or pool check failed, or the bundle copy failed. Nothing was
     exported. The log says whether the DuckDB table was undone. After a
     failed copy, a partial copy may remain: re-run with ``--force-backup``
     (it cannot help when the bundle's own pitch pool is not whole).
  3  the DuckDB file is locked: the app, or another reader, is running.
  4  the export or its round-trip failed. The bundle needs the rollback the
     log prints (or, after a failed export, a re-run: it keeps the copy).
"""

from __future__ import annotations

import argparse
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
# The steps live in scripts/pitch_pool_rebuild/. A caller that loads this file by path
# (a test) may not have scripts/ on sys.path.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

from pitch_pool_rebuild.checks import (  # noqa: E402
    _check_got_away,
    _check_label,
    _check_metadata,
    _check_moves,
    _check_rows,
    _restore,
    _rollback,
    _window_moves,
)
from pitch_pool_rebuild.common import (  # noqa: E402
    ALL_SEASONS_EXPECTED_MOVES,
    ART_DIR,
    BACKUP_SUFFIX,
    DEFAULT_SEASONS,
    DUCKDB_PATH,
    EXIT_BUNDLE,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_STOPPED,
    EXPECTED_BUILDER,
    MOVE_CODES,
    WINDOW_EXPECTED_MOVES,
    _builder_versions,
    _log,
    _pool_counts,
    _raw_code_rows_sql,
    _season_list,
)
from pitch_pool_rebuild.export import (  # noqa: E402
    _backup_bundle,
    _export,
    _print_next_steps,
    _print_rollback,
    _roundtrip,
)
from pitch_pool_rebuild.preflight import _preflight  # noqa: E402

from pipeline.batch.engine_artifacts import last_n_seasons  # noqa: E402
from pipeline.batch.player_profile_computor import (  # noqa: E402
    POOL_BUILDER_VERSION,
    STRIKE_THREE_FOUL_TYPES,
    PlayerProfileComputor,
)
from pipeline.batch.pool_chain import attach_pg  # noqa: E402

# ---------------------------------------------------------------------------
# The two modes
# ---------------------------------------------------------------------------


def _code_report(con: Any, seasons: list[int], window: list[int]) -> None:
    """--check-only: the class of every MOVE_CODES pitch at each strike count,
    today and under the new coding, joined pool to source."""
    sl = _season_list(seasons)
    rows = con.execute(
        f"""
        WITH r AS ({_raw_code_rows_sql(seasons)})
        SELECT p.season, r.code, r.strikes, p.outcome_type AS pool_class, r.new_class,
               COUNT(*) AS n, COUNT(*) FILTER (WHERE r.got_away_raw) AS n_got_away
        FROM r LEFT JOIN sim.pitch_pool p
          ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
         AND p.pitch_number = r.pitch_number AND p.season IN ({sl})
        GROUP BY ALL
        """
    ).fetchall()
    table: dict[tuple[str, str, str, str], list[int]] = {}
    moves = [0, 0]
    # [window, all] moved rows the source labels got-away: the foul tips and
    # foul bunts (check c's gate) and the other codes (reported only).
    got_away_gated = [0, 0]
    got_away_other = [0, 0]
    unmatched = 0
    for season, code, strikes, pool_class, new_class, n, n_ga in rows:
        if season is None:
            unmatched += int(n)
            continue
        key = (str(code), str(strikes), str(pool_class), str(new_class))
        cell = table.setdefault(key, [0, 0])
        in_window = int(season) in window
        cell[1] += int(n)
        if in_window:
            cell[0] += int(n)
        if pool_class != new_class:
            flagged = got_away_gated if code in STRIKE_THREE_FOUL_TYPES else got_away_other
            moves[1] += int(n)
            flagged[1] += int(n_ga)
            if in_window:
                moves[0] += int(n)
                flagged[0] += int(n_ga)
    _log(
        f"  the class of every {'/'.join(MOVE_CODES)} pitch by strike count (pool = today's class; "
        f"new = the {POOL_BUILDER_VERSION} coding):"
    )
    _log(
        f"     {'code':<5}{'strikes':<9}{'pool class':<17}{'new class':<17}{'window':>9}{'all':>9}"
    )
    for key in sorted(table):
        code, strikes, pool_class, new_class = key
        win_n, all_n = table[key]
        mark = "  moves" if pool_class != new_class else ""
        _log(
            f"     {code:<5}{strikes:<9}{pool_class:<17}{new_class:<17}{win_n:>9,}{all_n:>9,}{mark}"
        )
    _log(
        f"  the rebuild would move {moves[0]:,} window rows (measured 2026-09-23: "
        f"{WINDOW_EXPECTED_MOVES:,}) and {moves[1]:,} rows in {seasons[0]}-{seasons[-1]} "
        f"(about {ALL_SEASONS_EXPECTED_MOVES:,} for 2017-2026)"
    )
    _log(
        f"  of those, {'/'.join(STRIKE_THREE_FOUL_TYPES)} rows the source labels got-away (check c "
        f"must read 0): window {got_away_gated[0]:,}, all {got_away_gated[1]:,}; other codes "
        f"(reported, not gated): window {got_away_other[0]:,}, all {got_away_other[1]:,}"
    )
    if unmatched:
        _log(f"  WARNING: {unmatched:,} source rows of these codes have no pool row")


def _check_only(args: argparse.Namespace) -> int:
    seasons = sorted(set(args.seasons))
    art_dir = args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-553 check-only (read-only; writes nothing): {args.duckdb_path} ===")
    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        _log("=== the write path's pre-flight (informational here) ===")
        problems = _preflight(con, seasons, window, art_dir, backup_dir, args.force_backup)
        for p in problems:
            _log(f"  PRE-FLIGHT: {p}")
        if not problems:
            _log("  the pre-flight is clear (the DuckDB lock is tested only on the write path)")
        _log("=== check d on the pool as it stands ===")
        ok = _check_label(con, window)
        _log("=== the codes the new coding moves ===")
        _code_report(con, seasons, window)
    finally:
        con.close()
    _log(f"SIM-553 CHECK-ONLY COMPLETE: label check {'PASS' if ok else 'FAIL'}")
    return EXIT_OK if ok else EXIT_STOPPED


def _open_writable(path: str) -> Any:
    """Open the DuckDB file writable, or return None when another process holds its lock."""
    try:
        return duckdb.connect(path)
    except duckdb.IOException as exc:
        if "lock" in str(exc).lower():
            return None
        raise


def _rebuild(args: argparse.Namespace, t0: float) -> int:
    seasons = sorted(set(args.seasons))
    duckdb_path, art_dir = args.duckdb_path, args.art_dir.rstrip("/")
    backup_dir = art_dir + BACKUP_SUFFIX
    _log(f"=== SIM-553 pitch-pool rebuild: seasons {seasons}, builder {POOL_BUILDER_VERSION} ===")
    _log("=== 0. pre-flight ===")
    if POOL_BUILDER_VERSION != EXPECTED_BUILDER:
        _log(
            f"SIM-553 REBUILD FAILED: the builder is {POOL_BUILDER_VERSION!r}, not {EXPECTED_BUILDER!r}"
        )
        return EXIT_STOPPED
    if not os.path.isfile(duckdb_path):
        # duckdb.connect would CREATE an empty database at a wrong path.
        _log(f"SIM-553 REBUILD FAILED: {duckdb_path} does not exist")
        return EXIT_STOPPED
    con = _open_writable(duckdb_path)
    if con is None:
        _log(
            "another process holds the DuckDB file: a rebuild, or a reader in another "
            "container (the app holds no handle since SIM-524). Wait for it and retry."
        )
        _log("SIM-553 REBUILD FAILED: the DuckDB file is locked")
        return EXIT_LOCKED
    try:
        attach_pg(con)
        window = last_n_seasons(con)
        problems = _preflight(con, seasons, window, art_dir, backup_dir, args.force_backup)
        if problems:
            for p in problems:
                _log(f"  PRE-FLIGHT: {p}")
            _log("SIM-553 REBUILD FAILED at the pre-flight: nothing was written")
            return EXIT_STOPPED

        _log("=== 1. snapshot ===")
        before = _pool_counts(con, seasons)
        versions = _builder_versions(con, seasons)
        rerun = [s for s in seasons if versions.get(s) == POOL_BUILDER_VERSION]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _sim553_before AS "
            "SELECT pitch_id, season, outcome_type FROM sim.pitch_pool "
            f"WHERE season IN ({_season_list(seasons)})"
        )
        _log(f"  snapshot: {sum(before.values()):,} rows")

        # One transaction by default: a failed check, an exception or a killed
        # process leaves the table exactly as it was. --no-transaction runs the
        # autocommit pattern instead and restores from the snapshot.
        use_tx = not args.no_transaction
        undo = _rollback if use_tx else _restore
        mode = "one transaction" if use_tx else "autocommit (--no-transaction)"
        _log(f"=== 2. rebuild ({POOL_BUILDER_VERSION}), {mode} ===")
        t = time.time()
        if use_tx:
            con.execute("BEGIN TRANSACTION")
        try:
            comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
            comp._conn = con
            comp._build_pitch_pool(seasons, incremental=False)
            _log(f"  rebuilt in {(time.time() - t) / 60:.1f} min")
            _log("=== 3. checks ===")
            ok_a = _check_rows(con, seasons, before)
            ok_b = _check_moves(con, seasons, window, rerun)
            ok_c = _check_got_away(con, window)
            ok_d = _check_label(con, window)
            ok_e = _check_metadata(con, seasons)
            # Read here, before the COMMIT: an error still undoes the rebuild.
            moves = _window_moves(con, window)
            _log(f"  the window's moving pitches, for the round-trip: {len(moves[0]):,}")
        except Exception:
            _log("  the rebuild or a check raised; undoing it")
            undo(con, seasons, before, versions)
            raise
        failed = [
            name
            for name, good in zip("abcde", (ok_a, ok_b, ok_c, ok_d, ok_e), strict=True)
            if not good
        ]
        if failed:
            undo(con, seasons, before, versions)
            _log(
                f"SIM-553 REBUILD FAILED at check {', '.join(failed)}: undone; the bundle is "
                "untouched. Hold the next nightly pool build too: it rebuilds on the new coding"
            )
            return EXIT_STOPPED
        if use_tx:
            con.execute("COMMIT")
        _log("  every check passed" + (": COMMIT" if use_tx else ""))
    finally:
        con.close()

    _log("=== 4. bundle copy + pitch-pool export ===")
    try:
        rollback_dir = _backup_bundle(art_dir, backup_dir, args.force_backup)
    except Exception as exc:  # noqa: BLE001 - the bundle is untouched; say so and stop
        _log(f"  the copy failed: {type(exc).__name__}: {exc}")
        _log(
            "SIM-553 REBUILD FAILED at the bundle copy: sim.pitch_pool is rebuilt (committed); the "
            f"bundle is untouched. A partial copy may remain at {backup_dir}. Fix the cause, then "
            "re-run with --force-backup (it renames that copy aside, never deletes it, and copies "
            "again). The rebuild repeats with no class move. If the reason above says the "
            "bundle's own pitch pool is not whole, --force-backup cannot help: put a whole copy "
            "of the pre-rebuild bundle back first."
        )
        return EXIT_STOPPED
    try:
        t = time.time()
        _export(duckdb_path, art_dir, window)
        _log(f"  exported in {time.time() - t:.0f} s")
    except Exception:
        traceback.print_exc(file=sys.stdout)
        _print_rollback(art_dir, rollback_dir, moves)
        _log(
            "SIM-553 REBUILD FAILED at the export: the bundle's pitch_pool/ may be half-written. "
            f"Roll it back (above), or re-run: the re-run keeps {rollback_dir} as the rollback "
            "point and exports again"
        )
        return EXIT_BUNDLE
    _log("=== 5. round-trip ===")
    try:
        ok = _roundtrip(duckdb_path, art_dir, rollback_dir, window, moves)
    except Exception:
        traceback.print_exc(file=sys.stdout)
        _log("  the round-trip raised")
        ok = False
    if not ok:
        _print_rollback(art_dir, rollback_dir, moves)
        _log(
            "SIM-553 REBUILD FAILED at the round-trip: read the MISMATCH lines, then roll the "
            "bundle back (above)"
        )
        return EXIT_BUNDLE

    _log("=== 6. done ===")
    _print_rollback(art_dir, rollback_dir, moves)
    _print_next_steps()
    _log(f"SIM-553 REBUILD COMPLETE in {(time.time() - t0) / 60:.1f} min")
    return EXIT_OK


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEASONS),
        help="the pool seasons to rebuild (default 2017-2026; must hold the window)",
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="read-only: the pre-flight, check d and the per-code classes; no rebuild, no export",
    )
    ap.add_argument(
        "--force-backup",
        action="store_true",
        help=f"when <bundle>{BACKUP_SUFFIX} exists but is not whole (a partial copy, or one the "
        "bundle has moved on from), rename it aside (never delete it) and copy the bundle again, "
        "provided the bundle's own pitch pool is whole; a whole copy is always kept",
    )
    ap.add_argument(
        "--no-transaction",
        action="store_true",
        help="rebuild in autocommit (the SIM-518 pattern) and undo a failed check from the "
        "snapshot; the fallback if the one-transaction rebuild runs out of memory",
    )
    ap.add_argument("--duckdb-path", default=DUCKDB_PATH)
    ap.add_argument("--art-dir", default=ART_DIR, help="the engine-artifact bundle")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    # One stream in one order: the builder's and the exporter's log lines go to
    # stdout beside this script's own lines (force: a pipeline import may have
    # configured logging already).
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
            f"SIM-553 {'CHECK-ONLY' if args.check_only else 'REBUILD'} FAILED: {type(exc).__name__}: {exc}"
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
