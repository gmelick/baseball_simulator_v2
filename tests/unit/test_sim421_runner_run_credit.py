"""SIM-421 — the per-runner R credit on a terminal pitch.

The runs-scored prop (``R``) and the hits + runs + RBI prop (``HRR``) read
``PlayerStatLine.r``. Four paths in the loop wrote a wrong ``r`` on a pitch
that ends the plate appearance:

  * a PICKOFF OUT wrote ``baserunner_advances[runner] = 0`` for the retired
    runner. ``_accumulate_pa`` reads 0 as "scored", and a pickoff never sets
    ``steal_attempted`` (the guard the caught-stealing path relied on), so the
    picked-off runner was credited a run;
  * a GOT-AWAY pitch on strike three credited the scorer's ``r`` in
    ``_resolve_got_away_advance`` AND wrote the 0 entry, so ``_accumulate_pa``
    credited the same run again;
  * a STEAL on the same pitch as a scoring play skipped the whole credit
    loop (the guard meant only to protect a steal of home from a second
    credit), so a safe steal to 2B plus a home run credited nobody a run.
    ``_resolve_steal_outcome`` now records the steal-of-home runner in
    ``PlayResult.box_run_credited`` and ``_accumulate_pa`` skips exactly him;
  * a DROPPED THIRD STRIKE with the bases loaded and two outs forced the
    runner on 3B home but ``_force_on_reach`` wrote only the batter's
    entry, so the forced run reached the team score and never the runner's
    line. It now records every forced runner the way ``_resolve_walk`` does
    (the fourth path; found by the review's skeptics).

The team score was right every time; only the per-runner line was off, so
the acceptance lane's R band never saw it. The harness mirrors
``test_sim483_steal_run_credit`` (a steal or pickoff staged directly) and
``test_backend_sim319`` (a play drawn from the synthetic bundle).
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState
from simulation.prop_distributions import PropDistributionSet
from simulation.sim_loop import StateMachine, StealResolution
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


def _pickoff(runner: int = RUNNER, *, advancing: bool = False) -> StealResolution:
    return StealResolution(
        attempted=True,
        runner_id=runner,
        from_base=1,
        safe=False,
        pickoff=True,
        pickoff_advancing=advancing,
    )


def _got_away_machine(pitch_outcome: str) -> StateMachine:
    """A machine whose every drawn pitch is ``pitch_outcome`` and got away."""
    art = fixed_play_artifacts("field_out", pitch_model={pitch_outcome: 1.0}, got_away=True)
    sm = StateMachine(synthetic_sampler(art, 0), rng=np.random.default_rng(0))
    sm._got_away = True
    return sm


def _in_play_machine(event: str) -> StateMachine:
    """A machine whose every pitch is put in play for the canonical ``event``."""
    is_air = event == "home_run"
    art = fixed_play_artifacts(event, is_air=is_air, pitch_model={"in_play": 1.0})
    return StateMachine(synthetic_sampler(art, 0), rng=np.random.default_rng(0))


def _steal(runner: int, from_base: int, to_base: int, *, safe: bool) -> StealResolution:
    return StealResolution(
        attempted=True, runner_id=runner, from_base=from_base, to_base=to_base, safe=safe
    )


class TestPickoffOutOnATerminalPitch:
    def test_a_plain_pickoff_before_strike_three_credits_no_run(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm._pending_steal = _pickoff()
        result = sm.step_pitch(state, pitch_outcome="called_strike")
        assert result.pickoff_out and not result.steal_attempted
        assert result.runs_scored == 0 and state.away_score == 0
        assert state.outs == 2 and state.bases.first is None
        # The retired runner has NO advances entry: 0 means "scored" to the
        # accumulator, and he did not score.
        assert RUNNER not in result.baserunner_advances
        assert sm.boxscore.line(RUNNER).r == 0

    def test_an_advancing_pickoff_before_strike_three_is_a_cs_and_no_run(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm._pending_steal = _pickoff(advancing=True)
        sm.step_pitch(state, pitch_outcome="called_strike")
        line = sm.boxscore.line(RUNNER)
        assert line.cs == 1 and line.sb == 0
        assert line.r == 0

    def test_a_pickoff_on_a_non_terminal_pitch_credits_no_run(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm._pending_steal = _pickoff()
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.pickoff_out and not result.pa_terminal
        assert sm.boxscore is None or sm.boxscore.line(RUNNER).r == 0

    def test_the_picked_off_runner_posts_no_r_or_hrr_prop(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(first=RUNNER)
        state.strikes = 2
        sm._pending_steal = _pickoff()
        sm.step_pitch(state, pitch_outcome="called_strike")
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        # A line with no batting event at all posts nothing; a phantom run
        # used to post R {1: 1.0} and HRR {1: 1.0} for a runner who was out.
        for prop in ("R", "HRR"):
            dist = props.get(RUNNER, prop)
            assert dist is None or dist.prob(1) == 0.0


class TestGotAwayOnStrikeThree:
    def test_a_called_strike_three_got_away_credits_the_scorer_once(self):
        sm = _got_away_machine("called_strike")
        state = _state()
        state.bases = Bases(third=303)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.pa_terminal and result.event == "strikeout"
        assert result.runs_scored == 1 and state.away_score == 1
        assert state.outs == 1
        assert sm.boxscore.line(303).r == 1
        # The rest of the run's accounting stays as before.
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1 and pit.er == 1
        assert sm.boxscore.line(BATTER).rbi == 0  # no RBI on a got-away run

    def test_a_swinging_strike_three_with_first_occupied_credits_once(self):
        # First base occupied under two outs: the dropped-third-strike reach
        # does not apply, the ball still got away, the runner on third scores.
        sm = _got_away_machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=101, third=303)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.pa_terminal and result.runs_scored == 1
        assert state.outs == 1
        assert sm.boxscore.line(303).r == 1

    def test_a_non_terminal_got_away_still_credits_the_scorer(self):
        # A got-away ball never reaches ``_accumulate_pa``, so the resolver's
        # own credit is the only one — the guard must not remove it.
        sm = _got_away_machine("ball")
        state = _state()
        state.bases = Bases(third=303)
        result = sm.step_pitch(state)
        assert not result.pa_terminal and result.runs_scored == 1
        assert sm.boxscore.line(303).r == 1
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1 and pit.er == 1

    def test_the_scorer_posts_r_and_hrr_of_one(self):
        sm = _got_away_machine("called_strike")
        state = _state()
        state.bases = Bases(third=303)
        state.strikes = 2
        sm.step_pitch(state)
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        for prop in ("R", "HRR"):
            dist = props.get(303, prop)
            assert dist is not None
            assert dist.prob(1) == 1.0 and dist.prob(2) == 0.0


class TestStealOnAScoringPitch:
    """A steal resolves on the pitch; the pitch can also end the plate
    appearance with a scoring play. Every scorer is credited once."""

    def test_a_safe_steal_and_a_home_run_credit_both_scorers(self):
        sm = _in_play_machine("home_run")
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        result = sm.step_pitch(state)
        assert result.steal_attempted and result.event == "home_run"
        assert result.runs_scored == 2 and state.away_score == 2
        assert result.baserunner_advances == {RUNNER: 0, BATTER: 0}
        # The old guard skipped the loop on any steal pitch: both read 0.
        assert sm.boxscore.line(RUNNER).r == 1
        assert sm.boxscore.line(BATTER).r == 1
        assert sm.boxscore.line(BATTER).rbi == 2
        assert sm.boxscore.line(RUNNER).sb == 1
        # The team, pitcher and out accounting are unchanged by the fix.
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 2 and pit.er == 2
        assert state.outs == 0

    def test_a_safe_steal_and_a_single_that_scores_nobody_credits_no_run(self):
        sm = _in_play_machine("single")
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        result = sm.step_pitch(state)
        assert result.steal_attempted and result.event == "single"
        assert result.runs_scored == 0 and state.away_score == 0
        # The runner took second on the steal, then third on the single.
        assert state.bases.third == RUNNER and state.bases.first == BATTER
        assert sm.boxscore.line(RUNNER).r == 0
        assert sm.boxscore.line(BATTER).r == 0

    def test_a_steal_of_home_on_a_terminal_walk_is_credited_once(self):
        # The case the old whole-loop skip protected: the steal-of-home
        # runner is credited in ``_resolve_steal_outcome`` AND reads 0 in
        # ``baserunner_advances`` when ``_accumulate_pa`` runs.
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(third=303)
        state.balls = 3
        sm._pending_steal = _steal(303, 3, 4, safe=True)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert result.pa_terminal and result.event == "walk"
        assert result.steal_runs_scored == 1
        assert result.box_run_credited == {303}
        assert result.runs_scored == 1 and state.away_score == 1
        assert sm.boxscore.line(303).r == 1
        assert sm.boxscore.line(BATTER).rbi == 0  # Rule 9.04(b)
        assert sm.boxscore.line(BATTER).r == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1 and pit.er == 1

    def test_a_steal_of_home_and_a_home_run_credit_each_scorer_once(self):
        sm = _in_play_machine("home_run")
        state = _state()
        state.bases = Bases(third=303)
        sm._pending_steal = _steal(303, 3, 4, safe=True)
        result = sm.step_pitch(state)
        assert result.event == "home_run"
        assert result.runs_scored == 2 and state.away_score == 2
        assert sm.boxscore.line(303).r == 1  # not 2
        assert sm.boxscore.line(BATTER).r == 1
        assert sm.boxscore.line(BATTER).rbi == 1  # the steal run earns none
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 2 and pit.er == 2

    def test_a_caught_stealing_and_a_single_credit_the_other_scorer(self):
        sm = _in_play_machine("single")
        state = _state()
        state.bases = Bases(first=RUNNER, third=303)
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=False)
        result = sm.step_pitch(state)
        assert result.steal_attempted and result.event == "single"
        assert state.outs == 1
        assert result.runs_scored == 1 and state.away_score == 1
        # The caught runner has NO advances entry (0 means "scored").
        assert RUNNER not in result.baserunner_advances
        assert result.baserunner_advances[303] == 0
        line = sm.boxscore.line(RUNNER)
        assert line.cs == 1 and line.r == 0
        assert sm.boxscore.line(303).r == 1
        assert sm.boxscore.line(BATTER).rbi == 1
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 1 and pit.r_allowed == 1 and pit.er == 1

    def test_a_non_terminal_steal_of_home_is_still_credited(self):
        # A non-terminal pitch never reaches ``_accumulate_pa``: the resolver's
        # own credit must stay the one credit.
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _state()
        state.bases = Bases(third=303)
        sm._pending_steal = _steal(303, 3, 4, safe=True)
        result = sm.step_pitch(state, pitch_outcome="ball")
        assert not result.pa_terminal
        assert result.box_run_credited == {303}
        assert sm.boxscore.line(303).r == 1

    def test_the_steal_pitch_scorers_post_r_and_hrr_props(self):
        sm = _in_play_machine("home_run")
        state = _state()
        state.bases = Bases(first=RUNNER)
        sm._pending_steal = _steal(RUNNER, 1, 2, safe=True)
        sm.step_pitch(state)
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        runner_r = props.get(RUNNER, "R")
        assert runner_r is not None and runner_r.prob(1) == 1.0
        # The batter: 1 hit + 1 run + 2 RBI = 4.
        batter_hrr = props.get(BATTER, "HRR")
        assert batter_hrr is not None and batter_hrr.prob(4) == 1.0


class TestDroppedThirdStrikeForcesARunHome:
    """The uncaught third strike with the bases loaded and two outs: the batter
    reaches first, the runner on 3B is forced home. The forced run is the
    runner's ``r``, the batter's RBI, and the pitcher's run allowed."""

    def _bases_loaded_two_out_d3k(self) -> tuple[StateMachine, GameState, object]:
        sm = _got_away_machine("swinging_strike")
        state = _state()
        state.bases = Bases(first=RUNNER, second=202, third=303)
        state.outs = 2
        state.strikes = 2
        result = sm.step_pitch(state)
        return sm, state, result

    def test_the_forced_runner_is_credited_the_run_once(self):
        sm, state, result = self._bases_loaded_two_out_d3k()
        assert result.pa_terminal and result.event == "strikeout"
        # The batter reached: no out, the run forced home.
        assert state.outs == 2
        assert result.runs_scored == 1 and state.away_score == 1
        assert result.baserunner_advances[303] == 0
        assert result.baserunner_advances[202] == 3
        assert result.baserunner_advances[RUNNER] == 2
        assert result.baserunner_advances[BATTER] == 1
        assert sm.boxscore.line(303).r == 1
        assert sm.boxscore.line(202).r == 0 and sm.boxscore.line(RUNNER).r == 0
        assert state.bases.first == BATTER
        assert state.bases.second == RUNNER
        assert state.bases.third == 202

    def test_the_team_score_and_the_pitcher_charge_are_unchanged(self):
        sm, state, result = self._bases_loaded_two_out_d3k()
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 0
        assert pit.r_allowed == 1 and pit.er == 1
        # OPEN (pre-existing; the scope of the dropped-third-strike ticket,
        # SIM-484): the reach commits to the run ledger as a reach-on-error,
        # and the box reads that label, so the pitcher's K is NOT credited
        # here although official scoring credits a strikeout on a dropped
        # third strike. Pinned so the SIM-484 fix is seen.
        assert pit.k == 0
        assert sm.boxscore.line(BATTER).rbi == 1

    def test_the_forced_runner_posts_the_r_prop(self):
        sm, _state_, _result = self._bases_loaded_two_out_d3k()
        props = PropDistributionSet.from_boxscores([sm.boxscore])
        dist = props.get(303, "R")
        assert dist is not None and dist.prob(1) == 1.0

    def test_a_dropped_third_strike_with_first_base_open_forces_nobody(self):
        sm = _got_away_machine("swinging_strike")
        state = _state()
        state.bases = Bases(second=202, third=303)
        state.strikes = 2
        result = sm.step_pitch(state)
        assert result.pa_terminal
        assert result.runs_scored == 0 and state.away_score == 0
        assert result.baserunner_advances.get(BATTER) == 1
        assert 303 not in result.baserunner_advances or result.baserunner_advances[303] != 0
        assert sm.boxscore.line(303).r == 0
