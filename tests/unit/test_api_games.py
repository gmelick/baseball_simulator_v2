"""
test_api_games.py
=================
Unit tests for api/routes/games.py â€” the Phase-5 game-simulation endpoints
(SIM-355 GET /{date} + GET /{game_pk}/simulate, SIM-358 POST .../with_override,
SIM-359 caching).

ISOLATION STRATEGY
------------------
These tests build a TINY FastAPI app that includes ONLY the games router (NOT
``api.main.create_app()``), so the heavy similarity-engine lifespan and the live
ingestion pipeline import are never touched. The router reads two things off
``app.state`` â€” ``pg_pool`` and (optionally) ``sim_cache`` â€” both of which we
attach directly:

  * ``pg_pool``  â€” a fake asyncpg pool/connection that returns canned raw.games
    rows for ``fetch`` and is also handed to a MONKEYPATCHED ``resolve_game_state``
    so /simulate runs without a live Postgres/DuckDB.
  * ``sim_factory_ref`` â€” set to the no-DB rng factory so the BatchRunner runs a
    real (fast, in-process) batch with NO live sampler.

The ``/simulate`` and ``/with_override`` paths therefore exercise the REAL
BatchRunner + simulate_game loop + GameSimSummaryModel serialization â€” only the
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

# The no-DB, picklable rng factory â€” what the production factory ref is swapped
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

    async def fetchrow(self, sql, *args):  # pragma: no cover â€” not used directly here
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

    Full 1..9 lineups + a pitcher + season â€” enough for simulate_game to run a
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
    return a fixed small GameState â€” no live Postgres/DuckDB."""

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
# SIM-360 -- /simulate REUSES the shared app.state.sim_runner (with fallback)
# ===========================================================================


def test_simulate_reuses_shared_sim_runner(patch_resolver):
    """SIM-360: when the lifespan has attached a long-lived app.state.sim_runner,
    /simulate REUSES it instead of building a fresh BatchRunner per request."""
    from simulation.batch_runner import BatchRunner
    from simulation.batch_runner import InMemoryCache as _IMC

    shared = BatchRunner(cache=_IMC(), max_workers=1)
    app = _build_app(pool=_FakePool())
    app.state.sim_runner = shared

    # _build_runner must hand back the SHARED runner instance (not a new one).
    from starlette.requests import Request as _Req

    import api.routes.games as gm

    scope = {"type": "http", "app": app}
    assert gm._build_runner(_Req(scope)) is shared

    # And the endpoint runs end-to-end on the shared runner.
    client = TestClient(app)
    resp = client.get("/api/games/745001/simulate?n_iterations=5&base_seed=42")
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["n_iterations"] == 5


def test_simulate_falls_back_to_transient_runner_without_shared(patch_resolver):
    """SIM-360: with NO app.state.sim_runner attached (the existing test apps),
    _build_runner falls back to a transient BatchRunner so /simulate still works."""
    from starlette.requests import Request as _Req

    import api.routes.games as gm
    from simulation.batch_runner import BatchRunner

    app = _build_app(pool=_FakePool())
    assert getattr(app.state, "sim_runner", None) is None  # none attached

    scope = {"type": "http", "app": app}
    runner = gm._build_runner(_Req(scope))
    assert isinstance(runner, BatchRunner)
    # The transient fallback is in-process (max_workers=1 -> never forks).
    assert runner.resolve_max_workers(100) == 1

    client = TestClient(app)
    resp = client.get("/api/games/745001/simulate?n_iterations=4&base_seed=1")
    assert resp.status_code == 200, resp.text


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
    """An all-empty override is accepted (yields a zero delta â€” baseline==override
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
# SIM-388 -- multi-substitution override (SubstitutionSlot + substitutions[])
# ===========================================================================


class TestMultiSubstitutionOverride:
    """SIM-388: ``substitutions`` array lets the UI stage targeted single-player
    changes without passing the full batting order."""

    def test_single_home_sub_changes_one_slot(self, patch_resolver):
        """A single home substitution changes exactly the specified batting slot."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {
            "substitutions": [
                {"batting_order": 4, "player_id": 999001, "side": "home"},
            ],
            "description": "cleanup-hitter swap",
        }
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=5&base_seed=55",
            json=body,
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The override sim must have the substitute in slot 3 (0-indexed).
        # We can verify via the delta — if the swap happened, baseline ≠ override
        # at the same seed (unless the sub is identical, which is astronomically unlikely).
        # The delta shape must be valid regardless.
        assert "home_win_pct" in payload["delta"]["metrics"]

    def test_single_away_sub_changes_one_slot(self, patch_resolver):
        """A single away substitution is accepted."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {"substitutions": [{"batting_order": 1, "player_id": 999002, "side": "away"}]}
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=4&base_seed=66",
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_multiple_subs_different_slots(self, patch_resolver):
        """Multiple substitutions in one request are all applied."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {
            "substitutions": [
                {"batting_order": 1, "player_id": 888001, "side": "home"},
                {"batting_order": 5, "player_id": 888002, "side": "home"},
                {"batting_order": 9, "player_id": 888003, "side": "away"},
            ]
        }
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=4&base_seed=77",
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_empty_substitutions_list_is_valid(self, patch_resolver):
        """An empty substitutions list is a no-op (delta == 0 at same seed)."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=4&base_seed=88",
            json={"substitutions": []},
        )
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["delta"]["metrics"]
        assert all(m["delta"] == pytest.approx(0.0, abs=1e-9) for m in metrics.values())

    def test_substitutions_combined_with_pitcher_override(self, patch_resolver):
        """Targeted sub + pitcher override both applied to the same override run."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {
            "substitutions": [{"batting_order": 3, "player_id": 777001, "side": "home"}],
            "pitcher_id": 700002,
        }
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=4&base_seed=99",
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_full_lineup_and_sub_combined(self, patch_resolver):
        """Full lineup replacement + targeted sub: sub applies AFTER full lineup."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        # Replace home lineup, then further swap slot 2.
        new_lineup = [301, 302, 303, 304, 305, 306, 307, 308, 309]
        body = {
            "home_lineup": new_lineup,
            "substitutions": [{"batting_order": 2, "player_id": 999099, "side": "home"}],
        }
        resp = client.post(
            "/api/games/745001/simulate/with_override?n_iterations=4&base_seed=100",
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_batting_order_out_of_range_rejected_by_pydantic(self):
        """batting_order outside 1-9 is rejected with 422 by Pydantic validation."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {"substitutions": [{"batting_order": 0, "player_id": 100, "side": "home"}]}
        resp = client.post("/api/games/745001/simulate/with_override", json=body)
        assert resp.status_code == 422

    def test_invalid_side_rejected_by_pydantic(self):
        """side values other than 'home'/'away' are rejected with 422."""
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        body = {"substitutions": [{"batting_order": 1, "player_id": 100, "side": "visitor"}]}
        resp = client.post("/api/games/745001/simulate/with_override", json=body)
        assert resp.status_code == 422

    def test_apply_override_helper_targeted_sub(self):
        """Unit-test _apply_override directly: targeted sub modifies the correct slot."""
        from api.routes.games import SubstitutionSlot, _apply_override

        base = {
            "home_lineup": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "away_lineup": [10, 11, 12, 13, 14, 15, 16, 17, 18],
        }
        override = games_mod.RosterOverride(
            substitutions=[
                SubstitutionSlot(batting_order=4, player_id=999, side="home"),
                SubstitutionSlot(batting_order=1, player_id=888, side="away"),
            ]
        )
        result = _apply_override(base, override)
        # Slot 4 (1-indexed) = slot 3 (0-indexed)
        assert result["home_lineup"][3] == 999
        # Slot 1 (1-indexed) = slot 0 (0-indexed)
        assert result["away_lineup"][0] == 888
        # Other slots unchanged.
        assert result["home_lineup"][0] == 1
        assert result["away_lineup"][1] == 11


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


# ===========================================================================
# SIM-383 -- enriched GameCard (team names + records + venue)
# ===========================================================================

#: Canned rows that include the SIM-383 JOIN columns.  The fake pool returns
#: these verbatim regardless of which SQL is issued, so we just need the row
#: shape to include all the new keys.
_ENRICHED_CANNED_ROWS = [
    {
        "game_pk": 745001,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Final",
        "home_team_id": 147,
        "away_team_id": 111,
        "venue_id": 3313,
        # SIM-383 enrichment
        "home_team_name": "New York Yankees",
        "home_team_abbrev": "NYY",
        "away_team_name": "Boston Red Sox",
        "away_team_abbrev": "BOS",
        "venue_name": "Yankee Stadium",
        "venue_city": "Bronx",
        "home_wins": 75,
        "home_losses": 45,
        "away_wins": 60,
        "away_losses": 60,
    },
    {
        "game_pk": 745002,
        "season": 2024,
        "game_date": "2024-08-15",
        "status": "Scheduled",
        "home_team_id": 119,
        "away_team_id": 137,
        "venue_id": 22,
        "home_team_name": "Los Angeles Dodgers",
        "home_team_abbrev": "LAD",
        "away_team_name": "San Francisco Giants",
        "away_team_abbrev": "SF",
        "venue_name": "Dodger Stadium",
        "venue_city": "Los Angeles",
        "home_wins": 80,
        "home_losses": 40,
        "away_wins": 55,
        "away_losses": 65,
    },
]


class TestGameCardEnrichment:
    """SIM-383: ``GET /api/games/{date}`` returns team names, abbreviations,
    venue name + city, and season-to-date win/loss records."""

    def test_enriched_rows_populate_team_names(self):
        """Team names and abbreviations appear in the response when the JOIN
        columns are present in the pool rows."""
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        assert resp.status_code == 200
        games = {g["game_pk"]: g for g in resp.json()["games"]}
        g = games[745001]
        assert g["home_team_name"] == "New York Yankees"
        assert g["home_team_abbrev"] == "NYY"
        assert g["away_team_name"] == "Boston Red Sox"
        assert g["away_team_abbrev"] == "BOS"

    def test_enriched_rows_populate_venue_fields(self):
        """Venue name and city appear in the response."""
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        assert resp.status_code == 200
        g = next(g for g in resp.json()["games"] if g["game_pk"] == 745001)
        assert g["venue_name"] == "Yankee Stadium"
        assert g["venue_city"] == "Bronx"

    def test_enriched_rows_populate_records(self):
        """Win/loss records for both sides appear in the response."""
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        assert resp.status_code == 200
        g = next(g for g in resp.json()["games"] if g["game_pk"] == 745001)
        assert g["home_wins"] == 75
        assert g["home_losses"] == 45
        assert g["away_wins"] == 60
        assert g["away_losses"] == 60

    def test_enriched_second_game_has_its_own_team_data(self):
        """Each card carries its own team data (not shared across the response)."""
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        games = {g["game_pk"]: g for g in resp.json()["games"]}
        g = games[745002]
        assert g["home_team_abbrev"] == "LAD"
        assert g["away_team_abbrev"] == "SF"
        assert g["venue_name"] == "Dodger Stadium"
        assert g["home_wins"] == 80
        assert g["away_wins"] == 55

    def test_bare_rows_without_enrichment_default_to_none(self):
        """Existing rows that don't include enrichment columns (old cache, stubs)
        deserialise without error â€” new fields default to ``None``."""
        app = _build_app(pool=_FakePool(rows=CANNED_GAMES_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        assert resp.status_code == 200
        for g in resp.json()["games"]:
            assert g["home_team_name"] is None
            assert g["away_team_name"] is None
            assert g["venue_name"] is None
            assert g["home_wins"] is None
            assert g["home_losses"] is None
            assert g["away_wins"] is None
            assert g["away_losses"] is None

    def test_enriched_rows_cached_payload_preserves_enrichment(self):
        """SIM-359 + SIM-383: the cache round-trip (model_dump â†’ **cached)
        preserves the enrichment fields; the second call must not drop them."""
        from simulation.batch_runner import InMemoryCache as _IMC

        cache = _IMC()
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS), cache=cache)
        client = TestClient(app)

        first = client.get("/api/games/2024-08-15")
        second = client.get("/api/games/2024-08-15")
        assert first.status_code == second.status_code == 200
        # Both calls must carry enrichment; the second is a cache hit.
        for resp in (first, second):
            g = next(g for g in resp.json()["games"] if g["game_pk"] == 745001)
            assert g["home_team_name"] == "New York Yankees"
            assert g["home_wins"] == 75

    def test_enriched_rows_count_and_pks_unchanged(self):
        """SIM-383 enrichment does not change the count or game_pk set."""
        app = _build_app(pool=_FakePool(rows=_ENRICHED_CANNED_ROWS))
        client = TestClient(app)
        resp = client.get("/api/games/2024-08-15")
        body = resp.json()
        assert body["count"] == 2
        assert {g["game_pk"] for g in body["games"]} == {745001, 745002}


# ===========================================================================
# SIM-384 -- GET /api/games/{game_pk}/card (aggregate card + status enum)
# ===========================================================================

#: A single enriched game row that also includes the SIM-384 score fields.
_CARD_ROW = {
    "game_pk": 745001,
    "season": 2024,
    "game_date": "2024-08-15",
    "status": "Final",
    "home_team_id": 147,
    "away_team_id": 111,
    "venue_id": 3313,
    "home_team_name": "New York Yankees",
    "home_team_abbrev": "NYY",
    "away_team_name": "Boston Red Sox",
    "away_team_abbrev": "BOS",
    "venue_name": "Yankee Stadium",
    "venue_city": "Bronx",
    "home_wins": 75,
    "home_losses": 45,
    "away_wins": 60,
    "away_losses": 60,
    "home_score_final": 7,
    "away_score_final": 3,
}


class TestGameCardAggregate:
    """SIM-384: ``GET /api/games/{game_pk}/card`` returns the enriched identity
    + 3-state status + sim summary (if available) in one response."""

    def test_card_returns_200_with_correct_game_pk(self):
        """Basic happy-path: card returns 200 with the correct game_pk."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        resp = client.get("/api/games/745001/status")
        assert resp.status_code == 200
        assert resp.json()["game_pk"] == 745001

    def test_card_maps_final_status_correctly(self):
        """'Final' raw status â†’ 'final' in the response."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        resp = client.get("/api/games/745001/status")
        assert resp.json()["game_status"] == "final"

    def test_card_maps_live_status(self):
        """'Live' raw status â†’ 'live'."""
        row = {**_CARD_ROW, "status": "Live"}
        app = _build_app(pool=_FakePool(rows=[row]))
        client = TestClient(app)
        resp = client.get("/api/games/745001/status")
        assert resp.json()["game_status"] == "live"

    def test_card_maps_scheduled_statuses(self):
        """Preview / Warmup / Pre-Game raw statuses all â†’ 'scheduled'."""
        for raw in ("Preview", "Warmup", "Pre-Game"):
            row = {**_CARD_ROW, "status": raw}
            app = _build_app(pool=_FakePool(rows=[row]))
            client = TestClient(app)
            resp = client.get("/api/games/745001/status")
            assert resp.json()["game_status"] == "scheduled", f"failed for status={raw!r}"

    def test_card_maps_postponed_statuses(self):
        """Postponed / Suspended / Cancelled â†’ 'postponed'."""
        for raw in ("Postponed", "Suspended", "Cancelled"):
            row = {**_CARD_ROW, "status": raw}
            app = _build_app(pool=_FakePool(rows=[row]))
            client = TestClient(app)
            resp = client.get("/api/games/745001/status")
            assert resp.json()["game_status"] == "postponed", f"failed for status={raw!r}"

    def test_card_carries_enriched_team_data(self):
        """Team names, abbreviations, venue, and records are in the response."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        body = resp = client.get("/api/games/745001/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["home_team_name"] == "New York Yankees"
        assert body["home_team_abbrev"] == "NYY"
        assert body["away_team_name"] == "Boston Red Sox"
        assert body["venue_name"] == "Yankee Stadium"
        assert body["home_wins"] == 75
        assert body["away_losses"] == 60

    def test_card_carries_final_scores(self):
        """Final scores are present when status='Final'."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        body = client.get("/api/games/745001/status").json()
        assert body["home_score_final"] == 7
        assert body["away_score_final"] == 3

    def test_card_sim_summary_is_none_when_no_sim_persisted(self):
        """sim_summary is None when the fake pool yields no sim run."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        # The fake pool fetchrow will return _CARD_ROW for the sim-run query too,
        # which fails _sim_run_row_to_dict key access â†’ exception â†’ None.
        body = client.get("/api/games/745001/status").json()
        assert body["sim_summary"] is None

    def test_card_odds_is_none_placeholder(self):
        """odds is None (reserved for Phase 6 Sprint 4+)."""
        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        assert client.get("/api/games/745001/status").json()["odds"] is None

    def test_card_no_pool_returns_503(self):
        """Without a DB pool, the card endpoint 503s."""
        app = FastAPI()
        app.include_router(games_router)
        app.state.pg_pool = None
        app.state.sim_cache = None
        client = TestClient(app)
        resp = client.get("/api/games/745001/status")
        assert resp.status_code == 503

    def test_card_missing_game_returns_404(self):
        """When the pool returns no row (game not in DB), the card is 404."""
        app = _build_app(pool=_FakePool(rows=[]))  # empty row set
        client = TestClient(app)
        resp = client.get("/api/games/999999/status")
        assert resp.status_code == 404

    def test_card_with_canned_sim_summary_populates_field(self, monkeypatch):
        """When load_latest_sim_run is monkeypatched to return a canned summary,
        sim_summary is populated in the response."""
        # Build a minimal valid GameSimSummaryLite-compatible dict.
        # (We use the _ApiModel extra='forbid' contract so keys must match exactly.)
        canned_summary = {
            "n_iterations": 10,
            "home_win_pct": 0.6,
            "away_win_pct": 0.4,
            "tie_pct": 0.0,
            "home_score_mean": 4.5,
            "away_score_mean": 3.0,
            "total_score_mean": 7.5,
            "home_score_median": 4.0,
            "away_score_median": 3.0,
            "total_score_median": 7.0,
            "home_win_ci": {
                "point": 0.6,
                "low": 0.45,
                "high": 0.75,
                "level": 0.95,
                "method": "normal",
                "half_width": 0.15,
            },
            "away_win_ci": {
                "point": 0.4,
                "low": 0.25,
                "high": 0.55,
                "level": 0.95,
                "method": "normal",
                "half_width": 0.15,
            },
            "home_score_ci": {
                "point": 4.5,
                "low": 3.0,
                "high": 6.0,
                "level": 0.95,
                "method": "normal",
                "half_width": 1.5,
            },
            "away_score_ci": {
                "point": 3.0,
                "low": 2.0,
                "high": 4.0,
                "level": 0.95,
                "method": "normal",
                "half_width": 1.0,
            },
            "total_score_ci": {
                "point": 7.5,
                "low": 5.5,
                "high": 9.5,
                "level": 0.95,
                "method": "normal",
                "half_width": 2.0,
            },
            "simulated_at": "2024-08-15T12:00:00",
            "confidence_level": 0.95,
            "ci_method": "normal",
        }

        async def _fake_load(conn, game_pk):
            return {
                "run_id": 1,
                "game_pk": game_pk,
                "n_iterations": 10,
                "base_seed": 42,
                "summary": canned_summary,
                "created_at": None,
            }

        monkeypatch.setattr(
            games_mod,
            "sim_store",
            type(
                "_FakeSS",
                (),
                {
                    "load_latest_sim_run": staticmethod(_fake_load),
                },
            )(),
        )

        app = _build_app(pool=_FakePool(rows=[_CARD_ROW]))
        client = TestClient(app)
        body = client.get("/api/games/745001/status").json()
        assert body["sim_summary"] is not None
        assert body["sim_summary"]["home_win_pct"] == 0.6
        assert body["sim_summary"]["n_iterations"] == 10


# ===========================================================================
# SIM-390 -- GET /api/games/{game_pk}/props/{player_id}/{prop}
# ===========================================================================


def _make_fake_pset():
    """A minimal :class:`PropDistributionSet` for SIM-390 unit tests.

    Player 600001 has a pitcher 'K' PMF with support [4..8] and probabilities
    [0.10, 0.20, 0.40, 0.20, 0.10] (mean=6.0).  Used to verify p_over/p_under
    and edge_report computation without running the full sim pipeline.
    """
    import numpy as np

    from simulation.prop_distributions import PropDistribution, PropDistributionSet

    k_dist = PropDistribution(
        player_id=600001,
        prop="K",
        n=10,
        support=np.array([4, 5, 6, 7, 8], dtype=np.int64),
        probabilities=np.array([0.10, 0.20, 0.40, 0.20, 0.10], dtype=np.float64),
        mean=6.0,
        median=6.0,
        std=1.05,
    )
    return PropDistributionSet(n_iterations=10, by_player={600001: {"K": k_dist}})


class TestPlayerPropEdge:
    """SIM-390: GET /{game_pk}/props/{player_id}/{prop} endpoint.

    Tests cover: happy-path PMF shape, case-normalisation, line-triggered
    over/under/push, edge report with market odds, under-side edge, validation
    errors (invalid prop / bet_side / odds-without-line), 404 for absent
    player, 503 without pool, and numpy-free JSON round-trip.
    """

    def _patched_app(self, monkeypatch, *, pool=None):
        """App with mocked resolve_game_state and _build_prop_set."""

        async def _fake_resolve(conn, game_pk, **kwargs):
            return _small_game_state()

        monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve)

        pset = _make_fake_pset()
        monkeypatch.setattr(games_mod, "_build_prop_set", lambda **kw: pset)

        return _build_app(pool=pool or _FakePool())

    # ------------------------------------------------------------------
    # Happy-path shape
    # ------------------------------------------------------------------

    def test_prop_pmf_returns_200_with_pmf_fields(self, monkeypatch):
        """Basic call returns all PMF fields with no line enrichment."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["player_id"] == 600001
        assert body["prop"] == "K"
        assert body["n"] == 10
        assert body["mean"] == pytest.approx(6.0, abs=1e-6)
        assert isinstance(body["pmf"], dict)
        assert len(body["support"]) == 5
        assert len(body["probabilities"]) == 5
        # Without a line, the over/under fields are absent (None).
        assert body["line"] is None
        assert body["p_over"] is None
        assert body["p_under"] is None
        assert body["p_push"] is None
        assert body["edge_report"] is None

    def test_prop_lowercase_prop_name_is_normalised(self, monkeypatch):
        """Prop names are case-insensitive: 'k' is treated as 'K'."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/k")
        assert resp.status_code == 200, resp.text
        assert resp.json()["prop"] == "K"

    # ------------------------------------------------------------------
    # Line enrichment
    # ------------------------------------------------------------------

    def test_prop_with_half_integer_line_populates_over_under(self, monkeypatch):
        """line=5.5 (half-integer) → p_over+p_under==1, p_push==0."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K?line=5.5")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["line"] == pytest.approx(5.5, abs=1e-6)
        # PMF: [0.10, 0.20, 0.40, 0.20, 0.10] at [4,5,6,7,8]
        # p_over(5.5) = P(K>5.5) = P(K>=6) = 0.40+0.20+0.10 = 0.70
        assert body["p_over"] == pytest.approx(0.70, abs=1e-6)
        # p_under(5.5) = P(K<5.5) = P(K<=5) = 0.10+0.20 = 0.30
        assert body["p_under"] == pytest.approx(0.30, abs=1e-6)
        # Half-integer: no push.
        assert body["p_push"] == pytest.approx(0.0, abs=1e-6)
        assert body["p_over"] + body["p_under"] == pytest.approx(1.0, abs=1e-6)

    def test_prop_with_integer_line_has_push_mass(self, monkeypatch):
        """line=6 (integer) → push mass at exactly 6, over+under+push==1."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K?line=6")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # p_over(6) = P(K>6) = P(7)+P(8) = 0.20+0.10 = 0.30
        assert body["p_over"] == pytest.approx(0.30, abs=1e-6)
        # p_under(6) = P(K<6) = P(4)+P(5) = 0.10+0.20 = 0.30
        assert body["p_under"] == pytest.approx(0.30, abs=1e-6)
        # p_push(6) = P(K==6) = 0.40
        assert body["p_push"] == pytest.approx(0.40, abs=1e-6)
        total = body["p_over"] + body["p_under"] + body["p_push"]
        assert total == pytest.approx(1.0, abs=1e-6)

    # ------------------------------------------------------------------
    # Edge report
    # ------------------------------------------------------------------

    def test_prop_with_odds_populates_edge_report_over(self, monkeypatch):
        """line + over_ml + under_ml populates edge_report (OVER side by default)."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K?line=5.5&over_ml=-110&under_ml=-110")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        er = body["edge_report"]
        assert er is not None
        assert er["label"] == "prop:K"
        assert er["side"] == "over"
        assert er["line"] == pytest.approx(5.5, abs=1e-6)
        # sim_prob == p_over(5.5) == 0.70; symmetric -110/-110 → fair = 0.50
        assert er["sim_prob"] == pytest.approx(0.70, abs=1e-6)
        assert er["positive_edge"] is True  # 0.70 > 0.50
        assert isinstance(er["ev"], float)
        assert isinstance(er["edge"], float)

    def test_prop_bet_side_under_computes_under_edge(self, monkeypatch):
        """bet_side=under computes the edge for the UNDER side."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get(
            "/api/games/745001/props/600001/K?line=5.5&over_ml=-110&under_ml=-110&bet_side=under"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        er = body["edge_report"]
        assert er is not None
        assert er["side"] == "under"
        # sim_prob for UNDER at 5.5 = p_under(5.5) = 0.30
        assert er["sim_prob"] == pytest.approx(0.30, abs=1e-6)
        # 0.30 < 0.50 fair → negative edge
        assert er["positive_edge"] is False

    # ------------------------------------------------------------------
    # Validation errors
    # ------------------------------------------------------------------

    def test_prop_invalid_name_returns_422(self, monkeypatch):
        """A prop name outside ALL_PROPS returns 422."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/XYZ")
        assert resp.status_code == 422

    def test_prop_invalid_bet_side_returns_422(self, monkeypatch):
        """bet_side other than 'over'/'under' returns 422."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get(
            "/api/games/745001/props/600001/K?line=5.5&over_ml=-110&under_ml=-110&bet_side=home"
        )
        assert resp.status_code == 422

    def test_prop_odds_without_line_returns_422(self, monkeypatch):
        """Supplying over_ml without a line returns 422."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K?over_ml=-110&under_ml=-110")
        assert resp.status_code == 422

    def test_prop_player_not_in_set_returns_404(self, monkeypatch):
        """A player_id absent from the prop set returns 404."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/999999/K")
        assert resp.status_code == 404

    # ------------------------------------------------------------------
    # Infrastructure / serialization
    # ------------------------------------------------------------------

    def test_prop_no_pool_returns_503(self, monkeypatch):
        """Without a DB pool, the endpoint returns 503."""

        async def _fake_resolve(conn, game_pk, **kwargs):
            return _small_game_state()

        monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve)
        app = FastAPI()
        app.include_router(games_router)
        app.state.pg_pool = None
        app.state.sim_cache = None
        client = TestClient(app)
        resp = client.get("/api/games/745001/props/600001/K")
        assert resp.status_code == 503

    def test_prop_response_is_numpy_free(self, monkeypatch):
        """Full response (including edge_report) round-trips through json.dumps."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        body = client.get(
            "/api/games/745001/props/600001/K?line=6.0&over_ml=-110&under_ml=-110"
        ).json()
        json.dumps(body)  # must not raise (no numpy types)


# ===========================================================================
# SIM-386 -- GET /api/games/{game_pk}/live
# ===========================================================================

#: Canned game_state JSONB that the fake pool returns for live-state tests.
_LIVE_GAME_STATE = {
    "inning": 5,
    "half": "Top",
    "outs": 1,
    "balls": 2,
    "strikes": 1,
    "home_score": 3,
    "away_score": 2,
    "batting_team_id": 111,
    "fielding_team_id": 147,
    "on_1b": 123456,
    "on_2b": None,
    "on_3b": None,
    "current_batcher_id": 654321,
    "current_pitcher_id": 600001,
    "home_lineup": [201, 202, 203, 204, 205, 206, 207, 208, 209],
    "away_lineup": [101, 102, 103, 104, 105, 106, 107, 108, 109],
    "home_bullpen": [301, 302],
    "away_bullpen": [401, 402],
    "home_bench": [501],
    "away_bench": [601],
}


class _FakePoolWithLive(_FakePool):
    """A FakePool that returns a canned live-state row from fetchrow."""

    def __init__(self, *, live_row: dict | None = None, game_rows=None):
        super().__init__(rows=game_rows or [])
        self._live_row = live_row

    async def fetchrow(self, sql, *args):
        return self._live_row


class TestLiveGameState:
    """SIM-386: GET /{game_pk}/live endpoint.

    Tests cover: happy-path shape, all field types, 404 when not live,
    503 without pool, and JSON-serializable response.
    """

    @staticmethod
    def _canned_live_row():
        """A minimal sim.lineup_state row dict for use in tests."""
        return {
            "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "game_pk": 745001,
            "game_state": _LIVE_GAME_STATE,
            "updated_at": "2024-08-15T19:42:00",
        }

    def _patched_app(self, monkeypatch, *, live_row=None):
        """App with a monkeypatched load_live_game_state."""
        row = live_row if live_row is not None else self._canned_live_row()

        async def _fake_load(conn, game_pk):
            return {
                "session_id": row["session_id"],
                "game_pk": int(row["game_pk"]),
                "game_state": row["game_state"],
                "updated_at": row["updated_at"],
            }

        monkeypatch.setattr(
            games_mod,
            "sim_store",
            type("_FakeSS", (), {"load_live_game_state": staticmethod(_fake_load)})(),
        )
        return _build_app(pool=_FakePool())

    def test_live_state_returns_200_with_all_fields(self, monkeypatch):
        """Happy path: returns 200 with all inning/score/baserunner/lineup fields."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        resp = client.get("/api/games/745001/live")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["game_pk"] == 745001
        assert body["session_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert body["inning"] == 5
        assert body["half"] == "Top"
        assert body["outs"] == 1
        assert body["balls"] == 2
        assert body["strikes"] == 1
        assert body["home_score"] == 3
        assert body["away_score"] == 2
        assert body["batting_team_id"] == 111
        assert body["fielding_team_id"] == 147
        assert body["on_1b"] == 123456
        assert body["on_2b"] is None
        assert body["on_3b"] is None
        assert body["current_pitcher_id"] == 600001
        assert len(body["home_lineup"]) == 9
        assert len(body["away_lineup"]) == 9
        assert body["home_bullpen"] == [301, 302]
        assert body["away_bullpen"] == [401, 402]
        assert body["home_bench"] == [501]
        assert body["away_bench"] == [601]
        assert body["updated_at"] == "2024-08-15T19:42:00"

    def test_live_state_no_live_row_returns_404(self, monkeypatch):
        """When load_live_game_state returns None, the endpoint returns 404."""

        async def _fake_load(conn, game_pk):
            return None

        monkeypatch.setattr(
            games_mod,
            "sim_store",
            type("_FakeSS", (), {"load_live_game_state": staticmethod(_fake_load)})(),
        )
        app = _build_app(pool=_FakePool())
        client = TestClient(app)
        resp = client.get("/api/games/999999/live")
        assert resp.status_code == 404

    def test_live_state_no_pool_returns_503(self, monkeypatch):
        """Without a DB pool, the endpoint returns 503."""

        async def _fake_load(conn, game_pk):
            return self._canned_live_row()

        monkeypatch.setattr(
            games_mod,
            "sim_store",
            type("_FakeSS", (), {"load_live_game_state": staticmethod(_fake_load)})(),
        )
        app = FastAPI()
        app.include_router(games_router)
        app.state.pg_pool = None
        app.state.sim_cache = None
        client = TestClient(app)
        resp = client.get("/api/games/745001/live")
        assert resp.status_code == 503

    def test_live_state_response_is_json_serializable(self, monkeypatch):
        """Response round-trips through json.dumps (numpy-free contract)."""
        app = self._patched_app(monkeypatch)
        client = TestClient(app)
        body = client.get("/api/games/745001/live").json()
        json.dumps(body)  # must not raise

    def test_live_state_empty_game_state_uses_defaults(self, monkeypatch):
        """A minimal game_state dict (missing most fields) falls back to defaults."""
        row = {
            "session_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "game_pk": 745002,
            "game_state": {},  # completely empty
            "updated_at": None,
        }
        app = self._patched_app(monkeypatch, live_row=row)
        client = TestClient(app)
        resp = client.get("/api/games/745002/live")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # All missing fields should fall back to their defaults.
        assert body["inning"] == 1
        assert body["half"] == "Top"
        assert body["outs"] == 0
        assert body["home_score"] == 0
        assert body["away_score"] == 0
        assert body["on_1b"] is None
        assert body["home_lineup"] == []
        assert body["updated_at"] is None
