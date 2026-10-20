"""SIM-364 tests: winning / losing / save pitcher attribution.

Builds small synthetic ``PlayResult`` streams whose ``next_state`` carries the
committed inning/half/scores and the fielding-side ``pitcher_id`` (the pitcher of
record for the defending team), then asserts the derived W/L/Save decisions.

Pitcher-id convention used by these fixtures (purely for readability):
  * HOME pitchers: 100 (starter), 101 (reliever/closer)
  * AWAY pitchers: 200 (starter), 201 (reliever)
Recall: in ``Half.TOP`` the HOME pitcher throws (away bats); in ``Half.BOTTOM``
the AWAY pitcher throws (home bats).
"""

from __future__ import annotations

import unittest

from simulation.game_state import GameState, Half, PlayResult
from simulation.pitcher_decisions import (
    SAVE_LEAD_CEILING,
    STARTER_WIN_MIN_OUTS,
    PitcherDecisions,
    decisions_from_plays,
)


def _state(*, inning: int, half: Half, home: int, away: int, pitcher_id: int) -> GameState:
    """A minimal committed GameState: the fields the decision derivation reads."""
    return GameState(
        pitcher_id=pitcher_id,
        bat_hand="R",
        season=2024,
        inning=inning,
        half=half,
        home_score=home,
        away_score=away,
    )


def _play(
    *,
    inning: int,
    half: Half,
    home: int,
    away: int,
    pitcher_id: int,
    outs_recorded: int = 0,
) -> PlayResult:
    """A PlayResult carrying only the committed next_state we care about.

    ``outs_recorded`` defaults to 0 (the SIM-364 test pattern); SIM-414 tests
    set it to drive the starter-5-IP rule.
    """
    return PlayResult(
        pitch_outcome="in_play",
        outs_recorded=outs_recorded,
        next_state=_state(inning=inning, half=half, home=home, away=away, pitcher_id=pitcher_id),
    )


class TestCleanWin(unittest.TestCase):
    """One pitcher per side, home wins comfortably from the first run on."""

    def _stream(self) -> list[PlayResult]:
        # Top 1: home pitcher (100) fields, no scoring.
        # Bottom 1: away pitcher (200) fields, home scores 2 -> permanent lead.
        # Top/Bottom 2..3: stays 2-0, complete game.
        return [
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100),
            _play(inning=1, half=Half.BOTTOM, home=2, away=0, pitcher_id=200),
            _play(inning=2, half=Half.TOP, home=2, away=0, pitcher_id=100),
            _play(inning=2, half=Half.BOTTOM, home=2, away=0, pitcher_id=200),
            _play(inning=9, half=Half.TOP, home=2, away=0, pitcher_id=100),
        ]

    def test_win_and_loss(self):
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.winning_pitcher_id, 100)  # home starter
        self.assertEqual(d.losing_pitcher_id, 200)  # away starter allowed the lead
        self.assertEqual(d.home_score, 2)
        self.assertEqual(d.away_score, 0)

    def test_complete_game_no_save(self):
        # Finisher (home 100) is also the winner -> no save.
        d = decisions_from_plays(self._stream())
        self.assertIsNone(d.save_pitcher_id)

    def test_returns_dataclass(self):
        self.assertIsInstance(decisions_from_plays(self._stream()), PitcherDecisions)


class TestCloseGameWithSave(unittest.TestCase):
    """Home starter earns the win; a reliever finishes protecting a 1-run lead."""

    def _stream(self) -> list[PlayResult]:
        return [
            # Bottom 1: away starter 200 fields, home takes a permanent 1-0 lead.
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200),
            # Innings with the home STARTER (100) on the mound, lead holds 1-0.
            _play(inning=2, half=Half.TOP, home=1, away=0, pitcher_id=100),
            _play(inning=7, half=Half.TOP, home=1, away=0, pitcher_id=100),
            # 9th: home reliever/closer (101) comes in, still 1-0, closes it out.
            _play(inning=9, half=Half.TOP, home=1, away=0, pitcher_id=101),
        ]

    def test_win_to_starter(self):
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.winning_pitcher_id, 100)

    def test_save_to_reliever(self):
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.save_pitcher_id, 101)

    def test_loss_to_away_starter(self):
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.losing_pitcher_id, 200)


class TestBlowoutNoSave(unittest.TestCase):
    """Reliever finishes but the lead is never within the save ceiling."""

    def _stream(self) -> list[PlayResult]:
        big = SAVE_LEAD_CEILING + 5  # lead far beyond the save ceiling
        return [
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100),
            _play(inning=1, half=Half.BOTTOM, home=big, away=0, pitcher_id=200),
            # Home starter 100 holds the big lead.
            _play(inning=2, half=Half.TOP, home=big, away=0, pitcher_id=100),
            # Mop-up reliever 101 finishes, lead still huge -> no save.
            _play(inning=9, half=Half.TOP, home=big, away=0, pitcher_id=101),
        ]

    def test_no_save_in_blowout(self):
        d = decisions_from_plays(self._stream())
        self.assertIsNone(d.save_pitcher_id)
        self.assertEqual(d.winning_pitcher_id, 100)
        self.assertEqual(d.losing_pitcher_id, 200)


class TestLeadChangeFollowsPermanentLead(unittest.TestCase):
    """An early lead is relinquished; W/L follow the FINAL permanent lead."""

    def _stream(self) -> list[PlayResult]:
        return [
            # Top 1: away takes an early 2-0 lead off home starter 100.
            _play(inning=1, half=Half.TOP, home=0, away=2, pitcher_id=100),
            # Bottom 1: home pecks back to 1-2 off away starter 200.
            _play(inning=1, half=Half.BOTTOM, home=1, away=2, pitcher_id=200),
            # Top 5: away still up 2-1; HOME reliever 101 now pitching.
            _play(inning=5, half=Half.TOP, home=1, away=2, pitcher_id=101),
            # Bottom 5: home rallies to 3-2 -> permanent lead, off away RELIEVER 201.
            _play(inning=5, half=Half.BOTTOM, home=3, away=2, pitcher_id=201),
            # Bottom 8: lead holds 3-2.
            _play(inning=8, half=Half.BOTTOM, home=3, away=2, pitcher_id=201),
            # Top 9: home reliever 101 finishes 3-2.
            _play(inning=9, half=Half.TOP, home=3, away=2, pitcher_id=101),
        ]

    def test_win_to_home_reliever_on_mound_at_permanent_lead(self):
        # Home took the permanent lead in the bottom of the 5th; the home pitcher
        # of record at that moment is the reliever 101 (entered in the top of 5).
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.winning_pitcher_id, 101)

    def test_loss_to_away_reliever_who_allowed_the_lead(self):
        # The away pitcher who allowed the lead-taking run in the bottom 5th is
        # the away reliever 201.
        d = decisions_from_plays(self._stream())
        self.assertEqual(d.losing_pitcher_id, 201)

    def test_save_none_when_finisher_is_winner(self):
        # The home reliever 101 is BOTH the pitcher of record at the permanent
        # lead AND the finisher -> wins, no save.
        d = decisions_from_plays(self._stream())
        self.assertIsNone(d.save_pitcher_id)
        self.assertEqual(d.home_score, 3)
        self.assertEqual(d.away_score, 2)


class TestTieAndUnfinished(unittest.TestCase):
    def test_tie_no_decision(self):
        stream = [
            _play(inning=9, half=Half.TOP, home=3, away=3, pitcher_id=100),
            _play(inning=9, half=Half.BOTTOM, home=3, away=3, pitcher_id=200),
        ]
        d = decisions_from_plays(stream)
        self.assertIsNone(d.winning_pitcher_id)
        self.assertIsNone(d.losing_pitcher_id)
        self.assertIsNone(d.save_pitcher_id)
        self.assertEqual(d.home_score, 3)
        self.assertEqual(d.away_score, 3)

    def test_empty_stream(self):
        d = decisions_from_plays([])
        self.assertIsInstance(d, PitcherDecisions)
        self.assertIsNone(d.winning_pitcher_id)
        self.assertIsNone(d.losing_pitcher_id)
        self.assertIsNone(d.save_pitcher_id)
        self.assertEqual(d.home_score, 0)
        self.assertEqual(d.away_score, 0)

    def test_plays_without_next_state_are_skipped(self):
        # A leading None-next_state play must not crash; only committed states count.
        stream = [
            PlayResult(pitch_outcome="ball"),  # no next_state
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200),
            _play(inning=9, half=Half.TOP, home=1, away=0, pitcher_id=100),
        ]
        d = decisions_from_plays(stream)
        self.assertEqual(d.winning_pitcher_id, 100)
        self.assertEqual(d.losing_pitcher_id, 200)


class TestWalkOff(unittest.TestCase):
    """Home wins in its last at-bat (walk-off) — the decisive lead-taking play."""

    def _stream(self) -> list[PlayResult]:
        return [
            # Tied 2-2 into the bottom of the 9th.
            _play(inning=9, half=Half.TOP, home=2, away=2, pitcher_id=100),
            # Bottom 9: home walks it off 3-2 off the away reliever 201.
            _play(inning=9, half=Half.BOTTOM, home=3, away=2, pitcher_id=201),
        ]

    def test_walkoff_win_and_loss(self):
        d = decisions_from_plays(self._stream())
        # Home pitcher of record at the walk-off (last fielding play was top 9 -> 100).
        self.assertEqual(d.winning_pitcher_id, 100)
        # Away reliever 201 allowed the walk-off run.
        self.assertEqual(d.losing_pitcher_id, 201)
        self.assertEqual(d.home_score, 3)
        self.assertEqual(d.away_score, 2)


class TestSim414StarterMinIP(unittest.TestCase):
    """SIM-414: MLB Rule 9.17(b) — a starter must pitch 5 IP (15 outs) to win.

    Below that, the win is reassigned to the most-effective reliever (we
    approximate "most effective" by outs recorded; ties break by first
    appearance).  The starter still loses (the symmetric rule does not apply
    to the losing pitcher), and the save chain runs against the REASSIGNED
    winner.
    """

    def test_starter_with_4_2_ip_loses_the_win_to_long_relief(self):
        # Home starter (100) is the pitcher of record when home takes the
        # permanent lead in B1, but exits after recording only 14 outs (4.2 IP).
        # Long reliever 101 records 13 outs over the rest of the game and is the
        # most-effective candidate -> wins.
        stream = [
            # B1 home goes ahead 1-0 (lead taken by starter 100).  Starter
            # subsequently records 14 outs across innings 1-5.
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200, outs_recorded=3),
            _play(inning=2, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=3, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=4, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=2),
            # Long reliever 101 takes over, finishes 13 outs.
            _play(inning=5, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=3),
            _play(inning=6, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=3),
            _play(inning=7, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=3),
            _play(inning=8, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=3),
            _play(inning=9, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=1),
        ]
        d = decisions_from_plays(stream)
        self.assertEqual(d.winning_pitcher_id, 101)  # reassigned away from starter
        self.assertEqual(d.losing_pitcher_id, 200)  # losing-side rule unchanged

    def test_starter_with_5_ip_keeps_the_win(self):
        # The MIN_OUTS threshold is exactly 15; a starter who records 15 outs
        # qualifies for the win.
        stream = [
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200, outs_recorded=3),
            _play(inning=2, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=3, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=4, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=5, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            # Reliever 101 finishes the remaining outs.
            _play(inning=6, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=3),
            _play(inning=9, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=9),
        ]
        d = decisions_from_plays(stream)
        self.assertEqual(d.winning_pitcher_id, 100)  # starter qualifies (>=15 outs)

    def test_starter_under_5_ip_with_no_eligible_reliever_keeps_win(self):
        # The starter goes 4.2 (14 outs) but no reliever records any outs (the
        # synthetic stream cuts off there).  Defensive fallback: don't reassign.
        stream = [
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200, outs_recorded=3),
            _play(inning=2, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=3, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=4, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=5, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=2),
        ]
        d = decisions_from_plays(stream)
        self.assertEqual(d.winning_pitcher_id, 100)

    def test_reassigned_win_can_invalidate_a_save(self):
        # If the reliever-now-winner is ALSO the finisher who would otherwise
        # earn a save, no save is awarded (the same pitcher can't get both).
        stream = [
            # B1 home takes 1-0 lead off the starter 100.
            _play(inning=1, half=Half.TOP, home=0, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=1, half=Half.BOTTOM, home=1, away=0, pitcher_id=200, outs_recorded=3),
            # Starter exits after 4.2 IP (14 outs).
            _play(inning=2, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=3, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=3),
            _play(inning=4, half=Half.TOP, home=1, away=0, pitcher_id=100, outs_recorded=2),
            # Reliever 101 finishes (13 outs); save situation (lead <=3).
            _play(inning=5, half=Half.TOP, home=1, away=0, pitcher_id=101, outs_recorded=13),
        ]
        d = decisions_from_plays(stream)
        # Reliever 101 wins -> can NOT also save.
        self.assertEqual(d.winning_pitcher_id, 101)
        self.assertIsNone(d.save_pitcher_id)

    def test_starter_min_outs_constant_is_15(self):
        # Defensive: the constant ties this test file to the rule it documents.
        self.assertEqual(STARTER_WIN_MIN_OUTS, 15)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
