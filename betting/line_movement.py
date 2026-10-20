"""The line-movement series: one side's price at one book, from the opening to the close.

:func:`betting.clv_engine.clv_from_odds` compares two quotes: the entry and the
close. A book re-quotes a game many times between the opening line and the
closing line. The ordered quotes of one ``(game_pk, market, side, book)`` are
the line-movement series. From the series this module derives:

* the **implied-probability series**: the raw implied probability of each
  quote's American price (the book's probability for this one side, the vig,
  its margin, included), for a chart of how the market's opinion drifted;
* the **step deltas**: the change in American odds and in implied probability
  between each pair of consecutive quotes;
* the **opening-to-closing summary**: the total move in American odds, in
  implied probability and in the line (the spread or the total), and the
  direction: 'toward' this side when its implied probability rose, 'away'
  when it fell;
* the **closing line value (CLV)** of a bet at the opening price, by the
  :mod:`betting.clv_engine` arithmetic: the first quote is the entry and the
  last quote is the close;
* a **sharp-consensus** flag: whether the rows flagged ``is_sharp_book`` moved
  this side the same way as all the books together.

Movement and the CLV
--------------------
The de-vig (the removal of the vig) needs both sides of a market, so a series
of one side's prices cannot be de-vigged. Each quote's ``implied_prob``
therefore keeps the vig. The CLV de-vigs the two-way pairs at the opening and
at the closing quote. An implied probability that rises (the odds shorten,
e.g. -120 -> -150) means the market grew more confident in this side. A
positive ``clv_prob`` means a bet at the opening price beat the close. The raw
delta and the CLV can disagree in sign: the raw probability keeps the vig and
the CLV removes it. The CLV compares prices; the raw series shows the path.
The CLV here describes the market; the model is not judged by it (owner
ruling, 2026-09-28).

Design
------
* :func:`line_movement_from_quotes` is pure. It takes quotes (mappings or
  :class:`LineQuote`), sorts them, derives the series and returns a
  :class:`LineMovement`: no database, no random numbers.
* :func:`fetch_line_movement` is the only function that reads the database. It
  takes an asyncpg connection or a test stub, reads ``raw.game_odds`` in
  ``fetched_at`` order, groups the rows per (side, book) and calls the pure
  builder.
* Every odds conversion and every CLV goes through :mod:`betting.clv_engine`.
  This module only shapes the series.

The time axis of raw.game_odds
------------------------------
The series orders on ``fetched_at``, the time the writer stored the row. The
writer skips a row whose book, line type and prices match an earlier row of
the same game and source (a hash of the payload); the live writer re-stamps
that row's ``last_seen_at`` instead. So each stored row is a price set new to
the game, and a price that returns to an earlier value writes no new row. Each
row carries both sides of its market in paired columns (moneyline ``home_ml`` /
``away_ml``; run line ``home_spread_ml`` / ``away_spread_ml`` with
``home_spread`` / ``away_spread``; total ``over_ml`` / ``under_ml`` with
``total_line``), so one row de-vigs as a two-way market. This module reads
these three full-game markets only.

A run line: one pair, or two separate bets
------------------------------------------
A run-line row is one two-way bet only when the away spread is the negative of
the home spread (home −1.5 / away +1.5). The book also lists two separate bets,
such as home −1.5 / away −1.5 or home +1.5 / away +1.5. Their prices must not
be de-vigged against each other. So on a run line:

* each quote carries the other side's own spread (``other_line``);
* the series says whether its rows were pairs, two separate bets, or a mix
  (:attr:`LineMovement.run_line_shape`);
* the CLV compares only the same bet. When the side's spread moved between the
  opening and the closing quote, the series has no CLV, and
  :attr:`LineMovement.clv_note` says why;
* when either end is two separate bets, both ends are priced the same way:
  each price over the book's margin on the game's total, then its moneyline,
  at that time (:func:`betting.clv_engine.devig_one_sided`).
  :attr:`LineMovement.clv_basis` names the method.

One book per row
----------------
A ``bp:<id>`` row holds one book's prices for all its sides (``<id>`` is the
vendor's book id). A ``consensus`` row can mix two books in one row, so every
read here keeps only the ``bp:`` rows
(:data:`pipeline.odds_provider.STORED_BOOK_FILTER_SQL`). Each quote and each
series carries the book's display name (``book_name``, e.g. "FanDuel") for the
chart title.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from betting.clv_engine import (
    CLV,
    MarketSide,
    clv_from_odds,
    clv_from_prob,
    devig_one_sided,
    implied_prob_from_american,
    reference_margin_from_prices,
    run_line_is_pair,
)
from pipeline.odds_provider import STORED_BOOK_FILTER_SQL, book_display_name

# ===========================================================================
# Market and side -> (this side's column, the other side's column, the line column)
# ===========================================================================
#
# raw.game_odds stores both sides of a market in one row. A one-side series
# needs, per (market_type, side): this side's American-odds column, the other
# side's column (the de-vig needs the pair) and the line column (None for the
# moneyline). This module reads the three full-game markets; the table also
# holds the segment and team markets.

#: (this_american_col, other_american_col, line_col_or_None) per (market, side).
_MARKET_COLUMNS: dict[tuple[str, MarketSide], tuple[str, str, str | None]] = {
    ("moneyline", MarketSide.HOME): ("home_ml", "away_ml", None),
    ("moneyline", MarketSide.AWAY): ("away_ml", "home_ml", None),
    ("runline", MarketSide.HOME): ("home_spread_ml", "away_spread_ml", "home_spread"),
    ("runline", MarketSide.AWAY): ("away_spread_ml", "home_spread_ml", "away_spread"),
    ("total", MarketSide.OVER): ("over_ml", "under_ml", "total_line"),
    ("total", MarketSide.UNDER): ("under_ml", "over_ml", "total_line"),
}

#: The OTHER side's own line column on a run line (the away spread for the home
#: series, and the reverse). It tells a pair from two separate bets.
_OTHER_LINE_COLUMN: dict[tuple[str, MarketSide], str] = {
    ("runline", MarketSide.HOME): "away_spread",
    ("runline", MarketSide.AWAY): "home_spread",
}

#: ``reference_margin_at(quote) -> (margin, source)``: the book's margin on the
#: same game's two-way markets as of a quote (see :func:`fetch_line_movement`).
ReferenceMarginAt = Callable[["LineQuote"], tuple[float, str]]

#: The two sides of each market this module reads. An unknown market_type is
#: an error in :func:`fetch_line_movement` and :func:`_side_columns`.
_MARKET_SIDES: dict[str, tuple[MarketSide, MarketSide]] = {
    "moneyline": (MarketSide.HOME, MarketSide.AWAY),
    "runline": (MarketSide.HOME, MarketSide.AWAY),
    "total": (MarketSide.OVER, MarketSide.UNDER),
}


def _side_columns(market_type: str, side: MarketSide) -> tuple[str, str, str | None]:
    """The (this side, other side, line) raw.game_odds columns of a market and side.

    Raises ``ValueError`` for an unknown ``market_type`` or a ``side`` that does
    not belong to it (e.g. OVER on a moneyline).
    """
    key = (market_type, side)
    cols = _MARKET_COLUMNS.get(key)
    if cols is None:
        valid = _MARKET_SIDES.get(market_type)
        if valid is None:
            raise ValueError(
                f"unknown market_type {market_type!r} (expected one of {sorted(_MARKET_SIDES)})"
            )
        raise ValueError(
            f"side {side} is not valid for market_type {market_type!r} (valid sides: {valid})"
        )
    return cols


# ===========================================================================
# LineQuote: one stamped snapshot of one side of a market
# ===========================================================================


@dataclass(frozen=True, slots=True)
class LineQuote:
    """One stamped quote of one side of one market at one book: a point of the series.

    ``american`` is this side's American price at ``fetched_at``.
    ``other_american`` is the opposite side's price in the same snapshot, so the
    opening and the closing pair can be de-vigged into a :class:`CLV`.
    ``implied_prob`` is the raw implied probability of this one side
    (:func:`~betting.clv_engine.implied_prob_from_american`), the vig included:
    one side alone cannot be de-vigged. The no-vig value lives on the
    :class:`CLV`.
    """

    #: The time the writer stored the row (raw.game_odds ``fetched_at``). The sort
    #: takes any ordered type; the run-line margin reader also subtracts two
    #: stamps, so it needs a datetime or a number.
    fetched_at: Any
    #: 'opening' | 'current' | 'closing' | 'bet_placement' (the line_type CHECK).
    line_type: str
    #: The book label (raw.game_odds ``book``): ``bp:<id>``, or ``consensus`` on a
    #: row that names no book.
    book: str
    #: The row's ``is_sharp_book`` column; the sharp-consensus flag reads it.
    is_sharp_book: bool
    #: THIS side's American odds in this snapshot.
    american: float
    #: The OPPOSITE side's American odds in the same snapshot (for the de-vig).
    #: None when the row stored only this side; then there is no CLV.
    other_american: float | None = None
    #: The market line in this snapshot (the spread or the total). None for a moneyline.
    line: float | None = None
    #: The raw implied probability of ``american`` for this one side (the vig included).
    implied_prob: float = 0.0
    #: The OTHER side's own line in the same snapshot (run lines only).
    other_line: float | None = None
    #: The book's display name ("FanDuel" for ``bp:10``). A label the vocabulary
    #: does not name stays as it is (``consensus``, ``bp:999``).
    book_name: str = ""

    @property
    def is_run_line_pair(self) -> bool:
        """True when this run-line quote and its other side are the two sides of
        ONE bet (the other spread is the negative of this one)."""
        return run_line_is_pair(self.line, self.other_line)

    @staticmethod
    def from_american(
        *,
        fetched_at: Any,
        line_type: str,
        book: str,
        is_sharp_book: bool,
        american: float,
        other_american: float | None = None,
        line: float | None = None,
        other_line: float | None = None,
    ) -> LineQuote:
        """Build a :class:`LineQuote`; it fills ``implied_prob`` from ``american``
        and ``book_name`` from the book label.

        An American price of 0 raises ``ValueError``.
        """
        return LineQuote(
            fetched_at=fetched_at,
            line_type=str(line_type),
            book=str(book),
            book_name=book_display_name(str(book)),
            is_sharp_book=bool(is_sharp_book),
            american=float(american),
            other_american=None if other_american is None else float(other_american),
            line=None if line is None else float(line),
            implied_prob=implied_prob_from_american(float(american)),
            other_line=None if other_line is None else float(other_line),
        )


# ===========================================================================
# LineMovement: the ordered series and its opening-to-closing summary
# ===========================================================================


@dataclass(frozen=True, slots=True)
class LineMovement:
    """The line-movement series of one (game_pk, market, side, book).

    ``quotes`` holds the :class:`LineQuote` objects in time order, from the
    opening to the closing quote: one per stored row. The other fields are the
    opening-to-closing summary and the :class:`CLV` of a bet at the opening
    price. The module docstring says what the movement and the CLV mean.
    """

    game_pk: int
    market_type: str
    side: MarketSide
    book: str | None

    #: The ordered series, oldest (the opening) first, newest (the close) last.
    quotes: tuple[LineQuote, ...]

    #: The opening / closing American odds of THIS side (the first / last quote).
    opening_american: float | None = None
    closing_american: float | None = None
    #: The opening / closing raw implied probability (the first / last quote).
    opening_implied_prob: float | None = None
    closing_implied_prob: float | None = None

    #: closing_american - opening_american. American odds jump from + to - at even
    #: money, so implied_prob_delta is the better measure of the move.
    american_delta: float | None = None
    #: closing_implied_prob - opening_implied_prob. Above 0: the odds shortened and
    #: the market moved TOWARD this side; below 0: it moved away.
    implied_prob_delta: float | None = None
    #: The closing line minus the opening line (the spread or the total; None for
    #: a moneyline).
    line_delta: float | None = None

    #: The implied-probability change between each pair of consecutive quotes.
    #: Its length is len(quotes) - 1 (empty for a single quote).
    step_implied_prob_deltas: tuple[float, ...] = ()
    #: The American-odds change between each pair of consecutive quotes (same length).
    step_american_deltas: tuple[float, ...] = ()
    #: The raw implied probability of each quote, in order: the series to chart.
    implied_prob_series: tuple[float, ...] = ()

    #: 'toward' | 'away' | 'flat': the sign of implied_prob_delta, the direction
    #: the line moved for THIS side from the opening to the close.
    direction: str = "flat"

    #: The CLV of a bet at the opening quote against the closing quote. None when
    #: the series has fewer than 2 quotes or an end cannot be priced. On a run
    #: line it is also None when the side's spread moved (see ``clv_note``).
    clv: CLV | None = None

    #: True when the rows flagged ``is_sharp_book`` moved this side the SAME way as
    #: all the books together. None when either direction is flat (a game with no
    #: flagged row reads flat), and on a series that
    #: :func:`line_movement_from_quotes` builds directly: only
    #: :func:`fetch_line_movement` sees every book.
    sharp_consensus: bool | None = None

    #: Run lines only: 'pair' (every row the two sides of one bet), 'two_bets'
    #: (every row two separate bets) or 'mixed'. A row with a missing spread does
    #: not count. None on the other markets.
    run_line_shape: str | None = None
    #: How the CLV was priced. 'pair': both ends de-vigged as one two-way bet.
    #: 'two_bets': an end was two separate bets, so both ends are priced on their
    #: own price over the game's two-way margin. None when there is no CLV.
    clv_basis: str | None = None
    #: Run lines only: why there is no CLV, or a caveat on it, in plain words.
    clv_note: str | None = None
    #: The display name of ``book`` ("FanDuel" for ``bp:10``); an empty string
    #: when the series names no book.
    book_name: str = ""

    @property
    def has_movement(self) -> bool:
        """True when the series has 2 or more quotes and a non-zero net implied-
        probability delta. A price that moved and came back reads as no movement."""
        return len(self.quotes) >= 2 and bool(self.implied_prob_delta)

    @property
    def beat_close(self) -> bool:
        """True when a bet at the OPENING price beat the close (clv.beat_close)."""
        return self.clv is not None and self.clv.beat_close


# ===========================================================================
# The pure derivation
# ===========================================================================


def _coerce_quote(
    row: Mapping[str, Any] | LineQuote,
    *,
    market_type: str,
    side: MarketSide,
) -> LineQuote:
    """Turn one raw row (a mapping) or a :class:`LineQuote` into a LineQuote.

    A :class:`LineQuote` comes back as it is. A mapping is read by the
    ``raw.game_odds`` column layout of ``(market_type, side)``: this side's and
    the other side's American columns and the line column, plus ``fetched_at``,
    ``line_type``, ``book`` and ``is_sharp_book``. A row whose THIS-side price is
    NULL raises ``ValueError``, and the caller skips it. So does an unknown
    ``(market_type, side)`` or a price of 0.
    """
    if isinstance(row, LineQuote):
        return row
    this_col, other_col, line_col = _side_columns(market_type, side)
    american = row.get(this_col)
    if american is None:
        raise ValueError(f"row has no {this_col} price (NULL) -- skip")
    other = row.get(other_col)
    line_val = row.get(line_col) if line_col is not None else None
    other_line_col = _OTHER_LINE_COLUMN.get((market_type, side))
    other_line = row.get(other_line_col) if other_line_col is not None else None
    return LineQuote.from_american(
        fetched_at=row.get("fetched_at"),
        line_type=row.get("line_type", "current"),
        book=row.get("book", "consensus"),
        is_sharp_book=bool(row.get("is_sharp_book", False)),
        american=american,
        other_american=other,
        line=line_val,
        other_line=other_line,
    )


def _sort_key(q: LineQuote) -> tuple[int, Any]:
    """The sort key of a quote: ``fetched_at``, with a missing stamp first.

    The (has-stamp, value) tuple puts a quote with no ``fetched_at`` (e.g. a
    hand-built quote) before the stamped ones. Comparing None with a datetime
    would raise.
    """
    return (0, "") if q.fetched_at is None else (1, q.fetched_at)


def line_movement_from_quotes(
    rows: Sequence[Mapping[str, Any] | LineQuote],
    *,
    market_type: str,
    side: MarketSide,
    game_pk: int = 0,
    book: str | None = None,
    sharp_consensus: bool | None = None,
    reference_margin_at: ReferenceMarginAt | None = None,
) -> LineMovement:
    """Build a :class:`LineMovement` from quotes in any order. Pure.

    ``rows`` holds :class:`LineQuote` objects or ``raw.game_odds`` row mappings
    (read by the ``(market_type, side)`` column layout). The steps:

      1. turn each row into a quote, and drop a row that raises ``ValueError``
         (a NULL this-side price);
      2. sort by ``fetched_at`` (a quote with no stamp first);
      3. the raw implied-probability series and the step deltas in American odds
         and in implied probability (n - 1 of each; none for a single quote);
      4. the opening-to-closing summary: the American, implied-probability and
         line deltas, and the ``direction``;
      5. the :class:`CLV` of the opening against the closing two-way pair
         (:func:`~betting.clv_engine.clv_from_odds`), when both ends carry the
         other side's price; else ``clv`` is None.

    No rows give an empty movement. A single quote gives no movement: no deltas,
    the opening equals the close, ``direction`` "flat", no CLV. A missing other
    side's price at either end gives no CLV, but the series and the deltas are
    still there.

    ``game_pk``, ``book`` and ``sharp_consensus`` go onto the result as given.
    :func:`fetch_line_movement` supplies real values; a direct caller may keep
    the defaults.

    On a run line the CLV needs the SAME bet at both ends, so a side whose spread
    moved gets no CLV. When either end is two separate bets, both ends are priced
    over ``reference_margin_at`` (the game's two-way margin at that quote).
    Without it there is no CLV. The result carries
    :attr:`LineMovement.run_line_shape`, ``clv_basis`` and ``clv_note``.
    """
    quotes: list[LineQuote] = []
    for r in rows:
        try:
            quotes.append(_coerce_quote(r, market_type=market_type, side=side))
        except ValueError:
            # No this-side price for this market and side: not part of the series.
            continue
    quotes.sort(key=_sort_key)

    if not quotes:
        return LineMovement(
            game_pk=int(game_pk),
            market_type=market_type,
            side=side,
            book=book,
            quotes=(),
            sharp_consensus=sharp_consensus,
            book_name=book_display_name(book),
        )

    implied_series = tuple(q.implied_prob for q in quotes)
    step_ip = tuple(
        quotes[i + 1].implied_prob - quotes[i].implied_prob for i in range(len(quotes) - 1)
    )
    step_am = tuple(quotes[i + 1].american - quotes[i].american for i in range(len(quotes) - 1))

    opening, closing = quotes[0], quotes[-1]
    american_delta = closing.american - opening.american
    implied_prob_delta = closing.implied_prob - opening.implied_prob
    line_delta: float | None = None
    if opening.line is not None and closing.line is not None:
        line_delta = closing.line - opening.line

    if implied_prob_delta > 0.0:
        direction = "toward"
    elif implied_prob_delta < 0.0:
        direction = "away"
    else:
        direction = "flat"

    clv: CLV | None = None
    clv_basis: str | None = None
    clv_note: str | None = None
    run_line_shape: str | None = None
    if market_type == "runline":
        shapes = {s for s in (_quote_shape(q) for q in quotes) if s is not None}
        run_line_shape = shapes.pop() if len(shapes) == 1 else ("mixed" if shapes else None)
        if len(quotes) >= 2:
            clv, clv_basis, clv_note = _run_line_clv(opening, closing, reference_margin_at)
    elif (
        len(quotes) >= 2
        and opening.other_american is not None
        and closing.other_american is not None
    ):
        clv = clv_from_odds(
            entry_side_american=opening.american,
            entry_other_american=opening.other_american,
            close_side_american=closing.american,
            close_other_american=closing.other_american,
        )
        clv_basis = "pair"

    return LineMovement(
        game_pk=int(game_pk),
        market_type=market_type,
        side=side,
        book=book,
        quotes=tuple(quotes),
        opening_american=opening.american,
        closing_american=closing.american,
        opening_implied_prob=opening.implied_prob,
        closing_implied_prob=closing.implied_prob,
        american_delta=american_delta,
        implied_prob_delta=implied_prob_delta,
        line_delta=line_delta,
        step_implied_prob_deltas=step_ip,
        step_american_deltas=step_am,
        implied_prob_series=implied_series,
        direction=direction,
        clv=clv,
        sharp_consensus=sharp_consensus,
        run_line_shape=run_line_shape,
        clv_basis=clv_basis,
        clv_note=clv_note,
        book_name=book_display_name(book),
    )


def _quote_shape(q: LineQuote) -> str | None:
    """'pair' or 'two_bets' for one run-line quote; None when a spread is missing."""
    if q.line is None or q.other_line is None:
        return None
    return "pair" if q.is_run_line_pair else "two_bets"


def _run_line_clv(
    opening: LineQuote, closing: LineQuote, reference_margin_at: ReferenceMarginAt | None
) -> tuple[CLV | None, str | None, str | None]:
    """The run line's CLV (opening against close), its basis and its note.

    The CLV compares one bet at two times, so the side's spread must be the same
    at both. When both ends are pairs, each end de-vigs as one two-way bet. When
    either end is two separate bets, both ends are priced the same way: each
    price over the game's two-way margin at that time. So the CLV moves only when
    the price or the margin moves, never because the method changed."""
    if _quote_shape(opening) is None or _quote_shape(closing) is None:
        return None, None, "a spread is missing, so the bet is not known"
    if opening.line != closing.line:
        return (
            None,
            None,
            f"the line moved from {opening.line:+g} to {closing.line:+g}: not the same bet",
        )
    if opening.is_run_line_pair and closing.is_run_line_pair:
        if opening.other_american is None or closing.other_american is None:
            return None, None, "the other side's price is missing"
        clv = clv_from_odds(
            entry_side_american=opening.american,
            entry_other_american=opening.other_american,
            close_side_american=closing.american,
            close_other_american=closing.other_american,
        )
        return clv, "pair", None
    if reference_margin_at is None:
        return None, None, "two separate bets, and no reference market to remove the margin"
    entry_margin, entry_source = reference_margin_at(opening)
    close_margin, close_source = reference_margin_at(closing)
    clv = clv_from_prob(
        devig_one_sided(opening.american, entry_margin),
        devig_one_sided(closing.american, close_margin),
    )
    flat = [entry_source == "flat", close_source == "flat"]
    if all(flat):
        margin_words = "a flat 1.05 margin (no two-way market for this game)"
    elif any(flat):
        margin_words = "the game's two-way margin at one end and a flat 1.05 at the other"
    else:
        margin_words = "the game's two-way margin"
    if opening.is_run_line_pair or closing.is_run_line_pair:
        note = (
            "a pair at one end and two separate bets at the other: "
            f"both ends priced on their own, over {margin_words}"
        )
    else:
        note = f"priced as two separate bets: each price over {margin_words}"
    return clv, "two_bets", note


# ===========================================================================
# The database reader (an asyncpg connection or a test stub)
# ===========================================================================

#: Every column a series needs, for one game and one market, in ``fetched_at``
#: order (the opening first). The pure builder picks each side's columns. Only
#: the one-book ``bp:<id>`` rows: a ``consensus`` row can mix two books.
_SQL_FETCH_GAME_ODDS = f"""
    SELECT fetched_at, line_type, book, is_sharp_book, market_type,
           home_ml, away_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type = $2 AND {STORED_BOOK_FILTER_SQL}
    ORDER BY fetched_at ASC
"""

#: The same read, narrowed to one book.
_SQL_FETCH_GAME_ODDS_BOOK = f"""
    SELECT fetched_at, line_type, book, is_sharp_book, market_type,
           home_ml, away_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type = $2 AND book = $3 AND {STORED_BOOK_FILTER_SQL}
    ORDER BY fetched_at ASC
"""


#: The same game's two-way reference markets (the total and the moneyline), for
#: a run line whose rows are two separate bets. The one-book rows only.
_SQL_FETCH_REFERENCE_ODDS = f"""
    SELECT fetched_at, line_type, book, market_type, home_ml, away_ml, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type IN ('total', 'moneyline') AND {STORED_BOOK_FILTER_SQL}
    ORDER BY fetched_at ASC
"""


def _gap_seconds(a: Any, b: Any) -> float:
    """The distance in time between two ``fetched_at`` stamps (inf when one is missing)."""
    if a is None or b is None:
        return float("inf")
    d = a - b
    return abs(d.total_seconds()) if hasattr(d, "total_seconds") else abs(float(d))


def _reference_margin_reader(reference_rows: Sequence[Mapping[str, Any]]) -> ReferenceMarginAt:
    """Build ``reference_margin_at(quote)``: the book's margin on the game's total,
    then on its moneyline, from the quote's own snapshot. The quote's own book
    comes first, any book second, the flat margin last.

    The snapshot is the reference row of the quote's line type nearest the quote
    in time. The loader writes one snapshot's markets milliseconds apart
    (moneyline, run line, total), so the same snapshot's total is stamped just
    AFTER its run line; an "at or before" rule would read the previous
    snapshot's total. The accuracy comparison reads the same way: the closing
    total for a closing run line. A reference market with no row of the quote's
    line type falls back to its latest row at or before the quote."""

    def latest(market_type: str, quote: LineQuote, same_book: bool) -> Mapping[str, Any] | None:
        # The reference row for ``quote``: the nearest row of its line type, else
        # the latest row at or before it.
        rows = [
            r
            for r in reference_rows
            if r.get("market_type") == market_type
            and (not same_book or str(r.get("book", "consensus")) == quote.book)
        ]
        same_type = [r for r in rows if r.get("line_type") == quote.line_type]
        if same_type:
            if quote.fetched_at is None:
                return same_type[-1]
            # the quote's own snapshot; on a tie the earlier row (the list is time-ordered)
            return min(same_type, key=lambda r: _gap_seconds(r.get("fetched_at"), quote.fetched_at))
        best: Mapping[str, Any] | None = None
        for r in rows:
            t = r.get("fetched_at")
            if quote.fetched_at is not None and t is not None and t > quote.fetched_at:
                continue
            best = r  # rows arrive ordered by fetched_at, so the last one wins
        return best

    def at(quote: LineQuote) -> tuple[float, str]:
        for same_book in (True, False):
            total = latest("total", quote, same_book)
            moneyline = latest("moneyline", quote, same_book)
            if total is None and moneyline is None:
                continue
            candidates = [
                ("total", None, None)
                if total is None
                else ("total", total.get("over_ml"), total.get("under_ml")),
                ("moneyline", None, None)
                if moneyline is None
                else ("moneyline", moneyline.get("home_ml"), moneyline.get("away_ml")),
            ]
            margin, source = reference_margin_from_prices(candidates)
            if source != "flat":
                return margin, source
        return reference_margin_from_prices([])

    return at


def _row_to_mapping(row: Any) -> dict[str, Any]:
    """An asyncpg ``Record`` (or a stub's dict) as a dict the pure builder can read.

    ``dict(row)`` converts a ``Record``; a dict comes back as it is.
    """
    if isinstance(row, dict):
        return row
    return dict(row)


async def fetch_line_movement(
    conn: Any,
    *,
    game_pk: int,
    market_type: str,
    book: str | None = None,
) -> list[LineMovement]:
    """Read ``raw.game_odds`` and build the line-movement series per side and book.

    ``conn`` is an asyncpg connection or a test stub with an async
    ``fetch(sql, *args)``. The first query reads every one-book row
    (``bp:<id>``) of ``(game_pk, market_type)``, narrowed to ``book`` when one is
    given, in ``fetched_at`` order. On a run line with a row of two separate
    bets, a second query reads the game's total and moneyline rows for the
    margin. The rows are grouped per (side, book), and each group goes to the
    pure :func:`line_movement_from_quotes`.

    Returns one :class:`LineMovement` per (side, book) with quotes: the two sides
    of the market (HOME / AWAY on the moneyline and the run line, OVER / UNDER on
    the total) at each book. A game and market with no stored odds give ``[]``.
    Each series' :attr:`~LineMovement.sharp_consensus` says whether the flagged
    sharp rows moved that side the same way as all the rows. An unknown
    ``market_type`` raises ``ValueError``.
    """
    sides = _MARKET_SIDES.get(market_type)
    if sides is None:
        raise ValueError(
            f"unknown market_type {market_type!r} (expected one of {sorted(_MARKET_SIDES)})"
        )

    if book is None:
        rows = await conn.fetch(_SQL_FETCH_GAME_ODDS, int(game_pk), market_type)
    else:
        rows = await conn.fetch(_SQL_FETCH_GAME_ODDS_BOOK, int(game_pk), market_type, str(book))
    mappings = [_row_to_mapping(r) for r in (rows or [])]

    # A run line listed as two separate bets needs the game's two-way margin to
    # price each bet on its own. One more read, only then.
    reference_margin_at: ReferenceMarginAt | None = None
    if market_type == "runline" and any(
        (m.get("home_spread_ml") is not None or m.get("away_spread_ml") is not None)
        and m.get("home_spread") is not None
        and m.get("away_spread") is not None
        and not run_line_is_pair(m.get("home_spread"), m.get("away_spread"))
        for m in mappings
    ):
        reference_rows = await conn.fetch(_SQL_FETCH_REFERENCE_ODDS, int(game_pk))
        reference_margin_at = _reference_margin_reader(
            [_row_to_mapping(r) for r in (reference_rows or [])]
        )

    # Group rows by book so each series is one (side, book).
    by_book: dict[str, list[dict[str, Any]]] = {}
    for m in mappings:
        by_book.setdefault(str(m.get("book", "consensus")), []).append(m)

    results: list[LineMovement] = []
    for side in sides:
        # The sharp consensus of this side: did the sharp rows move it the SAME
        # way as all the rows pooled? Computed once per side.
        sharp_dir = _net_direction(mappings, market_type, side, sharp_only=True)
        overall_dir = _net_direction(mappings, market_type, side, sharp_only=False)
        if sharp_dir == "flat" or overall_dir == "flat":
            sharp_flag: bool | None = None
        else:
            sharp_flag = sharp_dir == overall_dir

        for bk, bk_rows in by_book.items():
            movement = line_movement_from_quotes(
                bk_rows,
                market_type=market_type,
                side=side,
                game_pk=int(game_pk),
                book=bk,
                sharp_consensus=sharp_flag,
                reference_margin_at=reference_margin_at,
            )
            # Keep only a series with quotes for this side.
            if movement.quotes:
                results.append(movement)
    return results


def _net_direction(
    rows: Sequence[Mapping[str, Any]],
    market_type: str,
    side: MarketSide,
    *,
    sharp_only: bool,
) -> str:
    """The net direction of a side over the pooled rows: 'toward', 'away' or 'flat'.

    It builds one series over the rows of ALL books (or of the flagged sharp
    rows only) and reads its ``direction``, so the first and the last quote can
    come from different books. No rows read 'flat'. The
    :attr:`LineMovement.sharp_consensus` flag compares the two directions.
    """
    pool = [r for r in rows if (r.get("is_sharp_book") if sharp_only else True)]
    if not pool:
        return "flat"
    movement = line_movement_from_quotes(pool, market_type=market_type, side=side)
    return movement.direction


__all__ = [
    "LineQuote",
    "LineMovement",
    "line_movement_from_quotes",
    "fetch_line_movement",
]
