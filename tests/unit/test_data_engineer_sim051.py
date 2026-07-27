"""
test_data_engineer_sim051.py
============================
Unit tests for SIM-051 — handedness-corrected `pull_relative_spray_angle` on
`sim.outcome_pool`, as corrected by SIM-440.

Background
----------
`pull_relative_spray_angle` is the raw `spray_angle` sign-flipped by batter
handedness so that "pull" is always positive (LF for a RHB, RF for a LHB).

**Which handedness column (SIM-440).** The flip keys on `raw.pitches.stand`,
the side the batter ACTUALLY BATTED FROM in that plate appearance. It does NOT
key on `raw.pitches.bat_hand`, which is the batter's ROSTER-DECLARED side.
Measured over 2017-2025 (docs/data_quality/2026-05-20-bat-side-coverage.md):

    'S' in `stand`    ->      0 rows, every season
    'S' in `bat_hand` -> 10.4-13.3% of rows, every season

The original SIM-051 implementation, this test file, DuckDB migration 0003 and
several architecture docs all asserted the opposite ("bat_hand is the per-PA
resolved handedness, NOT the roster value"). Keying on `bat_hand` gave every
switch-hitter batted ball a NULL here, and
`engine_artifacts.build_battedball_pool_artifact` filters on
`pull_relative_spray_angle IS NOT NULL` — so roughly 1 batted ball in 8 was
dropped outright from the production batted-ball draw.

Why this file was rewritten
---------------------------
The previous version was **structurally incapable of failing** against the
production code: it defined its own `_FLIP_SQL` literal, its own Python
reference helper and its own in-memory table, so reverting
`_build_outcome_pool` left every test green. It also enshrined the inverted
premise in a test named
`test_switch_hitter_uses_per_pa_bat_hand_not_roster_bats`.

These tests now extract the CASE expression from the **live production source**
(`inspect.getsource(PlayerProfileComputor._build_outcome_pool)`) and execute
that text against an in-memory DuckDB. Reverting the production query turns
them red.

Locked behaviours:
  (a) LHB pull -> positive
  (b) RHB pull -> positive
  (c) a switch hitter (bat_hand='S') gets a REAL value, keyed on `stand`
  (d) NULL spray -> NULL, without crashing
  (e) the production expression does not reference `bat_hand` at all

Run with:
    pytest tests/unit/test_data_engineer_sim051.py -v
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

import duckdb

# ---------------------------------------------------------------------------
# Extract the REAL production CASE expression
# ---------------------------------------------------------------------------


def _production_flip_case() -> str:
    """Pull the live `pull_relative_spray_angle` CASE out of the computor source.

    Deliberately not a copy: a copied literal is what made the previous version
    of this file unfalsifiable.
    """
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    src = inspect.getsource(PlayerProfileComputor._build_outcome_pool)
    alias = "AS pull_relative_spray_angle"
    assert alias in src, "pull_relative_spray_angle projection not found — retarget this test"
    head = src[: src.index(alias)]
    case = head[head.rindex("CASE") :]
    # Strip SQL comment lines so prose cannot satisfy the assertions below.
    return "\n".join(ln for ln in case.splitlines() if not ln.strip().startswith("--"))


_FLIP_CASE = _production_flip_case()

_FLIP_SQL = f"""
    SELECT
        {_FLIP_CASE} AS pull_relative_spray_angle
      FROM rp
     ORDER BY rn
"""


def _expected(spray_angle, stand):
    """Reference: positive = pull side, keyed on the per-PA `stand`."""
    if spray_angle is None:
        return None
    if stand == "R":
        return spray_angle
    if stand == "L":
        return -spray_angle
    return None


class TestProductionExpressionShape(unittest.TestCase):
    def test_flip_keys_on_stand_not_bat_hand(self):
        self.assertIn("rp.stand = 'R'", _FLIP_CASE)
        self.assertIn("rp.stand = 'L'", _FLIP_CASE)
        self.assertNotIn(
            "rp.bat_hand",
            _FLIP_CASE,
            "`bat_hand` is the roster-DECLARED side ('S' for every switch hitter, "
            "10.4-13.3% of rows). Keying the spray flip on it NULLs ~1 in 8 batted "
            "balls, which build_battedball_pool_artifact then filters out entirely.",
        )

    def test_sign_convention_positive_is_pull(self):
        r_line = next(ln for ln in _FLIP_CASE.splitlines() if "rp.stand = 'R'" in ln)
        l_line = next(ln for ln in _FLIP_CASE.splitlines() if "rp.stand = 'L'" in ln)
        self.assertNotIn("-rp.spray_angle", r_line, "RHB pull is already positive")
        self.assertIn("-rp.spray_angle", l_line, "LHB pull must be sign-flipped")


class TestPullRelativeSprayAngleDuckDB(unittest.TestCase):
    """Execute the extracted production expression against real DuckDB."""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE rp (
                rn          INTEGER,
                stand       VARCHAR,   -- side actually batted from this PA; never 'S'
                bat_hand    VARCHAR,   -- roster-declared side; 'S' for switch hitters
                spray_angle FLOAT
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def _flip(self, rows):
        self.conn.executemany("INSERT INTO rp VALUES (?, ?, ?, ?)", rows)
        return [r[0] for r in self.conn.execute(_FLIP_SQL).fetchall()]

    def test_pull_is_positive_for_both_hands(self):
        # rn, stand, bat_hand, spray_angle
        out = self._flip(
            [
                (0, "L", "L", -28.0),  # LHB pull (RF) -> +28
                (1, "R", "R", 28.0),  # RHB pull (LF) -> +28
                (2, "L", "L", 28.0),  # LHB oppo -> negative
                (3, "R", "R", -28.0),  # RHB oppo -> negative
            ]
        )
        self.assertAlmostEqual(out[0], 28.0)
        self.assertAlmostEqual(out[1], 28.0)
        self.assertLess(out[2], 0.0)
        self.assertLess(out[3], 0.0)

    def test_switch_hitter_gets_a_real_value(self):
        """SIM-440, the headline case.

        A switch hitter carries bat_hand='S' on EVERY row of his career. The
        per-PA side lives in `stand`. Keying on bat_hand returned NULL here and
        silently deleted him from the batted-ball pool.
        """
        out = self._flip(
            [
                (0, "L", "S", -22.0),  # switch hitter batting lefty this PA
                (1, "R", "S", 19.0),  # ... and righty in another PA
            ]
        )
        self.assertIsNotNone(out[0], "switch hitter must NOT be NULLed — that was the bug")
        self.assertAlmostEqual(out[0], 22.0)
        self.assertGreater(out[0], 0.0)
        self.assertIsNotNone(out[1])
        self.assertAlmostEqual(out[1], 19.0)

    def test_null_spray_is_the_only_null_path(self):
        out = self._flip(
            [
                (0, "R", "R", None),
                (1, "L", "S", None),
            ]
        )
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])

    def test_matches_the_reference_across_a_grid(self):
        cases = [
            ("R", "R", 12.5),
            ("R", "R", -12.5),
            ("L", "L", 33.0),
            ("L", "L", -33.0),
            ("L", "S", -40.0),  # switch hitter, batting L
            ("R", "S", 40.0),  # switch hitter, batting R
            ("R", "R", None),
            ("L", "L", None),
        ]
        out = self._flip([(i, st, bh, sa) for i, (st, bh, sa) in enumerate(cases)])
        for (stand, _bat_hand, sa), got in zip(cases, out, strict=True):
            exp = _expected(sa, stand)
            if exp is None:
                self.assertIsNone(got)
            else:
                self.assertAlmostEqual(got, exp)


class TestBatSideSemanticsAreDocumentedCorrectly(unittest.TestCase):
    """Guard the canonical definition against re-inversion.

    The inverted claim survived in 13+ places for over a year and was copied
    forward into new code each time. This asserts the one authoritative
    statement stays put and stays right.
    """

    def test_canonical_definition_exists_in_the_postgres_schema(self):
        schema = (
            Path(__file__).resolve().parents[2] / "db" / "schemas" / "01_postgres_schema.sql"
        ).read_text(encoding="utf-8")
        block = re.search(r"CANONICAL DEFINITION(.{0,3000}?)bat_hand\s+CHAR\(1\)", schema, re.S)
        self.assertIsNotNone(block, "the SIM-440 canonical bat_hand/stand note is missing")
        text = block.group(1)
        self.assertIn("ACTUALLY BATTED FROM", text)
        self.assertIn("ROSTER-DECLARED", text)
        # The note must define `stand` first, as the resolved side.
        self.assertLess(
            text.index("stand"),
            text.index("bat_hand"),
            "stand must be defined first, as the per-PA resolved side",
        )


if __name__ == "__main__":
    unittest.main()
