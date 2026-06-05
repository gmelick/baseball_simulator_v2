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

ACROSS-GAMES PARALLELISM
------------------------
A full-season backtest is feasible because the slate is fanned out OVER GAMES, not
over a single game's iterations: per-game cost is the irreducible per-PA full-pool
scoring (~1.5 s/iter) and the host is core-bound (~6 cores), so one game can't go
below ~30 s — but ~6 GAMES AT ONCE gives ~6× throughput. ``--workers N`` (default 6)
maps each WHOLE game onto a ``forkserver`` ``ProcessPoolExecutor`` worker that runs
it serially (resolve → N sims → prop dists → read odds → CLV bet records); each
worker holds only its own ~373 MB full-pool sampler cache (SIM-430), so 6 workers fit
in ~2.2 GB and the parent stays lean (loads NO engine artifacts). ``--workers 1`` is
the SERIAL in-process fallback (the no-pool debug mode + the byte-identical-verify
reference). The run is byte-identical to serial for the same (game set, base_seed,
iterations): each game is independent + deterministic from its per-iteration seed, so
the only thing parallelism changes — completion order — never affects any bet record.

USAGE
-----
    # In the app container (Postgres at db:5432, DuckDB at /data/...):
    python scripts/clv_backtest.py --seasons 2024 --max-games 50 --iterations 100
    python scripts/clv_backtest.py --seasons 2023 2024 --markets game
    python scripts/clv_backtest.py --seasons 2024 --markets props --min-edge 0.02
    # full season, 6 games at once (the across-games parallel mode):
    python scripts/clv_backtest.py --seasons 2024 --workers 6 --iterations 100
    # serial fallback (no pool — debug / byte-identical verify reference):
    python scripts/clv_backtest.py --seasons 2024 --max-games 2 --workers 1
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

#: ACROSS-GAMES default worker count. The host is ~6-core (SIM-430), and the
#: per-game cost is the irreducible per-PA full-pool scoring, so ~6 GAMES AT ONCE
#: is the throughput lever; each forkserver worker holds ~373 MB → ~2.2 GB total.
DEFAULT_WORKERS = 6

#: SIM-430: the pool's multiprocessing start method (mirrors
#: ``simulation.batch_runner``). ``forkserver`` forks workers from a lean ~30 MB
#: server so they do NOT COW-inherit any engine artifacts the parent might hold —
#: each worker loads its own ~373 MB bundle. Override via ``SIM_MP_START_METHOD``.
_MP_START_METHOD = os.environ.get("SIM_MP_START_METHOD", "forkserver").strip().lower()


def _pool_mp_context() -> Any:
    """Return the multiprocessing context for the across-games pool (SIM-430).

    Defaults to ``forkserver`` (workers fork from a lean server, never COW-inherit a
    big parent); falls back to the platform-default context when the requested
    method is unavailable (e.g. a host without forkserver). Mirrors
    :func:`simulation.batch_runner._pool_mp_context`.
    """
    import multiprocessing

    try:
        if _MP_START_METHOD in multiprocessing.get_all_start_methods():
            return multiprocessing.get_context(_MP_START_METHOD)
    except Exception:  # noqa: BLE001 — any oddity → platform default
        pass
    return multiprocessing.get_context()


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

    @classmethod
    def from_jsonable(cls, d: dict[str, Any]) -> BetRecord:
        """Rebuild a :class:`BetRecord` from its :meth:`to_jsonable` dict.

        The parent uses this to reconstruct the records a parallel worker returns as
        plain dicts (the picklable across-process payload), so the aggregation runs
        on the SAME :class:`BetRecord`s either execution mode produces.
        """
        return cls(**d)


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

    This is the SINGLE serial per-game replay both execution modes use, so the
    per-iteration seeds (``derive_seed(base_seed, i)``) — and therefore the sims —
    are IDENTICAL whether a game runs in the parent (``--workers 1``) or inside a
    parallel worker (``--workers > 1``).
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


# ===========================================================================
# The per-game unit of work — shared by the serial AND parallel execution paths
# ===========================================================================


async def _score_one_game(
    pool: Any,
    game_pk: int,
    *,
    do_game: bool,
    do_props: bool,
    iterations: int,
    base_seed: int,
    min_edge: float,
) -> tuple[list[BetRecord], str]:
    """Resolve, replay, and score ONE game; return ``(bet_records, status)``.

    This is the ENTIRE per-game pipeline, factored out of :func:`run`'s old loop so
    BOTH the serial in-process path and a parallel worker call the exact same code:

      1. read the game's opening+closing odds (``raw.game_odds`` / ``raw.prop_odds``);
      2. resolve the :class:`GameState` (lineup → state);
      3. replay ``iterations`` sims via the SAME :func:`_collect_game_results` seam
         (per-iteration seed = ``derive_seed(base_seed, i)`` — deterministic per game);
      4. build the :class:`GameSimSummary` + calibrated :class:`WinProbability`
         (+ the :class:`PropDistributionSet` for props);
      5. produce the per-bet records via the pure ``score_game_markets`` /
         ``score_prop_markets``.

    ``status`` is one of ``"scored"`` / ``"no_odds"`` / ``"unresolved"`` / ``"empty"``
    so the caller can keep the SAME run counters. A degenerate/failed game yields no
    bets and a non-``"scored"`` status; it NEVER raises out of here.

    Deterministic: the only RNG is the per-iteration seed derived from ``base_seed``,
    so the returned records are byte-identical regardless of where this runs.
    """
    from api.routes.games import _resolve_state_or_error
    from simulation.prop_distributions import PropDistributionSet
    from simulation.results import GameSimSummary
    from simulation.win_probability import win_probability

    # Read odds first — a game with NO odds rows is skipped (counts as 'no_odds').
    game_odds = await _fetch_game_odds(pool, game_pk) if do_game else {}
    prop_odds = await _fetch_prop_odds(pool, game_pk) if do_props else {}
    if not game_odds and not prop_odds:
        return [], "no_odds"

    try:
        state = await _resolve_state_or_error(pool, game_pk)
    except Exception as exc:  # noqa: BLE001 — skip un-resolvable games
        log.info("skip game %s (state unresolved: %s)", game_pk, type(exc).__name__)
        return [], "unresolved"

    results = await asyncio.to_thread(_collect_game_results, state, iterations, base_seed)
    if not results:
        return [], "empty"

    summary = GameSimSummary.from_results(results)
    wp = win_probability(summary)

    bets: list[BetRecord] = []
    if do_game and game_odds:
        bets.extend(score_game_markets(game_pk, wp, summary, game_odds, min_edge=min_edge))
    if do_props and prop_odds:
        pset = PropDistributionSet.from_results(results)
        bets.extend(score_prop_markets(game_pk, pset, prop_odds, min_edge=min_edge))
    return bets, "scored"


# ===========================================================================
# ACROSS-GAMES parallelism: a module-level, picklable worker + per-worker init
# ===========================================================================
#
# The lever (per the SIM-430 profiling note): per-game cost is the irreducible
# per-PA full-pool scoring (~1.5 s/iter) and the host is core-bound, so a single
# game can't go below ~30 s.  The fix is to run ~6 WHOLE GAMES AT ONCE: each
# forkserver worker runs one game serially (resolve → N sims → prop dists → read
# odds → CLV bet records) and holds only its own ~373 MB full-pool sampler cache
# (SIM-430), so 6 workers fit in ~2.2 GB.  The PARENT stays lean — it loads NO
# engine artifacts — so the forkserver workers never COW-inherit a big parent.

#: Per-worker process globals (one set per forkserver worker). ``_WORKER_INITED``
#: guards the one-time lazy init; ``_WORKER_LOOP`` is this worker's dedicated
#: asyncio event loop; ``_WORKER_POOL`` is its single asyncpg connection pool. All
#: are amortized across every game the worker handles.
_WORKER_INITED: bool = False
_WORKER_LOOP: Any = None
_WORKER_POOL: Any = None
_WORKER_DSN: str = DEFAULT_DSN


def _worker_lazy_init(dsn: str) -> None:
    """One-time per-worker setup (first call only; cheap no-op thereafter).

    Amortizes the two big per-worker costs across all the games this worker
    handles:

      * ``(a)`` warm THIS worker's ~373 MB full-pool sampler cache ONCE via
        :func:`simulation.production_factory.warm_worker_cache` (the SIM-402 seam),
        so the worker's FIRST game is a warm-cache hit, not a cold artifact-load;
      * ``(b)`` open ONE dedicated asyncio loop + ONE asyncpg pool for this worker
        (each worker reads odds / resolves state on its own connection).

    Guarded by the ``_WORKER_INITED`` module global so it runs exactly once per
    forkserver worker. The warm step is best-effort (full-pool off / missing
    artifacts → it returns False and the per-tile path warms lazily); the pool is
    required (a worker that can't reach Postgres can't score a game).
    """
    global _WORKER_INITED, _WORKER_LOOP, _WORKER_POOL, _WORKER_DSN
    if _WORKER_INITED:
        return
    _WORKER_DSN = dsn

    # (a) warm the full-pool sampler cache ONCE for this worker (best-effort).
    try:
        from simulation.production_factory import warm_worker_cache

        warm_worker_cache(None)
    except Exception:  # noqa: BLE001 — full-pool off / no artifacts → lazy warm
        pass

    # (b) one event loop + one asyncpg pool, owned by this worker for its lifetime.
    import asyncpg

    _WORKER_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_WORKER_LOOP)
    _WORKER_POOL = _WORKER_LOOP.run_until_complete(asyncpg.create_pool(dsn, min_size=1, max_size=2))
    _WORKER_INITED = True


def _process_one_game(game_pk: int, params: dict[str, Any]) -> dict[str, Any]:
    """Module-level, picklable across-games worker: score ONE game end-to-end.

    The function the ``ProcessPoolExecutor`` maps across the slate's ``game_pk``s.
    On its FIRST call in a given worker it runs :func:`_worker_lazy_init` (warm the
    sampler cache + open the asyncpg pool); every call then drives the SAME
    :func:`_score_one_game` pipeline on this worker's dedicated loop + pool.

    Returns a fully-picklable payload ``{"status": str, "bets": list[dict]}`` where
    each bet dict is ``BetRecord.to_jsonable()`` — the parent turns those back into
    :class:`BetRecord`s and folds ``status`` into the SAME run counters the serial
    path keeps. Returning plain dicts (not the dataclass) keeps the cross-process
    boundary robust to how this script module is named in the worker.

    A degenerate / failing game logs and contributes NO bets (status ``"unresolved"``)
    — it NEVER raises out, so one bad game can never sink the parallel run.
    """
    dsn = str(params.get("dsn", DEFAULT_DSN))
    try:
        _worker_lazy_init(dsn)
        bets, status = _WORKER_LOOP.run_until_complete(
            _score_one_game(
                _WORKER_POOL,
                int(game_pk),
                do_game=bool(params["do_game"]),
                do_props=bool(params["do_props"]),
                iterations=int(params["iterations"]),
                base_seed=int(params["base_seed"]),
                min_edge=float(params["min_edge"]),
            )
        )
        return {"status": status, "bets": [b.to_jsonable() for b in bets]}
    except Exception as exc:  # noqa: BLE001 — never sink the run on one game
        log.warning("worker: game %s failed (%s) — no bets", game_pk, type(exc).__name__)
        return {"status": "unresolved", "bets": []}


@dataclass
class _Counters:
    """Running tallies for the run summary log."""

    games_attempted: int = 0
    games_scored: int = 0
    games_no_odds: int = 0
    games_unresolved: int = 0
    bets: list[BetRecord] = field(default_factory=list)


def _tally(counters: _Counters, status: str) -> None:
    """Fold one game's status into the run counters (shared by both paths)."""
    counters.games_attempted += 1
    if status == "no_odds":
        counters.games_no_odds += 1
    elif status == "unresolved":
        counters.games_unresolved += 1
    elif status == "scored":
        counters.games_scored += 1
    # "empty" -> attempted but produced no results: no scored/skip bucket (as before).


async def _run_serial(
    game_pks: list[int],
    *,
    do_game: bool,
    do_props: bool,
    args: argparse.Namespace,
) -> _Counters:
    """SERIAL fallback (``--workers 1``): the original in-process path.

    Opens ONE asyncpg pool in this process and scores each game in turn via the
    SAME :func:`_score_one_game` pipeline the workers use — so this is the
    no-pool debug mode AND the reference the verify step compares against.
    """
    import asyncpg

    counters = _Counters()
    pool = None
    try:
        if game_pks:
            pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        for game_pk in game_pks:
            bets, status = await _score_one_game(
                pool,
                game_pk,
                do_game=do_game,
                do_props=do_props,
                iterations=args.iterations,
                base_seed=args.base_seed,
                min_edge=args.min_edge,
            )
            _tally(counters, status)
            counters.bets.extend(bets)
            if counters.games_scored and counters.games_scored % 25 == 0:
                log.info(
                    "  scored %d/%d games (%d bet rows) ...",
                    counters.games_scored,
                    len(game_pks),
                    len(counters.bets),
                )
    finally:
        if pool is not None:
            await pool.close()
    return counters


def _run_parallel(
    game_pks: list[int],
    *,
    workers: int,
    do_game: bool,
    do_props: bool,
    args: argparse.Namespace,
) -> _Counters:
    """ACROSS-GAMES parallel path (``--workers > 1``).

    Builds a ``ProcessPoolExecutor`` with ``mp_context=forkserver`` (SIM-430) and
    maps :func:`_process_one_game` across the ``game_pk``s. Each worker runs a WHOLE
    game serially (its own ~373 MB sampler cache + asyncpg pool, lazily inited once);
    ``workers`` games run at once. Bet records are collected AS THEY COMPLETE, then
    the parent aggregates with the SAME :func:`aggregate_scoreboard` and emits the
    SAME scoreboard/JSON. The PARENT loads NO engine artifacts (stays lean so the
    forkserver workers never COW-inherit a big parent).

    Byte-identical to the serial path for the SAME (game set, base_seed, iterations):
    each game is independent and deterministic from its per-iteration seed
    (``derive_seed(base_seed, i)``), so completion order — the only thing parallelism
    changes — does not affect any game's bet records.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    worker_params: dict[str, Any] = {
        "do_game": do_game,
        "do_props": do_props,
        "iterations": int(args.iterations),
        "base_seed": int(args.base_seed),
        "min_edge": float(args.min_edge),
        "dsn": args.dsn,
    }
    counters = _Counters()
    if not game_pks:
        return counters

    log.info(
        "ACROSS-GAMES parallel: %d games over %d forkserver workers (~373 MB each).",
        len(game_pks),
        workers,
    )
    with ProcessPoolExecutor(max_workers=workers, mp_context=_pool_mp_context()) as pool:
        futures = {
            pool.submit(_process_one_game, int(pk), worker_params): int(pk) for pk in game_pks
        }
        done = 0
        for fut in as_completed(futures):
            game_pk = futures[fut]
            try:
                payload = fut.result()
            except Exception as exc:  # noqa: BLE001 — one bad game never sinks the run
                log.warning("game %s worker crashed (%s)", game_pk, type(exc).__name__)
                _tally(counters, "unresolved")
                continue
            _tally(counters, str(payload.get("status", "unresolved")))
            counters.bets.extend(BetRecord.from_jsonable(d) for d in payload.get("bets", []))
            done += 1
            if done % 25 == 0:
                log.info(
                    "  completed %d/%d games (%d bet rows) ...",
                    done,
                    len(game_pks),
                    len(counters.bets),
                )
    return counters


async def run(args: argparse.Namespace) -> int:
    seasons = sorted({int(s) for s in args.seasons})
    do_game = args.markets in ("game", "all")
    do_props = args.markets in ("props", "all")
    workers = max(1, int(args.workers))
    log.info(
        "SIM-429 CLV backtest — seasons=%s iterations=%d markets=%s min_edge=%s workers=%d",
        seasons,
        args.iterations,
        args.markets,
        args.min_edge,
        workers,
    )

    game_pks = await _fetch_final_games(args.dsn, seasons, args.max_games)
    log.info("Found %d completed games to backtest.", len(game_pks))
    if not game_pks:
        log.warning("No completed games found — nothing to backtest.")

    if workers <= 1:
        # SERIAL fallback (the original in-process path; the verify reference).
        counters = await _run_serial(game_pks, do_game=do_game, do_props=do_props, args=args)
    else:
        # ACROSS-GAMES parallel — the pool is sync, so offload it off the loop.
        counters = await asyncio.to_thread(
            _run_parallel,
            game_pks,
            workers=workers,
            do_game=do_game,
            do_props=do_props,
            args=args,
        )

    scoreboard = aggregate_scoreboard(counters.bets)
    params = {
        "seasons": seasons,
        "iterations": int(args.iterations),
        "markets": args.markets,
        "min_edge": float(args.min_edge),
        "base_seed": int(args.base_seed),
        "workers": workers,
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
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "ACROSS-GAMES parallel workers (default %(default)s). Each forkserver "
            "worker runs a WHOLE game serially (~373 MB sampler cache each). "
            "--workers 1 is the SERIAL in-process fallback."
        ),
    )
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
