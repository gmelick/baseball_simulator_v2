"""Export the pitcher-by-pitcher similarity matrix the pitch draw weights by, and load it.

The export writes ``pitcher_sim.npz``: the profile index and the scores as JSON strings,
and the same scores as a dense float32 matrix.
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np

from bundle.common import _savez_plain

log = logging.getLogger("engine_artifacts")


def build_pitcher_sim_matrix(
    duckdb_path: str, out_dir: str, seasons: list[int], limit: int | None = None
) -> int:
    """Score each pitcher profile in ``seasons`` against the same-hand profiles; write the npz.

    The pitcher engine gives the composite similarity. The full run takes about an hour;
    ``limit`` scores only the first N profiles, for a check. Returns the number of
    profiles scored.
    """
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
    # A DENSE (n_prof x n_prof) float32 matrix sits beside the dict. The dict of dicts
    # costs gigabytes per process, and shared memory cannot hold it, so every worker
    # would keep a private copy and the host would run out of memory. The dense matrix
    # is small, and all the workers attach one read-only shared view. Row i holds query
    # profile i's scores (M[i, j] = sim(i, j), 0.0 when absent): the same values the
    # dict gives, so the draw is the same either way. The dict stays for an older loader.
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
) -> tuple[dict[str, int], np.ndarray | None]:
    """Load the pitcher similarity index and its dense matrix.

    Returns ({}, None) when the bundle has no file. The matrix is None for an npz
    without it.
    """
    ps_index: dict[str, int] = {}
    ps_matrix: np.ndarray | None = None
    ps_path = os.path.join(art_dir, "pitcher_sim.npz")
    if os.path.exists(ps_path):
        # A worker takes the dense matrix from the shared view; the parent reads it from
        # the npz. Either way the loader reads only the small ``index`` from disk and
        # never parses the large ``sims`` JSON.
        mv = views.get("pitcher_sim.matrix")
        # The bundle never carries a pickle. Every load here refuses
        # one, and ``_savez_plain`` keeps the builder from writing one.
        z = np.load(ps_path, allow_pickle=False)
        ps_index = json.loads(str(z["index"]))
        if isinstance(mv, np.ndarray):
            ps_matrix = mv
        elif "pitcher_sim_matrix" in z.files:
            ps_matrix = z["pitcher_sim_matrix"]
    return ps_index, ps_matrix
