"""Load each game's official box score into ``raw.game_player_stats`` and ``raw.game_bullpen``.

A sportsbook grades a player prop against the official box score, so the
platform grades props on these rows. The box also holds what no pitch row
records, such as an intentional walk or a pickoff out.

* :func:`parse_boxscore` turns a box score's ``teams`` dict into one
  :class:`PlayerGameStats` per player who appeared. The boxscore endpoint and
  the live feed's ``liveData.boxscore`` carry the same ``teams`` shape.
* :func:`parse_bullpen_listing` turns the same dict into one
  :class:`BullpenListing` per arm the box lists, so one fetch fills both tables.
* :func:`persist` and :func:`persist_bullpen` upsert through asyncpg;
  :func:`persist_sync` and :func:`persist_bullpen_sync` upsert on the historical
  loader's psycopg2 cursor. A re-run refreshes a row.
* :class:`BoxscoreIngest` fetches one box score through ``ingest.mlb.client``.
  Its :meth:`~BoxscoreIngest._mlb_get` is the one network seam the tests stub.

The caller passes the season, the game date and the team ids from ``raw.games``.
The database drivers load only inside the functions that use them, so the
parsers run in a test with no database.

The backfill loads the games already in ``raw.games``; the historical loader
writes both tables for each game it loads::

    python -m ingest.load_boxscores --seasons 2024 --max-games 200
    python -m ingest.load_boxscores --game-pks 746437 746438
    python -m ingest.load_boxscores --seasons 2024 --no-only-missing   # refresh
    python -m ingest.load_boxscores --seasons 2024 --bullpen-only

The backfill sends one MLB request per game, ``--sleep`` seconds apart.
``--only-missing`` (the default) skips a game that has rows. The run logs and
counts a failed game; five failures in a row stop it
(``--max-consecutive-failures``, 0 = never). The exit code is 1 when the run
stops early, or when it writes nothing and a game fails.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from typing import Any

from db.dsn import require_dsn
from ingest.mlb import client as mlb_client
from ingest.mlb.client import RetryPolicy

log = logging.getLogger("ingest.load_boxscores")


#: The two sides of a box score, in the order the rows are emitted.
SIDES: tuple[str, ...] = ("home", "away")

#: MLB box-score batting field → ``raw.game_player_stats`` column.
BATTING_FIELDS: dict[str, str] = {
    "plateAppearances": "pa",
    "atBats": "ab",
    "runs": "r",
    "hits": "h",
    "doubles": "b2",
    "triples": "b3",
    "homeRuns": "hr",
    "rbi": "rbi",
    "stolenBases": "sb",
    "caughtStealing": "cs",
    "baseOnBalls": "bb",
    "strikeOuts": "k",
    "hitByPitch": "hbp",
    "sacFlies": "sf",
    "totalBases": "tb",
}

#: MLB box-score pitching field → ``raw.game_player_stats`` column.
#: ``gamesStarted`` is not here: the parser turns it into the ``p_started`` boolean.
PITCHING_FIELDS: dict[str, str] = {
    "outs": "p_outs",
    "hits": "p_h",
    "runs": "p_r",
    "earnedRuns": "p_er",
    "baseOnBalls": "p_bb",
    "strikeOuts": "p_k",
    "homeRuns": "p_hr",
    "numberOfPitches": "p_pitches",
    "battersFaced": "p_batters_faced",
}


@dataclass(frozen=True, slots=True)
class PlayerGameStats:
    """One player's official line for one game: one ``raw.game_player_stats`` row.

    The field order is the INSERT column order (:data:`COLUMNS`). The table's
    default fills ``fetched_at``.
    """

    game_pk: int
    player_id: int
    team_id: int
    side: str  # 'home' | 'away'
    season: int
    game_date: date
    batting_order: int | None
    position_code: str | None
    played_bat: bool
    played_pitch: bool
    # batting line
    pa: int = 0
    ab: int = 0
    r: int = 0
    h: int = 0
    b2: int = 0
    b3: int = 0
    hr: int = 0
    rbi: int = 0
    sb: int = 0
    cs: int = 0
    bb: int = 0
    k: int = 0
    hbp: int = 0
    sf: int = 0
    tb: int = 0
    # pitching line
    p_outs: int = 0
    p_h: int = 0
    p_r: int = 0
    p_er: int = 0
    p_bb: int = 0
    p_k: int = 0
    p_hr: int = 0
    p_pitches: int = 0
    p_batters_faced: int = 0
    p_started: bool = False

    def as_tuple(self) -> tuple[Any, ...]:
        """The row's values in :data:`COLUMNS` order, for the INSERT."""
        return tuple(getattr(self, name) for name in COLUMNS)


#: The INSERT column list, derived from the dataclass so the two cannot drift.
COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(PlayerGameStats))


def _upsert_sql(
    table: str, columns: Sequence[str], key: Sequence[str], placeholders: Sequence[str]
) -> str:
    """The upsert of ``columns`` into ``table``.

    A conflict on ``key`` refreshes every other column and ``fetched_at``.
    """
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {sets}, fetched_at = now()"
    )


def _asyncpg_marks(n: int) -> list[str]:
    """asyncpg's placeholders: ``$1 … $n``."""
    return [f"${i}" for i in range(1, n + 1)]


_PLAYER_KEY = ("game_pk", "player_id")

#: The player-row upsert for asyncpg (``$n``) and for psycopg2 (``%s``, the game loader's driver).
UPSERT_SQL_ASYNCPG: str = _upsert_sql(
    "raw.game_player_stats", COLUMNS, _PLAYER_KEY, _asyncpg_marks(len(COLUMNS))
)
UPSERT_SQL_PSYCOPG2: str = _upsert_sql(
    "raw.game_player_stats", COLUMNS, _PLAYER_KEY, ["%s"] * len(COLUMNS)
)


# ---------------------------------------------------------------------------
# The pure parser
# ---------------------------------------------------------------------------


def _int(value: Any) -> int:
    """Coerce a box-score count to ``int``; a missing or odd value reads 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _batting_order(value: Any) -> int | None:
    """The lineup slot of a ``battingOrder`` code: '100' is 1, '701' (a sub in slot 7) is 7.

    None when the code is absent or not a number.
    """
    if value in (None, ""):
        return None
    try:
        return int(value) // 100
    except (TypeError, ValueError):
        return None


def side_team_id(team_box: Mapping[str, Any], fallback: int | None) -> int | None:
    """A side's team id from the box, else the caller's id from ``raw.games``."""
    team_id = (team_box.get("team") or {}).get("id")
    return fallback if team_id is None else team_id


def parse_boxscore(
    teams: Mapping[str, Any],
    *,
    game_pk: int,
    season: int,
    game_date: date,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
) -> list[PlayerGameStats]:
    """Parse a box score's ``teams`` dict into one row per player who appeared.

    ``teams`` is ``response["teams"]`` from the boxscore endpoint, or
    ``liveData["boxscore"]["teams"]`` from the live feed. Both have this shape:
    ``{"home"|"away": {"team": {"id"}, "players": {"ID<pid>": {"person":
    {"id"}, "position": {"abbreviation"}, "battingOrder", "stats": {"batting":
    {...}, "pitching": {...}}}}}}``.

    A player appeared when his ``stats.batting`` or ``stats.pitching`` dict is
    not empty. ``played_bat`` and ``played_pitch`` read ``gamesPlayed >= 1`` in
    each block, so a two-way player keeps both. A player with both dicts empty
    (a bench bat, an unused reliever) gets no row.

    ``team_id`` comes from the box. The caller's ``home_team_id`` and
    ``away_team_id`` (from ``raw.games``) fill a missing one; a side with
    neither logs a warning and gets no rows.
    """
    fallback_team = {"home": home_team_id, "away": away_team_id}
    rows: list[PlayerGameStats] = []
    for side in SIDES:
        team_box = teams.get(side) or {}
        team_id = side_team_id(team_box, fallback_team[side])
        for pdata in (team_box.get("players") or {}).values():
            pid = (pdata.get("person") or {}).get("id")
            if pid is None:
                continue
            stats = pdata.get("stats") or {}
            batting = stats.get("batting") or {}
            pitching = stats.get("pitching") or {}
            if not batting and not pitching:
                continue  # he did not appear: no row, and the book voids his bet
            if team_id is None:
                log.warning(
                    "SIM-545: game %s %s side has no team id — player %s skipped.",
                    game_pk,
                    side,
                    pid,
                )
                continue
            values: dict[str, Any] = {
                "game_pk": int(game_pk),
                "player_id": int(pid),
                "team_id": int(team_id),
                "side": side,
                "season": int(season),
                "game_date": game_date,
                "batting_order": _batting_order(pdata.get("battingOrder")),
                "position_code": ((pdata.get("position") or {}).get("abbreviation") or None),
                "played_bat": _int(batting.get("gamesPlayed")) >= 1,
                "played_pitch": _int(pitching.get("gamesPlayed")) >= 1,
            }
            if values["position_code"] is not None:
                values["position_code"] = str(values["position_code"])[:5]
            for mlb_name, col in BATTING_FIELDS.items():
                values[col] = _int(batting.get(mlb_name))
            for mlb_name, col in PITCHING_FIELDS.items():
                values[col] = _int(pitching.get(mlb_name))
            values["p_started"] = _int(pitching.get("gamesStarted")) >= 1
            rows.append(PlayerGameStats(**values))
    return rows


# ---------------------------------------------------------------------------
# Persistence (two drivers, one SQL)
# ---------------------------------------------------------------------------


async def _write_async(conn: Any, sql: str, rows: Iterable[Any]) -> int:
    """Run ``sql`` once per row through asyncpg. Returns the row count."""
    batch = [r.as_tuple() for r in rows]
    if batch:
        await conn.executemany(sql, batch)
    return len(batch)


def _write_sync(cur: Any, sql: str, rows: Iterable[Any]) -> int:
    """Run ``sql`` once per row on a psycopg2 cursor, in the caller's transaction.

    Returns the row count.
    """
    batch = [r.as_tuple() for r in rows]
    if batch:
        from psycopg2.extras import execute_batch

        execute_batch(cur, sql, batch)
    return len(batch)


async def persist(conn: Any, rows: Iterable[PlayerGameStats]) -> int:
    """Upsert player rows through asyncpg. Returns the rows written."""
    return await _write_async(conn, UPSERT_SQL_ASYNCPG, rows)


def persist_sync(cur: Any, rows: Iterable[PlayerGameStats]) -> int:
    """Upsert player rows on a psycopg2 cursor (the game loader's). The caller commits."""
    return _write_sync(cur, UPSERT_SQL_PSYCOPG2, rows)


# ---------------------------------------------------------------------------
# The per-game bullpen listing (the arms the box lists per side)
# ---------------------------------------------------------------------------

#: ``raw.game_bullpen.listed`` values, in the order the parser emits them.
LISTED_BULLPEN = "bullpen"
LISTED_PITCHED = "pitched"
LISTED_STARTED = "started"


@dataclass(frozen=True, slots=True)
class BullpenListing:
    """One arm the MLB box lists for a game: one ``raw.game_bullpen`` row.

    The simulator's pitching-change draw takes its pen from these rows.
    ``listed`` is ``'bullpen'`` (on the roster, did not pitch), ``'pitched'``
    (a reliever who appeared) or ``'started'`` (the side's starting pitcher:
    the first entry of the box's ``pitchers`` list). The field order is the
    INSERT column order (:data:`BULLPEN_COLUMNS`).
    """

    game_pk: int
    pitcher_id: int
    team_id: int
    side: str
    listed: str

    def as_tuple(self) -> tuple[Any, ...]:
        """The row's values in :data:`BULLPEN_COLUMNS` order, for the INSERT."""
        return tuple(getattr(self, name) for name in BULLPEN_COLUMNS)


#: The INSERT column list of ``raw.game_bullpen``, from the dataclass fields.
BULLPEN_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(BullpenListing))


_BULLPEN_KEY = ("game_pk", "pitcher_id")
BULLPEN_UPSERT_SQL_ASYNCPG: str = _upsert_sql(
    "raw.game_bullpen", BULLPEN_COLUMNS, _BULLPEN_KEY, _asyncpg_marks(len(BULLPEN_COLUMNS))
)
BULLPEN_UPSERT_SQL_PSYCOPG2: str = _upsert_sql(
    "raw.game_bullpen", BULLPEN_COLUMNS, _BULLPEN_KEY, ["%s"] * len(BULLPEN_COLUMNS)
)


def parse_bullpen_listing(
    teams: Mapping[str, Any],
    *,
    game_pk: int,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
) -> list[BullpenListing]:
    """The arms the box lists for each side, from the ``teams`` dict :func:`parse_boxscore` reads.

    The first id in ``pitchers`` reads ``'started'``, the rest of ``pitchers``
    ``'pitched'``, and every id in ``bullpen`` ``'bullpen'``. An id in both
    lists (a corrected box does this) keeps its ``pitchers`` reading. A side
    with no team id logs a warning and gets no rows.
    """
    fallback_team = {"home": home_team_id, "away": away_team_id}
    rows: list[BullpenListing] = []
    for side in SIDES:
        team_box = teams.get(side) or {}
        team_id = side_team_id(team_box, fallback_team[side])
        if team_id is None:
            log.warning("SIM-427: game %s %s side has no team id — no bullpen rows.", game_pk, side)
            continue
        seen: dict[int, str] = {}
        pitchers = [int(x) for x in (team_box.get("pitchers") or []) if x is not None]
        for i, pid in enumerate(pitchers):
            seen[pid] = LISTED_STARTED if i == 0 else LISTED_PITCHED
        for x in team_box.get("bullpen") or []:
            if x is None:
                continue
            pid = int(x)
            seen.setdefault(pid, LISTED_BULLPEN)
        for pid, listed in seen.items():
            rows.append(
                BullpenListing(
                    game_pk=int(game_pk),
                    pitcher_id=pid,
                    team_id=int(team_id),
                    side=side,
                    listed=listed,
                )
            )
    return rows


async def persist_bullpen(conn: Any, rows: Iterable[BullpenListing]) -> int:
    """Upsert bullpen listings through asyncpg. Returns the rows written."""
    return await _write_async(conn, BULLPEN_UPSERT_SQL_ASYNCPG, rows)


def persist_bullpen_sync(cur: Any, rows: Iterable[BullpenListing]) -> int:
    """Upsert bullpen listings on a psycopg2 cursor (the game loader's). The caller commits."""
    return _write_sync(cur, BULLPEN_UPSERT_SQL_PSYCOPG2, rows)


# ---------------------------------------------------------------------------
# The fetch wrapper
# ---------------------------------------------------------------------------


@dataclass
class BoxscoreIngest:
    """Fetch the official box score of one game.

    ``timeout`` is the HTTP time-out in seconds per call. ``max_attempts``
    counts every attempt, the first included. A transient failure (a time-out,
    a 429 or a 5xx) retries by :class:`~ingest.mlb.client.RetryPolicy`; any
    other 4xx is the final answer for that game. ``backoff`` is the wait in
    seconds before the second attempt, and it doubles on each later attempt.
    """

    timeout: float = 15.0
    max_attempts: int = 4
    backoff: float = 1.0

    # ----------------------------------------------------------------- network
    def _mlb_get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the network seam the tests stub)."""
        return mlb_client.get_json(
            f"{mlb_client.API_V1}/{path}",
            params,
            headers={"User-Agent": "baseball-sim/SIM-545"},
            timeout_s=self.timeout,
            policy=RetryPolicy(retries=self.max_attempts - 1, first_wait_s=self.backoff),
            logger=log,
            label="The box-score load",
        )

    def fetch_boxscore(self, game_pk: int) -> dict[str, Any]:
        """Fetch ``/game/{game_pk}/boxscore`` and return its ``teams`` dict.

        Raises ``ValueError`` when the response has no ``teams`` dict.
        """
        payload = self._mlb_get(f"game/{int(game_pk)}/boxscore")
        teams = payload.get("teams")
        if not isinstance(teams, dict):
            raise ValueError(f"boxscore for game {game_pk} carries no 'teams' block")
        return teams


__all__ = [
    "BATTING_FIELDS",
    "BULLPEN_COLUMNS",
    "BULLPEN_UPSERT_SQL_ASYNCPG",
    "BULLPEN_UPSERT_SQL_PSYCOPG2",
    "BoxscoreIngest",
    "BullpenListing",
    "COLUMNS",
    "PITCHING_FIELDS",
    "PlayerGameStats",
    "SIDES",
    "UPSERT_SQL_ASYNCPG",
    "UPSERT_SQL_PSYCOPG2",
    "parse_boxscore",
    "parse_bullpen_listing",
    "persist",
    "persist_bullpen",
    "persist_bullpen_sync",
    "persist_sync",
]


# ===========================================================================
# The backfill (python -m ingest.load_boxscores; the module docstring has the usage)
# ===========================================================================

#: Blank when ``BASEBALL_DB_DSN`` is unset; :func:`main` then fails through
#: ``db.dsn.require_dsn``, which names the missing variable.
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")

#: Games between progress log lines.
PROGRESS_EVERY = 100

#: Failures in a row that stop the run. One bad game is survivable. A run of
#: failures means an API outage or a schema mismatch, and the run would
#: otherwise "succeed" with nothing loaded.
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
    """The query of the completed games: Final, scored, ordered by game_pk.

    ``game_pks`` names the games when given; else ``seasons`` selects them.
    ``only_missing`` skips a game that already has rows in ``missing_table``:
    the box rows by default, ``raw.game_bullpen`` for the bullpen pass.
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


async def _fetch_final_games(conn: Any, args: argparse.Namespace) -> list[GameRef]:
    """The games the run loads, from ``raw.games``."""
    rows = await conn.fetch(
        final_games_sql(
            sorted({int(s) for s in args.seasons}),
            args.max_games,
            sorted({int(g) for g in args.game_pks}),
            args.only_missing,
            missing_table=("raw.game_bullpen" if args.bullpen_only else "raw.game_player_stats"),
        )
    )
    return [
        GameRef(
            game_pk=int(r["game_pk"]),
            season=int(r["season"]),
            game_date=r["game_date"],
            home_team_id=int(r["home_team_id"]),
            away_team_id=int(r["away_team_id"]),
        )
        for r in rows
    ]


async def load_game(
    ingest: BoxscoreIngest, conn: Any, ref: GameRef, *, bullpen_only: bool = False
) -> int:
    """Fetch one game's box score and upsert its rows. Returns the player rows written.

    The same fetch writes the bullpen listing (``raw.game_bullpen``).
    ``bullpen_only`` skips the player rows, for the games whose box rows are
    already loaded, and returns the listing rows written instead.
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
    # Return the player-row count: the run counts a game with 0 as an empty box.
    written = await persist(conn, rows)
    await persist_bullpen(conn, pen_rows)
    return written


async def run(args: argparse.Namespace) -> int:
    """Run the backfill. Returns the exit code: 0, 1 when the run fails, 2 with no games named."""
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
                written = await load_game(ingest, conn, ref, bullpen_only=args.bullpen_only)
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
    """The backfill's command-line entry point. Returns the exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    args.dsn = args.dsn or require_dsn()  # raises with no DSN
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
