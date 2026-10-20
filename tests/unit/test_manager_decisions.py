"""
test_manager_decisions.py
===============================
The manager decisions of the simulation loop's pre-pitch hook
(``simulation/loop/manager.py``): the leverage index and the intentional walk.

WHAT THIS COVERS
----------------
  * the **Leverage Index** (LI: how much a spot can swing the win probability)
    is monotone in the obvious directions (later inning => higher; bigger
    lead/deficit => lower; more runners on / fewer outs => higher);
  * the **intentional walk** (IBB) draws once per plate appearance at the
    measured rate of its cell: it fires in the canonical spot (first base open
    + a runner in scoring position + close & late) and NOT otherwise (first
    base taken, or early / blowout);
  * a game with **no manager profile** still completes (every hook is no-op-safe).

The pitching change is a draw; ``test_manager_draw.py`` tests it.

HOW THE TENDENCIES ARE INJECTED (no live DB)
--------------------------------------------
The decisions read from an injected ``manager`` tendency source (a plain dict of
the manager-similarity rate names), an injected IBB rate table and a fixed-seed
``numpy`` rng, so every decision is deterministic.  No DuckDB is touched.
"""

from __future__ import annotations

import numpy as np
import pytest

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
    inning: int = 9,
    half: Half = Half.TOP,
    home_score: int = 0,
    away_score: int = 0,
    first=None,
    second=None,
    third=None,
    outs: int = 0,
    batter_id: int = 101,
    pitch_count: int = 0,
) -> GameState:
    s = GameState(pitcher_id=PITCHER, bat_hand="R", season=SEASON)
    s.inning = inning
    s.half = half
    s.home_score = home_score
    s.away_score = away_score
    s.outs = outs
    s.bases = Bases(first=first, second=second, third=third)
    s.batter_id = batter_id
    s.pitcher_pitch_count = pitch_count
    return s


def _machine(manager=None, *, seed: int = 7) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager)


# A manager who does everything aggressively (all tendency rates near 1.0).
_AGGRESSIVE = {
    "steal_order_rate_per_1b_opp": 0.95,
    "platoon_advantage_exploitation_rate": 0.95,
    "starter_pull_pct_before_100": 0.95,
    "pinch_hit_rate_high_leverage": 0.95,
    "sac_bunt_rate_high_leverage": 0.95,
    "sac_bunt_rate_low_leverage": 0.95,
}
# A manager who never does anything (all rates 0.0) -- a "ride the lineup" type.
_PASSIVE = dict.fromkeys(_AGGRESSIVE, 0.0)


# ===========================================================================
# Leverage index monotonicity
# ===========================================================================


class TestLeverageIndex:
    def test_li_rises_with_the_inning(self):
        early = StateMachine.compute_leverage(_state(inning=1))
        late = StateMachine.compute_leverage(_state(inning=9))
        assert late > early

    def test_li_falls_as_the_margin_grows(self):
        tie = StateMachine.compute_leverage(_state(home_score=2, away_score=2))
        blowout = StateMachine.compute_leverage(_state(home_score=12, away_score=2))
        assert tie > blowout

    def test_li_rises_with_runners_on(self):
        empty = StateMachine.compute_leverage(_state(outs=0))
        loaded = StateMachine.compute_leverage(_state(first=1, second=2, third=3, outs=0))
        assert loaded > empty

    def test_li_falls_with_more_outs(self):
        no_out = StateMachine.compute_leverage(_state(second=2, outs=0))
        two_out = StateMachine.compute_leverage(_state(second=2, outs=2))
        assert no_out > two_out

    def test_li_is_written_to_the_manager_context(self):
        s = _state(inning=9, second=2)
        li = StateMachine.compute_leverage(s)
        assert s.manager.leverage == pytest.approx(li)


# ===========================================================================
# IBB (intentional walk)
# ===========================================================================


#: The injected rate table — the textbook cell (runner on 2B, 0 outs,
#: late, close) at certainty, everything else absent (measured rate ~0).
_IBB_CERTAIN = {(2, 0, True, True): 1.0}


def _ibb_machine(manager=_AGGRESSIVE, *, rates=None, seed: int = 1) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager, ibb_rates=rates)


class TestIntentionalWalk:
    """The IBB decision draws at the REAL rate of the PA's cell (sim.ibb_rates),
    never a tendency x leverage formula.  The tests inject a rate table; a
    cell at 1.0 fires deterministically."""

    def test_ibb_fires_at_the_cell_rate(self):
        # Runner on 2B, 1B open, tie game, bottom of the 9th, first pitch —
        # the injected table holds this cell at certainty.
        m = _ibb_machine(rates=_IBB_CERTAIN)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is True
        assert any(d["kind"] == "intentional_walk" for d in m.manager_decisions)

    def test_an_absent_cell_never_fires(self):
        # 1B occupied -> runners_state 3, a cell the table does not hold
        # (its measured rate is ~0).
        m = _ibb_machine(rates=_IBB_CERTAIN)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, first=44, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_an_early_blowout_is_a_different_cell(self):
        # 1st inning, 8-run margin -> (late=False, close=False): absent.
        m = _ibb_machine(rates=_IBB_CERTAIN)
        s = _state(inning=1, home_score=0, away_score=8, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_the_draw_fires_once_per_pa_not_per_pitch(self):
        # Mid-count (0-1) the PA's decision is already made — no re-roll (a
        # per-pitch re-roll compounds to 2.64x MLB's IBB volume, SIM-515).
        m = _ibb_machine(rates=_IBB_CERTAIN)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        s.strikes = 1
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_no_rate_table_never_fires(self):
        # A no-DB machine (rates None) issues no IBB — the no-op-safe contract;
        # there is NO formula fallback.
        m = _ibb_machine(rates=None)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_no_manager_never_fires(self):
        # Manager None keeps every hook a no-op even with a rate table wired.
        m = _ibb_machine(manager=None, rates=_IBB_CERTAIN)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_a_zero_rate_cell_never_fires(self):
        m = _ibb_machine(rates={(2, 0, True, True): 0.0})
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55)
        m._pre_pitch_hook(s)
        assert s.manager.intentional_walk_signalled is False

    def test_ibb_ends_the_pa_as_a_walk_putting_the_batter_on_first(self):
        # When the IBB signal is set, step_pitch issues the walk without a pitch.
        m = _ibb_machine(rates=_IBB_CERTAIN)
        s = _state(inning=9, half=Half.BOTTOM, home_score=3, away_score=3, second=55, batter_id=201)
        s.home_lineup = HOME_LINEUP
        s.home_lineup_slot = 0
        result = m.step_pitch(s)
        # The IBB carries its OWN canonical event so the lane's
        # IBB_PA pool band can tell it from a ball-4 walk (the box still
        # credits a BB/non-AB — _BB_CANONICAL holds both classes).
        assert result.event == "intentional_walk"
        # The batter (or the new due-up batter) reached first via the walk force.
        assert s.bases.first is not None


# ===========================================================================
# No-manager-profile games still complete (hooks are no-op-safe)
# ===========================================================================


class TestNoManagerProfile:
    def test_a_game_with_no_manager_profile_completes(self):
        # The loop and full-pool generators seed from independent SeedSequence
        # children. Seed 0 yields a 0-0 tie that runs to the extra-inning cap;
        # seed 2 decides in regulation. The seed is incidental — this test asserts
        # the game reaches a decision rather than stalling.
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
        # No manager profile => no decisions were taken (the hooks were no-ops).
        assert machine.manager_decisions == []

    def test_a_game_with_an_aggressive_manager_also_completes(self):
        # With a full tendency set wired, the game still ends validly
        # (decisions degrade gracefully, never an illegal state).
        rng = np.random.default_rng(1)
        machine = StateMachine(
            synthetic_sampler(league_artifacts(), 1),
            rng=rng,
            manager=_AGGRESSIVE,
        )
        # Seed bullpens on the initial state via the manager context.
        result = simulate_game(
            machine,
            seed=1,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert result.home_score != result.away_score
        assert result.innings_played >= 9
        result.final_state.assert_score_valid()
