"""tests/unit/test_ml_engines_sim042.py — SIM-042
==================================================
Unit tests for the batted-ball FAISS engine.  Engines are constructed
via __new__ + direct injection (no DuckDB).

Includes a SIM-051 readiness test: the engine must transparently fall
back to raw `spray_angle` when `pull_relative_spray_angle` is missing,
without crashing the build.
"""

from __future__ import annotations

import unittest

import duckdb
import numpy as np


def _make_engine(profiles_matrix: np.ndarray, meta_rows: list[dict],
                 *, index_kind: str = "flat",
                 spray_column: str = "spray_angle"):
    from similarity.engines.batted_ball_similarity import (
        BattedBallSimilarityEngine, BattedBallNormalizer,
        NearestBattedBall, FEATURE_DIM,
    )
    import faiss

    engine = BattedBallSimilarityEngine.__new__(BattedBallSimilarityEngine)
    engine._duckdb_path = ""
    engine._index_kind = index_kind
    engine._normalizer = BattedBallNormalizer()
    engine._normalizer.fit(profiles_matrix)
    scaled = engine._normalizer.normalize_batch(profiles_matrix)
    if index_kind == "hnsw":
        idx = faiss.IndexHNSWFlat(FEATURE_DIM, 32)
    else:
        idx = faiss.IndexFlatL2(FEATURE_DIM)
    idx.add(scaled)
    engine._index = idx
    engine._index_meta = [
        NearestBattedBall(
            pitch_id=r["pitch_id"], game_pk=r["game_pk"], season=r["season"],
            batter_id=r["batter_id"], pitcher_id=r["pitcher_id"],
            distance=0.0, bb_type=r["bb_type"], result_hits=r["result_hits"],
        ) for r in meta_rows
    ]
    engine._index_size = len(meta_rows)
    engine._spray_column_used = spray_column
    return engine


def _synthetic_pool(n: int = 500, seed: int = 42):
    rng = np.random.default_rng(seed)
    matrix = np.column_stack([
        rng.uniform(60.0, 115.0, n),    # exit_velo
        rng.uniform(-25.0, 50.0, n),    # launch_angle
        rng.uniform(-45.0, 45.0, n),    # spray_angle
    ]).astype(np.float64)

    meta = []
    bb_types = ["ground_ball", "fly_ball", "line_drive", "popup"]
    for i in range(n):
        # Realistic hit-class distribution by exit velocity bucket.
        ev = matrix[i, 0]
        la = matrix[i, 1]
        if ev > 105 and 22 <= la <= 35:
            hits = 4   # HR
        elif ev > 95 and la > 0:
            hits = np.random.RandomState(seed + i).choice([1, 2, 3], p=[0.6, 0.35, 0.05])
        elif la < 0:
            hits = 0   # ground out
        else:
            hits = int(np.random.RandomState(seed + i).choice([0, 1], p=[0.7, 0.3]))
        meta.append({
            "pitch_id": 2_000_000 + i,
            "game_pk": 800_000 + (i % 100),
            "season": 2022 + (i % 3),
            "batter_id": 660000 + (i % 30),
            "pitcher_id": 605000 + (i % 30),
            "bb_type": bb_types[i % len(bb_types)],
            "result_hits": int(hits),
        })
    return matrix, meta


class TestBattedBallEngineBasic(unittest.TestCase):

    def test_query_returns_k_results_sorted(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.batted_ball_similarity import BattedBallVector

        results = engine.query(
            BattedBallVector(exit_velo=104.0, launch_angle=22.0, spray_angle=8.0),
            k=10,
        )
        self.assertEqual(len(results), 10)
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i].distance, results[i + 1].distance + 1e-9)

    def test_self_query_top_result_is_zero_distance(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.batted_ball_similarity import BattedBallVector

        v = m[42]
        results = engine.query(BattedBallVector(*v.tolist()), k=3)
        self.assertEqual(results[0].pitch_id, meta[42]["pitch_id"])
        self.assertAlmostEqual(results[0].distance, 0.0, places=6)

    def test_outcome_distribution_sums_to_one(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.batted_ball_similarity import BattedBallVector

        dist = engine.outcome_distribution(
            BattedBallVector(exit_velo=104.0, launch_angle=27.0, spray_angle=10.0),
            k=20,
        )
        self.assertTrue(dist)
        self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)
        for k, v in dist.items():
            self.assertIn(k, {0, 1, 2, 3, 4})
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_high_exit_velo_high_la_returns_more_hr_neighbors(self):
        """Sanity check: a barreled ball (high EV, ideal LA) should pull
        its nearest neighbors from the HR-rich part of the pool."""
        m, meta = _synthetic_pool(n=2000)
        engine = _make_engine(m, meta)
        from similarity.engines.batted_ball_similarity import BattedBallVector

        barreled = engine.outcome_distribution(
            BattedBallVector(exit_velo=110.0, launch_angle=27.0, spray_angle=0.0),
            k=30,
        )
        weak = engine.outcome_distribution(
            BattedBallVector(exit_velo=70.0, launch_angle=-10.0, spray_angle=0.0),
            k=30,
        )
        # Barreled-ball distribution should weight HR (4) more heavily
        # than weak-grounder distribution.
        self.assertGreater(
            barreled.get(4, 0.0), weak.get(4, 0.0),
            f"barreled HR rate {barreled.get(4, 0.0)} not > weak HR rate "
            f"{weak.get(4, 0.0)}",
        )

    def test_empty_engine_returns_empty(self):
        from similarity.engines.batted_ball_similarity import (
            BattedBallSimilarityEngine, BattedBallVector,
        )
        engine = BattedBallSimilarityEngine.__new__(BattedBallSimilarityEngine)
        engine._index = None
        engine._index_meta = []
        engine._index_size = 0
        v = BattedBallVector(exit_velo=100.0, launch_angle=20.0, spray_angle=0.0)
        self.assertEqual(engine.query(v, k=5), [])
        self.assertEqual(engine.outcome_distribution(v, k=5), {})

    def test_query_batch_matches_individual(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.batted_ball_similarity import BattedBallVector
        queries = [
            BattedBallVector(*m[0].tolist()),
            BattedBallVector(*m[7].tolist()),
        ]
        batch = engine.query_batch(queries, k=5)
        for i, q in enumerate(queries):
            individual = engine.query(q, k=5)
            self.assertEqual(len(batch[i]), len(individual))
            for r_b, r_i in zip(batch[i], individual):
                self.assertEqual(r_b.pitch_id, r_i.pitch_id)


class TestBattedBallSpray051Readiness(unittest.TestCase):
    """SIM-042's loader must transparently swap in
    `pull_relative_spray_angle` once SIM-051 ships."""

    def _build_outcome_pool(self, conn, *, with_pull_relative: bool):
        conn.execute("CREATE SCHEMA IF NOT EXISTS sim")
        if with_pull_relative:
            conn.execute("""
                CREATE TABLE sim.outcome_pool (
                    pitch_id BIGINT, game_pk INTEGER, season SMALLINT,
                    batter_id INTEGER, pitcher_id INTEGER,
                    exit_velo FLOAT, launch_angle FLOAT,
                    spray_angle FLOAT, pull_relative_spray_angle FLOAT,
                    bb_type VARCHAR, result_hits SMALLINT
                )
            """)
        else:
            conn.execute("""
                CREATE TABLE sim.outcome_pool (
                    pitch_id BIGINT, game_pk INTEGER, season SMALLINT,
                    batter_id INTEGER, pitcher_id INTEGER,
                    exit_velo FLOAT, launch_angle FLOAT,
                    spray_angle FLOAT,
                    bb_type VARCHAR, result_hits SMALLINT
                )
            """)
        rng = np.random.default_rng(51)
        for i in range(1500):
            ev = float(rng.uniform(60, 110))
            la = float(rng.uniform(-25, 45))
            sa = float(rng.uniform(-45, 45))
            row = (
                2_000_000 + i, 800_000 + i, 2024,
                660000 + (i % 30), 605000 + (i % 30),
                ev, la, sa,
            )
            if with_pull_relative:
                # Pull-relative is just `sa` flipped on R-handed batters in
                # the real pipeline, but for this test we just plug in
                # something distinguishable.
                row = row + (sa * 0.95,)  # pull_relative_spray_angle
            row = row + ("line_drive" if la > 0 else "ground_ball",
                         int(rng.integers(0, 5)))
            placeholders = ", ".join(["?"] * len(row))
            conn.execute(f"INSERT INTO sim.outcome_pool VALUES ({placeholders})", row)

    def test_falls_back_to_raw_spray_angle_when_sim051_not_yet_shipped(self):
        from similarity.engines.batted_ball_similarity import (
            BattedBallSimilarityEngine,
        )
        conn = duckdb.connect(":memory:")
        self._build_outcome_pool(conn, with_pull_relative=False)
        chosen = BattedBallSimilarityEngine._select_spray_column(conn)
        self.assertEqual(chosen, "spray_angle")
        conn.close()

    def test_uses_pull_relative_when_sim051_has_shipped(self):
        from similarity.engines.batted_ball_similarity import (
            BattedBallSimilarityEngine,
        )
        conn = duckdb.connect(":memory:")
        self._build_outcome_pool(conn, with_pull_relative=True)
        chosen = BattedBallSimilarityEngine._select_spray_column(conn)
        self.assertEqual(chosen, "pull_relative_spray_angle")
        conn.close()


class TestBattedBallFeatureContract(unittest.TestCase):

    def test_feature_dim_is_3(self):
        from similarity.engines.batted_ball_similarity import (
            FEATURE_DIM, FEATURE_NAMES,
        )
        self.assertEqual(FEATURE_DIM, 3)
        self.assertEqual(FEATURE_NAMES, ["exit_velo", "launch_angle", "spray_angle"])

    def test_default_k_is_50(self):
        from similarity.engines.batted_ball_similarity import DEFAULT_K
        self.assertEqual(DEFAULT_K, 50)


if __name__ == "__main__":
    unittest.main()
