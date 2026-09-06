"""
linescore.py
============
SIM-362 -- per-inning linescore + team R/H/E derivation (Phase 5, Sprint 4).

The loop output / box-score surface (and the Phase-6 frontend scoreboard) needs
the classic baseball **linescore**: the per-inning runs grid plus each team's
Runs / Hits / Errors totals::

         1 2 3 4 5 6 7 8 9   R  H  E
    AWAY 0 1 0 0 2 0 0 0 0   3  7  1
    HOME 1 0 0 0 0 1 0 0 x   2  6  0

This module owns that contract + the *pure* derivation from one game's ordered
``PlayResult`` stream (the recorder's per-pitch output).  It is a SPEC/dataclass
+ pure-function deliverable in the style of ``simulation.snapshots`` -- NOT UI,
NOT a live API endpoint, NOT a loop edit.  Another agent wires it into the API.

DESIGN
------
  * Additive + pure.  ``linescore_from_plays`` reads existing ``PlayResult`` /
    ``GameState`` objects only.  No DB, no FAISS, no live API, no rng, no
    mutation.  ``sim_loop.py`` is NOT touched (import only).
  * Frozen, slotted dataclasses, matching ``simulation.snapshots`` style.

INNING / HALF -> COLUMN MAPPING
-------------------------------
Each ``PlayResult`` carries ``next_state`` -- the committed ``GameState`` AFTER
the pitch -- which we read for ``(inning, half)``.  A play's runs/hits/errors are
attributed to the inning + half of the *state it produced* (i.e. the half the
play occurred in):

  * ``Half.TOP``    -> the **away** team is batting -> away column.
  * ``Half.BOTTOM`` -> the **home** team is batting -> home column.

ERROR CHARGING
--------------
An error is charged to the **fielding** (defensive) side, NOT the batting side:

  * an error on a TOP-half play (away batting) is charged to the **home** team's
    defense;
  * an error on a BOTTOM-half play (home batting) is charged to the **away**
    team's defense.

IN-PROGRESS / WALK-OFF HANDLING
-------------------------------
The home team does not bat in the bottom of the final inning when it already
leads after the top half, and a walk-off can end the game mid-inning.  An inning
whose bottom half was never played is represented with ``home`` as ``None`` (the
classic "x" on a scoreboard) rather than ``0`` -- ``0`` would falsely claim the
home team batted and was held scoreless.  ``InningLine.home_played`` /
``away_played`` report which halves actually occurred.  Hits and errors that
land in an unplayed half simply never appear in the stream, so they are zero by
construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from simulation.game_state import Half, PlayResult

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The event names that count as a **hit** for the H column.  We accept both the
#: canonical outcome spellings (``simulation.constants``) and the raw
#: ``PlayResult.event`` spellings -- which happen to coincide for the four hit
#: types -- so the derivation is tolerant of either field being populated.
HIT_EVENTS: frozenset[str] = frozenset({"single", "double", "triple", "home_run"})


def _is_hit(result: PlayResult) -> bool:
    """True when this play is a base hit (1B/2B/3B/HR).

    Tolerant of canonical-vs-raw naming: a play counts as a hit if EITHER its
    ``canonical_event`` (the resolved canonical outcome key, SIM-312) OR its
    raw ``event`` is in :data:`HIT_EVENTS`.  A reach-on-error is its own
    canonical outcome since SIM-511 (it aliased to ``"single"`` before), so it
    never enters here; the ``is_error`` guard below stays as defense in depth
    for a legacy path that still labels the event a hit.
    """
    if result.is_error:
        return False
    if result.canonical_event in HIT_EVENTS:
        return True
    return result.event in HIT_EVENTS


# ---------------------------------------------------------------------------
# InningLine -- one inning's away/home run cells
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InningLine:
    """One inning of the linescore: the away (top) and home (bottom) run cells.

    ``away`` / ``home`` are the integer runs scored in that half-inning.  A half
    that was never played (e.g. the bottom of the final inning when the home team
    already leads, or a walk-off that ended the game) is ``None`` -- the
    scoreboard "x" -- distinguished from ``0`` (the half was played and scoreless).
    """

    inning: int
    away: int | None = None
    home: int | None = None

    @property
    def away_played(self) -> bool:
        """Whether the top (away) half of this inning was played."""
        return self.away is not None

    @property
    def home_played(self) -> bool:
        """Whether the bottom (home) half of this inning was played."""
        return self.home is not None


# ---------------------------------------------------------------------------
# Linescore -- the full per-inning grid + R/H/E totals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Linescore:
    """The full linescore: per-inning run grid + team R/H/E totals.

    ``innings`` is ordered 1..N (N >= 9 for extra innings, variable).  The team
    totals are the row sums (runs = sum of the team's played half cells; hits +
    errors counted from the play stream).
    """

    innings: list[InningLine] = field(default_factory=list)

    away_runs: int = 0
    home_runs: int = 0
    away_hits: int = 0
    home_hits: int = 0
    away_errors: int = 0
    home_errors: int = 0

    @property
    def n_innings(self) -> int:
        """How many innings the game lasted (>= 9, more for extra innings)."""
        return len(self.innings)

    @property
    def away_by_inning(self) -> list[int | None]:
        """The away (top) run cell per inning, in order (None == not played)."""
        return [ln.away for ln in self.innings]

    @property
    def home_by_inning(self) -> list[int | None]:
        """The home (bottom) run cell per inning, in order (None == not played)."""
        return [ln.home for ln in self.innings]


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------


def linescore_from_plays(results: Sequence[PlayResult]) -> Linescore:
    """Derive a :class:`Linescore` from one game's ordered ``PlayResult`` stream.

    Each play is attributed to the ``(inning, half)`` it was PLAYED in.  The
    committed ``next_state`` names that half for every play except the one
    that records the third out: the loop rolls the half-inning before it
    commits, so ``next_state`` then names the NEXT half.  Such a play can still
    carry runs (a lead runner scores before a trailing runner is thrown out —
    Rule 5.08 timing, which the SIM-512 advancement draws play), so the roll
    is detected (``outs_recorded > 0`` and the committed ``outs`` is back to
    0) and the play is attributed to the half it ended.  Before SIM-486 the
    no-DB harness could never produce that play, so the misattribution went
    unseen.  Runs (``runs_scored``) are summed
    per half-inning into the away (TOP) / home (BOTTOM) columns; hits are counted
    per **batting** team; errors are charged to the **fielding** team.  See the
    module docstring for the full mapping + in-progress conventions.

    A play whose ``next_state`` is ``None`` (e.g. a non-terminal pitch a recorder
    chose to omit a committed state for) is skipped for the runs grid but still
    contributes its hit/error to the team derived from... it cannot be placed, so
    it is skipped entirely.  In normal recorder output every committed play
    carries ``next_state``.

    An empty stream yields an empty :class:`Linescore` (no innings, all zeros).
    """
    # Per-(inning) accumulators.  We track which halves were actually played so
    # an unplayed bottom half stays None rather than collapsing to 0.
    away_runs_by_inning: dict[int, int] = {}
    home_runs_by_inning: dict[int, int] = {}
    away_played: set[int] = set()
    home_played: set[int] = set()

    away_hits = home_hits = 0
    away_errors = home_errors = 0
    max_inning = 0

    for result in results:
        state = result.next_state
        if state is None:
            # No committed state -> cannot place this play on the grid; skip.
            continue

        inning = int(state.inning)
        half = state.half
        if int(result.outs_recorded) > 0 and int(state.outs) == 0:
            # This play ended the half-inning; the committed state is the
            # NEXT half. Attribute the play to the half it was played in.
            if half == Half.BOTTOM:
                half = Half.TOP
            else:
                half = Half.BOTTOM
                inning -= 1
        max_inning = max(max_inning, inning)

        runs = int(result.runs_scored)

        if half == Half.TOP:
            # Away team batting -> away column; the home team is on defense.
            away_played.add(inning)
            away_runs_by_inning[inning] = away_runs_by_inning.get(inning, 0) + runs
            if _is_hit(result):
                away_hits += 1
            if result.is_error:
                home_errors += 1  # error charged to the fielding (home) side
        else:  # Half.BOTTOM
            # Home team batting -> home column; the away team is on defense.
            home_played.add(inning)
            home_runs_by_inning[inning] = home_runs_by_inning.get(inning, 0) + runs
            if _is_hit(result):
                home_hits += 1
            if result.is_error:
                away_errors += 1  # error charged to the fielding (away) side

    innings: list[InningLine] = []
    for inning in range(1, max_inning + 1):
        away_cell = away_runs_by_inning.get(inning, 0) if inning in away_played else None
        home_cell = home_runs_by_inning.get(inning, 0) if inning in home_played else None
        innings.append(InningLine(inning=inning, away=away_cell, home=home_cell))

    away_runs = sum(c for c in (ln.away for ln in innings) if c is not None)
    home_runs = sum(c for c in (ln.home for ln in innings) if c is not None)

    return Linescore(
        innings=innings,
        away_runs=away_runs,
        home_runs=home_runs,
        away_hits=away_hits,
        home_hits=home_hits,
        away_errors=away_errors,
        home_errors=home_errors,
    )


__all__ = [
    "HIT_EVENTS",
    "InningLine",
    "Linescore",
    "linescore_from_plays",
]
