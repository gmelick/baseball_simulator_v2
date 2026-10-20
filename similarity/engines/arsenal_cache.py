"""The cache of pairwise arsenal distances for the pitcher engine.

The arsenal distance (the Wasserstein-2 distance, W₂, between two pitchers'
pitch mixtures) costs about 0.5 ms a pair, and a query scores every
same-hand pitcher. The cache keeps each distance once: lazily, as queries
need it, or all at once through ``precompute``, serially or on a process pool.
It stores the raw distance, not the score, so a new ``ARSENAL_SCALE`` needs no
recompute.

The arsenal model (``ArsenalSimilarity``, ``GMMModel``) lives in
``pitcher_similarity``, which imports this module, so the functions here
import it at call time.
"""

from __future__ import annotations

import logging
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from similarity.engines.pitcher_similarity import GMMModel, PitcherProfile

# The engine's logger: the cache's lines read as the engine's.
log = logging.getLogger("pitcher_similarity")


def _arsenal_distance(gmm_a: GMMModel, gmm_b: GMMModel) -> float:
    """``ArsenalSimilarity.distance``, looked up at call time (see the module docstring)."""
    from similarity.engines.pitcher_similarity import ArsenalSimilarity

    return ArsenalSimilarity.distance(gmm_a, gmm_b)


def _cache_key(
    a: tuple[int, int],
    b: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """The symmetric key of a pair: the smaller (pitcher_id, season) first."""
    return (a, b) if a <= b else (b, a)


class ArsenalCache:
    """The pairwise W₂ distances of one engine, filled lazily or in one batch.

    It stores the raw W₂ distance, not the [0, 1] score: the score is a
    monotonic transform applied at query time.

    - Lazy: ``get_or_compute`` computes a missing pair and keeps it.
    - Batch: ``precompute`` fills every pair of a population once.
    - Persist: ``save`` and ``load`` keep the distances across runs.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}

        # The dense matrix serves the all-vs-one lookup as one row slice. The
        # dict above stays the store of record (lazy fills, save/load,
        # finite_distances); the matrix is a view of it for one population
        # (one handedness partition), rebuilt on demand. It holds the raw W₂:
        #   * 0.0 on the diagonal (self-distance),
        #   * NaN for a pair with no distance (a GMM was None, stored as inf),
        #   * the finite W₂ distance otherwise.
        # ``_matrix_index`` maps (pitcher_id, season) -> row/col index.
        self._matrix: NDArray[np.float64] | None = None
        self._matrix_index: dict[tuple[int, int], int] = {}

    def get(
        self,
        a: tuple[int, int],
        b: tuple[int, int],
    ) -> float | None:
        """The cached distance of a pair, or None on a miss."""
        return self._cache.get(_cache_key(a, b))

    def put(
        self,
        a: tuple[int, int],
        b: tuple[int, int],
        distance: float,
    ) -> None:
        """Store the distance of a pair."""
        self._cache[_cache_key(a, b)] = distance
        # A write may change a pair the dense matrix holds: drop the matrix;
        # the next vectorized lookup rebuilds it.
        self._matrix = None
        self._matrix_index = {}

    # ------------------------------------------------------------------
    # The vectorized lookup through the dense matrix
    # ------------------------------------------------------------------

    def build_matrix(
        self,
        profiles: list[PitcherProfile],
    ) -> None:
        """Build the dense symmetric W₂ matrix of ``profiles``.

        The method fills every missing pair through ``get_or_compute``, then
        writes the symmetric ``(n, n)`` matrix and the key-to-row map. A later
        all-vs-one lookup is one NumPy row slice, not a loop over the dict.

        The matrix holds the raw W₂ distance:
          * 0.0 on the diagonal (the self-distance),
          * NaN for a pair with no distance (a GMM was None, stored as inf),
          * the finite W₂ distance otherwise.

        Each finite value equals what ``get_or_compute`` returns for the pair.
        """
        keys = [(p.pitcher_id, p.season) for p in profiles]
        n = len(keys)

        mat = np.zeros((n, n), dtype=np.float64)
        for i in range(n):
            pi = profiles[i]
            for j in range(i + 1, n):
                pj = profiles[j]
                # A miss here calls put(), which clears self._matrix and
                # self._matrix_index, so both are published after the loop.
                dist = self.get_or_compute(keys[i], keys[j], pi.gmm, pj.gmm)
                v = dist if np.isfinite(dist) else np.nan
                mat[i, j] = v
                mat[j, i] = v
        # The diagonal stays 0.0 (self-distance) from np.zeros.
        self._matrix = mat
        self._matrix_index = {k: i for i, k in enumerate(keys)}

    def has_matrix(self, profiles: list[PitcherProfile]) -> bool:
        """True when the current dense matrix covers exactly ``profiles``."""
        if self._matrix is None:
            return False
        keys = [(p.pitcher_id, p.season) for p in profiles]
        if len(keys) != len(self._matrix_index):
            return False
        return all(k in self._matrix_index for k in keys)

    def row_distances(
        self,
        query_profile: PitcherProfile,
        profiles: list[PitcherProfile],
    ) -> NDArray[np.float64]:
        """The raw W₂ distances from ``query_profile`` to each of ``profiles``,
        as one 1-D array in the order of ``profiles``.

        When a dense matrix covers ``profiles`` and the query, the row is one
        matrix slice. Otherwise the method builds the row from the dict, and
        computes and keeps each missing pair.

        The values match ``get_or_compute``:
          * 0.0 for the query's own (pitcher_id, season),
          * inf for a pair with no distance (a GMM was None),
          * the finite W₂ distance otherwise.

        The matrix stores NaN for a pair with no distance; this method maps
        NaN back to inf, so a caller's ``np.isfinite`` mask drops the pair.
        """
        query_key = (query_profile.pitcher_id, query_profile.season)

        # A dense matrix covers this population and the query: one row slice.
        if self.has_matrix(profiles):
            qi = self._matrix_index.get(query_key)
            if qi is not None:
                row = self._matrix[qi].copy()
                return np.where(np.isnan(row), np.inf, row)

        # Otherwise build this one row against the dict, computing and keeping
        # each missing pair.
        keys = [(p.pitcher_id, p.season) for p in profiles]
        row = np.empty(len(profiles), dtype=np.float64)
        for i, (k, cand) in enumerate(zip(keys, profiles, strict=False)):
            if k == query_key:
                row[i] = 0.0
                continue
            dist = self.get_or_compute(query_key, k, query_profile.gmm, cand.gmm)
            row[i] = dist if np.isfinite(dist) else np.nan

        # NaN back to inf, as get_or_compute returns for a missing pair.
        return np.where(np.isnan(row), np.inf, row)

    def get_or_compute(
        self,
        key_a: tuple[int, int],
        key_b: tuple[int, int],
        gmm_a: GMMModel | None,
        gmm_b: GMMModel | None,
    ) -> float:
        """The cached W₂ distance of a pair; on a miss, compute it and keep it.

        The distance is 0.0 when the two keys are equal, and inf when either
        GMM is None.
        """
        if key_a == key_b:
            return 0.0

        cached = self.get(key_a, key_b)
        if cached is not None:
            return cached

        if gmm_a is None or gmm_b is None:
            dist = float("inf")
        else:
            dist = _arsenal_distance(gmm_a, gmm_b)

        self.put(key_a, key_b, dist)
        return dist

    def precompute(
        self,
        profiles: list[PitcherProfile],
        n_workers: int = 1,
    ) -> None:
        """Fill the cache for every pair of ``profiles``, then build their
        dense matrix.

        Parameters
        ----------
        profiles : list[PitcherProfile]
            Every profile of one hand partition.
        n_workers : int
            The number of worker processes. 1 runs in this process, with no
            start-up cost; more than 1 uses a process pool, which is faster
            for a large partition.
        """
        pairs_to_compute = []
        for i in range(len(profiles)):
            for j in range(i + 1, len(profiles)):
                pa, pb = profiles[i], profiles[j]
                key_a = (pa.pitcher_id, pa.season)
                key_b = (pb.pitcher_id, pb.season)
                if self.get(key_a, key_b) is None:
                    pairs_to_compute.append((i, j))

        if not pairs_to_compute:
            log.info("  Arsenal cache already complete for %d profiles.", len(profiles))
            # Build the dense matrix anyway, so queries take the row slice.
            self.build_matrix(profiles)
            return

        # A batch fill changes the store: drop the stale dense matrix.
        self._matrix = None
        self._matrix_index = {}

        log.info(
            "  Precomputing %d arsenal W2 distances (%d workers) …",
            len(pairs_to_compute),
            n_workers,
        )
        t0 = time.time()

        if n_workers <= 1:
            for idx, (i, j) in enumerate(pairs_to_compute):
                pa, pb = profiles[i], profiles[j]
                key_a = (pa.pitcher_id, pa.season)
                key_b = (pb.pitcher_id, pb.season)
                if pa.gmm is not None and pb.gmm is not None:
                    dist = _arsenal_distance(pa.gmm, pb.gmm)
                else:
                    dist = float("inf")
                self.put(key_a, key_b, dist)

                if (idx + 1) % 10000 == 0:
                    elapsed = time.time() - t0
                    rate = (idx + 1) / elapsed
                    remaining = (len(pairs_to_compute) - idx - 1) / rate
                    log.info(
                        "    %d/%d pairs (%.0f/sec, ~%.0fs remaining)",
                        idx + 1,
                        len(pairs_to_compute),
                        rate,
                        remaining,
                    )
        else:
            # On a process pool: each worker gets plain dicts, not GMMModel objects.
            gmm_data = {}
            for p in profiles:
                k = (p.pitcher_id, p.season)
                if p.gmm is not None:
                    gmm_data[k] = _serialize_gmm(p.gmm)

            chunk_size = max(1, len(pairs_to_compute) // (n_workers * 4))
            chunks = [
                pairs_to_compute[i : i + chunk_size]
                for i in range(0, len(pairs_to_compute), chunk_size)
            ]

            keys = [(p.pitcher_id, p.season) for p in profiles]

            with ProcessPoolExecutor(max_workers=n_workers) as executor:
                futures = {}
                for chunk in chunks:
                    future = executor.submit(
                        _compute_w2_chunk,
                        chunk,
                        keys,
                        gmm_data,
                    )
                    futures[future] = chunk

                done = 0
                for future in as_completed(futures):
                    results = future.result()
                    for (key_a, key_b), dist in results:
                        self.put(key_a, key_b, dist)
                    done += len(futures[future])

        elapsed = time.time() - t0
        log.info(
            "  Arsenal cache precomputed: %d pairs in %.1fs (%.0f pairs/sec).",
            len(pairs_to_compute),
            elapsed,
            len(pairs_to_compute) / max(elapsed, 0.001),
        )

        # The cache is complete: build the dense matrix for the row slices.
        self.build_matrix(profiles)

    @property
    def size(self) -> int:
        """The number of cached distances."""
        return len(self._cache)

    def finite_distances(self) -> NDArray[np.float64]:
        """Every cached W₂ distance as a 1-D array, without the inf values (the
        pairs with a missing GMM).

        This is the array to pass to calibrate_arsenal_gamma(). The engine
        turns the fitted gamma of the squared form into its linear
        ARSENAL_SCALE with arsenal_scale_from_gamma():

            from similarity.calibration.fitting import calibrate_arsenal_gamma
            from similarity.engines.pitcher_similarity import arsenal_scale_from_gamma

            import numpy as np

            engine.precompute_arsenal_cache()
            dists = engine._arsenal_cache.finite_distances()
            gamma = calibrate_arsenal_gamma(dists, target_median_score=0.50)
            optimal_scale = arsenal_scale_from_gamma(gamma, float(np.median(dists)))
        """
        all_dists = np.array(list(self._cache.values()), dtype=np.float64)
        return all_dists[np.isfinite(all_dists)]

    def save(self, path: str) -> None:
        """Save the cache to a pickle file."""
        with Path(path).open("wb") as f:
            pickle.dump(self._cache, f, protocol=pickle.HIGHEST_PROTOCOL)
        log.info("Arsenal cache saved: %d entries → %s", len(self._cache), path)

    def load(self, path: str) -> None:
        """Load a pickle file that ``save`` wrote, and merge it into the cache.

        Unpickling can run code from the file: load only a file this engine
        wrote. A missing file logs a warning and loads nothing.
        """
        p = Path(path)
        if not p.exists():
            log.warning("Cache file not found: %s", path)
            return
        with p.open("rb") as f:
            loaded = pickle.load(f)
        self._cache.update(loaded)
        # The loaded entries change the store: drop the dense matrix.
        self._matrix = None
        self._matrix_index = {}
        log.info("Arsenal cache loaded: %d entries from %s", len(loaded), path)


# --- The process-pool helpers (module-level, so a worker can import them) ---


def _serialize_gmm(gmm: GMMModel) -> dict:
    """The GMM's components as a plain dict, to send to a worker process."""
    return {
        "components": [
            {
                "weight": c.weight,
                "mean": c.mean.tolist(),
                "covariance": c.covariance.tolist(),
            }
            for c in gmm.components
        ],
    }


def _deserialize_gmm(data: dict) -> GMMModel:
    """Rebuild a minimal GMMModel (the weights, means and covariances only)
    from ``_serialize_gmm``'s dict."""
    from similarity.engines.pitcher_similarity import (
        GMM_FEATURE_DIM,
        GMM_FEATURE_NAMES,
        GMMComponent,
        GMMModel,
    )

    components = [
        GMMComponent(
            component_id=i,
            weight=c["weight"],
            mean=np.array(c["mean"], dtype=np.float64),
            covariance=np.array(c["covariance"], dtype=np.float64),
            n_pitches=0,
        )
        for i, c in enumerate(data["components"])
    ]
    return GMMModel(
        n_components=len(components),
        feature_names=GMM_FEATURE_NAMES,
        feature_means=np.zeros(GMM_FEATURE_DIM),
        feature_stds=np.ones(GMM_FEATURE_DIM),
        components=components,
    )


def _compute_w2_chunk(
    pairs: list[tuple[int, int]],
    keys: list[tuple[int, int]],
    gmm_data: dict[tuple[int, int], dict],
) -> list[tuple[tuple[tuple[int, int], tuple[int, int]], float]]:
    """The worker: the W₂ distance of each (i, j) pair of one chunk. A pair
    without both GMMs gets inf."""
    results = []
    for i, j in pairs:
        key_a, key_b = keys[i], keys[j]
        data_a = gmm_data.get(key_a)
        data_b = gmm_data.get(key_b)
        if data_a is not None and data_b is not None:
            gmm_a = _deserialize_gmm(data_a)
            gmm_b = _deserialize_gmm(data_b)
            dist = _arsenal_distance(gmm_a, gmm_b)
        else:
            dist = float("inf")
        results.append(((key_a, key_b), dist))
    return results
