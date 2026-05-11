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
      - A parallel array of NearestSituation metadata (for result construction)

    Usage:
        engine = SituationSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2019, 2020, 2021, 2022, 2023, 2024])
        nearest = engine.query(query_state, k=50)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._normalizer = SituationNormalizer()
        self._kdtree: KDTree | None = None
        self._index_meta: list[NearestSituation] = []  # parallel to kdtree leafs
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
    ) -> tuple[NDArray[np.float64], list[NearestSituation]]:
        """Load situations from DuckDB into a NumPy matrix and metadata list."""
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
                    COALESCE(pf.run_factor, 1.0)        AS park_factor_runs
                FROM derived.at_bat_situations abs
                LEFT JOIN derived.park_factors pf
                    ON pf.venue_id = abs.venue_id
                    AND pf.season  = abs.season
                WHERE abs.inning IS NOT NULL
                  {sf}
                ORDER BY abs.game_pk, abs.inning, abs.at_bat_number
            """).fetchall()
        except duckdb.CatalogException as e:
            log.warning("Could not load at_bat_situations: %s", e)
            return np.empty((0, len(SITUATION_FEATURES)), dtype=np.float64), []

        n = len(rows)
        log.info("Loading %d historical situations from DuckDB …", n)

        matrix = np.empty((n, len(SITUATION_FEATURES)), dtype=np.float64)
        meta = []

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
            meta.append(
                NearestSituation(
                    play_id=str(play_id or ""),
                    game_pk=int(game_pk or 0),
                    distance=0.0,  # filled at query time
                    inning=int(inning or 1),
                    outs=int(outs or 0),
                    runners=runners_bitmask,
                    leverage_index=float(li or 1.0),
                    score_differential=score_diff_clipped,
                )
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

        results = []
        for dist, idx in zip(distances, indices, strict=False):
            base = self._index_meta[idx]
            results.append(
                NearestSituation(
                    play_id=base.play_id,
                    game_pk=base.game_pk,
                    distance=float(dist),
                    inning=base.inning,
                    outs=base.outs,
                    runners=base.runners,
                    leverage_index=base.leverage_index,
                    score_differential=base.score_differential,
                )
            )

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

        results = []
        for distances, indices in zip(all_distances, all_indices, strict=False):
            row = []
            for dist, idx in zip(distances, indices, strict=False):
                base = self._index_meta[idx]
                row.append(
                    NearestSituation(
                        play_id=base.play_id,
                        game_pk=base.game_pk,
                        distance=float(dist),
                        inning=base.inning,
                        outs=base.outs,
                        runners=base.runners,
                        leverage_index=base.leverage_index,
                        score_differential=base.score_differential,
                    )
                )
            results.append(row)

        return results

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
        for meta in self._index_meta:
            counts[meta.outs] = counts.get(meta.outs, 0) + 1
        return counts

    def situation_count_by_inning(self) -> dict[int, int]:
        """Returns {inning: count} for the full index."""
        counts: dict[int, int] = {}
        for meta in self._index_meta:
            counts[meta.inning] = counts.get(meta.inning, 0) + 1
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
    for meta in engine._index_meta:
        counter[(meta.inning, meta.outs)] += 1

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
