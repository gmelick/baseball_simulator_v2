"""
test_backend_sim319.py
======================
Unit tests for SIM-319 -- **step 6 (fielding)** + **step 7 (baserunning + steals)**
of the Phase-4 simulation loop in ``simulation/sim_loop.py`` (Sprint 2 of the
loop build), plus the §5.4 dropped-third-strike edge.

These tests run with NO live DuckDB/FAISS: fielding is driven by an injected
batted-ball sample / a fake :class:`PlayResolver`, steals are resolved against an
injected ``sim.stolen_base_pool`` accessor with a fixed ``numpy`` rng, mirroring
the SIM-302/303/316/317/318 "inject the signal" pattern.

Coverage (the SIM-319 acceptance criteria):
  * fielding: an in-play OUT records the out and an in-play HIT does not, with the
    run/base-out delta produced via ``run_resolution.resolve_runs`` (provenance on
    the PlayResult), not inline;
  * baserunning: a single advances runners + a double scores a runner from 2B,
    both with the run resolved through ``resolve_runs``;
  * steals: a staged attempt resolving SAFE (runner advances) and CAUGHT
    (runner removed + an out recorded), the steal pool sampled for the outcome;
  * dropped third strike (§5.4): a swinging K3 with 1B open / two outs lets the
    batter reach when the resolver/edge fires.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half
from simulation.sim_loop import (
    EVENT_STRIKEOUT,
    EVENT_WALK,
    STEAL_CAUGHT,
    STEAL_SAFE,
    FieldingSignal,
    PlayResolver,
    StateMachine,
    _SampledStealPool,
)

SEASON = 2024
PITCHER = 477132


def _fresh_state(**kw) -> GameState:
    base = {"pitcher_id": PITCHER, "bat_hand": "R", "season": SEASON, "batter_id": 900}
    base.update(kw)
    return GameState(**base)


class _InjectedResolver(PlayResolver):
    """A resolver that returns a fixed FieldingSignal (no DB/FAISS).

    Also supports the in-play hand-off via ``_injected_battedball`` so the
    StateMachine's no-sampler path still reaches step 6/7.
    """

    def __init__(self, signal: FieldingSignal, *, dropped_k: bool = False):
        self._signal = signal
        self._dropped_k = dropped_k
        # A truthy sentinel so the loop's no-sampler in-play path resolves.
        self._injected_battedball = {"event": signal.event}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        return self._signal

    def dropped_third_strike(self, state, result) -> bool:
        return self._dropped_k


def _sig(event, hits, outs, runs, **kw) -> FieldingSignal:
    return FieldingSignal(event=event, result_hits=hits, result_outs=outs, result_runs=runs, **kw)


# ===========================================================================
# Step 6 — fielding resolution: out vs hit
# ===========================================================================


class TestFieldingResolution:
    def test_in_play_out_records_an_out_via_resolve_runs(self):
        sig = _sig("field_out", hits=0, outs=1, runs=0, fielder_id=12, is_error=False)
        sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
        state = _fresh_state(balls=1, strikes=1)
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.is_contact is True
        assert r.pa_terminal is True
        assert r.event == "field_out"
        assert r.outs_recorded == 1
        assert r.fielder_id == 12
        assert r.is_error is False
        assert state.outs == 1
        # The run value was resolved by run_resolution (RE24 delta), NOT inline.
        assert r.run_resolution_method == "re24_delta"
        assert r.canonical_event == "field_out"
        assert r.re_start is not None and r.re_end is not None

    def test_in_play_hit_records_no_out_and_reaches_base(self):
        sig = _sig("single", hits=1, outs=0, runs=0, fielder_id=7)
        sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
        state = _fresh_state()
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.event == "single"
        assert r.outs_recorded == 0
        assert state.outs == 0
        # The batter reached first base (no out, base occupancy grew).
        assert state.bases.occupancy == (True, False, False)
        assert r.run_resolution_method == "re24_delta"

    def test_error_flag_is_recorded(self):
        sig = _sig("field_error", hits=1, outs=0, runs=0, fielder_id=5, is_error=True)
        sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
        state = _fresh_state()
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.is_error is True
        assert r.fielder_id == 5
        assert state.outs == 0


# ===========================================================================
# Step 7 — baserunner advancement (runs via resolve_runs)
# ===========================================================================


class TestBaserunnerAdvancement:
    def test_single_advances_existing_runner(self):
        sig = _sig("single", hits=1, outs=0, runs=0)
        sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
        state = _fresh_state(batter_id=900)
        state.bases = Bases(first=101)  # runner on 1B
        r = sm.step_pitch(state, pitch_outcome="in_play")
        # Runner 101 pushed 1B->2B; batter (900) to 1B.
        assert state.bases.first == 900
        assert state.bases.second == 101
        assert r.baserunner_advances.get(101) == 2
        assert r.baserunner_advances.get(900) == 1
        assert r.run_resolution_method == "re24_delta"

    def test_double_scores_a_runner_from_second_via_resolve_runs(self):
        # The pool carried result_runs=1 (runner from 2B scores on the double).
        sm = StateMachine(rng=np.random.default_rng(0))
        resolver = _InjectedResolver(_sig("double", hits=2, outs=0, runs=1))
        # Carry the sampled result_runs through the injected sample.
        resolver._injected_battedball = {"event": "double", "result_runs": 1}
        sm.resolver = resolver
        state = _fresh_state(batter_id=900)
        state.bases = Bases(second=202)  # runner on 2B
        assert state.away_score == 0
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.event == "double"
        assert r.runs_scored == 1
        # The run physically scored (committed to the offense's score).
        assert state.away_score == 1  # top of 1st -> AWAY bats
        # The run VALUE went through resolve_runs (RE24 delta), not inline.
        assert r.run_resolution_method == "re24_delta"
        assert r.runs == pytest.approx(r.re_end - r.re_start + 1.0)
        # The batter is standing on 2B after the double.
        assert state.bases.second == 900

    def test_home_run_clears_the_bases_and_scores_all(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        resolver = _InjectedResolver(_sig("home_run", hits=4, outs=0, runs=2))
        resolver._injected_battedball = {"event": "home_run", "result_runs": 2}
        sm.resolver = resolver
        state = _fresh_state(batter_id=900)
        state.bases = Bases(first=101)  # one on -> 2 runs score (runner + batter)
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.event == "home_run"
        assert r.runs_scored == 2
        assert state.away_score == 2
        assert state.bases.occupancy == (False, False, False)  # bases cleared


# ===========================================================================
# Walk forces runners (run via resolve_runs)
# ===========================================================================


class TestWalkForcing:
    def test_bases_loaded_walk_forces_a_run(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, batter_id=900)
        state.bases = Bases(first=101, second=102, third=103)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.event == EVENT_WALK
        assert r.outs_recorded == 0
        assert r.runs_scored == 1  # runner from 3B forced home
        assert state.away_score == 1
        assert r.run_resolution_method == "re24_delta"

    def test_walk_with_first_open_forces_no_run(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, batter_id=900)
        state.bases = Bases(second=102)  # 1B open -> no force
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.event == EVENT_WALK
        assert r.runs_scored == 0
        assert state.bases.first == 900  # batter to 1B
        assert state.bases.second == 102  # 2B runner not forced

    def test_bases_loaded_walk_records_forced_advances(self):
        """SIM-414: a bases-loaded walk records every forced advance in
        ``baserunner_advances`` — including the runner on 3B who scored
        (``end_base == 0``). Previously only the batter's advance was recorded
        and the per-runner R credit silently dropped the forced run."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, batter_id=900)
        state.bases = Bases(first=101, second=102, third=103)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.event == EVENT_WALK
        assert r.runs_scored == 1
        # Every forced runner is recorded.
        assert r.baserunner_advances[103] == 0  # 3B runner scored
        assert r.baserunner_advances[102] == 3  # 2B -> 3B
        assert r.baserunner_advances[101] == 2  # 1B -> 2B
        assert r.baserunner_advances[900] == 1  # batter to 1B

    def test_bases_loaded_walk_credits_per_runner_R(self):
        """SIM-414: the per-runner R credit in :meth:`_accumulate_pa` fires
        for the runner on 3B forced home by a bases-loaded walk, so the
        boxscore Σ r equals the linescore run total."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, batter_id=900)
        state.bases = Bases(first=101, second=102, third=103)
        sm.step_pitch(state, pitch_outcome="ball")
        # Runner 103 crossed home -> credited 1 R; the other forced runners did
        # not score, so their r stays 0.
        assert sm.boxscore.line(103).r == 1
        assert sm.boxscore.line(102).r == 0
        assert sm.boxscore.line(101).r == 0
        assert sm.boxscore.line(900).r == 0

    def test_two_runners_walk_no_run_but_advances_recorded(self):
        """SIM-414: a walk with runners on 1B+2B records both forced advances
        (no run scores) — the dict is now semantically complete."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(balls=3, batter_id=900)
        state.bases = Bases(first=101, second=102)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.event == EVENT_WALK
        assert r.runs_scored == 0
        assert r.baserunner_advances[102] == 3  # 2B -> 3B
        assert r.baserunner_advances[101] == 2  # 1B -> 2B
        assert r.baserunner_advances[900] == 1
        # No 0-base advance -> no R credited to anyone.
        assert sm.boxscore.line(102).r == 0
        assert sm.boxscore.line(101).r == 0


# ===========================================================================
# Steals — decision (pre-pitch) + outcome (step 7) against the steal pool
# ===========================================================================


class TestSteals:
    def test_safe_steal_advances_the_runner(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(batter_id=900)
        state.bases = Bases(first=101)
        # Stage a steal of 2B that resolves SAFE.
        sm.stage_steal(runner_id=101, from_base=1, to_base=2, safe=True)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.steal_attempted is True
        assert r.steal_outcome == STEAL_SAFE
        assert state.bases.first is None
        assert state.bases.second == 101  # runner advanced 1B->2B
        assert state.outs == 0
        assert r.pa_terminal is False  # ball is non-terminal

    def test_caught_stealing_records_an_out_via_resolve_runs(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(batter_id=900)
        state.bases = Bases(first=101)
        sm.stage_steal(runner_id=101, from_base=1, to_base=2, safe=False)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.steal_attempted is True
        assert r.steal_outcome == STEAL_CAUGHT
        assert state.bases.first is None  # runner removed (out)
        assert state.outs == 1  # one out recorded
        assert r.outs_recorded == 1
        assert r.run_resolution_method == "re24_delta"

    def test_steal_outcome_sampled_from_the_stolen_base_pool(self):
        # An injected sim.stolen_base_pool of all-success rows -> the staged
        # steal (no explicit safe=) draws SAFE from the pool.
        class _Sim:
            stolen_base_pool = _SampledStealPool(rows=[(True, 1.0), (True, 1.0)])

        sm = StateMachine(rng=np.random.default_rng(1), sim=_Sim())
        state = _fresh_state(batter_id=900)
        state.bases = Bases(first=101)
        sm.stage_steal(runner_id=101, from_base=1, to_base=2)  # safe drawn from pool
        r = sm.step_pitch(state, pitch_outcome="called_strike")
        assert r.steal_attempted is True
        assert r.steal_outcome == STEAL_SAFE  # pool was all-success
        assert state.bases.second == 101

    def test_caught_stealing_can_be_the_third_out(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(batter_id=900, outs=2)
        state.bases = Bases(first=101)
        sm.stage_steal(runner_id=101, from_base=1, to_base=2, safe=False)
        r = sm.step_pitch(state, pitch_outcome="ball")
        assert r.steal_outcome == STEAL_CAUGHT
        # The 3rd out ended the half-inning: rolled to the bottom, bases clear.
        assert state.half == Half.BOTTOM
        assert state.outs == 0
        assert state.bases.occupancy == (False, False, False)


# ===========================================================================
# Dropped third strike (§5.4)
# ===========================================================================


class TestDroppedThirdStrike:
    def test_uncaught_k3_with_first_base_open_lets_batter_reach(self):
        # Resolver signals the catcher dropped it; 1B is open -> batter reaches.
        resolver = _InjectedResolver(_sig("field_out", 0, 1, 0), dropped_k=True)
        sm = StateMachine(resolver=resolver, rng=np.random.default_rng(0))
        state = _fresh_state(balls=0, strikes=2, batter_id=900)  # 1B open
        r = sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert r.pa_terminal is True
        assert r.outs_recorded == 0  # batter reached (no out)
        assert state.outs == 0
        assert state.bases.first == 900  # batter safe at 1B

    def test_ordinary_k3_records_an_out_when_edge_does_not_fire(self):
        # No dropped-K signal -> an ordinary strikeout (one out).
        resolver = _InjectedResolver(_sig("field_out", 0, 1, 0), dropped_k=False)
        sm = StateMachine(resolver=resolver, rng=np.random.default_rng(0))
        state = _fresh_state(balls=0, strikes=2, batter_id=900)
        r = sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert r.event == EVENT_STRIKEOUT
        assert r.outs_recorded == 1
        assert state.outs == 1
        assert state.bases.first is None  # batter did NOT reach

    def test_dropped_k3_not_eligible_with_first_occupied_and_under_two_outs(self):
        # 1B occupied AND fewer than two outs -> the edge is NOT eligible even if
        # the resolver would drop it; an ordinary strikeout results.
        resolver = _InjectedResolver(_sig("field_out", 0, 1, 0), dropped_k=True)
        sm = StateMachine(resolver=resolver, rng=np.random.default_rng(0))
        state = _fresh_state(balls=0, strikes=2, outs=0, batter_id=900)
        state.bases = Bases(first=101)  # 1B occupied, 0 outs
        r = sm.step_pitch(state, pitch_outcome="swinging_strike")
        assert r.event == EVENT_STRIKEOUT
        assert r.outs_recorded == 1
        assert state.outs == 1


# ===========================================================================
# Run/base-out discipline — every delta goes through resolve_runs
# ===========================================================================


class TestRunResolutionDiscipline:
    def test_every_terminal_play_carries_run_resolution_provenance(self):
        # Out, hit, walk, K -> each commits run_resolution_method (proof the loop
        # routed the run/base-out delta through resolve_runs, not inline).
        cases = [
            ("in_play", _sig("field_out", 0, 1, 0)),
            ("in_play", _sig("single", 1, 0, 0)),
        ]
        for outcome, sig in cases:
            sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
            r = sm.step_pitch(_fresh_state(), pitch_outcome=outcome)
            assert r.run_resolution_method == "re24_delta"

        # Walk + K go through resolve_runs too.
        sm = StateMachine(rng=np.random.default_rng(0))
        rw = sm.step_pitch(_fresh_state(balls=3), pitch_outcome="ball")
        assert rw.event == EVENT_WALK
        assert rw.run_resolution_method == "re24_delta"

        sm2 = StateMachine(rng=np.random.default_rng(0))
        rk = sm2.step_pitch(_fresh_state(strikes=2), pitch_outcome="called_strike")
        assert rk.event == EVENT_STRIKEOUT
        assert rk.run_resolution_method == "re24_delta"

    def test_scores_stay_non_negative_and_outs_bounded(self):
        sig = _sig("field_out", 0, 1, 0)
        sm = StateMachine(resolver=_InjectedResolver(sig), rng=np.random.default_rng(0))
        state = _fresh_state()
        for _ in range(3):
            sm.step_pitch(state, pitch_outcome="in_play")
        # 3 outs -> half rolled; scores never went negative.
        assert state.home_score >= 0 and state.away_score >= 0
        assert 0 <= state.outs <= 2
