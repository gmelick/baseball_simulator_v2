"""
api/routes/betting_stored_odds.py
=================================
The stored price source of the betting routes: every book's stored line for a game.

When a market has no injected price, /edges, /signals and /card-odds price it
from ``raw.game_odds``: the one-book rows (``bp:<id>``) of a sportsbook that a
bettor can use (one book per odds row, SIM-555). The usable line types depend
on the game's status in ``raw.games``:

  * a game in the ``Preview`` state (not started) reads its ``closing`` and
    ``current`` rows. The live pipeline stores every book's current line
    before first pitch, so an upcoming game prices from the day's lines;
  * any other game (live, final, or with no ``raw.games`` row) reads its
    ``closing`` rows only. The routes simulate the game from its first
    pitch, so only a pre-game price fits. A live game's current rows are
    in-play prices, and a loader run with ``--line-types current`` on a
    finished game stores a price taken after the game. The load guard
    refuses a closing row stamped after the scheduled start.

Per (market, book) the latest usable row counts, and a closing row beats any
current row of the same book. The SQL and the pure step
(:func:`_latest_pregame_rows`) both apply the rule, so a caller that passes
other rows still gets no current row for a game that is not in ``Preview``.

The live writer re-stamps a row's ``last_seen_at`` (Alembic 0029) on every
pass. A book's rows sort by that stamp, so a price that moves A -> B -> A
reads A, and a current row not seen within :data:`CURRENT_ROW_MAX_AGE_S` of
the game's newest sighting is a withdrawn price and drops out. Before 0029 the
read has no stamp: it sorts by ``fetched_at`` and keeps every row.

From the usable rows:

  * the FAIR probability de-vigs ONE book's row, the graded book: the first
    book on ``GRADED_BOOK_PREFERENCE`` with a row for the market, else the
    first other sportsbook by id. No report sets one book's price against
    another book's;
  * the OFFERED price of each side is the best price at the graded row's line
    across every stored book, the graded book's own price included (a tie goes
    to the book earlier on the list). The EV reads at that price; the edge
    (the simulator's probability minus the fair probability) does not change;
  * a run line listed as two separate bets reads the graded book's own
    two-way margin (:func:`_stored_reference_margin`).

A failed read logs a WARNING, and the markets fall back to the mock.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from betting.clv_engine import MarketSide, american_to_decimal, reference_margin_from_prices
from pipeline.odds_provider import (
    FULL_GAME_MARKET_TYPES,
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    book_id_from_label,
    is_bettable,
)
from pipeline.odds_row_guard import check_row

log = logging.getLogger("api.routes.betting")


#: The valid market_type values line-movement understands (mirrors the
#: betting.line_movement _MARKET_SIDES set / the Alembic-0003 CHECK). SIM-546:
#: the line-movement and CLV reads keep these three; /edges and /signals price
#: every game market (:data:`_EDGE_MARKET_TYPES`).
_VALID_MARKET_TYPES = ("moneyline", "runline", "total")


#: SIM-546: each segment's total of both teams (``game`` -> ``total``,
#: ``f5`` -> ``f5_total``). A run line listed as two separate bets reads its
#: reference margin from its own segment's total first.
_SEGMENT_TOTAL_OF: dict[str, str] = {
    GAME_MARKET_SEGMENT[m]: m
    for m in GAME_MARKET_TYPES
    if GAME_MARKET_KIND[m] == "total" and GAME_MARKET_SIDE[m] is None
}


# ---------------------------------------------------------------------------
# SIM-555: the stored price source -- every book's stored line for the game
# ---------------------------------------------------------------------------

#: SIM-555: the game status whose stored ``current`` rows are pre-game prices.
#: The live pipeline writes ``raw.games.status`` from the schedule's abstract
#: game state (``Preview`` / ``Live`` / ``Final``); ``Preview`` = not started.
_PREGAME_STATUS = "Preview"

#: SIM-555: the stored line types a game not yet started may be priced from.
_PREGAME_LINE_TYPES: tuple[str, ...] = ("closing", "current")

#: SIM-555: the stored line types every other game is priced from: the closing
#: line, the last pre-game price. Its ``current`` rows are in-play prices (or
#: prices taken after the game). See the module docstring, source 2.
_STARTED_LINE_TYPES: tuple[str, ...] = ("closing",)

#: SIM-555: the game's status (``$1`` = game_pk). No row = not in ``Preview``.
_SQL_GAME_STATUS = "SELECT status FROM raw.games WHERE game_pk = $1"

#: SIM-555: the latest usable row of each book for each requested game market
#: of a game: one-book rows only (``bp:<id>``), the listed sportsbooks only
#: (never the blend, a daily-fantasy app, an exchange, a prediction market or a
#: book the vocabulary does not list), the allowed line types only. ``$2`` is
#: the market list (:func:`_stored_read_markets`), ``$3`` the sportsbook labels
#: (:func:`pipeline.odds_provider.bettable_labels`), ``$4`` the allowed line
#: types (:func:`_usable_line_types`). A closing row sorts before any current
#: row of the same book. SIM-546: the read selects ``draw_ml``, the tie price
#: of a three-way market.
_SQL_STORED_GAME_ODDS = f"""
    SELECT DISTINCT ON (market_type, book)
           market_type, book, line_type, fetched_at,
           home_ml, away_ml, draw_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1
      AND market_type = ANY($2::varchar[])
      AND line_type = ANY($4::varchar[])
      AND {STORED_BOOK_FILTER_SQL}
      AND book = ANY($3::varchar[])
    ORDER BY market_type, book, (line_type = 'closing') DESC, fetched_at DESC
"""

#: SIM-519 Part G: the same read with ``last_seen_at`` (Alembic 0029). A book's
#: rows of one line type sort by when the price was last SEEN, so a price that
#: moves A -> B -> A reads A (the third pass re-stamps A's row).
_SQL_STORED_GAME_ODDS_SEEN = f"""
    SELECT DISTINCT ON (market_type, book)
           market_type, book, line_type, fetched_at, last_seen_at,
           home_ml, away_ml, draw_ml,
           home_spread, home_spread_ml, away_spread, away_spread_ml,
           total_line, over_ml, under_ml
    FROM raw.game_odds
    WHERE game_pk = $1
      AND market_type = ANY($2::varchar[])
      AND line_type = ANY($4::varchar[])
      AND {STORED_BOOK_FILTER_SQL}
      AND book = ANY($3::varchar[])
    ORDER BY market_type, book, (line_type = 'closing') DESC,
             COALESCE(last_seen_at, fetched_at) DESC
"""

#: SIM-519 Part G: a book's current row counts only while it was seen within
#: two pre-game cadences (20 minutes) of the game's newest pass. A withdrawn
#: price stops being re-stamped and falls out; a closing row always counts.
CURRENT_ROW_MAX_AGE_S = 1200

#: A market's sides as (side, price column, line column); a side with no line
#: column has ``None`` there.
SideColumns = tuple[tuple[MarketSide, str, str | None], ...]

#: SIM-546: each market KIND's sides (``pipeline.odds_provider.GAME_MARKET_KIND``).
#: A three-way market prices the tie as a third side. The yes / no market ("a
#: run in the first inning") is stored as over / under at line 0.5.
_KIND_SIDE_COLUMNS: dict[str, SideColumns] = {
    "moneyline": ((MarketSide.HOME, "home_ml", None), (MarketSide.AWAY, "away_ml", None)),
    "three_way": (
        (MarketSide.HOME, "home_ml", None),
        (MarketSide.AWAY, "away_ml", None),
        (MarketSide.DRAW, "draw_ml", None),
    ),
    "total": (
        (MarketSide.OVER, "over_ml", "total_line"),
        (MarketSide.UNDER, "under_ml", "total_line"),
    ),
    "yes_no": (
        (MarketSide.OVER, "over_ml", "total_line"),
        (MarketSide.UNDER, "under_ml", "total_line"),
    ),
    "runline": (
        (MarketSide.HOME, "home_spread_ml", "home_spread"),
        (MarketSide.AWAY, "away_spread_ml", "away_spread"),
    ),
}

#: SIM-555 / SIM-546: every game market's sides, generated from its kind. The
#: stored rows, the offered prices and the usability rule all read this one table.
_STORED_SIDE_COLUMNS: dict[str, SideColumns] = {
    m: _KIND_SIDE_COLUMNS[GAME_MARKET_KIND[m]] for m in GAME_MARKET_TYPES
}


#: SIM-555: stored rows by market, each list in the graded order (see
#: :func:`_stored_rows_by_market`).
StoredRows = Mapping[str, Sequence[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class _StoredMarket:
    """SIM-555: one market's stored prices, ready for the edge math.

    ``graded`` is ONE book's row. The fair probability de-vigs that row alone.
    ``offered`` gives each side the best price at the graded row's line across
    every stored book, as ``(American price, book label)``.
    """

    fair_book: str
    graded: Mapping[str, Any]
    offered: dict[MarketSide, tuple[float, str]]


def _graded_order(label: str) -> tuple[int, int]:
    """The sort key of a book: its place on ``GRADED_BOOK_PREFERENCE``, then its id.

    A book not on the list sorts after every book on it."""
    book_id = book_id_from_label(label)
    if book_id is None:
        return len(GRADED_BOOK_PREFERENCE) + 1, 0
    if book_id in GRADED_BOOK_PREFERENCE:
        return GRADED_BOOK_PREFERENCE.index(book_id), book_id
    return len(GRADED_BOOK_PREFERENCE), book_id


def _usable_line_types(status: str | None) -> tuple[str, ...]:
    """SIM-555 (pure): the stored line types a game in ``status`` may be priced from.

    ``Preview`` (not started): the closing and current rows. Any other status,
    or none (no ``raw.games`` row): the closing rows only."""
    return _PREGAME_LINE_TYPES if status == _PREGAME_STATUS else _STARTED_LINE_TYPES


def _latest_pregame_rows(rows: Sequence[Any], line_types: Sequence[str]) -> list[dict[str, Any]]:
    """SIM-555 (pure): each book's latest usable row of each market.

    ``line_types`` is :func:`_usable_line_types`' answer for the game. The SQL
    already returns one such row per (market, book). This repeats the rules in
    Python, so a caller that passes other rows still gets only the allowed
    line types (no ``current`` row for a game that is not in ``Preview``) and
    one row per book. A closing row beats any current row of the same book;
    between two rows of one line type, the one with the greatest
    ``fetched_at`` wins, and a row without one sorts first."""
    allowed = set(line_types)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        row = raw if isinstance(raw, dict) else dict(raw)
        if row.get("line_type") not in allowed:
            continue
        key = (str(row.get("market_type")), str(row.get("book")))
        held = latest.get(key)
        if held is None or _pregame_order(row) > _pregame_order(held):
            latest[key] = row
    return _drop_withdrawn(list(latest.values()))


def _pregame_order(row: Mapping[str, Any]) -> tuple[bool, tuple[bool, Any]]:
    """The sort key of one book's rows: a closing row first, then the newest
    sighting (SIM-519: ``last_seen_at`` when stored, else ``fetched_at``)."""
    if row.get("last_seen_at") is not None:
        return row.get("line_type") == "closing", (True, row["last_seen_at"])
    return row.get("line_type") == "closing", _fetched_order(row)


def _drop_withdrawn(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SIM-519 Part G (pure): drop each current row not seen within
    :data:`CURRENT_ROW_MAX_AGE_S` of the game's newest sighting.

    Rows without ``last_seen_at`` (before Alembic 0029) are all kept."""
    seen = [
        r["last_seen_at"]
        for r in rows
        if r.get("line_type") != "closing" and r.get("last_seen_at") is not None
    ]
    if not seen:
        return rows
    newest = max(seen)

    def fresh(r: dict[str, Any]) -> bool:
        stamp = r.get("last_seen_at")
        if r.get("line_type") == "closing" or stamp is None:
            return True
        return bool((newest - stamp).total_seconds() <= CURRENT_ROW_MAX_AGE_S)

    return [r for r in rows if fresh(r)]


def _fetched_order(row: Mapping[str, Any]) -> tuple[bool, Any]:
    """The sort key of a row's ``fetched_at``: a missing stamp sorts first."""
    fetched = row.get("fetched_at")
    return (fetched is not None, fetched)


def _stored_rows_by_market(
    rows: Sequence[Any], line_types: Sequence[str] = _STARTED_LINE_TYPES
) -> dict[str, list[dict[str, Any]]]:
    """SIM-555 (pure): the stored rows the edge math may use, by market.

    ``line_types`` is :func:`_usable_line_types`' answer for the game; the
    default is the closing line only. Only each book's latest usable row
    counts (:func:`_latest_pregame_rows`).
    That row is kept when its book is one a bettor can use, every price and
    line of its market is present, no price is 0 (no such American price) and
    the load guard (:func:`pipeline.odds_row_guard.check_row`) passes it. Each
    market's list is sorted by :func:`_graded_order`: the first row is the
    graded book's.

    SIM-546: the columns come from the market's kind
    (:data:`_STORED_SIDE_COLUMNS`). A three-way row needs its tie price too: a
    row with no ``draw_ml`` is not usable, so the graded row is the first book
    with a complete row, and with none the market falls to the mock."""
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in _latest_pregame_rows(rows, line_types):
        market = str(row.get("market_type"))
        sides = _STORED_SIDE_COLUMNS.get(market)
        label = row.get("book")
        if sides is None or not isinstance(label, str) or not is_bettable(label):
            continue
        columns = [c for _, price, line in sides for c in (price, line) if c is not None]
        if any(row.get(c) is None for c in columns):
            continue
        if any(float(row[price]) == 0.0 for _, price, _ in sides):
            continue
        if check_row(row) is not None:
            continue
        by_market.setdefault(market, []).append(row)
    for market_rows in by_market.values():
        market_rows.sort(key=lambda r: _graded_order(str(r["book"])))
    return by_market


def _stored_market(stored: StoredRows | None, market: str) -> _StoredMarket | None:
    """SIM-555 (pure): the graded row and each side's best offered price.

    ``stored`` is :func:`_stored_rows_by_market`'s output. The graded row is the
    market's first row. For each side, the offered price is the highest payout
    among the rows at the graded row's line for that side; the rows are in the
    graded order, so a tie keeps the book earlier on the list. None when the
    market has no stored row."""
    rows = list((stored or {}).get(market) or [])
    if not rows:
        return None
    graded = rows[0]
    offered: dict[MarketSide, tuple[float, str]] = {}
    for side, price_col, line_col in _STORED_SIDE_COLUMNS[market]:
        line_value = None if line_col is None else float(graded[line_col])
        best: tuple[float, float, str] | None = None
        for row in rows:
            if line_col is not None and float(row[line_col]) != line_value:
                continue
            price = float(row[price_col])
            payout = american_to_decimal(price)
            if best is None or payout > best[0]:
                best = (payout, price, str(row["book"]))
        if best is None:  # pragma: no cover -- the graded row always qualifies
            return None
        offered[side] = (best[1], best[2])
    return _StoredMarket(fair_book=str(graded["book"]), graded=graded, offered=offered)


def _reference_order(segment: str) -> tuple[tuple[str, str, str], ...]:
    """SIM-546 (pure): the two-way markets a run line listed as two separate
    bets reads its margin from, in order, as (market, price column, price
    column): the segment's own total, then the full-game total, then the
    moneyline. The full-game run line reads the total, then the moneyline. The
    accuracy comparison uses the same order (SIM-549)."""
    order: list[tuple[str, str, str]] = []
    if segment != "game":
        order.append((_SEGMENT_TOTAL_OF[segment], "over_ml", "under_ml"))
    order.append(("total", "over_ml", "under_ml"))
    order.append(("moneyline", "home_ml", "away_ml"))
    return tuple(order)


def _stored_reference_margin(
    stored: StoredRows | None, book: str, segment: str = "game"
) -> tuple[float, str]:
    """SIM-555 (pure): one book's two-way margin on the game: its total, then its
    moneyline, then the flat 1.05 (``reference_margin_from_prices``).

    SIM-546: a segment run line (``segment`` ``f1`` or ``f5``) reads the book's
    own segment total first (:func:`_reference_order`)."""

    def own(market: str) -> Mapping[str, Any] | None:
        rows = (stored or {}).get(market) or []
        return next((r for r in rows if r.get("book") == book), None)

    candidates: list[tuple[str, float | None, float | None]] = []
    for market, col_a, col_b in _reference_order(segment):
        row = own(market)
        candidates.append(
            (market, None, None) if row is None else (market, row.get(col_a), row.get(col_b))
        )
    return reference_margin_from_prices(candidates)


def _stored_read_markets(markets: Sequence[str]) -> list[str]:
    """SIM-546 (pure): the market types the stored read asks for, in the
    vocabulary's order.

    The three full-game markets always (a run line listed as two separate
    bets reads the total and the moneyline for its margin), every requested
    market, and the segment's total of a requested segment run line (its
    margin reads that first)."""
    wanted = set(FULL_GAME_MARKET_TYPES) | set(markets)
    for market in markets:
        if GAME_MARKET_KIND.get(market) == "runline":
            wanted.add(_SEGMENT_TOTAL_OF[GAME_MARKET_SEGMENT[market]])
    return [m for m in GAME_MARKET_TYPES if m in wanted]


async def _read_stored_rows(
    request: Request, game_pk: int, markets: Sequence[str] = _VALID_MARKET_TYPES
) -> dict[str, list[dict[str, Any]]]:
    """SIM-555: the game's usable stored rows by market (see :func:`_stored_rows_by_market`).

    Two reads on one connection: the game's status (:data:`_SQL_GAME_STATUS`),
    which sets the usable line types (:func:`_usable_line_types`), then each
    book's latest usable row of each market in ``markets``. Empty when the app
    has no pool, the game has no stored row, or a read fails; a failure logs a
    WARNING and the markets fall back to the mock."""
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return {}
    try:
        if getattr(pool, "acquire", None) is not None:
            async with pool.acquire() as conn:
                line_types, rows = await _fetch_stored_rows(conn, game_pk, markets)
        else:
            line_types, rows = await _fetch_stored_rows(pool, game_pk, markets)
        return _stored_rows_by_market(rows, line_types)
    except Exception as exc:  # the mock is the fallback; the route must not fail
        log.warning(
            "SIM-555: the stored odds read failed for game %s; the markets use the mock: %s",
            game_pk,
            exc,
        )
        return {}


async def _fetch_stored_rows(
    conn: Any, game_pk: int, markets: Sequence[str] = _VALID_MARKET_TYPES
) -> tuple[tuple[str, ...], list[Any]]:
    """SIM-555: (the usable line types, the stored rows) of one game, from ``conn``.

    Both reads go through ``fetch``, so a direct-connection pool and a
    connection work alike."""
    status_rows = await conn.fetch(_SQL_GAME_STATUS, int(game_pk))
    line_types = _usable_line_types(_status_of(status_rows))
    args = (int(game_pk), list(markets), bettable_labels(), list(line_types))
    try:
        rows = await conn.fetch(_SQL_STORED_GAME_ODDS_SEEN, *args)
    except Exception as exc:  # noqa: BLE001 -- before Alembic 0029: the read without the stamp
        if "last_seen_at" not in str(exc):
            raise
        rows = await conn.fetch(_SQL_STORED_GAME_ODDS, *args)
    return line_types, list(rows or [])


def _status_of(rows: Sequence[Any] | None) -> str | None:
    """SIM-555 (pure): the ``status`` of the first row, or None (no row, no status)."""
    for raw in rows or []:
        row = raw if isinstance(raw, Mapping) else dict(raw)
        status = row.get("status")
        return None if status is None else str(status)
    return None
