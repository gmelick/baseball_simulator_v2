"""The per-hand batted-ball pool the fielding draw reads."""

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
    join_rows,
    meta_column,
    optional_meta_column,
    recency_column,
    take_view,
    write_json,
    ymd_select,
)
from bundle.worker_pools import BattedBallPool
from profiles.sql import sql_seasons

log = logging.getLogger("engine_artifacts")


_BB_GEOM_COLS = ["exit_velo", "launch_angle", "pull_relative_spray_angle"]

#: The batted-ball CLASS vocabulary (``BattedBallPool.bb_class``).
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
    """Write the per-hand batted-ball pool over the season window: the
    geometry (exit velocity, launch angle, spray), the situation and the
    metadata (batter, event, result deltas)."""
    pool_dir = os.path.join(out_dir, "battedball_pool")
    os.makedirs(pool_dir, exist_ok=True)
    season_list = sql_seasons(seasons)
    where = (
        f"stand='%s' AND season IN ({season_list}) AND exit_velo IS NOT NULL "
        "AND launch_angle IS NOT NULL AND pull_relative_spray_angle IS NOT NULL"
    )
    counts: dict[str, int] = {}
    # The transition columns exist only on a 0018-migrated DB. Export
    # them when present; a pre-0018 DB still exports the legacy shape. The
    # export keeps EVERY row: the fielding draw's cell index is what excludes
    # the ``dest_ok = 0`` rows.
    op_cols = {str(d[0]) for d in con.execute("SELECT * FROM sim.outcome_pool LIMIT 0").description}
    has_transition = op_cols >= _BB_TRANSITION_SOURCE_COLS
    # The batting side exists only on a 0019-migrated DB. Same
    # back-compatible pattern as the transition columns.
    bat_home_select = ", bat_home" if "bat_home" in op_cols else ""
    transition_select = (
        ", runner_1b_dest, runner_2b_dest, runner_3b_dest, batter_dest, "
        "COALESCE(dest_outs_consistent, FALSE) AS dest_ok, "
        # The normalization facts: the thrown-out-advancing flags tell a
        # discretionary out from a force or doubled-off out; is_air marks the
        # tag-up shape (the SAME definition as the advancement pool's scenario
        # 4); the raw spray and distance feed the advancement draw (the
        # batted-ball geometry carries only the PULL-relative spray).
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
    # The ten pitch-feature columns of the pitch that
    # produced each batted ball, plus its pitcher. The pool build selects
    # them from the pitch pool, so a production outcome pool always has them;
    # a reduced synthetic pool may not, hence the probe. The loader treats a
    # missing pgeom file or pitcher column as None (the pitch-similarity
    # kernel then stays neutral).
    has_pgeom = set(_GEOM_COLS) <= op_cols
    pitcher_select = ", pitcher_id" if "pitcher_id" in op_cols else ""
    # The batted-ball class (the fielding draw's third
    # hard filter) — from bb_type when the pool carries it.
    class_select = _BB_CLASS_SELECT if "bb_type" in op_cols else ""
    # The fielding CHAIN — the eight-position alignment and
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
        geom = float_columns(d, _BB_GEOM_COLS)
        sit = float_columns(d, _SIT_COLS)
        np.save(os.path.join(pool_dir, f"{hand}.geom.npy"), geom)
        np.save(os.path.join(pool_dir, f"{hand}.sit.npy"), sit)
        # The pitch-id JOIN, the reverse of the ``pgeom`` export: for every
        # PITCH-pool row (its meta parquet,
        # written by build_pitch_pool_artifact just before), the row of
        # THIS pool that holds its batted ball, or -1. The pitch-result
        # draw's in-play row reads its own batted ball through it.
        bb_pid = np.asarray(np.ma.filled(d["pitch_id"], -1), dtype=np.int64)
        np.save(os.path.join(pool_dir, f"{hand}.pitch_id.npy"), bb_pid)
        pp_meta = os.path.join(out_dir, "pitch_pool", f"{hand}.meta.parquet")
        if os.path.exists(pp_meta):
            bb_row = pitch_join_rows(con, pp_meta, bb_pid)
            np.save(os.path.join(out_dir, "pitch_pool", f"{hand}.bb_row.npy"), bb_row)
            pitch_join[hand] = int((bb_row >= 0).sum())
            log.info(
                "battedball_pool[%s]: pitch join %d of %d pitch rows -> a batted ball",
                hand,
                pitch_join[hand],
                len(bb_row),
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
            # The per-row facts (migration 0012): p_throws (the pitcher's hand),
            # venue_id (the park), and fielded_by_position with fielder_player_id
            # (the fielder). The loader reads a pre-0012 artifact without them.
            # The transition destinations follow, in the same optional way.
            "p_throws, venue_id, fielded_by_position, fielder_player_id"
            f"{transition_select}{bat_home_select}{pitcher_select}{class_select}"
            f"{chain_select} "
            f"{ymd_select(con, 'sim.outcome_pool')}"
            f"FROM sim.outcome_pool WHERE {w}) "
            f"TO '{os.path.join(pool_dir, f'{hand}.meta.parquet')}' (FORMAT parquet)"
        )
        counts[hand] = int(n)
        log.info("battedball_pool[%s]: %d rows (seasons %s)", hand, n, seasons)
    write_json(
        os.path.join(pool_dir, "manifest.json"),
        {
            "seasons": seasons,
            "counts": counts,
            "geom_cols": _BB_GEOM_COLS,
            "sit_cols": _SIT_COLS,
            "transition": has_transition,
            # The pitch-geometry columns of {hand}.pgeom.npy.
            "pgeom_cols": _GEOM_COLS if has_pgeom else [],
            # Pitch rows joined to a batted ball, per hand
            # (the join file lives in pitch_pool/{hand}.bb_row.npy).
            "pitch_join": pitch_join,
            # The fielding chain columns are in the parquet.
            "chain": has_chain,
        },
    )
    return counts


def pitch_join_rows(
    con: duckdb.DuckDBPyConnection, meta_path: str, bb_pid: np.ndarray
) -> np.ndarray:
    """For every row of the pitch pool's meta parquet ``meta_path``, the row of
    the batted-ball pool (pitch ids ``bb_pid``) that holds its batted ball, or -1."""
    pp_pid = np.asarray(
        np.ma.filled(
            con.execute(f"SELECT pitch_id FROM read_parquet('{meta_path}')").fetchnumpy()[
                "pitch_id"
            ],
            -1,
        ),
        dtype=np.int64,
    )
    return join_rows(pp_pid, bb_pid)


#: The outcome-pool columns the transition export needs. Presence of
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


def _bb_fielders(views: dict[str, np.ndarray], hand: str, meta: dict) -> np.ndarray | None:
    """The (N, 8) alignment from fielder_2..fielder_9
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
            # Select the realism columns only when present,
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
                    # The transition destinations.
                    "runner_1b_dest",
                    "runner_2b_dest",
                    "runner_3b_dest",
                    "batter_dest",
                    "dest_ok",
                    # The normalization facts + geometry.
                    "r1_adv_out",
                    "r2_adv_out",
                    "r3_adv_out",
                    "is_air",
                    "spray_raw",
                    "hit_dist",
                    # The batting side.
                    "bat_home",
                    # The producing pitch's pitcher.
                    "pitcher_id",
                    # The batted-ball class.
                    "bb_class",
                    # The alignment + the fielding chain.
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
                    # The game date, for the draw-time cutoff.
                    "game_ymd",
                )
                if c in avail
            ]
            # The producing pitch's geometry file
            # (absent on a pre-sim518 export -> None).
            pgeom_path = os.path.join(bb_dir, f"{hand}.pgeom.npy")
            pgeom_view = views.get(f"bb_pool.{hand}.pgeom")
            m = con.execute(
                f"SELECT {', '.join(base_cols + opt_cols)} FROM read_parquet('{meta_path}')"
            ).fetchnumpy()
            p = f"bb_pool.{hand}"
            bb_pools[hand] = BattedBallPool(
                game_ymd=game_ymd_column(m),
                geom=take_view(
                    views, f"bb_pool.{hand}.geom", os.path.join(bb_dir, f"{hand}.geom.npy")
                ),
                sit=take_view(
                    views, f"bb_pool.{hand}.sit", os.path.join(bb_dir, f"{hand}.sit.npy")
                ),
                batter_id=meta_column(views, f"{p}.batter_id", m, "batter_id", np.int64),
                season=meta_column(views, f"{p}.season", m, "season", np.int64),
                event=np.asarray(m["events"], dtype=object),
                result_hits=meta_column(views, f"{p}.result_hits", m, "result_hits", np.int8),
                result_outs=meta_column(views, f"{p}.result_outs", m, "result_outs", np.int8),
                recency=recency_column(views, f"{p}.recency", m),
                # Pitcher hand per row (object 'L'/'R', not shared —
                # object-dtype like ``event``). None on a legacy artifact.
                p_throws=(np.asarray(m["p_throws"], dtype=object) if "p_throws" in m else None),
                # The numeric per-row facts (shared when published; None on
                # a pre-0012 bundle).
                venue_id=optional_meta_column(views, f"{p}.venue_id", m, "venue_id", np.int64),
                fielder_pos=optional_meta_column(
                    views, f"{p}.fielder_pos", m, "fielded_by_position", np.int8
                ),
                fielder_id=optional_meta_column(
                    views, f"{p}.fielder_id", m, "fielder_player_id", np.int64
                ),
                # The transition destinations (-1 = no runner on
                # that base pre-pitch; None on a pre-0018 bundle).
                r1_dest=optional_meta_column(
                    views, f"{p}.r1_dest", m, "runner_1b_dest", np.int8, -1
                ),
                r2_dest=optional_meta_column(
                    views, f"{p}.r2_dest", m, "runner_2b_dest", np.int8, -1
                ),
                r3_dest=optional_meta_column(
                    views, f"{p}.r3_dest", m, "runner_3b_dest", np.int8, -1
                ),
                batter_dest=optional_meta_column(
                    views, f"{p}.batter_dest", m, "batter_dest", np.int8, -1
                ),
                dest_ok=optional_meta_column(views, f"{p}.dest_ok", m, "dest_ok", np.int8),
                # The normalization facts + geometry.
                r1_adv_out=optional_meta_column(views, f"{p}.r1_adv_out", m, "r1_adv_out", np.int8),
                r2_adv_out=optional_meta_column(views, f"{p}.r2_adv_out", m, "r2_adv_out", np.int8),
                r3_adv_out=optional_meta_column(views, f"{p}.r3_adv_out", m, "r3_adv_out", np.int8),
                is_air=optional_meta_column(views, f"{p}.is_air", m, "is_air", np.int8),
                spray_raw=optional_meta_column(views, f"{p}.spray_raw", m, "spray_raw", np.float32),
                hit_dist=optional_meta_column(views, f"{p}.hit_dist", m, "hit_dist", np.float32),
                # The batting side (None on a pre-0019 bundle).
                bat_home=optional_meta_column(views, f"{p}.bat_home", m, "bat_home", np.int8),
                # The fielding chain (None on an older bundle).
                fielders=_bb_fielders(views, hand, m),
                putout_mask=optional_meta_column(
                    views, f"{p}.putout_mask", m, "putout_pos_mask", np.int16
                ),
                assist_mask=optional_meta_column(
                    views, f"{p}.assist_mask", m, "assist_pos_mask", np.int16
                ),
                # The producing pitch (None on a
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
                pitcher_id=optional_meta_column(
                    views, f"{p}.pitcher_id", m, "pitcher_id", np.int64
                ),
                # The batted-ball class (None on an older bundle).
                bb_class=optional_meta_column(views, f"{p}.bb_class", m, "bb_class", np.int8),
            )
    return bb_pools
