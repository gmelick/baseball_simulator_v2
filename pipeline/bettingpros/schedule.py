"""The MLB schedule facts the event match reads: the slates a game was played
on, its double-header number and its postponed start.
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

#: SIM-536: the largest gap allowed between the actual game's start time and the
#: BettingPros event we matched it to. A match beyond this is treated as no
#: match at all, rather than silently priced against the wrong game.
_MAX_EVENT_TIME_DELTA = timedelta(hours=2)

#: SIM-555: the largest gap the single-event rule accepts. The vendor sometimes
#: keeps a game's first start time after MLB moves it (game 745169: MLB moved
#: it to 18:15Z, the vendor's only event of the two teams that day kept
#: 00:15Z, six hours later). The rule takes such an event only when it is the
#: two teams' one event on the played date and the game is not a double-header.
_MAX_SINGLE_EVENT_DELTA = timedelta(hours=12)


class _ScheduleInfo(NamedTuple):
    """SIM-555: the MLB schedule facts the matcher reads beyond the meta 4-tuple.

    Every time is naive UTC, as :func:`_parse_utc` gives it.
    """

    #: The vendor slates to search: the played date first, then each date of
    #: a later entry that is not postponed (the resumption of a suspended game).
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
        """True for game 2 of a straight double-header with no listed start (SIM-555).

        MLB sets ``startTimeTBD`` on such a game and gives it a placeholder
        ``gameDate``: game 1's start plus 5 minutes (game 746773: 18:15Z
        beside game 1 at 18:10Z). Every straight double-header's game 2 of
        2019-2026 has this layout.
        """
        return self.double_header == "Y" and self.game_number == 2 and self.start_tbd


def _lone_schedule(date_str: str, game_dt: datetime | None) -> _ScheduleInfo:
    """The schedule facts of a game with one entry (SIM-555).

    The matcher uses it when the schedule cache holds nothing for a game (a
    subclass that resolves the meta another way).
    """
    anchors = () if game_dt is None else (game_dt,)
    return _ScheduleInfo((date_str,), anchors, None, None)


def _is_postponed(game: Mapping[str, Any]) -> bool:
    """True when an MLB schedule entry is a postponed game (SIM-555).

    The test reads two status fields. ``detailedState`` starts with
    "Postponed"; ``codedGameState`` "D" is MLB's postponed code (the ETL's
    ``_map_game_status`` reads the same code; the live payloads of 2024 carry
    both, with ``statusCode`` "DI" or "DR"). A suspended game is not
    postponed: MLB marks both of its entries "Final".
    """
    status = game.get("status") or {}
    detailed = str(status.get("detailedState") or "")
    return detailed.startswith("Postponed") or status.get("codedGameState") == "D"


def _read_schedule(
    data: Mapping[str, Any],
) -> tuple[tuple[str, str, str, datetime | None], _ScheduleInfo]:
    """The meta 4-tuple and the schedule facts of one ``schedule?gamePk=`` answer (SIM-555).

    MLB lists a game once per date it touched. A game postponed and made up
    later has two entries: the postponed one (its ``gameDate`` the original
    start) and the played one. A suspended game has two entries, both
    "Final": the first at the original first pitch, the second at the
    resumption. The PLAYED entry is the first entry that is not postponed;
    when every entry is postponed (a game never played), it is the first
    entry. The meta comes from the played entry, so a suspended game's start
    is its ORIGINAL first pitch (the load guard's stamp rules need it). The
    played entry's ``gameNumber`` and ``status.startTimeTBD`` tell a straight
    double-header's game 2, whose ``gameDate`` is a placeholder
    (:attr:`_ScheduleInfo.start_unknown`).
    Raises ``LookupError`` when the answer lists no entry, and ``KeyError``
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
