#!/usr/bin/env python
"""
scripts/sim559_backfill_start_positions.py
==========================================
Repair the starters' positions in ``raw.game_lineups`` (SIM-559).

WHY
---
The historical loader wrote each starter's ``position_code`` from the box
feed's ``position`` field. That field is the LAST position the player held in
the game, not the one he started at. A fielder who moved mid-game (left field
to right field after a double switch, say) therefore holds his final slot in
the simulator's defense map, his starting slot is empty, and the player who
really started at his final slot loses his glove. One team-game in four has a
hole; the certifying set has 29 holes in its 90 team-games. The loader now
reads ``allPositions[0]``, the starting position; this script applies the
same reading to the games already loaded.

WHAT IT WRITES
--------------
Per Final game with lineup rows (ordered by game_pk): one UPDATE over that
game's starters, writing ``allPositions[0]`` onto each row whose code differs
(``pipeline.etl.boxscore_ingest.persist_starting_positions``). A row that is
already right is not touched, so the table's ``updated_at`` trigger marks
exactly the repaired rows. The box comes from ``/api/v1/game/{pk}/boxscore``,
one MLB request per game, the same request the box-score backfill makes.
The box-score table (``raw.game_player_stats.position_code``) keeps the last
position, which is what the official box shows.

USAGE
-----
    python scripts/sim559_backfill_start_positions.py --dry-run --max-games 200
    python scripts/sim559_backfill_start_positions.py --seasons 2024
    python scripts/sim559_backfill_start_positions.py --game-pks 823372
    python scripts/sim559_backfill_start_positions.py --done-file /data/sim559_done.txt

``--dry-run`` fetches and compares but writes nothing; the summary still
counts the rows it WOULD change. ``--done-file PATH`` makes a run crash-safe:
a game is appended (one game_pk per line, forced to disk) once its UPDATE is
committed, and a later run skips the games listed. Five failures in a row stop
the run (``--max-consecutive-failures``; 0 = never). Exit code 1 when the run
stopped early, or fetched nothing and at least one game failed. Progress logs
every 100 games. The run needs Postgres only (no DuckDB lock): it can run while
the app serves.

COST
----
About 0.3 s per box fetch plus ``--sleep`` (default 0.25 s): the 22,742 Final
games of 2017-2026 take about 3.5 hours; ``--sleep 0.1`` brings it near 2.5.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pipeline.etl.boxscore_ingest import (  # noqa: E402
    BoxscoreIngest,
    StartingPosition,
    parse_starting_positions,
    persist_starting_positions,
)

log = logging.getLogger("sim559_backfill_start_positions")

DEFAULT_DSN = os.environ.get(
    "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
)
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
PROGRESS_EVERY = 100

_LINEUP_STARTERS_SQL = """
    SELECT team_id, player_id, position_code
    FROM   raw.game_lineups
    WHERE  game_pk = $1 AND sequence = 1 AND is_starter AND batting_order IS NOT NULL
"""


@dataclass
class GameRef:
    game_pk: int
    season: int
    home_team_id: int
    away_team_id: int


@dataclass
class RunSummary:
    games_fetched: int = 0
    games_changed: int = 0
    rows_changed: int = 0
    rows_would_change: int = 0
    games_skipped_done: int = 0
    failures: int = 0
    aborted: bool = False
    #: (stored code, starting code) pairs, for the run's report.
    moves: Counter = field(default_factory=Counter)

    @property
    def failed(self) -> bool:
        if self.aborted:
            return True
        return self.failures > 0 and self.games_fetched == 0


def final_games_sql(seasons: list[int], max_games: int | None, game_pks: list[int]) -> str:
    """Final games that have lineup rows, by game_pk; the filters are literal ints."""
    where = [
        "g.status = 'Final'",
        "EXISTS (SELECT 1 FROM raw.game_lineups l WHERE l.game_pk = g.game_pk)",
    ]
    if seasons:
        where.append(f"g.season IN ({', '.join(str(int(s)) for s in seasons)})")
    if game_pks:
        where.append(f"g.game_pk IN ({', '.join(str(int(p)) for p in game_pks)})")
    sql = (
        "SELECT g.game_pk, g.season, g.home_team_id, g.away_team_id FROM raw.games g "
        f"WHERE {' AND '.join(where)} ORDER BY g.game_pk"
    )
    if max_games is not None:
        sql += f" LIMIT {int(max_games)}"
    return sql


def read_done_file(path: str | None) -> set[int]:
    """The game_pks a done-file lists (a missing file is empty; a line that is
    not a whole number, such as one a crash cut short, is ignored)."""
    if not path or not os.path.exists(path):
        return set()
    done: set[int] = set()
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.isdigit():
                done.add(int(line))
    return done


def append_done(path: str | None, game_pk: int) -> None:
    """Append one finished game to the done-file and force it to disk."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{int(game_pk)}\n")
        fh.flush()
        os.fsync(fh.fileno())


def planned_changes(
    rows: list[StartingPosition], current: dict[tuple[int, int], str]
) -> list[tuple[StartingPosition, str]]:
    """The parsed starters whose stored lineup code differs from the starting
    position, each paired with the stored code. A starter the lineup lacks is
    not a change (there is no row to update)."""
    out: list[tuple[StartingPosition, str]] = []
    for r in rows:
        stored = current.get((r.team_id, r.player_id))
        if stored is not None and stored != r.position_code:
            out.append((r, stored))
    return out


async def _fetch_games(conn: Any, args: argparse.Namespace) -> list[GameRef]:
    rows = await conn.fetch(
        final_games_sql(
            sorted({int(s) for s in args.seasons}),
            args.max_games,
            sorted({int(g) for g in args.game_pks}),
        )
    )
    return [
        GameRef(
            game_pk=int(r["game_pk"]),
            season=int(r["season"]),
            home_team_id=int(r["home_team_id"]),
            away_team_id=int(r["away_team_id"]),
        )
        for r in rows
    ]


async def repair_game(
    ingest: BoxscoreIngest, conn: Any, ref: GameRef, *, dry_run: bool, summary: RunSummary
) -> int:
    """Fetch one box, compare, write (unless ``dry_run``). Returns the rows
    changed (dry: the rows that would change)."""
    teams = await asyncio.to_thread(ingest.fetch_boxscore, ref.game_pk)
    parsed = parse_starting_positions(
        teams, game_pk=ref.game_pk, home_team_id=ref.home_team_id, away_team_id=ref.away_team_id
    )
    current_rows = await conn.fetch(_LINEUP_STARTERS_SQL, ref.game_pk)
    current = {
        (int(r["team_id"]), int(r["player_id"])): str(r["position_code"]) for r in current_rows
    }
    changes = planned_changes(parsed, current)
    for r, stored in changes:
        summary.moves[(stored, r.position_code)] += 1
    if dry_run:
        summary.rows_would_change += len(changes)
        return len(changes)
    return await persist_starting_positions(conn, [r for r, _ in changes])


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    ingest = BoxscoreIngest(timeout=args.timeout)
    summary = RunSummary()
    done = read_done_file(args.done_file)
    conn = await asyncpg.connect(args.dsn)
    try:
        games = await _fetch_games(conn, args)
        if done:
            before = len(games)
            games = [g for g in games if g.game_pk not in done]
            summary.games_skipped_done = before - len(games)
        log.info(
            "SIM-559 starting-position backfill: %d games (%d already done, dry_run=%s, "
            "sleep=%.2fs)",
            len(games),
            summary.games_skipped_done,
            args.dry_run,
            args.sleep,
        )
        if not games:
            log.warning("No games to repair: nothing to do.")
            return 0
        consecutive_failures = 0
        for i, ref in enumerate(games, start=1):
            try:
                changed = await repair_game(
                    ingest, conn, ref, dry_run=args.dry_run, summary=summary
                )
            except Exception as exc:  # noqa: BLE001 - one game must never end the run
                summary.failures += 1
                consecutive_failures += 1
                log.warning(
                    "game %s failed (%d consecutive): %s", ref.game_pk, consecutive_failures, exc
                )
                limit = args.max_consecutive_failures
                if limit > 0 and consecutive_failures >= limit:
                    summary.aborted = True
                    log.error(
                        "aborting: %d consecutive game failures (last was game %s). That is an "
                        "API outage or a schema change, not one bad game. Fix the cause and "
                        "re-run with the same --done-file.",
                        consecutive_failures,
                        ref.game_pk,
                    )
                    break
            else:
                consecutive_failures = 0
                summary.games_fetched += 1
                if changed:
                    summary.games_changed += 1
                    if not args.dry_run:
                        summary.rows_changed += changed
                if not args.dry_run:
                    append_done(args.done_file, ref.game_pk)
            if i % PROGRESS_EVERY == 0:
                log.info(
                    "  %d/%d games (%d fetched, %d games changed, %d rows changed, %d failed) ...",
                    i,
                    len(games),
                    summary.games_fetched,
                    summary.games_changed,
                    summary.rows_would_change if args.dry_run else summary.rows_changed,
                    summary.failures,
                )
            if args.sleep > 0 and i < len(games):
                await asyncio.sleep(args.sleep)
    finally:
        await conn.close()

    rows = summary.rows_would_change if args.dry_run else summary.rows_changed
    log.info(
        "SIM-559 backfill complete%s: %d games fetched, %d games with a change, %d rows %s, "
        "%d failures.",
        " (DRY RUN)" if args.dry_run else "",
        summary.games_fetched,
        summary.games_changed,
        rows,
        "would change" if args.dry_run else "changed",
        summary.failures,
    )
    if summary.moves:
        top = ", ".join(f"{a}->{b}: {n}" for (a, b), n in summary.moves.most_common(12))
        log.info("  moves (stored -> starting): %s", top)
    if summary.failed:
        log.error(
            "SIM-559 backfill FAILED: %s. Exit code 1.",
            "stopped early on consecutive failures"
            if summary.aborted
            else "nothing was fetched and at least one game failed",
        )
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Repair the starters' positions in raw.game_lineups from the box feed (SIM-559)."
    )
    p.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help="Postgres DSN (reads raw.games, writes raw.game_lineups).",
    )
    p.add_argument(
        "--seasons", type=int, nargs="*", default=[], help="Seasons to repair (default: all)."
    )
    p.add_argument("--game-pks", type=int, nargs="*", default=[], help="Repair these games only.")
    p.add_argument("--max-games", type=int, default=None, help="Cap games (a smoke run).")
    p.add_argument("--dry-run", action="store_true", help="Fetch and compare; write nothing.")
    p.add_argument(
        "--done-file", default=None, help="Crash-safe resume: one finished game_pk per line."
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
