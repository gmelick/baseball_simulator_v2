"""
test_etl_schedule_filter_sim405fix.py
=====================================
Unit tests for the historical loader's future/non-Final game guard
(_schedule_game_is_final). The backfill must only load completed games — a
current-season schedule otherwise pulls unplayed games (0 pitches, one wasted
fetch + an empty raw.games stub each).
"""

from __future__ import annotations

from pipeline.etl.etl_historical_loader import _schedule_game_is_final


def _game(state: str | None) -> dict:
    return {"gamePk": 1, "status": {"abstractGameState": state}} if state else {"gamePk": 1}


def test_final_game_is_loadable():
    assert _schedule_game_is_final(_game("Final")) is True


def test_future_game_is_skipped():
    assert _schedule_game_is_final(_game("Preview")) is False


def test_in_progress_game_is_skipped():
    assert _schedule_game_is_final(_game("Live")) is False


def test_missing_status_is_skipped():
    assert _schedule_game_is_final(_game(None)) is False
    assert _schedule_game_is_final({}) is False
