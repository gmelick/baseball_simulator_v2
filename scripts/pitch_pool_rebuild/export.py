"""Steps 4 to 6: the bundle copy, the pitch-pool export, the round-trip and the
rollback text."""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

import duckdb
import numpy as np

from pipeline.batch.engine_artifacts import build_pitch_pool_artifact

from .common import _ROOT, HANDS, UNCHANGED_POOLS, _log, _npy_rows, _pq, _season_list, _sha256
from .preflight import _copy_problem, _join_bb_row, _pitch_pool_problem, _pitch_pool_strays

# ---------------------------------------------------------------------------
# The bundle: copy, export, round-trip
# ---------------------------------------------------------------------------


def _backup_bundle(art_dir: str, backup_dir: str, force: bool) -> str:
    """Copy the bundle aside, and return the rollback point (``backup_dir``).

    A whole copy from an earlier run of this rebuild (:func:`_copy_problem`)
    IS the rollback point, with or without ``--force-backup``, and the bundle
    is never copied over it: after a failure past the COMMIT the bundle may
    hold the new coding or a half-written export. ``--force-backup`` only
    renames aside (never deletes) a copy that is NOT whole. The bundle
    becomes the rollback point only when its own pitch pool is whole
    (:func:`_pitch_pool_problem`); the fresh copy must then pass the same
    test as a kept one.
    """
    if os.path.exists(backup_dir):
        why = _copy_problem(art_dir, backup_dir)
        if why is None:
            strays = _pitch_pool_strays(art_dir)
            _log(
                f"  kept {backup_dir} as the rollback point: it is whole (the bundle's files "
                "outside pitch_pool/, byte for byte, and a pitch pool whole by its own manifest); "
                "the bundle is NOT copied over it"
                + (f". The bundle's stray files, not counted: {strays}" if strays else "")
            )
            return backup_dir
        if not force:
            raise RuntimeError(
                f"{backup_dir} exists and cannot be the rollback point ({why}); "
                "re-run with --force-backup"
            )
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is not None:
            raise RuntimeError(
                f"{backup_dir} cannot be the rollback point ({why}), and the bundle cannot replace "
                f"it: its own pitch pool is not whole ({why_bundle}). Nothing was renamed or copied"
            )
        aside = f"{backup_dir}.{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(backup_dir, aside)
        _log(
            f"  {backup_dir} cannot be the rollback point ({why}); moved it to {aside} (not deleted)"
        )
    else:
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is not None:
            raise RuntimeError(
                f"the bundle cannot become the rollback point: its own pitch pool is not whole "
                f"({why_bundle}), and no copy exists. Nothing was copied"
            )
    t = time.time()
    shutil.copytree(art_dir, backup_dir, symlinks=True)
    _log(f"  copied {art_dir} -> {backup_dir} in {time.time() - t:.0f} s")
    why = _copy_problem(art_dir, backup_dir)
    if why is not None:
        raise RuntimeError(f"the fresh copy {backup_dir} is not whole ({why})")
    _log(f"  the fresh copy is whole by the kept copy's test ({time.time() - t:.0f} s in all)")
    return backup_dir


def _export(duckdb_path: str, art_dir: str, window: list[int]) -> None:
    """Step 4: export the pitch pool ONLY, then recompute its batted-ball join.

    ``build_pitch_pool_artifact`` (the nightly export's own function) writes
    ``pitch_pool/`` from DuckDB, read-only. The rebuild re-inserts the rows,
    so their file order moves, and ``pitch_pool/{hand}.bb_row.npy`` (each
    pitch's row in the batted-ball pool) must move with them. This step
    recomputes it exactly as ``build_battedball_pool_artifact`` computes it
    (:func:`_join_bb_row`), against the bundle's untouched
    ``battedball_pool/{hand}.pitch_id.npy``. The step writes no batted-ball,
    steal or advancement pool: DuckDB's steal and advancement pools can run
    ahead of the bundle (the drift line), and a refresh of them is a separate
    owner decision. The round-trip proves all three untouched.
    """
    ro = duckdb.connect(duckdb_path, read_only=True)
    try:
        counts = build_pitch_pool_artifact(ro, art_dir, window)
        for hand in ("L", "R"):
            bb_row = _join_bb_row(
                ro,
                os.path.join(art_dir, "pitch_pool", f"{hand}.meta.parquet"),
                os.path.join(art_dir, "battedball_pool", f"{hand}.pitch_id.npy"),
            )
            np.save(os.path.join(art_dir, "pitch_pool", f"{hand}.bb_row.npy"), bb_row)
            _log(
                f"  pitch_pool[{hand}]: {counts[hand]:,} rows exported; bb_row recomputed "
                f"({int((bb_row >= 0).sum()):,} pitches link a batted ball)"
            )
    finally:
        ro.close()


def _npy_equal(a_path: str, b_path: str) -> bool:
    a = np.load(a_path, mmap_mode="r")
    b = np.load(b_path, mmap_mode="r")
    if a.shape != b.shape or a.dtype != b.dtype:
        return False
    return bool(np.array_equal(a, b, equal_nan=bool(np.issubdtype(a.dtype, np.floating))))


def _columns(mem: Any, path: str) -> list[tuple[str, str]]:
    return [
        (str(r[0]), str(r[1]))
        for r in mem.execute(f"DESCRIBE SELECT * FROM {_pq(path)}").fetchall()
    ]


def _parquet_equal(mem: Any, a_path: str, b_path: str) -> tuple[bool, str]:
    """Row-for-row equality of two parquet files, in file order."""
    cols = _columns(mem, a_path)
    if cols != _columns(mem, b_path):
        return False, "the columns differ"
    cond = " OR ".join(f'a."{c}" IS DISTINCT FROM b."{c}"' for c, _t in cols) or "FALSE"
    diff = mem.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE a.file_row_number IS NULL OR b.file_row_number IS NULL
                                OR {cond})
        FROM read_parquet('{a_path}', file_row_number = true) a
        FULL OUTER JOIN read_parquet('{b_path}', file_row_number = true) b
          ON a.file_row_number = b.file_row_number
        """
    ).fetchone()[0]
    if int(diff) == 0:
        return True, "the same rows in the same order"
    only_a = mem.execute(
        f"SELECT COUNT(*) FROM (SELECT * FROM {_pq(a_path)} EXCEPT ALL SELECT * FROM {_pq(b_path)})"
    ).fetchone()[0]
    only_b = mem.execute(
        f"SELECT COUNT(*) FROM (SELECT * FROM {_pq(b_path)} EXCEPT ALL SELECT * FROM {_pq(a_path)})"
    ).fetchone()[0]
    if int(only_a) == 0 and int(only_b) == 0:
        return False, f"the same rows in a DIFFERENT order ({diff:,} positions differ)"
    return (
        False,
        f"{diff:,} rows differ ({only_a:,} only in the copy, {only_b:,} only in the export)",
    )


def _compare_pool_dir(mem: Any, old_dir: str, new_dir: str) -> bool:
    """One pool the export does not write, against the rollback point, file by file.

    Every file must be byte-identical to the copy's, or equal in content (the
    same array, the same parquet rows in the same order, the same JSON).
    """
    name = os.path.basename(new_dir)
    old_files = sorted(f for f in os.listdir(old_dir) if os.path.isfile(os.path.join(old_dir, f)))
    new_files = sorted(f for f in os.listdir(new_dir) if os.path.isfile(os.path.join(new_dir, f)))
    ok = old_files == new_files
    if not ok:
        _log(
            f"  {name}: the file lists differ — only in the copy "
            f"{sorted(set(old_files) - set(new_files))}, only in the export "
            f"{sorted(set(new_files) - set(old_files))}  MISMATCH"
        )
    identical = same_content = 0
    for f in sorted(set(old_files) & set(new_files)):
        a, b = os.path.join(old_dir, f), os.path.join(new_dir, f)
        if _sha256(a) == _sha256(b):
            identical += 1
            continue
        if f.endswith(".npy"):
            equal, detail = _npy_equal(a, b), "array content"
        elif f.endswith(".parquet"):
            equal, detail = _parquet_equal(mem, a, b)
        elif f.endswith(".json"):
            with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
                equal, detail = json.load(fa) == json.load(fb), "json content"
        else:
            equal, detail = False, "no content comparison for this file type"
        if equal:
            same_content += 1
        else:
            ok = False
            _log(f"  {name}/{f}: the bytes differ; {detail}  MISMATCH")
    _log(
        f"  {name}: {len(new_files)} files — {identical} byte-identical, {same_content} the "
        f"same content  {'OK' if ok else 'MISMATCH'}"
    )
    return ok


def _file_order_ids(mem: Any, path: str) -> np.ndarray:
    d = mem.execute(
        f"SELECT pitch_id FROM read_parquet('{path}', file_row_number = true) ORDER BY file_row_number"
    ).fetchnumpy()
    return np.asarray(np.ma.filled(d["pitch_id"], -1), dtype=np.int64)


def _coding_label(n_moving: int, n_old: int, n_new: int) -> tuple[str, bool]:
    """The coding a pitch pool reads, from its window pitches that move.

    ``n_moving`` counts those pitches in the pool, ``n_old`` the ones on
    their old class and ``n_new`` the ones on their new class. Returns the
    label and whether the pool reads ONE coding (never a mix).
    """
    if n_moving == 0:
        return "no pitch moves in the window: the two codings agree", True
    if n_old == n_moving:
        return "the OLD coding (the rollback restores it)", True
    if n_new == n_moving:
        return (
            "the NEW coding. WARNING: the rollback point already holds the new coding, so a "
            "rollback does NOT restore the old one",
            True,
        )
    return (
        f"a MIX of the two codings ({n_old:,} moving pitches on the old class, {n_new:,} on the "
        f"new, {n_moving - n_old - n_new:,} on neither)",
        False,
    )


def _copy_coding(rollback_dir: str, moves: tuple[list[int], list[str]]) -> str:
    """The coding the rollback point's pitch pool holds, read from its files now.

    The rollback text names it on every path, the export-failure path too,
    where no round-trip runs.
    """
    ids, raw_moves = moves
    mem = duckdb.connect()
    try:
        mem.execute(
            "CREATE TEMP TABLE _sim553_mv AS "
            "SELECT UNNEST(?::BIGINT[]) AS pitch_id, UNNEST(?::VARCHAR[]) AS raw_move",
            [ids, raw_moves],
        )
        found = n_old = n_new = 0
        for hand in HANDS:
            meta = os.path.join(rollback_dir, "pitch_pool", f"{hand}.meta.parquet")
            row = mem.execute(
                "SELECT COUNT(*), "
                "COUNT(*) FILTER (WHERE c.outcome_type = split_part(m.raw_move, '>', 1)), "
                "COUNT(*) FILTER (WHERE c.outcome_type = split_part(m.raw_move, '>', 2)) "
                f"FROM {_pq(meta)} c JOIN _sim553_mv m ON m.pitch_id = c.pitch_id"
            ).fetchone()
            f, o, n = row if row is not None else (0, 0, 0)
            found, n_old, n_new = found + int(f), n_old + int(o), n_new + int(n)
    except Exception as exc:  # noqa: BLE001 - the rollback text must still print
        return f"NOT READ ({type(exc).__name__}: {exc}); read the copy before you roll back"
    finally:
        mem.close()
    label, _one = _coding_label(found, n_old, n_new)
    if found != len(ids):
        label += f" ({found:,} of the {len(ids):,} moving window pitches are in the copy)"
    return label


def _roundtrip_pitch_pool(
    mem: Any,
    duckdb_path: str,
    art_dir: str,
    rollback_dir: str,
    window: list[int],
    moves: tuple[list[int], list[str]],
) -> bool:
    """The exported pitch pool, graded on its own.

    ``moves`` holds every window pitch whose raw code moves under the new
    coding, with its move ``'old>new'`` (:func:`_window_moves`). The grade
    never reads this run's DuckDB difference, so a first run and a re-run
    (the copy on the old coding or on the new) grade the same way.

    Hard, per batting side:
      * the exported pitch pool is whole by its own manifest, with no stray
        file (:func:`_pitch_pool_problem`, the rollback point's own test);
      * every exported pitch has DuckDB's class for the same ``pitch_id``
        (a full join: no pitch missing on either side, no class different);
      * every per-row file has the meta's rows;
      * the export and the copy hold the same ``pitch_id`` set;
      * every class change from the copy to the export is that pitch's
        expected move, and every pitch with a move carries its new class;
      * each pitch keeps its batted-ball row (``bb_row`` aligned on
        ``pitch_id``), and the linked pitches number the batted-ball
        manifest's ``pitch_join``;
      * each pitch keeps its geometry and situation rows (``geom`` and
        ``sit`` aligned on ``pitch_id``). The rebuild leaves these columns as
        they are (the pre-flight reads them row for row against DuckDB), so
        a difference means the copy or the export is misaligned.
    Hard, over both sides: the copy reads ONE coding, every moving pitch on
    its old class or every one on its new class, never a mix.
    Reported only: the copy-to-export class pairs, and any other meta column
    that differs from the copy (the rebuild recomputes those columns from
    ``raw.pitches``; checks a-e read the table against the source).
    """
    wl = _season_list(window)
    ids, raw_moves = moves
    mem.execute(
        "CREATE OR REPLACE TEMP TABLE _sim553_exp AS "
        "SELECT UNNEST(?::BIGINT[]) AS pitch_id, UNNEST(?::VARCHAR[]) AS raw_move",
        [ids, raw_moves],
    )
    try:
        with open(
            os.path.join(art_dir, "battedball_pool", "manifest.json"), encoding="utf-8"
        ) as fh:
            pitch_join = {str(k): int(v) for k, v in json.load(fh).get("pitch_join", {}).items()}
    except (OSError, ValueError):
        pitch_join = {}
    new_dir = os.path.join(art_dir, "pitch_pool")
    old_dir = os.path.join(rollback_dir, "pitch_pool")
    whole = _pitch_pool_problem(art_dir)
    ok = whole is None
    _log(
        "  the exported pitch pool by its own manifest: "
        + ("whole, no stray file  OK" if whole is None else f"{whole}  MISMATCH")
    )
    pairs: dict[tuple[str, str], int] = {}
    n_meta: dict[str, int] = {}
    n_expected = copy_old = copy_new = 0
    mem.execute(f"ATTACH '{duckdb_path}' AS sim553_db (READ_ONLY)")
    try:
        for hand in ("L", "R"):
            new_meta = os.path.join(new_dir, f"{hand}.meta.parquet")
            old_meta = os.path.join(old_dir, f"{hand}.meta.parquet")
            duck_rows = (
                "SELECT pitch_id, outcome_type FROM sim553_db.sim.pitch_pool "
                f"WHERE season IN ({wl}) AND stand = '{hand}'"
            )
            counts = {
                str(c): int(n)
                for c, n in mem.execute(
                    f"SELECT outcome_type, COUNT(*) FROM {_pq(new_meta)} GROUP BY 1"
                ).fetchall()
            }
            duck = {
                str(c): int(n)
                for c, n in mem.execute(
                    f"SELECT outcome_type, COUNT(*) FROM ({duck_rows}) GROUP BY 1"
                ).fetchall()
            }
            _log(f"  pitch_pool[{hand}] classes, export: {dict(sorted(counts.items()))}")
            _log(f"  pitch_pool[{hand}] classes, DuckDB: {dict(sorted(duck.items()))}")
            n_meta[hand] = sum(counts.values())
            for part in ("geom", "sit", "bb_row"):
                rows = _npy_rows(os.path.join(new_dir, f"{hand}.{part}.npy"))
                good = rows == n_meta[hand]
                ok = ok and good
                if not good:
                    _log(
                        f"  pitch_pool[{hand}].{part}.npy: {rows:,} rows, the meta "
                        f"{n_meta[hand]:,}  MISMATCH"
                    )
            # (i) Every exported pitch against DuckDB, by pitch_id.
            only_duck, only_export, differ = mem.execute(
                "SELECT COUNT(*) FILTER (WHERE n.pitch_id IS NULL), "
                "COUNT(*) FILTER (WHERE d.pitch_id IS NULL), "
                "COUNT(*) FILTER (WHERE n.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL "
                "AND n.outcome_type IS DISTINCT FROM d.outcome_type) "
                f"FROM {_pq(new_meta)} n FULL OUTER JOIN ({duck_rows}) d ON n.pitch_id = d.pitch_id"
            ).fetchone()
            good = int(only_duck) == 0 and int(only_export) == 0 and int(differ) == 0
            ok = ok and good
            _log(
                f"  (i) pitch_pool[{hand}] against DuckDB by pitch_id: {int(only_duck):,} only in "
                f"DuckDB, {int(only_export):,} only in the export, {int(differ):,} with another "
                f"class  {'OK' if good else 'MISMATCH'}"
            )
            # Against the copy, row by row on pitch_id (the rebuild re-inserts
            # the rows, so the file order moves; the pitch_id does not).
            new_cols = [c for c, _t in _columns(mem, new_meta)]
            old_cols = {c for c, _t in _columns(mem, old_meta)}
            common = [
                c for c in new_cols if c in old_cols and c not in ("pitch_id", "outcome_type")
            ]
            diff_sql = "".join(
                f', COUNT(*) FILTER (WHERE o."{c}" IS DISTINCT FROM n."{c}")' for c in common
            )
            row = mem.execute(
                f"SELECT COUNT(*) FILTER (WHERE o.pitch_id IS NULL), "
                f"COUNT(*) FILTER (WHERE n.pitch_id IS NULL){diff_sql} "
                f"FROM {_pq(old_meta)} o FULL OUTER JOIN {_pq(new_meta)} n "
                "ON o.pitch_id = n.pitch_id"
            ).fetchone()
            only_new, only_old = int(row[0]), int(row[1])
            good = only_new == 0 and only_old == 0
            ok = ok and good
            _log(
                f"  pitch_pool[{hand}] against the copy: {only_old:,} pitch_ids only in the copy, "
                f"{only_new:,} only in the export  {'OK' if good else 'MISMATCH'}"
            )
            drift = {c: int(v) for c, v in zip(common, row[2:], strict=True) if int(v)}
            # (ii) Every copy-to-export class change is that pitch's expected
            # move; every pitch with a move carries its new class.
            unexplained, wrong, n_exp, not_new, c_old, c_new = mem.execute(
                "SELECT COUNT(*) FILTER (WHERE o.outcome_type <> n.outcome_type "
                "AND e.raw_move IS NULL), "
                "COUNT(*) FILTER (WHERE o.outcome_type <> n.outcome_type AND e.raw_move "
                "IS NOT NULL AND e.raw_move <> (o.outcome_type || '>' || n.outcome_type)), "
                "COUNT(e.pitch_id), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND n.outcome_type IS DISTINCT FROM split_part(e.raw_move, '>', 2)), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND o.outcome_type = split_part(e.raw_move, '>', 1)), "
                "COUNT(*) FILTER (WHERE e.pitch_id IS NOT NULL "
                "AND o.outcome_type = split_part(e.raw_move, '>', 2)) "
                f"FROM {_pq(old_meta)} o JOIN {_pq(new_meta)} n ON o.pitch_id = n.pitch_id "
                "LEFT JOIN _sim553_exp e ON e.pitch_id = o.pitch_id"
            ).fetchone()
            good = int(unexplained) == 0 and int(wrong) == 0 and int(not_new) == 0
            ok = ok and good
            _log(
                f"  (ii) pitch_pool[{hand}] class changes from the copy: {int(unexplained):,} with "
                f"no expected move, {int(wrong):,} unlike the expected move; {int(not_new):,} of "
                f"{int(n_exp):,} moving pitches without their new class in the export  "
                f"{'OK' if good else 'MISMATCH'}"
            )
            n_expected += int(n_exp)
            copy_old += int(c_old)
            copy_new += int(c_new)
            for o, n, k in mem.execute(
                f"SELECT o.outcome_type, n.outcome_type, COUNT(*) FROM {_pq(old_meta)} o "
                f"JOIN {_pq(new_meta)} n ON o.pitch_id = n.pitch_id "
                "WHERE o.outcome_type <> n.outcome_type GROUP BY 1, 2"
            ).fetchall():
                pairs[(str(o), str(n))] = pairs.get((str(o), str(n)), 0) + int(k)
            # Each pitch keeps its batted ball.
            pid_old = _file_order_ids(mem, old_meta)
            pid_new = _file_order_ids(mem, new_meta)
            o_ord = np.argsort(pid_old, kind="stable")
            n_ord = np.argsort(pid_new, kind="stable")
            if np.array_equal(pid_old[o_ord], pid_new[n_ord]):
                for part in ("bb_row", "geom", "sit"):
                    a = np.load(os.path.join(old_dir, f"{hand}.{part}.npy"))
                    b = np.load(os.path.join(new_dir, f"{hand}.{part}.npy"))
                    if len(a) != len(pid_old) or len(b) != len(pid_new) or a.shape != b.shape:
                        ok = False
                        _log(
                            f"  pitch_pool[{hand}].{part}.npy: shape {a.shape} in the copy, "
                            f"{b.shape} in the export, against {len(pid_new):,} meta rows: the "
                            "rows cannot be aligned on pitch_id  MISMATCH"
                        )
                        del a, b
                        continue
                    a, b = a[o_ord], b[n_ord]
                    if not np.array_equal(a, b):
                        ok = False
                        if part == "bb_row":
                            _log(
                                f"  pitch_pool[{hand}].bb_row: a pitch points at a different "
                                "batted ball  MISMATCH"
                            )
                        else:
                            n_bad = int((a != b).any(axis=1).sum()) if a.ndim == 2 else -1
                            _log(
                                f"  pitch_pool[{hand}].{part}.npy: {n_bad:,} pitches hold other "
                                "values in the export than in the copy (aligned on pitch_id). "
                                "The rebuild leaves these columns as they are, so the copy or "
                                "the export is misaligned  MISMATCH"
                            )
                    del a, b
            else:
                ok = False
                _log(
                    f"  pitch_pool[{hand}]: the per-row files cannot be aligned on pitch_id  MISMATCH"
                )
            linked = int((np.load(os.path.join(new_dir, f"{hand}.bb_row.npy")) >= 0).sum())
            if hand in pitch_join:
                good = linked == pitch_join[hand]
                ok = ok and good
                _log(
                    f"  pitch_pool[{hand}].bb_row: {linked:,} pitches link a batted ball; the "
                    f"batted-ball manifest's pitch_join {pitch_join[hand]:,}  "
                    f"{'OK' if good else 'MISMATCH'}"
                )
            if drift:
                _log(
                    f"  pitch_pool[{hand}] WARNING (reported, not gated): meta columns other than "
                    "the class differ from the copy on these pitches; the rebuild recomputes them "
                    f"from raw.pitches: {drift}"
                )
    finally:
        mem.execute("DETACH sim553_db")
    # (iii) The copy reads one coding.
    good = n_expected == len(ids)
    ok = ok and good
    if not good:
        _log(
            f"  (iii) {len(ids):,} window pitches move under the new coding, and the export and "
            f"the copy hold {n_expected:,} of them  MISMATCH"
        )
    label, one_coding = _coding_label(n_expected, copy_old, copy_new)
    ok = ok and one_coding
    _log(f"  (iii) the copy reads {label}{'' if one_coding else '  MISMATCH'}")
    _log(
        "  pitch_pool class changes from the copy to the export (for the record): "
        f"{dict(sorted(pairs.items()))}"
    )
    with open(os.path.join(new_dir, "manifest.json"), encoding="utf-8") as fh:
        new_manifest = json.load(fh)
    with open(os.path.join(old_dir, "manifest.json"), encoding="utf-8") as fh:
        old_manifest = json.load(fh)
    good = (
        [int(s) for s in new_manifest.get("seasons", [])] == window
        and new_manifest.get("counts") == n_meta
        and old_manifest.get("counts") == n_meta
    )
    ok = ok and good
    _log(
        f"  pitch_pool manifest: seasons {new_manifest.get('seasons')} counts "
        f"{new_manifest.get('counts')} (the copy {old_manifest.get('counts')}) "
        f"{'OK' if good else 'MISMATCH'}"
    )
    return ok


def _roundtrip(
    duckdb_path: str,
    art_dir: str,
    rollback_dir: str,
    window: list[int],
    moves: tuple[list[int], list[str]],
) -> bool:
    """Step 5: the exported pitch pool (:func:`_roundtrip_pitch_pool`), then the
    pools the export does not write, file by file against the rollback point."""
    mem = duckdb.connect()  # in-memory: the bundle's files, DuckDB attached read-only
    try:
        ok = _roundtrip_pitch_pool(mem, duckdb_path, art_dir, rollback_dir, window, moves)
        for sub in UNCHANGED_POOLS:
            ok = (
                _compare_pool_dir(mem, os.path.join(rollback_dir, sub), os.path.join(art_dir, sub))
                and ok
            )
    finally:
        mem.close()
    return ok


def _print_rollback(art_dir: str, rollback_dir: str, moves: tuple[list[int], list[str]]) -> None:
    rejected = f"{art_dir}.sim553_rejected.{time.strftime('%Y%m%d-%H%M%S')}"
    _log(f"ROLLBACK (the bundle): stop the app, move {rollback_dir} back, start the app:")
    _log("    docker compose stop app")
    _log(
        "    MSYS_NO_PATHCONV=1 docker compose run --rm -T app sh -c "
        f"'mv {art_dir} {rejected} && mv {rollback_dir} {art_dir}'"
    )
    _log("    docker compose up -d app")
    _log(
        f"  The app then draws on the coding the copy holds, read from the copy now: "
        f"{_copy_coding(rollback_dir, moves)}."
    )
    _log(
        "  sim.pitch_pool in DuckDB keeps the new coding. The app's boot loads that table only "
        "into the pitch-to-pitch engine, which no route and no sim draw uses."
    )
    _log(
        "  HOLD every pool export until sim.pitch_pool is rebuilt on the coding you keep: "
        "make engine-artifacts, the nightly chain (scripts/nightly_ingest.sh step 3, also run "
        "by the scheduler) and any engine_artifacts --what pool or --what all. Each one exports "
        "the table's coding again and undoes this rollback."
    )
    _log(
        "  To make the rollback last: revert the code, then rebuild sim.pitch_pool for every "
        "season (2017-2026), then export. A nightly run rebuilds the current season only and "
        "leaves a mixed pool."
    )


def _smoke_games() -> str:
    """The before smoke's ten games (``results/sim553/sim553_smoke_before.json``), for the after smoke."""
    try:
        with open(_ROOT / "scripts" / "sim553_smoke_before.json", encoding="utf-8") as fh:
            before = json.load(fh)
        games = " ".join(str(int(g)) for g in before["game_pks"])
        return f"--iters {int(before['iters_per_game'])} {games}"
    except (OSError, ValueError, KeyError, TypeError):
        return "--iters <the before smoke's> <the ten game_pks of results/sim553/sim553_smoke_before.json>"


def _print_next_steps() -> None:
    _log("NEXT (the run book, design §10), from the repo root:")
    _log(
        "  4. docker compose up -d app — the boot log reads build_all_engines: 11/11 and the "
        "bundle loads"
    )
    _log("  5. the census on the pool window, strict (exit 1 on a label-check FAIL):")
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD:/app" '
        "app python scripts/pool_window_census.py --windows W1 --strict"
    )
    _log(
        "     then restate the four band centres (K_PA, BB_PA, HBP_PA, PITCHES_PA) in "
        "tests/acceptance/bands.py from the census's FULL-POOL chain (every pool row, which the "
        "simulator draws), NOT the label check's plate-appearance chain; expected about "
        "0.2262 / 0.0829 / 0.0112 / 3.906"
    )
    _log(
        "     then the standing label check (SIM_ACCEPTANCE=1: a missing store fails, never skips):"
    )
    _log(
        "       MSYS_NO_PATHCONV=1 docker compose run --rm -T -e SIM_ACCEPTANCE=1 "
        '-v "$PWD/tests:/app/tests" -v "$PWD/pipeline:/app/pipeline" '
        '-v "$PWD/pyproject.toml:/app/pyproject.toml" app python -m pytest '
        "tests/acceptance/test_pool_label_check.py -p no:cacheprovider"
    )
    _log(
        "  6. the balanced set's score on the rebuilt pool: the chosen dates must be the set's in "
        "tests/acceptance/bands.py, and every channel's deviation inside 0.6% (strikeouts about "
        "+0.30%):"
    )
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD:/app" '
        "app python scripts/balanced_game_set.py --slates 3 "
        "--json-out /app/results/sim553/sim553_game_set_after.json"
    )
    _log(
        "  7. the after smoke, the before smoke's games and iterations; expected strikeouts +4 to "
        "+5% a team-game, walks -2 to -3%, runs about -3%:"
    )
    _log(
        '       MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD:/app" '
        "app python scripts/sim_stats.py --json-out /app/results/sim553/sim553_smoke_after.json "
        f"{_smoke_games()}"
    )
