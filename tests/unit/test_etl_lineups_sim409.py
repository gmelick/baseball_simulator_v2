"""
test_etl_lineups_sim409.py
==========================
Unit tests for SIM-409's starting-lineup builder (_build_starting_lineup_rows),
which turns a feed/live boxscore into raw.game_lineups rows. Pure function —
no DB.
"""

from __future__ import annotations

from pipeline.etl.etl_historical_loader import _build_starting_lineup_rows


def _player(pid: int, batting_order: str | None, pos: str) -> dict:
    return {"person": {"id": pid}, "battingOrder": batting_order, "position": {"abbreviation": pos}}


def _game_dict(home_box: dict, away_box: dict, home_id=140, away_id=111) -> dict:
    return {
        "gameData": {"teams": {"home": {"id": home_id}, "away": {"id": away_id}}},
        "liveData": {"boxscore": {"teams": {"home": home_box, "away": away_box}}},
    }


def test_al_lineup_pitcher_added_separately():
    """AL/DH game: 9 batters (DH, no pitcher in order) + starting pitcher row."""
    home_box = {
        "players": {
            f"ID{pid}": _player(pid, bo, pos)
            for pid, bo, pos in [
                (1, "100", "2B"),
                (2, "200", "SS"),
                (3, "300", "1B"),
                (4, "400", "RF"),
                (5, "500", "DH"),
                (6, "600", "3B"),
                (7, "700", "C"),
                (8, "800", "LF"),
                (9, "900", "CF"),
            ]
        },
        "pitchers": [101, 102],  # starter 101, not in batting order
    }
    rows = _build_starting_lineup_rows(745001, 2024, _game_dict(home_box, {}))
    # 9 batters + 1 starting pitcher = 10 home rows.
    home_rows = [r for r in rows if r[2] == 140]
    assert len(home_rows) == 10
    # leadoff: (game_pk, season, team_id, player_id, batting_order, pos, is_starter, seq)
    leadoff = next(r for r in home_rows if r[3] == 1)
    assert leadoff == (745001, 2024, 140, 1, 1, "2B", True, 1)
    # starting pitcher: NULL batting_order, position 'P'
    sp = next(r for r in home_rows if r[3] == 101)
    assert sp == (745001, 2024, 140, 101, None, "P", True, 1)


def test_nl_pitcher_bats_not_double_added():
    """NL game: pitcher bats 9th (battingOrder 900, pos P) — not added twice."""
    home_box = {
        "players": {
            "ID1": _player(1, "100", "CF"),
            "ID9": _player(9, "900", "P"),  # pitcher bats 9th
        },
        "pitchers": [9],  # same player as the 9th batter
    }
    rows = [
        r
        for r in _build_starting_lineup_rows(745001, 2024, _game_dict(home_box, {}))
        if r[2] == 140
    ]
    pitcher_rows = [r for r in rows if r[3] == 9]
    assert len(pitcher_rows) == 1  # not double-added
    assert pitcher_rows[0] == (745001, 2024, 140, 9, 9, "P", True, 1)


def test_substitutes_are_skipped():
    """A slot's later occupant (battingOrder not ending in 00) is not a starter."""
    home_box = {
        "players": {
            "ID1": _player(1, "100", "LF"),  # starter, slot 1
            "ID2": _player(2, "101", "LF"),  # sub in slot 1 → skipped
        },
        "pitchers": [99],
    }
    rows = [
        r
        for r in _build_starting_lineup_rows(745001, 2024, _game_dict(home_box, {}))
        if r[2] == 140
    ]
    batter_ids = {r[3] for r in rows if r[4] is not None}
    assert 1 in batter_ids
    assert 2 not in batter_ids  # the substitute is excluded


def test_both_teams_built():
    box = {"players": {"ID1": _player(1, "100", "CF")}, "pitchers": [50]}
    away = {"players": {"ID2": _player(2, "100", "CF")}, "pitchers": [60]}
    rows = _build_starting_lineup_rows(745001, 2024, _game_dict(box, away))
    assert {r[2] for r in rows} == {140, 111}  # both team_ids present


def test_empty_boxscore_returns_no_rows():
    assert _build_starting_lineup_rows(745001, 2024, {"gameData": {}, "liveData": {}}) == []
