"""simulation.pitcher_decisions — W/L/Save attribution (SIM-364).

WHAT THIS IS
============
A **pure derivation** of the winning / losing / save pitcher decisions from an
ordered stream of :class:`~simulation.game_state.PlayResult` objects for a single
game.  It reads only the committed ``PlayResult.next_state`` (the post-play
:class:`~simulation.game_state.GameState`) — it never mutates state, calls the
DB, or re-runs the loop.  This is the Baseball-Analyst-owned codification of the
standard MLB decision rules (Phase 5, Sprint 4).

HOW THE PITCHER OF RECORD IS READ FROM GameState
================================================
``GameState`` does **not** carry separate ``home_pitcher_id`` / ``away_pitcher_id``
fields.  It carries a single ``pitcher_id`` (the *current* pitcher — the tile
pre-filter key, ``game_state.py`` line ~219) which is always the **fielding /
defending team's** pitcher, plus ``half``:

  * ``Half.TOP``    — the AWAY team bats, so the **HOME** pitcher is throwing.
  * ``Half.BOTTOM`` — the HOME team bats, so the **AWAY** pitcher is throwing.

So for any committed play state ``s`` the defending side and its pitcher are::

    defending_team = Team.HOME if s.half == Half.TOP else Team.AWAY
    defending_pitcher_id = s.pitcher_id

We walk the stream and, every time we see a play whose ``next_state`` shows a
given side fielding, we record that side's *current* pitcher of record.  A
substitution shows up naturally as a changed ``pitcher_id`` on a later play for
the same fielding side (``sim_loop`` sets ``state.pitcher_id = new_arm`` on a
bullpen move, line ~2120).

The next state has one flaw: on the play that ends a half-inning, it already
shows the NEXT half. Its ``half`` names the other fielding side, and its
``pitcher_id`` names the other side's pitcher. Read alone, it gives the last out
of each half to the wrong pitcher. So since SIM-557 each play result names its
own pitcher (``PlayResult.pitcher_id``) and his side
(``PlayResult.fielding_team``). The module reads those two fields first, for the
pitcher of record and for the out count of the starter rule below. A play
without the two fields (a hand-built stream) reads the next state, as before.

ATTRIBUTION RULES IMPLEMENTED
=============================
We track, after each play, the running score (from ``next_state``), which team
leads, and each side's current pitcher of record.  Let the game's final winner
be the team ahead after the last play (no decision on a tie or an unfinished /
empty stream).

* **Lead taken "for good".**  We find the *earliest* play after which the
  eventual winner held a lead it **never relinquished** through the final play
  (the winner's score strictly exceeds the loser's after that play and stays
  ahead — never falling to a tie-or-behind again).  This is the decisive
  lead-taking play.

* **Winning pitcher.**  The winning team's pitcher of record *on the mound at
  the moment the decisive lead-taking run scored*.  Because the lead-taking run
  is scored by the offense against the losing team, the winning team's pitcher
  of record at that instant is the one carried from its most recent fielding
  play (its half-inning). A side that has fielded no play yet has its first
  pitcher (its starter) on record: the away side can take the lead for good
  in the top of the 1st, before its starter throws a pitch (SIM-557). The
  module applies this rule only to a stream whose plays name their fielding
  side. A stream without the two fields keeps the old answer: no winning
  pitcher in that case.

  **SIM-414 — MLB Rule 9.17(b) starter exception.**  If the candidate winner is
  the winning team's **starter** and that starter did NOT record at least 15
  outs (5.0 IP), the win is reassigned to the most-effective reliever.  We
  approximate "most effective" by outs recorded (the simulator does not carry a
  leverage / WPA signal); ties break by first-appearance order; if no reliever
  has any outs we leave the starter (defensive fallback for under-instrumented
  streams).  The reassigned winner correctly invalidates a save for the same
  pitcher (the finisher-vs-winner check downstream sees the new id).

* **Losing pitcher.**  The losing team's pitcher who was on the mound when the
  decisive lead-taking run scored — i.e. the pitcher who *allowed* the run that
  gave the winner its permanent lead.  That is the defending pitcher of the
  decisive play (which is by construction a play in the winner's half of the
  inning, defended by the losing team).

* **Save.**  Awarded to the **final** pitcher of the **winning** team iff ALL of:
    1. that pitcher did **not** earn the win (no save for the winning pitcher);
    2. that pitcher **finished** the game (was the winning side's pitcher of
       record on the last play);
    3. the pitcher entered / protected a **save situation**, per the standard
       three-clause MLB Rule 9.19 heuristic — we use the most common clause:
       the lead was **3 runs or fewer** at some point while this finisher was
       pitching (equivalently, they entered with the tying run on deck).  We do
       NOT separately model the "pitched 3+ effective innings" third clause.
       No save in a blowout (lead always > 3 while the finisher pitched).

  The finisher must be a *different* pitcher than the winning pitcher; a complete
  game / closer-also-won yields no save.

TIES / UNFINISHED GAMES / WALK-OFFS
===================================
* Empty stream, or a final state that is tied, yields a :class:`PitcherDecisions`
  with all three ids ``None`` (no decision) but the final scores recorded.
* Walk-offs are handled transparently: the home team taking the permanent lead
  in its last at-bat is simply the decisive lead-taking play like any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from simulation.game_state import GameState, Half, PlayResult, Team

#: Standard MLB save-situation lead ceiling (Rule 9.19 clause (a)): a lead of
#: three runs or fewer with the finisher pitching at least one inning.
SAVE_LEAD_CEILING = 3

#: SIM-414: MLB Rule 9.17(b) — a starting pitcher must pitch at least 5 INNINGS
#: (15 outs) to be credited with the win.  When the starter does not reach 5
#: IP, the official scorer awards the win to the most-effective reliever; we
#: approximate "most effective" by outs recorded (with first-appearance order
#: breaking ties), since the simulator doesn't carry a leverage / WPA signal.
STARTER_WIN_MIN_OUTS = 15

__all__ = [
    "PitcherDecisions",
    "decisions_from_plays",
    "SAVE_LEAD_CEILING",
    "STARTER_WIN_MIN_OUTS",
]


@dataclass(frozen=True)
class PitcherDecisions:
    """The derived W/L/Save decision for a single game (SIM-364).

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

    SIM-557: a play that names its own fielding side and pitcher is read
    first. The next state names the NEXT half's pitcher on the play that ends
    a half-inning, so it credits that play to the wrong pitcher. A play
    without the two fields reads the next state, as before.
    """
    play_side = getattr(r, "fielding_team", None)
    if play_side is not None:
        return play_side, getattr(r, "pitcher_id", None), True
    return _defending_team(st), st.pitcher_id, False


def _side_pitcher_outs(
    results: Sequence[PlayResult], side: Team
) -> tuple[dict[int, int], list[int]]:
    """Each pitcher's outs for one side, and the order the pitchers first appear.

    The win module's five-inning starter rule (SIM-414) reads these counts for
    the winning side. The first pitcher in the order is the side's starter.
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
    # Iterate results+states together: states gives the committed score / poR,
    # results carries ``outs_recorded`` we need for SIM-414 (the starter-5-IP rule).
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

    # Per-play snapshots we need: running score + each side's pitcher of record.
    # The pitcher of record for a side is the defending pitcher_id from that
    # side's most recent fielding play.  Seed from whoever fields first.
    home_poR: int | None = None  # pitcher of record (home)
    away_poR: int | None = None  # pitcher of record (away)

    # SIM-414: per-pitcher outs recorded for the WINNING team's pitchers, plus
    # the order in which they first appeared (for tie-breaking the most-effective
    # reliever choice when the starter doesn't reach 5 IP).
    winner_pitcher_outs, winner_pitcher_order = _side_pitcher_outs([r for r, _ in paired], winner)
    winner_starter_id: int | None = winner_pitcher_order[0] if winner_pitcher_order else None

    # Build a per-state view: (winner_score, loser_score, winner_poR, loser_poR).
    # winner_poR/loser_poR are the pitchers of record *as of* that play, given
    # the substitutions seen so far.
    def _score_for(team: Team, st: GameState) -> int:
        return st.home_score if team == Team.HOME else st.away_score

    # SIM-557: each side's first pitcher in the stream (its starter). A side
    # has no pitcher of record until it fields a play. The away side bats
    # first, so its lead can come before its starter fields. The decisive
    # play then reads the side's first pitcher (see below). The module uses
    # this fallback only when the stream names its own sides (any_own), so a
    # stream without the two fields reads exactly as before SIM-557.
    first_pitcher: dict[Team, int] = {}
    any_own = False

    snapshots: list[tuple[int, int, int | None, int | None]] = []
    for r, st in paired:
        # SIM-557: the play's own side and pitcher, when it names them.
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
        # Winner is ahead at the end but never held a clean permanent lead in
        # this stream (shouldn't happen for a finished game, but be safe).
        return PitcherDecisions(home_score=home_final, away_score=away_final)

    _, _, win_poR_at_lead, lose_poR_at_lead = snapshots[decisive_idx]
    # SIM-557: a side that has fielded no play by the decisive play has its
    # starter on record. Example: the away side scores in the top of the 1st
    # and never trails. Its starter has thrown no pitch yet, but he is its
    # pitcher of record, so the win is his (or a reliever's, by the rule below).
    # A stream whose plays carry no fielding side skips this fallback: it reads
    # exactly as before SIM-557 (no winning pitcher in that case).
    if any_own and win_poR_at_lead is None:
        win_poR_at_lead = first_pitcher.get(winner)
    if any_own and lose_poR_at_lead is None:
        lose_poR_at_lead = first_pitcher.get(loser)
    winning_pitcher_id = win_poR_at_lead
    losing_pitcher_id = lose_poR_at_lead

    # SIM-414: MLB Rule 9.17(b) — if the winning team's STARTER is the pitcher
    # of record at the decisive play but did not pitch 5 innings (15 outs), the
    # official scorer awards the win to the most-effective reliever instead.
    # We approximate "most effective" by outs recorded; the starter is excluded,
    # ties break by first-appearance order.  If no reliever has any outs the
    # starter keeps the decision (defensive fallback — shouldn't happen in a
    # finished game with a sub-5-IP starter, but guard against an empty list).
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
            # Protecting a lead of 3 or fewer (tying run on deck heuristic).
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
