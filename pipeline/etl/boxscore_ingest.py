"""
pipeline/etl/boxscore_ingest.py
===============================
The official per-player box score as the prop ground truth (SIM-545, the
sub-ticket of the unpriced-prop-markets work, SIM-421).

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
  stubbable seam, :meth:`BoxscoreIngest._mlb_get` (stdlib ``urllib``, a small
  bounded retry on 429 / 5xx / timeouts). Persistence is :meth:`persist`
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

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from typing import Any

log = logging.getLogger("pipeline.etl.boxscore_ingest")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"

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

#: HTTP statuses the fetch retries (rate limit + server-side errors).
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Never sleep longer than this on a server's ``Retry-After`` say-so (the
#: historical loader's cap, SIM-441).
_MAX_RETRY_AFTER_S = 60.0


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """The server's ``Retry-After`` in seconds, capped at :data:`_MAX_RETRY_AFTER_S`.

    Returns ``None`` when the header is absent or is not a number (an
    HTTP-date), so the caller falls back to its doubling wait.
    """
    headers = exc.headers
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return min(float(raw), _MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        return None


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
# SIM-427: the per-game bullpen listing (the arms the box lists per side)
# ---------------------------------------------------------------------------

#: ``raw.game_bullpen.listed`` values, in the order the parser emits them.
LISTED_BULLPEN = "bullpen"
LISTED_PITCHED = "pitched"
LISTED_STARTED = "started"


@dataclass(frozen=True, slots=True)
class BullpenListing:
    """One arm the MLB box lists for a game (SIM-427, Alembic 0025).

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
    """The arms the box lists per side (SIM-427): every id in ``bullpen`` as
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
# The fetch wrapper
# ---------------------------------------------------------------------------


@dataclass
class BoxscoreIngest:
    """Fetch the official box score for a game and persist its rows (SIM-545).

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
    def _http_get_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers={"User-Agent": "baseball-sim/SIM-545"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — fixed host
            return json.loads(resp.read().decode("utf-8"))

    def _mlb_get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the ONLY network seam — stubbed in tests).

        Retries a 429 / 5xx / timeout up to ``max_attempts`` with a doubling
        wait; re-raises the last error when the attempts run out. A retried
        status that carries a numeric ``Retry-After`` header waits that long
        instead (capped at :data:`_MAX_RETRY_AFTER_S`), so a rate-limited run
        backs off as the server asks rather than hammering it every second.
        """
        qs = urllib.parse.urlencode(dict(params or {}))
        url = f"{_MLB_BASE}/{path}?{qs}" if qs else f"{_MLB_BASE}/{path}"
        wait = self.backoff
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            delay = wait
            try:
                return self._http_get_json(url)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in _RETRY_STATUSES:
                    raise
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    delay = retry_after
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if attempt < self.max_attempts:
                log.info(
                    "SIM-545: %s attempt %d failed (%s); retrying in %.1fs",
                    url,
                    attempt,
                    last,
                    delay,
                )
                time.sleep(delay)
                wait *= 2
        assert last is not None
        raise last

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
    "UPSERT_SQL_ASYNCPG",
    "parse_bullpen_listing",
    "persist_bullpen",
    "persist_bullpen_sync",
    "UPSERT_SQL_PSYCOPG2",
    "parse_boxscore",
    "persist",
    "persist_sync",
]
