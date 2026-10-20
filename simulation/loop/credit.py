"""The box-score credit of a completed plate appearance.

:meth:`CreditMixin._accumulate_pa` credits the batter and the pitcher at the
end of every plate appearance. Two credits happen elsewhere, when the play
happens: a pitcher's outs (``_record_outs`` in the run ledger) and the steal
credits (:mod:`simulation.loop.running_game`).
"""

from __future__ import annotations

from simulation.constants import resolve_event_to_canonical
from simulation.game_state import (
    GameState,
    PlayResult,
    Team,
)
from simulation.loop.base import LoopMixin
from simulation.loop.boxscore import BoxScore, PlayerStatLine


class CreditMixin(LoopMixin):
    """The state machine's box-score methods."""

    #: Canonical PA outcomes that are NOT at-bats (PA but not AB): walks, HBP,
    #: and the two productive outs (sac fly / sac bunt).  Everything else that is
    #: a terminal PA counts as an AB.
    _NON_AB_CANONICAL: frozenset[str] = frozenset(
        {
            "walk",
            "intentional_walk",
            "hit_by_pitch",
            "sacrifice_fly",
            "sacrifice_hit",
        }
    )
    #: Canonical PA outcomes that are base hits.
    _HIT_CANONICAL: frozenset[str] = frozenset(
        {
            "single",
            "double",
            "triple",
            "home_run",
        }
    )
    #: Canonical PA outcomes that count as a walk against the pitcher.
    _BB_CANONICAL: frozenset[str] = frozenset({"walk", "intentional_walk"})

    @staticmethod
    def _current_batter_id(state: GameState) -> int | None:
        """The id of the batter who just completed THIS PA (SIM-328 attribution).

        Reads the OFFENSE's lineup at its current slot pointer — the authoritative
        "batter currently up" — when a lineup is wired.  This is preferred over
        the loose ``state.batter_id`` because the loop leaves ``batter_id`` stale
        at the prior (now-fielding) team's hitter for the FIRST PA of each new
        half (``_advance_batting_order`` only repoints it at end-of-PA); reading
        the offense's slot here credits the correct hitter from the half's first
        PA without changing the locked ``batter_id`` / half-flip contract
        (SIM-316).  Falls back to ``state.batter_id`` when no lineup is wired (the
        count-machine-only tests).
        """
        if state.offense == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
        if lineup:
            return lineup[slot % len(lineup)]
        return state.batter_id

    def _box_line(self, player_id: int) -> PlayerStatLine:
        """Lazily create the boxscore and return a player's line (SIM-365).

        Mirrors the ``if self.boxscore is None`` guard inside
        :meth:`_accumulate_pa`, so steal accumulation (which happens in
        :meth:`_resolve_steal_outcome`, possibly on a non-terminal pitch that
        never reaches end-of-PA) shares the same per-game store.
        """
        if self.boxscore is None:
            self.boxscore = BoxScore()
        return self.boxscore.line(int(player_id))

    def _accumulate_pa(self, state: GameState, result: PlayResult) -> None:
        """Attribute one completed terminal PA to the batter + pitcher (SIM-328).

        Called from :meth:`_end_of_pa` BEFORE the batting order advances, so
        ``state.batter_id`` (offense) and ``state.pitcher_id`` (defense) still
        name the two players who just resolved the PA — which is exactly the
        attribution AC-2 requires (and the half-inning flip switches them
        automatically, since the ids are read off the live ``GameState``).

        Classification uses the resolved CANONICAL event
        (``result.canonical_event``, set by :meth:`_commit_run_delta` via
        :func:`run_resolution.resolve_runs`); when that is absent (e.g. a
        count-machine-only in-play terminal with no sampler) we fall back to
        :func:`resolve_event_to_canonical` on the raw ``result.event``.  A PA with
        no resolvable event (pure count-machine in-play hand-off, event is None)
        is NOT credited — there is no scored outcome to attribute.

        BATTER:
          * AB  — every terminal PA EXCEPT a walk / IBB / HBP / sac fly / sac
            bunt (the standard PA-minus-(BB+HBP+SF+SH) at-bat definition).
          * H   — single / double / triple / home_run.
          * HR  — home_run.
          * SO  — a strikeout PA (canonical 'strikeout'), the dropped-third-
            strike reach included (SIM-484); on ``so``, never on ``k``.
          * RBI — the integer runs that physically scored on this play
            (``result.runs_scored``), MINUS runs we treat as unearned-by-error
            (see the earned/unearned split below) — RBI is not credited on an
            error-driven run, mirroring the ER simplification.  A solo HR drives
            in 1 (the batter himself scores), a 3-run HR drives in 3.  No RBI
            either on a run marked in ``result.steal_runs_scored``: a steal of
            home, a got-away advance, a run a dropped-third-strike reach forces
            home. Rule 9.04(a) pays none on a steal, a wild pitch or a passed
            ball.

        PITCHER:
          * IP  — written by :meth:`_record_outs` when the out is recorded
            (SIM-557); not here.
          * BF  — one batter faced on every completed plate appearance, the
            intentional walk included (SIM-557).
          * K   — a strikeout PA (canonical 'strikeout'); credited even on a
            dropped-third-strike reach (no out) per the scoring rule.
          * BB  — a walk / IBB.
          * ER  — earned runs charged: the runs that scored on this play, EXCEPT
            when the play carried an error (``result.is_error``), in which case
            we treat the run(s) as UNEARNED and do not charge the pitcher.

        EARNED/UNEARNED — INNING-RECONSTRUCTION (SIM-414): full ER scoring per
        MLB Rule 9.16(b) treats as UNEARNED any run that would not have scored
        had the defense played errorlessly — including runs that score AFTER the
        inning "should have ended".  We approximate this by tracking
        ``_half_inning_error_outs_lost`` (incremented when an error play
        recorded no outs — the canonical reach-on-error) and flagging the run as
        unearned if ``outs_before_this_play + error_outs_lost >= 3``.  Combined
        with the per-play ``is_error`` flag, this captures the two dominant
        unearned-run patterns: (1) runs that score on an error play, (2) runs
        that score in an extended inning.  The rarer "runner reached on an
        error and later scored" case requires per-runner provenance and is
        deferred (a future ticket can add it).  RBI tracks ``is_error`` only
        (per Rule 9.04: a clean RBI in an extended inning still counts).
        """
        if self.boxscore is None:
            self.boxscore = BoxScore()
        box = self.boxscore

        canonical = result.canonical_event
        if canonical is None:
            canonical = resolve_event_to_canonical(result.event)

        runs = int(result.runs_scored or 0)
        outs = int(result.outs_recorded or 0)
        is_error = bool(result.is_error)

        # SIM-414: inning-reconstruction unearned-run flag.  ``state.outs`` has
        # already been incremented by this play, so the count BEFORE the play
        # is ``state.outs - outs``.  If errors earlier in the half-inning
        # prevented an out, the effective out total may already be >= 3 at the
        # start of this play — any run that scores here is unearned even when
        # this play itself is clean.
        outs_before_play = max(0, int(state.outs) - outs)
        effective_outs_before_play = outs_before_play + self._half_inning_error_outs_lost
        inning_should_have_ended = effective_outs_before_play >= 3
        run_is_unearned = is_error or inning_should_have_ended

        batter_id = self._current_batter_id(state)

        # ---- batter (offense) ----
        if batter_id is not None:
            bat = box.line(int(batter_id))
            if canonical is not None and canonical not in self._NON_AB_CANONICAL:
                bat.ab += 1
            if canonical in self._HIT_CANONICAL:
                bat.h += 1
                if canonical == "home_run":
                    bat.hr += 1
                # SIM-365: doubles / triples are a subset of ``h`` (so TB can be
                # computed exactly downstream); singles need no field (h - b2 - b3
                # - hr).  ADDITIVE on top of the existing ``h`` credit above.
                elif canonical == "double":
                    bat.b2 += 1
                elif canonical == "triple":
                    bat.b3 += 1
            # SIM-484: the batter's own strikeout, on every strikeout, the
            # dropped-third-strike reach included (Rule 9.15). It goes to
            # ``so``. ``k`` is the pitcher's, and the readers that tell a
            # pitcher by ``k`` would count this batter as one.
            if canonical == "strikeout":
                bat.so += 1
            # RBI: runs the batter drove in (not credited on an error-driven
            # run). SIM-483: a run that scored ON A STEAL (a terminal-pitch
            # steal of home folds its run into ``runs``) earns the batter NO
            # RBI — MLB Rule 9.04(a) pays none on a stolen base. The got-away
            # runs and the dropped-third-strike forced run share the marker.
            rbi_runs = max(0, int(runs) - int(result.steal_runs_scored))
            if rbi_runs and not is_error:
                bat.rbi += rbi_runs

        # ---- runs scored (SIM-365): credit each runner who crossed home on this
        # play.  ``baserunner_advances`` records ``end_base == 0`` for a runner who
        # SCORED (set in :meth:`_resolve_in_play_transition` for a runner or the
        # batter who crossed home, and SIM-414 for the runner on 3B forced home
        # by a bases-loaded walk).  A retired runner has NO entry: the in-play
        # path, :meth:`_resolve_pickoff` (SIM-507) and the caught-stealing
        # branch of :meth:`_resolve_steal_outcome` all write none.
        # SIM-421: a steal of home credits its runner in
        # :meth:`_resolve_steal_outcome` on every pitch (a NON-terminal steal
        # never reaches this method) and records him in ``box_run_credited``.
        # Skip exactly that runner here.  The old guard skipped the whole loop
        # on any steal pitch, so a safe steal to 2B plus a home run on the same
        # pitch credited nobody a run — the R and HRR props read ``line.r``.
        # A got-away run on a terminal pitch is credited HERE only
        # (:meth:`_resolve_got_away_advance` credits the runner on a
        # non-terminal pitch only).
        for rid, end_base in result.baserunner_advances.items():
            if int(end_base) == 0 and int(rid) not in result.box_run_credited:
                box.line(int(rid)).r += 1

        # ---- pitcher (defense) ----
        if state.pitcher_id is not None:
            pit = box.line(int(state.pitcher_id))
            # SIM-557: one batter faced per completed plate appearance. The
            # outs are not written here: _record_outs credits each out when
            # it is recorded.
            # The batters faced follow the loop's own count (state.pitcher_bf,
            # bumped in _end_of_pa). A known limit: in the single pre-pitch
            # steal draw (SIM_STEAL_PITCH_CLASS=0, not production), a pickoff
            # or caught stealing can make the third out before the pitch's
            # result, and the loop still ends this plate appearance. The
            # pitcher then gets a batter faced and the batter an at-bat. Both
            # the batter's line and state.pitcher_bf had this flaw before the
            # batters faced existed; its fix changes the batter's line too, so
            # it needs its own ticket.
            pit.bf += 1
            if canonical == "strikeout":
                pit.k += 1
            if canonical in self._BB_CANONICAL:
                pit.bb += 1
            # SIM-365: a base hit charged to the pitcher.
            if canonical in self._HIT_CANONICAL:
                pit.h_allowed += 1
            # ER: scored runs charged to the pitcher unless the run is unearned —
            # SIM-414: now also excludes runs that score after the inning "should
            # have ended" (an earlier error prevented an out).
            if runs and not run_is_unearned:
                pit.er += runs
            # SIM-365: runs allowed (R) include UNEARNED runs too, so unlike ER
            # there is no ``is_error`` exclusion — R >= ER by construction.  A
            # steal-of-home on a NON-terminal pitch is charged in
            # :meth:`_resolve_steal_outcome` (it never reaches this method);
            # a terminal-pitch steal-of-home's run is already in ``runs`` here.
            if runs:
                pit.r_allowed += runs

        # SIM-414: a reach-on-error (the canonical error play — outs_recorded == 0
        # with is_error == True) counts as one missed out; subsequent runs in the
        # same half-inning will be unearned once effective_outs reaches 3.
        if is_error and outs == 0:
            self._half_inning_error_outs_lost += 1
