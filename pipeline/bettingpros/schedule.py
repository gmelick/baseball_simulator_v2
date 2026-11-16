"""The MLB schedule facts the event match reads.

The facts are the dates a game was played on, its double-header number and
its postponed start. MLB lists a game once per date it touched, so one game
can have several schedule entries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import (
    datetime,
    timedelta,
)
from typing import (
    Any,
    NamedTuple,
)

from pipeline.bettingpros.parsing import _parse_utc

#: The largest gap between a game's start and its BettingPros event.
#: A larger gap is no match, so the game never takes another game's prices.
_MAX_EVENT_TIME_DELTA = timedelta(hours=2)

#: The largest gap the single-event rule accepts. The vendor sometimes keeps
#: a game's first start time after MLB moves it. For example, MLB moved game
#: 745169 to 18:15Z, and the vendor's event stayed at 00:15Z.
_MAX_SINGLE_EVENT_DELTA = timedelta(hours=12)


class _ScheduleInfo(NamedTuple):
    """The MLB schedule facts of one game, beyond the meta 4-tuple.

    The meta 4-tuple is (official date, home name, away name, start).
    Every time is naive UTC, as :func:`_parse_utc` gives it.
    """

    #: The vendor slates to search: the played date first, then the date of
    #: each later entry that is not postponed (a suspended game's resumption).
    slate_dates: tuple[str, ...]
    #: The played entry's start, then each later start that is not postponed.
    anchor_starts: tuple[datetime, ...]
    #: The played entry's ``doubleHeader`` code ('N', 'Y', 'S'), or ``None``.
    double_header: str | None
    #: The start of the last postponed entry before the played one, or ``None``.
    postponed_start: datetime | None
    #: The played entry's position among the game's schedule entries (0 = first).
    played_entry: int = 0
    #: The number of schedule entries the game has.
    n_entries: int = 1
    #: True when every entry is postponed: the game was never played.
    never_played: bool = False
    #: The played entry's ``gameNumber`` (1, or 2 on a double-header), or ``None``.
    game_number: int | None = None
    #: The played entry's ``status.startTimeTBD``: MLB lists no real start.
    start_tbd: bool = False

    @property
    def start_unknown(self) -> bool:
        """True for game 2 of a straight double-header with no listed start.

        MLB sets ``startTimeTBD`` on such a game. Its ``gameDate`` is a
        placeholder: game 1's start plus 5 minutes.
        """
        return self.double_header == "Y" and self.game_number == 2 and self.start_tbd


def _lone_schedule(date_str: str, game_dt: datetime | None) -> _ScheduleInfo:
    """The schedule facts of a game with one entry.

    The matcher uses them when the schedule cache holds nothing for the game.
    """
    anchors = () if game_dt is None else (game_dt,)
    return _ScheduleInfo((date_str,), anchors, None, None)


def _is_postponed(game: Mapping[str, Any]) -> bool:
    """True when an MLB schedule entry is a postponed game.

    The entry is postponed when ``detailedState`` starts with "Postponed" or
    ``codedGameState`` is "D". A suspended game is not postponed: MLB marks
    both of its entries "Final".
    """
    status = game.get("status") or {}
    detailed = str(status.get("detailedState") or "")
    return detailed.startswith("Postponed") or status.get("codedGameState") == "D"


def _read_schedule(
    data: Mapping[str, Any],
) -> tuple[tuple[str, str, str, datetime | None], _ScheduleInfo]:
    """The meta 4-tuple and the schedule facts of one ``schedule?gamePk=`` answer.

    A made-up game has a postponed entry at the original start, then a
    played entry. A suspended game has two "Final" entries: the first pitch
    and the resumption. The PLAYED entry is the first entry that is not
    postponed, or the first entry when every entry is postponed. The meta
    comes from the played entry, so a suspended game keeps its original
    first pitch.

    Raises ``LookupError`` when the answer lists no entry. Raises ``KeyError``
    when the played entry lacks a field the meta needs.
    """
    entries: list[tuple[Any, Mapping[str, Any]]] = [
        (day.get("date"), game)
        for day in data.get("dates") or []
        for game in day.get("games") or []
    ]
    if not entries:
        raise LookupError("the schedule lists no entry for the game")
    played_i = next((i for i, (_d, g) in enumerate(entries) if not _is_postponed(g)), 0)
    played = entries[played_i][1]
    date_str = str(played["officialDate"])
    home = str(played["teams"]["home"]["team"]["name"])
    away = str(played["teams"]["away"]["team"]["name"])
    game_dt = _parse_utc(str(played.get("gameDate", "")))
    slate_dates = [date_str]
    anchors = [] if game_dt is None else [game_dt]
    for day_date, game in entries[played_i + 1 :]:
        if _is_postponed(game):
            continue
        for value in (day_date, game.get("officialDate")):
            if value and str(value) not in slate_dates:
                slate_dates.append(str(value))
        resumed = _parse_utc(str(game.get("gameDate", "")))
        if resumed is not None and resumed not in anchors:
            anchors.append(resumed)
    postponed_start: datetime | None = None
    for _day_date, game in entries[:played_i]:
        parsed = _parse_utc(str(game.get("gameDate", "")))
        if parsed is not None:
            postponed_start = parsed
    double_header = played.get("doubleHeader")
    game_number = played.get("gameNumber")
    info = _ScheduleInfo(
        slate_dates=tuple(slate_dates),
        anchor_starts=tuple(anchors),
        double_header=None if double_header is None else str(double_header),
        postponed_start=postponed_start,
        played_entry=played_i,
        n_entries=len(entries),
        never_played=_is_postponed(played),
        game_number=game_number if isinstance(game_number, int) else None,
        start_tbd=(played.get("status") or {}).get("startTimeTBD") is True,
    )
    return (date_str, home, away, game_dt), info
