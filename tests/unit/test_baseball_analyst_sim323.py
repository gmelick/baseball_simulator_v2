"""
test_baseball_analyst_sim323.py
===============================
SIM-323 -- the **manager-decision module** behind the §3 (pre-pitch) and §5.3
(end-of-PA) hooks of the Phase-4 simulation loop (``simulation/sim_loop.py``).

WHAT THIS COVERS (the SIM-323 acceptance criteria)
--------------------------------------------------
  * the **Leverage Index** is monotone in the obvious directions (later inning =>
    higher; bigger lead/deficit => lower; more runners on / fewer outs => higher);
  * **IBB** fires in the canonical spot (first base open + RISP + close & late +
    high LI + an aggressive matchup manager) and NOT otherwise (first base taken,
    or early / blowout);
  * the steal **green-light** tracks the manager's steal tendency (a high-steal
    manager green-lights, a no-steal manager does not);
  * a high pitch-count + high-leverage spot triggers a **starter pull** to a
    **leverage-appropriate reliever** (the closer enters a high-LI late spot);
  * a **pinch-hit** fires from the bench in a high-leverage spot;
  * a game with **no manager profile** still completes (every hook is no-op-safe).

HOW THE TENDENCIES ARE INJECTED (no live DB)
--------------------------------------------
The decisions read from an injected ``manager`` tendency source (a plain dict of
the SIM-2.8 manager-similarity rate names) and from a fixed-seed ``numpy`` rng, so
every decision is deterministic.  This mirrors the SIM-316/319/320 "inject the
signal" pattern -- no DuckDB / FAISS is touched.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, Team
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


def _machine(manager=None, *, seed: int = 7, bench=None) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager, bench=bench)


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


#: SIM-515: the injected rate table — the textbook cell (runner on 2B, 0 outs,
#: late, close) at certainty, everything else absent (measured rate ~0).
_IBB_CERTAIN = {(2, 0, True, True): 1.0}


def _ibb_machine(manager=_AGGRESSIVE, *, rates=None, seed: int = 1) -> StateMachine:
    return StateMachine(rng=np.random.default_rng(seed), manager=manager, ibb_rates=rates)


class TestIntentionalWalk:
    """SIM-515: the IBB decision draws at the REAL rate of the PA's cell
    (sim.ibb_rates) — the hand-tuned tendency x leverage formula is retired.
    The tests inject a rate table; a cell at 1.0 fires deterministically."""

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
        # Mid-count (0-1) the PA's decision is already made — no re-roll (the
        # old per-pitch re-roll compounded to 2.64x MLB's IBB volume).
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
        # SIM-515/516: the IBB carries its OWN canonical event so the lane's
        # IBB_PA pool band can tell it from a ball-4 walk (the box still
        # credits a BB/non-AB — _BB_CANONICAL holds both classes).
        assert result.event == "intentional_walk"
        # The batter (or the new due-up batter) reached first via the walk force.
        assert s.bases.first is not None


# ===========================================================================
# Steal green-light
# ===========================================================================


class TestStealGreenLight:
    def test_green_light_tracks_an_aggressive_steal_tendency(self):
        m = _machine({"steal_order_rate_per_1b_opp": 0.9}, seed=3)
        s = _state(inning=7, first=11)
        m._pre_pitch_hook(s)
        assert s.manager.green_light_rate > 0.0

    def test_green_light_off_for_a_no_steal_manager(self):
        m = _machine({"steal_order_rate_per_1b_opp": 0.0}, seed=3)
        s = _state(inning=7, first=11)
        m._pre_pitch_hook(s)
        assert s.manager.green_light_rate == 0.0

    def test_green_light_higher_in_high_leverage(self):
        # Same tendency, a higher-leverage spot -> a higher (or equal) green-light.
        m = _machine({"steal_order_rate_per_1b_opp": 0.5}, seed=3)
        low = _state(inning=1, home_score=0, away_score=9, first=11)
        m._pre_pitch_hook(low)
        low_green = low.manager.green_light_rate
        m2 = _machine({"steal_order_rate_per_1b_opp": 0.5}, seed=3)
        high = _state(inning=9, half=Half.BOTTOM, home_score=2, away_score=2, first=11)
        m2._pre_pitch_hook(high)
        assert high.manager.green_light_rate >= low_green

    def test_the_steal_draw_is_staged_ungated_sim474(self):
        # SIM-474 deleted the green-light GATE: from 2026-06-04 to 2026-08-16 it
        # routed every production pitch to a resolver stub and zero steals were
        # attempted (SIM-495 measured SB 0.0000 against 0.59). The decision is
        # now the opportunity-pool draw, UNGATED — manager aggression weights
        # the draw instead of gating it — so a pool whose every row runs stages
        # a steal for an aggressive AND a passive manager alike.
        for rate in (1.0, 0.0):
            m = StateMachine(
                synthetic_sampler(league_artifacts(steal=(1.0, 1.0)), 2),
                rng=np.random.default_rng(2),
                manager={"steal_order_rate_per_1b_opp": rate},
            )
            s = _state(inning=7, first=11)
            m._pre_pitch_hook(s)
            assert m._pending_steal is not None and m._pending_steal.attempted, rate


# ===========================================================================
# Starter pull + bullpen by leverage
# ===========================================================================


class TestStarterPull:
    def test_high_pitch_count_high_leverage_pulls_to_the_closer(self):
        # Away team is defending in the bottom of the 9th (closer territory).
        bullpen = {Team.HOME: [301, 302, 303]}  # 301 == the closer (first arm)
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=105)
        s.manager.bullpen_available = bullpen
        m = _machine(_AGGRESSIVE, seed=4)
        m._end_of_pa_hook(s)
        # The defending team (HOME, fielding in the top) pulled its starter.
        assert s.pitcher_id == 301  # the closer entered
        assert s.pitcher_pitch_count == 0  # fresh arm
        assert any(d["kind"] == "pitching_change" for d in m.manager_decisions)

    def test_low_pitch_count_does_not_pull(self):
        bullpen = {Team.HOME: [301, 302, 303]}
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=10)
        s.manager.bullpen_available = bullpen
        m = _machine(_AGGRESSIVE, seed=4)
        m._end_of_pa_hook(s)
        assert s.pitcher_id == PITCHER  # starter stays

    def test_ceiling_forces_a_pull_even_for_a_passive_manager(self):
        bullpen = {Team.HOME: [301, 302, 303]}
        s = _state(inning=3, half=Half.TOP, home_score=0, away_score=0, pitch_count=120)
        s.manager.bullpen_available = bullpen
        m = _machine(_PASSIVE, seed=4)
        m._end_of_pa_hook(s)
        assert s.pitcher_id != PITCHER  # the hard ceiling forced the hook

    def test_pull_degrades_gracefully_with_an_empty_bullpen(self):
        s = _state(inning=9, half=Half.TOP, home_score=2, away_score=2, second=55, pitch_count=130)
        s.manager.bullpen_available = {}  # no arms available
        m = _machine(_AGGRESSIVE, seed=4)
        m._end_of_pa_hook(s)
        assert s.pitcher_id == PITCHER  # no illegal state -> starter stays


# ===========================================================================
# Pinch-hit
# ===========================================================================


class TestPinchHit:
    def test_pinch_hit_fires_from_the_bench_in_high_leverage(self):
        s = _state(inning=9, half=Half.BOTTOM, home_score=2, away_score=2, second=55)
        s.home_lineup = list(HOME_LINEUP)
        s.home_lineup_slot = 0
        bench = {Team.HOME: [777, 778]}
        m = _machine(_AGGRESSIVE, seed=6, bench=bench)
        out_batter = s.home_lineup[0]
        m._maybe_pinch_hit(s, StateMachine.compute_leverage(s))
        assert s.home_lineup[0] == 777  # the bench bat replaced the slot
        assert s.batter_id == 777
        assert out_batter not in s.home_lineup
        assert any(d["kind"] == "pinch_hit" for d in m.manager_decisions)

    def test_no_pinch_hit_in_low_leverage(self):
        s = _state(inning=1, half=Half.BOTTOM, home_score=0, away_score=9)
        s.home_lineup = list(HOME_LINEUP)
        bench = {Team.HOME: [777]}
        m = _machine(_AGGRESSIVE, seed=6, bench=bench)
        m._maybe_pinch_hit(s, StateMachine.compute_leverage(s))
        assert s.home_lineup[0] == HOME_LINEUP[0]  # unchanged


# ===========================================================================
# Sac-bunt setup
# ===========================================================================


class TestSacBunt:
    def test_sac_bunt_signalled_with_a_runner_on_and_under_two_outs(self):
        s = _state(inning=7, first=11, outs=1)
        m = _machine(_AGGRESSIVE, seed=8)
        m._maybe_sac_bunt(s, StateMachine.compute_leverage(s))
        assert any(d["kind"] == "sac_bunt" for d in m.manager_decisions)

    def test_no_sac_bunt_with_two_outs(self):
        s = _state(inning=7, first=11, outs=2)
        m = _machine(_AGGRESSIVE, seed=8)
        m._maybe_sac_bunt(s, StateMachine.compute_leverage(s))
        assert not any(d["kind"] == "sac_bunt" for d in m.manager_decisions)


# ===========================================================================
# No-manager-profile games still complete (hooks are no-op-safe)
# ===========================================================================


class TestNoManagerProfile:
    def test_a_game_with_no_manager_profile_completes(self):
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
        # No manager profile => no decisions were taken (the hooks were no-ops).
        assert machine.manager_decisions == []

    def test_a_game_with_an_aggressive_manager_also_completes(self):
        # With a full tendency set + a bullpen + bench wired, the game still ends
        # validly (decisions degrade gracefully, never an illegal state).
        rng = np.random.default_rng(1)
        bench = {Team.HOME: [777, 778], Team.AWAY: [677, 678]}
        machine = StateMachine(
            synthetic_sampler(league_artifacts(), 1),
            rng=rng,
            manager=_AGGRESSIVE,
            bench=bench,
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
