"""
api/routes/betting.py
=====================
The betting routes (prefix ``/api/betting``).

    GET /api/betting/games/{game_pk}/edges          the edge report of every side of each market
    GET /api/betting/games/{game_pk}/signals        the +EV bets, ranked by EV
    GET /api/betting/games/{game_pk}/line-movement  the opening-to-closing series per side and book
    GET /api/betting/games/{game_pk}/clv            the entry-versus-close CLV of each series
    GET /api/betting/games/{game_pk}/card-odds      the book's pregame lines for a slate card

This module holds the aggregate ``router`` (a login on every route), /edges,
/signals and the simulation they price from (:func:`_summary_and_winprob`).
The routers it includes add the rest: ``betting_lines`` (line movement and
CLV) and ``betting_card`` (the card's lines). The prices and the edge math
live in ``betting_pricing``, the stored lines in ``betting_stored_odds``. Each
sub-router is a bare ``APIRouter()``: the aggregate's prefix, tag and login
apply to its routes.

THE SIMULATION BEHIND THE EDGES
-------------------------------
/edges and /signals price the markets off a ``GameSimSummary``: the run-line
cover reads the per-iteration score margin, the total reads the totals. The
summary comes from the game routes' seam (``api.routes.games``): the game's
state, a ``GameSpec`` under the request's factory ref, and the
``BatchRunner``. The runner's cache memoizes the summary by (spec, seed, N),
so a /edges call after a /simulate at the same seed and N reads the cached
summary.

THE REAL FINAL
--------------
Once the game is final, each edge says how its side settled (``result``), and
``final`` gives the score: the stored grid when the nightly load has the game,
else the league feed's linescore.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from api.auth import require_auth
from api.routes import betting_card, betting_lines
from api.routes._common import _get_pool
from api.routes.betting_pricing import (
    _at_offered_price,  # noqa: F401 # re-export: removed in the integration pass
    _books_by_market,
    _EdgeBuild,
    _market_names,
    _parse_markets,
    _parse_prices,
    _priced_edge_reports,
    _priced_markets,
    _report_label,
    _side_value,
)
from api.routes.betting_stored_odds import (  # re-export: removed in the integration pass
    _SQL_GAME_STATUS,  # noqa: F401
    _SQL_STORED_GAME_ODDS,  # noqa: F401
    _SQL_STORED_GAME_ODDS_SEEN,  # noqa: F401
    _fetch_stored_rows,  # noqa: F401
    _latest_pregame_rows,  # noqa: F401
    _status_of,  # noqa: F401
    _stored_market,  # noqa: F401
    _stored_read_markets,  # noqa: F401
    _stored_reference_margin,  # noqa: F401
    _stored_rows_by_market,  # noqa: F401
    _usable_line_types,  # noqa: F401
)
from api.routes.games import (
    _build_runner,
    _resolve_state_or_error,
    _resolved_sim_kwargs,
    _run_batch,
    resolve_factory_ref,
)
from api.schemas import BetSignalModel, EdgeReportModel
from betting.bet_signal import BetSignalConfig, bet_signals_from_edges
from pipeline.odds_provider import GAME_MARKET_KIND, GAME_MARKET_SEGMENT, GAME_MARKET_TYPES
from simulation.batch_runner import GameSpec
from simulation.win_probability import IDENTITY_CALIBRATION, WinProbability, win_probability

log = logging.getLogger("api.routes.betting")

#: The aggregate router: every route under /api/betting requires a login
#: (cookie or API key). The two sub-routers join it here.
router = APIRouter(prefix="/api/betting", tags=["betting"], dependencies=[Depends(require_auth)])
router.include_router(betting_lines.router)
router.include_router(betting_card.router)


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
    #: The real final ({away, home}) once the game is over; each edge's
    #: ``result`` then says how its side settled.
    final: dict[str, int] | None = None
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


# ===========================================================================
# The game page: how each bet settled
# ===========================================================================

#: The real final's grid (``raw.games.inning_scores``) of a final game.
_SQL_FINAL_GRID = """
    SELECT inning_scores, status, home_score_final, away_score_final
      FROM raw.games WHERE game_pk = $1
"""

#: A report's label -> its market type (``run_line`` -> ``runline``).
_MARKET_OF_LABEL: dict[str, str] = {_report_label(m): m for m in GAME_MARKET_TYPES}


async def _actual_result(request: Request, game_pk: int) -> tuple[Any, dict[str, int]] | None:
    """The real final as a one-game ``SegmentRuns`` and its score, or None.

    The stored grid when the nightly load has the game; else the league feed's
    linescore (a game that ended today). None before the game is final.
    """
    from simulation.game_market_distributions import SegmentRuns

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        try:
            rows = await pool.fetch(_SQL_FINAL_GRID, int(game_pk))
        except Exception as exc:  # noqa: BLE001 -- the feed is the fallback
            log.warning("grading: the stored final read failed for %s: %s", game_pk, exc)
            rows = []
        for raw in rows or []:
            row = raw if isinstance(raw, Mapping) else dict(raw)
            grid = row.get("inning_scores")
            if isinstance(grid, str | bytes | bytearray):
                grid = json.loads(grid)
            if row.get("status") == "Final" and isinstance(grid, Mapping) and grid.get("home"):
                final = {
                    "away": int(row.get("away_score_final") or sum(v or 0 for v in grid["away"])),
                    "home": int(row.get("home_score_final") or sum(v or 0 for v in grid["home"])),
                }
                return SegmentRuns.from_official_grid(dict(grid)), final
    try:
        from api.routes.games_feed import _league_game_payload
        from pipeline.mlb_game_feed import parse_game_feed

        payload, _source, _err = await _league_game_payload(request, int(game_pk))
    except Exception as exc:  # noqa: BLE001
        log.warning("grading: the league feed read failed for %s: %s", game_pk, exc)
        return None
    if payload is None:
        return None
    card = parse_game_feed(payload)
    ls = card.get("linescore") or {}
    if card.get("status") != "final" or not ls.get("innings"):
        return None
    grid = {
        "away": [i.get("away") for i in ls["innings"]],
        "home": [i.get("home") for i in ls["innings"]],
    }
    final = {"away": int(ls["away"]["runs"] or 0), "home": int(ls["home"]["runs"] or 0)}
    return SegmentRuns.from_official_grid(grid), final


def _grade(actual: Any, label: str, side: str, line: float | None) -> str | None:
    """(pure) "won" / "lost" / "push" for one side of one market on the real final."""
    from simulation.game_market_distributions import market_outcome

    market = _MARKET_OF_LABEL.get(label)
    if market is None:
        return None
    kind = GAME_MARKET_KIND[market]
    try:
        if kind == "three_way":
            margin = int(actual.segment_margin(GAME_MARKET_SEGMENT[market])[0])
            won = {"home": margin > 0, "away": margin < 0, "draw": margin == 0}.get(side)
            return None if won is None else ("won" if won else "lost")
        if kind == "runline":
            o = market_outcome(actual, market, line=line, side=side)
            return "push" if o is None else ("won" if o == 1 else "lost")
        o = market_outcome(actual, market, line=line)
        if o is None:
            return "push"
        reference = side in ("home", "over")
        return "won" if (o == 1) == reference else "lost"
    except (ValueError, KeyError, IndexError) as exc:
        log.warning("grading: %s %s could not be graded: %s", label, side, exc)
        return None


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

    # The game page: each side graded once the game is final.
    actual = await _actual_result(request, int(game_pk))
    edges = []
    for r in built.reports:
        model = EdgeReportModel.from_dataclass(
            r, price_book=built.price_book.get((r.label, _side_value(r.side)))
        )
        if actual is not None:
            model.result = _grade(actual[0], model.label, model.side, model.line)
        edges.append(model)

    return EdgesResponse(
        game_pk=int(game_pk),
        n_iterations=int(summary.n_iterations),
        base_seed=base_seed,
        markets=priced,
        odds_source=built.odds_source,
        final=None if actual is None else actual[1],
        edges=edges,
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


__all__ = [
    "router",
    "EdgesResponse",
    "SignalsResponse",
    "RunLinePricingModel",
]
