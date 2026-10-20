"""The per-hand pitch pool: one geometry, situation and metadata file set per
batter hand.
"""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from db.introspect import duck_columns
from pipeline.batch.artifacts.common import (
    _GEOM_COLS,
    _SIT_COLS,
    take_view,
    ymd_select,
)
from pipeline.batch.artifacts.pools import HandPool

log = logging.getLogger("engine_artifacts")


def build_pitch_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the full per-hand pitch pool (recency-floored) as geom/sit/meta files."""
    pool_dir = os.path.join(out_dir, "pitch_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = ", ".join(str(int(s)) for s in seasons)
    counts: dict[str, int] = {}
    for hand in ("L", "R"):
        cols = ", ".join(_GEOM_COLS + _SIT_COLS)
        d = con.execute(
            f"SELECT {cols}, pitch_id, pitcher_id, batter_id, season, outcome_type, recency_weight "
            f"FROM sim.pitch_pool WHERE stand='{hand}' AND season IN ({season_list})"
        ).fetchnumpy()
        # SIM-517 / SIM-518: the optional pool columns (migrations 0022 /
        # 0023). Probed so a pre-migration sim DB still exports (the loader
        # treats them as optional exactly like the batted-ball realism
        # columns).
        pp_cols = duck_columns(con, "sim", "pitch_pool")
        has_catcher = "catcher_id" in pp_cols
        has_conditioning = pp_cols >= _PP_CONDITIONING_COLS
        # SIM-523 part E: the zone, for the receiving ratio's zone groups.
        zone_select = ", COALESCE(zone, 0) AS zone" if "zone" in pp_cols else ""
        n = len(d["pitch_id"])
        # fetchnumpy yields masked arrays for nullable cols; fill -> plain float32.
        geom = np.nan_to_num(
            np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in _GEOM_COLS], axis=1)
        ).astype(np.float32)
        sit = np.nan_to_num(
            np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in _SIT_COLS], axis=1)
        ).astype(np.float32)
        np.save(os.path.join(pool_dir, f"{hand}.geom.npy"), geom)
        np.save(os.path.join(pool_dir, f"{hand}.sit.npy"), sit)
        # Metadata via DuckDB COPY (no pandas dependency).
        receiving = (
            ", COALESCE(catcher_id, 0) AS catcher_id, "
            "CAST(COALESCE(got_away, FALSE) AS TINYINT) AS got_away"
            if has_catcher
            else ""
        )
        # SIM-518 (migration 0023): the draw-conditioning columns. A NULL (a
        # row the rebuild has not filled) exports as UNKNOWN — bat_home -1,
        # pitch count -1, times through the order 0 — and every consumer is
        # exactly neutral on an unknown row.
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
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "seasons": seasons,
                "counts": counts,
                "geom_cols": _GEOM_COLS,
                "sit_cols": _SIT_COLS,
                "conditioning": has_conditioning,
            },
            fh,
            indent=2,
        )
    return counts


#: SIM-518: the pitch-pool conditioning columns (migration 0023). Presence
#: of ALL of them marks a 0023-migrated DB; the export writes them together.
_PP_CONDITIONING_COLS = frozenset({"bat_home", "pitcher_pitch_count", "times_through_order"})


def _pp_take(
    views: dict[str, np.ndarray], hand: str, meta: dict, attr: str, col: str, dtype: type, fill: int
) -> np.ndarray | None:
    """SIM-518: the same optional-column rule for the PITCH pool."""
    v = views.get(f"pool.{hand}.{attr}")
    if isinstance(v, np.ndarray):
        return v
    if col in meta:
        return np.asarray(np.ma.filled(meta[col], fill), dtype=dtype)
    return None


def load_pitch_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, HandPool]:
    """Load both hands' pitch pools, taking each shared array from ``views``
    when the parent published it."""
    pool_dir = os.path.join(art_dir, "pitch_pool")
    pools: dict[str, HandPool] = {}
    for hand in ("L", "R"):
        geom = take_view(views, f"pool.{hand}.geom", os.path.join(pool_dir, f"{hand}.geom.npy"))
        sit = take_view(views, f"pool.{hand}.sit", os.path.join(pool_dir, f"{hand}.sit.npy"))
        # The metadata parquet stays disk-loaded — outcome_type is
        # object-dtype (not shareable) and the ids/recency are small.
        meta_path = os.path.join(pool_dir, f"{hand}.meta.parquet")
        # SIM-517: the receiving columns exist only on a post-0022
        # export — probe the parquet schema and select what is there
        # (the SIM-411/413 batted-ball pattern).
        pp_avail = {
            r[0] for r in con.execute(f"SELECT name FROM parquet_schema('{meta_path}')").fetchall()
        }
        receiving_sel = ", catcher_id, got_away" if "catcher_id" in pp_avail else ""
        # SIM-518: the conditioning columns exist only on a sim518
        # export — the same probe-and-select pattern.
        conditioning_sel = "".join(
            f", {c}"
            for c in ("bat_home", "pitcher_pitch_count", "times_through_order", "zone")
            if c in pp_avail
        )
        # SIM-535: the game date, on a bundle exported after it existed.
        ymd_sel = ", game_ymd" if "game_ymd" in pp_avail else ""
        m = con.execute(
            "SELECT pitcher_id, batter_id, season, outcome_type, recency_weight"
            f"{receiving_sel}{conditioning_sel}{ymd_sel} "
            f"FROM read_parquet('{meta_path}')"
        ).fetchnumpy()
        # When the shared map has the id/season/recency columns too,
        # prefer them (they round-trip identically and skip more disk).
        pid_view = views.get(f"pool.{hand}.pitcher_id")
        bid_view = views.get(f"pool.{hand}.batter_id")
        ssn_view = views.get(f"pool.{hand}.season")
        rec_view = views.get(f"pool.{hand}.recency")
        pools[hand] = HandPool(
            geom=geom,
            sit=sit,
            pitcher_id=(
                pid_view
                if isinstance(pid_view, np.ndarray)
                else np.asarray(np.ma.filled(m["pitcher_id"], 0), dtype=np.int64)
            ),
            batter_id=(
                bid_view
                if isinstance(bid_view, np.ndarray)
                else np.asarray(np.ma.filled(m["batter_id"], 0), dtype=np.int64)
            ),
            season=(
                ssn_view
                if isinstance(ssn_view, np.ndarray)
                else np.asarray(np.ma.filled(m["season"], 0), dtype=np.int64)
            ),
            outcome_type=np.asarray(m["outcome_type"], dtype=object),
            game_ymd=(
                np.asarray(np.ma.filled(m["game_ymd"], 0), dtype=np.int32)
                if "game_ymd" in m
                else None
            ),
            recency=(
                rec_view
                if isinstance(rec_view, np.ndarray)
                else np.nan_to_num(
                    np.ma.filled(m["recency_weight"], 1.0).astype(np.float32),
                    nan=1.0,
                )
            ),
            catcher_id=(
                views.get(f"pool.{hand}.catcher_id")
                if isinstance(views.get(f"pool.{hand}.catcher_id"), np.ndarray)
                else (
                    np.asarray(np.ma.filled(m["catcher_id"], 0), dtype=np.int64)
                    if "catcher_id" in m
                    else None
                )
            ),
            got_away=(
                views.get(f"pool.{hand}.got_away")
                if isinstance(views.get(f"pool.{hand}.got_away"), np.ndarray)
                else (
                    np.asarray(np.ma.filled(m["got_away"], 0), dtype=np.int8)
                    if "got_away" in m
                    else None
                )
            ),
            # SIM-518: the draw-conditioning columns (unknown = -1 / -1 / 0).
            bat_home=_pp_take(views, hand, m, "bat_home", "bat_home", np.int8, -1),
            pitch_count=_pp_take(
                views, hand, m, "pitch_count", "pitcher_pitch_count", np.int16, -1
            ),
            tto=_pp_take(views, hand, m, "tto", "times_through_order", np.int8, 0),
            # SIM-523 part E: the zone (None before part E).
            zone=_pp_take(views, hand, m, "zone", "zone", np.int8, 0),
            # SIM-523 part B: the pitch-id join (None before part B).
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
