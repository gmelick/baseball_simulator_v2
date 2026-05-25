"""
api/routes/betting.py
=====================
Phase-5 Betting API surface (Sprint 5, Wave 3).

Routes (prefix ``/api/betting``):

    GET  /api/betting/games/{game_pk}/edges          (SIM-367/SIM-339)
        run (or reuse) a Monte-Carlo sim for the game -> build the per-market
        EdgeReports (moneyline / total / run-line) from the GameSimSummary + the
        market odds -> return them as a list of EdgeReportModel.

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
     (total_line, run_line) and the endpoint prices against those. This makes the
     endpoint exercisable with real lines from any source and keeps the math
     deterministic / testable.
  2. the MOCK odds provider -- when a market's prices are NOT injected, the
     endpoint falls back to ``MockOddsAPI.get_odds(game_pk, market_type=...)``
     (the SIM-132/133 deterministic mock used by the live pipeline), so every
     market is always priceable even with no live feed wired. The response flags
     ``odds_source`` per market ("injected" | "mock") so a consumer knows which.

Line-movement is different: it reads the persisted ``raw.game_odds`` history from
the Postgres pool (the real opening->closing series), so it is pool-backed (503
without a pool) -- there is no synthetic fallback for a time-series that only the
DB has.

Owner: Backend Developer + Betting Analyst (SIM-367 / SIM-368 / SIM-369).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.routes.games import (
    _build_runner,
    _get_pool,
    _resolve_state_or_error,
    _run_batch,
    _sim_kwargs_from_state,
    resolve_factory_ref,
)
from api.schemas import (
    BetSignalModel,
    EdgeReportModel,
    LineMovementModel,
)
from betting.bet_signal import BetSignalConfig, bet_signals_from_edges
from betting.clv_engine import (
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    moneyline_edge_report,
    run_line_edge_report,
    total_over_under_edge_report,
)
from betting.line_movement import fetch_line_movement
from simulation.batch_runner import GameSpec
from simulation.win_probability import WinProbability, win_probability

log = logging.getLogger("api.routes.betting")

router = APIRouter(prefix="/api/betting", tags=["betting"])

#: The valid market_type values line-movement understands (mirrors the
#: betting.line_movement _MARKET_SIDES set / the Alembic-0003 CHECK).
_VALID_MARKET_TYPES = ("moneyline", "runline", "total")


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


def _resolve_price(
    injected: Optional[float], mock: dict[str, Any], key: str
) -> tuple[float, bool]:
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


def _build_edge_reports(
    summary: Any,
    win_prob: WinProbability,
    *,
    game_pk: int,
    markets: tuple[str, ...],
    home_ml: Optional[float],
    away_ml: Optional[float],
    over_ml: Optional[float],
    under_ml: Optional[float],
    total_line: Optional[float],
    home_rl_ml: Optional[float],
    away_rl_ml: Optional[float],
    run_line: Optional[float],
) -> tuple[list[EdgeReport], dict[str, str]]:
    """Build the EdgeReports for the requested markets off the sim + odds.

    For each requested market the two sides are priced:

      * **moneyline** -- HOME + AWAY off the SIM-330 :class:`WinProbability`
        (``moneyline_edge_report``), de-vigged against (home_ml, away_ml).
      * **total** -- OVER + UNDER off the SIM-327 ``total_scores`` array
        (``total_over_under_edge_report``) at ``total_line``, vs (over_ml, under_ml).
      * **runline** -- HOME + AWAY cover off the score margin
        (``run_line_edge_report``) at ``run_line`` (the HOME line; AWAY is its
        complement), vs (home_rl_ml, away_rl_ml).

    Prices come from the injected query params when supplied, else the mock
    provider (per market). A side whose simulated probability is degenerate (0.0 /
    1.0 -- no priceable edge) is skipped via :func:`_safe_report` rather than
    erroring the endpoint. Returns ``(reports, odds_source_by_market)`` where the
    source map flags "injected" / "mock" per market for the response envelope.
    """
    reports: list[EdgeReport] = []
    odds_source: dict[str, str] = {}

    if "moneyline" in markets:
        mock = _mock_odds(game_pk, "moneyline")
        h, h_inj = _resolve_price(home_ml, mock, "home_ml")
        a, a_inj = _resolve_price(away_ml, mock, "away_ml")
        odds_source["moneyline"] = "injected" if (h_inj or a_inj) else "mock"
        # HOME: side price = home_ml, other = away_ml.  AWAY: side = away_ml, other = home_ml.
        _safe_report(
            reports,
            lambda: moneyline_edge_report(
                win_prob,
                TwoWayMarket(side=MarketSide.HOME, entry=OddsQuote(side=h, other=a)),
                side=MarketSide.HOME,
            ),
        )
        _safe_report(
            reports,
            lambda: moneyline_edge_report(
                win_prob,
                TwoWayMarket(side=MarketSide.AWAY, entry=OddsQuote(side=a, other=h)),
                side=MarketSide.AWAY,
            ),
        )

    if "total" in markets:
        mock = _mock_odds(game_pk, "total")
        o, o_inj = _resolve_price(over_ml, mock, "over_ml")
        u, u_inj = _resolve_price(under_ml, mock, "under_ml")
        line, line_inj = _resolve_price(total_line, mock, "total_line")
        odds_source["total"] = "injected" if (o_inj or u_inj or line_inj) else "mock"
        _safe_report(
            reports,
            lambda: total_over_under_edge_report(
                summary,
                TwoWayMarket(
                    side=MarketSide.OVER, entry=OddsQuote(side=o, other=u, line=line)
                ),
                side=MarketSide.OVER,
            ),
        )
        _safe_report(
            reports,
            lambda: total_over_under_edge_report(
                summary,
                TwoWayMarket(
                    side=MarketSide.UNDER, entry=OddsQuote(side=u, other=o, line=line)
                ),
                side=MarketSide.UNDER,
            ),
        )

    if "runline" in markets:
        mock = _mock_odds(game_pk, "runline")
        h, h_inj = _resolve_price(home_rl_ml, mock, "home_spread_ml")
        a, a_inj = _resolve_price(away_rl_ml, mock, "away_spread_ml")
        # The HOME run line (negative == home laying runs); AWAY is its negation.
        eff_line, line_inj = _resolve_price(run_line, mock, "home_spread")
        odds_source["runline"] = "injected" if (h_inj or a_inj or line_inj) else "mock"
        _safe_report(
            reports,
            lambda: run_line_edge_report(
                summary,
                TwoWayMarket(
                    side=MarketSide.HOME, entry=OddsQuote(side=h, other=a, line=eff_line)
                ),
                side=MarketSide.HOME,
                line=eff_line,
            ),
        )
        _safe_report(
            reports,
            lambda: run_line_edge_report(
                summary,
                TwoWayMarket(
                    side=MarketSide.AWAY, entry=OddsQuote(side=a, other=h, line=eff_line)
                ),
                side=MarketSide.AWAY,
                line=eff_line,
            ),
        )

    return reports, odds_source


def _parse_markets(markets: Optional[str]) -> tuple[str, ...]:
    """Parse the ``markets`` query param (comma-separated) -> a validated tuple.

    Defaults to all three (moneyline,total,runline) when unset. A token not in the
    valid set is a 422 (a typo'd market should fail loudly, not be silently
    dropped).
    """
    if not markets:
        return _VALID_MARKET_TYPES
    requested = tuple(m.strip() for m in markets.split(",") if m.strip())
    bad = [m for m in requested if m not in _VALID_MARKET_TYPES]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown market(s) {bad}; expected a subset of "
                f"{list(_VALID_MARKET_TYPES)}"
            ),
        )
    return requested or _VALID_MARKET_TYPES


async def _summary_and_winprob(
    request: Request,
    *,
    game_pk: int,
    n_iterations: int,
    base_seed: Optional[int],
    use_cache: bool,
) -> tuple[Any, WinProbability]:
    """Resolve the lineup, run (or reuse) a sim, and return (summary, win_prob).

    REUSES the SIM-355 sim seam: resolve the GameState (404 on a bad game),
    build a GameSpec under the request's factory ref, and run the BatchRunner
    (cache-memoized). The :class:`WinProbability` is derived from the summary's
    raw arrays so the moneyline report consumes a calibrated SIM-330 probability
    (here uncalibrated -- identity -- since this is a best-effort edge surface).
    """
    pool = _get_pool(request)
    state = await _resolve_state_or_error(pool, game_pk)

    factory_ref = resolve_factory_ref(request)
    spec = GameSpec(machine_factory=factory_ref, sim_kwargs=_sim_kwargs_from_state(state))
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
    # SIM-330: a calibrated (here identity / best-effort) home/away win probability
    # derived from the summary's win-rate buckets -- the moneyline report's input.
    win_prob = win_probability(summary)
    return summary, win_prob


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class EdgesResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/edges`` envelope.

    Carries the per-market :class:`EdgeReportModel` list plus the run metadata
    (n_iterations / base_seed) and the per-market ``odds_source`` flags
    ("injected" / "mock") so a consumer knows where each market's prices came from.
    """

    game_pk: int
    n_iterations: int
    base_seed: Optional[int] = None
    markets: list[str] = Field(default_factory=list)
    odds_source: dict[str, str] = Field(default_factory=dict)
    edges: list[EdgeReportModel] = Field(default_factory=list)


class SignalsResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/signals`` envelope.

    Carries the ranked (+EV, EV descending) :class:`BetSignalModel` list plus the
    run metadata, the gate config that produced them (min_edge / min_ev /
    kelly_fraction / max_stake_fraction), and the per-market ``odds_source`` flags.
    """

    game_pk: int
    n_iterations: int
    base_seed: Optional[int] = None
    config: dict[str, float] = Field(default_factory=dict)
    odds_source: dict[str, str] = Field(default_factory=dict)
    signals: list[BetSignalModel] = Field(default_factory=list)


class LineMovementResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/line-movement`` envelope.

    Carries one :class:`LineMovementModel` per (side, book) present in the stored
    ``raw.game_odds`` history for the (game_pk, market_type[, book]).
    """

    game_pk: int
    market_type: str
    book: Optional[str] = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


class ClvSnapshotResponse(BaseModel):
    """The ``GET /api/betting/games/{game_pk}/clv`` envelope.

    A thin snapshot projection of /line-movement: the entry-vs-close CLV of every
    line-movement series that HAS one (a series with < 2 quotes or a missing
    opposite-side price is omitted). Each row carries the side / book identity plus
    the CLV's ``clv_prob`` / ``beat_close`` for a compact "did I beat the close"
    view without the full quote series.
    """

    game_pk: int
    market_type: str
    book: Optional[str] = None
    count: int = 0
    series: list[LineMovementModel] = Field(default_factory=list)


# ===========================================================================
# SIM-367/SIM-339 -- GET /api/betting/games/{game_pk}/edges
# ===========================================================================


@router.get(
    "/games/{game_pk}/edges",
    response_model=EdgesResponse,
    summary="Per-market edge reports (moneyline / total / run-line)",
    description=(
        "Run (or reuse, via the SIM-359 cache) a Monte-Carlo sim for the game and "
        "build the EdgeReports for the requested markets (moneyline / total / "
        "runline, both sides each) off the GameSimSummary + market odds. Odds come "
        "from the injected query params when supplied, else the deterministic mock "
        "provider (flagged per market in odds_source). numpy-free EdgeReportModel "
        "list. 503 if no DB pool, 404 if the lineup cannot be resolved, 422 on a "
        "bad market."
    ),
)
async def get_game_edges(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(200, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: Optional[int] = Query(None, description="Reproducibility seed for the batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
    markets: Optional[str] = Query(
        None, description="Comma-separated subset of moneyline,total,runline (default all)"
    ),
    home_ml: Optional[float] = Query(None, description="Injected home moneyline (American)"),
    away_ml: Optional[float] = Query(None, description="Injected away moneyline (American)"),
    over_ml: Optional[float] = Query(None, description="Injected total over price (American)"),
    under_ml: Optional[float] = Query(None, description="Injected total under price (American)"),
    total_line: Optional[float] = Query(None, description="Injected total line"),
    home_rl_ml: Optional[float] = Query(None, description="Injected home run-line price (American)"),
    away_rl_ml: Optional[float] = Query(None, description="Injected away run-line price (American)"),
    run_line: Optional[float] = Query(None, description="Injected HOME run line (e.g. -1.5)"),
) -> EdgesResponse:
    requested = _parse_markets(markets)
    summary, win_prob = await _summary_and_winprob(
        request,
        game_pk=int(game_pk),
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    reports, odds_source = _build_edge_reports(
        summary,
        win_prob,
        game_pk=int(game_pk),
        markets=requested,
        home_ml=home_ml,
        away_ml=away_ml,
        over_ml=over_ml,
        under_ml=under_ml,
        total_line=total_line,
        home_rl_ml=home_rl_ml,
        away_rl_ml=away_rl_ml,
        run_line=run_line,
    )

    return EdgesResponse(
        game_pk=int(game_pk),
        n_iterations=int(summary.n_iterations),
        base_seed=base_seed,
        markets=list(requested),
        odds_source=odds_source,
        edges=[EdgeReportModel.from_dataclass(r) for r in reports],
    )


# ===========================================================================
# SIM-369 -- GET /api/betting/games/{game_pk}/signals
# ===========================================================================


@router.get(
    "/games/{game_pk}/signals",
    response_model=SignalsResponse,
    summary="Ranked +EV bet-signal recommendations",
    description=(
        "Build the per-market EdgeReports (as /edges), gate them to the +EV set "
        "(strictly positive edge >= min_edge AND ev > min_ev), size each via "
        "fractional Kelly (kelly_fraction, capped at max_stake_fraction), and "
        "return them RANKED by EV descending. min_edge / kelly_fraction are tunable "
        "via query params. numpy-free BetSignalModel list. 503 if no DB pool, 404 "
        "if the lineup cannot be resolved, 422 on a bad market."
    ),
)
async def get_game_signals(
    game_pk: int,
    request: Request,
    n_iterations: int = Query(200, ge=1, le=10000, description="Monte-Carlo iterations"),
    base_seed: Optional[int] = Query(None, description="Reproducibility seed for the batch"),
    use_cache: bool = Query(True, description="Consult/populate the sim-result cache"),
    markets: Optional[str] = Query(
        None, description="Comma-separated subset of moneyline,total,runline (default all)"
    ),
    min_edge: float = Query(0.02, ge=0.0, le=1.0, description="Edge noise floor (gate)"),
    min_ev: float = Query(0.0, ge=-1.0, le=10.0, description="Minimum EV per unit (strict >)"),
    kelly_fraction: float = Query(0.25, ge=0.0, le=1.0, description="Fractional-Kelly multiplier"),
    max_stake_fraction: float = Query(
        0.05, ge=0.0, le=1.0, description="Hard cap on the stake fraction"
    ),
    home_ml: Optional[float] = Query(None, description="Injected home moneyline (American)"),
    away_ml: Optional[float] = Query(None, description="Injected away moneyline (American)"),
    over_ml: Optional[float] = Query(None, description="Injected total over price (American)"),
    under_ml: Optional[float] = Query(None, description="Injected total under price (American)"),
    total_line: Optional[float] = Query(None, description="Injected total line"),
    home_rl_ml: Optional[float] = Query(None, description="Injected home run-line price (American)"),
    away_rl_ml: Optional[float] = Query(None, description="Injected away run-line price (American)"),
    run_line: Optional[float] = Query(None, description="Injected HOME run line (e.g. -1.5)"),
) -> SignalsResponse:
    requested = _parse_markets(markets)
    summary, win_prob = await _summary_and_winprob(
        request,
        game_pk=int(game_pk),
        n_iterations=n_iterations,
        base_seed=base_seed,
        use_cache=use_cache,
    )

    reports, odds_source = _build_edge_reports(
        summary,
        win_prob,
        game_pk=int(game_pk),
        markets=requested,
        home_ml=home_ml,
        away_ml=away_ml,
        over_ml=over_ml,
        under_ml=under_ml,
        total_line=total_line,
        home_rl_ml=home_rl_ml,
        away_rl_ml=away_rl_ml,
        run_line=run_line,
    )

    config = BetSignalConfig(
        min_edge=float(min_edge),
        min_ev=float(min_ev),
        kelly_fraction=float(kelly_fraction),
        max_stake_fraction=float(max_stake_fraction),
    )
    signals = bet_signals_from_edges(reports, config=config)

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
        odds_source=odds_source,
        signals=[BetSignalModel.from_dataclass(s) for s in signals],
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
                f"unknown market_type {market_type!r}; expected one of "
                f"{list(_VALID_MARKET_TYPES)}"
            ),
        )
    return market_type


async def _fetch_movements(
    request: Request, *, game_pk: int, market_type: str, book: Optional[str]
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
    return await fetch_line_movement(
        pool, game_pk=int(game_pk), market_type=market_type, book=book
    )


@router.get(
    "/games/{game_pk}/line-movement",
    response_model=LineMovementResponse,
    summary="Opening->closing line-movement time-series per side/book",
    description=(
        "Read the persisted raw.game_odds history for the (game_pk, market_type"
        "[, book]) and build the SIM-368 line-movement time-series: one series per "
        "(side, book) with the ordered quotes, the per-step + opening->closing "
        "deltas, the running implied-prob surface, the steam direction, the "
        "sharp-consensus flag, and the entry-vs-close CLV. numpy-free "
        "LineMovementModel list. 503 if no DB pool, 422 on a bad market_type."
    ),
)
async def get_game_line_movement(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: Optional[str] = Query(None, description="Restrict to one book (else all books)"),
) -> LineMovementResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(
        request, game_pk=int(game_pk), market_type=mt, book=book
    )
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
        "an entry-vs-close CLV (>= 2 quotes with both opposite-side prices). Each "
        "model's clv.clv_prob / clv.beat_close answers 'did the opening price beat "
        "the close' for that side/book. numpy-free. 503 if no DB pool, 422 on a "
        "bad market_type."
    ),
)
async def get_game_clv(
    game_pk: int,
    request: Request,
    market_type: str = Query("moneyline", description="moneyline | runline | total"),
    book: Optional[str] = Query(None, description="Restrict to one book (else all books)"),
) -> ClvSnapshotResponse:
    mt = _validate_market_type(market_type)
    movements = await _fetch_movements(
        request, game_pk=int(game_pk), market_type=mt, book=book
    )
    # Only the series that actually have a computed CLV (the snapshot's point).
    with_clv = [m for m in movements if m.clv is not None]
    return ClvSnapshotResponse(
        game_pk=int(game_pk),
        market_type=mt,
        book=book,
        count=len(with_clv),
        series=[LineMovementModel.from_dataclass(m) for m in with_clv],
    )


__all__ = [
    "router",
    "EdgesResponse",
    "SignalsResponse",
    "LineMovementResponse",
    "ClvSnapshotResponse",
]
