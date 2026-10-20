"""The closing prices: the graded book's row of each market, the best price a
bettor could take, and the margin that prices a one-sided bet.

THE GRADED ROW (one graded book per market, SIM-555)
----------------------------------------------------
The store holds one row per book for each (game, market, line type): every
book's prices for every side, labelled ``book = 'bp:<id>'``. The rows labelled
``book = 'consensus'`` mix books inside one row, and no reader reads them. The
comparison grades each market against ONE book: the GRADED ROW is the row of the
first book on ``GRADED_BOOK_PREFERENCE`` that has a row for the market (the same
list on every game), and a book off the list only when no listed book has one.
Only the listed sportsbooks are graded (``pipeline.odds_provider.bettable_labels``):
never the vendor's blend, the daily-fantasy apps, the exchanges, the prediction
markets or a book the vocabulary does not list. ``--benchmark-book`` grades
against one named book instead; the blend ``bp:0`` serves as a second benchmark,
never the default.

Beside the graded row, the readers keep every sportsbook's closing row of the
market. :func:`attach_best_prices` reads them to put the best price a bettor could
take at the graded line on each record; the scoring never reads them. The plan:
``docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`` §5.5.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from betting.clv_engine import american_to_decimal, reference_margin_from_prices
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    book_id_from_label,
    book_label,
    graded_book_labels,
    graded_row_order_sql,
    is_bettable,
    resolve_book,
)

from .records import PROP_VOCAB_MAP, AccuracyRecord, run_line_market_key

# ---------------------------------------------------------------------------
# The odds readers
# ---------------------------------------------------------------------------

#: The key under which :func:`_fetch_game_odds` / :func:`_fetch_prop_odds` put
#: every sportsbook's closing row of a market, beside the graded ``opening`` /
#: ``closing`` rows: ``{market: {"closing": row, BETTABLE_CLOSING: [row, ...]}}``.
BETTABLE_CLOSING = "bettable_closing"

#: The odds columns of a ``raw.game_odds`` row, in the table's order.
_GAME_ODDS_COLUMNS = """home_ml, away_ml, draw_ml,
               home_spread, home_spread_ml, away_spread, away_spread_ml,
               total_line, over_ml, under_ml"""


def graded_book_filter_sql(param: str, *, benchmark: bool = False) -> str:
    """The WHERE tail that keeps the rows a reader may grade against.

    ``param`` names the query parameter that carries the books to keep (the
    listed sportsbooks, :func:`pipeline.odds_provider.bettable_labels`) or, with
    ``benchmark``, the one book to keep. The default form lists what may be
    graded, so a book the vocabulary does not list is never graded. Both forms
    keep the stored label filter, so a ``consensus`` row never reaches a reader.
    """
    if benchmark:
        return f"{STORED_BOOK_FILTER_SQL} AND book = {param}"
    return f"{STORED_BOOK_FILTER_SQL} AND book = ANY({param}::varchar[])"


def graded_query_args(benchmark_book: str | None = None) -> tuple[Any, ...]:
    """The parameters after the game (``$2`` on) of a graded-row query.

    The default read passes the labels to keep (``$2``, the listed sportsbooks)
    and the preference list (``$3``); a benchmark read passes its one label
    (``$2``).
    """
    if benchmark_book is not None:
        return (benchmark_book,)
    return (bettable_labels(), graded_book_labels())


def _graded_order_sql(benchmark: bool) -> str:
    """The ORDER BY tail after the keys: the graded book first, then the newest
    fetch. A benchmark read has one book, so only the fetch orders."""
    return "fetched_at DESC" if benchmark else graded_row_order_sql("$3")


#: The three-way game markets (the first-inning and first-five moneylines). The
#: tie is a priced outcome there.
THREE_WAY_MARKET_TYPES: tuple[str, ...] = tuple(
    m for m in GAME_MARKET_TYPES if GAME_MARKET_KIND[m] == "three_way"
)

#: The first ORDER BY term after the keys of a graded game-market read. It puts a
#: three-way row whose book lists no tie after every row that lists one (``false``
#: sorts before ``true``). The provider stores such a row with ``draw_ml`` empty
#: (test 12 of the one-book plan). The scorer cannot price it (the segment scorer
#: needs the tie), so the graded row is the first book on the list with a
#: COMPLETE row, and a tie-less row is graded only when no book lists the tie. The
#: market names come from the vocabulary, never from input, so they sit in the
#: SQL as literals.
INCOMPLETE_THREE_WAY_LAST_SQL = "(draw_ml IS NULL AND market_type IN ({}))".format(
    ", ".join(f"'{m}'" for m in THREE_WAY_MARKET_TYPES)
)


def graded_game_odds_sql(*, benchmark: bool = False) -> str:
    """The graded row per (market type, line type) of one game (``$1``).

    The order after the keys: a complete three-way row before a tie-less one
    (:data:`INCOMPLETE_THREE_WAY_LAST_SQL`), then the graded book, then the newest
    fetch.
    """
    return f"""
        SELECT DISTINCT ON (market_type, line_type)
               market_type, line_type, book,
               {_GAME_ODDS_COLUMNS}
        FROM raw.game_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
          AND {graded_book_filter_sql("$2", benchmark=benchmark)}
        ORDER BY market_type, line_type, {INCOMPLETE_THREE_WAY_LAST_SQL},
                 {_graded_order_sql(benchmark)}
        """


def graded_prop_odds_sql(*, benchmark: bool = False) -> str:
    """The graded row per (player, prop market, line type) of one game (``$1``)."""
    return f"""
        SELECT DISTINCT ON (player_id, prop_stat, line_type)
               player_id, prop_stat, line_type, book, line, over_ml, under_ml
        FROM raw.prop_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
          AND {graded_book_filter_sql("$2", benchmark=benchmark)}
        ORDER BY player_id, prop_stat, line_type, {_graded_order_sql(benchmark)}
        """


#: Every sportsbook's latest closing row per game market of one game (``$1``;
#: ``$2`` = the labels to keep, the listed sportsbooks).
BETTABLE_GAME_CLOSES_SQL = f"""
        SELECT DISTINCT ON (market_type, book)
               market_type, book,
               {_GAME_ODDS_COLUMNS}
        FROM raw.game_odds
        WHERE game_pk = $1 AND line_type = 'closing'
          AND {graded_book_filter_sql("$2")}
        ORDER BY market_type, book, fetched_at DESC
        """

#: Every sportsbook's latest closing row per player prop of one game.
BETTABLE_PROP_CLOSES_SQL = f"""
        SELECT DISTINCT ON (player_id, prop_stat, book)
               player_id, prop_stat, book, line, over_ml, under_ml
        FROM raw.prop_odds
        WHERE game_pk = $1 AND line_type = 'closing'
          AND {graded_book_filter_sql("$2")}
        ORDER BY player_id, prop_stat, book, fetched_at DESC
        """


def odds_row_provenance(benchmark_book: str | None = None) -> dict[str, Any]:
    """The report's stamp of the odds rows a run graded against.

    The skill table and the paired read refuse to merge or pair two reports whose
    stamps differ. A report graded against another book prices other lines. So
    does a report with no stamp: it graded the consensus rows, which mix books.
    """
    return {
        "odds_row_version": ODDS_ROW_VERSION,
        "graded_book_preference": list(GRADED_BOOK_PREFERENCE),
        "benchmark_book": benchmark_book,
    }


def resolve_benchmark_book(value: str) -> str:
    """``--benchmark-book``: a stored label or a short name → the label.

    ``'bp:0'`` → ``'bp:0'``; ``'draftkings'`` → ``'bp:12'``. An unknown name is an
    error: a benchmark that names no book would grade nothing.
    """
    book_id = resolve_book(value)
    if book_id is None:
        raise argparse.ArgumentTypeError(
            f"unknown book {value!r}: give a stored label such as bp:0 or a name such as draftkings"
        )
    return book_label(book_id)


async def fetch_graded_game_odds(
    pool: Any, game_pk: int, *, benchmark_book: str | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{market_type: {line_type: row}}``, the graded row of each game market of
    one game, opening and closing.

    Each row carries its ``book`` label beside the odds columns. A market with no
    stored book row is absent. ``benchmark_book`` (a label) grades against that
    one book instead of the preference list.
    """
    benchmark = benchmark_book is not None
    rows = await pool.fetch(
        graded_game_odds_sql(benchmark=benchmark),
        int(game_pk),
        *graded_query_args(benchmark_book),
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["market_type"]), {})[str(r["line_type"])] = dict(r)
    return out


async def fetch_graded_prop_odds(
    pool: Any, game_pk: int, *, benchmark_book: str | None = None
) -> dict[tuple[int, str], dict[str, dict[str, Any]]]:
    """``{(player_id, prop_stat): {line_type: row}}``, the graded row of each
    player prop of one game, opening and closing. The rule is
    :func:`fetch_graded_game_odds`'s."""
    benchmark = benchmark_book is not None
    rows = await pool.fetch(
        graded_prop_odds_sql(benchmark=benchmark),
        int(game_pk),
        *graded_query_args(benchmark_book),
    )
    out: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        key = (int(r["player_id"]), str(r["prop_stat"]))
        out.setdefault(key, {})[str(r["line_type"])] = dict(r)
    return out


async def fetch_bettable_game_closes(pool: Any, game_pk: int) -> dict[str, list[dict[str, Any]]]:
    """``{market_type: [row, ...]}``, every sportsbook's latest closing row of each
    game market of one game."""
    rows = await pool.fetch(BETTABLE_GAME_CLOSES_SQL, int(game_pk), bettable_labels())
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["market_type"]), []).append(dict(r))
    return out


async def fetch_bettable_prop_closes(
    pool: Any, game_pk: int
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    """``{(player_id, prop_stat): [row, ...]}``, every sportsbook's latest closing
    row of each player prop of one game."""
    rows = await pool.fetch(BETTABLE_PROP_CLOSES_SQL, int(game_pk), bettable_labels())
    out: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault((int(r["player_id"]), str(r["prop_stat"])), []).append(dict(r))
    return out


async def _fetch_game_odds(
    pool: Any, game_pk: int, *, benchmark_book: str | None = None
) -> dict[str, dict[str, Any]]:
    """``{market_type: {line_type: row_dict}}`` for one game's game odds.

    The row per (market type, line type) is the graded row
    (:func:`fetch_graded_game_odds`), not the latest row of any book. Each market
    that has one also carries, under :data:`BETTABLE_CLOSING`, every sportsbook's
    closing row (one more read per game), for :func:`attach_best_prices`. Only the
    opening and closing line types are read.
    """
    graded: dict[str, dict[str, Any]] = await fetch_graded_game_odds(
        pool, game_pk, benchmark_book=benchmark_book
    )
    if graded:
        closes = await fetch_bettable_game_closes(pool, game_pk)
        for market_type, by_lt in graded.items():
            by_lt[BETTABLE_CLOSING] = closes.get(market_type, [])
    return graded


async def _fetch_prop_odds(
    pool: Any, game_pk: int, *, benchmark_book: str | None = None
) -> dict[tuple[int, str], dict[str, Any]]:
    """``{(player_id, prop_stat): {line_type: row_dict}}`` for one game.

    The graded row per (player, prop market, line type)
    (:func:`fetch_graded_prop_odds`), with every sportsbook's closing row of the
    prop under :data:`BETTABLE_CLOSING`, as :func:`_fetch_game_odds` does.
    """
    graded: dict[tuple[int, str], dict[str, Any]] = await fetch_graded_prop_odds(
        pool, game_pk, benchmark_book=benchmark_book
    )
    if graded:
        closes = await fetch_bettable_prop_closes(pool, game_pk)
        for key, by_lt in graded.items():
            by_lt[BETTABLE_CLOSING] = closes.get(key, [])
    return graded


# ---------------------------------------------------------------------------
# The best price a bettor could take
# ---------------------------------------------------------------------------


def best_bettable_price(
    rows: Sequence[dict[str, Any]],
    price_col: str,
    *,
    line_col: str | None = None,
    line: float | None = None,
    required: Sequence[str] = (),
    preference: tuple[int, ...] = GRADED_BOOK_PREFERENCE,
) -> tuple[float, str] | None:
    """The highest-payout sportsbook price for one side (pure).

    ``rows`` are closing rows of one market, one per book. A row is a candidate
    when its book is bettable (a sportsbook), its ``price_col`` is set, its
    ``line_col`` equals ``line`` (the same bet: an over 8.5 is not an over 9), and
    every column in ``required`` is set. The payout is the decimal price. A tie
    goes to the book earlier on ``preference``, then to the lower book id.
    Returns ``(american price, book label)`` or ``None``.
    """
    rank = {book_id: i for i, book_id in enumerate(preference)}
    best: tuple[tuple[float, int, int], float, str] | None = None
    for row in rows:
        label = row.get("book")
        price = row.get(price_col)
        if not is_bettable(label) or price is None:
            continue
        if line_col is not None:
            row_line = row.get(line_col)
            if row_line is None or line is None or float(row_line) != float(line):
                continue
        if any(row.get(col) is None for col in required):
            continue
        try:
            payout = float(american_to_decimal(float(price)))
        except ValueError:
            continue  # a price of 0: no book posts one
        book_id = book_id_from_label(label)
        assert book_id is not None  # is_bettable accepts bp: labels only
        order = (-payout, rank.get(book_id, len(rank)), book_id)
        if best is None or order < best[0]:
            best = (order, float(price), str(label))
    return None if best is None else (best[1], best[2])


#: The model prop → its odds market (``PROP_VOCAB_MAP`` read backwards; the map is
#: one to one).
_PROP_STAT_OF_MODEL_PROP: dict[str, str] = {v: k for k, v in PROP_VOCAB_MAP.items()}


def _best_price_side(record: AccuracyRecord) -> tuple[str, str | None, tuple[str, ...]] | None:
    """The record's reference side as ``(price column, line column, required
    columns)``, or ``None`` for a market the comparison does not know.

    The reference side is the one the record is scored on: home on a moneyline, a
    three-way market and first to score; the home bet at the home spread on a run
    line, or the away bet at the away spread on a run line's away record; the over
    at the line on a total, a team total, a run in the first inning and a prop. A
    three-way candidate must list the tie, because a home price with no tie on
    offer may be another bet (a tie refunded, not lost).
    """
    if record.market_type == "prop":
        return ("over_ml", "line", ())
    kind = GAME_MARKET_KIND.get(record.market_type)
    if kind == "runline":
        if record.market == run_line_market_key(record.market_type, "away"):
            return ("away_spread_ml", "away_spread", ())
        return ("home_spread_ml", "home_spread", ())
    if kind in ("total", "yes_no"):
        return ("over_ml", "total_line", ())
    if kind == "three_way":
        return ("home_ml", None, ("draw_ml",))
    if kind == "moneyline":
        return ("home_ml", None, ())
    return None


def attach_best_prices(
    records: Sequence[AccuracyRecord],
    game_odds: dict[str, dict[str, Any]],
    prop_odds: dict[tuple[int, str], dict[str, Any]],
    *,
    preference: tuple[int, ...] = GRADED_BOOK_PREFERENCE,
) -> list[AccuracyRecord]:
    """Each record with its best bettable closing price (pure).

    For each record, the graded closing row of its market gives the line; the
    candidates are every sportsbook's closing row the reader kept under
    :data:`BETTABLE_CLOSING`, plus the graded row itself when its book is a
    sportsbook. :func:`best_bettable_price` picks the price. The record's scores do
    not change: only ``market_best_price`` and ``market_best_book`` are set, and a
    record with no candidate keeps them ``None``.
    """
    out: list[AccuracyRecord] = []
    for rec in records:
        side = _best_price_side(rec)
        by_lt: dict[str, Any] | None
        if rec.market_type == "prop":
            stat = _PROP_STAT_OF_MODEL_PROP.get(rec.market)
            by_lt = (
                prop_odds.get((int(rec.player_id), stat))
                if stat is not None and rec.player_id is not None
                else None
            )
        else:
            by_lt = game_odds.get(rec.market_type)
        graded = (by_lt or {}).get("closing")
        if side is None or graded is None:
            out.append(rec)
            continue
        price_col, line_col, required = side
        line = graded.get(line_col) if line_col is not None else None
        if line_col is not None and line is None:
            out.append(rec)
            continue
        candidates = list((by_lt or {}).get(BETTABLE_CLOSING) or [])
        if graded.get("book") not in {c.get("book") for c in candidates}:
            candidates.append(graded)
        best = best_bettable_price(
            candidates,
            price_col,
            line_col=line_col,
            line=line,
            required=required,
            preference=preference,
        )
        if best is None:
            out.append(rec)
            continue
        out.append(replace(rec, market_best_price=best[0], market_best_book=best[1]))
    return out


# ---------------------------------------------------------------------------
# The closing two-way price of one market
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosingPrices:
    """The closing two-way price of one market, with the closing row's own line.

    The comparison reads only the closing row: a game needs no opening row to be
    scored, and a line that moved between the open and the close is scored at its
    closing value.
    """

    side: float
    other: float
    line: float | None = None
    #: The other side's own line: the away spread on a run line. The book lists
    #: each team's run line under its own spread, and the two spreads tell a pair
    #: (home −0.5 / away +0.5) from two separate bets (home −1.5 / away −1.5).
    #: ``None`` for a market without one.
    other_line: float | None = None


def _closing_prices(
    odds: dict[str, dict[str, dict[str, Any]]],
    market_type: str,
    side_col: str,
    other_col: str,
    line_col: str | None,
    other_line_col: str | None = None,
) -> ClosingPrices | None:
    """The :class:`ClosingPrices` of a game market, from its closing row alone.

    Returns None when there is no closing row or either two-way price is NULL.
    The closing line is nearly always collected and a matched opening line is
    not, so the comparison never requires one.
    """
    by_lt = odds.get(market_type)
    if not by_lt:
        return None
    cl = by_lt.get("closing")
    if cl is None:
        return None
    side = cl.get(side_col)
    other = cl.get(other_col)
    if None in (side, other):
        return None
    line = cl.get(line_col) if line_col else None
    other_line = cl.get(other_line_col) if other_line_col else None
    return ClosingPrices(
        side=float(side),
        other=float(other),
        line=None if line is None else float(line),
        other_line=None if other_line is None else float(other_line),
    )


def _implied(american: float) -> float:
    """The raw (vig-included) implied probability of one American price."""
    from betting.clv_engine import implied_prob_from_american

    return float(implied_prob_from_american(float(american)))


# ---------------------------------------------------------------------------
# The margin that prices a one-sided bet
# ---------------------------------------------------------------------------
#
# A run line the book lists as two separate bets (see accuracy.grading) prices
# each bet from its own price over the book's margin on the same game's two-way
# markets. Here "margin" means a two-way market's two implied probabilities added
# up: about 1.05, never below 1. DEFAULT_ONE_SIDED_MARGIN (1.05, the last-resort
# margin), REFERENCE_MARGIN_BAND (1.00-1.15) and run_line_is_pair live in
# betting.clv_engine, which the live /edges and line-movement reads share.

#: The two-way total of each segment, the first reference market.
_SEGMENT_TOTAL_OF: dict[str, str] = {"game": "total", "f5": "f5_total", "f1": "f1_total"}


def _own_book_closing_row(
    odds: dict[str, dict[str, Any]], market_type: str, book: str
) -> dict[str, Any] | None:
    """``book``'s own closing row of one market, or ``None``.

    The graded closing row counts when it is that book's. Otherwise the row comes
    from the sportsbooks' closing rows the reader kept under
    :data:`BETTABLE_CLOSING`.
    """
    by_lt = odds.get(market_type) or {}
    graded = by_lt.get("closing")
    if graded is not None and graded.get("book") == book:
        return graded
    for row in by_lt.get(BETTABLE_CLOSING) or []:
        if row.get("book") == book:
            return row
    return None


def reference_margin(
    odds: dict[str, dict[str, dict[str, Any]]], segment: str, *, book: str | None = None
) -> tuple[float, str]:
    """The book's margin on the same game's two-way markets: ``(margin, source)``.

    The order is the same segment's two-way total, then the full-game total, then
    the full-game moneyline. The first market whose margin falls in
    ``REFERENCE_MARGIN_BAND`` wins; with none, ``DEFAULT_ONE_SIDED_MARGIN``
    (``source == "flat"``). A three-way market never enters: its margin prices the
    tie (1.22 on the first-five moneyline) and would under-price a one-sided bet by
    a tenth.

    ``book`` is the label of the priced run-line row. Each market's graded row is
    chosen on its own, so the graded total can be another book's. With ``book``
    set, the margin comes from THAT book's own rows (:func:`_own_book_closing_row`),
    in the same order, then the flat margin: no bet is divided by another book's
    cut. The live ``/edges`` page uses the same rule. ``book`` ``None`` (rows with no
    book label, such as a hand-built odds dict) reads the graded rows.
    """
    order = (
        (_SEGMENT_TOTAL_OF[segment], "over_ml", "under_ml"),
        ("total", "over_ml", "under_ml"),
        ("moneyline", "home_ml", "away_ml"),
    )
    candidates: list[tuple[str, float | None, float | None]] = []
    for market_type, side_col, other_col in order:
        if book is not None:
            row = _own_book_closing_row(odds, market_type, book)
            candidates.append(
                (market_type, None, None)
                if row is None
                else (market_type, row.get(side_col), row.get(other_col))
            )
            continue
        cp = _closing_prices(odds, market_type, side_col, other_col, None)
        candidates.append(
            (market_type, None, None) if cp is None else (market_type, cp.side, cp.other)
        )
    return reference_margin_from_prices(candidates)
