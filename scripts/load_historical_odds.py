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
    every player in the game's lineup. The pitcher markets
    (``PITCHER_PROP_STATS``: strikeouts / earned_runs / walks / outs_recorded /
    hits_allowed) are fetched only for pitchers; the batter markets
    (``BATTER_PROP_STATS``: hits / home_runs / total_bases / rbis / singles /
    doubles / triples / runs / stolen_bases / hits_runs_rbis) only for
    non-pitchers. Both tuples come from ``pipeline/odds_provider.py`` (the
    single source — SIM-421). Unresolved lines (null) are skipped (we never
    persist an empty quote).

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

    # SIM-421 follow-up pass: fetch ONLY the eight new markets for a season whose
    # seven original markets are already loaded. The game lines are skipped
    # (--no-game-odds); a re-run is idempotent through the odds_hash dedup.
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py
        --seasons 2024 --no-game-odds
        --prop-stats singles doubles triples runs stolen_bases hits_runs_rbis
                     outs_recorded hits_allowed

    # --prop-stats takes any subset of the 15-market vocabulary (an unknown value
    # stops the run before any fetch); --line-types defaults to "opening closing".

    # SIM-421 (owner ruling 2026-09-12): every game market the book posts is
    # loaded by default — the three full-game markets plus the twelve segment
    # and team markets (first-inning / first-five moneyline, total, run line;
    # each side's full-game and first-five team total; first team to score;
    # a run in the first inning). --game-markets narrows the set the same way
    # --prop-stats does, e.g. a follow-up pass for the twelve new markets only:
    ODDS_PROVIDER=bettingpros ODDS_API_KEY=... python scripts/load_historical_odds.py
        --seasons 2024 --no-props
        --game-markets f1_moneyline f5_moneyline f1_total f5_total f1_runline
                       f5_runline team_total_home team_total_away
                       f5_team_total_home f5_team_total_away first_to_score
                       first_inning_run

    # Via a Makefile wrapper (mirror make validate-props):
    make load-historical-odds FLAGS="--seasons 2024 --max-games 200"

This is an OFFLINE backfill job. It is network-bound; cap a smoke run with
``--max-games``. With ``ODDS_PROVIDER`` unset it uses the deterministic
MockOddsAPI (useful for a no-network synthetic-line smoke / wiring check).

The BettingPros provider caches one ``/offers`` response per (event, market)
for a time-to-live (SIM-421). The live pipeline needs a short one (30 s) so a
60-second cycle never re-uses a stale line. This job does not: its games are
over, their lines are final, and every player x market x line type of one game
is requested back-to-back. The job therefore sets ``ODDS_OFFERS_CACHE_TTL_S``
to ``OFFLINE_OFFERS_CACHE_TTL_S`` (600 s) when the variable is unset, which
collapses a game's several hundred prop requests into one fetch per market.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# SIM-421: the prop-market vocabulary is imported, never copied — the pitcher /
# batter split routes which markets a player is asked for.
from pipeline.odds_provider import (  # noqa: E402
    BATTER_PROP_STATS,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    PITCHER_PROP_STATS,
    PROP_STATS,
)

log = logging.getLogger("load_historical_odds")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)

#: line_types to backfill by default (entry + closing — the two CLV reference
#: points). ``--line-types`` overrides it with any subset of KNOWN_LINE_TYPES.
LINE_TYPES: tuple[str, ...] = ("opening", "closing")
KNOWN_LINE_TYPES: tuple[str, ...] = ("opening", "current", "closing")

#: SIM-421 (owner ruling 2026-09-12): every game market the book posts is
#: persisted per line_type (one row each) — the three full-game markets plus
#: the twelve segment and team markets. The vocabulary is imported from
#: ``pipeline/odds_provider.py`` (``GAME_MARKET_TYPES``); ``--game-markets``
#: narrows it for a follow-up pass.

#: SIM-421: the offers-cache time-to-live this OFFLINE job applies when
#: ODDS_OFFERS_CACHE_TTL_S is unset (see the module docstring for why a long
#: value is safe here and not in the live pipeline).
OFFLINE_OFFERS_CACHE_TTL_S = 600
_OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"

#: position_codes counted as pitchers in raw.game_lineups (P=pitcher; some feeds
#: use SP/RP). Everyone else is treated as a position player for prop routing.
_PITCHER_POSITIONS = frozenset({"P", "SP", "RP", "1"})


def _select_prop_stats(requested: list[str] | None) -> tuple[str, ...]:
    """The prop markets this run fetches, in canonical PROP_STATS order.

    ``None`` (no ``--prop-stats``) means every market. An unknown value raises
    ``ValueError`` naming it and the vocabulary — the run stops before any
    fetch, so a typo never silently loads a partial season.
    """
    if not requested:
        return PROP_STATS
    unknown = sorted({s for s in requested if s not in PROP_STATS})
    if unknown:
        raise ValueError(
            f"Unknown prop_stat value(s) {unknown}. Known values: {', '.join(PROP_STATS)}"
        )
    wanted = set(requested)
    return tuple(s for s in PROP_STATS if s in wanted)


def _select_game_markets(requested: list[str] | None) -> tuple[str, ...]:
    """The game markets this run fetches, in canonical GAME_MARKET_TYPES order.

    ``None`` (no ``--game-markets``) means every market. An unknown value
    raises ``ValueError`` naming it and the vocabulary, so the run stops before
    any fetch.
    """
    if not requested:
        return GAME_MARKET_TYPES
    unknown = sorted({m for m in requested if m not in GAME_MARKET_TYPES})
    if unknown:
        raise ValueError(
            f"Unknown market_type value(s) {unknown}. Known values: {', '.join(GAME_MARKET_TYPES)}"
        )
    wanted = set(requested)
    return tuple(m for m in GAME_MARKET_TYPES if m in wanted)


def _select_line_types(requested: list[str] | None) -> tuple[str, ...]:
    """The line types this run fetches (default: opening + closing).

    An unknown value raises ``ValueError`` (the raw.prop_odds CHECK constraint
    would reject it anyway; failing here is earlier and clearer).
    """
    if not requested:
        return LINE_TYPES
    unknown = sorted({lt for lt in requested if lt not in KNOWN_LINE_TYPES})
    if unknown:
        raise ValueError(
            f"Unknown line_type value(s) {unknown}. Known values: {', '.join(KNOWN_LINE_TYPES)}"
        )
    wanted = set(requested)
    return tuple(lt for lt in KNOWN_LINE_TYPES if lt in wanted)


def _configure_offline_cache() -> float:
    """Set the offers-cache time-to-live for this offline job; return the value in force.

    Only sets ``ODDS_OFFERS_CACHE_TTL_S`` when the operator left it unset, so an
    explicit value always wins. The provider reads the variable when it is
    built (``get_odds_provider``), so this runs before that call.
    """
    os.environ.setdefault(_OFFERS_CACHE_TTL_ENV, str(OFFLINE_OFFERS_CACHE_TTL_S))
    return float(os.environ[_OFFERS_CACHE_TTL_ENV])


def parse_skip_loaded_since(value: str | None) -> datetime | None:
    """SIM-421 resume: parse ``--skip-loaded-since`` into an aware datetime.

    ISO-8601 (``2026-09-12T23:40:00`` or with an offset). A value with no offset
    is read in the machine's local time zone — the same clock the loader logs
    in — so an operator can copy a timestamp straight from a log line.
    """
    if value is None or not str(value).strip():
        return None
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive == local wall-clock time
    return dt


async def _fetch_final_games(
    dsn: str,
    seasons: list[int],
    max_games: int | None,
    *,
    skip_loaded_since: datetime | None = None,
) -> list[dict]:
    """Return completed-game rows ``{game_pk}`` for the requested seasons.

    Reads ``raw.games`` (status='Final'); we only need the game_pk — the odds
    provider resolves teams/date itself from the MLB schedule. SIM-432: the live
    schema stores the final score in ``home_score_final`` / ``away_score_final``
    (not ``home_score``), so we gate on those being present (a real Final game).

    SIM-421 resume: ``skip_loaded_since`` drops every game that already holds a
    ``raw.prop_odds`` row fetched at or after that instant — the games a run
    that died mid-season had finished. A game the book listed no event for has
    no rows and is retried (cheap: no event, no offers calls). The one game in
    flight when the run died may hold a partial set of rows and is skipped too;
    a later full pass fills it through the dedup.
    """
    import asyncpg

    sl = ", ".join(str(int(s)) for s in seasons)
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    resume_clause = ""
    params: list[object] = []
    if skip_loaded_since is not None:
        resume_clause = (
            "AND NOT EXISTS (SELECT 1 FROM raw.prop_odds p "
            "WHERE p.game_pk = raw.games.game_pk AND p.fetched_at >= $1)"
        )
        params.append(skip_loaded_since)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT game_pk
            FROM raw.games
            WHERE status = 'Final'
              AND season IN ({sl})
              AND home_score_final IS NOT NULL AND away_score_final IS NOT NULL
              {resume_clause}
            ORDER BY game_pk
            {limit}
            """,
            *params,
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
    return any(odds.get(k) is not None for k in GAME_ODDS_FIELDS)


async def _load_game_odds(
    provider,
    persist,
    game_pk: int,
    *,
    line_types: tuple[str, ...] = LINE_TYPES,
    game_markets: tuple[str, ...] = GAME_MARKET_TYPES,
) -> int:
    """Fetch + persist the game lines (opening & closing by default) for one game.

    ``game_markets`` is the market subset (``--game-markets``); the default is
    every market the book posts. Returns rows written.
    """
    written = 0
    for line_type in line_types:
        for market_type in game_markets:
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


def _prop_stats_for_player(is_pitcher: bool, prop_stats: tuple[str, ...]) -> tuple[str, ...]:
    """SIM-421: the markets of ``prop_stats`` a pitcher (or a hitter) is asked for.

    A pitcher gets the pitcher markets only, a hitter the batter markets only,
    so ``hits`` (batter) and ``hits_allowed`` (pitcher) never cross.
    """
    role_stats = PITCHER_PROP_STATS if is_pitcher else BATTER_PROP_STATS
    return tuple(s for s in prop_stats if s in role_stats)


async def _load_prop_odds(
    provider,
    persist,
    game_pk: int,
    players: list[tuple[int, bool]],
    *,
    prop_stats: tuple[str, ...] = PROP_STATS,
    line_types: tuple[str, ...] = LINE_TYPES,
) -> int:
    """Fetch + persist the prop lines (opening & closing by default) for one game.

    ``prop_stats`` narrows the markets (``--prop-stats``); each player is asked
    only for the markets of his role. Returns rows written.
    """
    written = 0
    for player_id, is_pitcher in players:
        for prop_stat in _prop_stats_for_player(is_pitcher, prop_stats):
            for line_type in line_types:
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
    # SIM-421: the market subset and line types are validated here so a bad
    # value stops the run before any provider request.
    prop_stats = _select_prop_stats(getattr(args, "prop_stats", None))
    game_markets = _select_game_markets(getattr(args, "game_markets", None))
    line_types = _select_line_types(getattr(args, "line_types", None))
    no_game_odds = bool(getattr(args, "no_game_odds", False))
    # SIM-421: the long offline time-to-live must be in the environment BEFORE
    # the provider is built — it reads the variable in its constructor.
    cache_ttl = _configure_offline_cache()
    provider = get_odds_provider(args.provider)
    log.info(
        "SIM-435 historical odds backfill — seasons=%s provider=%s game_odds=%s props=%s "
        "game_markets=%s prop_stats=%s line_types=%s offers_cache_ttl_s=%s",
        seasons,
        type(provider).__name__,
        not no_game_odds,
        not args.no_props,
        list(game_markets),
        list(prop_stats),
        list(line_types),
        cache_ttl,
    )

    skip_since = parse_skip_loaded_since(getattr(args, "skip_loaded_since", None))
    games = await _fetch_final_games(
        args.dsn, seasons, args.max_games, skip_loaded_since=skip_since
    )
    if skip_since is not None:
        log.info("resume: skipping games with prop rows fetched since %s", skip_since.isoformat())
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
            if not no_game_odds:
                n_game_rows += await _load_game_odds(
                    provider,
                    persist_game,
                    game_pk,
                    line_types=line_types,
                    game_markets=game_markets,
                )

            if not args.no_props:
                players = await _fetch_lineup_players(pool, game_pk)
                if not players:
                    log.info("skip props for game %s (no lineup rows)", game_pk)
                else:
                    n_prop_rows += await _load_prop_odds(
                        provider,
                        persist_prop,
                        game_pk,
                        players,
                        prop_stats=prop_stats,
                        line_types=line_types,
                    )

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
        "--no-game-odds",
        action="store_true",
        help="Backfill prop lines only (skip the game-level lines) — the SIM-421 "
        "follow-up pass for a season whose game lines are already loaded.",
    )
    p.add_argument(
        "--prop-stats",
        nargs="+",
        default=None,
        metavar="PROP_STAT",
        help="Fetch only these prop markets (any subset of: "
        f"{', '.join(PROP_STATS)}). Default: every market.",
    )
    p.add_argument(
        "--game-markets",
        nargs="+",
        default=None,
        metavar="MARKET_TYPE",
        help="Fetch only these game markets (any subset of: "
        f"{', '.join(GAME_MARKET_TYPES)}). Default: every market.",
    )
    p.add_argument(
        "--line-types",
        nargs="+",
        default=None,
        metavar="LINE_TYPE",
        help=f"Line types to fetch (any subset of: {', '.join(KNOWN_LINE_TYPES)}). "
        f"Default: {' '.join(LINE_TYPES)}.",
    )
    p.add_argument(
        "--skip-loaded-since",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Resume a run that died: skip every game that already has a prop-odds row "
        "fetched at or after this instant (ISO-8601; no offset = local time).",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Odds provider name (defaults to ODDS_PROVIDER env / 'mock').",
    )
    args = p.parse_args(argv)
    # SIM-421: fail loudly on an unknown market or line type, before any work.
    try:
        _select_prop_stats(args.prop_stats)
        _select_line_types(args.line_types)
    except ValueError as exc:
        p.error(str(exc))
    return args


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
