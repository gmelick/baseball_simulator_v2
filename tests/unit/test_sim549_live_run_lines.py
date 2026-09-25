"""SIM-549 — the live betting pages price a run line by its shape.

A run line is ONE bet with two sides only when the away spread is the negative
of the home spread (home −1.5 / away +1.5). The book also lists two SEPARATE bets
(home −1.5 / away −1.5, or home +1.5 / away +1.5); pairing their prices
mis-prices each side. The accuracy comparison learned this first; these tests
pin the same rule on the live surface:

  * the shared primitives in ``betting.clv_engine``;
  * the line-movement series and its CLV (``betting.line_movement``): each quote
    knows the other side's spread; when either endpoint is two bets, both are
    priced over the game's two-way margin at that time; a side whose spread
    moved gets no CLV;
  * the ``/edges`` and ``/signals`` routes: an injected away spread, each report
    at its own side's spread, and ``run_line_pricing`` in the response.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes.betting import router as betting_router
from betting.clv_engine import (
    DEFAULT_ONE_SIDED_MARGIN,
    MarketSide,
    clv_from_odds,
    clv_from_prob,
    devig_one_sided,
    implied_prob_from_american,
    one_sided_edge_report,
    reference_margin_from_prices,
    run_line_bet_cover_prob,
    run_line_is_pair,
    spread_cover_prob,
)
from betting.line_movement import (
    _reference_margin_reader,
    fetch_line_movement,
    line_movement_from_quotes,
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
        rep = one_sided_edge_report(
            label="run_line",
            side=MarketSide.AWAY,
            line=-1.5,
            sim_prob=0.4,
            offered_american=150.0,
            fair_prob=0.374,
        )
        assert rep.line == -1.5 and rep.market_fair_prob == 0.374
        assert rep.edge == pytest.approx(0.4 - 0.374)
        assert rep.clv is None
        with pytest.raises(ValueError):  # a certain probability has no fair price
            one_sided_edge_report(
                label="run_line",
                side=MarketSide.HOME,
                line=-1.5,
                sim_prob=1.0,
                offered_american=150.0,
                fair_prob=0.3,
            )


# ===========================================================================
# The line-movement series and its CLV
# ===========================================================================


def _rl_row(t, home_line, home_ml, away_line, away_ml, *, book="consensus", line_type="current"):
    return {
        "fetched_at": t,
        "line_type": line_type,
        "book": book,
        "is_sharp_book": False,
        "market_type": "runline",
        "home_ml": None,
        "away_ml": None,
        "home_spread": home_line,
        "home_spread_ml": home_ml,
        "away_spread": away_line,
        "away_spread_ml": away_ml,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }


def _ref_row(t, market_type, a, b, *, book="consensus", line_type=None):
    row = {"fetched_at": t, "book": book, "market_type": market_type}
    if line_type is not None:
        row["line_type"] = line_type
    if market_type == "total":
        row.update(over_ml=a, under_ml=b, home_ml=None, away_ml=None)
    else:
        row.update(home_ml=a, away_ml=b, over_ml=None, under_ml=None)
    return row


class TestLineMovementSeries:
    def test_a_pair_series_keeps_its_clv_to_the_bit(self):
        rows = [_rl_row(1, -1.5, 140, 1.5, -160), _rl_row(2, -1.5, 120, 1.5, -140)]
        mv = line_movement_from_quotes(rows, market_type="runline", side=MarketSide.HOME)
        assert mv.run_line_shape == "pair" and mv.clv_basis == "pair" and mv.clv_note is None
        assert mv.clv == clv_from_odds(
            entry_side_american=140,
            entry_other_american=-160,
            close_side_american=120,
            close_other_american=-140,
        )
        assert mv.quotes[0].other_line == 1.5

    def test_two_separate_bets_are_priced_over_the_games_margin(self):
        rows = [_rl_row(1, -1.5, 350, -1.5, 150), _rl_row(2, -1.5, 300, -1.5, 170)]

        def margin_at(q):
            return (1.05, "total") if q.fetched_at == 1 else (1.07, "total")

        mv = line_movement_from_quotes(
            rows, market_type="runline", side=MarketSide.HOME, reference_margin_at=margin_at
        )
        assert mv.run_line_shape == "two_bets" and mv.clv_basis == "two_bets"
        assert mv.clv == clv_from_prob(_imp(350) / 1.05, _imp(300) / 1.07)
        assert "the game's two-way margin" in mv.clv_note
        away = line_movement_from_quotes(
            rows, market_type="runline", side=MarketSide.AWAY, reference_margin_at=margin_at
        )
        assert away.clv == clv_from_prob(_imp(150) / 1.05, _imp(170) / 1.07)
        assert away.quotes[0].line == -1.5 and away.quotes[0].other_line == -1.5

    def test_without_a_reference_market_there_is_no_clv(self):
        rows = [_rl_row(1, -1.5, 350, -1.5, 150), _rl_row(2, -1.5, 300, -1.5, 170)]
        mv = line_movement_from_quotes(rows, market_type="runline", side=MarketSide.HOME)
        assert mv.clv is None and "no reference market" in mv.clv_note

    def test_a_moved_line_is_not_the_same_bet(self):
        # game 744796's first-five shape: a pair at the open, two bets at the close
        rows = [_rl_row(1, 0.5, -115, -0.5, -110), _rl_row(2, -1.5, 350, -1.5, 150)]
        mv = line_movement_from_quotes(
            rows,
            market_type="runline",
            side=MarketSide.HOME,
            reference_margin_at=lambda q: (1.05, "total"),
        )
        assert mv.run_line_shape == "mixed"
        assert mv.clv is None and mv.clv_basis is None
        assert "moved from +0.5 to -1.5" in mv.clv_note

    def test_a_pair_at_one_end_and_two_bets_at_the_other(self):
        # Both ends are priced the same way, so the CLV moves only with the price.
        rows = [_rl_row(1, -1.5, 140, 1.5, -160), _rl_row(2, -1.5, 300, -1.5, 170)]
        mv = line_movement_from_quotes(
            rows,
            market_type="runline",
            side=MarketSide.HOME,
            reference_margin_at=lambda q: (1.06, "total"),
        )
        assert mv.run_line_shape == "mixed" and mv.clv_basis == "two_bets"
        assert mv.clv == clv_from_prob(_imp(140) / 1.06, _imp(300) / 1.06)
        assert mv.clv_note.startswith("a pair at one end and two separate bets at the other")

    def test_an_unchanged_price_reads_no_movement_across_a_shape_change(self):
        rows = [_rl_row(1, 1.5, 178, -1.5, -210), _rl_row(2, 1.5, 178, 1.5, -300)]
        mv = line_movement_from_quotes(
            rows,
            market_type="runline",
            side=MarketSide.HOME,
            reference_margin_at=lambda q: (1.05, "total"),
        )
        assert mv.clv is not None and mv.clv.clv_prob == pytest.approx(0.0, abs=1e-12)
        assert not mv.beat_close

    def test_a_missing_spread_gives_no_clv_and_no_error(self):
        rows = [_rl_row(1, None, 140, 1.5, -160), _rl_row(2, -1.5, 120, 1.5, -140)]
        mv = line_movement_from_quotes(
            rows,
            market_type="runline",
            side=MarketSide.HOME,
            reference_margin_at=lambda q: (1.05, "total"),
        )
        assert mv.clv is None and "spread is missing" in mv.clv_note
        # the row with a missing spread does not count toward the shape
        assert mv.run_line_shape == "pair"
        other_missing = [_rl_row(1, -1.5, 140, None, -160), _rl_row(2, -1.5, 120, None, -140)]
        mv = line_movement_from_quotes(
            other_missing,
            market_type="runline",
            side=MarketSide.HOME,
            reference_margin_at=lambda q: (1.05, "total"),
        )
        assert mv.run_line_shape is None and mv.clv is None

    def test_the_note_names_the_flat_margin(self):
        rows = [_rl_row(1, -1.5, 350, -1.5, 150), _rl_row(2, -1.5, 300, -1.5, 170)]

        def flat(q):
            return (1.05, "flat")

        mv = line_movement_from_quotes(
            rows, market_type="runline", side=MarketSide.HOME, reference_margin_at=flat
        )
        assert "a flat 1.05 margin" in mv.clv_note
        assert "the game's two-way margin" not in mv.clv_note

        def one_flat(q):
            return (1.05, "flat") if q.fetched_at == 1 else (1.07, "total")

        mv = line_movement_from_quotes(
            rows, market_type="runline", side=MarketSide.HOME, reference_margin_at=one_flat
        )
        assert "a flat 1.05 at the other" in mv.clv_note

    def test_other_markets_are_unchanged(self):
        rows = [
            {"fetched_at": 1, "home_ml": -120, "away_ml": 100, "line_type": "opening"},
            {"fetched_at": 2, "home_ml": -150, "away_ml": 130, "line_type": "closing"},
        ]
        mv = line_movement_from_quotes(rows, market_type="moneyline", side=MarketSide.HOME)
        assert mv.run_line_shape is None and mv.clv_basis == "pair"
        assert mv.clv == clv_from_odds(
            entry_side_american=-120,
            entry_other_american=100,
            close_side_american=-150,
            close_other_american=130,
        )


class TestReferenceMarginReader:
    def test_the_latest_row_at_or_before_the_quote_same_book_first(self):
        from betting.line_movement import LineQuote

        rows = [
            _ref_row(1, "total", -110, -110, book="consensus"),
            _ref_row(3, "total", -105, -125, book="consensus"),
            _ref_row(2, "total", -120, -120, book="pinnacle"),
        ]
        rows.sort(key=lambda r: r["fetched_at"])
        at = _reference_margin_reader(rows)

        def q(t, book):
            return LineQuote.from_american(
                fetched_at=t, line_type="current", book=book, is_sharp_book=False, american=150
            )

        assert at(q(2, "consensus")) == pytest.approx((2 * _imp(-110), "total"))
        assert at(q(5, "consensus")) == pytest.approx((_imp(-105) + _imp(-125), "total"))
        assert at(q(5, "pinnacle")) == pytest.approx((2 * _imp(-120), "total"))
        # a book with no reference rows of its own falls back to any book
        assert at(q(5, "circa"))[1] == "total"
        # nothing at or before the quote: the flat margin
        assert at(q(0, "consensus")) == (DEFAULT_ONE_SIDED_MARGIN, "flat")

    def test_the_quotes_own_snapshot_even_when_its_total_is_stamped_after_it(self):
        # The loader writes a snapshot's moneyline, run line and total
        # milliseconds apart, so the snapshot's total follows its run line.
        from datetime import datetime, timedelta

        from betting.line_movement import LineQuote

        t0 = datetime(2025, 5, 1, 12, 0, 0)
        open_rl, close_rl = t0, t0 + timedelta(hours=6)
        rows = [
            _ref_row(open_rl + timedelta(milliseconds=2), "total", -110, -110, line_type="opening"),
            _ref_row(
                close_rl + timedelta(milliseconds=2), "total", -114, -108, line_type="closing"
            ),
        ]
        at = _reference_margin_reader(rows)

        def q(t, line_type):
            return LineQuote.from_american(
                fetched_at=t,
                line_type=line_type,
                book="consensus",
                is_sharp_book=False,
                american=150,
            )

        assert at(q(open_rl, "opening")) == pytest.approx((2 * _imp(-110), "total"))
        assert at(q(close_rl, "closing")) == pytest.approx((_imp(-114) + _imp(-108), "total"))
        # 'current' snapshots: the nearest one, not the latest before the quote
        current = [
            _ref_row(t0 + timedelta(minutes=m, milliseconds=2), "total", a, b, line_type="current")
            for m, a, b in ((0, -110, -110), (30, -120, -120))
        ]
        at_current = _reference_margin_reader(current)
        quote_30 = q(t0 + timedelta(minutes=30), "current")
        assert at_current(quote_30) == pytest.approx((2 * _imp(-120), "total"))


class _StubConn:
    def __init__(self, runline_rows, reference_rows):
        self.runline_rows, self.reference_rows = runline_rows, reference_rows
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "IN ('total', 'moneyline')" in sql:
            return self.reference_rows
        return self.runline_rows


@pytest.mark.asyncio
class TestFetchLineMovement:
    async def test_an_away_only_price_still_reads_the_reference(self):
        runline = [_rl_row(1, -1.5, None, -1.5, 150), _rl_row(3, -1.5, None, -1.5, 170)]
        conn = _StubConn(runline, [_ref_row(0, "total", -110, -110)])
        movements = await fetch_line_movement(conn, game_pk=1, market_type="runline")
        assert len(conn.calls) == 2
        away = next(m for m in movements if m.side is MarketSide.AWAY)
        assert away.clv_basis == "two_bets"

    async def test_a_pair_run_line_reads_once(self):
        conn = _StubConn([_rl_row(1, -1.5, 140, 1.5, -160), _rl_row(2, -1.5, 120, 1.5, -140)], [])
        movements = await fetch_line_movement(conn, game_pk=1, market_type="runline")
        assert len(conn.calls) == 1
        assert {m.run_line_shape for m in movements} == {"pair"}

    async def test_two_bets_read_the_games_total_as_of_each_quote(self):
        runline = [_rl_row(1, -1.5, 350, -1.5, 150), _rl_row(3, -1.5, 300, -1.5, 170)]
        reference = [_ref_row(0, "total", -110, -110), _ref_row(2, "total", -105, -125)]
        conn = _StubConn(runline, reference)
        movements = await fetch_line_movement(conn, game_pk=1, market_type="runline")
        home = next(m for m in movements if m.side is MarketSide.HOME)
        assert home.clv == clv_from_prob(
            _imp(350) / (2 * _imp(-110)), _imp(300) / (_imp(-105) + _imp(-125))
        )

    async def test_two_bets_read_each_snapshots_own_total_in_the_real_order(self):
        # The real order: the snapshot's total is stamped just after its run line.
        runline = [
            _rl_row(1.0, -1.5, 350, -1.5, 150, line_type="opening"),
            _rl_row(3.0, -1.5, 300, -1.5, 170, line_type="closing"),
        ]
        reference = [
            _ref_row(1.002, "total", -110, -110, line_type="opening"),
            _ref_row(3.002, "total", -105, -125, line_type="closing"),
        ]
        conn = _StubConn(runline, reference)
        movements = await fetch_line_movement(conn, game_pk=1, market_type="runline")
        assert len(conn.calls) == 2
        home = next(m for m in movements if m.side is MarketSide.HOME)
        assert home.clv_basis == "two_bets"
        assert home.clv == clv_from_prob(
            _imp(350) / (2 * _imp(-110)), _imp(300) / (_imp(-105) + _imp(-125))
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
