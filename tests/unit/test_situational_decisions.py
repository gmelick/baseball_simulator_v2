"""
test_situational_decisions.py
===============================
Whole games with and without a manager profile complete validly.

WHAT THIS COVERS
----------------
  * a no-manager-profile game completes with no decision taken, and an
    aggressive-manager game completes validly.

  The loop has no hit-and-run or sac-fly-intent hook: a hit-and-run is a steal
  draw plus a pitch draw (the owner's decision of 2026-09-13), and the
  advancement draw from third owns sacrifice flies.

HOW THE MANAGER IS INJECTED (no live DB)
----------------------------------------
Decisions read an injected ``manager`` dict of manager rate names + a
fixed-seed numpy rng, and the plays come from the in-memory league bundle, so
no DuckDB / FAISS is touched.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState, Half
from simulation.sim_loop import StateMachine, simulate_game
from simulation.synthetic_bundle import league_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


# ===========================================================================
# Helpers
# ===========================================================================


def _state(
    *,
    inning: int = 7,
    half: Half = Half.TOP,
    home_score: int = 0,
    away_score: int = 0,
    first=None,
    second=None,
    third=None,
    outs: int = 0,
    balls: int = 0,
    strikes: int = 0,
    batter_id: int = 101,
) -> GameState:
    s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
    s.inning = inning
    s.half = half
    s.home_score = home_score
    s.away_score = away_score
    s.outs = outs
    s.balls = balls
    s.strikes = strikes
    s.bases = Bases(first=first, second=second, third=third)
    s.batter_id = batter_id
    return s


def _machine(manager=None, *, seed: int = 7, ibb_rates=None) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager, ibb_rates=ibb_rates)


# A manager who pulls every situational lever (all rates near 1.0).
_AGGRESSIVE = {
    "steal_order_rate_per_1b_opp": 0.95,
    "hit_and_run_rate_per_opportunity": 0.95,
    "platoon_advantage_exploitation_rate": 0.95,
    "sac_bunt_rate_high_leverage": 0.95,
    "sac_bunt_rate_low_leverage": 0.95,
    "squeeze_play_rate_per_3b_opp": 0.95,
}
# A manager who never moves (all rates 0.0).
_PASSIVE = dict.fromkeys(_AGGRESSIVE, 0.0)


# (The advancement draw from 3B owns sacrifice flies, pinned by
# tests/unit/test_transition_draw.py and the sac-fly ledger test in
# tests/unit/test_run_ledger.py. A hit-and-run is a steal draw plus a pitch
# draw, so the loop has no hook for it. This file pins that a no-manager game
# and an aggressive-manager game both complete.)


# ===========================================================================
# Games with and without a manager profile complete
# ===========================================================================


class TestNoProfileFullGame:
    def test_no_profile_game_completes_with_new_triggers_no_op(self):
        # The seed is incidental: this test asserts the game reaches a decision
        # rather than stalling. Some seeds end tied at the extra-inning cap;
        # seed 2 decides the game.
        rng = np.random.default_rng(2)
        machine = StateMachine(synthetic_sampler(league_artifacts(), 2), rng=rng)  # manager=None
        result = simulate_game(
            machine,
            seed=2,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert result.home_score != result.away_score
        assert result.innings_played >= 9
        # No profile => no situational decisions taken (the hooks are no-ops).
        assert machine.manager_decisions == []
        result.final_state.assert_score_valid()

    def test_aggressive_situational_manager_game_completes_validly(self):
        # The game must be legal at ANY seed. Seeds 1 and 7 are the two that
        # once ended with a runner on two bases, when in-play pitches resolved
        # to nothing; seed 2 is a control.
        for seed in (1, 2, 7):
            rng = np.random.default_rng(seed)
            machine = StateMachine(
                synthetic_sampler(league_artifacts(), seed),
                rng=rng,
                manager=_AGGRESSIVE,
            )
            result = simulate_game(
                machine,
                seed=seed,
                away_lineup=AWAY_LINEUP,
                home_lineup=HOME_LINEUP,
            )
            assert result.innings_played >= 9, seed
            result.final_state.assert_invariants(in_play=True)
            result.final_state.assert_score_valid()
