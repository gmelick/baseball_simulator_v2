"""
sim_loop.py
===========
The pitch-by-pitch game simulator: the state machine and the full-game driver.

Every decision is a similarity-weighted draw from a hard-filtered pool, never a
hand-tuned formula, and the drawn row IS the play (the owner's rulings of
2026-08-10 and 2026-08-29). All the draws read one
:class:`~simulation.full_pool_sampler.FullPoolSampler` over an engine-artifact
bundle: the production bundle on disk, or a :mod:`simulation.synthetic_bundle`
bundle in a test.

:class:`StateMachine` keeps its core here: the pitch step, the pitch draw, the
run ledger, the end of a plate appearance and the half-inning roll. Its other
methods come from the mixins in :mod:`simulation.loop`. :func:`simulate_game`
drives a machine through one game.

The count machine can run alone: a caller that passes ``pitch_outcome=`` to
:meth:`StateMachine.step_pitch` drives a fixed count sequence with no sampler.
Walks, strikeouts and the base-out bookkeeping resolve; an in-play pitch with
no sampler stays unresolved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from simulation.flags import read_bool
from simulation.game_state import (
    NO_PITCH,
    OUTS_PER_INNING,
    PITCH_OUTCOMES,
    Bases,
    GameState,
    Half,
    PlayResult,
    Team,
)
from simulation.loop.boxscore import BoxScore, GameSimResult
from simulation.loop.boxscore import (
    PlayerStatLine as PlayerStatLine,  # re-export: removed in the integration pass
)
from simulation.loop.credit import CreditMixin
from simulation.loop.in_play import InPlayMixin
from simulation.loop.manager import ManagerMixin
from simulation.loop.running_game import RunningGameMixin
from simulation.loop.terminals import TerminalsMixin
from simulation.loop.values import (
    _DOUBLE_PLAY_EVENTS as _DOUBLE_PLAY_EVENTS,  # re-export: removed in the integration pass
)
from simulation.loop.values import (
    _MAX_INNINGS,
    EVENT_HIT_BY_PITCH,
    EVENT_IN_PROGRESS,
    EVENT_STRIKEOUT,
    EVENT_WALK,
    FULL_POOL_STREAM,
    LOOP_STREAM,
    REGULATION_INNINGS,
    RunningGameTally,
    StealResolution,
    advance_count,
    spawn_rng_streams,
    times_through_order,
)
from simulation.run_resolution import resolve_runs


class StateMachine(InPlayMixin, RunningGameMixin, TerminalsMixin, ManagerMixin, CreditMixin):
    """The plate-appearance and half-inning state machine.

    It reads a mutable :class:`~simulation.game_state.GameState`, draws one
    pitch, runs the count machine (:func:`advance_count`), commits the result
    and returns a :class:`~simulation.game_state.PlayResult`. Every draw comes
    from ``full_pool_sampler``, a
    :class:`~simulation.full_pool_sampler.FullPoolSampler`. With no sampler the
    caller supplies each ``pitch_outcome`` and an in-play pitch stays
    unresolved: the count, out and base machine runs alone.
    """

    def __init__(
        self,
        full_pool_sampler=None,
        *,
        rng: np.random.Generator | None = None,
        manager=None,
        ibb_rates: dict[tuple[int, int, bool, bool], float] | None = None,
    ) -> None:
        self.rng = rng if rng is not None else np.random.default_rng()
        #: The measured intentional-walk rate per (runners_state, outs,
        #: is_late, is_close) cell, from ``sim.ibb_rates`` (the factory loads
        #: it). None, the no-database default, means no intentional walk fires.
        self.ibb_rates = ibb_rates
        #: The full-pool sampler: the pitch draw, the fielding draw, the
        #: advancement draws, the steal and pickoff draws and the pitching
        #: change all read it. None = the count machine alone (a caller
        #: supplies each pitch outcome; an in-play pitch stays unresolved).
        self.full_pool_sampler = full_pool_sampler
        # The pitch draw's weight has three lifetimes, so it has two cache
        # keys (the third part, the count, is the per-pitch bucket):
        #   * ``_fp_pitcher_key`` gates ``new_half_inning``, the pitcher factor
        #     over the WHOLE pool: per pitcher, batter hand, season and catcher.
        #   * ``_fp_pa_key`` gates ``new_plate_appearance``, the batter factor
        #     times the situation kernel: per batter AND base-out state. The
        #     base-out state moves INSIDE a plate appearance (a steal, a wild
        #     pitch, a caught stealing), and the situation factor must follow.
        # A single key once carried the batter and omitted the base-out state:
        # it recomputed the pitcher factor ~78 times a game for nothing and
        # left the situation factor stale after every mid-PA move (SIM-455).
        self._fp_pitcher_key: tuple | None = None
        self._fp_pa_key: tuple | None = None
        #: The live pitcher's (pitches before this plate appearance, times
        #: through the order), taken on the plate appearance's first pitch and
        #: held across its mid-PA rebuilds. None until the fatigue weight reads it.
        self._fp_pa_fatigue: tuple[int, int] | None = None
        #: The platoon weight on the fielding draw (``SIM_BB_PLATOON``): pass
        #: the live pitcher's hand. Read once per machine.
        self._bb_platoon = read_bool("SIM_BB_PLATOON")
        #: Honor the drawn pitch row's got-away fact (``SIM_GOT_AWAY``): a
        #: passed ball or wild pitch on THAT pitch, an uncaught third strike
        #: included, moves the runners. Off, the game reads no got-away fact.
        self._got_away = read_bool("SIM_GOT_AWAY")
        #: The last drawn pitch's got-away fact (False on every other path).
        self._last_pitch_got_away = False
        #: The manager gate: any non-None value turns the manager's decisions
        #: on (the pitching change, the intentional walk and the steal weight),
        #: which read the real per-side profiles on the game state, never this
        #: object. None = no manager: every decision does nothing.
        self.manager = manager
        #: Each defending team's available arms, staged by the production
        #: factory when ``SIM_MANAGER`` is on. ``simulate_game`` reads it when
        #: no ``bullpen=`` is passed; None = no pen, so no change can be made.
        self.bullpen: dict | None = None
        #: The manager decisions taken this game, one small dict each:
        #: {"kind", "inning", "leverage", plus the decision's own ids}.
        self.manager_decisions: list[dict] = []
        #: The pitching change as a draw from the change opportunity pool
        #: (``SIM_MANAGER_DRAW``; the factory sets it). Off, no change is drawn.
        self.manager_draw = False
        #: The steal or pickoff staged for this pitch; consumed and cleared by
        #: :meth:`_resolve_steal_outcome`, so it never carries across pitches.
        self._pending_steal: StealResolution | None = None
        #: The running game's counts (pickoffs, no-pitch third outs, voided
        #: steals, dropped-third-strike advances). It changes no play.
        self.running_game_tally = RunningGameTally()
        #: The game's box score, created on the first credit
        #: (:meth:`_box_line`), so a machine that is never driven carries
        #: none. ``simulate_game`` returns it on :class:`GameSimResult`.
        self.boxscore: BoxScore | None = None
        #: The outs errors kept from the defense this half-inning: a reach on
        #: an error that would have been an out adds 1. :meth:`_accumulate_pa`
        #: makes a run unearned once the inning "should have ended" (Rule
        #: 9.16(b)). :meth:`advance_half_inning` resets it.
        self._half_inning_error_outs_lost: int = 0

    # ===================================================================
    # The pitch step: read the GameState, draw, classify, commit, emit
    # ===================================================================

    def step_pitch(
        self,
        state: GameState,
        *,
        pitch_outcome: str | None = None,
    ) -> PlayResult:
        """Advance the game by exactly ONE pitch.

        The order:
          1. **Read** the GameState and check its live invariants.
          2. **The manager's decisions**: the pitching change on a plate
             appearance's first pitch (:meth:`_start_of_pa_hook`), then the
             intentional walk and, without the running game on the pitch, the
             single pre-pitch steal draw (:meth:`_pre_pitch_hook`). An
             intentional walk ends the plate appearance with no pitch.
          3. **The pickoff draw**, in the running game on the pitch (the
             sampler's steal pool carries the pitch class and
             ``steal_pitch_class`` is on): a pickoff throw is drawn BEFORE the
             pitch and resolved at once. A pickoff that makes the third out
             ends the half-inning with NO pitch thrown: the result's
             ``pitch_outcome`` is :data:`NO_PITCH` and ``pa_voided`` reads
             ``"pickoff_third_out"``. No pitch, event or plate appearance is
             credited; the ledger (:meth:`_record_outs`) credits the out to
             the pitcher on the mound, and a picked-off caught stealing charges
             the runner a caught stealing. The same batter leads off his
             team's next inning.
          4. **The pitch draw** from the sampler (the live count is the draw's
             bucket), unless the caller supplied ``pitch_outcome``. Then, in
             the running game on the pitch, the steal draw among real pitches
             of the same count and the same class as the pitch that came out.
          5. **The count machine** (:func:`advance_count`) classifies the pitch.
          6. **Commit**: the staged steal resolves first; on a terminal pitch
             the plate appearance resolves (walk, strikeout, ball in play) and
             rolls over, and the half-inning rolls at three outs.
          7. **Emit** a ``PlayResult`` whose ``next_state`` is the committed
             GameState.

        ``pitch_outcome`` (when given) MUST be one of
        :data:`simulation.game_state.PITCH_OUTCOMES`; it lets the count-machine
        tests drive fixed count sequences without a sampler.
        """
        # --- 1. read and check the live state --------------------------------
        state.assert_invariants(in_play=True)
        # Refuse an unknown injected outcome before any hook or draw runs, so a
        # refused pitch leaves nothing staged and no count moved.
        if pitch_outcome is not None and pitch_outcome not in PITCH_OUTCOMES:
            raise ValueError(f"pitch_outcome {pitch_outcome!r} is not one of {PITCH_OUTCOMES}.")
        # The got-away fact belongs to ONE drawn pitch; clear any stale carry
        # before this pitch draws (an injected outcome never sets it).
        self._last_pitch_got_away = False

        # --- 2. the manager's decisions --------------------------------------
        # The pitching change runs once, on the plate appearance's first pitch,
        # before the intentional walk and the steal. A steal staged by a
        # caller (``stage_steal``) stands; otherwise the pre-pitch hook may
        # stage one. ``_pending_steal`` is consumed and cleared in
        # :meth:`_resolve_steal_outcome`, so an attempt never carries across
        # pitches.
        if int(state.balls) == 0 and int(state.strikes) == 0:
            self._start_of_pa_hook(state)
        self._pre_pitch_hook(state)

        # An intentional walk ends the plate appearance without the count
        # machine: the batter is awarded first base through the walk's force
        # mechanics, and the plate appearance rolls over.
        if state.manager.intentional_walk_signalled:
            return self._issue_intentional_walk(state)

        # --- 3. the pickoff draw, before the pitch ---------------------------
        # The pickoff is its own draw from the count group of the steal
        # opportunity pool (the pitch rows and the pickoff rows). It resolves
        # now, so the pitch draw sees the bases it left. The result exists
        # before the pitch: the pickoff's out and run value commit to it, and
        # the pitch's fields are filled in once a pitch is thrown. The result
        # names the pitcher on the mound and his side; the manager's hooks ran
        # above, so a reliever who enters on this pitch is the one named.
        result = PlayResult(
            pitch_outcome=NO_PITCH,
            pitcher_id=state.pitcher_id,
            fielding_team=state.defense,
        )
        on_the_pitch = self._steal_order_active()
        picked = False
        if on_the_pitch and self._pending_steal is None:
            picked = self._pickoff_before_the_pitch(state, result)
            if picked and state.is_half_inning_over():
                # The pickoff made the third out. No pitch is thrown: no pitch
                # count, no event, no plate appearance. The batting order does
                # not move, so the same batter leads off the next inning with a
                # fresh count. The out is already on the pitcher's line
                # (_record_outs credited it); this return only rolls the
                # half-inning.
                result.pa_voided = "pickoff_third_out"
                self.running_game_tally.no_pitch_third_outs += 1
                self.advance_half_inning(state)
                result.next_state = state
                return result

        # Count this pitch: a pitch is thrown on every path below, so count it
        # here, once. The change draw reads the count; nothing else does.
        state.pitcher_pitch_count += 1

        # --- 4. the pitch draw -----------------------------------------------
        if pitch_outcome is None:
            if self.full_pool_sampler is None:
                raise ValueError(
                    "step_pitch needs either a full_pool_sampler or an explicit "
                    "pitch_outcome (count-machine-only mode)."
                )
            pitch_outcome = self._full_pool_outcome(state)

        # The steal draw runs AFTER the pitch, among the real pitches of the
        # same base, outs and count whose class is the class of the pitch that
        # came out: "went or stayed, safe or caught". The batter's result was
        # drawn with no knowledge of the steal, and the steal never changes it.
        # No steal draw on a pitch that already carried a pickoff outcome.
        if on_the_pitch and not picked:
            self._steal_opportunity_draw(state, pitch_class=pitch_outcome)

        # --- 5. the count machine --------------------------------------------
        # The drawn (or injected) outcome is taken as it is.
        adv = advance_count(state.balls, state.strikes, pitch_outcome)

        result.pitch_outcome = pitch_outcome
        result.is_contact = adv.is_contact
        result.pa_terminal = adv.terminal
        result.event = adv.event if adv.event != EVENT_IN_PROGRESS else None

        # --- 6. commit --------------------------------------------------------
        # A steal resolves on the pitch whether or not the plate appearance
        # ended. On a non-terminal pitch it is the only baserunning; on a
        # terminal pitch it resolves BEFORE the walk, strikeout or ball in
        # play, so a caught stealing that is the third out pre-empts the
        # plate appearance's result.
        if not adv.terminal:
            # A live plate appearance: resolve any steal (it can be the third
            # out), then commit the advanced count.
            self._resolve_steal_outcome(state, result)
            if state.is_half_inning_over():
                # A caught stealing ended the half-inning on a non-terminal
                # pitch: roll it (clears the bases, resets the count) and emit.
                self.advance_half_inning(state)
                result.next_state = state
                return result
            # The drawn pitch got away (a passed ball or a wild pitch): the
            # runners advance one base; the drawn row is the play. Skipped when
            # a steal or a pickoff already resolved this pitch's baserunning
            # (the pool rows mix the two channels; one mover per pitch keeps
            # the accounting canonical).
            if (
                self._last_pitch_got_away
                and not result.steal_attempted
                and not result.pickoff_out
                and not result.pickoff_error
            ):
                self._resolve_got_away_advance(state, result)
            state.balls = adv.balls
            state.strikes = adv.strikes
            state.assert_count_valid(mid_count=True)
            result.next_state = state
            return result

        # A terminal plate appearance. Commit the terminal count for
        # inspection, resolve the steal, then the plate appearance, then roll
        # over.
        state.balls = adv.balls
        state.strikes = adv.strikes

        # With two outs, a third strike the catcher HOLDS is the third out
        # before any throw, so a steal staged on that pitch is void: no stolen
        # base, no caught stealing (the owner's decision 3 of 2026-10-01). The
        # real pool holds no attempt there, so a class draw never stages one;
        # the rule guards a staged steal (a test, the single pre-pitch draw).
        # A pickoff is thrown before the pitch and is never voided. A third
        # strike that got away keeps the dropped-third-strike order below.
        pending = self._pending_steal
        if (
            adv.event == EVENT_STRIKEOUT
            and int(state.outs) == OUTS_PER_INNING - 1
            and not self._last_pitch_got_away
            and pending is not None
            and pending.attempted
            and not pending.pickoff
        ):
            self._pending_steal = None
            result.steal_voided = "third_out_first"
            voided = self.running_game_tally.voided
            voided["third_out_first"] = voided.get("third_out_first", 0) + 1

        # The dropped-third-strike rule reads first base and the outs AT THE
        # PITCH. A steal runs on the pitch, and the loop resolves it below,
        # before the strikeout, so after a steal the rule reads this snapshot.
        # A pickoff is thrown BEFORE the pitch, so after a pickoff the live
        # state is the state at the pitch.
        first_open_at_pitch = state.bases.first is None
        outs_at_pitch = int(state.outs)
        self._resolve_steal_outcome(state, result)
        if not state.is_half_inning_over():
            if adv.is_contact:
                self._resolve_in_play(state, result)
            elif adv.event == EVENT_WALK:
                self._resolve_walk(state, result)
            elif adv.event == EVENT_HIT_BY_PITCH:
                # The walk's force mechanics, under its own canonical event.
                self._resolve_walk(state, result, event=EVENT_HIT_BY_PITCH)
            elif adv.event == EVENT_STRIKEOUT:
                if result.steal_attempted:
                    self._resolve_strikeout(
                        state,
                        result,
                        first_open_at_pitch=first_open_at_pitch,
                        outs_at_pitch=outs_at_pitch,
                    )
                else:
                    self._resolve_strikeout(state, result)

        # --- 7. the end of the plate appearance, then emit -------------------
        # Advance the batting order, then reset the count for the next batter
        # or roll the half-inning.
        self._end_of_pa(state, result)
        result.next_state = state
        return result

    # ===================================================================
    # The out recorder: the one writer of a pitcher's outs
    # ===================================================================

    def _record_outs(self, state: GameState, n: int) -> None:
        """Record ``n`` outs and credit them to the pitcher on the mound.

        During live play the outs may reach 3 (the third out, which the
        half-inning roll then clears), so the check allows 3 and rejects 4 or
        more.

        This method is the one writer of a pitcher's outs on his box line (his
        innings pitched and the pitcher-outs prop).
        """
        if n < 0:
            raise ValueError("cannot record a negative number of outs.")
        state.record_out(n)
        # Allow the transient third out; the half-inning roll resets it.
        state.assert_outs_valid(in_play=False)
        # The ONE writer of a pitcher's outs. Every out the defense records
        # passes here once, before the half-inning rolls, so
        # ``state.pitcher_id`` still names the pitcher on the mound. A caught
        # stealing or a pickoff out on a pitch that does not end the plate
        # appearance gets the same credit as any other out. No other code may
        # add to ``outs_recorded`` on a box line, or an out counts twice.
        if n and state.pitcher_id is not None:
            self._box_line(int(state.pitcher_id)).outs_recorded += int(n)

    # ===================================================================
    # The pitch draw
    # ===================================================================

    def _full_pool_outcome(self, state: GameState) -> str:
        """Draw a pitch outcome from the full-pool sampler.

        The pitch weight has three parts with three lifetimes, and this method
        refreshes each one on its own trigger:

          * ``f_pitcher`` (``new_half_inning``) scores the WHOLE pool against
            the pitcher. It changes only with the pitcher, the batter hand,
            the season or the fielding catcher, so it recomputes once per
            pitcher, not once per batter.
          * ``f_batter`` and the situation factor (``new_plate_appearance``)
            recompute when the batter changes OR the base-out state changes.
            The base-out state moves INSIDE a plate appearance on a steal, a
            wild pitch or a caught stealing, and the situation factor must
            follow it.

        A pitcher change invalidates the plate-appearance weight too, because
        ``new_plate_appearance`` multiplies the base that ``new_half_inning``
        builds.

        The count is in neither key: the draw conditions on the live count
        per pitch through the bucket CDFs."""
        fp = self.full_pool_sampler
        hand = state.bat_hand if state.bat_hand in fp.a.pools else "R"
        season = int(getattr(state, "season", 2024) or 2024)

        # --- lifetime 1: f_pitcher, per (pitcher, hand, season, catcher) ------
        # The fielding catcher is in the key so the receiving factor (staged
        # in new_half_inning) follows a catcher change.
        catcher = state.away_catcher_id if state.offense == Team.HOME else state.home_catcher_id
        pitcher_key = (state.pitcher_id, hand, season, catcher)
        if pitcher_key != self._fp_pitcher_key:
            self._fp_pitcher_key = pitcher_key
            # The new base invalidates the PA weight built on top of the old one.
            self._fp_pa_key = None
            self._fp_pa_fatigue = None  # a new arm, a fresh snapshot
            fp.new_half_inning(
                hand,
                f"{state.pitcher_id}:{season}",
                catcher_key=(f"{int(catcher)}:{season}" if catcher is not None else None),
            )

        # --- lifetimes 2 + 3: f_batter (per batter) x f_situation (per base-out)
        bat = state.home_score if state.offense == Team.HOME else state.away_score
        fld = state.away_score if state.offense == Team.HOME else state.home_score
        score_diff = max(-5, min(5, int(bat) - int(fld)))
        # base-out only; the count is conditioned per pitch via the draw bucket.
        base_out = (int(state.outs), int(state.runners_state), int(state.inning), score_diff)
        # The conditioning inputs, passed ONLY when a weight is on. Off (the
        # default), the call below takes two positional arguments, so a
        # duck-typed sampler with that signature still works.
        extra: dict[str, object] = {}
        if (
            float(getattr(fp, "fatigue_pc_sigma", 0.0)) > 0.0
            or float(getattr(fp, "fatigue_tto_sigma", 0.0)) > 0.0
        ):
            # Fatigue is a PLATE-APPEARANCE fact: snapshot it on the PA's first
            # pitch (the 0-0 count — no later pitch of a PA reads 0-0) and hold
            # it across mid-PA base-out rebuilds. ``pitcher_pitch_count`` was
            # already incremented for THIS pitch, so "pitches before the PA" is
            # one less; ``pitcher_bf`` counts COMPLETED PAs, so it already reads
            # "batters faced before this PA" — the pool's own definition.
            if (int(state.balls) == 0 and int(state.strikes) == 0) or self._fp_pa_fatigue is None:
                self._fp_pa_fatigue = (
                    max(0, int(state.pitcher_pitch_count) - 1),
                    times_through_order(state.pitcher_bf.get(state.pitcher_id, 0)),
                )
            extra["pitch_count"], extra["tto"] = self._fp_pa_fatigue
        # The cell index and the batting-side weight read the live batting side.
        if float(getattr(fp, "pitch_home_off_weight", 1.0)) != 1.0 or bool(
            getattr(fp, "pitch_cell_index", False)
        ):
            extra["bat_home"] = state.offense == Team.HOME
        pa_key = (state.batter_id, season, base_out, tuple(sorted(extra.items())))
        if pa_key != self._fp_pa_key:
            self._fp_pa_key = pa_key
            fp.new_plate_appearance(
                f"{state.batter_id}:{season}",
                np.array(base_out, dtype=np.float32),
                **extra,
            )
        outcome = fp.draw(state.balls, state.strikes)
        # Carry the drawn row's got-away fact to the resolvers (read only when
        # the switch is on).
        if self._got_away:
            self._last_pitch_got_away = fp.last_pitch_got_away()
        return outcome

    # ===================================================================
    # The run ledger: every run value and out goes through here
    # ===================================================================

    @staticmethod
    def _snapshot_bases(state: GameState) -> Bases:
        """Copy the current base state.

        Several resolvers change ``state.bases`` **in place** before they
        commit, so a caller that keeps a reference keeps the changed object,
        and a ledger fed that reference reads the state AFTER the play as the
        state before it (SIM-499). Every caller of :meth:`_commit_run_delta`
        takes this copy first, at the top of the resolver, above every change.
        """
        b = state.bases
        return Bases(first=b.first, second=b.second, third=b.third)

    def _check_bases(self, bases: Bases, *, batter_id: int | None = None) -> None:
        """Assert the base state is legal. Passing ``batter_id`` also asserts
        the batter is not already standing on a bag."""
        bases.assert_consistent(batter_id=batter_id)

    def _commit_run_delta(
        self,
        state: GameState,
        result: PlayResult,
        *,
        event: str | None,
        result_hits: int,
        result_outs: int,
        result_runs: int,
        pre_outs: int,
        pre_bases: Bases,
        batter_reached: bool,
        runners_scored: int,
        runners_retired: int,
    ) -> None:
        """Resolve and commit ONE play's run value and base-out delta.

        This is the ONE place the loop turns a resolved play into a run value,
        a score change and an out count.

        **The caller supplies the state before the play.** ``pre_outs`` and
        ``pre_bases`` are what the caller measured BEFORE it touched anything.
        The state after the play is the live ``state``, which is ground truth
        here because every change has already happened. Neither state is
        derived. Reading the "before" state at commit time, after a resolver
        had moved the bases, mis-valued plays badly: a walk with a runner on
        first read +0.50 against a true +0.60, a caught stealing -0.24 against
        -0.62, a sac fly +0.76 against -0.13, a steal of home +1.00 against
        +0.11 (SIM-499).

        **The three transition facts** describe what happened to the bodies on
        the field, and go to :meth:`Bases.assert_transition`:

          * ``batter_reached`` — the batter BECAME a baserunner.  A batter
            retired at the plate is ``False`` and is NOT counted in
            ``runners_retired``; a home run is ``True`` and the batter is also
            one of ``runners_scored``.
          * ``runners_scored`` — bodies that crossed home, INCLUDING the batter
            on a home run.  This is a body count.  It is not always equal to
            ``result_runs``, which is the integer the pool supplied and the score
            commits; where they disagree, the pool row and the base state
            disagree, which is a defect elsewhere.
          * ``runners_retired`` — baserunners the fielders removed from a base.
            Outs on the batter are not in this count.

        The check is the runner-conservation identity: the bodies on base
        before plus the batter who reached equal the bodies on base after plus
        those who scored or were retired.

        ``result_hits`` does not feed the run value. It stays on the signature
        because it names the shape of the play; nothing reads it.
        """
        if int(state.outs) != int(pre_outs):
            raise AssertionError(
                f"_commit_run_delta: the caller passed pre_outs={pre_outs} but the "
                f"live state already holds outs={state.outs}. Outs must be recorded "
                "HERE, after the run value is resolved, never before the commit."
            )
        # A drawn multi-out event (a double play carrying 2 outs) can come from
        # a different base-out context: clamp it to the outs left in the
        # half-inning, so a double play with 2 out ends the inning at 3 rather
        # than overflowing to 4 (the ceiling check in _record_outs).
        outs_to_record = min(int(result_outs), max(0, OUTS_PER_INNING - int(pre_outs)))
        post_outs = int(pre_outs) + outs_to_record
        post_bases = state.bases

        # Check the runner-conservation identity. A failure here is a real
        # defect in the resolver that just ran. ``batter_id`` arms the two
        # checks that need to know who batted: no runner invented or swapped,
        # and the batter not standing on a base.
        pre_bases.assert_transition(
            post_bases,
            batter_reached=batter_reached,
            runners_scored=int(runners_scored),
            runners_retired=int(runners_retired),
            batter_id=state.batter_id,
        )

        rr = resolve_runs(
            event=event,
            pre_outs=int(pre_outs),
            pre_runners_state=int(pre_bases.runners_state),
            post_outs=post_outs,
            post_runners_state=int(post_bases.runners_state),
            result_runs=int(result_runs),
        )
        if rr.method != "re24_delta":
            raise AssertionError(
                f"_commit_run_delta: the ledger resolved by {rr.method!r}, not "
                "'re24_delta'. The context-free linear weight must never reach a "
                "committed run value (SIM-499)."
            )
        result.runs = rr.runs
        result.run_resolution_method = rr.method
        result.canonical_event = rr.canonical_event
        result.re_start = rr.re_start
        result.re_end = rr.re_end
        result.runs_scored += int(result_runs)
        if result_runs:
            state.add_runs(int(result_runs))
            state.assert_score_valid()
        if outs_to_record:
            result.outs_recorded += outs_to_record
            self._record_outs(state, outs_to_record)

    # ===================================================================
    # The end of a plate appearance and the half-inning roll
    # ===================================================================

    def _end_of_pa(self, state: GameState, result: PlayResult) -> None:
        """The end of a plate appearance: credit the box score, count the
        batter faced, advance the batting order, then reset the count for the
        next batter or roll the half-inning if the third out just landed.
        """
        # Credit the batter and the pitcher BEFORE the batting order advances:
        # ``state.pitcher_id`` still names the pitcher who just finished.
        self._accumulate_pa(state, result)

        # Count the batter the CURRENT pitcher just faced: the change draw and
        # the fatigue weight read it.
        pid = state.pitcher_id
        if pid is not None:
            state.pitcher_bf[pid] = state.pitcher_bf.get(pid, 0) + 1

        # Advance the batting-order pointer for the team that just batted.
        self._advance_batting_order(state)
        # One more plate appearance completed in this half.
        state.half_pa_count += 1

        if state.is_half_inning_over():
            # The third out ended the half-inning: the roll clears the bases,
            # resets the count and the outs, and flips the half.
            self.advance_half_inning(state)
        else:
            # Same half-inning, next batter: just reset the count.
            state.reset_count()
            state.batter_pa_count = 0
            # Re-validate the committed, ready-for-next-pitch state.
            state.assert_invariants(in_play=True)

    def advance_half_inning(self, state: GameState) -> None:
        """Roll the half-inning at three outs.

        * clear the bases,
        * reset the count and the outs to 0,
        * flip the half (TOP -> BOTTOM, or BOTTOM -> the next inning's TOP),
        * advance the inning number on the BOTTOM -> TOP flip,
        * point the matchup at the new half's due-up batter and pitcher.

        Each side's batting-order pointer lives on the GameState, so the side
        coming to bat resumes at the right batter. The game-level rules (the
        game-over check, the walk-off, the extra-innings ghost runner) are
        :func:`simulate_game`'s.
        """
        # Inspect the transient terminal state before clearing it.
        state.assert_outs_valid(in_play=False)  # tolerate outs == 3 here

        state.bases.clear()
        state.reset_outs()
        state.reset_count()
        state.half_pa_count = 0  # a fresh half inning
        # Errors don't carry across half-innings.
        self._half_inning_error_outs_lost = 0

        if state.half == Half.TOP:
            state.half = Half.BOTTOM
        else:
            state.half = Half.TOP
            state.inning += 1

        # Re-point the matchup at the new half's offense and defense (the
        # due-up batter and the fielding team's pitcher), so the draws follow
        # the game.
        self._set_half_matchup(state)

        # The freshly-rolled state must satisfy the live invariants.
        state.assert_invariants(in_play=True)

    # ===================================================================
    # The batting order and the half-inning matchup
    # ===================================================================

    def _advance_batting_order(self, state: GameState) -> None:
        """Advance the batting team's lineup-slot pointer by one, wrapping at
        the lineup length, and point ``batter_id`` at the new slot.

        No-op when the relevant lineup is empty (the count-machine tests run
        without a populated lineup).
        """
        if state.offense == Team.HOME:
            lineup = state.home_lineup
            if lineup:
                state.home_lineup_slot = (state.home_lineup_slot + 1) % len(lineup)
                state.batter_id = lineup[state.home_lineup_slot]
        else:
            lineup = state.away_lineup
            if lineup:
                state.away_lineup_slot = (state.away_lineup_slot + 1) % len(lineup)
                state.batter_id = lineup[state.away_lineup_slot]
        # The draws' batter hand follows the new batter.
        state.bat_hand = state.bat_hand_for(state.batter_id)

    def _set_half_matchup(self, state: GameState) -> None:
        """Point the matchup at the new half's offense and defense.

        :meth:`advance_half_inning` calls it AFTER the half flips: the side now
        batting resumes at its slot pointer (its due-up batter), and the
        pitcher becomes the fielding team's current pitcher (the away pitcher
        when home bats, and the reverse), refreshing the ``throw_hand`` and
        ``bat_hand`` the draws read. Each step is guarded, so the
        count-machine path (no lineups, no pitcher map) keeps its fixed
        matchup. A game that names only ONE side's pitcher never reaches here:
        ``simulate_game`` and the lineup resolver both refuse it, because the
        ``None`` guard below would leave the other side's pitcher on the mound.
        """
        if state.offense == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
            pitcher = state.away_pitcher_id  # away team pitches when home bats
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
            pitcher = state.home_pitcher_id
        if lineup:
            state.batter_id = lineup[slot % len(lineup)]
        if pitcher is not None:
            # With a manager wired, save the OUTGOING pitcher's count and
            # restore the INCOMING pitcher's own, so the two starters do not
            # share one count across half-innings (the change draw reads the
            # live ``pitcher_pitch_count``). A pulled starter's count is never
            # restored: the half swaps to the reliever in his place.
            if self.manager is not None and state.pitcher_id is not None:
                state.pitcher_pc[state.pitcher_id] = int(state.pitcher_pitch_count)
            state.pitcher_id = pitcher
            state.throw_hand = state.throw_hands.get(pitcher, state.throw_hand)
            if self.manager is not None:
                state.pitcher_pitch_count = int(state.pitcher_pc.get(pitcher, 0))
        state.bat_hand = state.bat_hand_for(state.batter_id)


# ---------------------------------------------------------------------------
# The full-game driver
# ---------------------------------------------------------------------------


def _place_ghost_runner(state: GameState) -> None:
    """Put the extra-innings automatic runner on second base.

    The rule's runner is the player who made the last out of the previous
    inning; the loop uses the batting team's previous lineup slot, the batter
    "due up minus one", so a real player id stands on second when a lineup is
    wired. Without a lineup the bag holds a synthetic id. Called only on the
    first pitch of an extra half-inning with the bases empty.
    """
    if state.bases.runners_state != 0:
        return  # already seeded / runners present — never double-place.
    lineup = state.home_lineup if state.offense == Team.HOME else state.away_lineup
    slot = state.home_lineup_slot if state.offense == Team.HOME else state.away_lineup_slot
    ghost_id: int | None = None
    if lineup:
        ghost_id = lineup[(slot - 1) % len(lineup)]
    # A non-None id so the bag reads as occupied even without a lineup; a
    # negative-id guard in Bases.assert_consistent rejects bad ids, so use a
    # large positive synthetic id for the no-lineup case.
    state.bases.second = ghost_id if ghost_id is not None else 999_000_000
    state.bases.assert_consistent()


def _game_over_after_half(state: GameState) -> bool:
    """The game-over check, run when a half-inning has just *completed*.

    ``state`` is the state AFTER the roll, so its ``inning`` and ``half``
    describe the half about to start; the check reasons about the half that
    just finished:

      * Just finished the **top** of an inning -> ``state.half == BOTTOM`` (the
        same inning). The game ends only if ``inning >= 9`` AND the **home
        team already leads**: the bottom is not played.
      * Just finished the **bottom** of an inning -> ``state.half == TOP`` and
        ``inning`` was incremented, so the completed inning is ``inning - 1``.
        The game ends if that inning is ``>= 9`` AND the score is **not tied**
        (a tie after a completed bottom means extra innings).
    """
    if state.half == Half.BOTTOM:
        # Top of `state.inning` just finished; bottom is pending.
        completed_inning = state.inning
        if completed_inning >= REGULATION_INNINGS and state.home_score > state.away_score:
            return True  # home leads after the top of the 9th+ -> no bottom.
        return False
    # state.half == TOP: the bottom of (state.inning - 1) just finished.
    completed_inning = state.inning - 1
    return bool(completed_inning >= REGULATION_INNINGS and state.home_score != state.away_score)


def _is_walkoff_live(state: GameState) -> bool:
    """True when a run that just scored is a walk-off: the **bottom** of the
    9th or any later inning with the **home team leading**. Checked after
    every pitch, so the game ends on the run that takes the lead, mid-inning,
    with no further batters."""
    return (
        state.half == Half.BOTTOM
        and state.inning >= REGULATION_INNINGS
        and state.home_score > state.away_score
    )


def simulate_game(
    state_machine: StateMachine | None = None,
    *,
    initial_state: GameState | None = None,
    seed: int | None = None,
    pitcher_id: int = 0,
    bat_hand: str = "R",
    season: int = 2024,
    away_lineup: list[int] | None = None,
    home_lineup: list[int] | None = None,
    bat_hands: dict[int, str] | None = None,
    throw_hands: dict[int, str] | None = None,
    home_pitcher_id: int | None = None,
    away_pitcher_id: int | None = None,
    home_catcher_id: int | None = None,
    away_catcher_id: int | None = None,
    home_defense: dict[str, int] | None = None,
    away_defense: dict[str, int] | None = None,
    park_run_factor: float = 1.0,
    venue_id: int | None = None,
    manager=None,
    bench=None,
    bullpen=None,
    pitcher_rest_days: dict[int, float] | None = None,
    # Each side's manager, the pen's recent usage and source, the per-side
    # profiles and the league means (all optional; every consumer is neutral
    # without them).
    home_manager_id: int | None = None,
    away_manager_id: int | None = None,
    pitcher_recent_usage: dict[int, tuple[int, int, int]] | None = None,
    bullpen_source: str | None = None,
    manager_profiles: Mapping[Any, dict[str, float]] | None = None,
    manager_league_profile: dict[str, float] | None = None,
    max_innings: int = _MAX_INNINGS,
    start_state: Mapping[str, Any] | None = None,
) -> GameSimResult:
    """Drive a :class:`StateMachine` through one complete game.

    The driver calls :meth:`StateMachine.step_pitch` until the game ends; the
    machine owns the count, the plays and the half-inning roll. The driver
    adds the game-level rules:

      * **Regulation:** nine full innings; the bottom of the 9th is skipped
        when the home team leads after the top.
      * **Walk-off:** the game ends at once, mid-inning, on the run that gives
        the home team the lead in the bottom of the 9th or later.
      * **Extra innings:** when tied after nine, more full innings, each half
        starting with the **ghost runner on second**.
      * **Determinism:** the per-game ``seed`` spawns two independent streams,
        one for the machine's loop rng (advancement, steal outcomes, the
        manager's decisions) and one for the sampler's rng (the pitch and the
        batted ball), so a (game, seed) pair replays exactly.
      * **The inning grid:** each team's runs per inning, on the result.

    Pass a built ``state_machine`` (the production factory's, or a test's over
    a :mod:`simulation.synthetic_bundle` bundle), or let the driver build a
    count-machine-only machine. Pass an ``initial_state``, or let the driver
    build a fresh "top of the 1st" GameState from ``pitcher_id`` /
    ``bat_hand`` / ``season`` / the two lineups and the other keyword facts.
    ``start_state`` starts the game at a plate appearance of the real game
    (:mod:`simulation.start_state`). The driver needs both lineups: it raises
    ``ValueError`` before the first pitch when either side has no batting
    order, or when the game names one side's starting pitcher and not the
    other's.

    The manager's inputs (``manager``, ``bullpen``, ``pitcher_rest_days``) are
    wired only when supplied. ``manager`` is attached to the machine only if
    it carries none (a built machine wins); ``bench`` is accepted and ignored.
    ``bullpen`` (a ``{Team|int: [pitcher_id, ...]}`` map, else the machine's
    own ``bullpen``) seeds ``state.manager.bullpen_available`` for the
    pitching change; ``pitcher_rest_days`` (``{pitcher_id: days}``) goes on
    the state. With none supplied, ``manager`` stays None and every manager
    decision does nothing.

    Returns a :class:`GameSimResult`. A machine with no sampler must be a
    subclass that supplies each pitch outcome itself (the count-machine-only
    drivers); otherwise ``step_pitch`` raises.
    """
    # --- two INDEPENDENT random streams from the one per-game seed ----------
    _streams = spawn_rng_streams(seed)
    loop_rng = _streams[LOOP_STREAM]
    full_pool_rng = _streams[FULL_POOL_STREAM]

    # --- Build the machine (thread the seed through its loop rng) -------------
    if state_machine is None:
        state_machine = StateMachine(rng=loop_rng)
    elif seed is not None:
        # Re-seed the supplied machine's loop rng so the per-game seed governs
        # the loop's draws.
        state_machine.rng = loop_rng

    # --- the manager (wired only when supplied) -------------------------------
    # A built machine's own manager wins; with no manager anywhere every
    # manager decision does nothing.
    if manager is not None and getattr(state_machine, "manager", None) is None:
        state_machine.manager = manager

    # --- Thread the seed through the full-pool sampler's rng ------------------
    # The production factory caches one sampler per worker process and reuses
    # it across seeds, so the per-game rng must be re-seeded here for the
    # cached sampler to draw reproducibly per seed. It gets stream 1.
    if seed is not None:
        fp = getattr(state_machine, "full_pool_sampler", None)
        if fp is not None and hasattr(fp, "rng"):
            fp.rng = full_pool_rng

    # --- Build the initial GameState (fresh top of the 1st) ------------------
    if initial_state is None:
        initial_state = GameState(
            pitcher_id=pitcher_id,
            bat_hand=bat_hand,
            season=season,
            away_lineup=list(away_lineup or []),
            home_lineup=list(home_lineup or []),
            seed=seed,
        )
        # Point the batter at the leadoff slot when a lineup is wired.
        if initial_state.away_lineup:
            initial_state.batter_id = initial_state.away_lineup[0]
        # The per-batter hands and both starters, so the draws' hands follow
        # the lineup. All of these are plain data, so they survive the batch
        # runner's sim-kwargs path.
        if bat_hands:
            initial_state.bat_hands = dict(bat_hands)
        if throw_hands:
            initial_state.throw_hands = dict(throw_hands)
        initial_state.home_pitcher_id = home_pitcher_id
        initial_state.away_pitcher_id = away_pitcher_id
        # The starters, kept apart from the current pitchers.
        initial_state.home_starter_id = home_pitcher_id
        initial_state.away_starter_id = away_pitcher_id
        initial_state.home_catcher_id = home_catcher_id
        initial_state.away_catcher_id = away_catcher_id
        # Each team's defense map (position name -> player_id) for the fielder
        # factor, and the venue run factor for the park kernel. Empty or 1.0
        # leaves the consumers neutral.
        if home_defense:
            initial_state.home_defense = dict(home_defense)
        if away_defense:
            initial_state.away_defense = dict(away_defense)
        initial_state.park_run_factor = float(park_run_factor)
        # The live park, for the fence stage.
        if venue_id is not None:
            initial_state.park = str(int(venue_id))
        initial_state.bat_hand = initial_state.bat_hand_for(initial_state.batter_id)
    state = initial_state
    if seed is not None and state.seed is None:
        state.seed = seed
    # The "what if" (simulation/start_state.py): start at a plate appearance of
    # the real game. Plain data applied to this iteration's own fresh state.
    if start_state:
        from simulation.start_state import apply_start_state

        apply_start_state(state, start_state)

    # --- A game needs a batting order on each side ---------------------------
    # The loop rotates the batter through the lineup and seats a batter who
    # reaches base by his id. With no lineup the batter id is None, and the
    # first hit trips the base check deep inside the in-play resolver with a
    # message that does not name the cause. So refuse here and name the
    # requirement. The check reads the state, so the kwargs path and the
    # initial_state path get the same rule.
    away_batters = len(state.away_lineup or [])
    home_batters = len(state.home_lineup or [])
    if away_batters == 0 or home_batters == 0:
        raise ValueError(
            "simulate_game needs a batting order on each side: pass away_lineup "
            "and home_lineup, or an initial_state that carries them (got "
            f"{away_batters} away and {home_batters} home batters)."
        )

    # --- A game that names one starter names both -----------------------------
    # ``_set_half_matchup`` swaps the pitcher at each half from the two ids.
    # With one id missing it changes nothing, so the other side's pitcher stays
    # on the mound and pitches to his own team. A game with neither id is the
    # fixed-matchup seam of the no-database tests (one pitcher id for the
    # whole game) and still plays.
    if (state.home_pitcher_id is None) != (state.away_pitcher_id is None):
        missing = "away" if state.away_pitcher_id is None else "home"
        raise ValueError(
            f"simulate_game needs a starting pitcher on each side: the {missing} "
            "side has none (got home_pitcher_id="
            f"{state.home_pitcher_id!r}, away_pitcher_id={state.away_pitcher_id!r}). "
            "Without it the other side's pitcher pitches both halves."
        )

    # --- The pen and the per-pitcher rest, onto the state ---------------------
    # Applied whether the state was built here or passed in. ``bullpen`` is the
    # per-defending-team available-arms map the pitching change pops from;
    # copy the arm lists so popping a reliever never changes the caller's
    # bullpen. With no explicit bullpen, fall back to the one staged on the
    # machine (the production factory's). Neither present: no pen.
    eff_bullpen = bullpen if bullpen else getattr(state_machine, "bullpen", None)
    if eff_bullpen:
        state.manager.bullpen_available = {team: list(arms) for team, arms in eff_bullpen.items()}
    eff_rest = (
        pitcher_rest_days
        if pitcher_rest_days
        else getattr(state_machine, "pitcher_rest_days", None)
    )
    if eff_rest:
        state.pitcher_rest_days = dict(eff_rest)
    # The managers, the pen's recent usage and source, and the profiles.
    if home_manager_id is not None:
        state.home_manager_id = int(home_manager_id)
    if away_manager_id is not None:
        state.away_manager_id = int(away_manager_id)
    if pitcher_recent_usage:
        state.pitcher_recent_usage = {
            int(k): (int(v[0]), int(v[1]), int(v[2])) for k, v in pitcher_recent_usage.items()
        }
    if bullpen_source is not None:
        state.bullpen_source = str(bullpen_source)
    if manager_profiles:
        state.away_manager_profile = dict(
            manager_profiles.get(0) or manager_profiles.get("0") or {}
        )
        state.home_manager_profile = dict(
            manager_profiles.get(1) or manager_profiles.get("1") or {}
        )
    if manager_league_profile:
        state.manager_league_profile = dict(manager_league_profile)
    # The start state's pen (the arms not yet used) wins over the game's pen.
    if start_state:
        from simulation.start_state import apply_start_bullpen

        apply_start_bullpen(state, start_state)

    # --- The game loop -------------------------------------------------------
    total_pitches = 0
    outs_played = 0
    walk_off = False
    # A pitch-count ceiling so a pathological machine that never records an out
    # (e.g. a degenerate injected outcome) cannot spin forever: a real inning is
    # ~15-40 pitches, so 200/inning is far above any plausible game yet bounds
    # the loop independently of the inning pointer (which only advances on a
    # completed half-inning).
    max_pitches = max(int(max_innings), REGULATION_INNINGS) * 200
    # The inning whose play actually decided / completed the game.  Tracked
    # explicitly because the half-inning roll advances ``state.inning`` PAST the
    # completed inning, so the post-loop pointer over-counts by one after a
    # completed regulation bottom half.
    last_played_inning = state.inning
    # Track whether the current half-inning has had its first pitch yet so the
    # ghost runner is seeded exactly once, at the start of each extra half.
    half_inning_open = False
    cur_inning, cur_half = state.inning, state.half
    # A start state is the real game: an extra half already has its runner on
    # second (or has moved him), so the first half is never seeded again.
    if start_state:
        half_inning_open = True

    # --- The inning grid ------------------------------------------------------
    # One run cell per inning for each team. The loop reads the score at the
    # start of each half; when the half ends, the batting side's cell is the
    # score now minus the score then. The roll comes after the runs are
    # credited, so a third-out play's runs land in the half it ended, as in
    # ``simulation.linescore``. The grid only reads the state: it draws no
    # random number.
    # A game that starts later than the top of the 1st (a caller's
    # ``initial_state``) has a ``None`` cell for each half it did not play,
    # as ``linescore_from_plays`` gives for the same plays.
    home_by_inning: list[int | None] = [None] * (state.inning - 1)
    away_by_inning: list[int | None] = [None] * (state.inning - 1)
    if state.half == Half.BOTTOM:
        away_by_inning.append(None)
    half_start_home, half_start_away = state.home_score, state.away_score
    # A start state carries the real runs of every completed half and the score
    # when the current half began, so the grid sums to the final score.
    if start_state:
        from simulation.start_state import start_grid

        away_by_inning, home_by_inning, half_start_away, half_start_home = start_grid(start_state)

    def _close_half(half: Half) -> None:
        """Write the runs of the half that just ended into its team's list."""
        if half == Half.TOP:
            away_by_inning.append(state.away_score - half_start_away)
        else:
            home_by_inning.append(state.home_score - half_start_home)

    while True:
        # Detect a brand-new half-inning (the pointer changed, or the very first
        # pitch of the game): re-arm the per-half ghost-runner seeding.
        if (state.inning, state.half) != (cur_inning, cur_half) or not half_inning_open:
            cur_inning, cur_half = state.inning, state.half
            half_inning_open = True
            # Every EXTRA half-inning (inning > regulation) starts with the
            # automatic runner on second.
            if state.inning > REGULATION_INNINGS:
                _place_ghost_runner(state)

        # --- one pitch (the machine owns the pitch step) -----------------
        prev_inning, prev_half = state.inning, state.half
        last_played_inning = prev_inning
        step = state_machine.step_pitch(state)
        # A pickoff that made the third out before the pitch threw no pitch
        # (its result reads NO_PITCH); it rolls the half, so the inning
        # pointer still bounds the loop.
        if getattr(step, "no_pitch", False) is not True:
            total_pitches += 1
        # The play stream's outs; the pitchers' box lines must sum to it.
        outs_played += int(getattr(step, "outs_recorded", 0) or 0)

        # The committed state is always invariant-valid.
        state.assert_invariants(in_play=True)

        rolled = (state.inning, state.half) != (prev_inning, prev_half)
        if rolled:
            # A half-inning just completed (3 outs).  Re-arm for the next half.
            half_inning_open = False
            # The inning grid. A half that ended on a pickoff with no pitch
            # was still played and gets its cell too.
            _close_half(prev_half)
            half_start_home, half_start_away = state.home_score, state.away_score
            if _game_over_after_half(state):
                break
            continue

        # Same half-inning still in progress: check for a walk-off (the home
        # team took the lead in the bottom of the 9th or later -> end
        # mid-inning, no further batters; the half-inning does NOT complete).
        if _is_walkoff_live(state):
            walk_off = True
            # The walk-off half was played; its runs go in the grid.
            _close_half(state.half)
            break

        # Safety guard: never spin past the inning OR pitch ceiling (the latter
        # bounds a pathological never-an-out machine that never rolls an inning).
        if state.inning > max_innings or total_pitches >= max_pitches:
            # The half in progress had a pitch; its runs go in the grid.
            _close_half(state.half)
            break

    # ``last_played_inning`` is the inning the deciding play happened in, so it is
    # the true count of innings played (a completed regulation bottom-9th leaves
    # the pointer at the top of the 10th, but only 9 innings were actually
    # played).
    innings_played = last_played_inning
    extra_innings = last_played_inning > REGULATION_INNINGS

    # Give both lists one cell per inning. The home list is the short one when
    # the game ended after a top half: the bottom was never played, so its
    # cell is None (the scoreboard "x").
    grid_len = max(len(home_by_inning), len(away_by_inning))
    home_by_inning.extend([None] * (grid_len - len(home_by_inning)))
    away_by_inning.extend([None] * (grid_len - len(away_by_inning)))

    return GameSimResult(
        home_score=state.home_score,
        away_score=state.away_score,
        innings_played=innings_played,
        final_state=state,
        walk_off=walk_off,
        extra_innings=extra_innings,
        seed=seed,
        total_pitches=total_pitches,
        outs_played=outs_played,
        # The box score the machine accumulated; None if no plate appearance
        # was ever credited.
        boxscore=getattr(state_machine, "boxscore", None),
        home_by_inning=home_by_inning,
        away_by_inning=away_by_inning,
    )


__all__ = [
    "BoxScore",
    "GameSimResult",
    "StateMachine",
    "simulate_game",
]
