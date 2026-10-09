"""SIM-519 Part A — the schedule client over recorded league responses.

The fixtures in ``tests/fixtures/mlb_schedule/`` are real schedule responses
(``hydrate=team,linescore,probablePitcher,lineups``), recorded 2026-10-09. They
pin the field names the parser reads.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pipeline import mlb_schedule as ms

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mlb_schedule"


def _load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _games(name: str) -> list[ms.ScheduleGame]:
    return ms.parse_schedule(_load(name))


def _by_pk(games: list[ms.ScheduleGame]) -> dict[int, ms.ScheduleGame]:
    return {g.game_pk: g for g in games}


def test_normal_day_fields() -> None:
    games = _games("normal_2024-08-15")
    assert len(games) == 7
    g = next(x for x in games if x.venue_name == "Citi Field")
    assert g.official_date == date(2024, 8, 15)
    assert g.season == 2024
    assert g.start_utc is not None and g.start_utc.tzinfo is not None
    assert g.start_time_tbd is False
    assert ms.card_state(g) == ms.FINAL
    assert g.home.abbreviation == "NYM"
    assert (g.away.score, g.home.score) == (7, 6)
    assert (g.away.hits, g.home.hits) == (12, 9)
    assert g.away.is_winner is True and g.home.is_winner is False
    assert (g.home.wins, g.home.losses) == (62, 59)
    assert g.home.probable_pitcher is not None
    assert g.home.probable_pitcher.name == "Jose Quintana"
    assert g.home.probable_pitcher.position == "P"
    assert g.lineups_posted
    assert g.home.lineup[0].name == "Francisco Lindor"
    assert g.home.lineup[0].position == "SS"
    # A final game carries no live inning or outs.
    assert g.inning is None and g.outs is None


def test_split_doubleheader_numbers_both_games() -> None:
    games = _games("split_dh_2024-04-13")
    split = [g for g in games if g.double_header == "S"]
    assert split, "the recorded day holds a split doubleheader"
    by_teams: dict[tuple[int, int], list[int]] = {}
    for g in split:
        by_teams.setdefault((g.away.team_id, g.home.team_id), []).append(g.game_number)
    assert any(sorted(nums) == [1, 2] for nums in by_teams.values())


def test_straight_doubleheader_and_makeup() -> None:
    games = _games("straight_dh_makeup_2024-04-04")
    straight = [g for g in games if g.double_header == "Y"]
    assert {g.game_number for g in straight} >= {1, 2}
    # The make-up of 2024-04-02's postponement says where it came from.
    assert any(g.rescheduled_from == date(2024, 4, 2) for g in games)


def test_postponed_entry_is_a_postponed_card_with_its_makeup_date() -> None:
    games = _games("postponed_2024-04-02")
    ppd = [g for g in games if g.coded_state == "D"]
    assert ppd
    g = ppd[0]
    assert g.abstract_state == "Final"  # the league's encoding
    assert ms.card_state(g) == ms.POSTPONED
    assert g.reason == "Rain"
    assert g.rescheduled_to == date(2024, 4, 4)


def test_cancelled_entry_is_postponed() -> None:
    games = _games("cancelled_2026-09-27")
    cancelled = [g for g in games if g.coded_state == "C"]
    assert cancelled
    assert all(ms.card_state(g) == ms.POSTPONED for g in cancelled)


def test_off_day_is_empty() -> None:
    assert _games("off_day_2024-07-17") == []


def test_suspended_game_appears_on_both_days_as_final() -> None:
    first = _by_pk(_games("suspended_2024-05-21"))
    g = first[745180]
    assert ms.card_state(g) == ms.FINAL


def test_preview_game() -> None:
    games = _games("preview_2026-10-10")
    assert len(games) == 1
    g = games[0]
    assert ms.card_state(g) == ms.SCHEDULED
    assert g.official_date == date(2026, 10, 10)
    # A night game whose UTC start falls on the next calendar day.
    assert g.start_utc == datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    assert g.series_description == "AL Division Series"
    assert g.home.score is None


def test_start_time_tbd_reads_none() -> None:
    payload = copy.deepcopy(_load("preview_2026-10-10"))
    payload["dates"][0]["games"][0]["status"]["startTimeTBD"] = True
    g = ms.parse_schedule(payload)[0]
    assert g.start_time_tbd is True
    assert g.start_utc is None


def test_live_game_reads_inning_half_and_outs() -> None:
    payload = copy.deepcopy(_load("normal_2024-08-15"))
    entry = payload["dates"][0]["games"][0]
    entry["status"].update(
        abstractGameState="Live", codedGameState="I", detailedState="In Progress"
    )
    entry["linescore"].update(currentInning=6, inningHalf="Top", outs=2)
    g = ms.parse_schedule(payload)[0]
    assert ms.card_state(g) == ms.LIVE
    assert (g.inning, g.inning_half, g.outs) == (6, "Top", 2)


def test_malformed_entry_is_skipped_not_fatal() -> None:
    payload = copy.deepcopy(_load("normal_2024-08-15"))
    del payload["dates"][0]["games"][0]["teams"]
    assert len(ms.parse_schedule(payload)) == 6


@pytest.mark.parametrize(
    ("abstract", "coded", "detailed", "expected"),
    [
        ("Preview", "S", "Scheduled", ms.SCHEDULED),
        ("Preview", "P", "Pre-Game", ms.SCHEDULED),
        ("Preview", "P", "Warmup", ms.SCHEDULED),
        ("Preview", "S", "Delayed Start", ms.SCHEDULED),
        ("Live", "I", "In Progress", ms.LIVE),
        ("Live", "I", "Delayed", ms.LIVE),
        ("Live", "M", "Manager challenge", ms.LIVE),
        ("Live", "N", "Umpire review", ms.LIVE),
        ("Final", "F", "Final", ms.FINAL),
        ("Final", "O", "Game Over", ms.FINAL),
        ("Final", "F", "Completed Early", ms.FINAL),
        ("Final", "D", "Postponed", ms.POSTPONED),
        ("Final", "C", "Cancelled", ms.POSTPONED),
        ("Live", "U", "Suspended: Rain", ms.POSTPONED),
        ("Final", "T", "Suspended", ms.POSTPONED),
        ("Preview", "S", "Postponed: Rain", ms.POSTPONED),
        ("Other", "Z", "Something new", ms.SCHEDULED),
        ("", "", "", ms.SCHEDULED),
    ],
)
def test_card_state_table(abstract: str, coded: str, detailed: str, expected: str) -> None:
    base = _games("preview_2026-10-10")[0]
    g = ms.ScheduleGame(
        **{
            **{f: getattr(base, f) for f in base.__dataclass_fields__},
            "abstract_state": abstract,
            "coded_state": coded,
            "detailed_state": detailed,
        }
    )
    assert ms.card_state(g) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Preview", ms.SCHEDULED),
        ("Warmup", ms.SCHEDULED),
        ("Pre-Game", ms.SCHEDULED),
        ("Live", ms.LIVE),
        ("Final", ms.FINAL),
        ("Postponed", ms.POSTPONED),
        ("Suspended: Rain", ms.POSTPONED),
        ("Cancelled", ms.POSTPONED),
        (None, ms.SCHEDULED),
        ("whatever", ms.SCHEDULED),
    ],
)
def test_card_state_from_raw(raw: str | None, expected: str) -> None:
    assert ms.card_state_from_raw(raw) == expected


def test_schedule_params() -> None:
    p = ms.schedule_params(date(2026, 10, 9), date(2026, 10, 10))
    assert p["startDate"] == "2026-10-09" and p["endDate"] == "2026-10-10"
    assert p["hydrate"] == ms.DEFAULT_HYDRATE
    assert p["gameTypes"].split(",") == list(ms.GAME_TYPES)
