"""The schedule helpers: the date windows of a range load, the final-game
filter and the circuit-breaker limit.
"""

from __future__ import annotations

import os
from datetime import (
    date,
    timedelta,
)

# The MLB schedule endpoint silently clamps any window wider than a year
# to startDate + 365 and still answers HTTP 200. Slice below that so nothing is
# lost, and leave headroom so a leap year cannot tip a slice over the limit.
_MAX_SCHEDULE_WINDOW_DAYS = 364

# How many games may fail back-to-back before a run aborts. One bad feed
# must be survivable across a 21,600-game sweep; an API outage or a schema
# mismatch must not be, because it would otherwise "succeed" having loaded almost
# nothing.
_CONSECUTIVE_FAILURE_LIMIT = int(os.environ.get("ETL_CONSECUTIVE_FAILURE_LIMIT", "5"))


def _date_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive date range into <=_MAX_SCHEDULE_WINDOW_DAYS slices."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=_MAX_SCHEDULE_WINDOW_DAYS), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# Schedule filtering
# ---------------------------------------------------------------------------


def _schedule_game_is_final(game: dict) -> bool:
    """True iff a schedule game entry is a completed ('Final') game.

    The historical loader pulls pitch-by-pitch Statcast data, which only exists
    for games that have actually been played.  A season schedule for the CURRENT
    year includes future / in-progress games (``abstractGameState`` 'Preview' /
    'Live'); loading those wastes one fetch each (0 pitches) and inserts an empty
    ``raw.games`` stub.  Gating on Final also makes ``refresh_seasons`` a correct
    nightly incremental updater (it picks up games as they finish).
    """
    return game.get("status", {}).get("abstractGameState") == "Final"
