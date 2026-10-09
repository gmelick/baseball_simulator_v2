"""
api/routes/betting.py
=====================
Phase-5 Betting API surface (Sprint 5, Wave 3).

Routes (prefix ``/api/betting``):

    GET  /api/betting/games/{game_pk}/edges          (SIM-367/SIM-339)
        run (or reuse) a Monte-Carlo sim for the game -> build the per-market
        EdgeReports from the GameSimSummary + the market odds -> return them as
        a list of EdgeReportModel. SIM-546: the fifteen game markets, the
        three full-game markets (moneyline / total / run-line) and the twelve
        segment and team markets (see "THE FIFTEEN GAME MARKETS" below).

    GET  /api/betting/games/{game_pk}/signals        (SIM-369)
        build the same EdgeReports, then run them through
        bet_signals_from_edges(...) and return the ranked +EV BetSignalModel list
        (EV descending). min_edge / kelly_fraction are tunable via query params.

    GET  /api/betting/games/{game_pk}/line-movement  (SIM-368)
        fetch_line_movement(conn, ...) from app.state.pg_pool ->
        LineMovementModel[] (the opening->closing time-series per side/book).
        503 if no pool; numpy-free.

    GET  /api/betting/games/{game_pk}/clv            (SIM-339/SIM-368)
        a thin snapshot: the entry-vs-close CLV of every line-movement series for
        a market (a convenience projection of /line-movement's clv field).

DESIGN -- thin handlers over the Wave-1/2 betting modules
---------------------------------------------------------
This router is intentionally thin and mirrors ``api/routes/games.py``: it reads
resources off ``request.app.state`` (the asyncpg pool), delegates the edge math
to :mod:`betting.clv_engine` (SIM-367 ``run_line_edge_report`` + the existing
moneyline / total reports), bet selection to :mod:`betting.bet_signal` (SIM-369),
and the line-movement time-series to :mod:`betting.line_movement` (SIM-368), and
JSON serialization to :mod:`api.schemas` (EdgeReportModel / BetSignalModel /
LineMovementModel) -- so NO numpy reaches the wire and this file owns only the
HTTP contract.

THE SIM SUMMARY FOR THE EDGE MATH (reuse of the SIM-355 sim seam)
-----------------------------------------------------------------
The edge / signal endpoints price markets off a :class:`GameSimSummary` -- they
need the per-iteration ``home_scores`` / ``away_scores`` / ``total_scores`` arrays
(the run-line cover math reads the score margin, the total math reads the totals
array). We obtain it by REUSING the SIM-355 sim machinery directly: resolve the
game's lineup into a GameState, build a :class:`GameSpec`, and run the
:class:`BatchRunner` (the SAME factory-ref + caching seams as ``/api/games/.../
simulate``). The runner's own SimCache memoizes the summary keyed on (spec + seed
+ N), so a /edges call right after a /simulate (or a /signals right after /edges)
at the same seed/N hits the cached summary rather than re-running the batch.

HOW ODDS ARE SOURCED (best-effort, injectable)
----------------------------------------------
Live market odds are not guaranteed available in every environment, so the edge /
signal endpoints take a layered approach, in precedence:

  1. INJECTED query params -- a caller can pass the exact American prices
     (home_ml/away_ml, over_ml/under_ml, home_rl_ml/away_rl_ml) + lines
     (total_line, run_line, away_run_line) and the endpoint prices against those. This makes the
     endpoint exercisable with real lines from any source and keeps the math
     deterministic / testable. A market with ANY injected param is priced this
     way; its other prices come from the mock, as before.
  2. the STORED lines (SIM-555) -- when a market has no injected param and
     ``raw.game_odds`` holds one-book rows (``bp:<id>``) of a book a bettor can
     use for the game, the endpoint prices from each book's latest usable row.
     The usable line types depend on the game's status in ``raw.games``:

       * a game in the ``Preview`` state (not started) reads its ``closing``
         and ``current`` rows. The live pipeline stores every book's current
         line before first pitch (its pre-game cycle), so an upcoming game is
         priced from the day's stored lines;
       * any other game (live, final, or with no ``raw.games`` row) reads its
         ``closing`` rows only. The endpoint simulates the game from its first
         pitch, so only a pre-game price fits the simulation. Once the game is
         live, the pipeline's current rows are in-play prices, and a loader
         run with ``--line-types current`` on a finished game stores a price
         taken after the game. A closing row is the last pre-game price, and
         the load guard refuses one stamped after the scheduled start.

     Per (market, book) the latest usable row counts, and a closing row beats
     any current row of the same book. The rule sits in the SQL AND in the pure
     step (:func:`_latest_pregame_rows`), so a caller that passes other rows
     still gets no current row for a game that is not in ``Preview``.

     Two limits stay on the pre-game current rows. Nothing records that a
     book still offers a price: the writers drop a row whose prices match an
     earlier row of the same book (the dedup hash has no time in it), and a
     book that stops quoting writes no row at all. So (a) a current price
     that goes A -> B -> A leaves B as the book's latest stored row, and (b)
     a book that withdraws its line keeps its last row as "latest" with no
     time bound. Either stale row can be offered as a side's best price, and
     when its book is the graded book, the fair probability comes from it
     too. The fix is a "last seen" stamp the live writer refreshes on every
     pass, with the read keeping only rows seen in the latest pass: a
     migration, left to the live-slate work (SIM-519). A closing row does not
     have this problem: the loader writes a book's closing row once per
     game, and a re-load adds a row only when the vendor's closing price
     changed (the newest row wins).

     From the usable rows:

       * the FAIR probability de-vigs ONE book's row, the graded book: the
         first book on ``GRADED_BOOK_PREFERENCE`` with a row for the market,
         else the first other sportsbook by id. No report sets one book's
         price against another book's;
       * the OFFERED price of each side is the best price at the graded row's
         line across every stored book, the graded book's own price included (a
         tie goes to the book earlier on the list). The EV is read at that
         price; the edge (the simulator's probability minus the fair
         probability) does not change;
       * a run line listed as two separate bets reads the graded book's own
         two-way margin: its total, then its moneyline, then a flat 1.05.

     The response names the book of each side's offered price
     (``price_book`` on the report) and the graded book of each market
     (``fair_book``).
  3. the MOCK odds provider -- otherwise the endpoint falls back to
     ``MockOddsAPI.get_odds(game_pk, market_type=...)`` (the SIM-132/133
     deterministic mock used by the live pipeline), so every market is always
     priceable even with no live feed wired. A failed read of the stored lines
     logs a WARNING and falls back here too.

The response flags ``odds_source`` per market ("injected" | "stored" | "mock")
so a consumer knows which.

Line-movement is different: it reads the persisted ``raw.game_odds`` history from
the Postgres pool (the real opening->closing series), so it is pool-backed (503
without a pool) -- there is no synthetic fallback for a time-series that only the
DB has.

A RUN LINE IS A PAIR, OR TWO SEPARATE BETS (SIM-549)
---------------------------------------------------
Each team's run line has its own spread. The two are the two sides of ONE bet
only when the away spread is the negative of the home spread (home -1.5 / away
+1.5). Then the two prices de-vig against each other, as before. Otherwise the
book listed two SEPARATE bets, such as home -1.5 / away -1.5. /edges prices each
bet from its own price over the book's margin. The margin comes from the game's
total, then its moneyline, then a flat 1.05 (``betting.clv_engine.devig_one_sided``).
Those reference prices are the injected over_ml / under_ml and home_ml / away_ml,
else the mock's. So an injected run line can rest on the mock's total.

Every run-line report carries its OWN side's spread in ``line``. The away side
used to carry the home spread. ``away_run_line`` injects the away spread. Left
out, it mirrors an injected ``run_line`` (a pair) or takes the provider's. The
response's ``run_line_pricing`` says which shape was priced. The line-movement and
CLV reads apply the same rule per quote. They compare only the SAME bet (see
``betting.line_movement``).

THE FIFTEEN GAME MARKETS (SIM-546)
---------------------------------
/edges and /signals price every market in ``pipeline.odds_provider.GAME_MARKET_TYPES``
by default; ``?markets=`` narrows. The three full-game markets are priced as
above and keep their labels (``moneyline``, ``total``, ``run_line``). The twelve
segment and team markets (the first-inning and first-five moneyline, total and
run line; each team's full-game and first-five total; the first team to score;
a run in the first inning) are priced from the summary's inning grid
(``simulation.game_market_distributions``). Each of their reports carries its
market type as its label. One table, generated from the market's kind, names
each market's sides and columns (:data:`_STORED_SIDE_COLUMNS`). A three-way
market (the first-inning and first-five moneylines) has a third side, the tie
(``draw``); its three prices de-vig together. A segment run line listed as two
separate bets reads its margin from the segment's own total first. A cached
summary with no inning grid (one from before the change) prices the
full-game markets only. The response names each market (``market_names``) and
says how every run line was priced (``run_line_pricing_by_label``). None of the
twelve probabilities is calibrated.

A caller injects prices for any of the fifteen through one query param,
``prices``: a JSON document keyed by market type (:func:`_parse_prices`). A
market named there is priced from it alone (``injected``). A market both in the
document and in the named params is a 422, like every other fault in it.

Owner: Backend Developer + Betting Analyst (SIM-367 / SIM-368 / SIM-369).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes._common import _get_pool
from api.routes.games import (
    _build_runner,
    _resolve_state_or_error,
    _resolved_sim_kwargs,
    _run_batch,
    resolve_factory_ref,
)
from api.schemas import (
    BetSignalModel,
    EdgeReportModel,
    LineMovementModel,
)
from betting.bet_signal import BetSignalConfig, bet_signals_from_edges

# SIM-546, the segment markets: this import names betting/ code new with them
# (the three-way builder, the samples builder, the tie side MarketSide.DRAW).
# The app imports betting/ from its image, so rebuild the image BEFORE this
# file lands in the running app (the deploy order of the design's section 8).
from betting.clv_engine import (
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    _build_edge_report,
    american_to_decimal,
    devig_one_sided,
    expected_value,
    moneyline_edge_report,
    one_sided_edge_report,
    reference_margin_from_prices,
    run_line_bet_cover_prob,
    run_line_edge_report,
    run_line_is_pair,
    samples_over_under_edge_report,
    three_way_edge_report,
    total_over_under_edge_report,
)
from betting.line_movement import fetch_line_movement

# SIM-555: the book vocabulary and the guard come from pipeline/, which the app
# bind-mounts. The app imports betting/ from its image, so a new betting/ name
# imported here would break the running app until the image is rebuilt.
from pipeline.odds_provider import (
    FULL_GAME_MARKET_TYPES,
    GAME_MARKET_KIND,
    GAME_MARKET_NAMES,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    book_display_name,
    book_id_from_label,
    is_bettable,
)
from pipeline.odds_row_guard import check_row
from simulation.batch_runner import GameSpec
from simulation.game_market_distributions import (
    SegmentRuns,
    segment_runs_from_summary,
    side_probabilities,
)
from simulation.win_probability import IDENTITY_CALIBRATION, WinProbability, win_probability

log = logging.getLogger("api.routes.betting")

router = APIRouter(prefix="/api/betting", tags=["betting"])

#: The valid market_type values line-movement understands (mirrors the
#: betting.line_movement _MARKET_SIDES set / the Alembic-0003 CHECK). SIM-546:
#: the line-movement and CLV reads keep these three; /edges and /signals price
#: every game market (:data:`_EDGE_MARKET_TYPES`).
_VALID_MARKET_TYPES = ("moneyline", "runline", "total")

#: SIM-546: the markets /edges and /signals price — every game market the book
#: posts, in the vocabulary's order. The default request prices all fifteen.
_EDGE_MARKET_TYPES: tuple[str, ...] = GAME_MARKET_TYPES

#: SIM-546: the report label of each full-game market. A segment or team
#: market's label is its market type; the three full-game labels predate it.
_FULL_GAME_LABELS: dict[str, str] = {
    "moneyline": "moneyline",
    "total": "total",
    "runline": "run_line",
}

#: SIM-546: each segment's total of both teams (``game`` -> ``total``,
#: ``f5`` -> ``f5_total``). A run line listed as two separate bets reads its
#: reference margin from its own segment's total first.
_SEGMENT_TOTAL_OF: dict[str, str] = {
    GAME_MARKET_SEGMENT[m]: m
    for m in GAME_MARKET_TYPES
    if GAME_MARKET_KIND[m] == "total" and GAME_MARKET_SIDE[m] is None
}


def _report_label(market: str) -> str:
    """SIM-546: the label a market's reports carry (``runline`` -> ``run_line``)."""
    return _FULL_GAME_LABELS.get(market, market)


# ---------------------------------------------------------------------------
# Odds sourcing -- injected query params else the deterministic mock provider
# ---------------------------------------------------------------------------


def _mock_odds(game_pk: int, market_type: str) -> dict[str, Any]:
    """The deterministic mock odds dict for a game + market_type.

    Lazily imports :class:`MockOddsAPI` from the live pipeline (a heavy module) so
    this router stays light to import; the mock is RNG-seeded on ``game_pk`` so it
    is deterministic and needs no live feed. Used as the per-market fallback when
    the caller did not inject that market's prices.
    """
    from pipeline.live.live_ingestion_pipeline import MockOddsAPI

    return MockOddsAPI.get_odds(int(game_pk), market_type=market_type)


def _resolve_price(injected: float | None, mock: dict[str, Any], key: str) -> tuple[float, bool]:
    """Pick a price: the injected value if given, else the mock's ``key``.

    Returns ``(price, was_injected)`` so the caller can flag the odds source.
    """
    if injected is not None:
        return float(injected), True
    return float(mock[key]), False


def _safe_report(reports: list[EdgeReport], builder: Any) -> None:
    """Build one EdgeReport and append it, SKIPPING a degenerate sim probability.

    The betting math (``prob_to_american``) requires a sim probability STRICTLY in
    (0, 1): a side whose simulated cover/over probability is exactly 0.0 or 1.0
    (which a small-N batch or an extreme line legitimately produces) raises
    ``ValueError`` deep in the report build. Such a side carries NO priceable edge
    (a 0%/100% model probability is not a tradeable market opinion), so we skip it
    rather than 500 the whole endpoint -- the other sides / markets still return.
    The market is still flagged in ``odds_source`` (the price WAS sourced); only
    the un-priceable report row is omitted.
    """
    try:
        reports.append(builder())
    except ValueError as exc:  # degenerate sim_prob (0/1) -> no priceable edge
        log.info("skipping degenerate edge report: %s", exc)


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

#: SIM-546: the fields of each market kind in the ``prices`` document, and the
#: stored-row column each one fills. Every field is required.
_DOC_FIELDS: dict[str, dict[str, str]] = {
    "moneyline": {"home_ml": "home_ml", "away_ml": "away_ml"},
    "three_way": {"home_ml": "home_ml", "away_ml": "away_ml", "draw_ml": "draw_ml"},
    "total": {"over_ml": "over_ml", "under_ml": "under_ml", "line": "total_line"},
    "yes_no": {"over_ml": "over_ml", "under_ml": "under_ml"},
    "runline": {
        "home_ml": "home_spread_ml",
        "away_ml": "away_spread_ml",
        "home_line": "home_spread",
        "away_line": "away_spread",
    },
}

#: SIM-546: the fields a kind accepts but does not need. The yes / no market
#: takes a ``line`` and ignores it: the market is always over / under 0.5.
_DOC_OPTIONAL_FIELDS: dict[str, frozenset[str]] = {"yes_no": frozenset({"line"})}

#: SIM-546: the line a yes / no market is priced at, whatever a row says.
_YES_NO_LINE = 0.5

#: SIM-546: the named query param each ``prices`` field of a full-game market
#: stands for. A full-game market in the document is priced through them.
_NAMED_PARAM_OF: dict[str, dict[str, str]] = {
    "moneyline": {"home_ml": "home_ml", "away_ml": "away_ml"},
    "total": {"over_ml": "over_ml", "under_ml": "under_ml", "line": "total_line"},
    "runline": {
        "home_ml": "home_rl_ml",
        "away_ml": "away_rl_ml",
        "home_line": "run_line",
        "away_line": "away_run_line",
    },
}

#: SIM-555: the injected query params of each market. A market with any of them
#: is priced from the injected values (and the mock for the rest). SIM-546: a
#: segment or team market has no named param; the ``prices`` document injects it.
_INJECTED_PARAMS: dict[str, tuple[str, ...]] = {
    m: tuple(_NAMED_PARAM_OF.get(m, {}).values()) for m in GAME_MARKET_TYPES
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
    return list(latest.values())


def _pregame_order(row: Mapping[str, Any]) -> tuple[bool, tuple[bool, Any]]:
    """The sort key of one book's rows: a closing row first, then the newest fetch."""
    return row.get("line_type") == "closing", _fetched_order(row)


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


def _at_offered_price(report: EdgeReport, offered: tuple[float, str] | None) -> EdgeReport:
    """SIM-555: the report read at the offered price.

    The EV moves to the offered price; the fair probability and the edge stay
    the graded book's. ``offered`` None (an injected or mock price) leaves the
    report as it is."""
    if offered is None:
        return report
    price = float(offered[0])
    return replace(report, offered_american=price, ev=expected_value(report.sim_prob, price))


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
    rows = await conn.fetch(_SQL_STORED_GAME_ODDS, *args)
    return line_types, list(rows or [])


def _status_of(rows: Sequence[Any] | None) -> str | None:
    """SIM-555 (pure): the ``status`` of the first row, or None (no row, no status)."""
    for raw in rows or []:
        row = raw if isinstance(raw, Mapping) else dict(raw)
        status = row.get("status")
        return None if status is None else str(status)
    return None


def _any_given(*values: float | None) -> bool:
    """True when the caller injected any of a market's query params."""
    return any(v is not None for v in values)


class _EdgeBuild(NamedTuple):
    """What :func:`_build_edge_reports` returns."""

    reports: list[EdgeReport]
    #: market -> "injected" | "stored" | "mock"
    odds_source: dict[str, str]
    #: SIM-549: how the run line was priced (None without the run line)
    run_line_pricing: dict[str, Any] | None
    #: SIM-555: (report label, side value) -> the stored book of the offered price
    price_book: dict[tuple[str, str], str]
    #: SIM-555: market -> the graded book whose row gave the fair probability
    fair_book: dict[str, str]
    #: SIM-546: report label -> how that run line was priced (all three run lines)
    run_line_pricing_by_label: dict[str, dict[str, Any]]


def _price_run_line(
    reports: list[EdgeReport],
    *,
    label: str,
    margin_source: Any,
    h: float,
    a: float,
    home_line: float,
    away_line: float,
    home_offered: tuple[float, str] | None,
    away_offered: tuple[float, str] | None,
    reference: Callable[[], tuple[float, str]],
) -> dict[str, Any]:
    """Price both sides of one run line and return how it was priced.

    ``margin_source`` is the summary (the full-game run line) or a raw
    per-iteration margin array, home minus away (SIM-546: a segment's margin).
    A pair (the away spread is the negative of the home spread) de-vigs the two
    prices against each other. Two separate bets (SIM-549) price each bet from
    its own price over ``reference()``, the book's two-way margin. Every report
    carries ``label`` and its own side's spread."""
    if run_line_is_pair(home_line, away_line):
        _safe_report(
            reports,
            lambda: _at_offered_price(
                replace(
                    run_line_edge_report(
                        margin_source,
                        TwoWayMarket(
                            side=MarketSide.HOME, entry=OddsQuote(side=h, other=a, line=home_line)
                        ),
                        side=MarketSide.HOME,
                        line=home_line,
                    ),
                    label=label,
                ),
                home_offered,
            ),
        )
        # The away report prices at the mirrored home line; it carries the
        # AWAY team's own spread (it used to carry the home spread).
        _safe_report(
            reports,
            lambda: _at_offered_price(
                replace(
                    run_line_edge_report(
                        margin_source,
                        TwoWayMarket(
                            side=MarketSide.AWAY, entry=OddsQuote(side=a, other=h, line=home_line)
                        ),
                        side=MarketSide.AWAY,
                        line=home_line,
                    ),
                    line=away_line,
                    label=label,
                ),
                away_offered,
            ),
        )
        return {"shape": "pair", "home_line": home_line, "away_line": away_line}
    # Two separate bets: each from its own price over the game's two-way margin.
    margin, source = reference()
    for side, price, own_line, offered in (
        (MarketSide.HOME, h, home_line, home_offered),
        (MarketSide.AWAY, a, away_line, away_offered),
    ):
        _safe_report(
            reports,
            lambda side=side, price=price, own_line=own_line, offered=offered: (
                one_sided_edge_report(
                    label=label,
                    side=side,
                    line=own_line,
                    sim_prob=run_line_bet_cover_prob(margin_source, side, own_line),
                    offered_american=price if offered is None else offered[0],
                    fair_prob=devig_one_sided(price, margin),
                )
            ),
        )
    return {
        "shape": "two_bets",
        "home_line": home_line,
        "away_line": away_line,
        "reference_margin": margin,
        "reference_source": source,
    }


def _unstored_reference_margin(
    game_pk: int,
    segment: str,
    named: Mapping[str, float | None],
    injected: Mapping[str, Mapping[str, float]],
) -> tuple[float, str]:
    """The two-way margin of a run line NOT priced from the stored rows.

    The candidates follow :func:`_reference_order`. The full-game total and
    moneyline read the injected query params, else the mock's prices, as
    before SIM-546. A segment total reads the ``prices`` document, else the
    mock's."""
    candidates: list[tuple[str, float | None, float | None]] = []
    for market, col_a, col_b in _reference_order(segment):
        if market in FULL_GAME_MARKET_TYPES:
            mock = _mock_odds(game_pk, market)
            params = _NAMED_PARAM_OF[market]
            price_a = _resolve_price(named.get(params[col_a]), mock, col_a)[0]
            price_b = _resolve_price(named.get(params[col_b]), mock, col_b)[0]
            candidates.append((market, price_a, price_b))
        else:
            row = injected.get(market) or _mock_odds(game_pk, market)
            candidates.append((market, row.get(col_a), row.get(col_b)))
    return reference_margin_from_prices(candidates)


def _segment_quote(
    market: str,
    *,
    game_pk: int,
    stored: StoredRows | None,
    injected: Mapping[str, Mapping[str, float]],
) -> tuple[Mapping[str, Any], str, _StoredMarket | None]:
    """SIM-546: the prices of one segment or team market and where they came from.

    The ``prices`` document's entry when it names the market (no stored row
    is read for it), else the graded stored row, else the mock. Returns
    ``(row-shaped prices, odds source, the stored market or None)``."""
    if market in injected:
        return injected[market], "injected", None
    stored_market = _stored_market(stored, market)
    if stored_market is not None:
        return stored_market.graded, "stored", stored_market
    return _mock_odds(game_pk, market), "mock", None


def _price_segment_market(
    reports: list[EdgeReport],
    market: str,
    runs: SegmentRuns,
    quote: Mapping[str, Any],
    stored_market: _StoredMarket | None,
    reference: Callable[[], tuple[float, str]],
) -> dict[str, Any] | None:
    """SIM-546: price every side of one segment or team market.

    The label of each report is the market type (``f5_total``). The simulated
    probabilities come from the per-iteration segment runs
    (:mod:`simulation.game_market_distributions`); none is calibrated. Returns
    how a run line was priced, else None."""
    kind = GAME_MARKET_KIND[market]
    sides = _STORED_SIDE_COLUMNS[market]

    def offered(side: MarketSide) -> tuple[float, str] | None:
        return None if stored_market is None else stored_market.offered[side]

    if kind in ("total", "yes_no"):
        line = _YES_NO_LINE if kind == "yes_no" else float(quote["total_line"])
        over, under = float(quote["over_ml"]), float(quote["under_ml"])
        samples = runs.market_samples(market)
        for side, own, other in ((MarketSide.OVER, over, under), (MarketSide.UNDER, under, over)):
            _safe_report(
                reports,
                lambda side=side, own=own, other=other: _at_offered_price(
                    samples_over_under_edge_report(
                        samples,
                        TwoWayMarket(side=side, entry=OddsQuote(side=own, other=other, line=line)),
                        side=side,
                        label=market,
                    ),
                    offered(side),
                ),
            )
        return None
    if kind == "three_way":
        home, away, draw = (float(quote[price]) for _, price, _ in sides)
        p_home, p_away, p_draw = side_probabilities(runs, market)
        for side, _price, _line in sides:
            _safe_report(
                reports,
                lambda side=side: _at_offered_price(
                    three_way_edge_report(
                        p_home,
                        p_away,
                        p_draw,
                        label=market,
                        side=side,
                        home_ml=home,
                        away_ml=away,
                        draw_ml=draw,
                    ),
                    offered(side),
                ),
            )
        return None
    if kind == "moneyline":
        # The first team to score: a two-way side market, uncalibrated.
        home, away = float(quote["home_ml"]), float(quote["away_ml"])
        p_first_home, p_first_away, _p_nobody = side_probabilities(runs, market)
        for side, sim_p, own, other in (
            (MarketSide.HOME, p_first_home, home, away),
            (MarketSide.AWAY, p_first_away, away, home),
        ):
            _safe_report(
                reports,
                lambda side=side, sim_p=sim_p, own=own, other=other: _at_offered_price(
                    _build_edge_report(
                        label=market,
                        side=side,
                        line=None,
                        sim_prob=sim_p,
                        market=TwoWayMarket(side=side, entry=OddsQuote(side=own, other=other)),
                    ),
                    offered(side),
                ),
            )
        return None
    # A run line over the segment.
    return _price_run_line(
        reports,
        label=market,
        margin_source=runs.segment_margin(GAME_MARKET_SEGMENT[market]),
        h=float(quote["home_spread_ml"]),
        a=float(quote["away_spread_ml"]),
        home_line=float(quote["home_spread"]),
        away_line=float(quote["away_spread"]),
        home_offered=offered(MarketSide.HOME),
        away_offered=offered(MarketSide.AWAY),
        reference=reference,
    )


def _build_edge_reports(
    summary: Any,
    win_prob: WinProbability,
    *,
    game_pk: int,
    markets: tuple[str, ...],
    home_ml: float | None,
    away_ml: float | None,
    over_ml: float | None,
    under_ml: float | None,
    total_line: float | None,
    home_rl_ml: float | None,
    away_rl_ml: float | None,
    run_line: float | None,
    away_run_line: float | None = None,
    stored: StoredRows | None = None,
    injected: Mapping[str, Mapping[str, float]] | None = None,
) -> _EdgeBuild:
    """Build the EdgeReports for the requested markets off the sim + odds.

    For each requested market every side is priced:

      * **moneyline** -- HOME + AWAY off the SIM-330 :class:`WinProbability`
        (``moneyline_edge_report``), de-vigged against (home_ml, away_ml).
      * **total** -- OVER + UNDER off the SIM-327 ``total_scores`` array
        (``total_over_under_edge_report``) at ``total_line``, vs (over_ml, under_ml).
      * **runline** -- HOME + AWAY cover off the score margin, each side at its
        OWN spread (``run_line`` for home, ``away_run_line`` for away). A pair
        (the away spread is the negative of the home spread) de-vigs
        (home_rl_ml, away_rl_ml) against each other (``run_line_edge_report``).
        Two separate bets (SIM-549) price each bet from its own price over
        the book's margin: the total's, then the moneyline's, then a flat 1.05
        (``one_sided_edge_report``).
      * **the twelve segment and team markets** (SIM-546) -- off the summary's
        inning grid (:func:`_price_segment_market`), each report labelled with
        its market type. A summary with no grid skips them and logs one line.

    Prices come from the injected query params when a full-game market has
    any, else (SIM-555) the ``stored`` rows (:func:`_stored_rows_by_market`)
    when the market has one, else the mock provider. SIM-546: ``injected`` is
    the parsed ``prices`` document's segment and team markets
    (:func:`_parse_prices`); a market in it is priced from it alone. A stored
    market takes its fair probability and its lines from the graded book's
    row, and each side's offered price (and so the EV) from the best stored
    price at that line. A side whose simulated probability is degenerate (0.0
    / 1.0 -- no priceable edge) is skipped via :func:`_safe_report` rather than
    erroring the endpoint. Returns an :class:`_EdgeBuild`.
    """
    injected = injected or {}
    reports: list[EdgeReport] = []
    odds_source: dict[str, str] = {}
    price_book: dict[tuple[str, str], str] = {}
    fair_book: dict[str, str] = {}
    pricing_by_label: dict[str, dict[str, Any]] = {}
    named: dict[str, float | None] = {
        "home_ml": home_ml,
        "away_ml": away_ml,
        "over_ml": over_ml,
        "under_ml": under_ml,
        "total_line": total_line,
    }

    def note_book(label: str, side: MarketSide, offered: tuple[float, str] | None) -> None:
        if offered is not None:
            price_book[(label, side.value)] = offered[1]

    if "moneyline" in markets:
        stored_ml = None if _any_given(home_ml, away_ml) else _stored_market(stored, "moneyline")
        if stored_ml is not None:
            h = float(stored_ml.graded["home_ml"])
            a = float(stored_ml.graded["away_ml"])
            odds_source["moneyline"] = "stored"
            fair_book["moneyline"] = stored_ml.fair_book
        else:
            mock = _mock_odds(game_pk, "moneyline")
            h, h_inj = _resolve_price(home_ml, mock, "home_ml")
            a, a_inj = _resolve_price(away_ml, mock, "away_ml")
            odds_source["moneyline"] = "injected" if (h_inj or a_inj) else "mock"
        # HOME: side price = home_ml, other = away_ml.  AWAY: side = away_ml, other = home_ml.
        for side, own, other in ((MarketSide.HOME, h, a), (MarketSide.AWAY, a, h)):
            offered = None if stored_ml is None else stored_ml.offered[side]
            _safe_report(
                reports,
                lambda side=side, own=own, other=other, offered=offered: _at_offered_price(
                    moneyline_edge_report(
                        win_prob,
                        TwoWayMarket(side=side, entry=OddsQuote(side=own, other=other)),
                        side=side,
                    ),
                    offered,
                ),
            )
            note_book("moneyline", side, offered)

    if "total" in markets:
        stored_tot = (
            None if _any_given(over_ml, under_ml, total_line) else _stored_market(stored, "total")
        )
        if stored_tot is not None:
            o = float(stored_tot.graded["over_ml"])
            u = float(stored_tot.graded["under_ml"])
            line = float(stored_tot.graded["total_line"])
            odds_source["total"] = "stored"
            fair_book["total"] = stored_tot.fair_book
        else:
            mock = _mock_odds(game_pk, "total")
            o, o_inj = _resolve_price(over_ml, mock, "over_ml")
            u, u_inj = _resolve_price(under_ml, mock, "under_ml")
            line, line_inj = _resolve_price(total_line, mock, "total_line")
            odds_source["total"] = "injected" if (o_inj or u_inj or line_inj) else "mock"
        for side, own, other in ((MarketSide.OVER, o, u), (MarketSide.UNDER, u, o)):
            offered = None if stored_tot is None else stored_tot.offered[side]
            _safe_report(
                reports,
                lambda side=side, own=own, other=other, offered=offered: _at_offered_price(
                    total_over_under_edge_report(
                        summary,
                        TwoWayMarket(side=side, entry=OddsQuote(side=own, other=other, line=line)),
                        side=side,
                    ),
                    offered,
                ),
            )
            note_book("total", side, offered)

    run_line_pricing: dict[str, Any] | None = None
    if "runline" in markets:
        stored_rl = (
            None
            if _any_given(home_rl_ml, away_rl_ml, run_line, away_run_line)
            else _stored_market(stored, "runline")
        )
        if stored_rl is not None:
            h = float(stored_rl.graded["home_spread_ml"])
            a = float(stored_rl.graded["away_spread_ml"])
            eff_line = float(stored_rl.graded["home_spread"])
            away_line = float(stored_rl.graded["away_spread"])
            odds_source["runline"] = "stored"
            fair_book["runline"] = stored_rl.fair_book
        else:
            mock = _mock_odds(game_pk, "runline")
            h, h_inj = _resolve_price(home_rl_ml, mock, "home_spread_ml")
            a, a_inj = _resolve_price(away_rl_ml, mock, "away_spread_ml")
            # The HOME run line (negative == home laying runs).
            eff_line, line_inj = _resolve_price(run_line, mock, "home_spread")
            # SIM-549: the AWAY team's own spread. Injected; else the mirror of an
            # injected home line (a pair); else the provider's own away spread.
            if away_run_line is not None:
                away_line, away_inj = float(away_run_line), True
            elif line_inj or mock.get("away_spread") is None:
                away_line, away_inj = -eff_line, False
            else:
                away_line, away_inj = float(mock["away_spread"]), False
            odds_source["runline"] = (
                "injected" if (h_inj or a_inj or line_inj or away_inj) else "mock"
            )
        home_offered = None if stored_rl is None else stored_rl.offered[MarketSide.HOME]
        away_offered = None if stored_rl is None else stored_rl.offered[MarketSide.AWAY]
        note_book("run_line", MarketSide.HOME, home_offered)
        note_book("run_line", MarketSide.AWAY, away_offered)
        # A stored run line reads the graded book's own margin (SIM-555).
        full_game_reference: Callable[[], tuple[float, str]] = (
            partial(_stored_reference_margin, stored, stored_rl.fair_book)
            if stored_rl is not None
            else partial(_unstored_reference_margin, game_pk, "game", named, injected)
        )
        run_line_pricing = _price_run_line(
            reports,
            label="run_line",
            margin_source=summary,
            h=h,
            a=a,
            home_line=eff_line,
            away_line=away_line,
            home_offered=home_offered,
            away_offered=away_offered,
            reference=full_game_reference,
        )
        pricing_by_label["run_line"] = run_line_pricing

    segment_markets = [m for m in markets if m not in FULL_GAME_MARKET_TYPES]
    runs = segment_runs_from_summary(summary) if segment_markets else None
    if segment_markets and runs is None:
        log.info(
            "SIM-546: game %s: the simulation summary has no inning grid; "
            "the segment and team markets %s are not priced",
            game_pk,
            segment_markets,
        )
    for market in segment_markets if runs is not None else []:
        quote, source, stored_market = _segment_quote(
            market, game_pk=game_pk, stored=stored, injected=injected
        )
        odds_source[market] = source
        if stored_market is not None:
            fair_book[market] = stored_market.fair_book
            for side, _price, _line in _STORED_SIDE_COLUMNS[market]:
                note_book(market, side, stored_market.offered[side])
        segment = GAME_MARKET_SEGMENT[market]
        reference: Callable[[], tuple[float, str]] = (
            partial(_stored_reference_margin, stored, stored_market.fair_book, segment)
            if stored_market is not None
            else partial(_unstored_reference_margin, game_pk, segment, named, injected)
        )
        pricing = _price_segment_market(reports, market, runs, quote, stored_market, reference)
        if pricing is not None:
            pricing_by_label[market] = pricing

    return _EdgeBuild(
        reports, odds_source, run_line_pricing, price_book, fair_book, pricing_by_label
    )


def _parse_prices(
    raw: str | None, named: Mapping[str, float | None]
) -> dict[str, dict[str, float]]:
    """SIM-546 (pure): the ``prices`` document, validated, as market -> row-shaped prices.

    The document is a JSON object keyed by market type; each entry holds the
    fields its kind needs (:data:`_DOC_FIELDS`). The returned entry uses the
    stored-row column names (``total_line``, ``home_spread_ml``, ...), so the
    pricing code reads a document entry like a stored row. A yes / no market
    takes an optional ``line`` and is priced at 0.5 whatever it says.

    A 422 names the market and the fault: a document that is not a JSON
    object, an unknown market, an entry that is not an object, an unknown
    field, a missing field, a field that is not a finite number, an American
    price of 0, or a market also given by the named query params (``named``).
    """
    if raw is None or not raw.strip():
        return {}
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise _prices_error(f"prices: the document is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise _prices_error("prices: the document must be a JSON object keyed by market type")
    parsed: dict[str, dict[str, float]] = {}
    for market, entry in doc.items():
        if market not in GAME_MARKET_KIND:
            raise _prices_error(
                f"prices: unknown market {market!r}; expected one of {list(GAME_MARKET_TYPES)}"
            )
        kind = GAME_MARKET_KIND[market]
        if not isinstance(entry, dict):
            raise _prices_error(f"prices[{market!r}]: the entry must be a JSON object of prices")
        required = _DOC_FIELDS[kind]
        optional = _DOC_OPTIONAL_FIELDS.get(kind, frozenset())
        unknown = [f for f in entry if f not in required and f not in optional]
        if unknown:
            raise _prices_error(
                f"prices[{market!r}]: unknown field(s) {unknown}; "
                f"the market takes {list(required) + sorted(optional)}"
            )
        row: dict[str, float] = {}
        for field_name, column in required.items():
            if field_name not in entry:
                raise _prices_error(f"prices[{market!r}]: missing field {field_name!r}")
            value = _finite_number(entry[field_name])
            if value is None:
                raise _prices_error(
                    f"prices[{market!r}]: field {field_name!r} is not a number "
                    f"({entry[field_name]!r})"
                )
            if field_name.endswith("_ml") and value == 0.0:
                raise _prices_error(
                    f"prices[{market!r}]: field {field_name!r} is 0, which is not an American price"
                )
            row[column] = value
        for field_name in optional:
            if field_name in entry and _finite_number(entry[field_name]) is None:
                raise _prices_error(
                    f"prices[{market!r}]: field {field_name!r} is not a number "
                    f"({entry[field_name]!r})"
                )
        if kind == "yes_no":
            row["total_line"] = _YES_NO_LINE
        clash = [p for p in _INJECTED_PARAMS[market] if named.get(p) is not None]
        if clash:
            raise _prices_error(
                f"prices[{market!r}]: the market is also given by the query param(s) "
                f"{clash}; give it one way"
            )
        parsed[market] = row
    return parsed


def _finite_number(value: Any) -> float | None:
    """SIM-546 (pure): ``value`` as a finite float, or None (a bool, a string,
    a null, NaN and infinity are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _prices_error(detail: str) -> HTTPException:
    """SIM-546: the 422 a bad ``prices`` document raises."""
    return HTTPException(status_code=422, detail=detail)


def _split_prices(
    named: Mapping[str, float | None], doc: Mapping[str, Mapping[str, float]]
) -> tuple[dict[str, float | None], dict[str, Mapping[str, float]]]:
    """SIM-546 (pure): fold the ``prices`` document into the request's prices.

    A full-game market in the document fills its named params (it is priced
    through them, as ``injected``). A segment or team market stays a
    document entry. Returns ``(named params, segment entries)``."""
    merged = dict(named)
    segments: dict[str, Mapping[str, float]] = {}
    for market, row in doc.items():
        params = _NAMED_PARAM_OF.get(market)
        if params is None:
            segments[market] = row
            continue
        fields = _DOC_FIELDS[GAME_MARKET_KIND[market]]
        for field_name, param in params.items():
            merged[param] = row[fields[field_name]]
    return merged, segments


async def _priced_edge_reports(
    request: Request,
    summary: Any,
    win_prob: WinProbability,
    *,
    game_pk: int,
    markets: tuple[str, ...],
    prices: Mapping[str, float | None],
    doc: Mapping[str, Mapping[str, float]] | None = None,
) -> _EdgeBuild:
    """SIM-555: read the stored lines when a requested market needs them, then
    build the reports. ``prices`` carries the injected query params by name.
    SIM-546: ``doc`` is the parsed ``prices`` document (:func:`_parse_prices`);
    a market it names reads no stored row."""
    named, injected = _split_prices(prices, doc or {})
    uninjected = [
        m
        for m in markets
        if m not in injected and not _any_given(*(named.get(p) for p in _INJECTED_PARAMS[m]))
    ]
    stored = (
        await _read_stored_rows(request, game_pk, _stored_read_markets(markets))
        if uninjected
        else {}
    )
    return _build_edge_reports(
        summary,
        win_prob,
        game_pk=game_pk,
        markets=markets,
        home_ml=named.get("home_ml"),
        away_ml=named.get("away_ml"),
        over_ml=named.get("over_ml"),
        under_ml=named.get("under_ml"),
        total_line=named.get("total_line"),
        home_rl_ml=named.get("home_rl_ml"),
        away_rl_ml=named.get("away_rl_ml"),
        run_line=named.get("run_line"),
        away_run_line=named.get("away_run_line"),
        stored=stored,
        injected=injected,
    )


def _priced_markets(requested: Sequence[str], built: _EdgeBuild) -> list[str]:
    """SIM-546 (pure): the requested markets that were priced, in vocabulary order."""
    return [m for m in GAME_MARKET_TYPES if m in requested and m in built.odds_source]


def _market_names(markets: Sequence[str]) -> dict[str, str]:
    """SIM-546 (pure): report label -> the market's plain name ("First five total")."""
    return {_report_label(m): GAME_MARKET_NAMES[m] for m in markets}


def _side_value(side: Any) -> str:
    """A MarketSide's string value ("home"); a plain string passes through."""
    return str(side.value) if hasattr(side, "value") else str(side)


def _books_by_market(fair_book: Mapping[str, str]) -> dict[str, str]:
    """SIM-555: each market's graded book as a display name ("DraftKings")."""
    return {market: book_display_name(label) for market, label in fair_book.items()}


def _parse_markets(markets: str | None) -> tuple[str, ...]:
    """Parse the ``markets`` query param (comma-separated) -> a validated tuple.

    SIM-546: defaults to every game market (:data:`_EDGE_MARKET_TYPES`, all
    fifteen) when unset, and returns the markets in the vocabulary's order. A
    token not in the valid set is a 422 (a typo'd market should fail loudly,
    not be silently dropped).
    """
    if not markets:
        return _EDGE_MARKET_TYPES
    requested = tuple(m.strip() for m in markets.split(",") if m.strip())
    bad = [m for m in requested if m not in _EDGE_MARKET_TYPES]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(f"unknown market(s) {bad}; expected a subset of {list(_EDGE_MARKET_TYPES)}"),
        )
    return tuple(m for m in _EDGE_MARKET_TYPES if m in requested) or _EDGE_MARKET_TYPES


async def _summary_and_winprob(
    request: Request,
    *,
    game_pk: int,
    n_iterations: int,
    base_seed: int | None,
    use_cache: bool,
) -> tuple[Any, WinProbability]:
    """Resolve the lineup, run (or reuse) a sim, and return (summary, win_prob).

    REUSES the SIM-355 sim seam: resolve the GameState (404 on a bad game),
    build a GameSpec under the request's factory ref, and run the BatchRunner
    (cache-memoized). The :class:`WinProbability` is derived from the summary's
    raw arrays so the moneyline report consumes a calibrated SIM-330 probability
    (here uncalibrated -- identity -- since this is a best-effort edge surface).

    SIM-452: the kwargs come from ``_resolved_sim_kwargs``, which looks the venue
    park factor up first. This surface used to call the bare builder, so every
    edge and CLV number it produced came from a park-blind simulation.
    """
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    spec = GameSpec(
        machine_factory=factory_ref,
        sim_kwargs=await _resolved_sim_kwargs(request, pool, state, game_pk),
    )
    runner = _build_runner(request)

    batch = await asyncio.to_thread(
        _run_batch,
        runner,
        spec,
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )
    summary = batch.summary
    # SIM-330 / SIM-387: thread the loaded CalibrationMap from app.state so CLV and
    # edge reports are computed off a *calibrated* win probability, not the identity
    # fallback.  Falls back to IDENTITY_CALIBRATION when the app was started without
    # a fitted CalibrationReport (e.g. in tests / early staging).
    cal_map = getattr(request.app.state, "calibration_map", IDENTITY_CALIBRATION)
    win_prob = win_probability(summary, calibration_map=cal_map)
    return summary, win_prob


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class RunLinePricingModel(BaseModel):
    """SIM-549: how the run line was priced.

    ``shape`` is 'pair' (the away spread is the negative of the home spread:
    the two prices de-vig against each other) or 'two_bets' (two separate bets:
    each price over the game's two-way ``reference_margin``, taken from
    ``reference_source`` -- 'total', 'moneyline' or 'flat').
    """

    shape: str
    home_line: float
    away_line: float
    reference_margin: float | None = None
    reference_source: str | None = None


class EdgesResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/edges`` envelope.

    Carries the per-market :class:`EdgeReportModel` list plus the run metadata
    (n_iterations / base_seed) and the per-market ``odds_source`` flags
    ("injected" / "stored" / "mock") so a consumer knows where each market's
    prices came from. SIM-555: "stored" = each book's stored closing line, or,
    before the game starts, its latest stored current line. A stored market
    names its graded book in
    ``fair_book`` (the book whose row gave the fair probability), and each
    report names the book of its offered price (``price_book``).
    """

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    markets: list[str] = Field(default_factory=list)
    odds_source: dict[str, str] = Field(default_factory=dict)
    edges: list[EdgeReportModel] = Field(default_factory=list)
    #: SIM-549: how the run line was priced (None when it was not requested).
    run_line_pricing: RunLinePricingModel | None = None
    #: SIM-555: market -> the stored label of the graded book (stored markets only).
    fair_book: dict[str, str] = Field(default_factory=dict)
    #: SIM-555: market -> that book's display name ("DraftKings").
    fair_book_name: dict[str, str] = Field(default_factory=dict)
    #: SIM-546: report label -> the market's plain name ("First five total"); the
    #: card titles its sections from it.
    market_names: dict[str, str] = Field(default_factory=dict)
    #: SIM-546: report label -> how that run line was priced, for every priced
    #: run line ("run_line", "f1_runline", "f5_runline").
    run_line_pricing_by_label: dict[str, RunLinePricingModel] = Field(default_factory=dict)


class SignalsResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/signals`` envelope.

    Carries the ranked (+EV, EV descending) :class:`BetSignalModel` list plus the
    run metadata, the gate config that produced them (min_edge / min_ev /
    kelly_fraction / max_stake_fraction), and the per-market ``odds_source`` flags
    ("injected" / "stored" / "mock"). SIM-555: ``fair_book`` as on /edges; each
    signal names the book of its offered price (``price_book``).
    """

    game_pk: int
    n_iterations: int
    base_seed: int | None = None
    config: dict[str, float] = Field(default_factory=dict)
    odds_source: dict[str, str] = Field(default_factory=dict)
    signals: list[BetSignalModel] = Field(default_factory=list)
    #: SIM-549: how the run line was priced (None when it was not requested).
    run_line_pricing: RunLinePricingModel | None = None
    #: SIM-555: market -> the stored label of the graded book (stored markets only).
    fair_book: dict[str, str] = Field(default_factory=dict)
    #: SIM-555: market -> that book's display name ("DraftKings").
    fair_book_name: dict[str, str] = Field(default_factory=dict)
    #: SIM-546: report label -> the market's plain name, so a signal on a
    #: first-five total or a tie can be labelled.
    market_names: dict[str, str] = Field(default_factory=dict)
    #: SIM-546: report label -> how that run line was priced (every priced run line).
    run_line_pricing_by_label: dict[str, RunLinePricingModel] = Field(default_factory=dict)


class LineMovementResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/line-movement`` envelope.

    Carries one :class:`LineMovementModel` per (side, book) present in the stored
    ``raw.game_odds`` history for the (game_pk, market_type[, book]).
    """

    game_pk: int
    market_type: str
    book: str | None = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


class ClvSnapshotResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/clv`` envelope.

    A thin snapshot projection of /line-movement: the entry-vs-close CLV of every
    line-movement series that HAS one. Omitted: a series with < 2 quotes, a pair
    with a missing opposite-side price, and a run-line side whose spread moved
    between the open and the close. Each row carries the side / book identity plus
    the CLV's ``clv_prob`` / ``beat_close`` for a compact "did I beat the close"
    view without the full quote series.
    """

    game_pk: int
    market_type: str
    book: str | None = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


# ===========================================================================
# SIM-367/SIM-339 -- GET /api/betting/games/{game_pk}/edges
# ===========================================================================


#: SIM-546: the description of the ``prices`` query param, shared by both routes.
_PRICES_PARAM_DESCRIPTION = (
    "A JSON document of injected prices keyed by market type, e.g. "
    '{"f5_total": {"over_ml": -110, "under_ml": -110, "line": 4.5}}. A moneyline-kind '
    "market takes home_ml and away_ml; a three-way market those and draw_ml; a total "
    "kind over_ml, under_ml and line (first_inning_run is always 0.5); a run line "
    "home_ml, away_ml, home_line and away_line. A market named here is priced from it "
    "alone (odds_source 'injected'). 422 on an unknown market or field, a missing or "
    "non-numeric field, or a market also given by the named params"
)

#: SIM-546: the description of the ``markets`` query param, shared by both routes.
_MARKETS_PARAM_DESCRIPTION = (
    "Comma-separated subset of the fifteen game markets (moneyline, runline, total, "
    "f1_moneyline, f5_moneyline, f1_total, f5_total, f1_runline, f5_runline, "
    "team_total_home, team_total_away, f5_team_total_home, f5_team_total_away, "
    "first_to_score, first_inning_run); default all fifteen"
)


def _named_prices(
    *,
    home_ml: float | None,
    away_ml: float | None,
    over_ml: float | None,
    under_ml: float | None,
    total_line: float | None,
    home_rl_ml: float | None,
    away_rl_ml: float | None,
    run_line: float | None,
    away_run_line: float | None,
) -> dict[str, float | None]:
    """The named injected query params of the three full-game markets, by name."""
    return {
        "home_ml": home_ml,
        "away_ml": away_ml,
        "over_ml": over_ml,
        "under_ml": under_ml,
        "total_line": total_line,
        "home_rl_ml": home_rl_ml,
        "away_rl_ml": away_rl_ml,
        "run_line": run_line,
        "away_run_line": away_run_line,
    }


def _pricing_models(built: _EdgeBuild) -> dict[str, RunLinePricingModel]:
    """SIM-546: every priced run line's pricing as the API model, by report label."""
    return {
        label: RunLinePricingModel(**pricing)
        for label, pricing in built.run_line_pricing_by_label.items()
    }


@router.get(
    "/games/{game_pk}/edges",
    response_model=EdgesResponse,
    summary="Per-market edge reports (the fifteen game markets)",
    dependencies=[Depends(require_auth)],
    description=(
        "Run (or reuse, via the SIM-359 cache) a Monte-Carlo sim for the game and "
        "build the EdgeReports for the requested markets (SIM-546: the fifteen game "
        "markets by default, every side of each) off the GameSimSummary + market "
        "odds. The three full-game markets keep their labels (moneyline, total, "
        "run_line); every segment or team market's label is its market type. Odds "
        "come from the injected query params or the prices document when supplied; "
        "else (SIM-555) the stored "
        "lines of the game, one row per book: the closing lines, plus the "
        "current lines while the game has not started (raw.games status "
        "Preview; an in-play line is never read). The fair probability comes "
        "from one book's row (the graded book, named in fair_book) and each "
        "side's offered price, and so its EV, from the best stored price at that "
        "line (its book named in price_book); else the deterministic mock "
        "provider. odds_source flags each "
        "market: injected, stored or mock. market_names gives each label its plain "
        "name. A cached summary with no inning grid prices the full-game markets "
        "only. numpy-free EdgeReportModel list. 503 if "
        "no DB pool, 404 if the lineup cannot be resolved, 422 on a bad market or "
        "a bad prices document."
    ),
)
async def get_game_edges(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(200, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(None, description="Reproducibility seed for the batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
    markets: str | None = Query(None, description=_MARKETS_PARAM_DESCRIPTION),
    home_ml: float | None = Query(None, description="Injected home moneyline (American)"),
    away_ml: float | None = Query(None, description="Injected away moneyline (American)"),
    over_ml: float | None = Query(None, description="Injected total over price (American)"),
    under_ml: float | None = Query(None, description="Injected total under price (American)"),
    total_line: float | None = Query(None, description="Injected total line"),
    home_rl_ml: float | None = Query(None, description="Injected home run-line price (American)"),
    away_rl_ml: float | None = Query(None, description="Injected away run-line price (American)"),
    run_line: float | None = Query(None, description="Injected HOME run line (e.g. -1.5)"),
    away_run_line: float | None = Query(
        None,
        description=(
            "Injected AWAY team's own run line (e.g. +1.5, or -1.5 when the book lists "
            "two separate bets); defaults to the mirror of run_line (a pair). Two "
            "separate bets are priced over the margin of over_ml / under_ml, then "
            "home_ml / away_ml (each injected, else the mock's)"
        ),
    ),
    prices: str | None = Query(None, description=_PRICES_PARAM_DESCRIPTION),
) -> EdgesResponse:
    requested = _parse_markets(markets)
    named = _named_prices(
        home_ml=home_ml,
        away_ml=away_ml,
        over_ml=over_ml,
        under_ml=under_ml,
        total_line=total_line,
        home_rl_ml=home_rl_ml,
        away_rl_ml=away_rl_ml,
        run_line=run_line,
        away_run_line=away_run_line,
    )
    # SIM-546: a bad prices document fails before the simulation runs.
    doc = _parse_prices(prices, named)
    summary, win_prob = await _summary_and_winprob(
        request,
        game_pk=int(game_pk),
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    built = await _priced_edge_reports(
        request,
        summary,
        win_prob,
        game_pk=int(game_pk),
        markets=requested,
        prices=named,
        doc=doc,
    )
    priced = _priced_markets(requested, built)

    return EdgesResponse(
        game_pk=int(game_pk),
        n_iterations=int(summary.n_iterations),
        base_seed=base_seed,
        markets=priced,
        odds_source=built.odds_source,
        edges=[
            EdgeReportModel.from_dataclass(
                r, price_book=built.price_book.get((r.label, _side_value(r.side)))
            )
            for r in built.reports
        ],
        run_line_pricing=(
            None
            if built.run_line_pricing is None
            else RunLinePricingModel(**built.run_line_pricing)
        ),
        fair_book=dict(built.fair_book),
        fair_book_name=_books_by_market(built.fair_book),
        market_names=_market_names(priced),
        run_line_pricing_by_label=_pricing_models(built),
    )


# ===========================================================================
# SIM-369 -- GET /api/betting/games/{game_pk}/signals
# ===========================================================================


@router.get(
    "/games/{game_pk}/signals",
    response_model=SignalsResponse,
    summary="Ranked +EV bet-signal recommendations",
    dependencies=[Depends(require_auth)],
    description=(
        "Build the per-market EdgeReports (as /edges, with the same injected / "
        "stored / mock odds sources and the same prices document; SIM-546: the "
        "fifteen game markets by default, a tie side included), gate them to the "
        "+EV set "
        "(strictly positive edge >= min_edge AND ev > min_ev), size each via "
        "fractional Kelly (kelly_fraction, capped at max_stake_fraction), and "
        "return them RANKED by EV descending. A signal priced from the stored "
        "lines names the book of its offered price (price_book). min_edge / "
        "kelly_fraction are tunable via query params. The segment and team "
        "markets' probabilities are not calibrated. numpy-free BetSignalModel "
        "list. 503 if no DB pool, 404 if the lineup cannot be resolved, 422 on a "
        "bad market or a bad prices document."
    ),
)
async def get_game_signals(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(200, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: int | None = Query(None, description="Reproducibility seed for the batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
    markets: str | None = Query(None, description=_MARKETS_PARAM_DESCRIPTION),
    min_edge: float = Query(0.02, ge=0.0, le=1.0, description="Edge noise floor (gate)"),
    min_ev: float = Query(0.0, ge=-1.0, le=10.0, description="Minimum EV per unit (strict >)"),
    kelly_fraction: float = Query(0.25, ge=0.0, le=1.0, description="Fractional-Kelly multiplier"),
    max_stake_fraction: float = Query(
        0.05, ge=0.0, le=1.0, description="Hard cap on the stake fraction"
    ),
    home_ml: float | None = Query(None, description="Injected home moneyline (American)"),
    away_ml: float | None = Query(None, description="Injected away moneyline (American)"),
    over_ml: float | None = Query(None, description="Injected total over price (American)"),
    under_ml: float | None = Query(None, description="Injected total under price (American)"),
    total_line: float | None = Query(None, description="Injected total line"),
    home_rl_ml: float | None = Query(None, description="Injected home run-line price (American)"),
    away_rl_ml: float | None = Query(None, description="Injected away run-line price (American)"),
    run_line: float | None = Query(None, description="Injected HOME run line (e.g. -1.5)"),
    away_run_line: float | None = Query(
        None,
        description=(
            "Injected AWAY team's own run line (e.g. +1.5, or -1.5 when the book lists "
            "two separate bets); defaults to the mirror of run_line (a pair). Two "
            "separate bets are priced over the margin of over_ml / under_ml, then "
            "home_ml / away_ml (each injected, else the mock's)"
        ),
    ),
    prices: str | None = Query(None, description=_PRICES_PARAM_DESCRIPTION),
) -> SignalsResponse:
    requested = _parse_markets(markets)
    named = _named_prices(
        home_ml=home_ml,
        away_ml=away_ml,
        over_ml=over_ml,
        under_ml=under_ml,
        total_line=total_line,
        home_rl_ml=home_rl_ml,
        away_rl_ml=away_rl_ml,
        run_line=run_line,
        away_run_line=away_run_line,
    )
    doc = _parse_prices(prices, named)
    summary, win_prob = await _summary_and_winprob(
        request,
        game_pk=int(game_pk),
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    built = await _priced_edge_reports(
        request,
        summary,
        win_prob,
        game_pk=int(game_pk),
        markets=requested,
        prices=named,
        doc=doc,
    )

    config = BetSignalConfig(
        min_edge=float(min_edge),
        min_ev=float(min_ev),
        kelly_fraction=float(kelly_fraction),
        max_stake_fraction=float(max_stake_fraction),
    )
    # SIM-546 (owner decision 4, 2026-10-09): the signals fire on all fifteen
    # markets, the uncalibrated segment and team markets included.
    signals = bet_signals_from_edges(built.reports, config=config)

    return SignalsResponse(
        game_pk=int(game_pk),
        n_iterations=int(summary.n_iterations),
        base_seed=base_seed,
        config={
            "min_edge": config.min_edge,
            "min_ev": config.min_ev,
            "kelly_fraction": config.kelly_fraction,
            "max_stake_fraction": config.max_stake_fraction,
        },
        odds_source=built.odds_source,
        signals=[
            BetSignalModel.from_dataclass(
                s, price_book=built.price_book.get((s.label, _side_value(s.side)))
            )
            for s in signals
        ],
        run_line_pricing=(
            None
            if built.run_line_pricing is None
            else RunLinePricingModel(**built.run_line_pricing)
        ),
        fair_book=dict(built.fair_book),
        fair_book_name=_books_by_market(built.fair_book),
        market_names=_market_names(_priced_markets(requested, built)),
        run_line_pricing_by_label=_pricing_models(built),
    )


# ===========================================================================
# SIM-368 -- GET /api/betting/games/{game_pk}/line-movement
# ===========================================================================


def _validate_market_type(market_type: str) -> str:
    """422 on an unknown market_type for the line-movement / clv reads."""
    if market_type not in _VALID_MARKET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown market_type {market_type!r}; expected one of {list(_VALID_MARKET_TYPES)}"
            ),
        )
    return market_type


async def _fetch_movements(
    request: Request, *, game_pk: int, market_type: str, book: str | None
) -> list[Any]:
    """Acquire a conn from the pool and run fetch_line_movement (503 if no pool).

    Handles both an asyncpg pool (``async with pool.acquire()``) and a pool that IS
    a connection exposing ``fetch`` (the mock-pool test idiom), mirroring
    ``api.routes.games._resolve_state_or_error``'s acquire-or-direct pattern.
    """
    pool = _get_pool(request)
    acquire = getattr(pool, "acquire", None)
    if acquire is not None:
        async with pool.acquire() as conn:
            return await fetch_line_movement(
                conn, game_pk=int(game_pk), market_type=market_type, book=book
            )
    return await fetch_line_movement(pool, game_pk=int(game_pk), market_type=market_type, book=book)


@router.get(
    "/games/{game_pk}/line-movement",
    response_model=LineMovementResponse,
    summary="Opening->closing line-movement time-series per side/book",
    description=(
        "Read the persisted raw.game_odds history for the (game_pk, market_type"
        "[, book]) and build the SIM-368 line-movement time-series: one series per "
        "(side, book) with the ordered quotes, the per-step + opening->closing "
        "deltas, the running implied-prob surface, the steam direction, the "
        "sharp-consensus flag, and the entry-vs-close CLV. A run line (SIM-549) "
        "carries run_line_shape: pair, two_bets or mixed. When either end is two "
        "separate bets, both ends are priced on their own price over the game's "
        "two-way margin. A side whose spread moved gets no CLV; clv_note says why. "
        "SIM-555: only the one-book rows are read (book = bp:<id>); each series and "
        "each quote carries the book's display name in book_name. "
        "numpy-free LineMovementModel list. 503 if no DB pool, 422 on a bad market_type."
    ),
)
async def get_game_line_movement(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: str | None = Query(
        None, description="Restrict to one stored book label, e.g. bp:12 (else all books)"
    ),
) -> LineMovementResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(request, game_pk=int(game_pk), market_type=mt, book=book)
    return LineMovementResponse(
        game_pk=int(game_pk),
        market_type=mt,
        book=book,
        count=len(movements),
        series=[LineMovementModel.from_dataclass(m) for m in movements],
    )


# ===========================================================================
# SIM-339/SIM-368 -- GET /api/betting/games/{game_pk}/clv (snapshot projection)
# ===========================================================================


@router.get(
    "/games/{game_pk}/clv",
    response_model=ClvSnapshotResponse,
    summary="Entry-vs-close CLV snapshot per side/book",
    description=(
        "A thin projection of /line-movement: the line-movement series that carry "
        "an entry-vs-close CLV (>= 2 quotes and both ends priceable). Each model's "
        "clv.clv_prob / clv.beat_close answers 'did the opening price beat the "
        "close' for that side/book. A run-line series has a CLV only when its "
        "spread was the same at both ends (SIM-549). clv_basis says whether it was "
        "priced as a pair or as two separate bets. SIM-555: the one-book rows only "
        "(book = bp:<id>). numpy-free. 503 if no DB pool, 422 on a bad market_type."
    ),
)
async def get_game_clv(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: str | None = Query(
        None, description="Restrict to one stored book label, e.g. bp:12 (else all books)"
    ),
) -> ClvSnapshotResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(request, game_pk=int(game_pk), market_type=mt, book=book)
    # Only the series that actually have a computed CLV (the snapshot's point).
    with_clv = [m for m in movements if m.clv is not None]
    return ClvSnapshotResponse(
        game_pk=int(game_pk),
        market_type=mt,
        book=book,
        count=len(with_clv),
        series=[LineMovementModel.from_dataclass(m) for m in with_clv],
    )


# ===========================================================================
# SIM-519 Part I -- GET /api/betting/games/{game_pk}/card-odds
# ===========================================================================

#: The three full-game markets the slate card shows.
_CARD_MARKETS: tuple[str, ...] = ("moneyline", "runline", "total")

#: The final score of a game, for the settlement (``$1`` = game_pk).
_SQL_CARD_FINAL = """
    SELECT status, home_score_final, away_score_final
    FROM raw.games WHERE game_pk = $1
"""


class CardMoneyline(BaseModel):
    book: str
    line_type: str
    away: float
    home: float


class CardRunLineSide(BaseModel):
    line: float
    price: float


class CardRunLine(BaseModel):
    book: str
    line_type: str
    away: CardRunLineSide
    home: CardRunLineSide


class CardTotal(BaseModel):
    book: str
    line_type: str
    line: float
    over: float
    under: float


class CardSettlement(BaseModel):
    """How each pregame line settled on the final score."""

    away_score: int
    home_score: int
    moneyline: str | None = None  # "away" | "home"
    runline: str | None = None  # "away" | "home" | "push"
    total: str | None = None  # "over" | "under" | "push"


class CardOddsResponse(BaseModel):
    """The book's pregame lines for one slate card (SIM-519 Part I).

    Each market is the graded book's row (``GRADED_BOOK_PREFERENCE``): its
    closing row once stored, else its latest current row while the game is in
    Preview. A market with no stored row is null. Never the mock.
    """

    game_pk: int
    moneyline: CardMoneyline | None = None
    runline: CardRunLine | None = None
    total: CardTotal | None = None
    settled: CardSettlement | None = None


def _card_settlement(odds: CardOddsResponse, away_score: int, home_score: int) -> CardSettlement:
    """(pure) The winner side of each priced market on the final score."""
    out = CardSettlement(away_score=away_score, home_score=home_score)
    if odds.moneyline is not None and away_score != home_score:
        out.moneyline = "away" if away_score > home_score else "home"
    if odds.runline is not None:
        margin = away_score + odds.runline.away.line - home_score
        out.runline = "push" if margin == 0 else ("away" if margin > 0 else "home")
    if odds.total is not None:
        runs = away_score + home_score
        out.total = (
            "push" if runs == odds.total.line else ("over" if runs > odds.total.line else "under")
        )
    return out


def _card_odds_from_rows(game_pk: int, stored: StoredRows) -> CardOddsResponse:
    """(pure) The graded row of each card market → the card's lines."""

    def graded(market: str) -> Mapping[str, Any] | None:
        rows = stored.get(market) or []
        return rows[0] if rows else None

    resp = CardOddsResponse(game_pk=game_pk)
    if (row := graded("moneyline")) is not None:
        resp.moneyline = CardMoneyline(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            away=float(row["away_ml"]),
            home=float(row["home_ml"]),
        )
    if (row := graded("runline")) is not None:
        resp.runline = CardRunLine(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            away=CardRunLineSide(
                line=float(row["away_spread"]), price=float(row["away_spread_ml"])
            ),
            home=CardRunLineSide(
                line=float(row["home_spread"]), price=float(row["home_spread_ml"])
            ),
        )
    if (row := graded("total")) is not None:
        resp.total = CardTotal(
            book=book_display_name(row["book"]),
            line_type=str(row["line_type"]),
            line=float(row["total_line"]),
            over=float(row["over_ml"]),
            under=float(row["under_ml"]),
        )
    return resp


@router.get(
    "/games/{game_pk}/card-odds",
    response_model=CardOddsResponse,
    summary="The book's pregame lines for a slate card (SIM-519 Part I)",
    dependencies=[Depends(require_auth)],
    description=(
        "The graded book's moneyline, run line and total for one game, from the stored "
        "rows only (never the mock): the closing row once stored, else the latest "
        "current row while the game is in Preview. On a final game `settled` says how "
        "each line settled. 404 when the game has no stored row for any of the three."
    ),
)
async def get_card_odds(game_pk: int, request: Request) -> CardOddsResponse:
    stored = await _read_stored_rows(request, int(game_pk), _CARD_MARKETS)
    odds = _card_odds_from_rows(int(game_pk), stored)
    if odds.moneyline is None and odds.runline is None and odds.total is None:
        raise HTTPException(status_code=404, detail=f"no stored odds for game {game_pk}")
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        try:
            rows = await pool.fetch(_SQL_CARD_FINAL, int(game_pk))
        except Exception as exc:  # noqa: BLE001 -- the lines still serve without a result
            log.warning("card-odds: the final-score read failed for %s: %s", game_pk, exc)
            rows = []
        for raw in rows or []:
            row = raw if isinstance(raw, Mapping) else dict(raw)
            away, home = row.get("away_score_final"), row.get("home_score_final")
            if row.get("status") == "Final" and away is not None and home is not None:
                odds.settled = _card_settlement(odds, int(away), int(home))
    return odds


__all__ = [
    "router",
    "EdgesResponse",
    "SignalsResponse",
    "LineMovementResponse",
    "ClvSnapshotResponse",
    "RunLinePricingModel",
    "CardOddsResponse",
]
