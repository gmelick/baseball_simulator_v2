"""Step 0, the pre-flight: every reason the write path must not start, and the
drift line (information only)."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

import duckdb
import numpy as np

from pipeline.batch.engine_artifacts import _GEOM_COLS, _SIT_COLS, RECENCY_FLOOR_SEASONS, join_rows
from pipeline.batch.player_profile_computor import POOL_BUILDER_VERSION, SQL_OUTCOME_TYPE

from .common import (
    DRIFT_POOLS,
    EXPECTED_BUILDER,
    HANDS,
    PITCH_POOL_PARTS,
    UNCHANGED_POOLS,
    _builder_versions,
    _log,
    _npy_shape,
    _pool_counts,
    _pq,
    _raw_code_rows_sql,
    _season_list,
    _sha256,
    _source_counts,
    _sql_in,
    _tree_files,
)

# ---------------------------------------------------------------------------
# Pre-flight (no write)
# ---------------------------------------------------------------------------


def _unknown_codes(con: Any, seasons: list[int]) -> list[tuple[Any, Any, int]]:
    """Source rows the new coding gives no class (decision 3: no ELSE).

    Any such row makes the pool INSERT fail on the NOT NULL column. The
    pre-flight names them before the rebuild deletes a row.
    """
    return [
        (code, strikes, int(n))
        for code, strikes, n in con.execute(
            f"SELECT TRIM(type) AS code, strikes, COUNT(*) FROM pg.raw.pitches "
            f"WHERE data_quality_flag = FALSE AND season IN ({_season_list(seasons)}) "
            f"AND ({SQL_OUTCOME_TYPE}) IS NULL GROUP BY 1, 2 ORDER BY 3 DESC"
        ).fetchall()
    ]


def _pitch_pool_strays(root: str) -> list[str]:
    """The entries of ``root/pitch_pool/`` that no export writes.

    A killed export leaves them: DuckDB's COPY TO an existing file writes a
    temporary ``tmp_<name>`` first, and a SIGKILL during that COPY leaves it
    beside the old file (measured on DuckDB 1.5.5).
    """
    pitch_dir = os.path.join(root, "pitch_pool")
    if not os.path.isdir(pitch_dir):
        return []
    whole = {"manifest.json"} | {f"{h}.{p}" for h in HANDS for p in PITCH_POOL_PARTS}
    return sorted(set(os.listdir(pitch_dir)) - whole)


def _export_replaces(name: str) -> bool:
    """TRUE for a stray the next export replaces.

    The export's COPY TO ``<side>.meta.parquet`` writes ``tmp_<side>.meta.parquet``
    first (over any old one), then renames it over the target. So a killed
    COPY's temporary file is gone after the next export (measured 2026-09-25 on
    DuckDB 1.1.3 and 1.5.5). No other stray is replaced.
    """
    return name in {f"tmp_{h}.meta.parquet" for h in HANDS}


def _pitch_pool_problem(root: str) -> str | None:
    """Why ``root``'s pitch pool is not whole by its OWN manifest, or None.

    Whole means the manifest reads and names the two batting sides;
    each side's meta, geometry, situation and batted-ball-join files hold the
    manifest's rows (the geometry and situation files with the manifest's
    columns); and ``pitch_pool/`` holds no other file. A stray file such as
    ``tmp_L.meta.parquet`` marks a killed export. The test reads ``root``
    alone and never compares it with another bundle. It checks counts, not
    order: :func:`_rollback_point_problems` checks each row against DuckDB.
    """
    strays = _pitch_pool_strays(root)
    if strays:
        return (
            f"its pitch_pool/ holds files no export writes: {strays} (a killed export leaves these)"
        )
    pitch_dir = os.path.join(root, "pitch_pool")
    mem = duckdb.connect()
    try:
        with open(os.path.join(pitch_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        counts = {str(k): int(v) for k, v in manifest["counts"].items()}
        if set(counts) != set(HANDS):
            return f"its pitch_pool manifest names the sides {sorted(counts)}, not {list(HANDS)}"
        width = {
            "geom": len(manifest.get("geom_cols", [])),
            "sit": len(manifest.get("sit_cols", [])),
        }
        for hand in HANDS:
            n = counts[hand]
            meta = os.path.join(pitch_dir, f"{hand}.meta.parquet")
            rows = {"meta": int(mem.execute(f"SELECT COUNT(*) FROM {_pq(meta)}").fetchone()[0])}
            for part in ("geom", "sit", "bb_row"):
                shape = _npy_shape(os.path.join(pitch_dir, f"{hand}.{part}.npy"))
                rows[part] = shape[0]
                want_ndim = 1 if part == "bb_row" else 2
                if len(shape) != want_ndim or (
                    want_ndim == 2 and width[part] and shape[1] != width[part]
                ):
                    return (
                        f"its pitch_pool/{hand}.{part}.npy has shape {shape}, not the manifest's "
                        f"{n:,} rows" + (f" x {width[part]} columns" if want_ndim == 2 else "")
                    )
            short = {k: v for k, v in rows.items() if v != n}
            if short:
                return f"its pitch_pool[{hand}] does not hold the manifest's {n:,} rows: {short}"
    except Exception as exc:  # noqa: BLE001 - any read failure means the pool is not whole
        return f"its pitch pool does not read whole ({type(exc).__name__}: {exc})"
    finally:
        mem.close()
    return None


def _copy_problem(art_dir: str, backup_dir: str) -> str | None:
    """Why ``backup_dir`` cannot be the rollback point, or None when it can.

    The rebuild writes ``pitch_pool/`` only. So a copy is WHOLE when
    (1) outside ``pitch_pool/`` it holds the bundle's files, byte for byte,
    and (2) its own pitch pool is whole by its own manifest
    (:func:`_pitch_pool_problem`). The copy's pitch pool is NEVER compared
    with the bundle's: after a failure past the COMMIT the bundle's may hold
    the new coding, a half-written export or a killed COPY's temporary file,
    and a whole copy is still the rollback point. A copy that fails (1) is a
    partial copy, or one the bundle has moved on from (a later export
    rewrote another pool); a copy that fails (2) is a partial copy.
    """

    def outside(root: str) -> dict[str, int]:
        return {k: v for k, v in _tree_files(root).items() if not k.startswith("pitch_pool/")}

    bundle = outside(art_dir)
    copy = outside(backup_dir)
    if set(bundle) != set(copy):
        missing = sorted(set(bundle) - set(copy))
        extra = sorted(set(copy) - set(bundle))
        return (
            f"outside pitch_pool/ its file list differs from the bundle's ({len(missing)} missing, "
            f"e.g. {missing[:3]}; {len(extra)} extra, e.g. {extra[:3]}): a partial copy, or the "
            "bundle changed after the copy was taken"
        )
    for rel in sorted(bundle):
        if bundle[rel] != copy[rel] or _sha256(os.path.join(art_dir, rel)) != _sha256(
            os.path.join(backup_dir, rel)
        ):
            return (
                f"its {rel} differs from the bundle's: a partial copy, or the bundle changed "
                "after the copy was taken"
            )
    why = _pitch_pool_problem(backup_dir)
    if why is not None:
        return f"its own pitch pool is not whole, a partial copy: {why}"
    return None


def _join_bb_row(con: Any, pitch_meta: str, bb_pitch_ids: str) -> np.ndarray:
    """The pitch pool's batted-ball join, computed exactly as
    ``engine_artifacts.build_battedball_pool_artifact`` computes it.

    For every row of ``pitch_meta`` (the pitch pool's meta parquet, in read
    order), the row of the batted-ball pool that holds its batted ball, or
    -1: ``join_rows`` over the meta's ``pitch_id`` and the batted-ball pool's
    ``{hand}.pitch_id.npy``.
    """
    pp_pid = np.asarray(
        np.ma.filled(
            con.execute(f"SELECT pitch_id FROM read_parquet('{pitch_meta}')").fetchnumpy()[
                "pitch_id"
            ],
            -1,
        ),
        dtype=np.int64,
    )
    bb_pid = np.asarray(np.load(bb_pitch_ids), dtype=np.int64)
    return join_rows(pp_pid, bb_pid)


def _bb_row_problems(con: Any, source: str) -> list[str]:
    """A fresh join of ``source``'s pitch and batted-ball ids must reproduce its
    own ``bb_row``. That proves, before any write, that the export's recomputed
    join (step 4) is the join the full pool export writes."""
    problems: list[str] = []
    for hand in ("L", "R"):
        pitch_dir = os.path.join(source, "pitch_pool")
        fresh = _join_bb_row(
            con,
            os.path.join(pitch_dir, f"{hand}.meta.parquet"),
            os.path.join(source, "battedball_pool", f"{hand}.pitch_id.npy"),
        )
        held = np.load(os.path.join(pitch_dir, f"{hand}.bb_row.npy"))
        same = held.shape == fresh.shape and bool(np.array_equal(held, fresh))
        _log(
            f"  the batted-ball join [{hand}]: a fresh join of {source}'s ids links "
            f"{int((fresh >= 0).sum()):,} of {len(fresh):,} pitches; it "
            f"{'reproduces' if same else 'DOES NOT reproduce'} the held bb_row  "
            f"{'OK' if same else 'MISMATCH'}"
        )
        if not same:
            problems.append(
                f"{source}: pitch_pool/{hand}.bb_row.npy is not the join of its own pitch and "
                "batted-ball ids, so a pitch-pool-only export cannot keep the join in step "
                "(an owner decision: export every pool)"
            )
    return problems


def _rollback_point_problems(con: Any, source: str, window: list[int]) -> list[str]:
    """The rollback point's pitch pool against DuckDB's window, pitch by pitch.

    ``source`` is the rollback point: the kept copy on a re-run, else the
    bundle. The export writes DuckDB's window, and the round-trip compares
    it with the rollback point after the COMMIT. So every difference the
    round-trip would find stops the run here, before a write:

      * the pitch ids, per batting side: the same set;
      * the classes: the same, EXCEPT the expected state on a re-run after a
        committed rebuild. There every window season reads the running
        builder in DuckDB, the rollback point reads the old coding, and the
        two differ by exactly the window's expected moves: every moving
        pitch is on its old class in the rollback point and on its new class
        in DuckDB, and no other pitch differs;
      * the geometry and situation files: DuckDB's values for the same
        pitch, row for row, with the columns the export writes. The coding
        never changes them, so a difference is a misaligned or stale file.
    """
    problems: list[str] = []
    wl = _season_list(window)
    hands = _sql_in(HANDS)
    pitch_dir = os.path.join(source, "pitch_pool")
    versions = _builder_versions(con, window)
    duck_new = all(versions.get(s) == EXPECTED_BUILDER for s in window)
    src_sql = " UNION ALL ".join(
        f"SELECT '{h}' AS hand, pitch_id, outcome_type "
        f"FROM {_pq(os.path.join(pitch_dir, f'{h}.meta.parquet'))}"
        for h in HANDS
    )
    rows = con.execute(
        f"""
        WITH s AS ({src_sql}),
        d AS (SELECT stand AS hand, pitch_id, outcome_type FROM sim.pitch_pool
              WHERE season IN ({wl}) AND stand IN {hands}),
        r AS ({_raw_code_rows_sql(window)}),
        m AS (SELECT p.pitch_id, r.expected_move AS raw_move
              FROM r JOIN sim.pitch_pool p
                ON p.game_pk = r.game_pk AND p.at_bat_number = r.at_bat_number
               AND p.pitch_number = r.pitch_number AND p.season IN ({wl}) AND p.stand IN {hands}
              WHERE r.expected_move IS NOT NULL)
        SELECT COALESCE(s.hand, d.hand) AS hand,
               COUNT(*) FILTER (WHERE s.pitch_id IS NULL),
               COUNT(*) FILTER (WHERE d.pitch_id IS NULL),
               COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL
                                AND s.outcome_type IS DISTINCT FROM d.outcome_type),
               COUNT(*) FILTER (WHERE s.pitch_id IS NOT NULL AND d.pitch_id IS NOT NULL
                                AND m.pitch_id IS NOT NULL
                                AND s.outcome_type = split_part(m.raw_move, '>', 1)
                                AND d.outcome_type = split_part(m.raw_move, '>', 2)),
               COUNT(m.pitch_id)
        FROM s FULL OUTER JOIN d ON s.pitch_id = d.pitch_id AND s.hand = d.hand
        LEFT JOIN m ON m.pitch_id = COALESCE(s.pitch_id, d.pitch_id)
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    ids_ok: dict[str, bool] = dict.fromkeys(HANDS, False)
    only_duck = only_src = differ = by_move = n_moving = 0
    for hand, od, osrc, df, bm, nm in rows:
        ids_ok[str(hand)] = int(od) == 0 and int(osrc) == 0
        only_duck, only_src = only_duck + int(od), only_src + int(osrc)
        differ, by_move, n_moving = differ + int(df), by_move + int(bm), n_moving + int(nm)
        _log(
            f"  the rollback point against DuckDB [{hand}]: {int(od):,} pitches only in DuckDB, "
            f"{int(osrc):,} only in {source}; {int(df):,} with another class "
            f"({int(bm):,} of them the expected move, of {int(nm):,} moving pitches)"
        )
    if only_duck or only_src:
        problems.append(
            f"{source}'s pitch pool and DuckDB's window hold different pitches ({only_duck:,} only "
            f"in DuckDB, {only_src:,} only in the rollback point). The export writes DuckDB's "
            "window, and the round-trip compares it with the rollback point pitch by pitch, so "
            "it would fail after the COMMIT. If the rollback point is a KEPT copy from an "
            "earlier run, it is stale: move it away by hand (mv it to a dated name; never "
            "delete it — it may be the only copy on the old coding), then re-run. If it is the "
            "bundle itself, bring the bundle's pitch pool into step with DuckDB first (a pool "
            "export: an owner decision)"
        )
    if differ == 0:
        _log(
            "  the classes: the rollback point and DuckDB read the same class on every pitch  OK"
            + (
                " (both read the new coding: the round-trip warns that a rollback keeps it)"
                if duck_new and n_moving
                else ""
            )
        )
    elif duck_new and differ == by_move == n_moving:
        _log(
            f"  the classes differ by exactly the {n_moving:,} expected moves: the rollback point "
            "reads the old coding, DuckDB the new (a re-run after a committed rebuild)  OK"
        )
    else:
        problems.append(
            f"{source}'s pitch pool and DuckDB's window disagree on {differ:,} pitch classes "
            + (
                f"({by_move:,} of them the expected move, of {n_moving:,} moving pitches; the "
                "expected state is all or none of them)"
                if duck_new
                else f"(DuckDB's window builders are {versions}, so no class may differ)"
            )
            + ". The round-trip would fail after the COMMIT"
        )
    # The geometry and the situation, row for row against DuckDB.
    try:
        with open(os.path.join(pitch_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        return problems + [f"{source}'s pitch pool manifest does not read ({exc})"]
    cols_ok = (
        list(manifest.get("geom_cols", _GEOM_COLS)) == _GEOM_COLS
        and list(manifest.get("sit_cols", _SIT_COLS)) == _SIT_COLS
    )
    if not cols_ok:
        problems.append(
            f"{source}'s pitch pool holds the columns {manifest.get('geom_cols')} / "
            f"{manifest.get('sit_cols')}, but the export writes {_GEOM_COLS} / {_SIT_COLS}"
        )
        return problems
    cols = _GEOM_COLS + _SIT_COLS
    for hand in HANDS:
        if not ids_ok[hand]:
            continue  # the rows cannot be aligned; the id stop above names it
        meta = os.path.join(pitch_dir, f"{hand}.meta.parquet")
        d = con.execute(
            f"SELECT {', '.join('p.' + c for c in cols)} "
            f"FROM read_parquet('{meta}', file_row_number = true) AS m "
            f"JOIN sim.pitch_pool p ON p.pitch_id = m.pitch_id AND p.season IN ({wl}) "
            f"AND p.stand = '{hand}' ORDER BY m.file_row_number"
        ).fetchnumpy()
        bad: dict[str, int] = {}
        for part, part_cols in (("geom", _GEOM_COLS), ("sit", _SIT_COLS)):
            # The export's own conversion (engine_artifacts.build_pitch_pool_artifact).
            want = np.nan_to_num(
                np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in part_cols], axis=1)
            ).astype(np.float32)
            held = np.load(os.path.join(pitch_dir, f"{hand}.{part}.npy"))
            if held.shape != want.shape:
                bad[part] = -1
            elif not np.array_equal(held, want):
                bad[part] = int((held != want).any(axis=1).sum())
            del want, held
        del d
        _log(
            f"  the geometry and situation [{hand}] against DuckDB, row for row: "
            + (
                "the same on every pitch  OK"
                if not bad
                else f"{bad} pitches differ (-1: another shape)  MISMATCH"
            )
        )
        if bad:
            problems.append(
                f"{source}'s pitch_pool[{hand}] geometry or situation rows differ from DuckDB's "
                f"for the same pitch ({bad} rows; -1 = another shape). The coding never changes "
                "these columns, so the file is misaligned (a killed export) or stale, and the "
                "round-trip would fail after the COMMIT"
            )
    return problems


def _bundle_problems(
    con: Any, art_dir: str, backup_dir: str, window: list[int], force: bool
) -> list[str]:
    """The bundle, its window, the rollback point, the batted-ball join and write access."""
    problems: list[str] = []
    if not os.path.isdir(os.path.join(art_dir, "pitch_pool")):
        return [f"the bundle has no pitch pool: {os.path.join(art_dir, 'pitch_pool')} is missing"]
    # The rollback point. A whole copy from an earlier run of this rebuild is
    # KEPT, with or without --force-backup: the bundle may by now hold the new
    # coding or a half-written export, so it must never be copied over the
    # copy. --force-backup replaces only a copy that is not whole, and only
    # with a bundle whose own pitch pool is whole.
    reuse = False
    exists = os.path.exists(backup_dir)
    if exists:
        why = _copy_problem(art_dir, backup_dir)
        if why is None:
            reuse = True
            _log(
                f"  the copy {backup_dir} is whole (the bundle's files outside pitch_pool/, byte "
                "for byte, and a pitch pool whole by its own manifest): the run keeps it as the "
                "rollback point (the bundle is not copied over it)"
            )
        elif force:
            _log(
                f"  the copy {backup_dir} cannot be the rollback point ({why}); --force-backup "
                "renames it aside (never deletes it) and copies the bundle, whose own pitch pool "
                "must be whole"
            )
        else:
            problems.append(
                f"the copy {backup_dir} exists but cannot be the rollback point: {why}. Pass "
                "--force-backup to rename it aside (never deleted) and copy the bundle again"
            )
    strays = _pitch_pool_strays(art_dir)
    source_whole = True
    if reuse:
        if strays:
            kept = [s for s in strays if not _export_replaces(s)]
            _log(
                f"  the bundle's pitch_pool/ holds {strays}, files no export writes (a killed "
                "export leaves these). The copy is judged on its own, so they do not count "
                "against it"
                + ("" if kept else "; the export's COPY replaces them (tmp_<side>.meta.parquet)")
            )
            if kept:
                problems.append(
                    f"the bundle's pitch_pool/ holds {kept}, which the export neither writes nor "
                    "replaces, so the exported pitch pool would not be whole. Move them out of "
                    "the bundle first"
                )
    else:
        # The bundle becomes the rollback point: its own pitch pool must be whole.
        why_bundle = _pitch_pool_problem(art_dir)
        if why_bundle is None:
            _log("  the bundle's own pitch pool is whole: the run copies it as the rollback point")
        else:
            source_whole = False
            problems.append(
                f"the bundle cannot become the rollback point: its own pitch pool is not whole "
                f"({why_bundle}). "
                + (
                    "--force-backup replaces a copy that is not whole only with a whole bundle"
                    if exists
                    else "No copy exists to roll back to"
                )
                + ". Put a whole copy of the pre-rebuild bundle back first"
            )
    # The seasons, the join and the comparison with DuckDB are read where the
    # rollback point is: the bundle's own pitch pool may be half-written on a
    # re-run.
    source = backup_dir if reuse else art_dir
    if source_whole:
        try:
            with open(os.path.join(source, "pitch_pool", "manifest.json"), encoding="utf-8") as fh:
                bundle_seasons = [int(s) for s in json.load(fh).get("seasons", [])]
        except (OSError, ValueError) as exc:
            return problems + [f"{source}'s pitch pool manifest does not read ({exc})"]
        if bundle_seasons != window:
            problems.append(
                f"{source}'s pitch pool holds seasons {bundle_seasons}, but the export would "
                f"write {window}; the export must rewrite the same window"
            )
        try:
            problems += _bb_row_problems(con, source)
        except Exception as exc:  # noqa: BLE001 - a missing or unreadable file is a stop, named
            problems.append(
                f"the batted-ball join of {source} does not read ({type(exc).__name__}: {exc})"
            )
        try:
            problems += _rollback_point_problems(con, source, window)
        except Exception as exc:  # noqa: BLE001 - an unreadable pool is a stop, named
            problems.append(
                f"the rollback point {source} does not compare with DuckDB's window "
                f"({type(exc).__name__}: {exc})"
            )
    parent = os.path.dirname(os.path.abspath(backup_dir))
    if not os.access(parent, os.W_OK):
        problems.append(f"no write access to {parent} (the backup copy goes there)")
    # The export writes pitch_pool/ only.
    d = os.path.join(art_dir, "pitch_pool")
    if not os.access(d, os.W_OK):
        problems.append(f"no write access to {d}")
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and not os.access(p, os.W_OK):
            problems.append(f"no write access to {p}")
    # The export reads the batted-ball ids; the round-trip compares the rest.
    for sub in UNCHANGED_POOLS:
        if not os.path.isdir(os.path.join(art_dir, sub)):
            problems.append(f"the bundle has no {sub}/; the round-trip needs it to compare")
    for hand in ("L", "R"):
        p = os.path.join(art_dir, "battedball_pool", f"{hand}.pitch_id.npy")
        if not os.path.isfile(p):
            problems.append(f"{p} is missing; the export joins the pitch pool to it")
    if not reuse:
        size = sum(_tree_files(art_dir).values())
        free = shutil.disk_usage(parent).free
        if free < 2 * size:
            problems.append(
                f"{free / 1e9:.1f} GB free under {parent}; the copy needs {size / 1e9:.1f} GB"
            )
    return problems


def _newer_rows(con: Any, table: str, keep: str, window: list[int], pool_dir: str) -> str:
    """The DuckDB rows dated after the bundle pool's newest game, as a phrase.

    Empty when the bundle or the table carries no game date.
    """
    try:
        newest = con.execute(
            "SELECT MAX(game_ymd) FROM read_parquet("
            f"'{os.path.join(pool_dir, '*.meta.parquet')}', union_by_name = true)"
        ).fetchone()[0]
        if newest is None:
            return ""
        n, first, last = con.execute(
            f"SELECT COUNT(*), MIN(game_date), MAX(game_date) FROM {table} "
            f"WHERE season IN ({_season_list(window)}) AND {keep} "
            f"AND CAST(strftime(game_date, '%Y%m%d') AS INTEGER) > {int(newest)}"
        ).fetchone()
    except Exception:  # noqa: BLE001 - the dates are a courtesy; the counts carry the finding
        return ""
    if not n:
        return f"; the bundle's newest game {newest}, none newer in DuckDB"
    return f"; {int(n):,} DuckDB rows are newer than the bundle's newest game ({newest}): the games of {first} to {last}"


def _pool_drift(con: Any, art_dir: str, window: list[int]) -> None:
    """INFORMATION, never a stop: DuckDB's window rows against the bundle's
    manifests, for the pools this rebuild leaves alone (``DRIFT_POOLS``).

    DuckDB's steal and advancement pools can run ahead of the bundle (a later
    pool build that no export carried). The pitch-only export leaves those
    files as they are, so the drift does not touch this rebuild. The
    line names it so the owner can decide; the next full pool export carries it.
    """
    wl = _season_list(window)
    ahead: list[str] = []
    for sub, table, key_sql, keep in DRIFT_POOLS:
        pool_dir = os.path.join(art_dir, sub)
        try:
            with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
            bundle = {str(k): int(v) for k, v in manifest.get("counts", {}).items()}
            duck = {
                str(k): int(n)
                for k, n in con.execute(
                    f"SELECT {key_sql}, COUNT(*) FROM {table} "
                    f"WHERE season IN ({wl}) AND {keep} GROUP BY 1"
                ).fetchall()
            }
        except Exception as exc:  # noqa: BLE001 - information only
            _log(f"  drift. {sub}: not read ({type(exc).__name__}: {exc})")
            continue
        diff = {
            k: duck.get(k, 0) - bundle.get(k, 0)
            for k in sorted(set(bundle) | set(duck))
            if duck.get(k, 0) != bundle.get(k, 0)
        }
        bundle_seasons = [int(s) for s in manifest.get("seasons", [])]
        seasons_note = (
            "" if bundle_seasons == window else f"; the bundle's seasons {bundle_seasons}"
        )
        if not diff and not seasons_note:
            _log(f"  drift. {sub} ({table}): {sum(duck.values()):,} rows in DuckDB and the bundle")
            continue
        ahead.append(sub)
        per_key = ", ".join(f"{k} {v:+,}" for k, v in diff.items())
        _log(
            f"  drift. {sub} ({table}): DuckDB {sum(duck.values()):,} rows, the bundle "
            f"{sum(bundle.values()):,} ({sum(diff.values()):+,}; by sub-pool: {per_key or 'none'})"
            f"{_newer_rows(con, table, keep, window, pool_dir)}{seasons_note}"
        )
    if ahead:
        _log(
            f"  drift. INFORMATION, not a stop: DuckDB's {', '.join(ahead)} differ from the "
            "bundle's. This rebuild exports the pitch pool only and leaves them as they are. "
            "Refreshing them is a separate owner decision; the next nightly '--what pool' "
            "export or 'make engine-artifacts' would carry the refresh."
        )


def _preflight(
    con: Any,
    seasons: list[int],
    window: list[int],
    art_dir: str,
    backup_dir: str,
    force: bool,
) -> list[str]:
    """Every reason the write path must not start. An empty list means go.

    It also prints the drift line (information only).
    """
    problems: list[str] = []
    if POOL_BUILDER_VERSION != EXPECTED_BUILDER:
        problems.append(
            f"POOL_BUILDER_VERSION is {POOL_BUILDER_VERSION!r}; this script rebuilds for "
            f"{EXPECTED_BUILDER!r}"
        )
    _log(f"  window (the {RECENCY_FLOOR_SEASONS} newest pool seasons, the export's): {window}")
    missing = [s for s in window if s not in seasons]
    if missing:
        problems.append(
            f"the seasons {seasons} leave out the window seasons {missing}; the export and "
            "check d need the whole window on one coding"
        )
    versions = _builder_versions(con, seasons)
    _log(f"  builder per season before: {versions}")
    pool = _pool_counts(con, seasons)
    source = _source_counts(con, seasons)
    for s in seasons:
        same = pool.get(s, 0) == source.get(s, 0) and pool.get(s, 0) > 0
        _log(
            f"  {s}: pool rows {pool.get(s, 0):,}  source rows {source.get(s, 0):,}  "
            f"{'OK' if same else 'MISMATCH'}"
        )
        if not same:
            problems.append(
                f"{s}: the pool holds {pool.get(s, 0):,} rows and raw.pitches {source.get(s, 0):,}. "
                "The source changed since the last pool build, so a pitch-pool-only rebuild would "
                "leave the outcome, steal and advancement pools behind it; run the full pool chain"
            )
    unknown = _unknown_codes(con, seasons)
    for code, strikes, n in unknown:
        problems.append(
            f"{n:,} source rows with code {code!r} at {strikes} strikes get no class under "
            "SQL_OUTCOME_TYPE (no ELSE, decision 3); add the code to a code set first"
        )
    if not unknown:
        _log("  every source code maps to a class under SQL_OUTCOME_TYPE")
    problems += _bundle_problems(con, art_dir, backup_dir, window, force)
    _pool_drift(con, art_dir, window)
    return problems
