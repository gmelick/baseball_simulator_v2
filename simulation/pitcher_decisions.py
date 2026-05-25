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
  play (its half-inning).  (Simplified-standard attribution: official scoring
  has discretion for a starter who didn't go 5 IP, but we apply the common
  "pitcher of record when the winning team took the permanent lead" rule and do
  not model the 5-IP starter exception.)

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

__all__ = [
    "PitcherDecisions",
    "decisions_from_plays",
    "SAVE_LEAD_CEILING",
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


def decisions_from_plays(results: Sequence[PlayResult]) -> PitcherDecisions:
    """Derive (winning, losing, save) pitchers from an ordered play stream.

    ``results`` is one game's :class:`PlayResult` objects in chronological order;
    each must carry a committed ``next_state``.  Plays without a ``next_state``
    are skipped (they cannot move the score / pitcher of record).  Returns a
    :class:`PitcherDecisions`; see the module docstring for the exact rules.
    """
    # Collect committed states in order.
    states: list[GameState] = [r.next_state for r in results if r.next_state is not None]
    if not states:
        return PitcherDecisions()

    final = states[-1]
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

    # Build a per-state view: (winner_score, loser_score, winner_poR, loser_poR).
    # winner_poR/loser_poR are the pitchers of record *as of* that play, given
    # the substitutions seen so far.
    def _score_for(team: Team, st: GameState) -> int:
        return st.home_score if team == Team.HOME else st.away_score

    snapshots: list[tuple[int, int, int | None, int | None]] = []
    for st in states:
        defending = _defending_team(st)
        if defending == Team.HOME:
            home_poR = st.pitcher_id
        else:
            away_poR = st.pitcher_id
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
    winning_pitcher_id = win_poR_at_lead
    losing_pitcher_id = lose_poR_at_lead

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
