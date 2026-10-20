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
        """SIM-517 part D: the drawn pitch got away — every runner advances one
        base (scoring from third). The drawn row IS the play: its got-away
        fact came from a real passed ball / wild pitch, and those are scored
        precisely because a runner advanced. No rng, no outs, no batter
        involvement; a no-op with the bases empty (a got-away row drawn into
        an empty-bases live state moves nobody — nothing is scored there in
        real baseball either).

        Run accounting mirrors the steal-of-home commit (the one other
        mid-pitch scoring path): the delta routes through
        :meth:`_commit_run_delta`, the run carries no RBI (Rule 9.04 — the
        ``steal_runs_scored`` field is the no-RBI-on-this-pitch marker), and
        on a NON-terminal pitch the runner's R and the pitcher's charge are
        credited here because ``_accumulate_pa`` never runs. On a TERMINAL
        pitch ``_accumulate_pa`` credits both. The run counts EARNED: ~90% of real
        got-aways are wild pitches (earned, Rule 9.16); the pool flag merges
        the passed-ball minority, a box-stat nuance accepted knowingly.
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
            # SIM-421 fix: credit the runner's box run ONLY on a NON-terminal
            # pitch. On a terminal pitch (a got-away strike three)
            # ``_accumulate_pa`` runs next and credits every runner whose
            # ``baserunner_advances`` entry reads 0, so a credit here too
            # doubled the runner's R (the R and HRR props read ``line.r``).
            # The pitcher's charge below already carried this guard.
            if not result.pa_terminal:
                self._box_line(int(scorer)).r += 1
            if not result.pa_terminal and state.pitcher_id is not None:
                self._box_line(int(state.pitcher_id)).r_allowed += 1
                outs_lost = self._half_inning_error_outs_lost
                if int(state.outs) + outs_lost < 3:
                    self._box_line(int(state.pitcher_id)).er += 1

    def _resolve_steal_outcome(self, state: GameState, result: PlayResult) -> None:
        """Resolve a steal staged by the pre-pitch hook (§3 item 4 / step 7).

        On a SAFE steal the runner advances one base (scoring on a steal of home);
        on a CAUGHT stealing the runner is removed and ONE out is recorded — and
        the out + any base-out delta is routed through
        :meth:`_commit_run_delta` -> ``resolve_runs`` (never inline).  Records
        ``steal_attempted`` / ``steal_outcome`` on the ``PlayResult``.  A no-op
        when no steal was staged.
        """
        steal = self._pending_steal
        # Consume + clear the staged attempt so it never carries to the next pitch.
        self._pending_steal = None
        if steal is None or not steal.attempted:
            return
        if steal.pickoff:
            # SIM-507: a pickoff outcome is not a steal attempt — it never
            # sets steal_attempted / steal_outcome or the SB/CS-band-visible
            # box credits except the advancing-out CS (Rule 9.07(h)).
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
        # SIM-499: measure the base-out state before _move_runner / _clear_base
        # mutate it in place.  ``moving`` is the runner actually standing on the
        # from-base: a steal staged from an empty bag moves no body, so the
        # transition facts below must say so rather than assume one.
        pre_outs = int(state.outs)
        pre_bases = self._snapshot_bases(state)
        moving = {1: pre_bases.first, 2: pre_bases.second, 3: pre_bases.third}.get(from_base)
        if steal.safe:
            result.steal_outcome = STEAL_SAFE
            # Move the runner off the from-base onto the to-base (or home).
            self._move_runner(state, from_base, to_base)
            # SIM-365: credit the stolen base to the runner.  Steals are
            # accumulated HERE (the single steal-commit site) rather than in
            # :meth:`_accumulate_pa` so a steal on a NON-terminal pitch — which
            # never reaches end-of-PA — is still counted.
            if rid is not None:
                self._box_line(int(rid)).sb += 1
            if to_base >= 4:
                # Steal of home: one run scores -> route through resolve_runs.
                # The runner leaves 3B, so he is a body that scored.  The old
                # ledger read the already-cleared bases as the pre-state and
                # recorded +1.00 against a true +0.11.
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
                # SIM-483: mark the run as a STEAL run so the terminal-pitch
                # accumulator can withhold the batter's RBI credit (Rule
                # 9.04(a) pays no RBI on a stolen base).
                result.steal_runs_scored += 1
                if rid is not None:
                    result.baserunner_advances[rid] = 0
                    # SIM-365: the runner scored on the steal of home.
                    self._box_line(int(rid)).r += 1
                    # SIM-421: mark the runner as credited, so a terminal
                    # pitch's ``_accumulate_pa`` skips exactly him (and no
                    # other scorer on the same pitch).
                    result.box_run_credited.add(int(rid))
                # SIM-365: charge the pitcher the run.  Only on a NON-terminal
                # pitch — on a terminal pitch ``_accumulate_pa`` will add this
                # play's ``runs_scored`` (which includes this run) to r_allowed,
                # so crediting here too would double-count.
                if not result.pa_terminal and state.pitcher_id is not None:
                    self._box_line(int(state.pitcher_id)).r_allowed += 1
                    # SIM-483: a stolen-base run is EARNED (Rule 9.16(a)), and
                    # the non-terminal path never reaches ``_accumulate_pa``'s
                    # ER credit — without this line every non-terminal steal of
                    # home under-counted the pitcher's ER. Unearned only when
                    # an earlier error means the inning should already be over
                    # (the same rule ``_accumulate_pa`` applies).
                    outs_lost = self._half_inning_error_outs_lost
                    if int(state.outs) + outs_lost < 3:
                        self._box_line(int(state.pitcher_id)).er += 1
            elif rid is not None:
                result.baserunner_advances[rid] = to_base
        else:
            result.steal_outcome = STEAL_CAUGHT
            # SIM-426: charge the caught stealing to the runner.
            if rid is not None:
                self._box_line(int(rid)).cs += 1
            # Remove the caught runner from his base, then route the OUT delta
            # through resolve_runs (no inline out arithmetic).
            self._clear_base(state, from_base)
            # A caught stealing RETIRES a baserunner.  The deleted conservation
            # formula had no term for that, so it kept the runner on base and
            # recorded -0.24 against a true -0.62.  The caller states the retired
            # count, so the ledger no longer has to guess it.
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
            # SIM-421: write NO ``baserunner_advances`` entry for the caught
            # runner. An entry of 0 means "scored" to ``_accumulate_pa``, which
            # now credits every scorer on a steal pitch; the old ``rid: 0``
            # write would have credited the caught runner a run. The in-play
            # and pickoff paths use the same convention: a retired runner has
            # no entry.

    def _resolve_pickoff(
        self, state: GameState, result: PlayResult, steal: StealResolution
    ) -> None:
        """SIM-507: resolve a pickoff outcome staged by the pre-pitch draw.

        Three outcomes, mirroring the labeled pool rows:
          * out, advancing (``safe=False, pickoff_advancing=True``) — a
            picked-off CAUGHT STEALING: the runner broke for the next base and
            was tagged. One out, the runner is charged a CS (Rule 9.07(h)) —
            the CS band's MLB reference counts this class.
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
        # SIM-421 fix: write NO ``baserunner_advances`` entry for the retired
        # runner. ``_accumulate_pa`` reads an entry of 0 as "scored", and a
        # pickoff never sets ``steal_attempted``, so the old ``rid: 0`` write
        # credited the picked-off runner a run on a terminal pitch (the R and
        # HRR props read ``line.r``). The in-play path uses the same
        # convention: a retired runner has no entry.

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
        b = state.bases
        if base == 1:
            b.first = None
        elif base == 2:
            b.second = None
        elif base == 3:
            b.third = None

    def _steal_order_active(self) -> bool:
        """SIM-554: True when the loop runs the running game on the pitch —
        the sampler's ``steal_pitch_class`` is on (``SIM_STEAL_PITCH_CLASS``)
        and its steal pool carries the pitch class (a bundle exported after
        migration 0031). Otherwise the steal and the pickoff stay one pre-pitch
        draw (SIM-474). Both reads must be exactly ``True``, so a duck-typed
        or mocked sampler keeps the old order."""
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
        """SIM-554: count one pickoff outcome at the count it was drawn at."""
        t = self.running_game_tally
        if out:
            t.pickoff_outs += 1
        elif error:
            t.pickoff_errors += 1
        key = (int(state.balls), int(state.strikes))
        t.pickoffs_by_count[key] = t.pickoffs_by_count.get(key, 0) + 1

    def _pickoff_before_the_pitch(self, state: GameState, result: PlayResult) -> bool:
        """SIM-554: draw the pickoff for the lead stealable runner and
        resolve it now, before the pitch. True when a pickoff outcome
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
        """Stage a steal attempt for the NEXT pitch (the §3 decision).

        ``safe`` is the drawn row's own outcome (:meth:`_steal_opportunity_draw`
        passes it) or a test's explicit choice.  The attempt is consumed +
        resolved in step 7 of the next :meth:`step_pitch`.
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
        """SIM-427: the batting side's manager weight on the steal draw's
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
        """SIM-474: stage a steal by drawing ONE row from the SIM-468 steal
        OPPORTUNITY pool — a similarity-weighted draw, never a formula.

        SIM-554: ``pitch_class`` is the class of the pitch that came out. With
        it (the running game on the pitch) the draw runs AFTER the pitch among
        the real pitches of that count and that class, and stages a steal only
        (the pickoff was drawn before the pitch). Without it the draw runs
        before the pitch and its row may carry a pickoff outcome (SIM-507).

        Fires every pitch with a stealable lead runner: on 1B with 2B open
        (target 2), or on 2B with 3B open (target 3, including 1B+2B — the
        lead runner drives). The pool is per-pitch and its cell is the exact
        (outs, balls, strikes), so per-pitch and per-count attempt volume is
        the pool's own — no tuned constant. The drawn row's ``attempted`` flag
        decides whether the runner goes; its ``success`` flag decides safe or
        caught; :meth:`_resolve_steal_outcome` commits it (step 7). Manager
        aggression enters as a WEIGHT on attempted rows (never a gate); with
        no manager wired the weight is neutral. No-op when the sampler or the
        pool is absent.
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
        # Manager aggression: the manager's tendency over the league mean,
        # clamped so even a never-runs manager only DAMPS the draw (a weight,
        # not a gate — SIM-474). No manager -> neutral.
        #
        # SIM-476 step 0 (2026-08-30): the LEVERAGE factor
        # (0.5 + 0.5*min(LI/1.5, 2)) is DELETED. It double-counted leverage —
        # the draw's hard cell (outs, balls, strikes) and soft score kernel
        # already embody how often real runners went at that leverage — and
        # because it reads 1.0 only at LI >= 1.5, it suppressed ordinary-pitch
        # attempts ~0.73-0.87x, the measured 2B attempts-per-opportunity -15%
        # (arm A/B numbers in docs/audit/2026-08-28-sim476-fit-plan.md). With
        # the league-flat SIM-427 default profile the ratio below is exactly
        # 1.0, so production aggression is NEUTRAL until real per-manager
        # rates land — and those must be measured attempt-rate ratios, never
        # a leverage formula. If localization ever shows a late-game residual,
        # the fix is an inning/late soft KERNEL on the steal draw (data
        # conditioning), not a reinstated multiplier.
        # SIM-427: the batting manager's MEASURED steal-order rate over the
        # season's MEASURED league mean (``derived.league_averages``' manager
        # row) — a ratio near 1, clamped; no leverage term (SIM-476 step 0).
        # Without a profile or a league mean on the state it is exactly 1.0.
        aggression = self._steal_aggression(state)
        # SIM-554: the class keyword is passed only in the new order, so a
        # duck-typed sampler with the SIM-474 signature keeps working.
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
            # A duck-typed test sampler may still return the pre-SIM-507 pair.
            (attempted, success), po_out, po_adv, po_err = drawn, False, False, False
        if po_out or po_err:
            # SIM-507: the drawn row carries a pickoff outcome — it pre-empts
            # the steal question for this pitch. Staged like a steal (the
            # pre-pitch decision resolves in step 7) and resolved by
            # _resolve_pickoff: an out retires the runner (a CS only when he
            # was advancing), an errant throw advances him.
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
