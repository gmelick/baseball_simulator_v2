"""tests/unit/test_baseball_analyst_sim202.py — SIM-202
=========================================================
Unit tests for the centralized run-value constants module
(``simulation/constants.py``).

The Baseball Analyst owns the run-value methodology for the simulator.
SIM-202 centralizes the offensive linear weights and defensive run-value
conversions into one sourced module so the numbers stay consistent and
auditable across the codebase.

Canonical 12-outcome partition
------------------------------
A plate appearance resolves to exactly one of 12 mutually-exclusive,
collectively-exhaustive outcomes. This is the standard Retrosheet/Statcast
PA-outcome partition used for linear-weight run accounting:

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit

The values are anchored to the 2024 MLB run environment (FanGraphs 2024
guts/constants; Tango/Lichtman/Dolphin, "The Book"). See the module docstring
in ``simulation/constants.py`` for the full source citation.
"""

from __future__ import annotations

import unittest

from simulation.constants import DEFENSIVE_RUN_VALUES, RUN_VALUES

# The defensible canonical set of exactly 12 standard PA outcomes.
EXPECTED_RUN_VALUE_KEYS = {
    "single",
    "double",
    "triple",
    "home_run",
    "walk",
    "intentional_walk",
    "hit_by_pitch",
    "strikeout",
    "field_out",
    "ground_into_double_play",
    "sacrifice_fly",
    "sacrifice_hit",
}


class TestRunValues(unittest.TestCase):
    """Core acceptance tests for the offensive RUN_VALUES table."""

    def test_run_values_contains_all_twelve_pa_outcomes(self) -> None:
        """KEY TEST: RUN_VALUES has exactly the 12 standard PA outcomes."""
        self.assertEqual(set(RUN_VALUES.keys()), EXPECTED_RUN_VALUE_KEYS)
        self.assertEqual(len(RUN_VALUES), 12)

    def test_all_values_are_floats(self) -> None:
        for key, value in RUN_VALUES.items():
            self.assertIsInstance(value, float, msg=f"{key} is not a float")

    def test_extra_base_hit_ordering(self) -> None:
        """HR > triple > double > single > walk > 0."""
        self.assertGreater(RUN_VALUES["home_run"], RUN_VALUES["triple"])
        self.assertGreater(RUN_VALUES["triple"], RUN_VALUES["double"])
        self.assertGreater(RUN_VALUES["double"], RUN_VALUES["single"])
        self.assertGreater(RUN_VALUES["single"], RUN_VALUES["walk"])
        self.assertGreater(RUN_VALUES["walk"], 0.0)

    def test_outs_are_negative(self) -> None:
        """field_out and strikeout carry negative run value."""
        self.assertLess(RUN_VALUES["field_out"], 0.0)
        self.assertLess(RUN_VALUES["strikeout"], 0.0)

    def test_double_play_is_worst_out(self) -> None:
        """GIDP (out + lost runner) is worse than a plain out."""
        self.assertLess(
            RUN_VALUES["ground_into_double_play"], RUN_VALUES["field_out"]
        )

    def test_hbp_at_least_walk(self) -> None:
        """HBP is worth at least an unintentional walk (can't be intentional)."""
        self.assertGreaterEqual(RUN_VALUES["hit_by_pitch"], RUN_VALUES["walk"])

    def test_intentional_walk_worth_less_than_uintentional(self) -> None:
        """IBB is worth far less than an uBB."""
        self.assertLess(RUN_VALUES["intentional_walk"], RUN_VALUES["walk"])

    def test_sacrifice_fly_is_productive(self) -> None:
        """A sac fly (scores a runner) beats a plain out."""
        self.assertGreater(RUN_VALUES["sacrifice_fly"], RUN_VALUES["field_out"])


class TestDefensiveRunValues(unittest.TestCase):
    """Defensive run-value table is centralized and unchanged (refactor)."""

    def test_keys_present(self) -> None:
        self.assertEqual(
            set(DEFENSIVE_RUN_VALUES.keys()),
            {
                "runs_per_oaa_infield",
                "runs_per_oaa_outfield",
                "runs_per_block_saved",
                "runs_per_strike_above_avg",
            },
        )

    def test_all_values_are_floats(self) -> None:
        for key, value in DEFENSIVE_RUN_VALUES.items():
            self.assertIsInstance(value, float, msg=f"{key} is not a float")

    def test_exact_values_preserved(self) -> None:
        """Values must match the prior inline constants exactly (refactor)."""
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"], 0.75)
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"], 0.90)
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_block_saved"], 0.25)
        self.assertEqual(
            DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"], 0.125
        )

    def test_outfield_oaa_worth_more_than_infield(self) -> None:
        self.assertGreater(
            DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"],
            DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"],
        )


class TestConstantsAreReferencedInPipeline(unittest.TestCase):
    """The pipeline computor must source its defensive constants from here."""

    def test_player_profile_computor_uses_centralized_values(self) -> None:
        import pipeline.batch.player_profile_computor as computor

        self.assertEqual(
            computor.RUNS_PER_OAA_INFIELD,
            DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"],
        )
        self.assertEqual(
            computor.RUNS_PER_OAA_OUTFIELD,
            DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"],
        )
        self.assertEqual(
            computor.RUNS_PER_BLOCK_SAVED,
            DEFENSIVE_RUN_VALUES["runs_per_block_saved"],
        )
        self.assertEqual(
            computor.RUNS_PER_STRIKE_ABOVE_AVG,
            DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"],
        )


if __name__ == "__main__":
    unittest.main()
