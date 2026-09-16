"""
simulation/game_market_distributions.py
=======================================
SIM-421 (owner ruling 2026-09-12) — the simulator prices EVERY game market
the book posts, not only the full-game moneyline, run line and total.

WHY THIS EXISTS
---------------
The odds tables now carry fifteen game markets (``pipeline/odds_provider.py``
``GAME_MARKET_TYPES``): the three full-game markets plus twelve segment and
team markets — the first-inning and first-five-innings moneyline, total and
run line; each side's full-game and first-five team total; the first team to
score; and "a run in the first inning". The simulator plays every game pitch
by pitch, so every one of those markets is a question the per-iteration
linescore already answers. This module turns N simulated games into the
probability each market needs, in one place, with one convention per market
kind.

THE INPUT
---------
:class:`SegmentRuns` holds, per iteration, the run totals every market settles
on: each team's full-game runs, first-inning runs and runs through five
innings, plus which team scored first. Build it from the per-iteration
:class:`~simulation.linescore.Linescore` (``from_linescores``), from the raw
per-iteration play streams (``from_plays``), or from plain per-inning run
lists (``from_inning_grids`` — the pickle-light form a parallel worker can
carry back). A half-inning that was never played (the scoreboard "x") counts
as zero runs, the way the official record does.

THE OUTPUT
----------
:func:`market_probability` answers "what is the simulator's probability of the
reference side of this market at this line?" for any market type, using the
same conventions the full-game markets already use in
:mod:`betting.clv_engine`:

  * a total-kind market (``total``, ``f1_total``, ``f5_total``, the four team
    totals) — the OVER is the STRICT ``P(runs > line)``; a whole-number line
    can push (``P(runs == line)``) and the push mass belongs to neither side;
  * ``first_inning_run`` — Yes IS "over 0.5 first-inning runs", so it is the
    total kind at line 0.5;
  * a moneyline-kind market (``moneyline``, ``first_to_score``) — HOME is
    ``P(home outscored away)`` over the segment (for the first team to score,
    ``P(home scored first)``); the full-game moneyline has no tie because a
    game plays until a winner;
  * a three-way market (``f1_moneyline``, ``f5_moneyline``) — HOME is
    ``P(home led after the segment)``, AWAY the mirror, and DRAW
    ``P(the segment ended tied)``; the three sum to one;
  * a run-line-kind market (``runline``, ``f1_runline``, ``f5_runline``) — HOME
    covers when ``home_runs + spread > away_runs`` over the segment; an equal
    result is a push (mirrors :func:`betting.clv_engine.spread_cover_prob`).

DETERMINISM
-----------
Pure numpy over the per-iteration arrays. No DB, no sampler, no RNG. The same
linescores always yield the same probabilities.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from pipeline.odds_provider import GAME_MARKET_KIND, GAME_MARKET_SEGMENT, GAME_MARKET_SIDE

#: The innings a "first five" market settles on.
FIRST_FIVE_INNINGS = 5

#: ``first_to_score`` codes: the home team scored first, the away team did, or
#: nobody scored (a suspended or 0-0 grid; never a completed game).
HOME_FIRST = 1
AWAY_FIRST = -1
NOBODY_FIRST = 0


def _segment_runs(cells: Sequence[int | None], through: int | None) -> int:
    """Runs in the first ``through`` innings of one team's cells (``None`` = all).

    An unplayed half (``None``) is zero runs — the official record scores it
    the same way.
    """
    picked = cells if through is None else cells[:through]
    return int(sum(c or 0 for c in picked))


def _first_to_score(away_cells: Sequence[int | None], home_cells: Sequence[int | None]) -> int:
    """Which team scored first: the innings are walked in order, top half then
    bottom half, and the first half-inning with a run decides."""
    for away, home in zip(away_cells, home_cells, strict=False):
        if (away or 0) > 0:
            return AWAY_FIRST
        if (home or 0) > 0:
            return HOME_FIRST
    return NOBODY_FIRST


@dataclass(frozen=True, slots=True)
class SegmentRuns:
    """Per-iteration run totals for every segment a game market settles on.

    Every array has length ``n`` (one entry per simulated game, in input
    order). Build with one of the three constructors; do not populate by hand.
    """

    n: int
    home_game: NDArray[np.int64]
    away_game: NDArray[np.int64]
    home_f1: NDArray[np.int64]
    away_f1: NDArray[np.int64]
    home_f5: NDArray[np.int64]
    away_f5: NDArray[np.int64]
    #: ``HOME_FIRST`` / ``AWAY_FIRST`` / ``NOBODY_FIRST`` per iteration.
    first_to_score: NDArray[np.int8]

    # ------------------------------------------------------------ builders
    @classmethod
    def from_inning_grids(
        cls, grids: Iterable[tuple[Sequence[int | None], Sequence[int | None]]]
    ) -> SegmentRuns:
        """Build from ``(home_cells, away_cells)`` per iteration — one run count
        per inning for each team, ``None`` for an unplayed half. This is the
        light form a worker can pickle back to the parent."""
        home_game: list[int] = []
        away_game: list[int] = []
        home_f1: list[int] = []
        away_f1: list[int] = []
        home_f5: list[int] = []
        away_f5: list[int] = []
        first: list[int] = []
        for home_cells, away_cells in grids:
            home_game.append(_segment_runs(home_cells, None))
            away_game.append(_segment_runs(away_cells, None))
            home_f1.append(_segment_runs(home_cells, 1))
            away_f1.append(_segment_runs(away_cells, 1))
            home_f5.append(_segment_runs(home_cells, FIRST_FIVE_INNINGS))
            away_f5.append(_segment_runs(away_cells, FIRST_FIVE_INNINGS))
            first.append(_first_to_score(away_cells, home_cells))
        n = len(home_game)
        if n == 0:
            raise ValueError("SegmentRuns needs >= 1 iteration")
        return cls(
            n=n,
            home_game=np.asarray(home_game, dtype=np.int64),
            away_game=np.asarray(away_game, dtype=np.int64),
            home_f1=np.asarray(home_f1, dtype=np.int64),
            away_f1=np.asarray(away_f1, dtype=np.int64),
            home_f5=np.asarray(home_f5, dtype=np.int64),
            away_f5=np.asarray(away_f5, dtype=np.int64),
            first_to_score=np.asarray(first, dtype=np.int8),
        )

    @classmethod
    def from_linescores(cls, linescores: Iterable[Any]) -> SegmentRuns:
        """Build from per-iteration :class:`~simulation.linescore.Linescore` objects."""
        return cls.from_inning_grids((ls.home_by_inning, ls.away_by_inning) for ls in linescores)

    @classmethod
    def from_plays(cls, per_iteration_plays: Iterable[Sequence[Any]]) -> SegmentRuns:
        """Build from the per-iteration ordered ``PlayResult`` streams (the
        ``record_game_plays`` output), through ``linescore_from_plays``."""
        from simulation.linescore import linescore_from_plays

        return cls.from_linescores(linescore_from_plays(plays) for plays in per_iteration_plays)

    @classmethod
    def from_official_grid(cls, inning_scores: dict[str, Sequence[int]]) -> SegmentRuns:
        """Build a one-iteration ``SegmentRuns`` from the official
        ``raw.games.inning_scores`` grid (``{"home": [...], "away": [...]}``) —
        the REAL outcome the accuracy comparison grades a market against."""
        return cls.from_inning_grids([(inning_scores["home"], inning_scores["away"])])

    # ------------------------------------------------------------ accessors
    def team_runs(self, segment: str, side: str) -> NDArray[np.int64]:
        """The per-iteration runs of ``side`` (``home`` / ``away``) over
        ``segment`` (``game`` / ``f1`` / ``f5``)."""
        key = f"{side}_{'game' if segment == 'game' else segment}"
        return getattr(self, key)  # type: ignore[no-any-return]

    def segment_total(self, segment: str) -> NDArray[np.int64]:
        return self.team_runs(segment, "home") + self.team_runs(segment, "away")

    def segment_margin(self, segment: str) -> NDArray[np.int64]:
        """Home runs minus away runs over ``segment``."""
        return self.team_runs(segment, "home") - self.team_runs(segment, "away")

    def market_samples(self, market_type: str) -> NDArray[np.int64]:
        """The per-iteration number a total-kind market settles on: the segment
        total, or one side's segment runs for a team total."""
        kind = GAME_MARKET_KIND[market_type]
        if kind not in ("total", "yes_no"):
            raise ValueError(f"{market_type!r} is not a total-kind market")
        segment = GAME_MARKET_SEGMENT[market_type]
        side = GAME_MARKET_SIDE[market_type]
        if side is None:
            return self.segment_total(segment)
        return self.team_runs(segment, side)


# ===========================================================================
# The probability each market needs
# ===========================================================================


def _validate_market(market_type: str) -> str:
    if market_type not in GAME_MARKET_KIND:
        raise ValueError(f"unknown game market_type {market_type!r}")
    return GAME_MARKET_KIND[market_type]


def total_probabilities(
    runs: SegmentRuns, market_type: str, line: float
) -> tuple[float, float, float]:
    """``(p_over, p_under, p_push)`` for a total-kind market at ``line``.

    Over is the STRICT ``P(runs > line)``, under the strict ``P(runs < line)``;
    a whole-number line keeps its push mass apart. ``first_inning_run`` is the
    same question at line 0.5 whatever line is passed (a yes / no market has
    no other line).
    """
    kind = _validate_market(market_type)
    if kind == "yes_no":
        line = 0.5
    samples = runs.market_samples(market_type)
    line_f = float(line)
    p_over = float(np.mean(samples > line_f))
    p_under = float(np.mean(samples < line_f))
    p_push = float(np.mean(samples == line_f))
    return p_over, p_under, p_push


def side_probabilities(runs: SegmentRuns, market_type: str) -> tuple[float, float, float]:
    """``(p_home, p_away, p_draw)`` for a moneyline-kind, three-way, or
    first-to-score market over its segment.

    ``p_draw`` is the tie share of the segment (zero for a completed full game,
    which plays until a winner; zero for the first team to score). The three
    sum to one when every iteration produced a result.
    """
    kind = _validate_market(market_type)
    if kind not in ("moneyline", "three_way"):
        raise ValueError(f"{market_type!r} is not a side market")
    if market_type == "first_to_score":
        first = runs.first_to_score
        return (
            float(np.mean(first == HOME_FIRST)),
            float(np.mean(first == AWAY_FIRST)),
            float(np.mean(first == NOBODY_FIRST)),
        )
    margin = runs.segment_margin(GAME_MARKET_SEGMENT[market_type])
    return (
        float(np.mean(margin > 0)),
        float(np.mean(margin < 0)),
        float(np.mean(margin == 0)),
    )


def cover_probabilities(
    runs: SegmentRuns, market_type: str, home_spread: float
) -> tuple[float, float, float]:
    """``(p_home_covers, p_away_covers, p_push)`` for a run-line-kind market at
    the HOME spread over its segment.

    Home covers when ``home_runs + home_spread > away_runs``; an exact equality
    (possible on a whole-number spread such as the first-inning pick'em at 0)
    is a push. Mirrors :func:`betting.clv_engine.spread_cover_prob`.
    """
    kind = _validate_market(market_type)
    if kind != "runline":
        raise ValueError(f"{market_type!r} is not a run-line market")
    margin = runs.segment_margin(GAME_MARKET_SEGMENT[market_type]).astype(np.float64)
    adjusted = margin + float(home_spread)
    return (
        float(np.mean(adjusted > 0)),
        float(np.mean(adjusted < 0)),
        float(np.mean(adjusted == 0)),
    )


def market_probability(runs: SegmentRuns, market_type: str, *, line: float | None = None) -> float:
    """The simulator's probability of the FIXED reference side of a market —
    the number the accuracy comparison pairs against the closing line.

    The reference side is HOME for a moneyline-kind, three-way or run-line
    market and OVER for a total-kind market — the convention
    ``scripts/clv_backtest.py`` fixed once so the read never depends on which
    side the model would have bet. ``line`` is the total line or the home
    spread; a side market ignores it.
    """
    kind = _validate_market(market_type)
    if kind in ("total", "yes_no"):
        if line is None and kind == "total":
            raise ValueError(f"{market_type!r} needs a line")
        return total_probabilities(runs, market_type, 0.5 if line is None else line)[0]
    if kind == "runline":
        if line is None:
            raise ValueError(f"{market_type!r} needs the home spread")
        return cover_probabilities(runs, market_type, line)[0]
    return side_probabilities(runs, market_type)[0]


def market_outcome(
    actual: SegmentRuns, market_type: str, *, line: float | None = None
) -> int | None:
    """The 0/1 outcome of the FIXED reference side on the REAL result (a
    one-iteration ``SegmentRuns`` from the official grid), or ``None`` on a
    push — the label the accuracy comparison scores against.

    A push (the total on the line, the adjusted margin exactly zero, a tied
    segment on a two-way side market) has no 0/1 label and returns ``None``;
    a tied segment on a THREE-way market is a real loss for the home side
    (the draw was a priced outcome), so it returns 0.
    """
    kind = _validate_market(market_type)
    if actual.n != 1:
        raise ValueError("market_outcome grades ONE real result")
    if kind in ("total", "yes_no"):
        if kind == "yes_no":
            line = 0.5
        if line is None:
            raise ValueError(f"{market_type!r} needs a line")
        value = float(actual.market_samples(market_type)[0])
        if value == float(line):
            return None
        return 1 if value > float(line) else 0
    if kind == "runline":
        if line is None:
            raise ValueError(f"{market_type!r} needs the home spread")
        adjusted = float(actual.segment_margin(GAME_MARKET_SEGMENT[market_type])[0]) + float(line)
        if adjusted == 0.0:
            return None
        return 1 if adjusted > 0 else 0
    if market_type == "first_to_score":
        first = int(actual.first_to_score[0])
        if first == NOBODY_FIRST:
            return None
        return 1 if first == HOME_FIRST else 0
    margin = int(actual.segment_margin(GAME_MARKET_SEGMENT[market_type])[0])
    if margin == 0:
        return 0 if kind == "three_way" else None
    return 1 if margin > 0 else 0


__all__ = [
    "SegmentRuns",
    "FIRST_FIVE_INNINGS",
    "HOME_FIRST",
    "AWAY_FIRST",
    "NOBODY_FIRST",
    "total_probabilities",
    "side_probabilities",
    "cover_probabilities",
    "market_probability",
    "market_outcome",
]
