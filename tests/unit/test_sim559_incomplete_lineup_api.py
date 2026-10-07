"""
test_sim559_incomplete_lineup_api.py
====================================
SIM-559, the last part: a KNOWN game whose lineup rows do not make a playable
game answers 503 with Retry-After, not 404.

Before this, the resolver raised the base ``LineupResolutionError`` for a side
with no pitcher or an empty batting order, and the API mapped every base-class
error to 404 Not Found — the answer for a game that is not in ``raw.games``.
A real game with a data gap is not missing; a later lineup publish or the box
backfill fills the gap, so the API now answers it as it answers an unpublished
lineup (SIM-409): 503 Service Unavailable with ``Retry-After: 900``.

The resolver raises ``LineupIncompleteError`` (a ``LineupResolutionError``
subclass, a sibling of ``LineupNotIngestedError``) at its three "rows exist but
do not resolve" sites; the API catches both subclasses before the base class.
No live DB: the resolver tests use dict rows, the API tests monkeypatch the
resolver as the SIM-409 tests do.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
import simulation.lineup_resolver as lr
from api.routes.games import router as games_router
from simulation.game_state import Half
from simulation.lineup_resolver import (
    LineupIncompleteError,
    LineupNotIngestedError,
    LineupResolutionError,
    build_game_state,
    resolve_game_state,
    resolve_lineup_from_rows,
)

GAME_PK, HOME, AWAY = 823372, 134, 119
HOME_BATTERS = list(range(1, 10))
AWAY_BATTERS = list(range(11, 20))
HOME_STARTER, AWAY_STARTER = 50, 60
POSITIONS = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH")


def _row(team: int, pid: int, bo: int | None, pos: str) -> dict:
    return {
        "team_id": team,
        "player_id": pid,
        "batting_order": bo,
        "position_code": pos,
        "is_starter": True,
        "sequence": 1,
    }


def _batting_rows(team: int, pids: list[int]) -> list[dict]:
    return [
        _row(team, pid, i + 1, pos)
        for i, (pid, pos) in enumerate(zip(pids, POSITIONS, strict=True))
    ]


def _resolve(rows: list[dict], box: dict[int, int] | None = None):
    return resolve_lineup_from_rows(
        game_pk=GAME_PK,
        season=2026,
        home_team_id=HOME,
        away_team_id=AWAY,
        lineup_rows=rows,
        box_starters=box,
    )


def _complete_rows() -> list[dict]:
    return (
        _batting_rows(HOME, HOME_BATTERS)
        + _batting_rows(AWAY, AWAY_BATTERS)
        + [_row(HOME, HOME_STARTER, None, "P"), _row(AWAY, AWAY_STARTER, None, "P")]
    )


# ---------------------------------------------------------------------------
# The error class
# ---------------------------------------------------------------------------


def test_incomplete_is_a_resolution_error_but_not_the_unpublished_one():
    exc = LineupIncompleteError("x")
    assert isinstance(exc, LineupResolutionError)
    assert isinstance(exc, ValueError)  # the SIM-326 invalid-state guard still catches it
    assert not isinstance(exc, LineupNotIngestedError)
    assert "LineupIncompleteError" in lr.__all__


# ---------------------------------------------------------------------------
# The resolver raises it at its three "rows exist but do not resolve" sites
# ---------------------------------------------------------------------------


class TestTheResolverRaisesIncomplete:
    def test_a_side_with_no_pitcher_is_incomplete(self):
        rows = _batting_rows(HOME, HOME_BATTERS) + _batting_rows(AWAY, AWAY_BATTERS)
        rows.append(_row(HOME, HOME_STARTER, None, "P"))  # the away side has no pitcher, no box
        with pytest.raises(LineupIncompleteError, match="no resolvable pitcher") as info:
            build_game_state(_resolve(rows), half=Half.TOP)
        assert f"the away team ({AWAY})" in str(info.value)
        assert "box backfill" in str(info.value)

    def test_an_empty_batting_order_is_incomplete(self):
        rows = _batting_rows(HOME, HOME_BATTERS) + [
            _row(HOME, HOME_STARTER, None, "P"),
            _row(AWAY, AWAY_STARTER, None, "P"),  # the away side has a pitcher and no batters
        ]
        with pytest.raises(LineupIncompleteError, match="empty batting order"):
            build_game_state(_resolve(rows), half=Half.TOP)

    def test_rows_for_neither_side_are_incomplete(self):
        rows = _batting_rows(999, HOME_BATTERS)  # a third team's rows only
        with pytest.raises(LineupIncompleteError, match="no batting-order rows resolved"):
            _resolve(rows)

    def test_the_box_starter_repairs_a_missing_pitcher_row(self):
        rows = _batting_rows(HOME, HOME_BATTERS) + _batting_rows(AWAY, AWAY_BATTERS)
        rows.append(_row(HOME, HOME_STARTER, None, "P"))
        state = build_game_state(_resolve(rows, {AWAY: AWAY_STARTER}), half=Half.TOP)
        assert state.away_pitcher_id == AWAY_STARTER

    def test_a_complete_lineup_builds_a_state(self):
        state = build_game_state(_resolve(_complete_rows()), half=Half.TOP)
        assert (state.home_pitcher_id, state.away_pitcher_id) == (HOME_STARTER, AWAY_STARTER)


class _Conn:
    """A fake asyncpg connection: one game row (or none) and the lineup rows."""

    def __init__(self, game_row, lineup_rows):
        self._game = game_row
        self._rows = lineup_rows

    async def fetchrow(self, sql, *args):
        return self._game

    async def fetch(self, sql, *args):
        return self._rows


class TestTheOtherTwoErrorsAreUnchanged:
    async def test_an_unknown_game_is_still_the_base_error(self):
        with pytest.raises(LineupResolutionError, match="not found in raw.games") as info:
            await resolve_game_state(_Conn(None, []), GAME_PK)
        assert not isinstance(info.value, LineupIncompleteError)
        assert not isinstance(info.value, LineupNotIngestedError)

    async def test_a_game_with_no_rows_is_still_not_ingested(self):
        game = {
            "game_pk": GAME_PK,
            "season": 2026,
            "home_team_id": HOME,
            "away_team_id": AWAY,
            "home_manager_id": None,
            "away_manager_id": None,
            "game_date": None,
        }
        with pytest.raises(LineupNotIngestedError, match="not yet published") as info:
            await resolve_game_state(_Conn(game, []), GAME_PK)
        assert not isinstance(info.value, LineupIncompleteError)


# ---------------------------------------------------------------------------
# The API answers 503 with Retry-After
# ---------------------------------------------------------------------------


class _Pool:
    """A pool with no ``acquire``: the route passes it to the resolver as the
    connection. The resolver is monkeypatched, so it is never read."""

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = _Pool()
    app.state.sim_cache = None
    app.state.sim_factory_ref = "simulation.batch_runner:rng_driven_machine_factory"
    return app


def _raising(exc: Exception):
    async def _raise(conn, game_pk, **kwargs):
        raise exc

    return _raise


class TestTheApiMapping:
    def test_simulate_answers_503_with_retry_after_for_an_incomplete_lineup(self, monkeypatch):
        monkeypatch.setattr(
            games_mod,
            "resolve_game_state",
            _raising(
                LineupIncompleteError(
                    f"the away team ({AWAY}) has no resolvable pitcher for game_pk={GAME_PK}: ..."
                )
            ),
        )
        resp = TestClient(_app()).get(f"/api/games/{GAME_PK}/simulate?n_iterations=3")
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "900"
        assert "no resolvable pitcher" in resp.json()["detail"]

    def test_the_boxscore_route_takes_the_same_mapping(self, monkeypatch):
        monkeypatch.setattr(
            games_mod, "resolve_game_state", _raising(LineupIncompleteError("empty batting order"))
        )
        resp = TestClient(_app()).get(f"/api/games/{GAME_PK}/boxscore?n_iterations=3")
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "900"

    def test_an_unpublished_lineup_still_answers_503(self, monkeypatch):
        monkeypatch.setattr(
            games_mod, "resolve_game_state", _raising(LineupNotIngestedError("not yet published"))
        )
        resp = TestClient(_app()).get(f"/api/games/{GAME_PK}/simulate?n_iterations=3")
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "900"

    def test_an_unknown_game_still_answers_404(self, monkeypatch):
        monkeypatch.setattr(
            games_mod,
            "resolve_game_state",
            _raising(LineupResolutionError("not found in raw.games")),
        )
        resp = TestClient(_app()).get("/api/games/999999/simulate?n_iterations=3")
        assert resp.status_code == 404
        assert "Retry-After" not in resp.headers
