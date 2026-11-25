"""Load the engine-artifact bundle from disk: the pools, the score tables and the reference data."""

from __future__ import annotations

import json
import os

import duckdb
import numpy as np

from bundle.common import read_json
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


def _load_actor_sim(art_dir: str, views: dict[str, np.ndarray]) -> dict[str, dict]:
    """Load the actor score matrices, each from its shared view when one exists.

    Returns {role: {"index": {key: row}, "matrix": array}}, or {} when the bundle has none.
    """
    actor_sim: dict[str, dict] = {}
    sim_dir = os.path.join(art_dir, "actor_sim")
    sim_manifest = read_json(sim_dir, "manifest.json") or {}
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
    """The loaded bundle a worker draws from: the pools, the score tables and the documents.

    :meth:`load` builds it from disk, so nothing live crosses the process-pool fork. The
    full-pool sampler reads it to build its draw weights.

    The parent can publish the big read-only numpy arrays into shared memory
    (:meth:`extract_shared_arrays`). Each worker then attaches them without a copy
    (:meth:`load`'s ``shared_views``), so ten workers hold one copy of about 300 MB instead
    of ten. :mod:`simulation.batch_runner` owns the shared-memory plumbing; this class
    defines only the map from flat name to array."""

    def __init__(
        self,
        pools,
        pitcher_sim_index=None,
        pitcher_sim=None,
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
        #: The intentional-walk (IBB) rate per cell and the venue run factors, read from
        #: the bundle. ``has_reference`` is False on a bundle without them; the worker then
        #: reads DuckDB.
        self.ibb_rates = ibb_rates
        self.venue_run_factors = venue_run_factors
        self.has_reference = has_reference
        #: Matrix role -> {"index": {key: row}, "matrix": (n, n) float32}; {} on a bundle
        #: without the matrices, and every actor factor is then neutral.
        self.actor_sim: dict[str, dict] = actor_sim or {}
        #: The park geometry document (``build_park_geometry``); None on a bundle without
        #: it, and the fence stage then stays off.
        self.park_geometry: dict | None = park_geometry
        #: The pitching-change opportunity pool; None on a bundle without it, and the
        #: manager draw then has no pool to draw from.
        self.change_pool: ChangePool | None = change_pool
        #: The receiving-ratio document (``build_receiving_profiles``); None on a bundle
        #: without it, and the factor then stays neutral.
        self.receiving: dict | None = receiving
        #: The reliever role table: ``"pitcher_id:season"`` -> (entries, high-leverage entry
        #: share, mean entry inning); None on a bundle without it, and the role weight then
        #: stays neutral.
        self.change_roles: dict[str, tuple[int, float, float]] | None = change_roles
        self.bb_pools: dict[str, BattedBallPool] = bb_pools or {}
        #: Target base ("2"/"3") -> StealPool; {} on a bundle without the steal pools.
        self.steal_pools: dict[str, StealPool] = steal_pools or {}
        #: Decision key ("{scenario}_{from_base}_{target_base}") -> AdvancementPool; {} on a
        #: bundle without them, and the advancement draw then stages no discretionary advance.
        self.adv_pools: dict[str, AdvancementPool] = adv_pools or {}
        self.pitcher_sim_index: dict[str, int] = pitcher_sim_index or {}
        #: The pitcher similarity as a dict of dicts. :meth:`load` never reads it, because it
        #: is about 2 GB per worker; only a caller that builds the bundle in memory passes it.
        self.pitcher_sim: dict[str, dict[str, float]] = pitcher_sim or {}
        #: The dense (n_prof x n_prof) float32 same-hand pitcher similarity matrix. The
        #: sampler reads one contiguous row of it in place of the dict, and the workers share
        #: it as one read-only view. On a bundle without it, ``load`` leaves the dict empty
        #: too, so the pitcher factor is flat.
        self.pitcher_sim_matrix: np.ndarray | None = pitcher_sim_matrix
        #: actor -> {"key_index": {id:season -> row}, "vecs", "mean", "std", "features"}
        self.actor_emb: dict[str, dict] = actor_emb or {}

    def extract_shared_arrays(self) -> dict[str, np.ndarray]:
        """Return the bundle's shareable numpy arrays, keyed by flat name.

        The parent calls it once after :meth:`load` and passes the result to
        ``BatchRunner(shared_arrays=...)``. The runner then copies each array into shared
        memory once for the warm pool. Each value is the loaded instance's own buffer, not a
        copy.

        A key reads ``"<prefix>.<sub>.<attr>"``, for example ``"pool.R.geom"`` or
        ``"actor_emb.batter.vecs"``; a pool with no sub-key (the change pool, the pitcher
        similarity matrix) reads ``"<prefix>.<attr>"``. The ``_*_SHAREABLE_ATTRS`` tuples of
        :mod:`bundle.worker_pools` name the attributes.

        An object-dtype array and the ``pitcher_sim`` dict cannot go through shared memory,
        so they stay out. The dense ``pitcher_sim_matrix`` stands in for the dict and goes in
        under ``"pitcher_sim.matrix"``.
        """
        # (key prefix, {sub-key: object}, attributes); an empty sub-key adds no part.
        groups: tuple[tuple[str, dict, tuple[str, ...]], ...] = (
            ("pool", self.pools, _HAND_POOL_SHAREABLE_ATTRS),
            ("bb_pool", self.bb_pools, _BB_POOL_SHAREABLE_ATTRS),
            ("actor_emb", self.actor_emb, _ACTOR_EMB_SHAREABLE_ATTRS),
            ("steal_pool", self.steal_pools, _STEAL_POOL_SHAREABLE_ATTRS),
            ("adv_pool", self.adv_pools, _ADV_POOL_SHAREABLE_ATTRS),
            (
                "change_pool",
                {"": self.change_pool} if self.change_pool is not None else {},
                _CHANGE_POOL_SHAREABLE_ATTRS,
            ),
            ("pitcher_sim", {"": {"matrix": self.pitcher_sim_matrix}}, ("matrix",)),
            ("actor_sim", self.actor_sim, ("matrix",)),
        )
        out: dict[str, np.ndarray] = {}
        for prefix, subs, attrs in groups:
            for sub, obj in subs.items():
                head = f"{prefix}.{sub}" if sub else prefix
                for attr in attrs:
                    arr = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
                    if isinstance(arr, np.ndarray):
                        out[f"{head}.{attr}"] = arr
        return out

    @classmethod
    def load(
        cls, art_dir: str, shared_views: dict[str, np.ndarray] | None = None
    ) -> EngineArtifacts:
        """Load the bundle from ``art_dir``, one loader per artifact, and return it.

        ``shared_views`` is the shared-memory map built from :meth:`extract_shared_arrays`.
        With it, the big pool and embedding arrays come straight from the views, and the
        worker skips their disk reads: a few MB of metadata in place of a cold load of
        hundreds of MB. Without the views, nine workers reading the whole bundle at once from
        a Windows-hosted Docker volume stall for minutes. When ``shared_views`` is None (the
        parent, or a development run), every array loads from disk.

        The object-dtype columns (``HandPool.outcome_type``, ``BattedBallPool.event``) and
        each actor's ``key_index`` and feature list cannot go through shared memory. They
        always load from disk; they are small.
        """
        views = shared_views or {}
        con = duckdb.connect(":memory:")
        try:
            pools = load_pitch_pools(con, art_dir, views)
            bb_pools = load_battedball_pools(con, art_dir, views)
            steal_pools = load_steal_pools(con, art_dir, views)
            adv_pools = load_advancement_pools(con, art_dir, views)
            change_pool, change_roles = load_change_pool(con, art_dir, views)
        finally:
            con.close()
        ps_index, ps_matrix = load_pitcher_sim(art_dir, views)
        actor_sim = _load_actor_sim(art_dir, views)
        actor_emb = load_actor_embeddings(art_dir, views)
        # A bundle may lack the reference tables; the worker then reads DuckDB.
        ibb_rows = _read_reference_rows(art_dir, IBB_RATES_FILE)
        venue_rows = _read_reference_rows(art_dir, VENUE_RUN_FACTORS_FILE)
        return cls(
            pools,
            pitcher_sim_index=ps_index,
            actor_emb=actor_emb,
            bb_pools=bb_pools,
            pitcher_sim_matrix=ps_matrix,
            steal_pools=steal_pools,
            adv_pools=adv_pools,
            actor_sim=actor_sim,
            park_geometry=read_json(art_dir, "park_geometry.json"),
            change_pool=change_pool,
            receiving=read_json(art_dir, "receiving.json"),
            change_roles=change_roles,
            ibb_rates=ibb_rates_from_rows(ibb_rows) if ibb_rows is not None else None,
            venue_run_factors=(
                venue_run_factors_from_rows(venue_rows) if venue_rows is not None else None
            ),
            has_reference=ibb_rows is not None or venue_rows is not None,
        )
