"""SIM-312 tests: RUN_VALUES<->Statcast events vocab fix + run resolution."""
from __future__ import annotations
import unittest
from simulation.constants import (
    CANONICAL_OUTCOME_KEYS, RUN_VALUES, STATCAST_EVENT_ALIASES,
    UnknownEventError, resolve_event_to_canonical, run_value_for_event,
)
from simulation.run_resolution import (
    OUTS_PER_INNING, RE24_MATRIX, advance_state, re24_from_rows,
    re24_value, resolve_runs,
)

STATCAST_TERMINAL_EVENTS = [
    "single", "double", "triple", "home_run",
    "walk", "intent_walk", "hit_by_pitch", "catcher_interf",
    "strikeout", "strikeout_double_play",
    "field_out", "force_out", "fielders_choice", "fielders_choice_out", "other_out",
    "grounded_into_double_play", "double_play", "triple_play",
    "sac_fly", "sac_fly_double_play", "sac_bunt", "sac_bunt_double_play",
    "field_error",
]
# Outs that COST runs (sac_fly excluded; it is productive/positive).
COMMON_STATCAST_OUTS = [
    "field_out", "force_out", "fielders_choice",
    "grounded_into_double_play", "double_play", "strikeout",
]


class TestBackCompat(unittest.TestCase):
    def test_twelve_canonical_keys(self):
        self.assertEqual(len(RUN_VALUES), 12)
        self.assertEqual(CANONICAL_OUTCOME_KEYS, frozenset(RUN_VALUES.keys()))

    def test_canonical_resolve_self(self):
        for k in RUN_VALUES:
            self.assertEqual(resolve_event_to_canonical(k), k)


class TestVocab(unittest.TestCase):
    def test_every_event_resolves(self):
        for ev in STATCAST_TERMINAL_EVENTS:
            with self.subTest(event=ev):
                c = resolve_event_to_canonical(ev)
                self.assertIsNotNone(c, f"{ev!r} did not resolve")
                self.assertIn(c, RUN_VALUES)

    def test_every_event_non_none_value(self):
        for ev in STATCAST_TERMINAL_EVENTS:
            with self.subTest(event=ev):
                rv = run_value_for_event(ev)
                self.assertIsNotNone(rv, f"{ev!r} silently missed")
                self.assertIsInstance(rv, float)

    def test_common_outs_negative_not_zero(self):
        for ev in COMMON_STATCAST_OUTS:
            with self.subTest(event=ev):
                rv = run_value_for_event(ev)
                self.assertIsNotNone(rv)
                self.assertLess(rv, 0.0, f"{ev!r} must cost runs")

    def test_sac_fly_productive(self):
        self.assertGreater(run_value_for_event("sac_fly"), 0.0)

    def test_alias_targets_real_keys(self):
        self.assertTrue(set(STATCAST_EVENT_ALIASES.values()) <= CANONICAL_OUTCOME_KEYS)

    def test_spellings_agree(self):
        for raw, canon in [("intent_walk", "intentional_walk"),
                           ("sac_fly", "sacrifice_fly"),
                           ("sac_bunt", "sacrifice_hit"),
                           ("grounded_into_double_play", "ground_into_double_play")]:
            with self.subTest(pair=(raw, canon)):
                self.assertEqual(run_value_for_event(raw), RUN_VALUES[canon])

    def test_dp_at_least_as_bad_as_field_out(self):
        self.assertLessEqual(run_value_for_event("double_play"),
                             run_value_for_event("field_out"))


class TestUnknownDetectable(unittest.TestCase):
    def test_unknown_returns_default(self):
        self.assertIsNone(run_value_for_event("made_up"))
        self.assertEqual(run_value_for_event("made_up", default=-99.0), -99.0)

    def test_unknown_strict_raises(self):
        with self.assertRaises(UnknownEventError):
            run_value_for_event("made_up", strict=True)

    def test_markers_resolve_none(self):
        for m in ("in_progress", "unknown", "", None):
            with self.subTest(marker=m):
                self.assertIsNone(resolve_event_to_canonical(m))


class TestRE24Matrix(unittest.TestCase):
    def test_all_24_states(self):
        self.assertEqual(set(RE24_MATRIX), {(o, rs) for o in range(3) for rs in range(8)})
        self.assertEqual(len(RE24_MATRIX), 24)

    def test_more_outs_lower(self):
        for rs in range(8):
            self.assertGreater(RE24_MATRIX[(0, rs)], RE24_MATRIX[(1, rs)])
            self.assertGreater(RE24_MATRIX[(1, rs)], RE24_MATRIX[(2, rs)])

    def test_more_runners_higher(self):
        for o in range(3):
            self.assertGreater(RE24_MATRIX[(o, 7)], RE24_MATRIX[(o, 0)])

    def test_three_outs_zero(self):
        self.assertEqual(re24_value(OUTS_PER_INNING, 7), 0.0)

    def test_from_rows_roundtrip(self):
        rows = [(o, rs, RE24_MATRIX[(o, rs)]) for o in range(3) for rs in range(8)]
        self.assertEqual(re24_from_rows(rows), RE24_MATRIX)

    def test_from_rows_rejects_incomplete(self):
        with self.assertRaises(ValueError):
            re24_from_rows([(0, 0, 0.5)])


class TestRE24Resolution(unittest.TestCase):
    def test_solo_hr(self):
        r = resolve_runs(event="home_run", outs=0, runners_state=0,
                         result_hits=4, result_outs=0, result_runs=1)
        self.assertEqual(r.method, "re24_delta")
        self.assertAlmostEqual(r.runs, 1.0, places=6)

    def test_grand_slam(self):
        r = resolve_runs(event="home_run", outs=0, runners_state=7,
                         result_hits=4, result_outs=0, result_runs=4)
        self.assertEqual(r.method, "re24_delta")
        self.assertGreater(r.runs, 1.5)
        self.assertLess(r.runs, 4.0)

    def test_k_two_outs_negative(self):
        r = resolve_runs(event="strikeout", outs=2, runners_state=3,
                         result_hits=0, result_outs=1, result_runs=0)
        self.assertEqual(r.method, "re24_delta")
        self.assertLess(r.runs, 0.0)
        self.assertEqual(r.new_outs, OUTS_PER_INNING)
        self.assertEqual(r.new_runners_state, 0)

    def test_gidp_worse_than_out(self):
        base = dict(outs=0, runners_state=1)
        gidp = resolve_runs(event="grounded_into_double_play",
                            result_hits=0, result_outs=2, result_runs=0, **base)
        out1 = resolve_runs(event="field_out",
                            result_hits=0, result_outs=1, result_runs=0, **base)
        self.assertEqual(gidp.method, "re24_delta")
        self.assertLess(gidp.runs, out1.runs)

    def test_single_scoring_positive(self):
        r = resolve_runs(event="single", outs=0, runners_state=2,
                         result_hits=1, result_outs=0, result_runs=1)
        self.assertEqual(r.method, "re24_delta")
        self.assertGreater(r.runs, 0.0)

    def test_advance_conserves(self):
        no, nr = advance_state(0, 1, result_hits=1, result_outs=0, result_runs=0)
        self.assertEqual(no, 0)
        self.assertEqual(bin(nr).count("1"), 2)

    def test_inning_end_clears(self):
        no, nr = advance_state(2, 7, result_hits=0, result_outs=1, result_runs=0)
        self.assertEqual(no, OUTS_PER_INNING)
        self.assertEqual(nr, 0)


class TestFallback(unittest.TestCase):
    def test_walk_fallback(self):
        r = resolve_runs(event="walk")
        self.assertEqual(r.method, "linear_weight")
        self.assertAlmostEqual(r.runs, RUN_VALUES["walk"], places=6)

    def test_alias_fallback(self):
        r = resolve_runs(event="field_out")
        self.assertEqual(r.method, "linear_weight")
        self.assertLess(r.runs, 0.0)
        self.assertAlmostEqual(r.runs, RUN_VALUES["field_out"], places=6)

    def test_partial_deltas_fallback(self):
        r = resolve_runs(event="single", outs=0, runners_state=0, result_hits=1)
        self.assertEqual(r.method, "linear_weight")

    def test_unknown_no_deltas_raises(self):
        with self.assertRaises(ValueError):
            resolve_runs(event="made_up")

    def test_strict_unknown_raises(self):
        with self.assertRaises((UnknownEventError, ValueError)):
            resolve_runs(event="made_up", strict=True)


if __name__ == "__main__":
    unittest.main()
