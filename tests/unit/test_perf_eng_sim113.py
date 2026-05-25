"""
test_perf_eng_sim113.py
=======================
Unit tests for SIM-113 — GMM batch pipeline performance.

Covers the three helpers extracted from PlayerProfileComputor's GMM pass:
  * _resolve_gmm_workers  — dynamic pool sizing (replaces hardcoded 8)
  * _chunk_tasks          — round-robin partitioning for chunked submission
  * _fit_gmm_batch        — one worker fits a chunk of pitchers (per-chunk IPC)
  * _flush_gmm_results    — bulk DuckDB writes instead of one per pitcher

Run:
    pytest tests/unit/test_perf_eng_sim113.py -v
"""

from __future__ import annotations

import os
import unittest

import duckdb
import numpy as np

import pipeline.batch.player_profile_computor as ppc


def _two_cluster_array(n: int = 400, seed: int = 7) -> np.ndarray:
    """Two well-separated Gaussian clusters across the GMM feature columns."""
    rng = np.random.default_rng(seed)
    d = len(ppc.GMM_FEATURE_NAMES)
    half = n // 2
    a = rng.normal(3.0, 0.5, size=(half, d))
    b = rng.normal(-3.0, 0.5, size=(n - half, d))
    return np.vstack([a, b]).astype(np.float32)


class TestResolveGmmWorkers(unittest.TestCase):
    def test_floor_one(self):
        self.assertEqual(ppc._resolve_gmm_workers(0), 1)
        self.assertEqual(ppc._resolve_gmm_workers(1), 1)

    def test_never_more_workers_than_tasks(self):
        self.assertLessEqual(ppc._resolve_gmm_workers(2), 2)

    def test_scales_with_cpu_not_hardcoded(self):
        cpu = os.cpu_count() or 2
        self.assertEqual(ppc._resolve_gmm_workers(10_000), max(1, cpu - 1))


class TestChunkTasks(unittest.TestCase):
    def test_preserves_all_tasks(self):
        tasks = list(range(23))
        chunks = ppc._chunk_tasks(tasks, 4)
        self.assertLessEqual(len(chunks), 4)
        self.assertTrue(all(len(c) > 0 for c in chunks))
        self.assertEqual(sorted(t for c in chunks for t in c), tasks)

    def test_single_and_empty(self):
        self.assertEqual(ppc._chunk_tasks([1, 2, 3], 1), [[1, 2, 3]])
        self.assertEqual(ppc._chunk_tasks([], 4), [])


class TestFitGmmBatch(unittest.TestCase):
    def test_batch_delegates_to_per_pitcher(self):
        arr = _two_cluster_array()
        out = ppc._fit_gmm_batch([(arr, 1, 2024)])
        self.assertEqual(len(out), 1)
        pid, ssn, model, comps = out[0]
        self.assertEqual((pid, ssn), (1, 2024))
        self.assertIsNotNone(model)
        # contract of _fit_gmm_for_pitcher: one component row per mixture component
        self.assertEqual(len(comps), model["n_components"])
        self.assertTrue(2 <= model["n_components"] <= 7)

    def test_multiple_pitchers_in_one_chunk(self):
        arr = _two_cluster_array()
        out = ppc._fit_gmm_batch([(arr, 1, 2024), (arr, 2, 2023)])
        self.assertEqual([(o[0], o[1]) for o in out], [(1, 2024), (2, 2023)])

    def test_too_few_pitches_returns_none(self):
        arr = np.zeros((3, len(ppc.GMM_FEATURE_NAMES)), dtype=np.float32)
        out = ppc._fit_gmm_batch([(arr, 9, 2024)])
        self.assertIsNone(out[0][2])


class TestFlushGmmResults(unittest.TestCase):
    def _conn(self):
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA derived")
        conn.execute(
            "CREATE TABLE derived.pitcher_season_metrics ("
            "pitcher_id INTEGER, season INTEGER, gmm_model JSON, "
            "below_minimum_sample BOOLEAN)"
        )
        conn.execute(
            "CREATE TABLE derived.pitcher_gmm_components ("
            "pitcher_id INTEGER, season INTEGER, component_id INTEGER, weight DOUBLE, "
            "PRIMARY KEY (pitcher_id, season, component_id))"
        )
        for pid in (1, 2, 3):
            conn.execute(
                "INSERT INTO derived.pitcher_season_metrics VALUES (?, 2024, NULL, FALSE)",
                [pid],
            )
        return conn

    def test_bulk_update_and_insert(self):
        conn = self._conn()
        fitted = [
            (
                1,
                2024,
                {"n_components": 2},
                [
                    {"pitcher_id": 1, "season": 2024, "component_id": 0, "weight": 0.6},
                    {"pitcher_id": 1, "season": 2024, "component_id": 1, "weight": 0.4},
                ],
            ),
        ]
        fallbacks = [(2, 2024)]
        ppc._flush_gmm_results(conn, fitted, fallbacks)

        model = conn.execute(
            "SELECT gmm_model FROM derived.pitcher_season_metrics WHERE pitcher_id=1"
        ).fetchone()[0]
        self.assertIn("n_components", str(model))

        self.assertTrue(
            conn.execute(
                "SELECT below_minimum_sample FROM derived.pitcher_season_metrics WHERE pitcher_id=2"
            ).fetchone()[0]
        )

        row3 = conn.execute(
            "SELECT gmm_model, below_minimum_sample FROM derived.pitcher_season_metrics WHERE pitcher_id=3"
        ).fetchone()
        self.assertIsNone(row3[0])
        self.assertFalse(row3[1])

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM derived.pitcher_gmm_components").fetchone()[0],
            2,
        )

    def test_empty_inputs_no_error(self):
        conn = self._conn()
        ppc._flush_gmm_results(conn, [], [])  # no-op, must not raise


if __name__ == "__main__":
    unittest.main()
