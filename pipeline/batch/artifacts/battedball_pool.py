"""The per-hand batted-ball pool the fielding draw reads."""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from pipeline.batch.artifacts.common import (
    _GEOM_COLS,
    _SIT_COLS,
    join_rows,
    take_view,
    ymd_select,
)
from pipeline.batch.artifacts.pools import BattedBallPool

log = logging.getLogger("engine_artifacts")


_BB_GEOM_COLS = ["exit_velo", "launch_angle", "pull_relative_spray_angle"]

#: SIM-523 part C: the batted-ball CLASS vocabulary (``BattedBallPool.bb_class``).
#: 0 = unknown. The three bunt types share one class.
BB_CLASS: dict[str, int] = {
    "ground_ball": 1,
    "line_drive": 2,
    "fly_ball": 3,
    "popup": 4,
    "bunt_grounder": 5,
    "bunt_popup": 5,
    "bunt_line_drive": 5,
}
_BB_CLASS_SELECT = (
    ", CASE bb_type "
    + " ".join(f"WHEN '{k}' THEN {v}" for k, v in BB_CLASS.items())
    + " ELSE 0 END AS bb_class"
)


def build_battedball_pool_artifact(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict[str, int]:
    """Write the full per-hand batted-ball pool (recency-floored) for SIM-425:
    geom (EV/LA/spray) + situation + metadata (batter, event, result deltas)."""
    pool_dir = os.path.join(out_dir, "battedball_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = ", ".join(str(int(s)) for s in seasons)
    where = (
        f"stand='%s' AND season IN ({season_list}) AND exit_velo IS NOT NULL "
        "AND launch_angle IS NOT NULL AND pull_relative_spray_angle IS NOT NULL"
    )
    counts: dict[str, int] = {}
    # SIM-510: the transition columns exist only on a 0018-migrated DB. Export
    # them when present; a pre-0018 DB still exports the legacy shape. The
    # export keeps EVERY row (SIM-510 is behavior-neutral) — the SIM-511
    # fielding draw's cell index is what excludes ``dest_ok = 0`` rows.
    op_cols = {str(d[0]) for d in con.execute("SELECT * FROM sim.outcome_pool LIMIT 0").description}
    has_transition = op_cols >= _BB_TRANSITION_SOURCE_COLS
    # SIM-491: the batting side exists only on a 0019-migrated DB. Same
    # back-compatible pattern as the transition columns.
    bat_home_select = ", bat_home" if "bat_home" in op_cols else ""
    transition_select = (
        ", runner_1b_dest, runner_2b_dest, runner_3b_dest, batter_dest, "
        "COALESCE(dest_outs_consistent, FALSE) AS dest_ok, "
        # SIM-511 normalization facts: the thrown-out-advancing flags tell a
        # discretionary out from a force/doubled-off out; is_air marks the
        # tag-up shape (SAME definition as the SIM-510 builder's scenario 4);
        # raw spray + distance feed the SIM-512 advancement kernel (the bb
        # geom carries only the PULL-relative spray).
        "COALESCE(runner_1b_out_advancing, FALSE) AS r1_adv_out, "
        "COALESCE(runner_2b_out_advancing, FALSE) AS r2_adv_out, "
        "COALESCE(runner_3b_out_advancing, FALSE) AS r3_adv_out, "
        "COALESCE(bb_type IN ('fly_ball', 'popup', 'line_drive'), FALSE) AS is_air, "
        "spray_angle AS spray_raw, hit_distance AS hit_dist"
        if has_transition
        else ""
    )
    if not has_transition:
        log.warning(
            "battedball_pool: sim.outcome_pool has no SIM-510 transition columns "
            "(pre-0018 DB) — exporting the legacy shape."
        )
    # SIM-518 (SIM-463): the ten pitch-feature columns of the pitch that
    # produced each batted ball, plus its pitcher. The pool build selects
    # them from the pitch pool, so a production outcome pool always has them;
    # a reduced synthetic pool may not, hence the probe. The loader treats a
    # missing pgeom file / pitcher column as None (the SIM-472 kernel then
    # stays neutral).
    has_pgeom = set(_GEOM_COLS) <= op_cols
    pitcher_select = ", pitcher_id" if "pitcher_id" in op_cols else ""
    # SIM-523 part C: the batted-ball class (the fielding draw's third
    # hard filter) — from bb_type when the pool carries it.
    class_select = _BB_CLASS_SELECT if "bb_type" in op_cols else ""
    # SIM-523 part G: the fielding CHAIN — the eight-position alignment and
    # the positions credited a putout / an assist (migration 0024); absent
    # on an older pool, and the loader is None without them.
    has_chain = "putout_pos_mask" in op_cols
    chain_select = (
        ", fielder_2, fielder_3, fielder_4, fielder_5, fielder_6, fielder_7, fielder_8, fielder_9, putout_pos_mask, assist_pos_mask"
        if has_chain
        else ""
    )
    pitch_join: dict[str, int] = {}
    for hand in ("L", "R"):
        w = where % hand
        # One query for geom + sit + pgeom so the three arrays share one row
        # order (the meta COPY below is a second scan of the same filter).
        pgeom_cols = _GEOM_COLS if has_pgeom else []
        d = con.execute(
            f"SELECT {', '.join(_BB_GEOM_COLS + _SIT_COLS + pgeom_cols)}, pitch_id "
            f"FROM sim.outcome_pool WHERE {w}"
        ).fetchnumpy()
        n = len(d[_BB_GEOM_COLS[0]])
        geom = np.nan_to_num(
            np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in _BB_GEOM_COLS], axis=1)
        ).astype(np.float32)
        sit = np.nan_to_num(
            np.stack([np.ma.filled(d[c], np.nan).astype(np.float32) for c in _SIT_COLS], axis=1)
        ).astype(np.float32)
        np.save(os.path.join(pool_dir, f"{hand}.geom.npy"), geom)
        np.save(os.path.join(pool_dir, f"{hand}.sit.npy"), sit)
        # SIM-523 part B: the pitch-id JOIN, in reverse of the SIM-518
        # ``pgeom`` export — for every PITCH-pool row (its meta parquet,
        # written by build_pitch_pool_artifact just before), the row of
        # THIS pool that holds its batted ball, or -1. The pitch-result
        # draw's in-play row reads its own batted ball through it.
        bb_pid = np.asarray(np.ma.filled(d["pitch_id"], -1), dtype=np.int64)
        np.save(os.path.join(pool_dir, f"{hand}.pitch_id.npy"), bb_pid)
        pp_meta = os.path.join(out_dir, "pitch_pool", f"{hand}.meta.parquet")
        if os.path.exists(pp_meta):
            pp_pid = np.asarray(
                np.ma.filled(
                    con.execute(f"SELECT pitch_id FROM read_parquet('{pp_meta}')").fetchnumpy()[
                        "pitch_id"
                    ],
                    -1,
                ),
                dtype=np.int64,
            )
            bb_row = join_rows(pp_pid, bb_pid)
            np.save(os.path.join(out_dir, "pitch_pool", f"{hand}.bb_row.npy"), bb_row)
            pitch_join[hand] = int((bb_row >= 0).sum())
            log.info(
                "battedball_pool[%s]: pitch join %d of %d pitch rows -> a batted ball",
                hand,
                pitch_join[hand],
                len(pp_pid),
            )
        if has_pgeom:
            # NaN is PRESERVED here (unlike geom/sit): a row whose pitch is
            # missing a value must be exactly neutral under the kernel, and
            # a 0.0 stand-in would be a wild outlier instead.
            pgeom = np.stack(
                [np.ma.filled(d[c], np.nan).astype(np.float32) for c in _GEOM_COLS], axis=1
            ).astype(np.float32)
            np.save(os.path.join(pool_dir, f"{hand}.pgeom.npy"), pgeom)
        con.execute(
            "COPY (SELECT pitch_id, batter_id, season, events, result_hits, result_outs, "
            "result_runs, recency_weight, "
            # SIM-411/413/425b: per-row realism facts (migration 0012). p_throws (the
            # pitcher hand) drives the SIM-413 platoon reweight; venue_id the SIM-411
            # park multiplier; fielded_by_position + fielder_player_id the SIM-425b
            # relative defensive nudge. The loader is back-compatible when these are
            # absent (a pre-0012 artifact), so writing them is safe to roll out ahead
            # of the consumers. SIM-510 appends the transition destinations the same
            # back-compatible way.
            "p_throws, venue_id, fielded_by_position, fielder_player_id"
            f"{transition_select}{bat_home_select}{pitcher_select}{class_select}"
            f"{chain_select} "
            f"{ymd_select(con, 'sim.outcome_pool')}"
            f"FROM sim.outcome_pool WHERE {w}) "
            f"TO '{os.path.join(pool_dir, f'{hand}.meta.parquet')}' (FORMAT parquet)"
        )
        counts[hand] = int(n)
        log.info("battedball_pool[%s]: %d rows (seasons %s)", hand, n, seasons)
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "seasons": seasons,
                "counts": counts,
                "geom_cols": _BB_GEOM_COLS,
                "sit_cols": _SIT_COLS,
                "transition": has_transition,
                # SIM-518 (SIM-463): the pitch-geometry columns of {hand}.pgeom.npy.
                "pgeom_cols": _GEOM_COLS if has_pgeom else [],
                # SIM-523 part B: pitch rows joined to a batted ball, per hand
                # (the join file lives in pitch_pool/{hand}.bb_row.npy).
                "pitch_join": pitch_join,
                # SIM-523 part G: the fielding chain columns are in the parquet.
                "chain": has_chain,
            },
            fh,
            indent=2,
        )
    return counts


#: SIM-510: the outcome-pool columns the transition export needs. Presence of
#: ALL of them marks a 0018-migrated, sim510.1-rebuilt DB.
_BB_TRANSITION_SOURCE_COLS = frozenset(
    {
        "runner_1b_dest",
        "runner_2b_dest",
        "runner_3b_dest",
        "batter_dest",
        "dest_outs_consistent",
        "runner_1b_out_advancing",
        "runner_2b_out_advancing",
        "runner_3b_out_advancing",
    }
)


def _bb_take(
    views: dict[str, np.ndarray], hand: str, meta: dict, attr: str, col: str, dtype: type, fill: int
) -> np.ndarray | None:
    """SIM-411/425b: load an OPTIONAL numeric batted-ball meta column.
    Prefer a published shared view; else fill the parquet column (NULL ->
    ``fill``); else (a pre-0012 artifact that lacks the column) return None
    so the consumer falls back to a neutral factor."""
    v = views.get(f"bb_pool.{hand}.{attr}")
    if isinstance(v, np.ndarray):
        return v
    if col in meta:
        return np.asarray(np.ma.filled(meta[col], fill), dtype=dtype)
    return None


def _bb_fielders(views: dict[str, np.ndarray], hand: str, meta: dict) -> np.ndarray | None:
    """SIM-523 part G: the (N, 8) alignment from fielder_2..fielder_9
    (a published view first; None on a pre-part-G artifact)."""
    v = views.get(f"bb_pool.{hand}.fielders")
    if isinstance(v, np.ndarray):
        return v
    if "fielder_2" not in meta:
        return None
    return np.column_stack(
        [np.asarray(np.ma.filled(meta[f"fielder_{k}"], 0), dtype=np.int64) for k in range(2, 10)]
    ).astype(np.int64)


def load_battedball_pools(
    con: duckdb.DuckDBPyConnection, art_dir: str, views: dict[str, np.ndarray]
) -> dict[str, BattedBallPool]:
    """Load both hands' batted-ball pools; {} when the bundle has none."""
    bb_pools: dict[str, BattedBallPool] = {}
    bb_dir = os.path.join(art_dir, "battedball_pool")
    if os.path.exists(os.path.join(bb_dir, "manifest.json")):
        for hand in ("L", "R"):
            meta_path = os.path.join(bb_dir, f"{hand}.meta.parquet")
            # SIM-411/413/425b: select the realism columns only when present,
            # so a pre-0012 artifact (no venue/hand/fielder columns) still loads.
            avail = {
                str(d[0])
                for d in con.execute(
                    f"SELECT * FROM read_parquet('{meta_path}') LIMIT 0"
                ).description
            }
            base_cols = [
                "batter_id",
                "season",
                "events",
                "result_hits",
                "result_outs",
                "recency_weight",
            ]
            opt_cols = [
                c
                for c in (
                    "p_throws",
                    "venue_id",
                    "fielded_by_position",
                    "fielder_player_id",
                    # SIM-510: the transition destinations.
                    "runner_1b_dest",
                    "runner_2b_dest",
                    "runner_3b_dest",
                    "batter_dest",
                    "dest_ok",
                    # SIM-511: the normalization facts + geometry.
                    "r1_adv_out",
                    "r2_adv_out",
                    "r3_adv_out",
                    "is_air",
                    "spray_raw",
                    "hit_dist",
                    # SIM-491: the batting side.
                    "bat_home",
                    # SIM-518 (SIM-463): the producing pitch's pitcher.
                    "pitcher_id",
                    # SIM-523 part C: the batted-ball class.
                    "bb_class",
                    # SIM-523 part G: the alignment + the fielding chain.
                    "fielder_2",
                    "fielder_3",
                    "fielder_4",
                    "fielder_5",
                    "fielder_6",
                    "fielder_7",
                    "fielder_8",
                    "fielder_9",
                    "putout_pos_mask",
                    "assist_pos_mask",
                    # SIM-535: the game date, for the draw-time cutoff.
                    "game_ymd",
                )
                if c in avail
            ]
            # SIM-518 (SIM-463): the producing pitch's geometry file
            # (absent on a pre-sim518 export -> None).
            pgeom_path = os.path.join(bb_dir, f"{hand}.pgeom.npy")
            pgeom_view = views.get(f"bb_pool.{hand}.pgeom")
            m = con.execute(
                f"SELECT {', '.join(base_cols + opt_cols)} FROM read_parquet('{meta_path}')"
            ).fetchnumpy()
            bb_pid_view = views.get(f"bb_pool.{hand}.batter_id")
            bb_ssn_view = views.get(f"bb_pool.{hand}.season")
            bb_rh_view = views.get(f"bb_pool.{hand}.result_hits")
            bb_ro_view = views.get(f"bb_pool.{hand}.result_outs")
            bb_rec_view = views.get(f"bb_pool.{hand}.recency")
            bb_pools[hand] = BattedBallPool(
                game_ymd=(
                    np.asarray(np.ma.filled(m["game_ymd"], 0), dtype=np.int32)
                    if "game_ymd" in m
                    else None
                ),
                geom=take_view(
                    views, f"bb_pool.{hand}.geom", os.path.join(bb_dir, f"{hand}.geom.npy")
                ),
                sit=take_view(
                    views, f"bb_pool.{hand}.sit", os.path.join(bb_dir, f"{hand}.sit.npy")
                ),
                batter_id=(
                    bb_pid_view
                    if isinstance(bb_pid_view, np.ndarray)
                    else np.asarray(np.ma.filled(m["batter_id"], 0), dtype=np.int64)
                ),
                season=(
                    bb_ssn_view
                    if isinstance(bb_ssn_view, np.ndarray)
                    else np.asarray(np.ma.filled(m["season"], 0), dtype=np.int64)
                ),
                event=np.asarray(m["events"], dtype=object),
                result_hits=(
                    bb_rh_view
                    if isinstance(bb_rh_view, np.ndarray)
                    else np.asarray(np.ma.filled(m["result_hits"], 0), dtype=np.int8)
                ),
                result_outs=(
                    bb_ro_view
                    if isinstance(bb_ro_view, np.ndarray)
                    else np.asarray(np.ma.filled(m["result_outs"], 0), dtype=np.int8)
                ),
                recency=(
                    bb_rec_view
                    if isinstance(bb_rec_view, np.ndarray)
                    else np.nan_to_num(
                        np.ma.filled(m["recency_weight"], 1.0).astype(np.float32),
                        nan=1.0,
                    )
                ),
                # SIM-413: pitcher hand per row (object 'L'/'R', not shared —
                # object-dtype like ``event``). None on a legacy artifact.
                p_throws=(np.asarray(m["p_throws"], dtype=object) if "p_throws" in m else None),
                # SIM-411 / SIM-425b numeric realism columns (shared when
                # published; None on a pre-0012 bundle).
                venue_id=_bb_take(views, hand, m, "venue_id", "venue_id", np.int64, 0),
                fielder_pos=_bb_take(
                    views, hand, m, "fielder_pos", "fielded_by_position", np.int8, 0
                ),
                fielder_id=_bb_take(views, hand, m, "fielder_id", "fielder_player_id", np.int64, 0),
                # SIM-510: the transition destinations (-1 = no runner on
                # that base pre-pitch; None on a pre-0018 bundle).
                r1_dest=_bb_take(views, hand, m, "r1_dest", "runner_1b_dest", np.int8, -1),
                r2_dest=_bb_take(views, hand, m, "r2_dest", "runner_2b_dest", np.int8, -1),
                r3_dest=_bb_take(views, hand, m, "r3_dest", "runner_3b_dest", np.int8, -1),
                batter_dest=_bb_take(views, hand, m, "batter_dest", "batter_dest", np.int8, -1),
                dest_ok=_bb_take(views, hand, m, "dest_ok", "dest_ok", np.int8, 0),
                # SIM-511: the normalization facts + geometry.
                r1_adv_out=_bb_take(views, hand, m, "r1_adv_out", "r1_adv_out", np.int8, 0),
                r2_adv_out=_bb_take(views, hand, m, "r2_adv_out", "r2_adv_out", np.int8, 0),
                r3_adv_out=_bb_take(views, hand, m, "r3_adv_out", "r3_adv_out", np.int8, 0),
                is_air=_bb_take(views, hand, m, "is_air", "is_air", np.int8, 0),
                spray_raw=_bb_take(views, hand, m, "spray_raw", "spray_raw", np.float32, 0),
                hit_dist=_bb_take(views, hand, m, "hit_dist", "hit_dist", np.float32, 0),
                # SIM-491: the batting side (None on a pre-0019 bundle).
                bat_home=_bb_take(views, hand, m, "bat_home", "bat_home", np.int8, 0),
                # SIM-523 part G: the fielding chain (None on an older bundle).
                fielders=_bb_fielders(views, hand, m),
                putout_mask=_bb_take(views, hand, m, "putout_mask", "putout_pos_mask", np.int16, 0),
                assist_mask=_bb_take(views, hand, m, "assist_mask", "assist_pos_mask", np.int16, 0),
                # SIM-518 (SIM-463): the producing pitch (None on a
                # pre-sim518 export).
                pgeom=(
                    pgeom_view
                    if isinstance(pgeom_view, np.ndarray)
                    else (
                        np.load(pgeom_path, allow_pickle=False)
                        if os.path.exists(pgeom_path)
                        else None
                    )
                ),
                pitcher_id=_bb_take(views, hand, m, "pitcher_id", "pitcher_id", np.int64, 0),
                # SIM-523 part C: the batted-ball class (None before part C).
                bb_class=_bb_take(views, hand, m, "bb_class", "bb_class", np.int8, 0),
            )
    return bb_pools
