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


def _play(*, inning: int, half: Half, home: int, away: int, pitcher_id: int) -> PlayResult:
    """A PlayResult carrying only the committed next_state we care about."""
    return PlayResult(
        pitch_outcome="in_play",
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
