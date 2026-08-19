"""SIM-312 tests: the Statcast events vocabulary + RE24 run resolution.

The linear-weight half of SIM-312 (``run_value_for_event`` over the
``RUN_VALUES`` table) was removed 2026-08-19 (owner ruling, the SIM-511+512
landing): production always resolved runs by RE24 over real states, and the
ledger rejected everything else. What SIM-312 still owns — and these tests
still pin — is the VOCABULARY (every Statcast terminal event resolves to a
canonical outcome key) and the RE24 resolution itself.
"""

from __future__ import annotations

import unittest

from simulation.constants import (
    CANONICAL_OUTCOME_KEYS,
    STATCAST_EVENT_ALIASES,
    resolve_event_to_canonical,
)
from simulation.run_resolution import (
    OUTS_PER_INNING,
    RE24_MATRIX,
    re24_from_rows,
    re24_value,
    resolve_runs,
)

STATCAST_TERMINAL_EVENTS = [
    "single",
    "double",
    "triple",
    "home_run",
    "walk",
    "intent_walk",
    "hit_by_pitch",
    "catcher_interf",
    "strikeout",
    "strikeout_double_play",
    "field_out",
    "force_out",
    "fielders_choice",
    "fielders_choice_out",
    "other_out",
    "grounded_into_double_play",
    "double_play",
    "triple_play",
    "sac_fly",
    "sac_fly_double_play",
    "sac_bunt",
    "sac_bunt_double_play",
    "field_error",
]


class TestBackCompat(unittest.TestCase):
    def test_thirteen_canonical_keys(self):
        # The 12 standard PA outcomes + field_error (SIM-496/511).
        self.assertEqual(len(CANONICAL_OUTCOME_KEYS), 13)
        self.assertIn("field_error", CANONICAL_OUTCOME_KEYS)

    def test_canonical_resolve_self(self):
        for k in CANONICAL_OUTCOME_KEYS:
            self.assertEqual(resolve_event_to_canonical(k), k)


class TestVocab(unittest.TestCase):
    def test_every_event_resolves(self):
        for ev in STATCAST_TERMINAL_EVENTS:
            with self.subTest(event=ev):
                c = resolve_event_to_canonical(ev)
                self.assertIsNotNone(c, f"{ev!r} did not resolve")
                self.assertIn(c, CANONICAL_OUTCOME_KEYS)

    def test_alias_targets_real_keys(self):
        self.assertTrue(set(STATCAST_EVENT_ALIASES.values()) <= CANONICAL_OUTCOME_KEYS)

    def test_spellings_agree(self):
        for raw, canon in [
            ("intent_walk", "intentional_walk"),
            ("sac_fly", "sacrifice_fly"),
            ("sac_bunt", "sacrifice_hit"),
            ("grounded_into_double_play", "ground_into_double_play"),
        ]:
            with self.subTest(pair=(raw, canon)):
                self.assertEqual(resolve_event_to_canonical(raw), canon)

    def test_field_error_is_its_own_outcome(self):
        """SIM-496/511: a reach-on-error is NOT a single.

        The old alias ``field_error -> single`` credited a drawn reach-on-
        error as a HIT in the boxscore while the loop retired the batter on
        the bases. The batter reaches on a ``field_error`` and the linescore
        keys on this canonical spelling to keep it out of the H column.
        """
        self.assertEqual(resolve_event_to_canonical("field_error"), "field_error")


class TestUnknownDetectable(unittest.TestCase):
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
        r = resolve_runs(
            event="home_run",
            pre_outs=0,
            pre_runners_state=0,
            post_outs=0,
            post_runners_state=0,
            result_runs=1,
        )
        self.assertEqual(r.method, "re24_delta")
        self.assertAlmostEqual(r.runs, 1.0, places=6)

    def test_grand_slam(self):
        r = resolve_runs(
            event="home_run",
            pre_outs=0,
            pre_runners_state=7,
            post_outs=0,
            post_runners_state=0,
            result_runs=4,
        )
        self.assertEqual(r.method, "re24_delta")
        self.assertGreater(r.runs, 1.5)
        self.assertLess(r.runs, 4.0)

    def test_k_two_outs_negative(self):
        r = resolve_runs(
            event="strikeout",
            pre_outs=2,
            pre_runners_state=3,
            post_outs=OUTS_PER_INNING,
            post_runners_state=0,
            result_runs=0,
        )
        self.assertEqual(r.method, "re24_delta")
        self.assertLess(r.runs, 0.0)
        self.assertEqual(r.post_outs, OUTS_PER_INNING)
        self.assertEqual(r.post_runners_state, 0)

    def test_gidp_worse_than_out(self):
        base = {"pre_outs": 0, "pre_runners_state": 1}
        gidp = resolve_runs(
            event="grounded_into_double_play",
            post_outs=2,
            post_runners_state=0,
            result_runs=0,
            **base,
        )
        out1 = resolve_runs(
            event="field_out", post_outs=1, post_runners_state=1, result_runs=0, **base
        )
        self.assertEqual(gidp.method, "re24_delta")
        self.assertLess(gidp.runs, out1.runs)

    def test_single_scoring_positive(self):
        r = resolve_runs(
            event="single",
            pre_outs=0,
            pre_runners_state=2,
            post_outs=0,
            post_runners_state=1,
            result_runs=1,
        )
        self.assertEqual(r.method, "re24_delta")
        self.assertGreater(r.runs, 0.0)

    # SIM-499 REMOVED two tests here: ``test_advance_conserves`` and
    # ``test_inning_end_clears``. Both drove ``run_resolution.advance_state``,
    # which SIM-499 deleted on purpose.
    #
    # That function DERIVED the after-state from a conservation identity,
    # ``new_on_base = old_on_base + reached - runs_scored``. The identity has no
    # notion of a runner RETIRED on the play, so a double play desynced it, and a
    # reach-on-error — which carries ``result_hits=0`` — was invisible to it, which
    # is why every reach-on-error recorded a run value of exactly 0.00.
    #
    # The ledger is now TOLD both states instead of deriving either. The identity
    # survives as ``Bases.assert_transition``, where it is a CHECK rather than a
    # derivation: wrong as a way to compute the after-state, right as a way to
    # verify one. ``tests/unit/test_sim500_base_invariants.py`` covers it there.


class TestStateRequired(unittest.TestCase):
    """The context-free linear-weight fallback is GONE (2026-08-19).

    ``resolve_runs`` accepts the full base-out state or nothing at all — and
    nothing at all is now an error, not a constant. A hand-set per-event
    number must never stand in for real states.
    """

    def test_partial_state_raises(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_runs(event="single", pre_outs=0, pre_runners_state=0)
        self.assertIn("post_outs", str(ctx.exception))

    def test_no_state_at_all_raises(self):
        with self.assertRaises(ValueError):
            resolve_runs(event="single")

    def test_unknown_event_without_state_raises(self):
        with self.assertRaises(ValueError):
            resolve_runs(event="made_up")


if __name__ == "__main__":
    unittest.main()
