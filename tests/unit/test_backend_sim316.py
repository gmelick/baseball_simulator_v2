"""
test_backend_sim316.py
======================
Unit tests for SIM-316 -- the GameState-driven plate-appearance / half-inning
**state machine** in ``simulation/sim_loop.py`` (Phase 4, the first loop-build
ticket).

These tests exercise the count machine and half-inning logic in the
*count-machine-only* mode (no sampler): :meth:`StateMachine.step_pitch` accepts
an explicit ``pitch_outcome`` so the §5.1 count transitions and §6.1 half-inning
roll can be driven deterministically with NO live DuckDB/FAISS, mirroring the
"inject the outcome" pattern the SIM-302/303 sampler tests use.

Coverage (the SIM-316 acceptance criteria):
  * count machine: ball-4 walk, strike-3 strikeout, two-strike-foul stays alive
    (the SIM-056 absorbing rule), and the pure ``advance_count`` classifier;
  * half-inning logic: 3 outs -> clear bases, reset count + outs, flip Half,
    advance inning on BOTTOM->TOP, carry the per-team lineup pointer forward;
  * invalid-state guards: bad transitions / corrupt state are rejected.
"""

from __future__ import annotations

import pytest

from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import (
    EVENT_STRIKEOUT,
    EVENT_WALK,
    CountAdvance,
    StateMachine,
    advance_count,
    simulate_game,
)

SEASON = 2024
PITCHER = 477132


def _fresh_state(**kw) -> GameState:
    """A 'top of the 1st, nobody on, 0-0, 0 outs' GameState."""
    base = {"pitcher_id": PITCHER, "bat_hand": "R", "season": SEASON}
    base.update(kw)
    return GameState(**base)


# ===========================================================================
# Pure count machine (advance_count) — spec §5.1
# ===========================================================================


class TestAdvanceCount:
    def test_ball_increments_then_walks_on_four(self):
        adv = advance_count(0, 0, "ball")
        assert adv == CountAdvance(1, 0, terminal=False, event="in_progress", is_contact=False)
        # Ball four is terminal -> walk.
        adv4 = advance_count(3, 2, "ball")
        assert adv4.balls == 4
        assert adv4.terminal is True
        assert adv4.event == EVENT_WALK
        assert adv4.is_contact is False

    def test_strike_increments_then_strikes_out_on_three(self):
        adv = advance_count(0, 0, "called_strike")
        assert adv == CountAdvance(0, 1, terminal=False, event="in_progress", is_contact=False)
        adv3 = advance_count(1, 2, "swinging_strike")
        assert adv3.strikes == 3
        assert adv3.terminal is True
        assert adv3.event == EVENT_STRIKEOUT

    def test_foul_under_two_strikes_is_an_ordinary_strike(self):
        assert advance_count(0, 0, "foul").strikes == 1
        assert advance_count(2, 1, "foul").strikes == 2

    def test_two_strike_foul_is_absorbed_and_stays_alive(self):
        # SIM-056 absorbing rule: with two strikes a foul does NOT advance the
        # count and does NOT terminate the PA.
        adv = advance_count(1, 2, "foul")
        assert adv.strikes == 2  # unchanged
        assert adv.balls == 1  # unchanged
        assert adv.terminal is False  # PA stays alive
        assert adv.is_contact is False

    def test_in_play_is_terminal_contact_with_no_event(self):
        adv = advance_count(2, 2, "in_play")
        assert adv.terminal is True
        assert adv.is_contact is True
        assert adv.event is None  # event resolved by the batted ball

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValueError):
            advance_count(0, 0, "bunt")

    def test_classifying_an_already_terminal_count_rejected(self):
        with pytest.raises(ValueError):
            advance_count(4, 0, "ball")  # already a walk
        with pytest.raises(ValueError):
            advance_count(0, 3, "called_strike")  # already a strikeout


# ===========================================================================
# StateMachine count machine driven through a PA — spec §5.1 / step 8 commit
# ===========================================================================


class TestStateMachineCount:
    def test_walk_terminates_pa_and_resets_count(self):
        sm = StateMachine()  # count-machine-only mode (no sampler)
        state = _fresh_state()
        # Three balls keep the PA live, count advances.
        for i in range(3):
            r = sm.step_pitch(state, pitch_outcome="ball")
            assert r.pa_terminal is False
            assert state.balls == i + 1
        # Ball four -> walk, PA rolls over (count reset for next batter).
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.pa_terminal is True
        assert r.event == EVENT_WALK
        assert r.outs_recorded == 0  # a walk records no out
        assert state.outs == 0
        assert (state.balls, state.strikes) == (0, 0)  # count reset for next PA

    def test_strikeout_terminates_pa_and_records_out(self):
        sm = StateMachine()
        state = _fresh_state()
        sm.step_pitch(state, pitch_outcome="called_strike")
        sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert state.strikes == 2
        r = sm.step_pitch(state, pitch_outcome="called_strike")
        assert r.pa_terminal is True
        assert r.event == EVENT_STRIKEOUT
        assert r.outs_recorded == 1
        assert state.outs == 1
        assert (state.balls, state.strikes) == (0, 0)  # count reset

    def test_two_strike_foul_keeps_pa_alive_indefinitely(self):
        sm = StateMachine()
        state = _fresh_state()
        sm.step_pitch(state, pitch_outcome="called_strike")
        sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.strikes == 2
        # A long run of two-strike fouls must never strike the batter out.
        for _ in range(10):
            r = sm.step_pitch(state, pitch_outcome="foul")
            assert r.pa_terminal is False
            assert state.strikes == 2  # absorbed, never advances to 3
            assert state.outs == 0
        # The PA is still live: a real strike now ends it.
        r = sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert r.pa_terminal is True
        assert r.event == EVENT_STRIKEOUT


# ===========================================================================
# Half-inning logic — spec §6.1
# ===========================================================================


class TestHalfInning:
    def test_three_outs_flips_half_clears_bases_resets_count_outs(self):
        sm = StateMachine()
        state = _fresh_state()
        # Put runners on to prove the bases get cleared on the roll.
        state.bases = Bases(first=101, second=102)
        assert state.half == Half.TOP and state.inning == 1

        # Three strikeouts in the top of the 1st.
        for _ in range(3):
            sm.step_pitch(state, pitch_outcome="called_strike")
            sm.step_pitch(state, pitch_outcome="called_strike")
            sm.step_pitch(state, pitch_outcome="called_strike")

        # Half-inning rolled: bottom of the 1st, clean slate.
        assert state.half == Half.BOTTOM
        assert state.inning == 1  # inning does NOT advance on TOP->BOTTOM
        assert state.outs == 0
        assert (state.balls, state.strikes) == (0, 0)
        assert state.bases.occupancy == (False, False, False)

    def test_bottom_to_top_advances_the_inning(self):
        sm = StateMachine()
        state = _fresh_state(half=Half.BOTTOM, inning=1)
        for _ in range(3):
            for _ in range(3):
                sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.half == Half.TOP
        assert state.inning == 2  # BOTTOM->TOP advances the inning

    def test_lineup_pointer_carries_across_half_innings(self):
        sm = StateMachine()
        away = [10, 11, 12, 13, 14, 15, 16, 17, 18]
        home = [20, 21, 22, 23, 24, 25, 26, 27, 28]
        state = _fresh_state(
            away_lineup=away,
            home_lineup=home,
            away_lineup_slot=0,
            home_lineup_slot=0,
            batter_id=away[0],
        )
        # Top of the 1st: AWAY bats. Two batters reach (walk), one strikes out
        # ... drive 3 outs via strikeouts, but advance the order each PA.
        # 3 strikeouts -> away pointer advanced 3 times -> slot 3.
        for _ in range(3):
            for _ in range(3):
                sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.away_lineup_slot == 3
        assert state.batter_id == away[3]  # away resumes at slot 3 next time
        assert state.home_lineup_slot == 0  # home untouched
        assert state.half == Half.BOTTOM

        # Bottom of the 1st: HOME bats. Two strikeouts then end the half.
        for _ in range(3):
            for _ in range(3):
                sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.home_lineup_slot == 3
        assert state.away_lineup_slot == 3  # AWAY pointer carried forward intact
        assert state.half == Half.TOP
        assert state.inning == 2

        # Top of the 2nd: AWAY resumes where it left off (slot 3, then advances).
        for _ in range(3):
            sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.away_lineup_slot == 4  # 3 -> 4 after this PA

    def test_lineup_pointer_wraps_around(self):
        sm = StateMachine()
        away = [1, 2, 3]
        state = _fresh_state(away_lineup=away, away_lineup_slot=2, batter_id=3)
        # One strikeout PA advances the slot 2 -> 0 (wrap past the lineup end).
        for _ in range(3):
            sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.away_lineup_slot == 0
        assert state.batter_id == 1
        assert state.outs == 1
        # Drive the half to its 3 outs (two more strikeout PAs): 0 -> 1 -> 2,
        # then the half rolls (slot is preserved across the roll).
        for _ in range(2):
            for _ in range(3):
                sm.step_pitch(state, pitch_outcome="called_strike")
        assert state.away_lineup_slot == 2  # carried across the half-inning roll
        assert state.half == Half.BOTTOM


# ===========================================================================
# Invalid-state guards — spec acceptance #3
# ===========================================================================


class TestInvalidStateGuards:
    def test_step_pitch_rejects_a_corrupt_incoming_count(self):
        sm = StateMachine()
        state = _fresh_state(balls=5)  # impossible live count
        with pytest.raises(ValueError):
            sm.step_pitch(state, pitch_outcome="ball")

    def test_step_pitch_rejects_negative_score(self):
        sm = StateMachine()
        state = _fresh_state()
        state.home_score = -1
        with pytest.raises(ValueError):
            sm.step_pitch(state, pitch_outcome="ball")

    def test_step_pitch_rejects_corrupt_base_runner_id(self):
        sm = StateMachine()
        state = _fresh_state()
        state.bases = Bases(first=-7)  # negative runner id
        with pytest.raises(ValueError):
            sm.step_pitch(state, pitch_outcome="ball")

    def test_step_pitch_rejects_too_many_outs_incoming(self):
        sm = StateMachine()
        state = _fresh_state(outs=3)  # half-inning should already have ended
        with pytest.raises(ValueError):
            sm.step_pitch(state, pitch_outcome="called_strike")

    def test_step_pitch_rejects_unknown_pitch_outcome(self):
        sm = StateMachine()
        state = _fresh_state()
        with pytest.raises(ValueError):
            sm.step_pitch(state, pitch_outcome="balk")

    def test_count_machine_only_mode_requires_an_outcome(self):
        sm = StateMachine()  # no sampler
        state = _fresh_state()
        with pytest.raises(ValueError):
            sm.step_pitch(state)  # no pitch_outcome and no sampler -> error

    def test_record_out_guard_rejects_overflow(self):
        sm = StateMachine()
        state = _fresh_state(outs=2)
        # Recording 2 more outs would push outs to 4 (> the terminal 3 ceiling).
        with pytest.raises(ValueError):
            sm._record_outs(state, 2)


# ===========================================================================
# Derived state reads + the SIM-320 driver stub
# ===========================================================================


class TestMiscContract:
    def test_offense_defense_and_score_diff_track_the_half(self):
        state = _fresh_state(home_score=3, away_score=1)
        # Top: AWAY bats, trails by 2.
        assert state.offense == Team.AWAY
        assert state.score_diff == 1 - 3
        state.half = Half.BOTTOM
        assert state.offense == Team.HOME
        assert state.score_diff == 3 - 1

    def test_simulate_game_is_implemented_by_sim320(self):
        # SIM-320 replaced the SIM-316 guarded NotImplementedError stub with the
        # real full-game driver.  Called with no sampler AND no way to produce a
        # pitch outcome it now fails fast with a clear ValueError (the machine
        # cannot sample) rather than the old NotImplementedError.
        with pytest.raises(ValueError):
            simulate_game()
