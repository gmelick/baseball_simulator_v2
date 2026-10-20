"""Tests of the ``GameState`` / ``PlayResult`` dataclass contract.

The types live in ``simulation/game_state.py``. The tests construct and change
a ``GameState``, assert its light invariants, build a ``PlayResult``, and
confirm the dataclasses expose the fields the loop reads and writes. No
database: plain construction only.
"""

from __future__ import annotations

import pytest

from simulation.game_state import (
    BALLS_FOR_WALK,
    OUTS_PER_INNING,
    STRIKES_FOR_STRIKEOUT,
    Bases,
    GameState,
    Half,
    ManagerContext,
    PlayResult,
    Team,
)
from simulation.run_resolution import re24_value

# ===========================================================================
# Construction
# ===========================================================================


def _fresh_state(**overrides) -> GameState:
    """A 'top of the 1st, 0-0, nobody on, 0 outs' state."""
    base = {"pitcher_id": 477132, "bat_hand": "L", "season": 2024}
    base.update(overrides)
    return GameState(**base)


def test_gamestate_constructs_with_sane_defaults():
    gs = _fresh_state()
    assert gs.balls == 0 and gs.strikes == 0
    assert gs.outs == 0
    assert gs.inning == 1
    assert gs.half == Half.TOP
    assert gs.home_score == 0 and gs.away_score == 0
    assert gs.bases.count_on_base == 0
    assert gs.runners_state == 0
    # The manager context exists and is typed (the manager hooks fill it).
    assert isinstance(gs.manager, ManagerContext)


def test_sampler_prefilter_keys_present():
    """``sampler_prefilter`` returns the (pitcher_id, bat_hand, season) tuple."""
    gs = _fresh_state(pitcher_id=123, bat_hand="R", season=2023)
    assert gs.sampler_prefilter() == (123, "R", 2023)


# ===========================================================================
# Mutation + invariants
# ===========================================================================


def test_count_mutation_and_mid_count_invariant():
    gs = _fresh_state()
    gs.add_ball()
    gs.add_ball()
    gs.add_ball()  # 3-0
    gs.add_strike()
    gs.add_strike()  # 3-2
    assert (gs.balls, gs.strikes) == (3, 2)
    # 3-2 is the max live mid-count; invariant holds.
    gs.assert_count_valid(mid_count=True)
    # A 4th ball is terminal -> the mid-count invariant must reject it.
    gs.add_ball()
    assert gs.balls == BALLS_FOR_WALK
    with pytest.raises(ValueError):
        gs.assert_count_valid(mid_count=True)
    # ...but the transient terminal value is allowed when mid_count=False.
    gs.assert_count_valid(mid_count=False)
    gs.reset_count()
    assert (gs.balls, gs.strikes) == (0, 0)


def test_strike_terminal_invariant():
    gs = _fresh_state()
    gs.add_strike()
    gs.add_strike()  # 0-2 (live)
    gs.assert_count_valid(mid_count=True)
    gs.add_strike()  # 0-3 (terminal strikeout)
    assert gs.strikes == STRIKES_FOR_STRIKEOUT
    with pytest.raises(ValueError):
        gs.assert_count_valid(mid_count=True)


def test_outs_invariant_during_play_and_at_three():
    gs = _fresh_state()
    gs.record_out()
    gs.record_out()  # 2 outs
    assert gs.outs == 2
    gs.assert_outs_valid(in_play=True)
    assert gs.is_half_inning_over() is False
    gs.record_out()  # 3rd out
    assert gs.outs == OUTS_PER_INNING
    assert gs.is_half_inning_over() is True
    # The third out is terminal -> in-play invariant rejects it.
    with pytest.raises(ValueError):
        gs.assert_outs_valid(in_play=True)
    # ...but inspecting the transient terminal state is allowed.
    gs.assert_outs_valid(in_play=False)
    gs.reset_outs()
    assert gs.outs == 0


def test_score_non_negative_and_offense_credit():
    gs = _fresh_state()  # top of 1st -> offense is AWAY
    assert gs.offense == Team.AWAY
    gs.add_runs(2)  # credits the offense (away)
    assert gs.away_score == 2 and gs.home_score == 0
    assert gs.score_diff == 2  # offense leads by 2
    gs.assert_score_valid()
    # Negative runs are rejected.
    with pytest.raises(ValueError):
        gs.add_runs(-1)


def test_score_diff_flips_with_half():
    gs = _fresh_state()
    gs.away_score = 1
    gs.home_score = 3
    # Top of 1st: offense=away, diff = away - home = -2.
    assert gs.score_diff == -2
    gs.half = Half.BOTTOM
    # Bottom: offense=home, diff = home - away = +2.
    assert gs.offense == Team.HOME
    assert gs.score_diff == 2


# ===========================================================================
# Base occupancy consistency + RE24 interop
# ===========================================================================


def test_base_occupancy_and_runners_state_encoding():
    bases = Bases(first=101, third=303)  # runners on 1B and 3B
    assert bases.occupancy == (True, False, True)
    assert bases.count_on_base == 2
    # bit0=1B, bit2=3B -> 0b101 == 5 (the run_resolution encoding).
    assert bases.runners_state == 0b101
    bases.assert_consistent()
    # Loaded bases -> 0b111 == 7.
    loaded = Bases(first=1, second=2, third=3)
    assert loaded.runners_state == 0b111
    loaded.clear()
    assert loaded.runners_state == 0 and loaded.count_on_base == 0


def test_negative_runner_id_rejected():
    with pytest.raises(ValueError):
        Bases(first=-5).assert_consistent()


def test_gamestate_runners_state_feeds_re24():
    """A GameState's (outs, runners_state) plug straight into the run
    expectancy (RE24) matrix: the encodings are byte-compatible."""
    gs = _fresh_state()
    gs.bases = Bases(first=10, second=20)  # 1B+2B -> 0b011 == 3
    gs.record_out()  # 1 out
    assert gs.runners_state == 3
    # No KeyError == the GameState state is a valid RE24 lookup key.
    re = re24_value(gs.outs, gs.runners_state)
    assert isinstance(re, float) and re > 0.0


def test_assert_invariants_aggregate():
    gs = _fresh_state()
    gs.add_ball()
    gs.record_out()
    gs.bases = Bases(second=7)
    gs.add_runs(1)
    # A committed, ready-for-next-pitch state passes the aggregate guard.
    gs.assert_invariants(in_play=True)


# ===========================================================================
# PlayResult defaults and field coverage
# ===========================================================================


def test_playresult_defaults_non_contact():
    """A non-contact pitch result has no batted-ball payload and neutral deltas."""
    pr = PlayResult(pitch_outcome="ball")
    assert pr.is_contact is False
    assert pr.event is None
    assert pr.runs == 0.0
    assert pr.outs_recorded == 0
    assert pr.baserunner_advances == {}
    assert pr.steal_attempted is False
    assert pr.next_state is None


def test_playresult_carries_run_resolution_provenance():
    """PlayResult carries where its run value came from, so the loop never
    derives run values again inline."""
    pr = PlayResult(
        pitch_outcome="in_play",
        is_contact=True,
        event="field_out",
        runs=-0.24,
        run_resolution_method="re24_delta",
        canonical_event="field_out",
        re_start=0.51,
        re_end=0.27,
    )
    for fld in (
        "runs",
        "run_resolution_method",
        "canonical_event",
        "re_start",
        "re_end",
        "runs_scored",
    ):
        assert hasattr(pr, fld)
    assert pr.run_resolution_method == "re24_delta"


# ===========================================================================
# The fields the loop reads and writes (the contract other modules compile against)
# ===========================================================================


def test_gamestate_exposes_spec_step1_situation_context_fields():
    """The situation the loop reads: count, outs, inning/half, base state,
    score, matchup, pitch count, plate-appearance count, park and lineups."""
    gs = _fresh_state()
    for fld in (
        "balls",
        "strikes",  # the count
        "outs",
        "inning",
        "half",  # inning/half
        "bases",  # the base state
        "home_score",
        "away_score",  # the score
        "pitcher_id",
        "bat_hand",
        "season",  # the matchup key
        "batter_id",
        "throw_hand",  # the matchup
        "pitcher_pitch_count",
        "batter_pa_count",
        "park",  # the live park
        "away_lineup",
        "home_lineup",  # the batting orders
        "away_lineup_slot",
        "home_lineup_slot",  # the lineup pointers
        "manager",
        "seed",
    ):  # the manager context / the seed
        assert hasattr(gs, fld), f"GameState missing spec field: {fld}"
    # Derived situation reads.
    for prop in ("offense", "defense", "score_diff", "runners_state"):
        assert hasattr(gs, prop)


def test_manager_context_fields_present():
    """The manager context carries the intentional walk, the pen and the
    leverage. It carries no green-light rate, pitch-out or hit-and-run
    signal: the steal is a draw with the manager's measured rate as a weight
    (the real manager and pen, SIM-427)."""
    mc = ManagerContext()
    for fld in ("leverage", "bullpen_available", "intentional_walk_signalled"):
        assert hasattr(mc, fld)
    for gone in ("green_light_rate", "pitch_out_signalled", "hit_and_run_signalled"):
        assert not hasattr(mc, gone)


def test_playresult_exposes_spec_step_deltas():
    """The pitch's results and the next-state pointer all have typed fields."""
    pr = PlayResult(pitch_outcome="in_play")
    for fld in (
        "pitch_outcome",
        "is_contact",
        "pa_terminal",
        "event",  # the pitch and its event
        "exit_velo",
        "launch_angle",
        "spray_angle",  # the batted ball
        "fielder_id",
        "is_error",
        "outs_recorded",  # the fielding
        "baserunner_advances",
        "steal_attempted",
        "steal_outcome",  # the running game
        "runs",
        "runs_scored",  # the runs
        "next_state",
    ):  # the committed state
        assert hasattr(pr, fld), f"PlayResult missing spec field: {fld}"
