"""The live betting pages price a run line by its shape.

A run line is ONE bet with two sides only when the away spread is the negative
of the home spread (home −1.5 / away +1.5). The book also lists two SEPARATE bets
(home −1.5 / away −1.5, or home +1.5 / away +1.5); pairing their prices
mis-prices each side (the run lines scored by shape, SIM-549). The accuracy
comparison applies the same rule; these tests pin it on the live surface:

  * the shared primitives in ``betting.bet_signal``;
  * the ``/edges`` and ``/signals`` routes: an injected away spread, each report
    at its own side's spread, and ``run_line_pricing`` in the response.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.betting import router as betting_router
from betting.bet_signal import (
    DEFAULT_ONE_SIDED_MARGIN,
    MarketSide,
    devig_one_sided,
    edge_report,
    implied_prob_from_american,
    reference_margin_from_prices,
    run_line_bet_cover_prob,
    run_line_is_pair,
    spread_cover_prob,
)
from simulation.game_state import GameState

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


def _imp(a: float) -> float:
    return implied_prob_from_american(a)


# ===========================================================================
# The shared primitives
# ===========================================================================


class TestPrimitives:
    def test_the_mirror_rule(self):
        assert run_line_is_pair(-1.5, 1.5) and run_line_is_pair(0.0, 0.0)
        assert not run_line_is_pair(-1.5, -1.5)
        assert not run_line_is_pair(0.5, -1.5)
        assert not run_line_is_pair(-1.5, None)

    def test_reference_margin_from_prices(self):
        margin, source = reference_margin_from_prices(
            [("total", -110.0, -110.0), ("moneyline", -150.0, 130.0)]
        )
        assert source == "total" and margin == pytest.approx(2 * _imp(-110.0))
        # a candidate outside the band (0.6 + 0.6) is skipped for the next
        margin, source = reference_margin_from_prices(
            [("total", -150.0, -150.0), ("moneyline", -150.0, 130.0)]
        )
        assert source == "moneyline"
        assert reference_margin_from_prices([("total", None, -110.0)]) == (
            DEFAULT_ONE_SIDED_MARGIN,
            "flat",
        )

    def test_devig_one_sided(self):
        assert devig_one_sided(350.0, 1.069) == pytest.approx((100 / 450) / 1.069)
        with pytest.raises(ValueError):
            devig_one_sided(350.0, 0.9)

    def test_the_bet_cover_prob_takes_each_side_at_its_own_spread(self):
        margins = [-3, -2, -1, 0, 1, 2, 3]
        assert run_line_bet_cover_prob(margins, MarketSide.HOME, -1.5) == pytest.approx(2 / 7)
        # away -1.5 covers when the away team wins by two or more
        assert run_line_bet_cover_prob(margins, MarketSide.AWAY, -1.5) == pytest.approx(2 / 7)
        assert run_line_bet_cover_prob(margins, MarketSide.AWAY, 1.5) == spread_cover_prob(
            margins, -1.5, MarketSide.AWAY
        )

    def test_one_sided_edge_report(self):
        rep = edge_report(
            label="run_line",
            side=MarketSide.AWAY,
            line=-1.5,
            sim_prob=0.4,
            offered_american=150.0,
            fair_prob=0.374,
        )
        assert rep.line == -1.5 and rep.market_fair_prob == 0.374
        assert rep.edge == pytest.approx(0.4 - 0.374)
        with pytest.raises(ValueError):  # a certain probability has no fair price
            edge_report(
                label="run_line",
                side=MarketSide.HOME,
                line=-1.5,
                sim_prob=1.0,
                offered_american=150.0,
                fair_prob=0.3,
            )


# ===========================================================================
# The /edges and /signals routes
# ===========================================================================


class _FakePool:
    async def fetch(self, sql, *args):
        return []


def _state() -> GameState:
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.batter_id = 101
    state.throw_hand = "R"
    return state


@pytest.fixture
def client(monkeypatch):
    async def _resolve(conn, game_pk, **kwargs):
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _resolve)
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = _FakePool()
    app.state.sim_cache = None
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    return TestClient(app)


class TestEdgesRoute:
    def test_two_separate_bets_price_each_side_on_its_own(self, client):
        resp = client.get(
            "/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=runline"
            "&home_rl_ml=350&away_rl_ml=150&run_line=-0.5&away_run_line=-0.5"
            "&over_ml=-109&under_ml=-121"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        margin = _imp(-109) + _imp(-121)
        pricing = body["run_line_pricing"]
        assert pricing["shape"] == "two_bets"
        assert pricing["reference_source"] == "total"
        assert pricing["reference_margin"] == pytest.approx(margin)
        by_side = {e["side"]: e for e in body["edges"]}
        assert by_side["home"]["line"] == -0.5 and by_side["away"]["line"] == -0.5
        assert by_side["home"]["market_fair_prob"] == pytest.approx(_imp(350) / margin)
        assert by_side["away"]["market_fair_prob"] == pytest.approx(_imp(150) / margin)
        assert body["odds_source"]["runline"] == "injected"

    def test_a_pair_carries_each_sides_own_spread(self, client):
        resp = client.get(
            "/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=runline"
            "&home_rl_ml=-110&away_rl_ml=-110&run_line=-0.5"
        )
        body = resp.json()
        assert body["run_line_pricing"]["shape"] == "pair"
        assert {e["side"]: e["line"] for e in body["edges"]} == {"home": -0.5, "away": 0.5}
        for e in body["edges"]:
            assert e["market_fair_prob"] == pytest.approx(0.5)

    def test_signals_carry_the_pricing_too(self, client):
        resp = client.get(
            "/api/betting/games/745001/signals?n_iterations=60&base_seed=7&markets=runline"
            "&home_rl_ml=350&away_rl_ml=150&run_line=-0.5&away_run_line=-0.5"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["run_line_pricing"]["shape"] == "two_bets"
