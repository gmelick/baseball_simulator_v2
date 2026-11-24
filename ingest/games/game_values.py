"""The game, lineup and player values the loader reads from the feed: the
manager, the height, the wind, the game status and the starting lineup rows.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ingest.mlb.game_feed import starting_position

log = logging.getLogger(__name__)


def _manager_from_coaches(game_pk: int, side: str, coaches: list[dict]) -> tuple[Any, str]:
    """Return a team's manager ``(id, name)`` from its coaches.

    The order is the acting manager (NTRM), then the manager (MNGR), then the
    bench coach (COAB). The result is ``("", "")`` when the list has none of them.

    There is no fallback to ``coaches[0]``: the first coach listed is usually a
    pitching coach. A wrong id reaches every pitch row, ``raw.games`` and
    ``raw.managers``, where ``_ensure_managers`` then marks the real manager
    departed. An empty id is safe: ``_ensure_managers`` skips it, and
    ``_ensure_game`` writes NULL.
    """
    manager_id: Any = ""
    manager_name = ""
    for coach in coaches:
        if coach["jobId"] == "NTRM":
            manager_id = coach["person"]["id"]
            manager_name = coach["person"]["fullName"]
            break
        if coach["jobId"] == "MNGR":
            manager_id = coach["person"]["id"]
            manager_name = coach["person"]["fullName"]
        if coach["jobId"] == "COAB" and manager_name == "":
            manager_id = coach["person"]["id"]
            manager_name = coach["person"]["fullName"]
    if manager_name == "":
        log.warning(
            "  game %s: no NTRM/MNGR/COAB coach for the %s team (%s roles "
            "returned); leaving %s_manager unset rather than guessing",
            game_pk,
            side,
            len(coaches),
            side,
        )
    return manager_id, manager_name


def _parse_height(height_str: str | None) -> int | None:
    """Convert an MLB height string (``6' 2"`` or ``6-2``) to inches.

    Returns None when the string is absent or does not parse.
    """
    if not height_str:
        return None
    try:
        height_str = height_str.replace('"', "").replace("'", "-").replace(" ", "")
        parts = height_str.split("-")
        return int(parts[0]) * 12 + int(parts[1])
    except Exception:
        return None


_WIND_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*mph\s*,\s*(.+?)\s*$", re.IGNORECASE)


def _parse_wind(raw: Any) -> tuple[int | None, str | None]:
    """Split the feed's wind string into ``(speed_mph, direction)``.

    The feed has one formatted string under ``gameData.weather.wind``, such as
    ``"8 mph, L To R"``, ``"12 mph, Out To CF"`` or ``"Calm"``. It has no separate
    speed and direction keys.

    Returns ``(None, None)`` for an absent value. Returns ``(0, "Calm")`` for
    ``"Calm"``: a windless day is a real observation, not missing data.
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
        # An unknown shape: keep the text as the direction, and invent no speed.
        log.debug("Unrecognised wind string %r; storing direction only", raw)
        return None, text[:50] or None
    return int(round(float(m.group(1)))), m.group(2)[:50]


def _map_game_status(gd: dict) -> str:
    """Map the feed's game state to the ``raw.games`` status value.

    The coded states D, T and C (postponed, suspended, cancelled) win over the
    abstract state, which can read "Final" for them.
    """
    abstract = gd.get("status", {}).get("abstractGameState", "Preview")
    coded = gd.get("status", {}).get("codedGameState", "")
    status_map = {
        "Preview": "Preview",
        "Live": "Live",
        "Final": "Final",
    }
    base = status_map.get(abstract, "Preview")
    if coded in ("D",):
        return "Postponed"
    if coded in ("T",):
        return "Suspended"
    if coded in ("C",):
        return "Cancelled"
    return base


def _build_starting_lineup_rows(game_pk: int, season: int, game_dict: dict) -> list[tuple]:
    """Build the ``raw.game_lineups`` rows of both starting lineups from the feed's box score.

    The source is ``liveData.boxscore.teams.{home,away}``. A player whose
    ``battingOrder`` ends in ``00`` starts in slot ``battingOrder // 100`` (1 to 9).
    The starting pitcher (``pitchers[0]``) joins with ``position_code='P'`` and a
    NULL ``batting_order`` when he does not bat (a designated-hitter game). The
    rows hold the opening lineup only, with no substitutions.

    ``position_code`` is the position the starter held at the first pitch: the
    feed's ``allPositions[0]``. The feed's ``position`` is his LAST position, which
    puts a fielder who moved during the game in the wrong slot of the defense map.
    A two-way starter who pitches and bats as the designated hitter is coded 'P'.

    Returns tuples in the insert's column order: ``(game_pk, season, team_id,
    player_id, batting_order, position_code, is_starter, sequence)``.
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
                continue  # a substitute in the slot, not the starter
            pid = pdata.get("person", {}).get("id")
            if pid is None:
                continue
            pos = starting_position(pdata) or "UT"
            rows.append((game_pk, season, int(team_id), int(pid), bo_int // 100, pos[:5], True, 1))
            seen.add(int(pid))
        # The starting pitcher, when he is not also in the batting order.
        pitchers = team_box.get("pitchers") or []
        if pitchers and int(pitchers[0]) not in seen:
            rows.append((game_pk, season, int(team_id), int(pitchers[0]), None, "P", True, 1))
    return rows
