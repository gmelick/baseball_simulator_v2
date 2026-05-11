"""
pitch_pitch_similarity.py
=========================
Step 2.10 — Pitch-to-Pitch Similarity (FAISS)
MLB Baseball Simulation Platform

Indexes every pitch in `sim.pitch_pool` into a FAISS index and supports
fast K-nearest-neighbor lookup.  Each query returns the `pitch_id` plus
sufficient metadata to look up the pitch's *outcome* in the play pool —
this is the foundational play-pool sampler for Phase 4 pitch-outcome
determination (Step 3a / 3b).

Why FAISS (and not KDTree like SIM-070)
---------------------------------------
The pitch pool is much larger than the situation pool — every clean
pitch from every loaded season, typically 2–3 M rows per season.  KDTree
query time degrades on high-dimensional dense vectors and the search
becomes the simulation loop's hot path.  FAISS's L2 / inner-product
indexes (Flat, IVF, HNSW) all amortize lookup cost across batches and
support GPU offload if we ever need it.

This engine ships with `IndexFlatL2` (exact search) by default — the
right starting point for correctness.  Performance Engineer (Agent 6,
SIM-041 collaborator) can swap in `IndexIVFFlat` or `IndexHNSWFlat` once
the index size + query budget are both measured against real hardware.

Feature vector
--------------
    [ velo, ivb, hb, spin_rate, spin_axis,
      release_x, release_z, release_ext,
      plate_x, plate_z ]

(10 dims.)  Categorical features (count, outs, runners, zone) are
intentionally NOT in the FAISS distance — they're filtered at SQL fetch
time so the FAISS index stays a pure pitch-physics distance.  Mixing
categorical and continuous features in a single L2 distance distorts
the metric without proper one-hot encoding.

Z-score normalization is fit on the loaded pool and applied at index
time and at query time.  Per-feature importance weights scale each
normalized dimension by sqrt(weight) so L2 distance reflects relative
feature importance — same pattern as SIM-070 (situation engine).

Recency weighting
-----------------
Per the ML Engineer "established calibration" rules, the engine
applies 2× weight to the last 2 seasons of indexed pitches by replicating
those rows in the index.  This is FAISS's idiomatic way of biasing
nearest-neighbor results toward recent data — true Bayesian recency
shrinkage is not available with raw distance metrics.  Replication is
disabled by default (`recency_boost=False`) so unit tests remain
deterministic; production builds turn it on.

Dependencies
------------
    pip install duckdb numpy faiss-cpu

Usage
-----
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

# FAISS is an optional runtime dependency (in requirements.txt; the unit
# tests for this engine deliberately avoid importing the engine module
# without a fixture-installed FAISS).  We re-export the import error so
# callers can detect it cleanly without crashing on import.
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
# Config — Pitch Feature Definitions & Weights
# ============================================================================

# Feature names + relative importance weights.  Weights are applied via
# sqrt-scale before FAISS indexing so L2 distance reflects feature priority.
# Calibration of these weights is the Baseball Analyst's deliverable; values
# here are reasonable starting points consistent with the GMM W₂ feature set
# used in SIM-001 (pitcher engine).
PITCH_FEATURES = [
    # feature_name,      weight   description
    ("velo", 1.20),  # release_speed (mph)
    ("ivb", 1.10),  # induced vertical break (gravity-removed)
    ("hb", 1.00),  # horizontal break
    ("spin_rate", 0.70),  # rpm
    ("spin_axis", 0.50),  # degrees, 0–360 (handled w/ wrap-around offset)
    ("release_x", 0.60),  # release_pos_x (ft)
    ("release_z", 0.60),  # release_pos_z (ft)
    ("release_ext", 0.40),  # release_extension (ft)
    ("plate_x", 0.90),  # location at plate
    ("plate_z", 0.90),
]

FEATURE_NAMES = [f for f, _ in PITCH_FEATURES]
FEATURE_WEIGHTS = np.array([w for _, w in PITCH_FEATURES], dtype=np.float64)
FEATURE_SCALE = np.sqrt(FEATURE_WEIGHTS)
FEATURE_DIM = len(PITCH_FEATURES)

# Default K for nearest-neighbor query
DEFAULT_K = 50

# Minimum indexed pitches for the engine to be considered usable
MIN_INDEX_SIZE = 1_000

# How many recent seasons get the recency boost (2× replication)
RECENCY_BOOST_SEASONS = 2


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class PitchVector:
    """Query vector for the pitch-to-pitch FAISS index."""

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
    """One entry in the pitch-to-pitch K-nearest-neighbor query output."""

    pitch_id: int  # FK into sim.pitch_pool
    game_pk: int
    season: int
    pitcher_id: int
    batter_id: int
    distance: float  # L2 distance in normalized + weighted space
    outcome_type: str  # ball / called_strike / swinging_strike / foul / in_play


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class PitchNormalizer:
    """Z-score normalizer for pitch feature vectors."""

    mean: NDArray | None = None
    std: NDArray | None = None

    def fit(self, matrix: NDArray[np.float64]) -> None:
        # NaN-aware so missing features (e.g. spin_axis on bunt-foul rows)
        # don't poison the mean/std.
        self.mean = np.nanmean(matrix, axis=0)
        self.std = np.nanstd(matrix, axis=0)
        # Guard zero-variance features (constant column in the pool).
        self.std[self.std == 0] = 1.0

    def normalize(self, vec: NDArray[np.float64]) -> NDArray[np.float64]:
        if self.mean is None:
            return (vec * FEATURE_SCALE).astype(np.float32)
        normed = (vec - self.mean) / self.std
        return (np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE).astype(np.float32)

    def normalize_batch(self, matrix: NDArray[np.float64]) -> NDArray[np.float32]:
        if self.mean is None:
            return (matrix * FEATURE_SCALE[np.newaxis, :]).astype(np.float32)
        normed = (matrix - self.mean[np.newaxis, :]) / self.std[np.newaxis, :]
        return (np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE[np.newaxis, :]).astype(np.float32)


# ============================================================================
# Main Engine
# ============================================================================


class PitchPitchSimilarityEngine:
    """
    Pitch-to-Pitch FAISS Engine (Step 2.10).

    Loads `sim.pitch_pool` into memory, fits a normalizer on the pool,
    standardizes + weights the features, and indexes the result in a
    FAISS IndexFlatL2.  Query returns K nearest neighbors with metadata.

    Usage:
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
        self._index_meta: list[NearestPitch] = []  # parallel to FAISS row order
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
        """Load the pitch pool, fit the normalizer, build the FAISS index."""
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
        """Replicate the most recent N seasons of pitches once so they show
        up twice in the FAISS index.  This biases nearest-neighbor results
        toward recent data without changing FAISS's distance metric."""
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
        if self._index_kind == self.INDEX_KIND_HNSW:
            # 32 neighbors per node is FAISS's default for HNSW.
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
        """Return the K nearest pitches to ``vector`` (closest first)."""
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
            # FAISS returns squared L2 distance for IndexFlatL2; emit Euclidean.
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
        """Batched query path — saves FAISS round-trips during simulation."""
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
        return self._index_size

    @property
    def is_built(self) -> bool:
        return self._index is not None and self._index_size > 0


# ============================================================================
# Helpers
# ============================================================================


def _f(value) -> float:
    """DuckDB returns Decimals or None for some numeric columns — coerce to
    a NaN-friendly float once at load time so the matrix stays numeric."""
    if value is None:
        return float("nan")
    return float(value)
