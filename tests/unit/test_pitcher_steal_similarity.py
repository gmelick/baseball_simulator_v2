"""The pitcher steal-prevention similarity engine's scores and its group
weights. The tests build the engine in memory through ``__new__``."""

from __future__ import annotations

import unittest

import numpy as np


class TestPitcherStealEngine(unittest.TestCase):
    """The pitcher steal-prevention engine's query results and group weights."""

    def _make_engine(self, n_pitchers: int = 40, n_seasons: int = 2) -> object:
        from similarity.engines.pitcher_steal_similarity import (
            OUTCOME_FEATURES,
            RBF_SIGMA_OUTCOME,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            PitcherStealPartition,
            PitcherStealProfile,
            PitcherStealSimilarityEngine,
            WeightedRBFSimilarity,
        )

        rng = np.random.default_rng(13)
        seasons = [2023, 2024]
        profiles = []

        for pid in range(1, n_pitchers + 1):
            throws = "R" if rng.random() > 0.3 else "L"
            base_out = rng.beta(5, 5, len(OUTCOME_FEATURES))

            for season in seasons[:n_seasons]:
                n_br = int(rng.integers(40, 300))
                profiles.append(
                    PitcherStealProfile(
                        pitcher_id=pid,
                        season=season,
                        throws=throws,
                        sample_baserunner_events=n_br,
                        sample_steal_attempts_against=int(n_br * 0.15),
                        outcome_vec=np.clip(
                            base_out + rng.normal(0, 0.02, len(OUTCOME_FEATURES)), 0, 1
                        ).astype(np.float64),
                        eb_alpha=float(n_br / (n_br + 25)),
                    )
                )

        engine = PitcherStealSimilarityEngine.__new__(PitcherStealSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {(p.pitcher_id, p.season): p for p in profiles}
        engine._league_avg = {"outcome": {}}
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = PitcherStealPartition()
        engine._out_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
        )
        engine._normalizer.fit(profiles)
        engine._partition.build(profiles, engine._normalizer)
        return engine

    def test_scores_bounded(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_outcome_sub_score(self):
        # The outcome sub-score is present.
        engine = self._make_engine()
        results = engine.query(1, 2023)
        r = results[0]
        self.assertIsNotNone(r.outcome_score)

    def test_symmetry(self):
        engine = self._make_engine()
        r_ab = engine.query_pair((1, 2023), (2, 2023))
        r_ba = engine.query_pair((2, 2023), (1, 2023))
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=10)

    def test_divergent_outcomes_pitcher_different(self):
        """A pitcher who allows steals freely and one who shuts them down are
        dissimilar on the outcome group."""
        engine = self._make_engine()
        from similarity.engines.pitcher_steal_similarity import (
            FeatureNormalizer,
            PitcherStealPartition,
            PitcherStealProfile,
        )

        free_pid = 990  # runners run wild
        shutdown_pid = 991  # shuts the running game down
        n_feats_out = len(engine._out_rbf.weights)

        engine._profiles[(free_pid, 2023)] = PitcherStealProfile(
            pitcher_id=free_pid,
            season=2023,
            throws="L",
            sample_baserunner_events=200,
            sample_steal_attempts_against=40,
            outcome_vec=np.full(n_feats_out, 0.95),  # high sb-against / attempt-rate
            eb_alpha=0.9,
        )
        engine._profiles[(shutdown_pid, 2023)] = PitcherStealProfile(
            pitcher_id=shutdown_pid,
            season=2023,
            throws="R",
            sample_baserunner_events=200,
            sample_steal_attempts_against=5,
            outcome_vec=np.full(n_feats_out, 0.05),  # low across the board
            eb_alpha=0.9,
        )

        all_p = list(engine._profiles.values())
        norm = FeatureNormalizer()
        norm.fit(all_p)
        engine._normalizer = norm
        part = PitcherStealPartition()
        part.build(all_p, norm)
        engine._partition = part

        r = engine.query_pair((free_pid, 2023), (shutdown_pid, 2023))
        self.assertIsNotNone(r)
        self.assertLess(r.score, 0.7, msg="Opposite steal-prevention outcomes should be dissimilar")

    def test_outcome_and_hold_weights_sum_to_one(self):
        """The outcome group carries 0.35 and the hold group (the lead and the
        jump the pitcher allows) 0.65: each group's share of its year-to-year
        repeat (the lead-distance work, SIM-531, owner decision 2026-09-16)."""
        from similarity.engines.pitcher_steal_similarity import WEIGHT_HOLD, WEIGHT_OUTCOME

        self.assertAlmostEqual(WEIGHT_OUTCOME, 0.35, places=9)
        self.assertAlmostEqual(WEIGHT_HOLD, 0.65, places=9)
        self.assertAlmostEqual(WEIGHT_OUTCOME + WEIGHT_HOLD, 1.0, places=9)
