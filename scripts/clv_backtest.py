#!/usr/bin/env python
"""
scripts/clv_backtest.py
=======================
SIM-429 — the **CLV (Closing Line Value) backtest scoreboard**: the gold-standard
validation that the simulator's +EV picks systematically BEAT THE CLOSE.

WHAT THIS IS
------------
A "hedge fund scoreboard": for a slate of COMPLETED games that already have
ingested odds, it (1) runs N-iteration sims to derive the model's prices, (2)
identifies the bets the model would PLACE — the side with positive model edge at
the OPENING line — and (3) measures whether those placed bets beat the CLOSING
line. Closing Line Value (CLV) is the canonical edge metric: a bet beats the close
when the de-vigged probability of the side you took ROSE from the opening line to
the closing line (you locked a better price than the final market consensus).

  * ``CLV > 0`` (``beat_close``) == you beat the close on that bet.
  * The HEADLINE metric is ``beat_close_rate`` — the fraction of the model's
    PLACED bets that beat the close. ~50% means no edge; a real edge shows as
    52–55%+ systematically.

This is the SIM-429 CLV backtest — the sub-ticket the SIM-435 historical-odds
backfill unblocks (you need both an opening AND a closing line per market to score
CLV). It is an OFFLINE validation job: it replays real sims, so it is slow — use
``--max-games`` for a smoke run. It never serves a request.

HOW IT REUSES THE EXISTING SEAMS (no re-invention)
--------------------------------------------------
  * **Sim run + game summary + win prob** — the SAME machinery the API / batch
    runner use: ``_resolve_state_or_error`` resolves a game's lineup into a
    GameState; ``record_game_plays`` (the ``api/routes/betting.py`` /
    ``scripts/validate_props.py`` path) replays N iterations under the production
    factory ref, collecting one ``GameSimResult`` per iteration; those build a
    ``GameSimSummary`` (carrying the per-iteration ``home_scores`` /
    ``away_scores`` / ``total_scores`` arrays) and, via
    ``simulation.win_probability.win_probability``, a calibrated ``WinProbability``.
  * **Per-player prop PMFs** — ``PropDistributionSet.from_results`` over the
    per-iteration boxscores (exactly the ``validate_props`` path); each
    ``PropDistribution`` answers ``p_over(line)`` / ``p_under(line)``.
  * **CLV math** — ``betting.clv_engine``: ``moneyline_edge_report`` /
    ``total_over_under_edge_report`` / ``run_line_edge_report`` / ``prop_edge_report``
    build the model edge + EV and let us PICK the +EV side; ``clv_from_odds``
    computes the entry→close CLV for the placed side (``CLV.clv_prob > 0`` ==
    beat the close).
  * **Odds I/O** — read directly from Postgres (``raw.game_odds`` /
    ``raw.prop_odds``), pulling the OPENING and CLOSING two-way prices per market.

THE MARKETS
-----------
  * Game: moneyline (home/away ML), total (over/under at ``total_line``), run-line
    (home/away at ``home_spread``).
  * Props (the SIM-134 7-market vocab; odds ``prop_stat`` → model
    ``PropDistribution`` stat): strikeouts→K, walks→BB, earned_runs→ER, hits→H,
    home_runs→HR, total_bases→TB, rbis→RBI.

PURE vs. IMPACTFUL
------------------
The per-bet CLV decision (:func:`evaluate_two_way_market`) and the scoreboard
aggregation (:func:`aggregate_scoreboard`) are PURE — no DB, no sim, no RNG — so
they are unit-testable on synthetic prices. Everything DB/sim-touching lives in
the ``_fetch_*`` readers and :func:`run`. The trust labels
(:data:`MARKET_TRUST`) and the prop vocab (:data:`PROP_VOCAB_MAP`) are plain data.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/clv_backtest.py --seasons 2024 --max-games 50 --iterations 100
    python scripts/clv_backtest.py --seasons 2023 2024 --markets game
    python scripts/clv_backtest.py --seasons 2024 --markets props --min-edge 0.02
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from betting.clv_engine import (  # noqa: E402
    EdgeReport,
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    clv_from_odds,
    moneyline_edge_report,
    prop_edge_report,
    run_line_edge_report,
    total_over_under_edge_report,
)

log = logging.getLogger("clv_backtest")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)
DEFAULT_DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_OUTPUT = os.environ.get("CLV_BACKTEST_PATH", "/data/clv_backtest.json")

#: The production machine factory (dotted ref) — the SAME one the API serves with.
_FACTORY_REF = "simulation.production_factory:production_machine_factory"

#: The two line_types every CLV computation needs (entry + close).
LINE_TYPES: tuple[str, ...] = ("opening", "closing")

# ===========================================================================
# Vocab: odds prop_stat -> model PropDistribution stat (the SIM-134 7 markets)
# ===========================================================================

#: Maps the ``raw.prop_odds.prop_stat`` vocabulary to the
#: ``simulation.prop_distributions`` model-prop names. Covers exactly the 7
#: markets the SIM-134 CHECK constraint enforces.
PROP_VOCAB_MAP: dict[str, str] = {
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
}

# ===========================================================================
# Trust labels: how much to trust each market's CLV (Betting-Analyst tiers)
# ===========================================================================

#: market key -> trust tier. The market key is the game market_type
#: ('moneyline'/'total'/'runline') OR the MODEL prop stat (K/BB/ER/H/HR/TB/RBI).
#: Tiers (per the §11 realism residual + SIM-429 over-prediction notes):
#:   trustworthy  — box rate stats within ~4% of MLB (H/HR/TB).
#:   loose        — moneyline (win-prob fit over a bounded sample).
#:   caution      — total/runline (the hits→runs conversion gap lives here).
#:   untrustworthy— K/BB/ER/RBI (over-predicted props / not validated — SIM-429).
MARKET_TRUST: dict[str, str] = {
    # trustworthy
    "H": "trustworthy",
    "HR": "trustworthy",
    "TB": "trustworthy",
    # loose
    "moneyline": "loose",
    # caution
    "total": "caution",
    "runline": "caution",
    # untrustworthy
    "K": "untrustworthy",
    "BB": "untrustworthy",
    "ER": "untrustworthy",
    "RBI": "untrustworthy",
}


def trust_label(market: str) -> str:
    """The trust tier for a market key (the game market_type or a model prop stat).

    Unknown markets fall back to ``"unknown"`` rather than raising, so a new
    market never crashes the scoreboard.
    """
    return MARKET_TRUST.get(str(market), "unknown")


# ===========================================================================
# Per-bet record + the pure per-market CLV evaluation
# ===========================================================================


@dataclass(frozen=True, slots=True)
class BetRecord:
    """One scored row of the backtest: the bet the model PLACED on one market.

    ``placed`` is False for a "no-bet" row (no side had positive model edge >=
    the floor) — such a row counts toward ``n_markets_priced`` but not toward
    ``n_bets_placed`` / the beat-close rate. When ``placed`` is True, ``side`` /
    ``model_edge`` / ``model_ev`` describe the chosen side and ``clv_prob`` /
    ``beat_close`` carry its entry→close CLV.
    """

    game_pk: int
    #: Market key: 'moneyline'/'total'/'runline' OR a model prop stat (K/H/...).
    market: str
    #: Coarse group for aggregation: 'moneyline'/'total'/'runline'/'prop'.
    market_type: str
    placed: bool
    side: str | None = None
    line: float | None = None
    model_edge: float | None = None
    model_ev: float | None = None
    clv_prob: float | None = None
    beat_close: bool | None = None
    #: Optional player id (props only) for provenance.
    player_id: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        """A JSON-safe dict of this record (all fields are already plain types)."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TwoWayPrices:
    """An opening + closing two-way American-odds quote for one market.

    ``side`` / ``other`` name the two legs the same way for opening and closing
    (e.g. (home_ml, away_ml) or (over_ml, under_ml)). All four prices must be
    present (non-None) for the market to be scoreable; ``line`` is the market line
    (None for a moneyline).
    """

    open_side: float
    open_other: float
    close_side: float
    close_other: float
    line: float | None = None


def _pick_side(
    report_side: EdgeReport,
    report_other: EdgeReport,
    *,
    min_edge: float,
) -> EdgeReport | None:
    """Pick the side the model would BET (the larger positive model edge >= floor).

    Both sides' edge reports are built on the OPENING quote. The model places a
    bet on whichever side has the greater model edge, provided that edge clears
    ``min_edge`` and is strictly positive; otherwise no bet (None) — a market the
    model passes on.
    """
    best = max((report_side, report_other), key=lambda r: r.edge)
    if best.edge > 0.0 and best.edge >= float(min_edge):
        return best
    return None


def evaluate_two_way_market(
    game_pk: int,
    market: str,
    market_type: str,
    side_a: MarketSide,
    side_b: MarketSide,
    report_for_side: Any,
    prices: TwoWayPrices,
    *,
    min_edge: float = 0.0,
    player_id: int | None = None,
) -> BetRecord:
    """PURE: pick the model's +EV side on the OPENING line and score its CLV.

    ``report_for_side(side, entry_market)`` is a callable that returns the
    :class:`EdgeReport` for ``side`` given a :class:`TwoWayMarket` built on the
    OPENING quote (the caller closes over the sim output — a ``WinProbability`` /
    ``GameSimSummary`` / ``PropDistribution`` — so this function stays free of any
    sim/DB dependency). The steps:

      1. build both sides' edge reports on the OPENING two-way quote;
      2. PICK the side with the larger positive model edge >= ``min_edge`` (else a
         'no-bet' record with ``placed=False``);
      3. for the placed side, compute ``clv = clv_from_odds(entry=opening price on
         that side, close=closing price on that side)`` and record
         ``clv_prob`` / ``beat_close = clv.clv_prob > 0``.

    Deterministic given the reports + prices. ``side_a`` is the leg whose OPENING
    price is ``prices.open_side``; ``side_b`` the other leg.
    """
    # The OPENING two-way market for each side (side price first, other second).
    entry_a = TwoWayMarket(
        side=side_a,
        entry=OddsQuote(side=prices.open_side, other=prices.open_other, line=prices.line),
    )
    entry_b = TwoWayMarket(
        side=side_b,
        entry=OddsQuote(side=prices.open_other, other=prices.open_side, line=prices.line),
    )
    report_a = report_for_side(side_a, entry_a)
    report_b = report_for_side(side_b, entry_b)

    chosen = _pick_side(report_a, report_b, min_edge=min_edge)
    if chosen is None:
        return BetRecord(
            game_pk=int(game_pk),
            market=market,
            market_type=market_type,
            placed=False,
            line=prices.line,
            player_id=player_id,
        )

    # Map the chosen side back to its opening/closing entry+other prices.
    if chosen.side is side_a:
        entry_side, entry_other = prices.open_side, prices.open_other
        close_side, close_other = prices.close_side, prices.close_other
    else:
        entry_side, entry_other = prices.open_other, prices.open_side
        close_side, close_other = prices.close_other, prices.close_side

    clv = clv_from_odds(
        entry_side_american=entry_side,
        entry_other_american=entry_other,
        close_side_american=close_side,
        close_other_american=close_other,
    )
    return BetRecord(
        game_pk=int(game_pk),
        market=market,
        market_type=market_type,
        placed=True,
        side=chosen.side.value,
        line=prices.line,
        model_edge=float(chosen.edge),
        model_ev=float(chosen.ev),
        clv_prob=float(clv.clv_prob),
        beat_close=bool(clv.clv_prob > 0.0),
        player_id=player_id,
    )


# ===========================================================================
# Scoreboard aggregation (PURE)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ScoreboardRow:
    """Aggregated CLV stats for one group (a market_type, a prop, or 'overall').

    The HEADLINE metric is :attr:`beat_close_rate` — the fraction of PLACED bets
    that beat the close (clv_prob > 0). ``mean_clv_prob`` / ``mean_model_edge`` are
    means over the placed bets only (0.0 when none were placed).
    """

    group: str
    trust: str
    n_games: int
    n_markets_priced: int
    n_bets_placed: int
    beat_close_rate: float
    mean_clv_prob: float
    mean_model_edge: float

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


def _row_for(group: str, trust: str, bets: Sequence[BetRecord]) -> ScoreboardRow:
    """Aggregate one bucket of :class:`BetRecord`s into a :class:`ScoreboardRow`.

    ``n_markets_priced`` counts every record (placed or no-bet — the market WAS
    priced); ``n_bets_placed`` counts only placed bets; ``beat_close_rate`` /
    ``mean_clv_prob`` / ``mean_model_edge`` are over the placed bets only.
    ``n_games`` is the distinct game count in the bucket.
    """
    placed = [b for b in bets if b.placed]
    n_placed = len(placed)
    if n_placed:
        beat = sum(1 for b in placed if b.beat_close)
        beat_close_rate = beat / n_placed
        mean_clv = sum(float(b.clv_prob or 0.0) for b in placed) / n_placed
        mean_edge = sum(float(b.model_edge or 0.0) for b in placed) / n_placed
    else:
        beat_close_rate = 0.0
        mean_clv = 0.0
        mean_edge = 0.0
    return ScoreboardRow(
        group=group,
        trust=trust,
        n_games=len({b.game_pk for b in bets}),
        n_markets_priced=len(bets),
        n_bets_placed=n_placed,
        beat_close_rate=beat_close_rate,
        mean_clv_prob=mean_clv,
        mean_model_edge=mean_edge,
    )


def aggregate_scoreboard(bets: Sequence[BetRecord]) -> dict[str, Any]:
    """PURE: roll a list of :class:`BetRecord`s into the full scoreboard.

    Returns a dict with:
      * ``overall`` — one :class:`ScoreboardRow` over every bet;
      * ``by_market`` — one row per distinct ``market`` key (the per-market_type
        game markets + each prop stat), each tagged with its trust label, sorted
        by trust tier then market key for a readable table.

    The HEADLINE per row is ``beat_close_rate``. Deterministic; no DB/sim.
    """
    bets = list(bets)
    overall = _row_for("overall", "—", bets)

    by_market_key: dict[str, list[BetRecord]] = {}
    for b in bets:
        by_market_key.setdefault(b.market, []).append(b)

    # Sort by trust tier (best first) then market key for a stable, readable table.
    _trust_order = {"trustworthy": 0, "loose": 1, "caution": 2, "untrustworthy": 3, "unknown": 4}
    rows = [
        _row_for(market, trust_label(market), bucket) for market, bucket in by_market_key.items()
    ]
    rows.sort(key=lambda r: (_trust_order.get(r.trust, 9), r.group))

    return {
        "overall": overall.to_jsonable(),
        "by_market": [r.to_jsonable() for r in rows],
    }


# ===========================================================================
# Readable scoreboard table
# ===========================================================================


def format_scoreboard(scoreboard: dict[str, Any], *, params: dict[str, Any]) -> str:
    """Render the aggregated scoreboard as a readable, trust-grouped text table."""
    lines = [
        "=" * 84,
        "SIM-429 CLV BACKTEST SCOREBOARD  (beat_close_rate = % of placed bets that beat the close)",
        "=" * 84,
        f"seasons={params.get('seasons')}  iterations={params.get('iterations')}  "
        f"markets={params.get('markets')}  min_edge={params.get('min_edge')}  "
        f"base_seed={params.get('base_seed')}",
        "",
    ]
    header = (
        f"{'market':<14}{'trust':<14}{'games':>6}{'priced':>8}"
        f"{'placed':>8}{'beat%':>9}{'meanCLV':>10}{'meanEdge':>10}"
    )

    def _fmt_row(r: dict[str, Any]) -> str:
        return (
            f"{r['group']:<14}{r['trust']:<14}{r['n_games']:>6}{r['n_markets_priced']:>8}"
            f"{r['n_bets_placed']:>8}{r['beat_close_rate'] * 100:>8.1f}%"
            f"{r['mean_clv_prob']:>10.4f}{r['mean_model_edge']:>10.4f}"
        )

    o = scoreboard["overall"]
    lines += [
        "--- OVERALL ---",
        header,
        _fmt_row(o),
        "",
        "--- BY MARKET (grouped by trust tier) ---",
        header,
    ]
    last_trust = None
    for r in scoreboard["by_market"]:
        if r["trust"] != last_trust:
            lines.append(f"  [{r['trust']}]")
            last_trust = r["trust"]
        lines.append(_fmt_row(r))
    lines.append("=" * 84)
    return "\n".join(lines)


# ===========================================================================
# DB readers (lazy asyncpg import — the unit test never reaches these)
# ===========================================================================


async def _fetch_final_games(dsn: str, seasons: list[int], max_games: int | None) -> list[int]:
    """Return ``game_pk``s for Final games in the requested seasons (ordered).

    SIM-432: the live schema stores the final score in ``home_score_final`` /
    ``away_score_final`` — we gate on those being present (a real Final game).
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
            ORDER BY game_pk
            {limit}
            """
        )
    finally:
        await conn.close()
    return [int(r["game_pk"]) for r in rows]


async def _fetch_game_odds(pool, game_pk: int) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{market_type: {line_type: row_dict}}`` for one game's game odds.

    Reads the most-recent ``raw.game_odds`` row per (market_type, line_type) — the
    closing snapshot at each line_type (there can be many 'opening' / 'closing'
    rows; the latest ``fetched_at`` is the authoritative one). Only the opening /
    closing line_types are kept (the two CLV reference points).
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (market_type, line_type)
               market_type, line_type,
               home_ml, away_ml,
               home_spread, home_spread_ml, away_spread, away_spread_ml,
               total_line, over_ml, under_ml
        FROM raw.game_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
        ORDER BY market_type, line_type, fetched_at DESC
        """,
        int(game_pk),
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["market_type"]), {})[str(r["line_type"])] = dict(r)
    return out


async def _fetch_prop_odds(pool, game_pk: int) -> dict[tuple[int, str], dict[str, dict[str, Any]]]:
    """Return ``{(player_id, prop_stat): {line_type: row_dict}}`` for one game.

    Reads the latest ``raw.prop_odds`` row per (player, prop_stat, line_type),
    restricted to the opening / closing line_types.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (player_id, prop_stat, line_type)
               player_id, prop_stat, line_type, line, over_ml, under_ml
        FROM raw.prop_odds
        WHERE game_pk = $1 AND line_type IN ('opening', 'closing')
        ORDER BY player_id, prop_stat, line_type, fetched_at DESC
        """,
        int(game_pk),
    )
    out: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        key = (int(r["player_id"]), str(r["prop_stat"]))
        out.setdefault(key, {})[str(r["line_type"])] = dict(r)
    return out


# ===========================================================================
# Per-game scoring (wires the sim output into the pure evaluation)
# ===========================================================================


def _game_prices(
    odds: dict[str, dict[str, dict[str, Any]]],
    market_type: str,
    side_col: str,
    other_col: str,
    line_col: str | None,
) -> TwoWayPrices | None:
    """Build :class:`TwoWayPrices` for a game market from its opening/closing rows.

    Returns None when either the opening or closing row is missing, or any of the
    two-way prices is NULL (an unscoreable market).
    """
    by_lt = odds.get(market_type)
    if not by_lt:
        return None
    op = by_lt.get("opening")
    cl = by_lt.get("closing")
    if op is None or cl is None:
        return None
    open_side = op.get(side_col)
    open_other = op.get(other_col)
    close_side = cl.get(side_col)
    close_other = cl.get(other_col)
    if None in (open_side, open_other, close_side, close_other):
        return None
    line = op.get(line_col) if line_col else None
    return TwoWayPrices(
        open_side=float(open_side),
        open_other=float(open_other),
        close_side=float(close_side),
        close_other=float(close_other),
        line=None if line is None else float(line),
    )


def score_game_markets(
    game_pk: int,
    win_prob: Any,
    summary: Any,
    odds: dict[str, dict[str, dict[str, Any]]],
    *,
    min_edge: float = 0.0,
) -> list[BetRecord]:
    """Score the three GAME markets (moneyline / total / runline) for one game.

    Wires the sim output (``win_prob`` / ``summary``) into the pure
    :func:`evaluate_two_way_market` via per-market edge-report closures. A market
    with a missing/NULL opening-or-closing price is skipped (not in the result).
    Degenerate sim probabilities (0/1, which ``clv_engine`` raises on) are caught
    so one bad market never sinks the game.
    """
    records: list[BetRecord] = []

    # --- moneyline (HOME/AWAY at home_ml/away_ml) ---
    prices = _game_prices(odds, "moneyline", "home_ml", "away_ml", None)
    if prices is not None:
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "moneyline",
                    "moneyline",
                    MarketSide.HOME,
                    MarketSide.AWAY,
                    lambda side, mkt: moneyline_edge_report(win_prob, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s moneyline skipped (degenerate): %s", game_pk, exc)

    # --- total (OVER/UNDER at total_line) ---
    prices = _game_prices(odds, "total", "over_ml", "under_ml", "total_line")
    if prices is not None and prices.line is not None:
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "total",
                    "total",
                    MarketSide.OVER,
                    MarketSide.UNDER,
                    lambda side, mkt: total_over_under_edge_report(summary, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s total skipped (degenerate): %s", game_pk, exc)

    # --- runline (HOME/AWAY at home_spread) ---
    prices = _game_prices(odds, "runline", "home_spread_ml", "away_spread_ml", "home_spread")
    if prices is not None and prices.line is not None:
        try:
            eff_line = prices.line
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    "runline",
                    "runline",
                    MarketSide.HOME,
                    MarketSide.AWAY,
                    lambda side, mkt: run_line_edge_report(summary, mkt, side=side, line=eff_line),
                    prices,
                    min_edge=min_edge,
                )
            )
        except ValueError as exc:
            log.info("game %s runline skipped (degenerate): %s", game_pk, exc)

    return records


def score_prop_markets(
    game_pk: int,
    pset: Any,
    prop_odds: dict[tuple[int, str], dict[str, dict[str, Any]]],
    *,
    min_edge: float = 0.0,
) -> list[BetRecord]:
    """Score the player-prop markets for one game.

    For each (player, odds prop_stat) with both an opening and a closing line, map
    the odds stat to the model prop (:data:`PROP_VOCAB_MAP`), look up that player's
    :class:`PropDistribution`, and score OVER/UNDER via the pure
    :func:`evaluate_two_way_market`. Players/props with no model distribution (the
    player never appeared) or a missing/NULL price are skipped.
    """
    records: list[BetRecord] = []
    for (player_id, odds_stat), by_lt in prop_odds.items():
        model_stat = PROP_VOCAB_MAP.get(odds_stat)
        if model_stat is None:
            continue
        op = by_lt.get("opening")
        cl = by_lt.get("closing")
        if op is None or cl is None:
            continue
        open_over, open_under = op.get("over_ml"), op.get("under_ml")
        close_over, close_under = cl.get("over_ml"), cl.get("under_ml")
        line = op.get("line")
        if None in (open_over, open_under, close_over, close_under, line):
            continue

        dist = pset.get(int(player_id), model_stat) if pset is not None else None
        if dist is None:
            continue

        prices = TwoWayPrices(
            open_side=float(open_over),
            open_other=float(open_under),
            close_side=float(close_over),
            close_other=float(close_under),
            line=float(line),
        )
        try:
            records.append(
                evaluate_two_way_market(
                    game_pk,
                    model_stat,
                    "prop",
                    MarketSide.OVER,
                    MarketSide.UNDER,
                    lambda side, mkt, _d=dist: prop_edge_report(_d, mkt, side=side),
                    prices,
                    min_edge=min_edge,
                    player_id=int(player_id),
                )
            )
        except ValueError as exc:
            log.info(
                "game %s prop %s/%s skipped (degenerate): %s", game_pk, player_id, model_stat, exc
            )
    return records


# ===========================================================================
# Sim replay (reuses the validate_props / betting.py seam)
# ===========================================================================


def _collect_game_results(state, n_iter: int, base_seed: int | None) -> list:
    """Replay one already-resolved game N times; return the per-iteration results.

    The SAME ``record_game_plays`` seam ``scripts/validate_props.py`` uses: builds
    the live machine from the production factory ref + the resolved GameState's
    sim_kwargs and runs ``simulate_game`` at each derived seed, collecting the
    ``GameSimResult`` (carrying ``.boxscore`` + the score) per iteration. Sync +
    CPU-bound, so the async caller offloads it via ``asyncio.to_thread``.
    """
    from api.routes.games import _sim_kwargs_from_state
    from simulation.batch_runner import derive_seed
    from simulation.play_recorder import record_game_plays

    sim_kwargs = _sim_kwargs_from_state(state)
    results = []
    for i in range(int(n_iter)):
        seed = derive_seed(base_seed, i)
        result, _plays = record_game_plays(
            factory_ref=_FACTORY_REF, seed=seed, sim_kwargs=sim_kwargs
        )
        results.append(result)
    return results


@dataclass
class _Counters:
    """Running tallies for the run summary log."""

    games_attempted: int = 0
    games_scored: int = 0
    games_no_odds: int = 0
    games_unresolved: int = 0
    bets: list[BetRecord] = field(default_factory=list)


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from api.routes.games import _resolve_state_or_error
    from simulation.prop_distributions import PropDistributionSet
    from simulation.results import GameSimSummary
    from simulation.win_probability import win_probability

    seasons = sorted({int(s) for s in args.seasons})
    do_game = args.markets in ("game", "all")
    do_props = args.markets in ("props", "all")
    log.info(
        "SIM-429 CLV backtest — seasons=%s iterations=%d markets=%s min_edge=%s",
        seasons,
        args.iterations,
        args.markets,
        args.min_edge,
    )

    game_pks = await _fetch_final_games(args.dsn, seasons, args.max_games)
    log.info("Found %d completed games to backtest.", len(game_pks))
    if not game_pks:
        log.warning("No completed games found — nothing to backtest.")

    counters = _Counters()
    pool = None
    try:
        if game_pks:
            pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        for game_pk in game_pks:
            counters.games_attempted += 1

            # Read odds first — skip a game with NO odds rows (log + count).
            game_odds = await _fetch_game_odds(pool, game_pk) if do_game else {}
            prop_odds = await _fetch_prop_odds(pool, game_pk) if do_props else {}
            if not game_odds and not prop_odds:
                counters.games_no_odds += 1
                log.info("skip game %s (no odds rows)", game_pk)
                continue

            try:
                state = await _resolve_state_or_error(pool, game_pk)
            except Exception as exc:  # noqa: BLE001 — skip un-resolvable games
                counters.games_unresolved += 1
                log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
                continue

            results = await asyncio.to_thread(
                _collect_game_results, state, args.iterations, args.base_seed
            )
            if not results:
                continue

            summary = GameSimSummary.from_results(results)
            wp = win_probability(summary)

            if do_game and game_odds:
                counters.bets.extend(
                    score_game_markets(game_pk, wp, summary, game_odds, min_edge=args.min_edge)
                )
            if do_props and prop_odds:
                pset = PropDistributionSet.from_results(results)
                counters.bets.extend(
                    score_prop_markets(game_pk, pset, prop_odds, min_edge=args.min_edge)
                )

            counters.games_scored += 1
            if counters.games_scored % 25 == 0:
                log.info(
                    "  scored %d/%d games (%d bet rows) ...",
                    counters.games_scored,
                    len(game_pks),
                    len(counters.bets),
                )
    finally:
        if pool is not None:
            await pool.close()

    scoreboard = aggregate_scoreboard(counters.bets)
    params = {
        "seasons": seasons,
        "iterations": int(args.iterations),
        "markets": args.markets,
        "min_edge": float(args.min_edge),
        "base_seed": int(args.base_seed),
        "dsn": args.dsn,
        "duckdb": args.duckdb,
        "factory_ref": _FACTORY_REF,
    }
    print(format_scoreboard(scoreboard, params=params))

    report = {
        "params": params,
        "counters": {
            "games_attempted": counters.games_attempted,
            "games_scored": counters.games_scored,
            "games_no_odds": counters.games_no_odds,
            "games_unresolved": counters.games_unresolved,
            "n_bets": len(counters.bets),
        },
        "scoreboard": scoreboard,
        "bets": [b.to_jsonable() for b in counters.bets],
    }
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    log.info("Wrote CLV backtest report -> %s", args.output)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CLV (Closing Line Value) backtest scoreboard (SIM-429)."
    )
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to backtest.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument("--iterations", type=int, default=100, help="Monte-Carlo iters per game.")
    p.add_argument(
        "--markets",
        choices=("game", "props", "all"),
        default="all",
        help="Which markets to score (default all).",
    )
    p.add_argument(
        "--min-edge",
        type=float,
        default=0.0,
        help="Minimum model edge to PLACE a bet (default 0.0).",
    )
    p.add_argument("--base-seed", type=int, default=0, help="Reproducibility seed.")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="CLV backtest JSON report path.")
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + odds).")
    p.add_argument("--duckdb", default=DEFAULT_DUCKDB_PATH, help="Sim DuckDB path.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
