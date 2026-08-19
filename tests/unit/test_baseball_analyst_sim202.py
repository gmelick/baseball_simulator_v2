"""tests/unit/test_baseball_analyst_sim202.py — SIM-202 / SIM-511
=========================================================
Unit tests for the constants module (``simulation/constants.py``).

SIM-202 centralized the run-value methodology into one module. The
linear-weight NUMBERS were removed 2026-08-19 (owner ruling, the SIM-511+512
landing): production never read them — the ledger resolves every play by RE24
over real base-out states — so only the outcome VOCABULARY remains, plus the
defensive run conversions the fielder/catcher engines are built on.

Canonical outcome partition
---------------------------
A plate appearance resolves to exactly one of these mutually-exclusive,
collectively-exhaustive outcomes (the standard Retrosheet/Statcast partition
plus ``field_error``, its own outcome since SIM-511 — a reach, never a hit):

    single, double, triple, home_run,
    walk, intentional_walk, hit_by_pitch,
    strikeout, field_out, ground_into_double_play,
    sacrifice_fly, sacrifice_hit, field_error
"""

from __future__ import annotations

import unittest

from simulation.constants import CANONICAL_OUTCOME_KEYS, DEFENSIVE_RUN_VALUES

# The defensible canonical outcome set: the 12 standard PA outcomes plus
# field_error (SIM-496/511).
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
        """The linear-weight table stays deleted (owner ruling 2026-08-19).

        A play's value comes from the RE24 matrix over real states
        (``simulation.run_resolution``), never a per-outcome constant. A
        reintroduced ``RUN_VALUES`` would invite the SIM-312 bug class back.
        """
        import simulation.constants as c

        self.assertFalse(hasattr(c, "RUN_VALUES"))
        self.assertFalse(hasattr(c, "run_value_for_event"))


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
        self.assertEqual(DEFENSIVE_RUN_VALUES["runs_per_strike_above_avg"], 0.125)

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
