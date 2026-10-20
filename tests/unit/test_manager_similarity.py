"""The manager similarity engine's scores: range, symmetry, the three
sub-scores, the usage weight and the empirical-Bayes prior. The tests build
the engine in memory through ``__new__``."""

from __future__ import annotations

import unittest

import numpy as np


class TestManagerEngine(unittest.TestCase):
    """The manager engine's query results, sub-scores, weights and prior."""

    def _make_engine(self, n_managers: int = 30, n_seasons: int = 3) -> object:
        from similarity.engines.manager_similarity import (
            AGGRESSION_FEATURES,
            PLATOON_FEATURES,
            RBF_SIGMA_AGGRESSION,
            RBF_SIGMA_PLATOON,
            RBF_SIGMA_USAGE,
            USAGE_FEATURES,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            ManagerPartition,
            ManagerProfile,
            ManagerSimilarityEngine,
            WeightedRBFSimilarity,
        )

        rng = np.random.default_rng(17)
        seasons = list(range(2022, 2022 + n_seasons))
        profiles = []

        for mid in range(1, n_managers + 1):
            base_us = rng.normal(0.5, 0.1, len(USAGE_FEATURES))
            base_ag = rng.beta(3, 10, len(AGGRESSION_FEATURES))
            base_pl = rng.beta(5, 5, len(PLATOON_FEATURES))

            for season in seasons:
                n_games = int(rng.integers(60, 162))
                profiles.append(
                    ManagerProfile(
                        manager_id=mid,
                        season=season,
                        sample_games=n_games,
                        sample_starter_decisions=n_games,
                        usage_vec=np.abs(base_us + rng.normal(0, 0.02, len(USAGE_FEATURES))).astype(
                            np.float64
                        ),
                        aggression_vec=np.clip(
                            base_ag + rng.normal(0, 0.01, len(AGGRESSION_FEATURES)), 0, 1
                        ).astype(np.float64),
                        platoon_vec=np.clip(
                            base_pl + rng.normal(0, 0.02, len(PLATOON_FEATURES)), 0, 1
                        ).astype(np.float64),
                        eb_alpha=float(n_games / (n_games + 30)),
                    )
                )

        engine = ManagerSimilarityEngine.__new__(ManagerSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {(p.manager_id, p.season): p for p in profiles}
        engine._league_avg = {"usage": {}, "aggression": {}, "platoon": {}}
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = ManagerPartition()
        engine._usage_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_USAGE, np.array([w for _, w in USAGE_FEATURES])
        )
        engine._agg_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_AGGRESSION, np.array([w for _, w in AGGRESSION_FEATURES])
        )
        engine._plat_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_PLATOON, np.array([w for _, w in PLATOON_FEATURES])
        )
        engine._normalizer.fit(profiles)
        engine._partition.build(profiles, engine._normalizer)
        return engine

    def test_cross_season_same_manager_high_similarity(self):
        """A manager's consecutive seasons should score above the population median."""
        engine = self._make_engine(n_managers=30, n_seasons=3)
        all_scores = []
        for mid in range(1, 6):
            r = engine.query(mid, 2022)
            all_scores.extend(x.score for x in r)
        pop_median = float(np.median(all_scores)) if all_scores else 0.5

        cross = []
        for mid in range(1, 31):
            r = engine.query_pair((mid, 2022), (mid, 2023))
            if r:
                cross.append(r.score)

        if cross:
            above = np.mean(np.array(cross) > pop_median)
            self.assertGreater(above, 0.60)

    def test_scores_in_range(self):
        engine = self._make_engine()
        for r in engine.query(1, 2022):
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_three_sub_scores(self):
        engine = self._make_engine()
        results = engine.query(1, 2022)
        r = results[0]
        self.assertIsNotNone(r.usage_score)
        self.assertIsNotNone(r.aggression_score)
        self.assertIsNotNone(r.platoon_score)

    def test_usage_weight_dominant(self):
        from similarity.engines.manager_similarity import (
            WEIGHT_AGGRESSION,
            WEIGHT_PLATOON,
            WEIGHT_USAGE,
        )

        self.assertGreater(WEIGHT_USAGE, WEIGHT_AGGRESSION)
        self.assertGreater(WEIGHT_USAGE, WEIGHT_PLATOON)

    def test_eb_prior_is_30(self):
        """Manager EB prior should be 30 — larger than player engines due to opportunity gating."""
        from similarity.engines.manager_similarity import EB_N_PRIOR

        self.assertEqual(EB_N_PRIOR, 30)

    def test_symmetry(self):
        engine = self._make_engine()
        r_ab = engine.query_pair((1, 2022), (2, 2022))
        r_ba = engine.query_pair((2, 2022), (1, 2022))
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=10)
