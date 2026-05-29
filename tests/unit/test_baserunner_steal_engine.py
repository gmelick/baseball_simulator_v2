"""tests/unit/test_baserunner_steal_engine.py — SIM-149
=========================================================
Dedicated unit tests for ``BaserunnerStealSimilarityEngine``
(``similarity/engines/baserunner_steal_similarity.py``).

The baserunner-steal engine was the only similarity engine without its
own dedicated test file (it was only exercised incidentally inside
``test_ml_engines_sim066_071.py``).  This module pins down invariants of
the RBF engine's scoring contract.

All engines are tested without DuckDB using in-memory profile
construction (via ``__new__`` to bypass the DB-dependent constructor),
following the established pattern documented in agent_team.md.

Notes on the engine's real behaviour (learned from the source):
  * Composite/sub-scores are RBF kernels in [0, 1]; this engine never
    emits distance semantics.
  * The composite is multiplied by ``sqrt(min(eb_alpha_a, eb_alpha_b))``
    — an empirical-Bayes confidence shrinkage that pulls low-sample
    candidates down.  A self/identical pair therefore only reaches the
    documented maximum (1.0) when ``eb_alpha == 1.0``.
  * The top-N limit is the keyword ``n`` (not ``k``).

SIM-408: the engine's Jump / First-Step sub-score (reaction_time_ms /
burst_distance_ft / break_angle_deg) was removed — those biomech features
are not published by Statcast, so derived.baserunner_steal_metrics cannot
supply them.  The engine now blends only Tendency + Success, and the
tendency feature vector is 2-dim (steal_attempt_rate, steal_attempt_rate_2b).

Run with:
    pytest tests/unit/test_baserunner_steal_engine.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _v(*vals) -> np.ndarray:
    return np.array([v or 0.0 for v in vals], dtype=np.float64)


def _make_engine(
    n_runners: int = 40,
    n_seasons: int = 2,
    *,
    eb_alpha_full: bool = False,
    seed: int = 42,
) -> object:
    """Construct a BaserunnerStealSimilarityEngine via __new__ with an
    in-memory profile population (no DuckDB).

    Parameters
    ----------
    eb_alpha_full : bool
        When True every profile gets ``eb_alpha == 1.0`` so the EB
        confidence multiplier becomes a no-op (useful for the
        self/identical max-score invariant).
    """
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

    rng = np.random.default_rng(seed)
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
                    tendency_vec=(base_tend + rng.normal(0, 0.02, len(TENDENCY_FEATURES))).astype(
                        np.float64
                    ),
                    success_vec=np.clip(
                        base_succ + rng.normal(0, 0.02, len(SUCCESS_FEATURES)), 0, 1
                    ).astype(np.float64),
                    eb_alpha=1.0 if eb_alpha_full else float(n_atts / (n_atts + 20)),
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


# ===========================================================================
# SIM-149 — scoring-contract invariants
# ===========================================================================


class TestBaserunnerStealEngineInvariants(unittest.TestCase):
    """Scoring-contract invariants for BaserunnerStealSimilarityEngine."""

    # (1) Zero distance to self → composite == documented max (1.0).
    def test_zero_distance_to_self_scores_max(self):
        """A profile compared against an identical copy (and full EB
        confidence) must reach the documented maximum composite of 1.0."""
        from similarity.engines.baserunner_steal_similarity import (
            BaserunnerStealProfile,
        )

        engine = _make_engine(eb_alpha_full=True)

        # Inject two byte-identical profiles with eb_alpha=1.0.
        tend = _v(0.2, 0.1)
        succ = _v(0.78, 0.55)
        for pid in (9001, 9002):
            engine._profiles[(pid, 2023)] = BaserunnerStealProfile(
                player_id=pid,
                season=2023,
                sample_steal_attempts=50,
                sample_first_base_opps=500,
                tendency_vec=tend.copy(),
                success_vec=succ.copy(),
                eb_alpha=1.0,
            )
        # No rebuild needed for query_pair (it normalizes on the fly using
        # the already-fitted normalizer).
        r = engine.query_pair((9001, 2023), (9002, 2023))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.score, 1.0, places=9)
        self.assertAlmostEqual(r.tendency_score, 1.0, places=9)
        self.assertAlmostEqual(r.success_score, 1.0, places=9)

    # (2) Monotonic ordering — a more-similar candidate scores higher.
    def test_monotonic_ordering_more_similar_scores_higher(self):
        from similarity.engines.baserunner_steal_similarity import (
            BaserunnerStealProfile,
            FeatureNormalizer,
            StealPartition,
        )

        engine = _make_engine()

        anchor = BaserunnerStealProfile(
            player_id=8000,
            season=2023,
            sample_steal_attempts=50,
            sample_first_base_opps=500,
            tendency_vec=_v(0.20, 0.10),
            success_vec=_v(0.80, 0.55),
            eb_alpha=1.0,
        )
        near = BaserunnerStealProfile(
            player_id=8001,
            season=2023,
            sample_steal_attempts=50,
            sample_first_base_opps=500,
            tendency_vec=_v(0.21, 0.11),  # tiny perturbation
            success_vec=_v(0.79, 0.56),
            eb_alpha=1.0,
        )
        far = BaserunnerStealProfile(
            player_id=8002,
            season=2023,
            sample_steal_attempts=50,
            sample_first_base_opps=500,
            tendency_vec=_v(0.85, 0.70),  # very different
            success_vec=_v(0.30, 0.20),
            eb_alpha=1.0,
        )
        for p in (anchor, near, far):
            engine._profiles[(p.player_id, p.season)] = p

        # Rebuild the normalizer/partition so the new spread is reflected.
        all_p = list(engine._profiles.values())
        norm = FeatureNormalizer()
        norm.fit(all_p)
        engine._normalizer = norm
        part = StealPartition()
        part.build(all_p, norm)
        engine._partition = part

        s_near = engine.query_pair((8000, 2023), (8001, 2023)).score
        s_far = engine.query_pair((8000, 2023), (8002, 2023)).score
        self.assertGreater(
            s_near,
            s_far,
            msg=f"near={s_near:.4f} should beat far={s_far:.4f}",
        )

    # (3) All scores within [0, 1].
    def test_all_scores_bounded_zero_to_one(self):
        engine = _make_engine()
        results = engine.query(1, 2023)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0 + 1e-9)
            for sub in (r.tendency_score, r.success_score):
                self.assertGreaterEqual(sub, 0.0)
                self.assertLessEqual(sub, 1.0 + 1e-9)

    # (4) Empirical-Bayes shrinkage pulls low-sample candidates down.
    def test_eb_shrinkage_lowers_low_sample_candidate(self):
        """Two candidates identical in feature space but differing in
        sample size: the low-sample one (low eb_alpha) must score lower
        because the composite is scaled by sqrt(min eb_alpha)."""
        from similarity.engines.baserunner_steal_similarity import (
            BaserunnerStealProfile,
            EmpiricalBayesShrinkage,
            FeatureNormalizer,
            StealPartition,
        )

        engine = _make_engine()

        tend = _v(0.25, 0.12)
        succ = _v(0.77, 0.54)

        query = BaserunnerStealProfile(
            player_id=7000,
            season=2023,
            sample_steal_attempts=80,
            sample_first_base_opps=800,
            tendency_vec=tend.copy(),
            success_vec=succ.copy(),
            eb_alpha=1.0,
        )
        shrink = EmpiricalBayesShrinkage()
        high_n, low_n = 100, 5
        high_sample = BaserunnerStealProfile(
            player_id=7001,
            season=2023,
            sample_steal_attempts=high_n,
            sample_first_base_opps=high_n * 10,
            tendency_vec=tend.copy(),
            success_vec=succ.copy(),
            eb_alpha=shrink.alpha(high_n),
        )
        low_sample = BaserunnerStealProfile(
            player_id=7002,
            season=2023,
            sample_steal_attempts=low_n,
            sample_first_base_opps=low_n * 10,
            tendency_vec=tend.copy(),
            success_vec=succ.copy(),
            eb_alpha=shrink.alpha(low_n),
        )
        for p in (query, high_sample, low_sample):
            engine._profiles[(p.player_id, p.season)] = p

        all_p = list(engine._profiles.values())
        norm = FeatureNormalizer()
        norm.fit(all_p)
        engine._normalizer = norm
        part = StealPartition()
        part.build(all_p, norm)
        engine._partition = part

        s_high = engine.query_pair((7000, 2023), (7001, 2023)).score
        s_low = engine.query_pair((7000, 2023), (7002, 2023)).score
        # Same features → identical RBF sub-scores; only EB confidence differs.
        self.assertGreater(
            s_high,
            s_low,
            msg=f"high-sample={s_high:.4f} should beat low-sample={s_low:.4f}",
        )
        # The low-sample candidate's effective weight (eb_alpha) is itself lower.
        self.assertLess(low_sample.eb_alpha, high_sample.eb_alpha)

    # (5) query_pair is symmetric.
    def test_query_pair_symmetric(self):
        engine = _make_engine()
        r_ab = engine.query_pair((1, 2023), (2, 2023))
        r_ba = engine.query_pair((2, 2023), (1, 2023))
        self.assertIsNotNone(r_ab)
        self.assertIsNotNone(r_ba)
        self.assertAlmostEqual(r_ab.score, r_ba.score, places=12)
        self.assertAlmostEqual(r_ab.tendency_score, r_ba.tendency_score, places=12)
        self.assertAlmostEqual(r_ab.success_score, r_ba.success_score, places=12)

    # (6) Below-minimum-sample profiles behave as documented.
    def test_below_minimum_flag_behaves_as_documented(self):
        """`below_minimum` defaults to False and is a plain, settable flag
        on the profile dataclass (the loader sets it from the DB column;
        the engine itself does not filter on it at query time)."""
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealProfile

        engine = _make_engine()
        # Default flag is False on the in-memory profiles.
        sample_profile = engine.get_profile(1, 2023)
        self.assertIsNotNone(sample_profile)
        self.assertFalse(sample_profile.below_minimum)

        # A profile explicitly flagged below_minimum is still constructable
        # and still scoreable (it produces a finite, bounded score) — the
        # flag is metadata, not a query-time crash guard.
        flagged = BaserunnerStealProfile(
            player_id=6000,
            season=2023,
            sample_steal_attempts=3,
            sample_first_base_opps=30,
            tendency_vec=_v(0.10, 0.05),
            success_vec=_v(0.70, 0.45),
            eb_alpha=0.13,
            below_minimum=True,
        )
        engine._profiles[(6000, 2023)] = flagged
        self.assertTrue(engine.get_profile(6000, 2023).below_minimum)
        r = engine.query_pair((1, 2023), (6000, 2023))
        self.assertIsNotNone(r)
        self.assertTrue(np.isfinite(r.score))
        self.assertGreaterEqual(r.score, 0.0)
        self.assertLessEqual(r.score, 1.0 + 1e-9)

    # (7) Feature normalization handles a constant/zero column without NaN.
    def test_normalizer_handles_constant_column_without_nan(self):
        """A feature column with zero variance across the population must
        not produce NaN scores (std==0 is replaced by 1.0 in fit)."""
        from similarity.engines.baserunner_steal_similarity import (
            SUCCESS_FEATURES,
            TENDENCY_FEATURES,
            BaserunnerStealProfile,
            FeatureNormalizer,
        )

        rng = np.random.default_rng(11)
        profiles = []
        for pid in range(1, 26):
            tend = rng.beta(3, 7, len(TENDENCY_FEATURES))
            tend[0] = 0.5  # constant first tendency feature across all
            succ = rng.beta(7, 3, len(SUCCESS_FEATURES))
            profiles.append(
                BaserunnerStealProfile(
                    player_id=pid,
                    season=2023,
                    sample_steal_attempts=40,
                    sample_first_base_opps=400,
                    tendency_vec=tend.astype(np.float64),
                    success_vec=succ.astype(np.float64),
                    eb_alpha=1.0,
                )
            )

        norm = FeatureNormalizer()
        norm.fit(profiles)
        # std of the constant column must have been coerced to 1.0 (no /0).
        self.assertTrue(np.all(norm.tendency_std != 0))
        for p in profiles:
            nt = norm.normalize_tendency(p.tendency_vec)
            ns = norm.normalize_success(p.success_vec)
            self.assertFalse(np.any(np.isnan(nt)))
            self.assertFalse(np.any(np.isnan(ns)))

    # (8) query(n=...) respects the limit and returns sorted-descending scores.
    def test_query_n_limit_and_descending_sort(self):
        engine = _make_engine(n_runners=40, n_seasons=2)
        results = engine.query(1, 2023, n=10)
        self.assertLessEqual(len(results), 10)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

        # The query target itself must never appear in its own results.
        keys = [(r.player_id, r.season) for r in results]
        self.assertNotIn((1, 2023), keys)

    # (9) Partition behaviour — candidates scored within the expected set.
    def test_partition_scores_full_population_minus_self(self):
        """StealPartition.score_all must score every profile in the index
        except the query key itself.  With N runners × S seasons profiles,
        an unlimited query returns exactly (N*S - 1) results."""
        n_runners, n_seasons = 20, 2
        engine = _make_engine(n_runners=n_runners, n_seasons=n_seasons)
        self.assertEqual(engine.profile_count, n_runners * n_seasons)

        results = engine.query(1, 2023)  # n=None → all
        self.assertEqual(len(results), n_runners * n_seasons - 1)

        # The candidate keys are exactly the population minus the query key.
        result_keys = {(r.player_id, r.season) for r in results}
        all_keys = set(engine.profile_ids())
        self.assertEqual(result_keys, all_keys - {(1, 2023)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
