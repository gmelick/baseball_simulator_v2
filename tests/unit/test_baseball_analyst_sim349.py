"""
test_baseball_analyst_sim349.py
===============================
SIM-349 -- the **situational-decision module**: the hit-and-run (pre-pitch) and
sac-fly-intent (PA-boundary) triggers added to the Phase-4 simulation loop
(``simulation/sim_loop.py``), consolidated with SIM-323's IBB + sac-bunt into one
leverage- and base/out-state-conditioned situational set.

WHAT THIS COVERS (what is left of the SIM-349 acceptance criteria)
------------------------------------------------------------------
  * a no-manager-profile game still completes with no decision taken, and an
    aggressive-manager game completes validly.

  The hit-and-run trigger (deleted 2026-09-13 by SIM-427 with the other dead
  hooks) and the sac-fly intent (retired 2026-08-19 by SIM-513) are gone; their
  tests went with them.

HOW THE TENDENCIES ARE INJECTED (no live DB)
--------------------------------------------
Decisions read an injected ``manager`` dict of the SIM-2.8 manager-similarity rate
names + a fixed-seed numpy rng, mirroring the SIM-316/319/320/323 inject-the-signal
pattern -- no DuckDB / FAISS is touched.
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


# (The `_SacFlyResolver` helper and the sac-fly-intent test classes lived
# here until 2026-08-19 — retired with the SIM-349 nudge by SIM-513. The
# SIM-512 tag draw from 3B owns sacrifice flies now, pinned by
# tests/unit/test_sim511_512_transition_draw.py and the retargeted sac-fly
# ledger test in tests/unit/test_sim499_run_ledger.py. The hit-and-run trigger,
# its consolidation with the steal initiate and its determinism tests lived
# here until 2026-09-13 — deleted with the hook by SIM-427 (owner decision:
# the four dead hooks go; a hit-and-run is a steal draw plus a pitch draw, and
# the hook changed no play). What stays: a no-manager game and an aggressive-
# manager game both complete.)


# ===========================================================================
# No-manager-profile games still complete (every new trigger is no-op-safe)
# ===========================================================================


class TestNoProfileFullGame:
    def test_no_profile_game_completes_with_new_triggers_no_op(self):
        # SIM-498 reseeded the loop and full-pool generators from independent
        # SeedSequence children, which legitimately changes the draw stream. Seed 0
        # now yields a 0-0 tie that runs to the extra-inning cap; seed 2 decides in
        # regulation. The seed is incidental — this test asserts the game reaches a
        # decision rather than stalling, and it still does.
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
        # No profile => no situational decisions taken (the hooks were no-ops).
        assert machine.manager_decisions == []
        result.final_state.assert_score_valid()

    def test_aggressive_situational_manager_game_completes_validly(self):
        # SIM-505 CLOSED: the fixture used to pass this assertion by SEED LUCK
        # (its in-play pitches resolved to nothing, so runners parked for
        # whole innings and seeds 1 and 7 ended with a runner on two bases).
        # With the injected batted-ball sample the resolver actually runs, so
        # the game is legal at ANY seed — asserted across the two previously
        # ILLEGAL seeds plus one that always passed.
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
