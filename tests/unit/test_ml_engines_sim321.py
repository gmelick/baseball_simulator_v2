"""
test_ml_engines_sim321.py
=========================
SIM-321 -- Cross-engine score fusion (the per-pitch shaping signal).

Design: ``docs/architecture/2026-06-24-cross-engine-fusion.md``.
Module:  ``simulation/score_fusion.py``.

These tests inject per-engine outputs (numbers and engine-style result objects)
-- no engine construction, no DB.  They assert the fusion:

  (A) is ORDER-INVARIANT where it should be (a symmetric fn of its inputs);
  (B) respects ``score_type`` -- a distance engine and a similarity engine
      combine sensibly (a smaller distance, like a higher similarity, raises
      the fused signal);
  (C) weights behave MONOTONICALLY -- raising one engine's affinity raises the
      fused value, and a heavier weight moves it more;
  (D) handles DEGENERATE inputs (missing/NaN/None, inf distance, all-missing);
  (E) does NOT cross the distance->weight boundary owned by the sampler
      (no reciprocal-distance weight, output is not a normalized p-vector,
      affinity is a monotone-decreasing comparability transform).
"""

from __future__ import annotations

import inspect
import math
import re
import unittest
from dataclasses import dataclass

from simulation import score_fusion as sf
from simulation.score_fusion import (
    EngineSignal,
    FusionResult,
    ScoreFusion,
    distance_to_affinity,
    fuse_scores,
)


# A tiny stand-in for the engines' result dataclasses (SimilarityResult.score /
# NearestSituation.distance) so we exercise the .score / .distance coercion.
@dataclass
class _SimResult:
    score: float


@dataclass
class _DistResult:
    distance: float


class TestComparabilityTransform(unittest.TestCase):
    """(B)/(E) the distance -> affinity comparability transform."""

    def test_affinity_is_bounded_0_1(self):
        for d in (0.0, 0.5, 1.0, 5.0, 100.0):
            a = distance_to_affinity(d)
            self.assertGreaterEqual(a, 0.0)
            self.assertLessEqual(a, 1.0)

    def test_affinity_monotone_decreasing_in_distance(self):
        # Smaller distance -> strictly higher affinity (ordering preserved).
        ds = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0]
        affs = [distance_to_affinity(d) for d in ds]
        for earlier, later in zip(affs, affs[1:]):
            self.assertGreater(earlier, later)

    def test_zero_distance_is_max_affinity(self):
        self.assertAlmostEqual(distance_to_affinity(0.0), 1.0)

    def test_inf_distance_is_zero_affinity(self):
        self.assertEqual(distance_to_affinity(float("inf")), 0.0)

    def test_nan_distance_is_nan(self):
        self.assertTrue(math.isnan(distance_to_affinity(float("nan"))))

    def test_negative_distance_clamped(self):
        # Defensive: negative distance clamps to 0 -> affinity 1.0.
        self.assertAlmostEqual(distance_to_affinity(-3.0), 1.0)

    def test_scale_must_be_positive(self):
        with self.assertRaises(ValueError):
            distance_to_affinity(1.0, scale=0.0)

    def test_larger_scale_softens_decay(self):
        # A bigger scale -> the same distance maps to a higher affinity.
        self.assertLess(distance_to_affinity(2.0, scale=1.0),
                        distance_to_affinity(2.0, scale=5.0))


class TestScoreTypeRespected(unittest.TestCase):
    """(B) similarity passed through; distance exp-mapped; sensible combination."""

    def test_similarity_passed_through_unchanged(self):
        s = EngineSignal("pitcher", 0.73, score_type="similarity")
        self.assertAlmostEqual(s.affinity(), 0.73)

    def test_distance_is_exp_mapped(self):
        s = EngineSignal("situation", 1.0, score_type="distance", scale=1.0)
        self.assertAlmostEqual(s.affinity(), math.exp(-1.0))

    def test_similarity_clamped_to_unit_interval(self):
        # A defensive >1 similarity is clamped, not exp-mapped.
        s = EngineSignal("batter", 1.4, score_type="similarity")
        self.assertEqual(s.affinity(), 1.0)
        s2 = EngineSignal("batter", -0.2, score_type="similarity")
        self.assertEqual(s2.affinity(), 0.0)

    def test_distance_and_similarity_combine_sensibly(self):
        # A closer situation (smaller distance) yields a higher fused signal,
        # holding the similarity engines fixed -- distance is honoured as
        # "lower = more similar".
        near = fuse_scores(
            {"pitcher": (0.7, "similarity"),
             "batter": (0.6, "similarity"),
             "situation": (0.2, "distance")},
            profile="pitch_draw",
        )
        far = fuse_scores(
            {"pitcher": (0.7, "similarity"),
             "batter": (0.6, "similarity"),
             "situation": (5.0, "distance")},
            profile="pitch_draw",
        )
        self.assertGreater(near.fused, far.fused)

    def test_result_object_coercion(self):
        # Accept engine-style result objects (.score / .distance).
        res = fuse_scores(
            {"pitcher": (_SimResult(0.8), "similarity"),
             "situation": (_DistResult(0.5), "distance")},
            weights={"pitcher": 0.6, "situation": 0.4},
        )
        self.assertTrue(0.0 <= res.fused <= 1.0)

    def test_unknown_score_type_raises(self):
        with self.assertRaises(ValueError):
            EngineSignal("x", 0.5, score_type="bogus").affinity()


class TestOrderInvariance(unittest.TestCase):
    """(A) fusion is a symmetric function of its (weight, affinity) pairs."""

    def test_mapping_order_does_not_matter(self):
        a = fuse_scores(
            {"pitcher": (0.8, "similarity"),
             "batter": (0.5, "similarity"),
             "situation": (1.0, "distance")},
            profile="pitch_draw",
        )
        b = fuse_scores(
            {"situation": (1.0, "distance"),
             "batter": (0.5, "similarity"),
             "pitcher": (0.8, "similarity")},
            profile="pitch_draw",
        )
        self.assertAlmostEqual(a.fused, b.fused, places=12)

    def test_iterable_order_does_not_matter(self):
        sigs = [
            EngineSignal("pitcher", 0.8, "similarity"),
            EngineSignal("batter", 0.5, "similarity"),
            EngineSignal("situation", 1.0, "distance"),
        ]
        f1 = ScoreFusion(profile="pitch_draw").fuse(sigs).fused
        f2 = ScoreFusion(profile="pitch_draw").fuse(list(reversed(sigs))).fused
        self.assertAlmostEqual(f1, f2, places=12)

    def test_order_invariance_linear_rule_too(self):
        w = {"pitcher": 0.5, "batter": 0.3, "situation": 0.2}
        a = fuse_scores(
            {"pitcher": (0.8, "similarity"), "batter": (0.4, "similarity"),
             "situation": (0.7, "distance")},
            weights=w, rule="linear",
        )
        b = fuse_scores(
            {"batter": (0.4, "similarity"), "situation": (0.7, "distance"),
             "pitcher": (0.8, "similarity")},
            weights=w, rule="linear",
        )
        self.assertAlmostEqual(a.fused, b.fused, places=12)


class TestMonotonicWeights(unittest.TestCase):
    """(C) weights + affinities behave monotonically."""

    def test_raising_one_affinity_raises_fused(self):
        base = fuse_scores(
            {"pitcher": (0.5, "similarity"), "batter": (0.5, "similarity"),
             "situation": (1.0, "distance")},
            profile="pitch_draw",
        )
        higher = fuse_scores(
            {"pitcher": (0.9, "similarity"), "batter": (0.5, "similarity"),
             "situation": (1.0, "distance")},
            profile="pitch_draw",
        )
        self.assertGreater(higher.fused, base.fused)

    def test_heavier_weight_moves_fused_more(self):
        # Same affinities; the engine whose affinity differs from the others
        # should pull the fused value more when it is weighted more heavily.
        sigs = {"a": (0.9, "similarity"), "b": (0.3, "similarity")}
        a_heavy = fuse_scores(sigs, weights={"a": 0.9, "b": 0.1}, rule="geometric")
        b_heavy = fuse_scores(sigs, weights={"a": 0.1, "b": 0.9}, rule="geometric")
        # Heavier weight on the high-affinity engine -> higher fused value.
        self.assertGreater(a_heavy.fused, b_heavy.fused)

    def test_all_equal_affinities_give_that_value(self):
        # Geometric mean of identical values is that value, weights notwithstanding.
        res = fuse_scores(
            {"pitcher": (0.6, "similarity"), "batter": (0.6, "similarity"),
             "situation": (0.6, "similarity")},
            profile="pitch_draw",
        )
        self.assertAlmostEqual(res.fused, 0.6, places=9)

    def test_geometric_is_and_leaning_vs_linear(self):
        # One near-zero affinity should crush the geometric mean far more than
        # the linear blend (AND- vs OR-semantics, design doc 4).
        sigs = {"a": (0.9, "similarity"), "b": (0.01, "similarity")}
        w = {"a": 0.5, "b": 0.5}
        geo = fuse_scores(sigs, weights=w, rule="geometric").fused
        lin = fuse_scores(sigs, weights=w, rule="linear").fused
        self.assertLess(geo, lin)


class TestDegenerateInputs(unittest.TestCase):
    """(D) missing / NaN / None / inf / empty handled without crashing."""

    def test_missing_engine_weight_redistributed(self):
        # 'situation' has no configured weight here -> ignored; the present two
        # engines renormalize to sum to 1.
        res = fuse_scores(
            {"pitcher": (0.8, "similarity"), "batter": (0.4, "similarity"),
             "situation": (1.0, "distance")},
            weights={"pitcher": 0.5, "batter": 0.5},
        )
        self.assertNotIn("situation", res.weights)
        self.assertAlmostEqual(sum(res.weights.values()), 1.0, places=9)

    def test_none_signal_dropped_and_weight_redistributed(self):
        # A None pitcher signal is missing -> dropped, batter+situation carry it.
        res = fuse_scores(
            {"pitcher": (None, "similarity"), "batter": (0.6, "similarity"),
             "situation": (0.5, "distance")},
            profile="pitch_draw",
        )
        self.assertNotIn("pitcher", res.weights)
        self.assertIn("batter", res.weights)
        self.assertIn("situation", res.weights)
        self.assertAlmostEqual(sum(res.weights.values()), 1.0, places=9)

    def test_nan_similarity_dropped(self):
        res = fuse_scores(
            {"pitcher": (float("nan"), "similarity"), "batter": (0.6, "similarity")},
            weights={"pitcher": 0.5, "batter": 0.5},
        )
        self.assertNotIn("pitcher", res.weights)
        self.assertAlmostEqual(res.fused, 0.6, places=9)

    def test_inf_distance_contributes_zero_affinity(self):
        # An infinitely-far situation -> affinity 0; geometric mean -> ~0.
        res = fuse_scores(
            {"pitcher": (0.8, "similarity"), "situation": (float("inf"), "distance")},
            weights={"pitcher": 0.5, "situation": 0.5},
            rule="geometric",
        )
        self.assertLess(res.fused, 1e-2)

    def test_all_missing_returns_neutral_zero(self):
        res = fuse_scores(
            {"pitcher": (None, "similarity"), "batter": (None, "similarity")},
            weights={"pitcher": 0.5, "batter": 0.5},
        )
        self.assertEqual(res.fused, 0.0)
        self.assertEqual(res.weights, {})

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            ScoreFusion(profile="does_not_exist")

    def test_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            fuse_scores({"a": (0.5, "similarity")}, weights={"a": 1.0}, rule="nope")


class TestDistanceWeightBoundaryNotCrossed(unittest.TestCase):
    """(E) fusion stays on the correct side of the sampler's boundary."""

    def test_output_is_not_a_normalized_probability_vector(self):
        # The fused per-candidate value need NOT sum to 1 across candidates.
        # Two candidates with identical high affinities both score high; their
        # fused values do not normalize to a p-vector summing to 1.
        cand_a = fuse_scores({"pitcher": (0.9, "similarity")},
                             weights={"pitcher": 1.0}).fused
        cand_b = fuse_scores({"pitcher": (0.9, "similarity")},
                             weights={"pitcher": 1.0}).fused
        self.assertAlmostEqual(cand_a, 0.9, places=9)
        self.assertAlmostEqual(cand_b, 0.9, places=9)
        self.assertGreater(cand_a + cand_b, 1.0)  # not a normalized distribution

    def test_affinities_are_diagnostic_and_unnormalized(self):
        res = fuse_scores(
            {"pitcher": (0.8, "similarity"), "batter": (0.7, "similarity"),
             "situation": (0.5, "distance")},
            profile="pitch_draw",
        )
        # Per-engine affinities are independent [0,1] values; they do not sum to 1.
        self.assertNotAlmostEqual(sum(res.affinities.values()), 1.0, places=3)
        for a in res.affinities.values():
            self.assertGreaterEqual(a, 0.0)
            self.assertLessEqual(a, 1.0)

    def test_module_source_has_no_sampler_distance_weight_transform(self):
        # Static guard: the sampler's reciprocal-distance weight transform must
        # NOT appear in the fusion module's executable code (boundary, doc 1).
        # Strip comments + docstrings so prose *describing* the boundary (which
        # is expected) does not trip the guard.
        code_lines = []
        for line in inspect.getsource(sf).splitlines():
            if line.strip().startswith("#"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        code = re.sub(r'""".*?"""', "", code, flags=re.DOTALL)
        code = re.sub(r"'''.*?'''", "", code, flags=re.DOTALL)
        # No EPS-style reciprocal-distance weighting in executable code.
        self.assertIsNone(
            re.search(r"1\.?0?\s*/\s*\(\s*d\w*\s*\+", code),
            "fusion must not implement the sampler's reciprocal-distance weight",
        )
        self.assertNotIn("EPS", code)
        # Fusion must not import the sampler (it stays sampler-independent).
        self.assertNotIn("play_pool_sampler", code)
        self.assertNotIn("import faiss", code)

    def test_affinity_transform_is_monotone_not_reciprocal(self):
        # The comparability transform is exp-decay (bounded), unlike the
        # sampler's unbounded reciprocal-distance weight.  At d=0 affinity is
        # exactly 1.0, not a huge reciprocal blow-up.
        self.assertEqual(distance_to_affinity(0.0), 1.0)
        # Doubling distance does NOT halve affinity (so it isn't 1/d).
        self.assertNotAlmostEqual(
            distance_to_affinity(2.0) / distance_to_affinity(1.0), 0.5, places=3
        )


class TestResultContract(unittest.TestCase):
    """The FusionResult shape SIM-317 / SIM-318 consume (design doc 5)."""

    def test_result_fields_present(self):
        res = fuse_scores(
            {"pitcher": (0.8, "similarity"), "batter": (0.5, "similarity"),
             "situation": (1.0, "distance")},
            profile="pitch_draw",
        )
        self.assertIsInstance(res, FusionResult)
        self.assertTrue(0.0 <= res.fused <= 1.0)
        self.assertEqual(set(res.affinities), {"pitcher", "batter", "situation"})
        self.assertEqual(res.profile, "pitch_draw")
        self.assertEqual(res.rule, "geometric")
        self.assertAlmostEqual(sum(res.weights.values()), 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
