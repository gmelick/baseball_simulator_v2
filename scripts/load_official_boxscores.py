#!/usr/bin/env python
"""
scripts/load_official_boxscores.py
==================================
The official box-score backfill (SIM-545, the sub-ticket of the unpriced-prop-
markets work, SIM-421): fetch the MLB Stats API box score for every completed
game and store one row per player who appeared in ``raw.game_player_stats``.

WHY
---
A sportsbook grades a player prop against the official box score. The
validation lanes and the closing-line-value backtest need that same ground
truth for every prop the market offers (singles, doubles, triples, runs,
stolen bases, hits+runs+RBI, outs recorded, hits allowed — as well as the
seven markets the platform already priced). The historical loader writes the
box for every game it loads from now on; this script backfills the games
already in ``raw.games``.

WHAT IT WRITES
--------------
Per Final game (ordered by game_pk for reproducibility): one
``raw.game_player_stats`` row per player whose box batting or pitching block
is non-empty, through ``INSERT … ON CONFLICT (game_pk, player_id) DO UPDATE``
(a re-run refreshes). Season, game date and the two team ids come from
``raw.games``; the box comes from ``/api/v1/game/{game_pk}/boxscore``.

USAGE
-----
    python scripts/load_official_boxscores.py --seasons 2024 --max-games 200
    python scripts/load_official_boxscores.py --seasons 2023 2024
    python scripts/load_official_boxscores.py --game-pks 746437 746438
    python scripts/load_official_boxscores.py --seasons 2024 --no-only-missing   # refresh

This is an OFFLINE backfill job: one MLB request per game, ``--sleep`` seconds
apart (default 0.25). ``--only-missing`` (the default) skips a game that
already has rows. A failure on one game is logged and counted; the run goes
on. Five failures in a row stop the run (``--max-consecutive-failures``;
0 = never stop). That pattern is an API outage or a schema mismatch, not one
bad game. The exit code is 1 when the run stops early. It is also 1 when the
run wrote nothing and at least one game failed. A wrapper then never reads an
empty run as a success. Progress logs every 100 games; a summary closes the
run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pipeline.etl.boxscore_ingest import (  # noqa: E402
    BoxscoreIngest,
    parse_boxscore,
    parse_bullpen_listing,
    persist,
    persist_bullpen,
)

log = logging.getLogger("load_official_boxscores")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN",
    "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim",
)

#: Games between progress log lines.
PROGRESS_EVERY = 100

#: Failures in a row that stop the run (the historical loader's rule, SIM-441):
#: one bad game is survivable; an API outage or a schema mismatch is not,
#: because the run would otherwise "succeed" having loaded nothing.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True, slots=True)
class GameRef:
    """What the parser needs from ``raw.games`` for one game."""

    game_pk: int
    season: int
    game_date: date
    home_team_id: int
    away_team_id: int


@dataclass(slots=True)
class RunSummary:
    """The closing counts of a backfill run."""

    games_fetched: int = 0
    rows_written: int = 0
    games_skipped: int = 0
    failures: int = 0
    #: True when the consecutive-failure limit stopped the run early.
    aborted: bool = False

    @property
    def failed(self) -> bool:
        """The run failed as a whole: it stopped early, or it wrote nothing and a game failed."""
        return self.aborted or (self.failures > 0 and self.rows_written == 0)


def final_games_sql(
    seasons: list[int],
    max_games: int | None,
    game_pks: list[int],
    only_missing: bool,
    *,
    missing_table: str = "raw.game_player_stats",
) -> str:
    """The completed-game query: Final, scored, ordered by game_pk.

    ``only_missing`` adds a NOT EXISTS against ``missing_table`` (the box rows
    by default; ``raw.game_bullpen`` for the SIM-427 bullpen pass) so a re-run
    skips games that already have rows. ``game_pks`` narrows to an explicit
    list; otherwise ``seasons`` selects.
    """
    where = ["status = 'Final'", "home_score_final IS NOT NULL", "away_score_final IS NOT NULL"]
    if game_pks:
        where.append("game_pk IN (" + ", ".join(str(int(g)) for g in game_pks) + ")")
    else:
        where.append("season IN (" + ", ".join(str(int(s)) for s in seasons) + ")")
    if only_missing:
        where.append(
            f"NOT EXISTS (SELECT 1 FROM {missing_table} s WHERE s.game_pk = raw.games.game_pk)"
        )
    limit = f"LIMIT {int(max_games)}" if max_games else ""
    return f"""
        SELECT game_pk, season, game_date, home_team_id, away_team_id
        FROM raw.games
        WHERE {" AND ".join(where)}
        ORDER BY game_pk
        {limit}
    """


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


async def _fetch_final_games(conn: Any, args: argparse.Namespace) -> list[GameRef]:
    rows = await conn.fetch(
        final_games_sql(
            sorted({int(s) for s in args.seasons}),
            args.max_games,
            sorted({int(g) for g in args.game_pks}),
            args.only_missing,
            missing_table=(
                "raw.game_bullpen"
                if getattr(args, "bullpen_only", False)
                else "raw.game_player_stats"
            ),
        )
    )
    return [
        GameRef(
            game_pk=int(r["game_pk"]),
            season=int(r["season"]),
            game_date=_as_date(r["game_date"]),
            home_team_id=int(r["home_team_id"]),
            away_team_id=int(r["away_team_id"]),
        )
        for r in rows
    ]


async def load_game(
    ingest: BoxscoreIngest, conn: Any, ref: GameRef, *, bullpen_only: bool = False
) -> int:
    """Fetch + parse + persist one game. Returns rows written (0 = an empty box).

    SIM-427: the same fetch also writes the per-game bullpen listing
    (``raw.game_bullpen``); ``bullpen_only`` skips the player-stats upsert —
    the backfill pass over games whose box rows are already loaded.
    """
    teams = await asyncio.to_thread(ingest.fetch_boxscore, ref.game_pk)
    pen_rows = parse_bullpen_listing(
        teams,
        game_pk=ref.game_pk,
        home_team_id=ref.home_team_id,
        away_team_id=ref.away_team_id,
    )
    if bullpen_only:
        return await persist_bullpen(conn, pen_rows)
    rows = parse_boxscore(
        teams,
        game_pk=ref.game_pk,
        season=ref.season,
        game_date=ref.game_date,
        home_team_id=ref.home_team_id,
        away_team_id=ref.away_team_id,
    )
    # The box rows first and the count returned is theirs (the run summary's
    # "rows written"; an empty box = 0 = skipped); the listing rides along.
    written = await persist(conn, rows)
    await persist_bullpen(conn, pen_rows)
    return written


async def run(args: argparse.Namespace) -> int:
    if not args.game_pks and not args.seasons:
        log.error("pass --seasons or --game-pks")
        return 2

    import asyncpg

    ingest = BoxscoreIngest(timeout=args.timeout)
    summary = RunSummary()
    conn = await asyncpg.connect(args.dsn)
    try:
        games = await _fetch_final_games(conn, args)
        log.info(
            "SIM-545 box-score backfill — %d games (only_missing=%s, sleep=%.2fs)",
            len(games),
            args.only_missing,
            args.sleep,
        )
        if not games:
            log.warning("No games to load — nothing to backfill.")
            return 0
        consecutive_failures = 0
        for i, ref in enumerate(games, start=1):
            try:
                written = await load_game(
                    ingest, conn, ref, bullpen_only=bool(getattr(args, "bullpen_only", False))
                )
            except Exception as exc:  # noqa: BLE001 — one game must never end the run
                summary.failures += 1
                consecutive_failures += 1
                log.warning(
                    "game %s failed (%d consecutive): %s", ref.game_pk, consecutive_failures, exc
                )
                limit = args.max_consecutive_failures
                if limit > 0 and consecutive_failures >= limit:
                    summary.aborted = True
                    log.error(
                        "aborting: %d consecutive game failures (last was game %s). This is a "
                        "systemic failure — an API outage or a schema mismatch — not one bad "
                        "game. Fix the cause and re-run; --only-missing skips the games written.",
                        consecutive_failures,
                        ref.game_pk,
                    )
                    break
            else:
                consecutive_failures = 0
                if written == 0:
                    summary.games_skipped += 1
                    log.info("game %s: empty box score, nothing written", ref.game_pk)
                else:
                    summary.games_fetched += 1
                    summary.rows_written += written
            if i % PROGRESS_EVERY == 0:
                log.info(
                    "  %d/%d games (%d fetched, %d rows, %d skipped, %d failed) ...",
                    i,
                    len(games),
                    summary.games_fetched,
                    summary.rows_written,
                    summary.games_skipped,
                    summary.failures,
                )
            if args.sleep > 0 and i < len(games):
                await asyncio.sleep(args.sleep)
    finally:
        await conn.close()

    log.info(
        "SIM-545 backfill complete: %d games fetched, %d rows written, %d games skipped, "
        "%d failures (re-runs refresh via ON CONFLICT DO UPDATE).",
        summary.games_fetched,
        summary.rows_written,
        summary.games_skipped,
        summary.failures,
    )
    if summary.failed:
        log.error(
            "SIM-545 backfill FAILED: %s — exit code 1.",
            "stopped early on consecutive failures"
            if summary.aborted
            else "nothing was written and at least one game failed",
        )
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill the official per-player box score for completed games (SIM-545)."
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN (completed games + writes).")
    p.add_argument("--seasons", type=int, nargs="*", default=[], help="Seasons to backfill.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (a smoke run).")
    p.add_argument("--game-pks", type=int, nargs="*", default=[], help="Load these games only.")
    p.add_argument(
        "--only-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip games that already have rows (default on; --no-only-missing refreshes).",
    )
    p.add_argument(
        "--bullpen-only",
        action="store_true",
        help=(
            "SIM-427: write only the per-game bullpen listing (raw.game_bullpen) from the "
            "box fetch, and select the games that lack listing rows under --only-missing — "
            "the backfill for games whose box rows are already loaded."
        ),
    )
    p.add_argument("--sleep", type=float, default=0.25, help="Seconds between MLB calls.")
    p.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout per call.")
    p.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help="Failures in a row that stop the run with exit code 1 (0 = never stop).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
