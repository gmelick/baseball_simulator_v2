"""
test_boxscore_ext_sim365.py
===========================
Unit tests for SIM-365 -- the **extended per-player boxscore** (Phase 5,
Sprint 4): the SIM-328 :class:`PlayerStatLine` grows additive batting fields
``b2`` (doubles) / ``b3`` (triples) / ``r`` (runs scored) / ``sb`` (stolen
bases) and pitching fields ``h_allowed`` / ``r_allowed``, accumulated inside the
plate-appearance loop, plus the corresponding upgrade of
``prop_distributions._total_bases`` to an EXACT total-bases formula (the
``TB_IS_LOWER_BOUND`` caveat is retired).

These tests run with NO live DuckDB/FAISS: ``_accumulate_pa`` is driven directly
with synthetic :class:`PlayResult`s (the cleanest way to exercise the boxscore
mapping), the steal accumulation is driven through ``_resolve_steal_outcome`` (the
single steal-commit site, which also catches a steal on a NON-terminal pitch),
and one end-to-end double is driven through the count-machine path with an
fixed-play bundle (SIM-486), so the production in-play path runs.

Coverage (the SIM-365 acceptance criteria):
  * the new PlayerStatLine fields default to 0 (existing constructions unaffected);
  * a double increments b2 (and h); a triple increments b3 (and h);
  * a scoring runner (baserunner_advances end_base == 0) increments that runner's r;
  * a successful steal increments the runner's sb; a steal of home also scores;
  * a CAUGHT stealing is NOT credited a steal, nor counted as a run;
  * the pitcher is charged h_allowed on a hit and r_allowed on any run scored,
    including UNEARNED (error) runs that ER excludes -- so r_allowed >= er;
  * _total_bases is now EXACT and TB_IS_LOWER_BOUND is False.
"""

from __future__ import annotations

import numpy as np

from simulation.game_state import Bases, GameState, PlayResult
from simulation.prop_distributions import TB_IS_LOWER_BOUND, _total_bases
from simulation.sim_loop import (
    STEAL_CAUGHT,
    STEAL_SAFE,
    PlayerStatLine,
    StateMachine,
    StealResolution,
)
from simulation.synthetic_bundle import fixed_play_artifacts, synthetic_sampler

SEASON = 2024
PITCHER = 477132
AWAY_LINEUP = list(range(101, 110))
HOME_LINEUP = list(range(201, 210))


# ===========================================================================
# Test doubles / fixtures
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


def _pa(
    canonical, *, runs_scored=0, outs_recorded=0, is_error=False, advances=None, event=None
) -> PlayResult:
    """A synthetic terminal PlayResult shaped the way ``_accumulate_pa`` reads it."""
    return PlayResult(
        pitch_outcome="in_play",
        is_contact=True,
        pa_terminal=True,
        event=event or canonical,
        canonical_event=canonical,
        runs_scored=int(runs_scored),
        outs_recorded=int(outs_recorded),
        is_error=is_error,
        baserunner_advances=dict(advances or {}),
    )


# ===========================================================================
# PlayerStatLine: the new fields default to 0 (additive / back-compat)
# ===========================================================================


class TestPlayerStatLineDefaults:
    def test_new_fields_default_to_zero(self):
        ln = PlayerStatLine(player_id=PITCHER)
        assert ln.b2 == 0
        assert ln.b3 == 0
        assert ln.r == 0
        assert ln.sb == 0
        assert ln.h_allowed == 0
        assert ln.r_allowed == 0

    def test_existing_positional_keyword_constructions_still_work(self):
        # A SIM-328-style construction (no new fields) is unaffected.
        ln = PlayerStatLine(player_id=1, ab=4, h=2, hr=1, rbi=3, outs_recorded=21, k=8, bb=2, er=3)
        assert (ln.ab, ln.h, ln.hr, ln.rbi) == (4, 2, 1, 3)
        assert (ln.outs_recorded, ln.k, ln.bb, ln.er) == (21, 8, 2, 3)
        # And the new fields are still 0.
        assert (ln.b2, ln.b3, ln.r, ln.sb, ln.h_allowed, ln.r_allowed) == (0, 0, 0, 0, 0, 0)


# ===========================================================================
# Doubles / triples: a subset of H, with the new b2 / b3 fields
# ===========================================================================


class TestDoublesAndTriples:
    def test_double_increments_b2_and_h(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        sm._accumulate_pa(state, _pa("double", advances={batter: 2}))

        bat = sm.boxscore.line(batter)
        assert bat.h == 1
        assert bat.b2 == 1
        assert bat.b3 == 0
        assert bat.hr == 0
        assert bat.ab == 1
        # The pitcher is charged the hit.
        assert sm.boxscore.line(PITCHER).h_allowed == 1

    def test_triple_increments_b3_and_h(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        sm._accumulate_pa(state, _pa("triple", advances={batter: 3}))

        bat = sm.boxscore.line(batter)
        assert bat.h == 1
        assert bat.b3 == 1
        assert bat.b2 == 0
        assert bat.hr == 0

    def test_single_credits_neither_b2_nor_b3(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        sm._accumulate_pa(state, _pa("single", advances={batter: 1}))

        bat = sm.boxscore.line(batter)
        assert bat.h == 1
        assert bat.b2 == 0
        assert bat.b3 == 0

    def test_in_play_double_end_to_end_records_b2(self):
        # Drive a real PA through the count machine with an injected double.
        sm = _machine("double")
        state = _fresh_state()
        batter = state.batter_id
        sm.step_pitch(state, pitch_outcome="in_play")

        bat = sm.boxscore.line(batter)
        assert bat.h == 1
        assert bat.b2 == 1
        assert sm.boxscore.line(PITCHER).h_allowed == 1


# ===========================================================================
# Runs scored: the scoring runner(s) get R; the pitcher gets r_allowed
# ===========================================================================


class TestRunsScored:
    def test_scoring_runner_is_credited_a_run(self):
        # A single that scores the runner from 3B: end_base == 0 marks the scorer.
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(third=505)
        batter = state.batter_id
        sm._accumulate_pa(
            state,
            _pa(
                "single",
                runs_scored=1,
                advances={505: 0, batter: 1},
            ),
        )

        # The runner from 3B scored.
        assert sm.boxscore.line(505).r == 1
        # The batter did not score (he is on 1B, end_base == 1).
        assert sm.boxscore.line(batter).r == 0
        # The batter is credited the RBI; the pitcher the run allowed.
        assert sm.boxscore.line(batter).rbi == 1
        assert sm.boxscore.line(PITCHER).r_allowed == 1

    def test_home_run_scores_the_batter(self):
        # A solo HR: the batter himself scores (end_base == 0 for the batter).
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        sm._accumulate_pa(
            state,
            _pa(
                "home_run",
                runs_scored=1,
                advances={batter: 0},
            ),
        )

        assert sm.boxscore.line(batter).r == 1
        assert sm.boxscore.line(batter).hr == 1
        assert sm.boxscore.line(PITCHER).r_allowed == 1

    def test_multiple_runners_each_score(self):
        sm = _machine()
        state = _fresh_state()
        batter = state.batter_id
        # A double clearing two runners (both end_base == 0), batter to 2B.
        sm._accumulate_pa(
            state,
            _pa(
                "double",
                runs_scored=2,
                advances={601: 0, 602: 0, batter: 2},
            ),
        )

        assert sm.boxscore.line(601).r == 1
        assert sm.boxscore.line(602).r == 1
        assert sm.boxscore.line(batter).b2 == 1
        assert sm.boxscore.line(PITCHER).r_allowed == 2


# ===========================================================================
# Pitcher: h_allowed on hits; r_allowed includes UNEARNED runs (unlike ER)
# ===========================================================================


class TestPitcherHitsAndRunsAllowed:
    def test_hit_allowed_charged_to_pitcher(self):
        sm = _machine()
        state = _fresh_state()
        sm._accumulate_pa(state, _pa("single", advances={state.batter_id: 1}))
        assert sm.boxscore.line(PITCHER).h_allowed == 1

    def test_strikeout_and_walk_are_not_hits_allowed(self):
        sm = _machine()
        state = _fresh_state()
        sm._accumulate_pa(state, _pa("strikeout", outs_recorded=1))
        sm._accumulate_pa(state, _pa("walk"))
        assert sm.boxscore.line(PITCHER).h_allowed == 0

    def test_runs_allowed_include_unearned_runs_that_er_excludes(self):
        # An error-driven run: NOT an earned run (ER), but IS a run allowed (R).
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(third=701)
        sm._accumulate_pa(
            state,
            _pa(
                "field_error",
                runs_scored=1,
                is_error=True,
                advances={701: 0},
            ),
        )

        pit = sm.boxscore.line(PITCHER)
        assert pit.er == 0  # unearned -> ER excludes it (SIM-328)
        assert pit.r_allowed == 1  # R counts it (SIM-365)
        assert pit.r_allowed >= pit.er

    def test_clean_run_counts_for_both_er_and_r_allowed(self):
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(third=702)
        sm._accumulate_pa(
            state,
            _pa(
                "single",
                runs_scored=1,
                advances={702: 0, state.batter_id: 1},
            ),
        )
        pit = sm.boxscore.line(PITCHER)
        assert pit.er == 1
        assert pit.r_allowed == 1


# ===========================================================================
# SIM-414 — Inning-reconstruction unearned runs (Rule 9.16(b))
# A run that scores AFTER the inning "should have ended" (an earlier
# reach-on-error prevented an out) is UNEARNED even when its play is clean.
# ===========================================================================


class TestSim414InningReconstruction:
    def test_reach_on_error_increments_half_inning_error_outs_lost(self):
        """The canonical reach-on-error (is_error + outs_recorded==0) adds 1
        to the per-half-inning missed-out counter."""
        sm = _machine()
        state = _fresh_state()
        assert sm._half_inning_error_outs_lost == 0
        sm._accumulate_pa(
            state,
            _pa("field_error", outs_recorded=0, is_error=True),
        )
        assert sm._half_inning_error_outs_lost == 1

    def test_error_play_with_recorded_out_does_not_increment(self):
        """An error that still records an out (e.g. throwing error after a
        force out) does NOT add to the missed-out counter."""
        sm = _machine()
        state = _fresh_state()
        sm._accumulate_pa(
            state,
            _pa("field_out", outs_recorded=1, is_error=True),
        )
        assert sm._half_inning_error_outs_lost == 0

    def test_clean_run_after_inning_should_have_ended_is_unearned(self):
        """2 actual outs + 1 error-out-lost = effective 3 outs.  A clean single
        that drives in a run scored AFTER this point is UNEARNED (charged to R
        but not to ER)."""
        sm = _machine()
        state = _fresh_state()
        state.outs = 2  # 2 outs already recorded
        sm._half_inning_error_outs_lost = 1  # earlier reach-on-error
        state.bases = Bases(third=701)
        sm._accumulate_pa(
            state,
            _pa(
                "single",
                runs_scored=1,
                outs_recorded=0,
                advances={701: 0, state.batter_id: 1},
            ),
        )
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1
        assert pit.er == 0  # unearned: inning should have ended
        # RBI is still credited (Rule 9.04: clean RBI in an extended inning counts).
        bat = sm.boxscore.line(state.batter_id)
        assert bat.rbi == 1

    def test_run_before_inning_would_have_ended_is_earned(self):
        """1 actual out + 1 error-out-lost = effective 2 outs.  A clean run on
        this play scores BEFORE the inning would have ended -> EARNED."""
        sm = _machine()
        state = _fresh_state()
        state.outs = 1
        sm._half_inning_error_outs_lost = 1
        state.bases = Bases(third=701)
        sm._accumulate_pa(
            state,
            _pa(
                "single",
                runs_scored=1,
                outs_recorded=0,
                advances={701: 0, state.batter_id: 1},
            ),
        )
        pit = sm.boxscore.line(PITCHER)
        assert pit.r_allowed == 1
        assert pit.er == 1  # earned: inning had not yet "should have ended"

    def test_two_reach_on_errors_then_clean_run_is_unearned(self):
        """Two reach-on-errors in the same half-inning + 1 actual out =
        effective 3 outs.  A subsequent clean run is unearned."""
        sm = _machine()
        state = _fresh_state()
        # First reach-on-error.
        sm._accumulate_pa(state, _pa("field_error", outs_recorded=0, is_error=True))
        # Second reach-on-error.
        sm._accumulate_pa(state, _pa("field_error", outs_recorded=0, is_error=True))
        # Actual out.
        state.outs = 1
        sm._accumulate_pa(state, _pa("strikeout", outs_recorded=1))
        # Clean RBI single.
        state.outs = 1  # still 1 actual out
        state.bases = Bases(third=701)
        sm._accumulate_pa(
            state,
            _pa(
                "single",
                runs_scored=1,
                outs_recorded=0,
                advances={701: 0, state.batter_id: 1},
            ),
        )
        pit = sm.boxscore.line(PITCHER)
        # effective_outs_before_clean_single = 1 + 2 = 3 -> unearned.
        assert pit.er == 0
        assert pit.r_allowed == 1

    def test_half_inning_roll_resets_error_counter(self):
        """Errors don't carry across half-innings.  ``advance_half_inning``
        resets the counter so a clean run in the next half is earned."""
        sm = _machine()
        state = _fresh_state()
        sm._half_inning_error_outs_lost = 2
        # Half-inning advances when state.outs has reached 3.  Force it via the
        # state setter (we just need the precondition the method asserts).
        state.outs = 3
        sm.advance_half_inning(state)
        assert sm._half_inning_error_outs_lost == 0


# ===========================================================================
# Stolen bases (accumulated in _resolve_steal_outcome -- catches non-terminal
# pitches too) + a caught stealing is neither an SB nor a run
# ===========================================================================


class TestStolenBases:
    def test_successful_steal_credits_the_runner_an_sb(self):
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(first=801)
        sm._pending_steal = StealResolution(
            attempted=True,
            runner_id=801,
            from_base=1,
            to_base=2,
            safe=True,
        )
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)

        assert sm.boxscore.line(801).sb == 1
        assert sm.boxscore.line(801).r == 0  # only advanced to 2B
        assert result.steal_outcome == STEAL_SAFE

    def test_steal_of_home_credits_sb_run_and_run_allowed(self):
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(third=802)
        sm._pending_steal = StealResolution(
            attempted=True,
            runner_id=802,
            from_base=3,
            to_base=4,
            safe=True,
        )
        # Non-terminal pitch: the run never reaches _accumulate_pa, so
        # _resolve_steal_outcome must charge the pitcher itself.
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)

        assert sm.boxscore.line(802).sb == 1
        assert sm.boxscore.line(802).r == 1
        assert sm.boxscore.line(PITCHER).r_allowed == 1

    def test_caught_stealing_is_not_an_sb_nor_a_run(self):
        sm = _machine()
        state = _fresh_state()
        state.bases = Bases(first=803)
        sm._pending_steal = StealResolution(
            attempted=True,
            runner_id=803,
            from_base=1,
            to_base=2,
            safe=False,
        )
        result = PlayResult(pitch_outcome="ball", pa_terminal=False)
        sm._resolve_steal_outcome(state, result)

        # A caught stealing touches no boxscore line at all (no SB, no R): the
        # box is either still None or carries a 0/0 line for the runner.
        if sm.boxscore is not None:
            ln = sm.boxscore.line(803)
            assert ln.sb == 0
            assert ln.r == 0
        assert result.steal_outcome == STEAL_CAUGHT
        # The caught runner's baserunner_advances == 0 is an OUT, not a score:
        # it carries steal_attempted, so _accumulate_pa skips run attribution.
        assert result.baserunner_advances.get(803) == 0


class TestStealRunAttributionDoesNotDoubleCount:
    def test_accumulate_pa_skips_runs_on_a_steal_play(self):
        # If a terminal PlayResult carries a steal (steal_attempted), the run
        # attribution in _accumulate_pa is skipped (the steal path owns it) so a
        # steal-of-home is not counted twice.  Here the 0-advance must NOT become
        # an R via _accumulate_pa.
        sm = _machine()
        state = _fresh_state()
        result = _pa("strikeout", outs_recorded=1, advances={902: 0})
        result.steal_attempted = True
        sm._accumulate_pa(state, result)
        assert sm.boxscore.line(902).r == 0


# ===========================================================================
# prop_distributions._total_bases is now EXACT (TB_IS_LOWER_BOUND is False)
# ===========================================================================


class TestTotalBasesExact:
    def test_tb_is_no_longer_a_lower_bound(self):
        assert TB_IS_LOWER_BOUND is False

    def test_exact_total_bases_with_a_double_and_a_triple(self):
        # h=3 incl. 1 double + 1 triple (and 1 single): TB = 1 + 2 + 3 = 6
        # via the formula h + b2 + 2*b3 + 3*hr = 3 + 1 + 2*1 + 0 = 6.
        ln = PlayerStatLine(player_id=1, h=3, b2=1, b3=1, hr=0)
        assert _total_bases(ln) == 6

    def test_exact_total_bases_with_home_runs(self):
        # 4 hits incl. 2 HR (2 singles): TB = 2*1 + 2*4 = 10
        # via h + b2 + 2*b3 + 3*hr = 4 + 0 + 0 + 3*2 = 10.
        ln = PlayerStatLine(player_id=1, h=4, hr=2)
        assert _total_bases(ln) == 10

    def test_all_singles_equals_hits(self):
        ln = PlayerStatLine(player_id=1, h=2)
        assert _total_bases(ln) == 2

    def test_grand_slam_line(self):
        # 1 hit, 1 HR -> 4 total bases.
        ln = PlayerStatLine(player_id=1, h=1, hr=1)
        assert _total_bases(ln) == 4
