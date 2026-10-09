"""The league schedule client (SIM-519 Part A).

The day slate is schedule-driven (owner ruling 2026-08-29): the league's public
schedule says which games exist on a date, when they start and what state they
are in. This module is the one place that knows the schedule's shape.

* :func:`parse_schedule` turns one schedule response into :class:`ScheduleGame`
  records. It is pure, and the tests run it over recorded responses in
  ``tests/fixtures/mlb_schedule/``.
* :func:`card_state` maps a game to one of the four card states the frontend
  draws: ``scheduled``, ``live``, ``final`` or ``postponed``. It is the only
  status mapper; the API's raw-status fallback uses :func:`card_state_from_raw`.
* The app reads the endpoint through ``api.league_feed.LeagueFeed`` (async,
  cached by the route). :func:`fetch_schedule_sync` reads it over ``urllib``
  for the loader and the nightly jobs, which keep their ``urllib`` transport
  (the crash-class finding, SIM-446).

The schedule needs no key. A field the response does not carry reads ``None``,
and the card hides it.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

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

_POSTPONED_CODES = frozenset({"D", "C", "U", "T"})
_POSTPONED_PREFIXES = ("Postponed", "Cancelled", "Suspended")

#: The league's coarse states; anything else maps to scheduled and is logged once.
_ABSTRACT_TO_CARD = {"Preview": SCHEDULED, "Live": LIVE, "Final": FINAL}

#: The ``raw.games.status`` vocabulary the pipeline and the loader write. The
#: API maps a stored row through this table when the schedule feed is down.
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
    hits: int | None
    errors: int | None
    is_winner: bool | None
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
    rescheduled_to: date | None
    rescheduled_from: date | None
    resumed_from: date | None
    raw: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def lineups_posted(self) -> bool:
        """True when the schedule carries both sides' batting orders."""
        return bool(self.home.lineup) and bool(self.away.lineup)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _int(v: Any) -> int | None:
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
    pid = _int(entry.get("id"))
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
        wins=_int(rec.get("wins")),
        losses=_int(rec.get("losses")),
        score=_int(side.get("score")) if side.get("score") is not None else _int(line.get("runs")),
        hits=_int(line.get("hits")),
        errors=_int(line.get("errors")),
        is_winner=side.get("isWinner") if isinstance(side.get("isWinner"), bool) else None,
        probable_pitcher=_player(side.get("probablePitcher"), "P"),
        lineup=players,
    )


def parse_game(entry: Mapping[str, Any]) -> ScheduleGame:
    """One schedule entry → :class:`ScheduleGame`. Raises ``KeyError`` on a
    malformed entry (no ``gamePk`` or no ``teams``)."""
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
        venue_id=_int(venue.get("id")),
        venue_name=venue.get("name"),
        series_description=entry.get("seriesDescription"),
        inning=_int(linescore.get("currentInning")) if live else None,
        inning_half=(linescore.get("inningHalf") or linescore.get("inningState")) if live else None,
        outs=_int(linescore.get("outs")) if live else None,
        rescheduled_to=_date(entry.get("rescheduleGameDate") or entry.get("rescheduleDate")),
        rescheduled_from=_date(entry.get("rescheduledFrom")),
        resumed_from=_date(entry.get("resumedFrom")),
        raw=entry,
    )


def parse_schedule(payload: Mapping[str, Any]) -> list[ScheduleGame]:
    """Every game of a schedule response, in the response's order.

    A malformed entry is skipped and logged at warning; it never fails the
    whole day.
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
    """The card state of a stored ``raw.games.status`` value (the fallback path)."""
    if not raw_status:
        return SCHEDULED
    if raw_status.startswith(_POSTPONED_PREFIXES):
        return POSTPONED
    return _RAW_STATUS_TO_CARD.get(raw_status, SCHEDULED)


# ---------------------------------------------------------------------------
# Fetching
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


def fetch_schedule_sync(
    *,
    start: date,
    end: date,
    hydrate: str = DEFAULT_HYDRATE,
    timeout_s: float = 30.0,
) -> list[ScheduleGame]:
    """Read the schedule over ``urllib`` (the loader's and the nightly jobs' transport)."""
    query = "&".join(f"{k}={v}" for k, v in schedule_params(start, end, hydrate).items())
    with urllib.request.urlopen(f"{SCHEDULE_URL}?{query}", timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return parse_schedule(payload)
