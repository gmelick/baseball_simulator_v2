"""
pipeline/batch/engine_artifacts.py
==================================
SIM-422 — Fork-safe engine-artifact bundle (the foundation of the
similarity-engine-wiring epic; see
``docs/architecture/2026-09-03-engine-wiring-and-full-pool-scoring.md``).

The live sim runs in ProcessPool workers that cannot receive the app-process
engine objects, so every engine's contribution must reach the worker as a disk
artifact (generalizing the SIM-421 deriver-from-disk pattern). This module is the
NIGHTLY BUILDER for that bundle; the per-worker LOADER is :class:`EngineArtifacts`
below.

Built here (incrementally; more engines land with SIM-424/425/426/427):
  * **Full per-hand pitch pool** (3-season recency floor) — the geometry + situation
    + per-row metadata the full-pool scorer iterates. ONE pool per batter hand; no
    pitcher/season hard filter (pitcher hand self-zeroes via the pitcher engine).
  * **Pitcher×pitcher similarity matrix** — same-hand W2+RBF composite from the
    SIM-075 pitcher engine; the keystone that lets pitcher similarity *weight* the
    pitch draw instead of hard-filtering on ``pitcher_id``.

Layout (under ``${BASEBALL_PLAY_POOL_DIR}/engine_artifacts/``):
  pitch_pool/<hand>.geom.npy      (N,10) float32 raw geometry
  pitch_pool/<hand>.sit.npy       (N,6)  float32 situation (balls,strikes,outs,
                                          runners_state,inning,score_diff)
  pitch_pool/<hand>.meta.parquet  pitch_id,pitcher_id,batter_id,season,outcome_type,recency
  pitcher_sim.npz                 profiles[(pid,season)] + same-hand similarity rows
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass

import duckdb
import numpy as np

log = logging.getLogger("engine_artifacts")

_GEOM_COLS = [
    "velo",
    "ivb",
    "hb",
    "spin_rate",
    "spin_axis",
    "release_x",
    "release_z",
    "release_ext",
    "plate_x",
    "plate_z",
]
_SIT_COLS = ["count_balls", "count_strikes", "outs", "runners_state", "inning", "score_diff"]

DEFAULT_POOL_DIR = os.environ.get("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")

#: SIM-423 perf gate: a hard recency floor keeps the per-hand pool ~1M rows so the
#: full-pool factorized draw clears the 2 s/game SLA (the 3-season choice).
RECENCY_FLOOR_SEASONS = 3


def last_n_seasons(con: duckdb.DuckDBPyConnection, n: int = RECENCY_FLOOR_SEASONS) -> list[int]:
    seasons = [
        int(r[0])
        for r in con.execute(
            "SELECT DISTINCT season FROM sim.pitch_pool ORDER BY season DESC"
        ).fetchall()
    ]
    return sorted(seasons[:n])


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
        con.execute(
            f"COPY (SELECT pitch_id, pitcher_id, batter_id, season, outcome_type, recency_weight "
            f"FROM sim.pitch_pool WHERE stand='{hand}' AND season IN ({season_list})) "
            f"TO '{os.path.join(pool_dir, f'{hand}.meta.parquet')}' (FORMAT parquet)"
        )
        counts[hand] = int(n)
        log.info("pitch_pool[%s]: %d rows (seasons %s)", hand, n, seasons)
    with open(os.path.join(pool_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"seasons": seasons, "counts": counts, "geom_cols": _GEOM_COLS, "sit_cols": _SIT_COLS},
            fh,
            indent=2,
        )
    return counts


def build_pitcher_sim_matrix(
    duckdb_path: str, out_dir: str, seasons: list[int], limit: int | None = None
) -> int:
    """Export the same-hand pitcher×pitcher composite similarity from the SIM-075
    engine. The FULL run is the ~1 h nightly job (~1.2 s/query × ~3k profiles);
    ``limit`` scores only the first N profiles (verification mode)."""
    from similarity.engines.pitcher_similarity import PitcherSimilarityEngine

    engine = PitcherSimilarityEngine(duckdb_path=duckdb_path)
    engine.build()
    profiles = [(pid, s) for (pid, s) in engine._profiles if int(s) in set(seasons)]
    index = {f"{pid}:{s}": i for i, (pid, s) in enumerate(profiles)}
    rows = profiles if limit is None else profiles[:limit]
    sims: dict[str, dict[str, float]] = {}
    for pid, s in rows:
        results = engine.query(pid, s)  # all same-hand profiles, sorted
        sims[f"{pid}:{s}"] = {f"{r.pitcher_id}:{r.season}": float(r.score) for r in results}
    os.makedirs(out_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(out_dir, "pitcher_sim.npz"),
        index=json.dumps(index),
        sims=json.dumps(sims),
    )
    log.info("pitcher_sim: %d query profiles scored (of %d total)", len(rows), len(profiles))
    return len(rows)


# ============================================================================
# Per-worker LOADER (the read side; fork-safe — everything comes off disk)
# ============================================================================


@dataclass
class HandPool:
    """One batter-hand's resident candidate pool for full-pool scoring (SIM-423)."""

    geom: np.ndarray  # (N, 10) float32 — raw geometry (kernel applied at sample time)
    sit: np.ndarray  # (N, 6)  float32 — situation (balls,strikes,outs,runners,inning,score_diff)
    pitcher_id: np.ndarray  # (N,) int64
    batter_id: np.ndarray  # (N,) int64
    outcome_type: np.ndarray  # (N,) object — ball/called_strike/swinging_strike/foul/in_play
    recency: np.ndarray  # (N,) float32

    @property
    def n(self) -> int:
        return int(self.geom.shape[0])


class EngineArtifacts:
    """Per-worker loader for the SIM-422 bundle (resident, built entirely from disk
    so nothing live crosses the ProcessPool fork).  Holds both batter-hand pools +
    (when present) the pitcher×pitcher similarity lookup; the SIM-423 sampler reads
    these to assemble the factorized full-pool weights."""

    def __init__(self, pools, pitcher_sim_index=None, pitcher_sim=None, seasons=None):
        self.pools: dict[str, HandPool] = pools
        self.pitcher_sim_index: dict[str, int] = pitcher_sim_index or {}
        self.pitcher_sim: dict[str, dict[str, float]] = pitcher_sim or {}
        self.seasons: list[int] = seasons or []

    @classmethod
    def load(cls, art_dir: str) -> EngineArtifacts:
        pool_dir = os.path.join(art_dir, "pitch_pool")
        with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        con = duckdb.connect(":memory:")
        pools: dict[str, HandPool] = {}
        try:
            for hand in ("L", "R"):
                geom = np.load(os.path.join(pool_dir, f"{hand}.geom.npy"))
                sit = np.load(os.path.join(pool_dir, f"{hand}.sit.npy"))
                meta_path = os.path.join(pool_dir, f"{hand}.meta.parquet")
                m = con.execute(
                    "SELECT pitcher_id, batter_id, outcome_type, recency_weight "
                    f"FROM read_parquet('{meta_path}')"
                ).fetchnumpy()
                pools[hand] = HandPool(
                    geom=geom,
                    sit=sit,
                    pitcher_id=np.asarray(np.ma.filled(m["pitcher_id"], 0), dtype=np.int64),
                    batter_id=np.asarray(np.ma.filled(m["batter_id"], 0), dtype=np.int64),
                    outcome_type=np.asarray(m["outcome_type"], dtype=object),
                    recency=np.nan_to_num(
                        np.ma.filled(m["recency_weight"], 1.0).astype(np.float32), nan=1.0
                    ),
                )
        finally:
            con.close()
        ps_index, ps_sims = {}, {}
        ps_path = os.path.join(art_dir, "pitcher_sim.npz")
        if os.path.exists(ps_path):
            z = np.load(ps_path, allow_pickle=True)
            ps_index = json.loads(str(z["index"]))
            ps_sims = json.loads(str(z["sims"]))
        return cls(pools, ps_index, ps_sims, manifest.get("seasons"))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Build the SIM-422 engine-artifact bundle.")
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB_PATH)
    ap.add_argument("--out-dir", default=os.path.join(DEFAULT_POOL_DIR, "engine_artifacts"))
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="Override the recency-floor seasons (default: last 3).",
    )
    ap.add_argument("--what", choices=["pool", "pitcher_sim", "all"], default="pool")
    ap.add_argument(
        "--pitcher-sim-limit",
        type=int,
        default=None,
        help="Score only N pitcher profiles (verification; omit for full nightly run).",
    )
    args = ap.parse_args(argv)

    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        seasons = args.seasons or last_n_seasons(con)
        if args.what in ("pool", "all"):
            build_pitch_pool_artifact(con, args.out_dir, seasons)
        if args.what in ("pitcher_sim", "all"):
            build_pitcher_sim_matrix(
                args.duckdb_path, args.out_dir, seasons, limit=args.pitcher_sim_limit
            )
    finally:
        con.close()
    log.info("engine-artifact build complete -> %s", args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
