"""tests/unit/test_ml_engines_sim041.py — SIM-041
==================================================
Unit tests for the pitch-to-pitch FAISS engine.  Engines are constructed
via __new__ + direct injection of the FAISS index + metadata so the tests
do not require DuckDB.
"""

from __future__ import annotations

import unittest

import numpy as np


def _make_engine(profiles_matrix: np.ndarray, meta_rows: list[dict], *, index_kind: str = "flat"):
    """Build a populated PitchPitchSimilarityEngine without DuckDB."""
    import faiss

    from similarity.engines.pitch_pitch_similarity import (
        FEATURE_DIM,
        NearestPitch,
        PitchNormalizer,
        PitchPitchSimilarityEngine,
    )

    engine = PitchPitchSimilarityEngine.__new__(PitchPitchSimilarityEngine)
    engine._duckdb_path = ""
    engine._index_kind = index_kind
    engine._normalizer = PitchNormalizer()
    engine._normalizer.fit(profiles_matrix)
    scaled = engine._normalizer.normalize_batch(profiles_matrix)
    if index_kind == "hnsw":
        idx = faiss.IndexHNSWFlat(FEATURE_DIM, 32)
    else:
        idx = faiss.IndexFlatL2(FEATURE_DIM)
    idx.add(scaled)
    engine._index = idx
    engine._index_meta = [
        NearestPitch(
            pitch_id=r["pitch_id"],
            game_pk=r["game_pk"],
            season=r["season"],
            pitcher_id=r["pitcher_id"],
            batter_id=r["batter_id"],
            distance=0.0,
            outcome_type=r["outcome_type"],
        )
        for r in meta_rows
    ]
    engine._index_size = len(meta_rows)
    return engine


def _synthetic_pool(n: int = 500, seed: int = 41):
    """Generate a deterministic synthetic pitch pool covering realistic
    Statcast envelopes for the 10-feature engine."""
    from similarity.engines.pitch_pitch_similarity import FEATURE_DIM

    rng = np.random.default_rng(seed)
    matrix = np.column_stack(
        [
            rng.uniform(78.0, 102.0, n),  # velo
            rng.uniform(-12.0, 22.0, n),  # ivb
            rng.uniform(-22.0, 22.0, n),  # hb
            rng.uniform(1800.0, 2900.0, n),  # spin_rate
            rng.uniform(0.0, 359.0, n),  # spin_axis
            rng.uniform(-3.0, 3.0, n),  # release_x
            rng.uniform(4.5, 7.0, n),  # release_z
            rng.uniform(5.5, 7.5, n),  # release_ext
            rng.uniform(-2.5, 2.5, n),  # plate_x
            rng.uniform(0.5, 4.5, n),  # plate_z
        ]
    ).astype(np.float64)
    assert matrix.shape == (n, FEATURE_DIM)

    meta = []
    outcomes = ["ball", "called_strike", "swinging_strike", "foul", "in_play"]
    for i in range(n):
        meta.append(
            {
                "pitch_id": 1_000_000 + i,
                "game_pk": 700_000 + (i % 100),
                "season": 2022 + (i % 3),
                "pitcher_id": 605000 + (i % 30),
                "batter_id": 660000 + (i % 30),
                "outcome_type": outcomes[i % len(outcomes)],
            }
        )
    return matrix, meta


class TestPitchPitchEngineBasic(unittest.TestCase):
    def test_query_returns_k_results_sorted_ascending(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        q = PitchVector(
            velo=94.0,
            ivb=18.0,
            hb=-3.0,
            spin_rate=2400.0,
            spin_axis=210.0,
            release_x=-1.5,
            release_z=5.9,
            release_ext=6.5,
            plate_x=0.4,
            plate_z=2.8,
        )
        results = engine.query(q, k=10)
        self.assertEqual(len(results), 10)
        # Distances must be non-decreasing
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i].distance, results[i + 1].distance + 1e-9)

    def test_self_query_returns_zero_distance_first(self):
        """An exact-match query (one of the indexed pitches) should return
        distance == 0 as its top result on a Flat index."""
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        # Pick row 17 directly from the matrix
        row = m[17]
        q = PitchVector(*row.tolist())
        results = engine.query(q, k=5)
        self.assertEqual(results[0].pitch_id, meta[17]["pitch_id"])
        self.assertAlmostEqual(results[0].distance, 0.0, places=6)

    def test_k_capped_at_index_size(self):
        m, meta = _synthetic_pool(n=12)
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        q = PitchVector(*m[0].tolist())
        results = engine.query(q, k=9999)
        self.assertEqual(len(results), 12)

    def test_distances_are_finite_and_non_negative(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        q = PitchVector(*m[5].tolist())
        for r in engine.query(q, k=20):
            self.assertTrue(np.isfinite(r.distance))
            self.assertGreaterEqual(r.distance, 0.0)

    def test_outcome_type_present_on_results(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        q = PitchVector(*m[0].tolist())
        valid = {"ball", "called_strike", "swinging_strike", "foul", "in_play"}
        for r in engine.query(q, k=10):
            self.assertIn(r.outcome_type, valid)

    def test_query_batch_matches_individual_query(self):
        m, meta = _synthetic_pool()
        engine = _make_engine(m, meta)
        from similarity.engines.pitch_pitch_similarity import PitchVector

        queries = [PitchVector(*m[0].tolist()), PitchVector(*m[1].tolist())]
        batch = engine.query_batch(queries, k=5)
        for i, q in enumerate(queries):
            individual = engine.query(q, k=5)
            self.assertEqual(len(batch[i]), len(individual))
            for r_b, r_i in zip(batch[i], individual, strict=False):
                self.assertEqual(r_b.pitch_id, r_i.pitch_id)
                self.assertAlmostEqual(r_b.distance, r_i.distance, places=5)

    def test_empty_engine_returns_empty(self):
        from similarity.engines.pitch_pitch_similarity import (
            PitchPitchSimilarityEngine,
            PitchVector,
        )

        engine = PitchPitchSimilarityEngine.__new__(PitchPitchSimilarityEngine)
        engine._index = None
        engine._index_meta = []
        engine._index_size = 0
        q = PitchVector(94, 18, -3, 2400, 210, -1.5, 5.9, 6.5, 0.4, 2.8)
        self.assertEqual(engine.query(q, k=5), [])
        self.assertEqual(engine.query_batch([q], k=5), [[]])


class TestPitchPitchHNSWPath(unittest.TestCase):
    """The HNSW path must produce sensible, sorted neighbors too — even
    if approximate (tolerated drift vs Flat)."""

    def test_hnsw_returns_sorted_results(self):
        m, meta = _synthetic_pool(n=2000)
        engine = _make_engine(m, meta, index_kind="hnsw")
        from similarity.engines.pitch_pitch_similarity import PitchVector

        q = PitchVector(*m[5].tolist())
        results = engine.query(q, k=20)
        self.assertGreater(len(results), 0)
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i].distance, results[i + 1].distance + 1e-9)


class TestPitchPitchFeatureContract(unittest.TestCase):
    """Lock in the engine feature surface — changing any of these is a
    calibration event that requires regenerating fixtures."""

    def test_feature_dim_is_10(self):
        from similarity.engines.pitch_pitch_similarity import (
            FEATURE_DIM,
            FEATURE_NAMES,
        )

        self.assertEqual(FEATURE_DIM, 10)
        self.assertEqual(
            FEATURE_NAMES,
            [
                "velo",
                "ivb",
                "hb",
                "spin_rate",
                "spin_axis",
                "release_x",
                "release_z",
                "release_ext",
                "plate_x",
                "plate_z",
            ],
        )

    def test_default_k_is_50(self):
        from similarity.engines.pitch_pitch_similarity import DEFAULT_K

        self.assertEqual(DEFAULT_K, 50)

    def test_recency_boost_replicates_recent_seasons(self):
        from similarity.engines.pitch_pitch_similarity import (
            FEATURE_DIM,
            RECENCY_BOOST_SEASONS,
            NearestPitch,
            PitchPitchSimilarityEngine,
        )

        engine = PitchPitchSimilarityEngine.__new__(PitchPitchSimilarityEngine)
        engine._duckdb_path = ""
        engine._index_kind = "flat"

        # 30 pitches: 10 from each of 2022, 2023, 2024.
        matrix = np.zeros((30, FEATURE_DIM), dtype=np.float64)
        meta = []
        for i in range(30):
            season = 2022 + (i // 10)
            meta.append(
                NearestPitch(
                    pitch_id=i,
                    game_pk=0,
                    season=season,
                    pitcher_id=0,
                    batter_id=0,
                    distance=0.0,
                    outcome_type="ball",
                )
            )
        boosted_m, boosted_meta = engine._apply_recency_boost(
            matrix, meta, seasons=[2022, 2023, 2024]
        )
        # The recency window is the last RECENCY_BOOST_SEASONS = 2 seasons
        # → 2023 + 2024 → 20 boosted rows.
        self.assertEqual(len(boosted_meta), 30 + 20)
        self.assertEqual(RECENCY_BOOST_SEASONS, 2)


if __name__ == "__main__":
    unittest.main()
