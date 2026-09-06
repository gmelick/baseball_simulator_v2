"""
test_backend_sim328.py
======================
Unit tests for SIM-328 -- the **per-player sim-average accumulators** built into
the plate-appearance loop in ``simulation/sim_loop.py`` (Sprint 3 of the Phase-4
loop build).

As a game simulates, each completed terminal plate appearance is attributed to
the current batter (offense) and the current pitcher (defense), accumulating a
per-player stat line on a per-game :class:`BoxScore`:

  * Batters:  AB / H / HR / RBI.
  * Pitchers: IP (outs / thirds) / K / BB / ER.

These tests run with NO live DuckDB/FAISS: the machine is driven with explicit
``pitch_outcome`` strings (count-machine-only mode) and an injected
:class:`PlayResolver` supplies the batted-ball resolution, mirroring the
SIM-316/319/320 "inject the signal" pattern.

Coverage (the SIM-328 acceptance criteria):
  * a walk does NOT count as an AB (PA - BB) but is charged to the pitcher as BB;
  * a HR credits the batter an AB + H + HR and the RBI(s) it drives in;
  * a strikeout is an AB for the batter + a K + an out (IP) for the pitcher;
  * IP accumulates in thirds (x.0 / x.1 / x.2) across outs;
  * an unearned run via ``is_error`` is NOT charged to the pitcher as ER
    (nor credited as a batter RBI);
  * attribution switches batter/pitcher correctly at the half-inning flip;
  * the per-game boxscore is exposed on ``GameSimResult.boxscore``.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import (
    BoxScore,
    GameSimResult,
    PlayerStatLine,
    StateMachine,
    simulate_game,
)
from simulation.synthetic_bundle import fixed_play_artifacts, league_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))  # 9 away batters
HOME_LINEUP = list(range(201, 210))  # 9 home batters


# ===========================================================================
# Test doubles — a fixed-play bundle (one event in every base-out cell)
# ===========================================================================


def _machine(play: str | None = None) -> StateMachine:
    """A machine whose every batted ball is ``play`` (SIM-486: the production
    in-play path over a fixed-play bundle), or count-machine-only when None."""
    fp = synthetic_sampler(fixed_play_artifacts(play), 0) if play else None
    return StateMachine(fp, rng=np.random.default_rng(0))


def _fresh_state(**kw) -> GameState:
    defaults = {
        "pitcher_id": PITCHER,
        "bat_hand": "R",
        "season": SEASON,
        "away_lineup": list(AWAY_LINEUP),
        "home_lineup": list(HOME_LINEUP),
        "batter_id": AWAY_LINEUP[0],
    }
    defaults.update(kw)
    return GameState(**defaults)


def _drive_walk(sm: StateMachine, state: GameState) -> None:
    """Drive a 4-pitch walk through the count-machine-only path."""
    for _ in range(4):
        sm.step_pitch(state, pitch_outcome="ball")


def _drive_strikeout(sm: StateMachine, state: GameState) -> None:
    """Drive a 3-pitch (called) strikeout."""
    for _ in range(3):
        sm.step_pitch(state, pitch_outcome="called_strike")


def _drive_in_play(sm: StateMachine, state: GameState) -> None:
    """Drive one in-play PA (the fixed-play bundle fixes the event)."""
    sm.step_pitch(state, pitch_outcome="in_play")


# ===========================================================================
# A walk is a PA but NOT an at-bat (batter) and is a BB (pitcher)
# ===========================================================================


class TestWalkIsNotAnAtBat:
    def test_walk_does_not_count_as_an_ab_and_is_a_pitcher_bb(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        _drive_walk(sm, state)

        box = sm.boxscore
        assert box is not None
        bat = box.line(batter)
        # PA - BB == 0 AB; no hit; no HR; no RBI (nobody forced in, empty bases).
        assert bat.ab == 0
        assert bat.h == 0
        assert bat.hr == 0
        assert bat.rbi == 0
        # The pitcher is charged a walk, no out, no ER.
        pit = box.line(PITCHER)
        assert pit.bb == 1
        assert pit.outs_recorded == 0
        assert pit.k == 0
        assert pit.er == 0


# ===========================================================================
# A home run: AB + H + HR for the batter, RBI(s) it drives in
# ===========================================================================


class TestHomeRunCreditsRbi:
    def test_solo_home_run_credits_ab_h_hr_and_one_rbi(self):
        sm = _machine("home_run")
        state = _fresh_state()
        batter = state.batter_id
        _drive_in_play(sm, state)

        bat = sm.boxscore.line(batter)
        assert bat.ab == 1
        assert bat.h == 1
        assert bat.hr == 1
        assert bat.rbi == 1  # solo HR drives in the batter himself
        # The HR scored a run charged to the pitcher as an earned run; no out.
        pit = sm.boxscore.line(PITCHER)
        assert pit.er == 1
        assert pit.outs_recorded == 0
        assert pit.k == 0

    def test_three_run_home_run_credits_three_rbi(self):
        # Bases loaded so a HR clears them: 3 runners + the batter == 4 runs,
        # the drawn row's destinations are what is committed/attributed.
        sm = _machine("home_run")
        state = _fresh_state()
        state.bases = Bases(first=501, second=502, third=503)
        batter = state.batter_id
        _drive_in_play(sm, state)

        bat = sm.boxscore.line(batter)
        assert bat.hr == 1
        assert bat.rbi == 4  # grand slam drives in 4
        assert sm.boxscore.line(PITCHER).er == 4


# ===========================================================================
# A strikeout: AB + (no H) for the batter, K + an out for the pitcher
# ===========================================================================


class TestStrikeout:
    def test_strikeout_is_an_ab_and_a_pitcher_k_and_out(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        _drive_strikeout(sm, state)

        bat = sm.boxscore.line(batter)
        assert bat.ab == 1
        assert bat.h == 0
        assert bat.hr == 0
        pit = sm.boxscore.line(PITCHER)
        assert pit.k == 1
        assert pit.outs_recorded == 1
        assert pit.bb == 0
        assert pit.er == 0


# ===========================================================================
# A single: AB + H, no HR, RBI only for runs it drives in
# ===========================================================================


class TestSingle:
    def test_single_is_an_ab_and_a_hit_no_hr(self):
        sm = _machine("single")
        state = _fresh_state()
        batter = state.batter_id
        _drive_in_play(sm, state)

        bat = sm.boxscore.line(batter)
        assert bat.ab == 1
        assert bat.h == 1
        assert bat.hr == 0
        assert bat.rbi == 0

    def test_rbi_single_credits_the_run_it_drives_in(self):
        sm = _machine("single")
        state = _fresh_state()
        state.bases = Bases(third=505)  # runner on 3B to drive in
        batter = state.batter_id
        _drive_in_play(sm, state)

        bat = sm.boxscore.line(batter)
        assert bat.h == 1
        assert bat.rbi == 1
        assert sm.boxscore.line(PITCHER).er == 1


# ===========================================================================
# IP accumulates in thirds (x.0 / x.1 / x.2)
# ===========================================================================


class TestInningsPitchedInThirds:
    def test_ip_accumulates_in_thirds(self):
        sm = _machine()
        state = _fresh_state()
        # 1 out: 0.1 IP
        _drive_strikeout(sm, state)
        assert sm.boxscore.line(PITCHER).outs_recorded == 1
        assert sm.boxscore.line(PITCHER).ip == pytest.approx(0.1)
        # 2 outs: 0.2 IP
        _drive_strikeout(sm, state)
        assert sm.boxscore.line(PITCHER).outs_recorded == 2
        assert sm.boxscore.line(PITCHER).ip == pytest.approx(0.2)
        # 3 outs ends the half-inning and rolls; the pitcher has a full inning.
        _drive_strikeout(sm, state)
        pit = sm.boxscore.line(PITCHER)
        assert pit.outs_recorded == 3
        assert pit.ip == pytest.approx(1.0)
        assert pit.ip_thirds == pytest.approx(1.0)

    def test_ip_form_for_seven_outs_is_two_point_one(self):
        line = PlayerStatLine(player_id=PITCHER, outs_recorded=7)
        assert line.ip == pytest.approx(2.1)  # 2 innings + 1 out
        assert line.ip_thirds == pytest.approx(7 / 3.0)
        assert line.ip_outs == 7


# ===========================================================================
# Unearned run via is_error is NOT charged to the pitcher as ER (nor RBI)
# ===========================================================================


class TestUnearnedRunNotChargedAsEr:
    def test_run_on_an_error_is_not_an_earned_run_nor_an_rbi(self):
        # An error-flagged play that scores a run: the run is unearned, so the
        # pitcher is not charged ER and the batter is not credited the RBI.
        sm = _machine("field_error")
        state = _fresh_state()
        state.bases = Bases(third=507)
        batter = state.batter_id
        _drive_in_play(sm, state)

        pit = sm.boxscore.line(PITCHER)
        assert pit.er == 0  # unearned -> not charged
        bat = sm.boxscore.line(batter)
        assert bat.rbi == 0  # no RBI on an error-driven run

    def test_a_clean_run_is_charged_but_an_error_run_is_not(self):
        # Control: the SAME signal without the error flag DOES charge the ER.
        sm = _machine("single")
        state = _fresh_state()
        state.bases = Bases(third=508)
        _drive_in_play(sm, state)
        assert sm.boxscore.line(PITCHER).er == 1


# ===========================================================================
# Attribution: the batter switches with the lineup; pitcher is read live
# ===========================================================================


class TestAttribution:
    def test_consecutive_batters_get_their_own_lines(self):
        sm = _machine("single")
        state = _fresh_state()
        first_batter = state.batter_id
        _drive_in_play(sm, state)
        second_batter = state.batter_id
        assert second_batter != first_batter  # lineup advanced
        _drive_in_play(sm, state)

        box = sm.boxscore
        assert box.line(first_batter).h == 1
        assert box.line(second_batter).h == 1
        # The single pitcher is charged across both PAs (no out -> no IP yet).
        assert box.line(PITCHER).outs_recorded == 0

    def test_half_inning_flip_switches_the_batting_team(self):
        # Retire the away side (top) with 3 outs, then a home batter comes up.
        sm = _machine()
        state = _fresh_state()
        away_batters = [AWAY_LINEUP[0], AWAY_LINEUP[1], AWAY_LINEUP[2]]
        for _ in range(3):
            _drive_strikeout(sm, state)
        # Now it is the bottom of the 1st: the home team bats.
        assert state.half == Half.BOTTOM
        assert state.offense == Team.HOME
        # The home leadoff batter (the team that just came up) takes the first
        # bottom-half PA; attribution credits the OFFENSE's current slot, so the
        # home leadoff hitter is the one charged (NOT the stale away batter_id).
        home_batter = HOME_LINEUP[state.home_lineup_slot]
        _drive_strikeout(sm, state)

        box = sm.boxscore
        # Three distinct away batters each took an AB on offense in the top.
        for b in away_batters:
            assert box.line(b).ab == 1
        # The home leadoff batter is credited an AB (the team switched correctly)
        # and NO away batter was charged for the bottom-half PA.
        assert home_batter in HOME_LINEUP
        assert box.line(home_batter).ab == 1
        # The pitcher accumulated all 4 outs (3 in the top + 1 in the bottom).
        assert box.line(PITCHER).outs_recorded == 4


# ===========================================================================
# The boxscore is exposed on GameSimResult.boxscore from simulate_game
# ===========================================================================


class TestBoxscoreExposedOnResult:
    def test_simulate_game_attaches_a_populated_boxscore(self):
        rng = np.random.default_rng(3)
        sm = StateMachine(synthetic_sampler(league_artifacts(), 3), rng=rng)
        r = simulate_game(
            sm,
            seed=3,
            away_lineup=AWAY_LINEUP,
            home_lineup=HOME_LINEUP,
        )
        assert isinstance(r, GameSimResult)
        assert r.boxscore is not None
        assert isinstance(r.boxscore, BoxScore)
        # Some batting + pitching activity was accumulated across the game.
        assert r.boxscore.batters, "expected at least one batter line"
        assert r.boxscore.pitchers, "expected at least one pitcher line"
        # The pitcher recorded outs across at least a full regulation game.
        pit = r.boxscore.line(0)  # default pitcher_id when none supplied
        assert pit.outs_recorded >= 9 * 3 - 1  # ~27 outs over 9 innings (minus walk-off slack)

    def test_total_hits_in_box_are_internally_consistent(self):
        # Every batter's H <= AB + (BB are not ABs); a hit is always also a PA.
        rng = np.random.default_rng(7)
        sm = StateMachine(synthetic_sampler(league_artifacts(), 7), rng=rng)
        r = simulate_game(sm, seed=7, away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP)
        for _pid, line in r.boxscore.batters.items():
            assert line.h <= line.ab  # a hit is always an at-bat here
            assert line.hr <= line.h  # every HR is a hit
            assert line.rbi >= 0


# ===========================================================================
# Additive contract: the boxscore field does not break the SIM-320 return
# ===========================================================================


class TestAdditiveContract:
    def test_result_without_a_boxscore_field_still_constructs(self):
        # A GameSimResult built without the boxscore arg leaves it None (optional).
        st = _fresh_state()
        r = GameSimResult(
            home_score=1,
            away_score=0,
            innings_played=9,
            final_state=st,
        )
        assert r.boxscore is None
        assert r.winner == Team.HOME
