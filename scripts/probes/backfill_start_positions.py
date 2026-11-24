#!/usr/bin/env python
"""
scripts/probes/backfill_start_positions.py
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
(:func:`persist_starting_positions`). A row that is
already right is not touched, so the table's ``updated_at`` trigger marks
exactly the repaired rows. The box comes from ``/api/v1/game/{pk}/boxscore``,
one MLB request per game, the same request the box-score backfill makes.
The box-score table (``raw.game_player_stats.position_code``) keeps the last
position, which is what the official box shows.

USAGE
-----
    python scripts/probes/backfill_start_positions.py --dry-run --max-games 200
    python scripts/probes/backfill_start_positions.py --seasons 2024
    python scripts/probes/backfill_start_positions.py --game-pks 823372
    python scripts/probes/backfill_start_positions.py --done-file /data/sim559_done.txt

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db.dsn import require_dsn  # noqa: E402
from ingest.load_boxscores import SIDES, BoxscoreIngest, side_team_id  # noqa: E402
from ingest.mlb.game_feed import starting_position  # noqa: E402

log = logging.getLogger("backfill_start_positions")


# ---------------------------------------------------------------------------
# The starting positions of a box, and their write onto the lineup rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartingPosition:
    """One starter's starting and last positions in a game.

    ``position_code`` is the starting position (``allPositions[0]``);
    ``last_position_code`` is the box's ``position``, the value the lineup
    rows loaded before the starting-position fix (SIM-559) carry.
    ``batting_order`` is the slot (1-9).
    """

    game_pk: int
    team_id: int
    player_id: int
    batting_order: int
    position_code: str
    last_position_code: str | None

    @property
    def moved(self) -> bool:
        """True when the player ended the game at another position."""
        return self.position_code != self.last_position_code


def parse_starting_positions(
    teams: Mapping[str, Any],
    *,
    game_pk: int,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
) -> list[StartingPosition]:
    """The starting position of every batting-order STARTER in a box.

    A starter is a player whose ``battingOrder`` ends in ``00``. The starting
    pitcher of a designated-hitter game has no batting order and no row here:
    his lineup row is coded 'P' already. The same ``teams`` dict
    :func:`parse_boxscore` reads, so one fetch serves the backfill.
    """
    fallback_team = {"home": home_team_id, "away": away_team_id}
    rows: list[StartingPosition] = []
    for side in SIDES:
        team_box = teams.get(side) or {}
        team_id = side_team_id(team_box, fallback_team[side])
        if team_id is None:
            log.warning("SIM-559: game %s %s side has no team id — no rows.", game_pk, side)
            continue
        for pdata in (team_box.get("players") or {}).values():
            pid = (pdata.get("person") or {}).get("id")
            bo = pdata.get("battingOrder")
            if pid is None or bo in (None, ""):
                continue
            try:
                bo_int = int(bo)
            except (TypeError, ValueError):
                continue
            if bo_int % 100 != 0:
                continue  # a later occupant of the slot, not the starter
            pos = starting_position(pdata)
            if pos is None:
                continue
            last = (pdata.get("position") or {}).get("abbreviation")
            rows.append(
                StartingPosition(
                    game_pk=int(game_pk),
                    team_id=int(team_id),
                    player_id=int(pid),
                    batting_order=bo_int // 100,
                    position_code=pos,
                    last_position_code=(str(last)[:5] if last else None),
                )
            )
    return rows


#: Repair the starters' lineup rows in one statement per game. Only a
#: row whose code differs is written, so ``updated_at`` (the table's trigger)
#: marks exactly the repaired rows.
STARTING_POSITION_UPDATE_SQL_ASYNCPG: str = """
    UPDATE raw.game_lineups AS l
    SET    position_code = v.position_code
    FROM   unnest($1::int[], $2::int[], $3::int[], $4::text[])
           AS v(game_pk, team_id, player_id, position_code)
    WHERE  l.game_pk = v.game_pk
      AND  l.team_id = v.team_id
      AND  l.player_id = v.player_id
      AND  l.sequence = 1
      AND  l.is_starter
      AND  l.position_code IS DISTINCT FROM v.position_code
"""


async def persist_starting_positions(conn: Any, rows: Iterable[StartingPosition]) -> int:
    """Write the starting positions onto the lineup rows (asyncpg). Returns the
    number of rows whose code changed (a row already right is not written)."""
    batch = list(rows)
    if not batch:
        return 0
    status = await conn.execute(
        STARTING_POSITION_UPDATE_SQL_ASYNCPG,
        [r.game_pk for r in batch],
        [r.team_id for r in batch],
        [r.player_id for r in batch],
        [r.position_code for r in batch],
    )
    return _rows_from_status(status)


def _rows_from_status(status: Any) -> int:
    """The row count in an asyncpg command tag such as ``'UPDATE 3'``; 0 when
    the tag is absent or not of that shape."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


#: SIM-443b: no default password. A blank value is resolved in main() through
#: db.dsn.require_dsn, which names the missing variable.
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
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
    args = parse_args(argv)
    args.dsn = args.dsn or require_dsn()  # SIM-443b: fail loudly with no DSN
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
