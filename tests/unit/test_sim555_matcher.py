"""SIM-555 (2026-10-01) — the matcher reads the PLAYED schedule entry of a game.

The defect, measured against the live MLB schedule and the vendor: MLB lists a game
once per date it touched. A game postponed and made up later has its postponed
entry first (``gameDate`` = the ORIGINAL start) and the played entry second. The
provider read ``dates[0].games[0]``, so it compared the vendor's events with the
original start, and the two-hour limit declined the right event. A suspended game has
two "Final" entries, and the vendor may move it to its resumption date's slate. A
game MLB moved can keep its first start time at the vendor. The loader's logs show a
same-team event declined on about 350 games of 2019-2026.

These tests pin the fix:

  * the played entry is the first entry not postponed (``detailedState`` starting
    "Postponed", or ``codedGameState`` "D"); every entry postponed → the first one;
  * a made-up game matches the event at its played start (745175, 746572);
  * game 2 of a straight double-header has no listed start (``startTimeTBD``; its
    ``gameDate`` is game 1's start plus 5 minutes): it takes the LATER of exactly two
    same-team events, never game 1's (746773, a made-up game 2; 745310, a regular
    one), and its rows measure their stamps from that event's start;
  * a suspended game matches the event on its resumption date's slate at the
    resumption time, and its rows keep the ORIGINAL first pitch as the start (746755);
  * the single-event rule: a re-timed game matches its two teams' only event of the
    day up to 12 hours away, with a WARNING (745169); never on a double-header, with
    two same-team events, or over 12 hours;
  * ``read_failures`` counts a failed extra-slate read, not an answer without the game;
  * every row of a made-up game carries ``postponed_start``; the load guard refuses a
    row stamped at or before it (opening and closing, game and prop rows) and keeps a
    row stamped after it;
  * ``forget_game`` drops a game's cached facts, so a long-lived provider that looked
    a game up before its postponement reads the new schedule.

The schedule payloads copy the shape of the real MLB answers for those games (read
2026-10-01). No network: the ``_mlb_get`` / ``_bp_get`` seams are stubbed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pipeline.bettingpros_odds_provider import (
    _MAX_EVENT_TIME_DELTA,
    _MAX_SINGLE_EVENT_DELTA,
    BettingProsOddsProvider,
    _is_postponed,
    _read_schedule,
)
from pipeline.odds_provider import GAME_MARKET_TYPES
from pipeline.odds_row_guard import RULES, RefusalTally, check_row, refuse_reason

_LOGGER = "pipeline.bettingpros_odds_provider"

_TEAMS = {
    "STL": ("St. Louis Cardinals", "Cardinals"),
    "CHC": ("Chicago Cubs", "Cubs"),
    "COL": ("Colorado Rockies", "Rockies"),
    "SEA": ("Seattle Mariners", "Mariners"),
    "CWS": ("Chicago White Sox", "White Sox"),
    "MIN": ("Minnesota Twins", "Twins"),
    "TEX": ("Texas Rangers", "Rangers"),
    "NYY": ("New York Yankees", "Yankees"),
    "BOS": ("Boston Red Sox", "Red Sox"),
}

_FINAL = {
    "abstractGameState": "Final",
    "codedGameState": "F",
    "detailedState": "Final",
    "statusCode": "F",
    "startTimeTBD": False,
    "abstractGameCode": "F",
}
_POSTPONED = {
    "abstractGameState": "Final",
    "codedGameState": "D",
    "detailedState": "Postponed",
    "statusCode": "DI",
    "startTimeTBD": False,
    "reason": "Inclement Weather",
    "abstractGameCode": "F",
}
_SCHEDULED = {
    "abstractGameState": "Preview",
    "codedGameState": "S",
    "detailedState": "Scheduled",
    "statusCode": "S",
    "startTimeTBD": False,
    "abstractGameCode": "P",
}


def _entry(
    game_pk: int,
    home: str,
    away: str,
    *,
    game_date: str,
    official: str,
    status: dict[str, Any] | None = None,
    double_header: str = "N",
    game_number: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """One MLB schedule entry, shaped like the live ``schedule?gamePk=`` answer."""
    game: dict[str, Any] = {
        "gamePk": game_pk,
        "gameType": "R",
        "season": game_date[:4],
        "gameDate": game_date,
        "officialDate": official,
        "status": dict(status or _FINAL),
        "teams": {
            "away": {"team": {"id": 1, "name": _TEAMS[away][0]}},
            "home": {"team": {"id": 2, "name": _TEAMS[home][0]}},
        },
        "doubleHeader": double_header,
        "gameNumber": game_number,
    }
    game.update(extra)
    return game


def _schedule(*dated: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """A schedule answer: one ``dates[]`` item per (date, entry), in order."""
    return {
        "totalGames": len(dated),
        "dates": [{"date": day, "games": [game]} for day, game in dated],
    }


def _event(event_id: int, home: str, away: str, scheduled: str) -> dict[str, Any]:
    """One BettingPros event (``scheduled`` is naive UTC text, as the vendor sends it)."""
    return {
        "id": event_id,
        "home": home,
        "visitor": away,
        "scheduled": scheduled,
        "participants": [
            {"id": home, "name": _TEAMS[home][1]},
            {"id": away, "name": _TEAMS[away][1]},
        ],
    }


def _slate(*events: dict[str, Any]) -> dict[str, Any]:
    """A vendor ``/events`` answer, with an unrelated game on it."""
    return {"events": [_event(1, "NYY", "BOS", "2024-01-01 23:05:00"), *events]}


class _Stub(BettingProsOddsProvider):
    """The provider with the MLB and vendor reads served from dicts.

    ``fail_dates`` names the vendor slates whose read times out.
    """

    def __init__(
        self,
        schedule: dict[str, Any],
        events_by_date: dict[str, dict[str, Any]],
        *,
        offers: dict[int, dict[str, Any]] | None = None,
        fail_dates: tuple[str, ...] = (),
        **kw: Any,
    ) -> None:
        super().__init__(api_key="test-key", **kw)
        self._schedule = schedule
        self._events_by_date = events_by_date
        self._offers_by_market = offers or {}
        self.fail_dates = set(fail_dates)
        self.queried_dates: list[str] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return self._schedule
        if path.startswith("people/"):
            return {"people": [{"fullName": "Sonny Gray"}]}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            self.queried_dates.append(params["date"])
            if params["date"] in self.fail_dates:
                raise TimeoutError("timed out")
            return self._events_by_date.get(params["date"], {"events": []})
        if path == "offers":
            return self._offers_by_market.get(int(params["market_id"]), {"offers": []})
        raise AssertionError(f"unexpected BP path {path}")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --------------------------------------------------------------------------- the five real games

#: 745175, CHC at STL: postponed 2024-05-24, made up 2024-07-13 (split double-header, game 2).
_745175 = _schedule(
    (
        "2024-05-24",
        _entry(
            745175,
            "STL",
            "CHC",
            game_date="2024-05-25T00:15:00Z",
            official="2024-07-13",
            status=_POSTPONED,
            rescheduleDate="2024-07-14T00:15:00Z",
            rescheduleGameDate="2024-07-13",
        ),
    ),
    (
        "2024-07-13",
        _entry(
            745175,
            "STL",
            "CHC",
            game_date="2024-07-14T00:15:00Z",
            official="2024-07-13",
            double_header="S",
            game_number=2,
            description="Makeup of 5/24 PPD",
            rescheduledFrom="2024-05-25T00:15:00Z",
            rescheduledFromDate="2024-05-24",
        ),
    ),
)
_745175_EVENTS = {
    # The original date's event: the matcher must never read it.
    "2024-05-24": _slate(_event(90001, "STL", "CHC", "2024-05-25 00:15:00")),
    "2024-07-13": _slate(
        _event(93535, "STL", "CHC", "2024-07-13 18:15:00"),  # game 1 of the double-header
        _event(93536, "STL", "CHC", "2024-07-14 00:15:00"),  # the made-up game
    ),
}

#: 746572, SEA at COL: postponed 2024-04-19, made up 2024-04-21 (split double-header, game 2).
_746572 = _schedule(
    (
        "2024-04-19",
        _entry(
            746572,
            "COL",
            "SEA",
            game_date="2024-04-20T00:40:00Z",
            official="2024-04-21",
            status=_POSTPONED,
        ),
    ),
    (
        "2024-04-21",
        _entry(
            746572,
            "COL",
            "SEA",
            game_date="2024-04-22T00:10:00Z",
            official="2024-04-21",
            double_header="S",
            game_number=2,
            description="Makeup of 4/19 PPD",
        ),
    ),
)
_746572_EVENTS = {
    "2024-04-21": _slate(
        _event(94440, "COL", "SEA", "2024-04-21 19:10:00"),
        _event(94441, "COL", "SEA", "2024-04-22 00:10:00"),
    ),
}

#: 746773, MIN at CWS: postponed 2024-07-09 (rain), made up 2024-07-10 as game 2 of a
#: straight double-header. MLB lists no start for it: ``startTimeTBD`` is set and its
#: ``gameDate`` is a placeholder, game 1's start (746772, 18:10Z) plus 5 minutes.
_746773 = _schedule(
    (
        "2024-07-09",
        _entry(
            746773,
            "CWS",
            "MIN",
            game_date="2024-07-10T00:10:00Z",
            official="2024-07-10",
            status={**_POSTPONED, "statusCode": "DR", "reason": "Rain"},
        ),
    ),
    (
        "2024-07-10",
        _entry(
            746773,
            "CWS",
            "MIN",
            game_date="2024-07-10T18:15:00Z",
            official="2024-07-10",
            status={**_FINAL, "startTimeTBD": True},
            double_header="Y",
            game_number=2,
            description="Makeup of 7/9 PPD",
        ),
    ),
)
#: 746772, game 1 of the same double-header: a real start, 18:10Z.
_746772 = _schedule(
    (
        "2024-07-10",
        _entry(
            746772,
            "CWS",
            "MIN",
            game_date="2024-07-10T18:10:00Z",
            official="2024-07-10",
            double_header="Y",
            game_number=1,
        ),
    ),
)
_746773_EVENTS = {
    "2024-07-10": _slate(
        _event(93328, "CWS", "MIN", "2024-07-10 18:10:00"),  # game 1 (5 min before the placeholder)
        _event(93329, "CWS", "MIN", "2024-07-10 21:25:00"),  # game 2, about three hours later
    ),
}

#: 745169, COL at STL, 2024-06-07: one entry; MLB moved it to 18:15Z, the vendor kept 7:15 pm.
_745169 = _schedule(
    (
        "2024-06-07",
        _entry(745169, "STL", "COL", game_date="2024-06-07T18:15:00Z", official="2024-06-07"),
    ),
)
_745169_EVENTS = {"2024-06-07": _slate(_event(92912, "STL", "COL", "2024-06-08 00:15:00"))}

#: 746755, TEX at CWS: suspended 2024-08-27, resumed 2024-08-28 at 21:10Z. Both entries Final.
_746755 = _schedule(
    (
        "2024-08-27",
        _entry(
            746755,
            "CWS",
            "TEX",
            game_date="2024-08-28T00:10:00Z",
            official="2024-08-27",
            resumeDate="2024-08-28T21:10:00Z",
            resumeGameDate="2024-08-28",
        ),
    ),
    (
        "2024-08-28",
        _entry(
            746755,
            "CWS",
            "TEX",
            game_date="2024-08-28T21:10:00Z",
            official="2024-08-27",
            resumedFrom="2024-08-28T00:10:00Z",
            resumedFromDate="2024-08-27",
        ),
    ),
)
_746755_EVENTS = {
    # The vendor has no event of the two teams on the 08-27 slate.
    "2024-08-27": _slate(),
    "2024-08-28": _slate(
        _event(94821, "CWS", "TEX", "2024-08-28 21:10:00"),  # the resumption
        _event(94822, "CWS", "TEX", "2024-08-29 00:10:00"),  # 08-28's own game
    ),
}


# --------------------------------------------------------------------------- the played entry


class TestThePlayedEntry:
    def test_postponed_reads_the_two_status_fields(self) -> None:
        assert _is_postponed({"status": _POSTPONED})
        assert _is_postponed({"status": {"detailedState": "Postponed: Rain"}})
        assert _is_postponed({"status": {"codedGameState": "D", "detailedState": "x"}})
        assert not _is_postponed({"status": _FINAL})
        suspended = {"codedGameState": "T", "detailedState": "Suspended: Rain"}
        assert not _is_postponed({"status": suspended})
        assert not _is_postponed({})  # a stub without a status

    def test_a_made_up_game_reads_its_played_entry(self) -> None:
        meta, info = _read_schedule(_745175)
        assert meta == (
            "2024-07-13",
            "St. Louis Cardinals",
            "Chicago Cubs",
            datetime(2024, 7, 14, 0, 15),
        )
        assert info.slate_dates == ("2024-07-13",)
        assert info.anchor_starts == (datetime(2024, 7, 14, 0, 15),)
        assert info.double_header == "S"
        assert info.postponed_start == datetime(2024, 5, 25, 0, 15)
        assert (info.played_entry, info.n_entries, info.never_played) == (1, 2, False)

    def test_a_suspended_game_keeps_its_original_first_pitch(self) -> None:
        meta, info = _read_schedule(_746755)
        assert meta == (
            "2024-08-27",
            "Chicago White Sox",
            "Texas Rangers",
            datetime(2024, 8, 28, 0, 10),
        )
        assert info.slate_dates == ("2024-08-27", "2024-08-28")
        assert info.anchor_starts == (datetime(2024, 8, 28, 0, 10), datetime(2024, 8, 28, 21, 10))
        assert info.double_header == "N"
        assert info.postponed_start is None
        assert info.played_entry == 0

    def test_a_game_never_played_falls_back_to_its_first_entry(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        schedule = _schedule(
            (
                "2024-09-01",
                _entry(
                    700001,
                    "STL",
                    "CHC",
                    game_date="2024-09-01T18:15:00Z",
                    official="2024-09-02",
                    status=_POSTPONED,
                ),
            ),
            (
                "2024-09-02",
                _entry(
                    700001,
                    "STL",
                    "CHC",
                    game_date="2024-09-02T18:15:00Z",
                    official="2024-09-02",
                    status=_POSTPONED,
                ),
            ),
        )
        meta, info = _read_schedule(schedule)
        assert meta == (
            "2024-09-02",
            "St. Louis Cardinals",
            "Chicago Cubs",
            datetime(2024, 9, 1, 18, 15),
        )
        assert info.played_entry == 0 and info.never_played is True
        assert info.postponed_start is None
        assert info.slate_dates == ("2024-09-02",)
        # Today's behaviour: the matcher measures the vendor's events from the first entry.
        provider = _Stub(
            schedule, {"2024-09-02": _slate(_event(7, "STL", "CHC", "2024-09-01 18:15:00"))}
        )
        event = provider._resolve_event(700001)
        assert event is not None and event["id"] == 7
        assert "every schedule entry of game_pk 700001 is postponed" in caplog.text

    def test_a_game_with_one_entry_reads_as_before(self) -> None:
        meta, info = _read_schedule(_745169)
        assert meta == (
            "2024-06-07",
            "St. Louis Cardinals",
            "Colorado Rockies",
            datetime(2024, 6, 7, 18, 15),
        )
        assert info.slate_dates == ("2024-06-07",)
        assert (info.double_header, info.postponed_start, info.played_entry) == ("N", None, 0)

    def test_an_answer_without_the_game_raises(self) -> None:
        with pytest.raises(LookupError):
            _read_schedule({"dates": []})
        with pytest.raises(LookupError):
            _read_schedule({"dates": [{"date": "2024-06-07", "games": []}]})

    def test_a_game_postponed_twice_keeps_the_later_postponed_start(self) -> None:
        """A price stamped between the two postponed starts is a price of an unplayed game too."""
        schedule = _schedule(
            (
                "2024-05-01",
                _entry(
                    700002,
                    "STL",
                    "CHC",
                    game_date="2024-05-01T23:15:00Z",
                    official="2024-05-03",
                    status=_POSTPONED,
                ),
            ),
            (
                "2024-05-02",
                _entry(
                    700002,
                    "STL",
                    "CHC",
                    game_date="2024-05-02T23:15:00Z",
                    official="2024-05-03",
                    status=_POSTPONED,
                ),
            ),
            (
                "2024-05-03",
                _entry(
                    700002,
                    "STL",
                    "CHC",
                    game_date="2024-05-03T23:15:00Z",
                    official="2024-05-03",
                ),
            ),
        )
        meta, info = _read_schedule(schedule)
        assert meta[0] == "2024-05-03" and meta[3] == datetime(2024, 5, 3, 23, 15)
        assert info.postponed_start == datetime(2024, 5, 2, 23, 15)
        assert (info.played_entry, info.n_entries) == (2, 3)
        assert info.slate_dates == ("2024-05-03",)
        assert info.anchor_starts == (datetime(2024, 5, 3, 23, 15),)

    def test_a_postponed_entry_after_the_played_one_is_not_searched(self) -> None:
        """A suspended game whose first resumption date was postponed: the postponed
        resumption adds no slate and no start; the real resumption does."""
        schedule = _schedule(
            (
                "2024-08-27",
                _entry(
                    746755,
                    "CWS",
                    "TEX",
                    game_date="2024-08-28T00:10:00Z",
                    official="2024-08-27",
                ),
            ),
            (
                "2024-08-28",
                _entry(
                    746755,
                    "CWS",
                    "TEX",
                    game_date="2024-08-28T21:10:00Z",
                    official="2024-08-29",
                    status=_POSTPONED,
                ),
            ),
            (
                "2024-08-30",
                _entry(
                    746755,
                    "CWS",
                    "TEX",
                    game_date="2024-08-30T18:10:00Z",
                    official="2024-08-27",
                ),
            ),
        )
        _meta, info = _read_schedule(schedule)
        assert info.slate_dates == ("2024-08-27", "2024-08-30")
        assert info.anchor_starts == (datetime(2024, 8, 28, 0, 10), datetime(2024, 8, 30, 18, 10))
        assert info.postponed_start is None
        assert info.played_entry == 0

    @pytest.mark.parametrize(
        ("double_header", "game_number", "tbd", "unknown"),
        [
            ("Y", 2, True, True),  # game 2 of a straight double-header: a placeholder start
            ("Y", 1, False, False),  # game 1 has a real start
            ("Y", 2, False, False),  # a listed start is a real start
            ("S", 2, True, False),  # a split double-header's games have their own starts
            ("N", 1, True, False),
        ],
    )
    def test_the_unknown_start_of_game_two(self, double_header, game_number, tbd, unknown) -> None:
        schedule = _schedule(
            (
                "2024-07-10",
                _entry(
                    746773,
                    "CWS",
                    "MIN",
                    game_date="2024-07-10T18:15:00Z",
                    official="2024-07-10",
                    status={**_FINAL, "startTimeTBD": tbd},
                    double_header=double_header,
                    game_number=game_number,
                ),
            ),
        )
        _meta, info = _read_schedule(schedule)
        assert (info.game_number, info.start_tbd) == (game_number, tbd)
        assert info.start_unknown is unknown


# --------------------------------------------------------------------------- the match


class TestMadeUpGames:
    @pytest.mark.parametrize(
        ("game_pk", "schedule", "events", "slate", "expected"),
        [
            (745175, _745175, _745175_EVENTS, "2024-07-13", 93536),
            (746572, _746572, _746572_EVENTS, "2024-04-21", 94441),
        ],
    )
    def test_the_event_at_the_played_start_matches(
        self, caplog, game_pk, schedule, events, slate, expected
    ) -> None:
        """The provider used to measure these events from the ORIGINAL start (weeks or
        hours away) and declined every one: the games got no odds."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        provider = _Stub(schedule, events)
        event = provider._resolve_event(game_pk)
        assert event is not None and event["id"] == expected
        assert provider.queried_dates == [slate]  # never the postponed date's slate
        assert "sanity limit" not in caplog.text  # the old decline is gone
        assert "single-event rule" not in caplog.text
        info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            f"game_pk {game_pk} matched event {expected}" in m and "postponed from" in m
            for m in info_lines
        )
        assert provider.read_failures == 0

    def test_the_postponed_entry_alone_declines(self, caplog) -> None:
        """The old read: matched from the postponed entry, the made-up game's event sits
        seven weeks from the start, and the matcher declines it."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        postponed_only = {"dates": _745175["dates"][:1]}
        provider = _Stub(postponed_only, _745175_EVENTS)
        assert provider._resolve_event(745175) is None
        assert provider.queried_dates == ["2024-07-13"]  # the entry's officialDate
        assert "for game_pk 745175 is 49 days, 18:00:00 from the real start time" in caplog.text
        assert "sanity limit" in caplog.text
        assert "single-event rule" not in caplog.text
        gap = datetime(2024, 7, 14, 0, 15) - datetime(2024, 5, 25, 0, 15)
        assert gap > _MAX_SINGLE_EVENT_DELTA > _MAX_EVENT_TIME_DELTA


class TestStraightDoubleHeaderGameTwo:
    """MLB lists no start for game 2 of a straight double-header. Its placeholder,
    game 1's start plus 5 minutes, sits nearer game 1's event than its own."""

    def test_the_made_up_game_two_takes_the_later_event(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        provider = _Stub(_746773, _746773_EVENTS)
        event = provider._resolve_event(746773)
        assert event is not None and event["id"] == 93329
        assert provider.queried_dates == ["2024-07-10"]
        assert "sanity limit" not in caplog.text
        assert "single-event rule" not in caplog.text
        info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "game_pk 746773 matched event 93329" in m
            and "the later of the two events" in m
            and "postponed from 2024-07-10 00:10:00" in m
            for m in info_lines
        )
        assert provider.read_failures == 0

    def test_game_one_and_game_two_get_different_events(self) -> None:
        game_one = _Stub(_746772, _746773_EVENTS)._resolve_event(746772)
        game_two = _Stub(_746773, _746773_EVENTS)._resolve_event(746773)
        assert game_one is not None and game_two is not None
        assert (game_one["id"], game_two["id"]) == (93328, 93329)

    def test_a_regular_game_two_takes_the_later_event(self) -> None:
        """Not only a made-up game: every straight double-header's game 2 has the
        placeholder (745310, 2024: game 1 at 23:05Z, game 2's placeholder 23:10Z)."""
        schedule = _schedule(
            (
                "2024-07-27",
                _entry(
                    745310,
                    "NYY",
                    "BOS",
                    game_date="2024-07-27T23:10:00Z",
                    official="2024-07-27",
                    status={**_FINAL, "startTimeTBD": True},
                    double_header="Y",
                    game_number=2,
                ),
            ),
        )
        events = {
            "2024-07-27": {
                "events": [
                    _event(95002, "NYY", "BOS", "2024-07-28 02:20:00"),  # listed first
                    _event(95001, "NYY", "BOS", "2024-07-27 23:05:00"),
                ]
            }
        }
        event = _Stub(schedule, events)._resolve_event(745310)
        assert event is not None and event["id"] == 95002

    @pytest.mark.parametrize(
        "scheduled",
        [
            ("2024-07-10 18:10:00",),  # only game 1's event: never take it
            ("2024-07-10 18:10:00", "2024-07-10 21:25:00", "2024-07-10 23:59:00"),
            ("2024-07-10 18:10:00", "2024-07-10 18:10:00"),  # two events at one time
            ("2024-07-10 18:10:00", "not a time"),
        ],
    )
    def test_any_other_slate_declines(self, caplog, scheduled) -> None:
        events = {
            "2024-07-10": _slate(
                *(_event(93328 + i, "CWS", "MIN", when) for i, when in enumerate(scheduled))
            )
        }
        provider = _Stub(_746773, events)
        assert provider._resolve_event(746773) is None
        assert "game 2 of a straight double-header with no listed start" in caplog.text
        assert "rather than take game 1's event" in caplog.text
        assert provider.read_failures == 0

    def test_its_rows_measure_the_stamps_from_the_vendor_start(self) -> None:
        offers = {
            122: _ml_offers(
                "CWS",
                "MIN",
                {
                    10: (136, -162, "2024-07-10 21:20:00"),  # before game 2's start: kept
                    12: (130, -155, "2024-07-10 21:45:00"),  # 20 minutes after it: late
                },
            )
        }
        provider = _Stub(_746773, _746773_EVENTS, offers=offers)
        rows = {
            r["book"]: r
            for r in provider.get_odds_by_book(746773, line_type="closing", market_type="moneyline")
        }
        assert set(rows) == {"bp:10", "bp:12"}
        for row in rows.values():
            assert row["scheduled_start"] == _utc("2024-07-10 21:25:00")  # not the placeholder
            assert row["postponed_start"] == _utc("2024-07-10 00:10:00")
        assert check_row(rows["bp:10"]) is None
        late = check_row(rows["bp:12"])
        assert late is not None and late.rule == "late_closing_stamp"
        props = provider.get_prop_odds(746773, 1, "strikeouts")
        assert props["scheduled_start"] == _utc("2024-07-10 21:25:00")

    def test_game_one_keeps_its_listed_start(self) -> None:
        offers = {122: _ml_offers("CWS", "MIN", {10: (-110, -110, "2024-07-10 18:00:00")})}
        provider = _Stub(_746772, _746773_EVENTS, offers=offers)
        (row,) = provider.get_odds_by_book(746772, line_type="closing", market_type="moneyline")
        assert row["scheduled_start"] == _utc("2024-07-10 18:10:00")


class TestSuspendedGame:
    def test_the_resumption_slate_matches_at_the_resumption_time(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        provider = _Stub(_746755, _746755_EVENTS)
        event = provider._resolve_event(746755)
        assert event is not None and event["id"] == 94821
        assert provider.queried_dates == ["2024-08-27", "2024-08-28"]
        assert provider.read_failures == 0
        info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "game_pk 746755 matched event 94821" in m
            and "the 2024-08-28 slate" in m
            and "resumption at 2024-08-28 21:10:00" in m
            for m in info_lines
        )

    def test_its_rows_carry_the_original_first_pitch(self) -> None:
        offers = {122: _ml_offers("CWS", "TEX", {10: (-120, 100, "2024-08-27 23:58:00")})}
        provider = _Stub(_746755, _746755_EVENTS, offers=offers)
        rows = provider.get_odds_by_book(746755, line_type="closing", market_type="moneyline")
        assert [r["book"] for r in rows] == ["bp:10"]
        row = rows[0]
        assert row["scheduled_start"] == _utc("2024-08-28 00:10:00")  # not the resumption
        assert row["game_date"] == "2024-08-27"
        assert row["postponed_start"] is None
        # A close stamped before the original first pitch is kept: the game had no postponement.
        assert check_row(row) is None

    def test_an_event_listed_on_both_slates_counts_once(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        same = _event(94821, "CWS", "TEX", "2024-08-28 00:10:00")
        provider = _Stub(_746755, {"2024-08-27": _slate(same), "2024-08-28": _slate(same)})
        event = provider._resolve_event(746755)
        assert event is not None and event["id"] == 94821
        assert "double-header" not in caplog.text
        assert "matched event 94821" not in caplog.text  # the played slate, the played start


class TestTheSingleEventRule:
    def test_a_re_timed_game_matches_its_only_event_with_a_warning(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER)
        provider = _Stub(_745169, _745169_EVENTS)
        event = provider._resolve_event(745169)
        assert event is not None and event["id"] == 92912
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0]
        assert "single-event rule" in message
        assert "event 92912" in message and "scheduled=2024-06-08 00:15:00" in message
        assert "6:00:00 from the real start 2024-06-07 18:15:00" in message
        assert "kept another start time" in message
        assert provider.read_failures == 0

    @pytest.mark.parametrize("double_header", ["Y", "S"])
    def test_it_never_fires_on_a_double_header(self, caplog, double_header) -> None:
        schedule = _schedule(
            (
                "2024-06-07",
                _entry(
                    745169,
                    "STL",
                    "COL",
                    game_date="2024-06-07T18:15:00Z",
                    official="2024-06-07",
                    double_header=double_header,
                ),
            ),
        )
        provider = _Stub(schedule, _745169_EVENTS)
        assert provider._resolve_event(745169) is None
        assert "sanity limit" in caplog.text
        assert provider.read_failures == 0

    def test_it_never_fires_with_two_same_team_events(self, caplog) -> None:
        events = {
            "2024-06-07": _slate(
                _event(92912, "STL", "COL", "2024-06-08 00:15:00"),
                _event(92913, "STL", "COL", "2024-06-07 12:00:00"),
            )
        }
        provider = _Stub(_745169, events)
        assert provider._resolve_event(745169) is None
        assert "sanity limit" in caplog.text

    @pytest.mark.parametrize(
        ("scheduled", "matched"),
        [
            ("2024-06-08 06:15:00", True),  # exactly 12 hours: inside the cap
            ("2024-06-08 06:16:00", False),  # 12 hours 1 minute
            ("2024-06-09 00:15:00", False),  # a day later
        ],
    )
    def test_the_twelve_hour_cap(self, scheduled, matched) -> None:
        provider = _Stub(_745169, {"2024-06-07": _slate(_event(92912, "STL", "COL", scheduled))})
        event = provider._resolve_event(745169)
        assert (event is not None) is matched

    def test_it_never_fires_on_an_event_from_another_slate(self) -> None:
        """The best event sits on the resumption slate: 9 hours from the resumption
        (21:10) and 12 hours from the played start (00:10). The played slate's lone
        event sits 10 hours from the played start, so it is not the best. The rule takes
        only an event of the played date's slate, so the matcher declines."""
        events = {
            "2024-08-27": _slate(_event(94820, "CWS", "TEX", "2024-08-27 14:10:00")),
            "2024-08-28": _slate(_event(94821, "CWS", "TEX", "2024-08-28 12:10:00")),
        }
        provider = _Stub(_746755, events)
        assert provider._resolve_event(746755) is None
        assert provider.queried_dates == ["2024-08-27", "2024-08-28"]

    def test_the_cap_is_measured_from_the_played_start(self, caplog) -> None:
        """The played slate's lone event sits 3 hours from the resumption (its gap) but
        18 hours from the played start: over the 12-hour cap, so the matcher declines."""
        events = {
            "2024-08-27": _slate(_event(94820, "CWS", "TEX", "2024-08-28 18:10:00")),
            "2024-08-28": _slate(),
        }
        provider = _Stub(_746755, events)
        assert provider._resolve_event(746755) is None
        assert "3:00:00 from the real start time" in caplog.text
        assert "sanity limit" in caplog.text


# --------------------------------------------------------------------------- read failures


class TestReadFailures:
    def test_a_failed_extra_slate_read_counts(self) -> None:
        provider = _Stub(_746755, _746755_EVENTS, fail_dates=("2024-08-28",))
        assert provider._resolve_event(746755) is None
        assert provider.queried_dates == ["2024-08-27", "2024-08-28"]
        assert provider.read_failures == 1

    def test_a_two_hour_match_stands_when_an_extra_slate_fails(self) -> None:
        """The vendor kept the event on the original date: the match is the game
        whatever the missing slate holds. The count keeps the game off the loader's
        done-list, so the next run reads the slate again."""
        events = {"2024-08-27": _slate(_event(94821, "CWS", "TEX", "2024-08-28 00:10:00"))}
        provider = _Stub(_746755, events, fail_dates=("2024-08-28",))
        event = provider._resolve_event(746755)
        assert event is not None and event["id"] == 94821
        assert provider.read_failures == 1

    def test_the_single_event_rule_needs_every_slate(self) -> None:
        """The lone played-slate event sits five hours off; the missing slate may hold
        the closer event, so the matcher declines."""
        events = {"2024-08-27": _slate(_event(94821, "CWS", "TEX", "2024-08-28 05:10:00"))}
        provider = _Stub(_746755, events, fail_dates=("2024-08-28",))
        assert provider._resolve_event(746755) is None
        assert provider.read_failures == 1
        # With every slate read, the same event matches by the single-event rule.
        provider = _Stub(_746755, events)
        event = provider._resolve_event(746755)
        assert event is not None and event["id"] == 94821
        assert provider.read_failures == 0

    @pytest.mark.parametrize(
        "schedule", [{"dates": []}, {"dates": [{"date": "2024-06-07", "games": []}]}, {}]
    )
    def test_an_answer_without_the_game_counts_nothing(self, schedule) -> None:
        provider = _Stub(schedule, _745169_EVENTS)
        assert provider._resolve_game_meta(745169) is None
        assert provider._resolve_event(745169) is None
        assert provider.queried_dates == []
        assert provider.read_failures == 0

    def test_a_failed_played_slate_read_counts_once(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, fail_dates=("2024-07-13",))
        assert provider._resolve_event(745175) is None
        assert provider.read_failures == 1


# --------------------------------------------------------------------------- the rows and the guard


def _line(cost: float, stamp: str, line: float | None = None) -> dict[str, Any]:
    return {
        "cost": cost,
        "line": line,
        "updated": stamp,
        "active": True,
        "is_off": False,
        "main": True,
    }


def _ml_offers(
    home: str,
    away: str,
    books: dict[int, tuple[int, int, str]],
    opener: tuple[int, int, int, str] | None = None,
) -> dict[str, Any]:
    """A moneyline offer: ``books`` maps id → (home, away, stamp); ``opener`` (id, home, away, created)."""
    sides = []
    for i, team in enumerate((home, away)):
        sel: dict[str, Any] = {
            "participant": team,
            "selection": "",
            "label": "",
            "books": [
                {"id": book_id, "lines": [_line(prices[i], prices[2])]}
                for book_id, prices in books.items()
            ],
        }
        if opener is not None:
            sel["opening_line"] = {
                "cost": opener[1 + i],
                "line": None,
                "book_id": opener[0],
                "created": opener[3],
            }
        sides.append(sel)
    return {"offers": [{"selections": sides, "participants": []}]}


def _k_offers(
    books: dict[int, tuple[int, int, str]], opener: tuple[int, int, int, str]
) -> dict[str, Any]:
    """A strikeout prop offer for Sonny Gray at 6.5 (same shapes as :func:`_ml_offers`)."""
    sides = []
    for i, label in enumerate(("Over", "Under")):
        sides.append(
            {
                "label": label,
                "selection": label.lower(),
                "books": [
                    {"id": book_id, "lines": [_line(prices[i], prices[2], line=6.5)]}
                    for book_id, prices in books.items()
                ],
                "opening_line": {
                    "cost": opener[1 + i],
                    "line": 6.5,
                    "book_id": opener[0],
                    "created": opener[3],
                },
            }
        )
    participant = {"name": "Sonny Gray", "player": {"first_name": "Sonny", "last_name": "Gray"}}
    return {"offers": [{"selections": sides, "participants": [participant]}]}


#: 745175's prices: bet365 (24) kept its close of the unplayed game (2024-05-24 23:46,
#: before the original 00:15 start); FanDuel (10) closed at the real game; DraftKings
#: (12) is stamped exactly at the original start.
_745175_OFFERS = {
    122: _ml_offers(
        "STL",
        "CHC",
        {
            24: (-125, 105, "2024-05-24 23:46:00"),
            10: (-140, 118, "2024-07-14 00:12:00"),
            12: (-130, 110, "2024-05-25 00:15:00"),
        },
        opener=(10, -120, 100, "2024-05-23 16:00:00"),
    ),
    285: _k_offers(
        {
            24: (-115, -105, "2024-05-24 23:46:00"),
            10: (-110, -110, "2024-07-14 00:12:00"),
        },
        opener=(10, -105, -115, "2024-07-12 15:00:00"),
    ),
}


class TestPostponedStartOnTheRows:
    def test_every_row_of_a_made_up_game_carries_it(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        postponed = _utc("2024-05-25 00:15:00")
        for line_type in ("opening", "closing"):
            for market in GAME_MARKET_TYPES:
                # The one-row call returns the empty row when the market has no offer.
                row = provider.get_odds(745175, line_type=line_type, market_type=market)
                assert row["postponed_start"] == postponed, (line_type, market)
                assert row["scheduled_start"] == _utc("2024-07-14 00:15:00")
                for book_row in provider.get_odds_by_book(
                    745175, line_type=line_type, market_type=market
                ):
                    assert book_row["postponed_start"] == postponed
            props = provider.get_prop_odds_by_book(
                745175, 543243, "strikeouts", line_type=line_type
            )
            assert props and all(r["postponed_start"] == postponed for r in props)
            empty = provider.get_prop_odds(745175, 543243, "walks", line_type=line_type)
            assert empty["postponed_start"] == postponed
        assert postponed.tzinfo is UTC

    def test_a_game_without_a_postponement_carries_none(self) -> None:
        offers = {122: _ml_offers("STL", "COL", {10: (-140, 118, "2024-06-07 18:10:00")})}
        provider = _Stub(_745169, _745169_EVENTS, offers=offers)
        rows = provider.get_odds_by_book(745169, line_type="closing")
        assert rows and all(r["postponed_start"] is None for r in rows)
        assert provider.get_prop_odds(745169, 1, "strikeouts")["postponed_start"] is None

    def test_an_unmatched_game_carries_none(self) -> None:
        provider = _Stub(_745175, {})
        assert provider.get_odds(745175)["postponed_start"] is None
        assert provider.get_prop_odds(745175, 1, "strikeouts")["postponed_start"] is None

    def test_a_subclass_that_resolves_the_meta_itself_carries_none(self) -> None:
        class _OwnMeta(_Stub):
            def _resolve_game_meta(self, game_pk):  # type: ignore[override]
                return (
                    "2024-07-13",
                    "St. Louis Cardinals",
                    "Chicago Cubs",
                    datetime(2024, 7, 14, 0, 15),
                )

        provider = _OwnMeta(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        rows = provider.get_odds_by_book(745175, line_type="closing")
        assert rows and all(r["postponed_start"] is None for r in rows)


class TestTheGuardRule:
    def test_the_rule_is_listed(self) -> None:
        assert "stamped_before_postponement" in RULES
        assert RULES[-1] == "stamped_before_postponement"

    def test_closing_game_rows(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        rows = {
            r["book"]: r
            for r in provider.get_odds_by_book(745175, line_type="closing", market_type="moneyline")
        }
        assert set(rows) == {"bp:24", "bp:10", "bp:12"}
        refusal = check_row(rows["bp:24"])
        assert refusal is not None and refusal.rule == "stamped_before_postponement"
        assert refusal.message == (
            "stamped 2024-05-24 23:46 UTC, before the postponed original start "
            "2024-05-25 00:15 UTC: the price of the game that was not played"
        )
        # Stamped AT the original start: refused too.
        at = check_row(rows["bp:12"])
        assert at is not None and at.rule == "stamped_before_postponement"
        # FanDuel's close of the real game is kept.
        assert check_row(rows["bp:10"]) is None
        assert refuse_reason(rows["bp:10"]) is None

    def test_opening_game_rows(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        (opening,) = provider.get_odds_by_book(745175, line_type="opening", market_type="moneyline")
        assert opening["book"] == "bp:10"
        assert opening["book_line_at"] == _utc("2024-05-23 16:00:00")
        refusal = check_row(opening)
        assert refusal is not None and refusal.rule == "stamped_before_postponement"

    def test_prop_rows(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        closing = {
            r["book"]: r
            for r in provider.get_prop_odds_by_book(
                745175, 543243, "strikeouts", line_type="closing"
            )
        }
        refused = check_row(closing["bp:24"])
        assert refused is not None and refused.rule == "stamped_before_postponement"
        assert check_row(closing["bp:10"]) is None
        # The opener was set for the made-up game (2024-07-12): kept.
        (opening,) = provider.get_prop_odds_by_book(
            745175, 543243, "strikeouts", line_type="opening"
        )
        assert check_row(opening) is None
        assert check_row({**opening, "book_line_at": _utc("2024-05-24 12:00:00")}) is not None

    def test_rows_without_a_postponed_start_are_not_checked(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        row = next(
            r
            for r in provider.get_odds_by_book(745175, line_type="closing", market_type="moneyline")
            if r["book"] == "bp:24"
        )
        assert check_row({**row, "postponed_start": None}) is None
        without = {k: v for k, v in row.items() if k != "postponed_start"}
        assert check_row(without) is None
        # A row without a stamp is not checked either.
        assert check_row({**row, "book_line_at": None}) is None

    def test_a_naive_postponed_start_reads_as_utc(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        row = next(
            r
            for r in provider.get_odds_by_book(745175, line_type="closing", market_type="moneyline")
            if r["book"] == "bp:10"
        )
        naive = {**row, "postponed_start": datetime(2024, 7, 14, 0, 12)}
        assert check_row(naive) is not None  # stamped AT the start, both read as UTC
        assert check_row({**row, "postponed_start": datetime(2024, 7, 14, 0, 11)}) is None

    def test_the_tally_counts_and_orders_the_rule(self) -> None:
        provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
        tally = RefusalTally()
        for row in provider.get_odds_by_book(745175, line_type="closing", market_type="moneyline"):
            tally.offered()
            refusal = check_row(row)
            if refusal is not None:
                tally.add(refusal, row["market_type"], row["book"])
        assert tally.counts() == {
            ("stamped_before_postponement", "moneyline", "bp:24"): 1,
            ("stamped_before_postponement", "moneyline", "bp:12"): 1,
        }
        assert "stamped_before_postponement: 2" in tally.summary()


class TestForgetGame:
    """A long-lived provider (the live pipeline) keeps a found game for the process
    lifetime. Looked up before its postponement, the game keeps its first entry's
    facts until ``forget_game`` drops them."""

    #: Day 1, before the rain: one entry, the original start, the original event.
    _DAY_ONE = _schedule(
        (
            "2024-07-09",
            _entry(
                746773,
                "CWS",
                "MIN",
                game_date="2024-07-10T00:10:00Z",
                official="2024-07-09",
                status=_SCHEDULED,
            ),
        ),
    )
    _DAY_ONE_EVENTS = {"2024-07-09": _slate(_event(93000, "CWS", "MIN", "2024-07-10 00:10:00"))}

    def _day_two(self) -> _Stub:
        """A provider that resolved the game on day 1, then sees day 2's answers."""
        offers = {122: _ml_offers("CWS", "MIN", {24: (-125, 105, "2024-07-09 23:46:00")})}
        provider = _Stub(self._DAY_ONE, dict(self._DAY_ONE_EVENTS), offers=offers)
        event = provider._resolve_event(746773)
        assert event is not None and event["id"] == 93000
        provider._schedule = _746773
        provider._events_by_date.update(_746773_EVENTS)
        return provider

    def test_without_it_the_facts_stay_stale(self) -> None:
        provider = self._day_two()
        assert provider._resolve_event(746773)["id"] == 93000
        (row,) = provider.get_odds_by_book(746773, line_type="current", market_type="moneyline")
        assert row["postponed_start"] is None
        assert check_row(row) is None

    def test_it_reads_the_new_schedule(self) -> None:
        provider = self._day_two()
        provider.forget_game(746773)
        event = provider._resolve_event(746773)
        assert event is not None and event["id"] == 93329
        (row,) = provider.get_odds_by_book(746773, line_type="current", market_type="moneyline")
        assert row["postponed_start"] == _utc("2024-07-10 00:10:00")
        refusal = check_row(row)  # bet365's price of the unplayed game
        assert refusal is not None and refusal.rule == "stamped_before_postponement"

    def test_it_drops_a_stored_failed_lookup(self) -> None:
        provider = _Stub(_745169, {"2024-06-07": _slate()}, offers_cache_ttl_s=600.0)
        assert provider._resolve_event(745169) is None
        provider._events_by_date.update(_745169_EVENTS)
        provider._events_by_date_cache.clear()
        assert provider._resolve_event(745169) is None  # the failure is still fresh
        provider.forget_game(745169)
        event = provider._resolve_event(745169)
        assert event is not None and event["id"] == 92912

    def test_an_unknown_game_is_a_no_op(self) -> None:
        provider = _Stub(_745169, _745169_EVENTS)
        provider.forget_game(1)
        assert provider._resolve_event(745169)["id"] == 92912


def test_the_late_closing_rule_still_runs_on_a_made_up_game() -> None:
    """A close stamped long after the REAL start is late, whatever the postponement."""
    provider = _Stub(_745175, _745175_EVENTS, offers=_745175_OFFERS)
    row = next(
        r
        for r in provider.get_odds_by_book(745175, line_type="closing", market_type="moneyline")
        if r["book"] == "bp:10"
    )
    late = {**row, "book_line_at": _utc("2024-07-14 00:15:00") + timedelta(minutes=40)}
    refusal = check_row(late)
    assert refusal is not None and refusal.rule == "late_closing_stamp"
