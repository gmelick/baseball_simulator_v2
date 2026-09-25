"""
betting/line_movement.py
========================
SIM-368 -- the **CLV / line-movement time-series surface** (Phase 5, Sprint 5,
Wave 2).  It lifts the SIM-339 :mod:`betting.clv_engine` from a SINGLE
entry-vs-close snapshot to the FULL opening -> ... -> closing line history.

WHAT THIS IS
------------
SIM-339's :func:`betting.clv_engine.clv_from_odds` answers ONE question: did the
price I ENTERED at beat the CLOSE?  It compares exactly two points -- a single
entry quote and a single closing quote.  But a market is not two points: a book
re-quotes a game many times between when the line OPENS and when it CLOSES (the
SIM-092 dedup keeps only the moments the price actually MOVED).  That ordered
sequence of quotes for one ``(game_pk, market, side, book)`` IS the
**line-movement time-series**.  This module reads that sequence and derives:

  * the **running implied-probability series** -- the de-vig-FREE single-side
    implied probability (:func:`~betting.clv_engine.implied_prob_from_american`)
    at every quote, so the caller can plot how the market's opinion drifted;
  * the **per-step deltas** -- the American-odds and implied-probability change
    between each consecutive pair of quotes (where the movement happened);
  * the **opening -> closing summary** -- total American move, total implied-prob
    move, total ``line`` move (for spread / total markets), and the DIRECTION
    (did the price steam TOWARD this side -- shorter odds / higher implied prob --
    or AWAY from it);
  * the **entry-vs-close** :class:`~betting.clv_engine.CLV` -- reusing the exact
    SIM-339 two-way de-vig + CLV math, but with "entry" defined as the FIRST
    (opening) quote and "close" the LAST (closing) quote of the series, so the
    snapshot CLV falls out of the time-series for free;
  * a **sharp-consensus** flag -- whether the SHARP books (Pinnacle / Circa,
    flagged ``is_sharp_book``) moved this side the same direction the whole
    market did, the classic "sharp money agrees" tell.

WHAT "MOVEMENT" AND "CLV OVER TIME" MEAN (the contract the API exposes)
----------------------------------------------------------------------
A quote's **implied probability** is the book's quoted single-side probability,
margin INCLUDED (one leg of a two-way market -- de-vig is a two-way operation, so
a lone series of one side cannot be de-vigged; the per-quote ``implied_prob`` is
therefore the RAW implied prob, and the de-vigged CLV is computed separately from
the two-way opening / closing pairs).  **Movement** is the change in that quote
sequence over time: positive implied-prob movement (odds shortening -- e.g.
-120 -> -150) means the market grew MORE confident in this side ("steam toward");
negative movement (odds lengthening -- e.g. -150 -> -120) means it cooled on this
side ("steam away").  **CLV over time** is that whole running implied-prob series
PLUS the canonical SIM-339 entry-vs-close CLV taken across the series endpoints:
the single CLV number is the opening->closing special case of the surface, and
the surface lets you see HOW the price got there (steady drift vs. a late steam
move) rather than just the endpoints.

SIGN CONVENTION (inherits SIM-339)
----------------------------------
We measure movement on this SIDE's implied probability.  ``implied_prob_delta``
> 0 means this side's implied probability ROSE from opening to closing (odds got
SHORTER / less generous for a backer) -- the market moved TOWARD this side.  The
embedded :class:`CLV` keeps SIM-339's sign: positive ``clv_prob`` means a bet at
the OPENING price BEAT THE CLOSE (the de-vigged probability of the side rose to
the close, i.e. opening odds were the better price).  Note these two can DISAGREE
in sign for the same series -- raw implied prob includes vig and is single-side,
de-vigged CLV strips the two-way margin -- which is exactly why the CLV is the
canonical value-bearing metric and the implied-prob series is the descriptive
surface.

DESIGN (mirrors db/sim_store.py + simulation/lineup_resolver.py)
----------------------------------------------------------------
  * **Pure derivation, isolated DB read.** :func:`line_movement_from_quotes` is
    pure: feed it a list of quotes (mappings or :class:`LineQuote`) and it sorts,
    derives, and returns a :class:`LineMovement` -- no DB, no RNG, deterministic.
    :func:`fetch_line_movement` is the ONLY DB-touching function; it takes a
    duck-typed asyncpg-or-mock ``conn``, reads ``raw.game_odds`` ordered by
    ``fetched_at``, groups the rows into per-(side, book) series, and calls the
    pure builder.  Unit-testable against a stub connection exactly like
    :mod:`db.sim_store`.
  * **Reuse clv_engine for all odds math.** Every American<->probability<->CLV
    computation goes through :mod:`betting.clv_engine`; this module owns only the
    time-series shaping.  numpy-light (plain Python arithmetic).

THE raw.game_odds TIMESTAMP COLUMN
----------------------------------
The ordering / time axis is **``fetched_at``** (``TIMESTAMPTZ NOT NULL DEFAULT
NOW()`` -- the column created in Alembic 0003).  There is no ``captured_at`` /
``updated_at`` on this table; ``fetched_at`` is the moment the snapshot was
pulled, and the SIM-092 dedup means each distinct ``fetched_at`` is a moment the
line actually moved.  Each row carries BOTH sides of its market in paired
columns (moneyline ``home_ml`` / ``away_ml``; runline ``home_spread_ml`` /
``away_spread_ml`` with ``home_spread`` / ``away_spread``; total ``over_ml`` /
``under_ml`` with ``total_line``), so a single row de-vigs as a two-way market.

A RUN LINE IS A PAIR, OR TWO SEPARATE BETS (SIM-549)
---------------------------------------------------
A run-line row is a two-way market only when the away spread is the negative of
the home spread (home −1.5 / away +1.5). The book also lists two SEPARATE bets,
such as home −1.5 / away −1.5 or home +1.5 / away +1.5. Their prices must not
be de-vigged against each other. So on a run line:

  * each quote carries the other side's own spread (``other_line``);
  * the series says whether its rows were pairs, two separate bets, or a mix
    (:attr:`LineMovement.run_line_shape`);
  * the CLV compares only the SAME bet. When the side's spread moved between
    the opening and the closing quote, the series carries no CLV. The note
    says why (:attr:`LineMovement.clv_note`);
  * when either endpoint is two separate bets, BOTH endpoints are priced the
    same way: each price over the book's margin on the game's total, then its
    moneyline, at that time (:func:`betting.clv_engine.devig_one_sided`).
    :attr:`LineMovement.clv_basis` says which way the CLV was priced.
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

# ===========================================================================
# Market -> (this-side column, other-side column, line column) mapping
# ===========================================================================
#
# raw.game_odds stores BOTH sides of every market in one row.  To build a
# one-SIDE series we need, per (market_type, side): which American-odds column is
# THIS side, which is the OTHER side (needed to de-vig the two-way pair), and the
# market's line column (None for moneyline).  market_type values are the
# Alembic-0003 CHECK set: 'moneyline' | 'runline' | 'total'.

#: (this_american_col, other_american_col, line_col_or_None) per (market, side).
_MARKET_COLUMNS: dict[tuple[str, MarketSide], tuple[str, str, str | None]] = {
    ("moneyline", MarketSide.HOME): ("home_ml", "away_ml", None),
    ("moneyline", MarketSide.AWAY): ("away_ml", "home_ml", None),
    ("runline", MarketSide.HOME): ("home_spread_ml", "away_spread_ml", "home_spread"),
    ("runline", MarketSide.AWAY): ("away_spread_ml", "home_spread_ml", "away_spread"),
    ("total", MarketSide.OVER): ("over_ml", "under_ml", "total_line"),
    ("total", MarketSide.UNDER): ("under_ml", "over_ml", "total_line"),
}

#: SIM-549: the OTHER side's own line column on a run line (the away spread for
#: the home series and the reverse). It tells a pair from two separate bets.
_OTHER_LINE_COLUMN: dict[tuple[str, MarketSide], str] = {
    ("runline", MarketSide.HOME): "away_spread",
    ("runline", MarketSide.AWAY): "home_spread",
}

#: ``reference_margin_at(quote) -> (margin, source)``: the book's margin on the
#: same game's two-way markets as of a quote (see :func:`fetch_line_movement`).
ReferenceMarginAt = Callable[["LineQuote"], tuple[float, str]]

#: Valid (market_type, side) side-pairings, so callers (and the API) can
#: enumerate what a market exposes without hard-coding the table layout.
_MARKET_SIDES: dict[str, tuple[MarketSide, MarketSide]] = {
    "moneyline": (MarketSide.HOME, MarketSide.AWAY),
    "runline": (MarketSide.HOME, MarketSide.AWAY),
    "total": (MarketSide.OVER, MarketSide.UNDER),
}


def _side_columns(market_type: str, side: MarketSide) -> tuple[str, str, str | None]:
    """The (this-side, other-side, line) game_odds columns for a market + side.

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
# LineQuote -- one timestamped snapshot of one side of a market
# ===========================================================================


@dataclass(frozen=True, slots=True)
class LineQuote:
    """One timestamped quote for a single side of one market at one book.

    A single point on the line-movement time-series.  ``american`` is THIS side's
    American price at ``fetched_at``; ``other_american`` is the opposite side's
    price at the SAME snapshot (kept so the opening / closing pair can be
    de-vigged into a fair :class:`CLV`).  ``implied_prob`` is the RAW single-side
    implied probability (margin INCLUDED) from
    :func:`~betting.clv_engine.implied_prob_from_american` -- de-vig is a two-way
    operation and a one-side series cannot be de-vigged, so the per-quote prob is
    the book's quoted (vigged) single-side probability; the de-vigged value lives
    on the :class:`CLV`.
    """

    #: When the snapshot was pulled (the raw.game_odds ``fetched_at`` column).
    #: Any ordered/comparable type (datetime, ISO str, int) -- used only to sort.
    fetched_at: Any
    #: 'opening' | 'current' | 'closing' | 'bet_placement' (the line_type CHECK).
    line_type: str
    #: The sportsbook this quote is from (raw.game_odds ``book``).
    book: str
    #: Whether ``book`` is a sharp book (Pinnacle / Circa) -- the SIM-133 flag.
    is_sharp_book: bool
    #: THIS side's American odds at this snapshot.
    american: float
    #: The OPPOSITE side's American odds at the SAME snapshot (for de-vig). May be
    #: None if the row stored only this side (then the CLV cannot be computed).
    other_american: float | None = None
    #: The market line at this snapshot (spread / total). None for a moneyline.
    line: float | None = None
    #: RAW single-side implied probability of ``american`` (margin included).
    implied_prob: float = 0.0
    #: SIM-549: the OTHER side's own line at the same snapshot (run lines only).
    other_line: float | None = None

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
        """Build a :class:`LineQuote`, computing ``implied_prob`` from ``american``.

        The convenience constructor used everywhere internally: it fills
        ``implied_prob`` via :func:`~betting.clv_engine.implied_prob_from_american`
        so callers never have to.
        """
        return LineQuote(
            fetched_at=fetched_at,
            line_type=str(line_type),
            book=str(book),
            is_sharp_book=bool(is_sharp_book),
            american=float(american),
            other_american=None if other_american is None else float(other_american),
            line=None if line is None else float(line),
            implied_prob=implied_prob_from_american(float(american)),
            other_line=None if other_line is None else float(other_line),
        )


# ===========================================================================
# LineMovement -- the ordered series + derived opening->closing summary
# ===========================================================================


@dataclass(frozen=True, slots=True)
class LineMovement:
    """The line-movement time-series for one (game_pk, market, side, book).

    ``quotes`` is the chronologically ORDERED list of :class:`LineQuote` from the
    opening through the closing line (every moment the price moved -- SIM-092
    dedup).  The remaining fields are the derived opening -> closing SUMMARY and
    the entry-vs-close :class:`CLV`.  See the module docstring for what
    "movement" / "CLV over time" mean.
    """

    game_pk: int
    market_type: str
    side: MarketSide
    book: str | None

    #: The ordered series, oldest (opening) -> newest (closing).
    quotes: tuple[LineQuote, ...]

    #: Opening / closing American odds for THIS side (first / last quote).
    opening_american: float | None = None
    closing_american: float | None = None
    #: Opening / closing RAW implied probability (first / last quote).
    opening_implied_prob: float | None = None
    closing_implied_prob: float | None = None

    #: closing_american - opening_american (American-points move; sign per the
    #: American convention, so prefer implied_prob_delta for monotone reasoning).
    american_delta: float | None = None
    #: closing_implied_prob - opening_implied_prob.  > 0 => odds shortened, the
    #: market moved TOWARD this side ("steam toward"); < 0 => moved away.
    implied_prob_delta: float | None = None
    #: closing line - opening line (spread / total markets; None for moneyline).
    line_delta: float | None = None

    #: Per-step implied-prob changes between consecutive quotes.  Length ==
    #: len(quotes) - 1 (empty for a single quote -- no movement).
    step_implied_prob_deltas: tuple[float, ...] = ()
    #: Per-step American-odds changes between consecutive quotes (same length).
    step_american_deltas: tuple[float, ...] = ()
    #: The running RAW implied-probability series == [q.implied_prob for q in
    #: quotes]; the plottable "CLV over time" surface.
    implied_prob_series: tuple[float, ...] = ()

    #: 'toward' | 'away' | 'flat' -- direction the line steamed for THIS side over
    #: the whole opening->closing window (sign of implied_prob_delta).
    direction: str = "flat"

    #: The SIM-339 entry-vs-close CLV with entry == opening quote, close ==
    #: closing quote.  None when the series has < 2 quotes or an endpoint cannot
    #: be priced. On a run line it is also None when the side's spread moved
    #: (see ``clv_note``).
    clv: CLV | None = None

    #: True iff the SHARP books in this game's wider market moved this side the
    #: SAME direction as the overall movement (sharp money agrees).  Only set by
    #: :func:`fetch_line_movement` (which sees every book); None on a pure single-
    #: book series built directly via :func:`line_movement_from_quotes`.
    sharp_consensus: bool | None = None

    #: SIM-549, run lines only: 'pair' (every row the two sides of one bet),
    #: 'two_bets' (every row two separate bets) or 'mixed'. A row with a
    #: missing spread does not count. None elsewhere.
    run_line_shape: str | None = None
    #: How the CLV was priced. 'pair': both endpoints de-vigged as one two-way
    #: bet. 'two_bets': an endpoint was two separate bets, so both endpoints
    #: are priced on their own price over the game's two-way margin. None when
    #: there is no CLV.
    clv_basis: str | None = None
    #: Run lines only: why there is no CLV, or a caveat on it, in plain words.
    clv_note: str | None = None

    @property
    def has_movement(self) -> bool:
        """True iff the line moved at all (>= 2 quotes and a non-zero net delta)."""
        return len(self.quotes) >= 2 and bool(self.implied_prob_delta)

    @property
    def beat_close(self) -> bool:
        """True iff a bet at the OPENING price beat the close (clv.beat_close)."""
        return self.clv is not None and self.clv.beat_close


# ===========================================================================
# Pure derivation
# ===========================================================================


def _coerce_quote(
    row: Mapping[str, Any] | LineQuote,
    *,
    market_type: str,
    side: MarketSide,
) -> LineQuote:
    """Coerce one raw row (mapping) or a :class:`LineQuote` into a LineQuote.

    A :class:`LineQuote` is returned as-is.  A mapping is read against the
    ``raw.game_odds`` column layout for ``(market_type, side)``: the this-side /
    other-side American columns + the line column, plus ``fetched_at`` /
    ``line_type`` / ``book`` / ``is_sharp_book``.  Rows whose THIS-side American
    price is NULL are signalled by raising ``ValueError`` (the caller skips them).
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
    """Order quotes by ``fetched_at``, with None timestamps sorted first.

    Returns a (has-timestamp, value) tuple so ``None`` fetched_at values (e.g. a
    hand-built quote that omitted the time) sort BEFORE timestamped ones rather
    than raising on a None-vs-datetime comparison.
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
    """PURE: build a :class:`LineMovement` from an (unordered) quote sequence.

    ``rows`` is a sequence of either :class:`LineQuote` objects or raw
    ``raw.game_odds`` row mappings (read against the ``(market_type, side)``
    column layout).  The steps:

      1. coerce + drop rows with a NULL this-side price;
      2. SORT chronologically by ``fetched_at`` (None-time quotes first);
      3. compute the running RAW implied-prob series + the per-step American /
         implied-prob deltas (length == n-1; empty for a single quote);
      4. summarise opening vs closing (American / implied-prob / line deltas +
         the steam ``direction``);
      5. build the SIM-339 entry-vs-close :class:`CLV` from the opening and
         closing quotes' two-way pairs (via
         :func:`~betting.clv_engine.clv_from_odds`) when both endpoints carry the
         opposite-side price; otherwise ``clv`` is None.

    Edge cases handled: an EMPTY input -> an empty movement (no deltas, no CLV); a
    SINGLE quote -> no movement (empty deltas, opening == closing, ``direction``
    "flat", ``clv`` None); a missing opening or closing opposite-side price ->
    ``clv`` None but the series / deltas are still computed.

    ``game_pk`` / ``book`` / ``sharp_consensus`` are carried onto the result for
    provenance; :func:`fetch_line_movement` supplies real values, direct callers
    may leave the defaults.

    SIM-549, a run line: the CLV needs the SAME bet at both endpoints. A side
    whose spread moved between them gets no CLV. When either endpoint is two
    separate bets, both endpoints are priced over ``reference_margin_at`` (the
    game's two-way margin at that quote). Without it, there is no CLV. The
    result carries :attr:`LineMovement.run_line_shape`, ``clv_basis`` and
    ``clv_note``.
    """
    quotes: list[LineQuote] = []
    for r in rows:
        try:
            quotes.append(_coerce_quote(r, market_type=market_type, side=side))
        except ValueError:
            # NULL this-side price for this market/side -- not part of the series.
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
    )


def _quote_shape(q: LineQuote) -> str | None:
    """'pair' or 'two_bets' for one run-line quote; None when a spread is missing."""
    if q.line is None or q.other_line is None:
        return None
    return "pair" if q.is_run_line_pair else "two_bets"


def _run_line_clv(
    opening: LineQuote, closing: LineQuote, reference_margin_at: ReferenceMarginAt | None
) -> tuple[CLV | None, str | None, str | None]:
    """SIM-549: the run line's entry-vs-close CLV, its basis and its note.

    The CLV compares one bet at two times, so the side's spread must be the same
    at both. Two pairs de-vig as before. When either endpoint is two separate
    bets, both endpoints are priced the same way: each price over the game's
    two-way margin at that time. So the CLV moves only when the price or the
    margin moves, never because the method changed."""
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
# DB reader (duck-typed asyncpg-or-mock conn) -- isolated, mockable
# ===========================================================================

#: All columns a line-movement series needs, ordered by the time axis. We pull
#: every market's side columns in one read and let the pure builder slice per
#: (market, side). ORDER BY fetched_at ASC == opening -> closing.
_SQL_FETCH_GAME_ODDS = """
    SELECT fetched_at, line_type, book, is_sharp_book, market_type,
           home_ml, away_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type = $2
    ORDER BY fetched_at ASC
"""

#: Same, restricted to a single book (book filter appended).
_SQL_FETCH_GAME_ODDS_BOOK = """
    SELECT fetched_at, line_type, book, is_sharp_book, market_type,
           home_ml, away_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type = $2 AND book = $3
    ORDER BY fetched_at ASC
"""


#: SIM-549: the same game's two-way reference markets, for a run line whose
#: rows are two separate bets.
_SQL_FETCH_REFERENCE_ODDS = """
    SELECT fetched_at, line_type, book, market_type, home_ml, away_ml, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1 AND market_type IN ('total', 'moneyline')
    ORDER BY fetched_at ASC
"""


def _gap_seconds(a: Any, b: Any) -> float:
    """The distance in time between two ``fetched_at`` stamps (inf when one is missing)."""
    if a is None or b is None:
        return float("inf")
    d = a - b
    return abs(d.total_seconds()) if hasattr(d, "total_seconds") else abs(float(d))


def _reference_margin_reader(reference_rows: Sequence[Mapping[str, Any]]) -> ReferenceMarginAt:
    """``reference_margin_at(quote)``: the book's margin on the game's total,
    then its moneyline, from the quote's own snapshot — the quote's own book
    first, any book second, the flat margin last.

    The snapshot is the reference row of the quote's line type nearest the
    quote in time. The loader writes one snapshot's markets milliseconds apart
    (moneyline, run line, total), so the same snapshot's total is stamped just
    AFTER its run line; an "at or before" rule would read the previous
    snapshot's total. This matches the accuracy comparison, which reads the
    closing total for a closing run line. A reference market with no row of the
    quote's line type falls back to its latest row at or before the quote."""

    def latest(market_type: str, quote: LineQuote, same_book: bool) -> Mapping[str, Any] | None:
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
    """Normalize an asyncpg Record (or a plain dict from a stub) to a dict.

    asyncpg ``Record`` objects support ``dict(row)``; a stub may already hand
    back a dict.  Either way we return a real dict the pure coercer can ``.get``.
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
    """Read ``raw.game_odds`` and build the per-side line-movement series.

    Duck-typed ``conn`` (a real asyncpg connection or a test stub exposing an
    async ``fetch(sql, *args)``), exactly like the :mod:`db.sim_store` readers.
    Issues ONE query: every ``raw.game_odds`` row for ``(game_pk, market_type)``
    (optionally narrowed to one ``book``), ordered by ``fetched_at`` ascending
    (opening -> closing).  The rows are then grouped per (side, book) and each
    group is handed to the pure :func:`line_movement_from_quotes`.

    Returns one :class:`LineMovement` per (side, book) present in the data
    (markets expose two sides -- moneyline HOME/AWAY, runline HOME/AWAY, total
    OVER/UNDER -- across one or many books).  An empty result for a game/market
    with no stored odds.  Each result's :attr:`~LineMovement.sharp_consensus` is
    set from whether the SHARP books moved that side the same way the whole
    market did.
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

    # SIM-549: a run line listed as two separate bets needs the game's two-way
    # margin to price each bet on its own. One more read, only then.
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
        # Sharp consensus for this side: did the sharp books move it the SAME way
        # the whole market (all books pooled) did?  Computed once per side.
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
            # Only emit a series that actually has quotes for this side.
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
    """Net opening->closing steam direction for a side across the pooled rows.

    Builds a temporary movement over ALL books (or only the sharp ones) so we can
    read its ``direction``.  Used to derive the :attr:`LineMovement.sharp_consensus`
    flag: 'toward' / 'away' / 'flat'.
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
