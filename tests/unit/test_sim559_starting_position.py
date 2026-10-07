"""
test_sim559_starting_position.py
================================
SIM-559: the lineup loader stores each starter's STARTING position
(``allPositions[0]``), not the last position he held (``position``).

The box feed's ``position`` field is the player's last position of the game.
Before SIM-559 the loader wrote it into ``raw.game_lineups.position_code``, so
a fielder who moved mid-game held his final slot in the simulator's defense
map, his starting slot was empty, and the player who really started at his
final slot lost his glove (one team-game in four; 29 of the certifying set's
90 team-games). These tests pin the new reading on the loader, the shared
helper, the backfill parser and the defense map the resolver builds.
Pure functions, no DB.
"""

from __future__ import annotations

import asyncio

import pytest

from pipeline.etl.boxscore_ingest import (
    STARTING_POSITION_UPDATE_SQL_ASYNCPG,
    StartingPosition,
    parse_starting_positions,
    persist_starting_positions,
    starting_position,
)
from pipeline.etl.etl_historical_loader import _build_starting_lineup_rows
from simulation.lineup_resolver import build_team_defense_map, resolve_lineup_from_rows

HOME, AWAY = 134, 119  # Pittsburgh, Los Angeles (NL) — the real game 823372's sides


def _player(
    pid: int,
    batting_order: str | None,
    last: str,
    all_positions: list[str] | None = None,
    *,
    games_started: int | None = None,
) -> dict:
    """A box ``players`` entry. ``last`` is the feed's ``position`` (the last
    position held); ``all_positions`` is the feed's ``allPositions`` in order.
    ``None`` leaves ``allPositions`` out of the payload (an older shape)."""
    p: dict = {
        "person": {"id": pid, "fullName": f"Player {pid}"},
        "battingOrder": batting_order,
        "position": {"abbreviation": last},
    }
    if all_positions is not None:
        p["allPositions"] = [{"abbreviation": a} for a in all_positions]
    if games_started is not None:
        p["stats"] = {"pitching": {"gamesStarted": games_started}}
    return p


def _game_dict(home_box: dict, away_box: dict) -> dict:
    return {
        "gameData": {"teams": {"home": {"id": HOME}, "away": {"id": AWAY}}},
        "liveData": {"boxscore": {"teams": {"home": home_box, "away": away_box}}},
    }


def _game_823372_home() -> dict:
    """The real shape of game 823372's home box (2026-10-05): Reynolds started
    in left and finished in right, Mangum started in center and finished in
    left, Callihan started and finished in right. The old reading gave two
    right fielders and no center fielder."""
    return {
        "team": {"id": HOME},
        "players": {
            "ID1": _player(1, "100", "1B", ["1B"]),
            "ID2": _player(2, "200", "2B", ["2B"]),
            "ID3": _player(3, "300", "RF", ["LF", "RF"]),  # Reynolds
            "ID4": _player(4, "400", "DH", ["DH"]),
            "ID5": _player(5, "500", "3B", ["3B"]),
            "ID6": _player(6, "600", "C", ["C"]),
            "ID7": _player(7, "700", "RF", ["RF"]),  # Callihan
            "ID8": _player(8, "800", "LF", ["CF", "LF"]),  # Mangum
            "ID9": _player(9, "900", "SS", ["SS"]),
            "ID50": _player(50, None, "P", ["P"], games_started=1),
            "ID51": _player(51, "301", "LF", ["LF"]),  # a sub in slot 3, not a starter
        },
        "pitchers": [50, 60],
    }


FIELDING = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")


# ---------------------------------------------------------------------------
# starting_position — the shared helper
# ---------------------------------------------------------------------------


def test_starting_position_is_the_first_of_all_positions():
    assert starting_position(_player(3, "300", "RF", ["LF", "RF"])) == "LF"


def test_starting_position_two_way_starter_reads_pitcher():
    """Ohtani pitches and bats as the designated hitter: P then DH, last DH."""
    assert starting_position(_player(1, "100", "DH", ["P", "DH"])) == "P"


def test_starting_position_position_player_who_pitched_keeps_his_glove():
    assert starting_position(_player(9, "900", "P", ["SS", "P"])) == "SS"


def test_starting_position_without_all_positions_falls_back_to_position():
    assert starting_position(_player(2, "200", "2B")) == "2B"


def test_starting_position_blank_first_entry_falls_back_to_position():
    p = _player(2, "200", "2B", ["2B"])
    p["allPositions"] = [{}]
    assert starting_position(p) == "2B"
    p["allPositions"] = []
    assert starting_position(p) == "2B"


def test_starting_position_none_when_nothing_names_one():
    assert starting_position({"person": {"id": 1}}) is None
    assert starting_position({"position": {}, "allPositions": [{"abbreviation": ""}]}) is None


def test_starting_position_truncates_to_the_column_width():
    assert starting_position({"allPositions": [{"abbreviation": "TOOLONG"}]}) == "TOOLO"


# ---------------------------------------------------------------------------
# _build_starting_lineup_rows — the loader
# ---------------------------------------------------------------------------


def _home_rows(game_dict: dict) -> list[tuple]:
    return [r for r in _build_starting_lineup_rows(823372, 2026, game_dict) if r[2] == HOME]


def test_loader_codes_a_moved_starter_at_his_starting_position():
    rows = _home_rows(_game_dict(_game_823372_home(), {}))
    by_pid = {r[3]: r for r in rows}
    assert by_pid[3][5] == "LF"  # Reynolds started in left
    assert by_pid[8][5] == "CF"  # Mangum started in center
    assert by_pid[7][5] == "RF"  # Callihan, unchanged
    codes = sorted(r[5] for r in rows if r[4] is not None and r[5] in FIELDING)
    assert codes == sorted(FIELDING)  # all eight gloves, each once


def test_loader_keeps_the_row_shape_and_the_sub_out():
    rows = _home_rows(_game_dict(_game_823372_home(), {}))
    assert len(rows) == 10  # nine batters + the starting pitcher
    assert 51 not in {r[3] for r in rows}
    assert (823372, 2026, HOME, 3, 3, "LF", True, 1) in rows
    assert (823372, 2026, HOME, 50, None, "P", True, 1) in rows


def test_loader_two_way_starter_is_the_pitcher_and_is_not_double_added():
    """The two-way starter bats first and pitches: one row, coded 'P', slot 1.
    Before SIM-559 he was coded 'DH' and his side had no pitcher row (the
    SIM-558 finding: 80 team-games)."""
    away = {
        "team": {"id": AWAY},
        "players": {
            "ID17": _player(17, "100", "DH", ["P", "DH"], games_started=1),
            "ID18": _player(18, "200", "CF", ["CF"]),
        },
        "pitchers": [17, 70],
    }
    rows = [
        r for r in _build_starting_lineup_rows(823372, 2026, _game_dict({}, away)) if r[2] == AWAY
    ]
    ohtani = [r for r in rows if r[3] == 17]
    assert ohtani == [(823372, 2026, AWAY, 17, 1, "P", True, 1)]


def test_loader_position_player_who_pitched_is_not_coded_pitcher():
    """A shortstop who threw the ninth is coded 'SS'; the real starter keeps
    the only 'P' row (before SIM-559: 308 team-games with a second 'P')."""
    home = {
        "team": {"id": HOME},
        "players": {
            "ID9": _player(9, "900", "P", ["SS", "P"]),
            "ID50": _player(50, None, "P", ["P"], games_started=1),
        },
        "pitchers": [50, 9],
    }
    rows = _home_rows(_game_dict(home, {}))
    assert {r[3]: r[5] for r in rows} == {9: "SS", 50: "P"}


def test_loader_without_all_positions_reads_as_before():
    """An older payload without ``allPositions`` keeps the previous reading."""
    home = {"team": {"id": HOME}, "players": {"ID2": _player(2, "200", "2B")}, "pitchers": [50]}
    rows = _home_rows(_game_dict(home, {}))
    assert {r[3]: r[5] for r in rows} == {2: "2B", 50: "P"}


def test_loader_nameless_position_falls_back_to_ut():
    home = {
        "team": {"id": HOME},
        "players": {"ID2": {"person": {"id": 2}, "battingOrder": "200", "position": {}}},
        "pitchers": [],
    }
    rows = _home_rows(_game_dict(home, {}))
    assert rows == [(823372, 2026, HOME, 2, 2, "UT", True, 1)]


# ---------------------------------------------------------------------------
# The defense map the resolver builds from the loader's rows
# ---------------------------------------------------------------------------


def _as_lineup_rows(rows: list[tuple]) -> list[dict]:
    keys = (
        "game_pk",
        "season",
        "team_id",
        "player_id",
        "batting_order",
        "position_code",
        "is_starter",
        "sequence",
    )
    return [dict(zip(keys, r, strict=True)) for r in rows]


def test_defense_map_has_all_eight_gloves_from_the_fixed_rows():
    rows = _build_starting_lineup_rows(823372, 2026, _game_dict(_game_823372_home(), {}))
    resolved = resolve_lineup_from_rows(
        game_pk=823372,
        season=2026,
        home_team_id=HOME,
        away_team_id=AWAY,
        lineup_rows=_as_lineup_rows(rows),
    )
    defense = build_team_defense_map(resolved.home)
    assert {k: defense[k] for k in FIELDING} == {
        "C": 6,
        "1B": 1,
        "2B": 2,
        "3B": 5,
        "SS": 9,
        "LF": 3,
        "CF": 8,
        "RF": 7,
    }
    assert defense["P"] == 50


def test_defense_map_from_the_old_reading_has_the_hole_the_fix_closes():
    """The same game under the old reading (last position): two right
    fielders, so one loses his slot, and no center fielder. This is the
    defect the backfill repairs; the test documents it so the gain is read
    against a pinned baseline."""
    old_rows = [
        (823372, 2026, HOME, pid, bo, last, True, 1)
        for pid, bo, last in [
            (1, 1, "1B"),
            (2, 2, "2B"),
            (3, 3, "RF"),
            (4, 4, "DH"),
            (5, 5, "3B"),
            (6, 6, "C"),
            (7, 7, "RF"),
            (8, 8, "LF"),
            (9, 9, "SS"),
        ]
    ] + [(823372, 2026, HOME, 50, None, "P", True, 1)]
    resolved = resolve_lineup_from_rows(
        game_pk=823372,
        season=2026,
        home_team_id=HOME,
        away_team_id=AWAY,
        lineup_rows=_as_lineup_rows(old_rows),
    )
    defense = build_team_defense_map(resolved.home)
    assert "CF" not in defense
    assert defense["RF"] == 3 and defense["LF"] == 8  # the wrong men in both corners
    assert 7 not in defense.values()  # the real right fielder has no glove


# ---------------------------------------------------------------------------
# parse_starting_positions + persist_starting_positions — the backfill
# ---------------------------------------------------------------------------


def test_parse_starting_positions_one_row_per_batting_starter():
    rows = parse_starting_positions({"home": _game_823372_home()}, game_pk=823372)
    assert len(rows) == 9  # the DH-game starting pitcher (no batting order) and the sub are out
    by_pid = {r.player_id: r for r in rows}
    assert by_pid[3] == StartingPosition(823372, HOME, 3, 3, "LF", "RF")
    assert by_pid[3].moved and not by_pid[7].moved
    assert sorted(r.batting_order for r in rows) == list(range(1, 10))


def test_parse_starting_positions_team_id_fallback_and_missing_side():
    box = _game_823372_home()
    del box["team"]
    rows = parse_starting_positions({"home": box}, game_pk=823372, home_team_id=HOME)
    assert {r.team_id for r in rows} == {HOME}
    assert parse_starting_positions({"home": box}, game_pk=823372) == []  # no id anywhere: no rows


def test_parse_starting_positions_skips_a_row_without_a_batting_order_or_a_position():
    box = {
        "team": {"id": HOME},
        "players": {
            "IDa": {"person": {"id": 1}, "battingOrder": "abc", "position": {"abbreviation": "C"}},
            "IDb": {"person": {"id": 2}, "battingOrder": "200", "position": {}},
            "IDc": {"person": {}, "battingOrder": "300", "position": {"abbreviation": "SS"}},
        },
    }
    assert parse_starting_positions({"home": box}, game_pk=1) == []


class _FakeConn:
    def __init__(self, status: str = "UPDATE 2") -> None:
        self.calls: list[tuple] = []
        self._status = status

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return self._status


def test_persist_starting_positions_sends_one_statement_with_the_arrays():
    conn = _FakeConn("UPDATE 2")
    rows = [
        StartingPosition(823372, HOME, 3, 3, "LF", "RF"),
        StartingPosition(823372, HOME, 8, 8, "CF", "LF"),
    ]
    changed = asyncio.run(persist_starting_positions(conn, rows))
    assert changed == 2
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert sql == STARTING_POSITION_UPDATE_SQL_ASYNCPG
    assert args == ([823372, 823372], [HOME, HOME], [3, 8], ["LF", "CF"])


def test_persist_starting_positions_writes_nothing_for_no_rows():
    conn = _FakeConn()
    assert asyncio.run(persist_starting_positions(conn, [])) == 0
    assert conn.calls == []


@pytest.mark.parametrize("status,expected", [("UPDATE 7", 7), ("UPDATE 0", 0), ("", 0), (None, 0)])
def test_persist_starting_positions_reads_the_command_tag(status, expected):
    conn = _FakeConn(status)
    rows = [StartingPosition(1, HOME, 3, 3, "LF", "RF")]
    assert asyncio.run(persist_starting_positions(conn, rows)) == expected


def test_update_sql_touches_only_starters_whose_code_differs():
    sql = " ".join(STARTING_POSITION_UPDATE_SQL_ASYNCPG.split())
    assert "l.sequence = 1" in sql
    assert "l.is_starter" in sql
    assert "l.position_code IS DISTINCT FROM v.position_code" in sql
