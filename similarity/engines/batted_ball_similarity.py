"""
batted_ball_similarity.py
=========================
Step 2.11 — Batted-Ball-to-Batted-Ball Similarity (FAISS)
MLB Baseball Simulation Platform

Indexes every in-play pitch in `sim.outcome_pool` into a FAISS index
keyed by the batted ball's physical fingerprint, and supports fast
K-nearest-neighbor lookup.  Each query returns metadata plus the result
(0-out / 1B / 2B / 3B / HR) so the simulator can sample a realistic
outcome distribution conditional on launch conditions.

This engine is the second of the two FAISS engines in Phase 2 (the
first being SIM-041, pitch-to-pitch).  Together they back the play-pool
sampling architecture for Phase 3.

Feature vector
--------------
    [ exit_velo, launch_angle, spray_angle ]

(3 dims.)  These are the three features that drive ~95 % of the
launch-condition variance in batted-ball outcomes (per Statcast
research and the Baseball Analyst's feature-set sign-off).  Categorical
features (bb_type, fielded_by_position) are intentionally *not* in the
FAISS distance — they're the *labels* we want to recover from
nearest-neighbor results, not the features used to find neighbors.

SIM-051 dependency: pull_relative_spray_angle
---------------------------------------------
Per the BACKLOG sprint plan (2026-05-13), this engine is *supposed* to
use the handedness-corrected `pull_relative_spray_angle` column instead
of raw `spray_angle`.  That column is owned by SIM-051 (Data Engineer)
and was open at sprint kickoff.  The engine ships now using raw
`spray_angle` and falls forward automatically: if the
`pull_relative_spray_angle` column exists in `sim.outcome_pool` at
build time, the loader uses it; otherwise it falls back to raw
`spray_angle` and emits an INFO-level warning so operators know they're
on the unstable feature.

When SIM-051 ships, no engine code change is needed — the next nightly
rebuild picks up the better feature automatically and regression
fixtures should be regenerated.

Recency weighting
-----------------
Same pattern as SIM-041 (pitch-to-pitch): replicate the most-recent
``RECENCY_BOOST_SEASONS`` of pitches once when ``recency_boost=True``
is passed to ``build()``.  Disabled by default for deterministic
testing.

Usage
-----
    engine = BattedBallSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
    engine.build(seasons=[2022, 2023, 2024])

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

# Feature names + per-feature importance weights for sqrt-scaling before
# FAISS L2 indexing.  Calibration of these weights is BA-owned territory.
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

# When SIM-051 ships, the column name on sim.outcome_pool changes from
# `spray_angle` (raw) to `pull_relative_spray_angle` (handedness-corrected).
# The build loader detects which exists and selects accordingly.
PREFERRED_SPRAY_COLUMN = "pull_relative_spray_angle"
FALLBACK_SPRAY_COLUMN = "spray_angle"


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class BattedBallVector:
    """Query vector for the batted-ball FAISS index."""

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
    """One entry in the batted-ball K-nearest-neighbor query output."""

    pitch_id: int  # FK into sim.outcome_pool / sim.pitch_pool
    game_pk: int
    season: int
    batter_id: int
    pitcher_id: int
    distance: float  # Euclidean distance in normalized + weighted space
    bb_type: str | None  # ground_ball / fly_ball / line_drive / popup
    result_hits: int  # 0 / 1 / 2 / 3 / 4 — the labeled outcome


# ============================================================================
# Feature Normalization
# ============================================================================


@dataclass(slots=True)
class BattedBallNormalizer:
    """Z-score normalizer for batted-ball feature vectors."""

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
    Batted-Ball-to-Batted-Ball FAISS Engine (Step 2.11).

    Loads `sim.outcome_pool` rows into memory, fits a normalizer, builds a
    FAISS index over the [exit_velo, launch_angle, spray_angle] vector,
    and answers K-nearest-neighbor queries.  Result entries carry the
    historical outcome (`result_hits` ∈ {0,1,2,3,4}) so the simulation
    loop can sample from a true conditional distribution.

    SIM-051 readiness: at build time the loader picks
    `pull_relative_spray_angle` if it exists on `sim.outcome_pool`,
    otherwise falls back to raw `spray_angle`.
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
        # Recorded after build() so introspection shows whether the
        # SIM-051 column was used.
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
        """Pick `pull_relative_spray_angle` if SIM-051 has shipped, else
        fall back to raw `spray_angle` and warn."""
        try:
            cols = {
                r[0]
                for r in conn.execute("""
                    SELECT column_name FROM information_schema.columns
                     WHERE table_schema = 'sim' AND table_name = 'outcome_pool'
                """).fetchall()
            }
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
    # Outcome aggregation helpers (the *reason* the engine exists)
    # ------------------------------------------------------------------

    def outcome_distribution(
        self,
        vector: BattedBallVector | NDArray[np.float64],
        k: int = DEFAULT_K,
    ) -> dict[int, float]:
        """Return a probability distribution over {0,1,2,3,4} hits drawn
        from the K nearest neighbors.  This is the function the
        simulation loop will actually call."""
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
        """Which spray column was selected by the loader.  Useful for
        operators verifying that SIM-051 has actually shipped."""
        return self._spray_column_used


# ============================================================================
# Helpers
# ============================================================================


def _f(value) -> float:
    if value is None:
        return float("nan")
    return float(value)
