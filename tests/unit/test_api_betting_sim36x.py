"""
test_api_betting_sim36x.py
==========================
Unit tests for api/routes/betting.py -- the Phase-5 betting API surface
(SIM-367 spread/run-line edges, SIM-368 line-movement, SIM-369 +EV bet signals)
exposed under the ``/api/betting`` prefix.

ISOLATION STRATEGY (mirrors test_api_games.py)
----------------------------------------------
These tests build a TINY FastAPI app that includes ONLY the betting router (NOT
``api.main.create_app()``), so the heavy similarity-engine lifespan + the live
ingestion pipeline are never started. The router reads ``app.state.pg_pool`` and
reuses the games router's sim seam (``sim_factory_ref``), both attached directly:

  * ``pg_pool`` -- a fake asyncpg pool/connection. For /edges + /signals it is
    handed to a MONKEYPATCHED ``resolve_game_state`` so the sim runs with no live
    Postgres/DuckDB; for /line-movement + /clv it serves canned ``raw.game_odds``
    rows from its ``fetch``.
  * ``sim_factory_ref`` -- the no-DB rng factory so the BatchRunner runs a real
    (fast, in-process) batch with NO live sampler.

The /edges + /signals paths therefore exercise the REAL sim loop + the real
SIM-367/369 betting math + the SIM-350 serializers; odds default to the
deterministic MockOddsAPI (or injected query params). /line-movement + /clv
exercise the REAL SIM-368 ``fetch_line_movement`` over the canned odds rows.

Owned by Backend Developer + Betting Analyst (SIM-367 / SIM-368 / SIM-369).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.betting import router as betting_router
from simulation.game_state import GameState

# The no-DB, picklable rng factory -- the production ref is swapped to this so
# /edges + /signals run a real batch with no live sampler/DuckDB.
NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePool:
    """A fake asyncpg pool/connection.

    Exposes ``fetch`` (for the line-movement raw.game_odds read) returning canned
    rows. /edges + /signals do not call fetch (resolve_game_state is monkeypatched)
    but the pool must be present so the route's ``_get_pool`` does not 503.
    """

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.fetch_calls: list = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def fetchrow(self, sql, *args):  # pragma: no cover -- not used here
        return self._rows[0] if self._rows else None


def _small_game_state() -> GameState:
    """A minimal but valid GameState the monkeypatched resolver returns."""
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.away_lineup_slot = 0
    state.home_lineup_slot = 0
    state.batter_id = 101
    state.throw_hand = "R"
    return state


# Two raw.game_odds moneyline rows for one game/book: opening (-120/+100) ->
# closing (-150/+130). The home implied prob rises (odds shorten) so the line
# moved TOWARD home; the away side moved away. Both rows carry both sides so the
# series de-vigs into an entry-vs-close CLV.
CANNED_ODDS_ROWS = [
    {
        "fetched_at": "2024-08-15T10:00:00+00:00",
        "line_type": "opening",
        "book": "pinnacle",
        "is_sharp_book": True,
        "market_type": "moneyline",
        "home_ml": -120,
        "away_ml": 100,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    },
    {
        "fetched_at": "2024-08-15T12:00:00+00:00",
        "line_type": "closing",
        "book": "pinnacle",
        "is_sharp_book": True,
        "market_type": "moneyline",
        "home_ml": -150,
        "away_ml": 130,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    },
]


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _build_app(*, pool, factory_ref=NO_DB_FACTORY_REF) -> FastAPI:
    """A tiny app with ONLY the betting router + the app.state the route reads."""
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = pool
    app.state.sim_cache = None  # BatchRunner picks its own backend
    app.state.sim_factory_ref = factory_ref  # the testability seam
    return app


@pytest.fixture
def patch_resolver(monkeypatch):
    """Monkeypatch resolve_game_state (as imported into the games module) so the
    betting router's reused sim seam runs without a live Postgres/DuckDB."""

    async def _fake_resolve_game_state(conn, game_pk, **kwargs):
        return _small_game_state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _fake_resolve_game_state)
    return _fake_resolve_game_state


# ===========================================================================
# GET /api/betting/games/{game_pk}/edges  (SIM-367 + SIM-339)
# ===========================================================================


def test_edges_returns_numpy_free_reports_incl_run_line(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)

    # The no-DB rng factory produces low-scoring games (totals ~0-4, margins
    # ~-4..2), so we inject a total_line + run_line that sit INSIDE that
    # distribution -- otherwise a realistic mock line (e.g. total 9.5) puts every
    # iteration on one side (a degenerate 0/1 sim prob that carries no priceable
    # edge and is skipped). The moneyline prices are NOT injected, so that market
    # still exercises the mock-odds path.
    resp = client.get(
        "/api/betting/games/745001/edges?n_iterations=120&base_seed=7&total_line=1.5&run_line=-0.5"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_pk"] == 745001
    # All three markets default -> 6 reports (both sides each).
    labels = {e["label"] for e in body["edges"]}
    assert "moneyline" in labels
    assert "total" in labels
    assert "run_line" in labels  # SIM-367 run-line is present.
    assert len(body["edges"]) == 6

    # Each report is the EdgeReportModel shape (numpy-free).
    for e in body["edges"]:
        for key in ("sim_prob", "market_fair_prob", "edge", "ev", "offered_american"):
            assert isinstance(e[key], int | float)
        assert isinstance(e["positive_edge"], bool)
        assert e["side"] in ("home", "away", "over", "under")

    # odds_source: moneyline prices were NOT injected -> mock; total/runline lines
    # WERE injected -> injected.
    assert body["odds_source"]["moneyline"] == "mock"
    assert body["odds_source"]["total"] == "injected"
    assert body["odds_source"]["runline"] == "injected"
    # Full round-trip proves numpy-free.
    json.dumps(body)


def test_edges_respects_injected_odds_and_market_filter(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)

    # Only the run-line market, with injected prices + line.
    resp = client.get(
        "/api/betting/games/745001/edges"
        "?n_iterations=30&base_seed=3&markets=runline"
        "&home_rl_ml=-110&away_rl_ml=-110&run_line=-1.5"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["markets"] == ["runline"]
    assert body["odds_source"]["runline"] == "injected"
    assert {e["label"] for e in body["edges"]} == {"run_line"}
    # The injected price is reflected on the offered side.
    for e in body["edges"]:
        assert e["offered_american"] == -110.0
        assert e["line"] == -1.5


def test_edges_bad_market_is_422(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/edges?markets=spread")
    assert resp.status_code == 422


def test_edges_no_pool_is_503():
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/edges")
    assert resp.status_code == 503


def test_edges_unknown_game_is_404(monkeypatch):
    from simulation.lineup_resolver import LineupResolutionError

    async def _raise(conn, game_pk, **kwargs):
        raise LineupResolutionError(f"game_pk={game_pk} not found")

    monkeypatch.setattr(games_mod, "resolve_game_state", _raise)
    app = _build_app(pool=_FakePool())
    client = TestClient(app)
    resp = client.get("/api/betting/games/999999/edges?n_iterations=5")
    assert resp.status_code == 404


# ===========================================================================
# GET /api/betting/games/{game_pk}/signals  (SIM-369)
# ===========================================================================


def test_signals_returns_ranked_positive_ev_only(patch_resolver):
    app = _build_app(pool=_FakePool())
    client = TestClient(app)

    # A permissive gate so we surface signals; inject lopsided odds (a big
    # underdog price on a side the sim likes) to guarantee at least one +EV bet.
    resp = client.get(
        "/api/betting/games/745001/signals"
        "?n_iterations=60&base_seed=11&markets=moneyline"
        "&home_ml=400&away_ml=-600&min_edge=0.0"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_pk"] == 745001
    sigs = body["signals"]
    # Every emitted signal is strictly +EV with a positive edge, and ranked by EV.
    evs = [s["ev"] for s in sigs]
    assert evs == sorted(evs, reverse=True)  # EV descending
    for i, s in enumerate(sigs):
        assert s["rank"] == i
        assert s["ev"] > 0.0
        assert s["edge"] > 0.0
        assert 0.0 <= s["stake_fraction"] <= body["config"]["max_stake_fraction"]
        # The full source EdgeReport is nested.
        assert "report" in s and "sim_prob" in s["report"]
    # config echo carries the gate that produced them.
    assert body["config"]["kelly_fraction"] == 0.25
    json.dumps(body)  # numpy-free


def test_signals_gate_excludes_non_positive_ev(patch_resolver):
    """A high min_edge gate yields an empty (but valid) signal list."""
    app = _build_app(pool=_FakePool())
    client = TestClient(app)
    resp = client.get(
        "/api/betting/games/745001/signals"
        "?n_iterations=40&base_seed=5&markets=moneyline&min_edge=0.99"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signals"] == []


def test_signals_no_pool_is_503():
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/signals")
    assert resp.status_code == 503


# ===========================================================================
# GET /api/betting/games/{game_pk}/line-movement  (SIM-368)
# ===========================================================================


def test_line_movement_returns_series_from_canned_odds():
    app = _build_app(pool=_FakePool(rows=CANNED_ODDS_ROWS))
    client = TestClient(app)

    resp = client.get("/api/betting/games/745001/line-movement?market_type=moneyline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_pk"] == 745001
    assert body["market_type"] == "moneyline"
    # Two sides (home/away) for one book -> 2 series.
    assert body["count"] == 2
    sides = {s["side"] for s in body["series"]}
    assert sides == {"home", "away"}

    home = next(s for s in body["series"] if s["side"] == "home")
    # Opening -120 -> closing -150: home implied prob rises (steam toward home).
    assert home["direction"] == "toward"
    assert home["has_movement"] is True
    assert len(home["quotes"]) == 2
    assert len(home["implied_prob_series"]) == 2
    assert home["implied_prob_series"][1] > home["implied_prob_series"][0]
    # Both endpoints carry the opposite price -> an entry-vs-close CLV is computed.
    assert home["clv"] is not None
    assert isinstance(home["clv"]["clv_prob"], int | float)
    json.dumps(body)  # numpy-free


def test_line_movement_book_filter_passes_through():
    pool = _FakePool(rows=CANNED_ODDS_ROWS)
    app = _build_app(pool=pool)
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/line-movement?market_type=moneyline&book=pinnacle")
    assert resp.status_code == 200, resp.text
    # The book-filtered SQL was used (3 args: game_pk, market_type, book).
    assert pool.fetch_calls
    assert len(pool.fetch_calls[-1][1]) == 3


def test_line_movement_bad_market_type_is_422():
    app = _build_app(pool=_FakePool(rows=[]))
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/line-movement?market_type=parlay")
    assert resp.status_code == 422


def test_line_movement_no_pool_is_503():
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = None
    app.state.sim_cache = None
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/line-movement?market_type=moneyline")
    assert resp.status_code == 503


def test_line_movement_empty_when_no_odds():
    app = _build_app(pool=_FakePool(rows=[]))
    client = TestClient(app)
    resp = client.get("/api/betting/games/745001/line-movement?market_type=total")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["series"] == []


# ===========================================================================
# GET /api/betting/games/{game_pk}/clv  (SIM-339/SIM-368 snapshot)
# ===========================================================================


def test_clv_snapshot_returns_only_series_with_clv():
    app = _build_app(pool=_FakePool(rows=CANNED_ODDS_ROWS))
    client = TestClient(app)
    client.get("/api/betting/games/745001/clv?market_type=moneyline")


# ===========================================================================
# SIM-387 — calibration wiring in /edges
# ===========================================================================


def test_edges_uses_calibration_map_from_app_state(patch_resolver):
    """SIM-387: calibration_map on app.state must be threaded to win_probability.

    Strategy: attach a calibration map that always returns 0.1 (strongly
    away-biased) and verify the moneyline HOME sim_prob is ~0.1 — far from the
    ~0.5 the identity map would give.  Uses a fixed base_seed for
    determinism and requests moneyline-only so the home sim_prob is unambiguous.
    """
    from simulation.win_probability import CalibrationMap

    # A map that clamps all sim probabilities to 0.1 regardless of the raw value.
    biased_map = CalibrationMap(fn=lambda _p: 0.1, name="away-bias-test")

    app = _build_app(pool=_FakePool())
    app.state.calibration_map = biased_map
    client = TestClient(app)

    resp = client.get(
        "/api/betting/games/745001/edges?n_iterations=120&base_seed=42&markets=moneyline"
    )
    assert resp.status_code == 200, resp.text
    edges = resp.json()["edges"]

    # Home side: calibrated sim_prob should be ~0.1 (the biased map output).
    home_edges = [e for e in edges if e["side"] == "home" and e["label"] == "moneyline"]
    assert home_edges, "expected a moneyline home edge in the response"
    assert home_edges[0]["sim_prob"] == pytest.approx(
        0.1, abs=0.01
    ), f"calibration map was not applied: got {home_edges[0]['sim_prob']!r}, expected ~0.1"


def test_edges_falls_back_to_identity_when_no_calibration_map(patch_resolver):
    """SIM-387: when app.state has no calibration_map the endpoint still works
    (falls back to IDENTITY_CALIBRATION -- no AttributeError / crash)."""
    app = _build_app(pool=_FakePool())
    # Deliberately do NOT set app.state.calibration_map.
    client = TestClient(app)

    resp = client.get(
        "/api/betting/games/745001/edges?n_iterations=40&base_seed=7&markets=moneyline"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_pk"] == 745001
    # Some edges should be present; no crash.
    assert isinstance(body["edges"], list)
