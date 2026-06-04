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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from betting.clv_engine import (
    CLV,
    MarketSide,
    clv_from_odds,
    implied_prob_from_american,
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
    #: closing quote.  None when the series has < 2 quotes or is missing the
    #: opposite-side price needed to de-vig either endpoint.
    clv: CLV | None = None

    #: True iff the SHARP books in this game's wider market moved this side the
    #: SAME direction as the overall movement (sharp money agrees).  Only set by
    #: :func:`fetch_line_movement` (which sees every book); None on a pure single-
    #: book series built directly via :func:`line_movement_from_quotes`.
    sharp_consensus: bool | None = None

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
    return LineQuote.from_american(
        fetched_at=row.get("fetched_at"),
        line_type=row.get("line_type", "current"),
        book=row.get("book", "consensus"),
        is_sharp_book=bool(row.get("is_sharp_book", False)),
        american=american,
        other_american=other,
        line=line_val,
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
    if (
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
    )


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
