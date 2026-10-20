"""tests/unit/test_simulation_constants.py
=========================================================
Unit tests for the constants module (``simulation/constants.py``).

The module holds the outcome VOCABULARY and the defensive run conversions
the fielder/catcher engines are built on.  It holds no linear-weight numbers
(the owner's ruling of 2026-08-19): the ledger resolves every play by RE24 over
real base-out states, so a per-outcome constant has no reader.

Canonical outcome partition
---------------------------
A plate appearance resolves to exactly one of these mutually-exclusive,
collectively-exhaustive outcomes (the standard Retrosheet/Statcast partition
plus ``field_error``, its own outcome — a reach, never a hit):

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit, field_error
"""

from __future__ import annotations

import unittest

import pipeline.batch.profiles.defense as profile_defense
from simulation.constants import CANONICAL_OUTCOME_KEYS, DEFENSIVE_RUN_VALUES

# The defensible canonical outcome set: the 12 standard PA outcomes plus
# field_error.
EXPECTED_CANONICAL_KEYS = {
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
    "field_error",
}


class TestCanonicalVocabulary(unittest.TestCase):
    """Acceptance tests for the canonical outcome vocabulary."""

    def test_vocabulary_is_exactly_the_thirteen_outcomes(self) -> None:
        """KEY TEST: the 12 standard PA outcomes + field_error, nothing else."""
        self.assertEqual(set(CANONICAL_OUTCOME_KEYS), EXPECTED_CANONICAL_KEYS)
        self.assertEqual(len(CANONICAL_OUTCOME_KEYS), 13)

    def test_no_run_value_table_exists(self) -> None:
        """No linear-weight table exists (the owner's ruling of 2026-08-19).

        A play's value comes from the RE24 matrix over real states
        (``simulation.run_resolution``), never a per-outcome constant. A
        ``RUN_VALUES`` table would invite a constant to stand in for real states.
        """
        import simulation.constants as c

        self.assertFalse(hasattr(c, "RUN_VALUES"))
        self.assertFalse(hasattr(c, "run_value_for_event"))


class TestDefensiveRunValues(unittest.TestCase):
    """The defensive run-value table lives in one place (CLAUDE.md §10)."""

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
        """The values are the established run conversions (CLAUDE.md §10)."""
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"], 0.75)
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"], 0.90)
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_block_saved"], 0.25)
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"], 0.125)

    def test_outfield_oaa_worth_more_than_infield(self) -> None:
        self.assertGreater(
            DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"],
            DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"],
        )


class TestConstantsAreReferencedInPipeline(unittest.TestCase):
    """The pipeline computor must source its defensive constants from here."""

    def test_player_profile_computor_uses_centralized_values(self) -> None:

        self.assertEqual(
            profile_defense.RUNS_PER_OAA_INFIELD,
            DEFENSIVE_RUN_VALUES["runs_per_oaa_infield"],
        )
        self.assertEqual(
            profile_defense.RUNS_PER_OAA_OUTFIELD,
            DEFENSIVE_RUN_VALUES["runs_per_oaa_outfield"],
        )
        self.assertEqual(
            profile_defense.RUNS_PER_BLOCK_SAVED,
            DEFENSIVE_RUN_VALUES["runs_per_block_saved"],
        )
        self.assertEqual(
            profile_defense.RUNS_PER_STRIKE_ABOVE_AVG,
            DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"],
        )


if __name__ == "__main__":
    unittest.main()
