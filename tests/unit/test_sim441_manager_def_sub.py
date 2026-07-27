"""
SIM-441 — `n_def_sub_late` must count plate appearances, not pitch rows.
=======================================================================

`defensive_sub` is a per-pitch column on ``raw.pitches``, but how MANY pitch rows
carry the flag is a property of the ETL parser, not of the manager:

  * before SIM-440 the parser broadcast the flag to every pitch of the plate
    appearance (~4 rows per substitution);
  * it now latches the flag onto exactly one pitch.

``fld_agg`` summed rows, so ``defensive_sub_rate_late_innings`` — the manager
engine's heaviest feature at weight **0.550** — was inflated by roughly the
average pitches-per-PA, and would have *silently rescaled* the moment the
corrective reload sweep ran.

Worse, the sweep rewrites games one at a time, so mid-sweep the corpus holds BOTH
conventions simultaneously. A row-counting aggregate would then mix two
incompatible units inside a single column, which is why this has to land BEFORE
the reload rather than after.

These tests execute the CASE/COUNT expression extracted from the LIVE production
source against an in-memory DuckDB, so reverting the query turns them red.
"""

from __future__ import annotations

import inspect
import unittest

import duckdb


def _fld_agg_sql() -> str:
    """The `fld_agg` CTE body, taken from the production source, comments stripped."""
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    src = inspect.getsource(PlayerProfileComputor._compute_manager_profiles)
    assert "fld_agg AS (" in src, "fld_agg CTE moved — retarget this test"
    body = src[src.index("fld_agg AS (") :]
    body = body[: body.index("\n            ),")]
    # Strip SQL comment lines: the block above this expression explains the old
    # bug and therefore contains the words this test asserts on.
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("--"))


class TestProductionExpressionShape(unittest.TestCase):
    def test_counts_distinct_plate_appearances(self):
        sql = _fld_agg_sql()
        self.assertIn("COUNT(DISTINCT", sql)
        self.assertIn("at_bat_number", sql, "the PA must be part of the distinct key")

    def test_does_not_sum_rows(self):
        sql = _fld_agg_sql()
        self.assertNotIn(
            "SUM(CASE WHEN defensive_sub",
            sql,
            "summing rows makes the feature depend on how many pitch rows the parser "
            "flags — which changed in SIM-440 and changes again per-game during a "
            "partial reload sweep",
        )


class TestInvariantToParserBroadcastSemantics(unittest.TestCase):
    """The number the query produces must not depend on the parser convention."""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE p (
                game_pk        INTEGER,
                at_bat_number  INTEGER,
                pitch_number   INTEGER,
                season         SMALLINT,
                inning         SMALLINT,
                defensive_sub  BOOLEAN,
                fld_mgr        INTEGER
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def _run(self, rows) -> int:
        self.conn.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        # Splice the production CTE body into a runnable query.
        body = _fld_agg_sql()
        select = body[body.index("SELECT") :]
        out = self.conn.execute(f"WITH fld_agg AS ({select}) SELECT n_def_sub_late FROM fld_agg")
        return out.fetchone()[0]

    @staticmethod
    def _pa(game, ab, n_pitches, flagged_pitches, inning=8):
        """One PA of `n_pitches`, with `flagged_pitches` of them carrying the flag."""
        return [(game, ab, i + 1, 2024, inning, i < flagged_pitches, 500) for i in range(n_pitches)]

    def test_broadcast_and_latched_agree(self):
        """4 flagged rows (old parser) and 1 flagged row (new parser) both = 1 sub."""
        old = self._run(self._pa(745001, 1, n_pitches=4, flagged_pitches=4))
        self.setUp()  # fresh table
        new = self._run(self._pa(745001, 1, n_pitches=4, flagged_pitches=1))
        self.assertEqual(old, 1)
        self.assertEqual(new, 1)
        self.assertEqual(old, new, "the feature must not depend on the parser convention")

    def test_a_half_reloaded_corpus_is_still_correct(self):
        """The mid-sweep state: some games broadcast, some latched.

        Three distinct late-inning substitutions across three games, recorded
        under different conventions, must total exactly 3.
        """
        rows = (
            self._pa(745001, 1, n_pitches=5, flagged_pitches=5)  # not yet reloaded
            + self._pa(745002, 1, n_pitches=6, flagged_pitches=1)  # reloaded
            + self._pa(745003, 1, n_pitches=3, flagged_pitches=3)  # not yet reloaded
        )
        self.assertEqual(self._run(rows), 3)

    def test_two_substitutions_in_one_game_count_twice(self):
        rows = self._pa(745001, 1, n_pitches=4, flagged_pitches=1) + self._pa(
            745001, 7, n_pitches=4, flagged_pitches=1
        )
        self.assertEqual(self._run(rows), 2)

    def test_mid_pa_substitution_is_not_dropped(self):
        """A defensive sub can land on a later pitch of the PA.

        This is why the fix counts distinct PAs over the per-pitch CTE rather
        than aggregating over `pa`, which keeps only the first pitch.
        """
        rows = [
            (745001, 1, 1, 2024, 8, False, 500),
            (745001, 1, 2, 2024, 8, False, 500),
            (745001, 1, 3, 2024, 8, True, 500),  # sub landed mid-PA
        ]
        self.assertEqual(self._run(rows), 1)

    def test_early_innings_are_excluded(self):
        rows = self._pa(745001, 1, n_pitches=4, flagged_pitches=4, inning=3)
        self.assertEqual(self._run(rows), 0)

    def test_no_substitutions_is_zero_not_null(self):
        rows = self._pa(745001, 1, n_pitches=4, flagged_pitches=0)
        self.assertEqual(self._run(rows), 0)


class TestRateDenominatorUnchanged(unittest.TestCase):
    def test_rate_is_still_per_game(self):
        """n_def_sub_late / n_fld_games — the numerator's unit changed, so confirm
        the denominator is still games, not PAs or pitches."""
        from pipeline.batch.player_profile_computor import PlayerProfileComputor

        src = inspect.getsource(PlayerProfileComputor._compute_manager_profiles)
        line = next(
            ln for ln in src.splitlines() if "defensive_sub_rate_late_innings" in ln and "/" in ln
        )
        self.assertRegex(line, r"n_def_sub_late\s*\*\s*1\.0\s*/\s*NULLIF\(\s*f\.n_fld_games")
        self.assertIn(
            "COUNT(DISTINCT game_pk)",
            src,
            "n_fld_games must remain a distinct-game count",
        )


if __name__ == "__main__":
    unittest.main()
