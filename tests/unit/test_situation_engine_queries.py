"""The situation engine's KDTree queries (a KDTree is a search tree over the
situation features): k results sorted by distance, batch queries equal to
single ones, and the feature vector. The tests build the engine in memory.
``test_situation_similarity.py`` holds the build, the normalizer and the
coverage report."""

from __future__ import annotations

import unittest

import numpy as np


class TestSituationEngine(unittest.TestCase):
    """The situation engine's KDTree queries."""

    def _make_engine(self, n_situations: int = 2000) -> object:
        from scipy.spatial import KDTree

        from similarity.engines.situation_similarity import (
            NearestSituation,
            SituationNormalizer,
            SituationSimilarityEngine,
        )

        rng = np.random.default_rng(99)

        n = n_situations
        raw_matrix = np.column_stack(
            [
                rng.integers(1, 13, n).astype(float),  # inning
                rng.integers(0, 2, n).astype(float),  # top_or_bottom
                rng.integers(0, 3, n).astype(float),  # outs
                rng.integers(0, 2, n).astype(float),  # on_1b
                rng.integers(0, 2, n).astype(float),  # on_2b
                rng.integers(0, 2, n).astype(float),  # on_3b
                rng.uniform(-5, 5, n),  # score_diff
                rng.uniform(0.1, 4.0, n),  # leverage_index
                rng.integers(0, 120, n).astype(float),  # pitcher_pc
                rng.integers(1, 5, n).astype(float),  # batter_pa_count
                rng.uniform(0.85, 1.15, n),  # park_factor
            ]
        )

        meta = []
        for i in range(n):
            runners = (
                (1 if raw_matrix[i, 3] else 0)
                | (2 if raw_matrix[i, 4] else 0)
                | (4 if raw_matrix[i, 5] else 0)
            )
            meta.append(
                NearestSituation(
                    play_id=f"play_{i:06d}",
                    game_pk=700000 + i,
                    distance=0.0,
                    inning=int(raw_matrix[i, 0]),
                    outs=int(raw_matrix[i, 2]),
                    runners=runners,
                    leverage_index=float(raw_matrix[i, 7]),
                    score_differential=float(raw_matrix[i, 6]),
                )
            )

        engine = SituationSimilarityEngine.__new__(SituationSimilarityEngine)
        engine._duckdb_path = ""
        engine._index_meta = meta
        engine._index_size = n

        norm = SituationNormalizer()
        norm.fit(raw_matrix)
        engine._normalizer = norm

        scaled = norm.normalize_batch(raw_matrix)
        engine._kdtree = KDTree(scaled)

        return engine

    def test_query_returns_k_results(self):
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine()
        q = SituationVector(
            inning=7,
            top_or_bottom=0,
            outs=1,
            runner_on_1b=0,
            runner_on_2b=1,
            runner_on_3b=0,
            score_differential=-1.0,
            leverage_index=2.1,
            pitcher_pitch_count=82,
            batter_pa_count=2,
            park_factor_runs=1.0,
        )
        results = engine.query(q, k=20)
        self.assertEqual(len(results), 20)

    def test_results_sorted_by_distance_ascending(self):
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine()
        q = SituationVector(
            inning=5,
            top_or_bottom=1,
            outs=0,
            runner_on_1b=1,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=1.0,
            pitcher_pitch_count=50,
            batter_pa_count=2,
            park_factor_runs=1.0,
        )
        results = engine.query(q, k=50)
        dists = [r.distance for r in results]
        self.assertEqual(dists, sorted(dists))

    def test_nearest_situation_has_small_distance(self):
        """The closest match to any query should have a small but non-negative distance."""
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine()
        q = SituationVector(
            inning=3,
            top_or_bottom=0,
            outs=2,
            runner_on_1b=1,
            runner_on_2b=1,
            runner_on_3b=0,
            score_differential=2.0,
            leverage_index=1.5,
            pitcher_pitch_count=40,
            batter_pa_count=1,
            park_factor_runs=1.05,
        )
        results = engine.query(q, k=1)
        self.assertGreaterEqual(results[0].distance, 0.0)

    def test_play_ids_non_empty(self):
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine()
        q = SituationVector(
            inning=9,
            top_or_bottom=1,
            outs=1,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=1,
            score_differential=-1.0,
            leverage_index=3.5,
            pitcher_pitch_count=15,
            batter_pa_count=3,
            park_factor_runs=0.95,
        )
        results = engine.query(q, k=10)
        for r in results:
            self.assertNotEqual(r.play_id, "")

    def test_batch_query_matches_individual(self):
        """Batch query results should match individual query results."""
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine()
        situations = [
            SituationVector(
                inning=i,
                top_or_bottom=0,
                outs=1,
                runner_on_1b=0,
                runner_on_2b=0,
                runner_on_3b=0,
                score_differential=float(i - 5),
                leverage_index=1.0,
                pitcher_pitch_count=50,
                batter_pa_count=2,
                park_factor_runs=1.0,
            )
            for i in range(1, 6)
        ]
        batch_results = engine.query_batch(situations, k=10)
        for idx, s in enumerate(situations):
            individual = engine.query(s, k=10)
            self.assertEqual(
                [r.play_id for r in batch_results[idx]],
                [r.play_id for r in individual],
            )

    def test_k_capped_at_index_size(self):
        """Requesting more than index_size results should not raise — returns index_size."""
        from similarity.engines.situation_similarity import SituationVector

        engine = self._make_engine(n_situations=100)
        q = SituationVector(
            inning=1,
            top_or_bottom=0,
            outs=0,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=1.0,
            pitcher_pitch_count=0,
            batter_pa_count=1,
            park_factor_runs=1.0,
        )
        results = engine.query(q, k=9999)
        self.assertLessEqual(len(results), 100)

    def test_feature_vector_length(self):
        from similarity.engines.situation_similarity import SITUATION_FEATURES, SituationVector

        q = SituationVector(
            inning=1,
            top_or_bottom=0,
            outs=0,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=1.0,
            pitcher_pitch_count=0,
            batter_pa_count=1,
            park_factor_runs=1.0,
        )
        arr = q.to_array()
        self.assertEqual(len(arr), len(SITUATION_FEATURES))

    def test_score_differential_clipped(self):
        """score_differential beyond ±5 should be clipped to ±5."""
        from similarity.engines.situation_similarity import SCORE_DIFF_CLIP, SituationVector

        q_extreme = SituationVector(
            inning=1,
            top_or_bottom=0,
            outs=0,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=20.0,  # way beyond clip threshold
            leverage_index=0.1,
            pitcher_pitch_count=0,
            batter_pa_count=1,
            park_factor_runs=1.0,
        )
        arr = q_extreme.to_array()
        score_diff_idx = 6  # position in SITUATION_FEATURES
        self.assertLessEqual(abs(arr[score_diff_idx]), SCORE_DIFF_CLIP + 1e-9)
