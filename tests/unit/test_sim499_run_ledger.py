"""
test_sim499_run_ledger.py
=========================
SIM-499 — the run-value ledger is TOLD its two base-out states.

WHAT WAS WRONG
--------------
``StateMachine._commit_run_delta`` read ``state.outs`` and ``state.runners_state``
at commit time and called that the state BEFORE the play.  Four resolvers mutate
the bases first, so "before" was the after-state.  It then DERIVED the
after-state with a conservation formula::

    new_on_base = old_on_base + batter_reached - runs_scored

which has no term for a runner RETIRED on the play, and which placed the
survivors on the lead-most bags rather than where they stood.

Both defects are deleted.  The caller measures both states and hands them over.

WHAT EACH TEST HERE IS FOR
--------------------------
Every value in this file was measured against the pre-fix code in this
repository before the fix landed, not taken from a ticket.  The tests below
assert the TRUE value and record the pre-fix value in the docstring, so each one
goes RED against the old ledger through the same public path:

===================================  ==========  ==========  ================
play                                 pre-fix     true        which defect
===================================  ==========  ==========  ================
walk, runner on 1B                      +0.50      +0.60      read-after-mutate
walk, bases loaded (1 forced run)       +0.69      +1.00      read-after-mutate
caught stealing, runner on 1B           -0.24      -0.62      both
ground-ball double play, runner 1B      -0.52      -0.66      derivation
reach on error (batter reaches 1B)      +1.10      +0.38      read-after-mutate
sac fly, runner on 3B                   +0.76      -0.13      read-after-mutate
steal of home, runner on 3B             +1.00      +0.11      read-after-mutate
===================================  ==========  ==========  ================

The RE24 table these numbers come from is ``run_resolution.RE24_MATRIX``.  The
tests read it rather than hard-coding 0.89, so a future re-fit of the matrix
moves the expectations with it and does not silently break the file.
"""

from __future__ import annotations

import numpy as np
import pytest

from simulation.game_state import Bases, GameState, Half, PlayResult
from simulation.run_resolution import RE24_MATRIX, RunResolution, re24_value, resolve_runs
from simulation.sim_loop import EVENT_WALK, FieldingSignal, PlayResolver, StateMachine

SEASON = 2024
PITCHER = 477132

# Base-out bitmask shorthand: bit0 = 1B, bit1 = 2B, bit2 = 3B.
EMPTY, ON_1B, ON_2B, ON_1B_2B, ON_3B = 0, 1, 2, 3, 4
LOADED = 7


def _re(outs: int, runners_state: int) -> float:
    """RE for a base-out state, read from the shipped matrix."""
    return re24_value(outs, runners_state)


def _fresh_state(**kw) -> GameState:
    base = {"pitcher_id": PITCHER, "bat_hand": "R", "season": SEASON, "batter_id": 900}
    base.update(kw)
    return GameState(**base)


class _InjectedResolver(PlayResolver):
    """Returns one fixed FieldingSignal, with no DB and no FAISS."""

    def __init__(self, signal: FieldingSignal, *, dropped_k: bool = False):
        self._signal = signal
        self._dropped_k = dropped_k
        self._injected_battedball = {"event": signal.event}

    def resolve_fielding(self, state, battedball_sample) -> FieldingSignal:
        return self._signal

    def dropped_third_strike(self, state, result) -> bool:
        return self._dropped_k


def _sig(event: str, hits: int, outs: int, runs: int, **kw) -> FieldingSignal:
    return FieldingSignal(event=event, result_hits=hits, result_outs=outs, result_runs=runs, **kw)


def _walk(state: GameState, sm: StateMachine) -> PlayResult:
    """Throw four balls and return the terminal PlayResult."""
    result = None
    for _ in range(4):
        result = sm.step_pitch(state, pitch_outcome="ball")
    assert result is not None and result.event == EVENT_WALK
    return result


# ===========================================================================
# (a) The "before" state must be the state before the play
# ===========================================================================


class TestTheBeforeStateIsMeasuredBeforeThePlay:
    """DEFECT (a): four resolvers mutate the bases, then commit.  The ledger read
    the mutated bases and called them the pre-play state."""

    def test_a_walk_records_the_pre_walk_base_state(self):
        """Runner on 1B, 0 outs.

        PRE-FIX: ``re_start`` was 1.49 — the RE of 1B+2B, the state AFTER the
        force — and the play scored +0.50.  TRUE: the walk moves the state from
        a runner on 1B (0.89) to 1B+2B (1.49), so it is worth +0.60.
        """
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101)

        result = _walk(state, sm)

        assert result.re_start == pytest.approx(_re(0, ON_1B))
        assert result.re_end == pytest.approx(_re(0, ON_1B_2B))
        assert result.runs == pytest.approx(_re(0, ON_1B_2B) - _re(0, ON_1B))
        assert result.runs == pytest.approx(0.60), (
            "the walk must be valued from the state BEFORE the force, not after it"
        )

    def test_a_bases_loaded_walk_scores_the_forced_run_from_the_right_state(self):
        """Bases loaded, 0 outs.  PRE-FIX +0.69.  TRUE +1.00.

        The base state is unchanged (loaded before, loaded after) and one run is
        forced home, so the play is worth exactly the run.  The old ledger
        derived an after-state of 2B+3B and lost 0.31 of it.
        """
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101, second=102, third=103)

        result = _walk(state, sm)

        assert result.runs_scored == 1
        assert result.re_start == pytest.approx(_re(0, LOADED))
        assert result.re_end == pytest.approx(_re(0, LOADED))
        assert result.runs == pytest.approx(1.0)

    def test_a_sac_fly_is_valued_from_before_the_runner_left_third(self):
        """Runner on 3B, 0 outs.  PRE-FIX +0.76.  TRUE -0.13.

        This is the PLACEMENT TRAP, retargeted 2026-08-19: the SIM-349 bias
        that first caught it is retired, and the SIM-512 tag draw now clears
        3B before the commit.  A snapshot taken below the transition
        application reads empty bases and values a productive out as if it
        had created a run from nothing.  The sac fly is a good out, not a
        good play: it trades a runner on third and no outs for nobody on and
        one out.
        """
        from types import SimpleNamespace

        from simulation.game_state import PlayResult
        from simulation.sim_loop import FieldingSignal

        class _TagFP:
            a = SimpleNamespace(adv_pools={"4_3_4": object()})

            def has_advancement(self) -> bool:
                return True

            def advancement_draw(self, *args, **kwargs):
                return (True, True, False)  # the tag from 3B scores

            def last_battedball_fielder(self):
                return None

        sm = StateMachine(rng=np.random.default_rng(0))
        sm.full_pool_sampler = _TagFP()
        state = _fresh_state()
        state.bases = Bases(third=103)
        result = PlayResult(pitch_outcome="in_play", is_contact=True)
        sig = FieldingSignal(
            event="field_out",
            result_hits=0,
            result_outs=1,
            result_runs=0,
            launch_angle=30.0,
            transition={
                "r1": -1,
                "r2": -1,
                "r3": 3,  # the row held him; the tag draw sends him
                "batter": 0,
                "adv1": False,
                "adv2": False,
                "adv3": False,
                "is_air": True,
                "ev": 95.0,
                "spray": 0.0,
                "dist": 320.0,
            },
        )
        sm._resolve_in_play_transition(
            state, result, sig, int(state.outs), sm._snapshot_bases(state)
        )

        assert result.event == "sacrifice_fly"
        assert result.runs_scored == 1
        assert result.re_start == pytest.approx(_re(0, ON_3B))
        assert result.runs == pytest.approx(_re(1, EMPTY) - _re(0, ON_3B) + 1.0)
        assert result.runs == pytest.approx(-0.13)
        assert result.runs < 0.0, "a sac fly costs more run expectancy than the run it buys"

    def test_a_steal_of_home_is_valued_from_before_the_runner_left(self):
        """Runner on 3B, 0 outs.  PRE-FIX +1.00.  TRUE +0.11.

        ``_move_runner`` empties 3B before the commit.  The old ledger read empty
        bases as the pre-state and credited the whole run with no cost.
        """
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(third=103)
        sm.stage_steal(runner_id=103, from_base=3, to_base=4, safe=True)

        result = sm.step_pitch(state, pitch_outcome="ball")

        assert result.runs_scored == 1
        assert result.re_start == pytest.approx(_re(0, ON_3B))
        assert result.re_end == pytest.approx(_re(0, EMPTY))
        assert result.runs == pytest.approx(0.11)


# ===========================================================================
# (b) A play that RETIRES a baserunner
# ===========================================================================


class TestAPlayThatRetiresARunner:
    """DEFECT (b): the conservation formula has no term for a runner the fielders
    remove, so it kept his body on the field and put it on the lead-most bag."""

    def test_a_caught_stealing_charges_the_runner_it_removed(self):
        """Runner on 1B, 0 outs.  PRE-FIX -0.24.  TRUE -0.62.

        A caught stealing costs the runner AND the out.  The old ledger charged
        the out only: it read the already-cleared bases as the pre-state (0.51)
        and then derived an after-state that also had nobody on, so the runner
        never appeared on either side of the subtraction.
        """
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101)
        sm.stage_steal(runner_id=101, from_base=1, to_base=2, safe=False)

        result = sm.step_pitch(state, pitch_outcome="ball")

        assert state.outs == 1
        assert state.bases.runner_ids() == ()
        assert result.re_start == pytest.approx(_re(0, ON_1B))
        assert result.re_end == pytest.approx(_re(1, EMPTY))
        assert result.runs == pytest.approx(-0.62)

    def test_a_double_play_is_valued_from_the_bases_that_actually_remain(self):
        """Runner on 1B, 0 outs, ground-ball double play.  PRE-FIX -0.52.

        This is the derivation defect on its own — the pre-state was read
        correctly here, because an in-play out does not move the bases.  The old
        formula kept one body on the field (1 on base + 0 reached - 0 scored)
        and placed it on THIRD, so it valued the after-state at RE(2 outs, 3B) =
        0.37 instead of the real RE(2 outs, 1B) = 0.23.

        The value asserted is the value of the play THIS simulator plays.  It
        leaves the doubled-off runner standing on first (BACKLOG.md:87, SIM-494
        — double plays are under-counted and the trail runner is not removed).
        The ledger's job is to report what happened, so it reports -0.66.  The
        second assertion pins the number the same play would be worth once
        SIM-494 removes that runner, so the day it lands this test says so.
        """
        sm = StateMachine(
            resolver=_InjectedResolver(_sig("grounded_into_double_play", 0, 2, 0)),
            rng=np.random.default_rng(0),
        )
        state = _fresh_state()
        state.bases = Bases(first=101)

        result = sm.step_pitch(state, pitch_outcome="in_play")

        assert state.outs == 2
        assert state.bases.first == 101, "SIM-494: this simulator does not remove him yet"
        assert result.re_start == pytest.approx(_re(0, ON_1B))
        assert result.re_end == pytest.approx(_re(2, ON_1B))
        assert result.runs == pytest.approx(-0.66)
        # The old formula's after-state, for the record: it invented a runner on
        # third.  That is a strictly different, and better, number than the truth.
        assert _re(2, ON_3B) > _re(2, ON_1B)
        # What the same play is worth once the trail runner is actually removed.
        assert _re(2, EMPTY) - _re(0, ON_1B) == pytest.approx(-0.78)

    def test_the_deleted_conservation_formula_cannot_express_a_retirement(self):
        """The formula, reconstructed here, against the transition it must model.

        A ground-ball double play with a runner on first ends with the bases
        empty and two outs.  The formula is handed the same play and returns a
        runner still standing on third.  No argument to it can say "the fielders
        took him off the base", which is why SIM-499 deletes it as a derivation
        and SIM-500 keeps the identity only as a check, where the caller states
        the retired count.
        """

        def deleted_conservation_after_state(
            outs: int, runners_state: int, *, result_hits: int, result_outs: int, result_runs: int
        ) -> tuple[int, int]:
            """Verbatim reconstruction of the deleted ``advance_state``."""
            new_outs = outs + result_outs
            if new_outs >= 3:
                return 3, 0
            reached = 1 if 1 <= result_hits <= 3 else 0
            n = max(0, min(3, bin(runners_state & 0b111).count("1") + reached - result_runs))
            rs = 0
            if n >= 1:
                rs |= 0b100
            if n >= 2:
                rs |= 0b010
            if n >= 3:
                rs |= 0b001
            return new_outs, rs

        derived = deleted_conservation_after_state(
            0, ON_1B, result_hits=0, result_outs=2, result_runs=0
        )
        assert derived == (2, ON_3B), "the formula keeps the body and promotes it to third"

        real_after = (2, EMPTY)
        assert derived != real_after

        # The formula's run value against the truth, on the same play.
        derived_value = _re(*derived) - _re(0, ON_1B)
        true_value = resolve_runs(
            event="grounded_into_double_play",
            pre_outs=0,
            pre_runners_state=ON_1B,
            post_outs=2,
            post_runners_state=EMPTY,
            result_runs=0,
        )
        assert derived_value == pytest.approx(-0.52)
        assert true_value.runs == pytest.approx(-0.78)
        assert derived_value > true_value.runs, "the formula under-charges the double play"


# ===========================================================================
# (c) Reach on error
# ===========================================================================


class TestReachOnError:
    """DEFECT (c): the ledger valued a reach on error wrongly.

    ⚠ A CORRECTION TO THE FILED DESCRIPTION, measured in this repository.
    The ticket says every reach on error records exactly 0.00 against a true
    +0.38.  Both halves are half right, and the difference matters:

      * The 0.00 is real and exact, and so is the 0.00 it should record —
        because on that path the batter never reaches base at all.  The play is
        resolved with ``result_hits=0`` and nobody is placed on first, so no run
        expectancy changed.  Making the batter reach is **SIM-496**, a separate
        open ticket, in a different method.  ``BACKLOG.md:89`` states the split
        exactly: "SIM-453 [now SIM-499] fixes what the ledger records, SIM-496
        fixes what the play is."
      * The +0.38 is the correct value the moment a batter DOES reach first on
        an error.  One path in the loop does that today — the dropped third
        strike — and it recorded **+1.10**, not 0.00.
    """

    def test_a_reach_on_error_that_puts_the_batter_on_first_is_worth_plus_038(self):
        """Bases empty, 0 outs, dropped third strike.  PRE-FIX +1.10.  TRUE +0.38.

        The batter reaches first on the catcher's error.  Empty bases (0.51) to
        a runner on first (0.89) is +0.38.  The old ledger read the post-reach
        bases as the pre-state and then derived a SECOND runner on top, valuing
        an empty-bases single at more than a bases-clearing double.
        """
        sm = StateMachine(
            resolver=_InjectedResolver(_sig("field_out", 0, 1, 0), dropped_k=True),
            rng=np.random.default_rng(0),
        )
        state = _fresh_state()

        result = None
        for _ in range(3):
            result = sm.step_pitch(state, pitch_outcome="swinging_strike")

        assert result is not None
        assert state.outs == 0, "a dropped third strike records no out"
        assert state.bases.first == 900, "the batter reached first"
        assert result.re_start == pytest.approx(_re(0, EMPTY))
        assert result.re_end == pytest.approx(_re(0, ON_1B))
        assert result.runs == pytest.approx(0.38)

    def test_a_reach_on_error_the_loop_never_completes_is_honestly_worth_zero(self):
        """The SIM-496 shape: ``field_error`` with ``result_hits=0``.

        Nobody is placed on first, so nothing about the base-out state changed
        and 0.00 is the truthful value of what this simulator did.  This test
        pins that, and pins WHY, so nobody reads the 0.00 as an unfixed SIM-499.
        When SIM-496 lands and the batter reaches, the ledger will read +0.38
        with no further change here — the test above already proves that.
        """
        sm = StateMachine(
            resolver=_InjectedResolver(_sig("field_error", 0, 0, 0, is_error=True)),
            rng=np.random.default_rng(0),
        )
        state = _fresh_state()

        result = sm.step_pitch(state, pitch_outcome="in_play")

        assert result.is_error is True
        assert state.bases.runner_ids() == (), "SIM-496: the batter never reaches"
        assert state.outs == 0
        assert result.runs == pytest.approx(0.0)
        assert result.re_start == result.re_end
        # The value the SAME play carries once SIM-496 puts him on first.
        assert _re(0, ON_1B) - _re(0, EMPTY) == pytest.approx(0.38)


# ===========================================================================
# The resolver refuses to guess
# ===========================================================================


class TestResolveRunsRefusesToGuess:
    def test_a_partial_state_raises_and_names_what_is_missing(self):
        """A partial call used to fall through to the context-free weight.

        That is the silent failure SIM-499 removes: the caller believed it had
        asked for a context-aware value and got a context-free one.
        """
        with pytest.raises(ValueError) as excinfo:
            resolve_runs(event="single", pre_outs=0, pre_runners_state=ON_1B)
        message = str(excinfo.value)
        assert "post_outs" in message
        assert "post_runners_state" in message
        assert "result_runs" in message

    def test_no_state_at_all_raises(self):
        # The context-free linear-weight fallback was REMOVED 2026-08-19
        # (owner ruling, the SIM-511+512 landing): a hand-set per-event
        # constant never stands in for real states. No state = an error.
        with pytest.raises(ValueError, match="linear-weight fallback was removed"):
            resolve_runs(event="walk")

    def test_a_play_cannot_remove_outs(self):
        with pytest.raises(ValueError, match="cannot remove outs"):
            resolve_runs(
                event="single",
                pre_outs=2,
                pre_runners_state=EMPTY,
                post_outs=1,
                post_runners_state=EMPTY,
                result_runs=0,
            )

    def test_negative_runs_raise(self):
        with pytest.raises(ValueError, match="result_runs cannot be negative"):
            resolve_runs(
                event="single",
                pre_outs=0,
                pre_runners_state=EMPTY,
                post_outs=0,
                post_runners_state=ON_1B,
                result_runs=-1,
            )

    def test_the_post_state_is_echoed_not_derived(self):
        """Give an after-state the old formula would never have produced."""
        r = resolve_runs(
            event="single",
            pre_outs=0,
            pre_runners_state=LOADED,
            post_outs=0,
            post_runners_state=ON_2B,
            result_runs=2,
        )
        assert r.post_outs == 0
        assert r.post_runners_state == ON_2B
        assert r.runs == pytest.approx(_re(0, ON_2B) - _re(0, LOADED) + 2.0)

    def test_a_completed_half_inning_carries_no_run_expectancy(self):
        r = resolve_runs(
            event="field_out",
            pre_outs=2,
            pre_runners_state=LOADED,
            post_outs=3,
            post_runners_state=LOADED,
            result_runs=0,
        )
        assert r.re_end == 0.0
        assert r.runs == pytest.approx(-RE24_MATRIX[(2, LOADED)])


# ===========================================================================
# The ledger checks the transition it is handed
# ===========================================================================


class TestTheLedgerChecksTheTransition:
    """SIM-500's ``Bases.assert_transition``, wired at the commit.

    Wrong as a way to DERIVE the after-state, right as a way to CHECK one: the
    caller supplies the retired count that a derivation had to guess.
    """

    @staticmethod
    def _machine_and_state():
        sm = StateMachine(rng=np.random.default_rng(0))
        # SIM-500: these tests verify the GUARD, so they opt into it. The flag is
        # off by default on a machine with no full-pool sampler, because the
        # per-tile scaffold strands runners and reaches states real baseball
        # cannot. A test that asserts the guard raises must turn it on.
        sm._enforce_base_invariants = True
        state = _fresh_state()
        return sm, state

    def test_a_body_that_vanishes_unexplained_raises(self):
        """The runner is gone from the bases and the caller claims nothing
        happened to him.  That is the double-play desync, caught."""
        sm, state = self._machine_and_state()
        pre_bases = Bases(first=101)
        state.bases = Bases()  # he is gone
        with pytest.raises(ValueError, match="does not conserve runners"):
            sm._commit_run_delta(
                state,
                PlayResult(pitch_outcome="in_play", is_contact=True),
                event="grounded_into_double_play",
                result_hits=0,
                result_outs=2,
                result_runs=0,
                pre_outs=0,
                pre_bases=pre_bases,
                batter_reached=False,
                runners_scored=0,
                runners_retired=0,
            )

    def test_the_same_play_passes_once_the_caller_states_the_retirement(self):
        sm, state = self._machine_and_state()
        pre_bases = Bases(first=101)
        state.bases = Bases()
        result = PlayResult(pitch_outcome="in_play", is_contact=True)
        sm._commit_run_delta(
            state,
            result,
            event="grounded_into_double_play",
            result_hits=0,
            result_outs=2,
            result_runs=0,
            pre_outs=0,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=0,
            runners_retired=1,
        )
        assert result.runs == pytest.approx(-0.78)

    def test_a_body_that_appears_unexplained_raises(self):
        sm, state = self._machine_and_state()
        pre_bases = Bases()
        state.bases = Bases(first=901, second=902)
        with pytest.raises(ValueError, match="does not conserve runners"):
            sm._commit_run_delta(
                state,
                PlayResult(pitch_outcome="in_play", is_contact=True),
                event="single",
                result_hits=1,
                result_outs=0,
                result_runs=0,
                pre_outs=0,
                pre_bases=pre_bases,
                batter_reached=True,
                runners_scored=0,
                runners_retired=0,
            )

    def test_committing_after_the_outs_are_already_recorded_raises(self):
        """The out ledger and the run ledger must not be run in the wrong order."""
        sm, state = self._machine_and_state()
        state.outs = 1
        with pytest.raises(AssertionError, match="pre_outs"):
            sm._commit_run_delta(
                state,
                PlayResult(pitch_outcome="in_play", is_contact=True),
                event="field_out",
                result_hits=0,
                result_outs=1,
                result_runs=0,
                pre_outs=0,
                pre_bases=Bases(),
                batter_reached=False,
                runners_scored=0,
                runners_retired=0,
            )

    def test_a_context_free_weight_can_never_be_committed(self):
        """The ledger rejects any resolution that is not an RE24 delta."""
        sm, state = self._machine_and_state()

        def _linear_weight(**_kw):
            return RunResolution(runs=0.33, method="linear_weight")

        original = __import__("simulation.sim_loop", fromlist=["resolve_runs"]).resolve_runs
        import simulation.sim_loop as sim_loop_module

        sim_loop_module.resolve_runs = _linear_weight
        try:
            with pytest.raises(AssertionError, match="re24_delta"):
                sm._commit_run_delta(
                    state,
                    PlayResult(pitch_outcome="in_play", is_contact=True),
                    event="single",
                    result_hits=1,
                    result_outs=0,
                    result_runs=0,
                    pre_outs=0,
                    pre_bases=Bases(),
                    batter_reached=False,
                    runners_scored=0,
                    runners_retired=0,
                )
        finally:
            sim_loop_module.resolve_runs = original


# ===========================================================================
# The snapshot is a copy, not a reference
# ===========================================================================


class TestTheSnapshotIsACopy:
    def test_snapshot_bases_does_not_alias_the_live_bases(self):
        """``_resolve_walk`` mutates ``state.bases`` in place.  A snapshot that
        returned the same object would leave the ledger reading the after-state
        again, which is the whole defect."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101)

        snapshot = sm._snapshot_bases(state)
        assert snapshot is not state.bases
        assert snapshot.runner_ids() == (101,)

        state.bases.second = state.bases.first
        state.bases.first = 900
        assert snapshot.runner_ids() == (101,), "the snapshot moved with the live object"

    def test_every_committing_resolver_snapshots_above_its_own_mutation(self):
        """A structural guard on the placement trap.

        Each of the four resolvers that commit must take its snapshot before the
        first line that can touch the bases.  The retired SIM-349 sac-fly bias
        was the one that caught someone out first; the SIM-511 transition
        resolver inherits its pin — it consumes ``pre_bases``, so the snapshot
        must sit above its call.
        """
        import inspect

        import simulation.sim_loop as sim_loop_module

        # The mutator strings are CALL SITES, precise enough that a prose mention
        # of the same method in a comment cannot match them.
        for method_name, mutator in (
            (
                "_resolve_in_play",
                "self._resolve_in_play_transition(state, result, sig, pre_outs, pre_bases)",
            ),
            ("_resolve_walk", "b.third = rid_2"),
            ("_resolve_strikeout", "forced_run = self._force_on_reach(state, result)"),
            ("_resolve_steal_outcome", "self._move_runner(state, from_base, to_base)"),
        ):
            src = inspect.getsource(getattr(sim_loop_module.StateMachine, method_name))
            assert mutator in src, f"{method_name} no longer contains {mutator!r}"
            snap = src.index("pre_bases = self._snapshot_bases(state)")
            assert snap < src.index(mutator), (
                f"{method_name} snapshots the base state BELOW {mutator!r}, which "
                "mutates it. The snapshot must sit above every mutation."
            )


# ===========================================================================
# The ordinary plays still resolve, and the score is untouched
# ===========================================================================


class TestTheOrdinaryPlaysAreUnchanged:
    def test_a_strikeout_moves_no_body(self):
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101)
        result = None
        for _ in range(3):
            result = sm.step_pitch(state, pitch_outcome="called_strike")
        assert result is not None
        assert state.outs == 1
        assert result.re_start == pytest.approx(_re(0, ON_1B))
        assert result.re_end == pytest.approx(_re(1, ON_1B))
        assert result.runs == pytest.approx(_re(1, ON_1B) - _re(0, ON_1B))

    def test_a_home_run_counts_the_batter_once_on_each_side(self):
        """He reaches AND he scores, so the conservation identity balances."""
        resolver = _InjectedResolver(_sig("home_run", 4, 0, 2))
        resolver._injected_battedball = {"event": "home_run", "result_runs": 2}
        sm = StateMachine(resolver=resolver, rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(first=101)

        result = sm.step_pitch(state, pitch_outcome="in_play")

        assert result.runs_scored == 2
        assert state.away_score == 2  # top of the 1st: the away team bats
        assert state.bases.runner_ids() == ()
        assert result.runs == pytest.approx(_re(0, EMPTY) - _re(0, ON_1B) + 2.0)

    def test_the_score_still_commits_the_pool_supplied_runs(self):
        """SIM-499 changes the run VALUE, not the score.

        ``result_runs`` is what the score commits; the body count goes to the
        transition check.  This test pins that the fix did not quietly re-route
        the score through the body count.
        """
        resolver = _InjectedResolver(_sig("double", 2, 0, 1))
        resolver._injected_battedball = {"event": "double", "result_runs": 1}
        sm = StateMachine(resolver=resolver, rng=np.random.default_rng(0))
        state = _fresh_state()
        state.bases = Bases(second=202)

        result = sm.step_pitch(state, pitch_outcome="in_play")

        assert result.runs_scored == 1
        assert state.away_score == 1
        assert state.bases.second == 900  # the batter stands on second

    def test_a_walk_with_no_batter_id_reaches_nobody(self):
        """The count-machine path sets no batter, so nobody reaches first and the
        transition must say so rather than assume a body appeared."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(batter_id=None)
        result = _walk(state, sm)
        assert state.bases.runner_ids() == ()
        assert result.runs == pytest.approx(0.0)

    def test_the_bottom_half_is_valued_the_same_way(self):
        """The ledger is side-agnostic; only the score commit differs."""
        sm = StateMachine(rng=np.random.default_rng(0))
        state = _fresh_state(half=Half.BOTTOM)
        state.bases = Bases(first=101)
        result = _walk(state, sm)
        assert result.runs == pytest.approx(0.60)
