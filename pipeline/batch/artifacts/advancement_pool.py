"""The advancement opportunity pools, one per runner decision."""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from db.introspect import duck_table_exists
from pipeline.batch.artifacts.common import take_view, ymd_select
from pipeline.batch.artifacts.pools import AdvancementPool

log = logging.getLogger("engine_artifacts")


#: SIM-510: the eight advancement decisions, keyed "{scenario}_{from_base}_
#: {target_base}" (see the migration-0018 header for the scenario names).
_ADV_KEYS = ("1_1_3", "2_2_4", "3_1_4", "4_1_2", "4_2_3", "4_3_4", "5_0_2", "5_0_3")

#: The advancement draw's kernel features: the batted ball's geometry (the
#: throw context) + the pre-pitch outs (a two-out contact play runs harder).
_ADV_FEAT_COLS = ["exit_velo", "launch_angle", "spray_angle", "hit_distance", "outs"]


def build_advancement_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the per-decision advancement OPPORTUNITY pools for SIM-512.

    One row per discretionary runner-advancement decision (SIM-510),
    attempted or not — the non-attempt rows are the denominator, exactly the
    steal-pool pattern. Sub-pools split by (scenario, from_base, target_base)
    because each is a different decision with different rates.
    """
    if not duck_table_exists(con, "sim", "advancement_opportunity_pool"):
        log.warning(
            "advancement_pool: sim.advancement_opportunity_pool absent "
            "(pre-0018 DB) — skipping the export."
        )
        return {}
    pool_dir = os.path.join(out_dir, "advancement_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = ", ".join(str(int(s)) for s in seasons)
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
        feat = np.nan_to_num(
            np.stack(
                [np.ma.filled(d[c], np.nan).astype(np.float32) for c in _ADV_FEAT_COLS], axis=1
            )
        ).astype(np.float32)
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
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"seasons": seasons, "counts": counts, "feat_cols": _ADV_FEAT_COLS}, fh, indent=2)
    return counts


def load_advancement_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, AdvancementPool]:
    """Load the advancement pools by decision key; {} when the bundle has none."""
    # SIM-510: the advancement opportunity pools, one per decision key.
    # Presence-gated like the others: {} on a legacy bundle, and the
    # SIM-512 draw then stages no discretionary advancement.
    adv_pools: dict[str, AdvancementPool] = {}
    ap_dir = os.path.join(art_dir, "advancement_pool")
    if os.path.exists(os.path.join(ap_dir, "manifest.json")):
        for key in _ADV_KEYS:
            meta_path = os.path.join(ap_dir, f"{key}.meta.parquet")
            if not os.path.exists(meta_path):
                continue
            m = con.execute(f"SELECT * FROM read_parquet('{meta_path}')").fetchnumpy()

            def _ap_take(attr: str, col: str, dtype, fill, *, _k=key, _m=m) -> np.ndarray:
                # _k/_m bind the loop variables at definition time (B023).
                v = views.get(f"adv_pool.{_k}.{attr}")
                if isinstance(v, np.ndarray):
                    return v
                return np.asarray(np.ma.filled(_m[col], fill), dtype=dtype)

            adv_pools[key] = AdvancementPool(
                # SIM-535: None on a bundle exported before the date
                # existed, which the sampler reports rather than
                # silently drawing from the future.
                game_ymd=(
                    np.asarray(np.ma.filled(m["game_ymd"], 0), dtype=np.int32)
                    if "game_ymd" in m
                    else None
                ),
                feat=take_view(
                    views, f"adv_pool.{key}.feat", os.path.join(ap_dir, f"{key}.feat.npy")
                ),
                runner_id=_ap_take("runner_id", "runner_id", np.int64, 0),
                fielder_id=_ap_take("fielder_id", "fielder_id", np.int64, 0),
                fielder_pos=_ap_take("fielder_pos", "fielder_pos", np.int8, 0),
                season=_ap_take("season", "season", np.int64, 0),
                attempted=_ap_take("attempted", "attempted", np.int8, 0),
                safe=_ap_take("safe", "safe", np.int8, 0),
                error_extra=_ap_take("error_extra", "error_extra", np.int8, 0),
                recency=(
                    views.get(f"adv_pool.{key}.recency")
                    if isinstance(views.get(f"adv_pool.{key}.recency"), np.ndarray)
                    else np.nan_to_num(
                        np.ma.filled(m["recency_weight"], 1.0).astype(np.float32),
                        nan=1.0,
                    )
                ),
            )
    return adv_pools
