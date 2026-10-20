"""The steal opportunity pools, one per target base."""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from db.introspect import duck_columns
from pipeline.batch.artifacts.common import take_view, ymd_select
from pipeline.batch.artifacts.pools import StealPool, steal_pitch_class_codes

log = logging.getLogger("engine_artifacts")


#: The steal draw's situation columns (SIM-474). `outs` and the count are the
#: hard-filter cell; `score_diff` is soft-kernelled. `inning` is deliberately
#: excluded — leverage shaping rides on the manager weight, not the pool.
_STEAL_SIT_COLS = ["count_balls", "count_strikes", "outs", "score_diff"]


def build_steal_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the per-target-base steal OPPORTUNITY pool for SIM-474.

    One row per pitch where a steal was possible (SIM-468), attempted or not —
    the `attempted` flag is the denominator the attempts-only pool lacks, so a
    single draw answers both "does the runner go" and "safe or caught".
    Sub-pools are split by target base ("2" = 1B->2B, "3" = 2B->3B), mirroring
    the per-hand split of the other pools.

    SIM-554: the meta parquet carries ``pitch_id``, the pitch's class
    (``pitch_class``; '' on a pickoff row) and ``is_pickoff_row``. The loader
    maps the class to the codes of :data:`STEAL_PITCH_CLASS_CODE`. A DuckDB
    without migration 0031 is refused before any file is written: the run book
    applies the migration before the export, and a bundle without classes
    would run the loop's old order without a word. Each target's two files are
    written under temporary names and moved into place only when both targets
    succeeded, the manifest last, so a failed export leaves the old pool whole.
    """
    missing = [c for c in ("pitch_class", "is_pickoff_row") if c not in _steal_pool_columns(con)]
    if missing:
        raise RuntimeError(
            f"sim.steal_opportunity_pool has no {', '.join(missing)} column: apply "
            "db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql before the "
            "export (SIM-554). No file was written."
        )
    pool_dir = os.path.join(out_dir, "steal_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = ", ".join(str(int(s)) for s in seasons)
    counts: dict[str, int] = {}
    # (temporary path, final path) of every file written, moved into place last.
    staged: list[tuple[str, str]] = []
    try:
        _write_steal_pool_targets(con, pool_dir, season_list, seasons, counts, staged)
        for tmp, final in staged:
            os.replace(tmp, final)
    finally:
        for tmp, _final in staged:
            if os.path.exists(tmp):
                os.remove(tmp)
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"seasons": seasons, "counts": counts, "sit_cols": _STEAL_SIT_COLS}, fh, indent=2)
    return counts


def _steal_pool_columns(con: duckdb.DuckDBPyConnection) -> set[str]:
    """SIM-554: the columns of sim.steal_opportunity_pool in this DuckDB file
    (an attached Postgres catalog is left out)."""
    return duck_columns(con, "sim", "steal_opportunity_pool", this_file_only=True)


def _write_steal_pool_targets(
    con: duckdb.DuckDBPyConnection,
    pool_dir: str,
    season_list: str,
    seasons: list[int],
    counts: dict[str, int],
    staged: list[tuple[str, str]],
) -> None:
    """SIM-554: write each target's sit and meta files under temporary names
    (``tmp_<target>.*``) and record them in ``staged``; the caller moves them
    into place once every target succeeded."""
    for target in ("2", "3"):
        w = f"target_base = {target} AND season IN ({season_list})"
        # SIM-554: both queries sort by pitch_id. The sit rows and the meta rows
        # are written by two queries, and row i of one must be row i of the
        # other. Without the sort they lined up only because two unsorted scans
        # of the same filter happened to return the same order.
        d = con.execute(
            f"SELECT {', '.join(_STEAL_SIT_COLS)} FROM sim.steal_opportunity_pool "
            f"WHERE {w} ORDER BY pitch_id"
        ).fetchnumpy()
        n = len(d[_STEAL_SIT_COLS[0]])
        sit = np.nan_to_num(
            np.stack(
                [np.ma.filled(d[c], np.nan).astype(np.float32) for c in _STEAL_SIT_COLS], axis=1
            )
        ).astype(np.float32)
        sit_tmp = os.path.join(pool_dir, f"tmp_{target}.sit.npy")
        meta_tmp = os.path.join(pool_dir, f"tmp_{target}.meta.parquet")
        staged.append((sit_tmp, os.path.join(pool_dir, f"{target}.sit.npy")))
        staged.append((meta_tmp, os.path.join(pool_dir, f"{target}.meta.parquet")))
        np.save(sit_tmp, sit)
        con.execute(
            "COPY (SELECT runner_id, pitcher_id, catcher_id, season, "
            "attempted, success, recency_weight, "
            # SIM-507: COALESCE keeps a pre-0017 DuckDB (NULL columns absent
            # from an un-migrated table would fail the SELECT outright — the
            # migration adds them; NULLs only appear on rows an old builder
            # wrote) reading as plain no-outcome rows.
            "COALESCE(pickoff_out, FALSE) AS pickoff_out, "
            "COALESCE(pickoff_advancing, FALSE) AS pickoff_advancing, "
            "COALESCE(pickoff_error, FALSE) AS pickoff_error, "
            # SIM-554 (migration 0031): the caller refused a table without these
            # two columns. A NULL class (a pickoff row, or a pitch row an old
            # builder wrote) exports as '', which the loader reads as 0;
            # is_pickoff_row tells the two apart.
            "pitch_id, COALESCE(pitch_class, '') AS pitch_class, "
            "COALESCE(is_pickoff_row, FALSE) AS is_pickoff_row "
            f"{ymd_select(con, 'sim.steal_opportunity_pool')}"
            f"FROM sim.steal_opportunity_pool WHERE {w} ORDER BY pitch_id) "
            f"TO '{meta_tmp}' (FORMAT parquet)"
        )
        counts[target] = int(n)
        log.info("steal_pool[%s]: %d rows (seasons %s)", target, n, seasons)
        # SIM-554: a pitch row with no class comes from a season that no
        # builder has rebuilt since migration 0031 (a ten-season export, say).
        # The sampler then keeps the single pre-pitch draw for the whole
        # bundle and logs it, so the export goes on and says so here.
        unclassed = con.execute(
            "SELECT COUNT(*) FROM sim.steal_opportunity_pool "
            f"WHERE {w} AND pitch_class IS NULL AND NOT COALESCE(is_pickoff_row, FALSE)"
        ).fetchone()
        if unclassed and unclassed[0]:
            log.warning(
                "steal_pool[%s]: %d pitch rows have no pitch class, so this bundle "
                "runs the single pre-pitch steal draw, not the running game on the "
                "pitch. Rebuild those seasons with builder sim554.1 or later "
                "(SIM-554) before the bundle reaches production.",
                target,
                unclassed[0],
            )


def load_steal_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, StealPool]:
    """Load the steal pools by target base; {} when the bundle has none."""
    # SIM-474: the steal opportunity pools ("2" = 1B->2B, "3" = 2B->3B).
    # Presence-gated like the batted-ball pool: {} on a legacy bundle,
    # and the sampler then stages no steals (the pre-SIM-474 behavior).
    steal_pools: dict[str, StealPool] = {}
    sp_dir = os.path.join(art_dir, "steal_pool")
    if os.path.exists(os.path.join(sp_dir, "manifest.json")):
        for target in ("2", "3"):
            meta_path = os.path.join(sp_dir, f"{target}.meta.parquet")
            # SELECT * so a pre-SIM-507 parquet (no pickoff columns)
            # still loads; the taker fills missing columns below.
            m = con.execute(f"SELECT * FROM read_parquet('{meta_path}')").fetchnumpy()

            def _sp_take(attr: str, col: str, dtype, fill, *, _t=target, _m=m) -> np.ndarray:
                # _t/_m bind the loop variables at definition time (B023).
                v = views.get(f"steal_pool.{_t}.{attr}")
                if isinstance(v, np.ndarray):
                    return v
                if col not in _m:
                    # SIM-507: a legacy bundle carries no pickoff
                    # columns — all-zero labels stage no pickoffs,
                    # the pre-SIM-507 behavior.
                    n_rows = len(_m["runner_id"])
                    return np.full(n_rows, fill, dtype=dtype)
                return np.asarray(np.ma.filled(_m[col], fill), dtype=dtype)

            # SIM-554: the class of each row's pitch. Not through
            # _sp_take: its fill would read a pre-0031 bundle as all
            # pickoff rows (code 0). No column means no classes (None),
            # and the loop then runs the single pre-pitch draw.
            pc_view = views.get(f"steal_pool.{target}.pitch_class")
            if isinstance(pc_view, np.ndarray):
                pitch_class: np.ndarray | None = pc_view
            elif "pitch_class" in m:
                pitch_class = steal_pitch_class_codes(
                    m["pitch_class"], where=f"steal_pool[{target}]"
                )
            else:
                pitch_class = None
            po_view = views.get(f"steal_pool.{target}.is_pickoff_row")
            if isinstance(po_view, np.ndarray):
                is_pickoff_row: np.ndarray | None = po_view
            elif "is_pickoff_row" in m:
                is_pickoff_row = np.asarray(np.ma.filled(m["is_pickoff_row"], False), dtype=np.int8)
            else:
                is_pickoff_row = None

            steal_pools[target] = StealPool(
                # SIM-535: the game date, for the draw-time cutoff.
                game_ymd=(
                    np.asarray(np.ma.filled(m["game_ymd"], 0), dtype=np.int32)
                    if "game_ymd" in m
                    else None
                ),
                sit=take_view(
                    views,
                    f"steal_pool.{target}.sit",
                    os.path.join(sp_dir, f"{target}.sit.npy"),
                ),
                runner_id=_sp_take("runner_id", "runner_id", np.int64, 0),
                pitcher_id=_sp_take("pitcher_id", "pitcher_id", np.int64, 0),
                catcher_id=_sp_take("catcher_id", "catcher_id", np.int64, 0),
                season=_sp_take("season", "season", np.int64, 0),
                attempted=_sp_take("attempted", "attempted", np.int8, 0),
                success=_sp_take("success", "success", np.int8, 0),
                recency=(
                    views.get(f"steal_pool.{target}.recency")
                    if isinstance(views.get(f"steal_pool.{target}.recency"), np.ndarray)
                    else np.nan_to_num(
                        np.ma.filled(m["recency_weight"], 1.0).astype(np.float32),
                        nan=1.0,
                    )
                ),
                pickoff_out=_sp_take("pickoff_out", "pickoff_out", np.int8, 0),
                pickoff_advancing=_sp_take("pickoff_advancing", "pickoff_advancing", np.int8, 0),
                pickoff_error=_sp_take("pickoff_error", "pickoff_error", np.int8, 0),
                pitch_class=pitch_class,
                is_pickoff_row=is_pickoff_row,
            )
            # SIM-554: the two files of a target come from two writes;
            # refuse a pair whose row counts differ (a torn export).
            n_meta = len(m["runner_id"])
            if steal_pools[target].n != n_meta:
                raise ValueError(
                    f"steal_pool[{target}]: {target}.sit.npy holds "
                    f"{steal_pools[target].n} rows but {target}.meta.parquet "
                    f"holds {n_meta}; re-export the steal pool (SIM-554)."
                )
    return steal_pools
