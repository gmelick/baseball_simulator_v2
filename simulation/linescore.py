"""
linescore.py
============
The per-inning linescore and each team's runs / hits / errors (R/H/E).

The box-score surface and the frontend scoreboard show the classic baseball
**linescore**: the per-inning runs grid plus each team's Runs / Hits / Errors
totals::

         1 2 3 4 5 6 7 8 9   R  H  E
    AWAY 0 1 0 0 1 0 0 0 0   2  7  1
    HOME 1 0 0 0 0 2 0 0 x   3  6  0

This module holds the linescore dataclasses and the *pure* derivation from one
game's ordered ``PlayResult`` stream (the recorder's per-pitch output).

DESIGN
------
  * Pure.  ``linescore_from_plays`` only reads ``PlayResult`` / ``GameState``
    objects: no DB, no FAISS, no live API, no random draw, no mutation.
  * Frozen, slotted dataclasses, in the style of ``simulation.snapshots``.

INNING / HALF -> COLUMN MAPPING
-------------------------------
Each ``PlayResult`` carries ``next_state``, the committed ``GameState`` AFTER
the pitch.  The derivation reads ``(inning, half)`` from it and credits the
play's runs, hits and errors to the half the play was played in (a play that
ends a half-inning is moved back to that half; see
:func:`linescore_from_plays`).  The half picks the column:

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

#: The event names that count as a **hit** for the H column.  The canonical
#: outcome spellings (``simulation.constants``) and the raw ``PlayResult.event``
#: spellings coincide for the four hit types, so the derivation works with
#: either field set.
HIT_EVENTS: frozenset[str] = frozenset({"single", "double", "triple", "home_run"})


def _is_hit(result: PlayResult) -> bool:
    """True when this play is a base hit (1B/2B/3B/HR).

    A play counts as a hit if EITHER its ``canonical_event`` (the resolved
    canonical outcome key) OR its raw ``event`` is in :data:`HIT_EVENTS`.  A
    reach on an error has its own canonical outcome (``field_error``), so it
    never matches a hit name.  The ``is_error`` guard is defense in depth: it
    keeps an error out of the H column even when a play labels its event a hit.
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
    already leads) is ``None`` -- the scoreboard "x" -- distinguished from ``0``
    (the half was played and scoreless).
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
    carry runs: a lead runner scores before a trailing runner is thrown out
    (the Rule 5.08 timing, which the advancement draws play).  So the roll is
    detected (``outs_recorded > 0`` and the committed ``outs`` back at 0), and
    the play is attributed to the half it ended.  Runs (``runs_scored``) are
    summed per half-inning into the away (TOP) / home (BOTTOM) columns; hits
    are counted per **batting** team; errors are charged to the **fielding**
    team.  See the module docstring for the full mapping and the walk-off
    conventions.

    A play whose ``next_state`` is ``None`` cannot be placed on the grid, so
    it is skipped entirely (its runs, hit and error too).  In normal recorder
    output every committed play carries ``next_state``.

    An empty stream yields an empty :class:`Linescore` (no innings, all zeros).
    """
    # Per-inning accumulators.  The played-half sets keep an unplayed half at
    # None rather than 0.
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
