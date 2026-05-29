"""
test_ml_engines_sim066_071.py
==============================
Unit tests for ML Engineer tickets SIM-066 through SIM-071.

Covers:
  SIM-066 — BaserunnerStealSimilarityEngine
  SIM-067 — CatcherSimilarityEngine
  SIM-068 — PitcherStealSimilarityEngine
  SIM-069 — ManagerSimilarityEngine
  SIM-070 — SituationSimilarityEngine (KDTree)
  SIM-071 — similarity_diagnostics.run_generic_diagnostics + BaserunnerEngine (missing tests)

All engines are tested without DuckDB using in-memory profile construction
(via __new__ to bypass constructors — following the established pattern
documented in agent_team.md: "In-memory profile assembly using __new__
to bypass constructors — avoids live DB dependencies in unit tests").

Run with:
    pytest tests/unit/test_ml_engines_sim066_071.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Common helper to build fake numpy vectors
# ---------------------------------------------------------------------------


def _v(*vals) -> np.ndarray:
    return np.array([v or 0.0 for v in vals], dtype=np.float64)


# ===========================================================================
# SIM-066 — BaserunnerStealSimilarityEngine
# ===========================================================================


class TestBaserunnerStealEngine(unittest.TestCase):
    """Tests for Step 2.5 — Baserunner Steal Similarity (RBF)."""

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


# ===========================================================================
# SIM-067 — CatcherSimilarityEngine
# ===========================================================================


class TestCatcherEngine(unittest.TestCase):
    """Tests for Step 2.6 — Catcher Similarity (RBF)."""

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
        """SIM-408: Offense sub-score TRIMmed; 4 defensive sub-scores remain."""
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


# ===========================================================================
# SIM-068 — PitcherStealSimilarityEngine
# ===========================================================================


class TestPitcherStealEngine(unittest.TestCase):
    """Tests for Step 2.7 — Pitcher Steal-Prevention Similarity (RBF)."""

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
        # SIM-408: outcome-only engine (Delivery + Pickoff removed).
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
        """SIM-408: outcome-only. A pitcher who allows steals freely and one who
        shuts them down should be dissimilar on the outcome dimension."""
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

    def test_outcome_is_sole_weight(self):
        """SIM-408: outcome is the only sub-score, weight 1.0."""
        from similarity.engines.pitcher_steal_similarity import WEIGHT_OUTCOME

        self.assertAlmostEqual(WEIGHT_OUTCOME, 1.0, places=9)


# ===========================================================================
# SIM-069 — ManagerSimilarityEngine
# ===========================================================================


class TestManagerEngine(unittest.TestCase):
    """Tests for Step 2.8 — Manager Similarity (RBF)."""

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


# ===========================================================================
# SIM-070 — SituationSimilarityEngine (KDTree)
# ===========================================================================


class TestSituationEngine(unittest.TestCase):
    """Tests for Step 2.9 — Situation KDTree Engine."""

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


# ===========================================================================
# SIM-071 — run_generic_diagnostics + Baserunner engine (missing tests)
# ===========================================================================


class TestRunGenericDiagnostics(unittest.TestCase):
    """Tests for similarity_diagnostics.run_generic_diagnostics (SIM-071)."""

    def _make_steal_engine(self, n: int = 30) -> object:
        """Reuse the steal engine factory from SIM-066 tests."""
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


class TestBaserunnerExtraBaseEngineCoreCoverage(unittest.TestCase):
    """
    SIM-071 gap: baserunner_similarity.py (Step 2.4) had no test file.
    These tests cover the engine's core scoring properties.
    """

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


# ===========================================================================
# WeightedRBFSimilarity shared property tests (applies to all new engines)
# ===========================================================================


class TestWeightedRBFProperties(unittest.TestCase):
    """Fundamental mathematical properties of the shared WeightedRBFSimilarity kernel."""

    def _make_rbf(self, sigma: float = 1.0, n_features: int = 4):
        from similarity.engines.baserunner_steal_similarity import WeightedRBFSimilarity

        weights = np.ones(n_features, dtype=np.float64)
        return WeightedRBFSimilarity(sigma, weights)

    def test_identical_vectors_score_one(self):
        rbf = self._make_rbf()
        x = np.array([0.5, 0.2, 0.8, 0.1])
        self.assertAlmostEqual(rbf.score(x, x.copy()), 1.0, places=10)

    def test_score_in_zero_to_one(self):
        rbf = self._make_rbf()
        rng = np.random.default_rng(0)
        for _ in range(100):
            x = rng.normal(0, 1, 4)
            y = rng.normal(0, 1, 4)
            s = rbf.score(x, y)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0 + 1e-12)

    def test_symmetry(self):
        rbf = self._make_rbf()
        x = np.array([1.0, -0.5, 2.0, 0.0])
        y = np.array([-0.5, 1.5, 0.2, 0.8])
        self.assertAlmostEqual(rbf.score(x, y), rbf.score(y, x), places=12)

    def test_batch_matches_individual(self):
        rbf = self._make_rbf()
        rng = np.random.default_rng(1)
        q = rng.normal(0, 1, 4)
        cands = rng.normal(0, 1, (10, 4))
        batch = rbf.score_batch(q, cands)
        for i in range(10):
            self.assertAlmostEqual(batch[i], rbf.score(q, cands[i]), places=10)

    def test_nan_treated_as_neutral(self):
        """NaN features should be treated as 0 distance (neutral)."""
        rbf = self._make_rbf()
        x = np.array([0.5, np.nan, 0.8, 0.1])
        y = np.array([0.5, 0.5, 0.8, 0.1])  # y has a value where x has NaN
        score_with_nan = rbf.score(x, y)
        # NaN treated as diff=0 → higher score than if the real diff was large
        x_filled = np.array([0.5, 0.5, 0.8, 0.1])  # x with NaN replaced by y's value
        score_no_diff = rbf.score(x_filled, y)
        self.assertAlmostEqual(score_with_nan, score_no_diff, places=10)

    def test_weights_normalized(self):
        """Weights should be normalized to sum to 1.0."""
        from similarity.engines.baserunner_steal_similarity import WeightedRBFSimilarity

        weights = np.array([2.0, 3.0, 1.0, 4.0])
        rbf = WeightedRBFSimilarity(sigma=1.0, reliability_weights=weights)
        self.assertAlmostEqual(rbf.weights.sum(), 1.0, places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
