"""Tests of the catcher engine's throwing and deterrence sub-scores.

The engine (``similarity/engines/catcher_similarity.py``) scores the arm in two
sub-scores:

  - Throwing: the throw itself (pop time, caught-stealing rate, arm strength).
    The result field is ``throwing_score``.
  - Deterrence: one feature, the steal attempts against the catcher per plate
    appearance with a runner on first or second.

Runners rarely test a catcher with an elite arm, so his caught-stealing rate
rests on few attempts; the deterrence sub-score shows his effect. The composite blends four
defensive sub-scores (framing, blocking, throwing, deterrence) at the ratio
45 : 20 : 12 : 8, divided by 0.85 so the weights sum to one.
"""

from __future__ import annotations

import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_with_profiles(profiles):
    """Build a catcher engine through ``__new__`` over ``profiles``, with no
    DuckDB file. The helper skips the shrinkage: the profiles stay raw."""
    from similarity.engines.catcher_similarity import (
        BLOCKING_FEATURES,
        DETERRENCE_FEATURES,
        FRAMING_FEATURES,
        RBF_SIGMA_BLOCKING,
        RBF_SIGMA_DETERRENCE,
        RBF_SIGMA_FRAMING,
        RBF_SIGMA_THROWING,
        THROWING_FEATURES,
        CatcherPartition,
        CatcherSimilarityEngine,
        EmpiricalBayesShrinkage,
        FeatureNormalizer,
        WeightedRBFSimilarity,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCatcherV2DeterrenceSplit(unittest.TestCase):
    """The deterrence sub-score, the throwing split and the composite's weights."""

    def test_deterrence_field_present_on_results(self):
        """Every result carries a deterrence score inside [0, 1]."""
        from similarity.engines.catcher_similarity import (
            BLOCKING_FEATURES,
            DETERRENCE_FEATURES,
            FRAMING_FEATURES,
            THROWING_FEATURES,
            CatcherProfile,
            CatcherSimilarityResult,
        )

        rng = np.random.default_rng(72)
        profiles = []
        for cid in range(1, 6):
            profiles.append(
                CatcherProfile(
                    catcher_id=cid,
                    season=2024,
                    sample_pitches_received=2000,
                    sample_block_opps=80,
                    sample_steal_attempts_against=30,
                    framing_vec=rng.normal(0.0, 0.02, len(FRAMING_FEATURES)),
                    blocking_vec=rng.beta(8, 2, len(BLOCKING_FEATURES)),
                    throwing_vec=rng.normal(2.0, 0.05, len(THROWING_FEATURES)),
                    deterrence_vec=rng.uniform(0.04, 0.12, len(DETERRENCE_FEATURES)),
                    eb_alpha=1.0,
                )
            )

        engine = _make_engine_with_profiles(profiles)
        results = engine.query(1, 2024)
        self.assertGreater(len(results), 0)
        # CatcherSimilarityResult is another name for SimilarityResult.
        self.assertIsInstance(results[0], CatcherSimilarityResult)
        self.assertTrue(hasattr(results[0], "deterrence_score"))
        for r in results:
            self.assertGreaterEqual(r.deterrence_score, 0.0)
            self.assertLessEqual(r.deterrence_score, 1.0 + 1e-9)

    def test_high_deterrence_low_execution_vs_inverse(self):
        """Two catchers who are opposites on every defensive axis score low.

        The "Cannon" has an elite glove and an elite arm, and runners rarely
        try him. The "Welcome Mat" has a poor glove and a poor arm, and runners
        try him often and succeed. The throwing and the deterrence sub-scores
        must each read low: neither one alone may hide the difference.
        """
        from similarity.engines.catcher_similarity import CatcherProfile

        rng = np.random.default_rng(72)

        # "Cannon": every defensive axis at the elite end of the real range.
        # throwing_vec = [pop_time, cs_rate, arm_strength_mph].
        cannon = CatcherProfile(
            catcher_id=1001,
            season=2024,
            sample_pitches_received=3000,
            sample_block_opps=120,
            sample_steal_attempts_against=20,
            framing_vec=np.array([0.12, 18.0, 0.55, 0.92]),
            blocking_vec=np.array([0.96, 0.18, 0.78]),
            throwing_vec=np.array([1.85, 0.45, 88.0]),
            deterrence_vec=np.array([0.03]),
            eb_alpha=1.0,
        )

        # "Welcome Mat": every defensive axis at the replacement-level end.
        welcome_mat = CatcherProfile(
            catcher_id=1002,
            season=2024,
            sample_pitches_received=3000,
            sample_block_opps=120,
            sample_steal_attempts_against=120,
            framing_vec=np.array([-0.04, -8.0, 0.40, 0.78]),
            blocking_vec=np.array([0.84, 0.55, 0.45]),
            throwing_vec=np.array([2.20, 0.12, 78.0]),
            deterrence_vec=np.array([0.17]),
            eb_alpha=1.0,
        )

        # Thirty average catchers across the real range, so the normalizer fits
        # a sensible mean and standard deviation for every feature.
        pool = []
        for cid in range(2000, 2030):
            pool.append(
                CatcherProfile(
                    catcher_id=cid,
                    season=2024,
                    sample_pitches_received=2000,
                    sample_block_opps=100,
                    sample_steal_attempts_against=40,
                    framing_vec=np.array(
                        [
                            rng.uniform(-0.02, 0.10),
                            rng.uniform(-3.0, 12.0),
                            rng.uniform(0.42, 0.52),
                            rng.uniform(0.82, 0.90),
                        ]
                    ),
                    blocking_vec=np.array(
                        [
                            rng.uniform(0.88, 0.94),
                            rng.uniform(0.25, 0.45),
                            rng.uniform(0.55, 0.70),
                        ]
                    ),
                    throwing_vec=np.array(
                        [
                            rng.uniform(1.90, 2.15),
                            rng.uniform(0.20, 0.35),
                            rng.uniform(80.0, 86.0),
                        ]
                    ),
                    deterrence_vec=np.array([rng.uniform(0.06, 0.13)]),
                    eb_alpha=1.0,
                )
            )

        profiles = [cannon, welcome_mat] + pool
        engine = _make_engine_with_profiles(profiles)

        result = engine.query_pair((cannon.catcher_id, 2024), (welcome_mat.catcher_id, 2024))
        self.assertIsNotNone(result)

        diag = (
            f"Cannon vs Welcome Mat scored {result.score:.4f} "
            f"(framing={result.framing_score:.3f}, "
            f"blocking={result.blocking_score:.3f}, "
            f"throwing={result.throwing_score:.3f}, "
            f"deterrence={result.deterrence_score:.3f}). "
            "The throwing/deterrence split should put this pair low."
        )
        self.assertLess(result.score, 0.45, msg=diag)

        # Each of the two arm sub-scores must read low on its own.
        self.assertLess(
            result.throwing_score,
            0.5,
            msg=f"throwing_score={result.throwing_score:.3f} too high",
        )
        self.assertLess(
            result.deterrence_score,
            0.5,
            msg=f"deterrence_score={result.deterrence_score:.3f} too high",
        )

    def test_deterrence_missing_sample_falls_back_gracefully(self):
        """A catcher with no deterrence vector still gets finite scores in [0, 1].

        The computor writes a NULL rate below 100 plate appearances with a
        runner on first or second. With no league row to fill it (this
        helper loads none), the engine places the catcher at the population
        mean, a z-score of 0.
        """
        from similarity.engines.catcher_similarity import (
            BLOCKING_FEATURES,
            DETERRENCE_FEATURES,
            FRAMING_FEATURES,
            THROWING_FEATURES,
            CatcherProfile,
        )

        rng = np.random.default_rng(73)
        profiles = []
        for cid in range(1, 8):
            profiles.append(
                CatcherProfile(
                    catcher_id=cid,
                    season=2024,
                    sample_pitches_received=2000,
                    sample_block_opps=80,
                    sample_steal_attempts_against=40,
                    framing_vec=rng.normal(0.0, 0.02, len(FRAMING_FEATURES)),
                    blocking_vec=rng.beta(8, 2, len(BLOCKING_FEATURES)),
                    throwing_vec=rng.normal(2.0, 0.05, len(THROWING_FEATURES)),
                    deterrence_vec=(
                        None if cid == 1 else rng.uniform(0.04, 0.13, len(DETERRENCE_FEATURES))
                    ),
                    eb_alpha=1.0,
                )
            )

        engine = _make_engine_with_profiles(profiles)
        results = engine.query(1, 2024)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertTrue(np.isfinite(r.score))
            self.assertTrue(np.isfinite(r.deterrence_score))
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)

    def test_defensive_weight_constants(self):
        """The four defensive weights keep the ratio 45 : 20 : 12 : 8 and sum to one."""
        from similarity.engines.catcher_similarity import (
            WEIGHT_BLOCKING,
            WEIGHT_DETERRENCE,
            WEIGHT_FRAMING,
            WEIGHT_THROWING,
        )

        self.assertAlmostEqual(WEIGHT_FRAMING, 0.45 / 0.85, places=9)
        self.assertAlmostEqual(WEIGHT_BLOCKING, 0.20 / 0.85, places=9)
        self.assertAlmostEqual(WEIGHT_THROWING, 0.12 / 0.85, places=9)
        self.assertAlmostEqual(WEIGHT_DETERRENCE, 0.08 / 0.85, places=9)
        total = WEIGHT_FRAMING + WEIGHT_BLOCKING + WEIGHT_THROWING + WEIGHT_DETERRENCE
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_eb_n_prior_unchanged(self):
        """The empirical-Bayes prior strength is 15, the fielder engine's value.

        The engine shrinks every sub-score on the catcher's called pitches.
        """
        from similarity.engines.catcher_similarity import EB_N_PRIOR

        self.assertEqual(EB_N_PRIOR, 15)


if __name__ == "__main__":
    unittest.main()
