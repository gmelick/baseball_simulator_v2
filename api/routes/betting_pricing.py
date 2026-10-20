"""
api/routes/betting_pricing.py
=============================
The edge math of /edges and /signals: the prices of each market and the edge
report of each side.

WHERE A MARKET'S PRICES COME FROM
---------------------------------
In this order:

  1. INJECTED prices: the named query params of the three full-game markets
     (home_ml / away_ml; over_ml / under_ml / total_line; home_rl_ml /
     away_rl_ml / run_line / away_run_line) or the ``prices`` document, which
     can name any of the fifteen markets (:func:`_parse_prices`). A full-game
     market with any named param is priced from those, and the mock fills its
     other prices. A market in the document is priced from the document
     alone. A market given both ways is a 422.
  2. the STORED lines (``api.routes.betting_stored_odds``), when the market
     has a usable stored row.
  3. the MOCK odds provider (``MockOddsAPI.get_odds``, seeded on game_pk), so
     every market is always priceable.

``odds_source`` in the response says which one: "injected", "stored" or "mock".

A RUN LINE IS A PAIR, OR TWO SEPARATE BETS
------------------------------------------
Each team's run line has its own spread. The two are the two sides of ONE bet
only when the away spread is the negative of the home spread (home -1.5 / away
+1.5); the two prices then de-vig against each other. Otherwise the book
listed two separate bets, such as home -1.5 / away -1.5, and each bet prices
from its own price over the book's two-way margin: the game's total, then its
moneyline, then a flat 1.05 (``betting.clv_engine.devig_one_sided``; the run
lines scored by shape, SIM-549). Those reference prices are the injected
over_ml / under_ml and home_ml / away_ml, else the mock's, so an injected run
line can rest on the mock's total. Every run-line report carries its own
side's spread in ``line``. ``away_run_line`` injects the away spread; left
out, it mirrors an injected ``run_line`` (a pair) or takes the provider's.

THE FIFTEEN GAME MARKETS
------------------------
The routes price every market in ``pipeline.odds_provider.GAME_MARKET_TYPES``
by default; ``?markets=`` narrows them. The three full-game markets keep their
labels (``moneyline``, ``total``, ``run_line``). The twelve segment and team
markets (the first-inning and first-five moneyline, total and run line; each
team's full-game and first-five total; the first team to score; a run in the
first inning) price from the summary's inning grid
(``simulation.game_market_distributions``), and each report's label is its
market type (the segment markets, SIM-546). One table, built from each
market's kind, names its sides and columns (:data:`_STORED_SIDE_COLUMNS`). A
three-way market (the first-inning and first-five moneylines) has a third
side, the tie (``draw``); its three prices de-vig together. A segment run line
listed as two separate bets reads its margin from its own segment's total
first. A summary with no inning grid prices the full-game markets only. None
of the twelve probabilities is calibrated.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import Any, NamedTuple

from fastapi import HTTPException, Request

from api.routes.betting_stored_odds import (
    _STORED_SIDE_COLUMNS,
    StoredRows,
    _read_stored_rows,
    _reference_order,
    _stored_market,
    _stored_read_markets,
    _stored_reference_margin,
    _StoredMarket,
)
from betting.clv_engine import (
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    _build_edge_report,
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
from pipeline.odds_provider import (
    FULL_GAME_MARKET_TYPES,
    GAME_MARKET_KIND,
    GAME_MARKET_NAMES,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_TYPES,
    book_display_name,
)
from simulation.game_market_distributions import (
    SegmentRuns,
    segment_runs_from_summary,
    side_probabilities,
)
from simulation.win_probability import WinProbability

log = logging.getLogger("api.routes.betting")


#: The markets /edges and /signals price: every game market the book posts,
#: in the vocabulary's order. The default request prices all fifteen.
_EDGE_MARKET_TYPES: tuple[str, ...] = GAME_MARKET_TYPES


#: The report label of each full-game market. A segment or team market's
#: label is its market type.
_FULL_GAME_LABELS: dict[str, str] = {
    "moneyline": "moneyline",
    "total": "total",
    "runline": "run_line",
}


def _report_label(market: str) -> str:
    """The label a market's reports carry (``runline`` -> ``run_line``)."""
    return _FULL_GAME_LABELS.get(market, market)


# ---------------------------------------------------------------------------
# The injected prices and the mock
# ---------------------------------------------------------------------------


def _mock_odds(game_pk: int, market_type: str) -> dict[str, Any]:
    """The deterministic mock odds of one market of a game.

    The function imports :class:`MockOddsAPI` from the live pipeline (a heavy
    module) only when it runs, so this module stays light to import. The mock
    seeds its draw on ``game_pk``, so it is deterministic and needs no live
    feed. A market with no injected and no stored price falls back to it.
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
    """Build one EdgeReport and append it; skip a degenerate sim probability.

    The betting math (``prob_to_american``) needs a sim probability strictly
    inside (0, 1). A side whose simulated probability is exactly 0.0 or 1.0 (a
    small batch or an extreme line can give one) raises ``ValueError`` in the
    report build. Such a side has no priceable edge, so the function skips its
    report rather than failing the route; the other sides and markets still
    return. The market keeps its ``odds_source`` flag, since its price was
    sourced.
    """
    try:
        reports.append(builder())
    except ValueError as exc:  # a sim probability of 0 or 1: no priceable edge
        log.info("skipping degenerate edge report: %s", exc)


#: The fields of each market kind in the ``prices`` document, and the
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

#: The fields a kind accepts but does not need. The yes / no market takes a
#: ``line`` and ignores it: the market is always over / under 0.5.
_DOC_OPTIONAL_FIELDS: dict[str, frozenset[str]] = {"yes_no": frozenset({"line"})}

#: The line a yes / no market is priced at, whatever a row says.
_YES_NO_LINE = 0.5

#: The named query param each ``prices`` field of a full-game market stands
#: for. A full-game market in the document is priced through them.
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

#: The injected query params of each market. A market with any of them is
#: priced from the injected values (and the mock for the rest). A segment or
#: team market has no named param; the ``prices`` document injects it.
_INJECTED_PARAMS: dict[str, tuple[str, ...]] = {
    m: tuple(_NAMED_PARAM_OF.get(m, {}).values()) for m in GAME_MARKET_TYPES
}


def _at_offered_price(report: EdgeReport, offered: tuple[float, str] | None) -> EdgeReport:
    """The report read at the offered price.

    The EV moves to the offered price; the fair probability and the edge stay
    the graded book's. ``offered`` None (an injected or mock price) leaves the
    report as it is."""
    if offered is None:
        return report
    price = float(offered[0])
    return replace(report, offered_american=price, ev=expected_value(report.sim_prob, price))


def _any_given(*values: float | None) -> bool:
    """True when the caller injected any of a market's query params."""
    return any(v is not None for v in values)


class _EdgeBuild(NamedTuple):
    """What :func:`_build_edge_reports` returns."""

    reports: list[EdgeReport]
    #: market -> "injected" | "stored" | "mock"
    odds_source: dict[str, str]
    #: how the full-game run line was priced (None without the run line)
    run_line_pricing: dict[str, Any] | None
    #: (report label, side value) -> the stored book of the offered price
    price_book: dict[tuple[str, str], str]
    #: market -> the graded book whose row gave the fair probability
    fair_book: dict[str, str]
    #: report label -> how that run line was priced (all three run lines)
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
    per-iteration margin array, home minus away (a segment's margin).
    A pair (the away spread is the negative of the home spread) de-vigs the two
    prices against each other. Two separate bets price each bet from
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
        # The away report prices at the mirrored home line and carries the
        # away team's own spread.
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
    moneyline read the injected query params, else the mock's prices. A
    segment total reads the ``prices`` document, else the mock's."""
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
    """The prices of one segment or team market and where they came from.

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
    """Price every side of one segment or team market.

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

      * **moneyline** -- HOME and AWAY off the :class:`WinProbability`
        (``moneyline_edge_report``), de-vigged against (home_ml, away_ml).
      * **total** -- OVER and UNDER off the summary's ``total_scores`` array
        (``total_over_under_edge_report``) at ``total_line``, against
        (over_ml, under_ml).
      * **runline** -- HOME and AWAY cover off the score margin, each side at its
        OWN spread (``run_line`` for home, ``away_run_line`` for away). A pair
        (the away spread is the negative of the home spread) de-vigs
        (home_rl_ml, away_rl_ml) against each other (``run_line_edge_report``).
        Two separate bets price each bet from its own price over the book's
        margin: the total's, then the moneyline's, then a flat 1.05
        (``one_sided_edge_report``).
      * **the twelve segment and team markets** -- off the summary's
        inning grid (:func:`_price_segment_market`), each report labelled with
        its market type. A summary with no grid skips them and logs one line.

    Prices come from the injected query params when a full-game market has
    any, else the ``stored`` rows (:func:`_stored_rows_by_market`) when the
    market has one, else the mock provider. ``injected`` is the parsed
    ``prices`` document's segment and team markets (:func:`_parse_prices`); a
    market in it is priced from it alone. A stored market takes its fair
    probability and its lines from the graded book's row, and each side's
    offered price (and so the EV) from the best stored price at that line. A
    side whose simulated probability is degenerate (0.0 or 1.0: no priceable
    edge) is skipped (:func:`_safe_report`) rather than failing the route.
    Returns an :class:`_EdgeBuild`.
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
        # HOME: side price home_ml, other away_ml. AWAY: side away_ml, other home_ml.
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
            # The HOME run line (negative: the home team lays runs).
            eff_line, line_inj = _resolve_price(run_line, mock, "home_spread")
            # The AWAY team's own spread: injected; else the mirror of an
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
        # A stored run line reads the graded book's own margin.
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
    """The ``prices`` document, validated, as market -> row-shaped prices (pure).

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
    """``value`` as a finite float, or None (pure).

    A bool, a string, a null, NaN and infinity are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _prices_error(detail: str) -> HTTPException:
    """The 422 a bad ``prices`` document raises."""
    return HTTPException(status_code=422, detail=detail)


def _split_prices(
    named: Mapping[str, float | None], doc: Mapping[str, Mapping[str, float]]
) -> tuple[dict[str, float | None], dict[str, Mapping[str, float]]]:
    """Fold the ``prices`` document into the request's prices (pure).

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
    """Read the stored lines when a requested market needs them, then build the
    reports.

    ``prices`` carries the injected query params by name. ``doc`` is the
    parsed ``prices`` document (:func:`_parse_prices`); a market it names
    reads no stored row."""
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
    """The requested markets that were priced, in the vocabulary's order (pure)."""
    return [m for m in GAME_MARKET_TYPES if m in requested and m in built.odds_source]


def _market_names(markets: Sequence[str]) -> dict[str, str]:
    """Report label -> the market's plain name, such as "First five total" (pure)."""
    return {_report_label(m): GAME_MARKET_NAMES[m] for m in markets}


def _side_value(side: Any) -> str:
    """A MarketSide's string value ("home"); a plain string passes through."""
    return str(side.value) if hasattr(side, "value") else str(side)


def _books_by_market(fair_book: Mapping[str, str]) -> dict[str, str]:
    """Each market's graded book as a display name, such as "DraftKings"."""
    return {market: book_display_name(label) for market, label in fair_book.items()}


def _parse_markets(markets: str | None) -> tuple[str, ...]:
    """Parse the ``markets`` query param (comma-separated) into a validated tuple.

    An unset param gives every game market (:data:`_EDGE_MARKET_TYPES`, all
    fifteen). The markets come back in the vocabulary's order. A token not in
    the valid set is a 422: a mistyped market fails loudly rather than
    dropping out.
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
