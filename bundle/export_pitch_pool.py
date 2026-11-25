"""Export the pitch pool, one file set per batter hand, and load it back in a worker.

The export writes ``pitch_pool/{L,R}.geom.npy`` (the pitch geometry), ``{L,R}.sit.npy``
(the game situation), ``{L,R}.meta.parquet`` (the ids and the draw columns) and
``manifest.json``. The batted-ball export then adds ``{L,R}.bb_row.npy``. The arrays and
the parquet come from two reads with no ORDER BY, so their rows line up only through
DuckDB's stored order.
"""

from __future__ import annotations

import logging
import os

import duckdb
import numpy as np

from bundle.common import (
    _GEOM_COLS,
    _SIT_COLS,
    float_columns,
    game_ymd_column,
    meta_column,
    optional_meta_column,
    recency_column,
    take_view,
    write_json,
    ymd_select,
)
from bundle.worker_pools import HandPool
from db.introspect import duck_columns
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


def build_pitch_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write both hands' pitch pools over ``seasons`` and the manifest.

    Returns the row count per hand.
    """
    pool_dir = os.path.join(out_dir, "pitch_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = sql_seasons(seasons)
    counts: dict[str, int] = {}
    for hand in ("L", "R"):
        cols = ", ".join(_GEOM_COLS + _SIT_COLS)
        d = con.execute(
            f"SELECT {cols}, pitch_id "
            f"FROM sim.pitch_pool WHERE stand='{hand}' AND season IN ({season_list})"
        ).fetchnumpy()
        # Probe the optional columns, so an older sim database without them still
        # exports. The loader treats them as optional too.
        pp_cols = duck_columns(con, "sim", "pitch_pool")
        has_catcher = "catcher_id" in pp_cols
        has_conditioning = pp_cols >= _PP_CONDITIONING_COLS
        # The zone, for the receiving ratio's zone groups.
        zone_select = ", COALESCE(zone, 0) AS zone" if "zone" in pp_cols else ""
        n = len(d["pitch_id"])
        geom = float_columns(d, _GEOM_COLS)
        sit = float_columns(d, _SIT_COLS)
        np.save(os.path.join(pool_dir, f"{hand}.geom.npy"), geom)
        np.save(os.path.join(pool_dir, f"{hand}.sit.npy"), sit)
        # DuckDB's COPY writes the metadata parquet, so the export needs no pandas.
        receiving = (
            ", COALESCE(catcher_id, 0) AS catcher_id, "
            "CAST(COALESCE(got_away, FALSE) AS TINYINT) AS got_away"
            if has_catcher
            else ""
        )
        # A NULL draw-conditioning value (a row the pool rebuild has not filled)
        # exports as unknown: bat_home -1, pitch count -1, times through the order 0.
        # Every reader treats an unknown row as neutral.
        conditioning = (
            ", CASE WHEN bat_home IS NULL THEN -1 WHEN bat_home THEN 1 ELSE 0 END AS bat_home, "
            "COALESCE(pitcher_pitch_count, -1) AS pitcher_pitch_count, "
            "COALESCE(times_through_order, 0) AS times_through_order"
            if has_conditioning
            else ""
        )
        con.execute(
            f"COPY (SELECT pitch_id, pitcher_id, batter_id, season, outcome_type, recency_weight"
            f"{receiving}{conditioning}{zone_select} "
            f"{ymd_select(con, 'sim.pitch_pool')}"
            f"FROM sim.pitch_pool WHERE stand='{hand}' AND season IN ({season_list})) "
            f"TO '{os.path.join(pool_dir, f'{hand}.meta.parquet')}' (FORMAT parquet)"
        )
        counts[hand] = int(n)
        log.info("pitch_pool[%s]: %d rows (seasons %s)", hand, n, seasons)
    write_json(
        os.path.join(pool_dir, "manifest.json"),
        {
            "seasons": seasons,
            "counts": counts,
            "geom_cols": _GEOM_COLS,
            "sit_cols": _SIT_COLS,
            "conditioning": has_conditioning,
        },
    )
    return counts


#: The pitch pool's draw-conditioning columns. The export writes them only when all
#: three exist, and then writes them together.
_PP_CONDITIONING_COLS = frozenset({"bat_home", "pitcher_pitch_count", "times_through_order"})


def load_pitch_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, HandPool]:
    """Load both hands' pitch pools; each array comes from ``views`` when the parent shared it."""
    pool_dir = os.path.join(art_dir, "pitch_pool")
    pools: dict[str, HandPool] = {}
    for hand in ("L", "R"):
        geom = take_view(views, f"pool.{hand}.geom", os.path.join(pool_dir, f"{hand}.geom.npy"))
        sit = take_view(views, f"pool.{hand}.sit", os.path.join(pool_dir, f"{hand}.sit.npy"))
        # The worker always reads the metadata parquet: outcome_type is an object
        # array, which shared memory cannot hold.
        meta_path = os.path.join(pool_dir, f"{hand}.meta.parquet")
        # Probe the parquet's schema and select only the columns it holds, so an
        # older bundle still loads.
        pp_avail = {
            r[0] for r in con.execute(f"SELECT name FROM parquet_schema('{meta_path}')").fetchall()
        }
        receiving_sel = ", catcher_id, got_away" if "catcher_id" in pp_avail else ""
        conditioning_sel = "".join(
            f", {c}"
            for c in ("bat_home", "pitcher_pitch_count", "times_through_order", "zone")
            if c in pp_avail
        )
        ymd_sel = ", game_ymd" if "game_ymd" in pp_avail else ""
        m = con.execute(
            "SELECT pitcher_id, batter_id, season, outcome_type, recency_weight"
            f"{receiving_sel}{conditioning_sel}{ymd_sel} "
            f"FROM read_parquet('{meta_path}')"
        ).fetchnumpy()
        # A column's shared view, when present, holds the same values as the parquet.
        p = f"pool.{hand}"
        pools[hand] = HandPool(
            geom=geom,
            sit=sit,
            pitcher_id=meta_column(views, f"{p}.pitcher_id", m, "pitcher_id", np.int64),
            batter_id=meta_column(views, f"{p}.batter_id", m, "batter_id", np.int64),
            season=meta_column(views, f"{p}.season", m, "season", np.int64),
            outcome_type=np.asarray(m["outcome_type"], dtype=object),
            game_ymd=game_ymd_column(m),
            recency=recency_column(views, f"{p}.recency", m),
            catcher_id=optional_meta_column(views, f"{p}.catcher_id", m, "catcher_id", np.int64),
            got_away=optional_meta_column(views, f"{p}.got_away", m, "got_away", np.int8),
            # The draw-conditioning columns (unknown = -1 / -1 / 0).
            bat_home=optional_meta_column(views, f"{p}.bat_home", m, "bat_home", np.int8, -1),
            pitch_count=optional_meta_column(
                views, f"{p}.pitch_count", m, "pitcher_pitch_count", np.int16, -1
            ),
            tto=optional_meta_column(views, f"{p}.tto", m, "times_through_order", np.int8),
            zone=optional_meta_column(views, f"{p}.zone", m, "zone", np.int8),
            # The row of each pitch's batted ball in the batted-ball pool (the
            # pitch-id join); None on an older bundle.
            bb_row=(
                views.get(f"pool.{hand}.bb_row")
                if isinstance(views.get(f"pool.{hand}.bb_row"), np.ndarray)
                else (
                    np.load(os.path.join(pool_dir, f"{hand}.bb_row.npy"), allow_pickle=False)
                    if os.path.exists(os.path.join(pool_dir, f"{hand}.bb_row.npy"))
                    else None
                )
            ),
        )
    return pools
