"""Export the advancement opportunity pools, one per runner decision, and load them back.

The export writes ``advancement_pool/{key}.feat.npy`` (the kernel features),
``{key}.meta.parquet`` (the actors and the outcome) and ``manifest.json``. The features and
the parquet come from two reads with no ORDER BY, so their rows line up only through
DuckDB's stored order.
"""

from __future__ import annotations

import logging
import os

import duckdb
import numpy as np

from bundle.common import (
    float_columns,
    game_ymd_column,
    meta_column,
    recency_column,
    take_view,
    write_json,
    ymd_select,
)
from bundle.worker_pools import AdvancementPool
from db.introspect import duck_table_exists
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


#: The eight advancement decisions, keyed "{scenario}_{from_base}_{target_base}".
#: The header of DuckDB migration 0018 names the scenarios.
_ADV_KEYS = ("1_1_3", "2_2_4", "3_1_4", "4_1_2", "4_2_3", "4_3_4", "5_0_2", "5_0_3")

#: The advancement draw's kernel features: the batted ball's geometry (the throw's
#: context) and the outs before the pitch (with two outs, a runner goes harder on contact).
_ADV_FEAT_COLS = ["exit_velo", "launch_angle", "spray_angle", "hit_distance", "outs"]


def build_advancement_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the advancement opportunity pools, one per decision, and the manifest.

    A row is a runner's chance to take an extra base, taken or not; the rows not taken
    give the draw its denominator. Each (scenario, from_base, target_base) is its own
    pool, because each decision has its own rates. Returns the row count per decision, or
    {} when the database has no advancement pool.
    """
    if not duck_table_exists(con, "sim", "advancement_opportunity_pool"):
        log.warning(
            "advancement_pool: sim.advancement_opportunity_pool absent "
            "(pre-0018 DB) — skipping the export."
        )
        return {}
    pool_dir = os.path.join(out_dir, "advancement_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = sql_seasons(seasons)
    counts: dict[str, int] = {}
    for key in _ADV_KEYS:
        scen, frm, tgt = (int(x) for x in key.split("_"))
        w = (
            f"scenario = {scen} AND from_base = {frm} AND target_base = {tgt} "
            f"AND season IN ({season_list})"
        )
        d = con.execute(
            f"SELECT {', '.join(_ADV_FEAT_COLS)} FROM sim.advancement_opportunity_pool WHERE {w}"
        ).fetchnumpy()
        n = len(d[_ADV_FEAT_COLS[0]])
        feat = float_columns(d, _ADV_FEAT_COLS)
        np.save(os.path.join(pool_dir, f"{key}.feat.npy"), feat)
        con.execute(
            "COPY (SELECT runner_id, fielder_id, fielder_pos, season, "
            "attempted, safe, error_extra, recency_weight "
            f"{ymd_select(con, 'sim.advancement_opportunity_pool')}"
            f"FROM sim.advancement_opportunity_pool WHERE {w}) "
            f"TO '{os.path.join(pool_dir, f'{key}.meta.parquet')}' (FORMAT parquet)"
        )
        counts[key] = int(n)
        log.info("advancement_pool[%s]: %d rows (seasons %s)", key, n, seasons)
    write_json(
        os.path.join(pool_dir, "manifest.json"),
        {"seasons": seasons, "counts": counts, "feat_cols": _ADV_FEAT_COLS},
    )
    return counts


def load_advancement_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, AdvancementPool]:
    """Load the advancement pools by decision key; {} when the bundle has none.

    With no advancement pool, the advancement draw stages no extra-base try.
    """
    adv_pools: dict[str, AdvancementPool] = {}
    ap_dir = os.path.join(art_dir, "advancement_pool")
    if os.path.exists(os.path.join(ap_dir, "manifest.json")):
        for key in _ADV_KEYS:
            meta_path = os.path.join(ap_dir, f"{key}.meta.parquet")
            if not os.path.exists(meta_path):
                continue
            m = con.execute(f"SELECT * FROM read_parquet('{meta_path}')").fetchnumpy()
            p = f"adv_pool.{key}"
            adv_pools[key] = AdvancementPool(
                # None on a bundle exported before the date existed. The sampler then
                # reports it rather than draw from the future.
                game_ymd=game_ymd_column(m),
                feat=take_view(
                    views, f"adv_pool.{key}.feat", os.path.join(ap_dir, f"{key}.feat.npy")
                ),
                runner_id=meta_column(views, f"{p}.runner_id", m, "runner_id", np.int64),
                fielder_id=meta_column(views, f"{p}.fielder_id", m, "fielder_id", np.int64),
                fielder_pos=meta_column(views, f"{p}.fielder_pos", m, "fielder_pos", np.int8),
                season=meta_column(views, f"{p}.season", m, "season", np.int64),
                attempted=meta_column(views, f"{p}.attempted", m, "attempted", np.int8),
                safe=meta_column(views, f"{p}.safe", m, "safe", np.int8),
                error_extra=meta_column(views, f"{p}.error_extra", m, "error_extra", np.int8),
                recency=recency_column(views, f"{p}.recency", m),
            )
    return adv_pools
