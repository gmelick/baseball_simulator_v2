"""SIM-484 — the box-score credits on the dropped-third-strike play.

On a swinging third strike that gets away with first base open or two outs, the
batter reaches first. The loop already put him there; the bookkeeping was wrong:

  * the reach committed to the run ledger as ``field_error``, so the box read a
    reach on an error — no strikeout for the pitcher, and the lane's
    reach-on-error counter counted it. Official scoring (Rule 9.15) credits the
    strikeout; on all 50 of 2025's reaches the official box does;
  * a run the reach forced home paid the batter an RBI. Rule 9.04(a) pays none:
    the run scores on the wild pitch or the passed ball;
  * the batter's own strikeout was never credited, on ANY strikeout. It now goes
    to ``PlayerStatLine.so``. ``k`` is the pitcher's field, and three readers
    tell a pitcher by it, so a batter's strikeout on ``k`` would hand every
    batter who struck out the pitcher props;
  * the "first base open" test read the bases AFTER a steal on the same pitch
    had moved them. A pickoff is thrown before the pitch, so after a pickoff
    the live bases are the bases at the pitch;
  * found on the way: a runner who stole on a got-away third strike that the
    batter could not run on was moved twice (the steal, then the got-away
    advance). The terminal path now keeps the non-terminal path's rule: one
    mover per pitch.

The relabel, the RBI marker and the batter's credit change no run value, out,
score or run allowed. The rule at the pitch and the one-mover guard change the
play itself, on purpose, in the cases they cover: a batter who used to reach is
out, a runner who used to move twice moves once. Every test asserts the score,
the outs and the runs allowed beside the credit it pins.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState, Half
from simulation.prop_distributions import PropDistributionSet
from simulation.sim_loop import STEAL_CAUGHT, STEAL_SAFE, StateMachine, StealResolution
from simulation.synthetic_bundle import fixed_play_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
BATTER = 900
RUNNER = 101


def _state(**kw) -> GameState:
    defaults = {
        "pitcher_id": PITCHER,
        "bat_hand": "R",
        "season": SEASON,
        "batter_id": BATTER,
        "away_lineup": [900 + i for i in range(9)],
        "home_lineup": [800 + i for i in range(9)],
    }
    defaults.update(kw)
    return GameState(**defaults)


def _machine(pitch_outcome: str, *, got_away: bool = True) -> StateMachine:
    """A machine whose every drawn pitch is ``pitch_outcome``; with
    ``got_away`` the drawn row is a real passed ball / wild pitch."""
    art = fixed_play_artifacts("field_out", pitch_model={pitch_outcome: 1.0}, got_away=got_away)
    sm = StateMachine(synthetic_sampler(art, 0), rng=np.random.default_rng(0))
    sm._got_away = True
    return sm


def _steal(runner: int, from_base: int, to_base: int, *, safe: bool) -> StealResolution:
    return StealResolution(
        attempted=True, runner_id=runner, from_base=from_base, to_base=to_base, safe=safe
    )


def _pickoff(runner: int = RUNNER, *, error: bool = False) -> StealResolution:
    """A pickoff throw to first: an out, or (``error``) a throw that gets away
    and moves the runner to second."""
    return StealResolution(attempted=True, runner_id=runner, from_base=1, safe=error, pickoff=True)


class TestTheReachIsAStrikeout:
    """First base open, nobody on: the batter strikes out and reaches first."""

    def _reach(self) -> tuple[StateMachine, GameState, object]:
        sm = _machine("swinging_strike")
        state = _state()
        state.strikes = 2
        result = sm.step_pitch(state)
        return sm, state, result

    def test_the_play_commits_as_a_strikeout(self):
        _sm, state, result = self._reach()
        assert result.pa_terminal and result.event == "strikeout"
        assert result.canonical_event == "strikeout"  # was field_error
        assert result.run_resolution_method == "re24_delta"
        assert not result.is_error
        assert state.bases.first == BATTER
        assert result.baserunner_advances == {BATTER: 1}

    def test_the_pitcher_is_credited_the_strikeout_and_no_out(self):
        sm, state, _result = self._reach()
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1  # was 0
        assert pit.outs_recorded == 0 and state.outs == 0
        assert pit.r_allowed == 0 and pit.er == 0 and pit.h_allowed == 0

    def test_the_batter_is_charged_the_strikeout_and_an_at_bat(self):
        sm, state, _result = self._reach()
        bat = sm.boxscore.line(BATTER)
        assert bat.so == 1
        assert bat.ab == 1 and bat.h == 0 and bat.rbi == 0 and bat.r == 0
        # The pitching field stays the pitcher's: a pure batter leaves it 0.
        assert bat.k == 0
        assert state.away_score == 0 and state.home_score == 0


class TestAnOrdinaryStrikeout:
    def test_the_batter_is_charged_the_strikeout_beside_the_pitcher(self):
        sm = _machine("swinging_strike", got_away=False)
        state = _state()
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.canonical_event == "strikeout" and result.outs_recorded == 1
        assert state.outs == 1 and state.bases.first is None
        bat = sm.boxscore.line(BATTER)
        pit = sm.boxscore.line(PITCHER)
        assert bat.so == 1 and bat.ab == 1 and bat.k == 0
        assert pit.k == 1 and pit.so == 0 and pit.outs_recorded == 1
        assert pit.r_allowed == 0 and state.away_score == 0

    def test_a_batter_who_struck_out_gets_no_pitcher_props(self):
        # The reason the credit lives on ``so``: the prop builder and
        # ``BoxScore.pitchers`` tell a pitcher by ``k``.
        sm = _machine("swinging_strike", got_away=False)
        state = _state()
        state.strikes = 2
        sm.step_pitch(state)
        assert BATTER not in sm.boxscore.pitchers
        assert BATTER in sm.boxscore.batters
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        assert props.get(BATTER, "K") is None
        assert props.get(BATTER, "OUTS") is None
        assert props.get(BATTER, "H") is not None
        pitcher_k = props.get(PITCHER, "K")
        assert pitcher_k is not None and pitcher_k.prob(1) == 1.0


class TestTheForcedRunPaysNoRbi:
    """Bases loaded, two outs: the reach forces the runner on third home."""

    def test_the_run_scores_with_no_rbi_and_the_strikeout_credited(self):
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER, second=202, third=303)
        state.outs = 2
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.canonical_event == "strikeout"
        assert result.runs_scored == 1 and state.away_score == 1
        assert result.steal_runs_scored == 1  # the no-RBI marker
        assert state.outs == 2
        assert state.bases.first == BATTER
        assert state.bases.second == RUNNER
        assert state.bases.third == 202
        bat = sm.boxscore.line(BATTER)
        assert bat.rbi == 0  # Rule 9.04(a); was 1
        assert bat.so == 1 and bat.ab == 1 and bat.h == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 0
        # The run is still allowed and earned (a wild pitch, seven times in ten).
        assert pit.r_allowed == 1 and pit.er == 1
        assert sm.boxscore.line(303).r == 1

    def test_a_steal_of_home_on_the_same_pitch_keeps_its_no_rbi_mark(self):
        # Bases loaded, two outs: the runner on third steals home on a
        # got-away strike three, and the reach then forces nobody home. The
        # reach adds its zero to the steal's mark; it must not overwrite it.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER, second=202, third=303)
        state.outs = 2
        state.strikes = 2
        sm._pending_steal = _steal(303, 3, 4, safe=True)
        result = sm.step_pitch(state)
        assert result.canonical_event == "strikeout"
        assert result.steal_runs_scored == 1
        assert result.runs_scored == 1 and state.away_score == 1
        assert state.outs == 2
        assert (state.bases.first, state.bases.second, state.bases.third) == (BATTER, RUNNER, 202)
        bat = sm.boxscore.line(BATTER)
        assert bat.rbi == 0 and bat.so == 1
        assert sm.boxscore.line(303).r == 1 and sm.boxscore.line(303).sb == 1
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.r_allowed == 1 and pit.er == 1


class TestTheRuleReadsTheBasesAtThePitch:
    """A steal runs ON the pitch and resolves before the strikeout. The
    dropped-third-strike rule reads first base and the outs as they were
    when the pitch was thrown."""

    def test_a_steal_of_second_under_two_outs_leaves_the_batter_out(self):
        # One out, a runner on first steals second on a got-away strike
        # three. At the pitch first base was occupied under two outs: the
        # batter is out. The loop used to see first base empty and let him
        # reach. Now the batter is out, and the one-mover guard keeps the
        # got-away from carrying the runner on to third.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 1
        state.strikes = 2
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        result = sm.step_pitch(state)
        assert result.steal_outcome == STEAL_SAFE
        assert result.outs_recorded == 1 and state.outs == 2
        assert state.bases.first is None  # the batter did not reach
        assert state.bases.second == RUNNER  # the steal stands
        assert state.bases.third is None  # no second move
        assert BATTER not in result.baserunner_advances
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 1 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1
        assert sm.boxscore.line(RUNNER).sb == 1
        assert state.away_score == 0

    def test_a_caught_stealing_under_two_outs_makes_the_batter_the_third_out(self):
        # One out, the runner is caught stealing on the same pitch (the
        # second out). At the pitch: not eligible, so the batter is the third
        # out. The loop used to see two outs and first base empty.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 1
        state.strikes = 2
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=False)
        result = sm.step_pitch(state)
        assert result.steal_outcome == STEAL_CAUGHT
        assert result.outs_recorded == 2
        assert BATTER not in result.baserunner_advances
        # The half-inning rolled: the bottom of the first, bases clear.
        assert state.half == Half.BOTTOM and state.outs == 0
        assert state.bases.occupancy == (False, False, False)
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 2 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1
        assert sm.boxscore.line(RUNNER).cs == 1
        assert state.away_score == 0

    def test_a_steal_of_second_with_two_outs_lets_the_batter_reach(self):
        # Two outs at the pitch: eligible whatever first base holds.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 2
        state.strikes = 2
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        result = sm.step_pitch(state)
        assert result.canonical_event == "strikeout" and result.outs_recorded == 0
        assert state.outs == 2
        assert state.bases.first == BATTER
        assert state.bases.second == RUNNER
        assert state.bases.third is None
        assert result.runs_scored == 0 and state.away_score == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 0 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1

    def test_after_a_pickoff_the_live_bases_are_the_bases_at_the_pitch(self):
        # A pickoff is thrown BEFORE the pitch. One out, the runner on first
        # is picked off (the second out); the pitch that follows sees first
        # base empty and two outs, so the batter may run.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 1
        state.strikes = 2
        sm._pending_steal = _pickoff()
        result = sm.step_pitch(state)
        assert result.pickoff_out and not result.steal_attempted
        assert result.outs_recorded == 1 and state.outs == 2
        assert state.bases.first == BATTER
        assert RUNNER not in result.baserunner_advances
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 1 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1
        assert state.away_score == 0

    def test_after_a_pickoff_error_first_base_is_open_at_the_pitch(self):
        # One out, the pickoff throw gets away and the runner takes second,
        # before the pitch. The pitch that follows sees first base empty, so
        # the batter may run on the got-away strike three.
        sm = _machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.outs = 1
        state.strikes = 2
        sm._pending_steal = _pickoff(error=True)
        result = sm.step_pitch(state)
        assert result.pickoff_error and not result.steal_attempted
        assert result.canonical_event == "strikeout" and result.outs_recorded == 0
        assert state.outs == 1
        assert state.bases.first == BATTER and state.bases.second == RUNNER
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 0 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1
        assert state.away_score == 0


class TestOneMoverPerPitch:
    """Found on the way: the terminal got-away advance had no guard."""

    def test_a_runner_who_stole_on_a_got_away_strike_three_moves_once(self):
        # A called third strike cannot be a dropped-third-strike reach, so
        # this isolates the guard: no steal, the got-away would move the
        # runner one base; with the steal, the steal is the one mover.
        sm = _machine("called_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        result = sm.step_pitch(state)
        assert result.canonical_event == "strikeout" and state.outs == 1
        assert state.bases.second == RUNNER  # not third
        assert state.bases.first is None and state.bases.third is None
        assert result.baserunner_advances == {RUNNER: 2}
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 1 and pit.r_allowed == 0
        assert sm.boxscore.line(BATTER).so == 1
        assert state.away_score == 0

    def test_after_a_pickoff_out_the_got_away_moves_nobody(self):
        # Runners on first and second, nobody out. The runner on first is
        # picked off; the called third strike then gets away. The pickoff
        # owns this pitch's baserunning, so the runner on second holds.
        sm = _machine("called_strike")
        state = _state()
        state.bases = Bases(first=RUNNER, second=202)
        state.strikes = 2
        sm._pending_steal = _pickoff()
        result = sm.step_pitch(state)
        assert result.pickoff_out
        assert state.outs == 2 and result.outs_recorded == 2
        assert state.bases.second == 202 and state.bases.third is None
        assert state.bases.first is None
        assert 202 not in result.baserunner_advances
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 2 and pit.r_allowed == 0
        assert state.away_score == 0

    def test_after_a_pickoff_error_the_got_away_moves_nobody_again(self):
        # The pickoff throw gets away and the runner takes second; the called
        # third strike then gets away too. The runner moves once.
        sm = _machine("called_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm._pending_steal = _pickoff(error=True)
        result = sm.step_pitch(state)
        assert result.pickoff_error
        assert state.outs == 1
        assert state.bases.second == RUNNER and state.bases.third is None
        assert result.baserunner_advances == {RUNNER: 2}
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1 and pit.outs_recorded == 1 and pit.r_allowed == 0
        assert state.away_score == 0

    def test_without_a_steal_the_got_away_still_advances_the_runner(self):
        sm = _machine("called_strike")
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert state.outs == 1
        assert state.bases.second == RUNNER
        assert result.baserunner_advances == {RUNNER: 2}
        assert sm.boxscore.line(PITCHER).k == 1
