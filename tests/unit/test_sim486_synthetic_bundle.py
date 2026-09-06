"""
tests/unit/test_sim486_synthetic_bundle.py
==========================================
SIM-486 -- the in-memory bundle every no-DB path draws from.

The per-tile fallback and the injected ``PlayResolver`` are deleted. A test
that needs a play now hands the production sampler a synthetic bundle and
drives the production in-play path (the SIM-511 transition draw). These
tests pin the bundle's contract:

  * every count bucket and every base-out cell has rows (the sampler's hard
    filters never hit an empty cell);
  * the canonical transitions state real baseball (a single pushes one base,
    a double play retires the runner from first, a double play is impossible
    without him);
  * a fixed-play bundle is deterministic through the loop;
  * the got-away pitch pool fires the dropped third strike;
  * a league game completes with a realistic run environment.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState
from simulation.sim_loop import EVENT_STRIKEOUT, StateMachine, simulate_game
from simulation.synthetic_bundle import (
    LEAGUE_INPLAY_MODEL,
    LEAGUE_PITCH_MODEL,
    canonical_transition,
    fixed_play_artifacts,
    inplay_rows,
    league_artifacts,
    synthetic_artifacts,
    synthetic_sampler,
)

AWAY = list(range(101, 110))
HOME = list(range(201, 210))


def _machine(art, seed: int = 0, **kw) -> StateMachine:
    return StateMachine(synthetic_sampler(art, seed), rng=np.random.default_rng(seed), **kw)


def _state(**kw) -> GameState:
    base = {"pitcher_id": 900, "bat_hand": "R", "season": 2024, "batter_id": 900}
    base.update(kw)
    return GameState(**base)


# ===========================================================================
# The bundle covers every hard-filter cell
# ===========================================================================


class TestCoverage:
    def test_every_count_bucket_draws_every_outcome(self):
        fp = synthetic_sampler(league_artifacts(), 0)
        fp.new_half_inning("R", "900:2024")
        fp.new_plate_appearance("101:2024", np.zeros(4, dtype=np.float32))
        for balls in range(4):
            for strikes in range(3):
                seen = {fp.draw(balls, strikes) for _ in range(200)}
                assert seen == set(LEAGUE_PITCH_MODEL), (balls, strikes, seen)

    def test_every_base_out_cell_has_rows(self):
        rows = inplay_rows()
        cells = {(r.outs, r.runners_state) for r in rows}
        assert cells == {(o, rs) for o in range(3) for rs in range(8)}

    def test_cell_weights_sum_to_the_model(self):
        rows = inplay_rows()
        total = sum(LEAGUE_INPLAY_MODEL.values())
        for outs in range(3):
            for rs in range(8):
                w = sum(r.weight for r in rows if (r.outs, r.runners_state) == (outs, rs))
                assert w == pytest.approx(total), (outs, rs)

    def test_the_sampler_reports_a_transition_bundle(self):
        fp = synthetic_sampler(league_artifacts(), 0)
        assert fp.has_transition("R") and fp.has_transition("L")
        assert fp.has_battedball() and fp.has_advancement()
        assert not fp.has_steal_pool()


# ===========================================================================
# The canonical transitions
# ===========================================================================


class TestCanonicalTransition:
    def test_a_single_pushes_one_base_and_scores_third(self):
        row = canonical_transition("single", 0, 0b111)
        assert (row.batter_dest, row.r1_dest, row.r2_dest, row.r3_dest) == (1, 2, 3, 4)
        assert (row.result_hits, row.result_outs) == (1, 0)

    def test_a_double_scores_second_and_third(self):
        row = canonical_transition("double", 1, 0b011)
        assert (row.batter_dest, row.r1_dest, row.r2_dest, row.r3_dest) == (2, 3, 4, -1)

    def test_a_home_run_clears_the_bases(self):
        row = canonical_transition("home_run", 2, 0b101)
        assert (row.batter_dest, row.r1_dest, row.r2_dest, row.r3_dest) == (4, 4, -1, 4)

    def test_an_out_holds_every_runner(self):
        row = canonical_transition("field_out", 0, 0b110)
        assert (row.batter_dest, row.r1_dest, row.r2_dest, row.r3_dest) == (0, -1, 2, 3)
        assert row.result_outs == 1 and not row.is_air

    def test_a_double_play_retires_the_runner_from_first(self):
        row = canonical_transition("grounded_into_double_play", 0, 0b001)
        assert (row.batter_dest, row.r1_dest) == (0, 0)
        assert row.result_outs == 2

    def test_a_double_play_scores_third_only_when_the_inning_survives(self):
        assert canonical_transition("grounded_into_double_play", 0, 0b101).r3_dest == 4
        assert canonical_transition("grounded_into_double_play", 1, 0b101).r3_dest == 3

    def test_a_double_play_is_impossible_without_a_runner_on_first(self):
        assert canonical_transition("grounded_into_double_play", 0, 0b110) is None
        assert canonical_transition("grounded_into_double_play", 2, 0b001) is None

    def test_an_error_reaches_the_batter_and_pushes_the_runners(self):
        row = canonical_transition("field_error", 0, 0b011)
        assert (row.batter_dest, row.r1_dest, row.r2_dest) == (1, 2, 3)
        assert (row.result_hits, row.result_outs) == (0, 0)


# ===========================================================================
# A fixed play is deterministic through the production in-play path
# ===========================================================================


class TestFixedPlay:
    def test_a_single_places_the_batter_and_pushes_the_runner(self):
        sm = _machine(fixed_play_artifacts("single"))
        state = _state()
        state.bases = Bases(first=101)
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.event == "single"
        assert (state.bases.first, state.bases.second) == (900, 101)
        assert r.run_resolution_method == "re24_delta"

    def test_a_double_play_ends_with_the_bases_empty(self):
        sm = _machine(fixed_play_artifacts("grounded_into_double_play"))
        state = _state()
        state.bases = Bases(first=101)
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert state.outs == 2 and r.outs_recorded == 2
        assert state.bases.runner_ids() == ()

    def test_an_override_replaces_the_canonical_destination(self):
        # A ground out holds the runner on third in the canonical row; an
        # override scores him (a productive out is row truth, not one of the
        # five discretionary movements the loop normalizes away).
        sm = _machine(fixed_play_artifacts("field_out", r3_dest=4))
        state = _state()
        state.bases = Bases(third=303)
        r = sm.step_pitch(state, pitch_outcome="in_play")
        assert r.runs_scored == 1 and state.away_score == 1
        assert state.bases.third is None and state.outs == 1

    def test_the_same_seed_replays_the_same_play(self):
        outs = []
        for _ in range(2):
            sm = _machine(fixed_play_artifacts("field_out"), seed=3)
            state = _state()
            sm.step_pitch(state, pitch_outcome="in_play")
            outs.append(state.outs)
        assert outs == [1, 1]


# ===========================================================================
# The got-away pitch pool fires the dropped third strike
# ===========================================================================


class TestGotAwayPool:
    def test_a_got_away_strike_three_reaches_first_when_it_is_open(self):
        art = fixed_play_artifacts("field_out", pitch_model={"swinging_strike": 1.0}, got_away=True)
        sm = _machine(art)
        sm._got_away = True
        state = _state()
        r = None
        for _ in range(3):
            r = sm.step_pitch(state)
        assert r is not None and r.event == EVENT_STRIKEOUT
        assert state.outs == 0 and state.bases.first == 900

    def test_with_the_flag_off_the_strikeout_is_an_out(self):
        art = fixed_play_artifacts("field_out", pitch_model={"swinging_strike": 1.0}, got_away=True)
        sm = _machine(art)
        state = _state()
        for _ in range(3):
            sm.step_pitch(state)
        assert state.outs == 1 and state.bases.first is None


# ===========================================================================
# A league game is realistic baseball
# ===========================================================================


class TestLeagueGame:
    def test_forty_games_land_in_the_realistic_run_band(self):
        runs = 0
        for seed in range(40):
            sm = _machine(league_artifacts(), seed=seed)
            r = simulate_game(sm, seed=seed, away_lineup=AWAY, home_lineup=HOME, pitcher_id=900)
            assert r.home_score != r.away_score
            runs += r.home_score + r.away_score
        assert 3.0 <= runs / 80 <= 6.5

    def test_a_platoon_skew_is_a_per_hand_pitch_model(self):
        skewed = dict(LEAGUE_PITCH_MODEL)
        skewed["in_play"] += 0.10
        skewed["swinging_strike"] -= 0.05
        skewed["called_strike"] -= 0.05
        art = synthetic_artifacts(pitch_models={"L": skewed, "R": LEAGUE_PITCH_MODEL})
        fp = synthetic_sampler(art, 0)
        share = {}
        for hand in ("L", "R"):
            fp.new_half_inning(hand, "900:2024")
            fp.new_plate_appearance("101:2024", np.zeros(4, dtype=np.float32))
            draws = [fp.draw(0, 0) for _ in range(1500)]
            share[hand] = draws.count("in_play") / len(draws)
        assert share["L"] > share["R"] + 0.04
