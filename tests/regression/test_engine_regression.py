"""
tests/regression/test_engine_regression.py — SIM-147
======================================================
Similarity engine regression gate.

PURPOSE
-------
Detect unintended drift in engine scoring between commits.  These tests do NOT
check whether the scores are good (that's unit test territory) — they check
that the scores haven't CHANGED.

There are two complementary test layers:

1. Mathematical property tests (always correct regardless of weights/data)
   -----------------------------------------------------------------------
   Run on all engines every CI push.  No golden files needed.  Verify:
     - Identical profiles score 1.0 (or max possible given EB discount)
     - Scores are bounded [0, 1]
     - Scoring is symmetric:  score(A→B) == score(B→A)
     - Sorted order is monotone:  results[i].score >= results[i+1].score
     - All sub-scores are finite (no NaN / Inf)

2. Golden-file snapshot tests (detect exact numeric drift)
   ---------------------------------------------------------------
   Run on all engines every CI push.  Golden files live at
   tests/regression/fixtures/<engine>.json.
   Regenerate with:  python tests/regression/generate_fixtures.py

   A golden-file test fails if any of these change:
     - Top-5 result keys for any fixture query
     - Composite score for any fixture query (within SCORE_ABS_TOLERANCE)

FIXTURE ENGINES
---------------
All engines are constructed via __new__ + direct profile injection (no DuckDB)
using deterministic synthetic data (seed=2026).  See conftest.py for details.

Profile key conventions:
  BaserunnerSteal  → (player_id, season)
  Catcher          → (catcher_id, season)
  PitcherSteal     → (pitcher_id, season)
  Manager          → (manager_id, season)
  Situation        → SituationVector dataclass (not dict-keyed)
"""

from __future__ import annotations

import json
import math

import pytest

from tests.regression.regression_config import (
    FIXTURES_DIR,
    SCORE_ABS_TOLERANCE,
    SYMMETRY_TOLERANCE,
    TOP_K_STABLE,
)

pytestmark = pytest.mark.regression


# ============================================================================
# Helpers
# ============================================================================


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(
            f"Golden file not found: {path}. "
            "Run `python tests/regression/generate_fixtures.py` to generate it."
        )
    with path.open() as f:
        return json.load(f)


def _assert_scores_finite(results, engine_name: str) -> None:
    for r in results:
        assert math.isfinite(r.score), f"{engine_name}: NaN/Inf composite score for result {r}"


def _assert_monotone(results, engine_name: str) -> None:
    for i in range(len(results) - 1):
        assert results[i].score >= results[i + 1].score - 1e-12, (
            f"{engine_name}: result[{i}].score={results[i].score:.6f} < "
            f"result[{i + 1}].score={results[i + 1].score:.6f} — not sorted"
        )


# ============================================================================
# Property Tests — BaserunnerStealSimilarityEngine
# ============================================================================


class TestStealEngineProperties:
    """Mathematical invariants for the steal engine."""

    def test_scores_bounded(self, steal_engine):
        ids = steal_engine.profile_ids()
        pid, season = ids[0]
        results = steal_engine.query(pid, season)
        for r in results:
            assert 0.0 <= r.score <= 1.0, f"score {r.score} out of [0, 1]"

    def test_results_sorted_descending(self, steal_engine):
        ids = steal_engine.profile_ids()
        pid, season = ids[2]
        results = steal_engine.query(pid, season)
        assert len(results) > 0
        _assert_monotone(results, "steal")

    def test_self_excluded(self, steal_engine):
        ids = steal_engine.profile_ids()
        pid, season = ids[0]
        results = steal_engine.query(pid, season)
        result_keys = {(r.player_id, r.season) for r in results}
        assert (pid, season) not in result_keys, "self should be excluded from results"

    def test_symmetry(self, steal_engine):
        ids = steal_engine.profile_ids()
        a, b = ids[0], ids[3]
        ab = steal_engine.query_pair(a, b)
        ba = steal_engine.query_pair(b, a)
        assert ab is not None and ba is not None
        assert (
            abs(ab.score - ba.score) <= SYMMETRY_TOLERANCE
        ), f"Asymmetry {abs(ab.score - ba.score):.2e} > {SYMMETRY_TOLERANCE:.2e}"

    def test_no_nan_inf(self, steal_engine):
        ids = steal_engine.profile_ids()
        pid, season = ids[1]
        results = steal_engine.query(pid, season)
        _assert_scores_finite(results, "steal")

    def test_sub_scores_present_and_bounded(self, steal_engine):
        ids = steal_engine.profile_ids()
        pid, season = ids[0]
        results = steal_engine.query(pid, season)
        assert len(results) > 0
        r = results[0]
        for attr in ("tendency_score", "success_score"):
            val = getattr(r, attr)
            assert math.isfinite(val), f"{attr} is not finite: {val}"
            assert 0.0 <= val <= 1.0, f"{attr}={val} out of [0, 1]"

    def test_identical_profiles_score_near_one(self, steal_engine):
        """Duplicate a profile — query it vs itself should return score ≈ 1.0."""
        from similarity.engines.baserunner_steal_similarity import BaserunnerStealProfile

        ids = steal_engine.profile_ids()
        orig_key = ids[0]
        orig = steal_engine._profiles[orig_key]

        dup_key = (orig.player_id + 9000, orig.season)
        dup = BaserunnerStealProfile(
            player_id=dup_key[0],
            season=dup_key[1],
            sample_steal_attempts=orig.sample_steal_attempts,
            sample_first_base_opps=orig.sample_first_base_opps,
            tendency_vec=orig.tendency_vec.copy(),
            success_vec=orig.success_vec.copy(),
            eb_alpha=1.0,
        )
        steal_engine._profiles[dup_key] = dup

        # Rebuild partition to include the duplicate
        all_profiles = list(steal_engine._profiles.values())
        steal_engine._normalizer.fit(all_profiles)
        steal_engine._partition.build(all_profiles, steal_engine._normalizer)

        result = steal_engine.query_pair(orig_key, dup_key)
        assert result is not None
        # With eb_alpha=1.0 and identical features, RBF(x, x) = 1.0
        assert (
            abs(result.score - 1.0) < 1e-9
        ), f"Identical profiles scored {result.score:.9f}, expected ≈ 1.0"

        # Clean up — remove dup so other tests aren't affected
        del steal_engine._profiles[dup_key]
        all_profiles = list(steal_engine._profiles.values())
        steal_engine._normalizer.fit(all_profiles)
        steal_engine._partition.build(all_profiles, steal_engine._normalizer)


# ============================================================================
# Property Tests — CatcherSimilarityEngine
# ============================================================================


class TestCatcherEngineProperties:
    def test_scores_bounded(self, catcher_engine):
        ids = catcher_engine.profile_ids()
        cid, season = ids[0]
        results = catcher_engine.query(cid, season)
        for r in results:
            assert 0.0 <= r.score <= 1.0

    def test_results_sorted_descending(self, catcher_engine):
        ids = catcher_engine.profile_ids()
        cid, season = ids[1]
        _assert_monotone(catcher_engine.query(cid, season), "catcher")

    def test_self_excluded(self, catcher_engine):
        ids = catcher_engine.profile_ids()
        cid, season = ids[0]
        result_keys = {(r.catcher_id, r.season) for r in catcher_engine.query(cid, season)}
        assert (cid, season) not in result_keys

    def test_symmetry(self, catcher_engine):
        ids = catcher_engine.profile_ids()
        a, b = ids[0], ids[4]
        ab = catcher_engine.query_pair(a, b)
        ba = catcher_engine.query_pair(b, a)
        assert ab is not None and ba is not None
        assert abs(ab.score - ba.score) <= SYMMETRY_TOLERANCE

    def test_four_sub_scores_present(self, catcher_engine):
        """SIM-408: catcher composite is a 4-sub-score defensive blend
        (Offense TRIMmed). Framing + Blocking + Throwing/Execution + Deterrence,
        renormalized over 0.85.
        """
        ids = catcher_engine.profile_ids()
        cid, season = ids[0]
        results = catcher_engine.query(cid, season)
        r = results[0]
        for attr in (
            "framing_score",
            "blocking_score",
            "throwing_score",
            "deterrence_score",
        ):
            val = getattr(r, attr)
            assert math.isfinite(val)
            assert 0.0 <= val <= 1.0, f"{attr}={val}"

    def test_no_nan_inf(self, catcher_engine):
        ids = catcher_engine.profile_ids()
        cid, season = ids[2]
        _assert_scores_finite(catcher_engine.query(cid, season), "catcher")


# ============================================================================
# Property Tests — PitcherStealSimilarityEngine
# ============================================================================


class TestPitcherStealEngineProperties:
    def test_scores_bounded(self, pitcher_steal_engine):
        ids = pitcher_steal_engine.profile_ids()
        pid, season = ids[0]
        for r in pitcher_steal_engine.query(pid, season):
            assert 0.0 <= r.score <= 1.0

    def test_results_sorted_descending(self, pitcher_steal_engine):
        ids = pitcher_steal_engine.profile_ids()
        pid, season = ids[2]
        _assert_monotone(pitcher_steal_engine.query(pid, season), "pitcher_steal")

    def test_self_excluded(self, pitcher_steal_engine):
        ids = pitcher_steal_engine.profile_ids()
        pid, season = ids[0]
        result_keys = {(r.pitcher_id, r.season) for r in pitcher_steal_engine.query(pid, season)}
        assert (pid, season) not in result_keys

    def test_symmetry(self, pitcher_steal_engine):
        ids = pitcher_steal_engine.profile_ids()
        a, b = ids[0], ids[5]
        ab = pitcher_steal_engine.query_pair(a, b)
        ba = pitcher_steal_engine.query_pair(b, a)
        assert ab is not None and ba is not None
        assert abs(ab.score - ba.score) <= SYMMETRY_TOLERANCE

    def test_outcome_sub_score_present(self, pitcher_steal_engine):
        # SIM-408: the Delivery + Pickoff sub-scores were removed (Statcast
        # publishes neither); the engine is now outcome-only.
        ids = pitcher_steal_engine.profile_ids()
        pid, season = ids[0]
        results = pitcher_steal_engine.query(pid, season)
        r = results[0]
        val = r.outcome_score
        assert math.isfinite(val)
        assert 0.0 <= val <= 1.0


# ============================================================================
# Property Tests — ManagerSimilarityEngine
# ============================================================================


class TestManagerEngineProperties:
    def test_scores_bounded(self, manager_engine):
        ids = manager_engine.profile_ids()
        mid, season = ids[0]
        for r in manager_engine.query(mid, season):
            assert 0.0 <= r.score <= 1.0

    def test_results_sorted_descending(self, manager_engine):
        ids = manager_engine.profile_ids()
        mid, season = ids[3]
        _assert_monotone(manager_engine.query(mid, season), "manager")

    def test_self_excluded(self, manager_engine):
        ids = manager_engine.profile_ids()
        mid, season = ids[0]
        result_keys = {(r.manager_id, r.season) for r in manager_engine.query(mid, season)}
        assert (mid, season) not in result_keys

    def test_symmetry(self, manager_engine):
        ids = manager_engine.profile_ids()
        a, b = ids[0], ids[6]
        ab = manager_engine.query_pair(a, b)
        ba = manager_engine.query_pair(b, a)
        assert ab is not None and ba is not None
        assert abs(ab.score - ba.score) <= SYMMETRY_TOLERANCE

    def test_three_sub_scores_present(self, manager_engine):
        ids = manager_engine.profile_ids()
        mid, season = ids[0]
        results = manager_engine.query(mid, season)
        r = results[0]
        for attr in ("usage_score", "aggression_score", "platoon_score"):
            val = getattr(r, attr)
            assert math.isfinite(val)
            assert 0.0 <= val <= 1.0

    def test_eb_prior_is_30(self):
        from similarity.engines.manager_similarity import EB_N_PRIOR

        assert EB_N_PRIOR == 30, (
            f"EB_N_PRIOR changed from 30 to {EB_N_PRIOR}. "
            "This is a fundamental calibration parameter — update golden files if intentional."
        )

    def test_no_nan_inf(self, manager_engine):
        ids = manager_engine.profile_ids()
        mid, season = ids[1]
        _assert_scores_finite(manager_engine.query(mid, season), "manager")


# ============================================================================
# Property Tests — SituationSimilarityEngine
# ============================================================================


class TestSituationEngineProperties:
    """KDTree-based engine has different properties than RBF engines."""

    def _make_query(self):
        from similarity.engines.situation_similarity import SituationVector

        return SituationVector(
            inning=7,
            top_or_bottom=0,
            outs=1,
            runner_on_1b=0,
            runner_on_2b=1,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=2.1,
            pitcher_pitch_count=55,
            batter_pa_count=2,
            park_factor_runs=1.02,
        )

    def test_returns_k_results(self, situation_engine):
        q = self._make_query()
        results = situation_engine.query(q, k=10)
        assert len(results) == 10

    def test_sorted_ascending_by_distance(self, situation_engine):
        q = self._make_query()
        results = situation_engine.query(q, k=15)
        for i in range(len(results) - 1):
            assert results[i].distance <= results[i + 1].distance + 1e-12, (
                f"Not sorted: [{i}].distance={results[i].distance:.6f} > "
                f"[{i + 1}].distance={results[i + 1].distance:.6f}"
            )

    def test_k_capped_at_index_size(self, situation_engine):
        q = self._make_query()
        results = situation_engine.query(q, k=9999)
        assert len(results) == situation_engine._index_size

    def test_distances_non_negative(self, situation_engine):
        q = self._make_query()
        for r in situation_engine.query(q, k=20):
            assert r.distance >= 0.0, f"Negative distance: {r.distance}"

    def test_play_ids_present(self, situation_engine):
        q = self._make_query()
        results = situation_engine.query(q, k=5)
        for r in results:
            assert r.play_id is not None

    def test_score_diff_clipped(self, situation_engine):
        """Extreme score differentials should not produce infinitely large distances."""
        from similarity.engines.situation_similarity import SituationVector

        extreme_q = SituationVector(
            inning=9,
            top_or_bottom=1,
            outs=2,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=99.0,  # way above SCORE_DIFF_CLIP
            leverage_index=0.1,
            pitcher_pitch_count=90,
            batter_pa_count=4,
            park_factor_runs=1.00,
        )
        results = situation_engine.query(extreme_q, k=5)
        assert len(results) > 0
        # The clipped query should return sensible finite distances
        for r in results:
            assert math.isfinite(r.distance)

    def test_feature_vector_length(self):
        sv = self._make_query()
        arr = sv.to_array()
        assert len(arr) == 11, f"SituationVector.to_array() has {len(arr)} elements, expected 11"

    def test_batch_equals_individual(self, situation_engine):
        from similarity.engines.situation_similarity import SituationVector

        queries = [
            self._make_query(),
            SituationVector(
                inning=3,
                top_or_bottom=1,
                outs=0,
                runner_on_1b=1,
                runner_on_2b=0,
                runner_on_3b=0,
                score_differential=-1.0,
                leverage_index=0.8,
                pitcher_pitch_count=30,
                batter_pa_count=1,
                park_factor_runs=0.95,
            ),
        ]
        k = 5
        batch_results = situation_engine.query_batch(queries, k=k)
        for i, q in enumerate(queries):
            individual = situation_engine.query(q, k=k)
            batch = batch_results[i]
            assert len(individual) == len(batch)
            for r_ind, r_bat in zip(individual, batch, strict=False):
                assert abs(r_ind.distance - r_bat.distance) < 1e-10, (
                    f"Batch/individual distance mismatch at query {i}: "
                    f"{r_ind.distance} vs {r_bat.distance}"
                )


# ============================================================================
# Golden-file Snapshot Tests
# ============================================================================


class TestStealEngineGoldenFile:
    """Detect numeric drift in steal engine outputs vs committed snapshot."""

    def test_top_k_keys_stable(self, steal_engine):
        fixture = _load_fixture("baserunner_steal")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = steal_engine.query(qk[0], qk[1])
            actual_keys = [(r.player_id, r.season) for r in results[:TOP_K_STABLE]]
            expected_keys = [tuple(k) for k in q["top5_keys"]]
            assert actual_keys == expected_keys, (
                f"Top-{TOP_K_STABLE} drift for query {qk}: "
                f"got {actual_keys}, expected {expected_keys}"
            )

    def test_composite_scores_stable(self, steal_engine):
        fixture = _load_fixture("baserunner_steal")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = steal_engine.query(qk[0], qk[1])
            for i, (actual, expected) in enumerate(
                zip(results[:TOP_K_STABLE], q["top5_scores"], strict=False)
            ):
                assert abs(actual.score - expected) <= SCORE_ABS_TOLERANCE, (
                    f"Score drift at query {qk} result[{i}]: "
                    f"got {actual.score:.12f}, expected {expected:.12f}, "
                    f"delta={abs(actual.score - expected):.2e}"
                )


class TestCatcherEngineGoldenFile:
    def test_top_k_keys_stable(self, catcher_engine):
        fixture = _load_fixture("catcher")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = catcher_engine.query(qk[0], qk[1])
            actual_keys = [(r.catcher_id, r.season) for r in results[:TOP_K_STABLE]]
            expected_keys = [tuple(k) for k in q["top5_keys"]]
            assert actual_keys == expected_keys, f"Top-{TOP_K_STABLE} drift for query {qk}"

    def test_composite_scores_stable(self, catcher_engine):
        fixture = _load_fixture("catcher")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = catcher_engine.query(qk[0], qk[1])
            for i, (actual, expected) in enumerate(
                zip(results[:TOP_K_STABLE], q["top5_scores"], strict=False)
            ):
                assert abs(actual.score - expected) <= SCORE_ABS_TOLERANCE, (
                    f"Score drift at query {qk} result[{i}]: "
                    f"delta={abs(actual.score - expected):.2e}"
                )


class TestPitcherStealEngineGoldenFile:
    def test_top_k_keys_stable(self, pitcher_steal_engine):
        fixture = _load_fixture("pitcher_steal")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = pitcher_steal_engine.query(qk[0], qk[1])
            actual_keys = [(r.pitcher_id, r.season) for r in results[:TOP_K_STABLE]]
            expected_keys = [tuple(k) for k in q["top5_keys"]]
            assert actual_keys == expected_keys

    def test_composite_scores_stable(self, pitcher_steal_engine):
        fixture = _load_fixture("pitcher_steal")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = pitcher_steal_engine.query(qk[0], qk[1])
            for _i, (actual, expected) in enumerate(
                zip(results[:TOP_K_STABLE], q["top5_scores"], strict=False)
            ):
                assert (
                    abs(actual.score - expected) <= SCORE_ABS_TOLERANCE
                ), f"Score drift delta={abs(actual.score - expected):.2e}"


class TestManagerEngineGoldenFile:
    def test_top_k_keys_stable(self, manager_engine):
        fixture = _load_fixture("manager")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = manager_engine.query(qk[0], qk[1])
            actual_keys = [(r.manager_id, r.season) for r in results[:TOP_K_STABLE]]
            expected_keys = [tuple(k) for k in q["top5_keys"]]
            assert actual_keys == expected_keys

    def test_composite_scores_stable(self, manager_engine):
        fixture = _load_fixture("manager")
        for q in fixture["queries"]:
            qk = tuple(q["query_key"])
            results = manager_engine.query(qk[0], qk[1])
            for _i, (actual, expected) in enumerate(
                zip(results[:TOP_K_STABLE], q["top5_scores"], strict=False)
            ):
                assert (
                    abs(actual.score - expected) <= SCORE_ABS_TOLERANCE
                ), f"Score drift delta={abs(actual.score - expected):.2e}"


class TestSituationEngineGoldenFile:
    def test_nearest_play_ids_stable(self, situation_engine):
        fixture = _load_fixture("situation")
        from similarity.engines.situation_similarity import SituationVector

        for q_data in fixture["queries"]:
            sv = SituationVector(**q_data["situation_vector"])
            results = situation_engine.query(sv, k=TOP_K_STABLE)
            actual_ids = [r.play_id for r in results]
            expected_ids = q_data["top5_play_ids"]
            assert actual_ids == expected_ids, (
                f"Situation top-{TOP_K_STABLE} play_id drift: "
                f"got {actual_ids}, expected {expected_ids}"
            )

    def test_nearest_distances_stable(self, situation_engine):
        fixture = _load_fixture("situation")
        from similarity.engines.situation_similarity import SituationVector

        for q_data in fixture["queries"]:
            sv = SituationVector(**q_data["situation_vector"])
            results = situation_engine.query(sv, k=TOP_K_STABLE)
            for i, (actual, expected) in enumerate(
                zip(results, q_data["top5_distances"], strict=False)
            ):
                assert abs(actual.distance - expected) <= SCORE_ABS_TOLERANCE, (
                    f"Situation distance drift at result[{i}]: "
                    f"delta={abs(actual.distance - expected):.2e}"
                )


# ============================================================================
# Cross-engine weight constants regression
# ============================================================================


class TestWeightConstants:
    """
    Sanity-check published weight constants across all engines.

    These tests guard against accidental weight changes that would silently
    shift similarity scores without failing a numerical test.
    Weights must sum to 1.0 to within floating-point tolerance.
    """

    def test_steal_weights_sum_to_one(self):
        # SIM-408: the JUMP sub-score was removed (biomech features Statcast
        # can't supply); the two surviving sub-scores renormalize to 1.0.
        from similarity.engines.baserunner_steal_similarity import (
            WEIGHT_SUCCESS,
            WEIGHT_TENDENCY,
        )

        total = WEIGHT_TENDENCY + WEIGHT_SUCCESS
        assert abs(total - 1.0) < 1e-9, f"steal weights sum to {total}"

    def test_catcher_weights_sum_to_one(self):
        """SIM-408: 4-sub-score defensive composite must sum to 1.0 (Offense
        TRIMmed; the 45/20/12/8 split renormalizes over 0.85)."""
        from similarity.engines.catcher_similarity import (
            WEIGHT_BLOCKING,
            WEIGHT_DETERRENCE,
            WEIGHT_FRAMING,
            WEIGHT_THROWING,
        )

        total = WEIGHT_FRAMING + WEIGHT_BLOCKING + WEIGHT_THROWING + WEIGHT_DETERRENCE
        assert abs(total - 1.0) < 1e-9, f"catcher weights sum to {total}"

    def test_catcher_v2_split_weights(self):
        """SIM-408: Offense (15%) TRIMmed; the 45/20/12/8 defensive split
        renormalizes over 0.85. Throwing stays split 12% execution + 8%
        deterrence (combined 0.20/0.85)."""
        from similarity.engines.catcher_similarity import (
            WEIGHT_BLOCKING,
            WEIGHT_DETERRENCE,
            WEIGHT_FRAMING,
            WEIGHT_THROWING,
        )

        assert abs(WEIGHT_FRAMING - 0.45 / 0.85) < 1e-9
        assert abs(WEIGHT_BLOCKING - 0.20 / 0.85) < 1e-9
        assert abs(WEIGHT_THROWING - 0.12 / 0.85) < 1e-9
        assert abs(WEIGHT_DETERRENCE - 0.08 / 0.85) < 1e-9
        # The two throwing-derived sub-scores still combine to 0.20/0.85.
        assert abs((WEIGHT_THROWING + WEIGHT_DETERRENCE) - 0.20 / 0.85) < 1e-9

    def test_pitcher_steal_weights_sum_to_one(self):
        # SIM-408: Delivery + Pickoff sub-scores removed (not in Statcast);
        # outcome is now the sole sub-score with weight 1.0.
        from similarity.engines.pitcher_steal_similarity import WEIGHT_OUTCOME

        assert abs(WEIGHT_OUTCOME - 1.0) < 1e-9, f"pitcher_steal weight is {WEIGHT_OUTCOME}"

    def test_manager_weights_sum_to_one(self):
        from similarity.engines.manager_similarity import (
            WEIGHT_AGGRESSION,
            WEIGHT_PLATOON,
            WEIGHT_USAGE,
        )

        total = WEIGHT_USAGE + WEIGHT_AGGRESSION + WEIGHT_PLATOON
        assert abs(total - 1.0) < 1e-9, f"manager weights sum to {total}"

    def test_catcher_framing_dominates(self):
        """Framing is the highest-weight sub-score per spec."""
        from similarity.engines.catcher_similarity import (
            WEIGHT_BLOCKING,
            WEIGHT_DETERRENCE,
            WEIGHT_FRAMING,
            WEIGHT_THROWING,
        )

        assert WEIGHT_FRAMING > WEIGHT_BLOCKING
        assert WEIGHT_FRAMING > WEIGHT_THROWING
        assert WEIGHT_FRAMING > WEIGHT_DETERRENCE

    def test_manager_eb_prior(self):
        from similarity.engines.manager_similarity import EB_N_PRIOR

        assert EB_N_PRIOR == 30

    def test_steal_eb_prior(self):
        from similarity.engines.baserunner_steal_similarity import EB_N_PRIOR

        assert EB_N_PRIOR == 20

    def test_pitcher_steal_eb_prior(self):
        from similarity.engines.pitcher_steal_similarity import EB_N_PRIOR

        assert EB_N_PRIOR == 25

    def test_catcher_eb_prior(self):
        from similarity.engines.catcher_similarity import EB_N_PRIOR

        assert EB_N_PRIOR == 15
