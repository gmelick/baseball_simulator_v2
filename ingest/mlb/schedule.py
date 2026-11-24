"""Parse the MLB schedule: which games exist on a date, when they start and their card state.

The day slate takes its games from this schedule, not from the database. This
module is the one place that knows the schedule's shape.

* :func:`parse_schedule` turns one schedule response into :class:`ScheduleGame`
  records. It is pure; the tests run it over the recorded responses in
  ``tests/fixtures/mlb_schedule/``.
* :func:`card_state` maps a game to one of the four card states the frontend
  draws: ``scheduled``, ``live``, ``final`` or ``postponed``. When the schedule
  read fails, the API maps the stored status with :func:`card_state_from_raw`.
* :func:`schedule_params` builds the query of one request. The reads go through
  ``ingest.mlb.client``.
* :func:`played_index` picks the played entry of a game that the schedule lists
  twice (a postponed game made up later).

The schedule needs no key. A field the response does not carry reads ``None``,
and the card hides it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

log = logging.getLogger(__name__)

#: Every game type the live pipeline follows (regular season and postseason).
GAME_TYPES: tuple[str, ...] = ("R", "F", "D", "L", "W", "C", "P")

#: The hydrate the slate needs: team names, the linescore of a live or final
#: game, the probable pitchers and the posted lineups.
DEFAULT_HYDRATE = "team,linescore,probablePitcher,lineups"

#: The four card states. The strings equal ``api.routes.games.GameStatus``.
SCHEDULED = "scheduled"
LIVE = "live"
FINAL = "final"
POSTPONED = "postponed"

#: The ``codedGameState`` values and the ``detailedState`` prefixes the card
#: shows as postponed.
_POSTPONED_CODES = frozenset({"D", "C", "U", "T"})
_POSTPONED_PREFIXES = ("Postponed", "Cancelled", "Suspended")

#: The league's coarse states; anything else maps to scheduled and is logged once.
_ABSTRACT_TO_CARD = {"Preview": SCHEDULED, "Live": LIVE, "Final": FINAL}

#: The ``raw.games.status`` values the pipeline and the loader write. The API
#: maps a stored row through this table when the schedule read fails.
_RAW_STATUS_TO_CARD = {
    "Preview": SCHEDULED,
    "Warmup": SCHEDULED,
    "Pre-Game": SCHEDULED,
    "Scheduled": SCHEDULED,
    "Live": LIVE,
    "In Progress": LIVE,
    "Final": FINAL,
    "Game Over": FINAL,
    "Completed Early": FINAL,
    "Postponed": POSTPONED,
    "Suspended": POSTPONED,
    "Cancelled": POSTPONED,
}

_unknown_states_logged: set[str] = set()


@dataclass(frozen=True)
class SchedulePlayer:
    """One player named by the schedule: a probable pitcher or a lineup slot."""

    player_id: int
    name: str
    position: str | None = None


@dataclass(frozen=True)
class ScheduleTeam:
    """One side of a scheduled game."""

    team_id: int
    name: str | None
    abbreviation: str | None
    wins: int | None
    losses: int | None
    score: int | None
    probable_pitcher: SchedulePlayer | None
    lineup: tuple[SchedulePlayer, ...] = ()


@dataclass(frozen=True)
class ScheduleGame:
    """One game of the schedule, as the slate needs it."""

    game_pk: int
    season: int
    official_date: date
    start_utc: datetime | None
    start_time_tbd: bool
    game_type: str
    abstract_state: str
    detailed_state: str
    coded_state: str
    reason: str | None
    double_header: str
    game_number: int
    home: ScheduleTeam
    away: ScheduleTeam
    venue_id: int | None
    venue_name: str | None
    series_description: str | None
    inning: int | None
    inning_half: str | None
    outs: int | None
    n_innings: int | None
    rescheduled_to: date | None
    rescheduled_from: date | None

    @property
    def lineups_posted(self) -> bool:
        """True when the schedule carries both sides' batting orders."""
        return bool(self.home.lineup) and bool(self.away.lineup)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def int_or_none(v: Any) -> int | None:
    """``v`` as an int; None when it is missing, blank or not a number."""
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


def _date(v: Any) -> date | None:
    """The calendar date of an ISO date or date-time string, else None."""
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _datetime(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _player(entry: Any, position: str | None = None) -> SchedulePlayer | None:
    if not isinstance(entry, Mapping):
        return None
    pid = int_or_none(entry.get("id"))
    if pid is None:
        return None
    if position is None:
        prim = entry.get("primaryPosition")
        if isinstance(prim, Mapping):
            position = prim.get("abbreviation")
    return SchedulePlayer(player_id=pid, name=str(entry.get("fullName") or ""), position=position)


def _team(side: Mapping[str, Any], line: Mapping[str, Any], lineup: Sequence[Any]) -> ScheduleTeam:
    team = side.get("team") or {}
    rec = side.get("leagueRecord") or {}
    players = tuple(p for p in (_player(x) for x in lineup) if p is not None)
    return ScheduleTeam(
        team_id=int(team.get("id") or 0),
        name=team.get("teamName") or team.get("clubName") or team.get("name"),
        abbreviation=team.get("abbreviation"),
        wins=int_or_none(rec.get("wins")),
        losses=int_or_none(rec.get("losses")),
        score=int_or_none(side.get("score"))
        if side.get("score") is not None
        else int_or_none(line.get("runs")),
        probable_pitcher=_player(side.get("probablePitcher"), "P"),
        lineup=players,
    )


def parse_game(entry: Mapping[str, Any]) -> ScheduleGame:
    """Parse one schedule entry into a :class:`ScheduleGame`.

    Raises ``KeyError`` when the entry has no ``gamePk``, no ``teams`` or no date.
    """
    status = entry.get("status") or {}
    teams = entry["teams"]
    linescore = entry.get("linescore") or {}
    line_teams = linescore.get("teams") or {}
    lineups = entry.get("lineups") or {}
    venue = entry.get("venue") or {}
    official = _date(entry.get("officialDate")) or _date(entry.get("gameDate"))
    if official is None:
        raise KeyError("officialDate")
    tbd = bool(status.get("startTimeTBD"))
    abstract = str(status.get("abstractGameState") or "")
    live = abstract == "Live"
    return ScheduleGame(
        game_pk=int(entry["gamePk"]),
        season=int(entry.get("season") or official.year),
        official_date=official,
        start_utc=None if tbd else _datetime(entry.get("gameDate")),
        start_time_tbd=tbd,
        game_type=str(entry.get("gameType") or ""),
        abstract_state=abstract,
        detailed_state=str(status.get("detailedState") or ""),
        coded_state=str(status.get("codedGameState") or ""),
        reason=status.get("reason"),
        double_header=str(entry.get("doubleHeader") or "N"),
        game_number=int(entry.get("gameNumber") or 1),
        home=_team(teams["home"], line_teams.get("home") or {}, lineups.get("homePlayers") or ()),
        away=_team(teams["away"], line_teams.get("away") or {}, lineups.get("awayPlayers") or ()),
        venue_id=int_or_none(venue.get("id")),
        venue_name=venue.get("name"),
        series_description=entry.get("seriesDescription"),
        inning=int_or_none(linescore.get("currentInning")) if live else None,
        inning_half=(linescore.get("inningHalf") or linescore.get("inningState")) if live else None,
        outs=int_or_none(linescore.get("outs")) if live else None,
        # The innings played so far ("Final/10" on an extra-inning card).
        n_innings=len(linescore.get("innings") or []) or None,
        rescheduled_to=_date(entry.get("rescheduleGameDate") or entry.get("rescheduleDate")),
        rescheduled_from=_date(entry.get("rescheduledFrom")),
    )


def parse_schedule(payload: Mapping[str, Any]) -> list[ScheduleGame]:
    """Every game of a schedule response, in the response's order.

    The parser logs a malformed entry at warning and skips it, so one bad
    entry never fails the whole day.
    """
    games: list[ScheduleGame] = []
    for day in payload.get("dates") or ():
        for entry in day.get("games") or ():
            try:
                games.append(parse_game(entry))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("schedule entry skipped (%s): gamePk=%s", exc, entry.get("gamePk"))
    return games


# ---------------------------------------------------------------------------
# The status mapper
# ---------------------------------------------------------------------------


def card_state_of(abstract_state: str, coded_state: str, detailed_state: str) -> str:
    """The card state of the league's three status fields.

    The coded state is read first, because the league marks a postponed game
    ``abstractGameState = "Final"``. The schedule and the per-game feed carry
    the same three fields, so both read their state here.
    """
    if coded_state in _POSTPONED_CODES or detailed_state.startswith(_POSTPONED_PREFIXES):
        return POSTPONED
    state = _ABSTRACT_TO_CARD.get(abstract_state)
    if state is None:
        key = f"{abstract_state}/{coded_state}/{detailed_state}"
        if key not in _unknown_states_logged:
            _unknown_states_logged.add(key)
            log.warning("unknown league state %s; the card shows it as scheduled", key)
        return SCHEDULED
    return state


def card_state(game: ScheduleGame) -> str:
    """The card state of a schedule game: scheduled, live, final or postponed."""
    return card_state_of(game.abstract_state, game.coded_state, game.detailed_state)


def card_state_from_raw(raw_status: str | None) -> str:
    """The card state of a stored ``raw.games.status`` value, for a failed schedule read."""
    if not raw_status:
        return SCHEDULED
    if raw_status.startswith(_POSTPONED_PREFIXES):
        return POSTPONED
    return _RAW_STATUS_TO_CARD.get(raw_status, SCHEDULED)


# ---------------------------------------------------------------------------
# The request and the played entry
# ---------------------------------------------------------------------------


def schedule_params(start: date, end: date, hydrate: str = DEFAULT_HYDRATE) -> dict[str, str]:
    """The query parameters of one schedule request."""
    return {
        "sportId": "1",
        "gameTypes": ",".join(GAME_TYPES),
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "hydrate": hydrate,
    }


def is_postponed(entry: Mapping[str, Any]) -> bool:
    """True when a schedule entry is a postponed game.

    The entry is postponed when ``detailedState`` starts with "Postponed" or
    ``codedGameState`` is "D". A suspended game is not postponed: MLB marks
    both of its entries "Final".
    """
    status = entry.get("status") or {}
    detailed = str(status.get("detailedState") or "")
    return detailed.startswith("Postponed") or status.get("codedGameState") == "D"


def schedule_entries(data: Mapping[str, Any]) -> list[tuple[Any, Mapping[str, Any]]]:
    """Every ``(date, game)`` entry of one schedule answer, in order."""
    return [
        (day.get("date"), game)
        for day in data.get("dates") or []
        for game in day.get("games") or []
    ]


def played_index(entries: Sequence[tuple[Any, Mapping[str, Any]]]) -> int:
    """The index of the PLAYED entry of one game's schedule entries.

    A made-up game lists a postponed entry at the original start, then the
    played entry. The played entry is the first that is not postponed, or the
    first entry when every entry is postponed.
    """
    return next((i for i, (_d, g) in enumerate(entries) if not is_postponed(g)), 0)
