"""The pitch-to-pitch similarity engine: the nearest real pitches to a pitch.

The engine indexes every pitch of ``sim.pitch_pool`` in a FAISS index (a
library for fast nearest-neighbour search over dense vectors). A query takes
one pitch vector and returns its K nearest pitches, each with its
``pitch_id`` and the class of its result. The engine compares pitches, not
players.

The API builds the engine at boot (``api/state.build_all_engines``). No
simulation path calls it: the simulator draws from the full pool through
``simulation/full_pool_sampler.py``.

Why FAISS, not a KD-tree. The pitch pool holds every clean pitch of every
loaded season: hundreds of thousands a season, millions in all. A KD-tree (a
space-splitting search tree) slows down on dense vectors at this size. FAISS
searches a whole batch of queries in one call. The default index is
``IndexFlatL2`` (exact search); ``index_kind="hnsw"`` builds an HNSW index
instead (a layered graph of near neighbours, an approximate search).

The feature vector (10 dimensions)::

    [ velo, ivb, hb, spin_rate, spin_axis,
      release_x, release_z, release_ext,
      plate_x, plate_z ]

The vector holds pitch physics only. The count, the outs, the runners and the
zone are categories, and one L2 distance over categories and measurements
together distorts the metric; the engine reads none of them. The engine
z-scores each feature (the gap from the pool mean, in standard deviations) on
the loaded pool, then multiplies it by the square root of its weight, so the
L2 distance weights each feature by its importance. The situation engine uses
the same pattern.

The recency boost. With ``recency_boost=True`` and a season list, the build
adds a second copy of each pitch of the last RECENCY_BOOST_SEASONS seasons.
The copies tilt the K nearest neighbours toward recent pitches, and the
distance metric stays the same. The boost is off by default, and the
API's boot build leaves it off.

Usage::

    engine = PitchPitchSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
    engine.build(seasons=[2022, 2023, 2024])

    query = PitchVector(velo=94.1, ivb=18.0, hb=-3.4, spin_rate=2380,
                        spin_axis=210.0, release_x=-1.5, release_z=5.9,
                        release_ext=6.5, plate_x=0.4, plate_z=2.7)
    results = engine.query(query, k=50)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray

# requirements.txt installs faiss-cpu, but the import stays optional: the module
# imports without FAISS, and the engine's constructor raises with the original
# import error.
try:  # pragma: no cover — import-time concern
    import faiss

    _FAISS_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    _FAISS_AVAILABLE = False
    _FAISS_IMPORT_ERROR = e

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pitch_pitch_similarity")


# ============================================================================
# Config: the pitch features and their weights
# ============================================================================

# The features and their importance weights. The build multiplies each z-scored
# feature by the square root of its weight, so the L2 distance follows the
# weights. The weights are hand-set starting points, not fitted. The first
# eight features are the pitcher engine's arsenal dimensions.
PITCH_FEATURES = [
    # (feature_name, weight)
    ("velo", 1.20),  # release speed (mph)
    ("ivb", 1.10),  # induced vertical break (gravity removed)
    ("hb", 1.00),  # horizontal break
    ("spin_rate", 0.70),  # rpm
    ("spin_axis", 0.50),  # degrees, 0-360, read as a plain number: 359 and 1 are 358 apart
    ("release_x", 0.60),  # release_pos_x (ft)
    ("release_z", 0.60),  # release_pos_z (ft)
    ("release_ext", 0.40),  # release extension (ft)
    ("plate_x", 0.90),  # the location at the plate
    ("plate_z", 0.90),
]

FEATURE_NAMES = [f for f, _ in PITCH_FEATURES]
FEATURE_WEIGHTS = np.array([w for _, w in PITCH_FEATURES], dtype=np.float64)
FEATURE_SCALE = np.sqrt(FEATURE_WEIGHTS)
FEATURE_DIM = len(PITCH_FEATURES)

# The default K of a nearest-neighbour query.
DEFAULT_K = 50

# Below this many pitches, the build logs a warning that the index may not be
# reliable.
MIN_INDEX_SIZE = 1_000

# The number of recent seasons that the recency boost copies once.
RECENCY_BOOST_SEASONS = 2


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class PitchVector:
    """One pitch, as a query vector for the index."""

    velo: float
    ivb: float
    hb: float
    spin_rate: float
    spin_axis: float
    release_x: float
    release_z: float
    release_ext: float
    plate_x: float
    plate_z: float

    def to_array(self) -> NDArray[np.float64]:
        """The ten features as a float array, in PITCH_FEATURES order."""
        return np.array(
            [
                float(self.velo),
                float(self.ivb),
                float(self.hb),
                float(self.spin_rate),
                float(self.spin_axis),
                float(self.release_x),
                float(self.release_z),
                float(self.release_ext),
                float(self.plate_x),
                float(self.plate_z),
            ],
            dtype=np.float64,
        )


@dataclass(frozen=True, slots=True)
class NearestPitch:
    """One pitch in a query's K nearest neighbours."""

    pitch_id: int  # the row's key in sim.pitch_pool
    game_pk: int
    season: int
    pitcher_id: int
    batter_id: int
    distance: float  # the L2 distance in the z-scored, weighted space
    # The result class: ball, called_strike, swinging_strike, foul, in_play or
    # hit_by_pitch.
    outcome_type: str


# ============================================================================
# The feature normalizer
# ============================================================================


@dataclass(slots=True)
class PitchNormalizer:
    """The z-score normalizer of the pitch vectors.

    The normalizer z-scores each feature, multiplies it by FEATURE_SCALE and
    casts the result to float32 for FAISS. A NaN feature becomes 0 (the pool
    mean).
    """

    mean: NDArray | None = None
    std: NDArray | None = None

    def fit(self, matrix: NDArray[np.float64]) -> None:
        """Fit each feature's mean and standard deviation on the pool."""
        # NaN-aware, so a missing feature (spin_axis on a bunted foul, for
        # example) does not spoil the mean or the standard deviation.
        self.mean = np.nanmean(matrix, axis=0)
        self.std = np.nanstd(matrix, axis=0)
        # A constant column gets a standard deviation of 1.
        self.std[self.std == 0] = 1.0

    def normalize(self, vec: NDArray[np.float64]) -> NDArray[np.float64]:
        """Scale one vector. Before ``fit``, the method only applies the weights."""
        if self.mean is None:
            return (vec * FEATURE_SCALE).astype(np.float32)
        normed = (vec - self.mean) / self.std
        return (np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE).astype(np.float32)

    def normalize_batch(self, matrix: NDArray[np.float64]) -> NDArray[np.float32]:
        """Scale each row of a matrix, as ``normalize`` scales one vector."""
        if self.mean is None:
            return (matrix * FEATURE_SCALE[np.newaxis, :]).astype(np.float32)
        normed = (matrix - self.mean[np.newaxis, :]) / self.std[np.newaxis, :]
        return (np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE[np.newaxis, :]).astype(np.float32)


# ============================================================================
# The engine
# ============================================================================


class PitchPitchSimilarityEngine:
    """The pitch-to-pitch engine over a FAISS index.

    The build loads ``sim.pitch_pool`` into memory, fits the normalizer on the
    pool, scales the features and indexes them (``IndexFlatL2``, or
    ``IndexHNSWFlat`` when ``index_kind="hnsw"``). A query returns the K
    nearest pitches with their metadata.

    Usage::

        engine = PitchPitchSimilarityEngine(duckdb_path="...")
        engine.build(seasons=[2022, 2023, 2024])
        results = engine.query(PitchVector(...), k=50)
    """

    INDEX_KIND_FLAT = "flat"
    INDEX_KIND_HNSW = "hnsw"

    def __init__(
        self,
        duckdb_path: str,
        index_kind: str = INDEX_KIND_FLAT,
    ) -> None:
        if not _FAISS_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                f"faiss-cpu is not installed. Original import error: {_FAISS_IMPORT_ERROR!r}"
            )
        if index_kind not in (self.INDEX_KIND_FLAT, self.INDEX_KIND_HNSW):
            raise ValueError(f"unknown index_kind: {index_kind!r}")

        self._duckdb_path = duckdb_path
        self._index_kind = index_kind
        self._normalizer = PitchNormalizer()
        self._index: faiss.Index | None = None
        self._index_meta: list[NearestPitch] = []  # one per FAISS row, in row order
        self._index_size = 0

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        seasons: list[int] | None = None,
        *,
        recency_boost: bool = False,
    ) -> None:
        """Load the pitch pool, fit the normalizer and build the FAISS index.

        ``recency_boost`` copies the pitches of the last RECENCY_BOOST_SEASONS
        seasons once; it needs ``seasons``. The normalizer fits after the copies, so
        the copied pitches count twice in the mean and the standard deviation.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            raw_matrix, meta = self._load_pool(conn, seasons)
        finally:
            conn.close()

        if len(raw_matrix) < MIN_INDEX_SIZE:
            log.warning(
                "PitchPitchSimilarityEngine: only %d pitches loaded "
                "(minimum %d). Build may not be reliable.",
                len(raw_matrix),
                MIN_INDEX_SIZE,
            )

        if recency_boost and seasons:
            raw_matrix, meta = self._apply_recency_boost(raw_matrix, meta, seasons)

        self._normalizer.fit(raw_matrix)
        scaled = self._normalizer.normalize_batch(raw_matrix)

        self._index = self._build_faiss_index(scaled)
        self._index_meta = meta
        self._index_size = len(meta)

        elapsed = time.time() - t0
        log.info(
            "PitchPitchSimilarityEngine built: %d pitches indexed (%s) in %.2fs.",
            self._index_size,
            self._index_kind,
            elapsed,
        )

    def _load_pool(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> tuple[NDArray[np.float64], list[NearestPitch]]:
        """Read the pool's feature matrix and one NearestPitch per row
        (distance 0.0). A NULL feature reads as NaN."""
        sf = ""
        if seasons:
            sf = f"WHERE season IN ({', '.join(str(s) for s in seasons)})"

        rows = conn.execute(f"""
            SELECT
                pitch_id, game_pk, season,
                pitcher_id, batter_id,
                velo, ivb, hb, spin_rate, spin_axis,
                release_x, release_z, release_ext,
                plate_x, plate_z,
                outcome_type
              FROM sim.pitch_pool
            {sf}
        """).fetchall()

        n = len(rows)
        matrix = np.empty((n, FEATURE_DIM), dtype=np.float64)
        meta: list[NearestPitch] = []
        for i, r in enumerate(rows):
            (
                pitch_id,
                game_pk,
                season,
                pitcher_id,
                batter_id,
                velo,
                ivb,
                hb,
                spin_rate,
                spin_axis,
                release_x,
                release_z,
                release_ext,
                plate_x,
                plate_z,
                outcome_type,
            ) = r
            matrix[i, :] = [
                _f(velo),
                _f(ivb),
                _f(hb),
                _f(spin_rate),
                _f(spin_axis),
                _f(release_x),
                _f(release_z),
                _f(release_ext),
                _f(plate_x),
                _f(plate_z),
            ]
            meta.append(
                NearestPitch(
                    pitch_id=pitch_id,
                    game_pk=game_pk,
                    season=season,
                    pitcher_id=pitcher_id,
                    batter_id=batter_id,
                    distance=0.0,
                    outcome_type=outcome_type,
                )
            )
        return matrix, meta

    def _apply_recency_boost(
        self,
        raw_matrix: NDArray[np.float64],
        meta: list[NearestPitch],
        seasons: list[int],
    ) -> tuple[NDArray[np.float64], list[NearestPitch]]:
        """Copy each pitch of the last RECENCY_BOOST_SEASONS seasons once, so
        it appears twice in the index. The copies tilt the nearest neighbours
        toward recent pitches, and the distance metric stays the same."""
        recent_cutoff = max(seasons) - (RECENCY_BOOST_SEASONS - 1)
        recent_mask = np.array(
            [m.season >= recent_cutoff for m in meta],
            dtype=bool,
        )
        if not recent_mask.any():
            return raw_matrix, meta
        boosted_matrix = np.vstack([raw_matrix, raw_matrix[recent_mask]])
        boosted_meta = list(meta) + [meta[i] for i in np.flatnonzero(recent_mask)]
        return boosted_matrix, boosted_meta

    def _build_faiss_index(self, scaled: NDArray[np.float32]) -> faiss.Index:
        """Build the FAISS index of the kind the engine holds, and add the rows."""
        if self._index_kind == self.INDEX_KIND_HNSW:
            # 32 neighbours per node, FAISS's default for HNSW.
            index = faiss.IndexHNSWFlat(FEATURE_DIM, 32)
        else:
            index = faiss.IndexFlatL2(FEATURE_DIM)
        if scaled.size:
            index.add(scaled)
        return index

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        vector: PitchVector | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> list[NearestPitch]:
        """The K nearest pitches to ``vector``, closest first. An empty or
        unbuilt index returns an empty list."""
        if self._index is None or self._index_size == 0:
            return []

        if isinstance(vector, PitchVector):
            raw = vector.to_array()
        else:
            raw = np.asarray(vector, dtype=np.float64)

        scaled = self._normalizer.normalize(raw).reshape(1, -1)
        k_eff = max(1, min(k, self._index_size))
        distances, indices = self._index.search(scaled, k_eff)

        results: list[NearestPitch] = []
        for d, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0:
                continue
            base = self._index_meta[idx]
            # Both index kinds return the squared L2 distance; report its root.
            results.append(
                NearestPitch(
                    pitch_id=base.pitch_id,
                    game_pk=base.game_pk,
                    season=base.season,
                    pitcher_id=base.pitcher_id,
                    batter_id=base.batter_id,
                    distance=float(np.sqrt(max(d, 0.0))),
                    outcome_type=base.outcome_type,
                )
            )
        return results

    def query_batch(
        self,
        vectors: list[PitchVector] | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> list[list[NearestPitch]]:
        """The K nearest pitches for each of several vectors, in one FAISS search."""
        if self._index is None or self._index_size == 0:
            return [[] for _ in vectors]

        if isinstance(vectors, np.ndarray):
            raw_matrix = vectors.astype(np.float64, copy=False)
        else:
            raw_matrix = np.array([v.to_array() for v in vectors], dtype=np.float64)

        scaled = self._normalizer.normalize_batch(raw_matrix)
        k_eff = max(1, min(k, self._index_size))
        distances, indices = self._index.search(scaled, k_eff)

        out: list[list[NearestPitch]] = []
        for row_d, row_i in zip(distances, indices, strict=False):
            row_results: list[NearestPitch] = []
            for d, idx in zip(row_d, row_i, strict=False):
                if idx < 0:
                    continue
                base = self._index_meta[idx]
                row_results.append(
                    NearestPitch(
                        pitch_id=base.pitch_id,
                        game_pk=base.game_pk,
                        season=base.season,
                        pitcher_id=base.pitcher_id,
                        batter_id=base.batter_id,
                        distance=float(np.sqrt(max(d, 0.0))),
                        outcome_type=base.outcome_type,
                    )
                )
            out.append(row_results)
        return out

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def index_size(self) -> int:
        """The number of indexed rows, the recency copies included."""
        return self._index_size

    @property
    def is_built(self) -> bool:
        """True when the index holds at least one row."""
        return self._index is not None and self._index_size > 0


# ============================================================================
# Helpers
# ============================================================================


def _f(value) -> float:
    """A DuckDB value as a float: a Decimal converts, and None becomes NaN."""
    if value is None:
        return float("nan")
    return float(value)
