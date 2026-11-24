"""
ingest/load_boxscores.py
===============================
The official per-player box score as the prop ground truth (SIM-545, part
of the unpriced prop markets, SIM-421).

WHY
---
A sportsbook grades a player prop against the OFFICIAL box score. The platform
derives its prop "actuals" from ``raw.pitches`` event labels today, which
covers only H / HR / TB and K / BB and misses what lives in no pitch row (an
intentional walk, a pickoff out). This module copies the box score verbatim
into ``raw.game_player_stats`` (Alembic 0023) so every prop the market offers
has the same actual the book uses.

WHAT
----
* :func:`parse_boxscore` — a PURE function over the box score's ``teams`` dict.
  It works for both the ``/api/v1/game/{game_pk}/boxscore`` response and the
  live feed's ``liveData.boxscore`` (the two carry the identical ``teams``
  shape). It emits one :class:`PlayerGameStats` per player who APPEARED: the
  box marks a non-participant with an EMPTY ``stats.batting`` and
  ``stats.pitching`` dict, and a participant has ``gamesPlayed = 1`` in the
  slot he played.
* :class:`BoxscoreIngest` — the fetch + persist wrapper. Network is ONE
  stubbable seam, :meth:`BoxscoreIngest._mlb_get`, over the one MLB client
  (``ingest.mlb.client``) with a small bounded retry. Persistence is :meth:`persist`
  (asyncpg, ``INSERT … ON CONFLICT (game_pk, player_id) DO UPDATE`` so a
  re-run refreshes) and :func:`persist_sync` (the psycopg2 twin the historical
  loader calls with its own pooled connection).

The module imports stdlib only at load time, so it is importable without a
running app and the parser is unit-tested against a captured payload
(``tests/fixtures/mlb/boxscore_746437.json``) with no live call and no DB.
Season, game date and the team ids come from ``raw.games``; the caller passes
them in.
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

#: MLB box-score pitching field → ``raw.game_player_stats`` column
#: (``gamesStarted`` is handled apart: it becomes the ``p_started`` boolean).
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
    """One player's official line for one game — mirrors ``raw.game_player_stats``.

    The field order IS the INSERT column order (see :data:`COLUMNS`); keep the
    two in step. ``fetched_at`` is the table's default and is not carried here.
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
        """Positional tuple in :data:`COLUMNS` order (the INSERT value list)."""
        return tuple(getattr(self, name) for name in COLUMNS)

    def as_dict(self) -> dict[str, Any]:
        """The row as a mapping keyed by column name (what the reader consumes)."""
        return {name: getattr(self, name) for name in COLUMNS}


#: The INSERT column list, derived from the dataclass so the two cannot drift.
COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(PlayerGameStats))

#: Every column the upsert refreshes on conflict (all but the primary key).
_UPDATE_COLUMNS: tuple[str, ...] = tuple(c for c in COLUMNS if c not in ("game_pk", "player_id"))


def _upsert_sql(placeholders: Sequence[str]) -> str:
    """Render the upsert with the driver's placeholder style."""
    cols = ", ".join(COLUMNS)
    vals = ", ".join(placeholders)
    sets = ",\n                ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)
    return f"""
            INSERT INTO raw.game_player_stats ({cols})
            VALUES ({vals})
            ON CONFLICT (game_pk, player_id) DO UPDATE SET
                {sets},
                fetched_at = now()
    """


#: The upsert for asyncpg (``$1 … $n`` placeholders).
UPSERT_SQL_ASYNCPG: str = _upsert_sql([f"${i}" for i in range(1, len(COLUMNS) + 1)])

#: The upsert for psycopg2 (``%s`` placeholders) — the historical loader's driver.
UPSERT_SQL_PSYCOPG2: str = _upsert_sql(["%s"] * len(COLUMNS))


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
    """``battingOrder`` '100' → slot 1; '701' (a sub in slot 7) → 7; absent → None."""
    if value in (None, ""):
        return None
    try:
        return int(value) // 100
    except (TypeError, ValueError):
        return None


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
    ``liveData["boxscore"]["teams"]`` from the live feed — the same shape:
    ``{"home"|"away": {"team": {"id"}, "players": {"ID<pid>": {"person":
    {"id"}, "position": {"abbreviation"}, "battingOrder", "stats": {"batting":
    {...}, "pitching": {...}}}}}}``.

    A player appears when his ``stats.batting`` OR ``stats.pitching`` dict is
    non-empty; ``played_bat`` / ``played_pitch`` read ``gamesPlayed >= 1`` in
    the respective block, so a two-way player keeps both slots. A player with
    both dicts empty (a bench bat, an unused reliever) produces NO row.

    ``team_id`` comes from ``teams[side]["team"]["id"]``; the caller's
    ``home_team_id`` / ``away_team_id`` (from ``raw.games``) are the fallback
    when the payload omits it. ``season`` and ``game_date`` are the caller's.
    """
    fallback_team = {"home": home_team_id, "away": away_team_id}
    rows: list[PlayerGameStats] = []
    for side in SIDES:
        team_box = teams.get(side) or {}
        team_id = (team_box.get("team") or {}).get("id")
        if team_id is None:
            team_id = fallback_team[side]
        for pdata in (team_box.get("players") or {}).values():
            pid = (pdata.get("person") or {}).get("id")
            if pid is None:
                continue
            stats = pdata.get("stats") or {}
            batting = stats.get("batting") or {}
            pitching = stats.get("pitching") or {}
            if not batting and not pitching:
                continue  # did not appear — no row, the book voids his bet
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


async def persist(conn: Any, rows: Iterable[PlayerGameStats]) -> int:
    """UPSERT rows through an asyncpg connection (or pool). Returns rows written.

    ``ON CONFLICT (game_pk, player_id) DO UPDATE`` so a re-run refreshes a
    game's rows in place. Empty input writes nothing and returns 0.
    """
    batch = [r.as_tuple() for r in rows]
    if not batch:
        return 0
    await conn.executemany(UPSERT_SQL_ASYNCPG, batch)
    return len(batch)


def persist_sync(cur: Any, rows: Iterable[PlayerGameStats]) -> int:
    """UPSERT rows on a psycopg2 CURSOR (the historical loader's driver).

    Runs on the caller's cursor so it shares the caller's transaction; the
    caller commits. Returns rows written; empty input writes nothing.
    """
    batch = [r.as_tuple() for r in rows]
    if not batch:
        return 0
    from psycopg2.extras import execute_batch

    execute_batch(cur, UPSERT_SQL_PSYCOPG2, batch)
    return len(batch)


# ---------------------------------------------------------------------------
# The per-game bullpen listing (the arms the box lists per side)
# ---------------------------------------------------------------------------

#: ``raw.game_bullpen.listed`` values, in the order the parser emits them.
LISTED_BULLPEN = "bullpen"
LISTED_PITCHED = "pitched"
LISTED_STARTED = "started"


@dataclass(frozen=True, slots=True)
class BullpenListing:
    """One arm the MLB box lists for a game (the real pen, SIM-427; Alembic 0025).

    ``listed``: ``'bullpen'`` — on the roster, did not pitch; ``'pitched'`` — a
    reliever who appeared; ``'started'`` — the side's starting pitcher (the
    first entry of the box's ``pitchers`` list). The field order IS the INSERT
    column order (:data:`BULLPEN_COLUMNS`).
    """

    game_pk: int
    pitcher_id: int
    team_id: int
    side: str
    listed: str

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(getattr(self, name) for name in BULLPEN_COLUMNS)


BULLPEN_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(BullpenListing))


def _bullpen_upsert_sql(placeholders: Sequence[str]) -> str:
    cols = ", ".join(BULLPEN_COLUMNS)
    return (
        f"INSERT INTO raw.game_bullpen ({cols}) VALUES ({', '.join(placeholders)}) "
        "ON CONFLICT (game_pk, pitcher_id) DO UPDATE SET "
        "team_id = EXCLUDED.team_id, side = EXCLUDED.side, listed = EXCLUDED.listed, "
        "fetched_at = now()"
    )


BULLPEN_UPSERT_SQL_ASYNCPG: str = _bullpen_upsert_sql(
    [f"${i}" for i in range(1, len(BULLPEN_COLUMNS) + 1)]
)
BULLPEN_UPSERT_SQL_PSYCOPG2: str = _bullpen_upsert_sql(["%s"] * len(BULLPEN_COLUMNS))


def parse_bullpen_listing(
    teams: Mapping[str, Any],
    *,
    game_pk: int,
    home_team_id: int | None = None,
    away_team_id: int | None = None,
) -> list[BullpenListing]:
    """The arms the box lists per side: every id in ``bullpen`` as
    ``'bullpen'``, the first id in ``pitchers`` as ``'started'``, the rest of
    ``pitchers`` as ``'pitched'``. An id in both lists (it happens on a
    corrected box) keeps its ``pitchers`` reading. The same ``teams`` dict
    :func:`parse_boxscore` reads, so one fetch serves both tables."""
    fallback_team = {"home": home_team_id, "away": away_team_id}
    rows: list[BullpenListing] = []
    for side in SIDES:
        team_box = teams.get(side) or {}
        team_id = (team_box.get("team") or {}).get("id")
        if team_id is None:
            team_id = fallback_team[side]
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
    """UPSERT bullpen listings through an asyncpg connection. Returns rows written."""
    batch = [r.as_tuple() for r in rows]
    if not batch:
        return 0
    await conn.executemany(BULLPEN_UPSERT_SQL_ASYNCPG, batch)
    return len(batch)


def persist_bullpen_sync(cur: Any, rows: Iterable[BullpenListing]) -> int:
    """UPSERT bullpen listings on a psycopg2 cursor (the historical loader)."""
    batch = [r.as_tuple() for r in rows]
    if not batch:
        return 0
    from psycopg2.extras import execute_batch

    execute_batch(cur, BULLPEN_UPSERT_SQL_PSYCOPG2, batch)
    return len(batch)


# ---------------------------------------------------------------------------
# The starting position
# ---------------------------------------------------------------------------


def starting_position(pdata: Mapping[str, Any]) -> str | None:
    """The position a player held when he entered the game.

    The box's ``position`` is the LAST position the player held in the game.
    ``allPositions`` lists every position he held, in order, so its first entry
    is where he started. A two-way starter who pitches and bats as the
    designated hitter lists P then DH and reads 'P'. A payload without
    ``allPositions``, or with a blank first entry, falls back to ``position``.
    None when neither names a position.
    """
    all_positions = pdata.get("allPositions") or []
    first = all_positions[0] if all_positions else None
    abbr = first.get("abbreviation") if isinstance(first, Mapping) else None
    if not abbr:
        abbr = (pdata.get("position") or {}).get("abbreviation")
    return str(abbr)[:5] if abbr else None


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
        team_id = (team_box.get("team") or {}).get("id")
        if team_id is None:
            team_id = fallback_team[side]
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


# ---------------------------------------------------------------------------
# The fetch wrapper
# ---------------------------------------------------------------------------


@dataclass
class BoxscoreIngest:
    """Fetch the official box score for a game and persist its rows.

    Parameters
    ----------
    timeout:
        HTTP timeout (seconds) per MLB Stats API call.
    max_attempts:
        Bounded retry on 429 / 5xx / a timeout; a 4xx other than 429 is not
        retried (it is a permanent answer for that game).
    backoff:
        Seconds before the second attempt; doubles per attempt. A numeric
        ``Retry-After`` header on a retried status replaces the wait for that
        attempt (capped at 60 s).
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
        """Fetch ``/game/{game_pk}/boxscore`` and return its ``teams`` dict."""
        payload = self._mlb_get(f"game/{int(game_pk)}/boxscore")
        teams = payload.get("teams")
        if not isinstance(teams, dict):
            raise ValueError(f"boxscore for game {game_pk} carries no 'teams' block")
        return teams

    # --------------------------------------------------------------- pipeline
    def fetch_rows(
        self,
        game_pk: int,
        *,
        season: int,
        game_date: date,
        home_team_id: int | None = None,
        away_team_id: int | None = None,
    ) -> list[PlayerGameStats]:
        """Fetch + parse one game's box score into rows (no persistence)."""
        teams = self.fetch_boxscore(game_pk)
        return parse_boxscore(
            teams,
            game_pk=game_pk,
            season=season,
            game_date=game_date,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )

    async def persist(self, conn: Any, rows: Iterable[PlayerGameStats]) -> int:
        """UPSERT rows through an asyncpg connection — see :func:`persist`."""
        return await persist(conn, rows)


__all__ = [
    "BATTING_FIELDS",
    "BULLPEN_COLUMNS",
    "BULLPEN_UPSERT_SQL_ASYNCPG",
    "BULLPEN_UPSERT_SQL_PSYCOPG2",
    "BullpenListing",
    "COLUMNS",
    "BoxscoreIngest",
    "PITCHING_FIELDS",
    "PlayerGameStats",
    "SIDES",
    "STARTING_POSITION_UPDATE_SQL_ASYNCPG",
    "StartingPosition",
    "UPSERT_SQL_ASYNCPG",
    "parse_bullpen_listing",
    "persist_bullpen",
    "persist_bullpen_sync",
    "UPSERT_SQL_PSYCOPG2",
    "parse_boxscore",
    "parse_starting_positions",
    "persist",
    "persist_starting_positions",
    "persist_sync",
    "starting_position",
]


# ===========================================================================
# The backfill: python -m ingest.load_boxscores
# ===========================================================================
#
# Fetch the box score of every completed game in raw.games and store one
# raw.game_player_stats row per player who appeared, plus the bullpen listing
# (raw.game_bullpen). The historical loader writes both for each game it
# loads; the backfill covers the games already stored.
#
#     python -m ingest.load_boxscores --seasons 2024 --max-games 200
#     python -m ingest.load_boxscores --game-pks 746437 746438
#     python -m ingest.load_boxscores --seasons 2024 --no-only-missing   # refresh
#
# One MLB request per game, --sleep seconds apart. --only-missing (the default)
# skips a game with rows. One game's failure is logged and counted; five in a
# row stop the run (--max-consecutive-failures, 0 = never). The exit code is 1
# when the run stops early, or when it wrote nothing and a game failed.

#: No default password. main() resolves a blank value through
#: db.dsn.require_dsn, which names the missing variable.
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")

#: Games between progress log lines.
PROGRESS_EVERY = 100

#: Failures in a row that stop the run (the historical loader's rule):
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
    by default; ``raw.game_bullpen`` for the bullpen pass) so a re-run
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

    The same fetch also writes the per-game bullpen listing
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
    args = parse_args(argv)
    args.dsn = args.dsn or require_dsn()  # Fail loudly with no DSN
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
