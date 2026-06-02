#!/usr/bin/env python
"""
scripts/load_historical_odds.py
===============================
SIM-435 — historical odds loader: backfill OPENING + CLOSING betting lines for
completed games into ``raw.game_odds`` / ``raw.prop_odds`` so the CLV backtest
(SIM-429 sub-4) has entry+closing lines to score against.

WHY
---
``raw.game_odds`` and ``raw.prop_odds`` are EMPTY (0 rows). The CLV engine
(SIM-339) needs, per completed game, an entry (opening) line and a closing line.
The live pipeline captures these forward (SIM-340 marking) but there is no
history. This script walks the Final games already in ``raw.games`` and pulls
their opening + closing lines from the configured odds provider (BettingPros by
default — set ``ODDS_PROVIDER=bettingpros`` + ``ODDS_API_KEY``) and persists them
through the SAME write path the live pipeline uses, so the odds_hash ON CONFLICT
dedup keeps re-runs idempotent.

WHAT IT WRITES
--------------
Per Final game (ordered by game_pk for reproducibility):
  * game odds: one ``raw.game_odds`` row per (line_type ∈ {opening, closing}) ×
    (market_type ∈ {moneyline, runline, total}) — six rows when all resolve.
  * prop odds: one ``raw.prop_odds`` row per (player, prop_stat, line_type) for
    every player in the game's lineup. Pitcher props (strikeouts / earned_runs /
    walks) are fetched only for pitchers; batter props (hits / home_runs /
    total_bases / rbis) only for non-pitchers. Unresolved lines (null) are
    skipped (we never persist an empty quote).

Persistence reuses ``LiveIngestionPipeline._persist_odds`` /
``_persist_prop_odds`` (so the SIM-092/SIM-340 ``odds_hash`` dedup + the
``raw.prop_odds`` CHECK constraint apply unchanged). The pipeline is constructed
WITHOUT starting it (no Redis / WS / HTTP loop) — we attach our own asyncpg pool
to ``pipeline._db`` and call the two persist coroutines directly.

USAGE
-----
    # In the app container, with a real provider configured:
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=… \
        python scripts/load_historical_odds.py --seasons 2024 --max-games 200
    python scripts/load_historical_odds.py --seasons 2023 2024            # game + prop
    python scripts/load_historical_odds.py --seasons 2024 --no-props      # game odds only

    # Via a Makefile wrapper (mirror make validate-props):
    make load-historical-odds FLAGS="--seasons 2024 --max-games 200"

This is an OFFLINE backfill job. It issues one provider request per
(game, market) / (game, player, prop) so it is network-bound; cap a smoke run
with ``--max-games``. With ``ODDS_PROVIDER`` unset it uses the deterministic
MockOddsAPI (useful for a no-network synthetic-line smoke / wiring check).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

log = logging.getLogger("load_historical_odds")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)

#: line_types to backfill (entry + closing — the two CLV reference points).
LINE_TYPES: tuple[str, ...] = ("opening", "closing")

#: game-odds market types persisted per line_type (one row each).
GAME_MARKET_TYPES: tuple[str, ...] = ("moneyline", "runline", "total")

#: prop_stat → fetched for pitchers (True) or batters (False). Mirrors the
#: Betting-Analyst 7-market split so we never request a pitcher prop on a batter.
PITCHER_PROP_STATS: tuple[str, ...] = ("strikeouts", "earned_runs", "walks")
BATTER_PROP_STATS: tuple[str, ...] = ("hits", "home_runs", "total_bases", "rbis")

#: position_codes counted as pitchers in raw.game_lineups (P=pitcher; some feeds
#: use SP/RP). Everyone else is treated as a position player for prop routing.
_PITCHER_POSITIONS = frozenset({"P", "SP", "RP", "1"})


async def _fetch_final_games(dsn: str, seasons: list[int], max_games: int | None) -> list[dict]:
    """Return completed-game rows ``{game_pk}`` for the requested seasons.

    Reads ``raw.games`` (status='Final'); we only need the game_pk — the odds
    provider resolves teams/date itself from the MLB schedule. SIM-432: the live
    schema stores the final score in ``home_score_final`` / ``away_score_final``
    (not ``home_score``), so we gate on those being present (a real Final game).
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
    return [{"game_pk": int(r["game_pk"])} for r in rows]


async def _fetch_lineup_players(pool, game_pk: int) -> list[tuple[int, bool]]:
    """Return ``(player_id, is_pitcher)`` for every player in ``game_pk``'s lineup.

    Reads ``raw.game_lineups`` (the SIM-353 lineup table). ``is_pitcher`` routes
    which prop markets are requested for the player. DISTINCT so a player who
    changed slots (sequence > 1) is fetched once.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT player_id, position_code
        FROM raw.game_lineups
        WHERE game_pk = $1
        """,
        int(game_pk),
    )
    out: list[tuple[int, bool]] = []
    for r in rows:
        pos = str(r["position_code"] or "").upper()
        out.append((int(r["player_id"]), pos in _PITCHER_POSITIONS))
    return out


def _has_line(odds: dict) -> bool:
    """True if a game-odds dict carries at least one resolved price/line."""
    keys = (
        "home_ml",
        "away_ml",
        "home_spread",
        "home_spread_ml",
        "away_spread",
        "away_spread_ml",
        "total_line",
        "over_ml",
        "under_ml",
    )
    return any(odds.get(k) is not None for k in keys)


async def _load_game_odds(provider, persist, game_pk: int) -> int:
    """Fetch + persist opening & closing game lines for one game. Returns rows written."""
    written = 0
    for line_type in LINE_TYPES:
        for market_type in GAME_MARKET_TYPES:
            try:
                odds = provider.get_odds(game_pk, line_type=line_type, market_type=market_type)
            except Exception as exc:  # noqa: BLE001 — skip a market we can't fetch
                log.warning(
                    "game odds fetch failed game %s %s/%s: %s",
                    game_pk,
                    line_type,
                    market_type,
                    exc,
                )
                continue
            if not _has_line(odds):
                continue
            try:
                await persist(game_pk, odds)
                written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "game odds persist failed game %s %s/%s: %s",
                    game_pk,
                    line_type,
                    market_type,
                    exc,
                )
    return written


async def _load_prop_odds(provider, persist, game_pk: int, players: list[tuple[int, bool]]) -> int:
    """Fetch + persist opening & closing prop lines for one game. Returns rows written."""
    written = 0
    for player_id, is_pitcher in players:
        stats = PITCHER_PROP_STATS if is_pitcher else BATTER_PROP_STATS
        for prop_stat in stats:
            for line_type in LINE_TYPES:
                try:
                    quote = provider.get_prop_odds(
                        game_pk, player_id, prop_stat, line_type=line_type
                    )
                except ValueError:
                    raise  # an unknown prop_stat is a programming error — surface it
                except Exception as exc:  # noqa: BLE001 — skip a market we can't fetch
                    log.warning(
                        "prop fetch failed game %s player %s %s/%s: %s",
                        game_pk,
                        player_id,
                        prop_stat,
                        line_type,
                        exc,
                    )
                    continue
                if quote.get("line") is None:
                    continue
                try:
                    await persist(quote)
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "prop persist failed game %s player %s %s/%s: %s",
                        game_pk,
                        player_id,
                        prop_stat,
                        line_type,
                        exc,
                    )
    return written


def _build_persisters(dsn: str, pool):
    """Return ``(persist_game, persist_prop)`` bound to the live pipeline write path.

    Reuses ``LiveIngestionPipeline._persist_odds`` / ``_persist_prop_odds`` (the
    SIM-092/SIM-340 odds_hash dedup + INSERT … ON CONFLICT DO NOTHING) without
    starting the pipeline: we construct it (a dummy redis_url satisfies the
    __init__ guard — start() is never called so Redis is untouched) and attach
    our own asyncpg pool to ``_db``.
    """
    from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

    pipeline = LiveIngestionPipeline(
        dsn=dsn,
        redis_url=os.environ.get("REDIS_URL", "redis://unused:6379"),
    )
    pipeline._db = pool  # attach our pool; start() (which would build one) is never called
    return pipeline._persist_odds, pipeline._persist_prop_odds


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from pipeline.odds_provider import get_odds_provider

    seasons = sorted({int(s) for s in args.seasons})
    provider = get_odds_provider(args.provider)
    log.info(
        "SIM-435 historical odds backfill — seasons=%s provider=%s props=%s",
        seasons,
        type(provider).__name__,
        not args.no_props,
    )

    games = await _fetch_final_games(args.dsn, seasons, args.max_games)
    log.info("Found %d completed games to backfill.", len(games))
    if not games:
        log.warning("No completed games found — nothing to backfill.")
        return 0

    pool = None
    n_game_rows = 0
    n_prop_rows = 0
    n_done = 0
    try:
        pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=4)
        persist_game, persist_prop = _build_persisters(args.dsn, pool)

        for g in games:
            game_pk = g["game_pk"]
            n_game_rows += await _load_game_odds(provider, persist_game, game_pk)

            if not args.no_props:
                players = await _fetch_lineup_players(pool, game_pk)
                if not players:
                    log.info("skip props for game %s (no lineup rows)", game_pk)
                else:
                    n_prop_rows += await _load_prop_odds(provider, persist_prop, game_pk, players)

            n_done += 1
            if n_done % 25 == 0:
                log.info(
                    "  backfilled %d/%d games (%d game rows, %d prop rows) ...",
                    n_done,
                    len(games),
                    n_game_rows,
                    n_prop_rows,
                )
    finally:
        if pool is not None:
            await pool.close()

    log.info(
        "SIM-435 backfill complete: %d games, %d game-odds rows, %d prop-odds rows written "
        "(re-runs are idempotent via odds_hash ON CONFLICT).",
        n_done,
        n_game_rows,
        n_prop_rows,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill opening+closing odds for completed games (SIM-435)."
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + writes).")
    p.add_argument("--seasons", type=int, nargs="+", required=True, help="Seasons to backfill.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (smoke run).")
    p.add_argument(
        "--no-props",
        action="store_true",
        help="Backfill game odds only (skip the per-player prop lines).",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Odds provider name (defaults to ODDS_PROVIDER env / 'mock').",
    )
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
