"""
test_api_games.py
=================
Unit tests for api/routes/games.py — the Phase-5 game-simulation endpoints
(SIM-355 GET /{date} + GET /{game_pk}/simulate, SIM-358 POST .../with_override,
SIM-359 caching).

ISOLATION STRATEGY
------------------
These tests build a TINY FastAPI app that includes ONLY the games router (NOT
``api.main.create_app()``), so the heavy similarity-engine lifespan and the live
ingestion pipeline import are never touched. The router reads two things off
``app.state`` — ``pg_pool`` and (optionally) ``sim_cache`` — both of which we
attach directly:

  * ``pg_pool``  — a fake asyncpg pool/connection that returns canned raw.games
    rows for ``fetch`` and is also handed to a MONKEYPATCHED ``resolve_game_state``
    so /simulate runs without a live Postgres/DuckDB.
  * ``sim_factory_ref`` — set to the no-DB rng factory so the BatchRunner runs a
    real (fast, in-process) batch with NO live sampler.

The ``/simulate`` and ``/with_override`` paths therefore exercise the REAL
BatchRunner + simulate_game loop + GameSimSummaryModel serialization — only the
DB (lineup resolution) and the production sampler are stubbed.

Owned by Backend Developer (SIM-355 / SIM-358 / SIM-359).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.games import router as games_router
from simulation.batch_runner import InMemoryCache
from simulation.game_state import GameState

# The no-DB, picklable rng factory — what the production factory ref is swapped
# to so /simulate runs a real batch with no live sampler/DuckDB.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePool:
    """A fake asyncpg pool/connection.

    Exposes ``fetch`` (for the date listing) returning canned rows, and
    ``fetchrow`` / ``acquire`` so it can stand in as both a pool and a
    connection. The /simulate path does not actually call fetch on it (we
    monkeypatch resolve_game_state), but it must be present so the route's
    ``_get_pool`` does not 503.
    """

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.fetch_calls: list = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def fetchrow(self, sql, *args):  # pragma: no cover — not used directly here
        return self._rows[0] if self._rows else None


CANNED_GAMES_ROWS = [
    {
        "game_pk": 745001,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Final",
        "home_team_id": 147,
        "away_team_id": 111,
        "venue_id": 3313,
    },
    {
        "game_pk": 745002,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Scheduled",
        "home_team_id": 119,
        "away_team_id": 137,
        "venue_id": 22,
    },
]


def _small_game_state() -> GameState:
    """A minimal but valid GameState the monkeypatched resolver returns.

    Full 1..9 lineups + a pitcher + season — enough for simulate_game to run a
    real (no-DB) game via the rng factory.
    """
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _build_app(*, pool, cache=None, factory_ref=NO_DB_FACTORY_REF) -> FastAPI:
    """A tiny app with ONLY the games router + the app.state the route reads."""
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = pool
    app.state.sim_cache = cache  # None => BatchRunner picks its own backend
    app.state.sim_factory_ref = factory_ref  # the testability seam
    return app


@pytest.fixture
def patch_resolver(monkeypatch):
    """Monkeypatch resolve_game_state (as imported into the games module) to
    return a fixed small GameState — no live Postgres/DuckDB."""

    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


# ===========================================================================
# GET /api/games/{date}  (SIM-355)
# ===========================================================================


def test_games_on_date_returns_canned_games():
    app = _build_app(pool=_FakePool(rows=CANNED_GAMES_ROWS))
    client = TestClient(app)

    resp = client.get("/api/games/2024-08-15")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-08-15"
    assert body["count"] == 2
    pks = {g["game_pk"] for g in body["games"]}
    assert pks == {745001, 745002}
    # Field shape is faithful.
    g0 = next(g for g in body["games"] if g["game_pk"] == 745001)
    assert g0["status"] == "Final"
    assert g0["home_team_id"] == 147
    assert g0["away_team_id"] == 111
    assert g0["venue_id"] == 3313
    assert g0["season"] == 2024


def test_games_on_date_empty_list():
    app = _build_app(pool=_FakePool(rows=[]))
    client = TestClient(app)
    resp = client.get("/api/games/2024-01-01")
    assert resp.status_code == 200
    assert resp.json() == {"date": "2024-01-01", "count": 0, "games": []}


def test_games_on_date_bad_date_is_422():
    app = _build_app(pool=_FakePool(rows=[]))
    client = TestClient(app)
    resp = client.get("/api/games/not-a-date")
    assert resp.status_code == 422


def test_games_on_date_no_pool_is_503():
    """No pg_pool attached -> 503 (the route's _get_pool contract)."""
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.get("/api/games/2024-08-15")
    assert resp.status_code == 503


def test_games_on_date_uses_listing_cache():
    """SIM-359: a second call with a shared cache serves from the cache and does
    NOT re-query the pool."""
    pool = _FakePool(rows=CANNED_GAMES_ROWS)
    cache = InMemoryCache()
    app = _build_app(pool=pool, cache=cache)
    client = TestClient(app)

    first = client.get("/api/games/2024-08-15")
    second = client.get("/api/games/2024-08-15")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    # Only the FIRST call hit the pool; the second was a cache hit.
    assert len(pool.fetch_calls) == 1


# ===========================================================================
# GET /api/games/{game_pk}/simulate  (SIM-355 + SIM-359)
# ===========================================================================


def test_simulate_returns_numpy_free_summary(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)

    resp = client.get("/api/games/745001/simulate?n_iterations=5&base_seed=42")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_pk"] == 745001
    assert body["n_iterations"] == 5
    assert body["base_seed"] == 42
    summary = body["summary"]
    # The GameSimSummaryModel shape (SIM-350).
    assert summary["n_iterations"] == 5
    for key in ("home_win_pct", "away_win_pct", "home_score_mean", "total_score_mean"):
        assert isinstance(summary[key], (int, float))
    # The raw per-iteration arrays are present and JSON-native (no numpy).
    assert isinstance(summary["home_scores"], list)
    assert len(summary["home_scores"]) == 5
    assert all(isinstance(x, int) for x in summary["home_scores"])
    # CIs are nested models.
    assert "point" in summary["home_win_ci"]
    # Full round-trip through json proves numpy-free.
    json.dumps(body)


def test_simulate_is_deterministic_for_fixed_seed(patch_resolver):
    app = _build_app(pool=_FakePool(), cache=None)
    client = TestClient(app)
    # use_cache=false on both so we exercise the real recompute path twice.
    a = client.get("/api/games/745001/simulate?n_iterations=8&base_seed=7&use_cache=false")
    b = client.get("/api/games/745001/simulate?n_iterations=8&base_seed=7&use_cache=false")
    assert a.status_code == b.status_code == 200
    assert a.json()["summary"]["home_scores"] == b.json()["summary"]["home_scores"]


def test_simulate_cache_path_does_not_break_second_call(patch_resolver):
    """SIM-359: a shared sim cache memoizes the summary; the second call returns
    from_cache=True and an identical summary."""
    cache = InMemoryCache()
    app = _build_app(pool=_FakePool(), cache=cache)
    client = TestClient(app)

    first = client.get("/api/games/745001/simulate?n_iterations=6&base_seed=3")
    second = client.get("/api/games/745001/simulate?n_iterations=6&base_seed=3")
    assert first.status_code == second.status_code == 200
    assert first.json()["from_cache"] is False
    assert second.json()["from_cache"] is True
    assert first.json()["summary"] == second.json()["summary"]


def test_simulate_no_pool_is_503():
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.get("/api/games/745001/simulate")
    assert resp.status_code == 503


def test_simulate_unknown_game_is_404(monkeypatch):
    """A LineupResolutionError from the resolver -> 404, not 500."""
    from simulation.lineup_resolver import LineupResolutionError

    async def _raise(conn, game_pk, **kwargs):
        raise LineupResolutionError(f"game_pk={game_pk} not found")

    monkeypatch.setattr(games_mod, "resolve_game_state", _raise)
    app = _build_app(pool=_FakePool())
    client = TestClient(app)
    resp = client.get("/api/games/999999/simulate?n_iterations=3")
    assert resp.status_code == 404


# ===========================================================================
# POST /api/games/{game_pk}/simulate/with_override  (SIM-358)
# ===========================================================================


def test_with_override_returns_baseline_override_delta(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)

    body = {
        "home_lineup": [301, 302, 303, 304, 305, 306, 307, 308, 309],
        "pitcher_id": 700002,
        "description": "swap home lineup + ace",
    }
    resp = client.post(
        "/api/games/745001/simulate/with_override?n_iterations=6&base_seed=11",
        json=body,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["game_pk"] == 745001
    assert payload["n_iterations"] == 6
    # Baseline + override are both full GameSimSummaryModels.
    assert payload["baseline"]["n_iterations"] == 6
    assert payload["override"]["n_iterations"] == 6
    # The delta carries a metrics map keyed by the tracked field names.
    metrics = payload["delta"]["metrics"]
    assert "home_win_pct" in metrics
    assert "total_score_mean" in metrics
    md = metrics["home_win_pct"]
    # delta == override - baseline.
    assert md["delta"] == pytest.approx(md["override"] - md["baseline"], abs=1e-9)
    assert payload["delta"]["description"] == "swap home lineup + ace"
    json.dumps(payload)  # numpy-free


def test_with_override_empty_body_is_valid(patch_resolver):
    """An all-empty override is accepted (yields a zero delta — baseline==override
    at the same seed)."""
    app = _build_app(pool=_FakePool())
    client = TestClient(app)
    resp = client.post(
        "/api/games/745001/simulate/with_override?n_iterations=5&base_seed=9",
        json={},
    )
    assert resp.status_code == 200, resp.text
    metrics = resp.json()["delta"]["metrics"]
    # Same lineup + same seed => baseline and override summaries are identical.
    assert all(m["delta"] == pytest.approx(0.0, abs=1e-9) for m in metrics.values())


def test_with_override_no_pool_is_503():
    app = FastAPI()
    app.include_router(games_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.post("/api/games/745001/simulate/with_override", json={})
    assert resp.status_code == 503


# ===========================================================================
# Factory-ref seam (SIM-355)
# ===========================================================================


def test_resolve_factory_ref_precedence(monkeypatch):
    """app.state override wins over env, which wins over the module default."""
    from types import SimpleNamespace

    class _Req:
        def __init__(self, state):
            self.app = SimpleNamespace(state=state)

    # 1. app.state.sim_factory_ref wins.
    req = _Req(SimpleNamespace(sim_factory_ref="a.b:c"))
    monkeypatch.setenv("SIM_MACHINE_FACTORY_REF", "d.e:f")
    assert games_mod.resolve_factory_ref(req) == "a.b:c"

    # 2. env wins when no app.state override.
    req2 = _Req(SimpleNamespace(sim_factory_ref=None))
    assert games_mod.resolve_factory_ref(req2) == "d.e:f"

    # 3. module default when neither set.
    monkeypatch.delenv("SIM_MACHINE_FACTORY_REF", raising=False)
    req3 = _Req(SimpleNamespace(sim_factory_ref=None))
    assert games_mod.resolve_factory_ref(req3) == games_mod.PRODUCTION_FACTORY_REF
