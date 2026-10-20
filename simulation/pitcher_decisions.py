"""simulation.pitcher_decisions — the winning, losing and save pitchers of one game.

WHAT THIS IS
============
A **pure derivation** of the win, the loss and the save from one game's ordered
stream of :class:`~simulation.game_state.PlayResult` objects. It reads the plays
and their committed ``next_state`` (the post-play
:class:`~simulation.game_state.GameState`). It never changes a state, calls the
database or re-runs the loop. The rules are the standard MLB decision rules.

HOW THE MODULE READS THE PITCHER OF RECORD
==========================================
Each play result names its own pitcher (``PlayResult.pitcher_id``) and his side
(``PlayResult.fielding_team``). The module reads those two fields first, for the
pitcher of record and for the out count of the starter rule below.

A play without the two fields (a hand-built stream) reads the next state. Its
``pitcher_id`` is the fielding team's current pitcher, and its ``half`` names
the fielding team:

  * ``Half.TOP``    — the AWAY team bats, so the **HOME** pitcher is throwing.
  * ``Half.BOTTOM`` — the HOME team bats, so the **AWAY** pitcher is throwing.

So for a committed play state ``s`` the fielding side and its pitcher are::

    defending_team = Team.HOME if s.half == Half.TOP else Team.AWAY
    defending_pitcher_id = s.pitcher_id

The next state has one flaw: on the play that ends a half-inning, it already
shows the NEXT half. Read alone, it gives the last out of each half to the
other side's pitcher. That is why the play's own fields come first.

The module walks the stream. On each play it records the fielding side's
current pitcher as that side's pitcher of record. A pitching change shows up as
a new ``pitcher_id`` on a later play for the same side.

ATTRIBUTION RULES
=================
After each play the module tracks the running score (from ``next_state``) and
each side's pitcher of record. The game's winner is the team ahead after the
last play. A tie, or an empty or unfinished stream, has no decision.

* **Lead taken "for good".** The decisive play is the *earliest* play after
  which the eventual winner leads and never again falls to a tie or behind
  through the final play.

* **Winning pitcher.** The winning team's pitcher of record *when the decisive
  run scored*. The winner bats on the decisive play, so its pitcher of record
  is the one from its most recent fielding play. A side that has fielded no
  play yet has its first pitcher (its starter) on record: the away side can
  take the lead for good in the top of the 1st, before its starter throws a
  pitch. The module applies this rule only to a stream whose plays name their
  fielding side. A stream without the two fields gives no winning pitcher in
  that case.

  **MLB Rule 9.17(b), the starter exception.** If the candidate winner is the
  winning team's **starter** and he recorded fewer than 15 outs (5.0 innings),
  the win goes to the most effective reliever. The module reads "most
  effective" as the most outs recorded: the simulator carries no leverage or
  win-probability signal. Ties break by the order of first appearance. If no
  reliever has an out, the starter keeps the win (a guard for a thin stream).
  The save check compares the finisher with the reassigned winner, so the new
  winner cannot also get the save.

* **Losing pitcher.** The losing team's pitcher on the mound when the decisive
  run scored. That is the fielding pitcher of the decisive play, which is
  always in the winner's half of the inning.

* **Save.** The **final** pitcher of the **winning** team gets the save when
  ALL of these hold:
    1. he did **not** earn the win;
    2. he **finished** the game (the winning side's pitcher of record on the
       last play);
    3. he pitched in a **save situation**. The module uses the most common
       clause of MLB Rule 9.19: the lead was **3 runs or fewer** at some point
       while he pitched. It does NOT model the "3+ effective innings" clause.
       A blowout (a lead above 3 the whole time he pitched) gives no save.

  The finisher must differ from the winning pitcher, so a complete game, or a
  closer who also won, gives no save.

TIES / UNFINISHED GAMES / WALK-OFFS
===================================
* An empty stream, or a tied final state, gives a :class:`PitcherDecisions`
  with all three ids ``None`` and the final scores recorded.
* A walk-off needs no special case: the home team's permanent lead in its last
  at-bat is the decisive play like any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from simulation.game_state import GameState, Half, PlayResult, Team

#: The largest lead of a save situation (MLB Rule 9.19, clause (a)): three runs
#: or fewer. The module does not check the clause's one-inning minimum.
SAVE_LEAD_CEILING = 3

#: MLB Rule 9.17(b): a starter must pitch at least five innings (15 outs) to
#: earn the win. Short of that, the scorer gives the win to the most effective
#: reliever. The module reads that as the most outs recorded, with ties broken
#: by first appearance: the simulator carries no leverage or win-probability
#: signal.
STARTER_WIN_MIN_OUTS = 15

__all__ = [
    "PitcherDecisions",
    "decisions_from_plays",
    "SAVE_LEAD_CEILING",
    "STARTER_WIN_MIN_OUTS",
]


@dataclass(frozen=True)
class PitcherDecisions:
    """The win, the loss and the save of one game.

    All three pitcher ids are ``None`` when no decision can be made (a tie, an
    empty/unfinished stream).  ``home_score`` / ``away_score`` are the final
    committed scores for context.
    """

    winning_pitcher_id: int | None = None
    losing_pitcher_id: int | None = None
    save_pitcher_id: int | None = None
    home_score: int = 0
    away_score: int = 0


def _defending_team(state: GameState) -> Team:
    """The fielding team for ``state``: HOME pitches in the TOP, AWAY in BOTTOM."""
    return Team.HOME if state.half == Half.TOP else Team.AWAY


def _play_side_and_pitcher(r: PlayResult, st: GameState) -> tuple[Team, int | None, bool]:
    """The fielding side and the pitcher of one play, and whether the play named them.

    A play that names its own fielding side and pitcher is read first. The
    next state names the NEXT half's pitcher on the play that ends a
    half-inning, so it credits that play to the wrong pitcher. A play without
    the two fields reads the next state.
    """
    play_side = getattr(r, "fielding_team", None)
    if play_side is not None:
        return play_side, getattr(r, "pitcher_id", None), True
    return _defending_team(st), st.pitcher_id, False


def _side_pitcher_outs(
    results: Sequence[PlayResult], side: Team
) -> tuple[dict[int, int], list[int]]:
    """Each pitcher's outs for one side, and the order the pitchers first appear.

    The five-inning starter rule reads these counts for the winning side. The
    first pitcher in the order is the side's starter.
    Plays without a next state are skipped, as in ``decisions_from_plays``.
    """
    outs: dict[int, int] = {}
    order: list[int] = []
    for r in results:
        st = r.next_state
        if st is None:
            continue
        defending, pid, _own = _play_side_and_pitcher(r, st)
        if defending != side or pid is None:
            continue
        pid = int(pid)
        if pid not in outs:
            outs[pid] = 0
            order.append(pid)
        outs[pid] += int(getattr(r, "outs_recorded", 0) or 0)
    return outs, order


def decisions_from_plays(results: Sequence[PlayResult]) -> PitcherDecisions:
    """Derive (winning, losing, save) pitchers from an ordered play stream.

    ``results`` is one game's :class:`PlayResult` objects in chronological order;
    each must carry a committed ``next_state``.  Plays without a ``next_state``
    are skipped (they cannot move the score / pitcher of record).  Returns a
    :class:`PitcherDecisions`; see the module docstring for the exact rules.
    """
    # Pair each result with its state. The state gives the committed score; the
    # result names its pitcher and carries ``outs_recorded`` (the starter rule).
    paired: list[tuple[PlayResult, GameState]] = [
        (r, r.next_state) for r in results if r.next_state is not None
    ]
    if not paired:
        return PitcherDecisions()

    final = paired[-1][1]
    home_final, away_final = final.home_score, final.away_score

    # No decision on a tie (or a still-tied / unfinished stream).
    if home_final == away_final:
        return PitcherDecisions(home_score=home_final, away_score=away_final)

    winner: Team = Team.HOME if home_final > away_final else Team.AWAY
    loser: Team = Team.AWAY if winner == Team.HOME else Team.HOME

    # Each side's pitcher of record is the pitcher of that side's most recent
    # fielding play. Both start empty.
    home_poR: int | None = None  # pitcher of record (home)
    away_poR: int | None = None  # pitcher of record (away)

    # The outs of each of the WINNING team's pitchers, and the order of their
    # first appearance (the starter first; the tie-break of the starter rule).
    winner_pitcher_outs, winner_pitcher_order = _side_pitcher_outs([r for r, _ in paired], winner)
    winner_starter_id: int | None = winner_pitcher_order[0] if winner_pitcher_order else None

    # Build a per-state view: (winner_score, loser_score, winner_poR, loser_poR).
    # winner_poR/loser_poR are the pitchers of record *as of* that play, given
    # the substitutions seen so far.
    def _score_for(team: Team, st: GameState) -> int:
        return st.home_score if team == Team.HOME else st.away_score

    # Each side's first pitcher in the stream (its starter). A side has no
    # pitcher of record until it fields a play. The away side bats first, so
    # its lead can come before its starter fields. The decisive play then
    # reads the side's first pitcher (see below). The module uses this
    # fallback only when the stream names its own sides (``any_own``).
    first_pitcher: dict[Team, int] = {}
    any_own = False

    snapshots: list[tuple[int, int, int | None, int | None]] = []
    for r, st in paired:
        # The play's own side and pitcher, when it names them.
        defending, pid, own = _play_side_and_pitcher(r, st)
        any_own = any_own or own
        if defending == Team.HOME:
            home_poR = pid
        else:
            away_poR = pid
        if pid is not None and defending not in first_pitcher:
            first_pitcher[defending] = int(pid)
        winner_poR = home_poR if winner == Team.HOME else away_poR
        loser_poR = home_poR if loser == Team.HOME else away_poR
        snapshots.append(
            (
                _score_for(winner, st),
                _score_for(loser, st),
                winner_poR,
                loser_poR,
            )
        )

    # Find the decisive lead-taking play: the earliest index after which the
    # winner leads and never again falls to a tie-or-behind through the end.
    decisive_idx: int | None = None
    n = len(snapshots)
    for i in range(n):
        w_i, l_i, _, _ = snapshots[i]
        if w_i <= l_i:
            continue
        # winner leads after play i; does the lead hold for the rest of the game?
        if all(snapshots[j][0] > snapshots[j][1] for j in range(i, n)):
            decisive_idx = i
            break

    if decisive_idx is None:
        # A guard only: the winner leads after the last play, so the last
        # play always qualifies as the decisive one.
        return PitcherDecisions(home_score=home_final, away_score=away_final)

    _, _, win_poR_at_lead, lose_poR_at_lead = snapshots[decisive_idx]
    # A side that has fielded no play by the decisive play has its starter on
    # record. Example: the away side scores in the top of the 1st and never
    # trails. Its starter has thrown no pitch yet, but he is its pitcher of
    # record, so the win is his (or a reliever's, by the rule below). A stream
    # whose plays carry no fielding side skips this fallback (no winning
    # pitcher in that case).
    if any_own and win_poR_at_lead is None:
        win_poR_at_lead = first_pitcher.get(winner)
    if any_own and lose_poR_at_lead is None:
        lose_poR_at_lead = first_pitcher.get(loser)
    winning_pitcher_id = win_poR_at_lead
    losing_pitcher_id = lose_poR_at_lead

    # MLB Rule 9.17(b): when the winning team's STARTER is the pitcher of
    # record at the decisive play but recorded fewer than 15 outs, the win goes
    # to the most effective reliever. "Most effective" is the most outs; the
    # starter is excluded, and ties break by first appearance. If no reliever
    # has an out, the starter keeps the win (a guard for a thin stream).
    if (
        winning_pitcher_id is not None
        and winner_starter_id is not None
        and winning_pitcher_id == winner_starter_id
        and winner_pitcher_outs.get(winner_starter_id, 0) < STARTER_WIN_MIN_OUTS
    ):
        reliever_candidates = [pid for pid in winner_pitcher_order if pid != winner_starter_id]
        if reliever_candidates:
            # Pick the reliever with the most outs; tie -> earliest appearance.
            best = max(
                reliever_candidates,
                key=lambda pid: (winner_pitcher_outs.get(pid, 0), -winner_pitcher_order.index(pid)),
            )
            if winner_pitcher_outs.get(best, 0) > 0:
                winning_pitcher_id = best

    # ---- Save -----------------------------------------------------------------
    # The winning side's pitcher of record on the final play (the finisher).
    finisher_id = snapshots[-1][2]
    save_pitcher_id: int | None = None

    if (
        finisher_id is not None
        and finisher_id != winning_pitcher_id  # finisher did not get the win
    ):
        # A save needs the finisher to have pitched in a save situation: a lead
        # of <= SAVE_LEAD_CEILING at some point while THEY were the winner's
        # pitcher of record.  Scan the plays the finisher was on the mound for.
        in_save_situation = False
        finisher_pitched = False
        for w_s, l_s, w_poR, _ in snapshots:
            if w_poR != finisher_id:
                continue
            finisher_pitched = True
            lead = w_s - l_s
            # A lead of 3 or fewer is a save situation.
            if 0 < lead <= SAVE_LEAD_CEILING:
                in_save_situation = True
        if finisher_pitched and in_save_situation:
            save_pitcher_id = finisher_id

    return PitcherDecisions(
        winning_pitcher_id=winning_pitcher_id,
        losing_pitcher_id=losing_pitcher_id,
        save_pitcher_id=save_pitcher_id,
        home_score=home_final,
        away_score=away_final,
    )
