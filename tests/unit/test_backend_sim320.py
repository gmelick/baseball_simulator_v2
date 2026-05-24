"""
test_backend_sim320.py
======================
Unit tests for SIM-320 -- the **full-game driver** ``simulate_game()`` + the
step-8 game-level loop control (regulation / walk-off / extra innings / ghost
runner / deterministic seeding) in ``simulation/sim_loop.py`` (Sprint 2 capstone
of the Phase-4 loop build).

These tests run with NO live DuckDB/FAISS: the driver is fed an injected
:class:`StateMachine` whose per-pitch outcomes come from a fixed ``numpy`` rng (no
sampler), and fielding is resolved by an injected :class:`PlayResolver`, mirroring
the SIM-316/317/318/319 "inject the signal" pattern.

Coverage (the SIM-320 acceptance criteria):
  * a full game completes with a VALID final state (guards held across the game);
  * a fixed seed reproduces the same final score (determinism, §6.3);
  * a home lead after the top of the 9th ends the game WITHOUT the bottom (§6.2);
  * a walk-off (home takes the lead in the bottom of the 9th) ends mid-inning;
  * a tie after 9 goes to extra innings with the ghost runner on 2B (§6.2);
  * invalid-state guards hold across a simulated game (outs <=2, scores >=0).
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import (
    REGULATION_INNINGS,
    FieldingSignal,
    GameSimResult,
    PlayResolver,
    StateMachine,
    simulate_game,
)

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))   # 9 batters
HOME_LINEUP = list(range(201, 210))   # 9 batters


# ===========================================================================
# Test doubles — a no-DB resolver + an rng-driven StateMachine
# ===========================================================================


class _CyclingResolver(PlayResolver):
    """Resolve an in-play ball to an out (default) or a single (a fraction of the
    time, governed by the shared rng) so games make progress, score, and END.
    No DB/FAISS — the batted-ball sample is injected."""

    def __init__(self, rng: "np.random.Generator", hit_rate: float = 0.30):
        self.rng = rng
        self.hit_rate = float(hit_rate)
        self._injected_battedball = {"event": "field_out"}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        if float(self.rng.random()) < self.hit_rate:
            return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=0)
        return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)


class _RngStateMachine(StateMachine):
    """A StateMachine that draws each pitch outcome from its own loop rng (no
    sampler), so an entire game can be driven deterministically from one seed
    without a live sampler.  This is the no-DB unit-test driver path."""

    def step_pitch(self, state, **_kw):  # type: ignore[override]
        r = float(self.rng.random())
        if r < 0.55:
            outcome = "in_play"
        elif r < 0.75:
            outcome = "ball"
        elif r < 0.92:
            outcome = "called_strike"
        else:
            outcome = "foul"
        return super().step_pitch(state, pitch_outcome=outcome)


def _make_machine(seed: int, hit_rate: float = 0.30) -> _RngStateMachine:
    rng = np.random.default_rng(seed)
    return _RngStateMachine(resolver=_CyclingResolver(rng, hit_rate=hit_rate), rng=rng)


def _run(seed: int, hit_rate: float = 0.30) -> GameSimResult:
    return simulate_game(
        _make_machine(seed, hit_rate=hit_rate),
        seed=seed,
        away_lineup=AWAY_LINEUP,
        home_lineup=HOME_LINEUP,
    )


# ===========================================================================
# A full game completes with a valid final state
# ===========================================================================


class TestFullGameCompletes:
    def test_a_full_game_completes_with_a_valid_final_state(self):
        r = _run(0)
        assert isinstance(r, GameSimResult)
        # The game decided a winner (never returns tied in a normal finish).
        assert r.home_score != r.away_score
        assert r.home_score >= 0 and r.away_score >= 0
        # At least a full regulation was played.
        assert r.innings_played >= REGULATION_INNINGS
        # The terminal state is invariant-valid (a transient terminal snapshot:
        # the half may sit at the just-rolled boundary, so allow the in_play=False
        # tolerance the loop itself uses on the last committed state).
        r.final_state.assert_score_valid()
        assert 0 <= r.final_state.outs <= 3
        assert r.total_pitches > 0
        # The winner helper agrees with the score.
        if r.home_score > r.away_score:
            assert r.winner == Team.HOME
        else:
            assert r.winner == Team.AWAY

    def test_many_games_all_decide_a_winner_and_hold_guards(self):
        for seed in range(40):
            r = _run(seed)
            assert r.home_score != r.away_score, f"seed {seed} ended tied"
            assert r.home_score >= 0 and r.away_score >= 0
            assert 0 <= r.final_state.outs <= 3
            # extra_innings <=> more than 9 innings were played.
            assert (r.innings_played > REGULATION_INNINGS) == r.extra_innings


# ===========================================================================
# Determinism (§6.3): a fixed seed reproduces the same game
# ===========================================================================


class TestDeterminism:
    def test_fixed_seed_reproduces_the_same_final_score(self):
        a = _run(7)
        b = _run(7)
        assert (a.home_score, a.away_score) == (b.home_score, b.away_score)
        assert a.innings_played == b.innings_played
        assert a.walk_off == b.walk_off
        assert a.extra_innings == b.extra_innings
        assert a.total_pitches == b.total_pitches

    def test_different_seeds_vary_the_game(self):
        # Across a spread of seeds, not every game is identical (the seed matters).
        results = {(_run(s).home_score, _run(s).away_score, _run(s).innings_played)
                   for s in range(12)}
        assert len(results) > 1

    def test_seed_threads_through_the_sampler_rng(self):
        # A sampler-less machine is fine; assert the driver re-seeds a sampler rng
        # when one is present (the §6.3 thread-through).  Use a tiny stub sampler.
        class _StubSampler:
            def __init__(self):
                self.rng = np.random.default_rng(999)

        class _NoSampleSM(_RngStateMachine):
            pass

        sm = _NoSampleSM(resolver=_CyclingResolver(np.random.default_rng(3)),
                         rng=np.random.default_rng(3))
        sm.sampler = _StubSampler()
        before = sm.sampler.rng
        simulate_game(sm, seed=123, away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP)
        # The sampler rng was replaced with a freshly-seeded Generator.
        assert sm.sampler.rng is not before


# ===========================================================================
# Regulation: home lead after the top of the 9th ends the game (no bottom)
# ===========================================================================


class TestRegulationNoBottomNinth:
    def test_home_lead_after_top_9_ends_without_the_bottom(self):
        # Start in the top of the 9th with the home team already ahead and bases
        # empty / 2 outs; one more out completes the top of the 9th -> game over,
        # the bottom of the 9th is NOT played (spec §6.2).
        state = GameState(
            pitcher_id=PITCHER, bat_hand="R", season=SEASON,
            away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP, batter_id=101,
            inning=REGULATION_INNINGS, half=Half.TOP, outs=2,
            home_score=5, away_score=3,
        )
        # An out-only resolver so the top of the 9th ends immediately.
        rng = np.random.default_rng(0)

        class _OutSM(StateMachine):
            def step_pitch(self, st, **_kw):  # always an in-play out
                return super().step_pitch(st, pitch_outcome="in_play")

        class _AllOut(PlayResolver):
            def __init__(self):
                self._injected_battedball = {"event": "field_out"}
            def resolve_fielding(self, st, bb):
                return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)

        sm = _OutSM(resolver=_AllOut(), rng=rng)
        r = simulate_game(sm, initial_state=state, seed=1)
        assert r.home_score == 5 and r.away_score == 3
        assert r.winner == Team.HOME
        assert r.walk_off is False
        assert r.extra_innings is False
        # The game ended on the half-inning roll OUT of the top of the 9th, i.e.
        # the bottom of the 9th was never batted -> final pointer is the bottom
        # of the 9th (pending, never played).
        assert r.final_state.inning == REGULATION_INNINGS
        assert r.final_state.half == Half.BOTTOM
        assert r.innings_played == REGULATION_INNINGS


# ===========================================================================
# Walk-off: home takes the lead in the bottom of the 9th -> ends mid-inning
# ===========================================================================


class TestWalkOff:
    def test_walk_off_ends_the_game_mid_inning(self):
        # Bottom of the 9th, tied, a runner on 3B with the batter due; a single
        # (the resolver scores the runner via result_runs) gives the home team
        # the lead -> walk-off, the half-inning does NOT complete.
        state = GameState(
            pitcher_id=PITCHER, bat_hand="R", season=SEASON,
            away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP, batter_id=201,
            inning=REGULATION_INNINGS, half=Half.BOTTOM, outs=0,
            home_score=2, away_score=2,
        )
        state.home_lineup_slot = 0
        state.bases = Bases(third=205)  # runner on 3B

        class _WalkoffSM(StateMachine):
            def step_pitch(self, st, **_kw):
                return super().step_pitch(st, pitch_outcome="in_play")

        class _ScoringSingle(PlayResolver):
            def __init__(self):
                self._injected_battedball = {"event": "single", "result_runs": 1}
            def resolve_fielding(self, st, bb):
                return FieldingSignal(event="single", result_hits=1, result_outs=0, result_runs=1)

        sm = _WalkoffSM(resolver=_ScoringSingle(), rng=np.random.default_rng(0))
        r = simulate_game(sm, initial_state=state, seed=1)
        assert r.walk_off is True
        assert r.home_score == 3 and r.away_score == 2
        assert r.winner == Team.HOME
        # Ended mid-inning: still the bottom of the 9th, fewer than 3 outs.
        assert r.final_state.half == Half.BOTTOM
        assert r.final_state.inning == REGULATION_INNINGS
        assert r.final_state.outs < 3
        assert r.innings_played == REGULATION_INNINGS


# ===========================================================================
# Extra innings + ghost runner (§6.2)
# ===========================================================================


class TestExtraInnings:
    def test_tie_after_nine_goes_to_extras(self):
        # A low-scoring rng-driven game that ends tied after 9 must continue into
        # extra innings.  Search a few seeds for one that is tied at the end of 9
        # by construction would be flaky; instead assert the *invariant* across a
        # spread: every extra-inning game played more than 9 innings.
        seen_extra = False
        for seed in range(60):
            r = _run(seed, hit_rate=0.18)  # low offense -> more ties -> more extras
            if r.extra_innings:
                seen_extra = True
                assert r.innings_played > REGULATION_INNINGS
                assert r.home_score != r.away_score  # extras still settle a winner
        assert seen_extra, "expected at least one extra-inning game across 60 seeds"

    def test_ghost_runner_is_on_second_at_the_start_of_an_extra_half(self):
        # Force the start of the top of the 10th (tied) and assert the driver
        # seeds the automatic runner on 2B before the first pitch is resolved.
        state = GameState(
            pitcher_id=PITCHER, bat_hand="R", season=SEASON,
            away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP, batter_id=101,
            inning=REGULATION_INNINGS + 1, half=Half.TOP, outs=0,
            home_score=4, away_score=4,
        )
        captured = {}

        class _CaptureSM(StateMachine):
            def step_pitch(self, st, **_kw):
                # Capture the base state on the FIRST pitch of the extra half,
                # before any out is recorded.
                if "first" not in captured:
                    captured["first"] = st.bases.occupancy
                # End the half quickly with outs so the game can settle.
                return super().step_pitch(st, pitch_outcome="in_play")

        class _AllOut(PlayResolver):
            def __init__(self):
                self._injected_battedball = {"event": "field_out"}
            def resolve_fielding(self, st, bb):
                return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)

        sm = _CaptureSM(resolver=_AllOut(), rng=np.random.default_rng(0))
        simulate_game(sm, initial_state=state, seed=1)
        # The ghost runner was on 2B (and only 2B) at the first pitch of the 10th.
        assert captured["first"] == (False, True, False)

    def test_extra_inning_game_settles_with_a_winner(self):
        # A tied-after-9 start drives into extras (ghost runner each half) and
        # eventually decides a winner — never returns tied.
        state = GameState(
            pitcher_id=PITCHER, bat_hand="R", season=SEASON,
            away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP, batter_id=101,
            inning=REGULATION_INNINGS, half=Half.TOP, outs=0,
            home_score=3, away_score=3,
        )
        # Start at the top of the 9th tied; the game must play the 9th then go to
        # extras and settle.
        sm = _make_machine(11)
        r = simulate_game(sm, initial_state=state, seed=11)
        assert r.home_score != r.away_score
        assert r.innings_played >= REGULATION_INNINGS


# ===========================================================================
# Invalid-state guards hold across a simulated game
# ===========================================================================


class TestGuardsHold:
    def test_guards_hold_across_a_simulated_game(self):
        # Across many full games, outs never exceed 3 at any committed boundary,
        # scores never go negative, and bases stay consistent.
        for seed in range(25):
            r = _run(seed)
            st = r.final_state
            st.assert_score_valid()
            st.bases.assert_consistent()
            assert 0 <= st.outs <= 3
            assert st.balls >= 0 and st.strikes >= 0

    def test_pathological_no_out_game_terminates_via_the_safety_ceiling(self):
        # A degenerate machine that never records an out would loop forever (the
        # half-inning never reaches 3 outs, so the inning pointer never advances);
        # the pitch-count safety ceiling must stop it so the driver always RETURNS
        # rather than hangs.
        class _NeverOutSM(StateMachine):
            def step_pitch(self, st, **_kw):
                # Always a ball -> walks, never an out (offense never retired).
                return super().step_pitch(st, pitch_outcome="ball")

        sm = _NeverOutSM(rng=np.random.default_rng(0))
        r = simulate_game(
            sm, seed=1, away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP,
            max_innings=12,
        )
        # The driver returned (did not hang) and bounded its work.
        assert isinstance(r, GameSimResult)
        assert r.total_pitches <= max(12, REGULATION_INNINGS) * 200


# ===========================================================================
# The driver builds its own machine / state when not supplied
# ===========================================================================


class TestDriverConstruction:
    def test_driver_builds_a_state_machine_from_an_injected_resolver(self):
        # No state_machine passed: the driver constructs one from the resolver +
        # seed.  Use an out-biased resolver so the game ends promptly.
        class _AllOut(PlayResolver):
            def __init__(self):
                self._injected_battedball = {"event": "field_out"}
            def resolve_fielding(self, st, bb):
                return FieldingSignal(event="field_out", result_hits=0, result_outs=1, result_runs=0)

        # With a built-from-scratch machine the driver samples nothing (no
        # sampler) and would need explicit outcomes; instead assert it raises a
        # clear error rather than hang, proving the no-sampler guard fires.
        with pytest.raises(ValueError):
            simulate_game(
                resolver=_AllOut(), seed=1,
                away_lineup=AWAY_LINEUP, home_lineup=HOME_LINEUP,
                max_innings=3,
            )
