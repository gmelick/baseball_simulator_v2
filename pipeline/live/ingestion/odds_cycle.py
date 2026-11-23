"""The live odds cycle's timing and broadcast form: the cadence, the staleness test and the
JSON form of an offer.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from betting.load_odds_to_db import _stamp_param
from pipeline.live.ingestion.constants import (
    PREGAME_NEAR_START_CADENCE_S,
    PREGAME_NEAR_START_WINDOW_S,
    PREGAME_ODDS_CADENCE_S,
)

#: The by-book row keys that serve the load guard and the logs only. They are
#: never stored and never sent to a browser: the scheduled start (a datetime),
#: the official date and a made-up game's postponed original start.
_GUARD_ONLY_KEYS = frozenset({"scheduled_start", "game_date", "postponed_start"})


def _json_default(value: Any) -> str:
    """The ``json.dumps`` fallback: a date or a datetime as ISO-8601 text.

    The BettingPros rows carry ``book_line_at`` as a datetime; a bare
    ``json.dumps`` raises ``TypeError`` on it. Any other unknown type still
    raises, so a real bug stays loud.
    """
    if isinstance(value, date):  # a datetime is a date too
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _jsonable_odds(odds: Mapping[str, Any] | None) -> dict[str, Any]:
    """An odds row made safe for ``json.dumps``.

    The row loses the guard-only keys (``scheduled_start``, ``game_date``,
    ``postponed_start``) and sends a date or a datetime (``book_line_at``) as
    ISO-8601 text. Every other key passes through unchanged. ``None`` gives an
    empty dict.
    """
    out: dict[str, Any] = {}
    for key, value in (odds or {}).items():
        if key in _GUARD_ONLY_KEYS:
            continue
        out[key] = value.isoformat() if isinstance(value, date) else value
    return out


def _odds_facts_may_be_stale(game: Mapping[str, Any]) -> bool:
    """True when the odds provider's cached facts of a schedule entry's game may be old.

    The provider keeps a found game's schedule facts and vendor event for the
    process lifetime. A game looked up before a postponement keeps its
    original start and event. Three kinds of entry say the facts changed:

      * the original date's entry of a postponed or suspended game
        (``rescheduleGameDate`` / ``resumeGameDate``);
      * a postponed entry not yet rescheduled (``detailedState`` starting
        "Postponed", or ``codedGameState`` "D", as the provider reads it);
      * a made-up or resumed game before its first pitch (``rescheduledFrom`` /
        ``resumedFrom`` while ``Preview``). The poll reads one calendar date's
        schedule (``date.today()``, UTC in the container), so it misses the
        original date's entry of a game postponed after midnight UTC; this
        entry catches that game on its new date.

    The poll drops the cached facts on each such entry. A live game keeps
    them, so the per-pitch refresh reads nothing again.
    """
    if "rescheduleGameDate" in game or "resumeGameDate" in game:
        return True
    status = game.get("status") or {}
    if str(status.get("detailedState") or "").startswith("Postponed"):
        return True
    if status.get("codedGameState") == "D":
        return True
    made_up = "rescheduledFrom" in game or "resumedFrom" in game
    return made_up and status.get("abstractGameState") == "Preview"


def _scheduled_start(game: Mapping[str, Any]) -> datetime | None:
    """A schedule entry's scheduled start as an aware UTC datetime.

    The entry's ``gameDate`` is ISO 8601 UTC text ("2026-10-09T23:05:00Z").
    A missing or unreadable value gives ``None``.
    """
    return _stamp_param(game.get("gameDate"))


def pregame_odds_cadence_s(game: Mapping[str, Any], now: datetime) -> int:
    """PURE: the pre-game cycle's cadence in seconds for one schedule entry.

    Inside PREGAME_NEAR_START_WINDOW_S of the scheduled start, or past the
    start while the game still waits in ``Preview`` (a delay), the cadence is
    PREGAME_NEAR_START_CADENCE_S (60 s). Further out, or with no readable
    start, it is PREGAME_ODDS_CADENCE_S (600 s). ``now`` is an aware datetime.
    """
    start = _scheduled_start(game)
    if start is None:
        return PREGAME_ODDS_CADENCE_S
    if (start - now).total_seconds() <= PREGAME_NEAR_START_WINDOW_S:
        return PREGAME_NEAR_START_CADENCE_S
    return PREGAME_ODDS_CADENCE_S
