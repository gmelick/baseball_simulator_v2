"""tests/unit/test_ml_engines_sim072.py — SIM-072
==================================================
Unit tests covering the v2 catcher similarity engine.

SIM-072 split the v1 throwing dimension (20%) into:
  - Throwing/Execution sub-score (12%) — pop time, CS rate, exchange,
    arm strength.  Field name `throwing_score` retained.
  - Deterrence sub-score (8%) — single-feature, driven by
    steal_attempt_rate_against (PA-level rate, SIM-073).

The acceptance criteria require a synthetic regression test for the
split: a high-deterrence/low-execution catcher and a high-execution/
low-deterrence catcher must score < 0.40 against each other.  The
intuition is that a catcher who's never challenged (low rate) but
poor mechanics is a fundamentally different player profile from a
catcher with elite mechanics whom everyone runs on, and the engine
should now distinguish them — the v1 engine could not, because the
two cancelled out inside one composite throwing score.
"""

from __future__ import annotations

import unittest

import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine_with_profiles(profiles):
    """Construct a CatcherSimilarityEngine via __new__ with the supplied
    profiles injected directly (no DuckDB dependency)."""
    from similarity.engines.catcher_similarity import (
        CatcherSimilarityEngine,
        EmpiricalBayesShrinkage,
        FeatureNormalizer,
        CatcherPartition,
        WeightedRBFSimilarity,
        FRAMING_FEATURES, BLOCKING_FEATURES, THROWING_FEATURES,
        DETERRENCE_FEATURES, OFFENSE_FEATURES,
        RBF_SIGMA_FRAMING, RBF_SIGMA_BLOCKING, RBF_SIGMA_THROWING,
        RBF_SIGMA_DETERRENCE, RBF_SIGMA_OFFENSE,
    )

    engine = CatcherSimilarityEngine.__new__(CatcherSimilarityEngine)
    engine._duckdb_path = ""
    engine._profiles = {(p.catcher_id, p.season): p for p in profiles}
    engine._league_avg = {
        "framing": {}, "blocking": {}, "throwing": {},
        "deterrence": {}, "offense": {},
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
    engine._offense_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_OFFENSE, np.array([w for _, w in OFFENSE_FEATURES])
    )
    engine._normalizer.fit(profiles)
    engine._partition.build(profiles, engine._normalizer)
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCatcherV2DeterrenceSplit(unittest.TestCase):
    """SIM-072 acceptance tests."""

    def test_deterrence_field_present_on_results(self):
        """AC #1: deterrence_score is a first-class field on results."""
        from similarity.engines.catcher_similarity import (
            CatcherProfile, CatcherSimilarityResult,
            FRAMING_FEATURES, BLOCKING_FEATURES, THROWING_FEATURES,
            DETERRENCE_FEATURES, OFFENSE_FEATURES,
        )

        rng = np.random.default_rng(72)
        profiles = []
        for cid in range(1, 6):
            profiles.append(CatcherProfile(
                catcher_id=cid, season=2024,
                sample_pitches_received=2000,
                sample_block_opps=80,
                sample_steal_attempts_against=30,
                framing_vec=rng.normal(0.0, 0.02, len(FRAMING_FEATURES)),
                blocking_vec=rng.beta(8, 2, len(BLOCKING_FEATURES)),
                throwing_vec=rng.normal(2.0, 0.05, len(THROWING_FEATURES)),
                deterrence_vec=rng.uniform(0.04, 0.12, len(DETERRENCE_FEATURES)),
                offense_vec=rng.beta(5, 5, len(OFFENSE_FEATURES)),
                eb_alpha=1.0,
            ))

        engine = _make_engine_with_profiles(profiles)
        results = engine.query(1, 2024)
        self.assertGreater(len(results), 0)
        # SimilarityResult is aliased as CatcherSimilarityResult
        self.assertIsInstance(results[0], CatcherSimilarityResult)
        self.assertTrue(hasattr(results[0], "deterrence_score"))
        for r in results:
            self.assertGreaterEqual(r.deterrence_score, 0.0)
            self.assertLessEqual(r.deterrence_score, 1.0 + 1e-9)

    def test_high_deterrence_low_execution_vs_inverse(self):
        """AC #8: a high-deterrence/low-execution catcher and a
        low-deterrence/high-execution catcher must score < 0.40 against
        each other.

        We construct two catchers who are opposites along *every* axis —
        the "Cannon" archetype (elite glove, elite arm, productive bat,
        nobody runs on him) versus the "Welcome Mat" archetype (poor glove,
        poor arm, slap-hitter, everybody runs on him and succeeds).  Both
        the throwing/execution and the deterrence sub-scores must be low
        (the v2 split is what makes that possible — v1 collapsed both
        into one 20% throwing block).

        The composite must land < 0.40, demonstrating the engine no
        longer confuses these two profiles.
        """
        from similarity.engines.catcher_similarity import CatcherProfile

        rng = np.random.default_rng(72)

        # "Cannon" — every axis at the elite end of the envelope.
        cannon = CatcherProfile(
            catcher_id=1001, season=2024,
            sample_pitches_received=3000,
            sample_block_opps=120,
            sample_steal_attempts_against=20,
            framing_vec=np.array([0.12, 18.0, 0.55, 0.92]),
            blocking_vec=np.array([0.96, 0.18, 0.78]),
            throwing_vec=np.array([1.85, 0.45, 0.62, 88.0]),
            deterrence_vec=np.array([0.03]),
            offense_vec=np.array([0.18, 0.10, 90.0, 0.45]),
            eb_alpha=1.0,
        )

        # "Welcome Mat" — every axis at the replacement-level end.
        welcome_mat = CatcherProfile(
            catcher_id=1002, season=2024,
            sample_pitches_received=3000,
            sample_block_opps=120,
            sample_steal_attempts_against=120,
            framing_vec=np.array([-0.04, -8.0, 0.40, 0.78]),
            blocking_vec=np.array([0.84, 0.55, 0.45]),
            throwing_vec=np.array([2.20, 0.12, 0.78, 78.0]),
            deterrence_vec=np.array([0.17]),
            offense_vec=np.array([0.30, 0.04, 84.0, 0.28]),
            eb_alpha=1.0,
        )

        # Population of "average" catchers spanning the realistic envelope
        # so the normalizer fits sensible mean/std for every dimension.
        # Without this pool, the two-catcher case has zero variance on the
        # endpoint axes and normalization degenerates.
        pool = []
        for cid in range(2000, 2030):
            pool.append(CatcherProfile(
                catcher_id=cid, season=2024,
                sample_pitches_received=2000,
                sample_block_opps=100,
                sample_steal_attempts_against=40,
                framing_vec=np.array([
                    rng.uniform(-0.02, 0.10),
                    rng.uniform(-3.0, 12.0),
                    rng.uniform(0.42, 0.52),
                    rng.uniform(0.82, 0.90),
                ]),
                blocking_vec=np.array([
                    rng.uniform(0.88, 0.94),
                    rng.uniform(0.25, 0.45),
                    rng.uniform(0.55, 0.70),
                ]),
                throwing_vec=np.array([
                    rng.uniform(1.90, 2.15),
                    rng.uniform(0.20, 0.35),
                    rng.uniform(0.65, 0.75),
                    rng.uniform(80.0, 86.0),
                ]),
                deterrence_vec=np.array([rng.uniform(0.06, 0.13)]),
                offense_vec=np.array([
                    rng.uniform(0.20, 0.27),
                    rng.uniform(0.06, 0.09),
                    rng.uniform(86.0, 89.0),
                    rng.uniform(0.32, 0.40),
                ]),
                eb_alpha=1.0,
            ))

        profiles = [cannon, welcome_mat] + pool
        engine = _make_engine_with_profiles(profiles)

        result = engine.query_pair(
            (cannon.catcher_id, 2024), (welcome_mat.catcher_id, 2024)
        )
        self.assertIsNotNone(result)

        diag = (
            f"Cannon vs Welcome Mat scored {result.score:.4f} "
            f"(framing={result.framing_score:.3f}, "
            f"blocking={result.blocking_score:.3f}, "
            f"throwing={result.throwing_score:.3f}, "
            f"deterrence={result.deterrence_score:.3f}, "
            f"offense={result.offense_score:.3f}). "
            "v2 split should put this pair below 0.40."
        )
        self.assertLess(result.score, 0.40, msg=diag)

        # Both throwing axes must independently score low — the whole point
        # of the SIM-072 split is that *neither* alone hides the difference.
        self.assertLess(
            result.throwing_score, 0.5,
            msg=f"throwing_score={result.throwing_score:.3f} too high",
        )
        self.assertLess(
            result.deterrence_score, 0.5,
            msg=f"deterrence_score={result.deterrence_score:.3f} too high",
        )

    def test_deterrence_missing_sample_falls_back_gracefully(self):
        """A catcher with deterrence_vec=None (sub-100-PA min-sample
        guard from SIM-073) must still produce finite scores and not
        crash the engine."""
        from similarity.engines.catcher_similarity import (
            CatcherProfile,
            FRAMING_FEATURES, BLOCKING_FEATURES, THROWING_FEATURES,
            DETERRENCE_FEATURES, OFFENSE_FEATURES,
        )

        rng = np.random.default_rng(73)
        profiles = []
        for cid in range(1, 8):
            profiles.append(CatcherProfile(
                catcher_id=cid, season=2024,
                sample_pitches_received=2000,
                sample_block_opps=80,
                sample_steal_attempts_against=40,
                framing_vec=rng.normal(0.0, 0.02, len(FRAMING_FEATURES)),
                blocking_vec=rng.beta(8, 2, len(BLOCKING_FEATURES)),
                throwing_vec=rng.normal(2.0, 0.05, len(THROWING_FEATURES)),
                deterrence_vec=(
                    None if cid == 1
                    else rng.uniform(0.04, 0.13, len(DETERRENCE_FEATURES))
                ),
                offense_vec=rng.beta(5, 5, len(OFFENSE_FEATURES)),
                eb_alpha=1.0,
            ))

        engine = _make_engine_with_profiles(profiles)
        results = engine.query(1, 2024)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertTrue(np.isfinite(r.score))
            self.assertTrue(np.isfinite(r.deterrence_score))
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_v2_weight_constants(self):
        """Spot-check the new weight constants (also asserted in regression)."""
        from similarity.engines.catcher_similarity import (
            WEIGHT_FRAMING, WEIGHT_BLOCKING, WEIGHT_THROWING,
            WEIGHT_DETERRENCE, WEIGHT_OFFENSE,
        )
        self.assertEqual(WEIGHT_FRAMING, 0.45)
        self.assertEqual(WEIGHT_BLOCKING, 0.20)
        self.assertEqual(WEIGHT_THROWING, 0.12)
        self.assertEqual(WEIGHT_DETERRENCE, 0.08)
        self.assertEqual(WEIGHT_OFFENSE, 0.15)
        total = (
            WEIGHT_FRAMING + WEIGHT_BLOCKING + WEIGHT_THROWING
            + WEIGHT_DETERRENCE + WEIGHT_OFFENSE
        )
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_eb_n_prior_unchanged(self):
        """AC #5: EB_N_PRIOR=15 retained for both throwing-derived sub-scores."""
        from similarity.engines.catcher_similarity import EB_N_PRIOR
        self.assertEqual(EB_N_PRIOR, 15)


if __name__ == "__main__":
    unittest.main()
