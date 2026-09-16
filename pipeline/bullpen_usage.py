"""
pipeline/bullpen_usage.py
=========================
SIM-427 — the pen's facts from the official box score and the MLB API's
per-game bullpen listing, for the pitching-change draw and the reliever draw.

Two consumers share these queries so the pool rows and the live candidates
carry the SAME definitions (the fit compares one against the other):

* the artifact builder (``pipeline/batch/engine_artifacts.py``,
  ``build_pitching_change_pool``), in bulk, for every appearance in the pool
  window — the incoming arm's rest as of the game he entered, and the game's
  two managers;
* the lineup resolver (``simulation/lineup_resolver.py``), per game, for the
  arms the box lists as available — the live candidates' rest.

THE FACTS
---------
* ``days_rest`` — days since the arm's last appearance, CAPPED at
  :data:`REST_CAP_DAYS` (five or more days is "fully rested"; a Gaussian on the
  raw count would call a 30-day arm far from a 6-day arm). No appearance in
  the trailing :data:`LOOKBACK_DAYS` days reads as the cap.
* ``pitched_2d`` — the arm appeared on either of the two preceding days
  (the owner's "pitched in the last two days", 2026-09-13).
* ``pitches_3d`` — his pitches over the three preceding days (``p_pitches``).
* the ROLE — an arm that STARTED any of the team's last :data:`ROTATION_GAMES`
  games with at least :data:`ROTATION_START_MIN_PITCHES` pitches is a rotation
  starter and leaves the pen (an opener's short start does not count); the
  box's ``bullpen`` list carries the rotation's off-day starters, so this rule
  is what makes the listing a pen.

All windows are strictly BEFORE the game date. Two games on one date (a
doubleheader) do not see each other's usage — a known, small gap.

Every query is plain SQL in two spellings (asyncpg ``$n``, psycopg2 ``%s``)
built by one function each, so a unit test pins the text once.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

#: Days of rest at which an arm counts as fully rested (and the cap).
REST_CAP_DAYS = 5
#: How far back an arm's appearances are read.
LOOKBACK_DAYS = 30
#: The team's most recent games whose starters form its rotation.
ROTATION_GAMES = 5
#: A start counts as a ROTATION start only from this many pitches: an opener's
#: start (under 45 pitches) leaves him a reliever. Measured 2024-2025: 58% of the
#: relievers the plain rule wrongly removed had opened (a start under 45 pitches);
#: a real rotation starter knocked out that early is rare (about 1 start in 25).
ROTATION_START_MIN_PITCHES = 45
#: "Recent" pitches: the preceding three days.
PITCHES_WINDOW_DAYS = 3
#: "Pitched recently": either of the two preceding days.
PITCHED_WINDOW_DAYS = 2

#: The values ``raw.game_bullpen.listed`` takes (the box ingest writes them).
LISTED_AVAILABLE: tuple[str, ...] = ("bullpen", "pitched")


@dataclass(frozen=True, slots=True)
class ArmUsage:
    """One arm's recent usage as of a game date."""

    pitcher_id: int
    days_rest: int  # capped at REST_CAP_DAYS
    pitched_2d: bool
    pitches_3d: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (int(self.days_rest), 1 if self.pitched_2d else 0, int(self.pitches_3d))


def _ph(driver: str, n: int) -> str:
    return f"${n}" if driver == "asyncpg" else "%s"


def usage_for_appearances_sql(driver: str = "asyncpg") -> str:
    """Bulk: every appearance in the given seasons with the arm's rest AS OF
    that game (strictly earlier appearances). One parameter: the seasons array.
    Columns: game_pk, player_id, days_rest, pitched_2d, pitches_3d."""
    p1 = _ph(driver, 1)
    cast = "::int[]" if driver == "asyncpg" else ""
    return f"""
        WITH app AS (
            SELECT game_pk, player_id, game_date
            FROM raw.game_player_stats
            WHERE played_pitch AND season = ANY({p1}{cast})
        )
        SELECT a.game_pk, a.player_id,
               LEAST(COALESCE((a.game_date - MAX(b.game_date))::int, {REST_CAP_DAYS}),
                     {REST_CAP_DAYS}) AS days_rest,
               COALESCE(BOOL_OR(b.game_date >= a.game_date - {PITCHED_WINDOW_DAYS}), FALSE)
                   AS pitched_2d,
               COALESCE(SUM(b.p_pitches)
                        FILTER (WHERE b.game_date >= a.game_date - {PITCHES_WINDOW_DAYS}), 0)::int
                   AS pitches_3d
        FROM app a
        LEFT JOIN raw.game_player_stats b
               ON b.player_id = a.player_id AND b.played_pitch
              AND b.game_date < a.game_date AND b.game_date >= a.game_date - {LOOKBACK_DAYS}
        GROUP BY a.game_pk, a.player_id, a.game_date
    """


def usage_for_candidates_sql(driver: str = "asyncpg") -> str:
    """Per game: the rest of a list of arms as of a date. Parameters: the
    pitcher-id array, the game date. Columns: player_id, days_rest,
    pitched_2d, pitches_3d — every requested id appears (no appearance in the
    window = fully rested, no recent pitches)."""
    p1, p2 = _ph(driver, 1), _ph(driver, 2)
    cast = "::int[]" if driver == "asyncpg" else ""
    return f"""
        SELECT p.player_id,
               LEAST(COALESCE(({p2}::date - MAX(b.game_date))::int, {REST_CAP_DAYS}),
                     {REST_CAP_DAYS}) AS days_rest,
               COALESCE(BOOL_OR(b.game_date >= {p2}::date - {PITCHED_WINDOW_DAYS}), FALSE)
                   AS pitched_2d,
               COALESCE(SUM(b.p_pitches)
                        FILTER (WHERE b.game_date >= {p2}::date - {PITCHES_WINDOW_DAYS}), 0)::int
                   AS pitches_3d
        FROM unnest({p1}{cast}) AS p(player_id)
        LEFT JOIN raw.game_player_stats b
               ON b.player_id = p.player_id AND b.played_pitch
              AND b.game_date < {p2}::date AND b.game_date >= {p2}::date - {LOOKBACK_DAYS}
        GROUP BY p.player_id
    """


def rotation_starters_sql(driver: str = "asyncpg") -> str:
    """The team's rotation as of a date: the distinct starters of its last
    ROTATION_GAMES games before the date whose start ran at least
    ROTATION_START_MIN_PITCHES pitches (an opener's start does not make him a
    rotation starter). Parameters: team_id, game date."""
    p1, p2 = _ph(driver, 1), _ph(driver, 2)
    return f"""
        SELECT DISTINCT player_id
        FROM (
            SELECT player_id, p_pitches,
                   DENSE_RANK() OVER (ORDER BY game_date DESC, game_pk DESC) AS rk
            FROM raw.game_player_stats
            WHERE team_id = {p1} AND p_started AND game_date < {p2}::date
        ) t
        WHERE rk <= {ROTATION_GAMES} AND p_pitches >= {ROTATION_START_MIN_PITCHES}
    """


def game_listing_sql(driver: str = "asyncpg") -> str:
    """The arms the box lists for one game and team. Parameters: game_pk, team_id."""
    p1, p2 = _ph(driver, 1), _ph(driver, 2)
    return f"""
        SELECT pitcher_id, listed
        FROM raw.game_bullpen
        WHERE game_pk = {p1} AND team_id = {p2}
    """


def game_managers_sql(driver: str = "asyncpg") -> str:
    """The two managers of every game in the given seasons. Parameter: the seasons array."""
    p1 = _ph(driver, 1)
    cast = "::int[]" if driver == "asyncpg" else ""
    return f"""
        SELECT game_pk, home_manager_id, away_manager_id
        FROM raw.games
        WHERE season = ANY({p1}{cast})
    """


def pen_from_listing(
    listing: Iterable[tuple[int, str]],
    rotation: Iterable[int],
    exclude: Iterable[int] = (),
) -> list[int]:
    """PURE: the pen = the arms listed as available (``bullpen`` or ``pitched``)
    minus the rotation starters minus ``exclude`` (today's starter). Order:
    ``pitched`` arms first in listing order (the arms the manager used), then
    the rest — a stable order the positional fallback can read."""
    rot = {int(x) for x in rotation}
    skip = rot | {int(x) for x in exclude}
    pitched: list[int] = []
    sat: list[int] = []
    seen: set[int] = set()
    for pid, listed in listing:
        pid = int(pid)
        if pid in skip or pid in seen or listed not in LISTED_AVAILABLE:
            continue
        seen.add(pid)
        (pitched if listed == "pitched" else sat).append(pid)
    return pitched + sat


# ---------------------------------------------------------------------------
# The async (resolver) side
# ---------------------------------------------------------------------------


async def fetch_pen_for_game(
    conn: Any,
    *,
    game_pk: int,
    team_id: int,
    game_date: date,
    exclude: Iterable[int] = (),
) -> tuple[list[int], dict[int, ArmUsage]] | None:
    """The team's pen for the game and each arm's usage, or None when the
    box lists nothing for the game (the caller falls back and says so)."""
    listing = await conn.fetch(game_listing_sql("asyncpg"), int(game_pk), int(team_id))
    if not listing:
        return None
    rotation = await conn.fetch(rotation_starters_sql("asyncpg"), int(team_id), game_date)
    pen = pen_from_listing(
        [(int(r["pitcher_id"]), str(r["listed"])) for r in listing],
        [int(r["player_id"]) for r in rotation],
        exclude,
    )
    usage = await fetch_usage_for_candidates(conn, pen, game_date)
    return pen, usage


async def fetch_usage_for_candidates(
    conn: Any, pitcher_ids: Sequence[int], game_date: date
) -> dict[int, ArmUsage]:
    ids = sorted({int(p) for p in pitcher_ids})
    if not ids:
        return {}
    rows = await conn.fetch(usage_for_candidates_sql("asyncpg"), ids, game_date)
    out: dict[int, ArmUsage] = {}
    for r in rows:
        pid = int(r["player_id"])
        out[pid] = ArmUsage(
            pitcher_id=pid,
            days_rest=int(r["days_rest"]),
            pitched_2d=bool(r["pitched_2d"]),
            pitches_3d=int(r["pitches_3d"]),
        )
    return out


# ---------------------------------------------------------------------------
# The sync (artifact builder) side
# ---------------------------------------------------------------------------


#: ``raw.players.throws`` -> the pool's int8 code (0 = unknown).
THROWS_CODE: dict[str, int] = {"L": 1, "R": 2}


def pitcher_throws_sql(driver: str = "asyncpg") -> str:
    """Every pitcher's throwing hand (``raw.players``). No parameters."""
    return "SELECT player_id, throws FROM raw.players WHERE throws IS NOT NULL"


def fetch_game_context_sync(dsn: str, seasons: Sequence[int]) -> tuple[dict, dict, dict]:
    """The builder's one Postgres read: ``{game_pk: (home_manager_id,
    away_manager_id)}``, ``{(game_pk, pitcher_id): (days_rest, pitched_2d,
    pitches_3d)}`` for every appearance in ``seasons``, and ``{pitcher_id:
    throws code}`` (1 = L, 2 = R). psycopg2, one connection, read-only."""
    import psycopg2

    seasons_list = [int(s) for s in seasons]
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(game_managers_sql("psycopg2"), (seasons_list,))
            managers = {int(g): (h, a) for g, h, a in cur.fetchall()}
            cur.execute(usage_for_appearances_sql("psycopg2"), (seasons_list,))
            usage = {
                (int(g), int(p)): (int(d), 1 if b else 0, int(n))
                for g, p, d, b, n in cur.fetchall()
            }
            cur.execute(pitcher_throws_sql("psycopg2"))
            throws = {int(pid): THROWS_CODE.get(str(t).upper()[:1], 0) for pid, t in cur.fetchall()}
    finally:
        conn.close()
    return managers, usage, throws


__all__ = [
    "LISTED_AVAILABLE",
    "LOOKBACK_DAYS",
    "PITCHED_WINDOW_DAYS",
    "PITCHES_WINDOW_DAYS",
    "REST_CAP_DAYS",
    "ROTATION_GAMES",
    "ROTATION_START_MIN_PITCHES",
    "THROWS_CODE",
    "ArmUsage",
    "fetch_game_context_sync",
    "fetch_pen_for_game",
    "fetch_usage_for_candidates",
    "game_listing_sql",
    "game_managers_sql",
    "pen_from_listing",
    "pitcher_throws_sql",
    "rotation_starters_sql",
    "usage_for_appearances_sql",
    "usage_for_candidates_sql",
]
