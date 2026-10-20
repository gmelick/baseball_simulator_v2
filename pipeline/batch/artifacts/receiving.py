"""The catcher receiving document: each catcher's framing and blocking
multipliers on taken pitches.
"""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

log = logging.getLogger("engine_artifacts")


# ---------------------------------------------------------------------------
# SIM-523 part E — the catcher RECEIVING ratio (the ball-strike ratio on taken pitches)
# ---------------------------------------------------------------------------
#
# The receiving factor of the pitch-result draw: on TAKEN pitches (called
# strikes and balls) only, a called-strike row × the live catcher's framing
# multiplier at the row's zone group — his called-strike rate over the
# group's league rate — and a ball row × the mirror, (1 − L·m) / (1 − L), so
# the taken group's expected mass is unchanged; blocking the same way on the
# got-away rows among taken pitches with the catcher's got-aways ABOVE
# EXPECTATION (his actual over the league's expected for the pitches he
# received, by pitch height and zone — never the raw got-away rate, which the
# 2026-09-08 tests measured as a staff-wildness reading). Both rates are
# shrunk toward the league (a prior of RECV_FRAME_PRIOR taken pitches per
# group; RECV_BLOCK_PRIOR expected got-aways). Built from the pool itself, so
# the rows' zones and the catchers' rates share one definition.

RECV_FRAME_PRIOR = 200.0
RECV_BLOCK_PRIOR = 5.0
RECV_FRAME_GROUPS = ("heart", "edge", "outside")
RECV_PZ_EDGES = (0.5, 1.0, 1.5)
RECV_N_BLOCK_CELLS = 8


def recv_zone_group(zone: np.ndarray) -> np.ndarray:
    """The zone group of every pitch: 1 the heart (Statcast zone 5), 2 the
    in-zone edge (zones 1-4 and 6-9), 3 outside (zones 11-14), 0 unknown."""
    z = np.asarray(zone, dtype=np.int64)
    out = np.zeros(len(z), dtype=np.int8)
    out[z == 5] = 1
    out[((z >= 1) & (z <= 4)) | ((z >= 6) & (z <= 9))] = 2
    out[(z >= 11) & (z <= 14)] = 3
    return out


def recv_block_cell(zone: np.ndarray, plate_z: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """The blocking cell of every pitch — the pitch-height bucket (below 0.5
    ft, below 1.0, below 1.5, higher) times in-zone or not — 0..7, or -1 when
    the pitch's geometry is incomplete or its zone unknown."""
    z = np.asarray(zone, dtype=np.int64)
    pz = np.asarray(plate_z, dtype=np.float32)
    bucket = np.digitize(pz, RECV_PZ_EDGES).astype(np.int64)
    in_zone = ((z >= 1) & (z <= 9)).astype(np.int64)
    cell = bucket * 2 + in_zone
    known = np.asarray(valid, dtype=bool) & (z > 0)
    return np.where(known, cell, -1).astype(np.int8)


def build_receiving_profiles(
    con: duckdb.DuckDBPyConnection, out_dir: str, seasons: list[int]
) -> dict:
    """Write ``<out_dir>/receiving.json``: the league called-strike rate per
    zone group and got-away rate per blocking cell over taken pitches, and
    every catcher-season's framing multipliers and blocking ratio."""
    season_list = ", ".join(str(int(s)) for s in seasons)
    pz_case = (
        f"(CASE WHEN plate_z < {RECV_PZ_EDGES[0]} THEN 0 WHEN plate_z < {RECV_PZ_EDGES[1]} THEN 1 "
        f"WHEN plate_z < {RECV_PZ_EDGES[2]} THEN 2 ELSE 3 END) * 2 "
        "+ CAST(zone BETWEEN 1 AND 9 AS INTEGER)"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _recv AS
        SELECT COALESCE(catcher_id, 0) AS catcher_id, season,
               CASE WHEN zone = 5 THEN 1 WHEN zone BETWEEN 1 AND 9 THEN 2
                    WHEN zone BETWEEN 11 AND 14 THEN 3 ELSE 0 END AS g,
               CASE WHEN plate_z IS NULL OR velo IS NULL OR velo <= 0 OR zone IS NULL OR zone <= 0
                    THEN -1 ELSE {pz_case} END AS c,
               CAST(outcome_type = 'called_strike' AS INTEGER) AS cs,
               CAST(COALESCE(got_away, FALSE) AS INTEGER) AS ga
        FROM sim.pitch_pool
        WHERE season IN ({season_list}) AND outcome_type IN ('called_strike', 'ball')
        """
    )
    # The league rates PER SEASON (the outside called-strike rate fell from
    # 7.1% in 2023-24 to 4.1-4.5% in 2025-26, so a pooled rate would call every
    # 2024 catcher a good framer and every 2026 catcher a poor one), plus the
    # pooled rates as the fallback for a row season the document lacks.
    league_frame: dict[str, dict[str, float]] = {}
    league_frame_all: dict[str, float] = {}
    for season, g, n, cs in con.execute(
        "SELECT season, g, count(*), sum(cs) FROM _recv WHERE g > 0 GROUP BY 1, 2"
    ).fetchall():
        league_frame.setdefault(str(int(season)), {})[RECV_FRAME_GROUPS[int(g) - 1]] = (
            float(cs) / float(n) if n else 0.0
        )
    for g, n, cs in con.execute(
        "SELECT g, count(*), sum(cs) FROM _recv WHERE g > 0 GROUP BY g"
    ).fetchall():
        league_frame_all[RECV_FRAME_GROUPS[int(g) - 1]] = float(cs) / float(n) if n else 0.0
    league_block: dict[str, list[float]] = {}
    league_block_all = [0.0] * RECV_N_BLOCK_CELLS
    for season, c, n, ga in con.execute(
        "SELECT season, c, count(*), sum(ga) FROM _recv WHERE c >= 0 GROUP BY 1, 2"
    ).fetchall():
        league_block.setdefault(str(int(season)), [0.0] * RECV_N_BLOCK_CELLS)[int(c)] = (
            float(ga) / float(n) if n else 0.0
        )
    for c, n, ga in con.execute(
        "SELECT c, count(*), sum(ga) FROM _recv WHERE c >= 0 GROUP BY c"
    ).fetchall():
        league_block_all[int(c)] = float(ga) / float(n) if n else 0.0
    frame_rows = con.execute(
        "SELECT catcher_id, season, g, count(*), sum(cs) FROM _recv "
        "WHERE catcher_id > 0 AND g > 0 GROUP BY 1, 2, 3"
    ).fetchall()
    block_rows = con.execute(
        "SELECT catcher_id, season, c, count(*), sum(ga) FROM _recv "
        "WHERE catcher_id > 0 AND c >= 0 GROUP BY 1, 2, 3"
    ).fetchall()
    con.execute("DROP TABLE _recv")
    catchers: dict[str, dict] = {}
    for cid, season, g, n, cs in frame_rows:
        e = catchers.setdefault(f"{int(cid)}:{int(season)}", {"frame": {}, "taken": 0})
        name = RECV_FRAME_GROUPS[int(g) - 1]
        L = league_frame.get(str(int(season)), league_frame_all).get(name, 0.0)
        rate = (float(cs) + L * RECV_FRAME_PRIOR) / (float(n) + RECV_FRAME_PRIOR)
        m = rate / L if L > 0.0 else 1.0
        hi = (1.0 / L) if L > 0.0 else 1.0  # keeps the ball rows' mirror >= 0
        e["frame"][name] = round(float(min(max(m, 0.0), hi)), 5)
        e["taken"] += int(n)
    for cid, season, c, n, ga in block_rows:
        e = catchers.setdefault(f"{int(cid)}:{int(season)}", {"frame": {}, "taken": 0})
        e["got_away"] = e.get("got_away", 0) + int(ga)
        cells = league_block.get(str(int(season)), league_block_all)
        e["expected"] = e.get("expected", 0.0) + cells[int(c)] * float(n)
    g_max = max(league_block_all) if league_block_all else 0.0
    b_hi = (1.0 / g_max) if g_max > 0.0 else 5.0
    for e in catchers.values():
        for name in RECV_FRAME_GROUPS:
            e["frame"].setdefault(name, 1.0)
        actual = float(e.get("got_away", 0))
        expected = float(e.get("expected", 0.0))
        b = (actual + RECV_BLOCK_PRIOR) / (expected + RECV_BLOCK_PRIOR)
        e["block"] = round(float(min(max(b, 0.0), b_hi)), 5)
        e["expected"] = round(expected, 3)
    doc = {
        "seasons": [int(s) for s in seasons],
        "frame_prior": RECV_FRAME_PRIOR,
        "block_prior": RECV_BLOCK_PRIOR,
        "groups": list(RECV_FRAME_GROUPS),
        "pz_edges": list(RECV_PZ_EDGES),
        "league_frame": league_frame,
        "league_block": league_block,
        "league_frame_all": league_frame_all,
        "league_block_all": league_block_all,
        "catchers": catchers,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "receiving.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    log.info(
        "receiving: %d catcher-seasons; league called-strike rate per group %s; got-away per cell %s",
        len(catchers),
        {s: {k: round(v, 4) for k, v in d.items()} for s, d in league_frame.items()},
        [round(v, 4) for v in league_block_all],
    )
    return doc


def load_receiving(art_dir: str) -> dict | None:
    """Load the receiving document; None when the bundle has none."""
    receiving: dict | None = None
    rv_path = os.path.join(art_dir, "receiving.json")
    if os.path.exists(rv_path):
        with open(rv_path, encoding="utf-8") as fh:
            receiving = json.load(fh)
    return receiving
