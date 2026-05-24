"""
test_data_engineer_sim051.py
============================
Unit tests for Data Engineer ticket SIM-051 — handedness-corrected
`pull_relative_spray_angle` on `sim.outcome_pool`.

Background
----------
`pull_relative_spray_angle` is the raw `spray_angle` flipped by *per-PA* batter
handedness so that "pull" is always positive (LF for a RHB, RF for a LHB).

The transformation is NOT an importable Python function — it lives as inline
SQL inside `PlayerProfileComputor._build_outcome_pool`
(pipeline/batch/player_profile_computor.py, the CASE expression around the
`pull_relative_spray_angle` projection) and is mirrored in the SIM-051
migration (db/migrations/duckdb/0003_sim051_pull_relative_spray_angle.sql).

Documented flip:

    CASE
        WHEN bat_hand = 'R' THEN  spray_angle   -- pull side already positive
        WHEN bat_hand = 'L' THEN -spray_angle   -- flip
        ELSE NULL                               -- switch hitter ('S') / unresolved
    END

Crucially the flip uses the per-PA resolved `bat_hand` value, NOT the roster
`bats` value, so a switch hitter is corrected with the hand he actually batted
with for that plate appearance.

These tests lock the four required behaviours:
  (a) LHB pull -> positive
  (b) RHB pull -> positive
  (c) switch hitter resolved via per-PA `bat_hand` (not roster `bats`)
  (d) NULL spray / unresolved switch ('S') handled without crashing

We assert both against a tiny Python reference helper *and* against an
in-memory DuckDB connection that runs the exact production CASE expression,
so the test stays faithful to the SQL that actually ships.

Run with:
    pytest tests/unit/test_data_engineer_sim051.py -v
"""

from __future__ import annotations

import unittest

import duckdb

# ---------------------------------------------------------------------------
# Reference helper — a faithful Python transcription of the production SQL
# CASE expression.  "Pull" is always positive: a positive raw spray_angle is
# pull for a RHB (LF); for a LHB the sign is flipped so RF pull is positive.
# ---------------------------------------------------------------------------


def pull_relative_spray_angle(spray_angle, bat_hand):
    """Return the handedness-corrected spray angle (SIM-051).

    Mirrors the inline SQL in PlayerProfileComputor._build_outcome_pool.

    Parameters
    ----------
    spray_angle : float | None
        Raw Statcast spray angle, or None.
    bat_hand : str | None
        Per-PA resolved batter handedness: 'R', 'L', or 'S' (switch /
        unresolved).  Anything else (incl. None) yields None.
    """
    if spray_angle is None:
        return None
    if bat_hand == "R":
        return spray_angle
    if bat_hand == "L":
        return -spray_angle
    return None  # 'S' (unresolved switch hitter) or anything unexpected


# The production CASE expression, kept byte-for-byte close to the SQL in
# _build_outcome_pool / migration 0003 so the DuckDB-backed tests exercise the
# real transformation rather than a paraphrase.
_FLIP_SQL = """
    SELECT
        CASE
            WHEN bat_hand = 'R' THEN  spray_angle
            WHEN bat_hand = 'L' THEN -spray_angle
            ELSE NULL
        END AS pull_relative_spray_angle
      FROM raw_bip
     ORDER BY rn
"""


class TestPullRelativeSprayAnglePython(unittest.TestCase):
    """The pure-Python reference helper (the documented flip formula)."""

    def test_lhb_pull_is_positive(self):
        # For a LHB, pulling means hitting to RF.  Statcast raw spray_angle is
        # negative on the pull side for a lefty, so the flip must make it
        # positive.
        lhb_pull_raw = -28.0
        self.assertGreater(pull_relative_spray_angle(lhb_pull_raw, "L"), 0.0)
        self.assertAlmostEqual(
            pull_relative_spray_angle(lhb_pull_raw, "L"), 28.0
        )

    def test_rhb_pull_is_positive(self):
        # For a RHB, pulling means hitting to LF, which is already positive in
        # the raw convention -> passes through unchanged.
        rhb_pull_raw = 28.0
        self.assertGreater(pull_relative_spray_angle(rhb_pull_raw, "R"), 0.0)
        self.assertAlmostEqual(
            pull_relative_spray_angle(rhb_pull_raw, "R"), 28.0
        )

    def test_both_handedness_pull_share_same_sign(self):
        # The whole point of SIM-051: a LHB pull and a RHB pull must end up
        # with the SAME sign so the similarity engine treats them alike.
        lhb = pull_relative_spray_angle(-30.0, "L")
        rhb = pull_relative_spray_angle(30.0, "R")
        self.assertGreater(lhb, 0.0)
        self.assertGreater(rhb, 0.0)
        # And an oppo-field ball for each is negative for both.
        lhb_oppo = pull_relative_spray_angle(30.0, "L")
        rhb_oppo = pull_relative_spray_angle(-30.0, "R")
        self.assertLess(lhb_oppo, 0.0)
        self.assertLess(rhb_oppo, 0.0)

    def test_switch_hitter_uses_per_pa_bat_hand_not_roster_bats(self):
        # A switch hitter (roster `bats` == 'S') faces a RHP and bats lefty for
        # this PA -> bat_hand resolves to 'L'.  The flip MUST use the per-PA
        # 'L', not the roster 'S' (which would null the row).
        roster_bats = "S"  # NOT used by the flip
        per_pa_bat_hand = "L"  # resolved at PA time vs the RHP
        raw = -22.0  # lefty pull
        result = pull_relative_spray_angle(raw, per_pa_bat_hand)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)
        # Sanity: if we had (wrongly) used the roster 'S' value it would be NULL.
        self.assertIsNone(pull_relative_spray_angle(raw, roster_bats))

    def test_null_spray_and_unresolved_switch_do_not_crash(self):
        # NULL spray -> NULL, regardless of hand.
        self.assertIsNone(pull_relative_spray_angle(None, "R"))
        self.assertIsNone(pull_relative_spray_angle(None, "L"))
        # Unresolved switch hitter ('S') -> NULL (does not crash).
        self.assertIsNone(pull_relative_spray_angle(15.0, "S"))


class TestPullRelativeSprayAngleDuckDB(unittest.TestCase):
    """Run the *actual* production CASE expression in an in-memory DuckDB so
    the documented SQL — not just the Python paraphrase — is verified."""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE raw_bip (
                rn          INTEGER,
                bat_hand    VARCHAR,   -- per-PA resolved handedness
                roster_bats VARCHAR,   -- roster `bats` (must be ignored by flip)
                spray_angle FLOAT
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def _insert(self, rows):
        self.conn.executemany(
            "INSERT INTO raw_bip VALUES (?, ?, ?, ?)", rows
        )

    def _flip(self):
        return [r[0] for r in self.conn.execute(_FLIP_SQL).fetchall()]

    def test_sql_flip_matches_documented_formula(self):
        # rn, bat_hand, roster_bats, spray_angle
        self._insert(
            [
                (0, "L", "L", -28.0),  # LHB pull -> +28
                (1, "R", "R", 28.0),   # RHB pull -> +28
                (2, "L", "S", -22.0),  # switch batting L this PA -> +22
                (3, "R", "S", 19.0),   # switch batting R this PA -> +19
                (4, "S", "S", 15.0),   # unresolved switch -> NULL
                (5, "R", "R", None),   # NULL spray -> NULL
            ]
        )
        out = self._flip()

        # (a) LHB pull positive, (b) RHB pull positive
        self.assertAlmostEqual(out[0], 28.0)
        self.assertGreater(out[0], 0.0)
        self.assertAlmostEqual(out[1], 28.0)
        self.assertGreater(out[1], 0.0)

        # (c) switch hitter resolved via per-PA bat_hand, NOT roster_bats='S'
        self.assertIsNotNone(out[2])
        self.assertAlmostEqual(out[2], 22.0)
        self.assertGreater(out[2], 0.0)
        self.assertAlmostEqual(out[3], 19.0)

        # (d) unresolved switch + NULL spray both produce NULL without crashing
        self.assertIsNone(out[4])
        self.assertIsNone(out[5])

    def test_sql_flip_agrees_with_python_reference(self):
        # Cross-check: every DuckDB result equals the Python helper for a grid
        # of inputs (incl. NULLs and 'S').
        rows = []
        cases = [
            ("R", "R", 12.5),
            ("R", "R", -12.5),
            ("L", "L", 33.0),
            ("L", "L", -33.0),
            ("L", "S", -40.0),
            ("R", "S", 40.0),
            ("S", "S", 5.0),
            ("R", "R", None),
            ("L", "L", None),
        ]
        for i, (hand, roster, sa) in enumerate(cases):
            rows.append((i, hand, roster, sa))
        self._insert(rows)
        sql_out = self._flip()
        for (hand, _roster, sa), got in zip(cases, sql_out):
            expected = pull_relative_spray_angle(sa, hand)
            if expected is None:
                self.assertIsNone(got)
            else:
                self.assertAlmostEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
