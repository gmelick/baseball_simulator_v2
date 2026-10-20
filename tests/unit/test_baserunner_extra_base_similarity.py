"""The baserunner extra-base similarity engine's core scores: range,
self-exclusion, order, symmetry, the three sub-scores and their weights. The
tests build the engine in memory through ``__new__``."""

from __future__ import annotations

import unittest

import numpy as np


class TestBaserunnerExtraBaseEngine(unittest.TestCase):
    """The core scoring properties of ``baserunner_similarity.py``."""

    def _make_engine(self, n_runners: int = 50, n_seasons: int = 2):
        from similarity.engines.baserunner_similarity import (
            AGGRESSION_FEATURES,
            RBF_SIGMA_AGGRESSION,
            RBF_SIGMA_SPEED,
            RBF_SIGMA_SUCCESS,
            SPEED_FEATURES,
            SUCCESS_FEATURES,
            BaserunnerPartition,
            BaserunnerProfile,
            BaserunnerSimilarityEngine,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            WeightedRBFSimilarity,
        )

        rng = np.random.default_rng(55)
        seasons = list(range(2022, 2022 + n_seasons))
        profiles = []

        for pid in range(1, n_runners + 1):
            base_speed = rng.normal(27.5, 1.5)
            speed_factor = (base_speed - 25.0) / 6.0
            base_agg = np.clip(
                0.45 + speed_factor * 0.2 + rng.normal(0, 0.05, len(AGGRESSION_FEATURES)), 0, 1
            )
            base_suc = np.clip(
                0.70 + speed_factor * 0.1 + rng.normal(0, 0.05, len(SUCCESS_FEATURES)), 0, 1
            )

            for season in seasons:
                n_opps = int(rng.integers(25, 120))
                profiles.append(
                    BaserunnerProfile(
                        player_id=pid,
                        season=season,
                        sample_advancement_opps=n_opps,
                        speed_vec=np.array([base_speed + rng.normal(0, 0.3)]),
                        aggression_vec=np.clip(
                            base_agg + rng.normal(0, 0.02, len(AGGRESSION_FEATURES)), 0, 1
                        ).astype(np.float64),
                        success_vec=np.clip(
                            base_suc + rng.normal(0, 0.02, len(SUCCESS_FEATURES)), 0, 1
                        ).astype(np.float64),
                        sample_first_to_third_opps=int(rng.integers(5, 40)),
                        sample_second_to_home_opps=int(rng.integers(3, 25)),
                        sample_first_to_home_opps=int(rng.integers(1, 12)),
                        sample_tag_up_opps=int(rng.integers(1, 8)),
                        eb_alpha=float(n_opps / (n_opps + 15)),
                    )
                )

        engine = BaserunnerSimilarityEngine.__new__(BaserunnerSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {(p.player_id, p.season): p for p in profiles}
        engine._league_avg = {"speed": {}, "aggression": {}, "success": {}}
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = BaserunnerPartition()
        engine._speed_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SPEED, np.array([w for _, w in SPEED_FEATURES])
        )
        engine._agg_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_AGGRESSION, np.array([w for _, w in AGGRESSION_FEATURES])
        )
        engine._success_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SUCCESS, np.array([w for _, w in SUCCESS_FEATURES])
        )
        engine._normalizer.fit(profiles)
        engine._partition.build(profiles, engine._normalizer)
        return engine

    def test_scores_bounded(self):
        engine = self._make_engine()
        for r in engine.query(1, 2022):
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_self_excluded_from_results(self):
        engine = self._make_engine()
        for r in engine.query(1, 2022):
            self.assertNotEqual((r.player_id, r.season), (1, 2022))

    def test_sorted_descending(self):
        engine = self._make_engine()
        scores = [r.score for r in engine.query(1, 2022)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_symmetry(self):
        engine = self._make_engine()
        r_ab = engine.query_pair((1, 2022), (2, 2022))
        r_ba = engine.query_pair((2, 2022), (1, 2022))
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=10)

    def test_three_sub_scores(self):
        engine = self._make_engine()
        r = engine.query(1, 2022)[0]
        self.assertIsNotNone(r.speed_score)
        self.assertIsNotNone(r.aggression_score)
        self.assertIsNotNone(r.success_score)

    def test_weight_sum_equals_one(self):
        from similarity.engines.baserunner_similarity import (
            WEIGHT_AGGRESSION,
            WEIGHT_SPEED,
            WEIGHT_SUCCESS,
        )

        self.assertAlmostEqual(WEIGHT_SPEED + WEIGHT_AGGRESSION + WEIGHT_SUCCESS, 1.0)
