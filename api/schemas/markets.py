"""
api/schemas/markets.py
======================
The models of ``betting``: the edge report and the bet signal.
"""

from __future__ import annotations

from typing import Any

from api.schemas._base import _ApiModel
from betting.markets import book_display_name


class EdgeReportModel(_ApiModel):
    """Response model for :class:`betting.pricing.EdgeReport` (SIM-339).

    The edge / EV report for one side of one market or prop.  ``side`` is the
    MarketSide enum's ``.value`` (a JSON-native string, e.g. "home" / "over").
    ``positive_edge`` mirrors the source's derived property (edge > 0).

    SIM-555: when the betting routes price a market from the stored lines (the
    closing line; before the game starts, also the current line),
    ``price_book`` names the book whose price is ``offered_american`` (the best
    price a bettor can take at this line, e.g. ``bp:10``) and
    ``price_book_name`` its display name ("FanDuel"). Both are None for an
    injected price. The source dataclass carries no book, so the route
    sets them.
    """

    label: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    line: float | None = None

    sim_prob: float
    market_fair_prob: float

    edge: float
    ev: float

    offered_american: float
    sim_fair_american: float

    positive_edge: bool

    #: The stored book label of the offered price (None: an injected price).
    price_book: str | None = None
    #: That book's display name ("FanDuel").
    price_book_name: str | None = None

    #: How this side settled on the real final: "won", "lost" or "push"; None
    #: before the game is final.
    result: str | None = None

    @classmethod
    def from_dataclass(cls, report: Any, *, price_book: str | None = None) -> EdgeReportModel:
        """Build from a :class:`betting.pricing.EdgeReport`.

        ``price_book`` is the stored label of the offered price's book; the
        display name follows from it.
        """
        side = report.side
        side_value = side.value if hasattr(side, "value") else str(side)
        return cls(
            label=str(report.label),
            side=str(side_value),
            line=None if report.line is None else float(report.line),
            sim_prob=float(report.sim_prob),
            market_fair_prob=float(report.market_fair_prob),
            edge=float(report.edge),
            ev=float(report.ev),
            offered_american=float(report.offered_american),
            sim_fair_american=float(report.sim_fair_american),
            positive_edge=bool(report.positive_edge),
            price_book=price_book,
            price_book_name=None if price_book is None else book_display_name(price_book),
        )


class BetSignalModel(_ApiModel):
    """Response model for :class:`betting.pricing.BetSignal` (SIM-369).

    A fireable +EV bet recommendation derived from one :class:`EdgeReport`.
    ``side`` is the MarketSide enum's ``.value`` (a JSON-native string, e.g.
    "home" / "over"); ``report`` is the full source :class:`EdgeReportModel` (so a
    consumer gets the sim/market probabilities behind the signal). The list
    a signals endpoint returns is already ranked by EV descending (``rank`` 0 ==
    strongest). numpy-free (plain floats / ints).
    """

    #: Source report's market label ("moneyline" / "total" / "run_line" / "prop:K").
    label: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    line: float | None = None
    offered_american: float

    edge: float
    ev: float
    stake_fraction: float
    rank: int

    #: The full source EdgeReport (sim_prob, market fair prob, sim_fair_american).
    report: EdgeReportModel

    #: The stored book label of the offered price (None: an injected price).
    price_book: str | None = None
    #: That book's display name ("FanDuel").
    price_book_name: str | None = None

    @classmethod
    def from_dataclass(cls, signal: Any, *, price_book: str | None = None) -> BetSignalModel:
        """Build from a :class:`betting.pricing.BetSignal`.

        ``price_book`` is the stored label of the offered price's book. The
        nested report carries it too.
        """
        side = signal.side
        side_value = side.value if hasattr(side, "value") else str(side)
        return cls(
            label=str(signal.label),
            side=str(side_value),
            line=None if signal.line is None else float(signal.line),
            offered_american=float(signal.offered_american),
            edge=float(signal.edge),
            ev=float(signal.ev),
            stake_fraction=float(signal.stake_fraction),
            rank=int(signal.rank),
            report=EdgeReportModel.from_dataclass(signal.report, price_book=price_book),
            price_book=price_book,
            price_book_name=None if price_book is None else book_display_name(price_book),
        )
