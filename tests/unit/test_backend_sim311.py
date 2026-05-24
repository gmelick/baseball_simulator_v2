"""
test_backend_sim311.py
======================
Unit tests for SIM-311 -- the ``GameState`` / ``PlayResult`` dataclass contract
(``simulation/game_state.py``).

This is a CONTRACT deliverable: the tests construct + mutate a ``GameState``,
assert the lightweight invariants, round-trip a ``PlayResult``, and confirm the
dataclasses expose the fields the SIM-310 spec
(``docs/architecture/2026-06-17-phase4-sim-loop-spec.md`` §2 step I/O) requires.

No DB / no FAISS: plain construction only (the state-machine that drives these
types is SIM-316; the invalid-state harness is SIM-326).
"""

from __future__ import annotations

import pytest

from simulation.game_state import (
    BALLS_FOR_WALK,
    CONTACT_PITCH_OUTCOME,
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
    base = dict(pitcher_id=477132, bat_hand="L", season=2024)
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
    # Manager context hook exists and is typed (SIM-323 fills it).
    assert isinstance(gs.manager, ManagerContext)


def test_sampler_prefilter_keys_present():
    """Spec §4.3 — (pitcher_id, bat_hand, season) are tile pre-filter keys."""
    gs = _fresh_state(pitcher_id=123, bat_hand="R", season=2023)
    assert gs.sampler_prefilter() == (123, "R", 2023)


# ===========================================================================
# Mutation + invariants
# ===========================================================================


def test_count_mutation_and_mid_count_invariant():
    gs = _fresh_state()
    gs.add_ball()
    gs.add_ball()
    gs.add_ball()          # 3-0
    gs.add_strike()
    gs.add_strike()        # 3-2
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
    gs.add_strike()        # 0-2 (live)
    gs.assert_count_valid(mid_count=True)
    gs.add_strike()        # 0-3 (terminal strikeout)
    assert gs.strikes == STRIKES_FOR_STRIKEOUT
    with pytest.raises(ValueError):
        gs.assert_count_valid(mid_count=True)


def test_outs_invariant_during_play_and_at_three():
    gs = _fresh_state()
    gs.record_out()
    gs.record_out()        # 2 outs
    assert gs.outs == 2
    gs.assert_outs_valid(in_play=True)
    assert gs.is_half_inning_over() is False
    gs.record_out()        # 3rd out
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
    gs = _fresh_state()              # top of 1st -> offense is AWAY
    assert gs.offense == Team.AWAY
    gs.add_runs(2)                   # credits the offense (away)
    assert gs.away_score == 2 and gs.home_score == 0
    assert gs.score_diff == 2       # offense leads by 2
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
    bases = Bases(first=101, third=303)   # runners on 1B and 3B
    assert bases.occupancy == (True, False, True)
    assert bases.count_on_base == 2
    # bit0=1B, bit2=3B -> 0b101 == 5 (matches run_resolution encoding).
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
    """A GameState's (outs, runners_state) plug straight into the SIM-312 RE24
    matrix -- the encodings are byte-compatible."""
    gs = _fresh_state()
    gs.bases = Bases(first=10, second=20)   # 1B+2B -> 0b011 == 3
    gs.record_out()                          # 1 out
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
# PlayResult round-trip + spec field coverage
# ===========================================================================


def test_playresult_roundtrip_to_scaffold_dict():
    """PlayResult generalizes the scaffold dict; as_scaffold_dict() restores it."""
    pitch_sample = {"row_id": 42, "pitch_outcome": "in_play",
                    "distance": 0.1, "weight": 0.2, "tile": "2024/477132/L",
                    "fellback": False}
    bb_sample = {"row_id": 99, "event": "double", "distance": 0.3,
                 "weight": 0.4, "tile": "2024/battedball/L", "fellback": False}
    pr = PlayResult(
        pitch_outcome=CONTACT_PITCH_OUTCOME,
        is_contact=True,
        pa_terminal=True,
        event="double",
        runs=1.0,
        run_resolution_method="re24_delta",
        canonical_event="double",
        re_start=0.51,
        re_end=1.16,
        runs_scored=0,
        exit_velo=104.2,
        launch_angle=18.0,
        spray_angle=-12.0,
        fielder_id=8,
        outs_recorded=0,
        baserunner_advances={101: 3},
        pitch_sample=pitch_sample,
        battedball_sample=bb_sample,
        fellback=False,
    )

    d = pr.as_scaffold_dict()
    # Exactly the scaffold dict keys, restored faithfully.
    assert set(d) == {"pitch_outcome", "is_contact", "event", "runs",
                      "fellback", "pitch_sample", "battedball_sample"}
    assert d["pitch_outcome"] == CONTACT_PITCH_OUTCOME
    assert d["is_contact"] is True
    assert d["event"] == "double"
    assert d["runs"] == 1.0
    assert d["fellback"] is False
    assert d["pitch_sample"] is pitch_sample
    assert d["battedball_sample"] is bb_sample


def test_playresult_defaults_non_contact():
    """A non-contact pitch result has no batted-ball payload + neutral deltas."""
    pr = PlayResult(pitch_outcome="ball")
    assert pr.is_contact is False
    assert pr.event is None
    assert pr.battedball_sample is None
    assert pr.runs == 0.0
    assert pr.outs_recorded == 0
    assert pr.baserunner_advances == {}
    assert pr.steal_attempted is False
    assert pr.next_state is None


def test_playresult_carries_run_resolution_provenance():
    """SIM-312 alignment: PlayResult exposes the run-resolution provenance so the
    loop never re-derives run values inline (spec §8)."""
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
    for fld in ("runs", "run_resolution_method", "canonical_event",
                "re_start", "re_end", "runs_scored"):
        assert hasattr(pr, fld)
    assert pr.run_resolution_method == "re24_delta"


# ===========================================================================
# Spec §2 step-I/O field coverage (the contract other tickets compile against)
# ===========================================================================


def test_gamestate_exposes_spec_step1_situation_context_fields():
    """Spec step 1 reads: count, outs, inning/half, base state, score diff,
    leverage, pitcher pitch-count, batter PA-count, park."""
    gs = _fresh_state()
    for fld in ("balls", "strikes",           # count (step 4 / §5.1)
                "outs",                         # §6.1
                "inning", "half",               # inning/half
                "bases",                        # base state (step 7)
                "home_score", "away_score",     # score (step 8)
                "pitcher_id", "bat_hand", "season",  # pre-filter (§4.3)
                "batter_id", "throw_hand",      # matchup (step 2)
                "pitcher_pitch_count", "batter_pa_count", "park",  # step-1 reads
                "away_lineup", "home_lineup",   # batting order (§6.1)
                "away_lineup_slot", "home_lineup_slot",  # lineup pointers (§6.1)
                "manager", "seed"):             # manager hook (§3) / RNG (§6.3)
        assert hasattr(gs, fld), f"GameState missing spec field: {fld}"
    # Derived situation reads.
    for prop in ("offense", "defense", "score_diff", "runners_state"):
        assert hasattr(gs, prop)


def test_manager_context_fields_present():
    """Spec §3 pre-pitch hook context (steal/IBB/pitch-out/bullpen) — SIM-323."""
    mc = ManagerContext()
    for fld in ("leverage", "green_light_rate", "bullpen_available",
                "intentional_walk_signalled", "pitch_out_signalled"):
        assert hasattr(mc, fld)


def test_playresult_exposes_spec_step_deltas():
    """Spec steps 5/6/7 deltas + step-8 next-state pointer all have typed homes."""
    pr = PlayResult(pitch_outcome="in_play")
    for fld in ("pitch_outcome", "is_contact", "pa_terminal", "event",  # step 3/4
                "exit_velo", "launch_angle", "spray_angle",             # step 5
                "fielder_id", "is_error", "outs_recorded",              # step 6
                "baserunner_advances", "steal_attempted", "steal_outcome",  # step 7
                "runs", "runs_scored",                                  # step 8 runs
                "pitch_sample", "battedball_sample", "fellback",        # raw payloads
                "next_state"):                                          # step 8 state
        assert hasattr(pr, fld), f"PlayResult missing spec field: {fld}"
