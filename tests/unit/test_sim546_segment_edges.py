"""SIM-546 — the twelve segment and team markets on /edges and /signals.

The edge route prices every game market the book posts, not only the three
full-game markets. These tests pin the route's side of that change (the
design's tests 7 to 14 and 23):

  * the default request prices all fifteen markets from the mock, every side
    of each, each report labelled with its market type, and names them;
  * a three-way market (the first-inning and first-five moneylines) de-vigs
    its three prices together, and each side's EV reads its own price;
  * the stored rows price the segment markets like the full-game ones: one
    graded book's row gives the fair probability, the best book's price is
    offered, and a three-way row with no tie price is not usable;
  * a segment run line listed as two separate bets reads its reference margin
    from its own segment's total first;
  * a cached summary with no inning grid prices the full-game markets only;
  * the bet signals can name a tie;
  * the three full-game markets do not change;
  * the ``prices`` document injects any market, strictly validated.

Most route tests hand the route a synthetic summary (``_summary_and_winprob``
is patched), so every simulated probability is known and none is 0 or 1. The
legacy-market test runs the real no-DB simulation, as the older route tests do.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from urllib.parse import quote

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes import betting as betting_routes
from api.routes.betting import router as betting_router
from betting.clv_engine import (
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    devig_multiway,
    devig_two_way,
    expected_value,
    implied_prob_from_american,
    samples_over_under_edge_report,
    three_way_edge_report,
    total_over_under_edge_report,
)
from pipeline.live.live_ingestion_pipeline import MockOddsAPI
from pipeline.odds_provider import GAME_MARKET_KIND, GAME_MARKET_NAMES, GAME_MARKET_TYPES
from simulation.game_market_distributions import SegmentRuns, side_probabilities
from simulation.game_state import GameState
from simulation.results import GameSimSummary
from simulation.win_probability import win_probability

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"
GAME_PK = 745001


def _imp(a: float) -> float:
    return implied_prob_from_american(a)


# ===========================================================================
# A synthetic summary with an inning grid
# ===========================================================================


def _grid_summary(n: int = 400, *, seed: int = 546, grids: bool = True) -> GameSimSummary:
    """A summary of ``n`` made-up games, nine innings each, scored about five
    runs a team. A tie after nine gets a tenth inning the home team wins. With
    ``grids`` False the results carry no inning grid (an old cache entry)."""
    rng = np.random.default_rng(seed)
    results = []
    for _ in range(n):
        home = [int(x) for x in rng.choice(4, size=9, p=[0.65, 0.2, 0.1, 0.05])]
        away = [int(x) for x in rng.choice(4, size=9, p=[0.65, 0.2, 0.1, 0.05])]
        if sum(home) == sum(away):
            home.append(1)
            away.append(0)
        results.append(
            SimpleNamespace(
                home_score=sum(home),
                away_score=sum(away),
                home_by_inning=home if grids else None,
                away_by_inning=away if grids else None,
            )
        )
    return GameSimSummary.from_results(results)  # type: ignore[arg-type]


SUMMARY = _grid_summary()
RUNS = SegmentRuns.from_inning_grids(SUMMARY.inning_grids)


class _StubPool:
    """A direct-connection pool: the stored read gets the canned rows.

    ``status`` is the game's ``raw.games.status`` (None: no row)."""

    def __init__(self, rows=None, *, status=None):
        self.rows = rows or []
        self.status = status
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "raw.game_odds" in sql:
            return list(self.rows)
        if "FROM raw.games" in sql:
            return [] if self.status is None else [{"status": self.status}]
        return []

    @property
    def odds_calls(self):
        return [(sql, args) for sql, args in self.calls if "raw.game_odds" in sql]


@pytest.fixture
def client_for(monkeypatch):
    """A client over the betting router whose simulation is ``summary``."""

    def _make(pool=None, summary=SUMMARY):
        async def _fake_summary(request, **kwargs):
            return summary, win_probability(summary)

        monkeypatch.setattr(betting_routes, "_summary_and_winprob", _fake_summary)
        app = FastAPI()
        app.include_router(betting_router)
        app.state.pg_pool = pool if pool is not None else _StubPool()
        app.state.sim_cache = None
        return TestClient(app)

    return _make


def _row(market, book, **prices):
    row = {
        "market_type": market,
        "book": book,
        "line_type": "closing",
        "fetched_at": 1,
        "home_ml": None,
        "away_ml": None,
        "draw_ml": None,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }
    row.update(prices)
    return row


def _by(body, label):
    return {e["side"]: e for e in body["edges"] if e["label"] == label}


def _edges_url(query: str = "") -> str:
    return f"/api/betting/games/{GAME_PK}/edges?n_iterations=400&base_seed=7{query}"


def _prices(doc) -> str:
    return "&prices=" + quote(json.dumps(doc))


# ===========================================================================
# The builders (betting/clv_engine.py)
# ===========================================================================


class TestBuilders:
    def test_the_tie_is_a_side(self):
        assert MarketSide.DRAW.value == "draw"

    def test_the_total_builder_delegates_to_the_samples_builder(self):
        market = TwoWayMarket(side=MarketSide.OVER, entry=OddsQuote(-110, -105, line=9.5))
        for side in (MarketSide.OVER, MarketSide.UNDER):
            old = total_over_under_edge_report(SUMMARY, market, side=side)
            new = samples_over_under_edge_report(
                SUMMARY.total_scores, market, side=side, label="total"
            )
            assert old == new

    def test_the_samples_builder_takes_its_label_and_splits_the_push(self):
        samples = np.array([0, 1, 1, 2, 3])
        market = TwoWayMarket(side=MarketSide.OVER, entry=OddsQuote(-110, -110, line=1.0))
        over = samples_over_under_edge_report(samples, market, side=MarketSide.OVER, label="x")
        under = samples_over_under_edge_report(samples, market, side=MarketSide.UNDER, label="x")
        assert over.label == "x" and over.line == 1.0
        assert over.sim_prob == pytest.approx(0.4)
        assert under.sim_prob == pytest.approx(0.2)  # the two 1s push
        with pytest.raises(ValueError):
            samples_over_under_edge_report([], market, label="x")

    def test_the_three_way_builder_de_vigs_the_three_prices(self):
        prices = (150.0, 180.0, 120.0)
        fair = devig_multiway([_imp(p) for p in prices])
        probs = (0.35, 0.3, 0.35)
        reports = [
            three_way_edge_report(
                *probs,
                label="f5_moneyline",
                side=side,
                home_ml=prices[0],
                away_ml=prices[1],
                draw_ml=prices[2],
            )
            for side in (MarketSide.HOME, MarketSide.AWAY, MarketSide.DRAW)
        ]
        assert sum(r.market_fair_prob for r in reports) == pytest.approx(1.0, abs=1e-12)
        for report, p, own, f in zip(reports, probs, prices, fair, strict=True):
            assert report.sim_prob == p
            assert report.market_fair_prob == pytest.approx(f)
            assert report.edge == pytest.approx(p - f)
            assert report.ev == pytest.approx(expected_value(p, own))
            assert report.offered_american == own
            assert report.clv is None and report.line is None

    def test_the_three_way_builder_refuses_a_certain_side_and_an_over_side(self):
        kwargs = {"label": "f1_moneyline", "home_ml": 150, "away_ml": 180, "draw_ml": 120}
        with pytest.raises(ValueError):
            three_way_edge_report(1.0, 0.0, 0.0, side=MarketSide.HOME, **kwargs)
        with pytest.raises(ValueError):
            three_way_edge_report(0.4, 0.3, 0.3, side=MarketSide.OVER, **kwargs)


# ===========================================================================
# Test 7: the default request prices all fifteen from the mock
# ===========================================================================


def test_edges_default_prices_fifteen_markets_from_the_mock(client_for):
    resp = client_for().get(_edges_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["markets"] == list(GAME_MARKET_TYPES)
    assert body["odds_source"] == dict.fromkeys(GAME_MARKET_TYPES, "mock")
    labels = [betting_routes._report_label(m) for m in GAME_MARKET_TYPES]
    assert body["market_names"] == {
        betting_routes._report_label(m): GAME_MARKET_NAMES[m] for m in GAME_MARKET_TYPES
    }
    assert len(body["market_names"]) == 15
    counts: dict[str, int] = {}
    for e in body["edges"]:
        counts[e["label"]] = counts.get(e["label"], 0) + 1
    assert set(counts) == set(labels)
    for market in GAME_MARKET_TYPES:
        expected = 3 if GAME_MARKET_KIND[market] == "three_way" else 2
        assert counts[betting_routes._report_label(market)] == expected, market
    # The three full-game labels stay; the run line keeps its source key.
    assert {"moneyline", "total", "run_line"} <= set(counts)
    assert "runline" in body["odds_source"] and "runline" not in counts
    # Every run line is a pair in the mock, and each one says so.
    assert set(body["run_line_pricing_by_label"]) == {"run_line", "f1_runline", "f5_runline"}
    assert body["run_line_pricing"] == body["run_line_pricing_by_label"]["run_line"]
    json.dumps(body)


def test_the_segment_reports_read_the_segment_runs(client_for):
    body = client_for().get(_edges_url("&markets=f5_total,first_to_score")).json()
    f5 = _by(body, "f5_total")
    mock = MockOddsAPI.get_odds(GAME_PK, market_type="f5_total")
    line = float(mock["total_line"])
    assert f5["over"]["line"] == line
    assert f5["over"]["sim_prob"] == pytest.approx(float(np.mean(RUNS.segment_total("f5") > line)))
    first = _by(body, "first_to_score")
    p_home, p_away, _ = side_probabilities(RUNS, "first_to_score")
    assert first["home"]["sim_prob"] == pytest.approx(p_home)
    assert first["away"]["sim_prob"] == pytest.approx(p_away)
    fs = MockOddsAPI.get_odds(GAME_PK, market_type="first_to_score")
    assert first["home"]["market_fair_prob"] == pytest.approx(
        devig_two_way(fs["home_ml"], fs["away_ml"])[0]
    )


def test_a_yes_no_market_is_priced_at_half_a_run(client_for):
    body = client_for().get(_edges_url("&markets=first_inning_run")).json()
    yes = _by(body, "first_inning_run")["over"]
    assert yes["line"] == 0.5
    assert yes["sim_prob"] == pytest.approx(float(np.mean(RUNS.segment_total("f1") > 0)))


# ===========================================================================
# Test 8: the three-way fair probabilities sum to one
# ===========================================================================


def test_three_way_fair_probabilities_sum_to_one(client_for):
    body = client_for().get(_edges_url("&markets=f5_moneyline")).json()
    sides = _by(body, "f5_moneyline")
    assert set(sides) == {"home", "away", "draw"}
    assert sum(e["market_fair_prob"] for e in sides.values()) == pytest.approx(1.0, abs=1e-9)
    mock = MockOddsAPI.get_odds(GAME_PK, market_type="f5_moneyline")
    probs = dict(
        zip(("home", "away", "draw"), side_probabilities(RUNS, "f5_moneyline"), strict=True)
    )
    for side, column in (("home", "home_ml"), ("away", "away_ml"), ("draw", "draw_ml")):
        e = sides[side]
        assert e["sim_prob"] == pytest.approx(probs[side])
        assert e["offered_american"] == float(mock[column])
        assert e["ev"] == pytest.approx(expected_value(e["sim_prob"], float(mock[column])))


# ===========================================================================
# Test 9 and 10: the stored rows price the segment markets
# ===========================================================================

F5_TOTAL_ROWS = [
    _row("f5_total", "bp:12", total_line=4.5, over_ml=-110, under_ml=-110),
    _row("f5_total", "bp:10", total_line=4.5, over_ml=-105, under_ml=-115),
    _row("f5_total", "bp:19", total_line=5.5, over_ml=120, under_ml=-140),
]
F1_ML_ROWS = [
    _row("f1_moneyline", "bp:12", home_ml=180, away_ml=200, draw_ml=110),
    _row("f1_moneyline", "bp:10", home_ml=190, away_ml=190, draw_ml=110),
]


def test_stored_rows_price_the_segment_markets(client_for):
    pool = _StubPool([*F5_TOTAL_ROWS, *F1_ML_ROWS])
    body = client_for(pool).get(_edges_url()).json()
    # The default request reads every market, the tie price included.
    sql, args = pool.odds_calls[0]
    assert "draw_ml" in sql
    assert args[1] == list(GAME_MARKET_TYPES)
    assert body["odds_source"]["f5_total"] == "stored"
    assert body["odds_source"]["f1_moneyline"] == "stored"
    assert body["fair_book"]["f5_total"] == "bp:12"
    assert body["fair_book"]["f1_moneyline"] == "bp:12"
    assert body["fair_book_name"]["f5_total"] == "DraftKings"

    total = _by(body, "f5_total")
    assert total["over"]["line"] == 4.5
    assert total["over"]["market_fair_prob"] == pytest.approx(0.5)
    # The best price at DraftKings' line: FanDuel's over, DraftKings' under.
    assert (total["over"]["offered_american"], total["over"]["price_book"]) == (-105.0, "bp:10")
    assert (total["under"]["offered_american"], total["under"]["price_book"]) == (-110.0, "bp:12")
    assert total["over"]["ev"] == pytest.approx(expected_value(total["over"]["sim_prob"], -105.0))

    ml = _by(body, "f1_moneyline")
    fair = devig_multiway([_imp(180), _imp(200), _imp(110)])  # DraftKings' row alone
    for side, f in zip(("home", "away", "draw"), fair, strict=True):
        assert ml[side]["market_fair_prob"] == pytest.approx(f)
    assert (ml["home"]["offered_american"], ml["home"]["price_book"]) == (190.0, "bp:10")
    assert (ml["away"]["offered_american"], ml["away"]["price_book"]) == (200.0, "bp:12")
    # A tie on price keeps the book earlier on the list.
    assert (ml["draw"]["offered_american"], ml["draw"]["price_book"]) == (110.0, "bp:12")
    # The markets with no stored row read the mock.
    assert body["odds_source"]["f5_moneyline"] == "mock"


def test_three_way_row_without_a_tie_is_not_usable(client_for):
    rows = [
        _row("f1_moneyline", "bp:12", home_ml=180, away_ml=200),  # no tie price
        _row("f1_moneyline", "bp:19", home_ml=185, away_ml=195, draw_ml=105),
    ]
    body = client_for(_StubPool(rows)).get(_edges_url("&markets=f1_moneyline")).json()
    assert body["odds_source"] == {"f1_moneyline": "stored"}
    assert body["fair_book"] == {"f1_moneyline": "bp:19"}
    assert body["fair_book_name"] == {"f1_moneyline": "BetMGM"}
    # With no complete row the market falls to the mock.
    body = client_for(_StubPool(rows[:1])).get(_edges_url("&markets=f1_moneyline")).json()
    assert body["odds_source"] == {"f1_moneyline": "mock"}
    assert body["fair_book"] == {}


def test_the_usability_rule_reads_the_kind():
    by_market = betting_routes._stored_rows_by_market(
        [
            _row("f1_moneyline", "bp:12", home_ml=180, away_ml=200),
            _row("first_to_score", "bp:12", home_ml=-110, away_ml=-110),
            _row("first_inning_run", "bp:12", total_line=0.5, over_ml=-120, under_ml=100),
            _row("f5_runline", "bp:12", home_spread=-0.5, home_spread_ml=120),
        ]
    )
    assert set(by_market) == {"first_to_score", "first_inning_run"}


# ===========================================================================
# Test 11: a segment run line listed as two bets
# ===========================================================================


def test_segment_run_line_two_bets_reads_the_segment_total_margin(client_for):
    rows = [
        _row(
            "f5_runline",
            "bp:12",
            home_spread=-1.5,
            home_spread_ml=150,
            away_spread=-1.5,
            away_spread_ml=160,
        ),
        _row("f5_total", "bp:12", total_line=4.5, over_ml=-110, under_ml=-110),
        # The full-game total of the same book: read only after the f5 total.
        _row("total", "bp:12", total_line=9.5, over_ml=-120, under_ml=100),
    ]
    pool = _StubPool(rows)
    body = client_for(pool).get(_edges_url("&markets=f5_runline")).json()
    # A segment run line's read takes the segment total and the full-game markets.
    assert pool.odds_calls[0][1][1] == ["moneyline", "runline", "total", "f5_total", "f5_runline"]
    pricing = body["run_line_pricing_by_label"]["f5_runline"]
    margin = 2 * _imp(-110)
    assert pricing["shape"] == "two_bets"
    assert pricing["reference_source"] == "f5_total"
    assert pricing["reference_margin"] == pytest.approx(margin)
    assert body["run_line_pricing"] is None  # the full-game run line was not asked
    sides = _by(body, "f5_runline")
    assert sides["home"]["market_fair_prob"] == pytest.approx(_imp(150) / margin)
    assert sides["away"]["market_fair_prob"] == pytest.approx(_imp(160) / margin)
    margin_f5 = RUNS.segment_margin("f5")
    assert sides["home"]["sim_prob"] == pytest.approx(float(np.mean(margin_f5 > 1.5)))
    assert sides["away"]["sim_prob"] == pytest.approx(float(np.mean(margin_f5 < -1.5)))
    assert {s: e["line"] for s, e in sides.items()} == {"home": -1.5, "away": -1.5}


def test_an_injected_segment_run_line_reads_the_injected_segment_total(client_for):
    doc = {
        "f1_runline": {"home_ml": 300, "away_ml": 320, "home_line": -0.5, "away_line": -0.5},
        "f1_total": {"over_ml": -115, "under_ml": -105, "line": 0.5},
    }
    body = client_for().get(_edges_url("&markets=f1_runline" + _prices(doc))).json()
    pricing = body["run_line_pricing_by_label"]["f1_runline"]
    assert pricing["shape"] == "two_bets"
    assert pricing["reference_source"] == "f1_total"
    assert pricing["reference_margin"] == pytest.approx(_imp(-115) + _imp(-105))
    assert body["odds_source"] == {"f1_runline": "injected"}


def test_a_segment_run_line_pair_de_vigs_its_two_prices(client_for):
    rows = [
        _row(
            "f1_runline",
            "bp:12",
            home_spread=-0.5,
            home_spread_ml=130,
            away_spread=0.5,
            away_spread_ml=-150,
        )
    ]
    body = client_for(_StubPool(rows)).get(_edges_url("&markets=f1_runline")).json()
    assert body["run_line_pricing_by_label"]["f1_runline"]["shape"] == "pair"
    sides = _by(body, "f1_runline")
    fair_home, fair_away = devig_two_way(130, -150)
    assert sides["home"]["market_fair_prob"] == pytest.approx(fair_home)
    assert sides["away"]["market_fair_prob"] == pytest.approx(fair_away)
    assert {s: e["line"] for s, e in sides.items()} == {"home": -0.5, "away": 0.5}


# ===========================================================================
# Test 12: a summary without grids prices the full-game markets only
# ===========================================================================


def test_summary_without_grids_skips_the_segment_markets(client_for, caplog):
    summary = _grid_summary(grids=False)
    assert summary.inning_grids is None
    with caplog.at_level(logging.INFO, logger="api.routes.betting"):
        resp = client_for(summary=summary).get(_edges_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["markets"] == ["moneyline", "runline", "total"]
    assert set(body["odds_source"]) == {"moneyline", "runline", "total"}
    assert {e["label"] for e in body["edges"]} == {"moneyline", "total", "run_line"}
    assert set(body["market_names"]) == {"moneyline", "total", "run_line"}
    notes = [r for r in caplog.records if "no inning grid" in r.getMessage()]
    assert len(notes) == 1


# ===========================================================================
# Test 13: the signals can name a tie
# ===========================================================================


def test_signals_rank_a_draw_side(client_for):
    doc = {"f1_moneyline": {"home_ml": -150, "away_ml": 200, "draw_ml": 800}}
    resp = client_for().get(
        f"/api/betting/games/{GAME_PK}/signals?n_iterations=400&base_seed=7"
        "&markets=f1_moneyline&min_edge=0.0" + _prices(doc)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["odds_source"] == {"f1_moneyline": "injected"}
    draw = [s for s in body["signals"] if s["side"] == "draw"]
    assert len(draw) == 1
    assert draw[0]["label"] == "f1_moneyline"
    assert draw[0]["offered_american"] == 800.0
    assert draw[0]["ev"] > 0.0
    assert body["market_names"] == {"f1_moneyline": "First inning moneyline"}
    evs = [s["ev"] for s in body["signals"]]
    assert evs == sorted(evs, reverse=True)


# ===========================================================================
# Test 14: the three full-game markets do not change
# ===========================================================================


def _state() -> GameState:
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.batter_id = 101
    state.throw_hand = "R"
    return state


def test_legacy_markets_unchanged(monkeypatch):
    """The real no-DB simulation: ``?markets=moneyline,total`` returns exactly those."""

    async def _resolve(conn, game_pk, **kwargs):
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _resolve)
    app = FastAPI()
    app.include_router(betting_router)
    app.state.pg_pool = _StubPool()
    app.state.sim_cache = None
    app.state.sim_factory_ref = NO_DB_FACTORY_REF
    body = (
        TestClient(app)
        .get(
            f"/api/betting/games/{GAME_PK}/edges?n_iterations=40&base_seed=7&markets=total,moneyline"
        )
        .json()
    )
    assert body["markets"] == ["moneyline", "total"]
    assert set(body["odds_source"]) == {"moneyline", "total"}
    assert {e["label"] for e in body["edges"]} <= {"moneyline", "total"}
    assert body["run_line_pricing"] is None
    assert body["run_line_pricing_by_label"] == {}
    assert body["market_names"] == {"moneyline": "Moneyline", "total": "Total"}


def test_the_twelve_do_not_move_the_three(client_for):
    client = client_for()
    three = client.get(_edges_url("&markets=moneyline,total,runline")).json()
    fifteen = client.get(_edges_url()).json()
    legacy = {"moneyline", "total", "run_line"}
    assert [e for e in fifteen["edges"] if e["label"] in legacy] == three["edges"]
    assert fifteen["run_line_pricing"] == three["run_line_pricing"]
    for key in ("moneyline", "total", "runline"):
        assert fifteen["odds_source"][key] == three["odds_source"][key]


def test_the_stored_read_of_a_full_game_request_is_unchanged():
    assert betting_routes._stored_read_markets(("moneyline",)) == [
        "moneyline",
        "runline",
        "total",
    ]
    assert betting_routes._stored_read_markets(GAME_MARKET_TYPES) == list(GAME_MARKET_TYPES)


def test_a_bad_market_is_still_a_422(client_for):
    resp = client_for().get(_edges_url("&markets=f5_spread"))
    assert resp.status_code == 422
    assert "f5_spread" in resp.text


# ===========================================================================
# Test 23: the prices document
# ===========================================================================


class TestPricesParam:
    DOC = {
        "f5_total": {"over_ml": 105, "under_ml": -125, "line": 5.5},
        "f1_moneyline": {"home_ml": 150, "away_ml": 180, "draw_ml": 120},
    }

    def test_a_document_prices_its_markets_as_injected(self, client_for):
        # A stored f5 total exists; the document wins and the store is not read for it.
        pool = _StubPool(F5_TOTAL_ROWS)
        body = (
            client_for(pool)
            .get(_edges_url("&markets=f5_total,f1_moneyline" + _prices(self.DOC)))
            .json()
        )
        assert body["odds_source"] == {"f1_moneyline": "injected", "f5_total": "injected"}
        assert body["fair_book"] == {}
        total = _by(body, "f5_total")
        assert total["over"]["line"] == 5.5
        assert total["over"]["offered_american"] == 105.0
        assert total["under"]["offered_american"] == -125.0
        assert total["over"]["market_fair_prob"] == pytest.approx(devig_two_way(105, -125)[0])
        assert total["over"]["price_book"] is None
        ml = _by(body, "f1_moneyline")
        assert {s: e["offered_american"] for s, e in ml.items()} == {
            "home": 150.0,
            "away": 180.0,
            "draw": 120.0,
        }
        # Every requested market was injected: no stored read at all.
        assert pool.odds_calls == []

    def test_a_full_game_market_in_the_document(self, client_for):
        doc = {"total": {"over_ml": -120, "under_ml": 100, "line": 10.5}}
        body = client_for().get(_edges_url("&markets=total" + _prices(doc))).json()
        assert body["odds_source"] == {"total": "injected"}
        sides = _by(body, "total")
        assert sides["over"]["line"] == 10.5
        assert sides["over"]["offered_american"] == -120.0

    def test_the_yes_no_market_ignores_the_line(self, client_for):
        doc = {"first_inning_run": {"over_ml": -120, "under_ml": 100, "line": 3.5}}
        body = client_for().get(_edges_url("&markets=first_inning_run" + _prices(doc))).json()
        assert _by(body, "first_inning_run")["over"]["line"] == 0.5
        doc = {"first_inning_run": {"over_ml": -120, "under_ml": 100}}
        resp = client_for().get(_edges_url("&markets=first_inning_run" + _prices(doc)))
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        ("doc", "words"),
        [
            ({"f5_spread": {"home_ml": 100}}, ["unknown market", "f5_spread"]),
            ({"f5_total": {"over_ml": -110, "line": 4.5}}, ["f5_total", "missing", "under_ml"]),
            (
                {"f5_total": {"over_ml": "-110", "under_ml": -110, "line": 4.5}},
                ["f5_total", "over_ml", "not a number"],
            ),
            (
                {"f1_moneyline": {"home_ml": 150, "away_ml": 180, "draw_ml": None}},
                ["f1_moneyline", "draw_ml", "not a number"],
            ),
            (
                {"f5_total": {"over_ml": -110, "under_ml": -110, "line": 4.5, "book": 1}},
                ["f5_total", "unknown field", "book"],
            ),
            (
                {"first_to_score": {"home_ml": 0, "away_ml": -110}},
                ["first_to_score", "home_ml", "American price"],
            ),
            ({"f5_total": [1, 2]}, ["f5_total", "object"]),
        ],
    )
    def test_a_bad_document_is_a_422_naming_the_fault(self, client_for, doc, words):
        resp = client_for().get(_edges_url(_prices(doc)))
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        for word in words:
            assert word in detail

    def test_a_market_given_both_ways_is_a_422(self, client_for):
        doc = {"moneyline": {"home_ml": -120, "away_ml": 100}}
        resp = client_for().get(_edges_url("&home_ml=-130" + _prices(doc)))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "moneyline" in detail and "home_ml" in detail and "one way" in detail

    def test_a_document_that_is_not_an_object_is_a_422(self, client_for):
        for raw in ("not json", "[1, 2]"):
            resp = client_for().get(_edges_url("&prices=" + quote(raw)))
            assert resp.status_code == 422
            assert "prices" in resp.json()["detail"]

    def test_signals_take_the_same_document(self, client_for):
        resp = client_for().get(
            f"/api/betting/games/{GAME_PK}/signals?n_iterations=400&base_seed=7"
            "&markets=f5_total,f1_moneyline&min_edge=0.0" + _prices(self.DOC)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["odds_source"] == {"f1_moneyline": "injected", "f5_total": "injected"}
        resp = client_for().get(
            f"/api/betting/games/{GAME_PK}/signals?n_iterations=40" + _prices({"nope": {}})
        )
        assert resp.status_code == 422
