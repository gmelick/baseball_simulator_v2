"""
tests/unit/test_api_realism_wiring_sim411_425b.py
=================================================
SIM-411 / SIM-425b — the API wiring that threads the realism inputs into the
``simulate_game`` GameSpec so the gated consumers can act in PRODUCTION:

  * ``_sim_kwargs_from_state`` carries the per-position defense maps (SIM-425b) +
    the venue park run-factor (SIM-411) off the resolved GameState;
  * ``_resolve_park_run_factor`` resolves the venue (Postgres) -> park factor
    (DuckDB ``derived.park_factors``) two-source lookup, defaulting to a neutral
    1.0 on every missing piece so ``/simulate`` is never broken by it.
"""

from __future__ import annotations

import asyncio

from api.routes.games import _resolve_park_run_factor, _sim_kwargs_from_state
from simulation.game_state import GameState


def test_sim_kwargs_carries_defense_maps_and_park_factor():
    state = GameState(
        pitcher_id=1,
        bat_hand="R",
        season=2024,
        home_defense={"C": 2, "SS": 6},
        away_defense={"P": 9, "CF": 8},
        park_run_factor=1.15,
    )
    kw = _sim_kwargs_from_state(state)
    assert kw["home_defense"] == {"C": 2, "SS": 6}
    assert kw["away_defense"] == {"P": 9, "CF": 8}
    assert kw["park_run_factor"] == 1.15


def test_sim_kwargs_defaults_neutral_when_state_bare():
    # A state with no defense / no park factor -> empty maps + 1.0 (the consumers
    # then stay neutral even with their flags on).
    state = GameState(pitcher_id=1, bat_hand="R", season=2024)
    kw = _sim_kwargs_from_state(state)
    assert kw["home_defense"] == {} and kw["away_defense"] == {}
    assert kw["park_run_factor"] == 1.0


# --- _resolve_park_run_factor (two-source venue -> factor lookup) -------------


class _FakeRecord(dict):
    """asyncpg-Record-like: supports row["venue_id"]."""


class _FakePool:
    """A direct-connection-style pool (no .acquire) exposing async fetchrow."""

    def __init__(self, venue_id):
        self._venue_id = venue_id

    async def fetchrow(self, sql, gid):
        return None if self._venue_id is None else _FakeRecord(venue_id=self._venue_id)


class _FakeDuckRes:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeCon:
    def __init__(self, factor):
        self._factor = factor

    def execute(self, sql, params):
        return _FakeDuckRes(None if self._factor is None else (self._factor,))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_park_factor_resolved_from_both_sources():
    f = _run(_resolve_park_run_factor(_FakePool(15), _FakeCon(1.18), 777, 2024))
    assert f == 1.18


def test_park_factor_neutral_without_duckdb():
    assert _run(_resolve_park_run_factor(_FakePool(15), None, 777, 2024)) == 1.0


def test_park_factor_neutral_when_venue_unknown():
    assert _run(_resolve_park_run_factor(_FakePool(None), _FakeCon(1.18), 777, 2024)) == 1.0


def test_park_factor_neutral_when_no_factor_row():
    assert _run(_resolve_park_run_factor(_FakePool(15), _FakeCon(None), 777, 2024)) == 1.0


def test_park_factor_clamps_degenerate_value():
    # A garbage factor outside the sane park range falls back to neutral.
    assert _run(_resolve_park_run_factor(_FakePool(15), _FakeCon(9.9), 777, 2024)) == 1.0


def test_park_factor_swallows_query_error():
    class _BoomCon:
        def execute(self, *_a, **_k):
            raise RuntimeError("duckdb down")

    assert _run(_resolve_park_run_factor(_FakePool(15), _BoomCon(), 777, 2024)) == 1.0
