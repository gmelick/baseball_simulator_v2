"""The schedule helpers: the date windows of a range load, the Final-game filter and the
limit on games that fail in a row.
"""

from __future__ import annotations

import os
from datetime import (
    date,
    timedelta,
)

# The MLB schedule endpoint cuts any window wider than a year to startDate + 365
# and still answers HTTP 200. The slices stay below that, with a day of headroom
# for a leap year.
_MAX_SCHEDULE_WINDOW_DAYS = 364

# How many games may fail in a row before a run stops. A run must survive one bad
# feed. It must not survive an API outage or a schema mismatch: such a run would
# otherwise "succeed" with almost nothing loaded.
_CONSECUTIVE_FAILURE_LIMIT = int(os.environ.get("ETL_CONSECUTIVE_FAILURE_LIMIT", "5"))


def _date_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive date range into slices that the schedule endpoint answers in full."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=_MAX_SCHEDULE_WINDOW_DAYS), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks


def _schedule_game_is_final(game: dict) -> bool:
    """Return True when a schedule entry is a completed ('Final') game.

    Only a played game has pitch-by-pitch data. The current season's schedule
    also lists games not yet played or in progress ('Preview' / 'Live'). Loading
    one wastes a fetch and writes an empty ``raw.games`` row. The filter also lets
    a nightly ``refresh_seasons`` pick up each game as it finishes.
    """
    return game.get("status", {}).get("abstractGameState") == "Final"
