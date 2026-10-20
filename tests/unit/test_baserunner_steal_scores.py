"""The baserunner steal engine's scores, and the generic similarity diagnostics
(``similarity_diagnostics.run_generic_diagnostics``) run over that engine.

The engine is an RBF kernel (radial basis function: a similarity that falls off with the weighted distance between two profiles). The tests build it in memory through ``__new__``,
so no DuckDB file is needed. ``test_baserunner_steal_engine.py`` holds the
engine's scoring contract.
"""

from __future__ import annotations

import unittest

import numpy as np


class TestBaserunnerStealEngine(unittest.TestCase):
    """The baserunner steal engine's query results and sub-scores."""

    def _make_engine(self, n_runners: int = 40, n_seasons: int = 2) -> object:
        from similarity.engines.baserunner_steal_similarity import (
            RBF_SIGMA_SUCCESS,
            RBF_SIGMA_TENDENCY,
            SUCCESS_FEATURES,
            TENDENCY_FEATURES,
            BaserunnerStealProfile,
            BaserunnerStealSimilarityEngine,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            StealPartition,
            WeightedRBFSimilarity,
        )

        rng = np.random.default_rng(42)
        seasons = list(range(2023, 2023 + n_seasons))
        profiles = []

        for pid in range(1, n_runners + 1):
            base_tend = rng.beta(3, 7, len(TENDENCY_FEATURES))
            base_succ = rng.beta(7, 3, len(SUCCESS_FEATURES))
            for season in seasons:
                n_atts = int(rng.integers(15, 60))
                profiles.append(
                    BaserunnerStealProfile(
                        player_id=pid,
                        season=season,
                        sample_steal_attempts=n_atts,
                        sample_first_base_opps=n_atts * 10,
                        tendency_vec=(
                            base_tend + rng.normal(0, 0.02, len(TENDENCY_FEATURES))
                        ).astype(np.float64),
                        success_vec=np.clip(
                            base_succ + rng.normal(0, 0.02, len(SUCCESS_FEATURES)), 0, 1
                        ).astype(np.float64),
                        eb_alpha=float(n_atts / (n_atts + 20)),
                    )
                )

        engine = BaserunnerStealSimilarityEngine.__new__(BaserunnerStealSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {(p.player_id, p.season): p for p in profiles}
        engine._league_avg = {"tendency": {}, "success": {}}
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = StealPartition()
        engine._tend_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_TENDENCY, np.array([w for _, w in TENDENCY_FEATURES])
        )
        engine._succ_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SUCCESS, np.array([w for _, w in SUCCESS_FEATURES])
        )
        engine._normalizer.fit(profiles)
        engine._partition.build(profiles, engine._normalizer)
        return engine

    def test_query_returns_results(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        self.assertGreater(len(results), 0)

    def test_query_excludes_self(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        keys = [(r.player_id, r.season) for r in results]
        self.assertNotIn((1, 2023), keys)

    def test_scores_bounded_zero_to_one(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_scores_sorted_descending(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_n_respected(self):
        engine = self._make_engine()
        results = engine.query(1, 2023, n=10)
        self.assertLessEqual(len(results), 10)

    def test_query_pair_symmetry(self):
        engine = self._make_engine()
        r_ab = engine.query_pair((1, 2023), (2, 2023))
        r_ba = engine.query_pair((2, 2023), (1, 2023))
        self.assertIsNotNone(r_ab)
        self.assertIsNotNone(r_ba)
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=10)

    def test_cross_season_self_similarity_above_median(self):
        """Same runner in two seasons should score above the population median."""
        engine = self._make_engine(n_runners=40, n_seasons=2)
        all_scores = []
        ids = engine.profile_ids()
        for key in ids[:20]:
            pid, s = key
            results = engine.query(pid, s)
            all_scores.extend(r.score for r in results)

        pop_median = float(np.median(all_scores))

        cross_scores = []
        for pid in range(1, 41):
            if (pid, 2023) in engine._profiles and (pid, 2024) in engine._profiles:
                r = engine.query_pair((pid, 2023), (pid, 2024))
                if r:
                    cross_scores.append(r.score)

        if cross_scores:
            above_pct = np.mean(np.array(cross_scores) > pop_median)
            self.assertGreater(
                above_pct,
                0.60,
                msg="Cross-season self-pairs should mostly beat the population median",
            )

    def test_missing_profile_returns_empty(self):
        engine = self._make_engine()
        results = engine.query(99999, 2023)
        self.assertEqual(results, [])

    def test_sub_scores_all_present(self):
        engine = self._make_engine()
        results = engine.query(1, 2023)
        for r in results:
            self.assertIsNotNone(r.tendency_score)
            self.assertIsNotNone(r.success_score)

    def test_profile_count(self):
        engine = self._make_engine(n_runners=40, n_seasons=2)
        self.assertEqual(engine.profile_count, 80)


class TestRunGenericDiagnostics(unittest.TestCase):
    """``run_generic_diagnostics`` over the baserunner steal engine."""

    def _make_steal_engine(self, n: int = 30) -> object:
        """The steal engine, built by TestBaserunnerStealEngine's builder."""
        return TestBaserunnerStealEngine()._make_engine(n_runners=n, n_seasons=2)

    def test_returns_diagnostic_report(self):
        from similarity.similarity_diagnostics import DiagnosticReport, run_generic_diagnostics

        engine = self._make_steal_engine()
        report = run_generic_diagnostics(
            engine,
            sub_score_names=["tendency_score", "success_score"],
            n_query_samples=10,
            engine_name="BaserunnerSteal",
        )
        self.assertIsInstance(report, DiagnosticReport)

    def test_report_has_composite_distribution(self):
        from similarity.similarity_diagnostics import run_generic_diagnostics

        engine = self._make_steal_engine()
        report = run_generic_diagnostics(
            engine,
            sub_score_names=["tendency_score", "success_score"],
            n_query_samples=10,
        )
        names = [d.name for d in report.distributions]
        self.assertIn("composite", names)

    def test_report_has_all_sub_score_distributions(self):
        from similarity.similarity_diagnostics import run_generic_diagnostics

        engine = self._make_steal_engine()
        sub_scores = ["tendency_score", "success_score"]
        report = run_generic_diagnostics(engine, sub_score_names=sub_scores, n_query_samples=10)
        names = [d.name for d in report.distributions]
        for s in sub_scores:
            self.assertIn(s, names)

    def test_n_profiles_correct(self):
        from similarity.similarity_diagnostics import run_generic_diagnostics

        engine = self._make_steal_engine(n=20)
        report = run_generic_diagnostics(
            engine,
            sub_score_names=["tendency_score", "success_score"],
            n_query_samples=5,
        )
        self.assertEqual(report.n_profiles, 40)  # 20 runners × 2 seasons

    def test_composite_scores_are_finite(self):
        from similarity.similarity_diagnostics import run_generic_diagnostics

        engine = self._make_steal_engine()
        report = run_generic_diagnostics(
            engine,
            sub_score_names=["tendency_score", "success_score"],
            n_query_samples=15,
        )
        comp = next(d for d in report.distributions if d.name == "composite")
        self.assertEqual(comp.n_nan, 0, "No NaN in composite scores")
        self.assertEqual(comp.n_inf, 0, "No Inf in composite scores")

    def test_empty_engine_returns_empty_report(self):
        from similarity.engines.baserunner_steal_similarity import (
            RBF_SIGMA_SUCCESS,
            RBF_SIGMA_TENDENCY,
            SUCCESS_FEATURES,
            TENDENCY_FEATURES,
            BaserunnerStealSimilarityEngine,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            StealPartition,
            WeightedRBFSimilarity,
        )
        from similarity.similarity_diagnostics import run_generic_diagnostics

        engine = BaserunnerStealSimilarityEngine.__new__(BaserunnerStealSimilarityEngine)
        engine._duckdb_path = ""
        engine._profiles = {}
        engine._league_avg = {"tendency": {}, "success": {}}
        engine._normalizer = FeatureNormalizer()
        engine._shrinkage = EmpiricalBayesShrinkage()
        engine._partition = StealPartition()
        engine._tend_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_TENDENCY, np.array([w for _, w in TENDENCY_FEATURES])
        )
        engine._succ_rbf = WeightedRBFSimilarity(
            RBF_SIGMA_SUCCESS, np.array([w for _, w in SUCCESS_FEATURES])
        )

        report = run_generic_diagnostics(
            engine,
            sub_score_names=["tendency_score", "success_score"],
            n_query_samples=10,
        )
        self.assertEqual(report.n_profiles, 0)
