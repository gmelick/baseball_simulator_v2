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
        """Resolve a ball-4 walk (§5.1 / §7): force the runners + score any
        forced run, routing the run/base-out delta through ``resolve_runs``.

        SIM-509: a HIT BY PITCH awards 1B with the same force mechanics, so it
        resolves here too — pass ``event=EVENT_HIT_BY_PITCH`` and the play
        carries its own canonical event (never counted as a BB).

        A walk forces the batter to 1B and pushes each runner ahead only when the
        bag behind him is occupied (a true force).  The number of forced runs is
        computed from the base state, then handed to :meth:`_commit_run_delta`.

        SIM-414: each forced advance is also recorded in
        ``result.baserunner_advances`` so the per-runner R credit in
        :meth:`_accumulate_pa` fires for a walk-forced run (previously a
        documented under-count that made the boxscore disagree with the
        linescore once shown together).
        """
        result.event = event
        result.pa_terminal = True
        b = state.bases
        # SIM-499: measure the base-out state BEFORE the force pushes anyone.
        # ``b`` is mutated in place below, so the ledger needs a copy, not a
        # reference — the reference is what made the old ledger read the
        # post-walk state and call it the pre-walk state.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        # Capture pre-walk runner identities before the base mutation overwrites
        # them — the advances dict is keyed on the runner who moved, not the bag.
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
        # Batter to 1B.
        #
        # SIM-500: assign UNCONDITIONALLY. The old form was
        #     b.first = state.batter_id if state.batter_id is not None else b.first
        # which kept the OLD occupant of 1B whenever no batter was set — the
        # count-machine path, which runs with no lineups. But the push above has
        # already moved that same runner to 2B, so the fallback left ONE runner id
        # standing on BOTH bags: a physically impossible state that then fed the
        # steal, situation and run-value logic. The SIM-500 guard caught it on its
        # first run ("runner 105 stands on both 1B and 2B").
        #
        # With no batter, 1B is simply vacant: the runner advanced and nobody
        # replaced him. That under-counts the walk by one baserunner on a path that
        # has no batter to count, which is the only self-consistent reading. Every
        # path that HAS a batter is unchanged.
        b.first = state.batter_id
        self._check_bases(b)
        if state.batter_id is not None:
            result.baserunner_advances[state.batter_id] = 1
        # Route the (possibly forced) run through resolve_runs — no inline runs.
        # The walk's after-state is deterministic: the batter reaches 1B (unless
        # no batter is set, the count-machine path), and any forced run is a body
        # that left the bases.  No runner is retired on a walk.
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
        """Issue an intentional walk signalled by the manager (SIM-323 §3 item 2).

        No pitch is thrown: the batter is awarded first base via the same
        forced-runner mechanics as a ball-4 walk, the run-resolution provenance is
        attached, and the PA is rolled over (batting order advances, count resets
        / half-inning rolls).  Returns the :class:`PlayResult` (``pitch_outcome``
        carries the sentinel ``"ball"`` since an IBB is recorded as four balls).

        SIM-515/516: the play carries its OWN canonical event
        (``intentional_walk`` — the SIM-509 HBP pattern), so the box still
        credits a BB/non-AB while the lane's IBB_PA pool band can see it. The
        first certifying lane proved the old ``walk`` label unreadable: the
        IBB numerator read zero and the surplus polluted BB_PA.
        """
        result = PlayResult(
            pitch_outcome="ball",
            is_contact=False,
            pa_terminal=True,
            event=EVENT_INTENTIONAL_WALK,
            # SIM-557: the walk names the pitcher who issues it and his side.
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
        """Resolve a strike-3 strikeout (§5.1) incl. the dropped-third-strike
        edge (§5.4).

        Ordinary K: route a one-out delta through ``resolve_runs``.  Dropped
        third strike (uncaught swinging strike-3 with **1B open OR two outs**):
        the batter may reach first — modelled here as a reach (no out, batter to
        1B) when the drawn pitch got away; the run/base-out delta still goes
        through ``resolve_runs``.

        SIM-484: the reach commits as a STRIKEOUT, because the play is one.
        Official scoring credits the pitcher's K and charges the batter's
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
        # SIM-517: read the D3K predicate at the pitch, before this method
        # moves anyone. A got-away strike three that CANNOT award first (1B
        # occupied, under two outs) still advances the runners: the ball
        # still got away. The advance commits its own delta. The K's
        # snapshots below then measure the post-advance state, so the two
        # commits chain like a steal and a K.
        d3k = self._dropped_third_strike(
            state,
            result,
            first_open_at_pitch=first_open_at_pitch,
            outs_at_pitch=outs_at_pitch,
        )
        # SIM-484: one mover per pitch, the non-terminal path's rule. When a
        # steal or a pickoff already resolved this pitch's baserunning, the
        # got-away moves nobody. Without the guard, a runner who stole second
        # on a called third strike that got away went on to third.
        #
        # SIM-554 (decision 4, owner 2026-10-01): the advance runs on the
        # dropped-third-strike reach too, BEFORE the batter takes first. Every
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
        # SIM-499: measure the base-out state before _force_on_reach can push
        # anyone.  ``_force_on_reach`` mutates ``state.bases`` in place.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        if d3k:
            # Uncaught K3: batter reaches 1B (no out recorded), pushing forced
            # runners exactly like a walk does.  resolve_runs scores any force.
            # SIM-554: after the got-away advance above first base is open, so
            # the push moves nobody unless a steal or a pickoff on this pitch
            # skipped the advance.
            self.running_game_tally.d3k_reaches += 1
            forced_run = self._force_on_reach(state, result)
            # SIM-484: the forced run scores on the wild pitch or the passed
            # ball, not on the batter, so it pays no RBI. The got-away advance
            # and the steal of home use the same marker.
            #
            # The run stays EARNED. The pool's got-away flag has no kind, so
            # the loop cannot tell a wild pitch (earned) from a passed ball
            # (unearned). About seven in ten real uncaught third strikes are
            # wild pitches (2023-2026). Splitting the charge needs a kind
            # column on the pitch pool and a rebuild (the plan's decision 4).
            result.steal_runs_scored += forced_run
            # SIM-484: the reach commits as a strikeout. It used to commit as
            # ``field_error``, so the box read a reach on an error: no K, an
            # RBI paid, a count on the lane's reach-on-error channel. The run
            # value comes from the two base-out states, never from the label,
            # so the run value does not change. A reach is not a hit
            # (``result_hits=0``).
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
        """The §5.4 dropped-third-strike predicate.

        The edge is *eligible* only on a swinging strike-3 when first base is
        OPEN or there are two outs (the official rule).  Whether the ball got
        away is the DRAWN PITCH ROW's own fact (SIM-517: the row was a real
        uncaught third strike — no roll, no formula).  Without that fact the
        edge does NOT fire (an ordinary K).

        SIM-484: the rule reads first base and the outs AT THE PITCH. When a
        steal on the same pitch has moved them, the caller passes the snapshot
        it took before the steal resolved. Otherwise this method reads the
        live state.

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
        """Place the batter on 1B and force runners behind him (the
        dropped-third-strike reach; the walk has its own copy in
        :meth:`_resolve_walk`).  Returns forced runs.

        SIM-421 fix: record every forced runner in ``result.baserunner_advances``
        the way :meth:`_resolve_walk` does. The forced run from 3B is written
        as ``0`` ("scored"), because :meth:`_accumulate_pa` credits a runner's
        box ``r`` only from that entry. Before this fix the reach wrote only
        the batter's entry, so a run forced home by a dropped third strike
        with the bases loaded reached the team score but never the runner's
        box line — and the R and HRR props read ``line.r``.
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
