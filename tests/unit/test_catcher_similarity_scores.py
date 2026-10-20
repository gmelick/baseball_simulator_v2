"""The catcher similarity engine's scores: range, symmetry, self-exclusion,
the four defensive sub-scores and the empirical-Bayes prior. The tests build
the engine in memory through ``__new__``. ``test_catcher_engine_throwing.py``
holds the throwing and deterrence split."""

from __future__ import annotations

import unittest

import numpy as np


class TestCatcherEngine(unittest.TestCase):
    """The catcher engine's query results, sub-scores and prior."""

    def _make_engine(self, n_catchers: int = 30, n_seasons: int = 2) -> object:
        from similarity.engines.catcher_similarity import (
            BLOCKING_FEATURES,
            DETERRENCE_FEATURES,
            EB_N_PRIOR,
            FRAMING_FEATURES,
            RBF_SIGMA_BLOCKING,
            RBF_SIGMA_DETERRENCE,
            RBF_SIGMA_FRAMING,
            RBF_SIGMA_THROWING,
            THROWING_FEATURES,
            CatcherPartition,
            CatcherProfile,
            CatcherSimilarityEngine,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            WeightedRBFSimilarity,
        )

        rng = np.random.default_rng(7)
        seasons = list(range(2023, 2023 + n_seasons))
        profiles = []

        for pid in range(1, n_catchers + 1):
            base_fr = rng.normal(0.0, 0.02, len(FRAMING_FEATURES))  # centered near 0
            base_bl = rng.beta(8, 2, len(BLOCKING_FEATURES))
            base_th = rng.normal(0.0, 0.03, len(THROWING_FEATURES))
            base_det = rng.uniform(0.05, 0.12, len(DETERRENCE_FEATURES))

            for season in seasons:
                n_pitches = int(rng.integers(500, 4000))
                profiles.append(
                    CatcherProfile(
                        catcher_id=pid,
                        season=season,
                        sample_pitches_received=n_pitches,
                        sample_block_opps=int(n_pitches * 0.05),
                        sample_steal_attempts_against=int(n_pitches * 0.02),
                        framing_vec=(base_fr + rng.normal(0, 0.005, len(FRAMING_FEATURES))).astype(
                            np.float64
                        ),
                        blocking_vec=np.clip(
                            base_bl + rng.normal(0, 0.02, len(BLOCKING_FEATURES)), 0, 1
                        ).astype(np.float64),
                        throwing_vec=(
                            base_th + rng.normal(0, 0.005, len(THROWING_FEATURES))
                        ).astype(np.float64),
                        deterrence_vec=np.clip(
                            base_det + rng.normal(0, 0.005, len(DETERRENCE_FEATURES)),
                            0.0,
                            1.0,
                        ).astype(np.float64),
                        eb_alpha=float(n_pitches / (n_pitches + EB_N_PRIOR)),
                    )
                )

        engine = CatcherSimilarityEngine.__new__(CatcherSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {(p.catcher_id, p.season): p for p in profiles}
        engine._league_avg = {
            "framing": {},
            "blocking": {},
            "throwing": {},
            "deterrence": {},
        }
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = CatcherPartition()
        engine._framing_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_FRAMING, np.array([w for _, w in FRAMING_FEATURES])
        )
        engine._blocking_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_BLOCKING, np.array([w for _, w in BLOCKING_FEATURES])
        )
        engine._throwing_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_THROWING, np.array([w for _, w in THROWING_FEATURES])
        )
        engine._deterrence_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_DETERRENCE, np.array([w for _, w in DETERRENCE_FEATURES])
        )
        engine._normalizer.fit(profiles)
        engine._partition.build(profiles, engine._normalizer)
        return engine

    def test_scores_in_range(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_four_sub_scores_present(self):
        """The four defensive sub-scores are present (the engine scores no
        offense)."""
        engine = self._make_engine()
        results = engine.query(1, 2023)
        self.assertGreater(len(results), 0)
        r = results[0]
        self.assertIsNotNone(r.framing_score)
        self.assertIsNotNone(r.blocking_score)
        self.assertIsNotNone(r.throwing_score)
        self.assertIsNotNone(r.deterrence_score)

    def test_symmetry(self):
        engine = self._make_engine()
        r_ab = engine.query_pair((1, 2023), (2, 2023))
        r_ba = engine.query_pair((2, 2023), (1, 2023))
        self.assertIsNotNone(r_ab)
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=10)

    def test_self_not_in_results(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        keys = [(r.catcher_id, r.season) for r in results]
        self.assertNotIn((1, 2023), keys)

    def test_eb_n_prior_is_15(self):
        """EB_N_PRIOR must be 15 for catcher/fielder class engines per ML Engineer spec."""
        from similarity.engines.catcher_similarity import EB_N_PRIOR

        self.assertEqual(EB_N_PRIOR, 15)

    def test_high_volume_catcher_has_higher_eb_alpha(self):
        """Catchers with more pitches should have higher confidence (eb_alpha closer to 1.0)."""
        from similarity.engines.catcher_similarity import EmpiricalBayesShrinkage

        shrink = EmpiricalBayesShrinkage()
        low_vol = shrink.alpha(300)
        high_vol = shrink.alpha(5000)
        self.assertGreater(high_vol, low_vol)
