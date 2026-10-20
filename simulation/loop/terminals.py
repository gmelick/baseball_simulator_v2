"""The plate appearances that end without a ball in play: the walk, the hit by
pitch, the intentional walk, the strikeout and the dropped third strike."""

from __future__ import annotations

from simulation.game_state import (
    OUTS_PER_INNING,
    GameState,
    PlayResult,
)
from simulation.loop.base import LoopMixin
from simulation.loop.values import (
    EVENT_INTENTIONAL_WALK,
    EVENT_STRIKEOUT,
    EVENT_WALK,
)


class TerminalsMixin(LoopMixin):
    """The state machine's walk and strikeout methods."""

    def _resolve_walk(self, state: GameState, result: PlayResult, event: str = EVENT_WALK) -> None:
        """Resolve ball four: force the runners and score any forced run,
        through the run ledger.

        A hit by pitch awards first base with the same force mechanics, so it
        resolves here too: pass ``event=EVENT_HIT_BY_PITCH`` and the play
        carries its own canonical event (never counted as a walk).

        A walk puts the batter on first and pushes each runner ahead only when
        the bag behind him is occupied (a true force). Each forced advance is
        recorded in ``result.baserunner_advances``, so the per-runner run
        credit in :meth:`_accumulate_pa` fires for a run the walk forces home.
        """
        result.event = event
        result.pa_terminal = True
        b = state.bases
        # Measure the base-out state BEFORE the force pushes anyone. ``b`` is
        # changed in place below, so the ledger needs a copy, not a reference
        # (SIM-499).
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        # Capture the runners before the push overwrites them: the advances
        # dict is keyed on the runner who moved, not the bag.
        rid_1 = b.first
        rid_2 = b.second
        rid_3 = b.third
        # A walk forces runners only on consecutive occupancy from 1B.
        forced_run = 0
        if rid_1 is not None:
            if rid_2 is not None:
                if rid_3 is not None:
                    forced_run = 1  # bases loaded -> run forced home
                    # Runner on 3B scored (end_base == 0 triggers the R credit).
                    result.baserunner_advances[int(rid_3)] = 0
                # push 2B -> 3B
                b.third = rid_2
                result.baserunner_advances[int(rid_2)] = 3
            # push 1B -> 2B
            b.second = rid_1
            result.baserunner_advances[int(rid_1)] = 2
        # Batter to 1B, unconditionally. With no batter set (the count-machine
        # path, which runs with no lineups) first base is simply vacant: the
        # push above already moved its runner to second, and keeping him on
        # first too would stand one runner on two bags.
        b.first = state.batter_id
        self._check_bases(b)
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        # The walk's after-state is deterministic: the batter reaches 1B (unless
        # no batter is set), and any forced run is a body that left the bases.
        # No runner is retired on a walk.
        self._commit_run_delta(
            state,
            result,
            event=event,
            result_hits=0,
            result_outs=0,
            result_runs=forced_run,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=state.batter_id is not None,
            runners_scored=forced_run,
            runners_retired=0,
        )

    def _issue_intentional_walk(self, state: GameState) -> PlayResult:
        """Issue the intentional walk the manager's hook signalled.

        No pitch is thrown: the batter is awarded first base through the walk's
        force mechanics, and the plate appearance rolls over (the batting order
        advances; the count resets or the half-inning rolls). The result's
        ``pitch_outcome`` reads ``"ball"``, since an intentional walk is
        recorded as four balls.

        The play carries its OWN canonical event, ``intentional_walk``, so the
        box credits a walk and a non-at-bat while the lane's intentional-walk
        band can still see it.
        """
        result = PlayResult(
            pitch_outcome="ball",
            is_contact=False,
            pa_terminal=True,
            event=EVENT_INTENTIONAL_WALK,
            # The walk names the pitcher who issues it and his side.
            pitcher_id=state.pitcher_id,
            fielding_team=state.defense,
        )
        # Consume the signal so it never carries to the next pitch / PA.
        state.manager.intentional_walk_signalled = False
        self._resolve_walk(state, result, event=EVENT_INTENTIONAL_WALK)
        self._end_of_pa(state, result)
        result.next_state = state
        return result

    def _resolve_strikeout(
        self,
        state: GameState,
        result: PlayResult,
        *,
        first_open_at_pitch: bool | None = None,
        outs_at_pitch: int | None = None,
    ) -> None:
        """Resolve strike three, the dropped third strike included.

        An ordinary strikeout commits one out through the run ledger. On a
        dropped third strike (a swinging strike three that got away, with
        first base open or two outs) the batter reaches first and no out is
        recorded.

        The reach commits as a STRIKEOUT, because the play is one: official
        scoring credits the pitcher's strikeout and charges the batter's
        (Rule 9.15(a)(3)). A run the reach forces home pays no RBI: Rule
        9.04(a) pays one only on a hit, a sacrifice, an infield out, a
        fielder's choice or a bases-full award, and this run scores on the
        wild pitch or the passed ball. ``first_open_at_pitch`` and
        ``outs_at_pitch`` carry the state at the pitch after a steal on the
        same pitch has moved it (:meth:`step_pitch`). Without them, the live
        state is the state at the pitch.
        """
        result.event = EVENT_STRIKEOUT
        result.pa_terminal = True
        # Read the dropped-third-strike rule at the pitch, before this method
        # moves anyone. A got-away strike three that CANNOT award first (1B
        # occupied, under two outs) still advances the runners: the ball
        # still got away. The advance commits its own delta. The strikeout's
        # snapshots below then measure the post-advance state, so the two
        # commits chain like a steal and a strikeout.
        d3k = self._dropped_third_strike(
            state,
            result,
            first_open_at_pitch=first_open_at_pitch,
            outs_at_pitch=outs_at_pitch,
        )
        # One mover per pitch, as on a non-terminal pitch: when a steal or a
        # pickoff already resolved this pitch's baserunning, the got-away
        # moves nobody.
        #
        # The advance runs on the dropped-third-strike reach too, BEFORE the
        # batter takes first (the owner's decision 4 of 2026-10-01). Every
        # runner moves up one base while the ball is loose (28 of 32 real
        # unforced runners moved, 2023-2026); a runner on third scores with no
        # RBI. First base is then open, so the reach below pushes nobody.
        if (
            self._last_pitch_got_away
            and not result.steal_attempted
            and not result.pickoff_out
            and not result.pickoff_error
        ):
            b = state.bases
            on_base = sum(x is not None for x in (b.first, b.second, b.third))
            self._resolve_got_away_advance(state, result)
            if d3k:
                self.running_game_tally.d3k_runners_moved += on_base
        # Measure the base-out state before _force_on_reach can push anyone:
        # ``_force_on_reach`` changes ``state.bases`` in place.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        if d3k:
            # The batter reaches first with no out, pushing forced runners as a
            # walk does. After the got-away advance above first base is open,
            # so the push moves nobody unless a steal or a pickoff on this
            # pitch skipped the advance.
            self.running_game_tally.d3k_reaches += 1
            forced_run = self._force_on_reach(state, result)
            # The forced run scores on the wild pitch or the passed ball, not
            # on the batter, so it pays no RBI. The got-away advance and the
            # steal of home use the same marker.
            #
            # The run stays EARNED. The pool's got-away flag has no kind, so
            # the loop cannot tell a wild pitch (earned) from a passed ball
            # (unearned). About seven in ten real uncaught third strikes are
            # wild pitches (2023-2026). Splitting the charge needs a kind
            # column on the pitch pool and a rebuild.
            result.steal_runs_scored += forced_run
            # The reach commits as a strikeout and is not a hit
            # (``result_hits=0``). The run value comes from the two base-out
            # states, never from the label.
            self._commit_run_delta(
                state,
                result,
                event=EVENT_STRIKEOUT,  # the batter struck out and reached 1B
                result_hits=0,
                result_outs=0,
                result_runs=forced_run,
                pre_outs=pre_outs,
                pre_bases=pre_bases,
                batter_reached=state.batter_id is not None,
                runners_scored=forced_run,
                runners_retired=0,
            )
            return
        # Ordinary strikeout: one out, no base/score change beyond the out.  The
        # batter is retired at the plate, so he never becomes a runner and he is
        # NOT a retired baserunner: the bases do not move at all.
        self._commit_run_delta(
            state,
            result,
            event=EVENT_STRIKEOUT,
            result_hits=0,
            result_outs=1,
            result_runs=0,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=0,
            runners_retired=0,
        )

    def _dropped_third_strike(
        self,
        state: GameState,
        result: PlayResult,
        *,
        first_open_at_pitch: bool | None = None,
        outs_at_pitch: int | None = None,
    ) -> bool:
        """The dropped-third-strike rule: may the batter run to first?

        Only on a swinging strike three, and only when first base is OPEN or
        there are two outs (the official rule). Whether the ball got away is
        the DRAWN PITCH ROW's own fact: the row was a real uncaught third
        strike, with no roll and no formula. Without that fact it is an
        ordinary strikeout.

        The rule reads first base and the outs AT THE PITCH. When a steal on
        the same pitch has moved them, the caller passes the snapshot it took
        before the steal resolved. Otherwise this method reads the live state.

        Known gap: Rule 5.05(a)(2) also lets the batter run on a CALLED third
        strike that is not caught. This predicate reads swinging strikes only.
        The window holds two such rows, both in 2026 (of 296 uncaught third
        strikes in 2023-2026).
        """
        if result.pitch_outcome != "swinging_strike":
            return False
        first_base_open = (
            state.bases.first is None if first_open_at_pitch is None else first_open_at_pitch
        )
        outs = int(state.outs) if outs_at_pitch is None else int(outs_at_pitch)
        two_outs = outs >= OUTS_PER_INNING - 1
        if not (first_base_open or two_outs):
            return False
        return bool(self._last_pitch_got_away)

    def _force_on_reach(self, state: GameState, result: PlayResult) -> int:
        """Place the batter on first and force the runners behind him (the
        dropped-third-strike reach; the walk has its own copy in
        :meth:`_resolve_walk`). Returns the forced runs.

        Every forced runner is recorded in ``result.baserunner_advances`` as
        :meth:`_resolve_walk` records them. A run forced home from third is
        written as ``0`` ("scored"), because :meth:`_accumulate_pa` credits a
        runner's box run only from that entry (the R and HRR props read
        ``line.r``).
        """
        b = state.bases
        # Capture the pre-shift runner identities: the advances dict is keyed
        # on the runner who moved, not the bag.
        rid_1 = b.first
        rid_2 = b.second
        rid_3 = b.third
        forced_run = 0
        if rid_1 is not None:
            if rid_2 is not None:
                if rid_3 is not None:
                    forced_run = 1
                    result.baserunner_advances[int(rid_3)] = 0
                b.third = rid_2
                result.baserunner_advances[int(rid_2)] = 3
            b.second = rid_1
            result.baserunner_advances[int(rid_1)] = 2
        b.first = state.batter_id if state.batter_id is not None else b.first
        self._check_bases(b)
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        return forced_run
