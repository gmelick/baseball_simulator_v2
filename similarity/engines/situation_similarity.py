"""The situation engine: the historical plate appearances nearest a game state.

The profile engines compare players; this one compares GAME STATES. A query
returns the K plate appearances whose state before the first pitch is
nearest the query's. The Similarity Explorer's Situation Finder calls it
(``api/routes/similarity_explorer.py``). The simulator does not: it draws
from the full-pool sampler.

A state is an 11-feature vector:

    [inning, top_or_bottom, outs, runner_on_1b, runner_on_2b,
     runner_on_3b, score_differential, leverage_index,
     pitcher_pitch_count, batter_pa_count, park_factor_runs]

Why a KD-tree
-------------
  A KD-tree (a tree that splits the space one feature at a time) finds the
  exact K nearest rows without a score for every row. The profile engines
  score every profile, because they hold thousands of player-seasons. This
  index holds hundreds of thousands of plate appearances a season, and a
  KD-tree query costs about O(K log N), not O(N). The engine builds the tree
  once at startup and keeps it in memory.

Distance
--------
  The engine z-scores each feature (the value minus the index's mean, over
  its standard deviation), then multiplies it by the square root of its
  weight (``SITUATION_FEATURES``). The Euclidean distance in that space
  weights each feature's squared difference by its weight. The categorical
  features (outs, the runners, the inning) count as continuous: a 2-out state
  is far from a 0-out state, and the distance shows that.

Leverage index
--------------
  The leverage index (LI, how much the state can swing the game) is a
  feature, because it carries what inning, outs and runners alone miss: a
  runner on second in the 8th means more in a 1-run game than in a 7-run
  game. LI puts that in one number.

Columnar metadata
-----------------
  The engine keeps each row's metadata as parallel NumPy column arrays
  (``ColumnarSituationMeta``), one array per field. The arrays are compact,
  and a process can share them read-only. A Python object per row would cost
  about 120 MB at a million rows. A query rebuilds each ``NearestSituation``
  from the arrays.

Usage
-----
  engine = SituationSimilarityEngine(duckdb_path="/data/baseball_sim.duckdb")
  engine.build(seasons=[2019, 2020, 2021, 2022, 2023, 2024])

  # The 50 historical states nearest the current one
  query_state = SituationVector(
      inning=7, top_or_bottom=0, outs=1,
      runner_on_1b=1, runner_on_2b=0, runner_on_3b=0,
      score_differential=-1, leverage_index=2.1,
      pitcher_pitch_count=82, batter_pa_count=3,
      park_factor_runs=1.05,
  )
  nearest = engine.query(query_state, k=50)

  # Each result carries the plate appearance's play_id
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
# Feature definitions and weights
# ============================================================================

# Each feature and its weight. The engine multiplies each z-scored feature by
# sqrt(weight) before the KD-tree indexes it, so the squared Euclidean
# distance weights each feature's squared difference by its weight.
SITUATION_FEATURES = [
    # feature_name,             weight   description
    ("inning", 0.80),  # 1-9, more in extra innings
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
# The factor on each z-scored feature: sqrt(weight).
FEATURE_SCALE = np.sqrt(FEATURE_WEIGHTS)

# The score differential is clipped to [-5, 5], so a blowout does not stretch
# the distance.
SCORE_DIFF_CLIP = 5

# The default K for a query.
DEFAULT_K = 50

# Below this many indexed situations, the build logs a warning.
MIN_INDEX_SIZE = 1000


# ============================================================================
# Data structures
# ============================================================================


@dataclass(frozen=True, slots=True)
class SituationVector:
    """One game state, as a query or an indexed row.

    Each field holds its value before the plate appearance's first pitch,
    before the outcome is known.
    """

    inning: int  # 1-based inning number
    top_or_bottom: int  # 0 = top (away batting), 1 = bottom (home batting)
    outs: int  # 0, 1, or 2
    runner_on_1b: int  # 1 if occupied, 0 otherwise
    runner_on_2b: int
    runner_on_3b: int
    score_differential: float  # home_score - away_score; to_array clips it to ±5
    # The leverage index. The index holds the profile computor's approximation,
    # 0.18 to 3.59; the real index runs 0 to about 10.
    leverage_index: float
    pitcher_pitch_count: int  # the pitcher's pitches this game before the plate appearance
    batter_pa_count: int  # this batter's plate appearance number in the game (1 = his first)
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
    """One of the K nearest historical states a query returns."""

    play_id: str  # the plate appearance's id, '<game_pk>-<at_bat_number>'
    game_pk: int  # game identifier
    distance: float  # Euclidean distance in the z-scored, weighted space
    inning: int
    outs: int
    runners: int  # bitmask: 0b001=1B, 0b010=2B, 0b100=3B
    leverage_index: float
    score_differential: float


# ============================================================================
# Columnar metadata store
# ============================================================================
#
# The store keeps one NumPy array per `NearestSituation` field. The arrays are
# contiguous and compact, and a process can share them read-only
# (`multiprocessing.shared_memory` or mmap). A Python object per row would
# cost about 120 bytes, more than the KD-tree's own data, and could not be
# shared.
#
# Field → array (the store keeps no `distance`: a query fills it in from the
# KD-tree result):
#
#   play_id            -> str   : fixed-width unicode array (np.dtype('U<W>')),
#                                 as wide as the longest id. The ids are nearly
#                                 unique, so an integer code with a lookup table
#                                 saves nothing.
#   game_pk            -> int   : np.int64
#   inning             -> int   : np.int16
#   outs               -> int   : np.int8
#   runners (bitmask)  -> int   : np.int8   (values 0..7)
#   leverage_index     -> float : np.float64
#   score_differential -> float : np.float64


@dataclass(slots=True)
class ColumnarSituationMeta:
    """The per-row situation metadata, as parallel NumPy column arrays.

    ``len()``, integer indexing and iteration each give a ``NearestSituation``
    rebuilt from the arrays, with ``distance=0.0``. A caller can treat the
    store as a sequence of ``NearestSituation``.

    The arrays are write-protected (``flags.writeable = False``) after
    construction. No caller can change the index in place, so the store is
    safe to share read-only across processes.
    """

    play_id: NDArray  # dtype '<U...' fixed-width unicode
    game_pk: NDArray[np.int64]
    inning: NDArray[np.int16]
    outs: NDArray[np.int8]
    runners: NDArray[np.int8]
    leverage_index: NDArray[np.float64]
    score_differential: NDArray[np.float64]

    def __post_init__(self) -> None:
        # Make the store read-only. No consumer may change a shared array; the
        # cleared write flag enforces that, and sharing across processes
        # without a copy needs it.
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
                # A view that does not own its data may refuse; that is fine here.
                pass

    @classmethod
    def empty(cls) -> ColumnarSituationMeta:
        """An empty store (zero rows), for an engine with no loaded situations."""
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
        """Build the store from the per-field Python lists the load collects."""
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
        """Rebuild the NearestSituation at ``idx``; ``distance`` defaults to 0.0."""
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
# Feature normalization (fitted on the index, applied to each query)
# ============================================================================


@dataclass(slots=True)
class SituationNormalizer:
    """The z-score and weight scaling of a situation vector.

    Each feature becomes (value - mean) / std, times sqrt(weight). A NaN
    becomes 0, the index mean. Before a fit, only the weight scaling applies.
    """

    mean: NDArray | None = None
    std: NDArray | None = None

    def fit(self, matrix: NDArray[np.float64]) -> None:
        """Fit the means and standard deviations on the index matrix (N × n_features)."""
        self.mean = np.nanmean(matrix, axis=0)
        self.std = np.nanstd(matrix, axis=0)
        self.std[self.std == 0] = 1.0

    def normalize(self, vec: NDArray[np.float64]) -> NDArray[np.float64]:
        """Z-score and weight one situation vector."""
        if self.mean is None:
            return vec * FEATURE_SCALE
        normed = (vec - self.mean) / self.std
        return np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE

    def normalize_batch(self, matrix: NDArray[np.float64]) -> NDArray[np.float64]:
        """Z-score and weight every row of a situation matrix."""
        if self.mean is None:
            return matrix * FEATURE_SCALE[np.newaxis, :]
        normed = (matrix - self.mean[np.newaxis, :]) / self.std[np.newaxis, :]
        return np.nan_to_num(normed, nan=0.0) * FEATURE_SCALE[np.newaxis, :]


# ============================================================================
# The engine
# ============================================================================


class SituationSimilarityEngine:
    """The situation-to-situation engine: a KD-tree over historical game states.

    It indexes one row per historical plate appearance and finds the K rows
    nearest a query state.

    The engine stores:
      - a KD-tree of the z-scored, weighted situation vectors, for the lookup;
      - a ColumnarSituationMeta of each row's metadata, to build the results.

    Usage:
        engine = SituationSimilarityEngine(duckdb_path="path/to/db.duckdb")
        engine.build(seasons=[2019, 2020, 2021, 2022, 2023, 2024])
        nearest = engine.query(query_state, k=50)
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path
        self._normalizer = SituationNormalizer()
        self._kdtree: KDTree | None = None
        # The row metadata, in the same row order as the KD-tree's data.
        self._index_meta: ColumnarSituationMeta = ColumnarSituationMeta.empty()
        self._index_size = 0

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, seasons: list[int] | None = None) -> None:
        """Load the historical situations from DuckDB and build the KD-tree.

        The source is the table derived.at_bat_situations: one row per PLATE
        APPEARANCE (not per pitch), with the state at its first pitch. The
        load joins each row's run park factor from derived.park_factors.
        Raise when no row loads.
        """
        t0 = time.time()
        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            raw_matrix, meta = self._load_situations(conn, seasons)
        finally:
            conn.close()

        if len(raw_matrix) == 0:
            # A zero-row index is useless. The z-score of an empty matrix is
            # NaN (numpy warns "Mean of empty slice"), so the engine would
            # return NaN distances, which is worse than no engine. Raise, so
            # ``api.state.build_all_engines`` skips this engine and marks it
            # failed, as it does for any engine whose source table is missing.
            # The trigger is a missing or empty ``derived.at_bat_situations``.
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

        # Fit the z-score on the index, then scale every row.
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
        """Load the situations into a feature matrix and the columnar metadata.

        A missing table loads zero rows. A NULL field takes a neutral default,
        such as LI 1.0 or park factor 1.0.
        """
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

        # Collect each field in a Python list, then pack the lists into NumPy
        # arrays once, through ColumnarSituationMeta.from_columns(). The loop
        # then builds no Python object per row.
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
        """Find the K historical situations nearest the query state.

        Parameters
        ----------
        situation : SituationVector
            The current game state, before the plate appearance.
        k : int
            The number of nearest rows to return; at most the index size.

        Returns
        -------
        A list of NearestSituation, nearest first. Raise when the engine is
        not built.
        """
        if self._kdtree is None:
            raise RuntimeError("Engine not built. Call build() first.")

        if self._index_size == 0:
            return []

        effective_k = min(k, self._index_size)

        query_vec = self._normalizer.normalize(situation.to_array())
        distances, indices = self._kdtree.query(query_vec, k=effective_k)

        # The KD-tree returns scalars when k=1; make them arrays.
        if effective_k == 1:
            distances = np.array([distances])
            indices = np.array([indices])

        meta = self._index_meta
        results = []
        for dist, idx in zip(distances, indices, strict=False):
            # Rebuild the NearestSituation at this row with the query's
            # distance.
            results.append(self._row_from_meta(meta, idx, dist))

        return results

    def query_batch(
        self,
        situations: list[SituationVector],
        k: int = DEFAULT_K,
    ) -> list[list[NearestSituation]]:
        """Find the K nearest situations for each query state, in one KD-tree call.

        The results match one ``query`` call per state.
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

        # The KD-tree drops the K axis when k=1; restore it.
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
        """Rebuild the NearestSituation at ``idx`` with the query's distance.

        ``meta`` is the columnar store (``meta.row``) or a plain
        ``list[NearestSituation]``, which the regression and unit tests
        inject. Both give the same result.
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
        """The number of historical situations in the index."""
        return self._index_size

    def is_built(self) -> bool:
        return self._kdtree is not None

    def situation_count_by_outs(self) -> dict[int, int]:
        """The index's row count per outs value: {outs: count}."""
        counts: dict[int, int] = {0: 0, 1: 0, 2: 0}
        # Count on the column array, with no NearestSituation per row.
        outs_arr = self._index_meta.outs
        if len(outs_arr) > 0:
            vals, vcounts = np.unique(outs_arr, return_counts=True)
            for v, c in zip(vals, vcounts, strict=False):
                counts[int(v)] = counts.get(int(v), 0) + int(c)
        return counts

    def situation_count_by_inning(self) -> dict[int, int]:
        """The index's row count per inning, in inning order: {inning: count}."""
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
# Coverage summary by inning and outs
# ============================================================================


def build_coverage_report(engine: SituationSimilarityEngine) -> str:
    """A text table of the index's row count per (inning, outs).

    It shows innings 1 to 12 and skips an inning with no rows. Use it to check
    that the index covers every common game state.
    """
    from collections import Counter

    counter: Counter = Counter()
    meta = engine._index_meta
    # Pair (inning, outs) straight from the column arrays.
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
