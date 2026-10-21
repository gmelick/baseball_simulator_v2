"""The engine-artifact bundle: the nightly builder and the per-worker loader.

The simulator runs in process-pool workers that cannot receive the app
process's engine objects, so every engine's contribution reaches a worker as
files on disk (the design: docs/technical/architecture.md).
``python -m pipeline.batch.engine_artifacts --what all`` builds the bundle;
:class:`EngineArtifacts` loads it in a worker.

Each artifact has its own module in ``pipeline/batch/artifacts/`` with its
builder and its loader: the per-hand pitch and batted-ball pools, the steal,
advancement and pitching-change pools, the park geometry, the catcher
receiving document, the pitcher-by-pitcher similarity matrix, the actor
embeddings and the reference tables. This module keeps the actor score
matrices (the tests patch ``_ENGINE_LOADER``, ``concentration_report`` and
``_matrix_from_queries`` here), ``EngineArtifacts`` and the entry point.

Without ``--out-dir`` the build writes a new version of the published bundle
and moves the link to it when the build is complete
(``pipeline/batch/bundle_versions.py``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

import duckdb
import numpy as np

from pipeline.batch.artifacts.actor_embeddings import build_actor_embeddings, load_actor_embeddings
from pipeline.batch.artifacts.advancement_pool import (
    build_advancement_pool_artifact,
    load_advancement_pools,
)
from pipeline.batch.artifacts.battedball_pool import (
    build_battedball_pool_artifact,
    load_battedball_pools,
)
from pipeline.batch.artifacts.common import (
    DEFAULT_DUCKDB_PATH,
    DEFAULT_POOL_DIR,
    _savez_plain,
    last_n_seasons,
)
from pipeline.batch.artifacts.manager_pool import build_pitching_change_pool, load_change_pool
from pipeline.batch.artifacts.park_geometry import build_park_geometry, load_park_geometry
from pipeline.batch.artifacts.pitch_pool import build_pitch_pool_artifact, load_pitch_pools
from pipeline.batch.artifacts.pitcher_sim import build_pitcher_sim_matrix, load_pitcher_sim
from pipeline.batch.artifacts.pools import (
    _ACTOR_EMB_SHAREABLE_ATTRS,
    _ADV_POOL_SHAREABLE_ATTRS,
    _BB_POOL_SHAREABLE_ATTRS,
    _CHANGE_POOL_SHAREABLE_ATTRS,
    _HAND_POOL_SHAREABLE_ATTRS,
    _STEAL_POOL_SHAREABLE_ATTRS,
    AdvancementPool,
    BattedBallPool,
    ChangePool,
    HandPool,
    StealPool,
)
from pipeline.batch.artifacts.receiving import build_receiving_profiles, load_receiving
from pipeline.batch.artifacts.reference import (
    IBB_RATES_FILE,
    VENUE_RUN_FACTORS_FILE,
    _read_reference_rows,
    build_reference_tables,
    ibb_rates_from_rows,
    venue_run_factors_from_rows,
)
from pipeline.batch.artifacts.steal_pool import build_steal_pool_artifact, load_steal_pools
from pipeline.batch.bundle_versions import (
    discard_bundle_version,
    prune_bundle_versions,
    publish_bundle_version,
    stage_bundle_version,
)

log = logging.getLogger("engine_artifacts")


# ============================================================================
# The actor SCORE MATRICES (the pitcher pattern, generalized)
# ============================================================================
#
# The play-picker redesign (docs/audit/2026-09-08-sim523-play-picker-redesign-
# plan.md): every actor factor in the draw is its ENGINE's composite
# 0-to-1 score, emitted nightly as a dense matrix and looked up at draw time —
# no distances, no bell curves on the play path. One matrix per actor role:
#
#   batter          the batter engine's composite (pitch draw + fielding draw)
#   catcher         the catcher engine's composite
#   catcher_throwing  its throwing sub-score (the steal draw's catcher-arm factor)
#   fielder_<POS>   the fielder engine's composite, one matrix per position
#   runner_steal    the steal engine's composite (the steal draw's runner factor)
#   runner_adv      the baserunner engine's composite (the advancement draws)
#   pitcher_steal   the pitcher-hold engine's composite (the steal draw)
#
# Each matrix is indexed by the same "id:season" (fielders "id:POS:season") key
# the actor embedding uses, restricted to the artifact's seasons, so the
# sampler maps its existing per-row embedding indices onto matrix columns with
# one lookup table. The diagonal is 1.0 (the same profile); a pair the engine
# did not score is NaN and reads as neutral (1.0) in the sampler.
#
# The CONCENTRATION REPORT runs at build time: for every catcher-season and
# fielder-season, the share of the pool's draw weight his row of the matrix
# would put on his OWN staff's rows against the unweighted share. A distance
# kernel measured 8.9% on the live catcher's own pitches against a pool share
# of 0.56% (2026-09-08); a matrix whose p90 ratio exceeds
# CONCENTRATION_MAX_RATIO fails a --strict build.

#: Engine class per matrix role. Local (not api.state's registry) so the
#: pipeline layer never imports the API layer; a test swaps _ENGINE_LOADER.
_ACTOR_SIM_ENGINES: dict[str, tuple[str, str]] = {
    "batter": ("similarity.engines.batter_similarity", "BatterSimilarityEngine"),
    "catcher": ("similarity.engines.catcher_similarity", "CatcherSimilarityEngine"),
    "fielder": ("similarity.engines.fielder_similarity", "FielderSimilarityEngine"),
    "baserunner_steal": (
        "similarity.engines.baserunner_steal_similarity",
        "BaserunnerStealSimilarityEngine",
    ),
    "baserunner": ("similarity.engines.baserunner_similarity", "BaserunnerSimilarityEngine"),
    "pitcher_steal": (
        "similarity.engines.pitcher_steal_similarity",
        "PitcherStealSimilarityEngine",
    ),
    # The manager (the usage sub-score weights the pitching-change draw).
    "manager": ("similarity.engines.manager_similarity", "ManagerSimilarityEngine"),
}

#: The matrices, each: (engine, the key-tuple -> "a:b[:c]" string, the score
#: attribute on the engine's SimilarityResult, the result's key attributes).
_ACTOR_SIM_SPECS: dict[str, dict] = {
    # The manager's USAGE similarity (owner decision 2026-09-13, SIM-427) —
    # starter pitch counts, pulled-before-100, closer entry leverage, the
    # high-leverage reliever share, opener and bulk rates, available-reliever
    # usage — is the weight on the pitching-change draw. The concentration
    # check does not apply to it by design (the live manager's own rows
    # carrying more weight is the point); the fit's effective-sample floor is
    # its guard.
    "manager_usage": {
        "engine": "manager",
        "score": "usage_score",
        "keys": ("manager_id", "season"),
    },
    "batter": {"engine": "batter", "score": "score", "keys": ("batter_id", "season")},
    "catcher": {"engine": "catcher", "score": "score", "keys": ("catcher_id", "season")},
    "catcher_throwing": {
        "engine": "catcher",
        "score": "throwing_score",
        "keys": ("catcher_id", "season"),
    },
    "runner_steal": {
        "engine": "baserunner_steal",
        "score": "score",
        "keys": ("player_id", "season"),
    },
    "runner_adv": {"engine": "baserunner", "score": "score", "keys": ("player_id", "season")},
    "pitcher_steal": {
        "engine": "pitcher_steal",
        "score": "score",
        "keys": ("pitcher_id", "season"),
    },
}
_FIELDER_POSITIONS = ("1B", "2B", "3B", "SS", "LF", "CF", "RF")
#: The engines that score their THIN profiles too, so the matrices cover
#: every runner-season the pools hold (without them two thirds of the steal
#: rows and half of the advancement rows would have no score).
_THIN_PROFILE_ENGINES = frozenset({"baserunner", "baserunner_steal"})
CONCENTRATION_MAX_RATIO = 3.0


def _default_actor_engine_loader(module_path: str, class_name: str):
    import importlib

    return getattr(importlib.import_module(module_path), class_name)


_ENGINE_LOADER = _default_actor_engine_loader


def _key_of(result, key_attrs: tuple[str, ...]) -> str:
    # A list comprehension, not ``str.join(<generator>)``. The steal-runner
    # matrix build (6.7 million calls) segfaulted three times out of three
    # inside the generator on 2026-09-16 and ran clean in this form. The same
    # generator form then ran clean 8 of 8 that evening, so the fault is
    # stochastic (the crash class of CLAUDE.md §2a). This form is a precaution,
    # not a proven fix (scripts/genexpr_crash_probe.py).
    return ":".join([str(getattr(result, a)) for a in key_attrs])


def _matrix_from_queries(
    profile_keys: list[tuple], query_fn, key_attrs: tuple[str, ...], score_attr: str
) -> tuple[dict[str, int], np.ndarray]:
    """Score every profile against every other via the engine's own query and
    pack the results into a dense float32 matrix (diagonal 1.0, unscored NaN)."""
    index = {":".join(str(x) for x in k): i for i, k in enumerate(profile_keys)}
    n = len(index)
    mat = np.full((n, n), np.nan, dtype=np.float32)
    for i, k in enumerate(profile_keys):
        mat[i, i] = 1.0
        for r in query_fn(*k):
            j = index.get(_key_of(r, key_attrs))
            if j is not None:
                mat[i, j] = float(getattr(r, score_attr))
    return index, mat


def _write_actor_sim(out_dir: str, name: str, index: dict[str, int], mat: np.ndarray) -> None:
    _savez_plain(os.path.join(out_dir, f"{name}.npz"), index=json.dumps(index), matrix=mat)


def build_actor_sim_matrices(
    duckdb_path: str,
    out_dir: str,
    seasons: list[int],
    *,
    limit: int | None = None,
    strict: bool = False,
    only: set[str] | None = None,
) -> dict[str, int]:
    """Export the actor score matrices for ``seasons`` into
    ``<out_dir>/actor_sim/`` and write the concentration report. ``limit``
    scores only the first N profiles per matrix (verification mode). Returns
    the matrix sizes by name.

    ``only`` (matrix names, e.g. ``{"manager_usage"}``) rebuilds those
    matrices alone and MERGES their sizes into the existing manifest — one
    matrix at a time, without paying for the others (a full build segfaulted
    on the batter engine on 2026-09-13; CLAUDE.md §2a). The concentration
    report is not re-run in that mode.
    """
    sim_dir = os.path.join(out_dir, "actor_sim")
    os.makedirs(sim_dir, exist_ok=True)
    season_set = {int(s) for s in seasons}
    sizes: dict[str, int] = {}
    if only:
        manifest_path = os.path.join(sim_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as fh:
                sizes = dict(json.load(fh).get("sizes", {}))
    engines: dict[str, Any] = {}

    def _engine(name: str):
        eng = engines.get(name)
        if eng is None:
            module, cls = _ACTOR_SIM_ENGINES[name]
            eng = _ENGINE_LOADER(module, cls)(duckdb_path=duckdb_path)
            if name in _THIN_PROFILE_ENGINES:
                try:
                    eng.build(seasons=sorted(season_set), include_below_minimum=True)
                except TypeError:  # a stand-in engine without the option
                    eng.build(seasons=sorted(season_set))
            else:
                eng.build(seasons=sorted(season_set))
            engines[name] = eng
        return eng

    for name, spec in _ACTOR_SIM_SPECS.items():
        if only and name not in only:
            continue
        eng = _engine(spec["engine"])
        keys = sorted(k for k in eng._profiles if int(k[-1]) in season_set)
        if limit is not None:
            keys = keys[:limit]
        index, mat = _matrix_from_queries(keys, eng.query, spec["keys"], spec["score"])
        _write_actor_sim(sim_dir, name, index, mat)
        sizes[name] = len(index)
        log.info("actor_sim[%s]: %d x %d", name, len(index), len(index))
    if not only or any(n.startswith("fielder_") for n in only):
        feng = _engine("fielder")
        for pos in _FIELDER_POSITIONS:
            if only and f"fielder_{pos}" not in only:
                continue
            keys = sorted(k for k in feng._profiles if k[1] == pos and int(k[2]) in season_set)
            if limit is not None:
                keys = keys[:limit]
            index, mat = _matrix_from_queries(
                keys, feng.query, ("player_id", "position", "season"), "score"
            )
            _write_actor_sim(sim_dir, f"fielder_{pos}", index, mat)
            sizes[f"fielder_{pos}"] = len(index)
            log.info("actor_sim[fielder_%s]: %d x %d", pos, len(index), len(index))
    with open(os.path.join(sim_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"seasons": sorted(season_set), "sizes": sizes}, fh, indent=2)
    if only:
        return sizes

    report = concentration_report(duckdb_path, sim_dir, sorted(season_set))
    with open(os.path.join(sim_dir, "concentration.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    worst = max((v.get("ratio_p90", 0.0) for v in report.values()), default=0.0)
    if worst > CONCENTRATION_MAX_RATIO:
        msg = (
            f"actor_sim concentration: a matrix's p90 own-staff ratio is {worst:.2f} "
            f"(> {CONCENTRATION_MAX_RATIO}); the draw would lean on the live player's team"
        )
        if strict:
            raise RuntimeError(msg)
        log.warning(msg)
    return sizes


def concentration_share(
    matrix: np.ndarray,
    live_row: int,
    row_actor_col: np.ndarray,
    row_pitcher: np.ndarray,
    staff: set[int],
    power: float = 1.0,
) -> tuple[float, float, float]:
    """For one live profile: (weighted own-staff share, unweighted share, ESS
    share) over pool rows whose actor has a matrix column (``row_actor_col``
    >= 0), the matrix row raised to ``power`` (the fitted power; 1.0 = the
    raw score). Pure arithmetic, unit-tested on a toy pool."""
    valid = row_actor_col >= 0
    if not valid.any():
        return float("nan"), float("nan"), float("nan")
    w = matrix[live_row, row_actor_col[valid]].astype(np.float64)
    w = np.where(np.isfinite(w), w, 1.0)
    if power != 1.0:
        w = np.power(np.clip(w, 0.0, None), float(power))
    own = np.isin(row_pitcher[valid], list(staff))
    total = float(w.sum())
    if total <= 0.0:
        return float("nan"), float(own.mean()), float("nan")
    return float(w[own].sum() / total), float(own.mean()), float(total * total / (w @ w) / w.size)


def concentration_report(
    duckdb_path: str,
    sim_dir: str,
    seasons: list[int],
    powers: dict[str, float] | None = None,
) -> dict[str, dict]:
    """The build-time concentration check for the two actors whose rows carry a
    staff: catchers (the pitch pool) and fielders (the batted-ball pool). Each
    entry reports the distribution over live profiles of the ratio (weighted
    own-staff share / unweighted own-staff share) and of the effective sample
    share of the actor's matrix row over the pool."""
    pw = powers or {}  # The fitted powers (1.0 = the raw score)
    season_list = ", ".join(str(int(s)) for s in seasons)
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        pp = con.execute(
            "SELECT season, catcher_id, pitcher_id FROM sim.pitch_pool "
            f"WHERE season IN ({season_list}) AND catcher_id IS NOT NULL AND catcher_id > 0"
        ).fetchnumpy()
        op_cols = {
            str(d[0]) for d in con.execute("SELECT * FROM sim.outcome_pool LIMIT 0").description
        }
        bb = None
        if {"fielder_player_id", "fielded_by_position", "pitcher_id"} <= op_cols:
            bb = con.execute(
                "SELECT season, fielder_player_id, fielded_by_position, pitcher_id "
                f"FROM sim.outcome_pool WHERE season IN ({season_list}) "
                "AND fielder_player_id IS NOT NULL AND fielder_player_id > 0"
            ).fetchnumpy()
    finally:
        con.close()

    def _load(name: str):
        p = os.path.join(sim_dir, f"{name}.npz")
        if not os.path.exists(p):
            return None, None
        z = np.load(p, allow_pickle=False)
        return json.loads(str(z["index"])), z["matrix"]

    def _summarize(entries: list[tuple[float, float, float]]) -> dict:
        arr = np.asarray([e for e in entries if all(np.isfinite(e))], dtype=np.float64)
        if arr.size == 0:
            return {"profiles": 0}
        ratio = arr[:, 0] / np.maximum(arr[:, 1], 1e-9)
        return {
            "profiles": int(arr.shape[0]),
            "own_share_weighted_median": float(np.median(arr[:, 0])),
            "own_share_unweighted_median": float(np.median(arr[:, 1])),
            "ratio_median": float(np.median(ratio)),
            "ratio_p90": float(np.percentile(ratio, 90)),
            "ratio_max": float(ratio.max()),
            "ess_share_median": float(np.median(arr[:, 2])),
        }

    report: dict[str, dict] = {}
    # --- catchers over the pitch pool
    for name in ("catcher", "catcher_throwing"):
        index, mat = _load(name)
        if index is None:
            continue
        seasons_arr = np.ma.filled(pp["season"], 0).astype(np.int64)
        cid = np.ma.filled(pp["catcher_id"], 0).astype(np.int64)
        pid = np.ma.filled(pp["pitcher_id"], 0).astype(np.int64)
        col = np.fromiter(
            (index.get(f"{c}:{s}", -1) for c, s in zip(cid, seasons_arr, strict=False)),
            dtype=np.int64,
            count=cid.size,
        )
        entries = []
        for key, row in index.items():
            c, s = (int(x) for x in key.split(":"))
            in_season = seasons_arr == s
            staff = set(pid[in_season & (cid == c)].tolist())
            if not staff:
                continue
            entries.append(
                concentration_share(
                    mat, row, col[in_season], pid[in_season], staff, pw.get(name, 1.0)
                )
            )
        report[name] = _summarize(entries)
    # --- fielders over the batted-ball pool, per position
    if bb is not None:
        pos_name = {3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}
        seasons_arr = np.ma.filled(bb["season"], 0).astype(np.int64)
        fid = np.ma.filled(bb["fielder_player_id"], 0).astype(np.int64)
        fpos = np.ma.filled(bb["fielded_by_position"], 0).astype(np.int64)
        pid = np.ma.filled(bb["pitcher_id"], 0).astype(np.int64)
        for pnum, pos in pos_name.items():
            index, mat = _load(f"fielder_{pos}")
            if index is None:
                continue
            at_pos = fpos == pnum
            col = np.fromiter(
                (
                    index.get(f"{f}:{pos}:{s}", -1) if ok else -1
                    for f, s, ok in zip(fid, seasons_arr, at_pos, strict=False)
                ),
                dtype=np.int64,
                count=fid.size,
            )
            entries = []
            for key, row in index.items():
                f, _p, s = key.split(":")
                f, s = int(f), int(s)
                in_season = seasons_arr == s
                staff = set(pid[in_season & at_pos & (fid == f)].tolist())
                if not staff:
                    continue
                entries.append(
                    concentration_share(
                        mat,
                        row,
                        col[in_season],
                        pid[in_season],
                        staff,
                        pw.get(f"fielder_{pos}", pw.get("fielder", 1.0)),
                    )
                )
            report[f"fielder_{pos}"] = _summarize(entries)
    return report


def _load_actor_sim(art_dir: str, views: dict[str, np.ndarray]) -> dict[str, dict]:
    """Load the actor score matrices; {} when the bundle has none."""
    actor_sim: dict[str, dict] = {}
    sim_dir = os.path.join(art_dir, "actor_sim")
    if os.path.exists(os.path.join(sim_dir, "manifest.json")):
        with open(os.path.join(sim_dir, "manifest.json"), encoding="utf-8") as fh:
            sim_manifest = json.load(fh)
        for name in sim_manifest.get("sizes", {}):
            p = os.path.join(sim_dir, f"{name}.npz")
            if not os.path.exists(p):
                continue
            z = np.load(p, allow_pickle=False)
            mv = views.get(f"actor_sim.{name}.matrix")
            actor_sim[name] = {
                "index": json.loads(str(z["index"])),
                "matrix": mv if isinstance(mv, np.ndarray) else z["matrix"],
            }
    return actor_sim


class EngineArtifacts:
    """The per-worker bundle, built entirely from disk so nothing live crosses
    the process-pool fork. It holds both batter hands' pools, the other pools,
    the documents and (when present) the pitcher-by-pitcher similarity lookup;
    the full-pool sampler reads them to assemble its draw weights.

    The big read-only numpy arrays (pitch pool + batted-ball pool +
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
        pitcher_sim_matrix=None,
        steal_pools=None,
        adv_pools=None,
        actor_sim=None,
        park_geometry: dict | None = None,
        change_pool: ChangePool | None = None,
        receiving: dict | None = None,
        change_roles: dict[str, tuple[int, float, float]] | None = None,
        ibb_rates: dict[tuple[int, int, bool, bool], float] | None = None,
        venue_run_factors: dict[tuple[int, int], float] | None = None,
        has_reference: bool = False,
    ):
        self.pools: dict[str, HandPool] = pools
        #: The IBB rate per cell and the venue run factors, read from the
        #: bundle. ``has_reference`` is False on an older bundle; the worker
        #: then reads DuckDB.
        self.ibb_rates = ibb_rates
        self.venue_run_factors = venue_run_factors
        self.has_reference = has_reference
        #: Matrix role -> {"index": {key: row}, "matrix": (n, n)
        #: float32}; {} on a bundle built before the actor matrices (then every
        #: actor factor is neutral).
        self.actor_sim: dict[str, dict] = actor_sim or {}
        #: The park geometry document (``build_park_geometry``),
        #: or None on a bundle without it — the fence stage then stays off.
        self.park_geometry: dict | None = park_geometry
        #: The pitching-change opportunity pool, or None on a bundle without
        #: it (the manager draw then has no pool to draw from).
        self.change_pool: ChangePool | None = change_pool
        #: The receiving-ratio document (``build_receiving_profiles``),
        #: or None on a bundle without it — the factor then stays neutral.
        self.receiving: dict | None = receiving
        #: The reliever ROLE sidecar — ``"pitcher_id:season"`` ->
        #: (entries, high-leverage entry share, mean entry inning); None on a
        #: bundle without it (the role weight then stays neutral).
        self.change_roles: dict[str, tuple[int, float, float]] | None = change_roles
        self.bb_pools: dict[str, BattedBallPool] = bb_pools or {}
        #: Target base ("2"/"3") -> StealPool; {} on a legacy bundle.
        self.steal_pools: dict[str, StealPool] = steal_pools or {}
        #: Decision key ("{scenario}_{from_base}_{target_base}") ->
        #: AdvancementPool; {} on a legacy bundle (the advancement draw then
        #: stages no discretionary advancement).
        self.adv_pools: dict[str, AdvancementPool] = adv_pools or {}
        self.pitcher_sim_index: dict[str, int] = pitcher_sim_index or {}
        self.pitcher_sim: dict[str, dict[str, float]] = pitcher_sim or {}
        #: Dense (n_prof x n_prof) float32 same-hand similarity matrix.
        #: When present, :meth:`FullPoolSampler._f_pitcher` reads a contiguous row
        #: instead of scattering the ~2 GB ``pitcher_sim`` dict, and the dict is
        #: NOT loaded per worker (it rides the shared-memory seam as one
        #: read-only view). None on a legacy bundle that predates the matrix.
        self.pitcher_sim_matrix: np.ndarray | None = pitcher_sim_matrix
        self.seasons: list[int] = seasons or []
        #: actor -> {"key_index": {id:season -> row}, "vecs", "mean", "std", "features"}
        self.actor_emb: dict[str, dict] = actor_emb or {}

    # ------------------------------------------------------------------
    # Shared-memory publish/attach helpers
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

        The object-dtype arrays (``outcome_type`` / ``event``) and the legacy
        ``pitcher_sim`` dict-of-dicts are NOT included — they are not shareable
        through ``shared_memory`` and stay per-worker. The ``pitcher_sim`` dict
        would be about 2 GB resident per worker, so the dense
        ``pitcher_sim_matrix`` (about 11 MB) replaces it: the matrix IS published
        here under ``"pitcher_sim.matrix"`` and attached read-only by every
        worker.
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
        # The steal opportunity pools.
        for target, spool in self.steal_pools.items():
            for attr in _STEAL_POOL_SHAREABLE_ATTRS:
                arr = getattr(spool, attr, None)
                if isinstance(arr, np.ndarray):
                    out[f"steal_pool.{target}.{attr}"] = arr
        # The advancement opportunity pools.
        for key, apool in self.adv_pools.items():
            for attr in _ADV_POOL_SHAREABLE_ATTRS:
                arr = getattr(apool, attr, None)
                if isinstance(arr, np.ndarray):
                    out[f"adv_pool.{key}.{attr}"] = arr
        # The pitching-change opportunity pool.
        if self.change_pool is not None:
            for attr in _CHANGE_POOL_SHAREABLE_ATTRS:
                arr = getattr(self.change_pool, attr, None)
                if isinstance(arr, np.ndarray):
                    out[f"change_pool.{attr}"] = arr
        # The dense pitcher similarity matrix (replaces the ~2 GB dict).
        if isinstance(self.pitcher_sim_matrix, np.ndarray):
            out["pitcher_sim.matrix"] = self.pitcher_sim_matrix
        # The actor score matrices.
        for name, entry in self.actor_sim.items():
            m = entry.get("matrix")
            if isinstance(m, np.ndarray):
                out[f"actor_sim.{name}.matrix"] = m
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
        # Attach the shared steal-pool views.
        for target, spool in self.steal_pools.items():
            for attr in _STEAL_POOL_SHAREABLE_ATTRS:
                v = views.get(f"steal_pool.{target}.{attr}")
                if isinstance(v, np.ndarray):
                    setattr(spool, attr, v)
        # Attach the shared advancement-pool views.
        for key, apool in self.adv_pools.items():
            for attr in _ADV_POOL_SHAREABLE_ATTRS:
                v = views.get(f"adv_pool.{key}.{attr}")
                if isinstance(v, np.ndarray):
                    setattr(apool, attr, v)
        # Attach the shared change-pool views.
        if self.change_pool is not None:
            for attr in _CHANGE_POOL_SHAREABLE_ATTRS:
                v = views.get(f"change_pool.{attr}")
                if isinstance(v, np.ndarray):
                    setattr(self.change_pool, attr, v)
        # Attach the shared dense pitcher-sim matrix view.
        mv = views.get("pitcher_sim.matrix")
        if isinstance(mv, np.ndarray):
            self.pitcher_sim_matrix = mv
        # Attach the shared actor score matrices.
        for name, entry in self.actor_sim.items():
            v = views.get(f"actor_sim.{name}.matrix")
            if isinstance(v, np.ndarray):
                entry["matrix"] = v

    @classmethod
    def load(
        cls, art_dir: str, shared_views: dict[str, np.ndarray] | None = None
    ) -> EngineArtifacts:
        """Load the bundle from disk, one loader per artifact. When
        ``shared_views`` is supplied (the shared-memory map from
        :func:`extract_shared_arrays`), the big per-pool and per-embedding numpy
        arrays come DIRECTLY from the views and their ``.npy`` disk reads are
        SKIPPED: a 150-300 MB cold load becomes a 5 MB metadata read per worker.
        Without it, nine cold workers reading the full bundle from a
        Windows-hosted Docker volume at once stall for about 500 s on a
        10-iteration request. When ``shared_views`` is None (the dev path, or
        no published bundle), every array loads from disk.

        The OBJECT-dtype columns (``outcome_type`` on HandPool, ``event`` on
        BattedBallPool), the metadata-only ids/seasons/recency columns, the
        ``pitcher_sim`` dict-of-dicts, and the per-actor ``key_index`` / feature
        lists are NEVER shareable through ``shared_memory`` and ALWAYS load
        from disk — they're small (typically <5 MB combined per worker).
        """
        views = shared_views or {}
        pool_dir = os.path.join(art_dir, "pitch_pool")
        with open(os.path.join(pool_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        con = duckdb.connect(":memory:")
        try:
            pools = load_pitch_pools(con, art_dir, views)
            bb_pools = load_battedball_pools(con, art_dir, views)
            steal_pools = load_steal_pools(con, art_dir, views)
            adv_pools = load_advancement_pools(con, art_dir, views)
            change_pool, change_roles = load_change_pool(con, art_dir, views)
        finally:
            con.close()
        ps_index, ps_sims, ps_matrix = load_pitcher_sim(art_dir, views)
        actor_sim = _load_actor_sim(art_dir, views)
        receiving = load_receiving(art_dir)
        park_geometry = load_park_geometry(art_dir)
        actor_emb = load_actor_embeddings(art_dir, views)
        # The reference tables (absent on an older bundle).
        ibb_rows = _read_reference_rows(art_dir, IBB_RATES_FILE)
        venue_rows = _read_reference_rows(art_dir, VENUE_RUN_FACTORS_FILE)
        return cls(
            pools,
            ps_index,
            ps_sims,
            manifest.get("seasons"),
            actor_emb,
            bb_pools,
            ps_matrix,
            steal_pools,
            adv_pools,
            actor_sim=actor_sim,
            park_geometry=park_geometry,
            change_pool=change_pool,
            receiving=receiving,
            change_roles=change_roles,
            ibb_rates=ibb_rates_from_rows(ibb_rows) if ibb_rows is not None else None,
            venue_run_factors=(
                venue_run_factors_from_rows(venue_rows) if venue_rows is not None else None
            ),
            has_reference=ibb_rows is not None or venue_rows is not None,
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Build the SIM-422 engine-artifact bundle.")
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB_PATH)
    ap.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Write the bundle IN PLACE here. Omit it to build a new VERSION of the "
            "published bundle (DEFAULT_POOL_DIR/engine_artifacts) and swap it in when "
            "the build is complete (SIM-563)."
        ),
    )
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=None,
        help="Override the recency-floor seasons (default: the last 4 in the pitch pool).",
    )
    ap.add_argument(
        "--what",
        choices=[
            "pool",
            "pitcher_sim",
            "actors",
            "actors_sim",
            "park",
            "manager",
            "receiving",
            "reference",
            "all",
        ],
        default="pool",
    )
    ap.add_argument(
        "--strict-concentration",
        action="store_true",
        help="SIM-523: fail the actor-matrix build when a matrix's p90 own-staff ratio "
        f"exceeds {CONCENTRATION_MAX_RATIO}.",
    )
    ap.add_argument(
        "--pitcher-sim-limit",
        type=int,
        default=None,
        help="Score only N pitcher profiles (verification; omit for full nightly run).",
    )
    ap.add_argument(
        "--matrix",
        action="append",
        default=None,
        help=(
            "SIM-427: with --what actors_sim, build only this matrix (repeatable; e.g. "
            "--matrix manager_usage) and merge its size into the existing manifest."
        ),
    )
    args = ap.parse_args(argv)

    # With no --out-dir, build a staged version and publish it at the end.
    link = os.path.join(DEFAULT_POOL_DIR, "engine_artifacts")
    staged = args.out_dir is None
    if staged:
        args.out_dir = stage_bundle_version(link)
    try:
        _build(args)
    except BaseException:
        if staged:
            discard_bundle_version(args.out_dir)
        raise
    if staged:
        publish_bundle_version(link, args.out_dir)
        prune_bundle_versions(link)
    log.info("engine-artifact build complete -> %s", args.out_dir)
    return 0


def _build(args: argparse.Namespace) -> None:
    con = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        seasons = args.seasons or last_n_seasons(con)
        if args.what in ("pool", "all"):
            build_pitch_pool_artifact(con, args.out_dir, seasons)
            build_battedball_pool_artifact(con, args.out_dir, seasons)
            build_steal_pool_artifact(con, args.out_dir, seasons)
            build_advancement_pool_artifact(con, args.out_dir, seasons)
        if args.what in ("actors", "all"):
            build_actor_embeddings(con, args.out_dir)
        if args.what in ("park", "all"):
            # The park geometry + the carry model.
            build_park_geometry(con, args.out_dir, seasons)
        if args.what in ("manager", "all"):
            # The pitching-change opportunity pool.
            build_pitching_change_pool(con, args.out_dir, seasons)
        if args.what in ("receiving", "all"):
            # The catcher receiving ratios.
            build_receiving_profiles(con, args.out_dir, seasons)
        if args.what in ("reference", "all"):
            # The IBB rates and the venue run factors, for the workers.
            build_reference_tables(con, args.out_dir)
        if args.what in ("pitcher_sim", "all"):
            build_pitcher_sim_matrix(
                args.duckdb_path, args.out_dir, seasons, limit=args.pitcher_sim_limit
            )
        if args.what in ("actors_sim", "all"):
            # The actor score matrices + the concentration report.
            build_actor_sim_matrices(
                args.duckdb_path,
                args.out_dir,
                seasons,
                limit=args.pitcher_sim_limit,
                strict=args.strict_concentration,
                only=set(args.matrix) if args.matrix else None,
            )
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
