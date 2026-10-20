"""The running game: the steal, the pickoff and the got-away advance.

In production the loop draws the pickoff before the pitch and the steal after
it, among real pitches of the same count and class. Without the pitch class
the steal and the pickoff are one draw before the pitch. A drawn steal is
staged on the machine and resolved on the same pitch.
"""

from __future__ import annotations

from typing import Any

from simulation.game_state import (
    GameState,
    PlayResult,
    Team,
)
from simulation.loop.base import LoopMixin
from simulation.loop.values import (
    _NEXT_BASE,
    STEAL_CAUGHT,
    STEAL_SAFE,
    StealResolution,
)


class RunningGameMixin(LoopMixin):
    """The state machine's running-game methods."""

    def _resolve_got_away_advance(self, state: GameState, result: PlayResult) -> None:
        """The drawn pitch got away: every runner advances one base, and a
        runner on third scores. The drawn row IS the play: its got-away fact
        came from a real passed ball or wild pitch, which is scored because a
        runner advanced. No random number, no out, no batter; with the bases
        empty nobody moves.

        The run accounting follows the steal of home, the other mid-pitch
        scoring path. The delta goes through :meth:`_commit_run_delta`. The
        run carries no RBI (Rule 9.04): ``steal_runs_scored`` is the
        no-RBI marker. On a NON-terminal pitch the runner's run and the
        pitcher's charge are credited here, because ``_accumulate_pa`` never
        runs; on a terminal pitch ``_accumulate_pa`` credits both. The run
        counts as earned: about 90% of real got-aways are wild pitches
        (earned, Rule 9.16), and the pool's flag does not tell the passed-ball
        minority apart.
        """
        b = state.bases
        if b.first is None and b.second is None and b.third is None:
            return
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        runs = 0
        scorer = pre_bases.third
        # Back to front so no move overwrites an occupied bag.
        if pre_bases.third is not None:
            self._move_runner(state, 3, 4)
            runs = 1
        if pre_bases.second is not None:
            self._move_runner(state, 2, 3)
            result.baserunner_advances[int(pre_bases.second)] = 3
        if pre_bases.first is not None:
            self._move_runner(state, 1, 2)
            result.baserunner_advances[int(pre_bases.first)] = 2
        self._check_bases(b)
        self._commit_run_delta(
            state,
            result,
            event="wild_pitch",
            result_hits=0,
            result_outs=0,
            result_runs=runs,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=runs,
            runners_retired=0,
        )
        if runs and scorer is not None:
            result.baserunner_advances[int(scorer)] = 0
            # No RBI on a got-away run (the same withholding a steal of home
            # uses — the field is the no-RBI marker, not steal-specific here).
            result.steal_runs_scored += runs
            # Credit the runner's box run ONLY on a NON-terminal pitch. On a
            # terminal pitch (a got-away strike three) ``_accumulate_pa`` runs
            # next and credits every runner whose ``baserunner_advances`` entry
            # reads 0; a credit here too would double the runner's run (the R
            # and HRR props read ``line.r``).
            if not result.pa_terminal:
                self._box_line(int(scorer)).r += 1
            if not result.pa_terminal and state.pitcher_id is not None:
                self._box_line(int(state.pitcher_id)).r_allowed += 1
                outs_lost = self._half_inning_error_outs_lost
                if int(state.outs) + outs_lost < 3:
                    self._box_line(int(state.pitcher_id)).er += 1

    def _resolve_steal_outcome(self, state: GameState, result: PlayResult) -> None:
        """Resolve the steal or pickoff staged on this pitch.

        On a SAFE steal the runner advances one base (scoring on a steal of
        home); on a CAUGHT stealing the runner is removed and ONE out is
        recorded. The out and any base-out delta go through
        :meth:`_commit_run_delta`. Records ``steal_attempted`` and
        ``steal_outcome`` on the ``PlayResult``. A no-op when nothing was
        staged.
        """
        steal = self._pending_steal
        # Consume and clear the staged attempt, so it never carries to the next pitch.
        self._pending_steal = None
        if steal is None or not steal.attempted:
            return
        if steal.pickoff:
            # A pickoff outcome is not a steal attempt: it never sets
            # steal_attempted or steal_outcome, and credits no stolen base and
            # no caught stealing except the picked-off runner who was breaking
            # (Rule 9.07(h)).
            self._resolve_pickoff(state, result, steal)
            return
        result.steal_attempted = True
        from_base = steal.from_base
        to_base = (
            steal.to_base
            if steal.to_base is not None
            else (_NEXT_BASE.get(from_base, 4) if from_base else 4)
        )
        rid = steal.runner_id
        # Measure the base-out state before _move_runner or _clear_base
        # changes it in place. ``moving`` is the runner actually standing on the
        # from-base: a steal staged from an empty bag moves no body, so the
        # transition facts below must say so rather than assume one.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        moving = {1: pre_bases.first, 2: pre_bases.second, 3: pre_bases.third}.get(from_base)
        if steal.safe:
            result.steal_outcome = STEAL_SAFE
            # Move the runner off the from-base onto the to-base (or home).
            self._move_runner(state, from_base, to_base)
            # Credit the stolen base to the runner HERE, the one steal-commit
            # site, not in :meth:`_accumulate_pa`: a steal on a NON-terminal
            # pitch never reaches the end of the plate appearance.
            if rid is not None:
                self._box_line(int(rid)).sb += 1
            if to_base >= 4:
                # Steal of home: one run scores, through the run ledger. The
                # runner leaves third, so he is a body that scored.
                self._commit_run_delta(
                    state,
                    result,
                    event="stolen_base_home",
                    result_hits=0,
                    result_outs=0,
                    result_runs=1,
                    pre_outs=pre_outs,
                    pre_bases=pre_bases,
                    batter_reached=False,
                    runners_scored=1 if moving is not None else 0,
                    runners_retired=0,
                )
                # Mark the run as a STEAL run, so the terminal-pitch credit
                # withholds the batter's RBI (Rule 9.04(a) pays no RBI on a
                # stolen base).
                result.steal_runs_scored += 1
                if rid is not None:
                    result.baserunner_advances[rid] = 0
                    # The runner scored on the steal of home.
                    self._box_line(int(rid)).r += 1
                    # Mark the runner as credited, so a terminal pitch's
                    # ``_accumulate_pa`` skips exactly him (and no other scorer
                    # on the same pitch).
                    result.box_run_credited.add(int(rid))
                # Charge the pitcher the run, only on a NON-terminal pitch. On a
                # terminal pitch ``_accumulate_pa`` adds this play's
                # ``runs_scored`` (this run included) to r_allowed.
                if not result.pa_terminal and state.pitcher_id is not None:
                    self._box_line(int(state.pitcher_id)).r_allowed += 1
                    # A stolen-base run is EARNED (Rule 9.16(a)), and the
                    # non-terminal path never reaches ``_accumulate_pa``'s
                    # earned-run credit. It is unearned only when an earlier
                    # error means the inning should already be over (the rule
                    # ``_accumulate_pa`` applies).
                    outs_lost = self._half_inning_error_outs_lost
                    if int(state.outs) + outs_lost < 3:
                        self._box_line(int(state.pitcher_id)).er += 1
            elif rid is not None:
                result.baserunner_advances[rid] = to_base
        else:
            result.steal_outcome = STEAL_CAUGHT
            # Charge the caught stealing to the runner.
            if rid is not None:
                self._box_line(int(rid)).cs += 1
            # Remove the caught runner from his base, then commit the out
            # through the run ledger. A caught stealing RETIRES a baserunner;
            # the call states the retired count, so the ledger never guesses it.
            self._clear_base(state, from_base)
            self._commit_run_delta(
                state,
                result,
                event="caught_stealing",
                result_hits=0,
                result_outs=1,
                result_runs=0,
                pre_outs=pre_outs,
                pre_bases=pre_bases,
                batter_reached=False,
                runners_scored=0,
                runners_retired=1 if moving is not None else 0,
            )
            # Write NO ``baserunner_advances`` entry for the caught runner. An
            # entry of 0 means "scored" to ``_accumulate_pa``, which credits
            # every scorer on the pitch. Every path keeps the convention: a
            # retired runner has no entry.

    def _resolve_pickoff(
        self, state: GameState, result: PlayResult, steal: StealResolution
    ) -> None:
        """Resolve a drawn pickoff outcome.

        Three outcomes, mirroring the labeled pool rows:
          * out, advancing (``safe=False, pickoff_advancing=True``) — a
            picked-off CAUGHT STEALING: the runner broke for the next base and
            was tagged. One out, and the runner is charged a caught stealing
            (Rule 9.07(h)); the caught-stealing band's reference counts it.
          * out, plain (``safe=False``) — tagged at his own base. One out,
            NO caught-stealing credit.
          * error (``safe=True``) — an errant throw; the runner advances one
            base. No steal credit and no ledger delta, the same shape as a
            safe steal to 2B/3B (occupancy moves; no run, no out).
        """
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        from_base = steal.from_base
        to_base = (
            steal.to_base
            if steal.to_base is not None
            else (_NEXT_BASE.get(from_base, 4) if from_base else 4)
        )
        rid = steal.runner_id
        moving = {1: pre_bases.first, 2: pre_bases.second, 3: pre_bases.third}.get(from_base)
        if steal.safe:
            result.pickoff_error = True
            self._move_runner(state, from_base, to_base)
            if rid is not None:
                result.baserunner_advances[rid] = to_base
            return
        result.pickoff_out = True
        if steal.pickoff_advancing and rid is not None:
            self._box_line(int(rid)).cs += 1
        self._clear_base(state, from_base)
        self._commit_run_delta(
            state,
            result,
            event="caught_stealing" if steal.pickoff_advancing else "pickoff",
            result_hits=0,
            result_outs=1,
            result_runs=0,
            pre_outs=pre_outs,
            pre_bases=pre_bases,
            batter_reached=False,
            runners_scored=0,
            runners_retired=1 if moving is not None else 0,
        )
        # Write NO ``baserunner_advances`` entry for the retired runner:
        # ``_accumulate_pa`` reads an entry of 0 as "scored". A retired runner
        # has no entry on every path.

    @staticmethod
    def _move_runner(state: GameState, from_base: int | None, to_base: int) -> None:
        """Move a runner from ``from_base`` to ``to_base`` (4 == home/off bases)."""
        b = state.bases
        rid = {1: b.first, 2: b.second, 3: b.third}.get(from_base)
        RunningGameMixin._clear_base(state, from_base)
        if to_base == 1:
            b.first = rid
        elif to_base == 2:
            b.second = rid
        elif to_base == 3:
            b.third = rid
        # to_base >= 4: scored — runner leaves the bases entirely.
        # No base-state check here: this is a low-level primitive with no ``self``,
        # and every caller validates once its whole move sequence is finished. A
        # check mid-sequence would fire on a legal intermediate state.

    @staticmethod
    def _clear_base(state: GameState, base: int | None) -> None:
        """Empty ``base`` (1-3); any other value does nothing."""
        b = state.bases
        if base == 1:
            b.first = None
        elif base == 2:
            b.second = None
        elif base == 3:
            b.third = None

    def _steal_order_active(self) -> bool:
        """True when the loop runs the running game on the pitch: the
        sampler's ``steal_pitch_class`` is on (``SIM_STEAL_PITCH_CLASS``) and
        its steal pool carries the pitch class (a bundle exported after
        migration 0031). Otherwise the steal and the pickoff are one draw
        before the pitch. Both reads must be exactly ``True``, so a duck-typed
        or mocked sampler keeps the single draw."""
        fp = self.full_pool_sampler
        if fp is None or getattr(fp, "steal_pitch_class", False) is not True:
            return False
        has = getattr(fp, "has_steal_classes", None)
        return callable(has) and has() is True

    @staticmethod
    def _lead_stealable_runner(state: GameState) -> tuple[Any, int, int] | None:
        """The lead stealable runner -> (runner id, his base, the target
        base), or None. On 1B with 2B open (target 2), or on 2B with 3B open
        (target 3, including 1B+2B — the lead runner drives)."""
        b = state.bases
        if b.first is not None and b.second is None:
            return b.first, 1, 2
        if b.second is not None and b.third is None:
            return b.second, 2, 3
        return None

    @staticmethod
    def _running_game_keys(state: GameState, runner_id: Any) -> tuple[str, str, str | None]:
        """The (runner, pitcher, catcher) keys the steal and pickoff draws
        read, as ``"{player id}:{season}"``; the catcher is the FIELDING
        side's, None when unknown."""
        season = int(getattr(state, "season", 2024) or 2024)
        pitcher = state.pitcher_id
        catcher = state.away_catcher_id if state.offense == Team.HOME else state.home_catcher_id
        return (
            f"{int(runner_id)}:{season}",
            f"{int(pitcher)}:{season}" if pitcher is not None else "",
            f"{int(catcher)}:{season}" if catcher is not None else None,
        )

    def _tally_pickoff(self, state: GameState, out: bool, error: bool) -> None:
        """Count one pickoff outcome at the count it was drawn at."""
        t = self.running_game_tally
        if out:
            t.pickoff_outs += 1
        elif error:
            t.pickoff_errors += 1
        key = (int(state.balls), int(state.strikes))
        t.pickoffs_by_count[key] = t.pickoffs_by_count.get(key, 0) + 1

    def _pickoff_before_the_pitch(self, state: GameState, result: PlayResult) -> bool:
        """Draw the pickoff for the lead stealable runner and resolve it
        now, before the pitch. True when a pickoff outcome
        happened — an out (a caught stealing when he was breaking) or a throw
        that got away and moved him up. One row of the count group answers;
        :meth:`_resolve_pickoff` commits it, with no steal credit either way."""
        fp = self.full_pool_sampler
        if fp is None or not fp.has_steal_pool():
            return False
        lead = self._lead_stealable_runner(state)
        if lead is None or lead[0] is None:
            return False
        runner_id, from_base, target = lead
        runner_key, pitcher_key, catcher_key = self._running_game_keys(state, runner_id)
        self.running_game_tally.pickoff_draws += 1
        drawn = fp.pickoff_draw(
            target,
            runner_key,
            pitcher_key,
            catcher_key,
            outs=int(state.outs),
            balls=int(state.balls),
            strikes=int(state.strikes),
            score_diff=int(state.score_diff),
        )
        if drawn is None:
            return False
        po_out, po_adv, po_err = drawn
        if not (po_out or po_err):
            return False
        self._tally_pickoff(state, bool(po_out), bool(po_err))
        self._resolve_pickoff(
            state,
            result,
            StealResolution(
                attempted=True,
                runner_id=runner_id,
                from_base=from_base,
                to_base=_NEXT_BASE.get(from_base, 4),
                safe=bool(po_err and not po_out),
                pickoff=True,
                pickoff_advancing=bool(po_adv),
            ),
        )
        return True

    def stage_steal(
        self,
        *,
        runner_id: int | None = None,
        from_base: int = 1,
        to_base: int | None = None,
        safe: bool,
    ) -> None:
        """Stage a steal attempt for :meth:`_resolve_steal_outcome`: the same
        pitch's when the steal draw stages it, the next pitch's when a test
        stages it between pitches.

        ``safe`` is the drawn row's own outcome (:meth:`_steal_opportunity_draw`
        passes it) or a test's explicit choice.
        """
        if to_base is None:
            to_base = _NEXT_BASE.get(from_base, 4)
        self._pending_steal = StealResolution(
            attempted=True,
            runner_id=runner_id,
            from_base=from_base,
            to_base=to_base,
            safe=bool(safe),
        )

    def _steal_aggression(self, state: GameState) -> float:
        """The batting side's manager weight on the steal draw's
        attempted rows — his measured ``steal_order_rate_per_1b_opp`` over the
        season's measured league mean, clamped to [0.05, 4.0]; exactly 1.0
        (neutral) with no manager wired, no profile or no league row."""
        if self.manager is None:
            return 1.0
        prof = (
            state.home_manager_profile if state.offense == Team.HOME else state.away_manager_profile
        ) or {}
        league = getattr(state, "manager_league_profile", None) or {}
        rate = prof.get("steal_order_rate_per_1b_opp")
        mean = league.get("steal_order_rate_per_1b_opp")
        if rate is None or mean is None or not (float(mean) > 0.0):
            return 1.0
        return float(min(max(float(rate) / float(mean), 0.05), 4.0))

    def _steal_opportunity_draw(self, state: GameState, *, pitch_class: str | None = None) -> None:
        """Stage a steal by drawing ONE row from the steal opportunity pool:
        a similarity-weighted draw, never a formula.

        ``pitch_class`` is the class of the pitch that came out. With it (the
        running game on the pitch) the draw runs AFTER the pitch among the real
        pitches of that count and that class, and stages a steal only (the
        pickoff was drawn before the pitch). Without it the draw runs before
        the pitch and its row may carry a pickoff outcome.

        Fires every pitch with a stealable lead runner: on 1B with 2B open
        (target 2), or on 2B with 3B open (target 3, including 1B+2B — the
        lead runner drives). The pool is per-pitch and its cell is the exact
        (outs, balls, strikes), so per-pitch and per-count attempt volume is
        the pool's own — no tuned constant. The drawn row's ``attempted`` flag
        decides whether the runner goes; its ``success`` flag decides safe or
        caught; :meth:`_resolve_steal_outcome` commits it. The manager's
        aggression is a WEIGHT on attempted rows, never a gate; with no
        manager wired it is neutral. A no-op when the sampler or the pool is
        absent.
        """
        fp = self.full_pool_sampler
        if fp is None or self._pending_steal is not None:
            return
        if not fp.has_steal_pool():
            return
        lead = self._lead_stealable_runner(state)
        if lead is None or lead[0] is None:
            return
        runner_id, from_base, target = lead
        runner_key, pitcher_key, catcher_key = self._running_game_keys(state, runner_id)
        # The manager's aggression: his measured steal-order rate over the
        # season's measured league mean, clamped, so even a manager who never
        # runs only DAMPS the draw (a weight, not a gate). There is no leverage
        # term: the draw's hard cell (outs, balls, strikes) and its score
        # kernel already carry how often real runners went at that leverage,
        # and a leverage multiplier cut ordinary-pitch attempts at second by
        # about 15% (the fit plan, SIM-476). A late-game residual, if one
        # shows, is a soft inning kernel on the draw, never a multiplier.
        aggression = self._steal_aggression(state)
        # The class keyword is passed only on the pitch, so a duck-typed
        # sampler without it keeps working.
        by_class: dict[str, Any] = {} if pitch_class is None else {"pitch_class": pitch_class}
        drawn = fp.steal_draw(
            target,
            runner_key,
            pitcher_key,
            catcher_key,
            outs=int(state.outs),
            balls=int(state.balls),
            strikes=int(state.strikes),
            score_diff=int(state.score_diff),
            aggression=aggression,
            **by_class,
        )
        if drawn is None:
            return
        if len(drawn) >= 5:
            attempted, success, po_out, po_adv, po_err = drawn[:5]
        else:
            # A duck-typed test sampler may return the (attempted, success) pair.
            (attempted, success), po_out, po_adv, po_err = drawn, False, False, False
        if po_out or po_err:
            # The drawn row carries a pickoff outcome, which pre-empts the
            # steal question for this pitch. It is staged like a steal and
            # resolved by _resolve_pickoff: an out retires the runner (a caught
            # stealing only when he was breaking), an errant throw moves him up.
            self._tally_pickoff(state, bool(po_out), bool(po_err))
            self._pending_steal = StealResolution(
                attempted=True,
                runner_id=runner_id,
                from_base=from_base,
                to_base=_NEXT_BASE.get(from_base, 4),
                safe=bool(po_err and not po_out),
                pickoff=True,
                pickoff_advancing=bool(po_adv),
            )
            return
        if not attempted:
            return
        self.stage_steal(runner_id=runner_id, from_base=from_base, safe=bool(success))
