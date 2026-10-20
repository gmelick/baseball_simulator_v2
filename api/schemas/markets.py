"""
api/schemas/markets.py
======================
The models of ``betting``: CLV (closing-line value), the edge report, the bet
signal and the line movement.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from api.schemas._base import _ApiModel
from api.serialization import to_jsonable
from pipeline.odds_provider import book_display_name


class CLVModel(_ApiModel):
    """Response model for :class:`betting.clv_engine.CLV` (SIM-339).

    Closing Line Value for one side of one market/prop.  ``beat_close`` mirrors
    the source dataclass's derived property (clv_prob > 0).
    """

    entry_fair_prob: float
    close_fair_prob: float
    clv_prob: float
    entry_fair_decimal: float
    close_fair_decimal: float
    clv_decimal: float
    beat_close: bool

    @classmethod
    def from_dataclass(cls, clv: Any) -> CLVModel:
        """Build from a :class:`betting.clv_engine.CLV`."""
        return cls(
            entry_fair_prob=float(clv.entry_fair_prob),
            close_fair_prob=float(clv.close_fair_prob),
            clv_prob=float(clv.clv_prob),
            entry_fair_decimal=float(clv.entry_fair_decimal),
            close_fair_decimal=float(clv.close_fair_decimal),
            clv_decimal=float(clv.clv_decimal),
            beat_close=bool(clv.beat_close),
        )


class EdgeReportModel(_ApiModel):
    """Response model for :class:`betting.clv_engine.EdgeReport` (SIM-339).

    The full edge / EV / CLV report for one side of one market or prop.  ``side``
    is the MarketSide enum's ``.value`` (a JSON-native string, e.g. "home" /
    "over"); ``clv`` is present only when a closing quote was supplied.
    ``positive_edge`` mirrors the source's derived property (edge > 0).

    SIM-555: when the betting routes price a market from the stored lines (the
    closing line; before the game starts, also the current line),
    ``price_book`` names the book whose price is ``offered_american`` (the best
    price a bettor can take at this line, e.g. ``bp:10``) and
    ``price_book_name`` its display name ("FanDuel"). Both are None for an
    injected or mock price. The source dataclass carries no book, so the route
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

    clv: CLVModel | None = None
    positive_edge: bool

    #: The stored book label of the offered price (None: injected or mock).
    price_book: str | None = None
    #: That book's display name ("FanDuel").
    price_book_name: str | None = None

    #: How this side settled on the real final: "won", "lost" or "push"; None
    #: before the game is final.
    result: str | None = None

    @classmethod
    def from_dataclass(cls, report: Any, *, price_book: str | None = None) -> EdgeReportModel:
        """Build from a :class:`betting.clv_engine.EdgeReport`.

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
            clv=None if report.clv is None else CLVModel.from_dataclass(report.clv),
            positive_edge=bool(report.positive_edge),
            price_book=price_book,
            price_book_name=None if price_book is None else book_display_name(price_book),
        )


class BetSignalModel(_ApiModel):
    """Response model for :class:`betting.bet_signal.BetSignal` (SIM-369).

    A fireable +EV bet recommendation derived from one :class:`EdgeReport`.
    ``side`` is the MarketSide enum's ``.value`` (a JSON-native string, e.g.
    "home" / "over"); ``report`` is the full source :class:`EdgeReportModel` (so a
    consumer gets the sim/market probabilities + CLV behind the signal). The list
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
    confidence: float
    rank: int

    #: The full source EdgeReport (sim_prob, market fair prob, sim_fair_american, CLV).
    report: EdgeReportModel

    #: The stored book label of the offered price (None: injected or mock).
    price_book: str | None = None
    #: That book's display name ("FanDuel").
    price_book_name: str | None = None

    @classmethod
    def from_dataclass(cls, signal: Any, *, price_book: str | None = None) -> BetSignalModel:
        """Build from a :class:`betting.bet_signal.BetSignal`.

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
            confidence=float(signal.confidence),
            rank=int(signal.rank),
            report=EdgeReportModel.from_dataclass(signal.report, price_book=price_book),
            price_book=price_book,
            price_book_name=None if price_book is None else book_display_name(price_book),
        )


class LineQuoteModel(_ApiModel):
    """Response model for :class:`betting.line_movement.LineQuote` (SIM-368).

    One timestamped quote for a single side of one market at one book -- a point
    on the line-movement time-series. ``fetched_at`` is rendered as a string (a
    datetime is ISO-8601'd via :func:`api.serialization.to_jsonable`, an already-
    string timestamp passes through). ``implied_prob`` is the RAW single-side
    implied probability. numpy-free.
    """

    fetched_at: str | None = None
    line_type: str
    book: str
    is_sharp_book: bool
    american: float
    other_american: float | None = None
    line: float | None = None
    implied_prob: float
    #: The other side's own line (run lines only). A run-line quote is
    #: a pair when it is the negative of ``line``; otherwise two separate bets.
    other_line: float | None = None
    #: The book's display name ("FanDuel" for ``bp:10``).
    book_name: str = ""

    @classmethod
    def from_dataclass(cls, quote: Any) -> LineQuoteModel:
        """Build from a :class:`betting.line_movement.LineQuote`."""
        fa = quote.fetched_at
        fetched_at = None if fa is None else str(to_jsonable(fa))
        return cls(
            fetched_at=fetched_at,
            line_type=str(quote.line_type),
            book=str(quote.book),
            is_sharp_book=bool(quote.is_sharp_book),
            american=float(quote.american),
            other_american=(None if quote.other_american is None else float(quote.other_american)),
            line=None if quote.line is None else float(quote.line),
            implied_prob=float(quote.implied_prob),
            other_line=(
                None if getattr(quote, "other_line", None) is None else float(quote.other_line)
            ),
            # The app imports betting/ from its image; a LineQuote without
            # book_name (an image built before the field) falls back to the
            # vocabulary.
            book_name=getattr(quote, "book_name", None) or book_display_name(str(quote.book)),
        )


class LineMovementModel(_ApiModel):
    """Response model for :class:`betting.line_movement.LineMovement` (SIM-368).

    The line-movement time-series for one (game_pk, market, side, book): the
    ordered :class:`LineQuoteModel` series plus the derived opening->closing
    summary (the American / implied-prob / line deltas, the per-step delta series,
    the running ``implied_prob_series`` plottable surface, the steam ``direction``)
    and the entry-vs-close :class:`CLVModel`. ``side`` is the MarketSide enum's
    ``.value``; ``has_movement`` / ``beat_close`` mirror the source's derived
    properties. numpy-free (plain floats).
    """

    game_pk: int
    market_type: str
    #: The MarketSide enum value as a string ("home"/"away"/"over"/"under").
    side: str
    book: str | None = None
    #: The book's display name ("FanDuel" for ``bp:10``); the chart
    #: title shows it. An empty string when the series names no book.
    book_name: str = ""

    quotes: list[LineQuoteModel] = Field(default_factory=list)

    opening_american: float | None = None
    closing_american: float | None = None
    opening_implied_prob: float | None = None
    closing_implied_prob: float | None = None

    american_delta: float | None = None
    implied_prob_delta: float | None = None
    line_delta: float | None = None

    step_implied_prob_deltas: list[float] = Field(default_factory=list)
    step_american_deltas: list[float] = Field(default_factory=list)
    implied_prob_series: list[float] = Field(default_factory=list)

    direction: str = "flat"

    clv: CLVModel | None = None
    sharp_consensus: bool | None = None

    #: Run lines only: 'pair' / 'two_bets' / 'mixed' — whether the
    #: book listed the two sides of one bet or two separate bets.
    run_line_shape: str | None = None
    #: How the CLV was priced: 'pair' (one two-way bet at both ends) or
    #: 'two_bets' (an end was two separate bets, so both ends are priced on
    #: their own price over the game's two-way margin). None without a CLV.
    clv_basis: str | None = None
    #: Run lines only: why there is no CLV, or a caveat on it, in plain words.
    clv_note: str | None = None

    #: Derived from the source dataclass's properties.
    has_movement: bool = False
    beat_close: bool = False

    @classmethod
    def from_dataclass(cls, mv: Any) -> LineMovementModel:
        """Build from a :class:`betting.line_movement.LineMovement`."""
        side = mv.side
        side_value = side.value if hasattr(side, "value") else str(side)

        def _of(v: Any) -> float | None:
            return None if v is None else float(v)

        return cls(
            game_pk=int(mv.game_pk),
            market_type=str(mv.market_type),
            side=str(side_value),
            book=None if mv.book is None else str(mv.book),
            # The same image fallback as LineQuoteModel.book_name.
            book_name=getattr(mv, "book_name", None)
            or book_display_name(None if mv.book is None else str(mv.book)),
            quotes=[LineQuoteModel.from_dataclass(q) for q in mv.quotes],
            opening_american=_of(mv.opening_american),
            closing_american=_of(mv.closing_american),
            opening_implied_prob=_of(mv.opening_implied_prob),
            closing_implied_prob=_of(mv.closing_implied_prob),
            american_delta=_of(mv.american_delta),
            implied_prob_delta=_of(mv.implied_prob_delta),
            line_delta=_of(mv.line_delta),
            step_implied_prob_deltas=[float(x) for x in mv.step_implied_prob_deltas],
            step_american_deltas=[float(x) for x in mv.step_american_deltas],
            implied_prob_series=[float(x) for x in mv.implied_prob_series],
            direction=str(mv.direction),
            clv=None if mv.clv is None else CLVModel.from_dataclass(mv.clv),
            sharp_consensus=(None if mv.sharp_consensus is None else bool(mv.sharp_consensus)),
            run_line_shape=getattr(mv, "run_line_shape", None),
            clv_basis=getattr(mv, "clv_basis", None),
            clv_note=getattr(mv, "clv_note", None),
            has_movement=bool(mv.has_movement),
            beat_close=bool(mv.beat_close),
        )
