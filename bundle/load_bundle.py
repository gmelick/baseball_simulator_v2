"""Load the engine-artifact bundle in a worker: the pools, the score tables and the reference data."""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from bundle.catcher_receiving import load_receiving
from bundle.export_actor_embeddings import load_actor_embeddings
from bundle.export_advancement_pool import (
    load_advancement_pools,
)
from bundle.export_batted_ball_pool import (
    load_battedball_pools,
)
from bundle.export_pitch_pool import load_pitch_pools
from bundle.export_pitcher_scores import load_pitcher_sim
from bundle.export_pitching_change_pool import load_change_pool
from bundle.export_steal_pool import load_steal_pools
from bundle.park_geometry import load_park_geometry
from bundle.reference_tables import (
    IBB_RATES_FILE,
    VENUE_RUN_FACTORS_FILE,
    _read_reference_rows,
    ibb_rates_from_rows,
    venue_run_factors_from_rows,
)
from bundle.worker_pools import (
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

log = logging.getLogger("engine_artifacts")


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
