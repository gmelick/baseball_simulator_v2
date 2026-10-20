"""Batted-ball-to-batted-ball similarity: a nearest-neighbor search over FAISS.

The engine indexes the batted balls of ``sim.outcome_pool`` that carry all
three launch measures in FAISS (a library for fast nearest-neighbor search),
and
answers K-nearest-neighbor queries. Each neighbor carries its real outcome
(``result_hits``: 0 = no hit, 1 = single, 2 = double, 3 = triple, 4 = home
run), so ``outcome_distribution`` gives the outcome mix of the batted balls
most like a given one.

The simulator does not call this engine. Its fielding draw lives in the
full-pool sampler, over the engine-artifact bundle. The app builds this
engine at boot with the other ten, and the Similarity Explorer lists it as a
distance engine.

Feature vector
--------------
    [ exit_velo, launch_angle, spray_angle ]

Each feature is z-scored over the indexed population (a z-score is the
distance from the mean in standard deviations), then scaled by the square
root of its weight. The index measures plain Euclidean (L2) distance, so the
squared distance is the weighted sum of squared z-differences. The batted-ball
type and the fielder are not features: they are the labels a neighbor
carries, not the measures that find it.

The spray angle
---------------
The engine reads ``pull_relative_spray_angle``: the spray angle with its
sign flipped for a left-handed batter, so a pull ball has the same sign from
either side (negative = pull). A left-handed and a right-handed pull ball then
sit together. A table without that column falls back to the raw ``spray_angle``,
and the build logs that the outcomes then mix the two sides.

Recency weighting
-----------------
``build(recency_boost=True)`` adds a second copy of every batted ball from
the last ``RECENCY_BOOST_SEASONS`` seasons, so recent play counts twice. It is
off by default, which keeps the tests deterministic.

Usage
-----
    engine = BattedBallSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
    engine.build(seasons=[2023, 2024, 2025])

    query = BattedBallVector(exit_velo=104.0, launch_angle=22.0, spray_angle=8.0)
    results = engine.query(query, k=50)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray

from db.introspect import duck_columns

try:  # pragma: no cover
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
log = logging.getLogger("batted_ball_similarity")


# ============================================================================
# Config
# ============================================================================

# The features and their weights. Each z-scored feature is multiplied by the
# square root of its weight before indexing, so the L2 distance squared is the
# weighted sum of squared differences. The Baseball Analyst owns these values.
BATTED_BALL_FEATURES = [
    # name,         weight   notes
    ("exit_velo", 1.30),  # mph; primary driver of HR/extra-base outcome
    ("launch_angle", 1.10),  # degrees; defines bb_type implicitly
    ("spray_angle", 0.80),  # degrees; pull / oppo / center axis
]

FEATURE_NAMES = [f for f, _ in BATTED_BALL_FEATURES]
FEATURE_WEIGHTS = np.array([w for _, w in BATTED_BALL_FEATURES], dtype=np.float64)
FEATURE_SCALE = np.sqrt(FEATURE_WEIGHTS)
FEATURE_DIM = len(BATTED_BALL_FEATURES)

DEFAULT_K = 50
MIN_INDEX_SIZE = 1_000
RECENCY_BOOST_SEASONS = 2

# The build reads the handedness-corrected spray angle when sim.outcome_pool has
# it, and the raw one otherwise.
PREFERRED_SPRAY_COLUMN = "pull_relative_spray_angle"
FALLBACK_SPRAY_COLUMN = "spray_angle"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class BattedBallVector:
    """A query for the index.

    ``spray_angle`` must follow the convention of the column the engine built
    on (``spray_column_used``): pull-relative or raw.
    """

    exit_velo: float
    launch_angle: float
    spray_angle: float

    def to_array(self) -> NDArray[np.float64]:
        return np.array(
            [float(self.exit_velo), float(self.launch_angle), float(self.spray_angle)],
            dtype=np.float64,
        )


@dataclass(frozen=True, slots=True)
class NearestBattedBall:
    """One neighbor in a query's output, with its real outcome."""

    pitch_id: int  # FK into sim.outcome_pool / sim.pitch_pool
    game_pk: int
    season: int
    batter_id: int
    pitcher_id: int
    distance: float  # Euclidean distance in the z-scored, weighted space
    bb_type: str | None  # ground_ball / fly_ball / line_drive / popup
    result_hits: int  # 0 = no hit, 1-4 = single to home run (NULL reads 0)


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class BattedBallNormalizer:
    """Z-score normalizer for the batted-ball features; it also applies the weights.

    A NaN z-scores to 0, the mean. A feature with no spread keeps a standard
    deviation of 1. Before ``fit`` runs, a vector is only weighted.
    """

    mean: NDArray | None = None
    std: NDArray | None = None

    def fit(self, matrix: NDArray[np.float64]) -> None:
        self.mean = np.nanmean(matrix, axis=0)
        self.std = np.nanstd(matrix, axis=0)
        self.std[self.std == 0] = 1.0

    def normalize(self, vec: NDArray[np.float64]) -> NDArray[np.float32]:
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


class BattedBallSimilarityEngine:
    """
    The batted-ball-to-batted-ball FAISS engine.

    ``build`` loads the ``sim.outcome_pool`` rows into memory, fits the
    normalizer and builds a FAISS index over [exit_velo, launch_angle,
    spray_angle]. ``query`` answers K-nearest-neighbor queries; each neighbor
    carries its real outcome (``result_hits``).

    The index is exact (``flat``) by default. ``hnsw`` builds an approximate
    graph index (HNSW, a hierarchical small-world graph) that answers faster
    and can miss a true neighbor.
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
        self._normalizer = BattedBallNormalizer()
        self._index: faiss.Index | None = None
        self._index_meta: list[NearestBattedBall] = []
        self._index_size = 0
        # The spray column the last build read, for a reader to check.
        self._spray_column_used: str | None = None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        seasons: list[int] | None = None,
        *,
        recency_boost: bool = False,
    ) -> None:
        """Load the batted balls of ``seasons`` (every season when None) and index them.

        ``recency_boost`` works only with an explicit season list; it runs
        before the normalizer fit, so the copies count in the mean and spread.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            spray_col = self._select_spray_column(conn)
            self._spray_column_used = spray_col
            raw_matrix, meta = self._load_pool(conn, seasons, spray_col)
        finally:
            conn.close()

        if len(raw_matrix) < MIN_INDEX_SIZE:
            log.warning(
                "BattedBallSimilarityEngine: only %d batted balls loaded "
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
            "BattedBallSimilarityEngine built: %d batted balls indexed (%s) "
            "via spray-column=%r in %.2fs.",
            self._index_size,
            self._index_kind,
            spray_col,
            elapsed,
        )

    @staticmethod
    def _select_spray_column(conn: duckdb.DuckDBPyConnection) -> str:
        """The pull-relative spray column when the table has it, else the raw one.

        The fallback logs a line at INFO level.
        """
        try:
            cols = duck_columns(conn, "sim", "outcome_pool")
        except Exception:  # pragma: no cover — non-DuckDB connection
            cols = set()

        if PREFERRED_SPRAY_COLUMN in cols:
            return PREFERRED_SPRAY_COLUMN
        log.info(
            "BattedBallSimilarityEngine: SIM-051 column %r not found; "
            "falling back to raw %r.  Outcomes will be slightly biased "
            "by handedness until SIM-051 ships.",
            PREFERRED_SPRAY_COLUMN,
            FALLBACK_SPRAY_COLUMN,
        )
        return FALLBACK_SPRAY_COLUMN

    def _load_pool(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
        spray_col: str,
    ) -> tuple[NDArray[np.float64], list[NearestBattedBall]]:
        sf = ""
        if seasons:
            sf = f"AND season IN ({', '.join(str(s) for s in seasons)})"

        rows = conn.execute(f"""
            SELECT
                pitch_id, game_pk, season,
                batter_id, pitcher_id,
                exit_velo, launch_angle, {spray_col} AS spray_angle,
                bb_type, result_hits
              FROM sim.outcome_pool
             WHERE exit_velo IS NOT NULL
               AND launch_angle IS NOT NULL
               AND {spray_col} IS NOT NULL
               {sf}
        """).fetchall()

        n = len(rows)
        matrix = np.empty((n, FEATURE_DIM), dtype=np.float64)
        meta: list[NearestBattedBall] = []
        for i, r in enumerate(rows):
            (
                pitch_id,
                game_pk,
                season,
                batter_id,
                pitcher_id,
                exit_velo,
                launch_angle,
                spray_angle,
                bb_type,
                result_hits,
            ) = r
            matrix[i, :] = [_f(exit_velo), _f(launch_angle), _f(spray_angle)]
            meta.append(
                NearestBattedBall(
                    pitch_id=pitch_id,
                    game_pk=game_pk,
                    season=season,
                    batter_id=batter_id,
                    pitcher_id=pitcher_id,
                    distance=0.0,
                    bb_type=bb_type,
                    result_hits=int(result_hits or 0),
                )
            )
        return matrix, meta

    def _apply_recency_boost(
        self,
        raw_matrix: NDArray[np.float64],
        meta: list[NearestBattedBall],
        seasons: list[int],
    ) -> tuple[NDArray[np.float64], list[NearestBattedBall]]:
        """Append a copy of every row from the last RECENCY_BOOST_SEASONS seasons.

        A query can then return a recent ball twice, so it counts twice in
        ``outcome_distribution``.
        """
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
        vector: BattedBallVector | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> list[NearestBattedBall]:
        """The ``k`` nearest batted balls, nearest first.

        ``k`` is capped at the index size. An unbuilt or empty index returns
        an empty list.
        """
        if self._index is None or self._index_size == 0:
            return []

        if isinstance(vector, BattedBallVector):
            raw = vector.to_array()
        else:
            raw = np.asarray(vector, dtype=np.float64)

        scaled = self._normalizer.normalize(raw).reshape(1, -1)
        k_eff = max(1, min(k, self._index_size))
        distances, indices = self._index.search(scaled, k_eff)

        results: list[NearestBattedBall] = []
        for d, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0:
                continue
            base = self._index_meta[idx]
            results.append(
                NearestBattedBall(
                    pitch_id=base.pitch_id,
                    game_pk=base.game_pk,
                    season=base.season,
                    batter_id=base.batter_id,
                    pitcher_id=base.pitcher_id,
                    distance=float(np.sqrt(max(d, 0.0))),
                    bb_type=base.bb_type,
                    result_hits=base.result_hits,
                )
            )
        return results

    def query_batch(
        self,
        vectors: list[BattedBallVector] | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> list[list[NearestBattedBall]]:
        """``query`` for each vector, in one FAISS search."""
        if self._index is None or self._index_size == 0:
            return [[] for _ in vectors]

        if isinstance(vectors, np.ndarray):
            raw_matrix = vectors.astype(np.float64, copy=False)
        else:
            raw_matrix = np.array([v.to_array() for v in vectors], dtype=np.float64)

        scaled = self._normalizer.normalize_batch(raw_matrix)
        k_eff = max(1, min(k, self._index_size))
        distances, indices = self._index.search(scaled, k_eff)

        out: list[list[NearestBattedBall]] = []
        for row_d, row_i in zip(distances, indices, strict=False):
            row_results: list[NearestBattedBall] = []
            for d, idx in zip(row_d, row_i, strict=False):
                if idx < 0:
                    continue
                base = self._index_meta[idx]
                row_results.append(
                    NearestBattedBall(
                        pitch_id=base.pitch_id,
                        game_pk=base.game_pk,
                        season=base.season,
                        batter_id=base.batter_id,
                        pitcher_id=base.pitcher_id,
                        distance=float(np.sqrt(max(d, 0.0))),
                        bb_type=base.bb_type,
                        result_hits=base.result_hits,
                    )
                )
            out.append(row_results)
        return out

    # ------------------------------------------------------------------
    # Outcome mix
    # ------------------------------------------------------------------

    def outcome_distribution(
        self,
        vector: BattedBallVector | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> dict[int, float]:
        """The share of each ``result_hits`` value among the ``k`` nearest
        neighbors; an outcome no neighbor had is absent. Empty when the index
        is empty."""
        results = self.query(vector, k=k)
        if not results:
            return {}
        counts: dict[int, int] = {}
        for r in results:
            counts[r.result_hits] = counts.get(r.result_hits, 0) + 1
        total = sum(counts.values())
        return {k_: v / total for k_, v in counts.items()}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def index_size(self) -> int:
        return self._index_size

    @property
    def is_built(self) -> bool:
        return self._index is not None and self._index_size > 0

    @property
    def spray_column_used(self) -> str | None:
        """The spray column the last build read, or None before a build."""
        return self._spray_column_used


# ============================================================================
# Helpers
# ============================================================================


def _f(value) -> float:
    """A float, with NULL read as NaN."""
    if value is None:
        return float("nan")
    return float(value)
