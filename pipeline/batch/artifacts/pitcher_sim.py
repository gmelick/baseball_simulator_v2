"""The pitcher-by-pitcher similarity matrix the pitch draw weights by."""

from __future__ import annotations

import json
import logging
import os

import numpy as np

from pipeline.batch.artifacts.common import _savez_plain

log = logging.getLogger("engine_artifacts")


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
    # SIM-430: also store a DENSE (n_prof x n_prof) float32 similarity matrix.
    # The dict-of-dicts costs ~2 GB resident per process (Python str/float/dict
    # overhead) and is NOT shareable through multiprocessing.shared_memory, so
    # every ProcessPool worker held a full private copy -> 10 workers OOM-killed
    # the host (the SIM-430 fan-out blocker). The dense matrix is ~11 MB for 1677
    # profiles and rides the existing SIM-403b shared-memory seam, so ALL workers
    # attach one read-only view. Row i = the prof_score vector for query profile
    # index i (M[i, j] = sim(i, j), 0.0 when absent) — exactly what _f_pitcher
    # scatters from the dict today, so the drawn outcome is byte-identical. The
    # dict is kept in the npz for back-compat (older loaders + the matrix-absent
    # fallback path).
    n_prof = len(index)
    sim_matrix = np.zeros((n_prof, n_prof), dtype=np.float32)
    for qk, srow in sims.items():
        i = index.get(qk)
        if i is None:
            continue
        for tk, v in srow.items():
            j = index.get(tk)
            if j is not None:
                sim_matrix[i, j] = v
    _savez_plain(
        os.path.join(out_dir, "pitcher_sim.npz"),
        index=json.dumps(index),
        sims=json.dumps(sims),
        pitcher_sim_matrix=sim_matrix,
    )
    log.info("pitcher_sim: %d query profiles scored (of %d total)", len(rows), len(profiles))
    return len(rows)


def load_pitcher_sim(
    art_dir: str, views: dict[str, np.ndarray]
) -> tuple[dict[str, int], dict[str, dict[str, float]], np.ndarray | None]:
    """Load the pitcher similarity index and its dense matrix (or, on a bundle
    without the matrix, the similarity dict)."""
    ps_index: dict[str, int] = {}
    ps_sims: dict[str, dict[str, float]] = {}
    ps_matrix: np.ndarray | None = None
    ps_path = os.path.join(art_dir, "pitcher_sim.npz")
    if os.path.exists(ps_path):
        # SIM-430: prefer the dense matrix. A worker gets it as a shared view
        # (``views["pitcher_sim.matrix"]``); the parent/dev path reads it from
        # the npz. In EITHER case we read ONLY the tiny ``index`` from disk and
        # SKIP json-parsing the ~2 GB ``sims`` dict. The dict is parsed solely
        # on a legacy bundle that has no ``pitcher_sim_matrix`` array.
        mv = views.get("pitcher_sim.matrix")
        # SIM-565: the bundle never carries a pickle. Every load here refuses
        # one, and ``_savez_plain`` keeps the builder from writing one.
        z = np.load(ps_path, allow_pickle=False)
        ps_index = json.loads(str(z["index"]))
        if isinstance(mv, np.ndarray):
            ps_matrix = mv
        elif "pitcher_sim_matrix" in z.files:
            ps_matrix = z["pitcher_sim_matrix"]
        else:
            ps_sims = json.loads(str(z["sims"]))
    return ps_index, ps_sims, ps_matrix
