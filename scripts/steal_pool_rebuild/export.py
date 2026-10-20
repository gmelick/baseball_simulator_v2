"""Steps 5 to 7: the export, the round-trip, the steal band centres and the
rollback text."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from typing import Any

import duckdb
import numpy as np

from pipeline.batch.artifacts.pools import STEAL_PITCH_CLASS_CODE
from pipeline.batch.engine_artifacts import build_steal_pool_artifact

from .bundle import bundle_hashes, compare_hashes, steal_pool_problem
from .common import (
    _ROOT,
    FALLBACK_CENTRES,
    LABELS,
    MEASURED_FIT,
    POOL,
    TARGETS,
    _columns,
    _log,
    _ok,
    _pq,
    _season_list,
    _sql_path,
)

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
    recency-weighted and the safe share is not: the same reading gives the
    steal band centres of ``tests/acceptance/bands.py``. The pickoff outcomes
    count every row (the tags on pitch rows and the pickoff rows) over the
    pitch rows.
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
    except Exception as exc:  # noqa: BLE001 - FALLBACK_CENTRES holds the 2026-09 values
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
        "(scripts/probes/running_game_census.py; its docstring has the recipe); a stop rule "
        "it prints stops the run book for a diagnosis"
    )
    _log("  6. the ten-game smoke (scripts/sim_stats.py, 10 x 50): no collapse")
    _log(
        "  7. the lane (45 x 130, tests/acceptance/test_production_bands.py); "
        "update the steal centres in tests/acceptance/bands.py first if the lines above say MOVED"
    )
    _log("  8. close: CHANGES.md, BACKLOG.xlsx, the docs; merge before the next nightly pool build")
