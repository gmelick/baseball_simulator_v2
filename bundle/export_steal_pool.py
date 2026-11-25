"""The steal opportunity pools, one per target base."""

from __future__ import annotations

import logging
import os

import duckdb
import numpy as np

from bundle.common import (
    float_columns,
    game_ymd_column,
    meta_column,
    optional_meta_column,
    recency_column,
    take_view,
    write_json,
    ymd_select,
)
from bundle.worker_pools import StealPool, steal_pitch_class_codes
from db.introspect import duck_columns
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


#: The steal draw's situation columns. `outs` and the count are the
#: hard-filter cell; `score_diff` is soft-kernelled. `inning` is deliberately
#: excluded — leverage shaping rides on the manager weight, not the pool.
_STEAL_SIT_COLS = ["count_balls", "count_strikes", "outs", "score_diff"]


def build_steal_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the per-target-base steal OPPORTUNITY pools the steal draw reads.

    One row per pitch where a steal was possible, attempted or not —
    the `attempted` flag is the denominator the attempts-only pool lacks, so a
    single draw answers both "does the runner go" and "safe or caught".
    Sub-pools are split by target base ("2" = 1B->2B, "3" = 2B->3B), mirroring
    the per-hand split of the other pools.

    The meta parquet carries ``pitch_id``, the pitch's class
    (``pitch_class``; '' on a pickoff row) and ``is_pickoff_row``. The loader
    maps the class to the codes of :data:`STEAL_PITCH_CLASS_CODE`. A DuckDB
    without migration 0031 is refused before any file is written: the run book
    applies the migration before the export, and a bundle without classes
    would run the loop's old order without a word. Each target's two files are
    written under temporary names and moved into place only when both targets
    succeeded, the manifest last, so a failed export leaves the old pool whole.
    """
    have = duck_columns(con, "sim", "steal_opportunity_pool", this_file_only=True)
    missing = [c for c in ("pitch_class", "is_pickoff_row") if c not in have]
    if missing:
        raise RuntimeError(
            f"sim.steal_opportunity_pool has no {', '.join(missing)} column: apply "
            "db/migrations/duckdb/0031_sim554_steal_pool_pitch_class.sql before the "
            "export (SIM-554). No file was written."
        )
    pool_dir = os.path.join(out_dir, "steal_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = sql_seasons(seasons)
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
    write_json(
        os.path.join(pool_dir, "manifest.json"),
        {"seasons": seasons, "counts": counts, "sit_cols": _STEAL_SIT_COLS},
    )
    return counts


def _write_steal_pool_targets(
    con: duckdb.DuckDBPyConnection,
    pool_dir: str,
    season_list: str,
    seasons: list[int],
    counts: dict[str, int],
    staged: list[tuple[str, str]],
) -> None:
    """Write each target's sit and meta files under temporary names
    (``tmp_<target>.*``) and record them in ``staged``; the caller moves them
    into place once every target succeeded."""
    for target in ("2", "3"):
        w = f"target_base = {target} AND season IN ({season_list})"
        # Both queries sort by pitch_id. The sit rows and the meta rows
        # are written by two queries, and row i of one must be row i of the
        # other. Without the sort they lined up only because two unsorted scans
        # of the same filter happened to return the same order.
        d = con.execute(
            f"SELECT {', '.join(_STEAL_SIT_COLS)} FROM sim.steal_opportunity_pool "
            f"WHERE {w} ORDER BY pitch_id"
        ).fetchnumpy()
        n = len(d[_STEAL_SIT_COLS[0]])
        sit = float_columns(d, _STEAL_SIT_COLS)
        sit_tmp = os.path.join(pool_dir, f"tmp_{target}.sit.npy")
        meta_tmp = os.path.join(pool_dir, f"tmp_{target}.meta.parquet")
        staged.append((sit_tmp, os.path.join(pool_dir, f"{target}.sit.npy")))
        staged.append((meta_tmp, os.path.join(pool_dir, f"{target}.meta.parquet")))
        np.save(sit_tmp, sit)
        con.execute(
            "COPY (SELECT runner_id, pitcher_id, catcher_id, season, "
            "attempted, success, recency_weight, "
            # COALESCE keeps a pre-0017 DuckDB (NULL columns absent
            # from an un-migrated table would fail the SELECT outright — the
            # migration adds them; NULLs only appear on rows an old builder
            # wrote) reading as plain no-outcome rows.
            "COALESCE(pickoff_out, FALSE) AS pickoff_out, "
            "COALESCE(pickoff_advancing, FALSE) AS pickoff_advancing, "
            "COALESCE(pickoff_error, FALSE) AS pickoff_error, "
            # The caller refused a table without these
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
        # A pitch row with no class comes from a season that no
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
    # The steal opportunity pools ("2" = 1B->2B, "3" = 2B->3B).
    # Presence-gated like the batted-ball pool: {} on a legacy bundle,
    # and the sampler then stages no steals.
    steal_pools: dict[str, StealPool] = {}
    sp_dir = os.path.join(art_dir, "steal_pool")
    if os.path.exists(os.path.join(sp_dir, "manifest.json")):
        for target in ("2", "3"):
            meta_path = os.path.join(sp_dir, f"{target}.meta.parquet")
            m = con.execute(f"SELECT * FROM read_parquet('{meta_path}')").fetchnumpy()
            p = f"steal_pool.{target}"
            # The class of each row's pitch. No column means no classes
            # (None), and the loop then runs the single pre-pitch draw.
            pc_view = views.get(f"{p}.pitch_class")
            if isinstance(pc_view, np.ndarray):
                pitch_class: np.ndarray | None = pc_view
            elif "pitch_class" in m:
                pitch_class = steal_pitch_class_codes(
                    m["pitch_class"], where=f"steal_pool[{target}]"
                )
            else:
                pitch_class = None

            steal_pools[target] = StealPool(
                # The game date, for the draw-time cutoff.
                game_ymd=game_ymd_column(m),
                sit=take_view(
                    views,
                    f"steal_pool.{target}.sit",
                    os.path.join(sp_dir, f"{target}.sit.npy"),
                ),
                runner_id=meta_column(views, f"{p}.runner_id", m, "runner_id", np.int64),
                pitcher_id=meta_column(views, f"{p}.pitcher_id", m, "pitcher_id", np.int64),
                catcher_id=meta_column(views, f"{p}.catcher_id", m, "catcher_id", np.int64),
                season=meta_column(views, f"{p}.season", m, "season", np.int64),
                attempted=meta_column(views, f"{p}.attempted", m, "attempted", np.int8),
                success=meta_column(views, f"{p}.success", m, "success", np.int8),
                recency=recency_column(views, f"{p}.recency", m),
                pickoff_out=meta_column(views, f"{p}.pickoff_out", m, "pickoff_out", np.int8),
                pickoff_advancing=meta_column(
                    views, f"{p}.pickoff_advancing", m, "pickoff_advancing", np.int8
                ),
                pickoff_error=meta_column(views, f"{p}.pickoff_error", m, "pickoff_error", np.int8),
                pitch_class=pitch_class,
                is_pickoff_row=optional_meta_column(
                    views, f"{p}.is_pickoff_row", m, "is_pickoff_row", np.int8, False
                ),
            )
            # The two files of a target come from two writes;
            # refuse a pair whose row counts differ (a torn export).
            n_meta = len(m["runner_id"])
            if steal_pools[target].n != n_meta:
                raise ValueError(
                    f"steal_pool[{target}]: {target}.sit.npy holds "
                    f"{steal_pools[target].n} rows but {target}.meta.parquet "
                    f"holds {n_meta}; re-export the steal pool (SIM-554)."
                )
    return steal_pools
