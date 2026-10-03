#!/usr/bin/env python
"""
scripts/sim555_retire_consensus_rows.py — SIM-555: archive, then delete, the old
``consensus`` odds rows, one season at a time.

WHY THIS EXISTS
---------------
Before SIM-555 every stored odds row said ``book = 'consensus'``, and the
provider mixed books inside one row. The re-load stores one row per book,
labelled ``'bp:<id>'``, and every reader filters to that label. Once the census
passes on a season's new rows (``scripts/sim555_odds_census.sql``), the old rows
of that season go: they are copied into ``raw.game_odds_archive`` /
``raw.prop_odds_archive`` (migration 0028) and deleted from the live tables. The
deletion also removes the 2024 duplicate rows of the two old loads.

WHAT IT DOES, PER SEASON, IN ONE TRANSACTION
--------------------------------------------
1. It counts the Final games of the season that have a ``consensus`` closing
   moneyline but no ``bp:`` closing moneyline, less the games named with
   ``--allow-missing``. When that count is not zero, the season is not
   re-loaded yet: the script refuses the season, prints the count and a few of
   the games, and touches nothing. It prints the named games it let through,
   with their count.
2. It also counts the Final games that have ``consensus`` closing props but no
   ``bp:`` closing prop, and prints a WARNING with the count. It does not refuse
   on that count: the vendor may no longer serve an old game's props, and the
   archive keeps the old rows. Read it in the dry run first.
3. Otherwise it copies every ``consensus`` row of the season's games into the
   archive tables (the game odds, then the props) and deletes them. It checks
   that each table archived and deleted the same number of rows as it counted
   first; a mismatch rolls the season back.

The transaction reads one snapshot (repeatable read), so a row written by a
concurrent load after the start is neither archived nor deleted.

THE NAMED GAMES (``--allow-missing``)
-------------------------------------
A few games can never get a ``bp:`` closing moneyline, so each would refuse its
season for good. ``--allow-missing GAME_PK ...`` names them. A named game does
not refuse its season, and its ``consensus`` rows go to the archive with the
season's other rows. Before the seasons, the script reads the season of each
named game. A name that refuses nothing is a stale name: the game has a ``bp:``
closing moneyline, has no ``consensus`` closing moneyline, is not Final, or sits
in no named season. The script prints a WARNING for each stale name and runs on.
Any other missing game still refuses its season.

``--dry-run`` runs steps 1 and 2 and the counts in a read-only transaction and writes
nothing. The script writes only when it runs without ``--dry-run``; no test runs
it against a real database (the tests drive it with a stub connection).

EXIT CODES
----------
0 every season archived (or, with ``--dry-run``, every season would be);
2 at least one season refused (not re-loaded yet); the other seasons ran;
3 an error: the archive table lacks a live column, or the counts disagreed
  (that season rolled back).

After a real run, VACUUM the two live tables (VACUUM cannot run inside a
transaction, so the script does not). Use ``PARALLEL 0``: the database
container's shared memory (64 MB) is too small for the parallel index workers,
and a plain ``VACUUM (ANALYZE)`` fails with "could not resize shared memory
segment ... No space left on device":

    docker compose exec -T db psql -U baseball_user -d baseball_sim -c "VACUUM (ANALYZE, PARALLEL 0) raw.game_odds;" -c "VACUUM (ANALYZE, PARALLEL 0) raw.prop_odds;"

(Historical: before step 8, games 745169, 745175, 746572, 746773 and 746755
had to be re-loaded with the fixed matcher, or step 8 refused 2024. They were
re-loaded on 2026-10-01 and hold ``bp:`` closing moneylines.)

The commands for the step-8 run (the dry run first). The mount makes the
container read this copy of the script, not the copy baked into the image:

    MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts:ro" app python scripts/sim555_retire_consensus_rows.py --seasons 2019 2020 2021 2022 2023 2024 2025 2026 --allow-missing 567323 745659 --dry-run
    MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts:ro" app python scripts/sim555_retire_consensus_rows.py --seasons 2019 2020 2021 2022 2023 2024 2025 2026 --allow-missing 567323 745659

(From the Windows Command Prompt, write ``-v "%CD%\\scripts:/app/scripts"`` and drop
``MSYS_NO_PATHCONV=1``: cmd.exe does not expand ``$PWD``.) The real run ran on
2026-10-01 with these two names and archived and deleted 359,524 game-odds rows
and 4,035,864 prop rows.

Game 567323 (2019) is named because it has no closing moneyline before first
pitch. The vendor's event matches it, but every book's moneyline close is
stamped 68-206 minutes after its first pitch, so the load guard refuses each
one. Game 745659 (2024) is game 2 of a straight double-header: its old rows
held game 1's prices (the old matcher's defect), and the vendor's own event for
it carries no sportsbook offer (only a pick'em app's props and the vendor's
blend of them).

The plan: docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md §4 and §8 step 8.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_ERROR = 3

#: (live table, archive table) — the game odds first, then the props.
TABLES: tuple[tuple[str, str], ...] = (
    ("game_odds", "game_odds_archive"),
    ("prop_odds", "prop_odds_archive"),
)

#: How many refused games the script names.
EXAMPLE_GAMES = 10

#: The Final games of a season with a consensus closing moneyline and no bp: one.
MISSING_SQL = """
SELECT g.game_pk
FROM raw.games g
WHERE g.season = $1 AND g.status = 'Final'
  AND EXISTS (
      SELECT 1 FROM raw.game_odds o
      WHERE o.game_pk = g.game_pk AND o.book = 'consensus'
        AND o.market_type = 'moneyline' AND o.line_type = 'closing')
  AND NOT EXISTS (
      SELECT 1 FROM raw.game_odds o
      WHERE o.game_pk = g.game_pk AND o.book LIKE 'bp:%'
        AND o.market_type = 'moneyline' AND o.line_type = 'closing')
ORDER BY g.game_pk
"""

#: The Final games of a season with consensus closing props and no bp: closing prop.
MISSING_PROPS_SQL = """
SELECT g.game_pk
FROM raw.games g
WHERE g.season = $1 AND g.status = 'Final'
  AND EXISTS (
      SELECT 1 FROM raw.prop_odds p
      WHERE p.game_pk = g.game_pk AND p.book = 'consensus' AND p.line_type = 'closing')
  AND NOT EXISTS (
      SELECT 1 FROM raw.prop_odds p
      WHERE p.game_pk = g.game_pk AND p.book LIKE 'bp:%' AND p.line_type = 'closing')
ORDER BY g.game_pk
"""

#: SIM-555: the season of each game named with --allow-missing (read once, before
#: the seasons; a name raw.games lacks returns no row).
NAMED_GAMES_SQL = "SELECT game_pk, season FROM raw.games WHERE game_pk = ANY($1::int[])"

#: The season's games: every status, because a consensus row may sit on a game
#: that never went Final (a postponement).
_SEASON_GAMES = "SELECT game_pk FROM raw.games WHERE season = $1"

COUNT_SQL = (
    "SELECT count(*) FROM raw.{table} WHERE book = 'consensus' AND game_pk IN ("
    + _SEASON_GAMES
    + ")"
)
ARCHIVE_SQL = (
    "INSERT INTO raw.{archive} ({columns}) SELECT {columns} FROM raw.{table} "
    "WHERE book = 'consensus' AND game_pk IN (" + _SEASON_GAMES + ")"
)
DELETE_SQL = (
    "DELETE FROM raw.{table} WHERE book = 'consensus' AND game_pk IN (" + _SEASON_GAMES + ")"
)
COLUMNS_SQL = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'raw' AND table_name = $1
ORDER BY ordinal_position
"""


class RetireError(RuntimeError):
    """The season cannot be archived safely; its transaction rolls back."""


@dataclass
class SeasonResult:
    """What the script found and did for one season."""

    season: int
    dry_run: bool
    #: Final games with a consensus closing moneyline and no bp: one, less the named games.
    missing_games: int = 0
    examples: list[int] = field(default_factory=list)
    #: SIM-555: the named games (--allow-missing) with a consensus closing moneyline and
    #: no bp: one. They do not refuse the season.
    allowed: list[int] = field(default_factory=list)
    #: SIM-555: the named games of this season that are not missing (a warning only).
    stale_names: list[int] = field(default_factory=list)
    #: Final games with consensus closing props and no bp: closing prop (a warning only).
    missing_prop_games: int = 0
    prop_examples: list[int] = field(default_factory=list)
    #: consensus rows counted per live table.
    counted: dict[str, int] = field(default_factory=dict)
    #: rows archived and deleted per live table (empty on a refusal or a dry run).
    archived: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return self.missing_games > 0


def rows_affected(status: str) -> int:
    """The row count of an asyncpg status string: ``'INSERT 0 12'`` → 12, ``'DELETE 12'`` → 12."""
    try:
        return int(str(status).strip().split()[-1])
    except (IndexError, ValueError) as exc:
        raise RetireError(f"cannot read a row count from the status {status!r}") from exc


async def archive_columns(conn: Any, table: str, archive: str) -> list[str]:
    """The live table's columns, in order, after a check that the archive holds every one.

    A live column the archive lacks would be lost by the copy, so it raises
    :class:`RetireError`. An archive column the live table lacks is left empty.
    """
    live = [str(r["column_name"]) for r in await conn.fetch(COLUMNS_SQL, table)]
    kept = {str(r["column_name"]) for r in await conn.fetch(COLUMNS_SQL, archive)}
    if not live:
        raise RetireError(f"raw.{table} has no columns (does the table exist?)")
    lost = [c for c in live if c not in kept]
    if lost:
        raise RetireError(
            f"raw.{archive} lacks the live columns {lost}; the copy would lose them "
            "(re-create the archive from the live table)"
        )
    return live


async def named_game_seasons(conn: Any, games: Collection[int]) -> dict[int, int]:
    """SIM-555: the season of each named game that raw.games holds (no query for no names)."""
    if not games:
        return {}
    rows = await conn.fetch(NAMED_GAMES_SQL, sorted({int(g) for g in games}))
    return {int(r["game_pk"]): int(r["season"]) for r in rows}


async def retire_season(
    conn: Any, season: int, *, dry_run: bool, allow_missing: Collection[int] = ()
) -> SeasonResult:
    """Check, count and (unless ``dry_run``) archive and delete one season's consensus rows.

    One transaction. A refused season and a dry run write nothing. A count that
    disagrees raises :class:`RetireError`, which rolls the season back.

    SIM-555: ``allow_missing`` holds this season's named games. A named game that
    lacks a bp: closing moneyline does not refuse the season; its consensus rows
    go to the archive with the rest. A named game that is not missing is a
    stale name, which the result lists.
    """
    result = SeasonResult(season=int(season), dry_run=dry_run)
    named = {int(g) for g in allow_missing}
    async with conn.transaction(isolation="repeatable_read", readonly=dry_run):
        missing = [int(r["game_pk"]) for r in await conn.fetch(MISSING_SQL, int(season))]
        unnamed = [g for g in missing if g not in named]
        result.missing_games = len(unnamed)
        result.examples = unnamed[:EXAMPLE_GAMES]
        result.allowed = [g for g in missing if g in named]
        result.stale_names = sorted(named.difference(missing))
        no_props = [int(r["game_pk"]) for r in await conn.fetch(MISSING_PROPS_SQL, int(season))]
        result.missing_prop_games = len(no_props)
        result.prop_examples = no_props[:EXAMPLE_GAMES]
        for table, _archive in TABLES:
            result.counted[table] = int(
                await conn.fetchval(COUNT_SQL.format(table=table), int(season)) or 0
            )
        if result.refused or dry_run:
            return result
        for table, archive in TABLES:
            columns = ", ".join(await archive_columns(conn, table, archive))
            archived = rows_affected(
                await conn.execute(
                    ARCHIVE_SQL.format(table=table, archive=archive, columns=columns), int(season)
                )
            )
            deleted = rows_affected(await conn.execute(DELETE_SQL.format(table=table), int(season)))
            expected = result.counted[table]
            if not (archived == deleted == expected):
                raise RetireError(
                    f"season {season} raw.{table}: counted {expected}, archived {archived}, "
                    f"deleted {deleted}; rolled back"
                )
            result.archived[table] = archived
            result.deleted[table] = deleted
    return result


def format_result(result: SeasonResult) -> str:
    """One season's outcome in plain words (a second line carries the props warning)."""
    counts = ", ".join(f"raw.{t} {n:,}" for t, n in result.counted.items())
    if result.refused:
        line = (
            f"season {result.season}: REFUSED — {result.missing_games} Final games have a "
            f"consensus closing moneyline and no bp: one (first: {result.examples}). "
            f"Nothing changed. Consensus rows: {counts}."
        )
    elif result.dry_run:
        line = f"season {result.season}: dry run — would archive and delete {counts}."
    else:
        done = ", ".join(f"raw.{t} {n:,}" for t, n in result.archived.items())
        line = f"season {result.season}: archived and deleted {done}."
    if result.allowed:
        line += (
            f"\n  ALLOWED by --allow-missing ({len(result.allowed)}): {result.allowed}. Each has "
            "a consensus closing moneyline and no bp: one; none refuses the season."
        )
    if result.stale_names:
        line += (
            f"\n  WARNING: stale --allow-missing names {result.stale_names}. Each has a bp: "
            "closing moneyline, no consensus closing moneyline, or is not Final; the name "
            "changes nothing."
        )
    if result.missing_prop_games:
        line += (
            f"\n  WARNING: {result.missing_prop_games} Final games have consensus closing props "
            f"and no bp: closing prop (first: {result.prop_examples}); their old props go to "
            "the archive all the same."
        )
    return line


async def run(
    conn: Any, seasons: list[int], *, dry_run: bool, allow_missing: Collection[int] = ()
) -> int:
    """Every season in order; a refused season does not stop the others. Returns the exit code.

    SIM-555: each season gets its own named games from ``allow_missing``. A named
    game in no named season is a stale name: a WARNING, never a stop.
    """
    code = EXIT_OK
    archived_any = False
    named = sorted({int(g) for g in allow_missing})
    season_of = await named_game_seasons(conn, named)
    outside = [g for g in named if season_of.get(g) not in seasons]
    if outside:
        print(
            f"WARNING: stale --allow-missing names {outside}. Each is in no named season "
            "(or not in raw.games); the name changes nothing."
        )
    for season in seasons:
        mine = [g for g in named if season_of.get(g) == season]
        try:
            result = await retire_season(conn, season, dry_run=dry_run, allow_missing=mine)
        except RetireError as exc:
            print(f"season {season}: ERROR — {exc}")
            code = EXIT_ERROR
            continue
        print(format_result(result))
        archived_any = archived_any or bool(result.archived)
        if result.refused and code == EXIT_OK:
            code = EXIT_REFUSED
    if archived_any:
        print(
            "Next: VACUUM (ANALYZE, PARALLEL 0) raw.game_odds; "
            "VACUUM (ANALYZE, PARALLEL 0) raw.prop_odds;"
        )
    return code


async def _main_async(args: argparse.Namespace) -> int:
    import asyncpg

    conn = await asyncpg.connect(args.dsn, timeout=60)
    try:
        return await run(
            conn,
            list(args.seasons),
            dry_run=bool(args.dry_run),
            allow_missing=list(args.allow_missing),
        )
    finally:
        await conn.close()


def _game_pk(text: str) -> int:
    """SIM-555: one --allow-missing value, a game_pk (a whole number above zero)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a game_pk: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"not a game_pk: {text!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", required=True, help="the seasons to retire")
    ap.add_argument("--dry-run", action="store_true", help="check and count only; write nothing")
    ap.add_argument(
        "--allow-missing",
        type=_game_pk,
        nargs="+",
        default=[],
        metavar="GAME_PK",
        help="games that may lack a bp: closing moneyline; they do not refuse their season",
    )
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN", ""))
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("no DSN: pass --dsn or set BASEBALL_DB_DSN")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
