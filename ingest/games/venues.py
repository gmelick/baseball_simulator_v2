"""Resolve the venue of a game: from the feed, or from the overrides for the neutral-site
games whose feed has none.
"""

from __future__ import annotations

import logging
from typing import Any

from ingest.games.coercion import to_str

log = logging.getLogger(__name__)


class MissingVenueError(RuntimeError):
    """A game's feed carries no venue, and no venue is known for it."""


# The games whose feed has no ``gameData.venue`` at all.
#
# A one-off neutral-site game can ship with no venue anywhere in the feed: the
# schedule endpoint returns ``{"link": "/api/v1/venues/null"}``. MLB lists the
# venue in the unfiltered ``/api/v1/venues`` catalogue (not in the season lists),
# so each entry here is a lookup, not a guess.
#
# Never fall back to ``teams.home.venue``: for these games it is the home team's
# REGULAR park. A wrong venue_id passes the NOT NULL and the foreign key, so
# nothing flags it, and it silently distorts that park's park factor.
_VENUE_OVERRIDES: dict[int, tuple[int, str]] = {
    632924: (5445, "Field of Dreams"),  # 2021-08-12 NYY @ CWS — Dyersville, IA
    663023: (5445, "Field of Dreams"),  # 2022-08-11 CHC @ CIN — Dyersville, IA
}


def _resolve_venue(game_pk: int, game_data: dict) -> tuple[int, str]:
    """Return the ``(id, name)`` of the venue where the game was PLAYED.

    Raises :class:`MissingVenueError` rather than guess, with the steps to fix
    it. ``raw.pitches.venue_id`` is NOT NULL with a foreign key to ``raw.venues``,
    so the database accepts a wrong venue without complaint. The error then shows
    up only much later, as a distorted park factor.
    """
    venue = game_data.get("venue") or {}
    if venue.get("id") is not None:
        return int(venue["id"]), to_str(venue.get("name")) or ""

    override = _VENUE_OVERRIDES.get(game_pk)
    if override is not None:
        log.warning(
            "game %s: feed omits gameData.venue; using documented override venue %s (%s)",
            game_pk,
            override[0],
            override[1],
        )
        return override

    raise MissingVenueError(
        f"game {game_pk}: the feed has no gameData.venue and no override is registered. "
        f"This is an MLB API gap on one-off neutral-site games (Field of Dreams, and "
        f"potentially the Little League Classic / international series). Look the venue "
        f"up in the UNFILTERED catalogue — https://statsapi.mlb.com/api/v1/venues — and "
        f"add it to _VENUE_OVERRIDES with a dated comment. Do NOT substitute "
        f"teams.home.venue: for these games that is the home team's regular park, and it "
        f"would silently corrupt that park's park factor."
    )


def _resolve_venue_id(game_pk: int, game_data: dict) -> int:
    """:func:`_resolve_venue` for the call sites that only need the id."""
    return _resolve_venue(game_pk, game_data)[0]


def _first_nonblank(*candidates: Any) -> str:
    """Return the first candidate that is a non-blank string, else ``""``.

    A whitespace-only value must not count: ``" "`` is truthy, so an ``or`` guard
    downstream accepts it as a real name. :func:`to_str` strips each candidate
    and maps whitespace to ``None``.
    """
    for candidate in candidates:
        text = to_str(candidate)
        if text:
            return text
    return ""
