"""
situation_similarity.py
=======================
Step 2.9 — Situation-to-Situation Similarity (KDTree)
MLB Baseball Simulation Platform

Finds the K historically nearest game situations to a query situation
and returns the play outcomes that occurred in those situations.  This
is the foundational play-pool lookup mechanism for the Phase 4 simulation
loop: rather than sampling from an unconditioned distribution, the
simulation samples from outcomes observed in similar situations.

Unlike all other similarity engines (which compare players), this engine
compares GAME STATES.  The "profile" is a game state vector:

    [inning, top_or_bottom, outs, runner_on_1b, runner_on_2b,
     runner_on_3b, score_differential, leverage_index,
     pitcher_pitch_count, batter_pa_count, park_factor_runs]

KDTree Choice
-------------
  Unlike the RBF engines (which compute exhaustive pairwise scores),
  the situation engine uses a scipy KDTree for approximate K-nearest-
  neighbor lookup.  This is appropriate because:

    1. The index size is MUCH larger — millions of historical situations
       vs. thousands of player-seasons.
    2. The query is approximate by nature — we don't need the globally
       closest 50 situations, just a representative sample of near
       neighbors.
    3. KDTree query scales as O(K * log N) rather than O(N), which is
       critical for the simulation loop running 100 iterations per game.

  The KDTree is rebuilt from historical data during engine initialization
  and stored in memory.  It is NOT rebuilt during a live game.

Feature Normalization
---------------------
  All features are z-score normalized before indexing.  The KDTree uses
  Euclidean distance in normalized space, which is equivalent to a
  distance function that weights all dimensions equally — which is the
  correct behavior after z-score normalization.

  Categorical features (outs, baserunner state, inning) are treated as
  continuous after normalization.  A 2-out situation is meaningfully
  different from a 0-out situation, and the normalized Euclidean distance
  captures this correctly.

Leverage Index
--------------
  leverage_index (LI) is included as a dimension because it captures
  something inning + outs + runners don't fully express.  An 8th-inning
  runner-on-second situation is different in a 1-run game vs. a 7-run
  game.  LI compresses this into a single float.  Higher LI → higher
  variance in play outcomes, which should pull similar situations
  together.

Columnar Metadata (SIM-334)
---------------------------
  The per-row situation metadata is stored as parallel NumPy column
  arrays (``ColumnarSituationMeta``) rather than a ``list[NearestSituation]``
  of Python objects.  The list cost ~120 MB at 1 M rows and was
  un-shareable across processes.  The columnar store is contiguous,
  compact, and trivially share-able read-only via mmap / shared memory,
  while reconstructing byte-for-byte identical ``NearestSituation`` results
  on demand so the public query API is unchanged.

Dependencies
------------
  pip install duckdb numpy scipy

Usage
-----
  engine = SituationSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2019, 2020, 2021, 2022, 2023, 2024])

  # Query: find 50 most similar historical situations to the current state
  query_state = SituationVector(
      inning=7, top_or_bottom=0, outs=1,
      runner_on_1b=1, runner_on_2b=0, runner_on_3b=0,
      score_differential=-1, leverage_index=2.1,
      pitcher_pitch_count=82, batter_pa_count=3,
      park_factor_runs=1.05,
  )
  nearest = engine.query(query_state, k=50)

  # Each result carries the play_id to look up outcomes in the play pool
  play_ids = [r.play_id for r in nearest]
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import duckdb
import numpy as np
from numpy.typing import NDArray
from scipy.spatial import KDTree

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("situation_similarity")


# ============================================================================
# Config — Situation Feature Definitions & Weights
# ============================================================================

# Feature names and their relative importance weights for z-score normalization.
# KDTree uses Euclidean distance after normalization.  These weights are applied
# by multiplying the normalized feature values by sqrt(weight) before indexing,
# which scales Euclidean distance proportionally to feature importance.
SITUATION_FEATURES = [
    # feature_name,             weight   description
    ("inning", 0.80),  # 1-9 (or extra innings up to 12)
    ("top_or_bottom", 0.50),  # 0 = top, 1 = bottom
    ("outs", 1.00),  # 0, 1, or 2
    ("runner_on_1b", 0.80),  # 0 or 1
    ("runner_on_2b", 1.00),  # 0 or 1 (RISP — higher impact)
    ("runner_on_3b", 0.90),  # 0 or 1 (run about to score)
    ("score_differential", 0.70),  # home - away score; clipped [-5, 5]
    ("leverage_index", 1.20),  # real-valued LI; strong discriminator
    ("pitcher_pitch_count", 0.60),  # proxy for fatigue and stuff decay
    ("batter_pa_count", 0.40),  # times through order effect
    ("park_factor_runs", 0.50),  # 1.0 = average; range ~0.85-1.15
]

FEATURE_NAMES = [f for f, _ in SITUATION_FEATURES]
FEATURE_WEIGHTS = np.array([w for _, w in SITUATION_FEATURES], dtype=np.float64)
# Scaling factors applied before KDTree insert: multiply each normalized feature
# by sqrt(weight) so Euclidean distance reflects relative importance.
FEATURE_SCALE = np.sqrt(FEATURE_WEIGHTS)

# Score differential is clipped to [-5, 5] to prevent blowout games from
# distorting the distance metric.
SCORE_DIFF_CLIP = 5

# Default K for nearest-neighbor query
DEFAULT_K = 50

# Minimum historical situations for the engine to be usable
MIN_INDEX_SIZE = 1000


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class SituationVector:
    """
    A game state vector for KDTree query or indexing.

    All fields should be set to the current values at the moment of the
    pitch (i.e., BEFORE the play outcome is known).
    """

    inning: int  # 1-based inning number
    top_or_bottom: int  # 0 = top (away batting), 1 = bottom (home batting)
    outs: int  # 0, 1, or 2
    runner_on_1b: int  # 1 if occupied, 0 otherwise
    runner_on_2b: int
    runner_on_3b: int
    score_differential: float  # home_score - away_score at moment of pitch
    leverage_index: float  # pre-pitch LI (0.0 to ~10.0, typical range 0.1-4.0)
    pitcher_pitch_count: int  # pitches thrown so far this appearance
    batter_pa_count: int  # plate appearances by this batter this game
    park_factor_runs: float  # park run factor (1.0 = league average)

    def to_array(self) -> NDArray[np.float64]:
        return np.array(
            [
                float(self.inning),
                float(self.top_or_bottom),
                float(self.outs),
                float(self.runner_on_1b),
                float(self.runner_on_2b),
                float(self.runner_on_3b),
                float(np.clip(self.score_differential, -SCORE_DIFF_CLIP, SCORE_DIFF_CLIP)),
                float(self.leverage_index),
                float(self.pitcher_pitch_count),
                float(self.batter_pa_count),
                float(self.park_factor_runs),
            ],
            dtype=np.float64,
        )


@dataclass(frozen=True, slots=True)
class NearestSituation:
    """One entry in the K-nearest-neighbor query output."""

    play_id: str  # foreign key into the play pool / play-by-play table
    game_pk: int  # game identifier
    distance: float  # Euclidean distance in normalized+weighted space
    inning: int
    outs: int
    runners: int  # bitmask: 0b001=1B, 0b010=2B, 0b100=3B
    leverage_index: float
    score_differential: float


# ============================================================================
# Columnar metadata store (SIM-334)
# ============================================================================
#
# The engine previously stored row metadata as a `list[NearestSituation]` —
# one ~120-byte Python slots object per indexed PA (~120 MB at 1 M rows).  That
# list is un-shareable across processes (it is a graph of Python objects) and is
# *larger than the raw KDTree data itself* (SIM-280 §2.1).  This columnar store
# replaces that list with parallel NumPy arrays — one dtype-appropriate array per
# `NearestSituation` field — which are contiguous, compact, and trivially
# share-able read-only via `multiprocessing.shared_memory` / mmap (SIM-281 D2).
#
# Field → array mapping (the per-row `distance` is NOT stored: it is always 0.0
# at index time and is filled in from the KDTree result at query time):
#
#   play_id            -> str   : fixed-width unicode array (np.dtype('U<W>')),
#                                 width auto-sized to the longest id; play_ids
#                                 are near-unique foreign keys so an int-code +
#                                 category table buys nothing — a fixed-width
#                                 array is the compact, share-able encoding.
#   game_pk            -> int   : np.int64
#   inning             -> int   : np.int16
#   outs               -> int   : np.int8
#   runners (bitmask)  -> int   : np.int8   (values 0..7)
#   leverage_index     -> float : np.float64
#   score_differential -> float : np.float64
#
# The store reconstructs a byte-for-byte identical `NearestSituation` on demand,
# so the public query API and result objects are unchanged.  It is also a drop-in
# replacement for the old `list[NearestSituation]` everywhere it was iterated or
# indexed (it supports `len()`, integer indexing, and iteration, each yielding a
# reconstructed `NearestSituation`).


@dataclass(slots=True)
class ColumnarSituationMeta:
    """
    Column-parallel NumPy store of per-row situation metadata.

    Replaces ``list[NearestSituation]``.  One array per field; integer indexing,
    iteration, and ``len()`` reconstruct a ``NearestSituation`` (with
    ``distance=0.0``, matching the old index-time sentinel) so existing callers
    that treat ``_index_meta`` as a sequence of ``NearestSituation`` keep working
    unchanged.

    The arrays are write-protected (``flags.writeable = False``) after
    construction so the store is read-only/lazy: callers cannot mutate the index
    in place, which keeps it safe to share read-only across processes.
    """

    play_id: NDArray  # dtype '<U...' fixed-width unicode
    game_pk: NDArray[np.int64]
    inning: NDArray[np.int16]
    outs: NDArray[np.int8]
    runners: NDArray[np.int8]
    leverage_index: NDArray[np.float64]
    score_differential: NDArray[np.float64]

    def __post_init__(self) -> None:
        # Make the columnar store read-only.  Shared read-only arrays must not be
        # mutated by any consumer; freezing the write flag enforces that and is a
        # prerequisite for zero-copy sharing across processes.
        for arr in (
            self.play_id,
            self.game_pk,
            self.inning,
            self.outs,
            self.runners,
            self.leverage_index,
            self.score_differential,
        ):
            try:
                arr.flags.writeable = False
            except (ValueError, AttributeError):
                # A non-owning view may refuse; that is acceptable for our use.
                pass

    @classmethod
    def empty(cls) -> ColumnarSituationMeta:
        """An empty store (zero rows) — used for the empty/missing-catalog path."""
        return cls(
            play_id=np.empty(0, dtype="<U1"),
            game_pk=np.empty(0, dtype=np.int64),
            inning=np.empty(0, dtype=np.int16),
            outs=np.empty(0, dtype=np.int8),
            runners=np.empty(0, dtype=np.int8),
            leverage_index=np.empty(0, dtype=np.float64),
            score_differential=np.empty(0, dtype=np.float64),
        )

    @classmethod
    def from_columns(
        cls,
        *,
        play_id: list[str],
        game_pk: list[int],
        inning: list[int],
        outs: list[int],
        runners: list[int],
        leverage_index: list[float],
        score_differential: list[float],
    ) -> ColumnarSituationMeta:
        """Build the store from per-field Python lists collected during load."""
        n = len(game_pk)
        if n == 0:
            return cls.empty()
        return cls(
            # np.array on a list[str] picks the minimal fixed-width '<U' dtype.
            play_id=np.array(play_id, dtype=np.str_),
            game_pk=np.asarray(game_pk, dtype=np.int64),
            inning=np.asarray(inning, dtype=np.int16),
            outs=np.asarray(outs, dtype=np.int8),
            runners=np.asarray(runners, dtype=np.int8),
            leverage_index=np.asarray(leverage_index, dtype=np.float64),
            score_differential=np.asarray(score_differential, dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.game_pk.shape[0])

    def row(self, idx: int, distance: float = 0.0) -> NearestSituation:
        """Reconstruct the NearestSituation at ``idx`` (default index-time sentinel)."""
        return NearestSituation(
            play_id=str(self.play_id[idx]),
            game_pk=int(self.game_pk[idx]),
            distance=float(distance),
            inning=int(self.inning[idx]),
            outs=int(self.outs[idx]),
            runners=int(self.runners[idx]),
            leverage_index=float(self.leverage_index[idx]),
            score_differential=float(self.score_differential[idx]),
        )

    def __getitem__(self, idx: int) -> NearestSituation:
        return self.row(int(idx))

    def __iter__(self):
        for i in range(len(self)):
            yield self.row(i)


# ============================================================================
# Feature Normalization (fit on indexed situations, apply to query)
# ============================================================================


@dataclass(slots=True)
class SituationNormalizer:
    """Z-score normalizer for situation feature vectors."""

    mean: NDArray | None = None
    std: NDArray | None = None

    def fit(self, matrix: NDArray[np.float64]) -> None:
        """Fit on the full historical situation matrix (shape N × n_features)."""
        self.mean = np.nanmean(matrix, axis=0)
        self.std = np.nanstd(matrix, axis=0)
        self.std[self.std == 0] = 1.0

    def normalize(self, vec: NDArray[np.float64]) -> NDArray[np.float64]:
        """Normalize a single situation vector and apply importance weighting."""
        if self.mean is None:
            return vec * FEATURE_SCALE
        normed = (vec - self.mean) / self.std
        return np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE

    def normalize_batch(self, matrix: NDArray[np.float64]) -> NDArray[np.float64]:
        """Normalize a batch of situation vectors."""
        if self.mean is None:
            return matrix * FEATURE_SCALE[np.newaxis, :]
        normed = (matrix - self.mean[np.newaxis, :]) / self.std[np.newaxis, :]
        return np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE[np.newaxis, :]


# ============================================================================
# Main Engine
# ============================================================================


class SituationSimilarityEngine:
    """
    Situation-to-Situation KDTree Engine (Step 2.9).

    Indexes millions of historical at-bat situations and supports fast
    K-nearest-neighbor lookup for play-pool sampling in the simulation loop.

    The engine stores:
      - A KDTree of normalized situation vectors (for fast lookup)
      - A ColumnarSituationMeta of per-row metadata (parallel NumPy column
        arrays) for result construction.  This replaces the old
        ``list[NearestSituation]`` (SIM-334) to cut memory ~10x and make the
        index share-able read-only across processes.

    Usage:
        engine = SituationSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2019, 2020, 2021, 2022, 2023, 2024])
        nearest = engine.query(query_state, k=50)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._normalizer = SituationNormalizer()
        self._kdtree: KDTree | None = None
        # Columnar metadata store, parallel to the kdtree leaves (SIM-334).
        self._index_meta: ColumnarSituationMeta = ColumnarSituationMeta.empty()
        self._index_size = 0

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """
        Load historical at-bat situations from DuckDB and build the KDTree.

        The KDTree is built from the derived.at_bat_situations view, which
        is a pre-computed join of pitches, game states, and park factors.
        Each row represents a PLATE APPEARANCE (not a pitch) — we index
        by PA because the simulation loop operates at PA granularity.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            raw_matrix, meta = self._load_situations(conn, seasons)
        finally:
            conn.close()

        if len(raw_matrix) == 0:
            # SIM-408 hardening: a zero-row index is degenerate. Normalizing an
            # empty matrix yields a NaN normalization (numpy emits "Mean of
            # empty slice" / "Degrees of freedom <= 0"), producing a hollow
            # engine that would silently feed NaN situation similarities — which
            # is strictly worse than being absent. Raise instead, so
            # ``api.state.build_all_engines`` SKIPS this engine (marks it failed,
            # exactly like the steal engines whose source tables are likewise
            # missing) rather than registering a NaN-poisoned one. The live
            # trigger is a missing/empty ``derived.at_bat_situations`` — see
            # docs/audit/2026-05-29-sim408-engine-schema-divergence.md.
            raise RuntimeError(
                "SituationSimilarityEngine: no situations loaded "
                "(derived.at_bat_situations is missing or empty) — refusing to "
                "build a degenerate empty index."
            )

        if len(raw_matrix) < MIN_INDEX_SIZE:
            log.warning(
                "SituationSimilarityEngine: only %d situations loaded "
                "(minimum %d). Build may not be reliable.",
                len(raw_matrix),
                MIN_INDEX_SIZE,
            )

        # Fit normalizer and build scaled matrix
        self._normalizer.fit(raw_matrix)
        scaled = self._normalizer.normalize_batch(raw_matrix)

        self._kdtree = KDTree(scaled)
        self._index_meta = meta
        self._index_size = len(meta)

        elapsed = time.time() - t0
        log.info(
            "SituationSimilarityEngine built: %d situations indexed in %.2fs.",
            self._index_size,
            elapsed,
        )

    def _load_situations(
        self,
        conn: duckdb.DuckDBPyConnection,
        seasons: list[int] | None,
    ) -> tuple[NDArray[np.float64], ColumnarSituationMeta]:
        """Load situations from DuckDB into a NumPy matrix and columnar metadata."""
        sf = ""
        if seasons:
            sf = f"AND abs.season IN ({', '.join(str(s) for s in seasons)})"

        try:
            rows = conn.execute(f"""
                SELECT
                    abs.play_id,
                    abs.game_pk,
                    abs.inning,
                    abs.top_or_bottom,
                    abs.outs_when_up,
                    abs.on_1b,
                    abs.on_2b,
                    abs.on_3b,
                    COALESCE(abs.home_score, 0) - COALESCE(abs.away_score, 0)
                        AS score_differential,
                    COALESCE(abs.leverage_index, 1.0)  AS leverage_index,
                    COALESCE(abs.pitcher_pitch_count, 0) AS pitcher_pc,
                    COALESCE(abs.batter_pa_count, 1)   AS batter_pa,
                    COALESCE(pf.regressed_factor, 1.0) AS park_factor_runs
                FROM derived.at_bat_situations abs
                LEFT JOIN derived.park_factors pf
                    ON pf.venue_id = abs.venue_id
                    AND pf.season  = abs.season
                    AND pf.factor_type = 'R'   -- SIM-408: park_factors has no run_factor col;
                                               -- the run park factor is the 'R' factor_type row
                WHERE abs.inning IS NOT NULL
                  {sf}
                ORDER BY abs.game_pk, abs.inning, abs.at_bat_number
            """).fetchall()
        except duckdb.CatalogException as e:
            log.warning("Could not load at_bat_situations: %s", e)
            return (
                np.empty((0, len(SITUATION_FEATURES)), dtype=np.float64),
                ColumnarSituationMeta.empty(),
            )

        n = len(rows)
        log.info("Loading %d historical situations from DuckDB …", n)

        matrix = np.empty((n, len(SITUATION_FEATURES)), dtype=np.float64)

        # Collect per-field columns (Python lists), then pack into NumPy arrays
        # once at the end via ColumnarSituationMeta.from_columns().  This keeps
        # the load loop allocation-light and avoids one Python object per row.
        col_play_id: list[str] = []
        col_game_pk: list[int] = []
        col_inning: list[int] = []
        col_outs: list[int] = []
        col_runners: list[int] = []
        col_leverage_index: list[float] = []
        col_score_differential: list[float] = []

        for idx, row in enumerate(rows):
            (
                play_id,
                game_pk,
                inning,
                top_or_bottom,
                outs,
                on_1b,
                on_2b,
                on_3b,
                score_diff,
                li,
                pc,
                pa_count,
                park_factor,
            ) = row

            runners_bitmask = (1 if on_1b else 0) | (2 if on_2b else 0) | (4 if on_3b else 0)
            score_diff_clipped = float(
                np.clip(score_diff or 0.0, -SCORE_DIFF_CLIP, SCORE_DIFF_CLIP)
            )

            matrix[idx] = [
                float(inning or 1),
                float(top_or_bottom or 0),
                float(outs or 0),
                float(1 if on_1b else 0),
                float(1 if on_2b else 0),
                float(1 if on_3b else 0),
                score_diff_clipped,
                float(li or 1.0),
                float(pc or 0),
                float(pa_count or 1),
                float(park_factor or 1.0),
            ]

            col_play_id.append(str(play_id or ""))
            col_game_pk.append(int(game_pk or 0))
            col_inning.append(int(inning or 1))
            col_outs.append(int(outs or 0))
            col_runners.append(runners_bitmask)
            col_leverage_index.append(float(li or 1.0))
            col_score_differential.append(score_diff_clipped)

        meta = ColumnarSituationMeta.from_columns(
            play_id=col_play_id,
            game_pk=col_game_pk,
            inning=col_inning,
            outs=col_outs,
            runners=col_runners,
            leverage_index=col_leverage_index,
            score_differential=col_score_differential,
        )

        return matrix, meta

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        situation: SituationVector,
        k: int = DEFAULT_K,
    ) -> list[NearestSituation]:
        """
        Find the K most similar historical situations to the query vector.

        Parameters
        ----------
        situation : SituationVector
            Current game state (before the play).
        k : int
            Number of nearest neighbors to return.

        Returns
        -------
        List of NearestSituation, sorted by distance ascending (closest first).
        """
        if self._kdtree is None:
            raise RuntimeError("Engine not built. Call build() first.")

        if self._index_size == 0:
            return []

        effective_k = min(k, self._index_size)

        query_vec = self._normalizer.normalize(situation.to_array())
        distances, indices = self._kdtree.query(query_vec, k=effective_k)

        # KDTree returns scalars when k=1; normalize to arrays
        if effective_k == 1:
            distances = np.array([distances])
            indices = np.array([indices])

        meta = self._index_meta
        results = []
        for dist, idx in zip(distances, indices, strict=False):
            # Reconstruct the NearestSituation at this index, filling in the
            # query-time distance.  Field values are identical to the old
            # list[NearestSituation] path.
            results.append(self._row_from_meta(meta, idx, dist))

        return results

    def query_batch(
        self,
        situations: list[SituationVector],
        k: int = DEFAULT_K,
    ) -> list[list[NearestSituation]]:
        """
        Batch query: find K nearest situations for each query in the list.

        More efficient than calling query() N times for simulation batches.
        """
        if self._kdtree is None:
            raise RuntimeError("Engine not built. Call build() first.")

        if self._index_size == 0:
            return [[] for _ in situations]

        effective_k = min(k, self._index_size)
        query_matrix = np.array(
            [self._normalizer.normalize(s.to_array()) for s in situations], dtype=np.float64
        )

        all_distances, all_indices = self._kdtree.query(query_matrix, k=effective_k)

        # Normalize shape for k=1
        if effective_k == 1:
            all_distances = all_distances[:, np.newaxis]
            all_indices = all_indices[:, np.newaxis]

        meta = self._index_meta
        results = []
        for distances, indices in zip(all_distances, all_indices, strict=False):
            row = []
            for dist, idx in zip(distances, indices, strict=False):
                row.append(self._row_from_meta(meta, idx, dist))
            results.append(row)

        return results

    @staticmethod
    def _row_from_meta(meta, idx, distance) -> NearestSituation:
        """Reconstruct a NearestSituation at ``idx`` with the query distance.

        Handles BOTH the SIM-334 columnar store (``meta.row``) and a plain
        ``list[NearestSituation]`` (the regression/test-injection path), so
        query results are identical regardless of how ``_index_meta`` was
        populated.
        """
        if hasattr(meta, "row"):
            return meta.row(int(idx), distance=float(distance))
        base = meta[int(idx)]
        return NearestSituation(
            play_id=base.play_id,
            game_pk=base.game_pk,
            distance=float(distance),
            inning=base.inning,
            outs=base.outs,
            runners=base.runners,
            leverage_index=base.leverage_index,
            score_differential=base.score_differential,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @property
    def index_size(self) -> int:
        """Number of historical situations in the index."""
        return self._index_size

    def is_built(self) -> bool:
        return self._kdtree is not None

    def situation_count_by_outs(self) -> dict[int, int]:
        """Returns {outs_value: count} for the full index."""
        counts: dict[int, int] = {0: 0, 1: 0, 2: 0}
        # Count directly off the columnar array (no per-row object reconstruction).
        outs_arr = self._index_meta.outs
        if len(outs_arr) > 0:
            vals, vcounts = np.unique(outs_arr, return_counts=True)
            for v, c in zip(vals, vcounts, strict=False):
                counts[int(v)] = counts.get(int(v), 0) + int(c)
        return counts

    def situation_count_by_inning(self) -> dict[int, int]:
        """Returns {inning: count} for the full index."""
        counts: dict[int, int] = {}
        inning_arr = self._index_meta.inning
        if len(inning_arr) > 0:
            vals, vcounts = np.unique(inning_arr, return_counts=True)
            for v, c in zip(vals, vcounts, strict=False):
                counts[int(v)] = int(c)
        return sorted_dict(counts)


def sorted_dict(d: dict) -> dict:
    return {k: d[k] for k in sorted(d)}


# ============================================================================
# Convenience: Inning/Outs coverage summary
# ============================================================================


def build_coverage_report(engine: SituationSimilarityEngine) -> str:
    """
    Human-readable coverage summary showing how many historical situations
    exist per (inning, outs) combination.  Helps validate that the index
    has good coverage for all game states the simulation will encounter.
    """
    from collections import Counter

    counter: Counter = Counter()
    meta = engine._index_meta
    # Pair (inning, outs) directly off the columnar arrays.
    inning_arr = meta.inning
    outs_arr = meta.outs
    for inning_val, outs_val in zip(inning_arr.tolist(), outs_arr.tolist(), strict=False):
        counter[(int(inning_val), int(outs_val))] += 1

    lines = [
        "SituationSimilarityEngine Coverage Report",
        "=" * 50,
        f"Total indexed situations: {engine.index_size:,}",
        "",
        f"{'Inning':<8} {'Outs=0':>8} {'Outs=1':>8} {'Outs=2':>8}",
    ]
    for inning in range(1, 13):
        row = [
            counter.get((inning, 0), 0),
            counter.get((inning, 1), 0),
            counter.get((inning, 2), 0),
        ]
        if any(row):
            lines.append(f"{inning:<8} {row[0]:>8,} {row[1]:>8,} {row[2]:>8,}")

    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    engine = SituationSimilarityEngine(duckdb_path="../../db/schemas/baseball_simulator.duckdb")
    engine.build(seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017])
    print(build_coverage_report(engine))

    # Example query: 7th inning, 1 out, runner on 2nd, tie game
    query = SituationVector(
        inning=7,
        top_or_bottom=0,
        outs=1,
        runner_on_1b=0,
        runner_on_2b=1,
        runner_on_3b=0,
        score_differential=0.0,
        leverage_index=2.1,
        pitcher_pitch_count=82,
        batter_pa_count=2,
        park_factor_runs=1.0,
    )
    nearest = engine.query(query, k=20)
    print("\nTop 20 nearest situations:")
    for r in nearest[:5]:
        print(
            f"  play_id={r.play_id}  game_pk={r.game_pk}  "
            f"inn={r.inning}  outs={r.outs}  runners={r.runners:03b}  "
            f"li={r.leverage_index:.2f}  dist={r.distance:.4f}"
        )
