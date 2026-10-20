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
        """The id of the batter who just completed THIS plate appearance.

        It reads the OFFENSE's lineup at its current slot when a lineup is
        wired. ``state.batter_id`` is not used: the loop leaves it at the
        previous half's last hitter for the first plate appearance of each new
        half, because ``_advance_batting_order`` moves it only at the end of a
        plate appearance. Falls back to ``state.batter_id`` with no lineup
        (the count-machine tests).
        """
        if state.offense == Team.HOME:
            lineup, slot = state.home_lineup, state.home_lineup_slot
        else:
            lineup, slot = state.away_lineup, state.away_lineup_slot
        if lineup:
            return lineup[slot % len(lineup)]
        return state.batter_id

    def _box_line(self, player_id: int) -> PlayerStatLine:
        """A player's line in the game's box score, which it creates on first
        use. Every credit writes through it, so a steal on a pitch that never
        ends a plate appearance shares the same per-game store."""
        if self.boxscore is None:
            self.boxscore = BoxScore()
        return self.boxscore.line(int(player_id))

    def _accumulate_pa(self, state: GameState, result: PlayResult) -> None:
        """Credit one completed plate appearance to the batter and the pitcher.

        :meth:`_end_of_pa` calls it BEFORE the batting order advances, so
        ``state.pitcher_id`` still names the pitcher who just finished it, and
        :meth:`_current_batter_id` the batter.

        The classification uses the play's canonical event
        (``result.canonical_event``, set by the run ledger). With none (a
        count-machine in-play pitch with no sampler) it resolves
        ``result.event``; a plate appearance with no resolvable event gets no
        credit.

        BATTER:
          * AB  — every plate appearance EXCEPT a walk, an intentional walk, a
            hit by pitch, a sacrifice fly or a sacrifice bunt.
          * H   — single / double / triple / home_run.
          * HR  — home_run.
          * SO  — a strikeout, the dropped-third-strike reach included; on
            ``so``, never on ``k``.
          * RBI — the runs that scored on this play (``result.runs_scored``),
            none on an error play. No RBI either on a run marked in
            ``result.steal_runs_scored``: a steal of home, a got-away advance,
            a run a dropped-third-strike reach forces home. Rule 9.04(a) pays
            none on a steal, a wild pitch or a passed ball. A solo home run
            drives in 1, a three-run home run 3.

        PITCHER:
          * IP  — written by :meth:`_record_outs` when the out is recorded; not
            here.
          * BF  — one batter faced on every completed plate appearance, the
            intentional walk included.
          * K   — a strikeout, credited even on a dropped-third-strike reach.
          * BB  — a walk or an intentional walk.
          * H   — a base hit allowed.
          * ER  — the runs that scored on this play, unless they are unearned.
          * R   — every run that scored on this play, earned or not.

        EARNED AND UNEARNED: Rule 9.16(b) makes a run UNEARNED when it would
        not have scored had the defense played without errors, which includes
        a run scored after the inning "should have ended". The approximation:
        a run on an error play is unearned, and so is any run once the outs
        before the play plus ``_half_inning_error_outs_lost`` (the reaches on
        an error this half) reach three. A runner who reached on an error and
        scored later is not traced; that needs per-runner provenance. The RBI
        reads the error play only: a clean RBI in an extended inning counts
        (Rule 9.04).
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

        # The unearned-run flag of the extended inning. ``state.outs`` already
        # holds this play's outs, so the count BEFORE the play is
        # ``state.outs - outs``. When errors earlier in the half kept the
        # defense from an out, the effective count may reach 3 before this
        # play, and any run here is unearned even on a clean play.
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
                # Doubles and triples are a subset of ``h``, so total bases
                # are exact downstream; singles need no field (h - b2 - b3 - hr).
                elif canonical == "double":
                    bat.b2 += 1
                elif canonical == "triple":
                    bat.b3 += 1
            # The batter's own strikeout, on every strikeout, the
            # dropped-third-strike reach included (Rule 9.15). It goes to
            # ``so``. ``k`` is the pitcher's, and the readers that tell a
            # pitcher by ``k`` would count this batter as one.
            if canonical == "strikeout":
                bat.so += 1
            # RBI: the runs the batter drove in, none on an error play. A run
            # that scored on a steal (a terminal-pitch steal of home folds its
            # run into ``runs``) pays no RBI (Rule 9.04(a)); the got-away runs
            # and the dropped-third-strike forced run share the marker.
            rbi_runs = max(0, int(runs) - int(result.steal_runs_scored))
            if rbi_runs and not is_error:
                bat.rbi += rbi_runs

        # ---- runs scored: each runner who crossed home on this play ----
        # ``baserunner_advances`` records ``end_base == 0`` for a runner or a
        # batter who SCORED; a retired runner has NO entry on every path. A
        # steal of home credits its runner in :meth:`_resolve_steal_outcome`
        # on every pitch and records him in ``box_run_credited``; skip exactly
        # him, so another scorer on the same pitch is still credited. A
        # got-away run on a terminal pitch is credited here only
        # (:meth:`_resolve_got_away_advance` credits it on a non-terminal
        # pitch only).
        for rid, end_base in result.baserunner_advances.items():
            if int(end_base) == 0 and int(rid) not in result.box_run_credited:
                box.line(int(rid)).r += 1

        # ---- pitcher (defense) ----
        if state.pitcher_id is not None:
            pit = box.line(int(state.pitcher_id))
            # One batter faced per completed plate appearance, following the
            # loop's own count (``state.pitcher_bf``, bumped in _end_of_pa).
            # The outs are not written here: _record_outs credits each out
            # when it is recorded.
            #
            # A known limit of the single pre-pitch steal draw
            # (SIM_STEAL_PITCH_CLASS=0, not production): a pickoff or a caught
            # stealing can make the third out before the pitch's result, and
            # the loop still ends this plate appearance, so the pitcher gets a
            # batter faced and the batter an at-bat. A fix changes the batter's
            # line too, so it needs its own ticket.
            pit.bf += 1
            if canonical == "strikeout":
                pit.k += 1
            if canonical in self._BB_CANONICAL:
                pit.bb += 1
            if canonical in self._HIT_CANONICAL:
                pit.h_allowed += 1
            # ER: the runs scored, unless unearned (an error play, or an
            # inning that should already have ended).
            if runs and not run_is_unearned:
                pit.er += runs
            # R: every run, earned or not, so R >= ER. A steal of home on a
            # NON-terminal pitch is charged in :meth:`_resolve_steal_outcome`
            # (it never reaches here); a terminal-pitch steal of home's run is
            # already in ``runs``.
            if runs:
                pit.r_allowed += runs

        # A reach on an error (an error play that recorded no out) counts as
        # one missed out: later runs this half are unearned once the effective
        # outs reach three.
        if is_error and outs == 0:
            self._half_inning_error_outs_lost += 1
