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


_BB_GEOM_COLS = ["exit_velo", "launch_angle", "pull_relative_spray_angle"]


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
    for hand in ("L", "R"):
        w = where % hand
        d = con.execute(
            f"SELECT {', '.join(_BB_GEOM_COLS + _SIT_COLS)} FROM sim.outcome_pool WHERE {w}"
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
        con.execute(
            "COPY (SELECT batter_id, season, events, result_hits, result_outs, result_runs, "
            f"recency_weight FROM sim.outcome_pool WHERE {w}) "
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
            },
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


#: Actor engines whose per-(id, season) season-metrics vector is exported as a v1
#: embedding (the worker z-scores + RBFs it for the f_batter / f_catcher / ... factor).
#: SIM-424/425/426/427 refine these to each engine's exact feature selection/weights.
_ACTOR_TABLES = {
    "batter": "batter_season_metrics",
    "catcher": "catcher_season_metrics",
    "fielder": "fielder_season_metrics",
    "baserunner": "baserunner_season_metrics",
    "manager": "manager_season_metrics",
}
_NUMERIC_TYPES = {"DOUBLE", "FLOAT", "REAL", "INTEGER", "BIGINT", "SMALLINT", "DECIMAL"}


def build_actor_embeddings(con: duckdb.DuckDBPyConnection, out_dir: str) -> dict[str, int]:
    """Export per-(actor_id, season) embeddings + global mean/std for each actor
    engine, so the worker can z-score + RBF-score the actor factor on the fly."""
    os.makedirs(out_dir, exist_ok=True)
    counts: dict[str, int] = {}
    for actor, table in _ACTOR_TABLES.items():
        schema = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema='derived' AND table_name='{table}' ORDER BY ordinal_position"
        ).fetchall()
        id_col = next(c for c, _ in schema if c.endswith("_id") or c == "player_id")
        feats = [
            c
            for c, dt in schema
            if dt.upper() in _NUMERIC_TYPES
            and c not in (id_col, "season")
            and not c.startswith("sample_")
        ]
        d = con.execute(
            f"SELECT {id_col}, season, {', '.join(feats)} FROM derived.{table}"
        ).fetchnumpy()
        ids = np.ma.filled(d[id_col], 0).astype(np.int64)
        seasons = np.ma.filled(d["season"], 0).astype(np.int64)
        keys = [f"{int(i)}:{int(s)}" for i, s in zip(ids, seasons, strict=False)]
        mat = np.stack(
            [np.ma.filled(np.ma.asarray(d[f]).astype(np.float32), np.nan) for f in feats],
            axis=1,
        )
        mean = np.nan_to_num(np.nanmean(mat, axis=0)).astype(np.float32)
        std = np.nan_to_num(np.nanstd(mat, axis=0), nan=1.0).astype(np.float32)
        std[std == 0.0] = 1.0
        np.savez_compressed(
            os.path.join(out_dir, f"{actor}_emb.npz"),
            keys=json.dumps(keys),
            vecs=np.nan_to_num(mat).astype(np.float32),
            mean=mean,
            std=std,
            features=json.dumps(feats),
        )
        counts[actor] = len(keys)
        log.info("%s_emb: %d (id,season) rows × %d features", actor, len(keys), len(feats))
    return counts


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
    season: np.ndarray  # (N,) int64 — for the (pitcher_id:season) pitcher-sim key
    outcome_type: np.ndarray  # (N,) object — ball/called_strike/swinging_strike/foul/in_play
    recency: np.ndarray  # (N,) float32

    @property
    def n(self) -> int:
        return int(self.geom.shape[0])


@dataclass
class BattedBallPool:
    """One batter-hand's resident batted-ball pool for SIM-425 (step 5/6)."""

    geom: np.ndarray  # (N, 3) float32 — exit_velo, launch_angle, spray
    sit: np.ndarray  # (N, 6)  float32
    batter_id: np.ndarray  # (N,) int64
    season: np.ndarray  # (N,) int64
    event: np.ndarray  # (N,) object
    result_hits: np.ndarray  # (N,) int8 (0=out,1=1B,2=2B,3=3B,4=HR)
    result_outs: np.ndarray  # (N,) int8
    recency: np.ndarray  # (N,) float32

    @property
    def n(self) -> int:
        return int(self.sit.shape[0])


#: SIM-403b — the shareable subset of numpy arrays inside an EngineArtifacts
#: bundle, by flat name. Used for zero-copy publication into
#: ``multiprocessing.shared_memory`` so worker subprocesses don't each copy the
#: ~hundreds-of-MB pool/embedding arrays from disk. The key convention is
#: ``"<bucket>.<sub>.<attr>"`` so the worker can unambiguously route each view
#: back to its slot. Object-dtype arrays (``HandPool.outcome_type`` /
#: ``BattedBallPool.event``) and the pitcher-sim dict-of-dicts are kept
#: per-worker (loaded from disk) — they are not shareable through
#: ``shared_memory`` and live as small picklable Python objects anyway.
_HAND_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "geom",
    "sit",
    "pitcher_id",
    "batter_id",
    "season",
    "recency",
)
_BB_POOL_SHAREABLE_ATTRS: tuple[str, ...] = (
    "geom",
    "sit",
    "batter_id",
    "season",
    "result_hits",
    "result_outs",
    "recency",
)
_ACTOR_EMB_SHAREABLE_ATTRS: tuple[str, ...] = ("vecs", "mean", "std")


class EngineArtifacts:
    """Per-worker loader for the SIM-422 bundle (resident, built entirely from disk
    so nothing live crosses the ProcessPool fork).  Holds both batter-hand pools +
    (when present) the pitcher×pitcher similarity lookup; the SIM-423 sampler reads
    these to assemble the factorized full-pool weights.

    SIM-403b: the big read-only numpy arrays (pitch pool + batted-ball pool +
    actor embeddings) can be PUBLISHED INTO SHARED MEMORY by the parent (via
    :meth:`extract_shared_arrays`) and ATTACHED zero-copy by each worker (via
    :meth:`attach_shared_views`), so a 10-worker pool resident-set drops from
    ``10 × ~300 MB`` to ``~300 MB`` total + per-worker scratch. The shared-mem
    plumbing itself lives in :mod:`simulation.batch_runner` (``shared_arrays=``
    + :func:`_worker_init`); this module just defines the (flat-name -> array)
    contract."""

    def __init__(
        self,
        pools,
        pitcher_sim_index=None,
        pitcher_sim=None,
        seasons=None,
        actor_emb=None,
        bb_pools=None,
    ):
        self.pools: dict[str, HandPool] = pools
        self.bb_pools: dict[str, BattedBallPool] = bb_pools or {}
        self.pitcher_sim_index: dict[str, int] = pitcher_sim_index or {}
        self.pitcher_sim: dict[str, dict[str, float]] = pitcher_sim or {}
        self.seasons: list[int] = seasons or []
        #: actor -> {"key_index": {id:season -> row}, "vecs", "mean", "std", "features"}
        self.actor_emb: dict[str, dict] = actor_emb or {}

    # ------------------------------------------------------------------
    # SIM-403b — shared-memory publish/attach helpers
    # ------------------------------------------------------------------

    def extract_shared_arrays(self) -> dict[str, np.ndarray]:
        """Return the shareable numpy arrays in this bundle keyed by flat name.

        Parent-side: call once after :meth:`load`, hand the result to
        ``BatchRunner(shared_arrays=...)`` so the runner publishes them into
        ``multiprocessing.shared_memory`` ONCE for the warm pool. Each value is
        the same buffer the loaded instance carries (no copy here); the runner
        does the single copy into shared memory.

        Keys:
          * ``"pool.<hand>.<attr>"``      — HandPool attrs in
            :data:`_HAND_POOL_SHAREABLE_ATTRS`.
          * ``"bb_pool.<hand>.<attr>"``   — BattedBallPool attrs in
            :data:`_BB_POOL_SHAREABLE_ATTRS`.
          * ``"actor_emb.<actor>.<attr>"`` — for ``attr`` in
            :data:`_ACTOR_EMB_SHAREABLE_ATTRS` and any actor present in
            :attr:`actor_emb`.

        Object-dtype arrays (``outcome_type`` / ``event``) and the
        ``pitcher_sim`` dict-of-dicts are NOT included — they are not shareable
        through ``shared_memory`` and stay per-worker (each worker loads them
        from disk as today; they're a small fraction of the bundle by size).
        """
        out: dict[str, np.ndarray] = {}
        for hand, pool in self.pools.items():
            for attr in _HAND_POOL_SHAREABLE_ATTRS:
                arr = getattr(pool, attr, None)
                if isinstance(arr, np.ndarray):
                    out[f"pool.{hand}.{attr}"] = arr
        for hand, pool in self.bb_pools.items():
            for attr in _BB_POOL_SHAREABLE_ATTRS:
                arr = getattr(pool, attr, None)
                if isinstance(arr, np.ndarray):
                    out[f"bb_pool.{hand}.{attr}"] = arr
        for actor, emb in self.actor_emb.items():
            for attr in _ACTOR_EMB_SHAREABLE_ATTRS:
                v = emb.get(attr)
                if isinstance(v, np.ndarray):
                    out[f"actor_emb.{actor}.{attr}"] = v
        return out

    def attach_shared_views(self, views: dict[str, np.ndarray]) -> None:
        """In-place replace this bundle's shareable arrays with ``views``.

        Worker-side: after :meth:`load` returns a freshly-disk-loaded bundle,
        call this with the attached shared-memory views (from
        :data:`simulation.batch_runner._WORKER_SHARED` ``["views"]``) so each
        large array is REPLACED by a zero-copy view over the parent's segment.
        The disk-loaded buffers then become unreferenced and the worker's
        resident-set drops to the shared one.

        ``views`` is the same flat-name -> ndarray map :meth:`extract_shared_arrays`
        emits (the runner pickles the segment registry across the fork; the worker
        initializer rebuilds the ndarray views over the attached segments). Keys
        that don't match this bundle's structure are silently ignored — a
        defensive contract so a stale registry from a previous artifact build
        doesn't crash the worker.
        """
        if not views:
            return
        for hand, pool in self.pools.items():
            for attr in _HAND_POOL_SHAREABLE_ATTRS:
                key = f"pool.{hand}.{attr}"
                v = views.get(key)
                if isinstance(v, np.ndarray):
                    setattr(pool, attr, v)
        for hand, pool in self.bb_pools.items():
            for attr in _BB_POOL_SHAREABLE_ATTRS:
                key = f"bb_pool.{hand}.{attr}"
                v = views.get(key)
                if isinstance(v, np.ndarray):
                    setattr(pool, attr, v)
        for actor, emb in self.actor_emb.items():
            for attr in _ACTOR_EMB_SHAREABLE_ATTRS:
                key = f"actor_emb.{actor}.{attr}"
                v = views.get(key)
                if isinstance(v, np.ndarray):
                    emb[attr] = v

    @classmethod
    def load(
        cls, art_dir: str, shared_views: dict[str, np.ndarray] | None = None
    ) -> EngineArtifacts:
        """Load the bundle from disk. When ``shared_views`` is supplied (the
        SIM-403b shared-memory map from :func:`extract_shared_arrays`), the big
        per-pool / per-embedding numpy arrays are taken DIRECTLY from the views
        and the corresponding ``.npy`` disk reads are SKIPPED — turning a
        ~150-300 MB cold-load into a ~5 MB metadata-only read per worker.  This
        is the SIM-402 fix: without it, 9 cold workers all racing to read the
        full bundle from a Windows-hosted Docker volume serializes into a
        ~500 s stall on a 10-iteration request.  When ``shared_views`` is None
        (the dev path / no published bundle), every array still loads from disk
        exactly as before.

        The OBJECT-dtype columns (``outcome_type`` on HandPool, ``event`` on
        BattedBallPool), the metadata-only ids/seasons/recency columns, the
        ``pitcher_sim`` dict-of-dicts, and the per-actor ``key_index`` / feature
        lists are NEVER shareable through ``shared_memory`` and ALWAYS load
        from disk — they're small (typically <5 MB combined per worker).
        """
        views = shared_views or {}

        def _take(key: str, disk_path: str) -> np.ndarray:
            """Either a shared view (zero-cost) or a fresh ``np.load`` (cold)."""
            v = views.get(key)
            if isinstance(v, np.ndarray):
                return v
            return np.load(disk_path)

        pool_dir = os.path.join(art_dir, "pitch_pool")
        with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        con = duckdb.connect(":memory:")
        pools: dict[str, HandPool] = {}
        try:
            for hand in ("L", "R"):
                geom = _take(f"pool.{hand}.geom", os.path.join(pool_dir, f"{hand}.geom.npy"))
                sit = _take(f"pool.{hand}.sit", os.path.join(pool_dir, f"{hand}.sit.npy"))
                # The metadata parquet stays disk-loaded — outcome_type is
                # object-dtype (not shareable) and the ids/recency are small.
                meta_path = os.path.join(pool_dir, f"{hand}.meta.parquet")
                m = con.execute(
                    "SELECT pitcher_id, batter_id, season, outcome_type, recency_weight "
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
                    recency=(
                        rec_view
                        if isinstance(rec_view, np.ndarray)
                        else np.nan_to_num(
                            np.ma.filled(m["recency_weight"], 1.0).astype(np.float32),
                            nan=1.0,
                        )
                    ),
                )
            bb_pools: dict[str, BattedBallPool] = {}
            bb_dir = os.path.join(art_dir, "battedball_pool")
            if os.path.exists(os.path.join(bb_dir, "manifest.json")):
                for hand in ("L", "R"):
                    m = con.execute(
                        "SELECT batter_id, season, events, result_hits, result_outs, "
                        f"recency_weight FROM read_parquet('{os.path.join(bb_dir, f'{hand}.meta.parquet')}')"
                    ).fetchnumpy()
                    bb_pid_view = views.get(f"bb_pool.{hand}.batter_id")
                    bb_ssn_view = views.get(f"bb_pool.{hand}.season")
                    bb_rh_view = views.get(f"bb_pool.{hand}.result_hits")
                    bb_ro_view = views.get(f"bb_pool.{hand}.result_outs")
                    bb_rec_view = views.get(f"bb_pool.{hand}.recency")
                    bb_pools[hand] = BattedBallPool(
                        geom=_take(
                            f"bb_pool.{hand}.geom", os.path.join(bb_dir, f"{hand}.geom.npy")
                        ),
                        sit=_take(f"bb_pool.{hand}.sit", os.path.join(bb_dir, f"{hand}.sit.npy")),
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
                    )
        finally:
            con.close()
        ps_index, ps_sims = {}, {}
        ps_path = os.path.join(art_dir, "pitcher_sim.npz")
        if os.path.exists(ps_path):
            z = np.load(ps_path, allow_pickle=True)
            ps_index = json.loads(str(z["index"]))
            ps_sims = json.loads(str(z["sims"]))
        actor_emb: dict[str, dict] = {}
        for actor in _ACTOR_TABLES:
            p = os.path.join(art_dir, f"{actor}_emb.npz")
            if os.path.exists(p):
                # When all three shareable arrays for this actor are in views,
                # SKIP the .npz load entirely — load only the keys + features
                # (the small picklable members) from a lightweight read.
                vecs_v = views.get(f"actor_emb.{actor}.vecs")
                mean_v = views.get(f"actor_emb.{actor}.mean")
                std_v = views.get(f"actor_emb.{actor}.std")
                if all(isinstance(v, np.ndarray) for v in (vecs_v, mean_v, std_v)):
                    # Cheap partial-load of the npz: keys + features are tiny.
                    z = np.load(p, allow_pickle=True)
                    keys = json.loads(str(z["keys"]))
                    features = json.loads(str(z["features"]))
                    actor_emb[actor] = {
                        "key_index": {k: i for i, k in enumerate(keys)},
                        "vecs": vecs_v,
                        "mean": mean_v,
                        "std": std_v,
                        "features": features,
                    }
                else:
                    z = np.load(p, allow_pickle=True)
                    keys = json.loads(str(z["keys"]))
                    actor_emb[actor] = {
                        "key_index": {k: i for i, k in enumerate(keys)},
                        "vecs": z["vecs"],
                        "mean": z["mean"],
                        "std": z["std"],
                        "features": json.loads(str(z["features"])),
                    }
        return cls(pools, ps_index, ps_sims, manifest.get("seasons"), actor_emb, bb_pools)


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
    ap.add_argument("--what", choices=["pool", "pitcher_sim", "actors", "all"], default="pool")
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
            build_battedball_pool_artifact(con, args.out_dir, seasons)
        if args.what in ("actors", "all"):
            build_actor_embeddings(con, args.out_dir)
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
