"""SIM-483 — steal runs earn no RBI (Rule 9.04(b)); they stay EARNED (9.16(a)).

Latent until SIM-474 revived the running game; live since. Two credits were
wrong on a steal of home:

  * a TERMINAL-pitch steal of home folds its run into the play's ``runs``, and
    ``_accumulate_pa`` credited the batter an RBI for it — Rule 9.04(b) awards
    no run batted in on a stolen base;
  * a NON-terminal steal of home charged the pitcher ``r_allowed`` but never
    ``er``, though a stolen-base run is an earned run (Rule 9.16(a)) — every
    such run under-counted ERA's numerator.

The harness mirrors ``test_boxscore_ext_sim365``: no DB, the machine driven
directly through ``_resolve_steal_outcome`` / ``_accumulate_pa``.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState, PlayResult
from simulation.sim_loop import StateMachine, StealResolution

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = [900 + i for i in range(9)]
HOME_LINEUP = [800 + i for i in range(9)]


def _machine() -> StateMachine:
    return StateMachine(rng=np.random.default_rng(0))


def _state(**kw) -> GameState:
    defaults = {
        "pitcher_id": PITCHER,
        "bat_hand": "R",
        "season": SEASON,
        "away_lineup": list(AWAY_LINEUP),
        "home_lineup": list(HOME_LINEUP),
    }
    defaults.update(kw)
    return GameState(**defaults)


def _stage_home_steal(sm: StateMachine, runner: int = 802) -> None:
    sm._pending_steal = StealResolution(
        attempted=True, runner_id=runner, from_base=3, to_base=4, safe=True
    )


class TestRule904bNoRbiOnASteal:
    def test_terminal_pitch_steal_of_home_earns_the_batter_no_rbi(self):
        sm = _machine()
        state = _state()
        state.bases = Bases(third=802)
        _stage_home_steal(sm)
        # The steal resolves on a pitch that ALSO ends the PA (a walk, say):
        # the steal run rides the play's `runs` into _accumulate_pa.
        result = PlayResult(pitch_outcome="ball", pa_terminal=True)
        sm._resolve_steal_outcome(state, result)
        assert result.steal_runs_scored == 1
        result.canonical_event = "walk"
        result.event = "walk"
        result.runs_scored = 1  # the steal run, folded in
        sm._accumulate_pa(state, result)
        batter = sm._current_batter_id(state)
        assert sm.boxscore.line(int(batter)).rbi == 0  # Rule 9.04(b)
        assert sm.boxscore.line(802).r == 1  # the run itself still counts

    def test_a_driven_in_run_beside_the_steal_run_still_earns_its_rbi(self):
        sm = _machine()
        state = _state()
        state.bases = Bases(third=802)
        _stage_home_steal(sm)
        result = PlayResult(pitch_outcome="in_play", pa_terminal=True, is_contact=True)
        sm._resolve_steal_outcome(state, result)
        # The batter then singles home ANOTHER runner on the same terminal
        # play: 2 runs total, 1 from the steal -> exactly 1 RBI.
        result.canonical_event = "single"
        result.event = "single"
        result.runs_scored = 2
        sm._accumulate_pa(state, result)
        batter = sm._current_batter_id(state)
        assert sm.boxscore.line(int(batter)).rbi == 1

    def test_an_ordinary_steal_changes_no_rbi_arithmetic(self):
        sm = _machine()
        state = _state()
        state.bases = Bases(first=801)
        sm._pending_steal = StealResolution(
            attempted=True, runner_id=801, from_base=1, to_base=2, safe=True
        )
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)
        assert result.steal_runs_scored == 0


class TestRule916aStealRunsAreEarned:
    def test_non_terminal_steal_of_home_charges_the_pitcher_an_earned_run(self):
        sm = _machine()
        state = _state()
        state.bases = Bases(third=802)
        _stage_home_steal(sm)
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)
        line = sm.boxscore.line(PITCHER)
        assert line.r_allowed == 1
        assert line.er == 1  # Rule 9.16(a): a stolen-base run is earned

    def test_the_steal_run_is_unearned_when_the_inning_should_be_over(self):
        sm = _machine()
        state = _state()
        state.bases = Bases(third=802)
        state.outs = 2
        # An earlier error cost an out: effective outs = 3, so every later
        # run is unearned — the same rule _accumulate_pa applies.
        sm._half_inning_error_outs_lost = 1
        _stage_home_steal(sm)
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)
        line = sm.boxscore.line(PITCHER)
        assert line.r_allowed == 1
        assert line.er == 0
