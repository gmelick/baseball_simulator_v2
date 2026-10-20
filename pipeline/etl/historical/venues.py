"""The venue of a game, read from the feed, with the overrides for the
neutral-site games that ship without one.
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.etl.coercion import to_str

log = logging.getLogger("etl_historical_loader")


class MissingVenueError(RuntimeError):
    """A game's feed carries no venue, and no venue is known for it."""


# SIM-447: games whose feed omits ``gameData.venue`` ENTIRELY.
#
# MLB's one-off neutral-site games can ship with no venue ANYWHERE in the feed:
# ``gameData.venue`` is absent and the schedule endpoint returns
# ``{"link": "/api/v1/venues/null"}``. Before this map, ``gd["venue"]["id"]``
# raised a bare ``KeyError``, the sweep's per-game isolation swallowed it, and
# the two affected games silently kept their pre-SIM-440 rows.
#
# The venue is not derivable from the game data, but MLB DOES publish it in the
# unfiltered ``/api/v1/venues`` catalog — it is missing only from the
# season-filtered lists — so each entry here is a lookup, not a guess.
#
# ``teams.home.venue`` is deliberately NOT used as a fallback: for these games it
# reports the home team's REGULAR park, so it would attribute a game played in a
# cornfield in Dyersville, Iowa to Guaranteed Rate Field and quietly distort that
# park's SIM-411 park factor. A wrong venue_id satisfies the NOT NULL and the FK,
# so nothing downstream would ever flag it.
_VENUE_OVERRIDES: dict[int, tuple[int, str]] = {
    632924: (5445, "Field of Dreams"),  # 2021-08-12 NYY @ CWS — Dyersville, IA
    663023: (5445, "Field of Dreams"),  # 2022-08-11 CHC @ CIN — Dyersville, IA
}


def _resolve_venue(game_pk: int, game_data: dict) -> tuple[int, str]:
    """The ``(id, name)`` of the venue a game was actually PLAYED at.

    Fails loudly rather than guessing. ``raw.pitches.venue_id`` is NOT NULL with
    an FK to ``raw.venues``, so a plausible-but-wrong venue is accepted by the
    database without complaint and only ever surfaces as a distorted park factor
    much later. An unrecognised neutral site should stop the game and say what to
    do about it.
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
    """The first candidate that is a non-blank string, else ``""``.

    Whitespace must not count as a value — that is the whole point. The bug this
    exists for defaulted a venue name to a single space, and ``" "`` is TRUTHY, so
    every ``or``-style guard downstream accepted it as a real name and all 331
    rows in ``raw.venues`` shipped blank to the API.

    The blank-handling itself lives in :func:`to_str`, which strips and maps
    whitespace-only to ``None``; re-stripping here would just duplicate it.
    """
    for candidate in candidates:
        text = to_str(candidate)
        if text:
            return text
    return ""
