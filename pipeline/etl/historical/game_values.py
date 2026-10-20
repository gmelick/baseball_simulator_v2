"""The game, lineup and player values the loader reads from the feed: the
height, the wind, the game status and the starting lineup rows.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.etl.boxscore_ingest import starting_position

log = logging.getLogger("etl_historical_loader")


# ---------------------------------------------------------------------------
# Module-level helpers used by _ensure_prerequisites
# ---------------------------------------------------------------------------


def _parse_height(height_str: str | None) -> int | None:
    """
    Converts MLB API height string (e.g. "6' 2\"") to total inches.
    Returns None if the string is absent or unparseable.
    """
    if not height_str:
        return None
    try:
        # Format: "6' 2\"" or "6-2"
        height_str = height_str.replace('"', "").replace("'", "-").replace(" ", "")
        parts = height_str.split("-")
        return int(parts[0]) * 12 + int(parts[1])
    except Exception:
        return None


_WIND_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*mph\s*,\s*(.+?)\s*$", re.IGNORECASE)


def _parse_wind(raw: Any) -> tuple[int | None, str | None]:
    """Split the MLB feed's single wind string into (speed_mph, direction).

    SIM-441. The feed exposes ONE formatted string under ``gameData.weather.wind``
    — e.g. ``"8 mph, L To R"``, ``"12 mph, Out To CF"``, ``"Calm"`` — but this
    loader read ``weather["speed"]`` and ``weather["direction"]``, keys the feed
    has never provided. Both columns were therefore NULL on every game ever
    loaded. (The two variables were also bound to the same dict, which is how the
    mistake survived review.)

    Returns ``(None, None)`` for an absent value, and ``(0, "Calm")`` for the
    literal ``"Calm"`` the feed uses on a windless day — that is a real
    observation, not missing data, and collapsing it to NULL would lose it.
    """
    if not raw or not isinstance(raw, str):
        return None, None
    text = raw.strip()
    if not text:
        return None, None
    if text.lower() == "calm":
        return 0, "Calm"
    m = _WIND_RE.match(text)
    if not m:
        # Unknown shape — keep the direction text rather than dropping the row's
        # only wind information, but do not invent a speed.
        log.debug("Unrecognised wind string %r; storing direction only", raw)
        return None, text[:50] or None
    return int(round(float(m.group(1)))), m.group(2)[:50]


def _map_game_status(gd: dict) -> str:
    """
    Maps the MLB API abstractGameState to the raw.games status enum.
    """
    abstract = gd.get("status", {}).get("abstractGameState", "Preview")
    coded = gd.get("status", {}).get("codedGameState", "")
    status_map = {
        "Preview": "Preview",
        "Live": "Live",
        "Final": "Final",
    }
    base = status_map.get(abstract, "Preview")
    # Handle edge cases that share abstractGameState="Final"
    if coded in ("D",):
        return "Postponed"
    if coded in ("T",):
        return "Suspended"
    if coded in ("C",):
        return "Cancelled"
    return base


def _build_starting_lineup_rows(game_pk: int, season: int, game_dict: dict) -> list[tuple]:
    """Build raw.game_lineups rows for the STARTING lineups from a feed/live boxscore (SIM-409).

    Reads ``liveData.boxscore.teams.{home,away}``: a player whose ``battingOrder``
    ends in ``00`` (``int(bo) % 100 == 0``) is the original starter in slot
    ``int(bo) // 100`` (1–9); the starting pitcher (``pitchers[0]``) is added with
    ``position_code='P'`` and a NULL ``batting_order`` when not in the batting
    order (AL / DH games). Substitutions (sequence > 1, pinch roles) are out of
    scope — this ingests the opening lineup the sim resolves from.

    ``position_code`` is the position the starter held at first pitch (SIM-559):
    the feed's ``allPositions[0]``. The feed's ``position`` is the LAST position
    he held, and the rows loaded before SIM-559 carry that value, so a fielder
    who moved mid-game left his starting slot empty in the defense map and a
    second player at his final one. A two-way starter who pitches and bats as
    the designated hitter is coded 'P'.

    Returns tuples in the raw.game_lineups insert column order:
    ``(game_pk, season, team_id, player_id, batting_order, position_code,
    is_starter, sequence)``.
    """
    gd = game_dict.get("gameData", {})
    box = game_dict.get("liveData", {}).get("boxscore", {}).get("teams", {})
    rows: list[tuple] = []
    for side in ("home", "away"):
        team_id = gd.get("teams", {}).get(side, {}).get("id")
        team_box = box.get(side, {})
        if team_id is None or not team_box:
            continue
        seen: set[int] = set()
        for pdata in team_box.get("players", {}).values():
            bo = pdata.get("battingOrder")
            if not bo:
                continue
            try:
                bo_int = int(bo)
            except (TypeError, ValueError):
                continue
            if bo_int % 100 != 0:
                continue  # a later-entering occupant of the slot, not the starter
            pid = pdata.get("person", {}).get("id")
            if pid is None:
                continue
            pos = starting_position(pdata) or "UT"
            rows.append((game_pk, season, int(team_id), int(pid), bo_int // 100, pos[:5], True, 1))
            seen.add(int(pid))
        # Starting pitcher — not in the batting order for AL/DH games.
        pitchers = team_box.get("pitchers") or []
        if pitchers and int(pitchers[0]) not in seen:
            rows.append((game_pk, season, int(team_id), int(pitchers[0]), None, "P", True, 1))
    return rows
