"""SIM-555 — the live pages read one book per row and name the book.

Every new ``raw.game_odds`` row holds one book's prices and says which book
(``bp:<id>``). The old ``consensus`` rows can mix two books inside one row.
These tests pin the live surface's side of the change:

  * the line-movement reads keep only the one-book rows (the three SQL
    statements carry the label filter), and every quote and series names its
    book (``book_name``), through the API models too;
  * ``/edges`` and ``/signals`` price a market from the stored lines when no
    price is injected: the fair probability from ONE book's row (the graded
    book, first on the preference list), the offered price of each side from
    the best stored price at that line, with both books named. No pool, no
    stored row or a failed read gives the mock, as before;
  * the stored lines are each book's closing row, plus its latest ``current``
    row while the game is in ``Preview`` (not started). A current row of a game
    in any other state (an in-play price, or one taken after the game) is
    never read.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.games as games_mod
from api.routes import betting as betting_routes
from api.routes.betting import router as betting_router
from api.schemas import LineMovementModel, LineQuoteModel
from betting import line_movement as lm
from betting.clv_engine import (
    MarketSide,
    OddsQuote,
    TwoWayMarket,
    devig_two_way,
    expected_value,
    implied_prob_from_american,
    moneyline_edge_report,
)
from pipeline.odds_provider import BOOK_KIND, STORED_BOOK_FILTER_SQL, bettable_labels
from simulation.game_state import GameState

NO_DB_FACTORY_REF = "simulation.batch_runner:rng_driven_machine_factory"


def _imp(a: float) -> float:
    return implied_prob_from_american(a)


# ===========================================================================
# The line-movement reads: the label filter and the book's name
# ===========================================================================


class TestLineMovementLabelFilter:
    def test_the_three_statements_carry_the_filter(self):
        for sql in (
            lm._SQL_FETCH_GAME_ODDS,
            lm._SQL_FETCH_GAME_ODDS_BOOK,
            lm._SQL_FETCH_REFERENCE_ODDS,
        ):
            assert STORED_BOOK_FILTER_SQL in sql
            assert "book LIKE 'bp:%'" in sql

    @pytest.mark.asyncio
    async def test_every_read_of_a_two_bets_run_line_sends_the_filter(self):
        calls: list[str] = []

        class _Conn:
            async def fetch(self, sql, *args):
                calls.append(sql)
                if "IN ('total', 'moneyline')" in sql:
                    return []
                return [_rl_row(1, -1.5, 350, -1.5, 150), _rl_row(2, -1.5, 300, -1.5, 170)]

        await lm.fetch_line_movement(_Conn(), game_pk=1, market_type="runline")
        assert len(calls) == 2
        assert all("book LIKE 'bp:%'" in sql for sql in calls)

    @pytest.mark.asyncio
    async def test_the_book_filter_keeps_the_label_filter(self):
        calls: list[tuple[str, tuple]] = []

        class _Conn:
            async def fetch(self, sql, *args):
                calls.append((sql, args))
                return []

        await lm.fetch_line_movement(_Conn(), game_pk=5, market_type="total", book="bp:12")
        sql, args = calls[0]
        assert "AND book = $3" in sql and "book LIKE 'bp:%'" in sql
        assert args == (5, "total", "bp:12")


def _rl_row(t, home_line, home_ml, away_line, away_ml, *, book="bp:12"):
    return {
        "fetched_at": t,
        "line_type": "current",
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


def _ml_row(t, home_ml, away_ml, *, book="bp:10", line_type="current"):
    return {
        "fetched_at": t,
        "line_type": line_type,
        "book": book,
        "is_sharp_book": False,
        "market_type": "moneyline",
        "home_ml": home_ml,
        "away_ml": away_ml,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }


class TestBookName:
    def test_a_quote_names_its_book(self):
        def quote(book):
            return lm.LineQuote.from_american(
                fetched_at=1, line_type="closing", book=book, is_sharp_book=False, american=-110
            )

        assert quote("bp:10").book_name == "FanDuel"
        assert quote("bp:12").book_name == "DraftKings"
        assert quote("bp:999").book_name == "bp:999"  # an id the vocabulary does not name
        assert quote("consensus").book_name == "consensus"

    def test_a_series_names_its_book(self):
        rows = [_ml_row(1, -120, 100, book="bp:19"), _ml_row(2, -130, 110, book="bp:19")]
        mv = lm.line_movement_from_quotes(
            rows, market_type="moneyline", side=MarketSide.HOME, book="bp:19"
        )
        assert mv.book_name == "BetMGM"
        assert all(q.book_name == "BetMGM" for q in mv.quotes)
        # A series with no book (a direct build) and an empty one.
        assert (
            lm.line_movement_from_quotes(
                rows, market_type="moneyline", side=MarketSide.HOME
            ).book_name
            == ""
        )
        empty = lm.line_movement_from_quotes(
            [], market_type="moneyline", side=MarketSide.HOME, book="bp:24"
        )
        assert empty.book_name == "bet365"

    @pytest.mark.asyncio
    async def test_the_read_groups_by_book_and_names_each(self):
        class _Conn:
            async def fetch(self, sql, *args):
                return [
                    _ml_row(1, -120, 100, book="bp:12"),
                    _ml_row(2, -130, 110, book="bp:12"),
                    _ml_row(1, -118, -102, book="bp:10"),
                ]

        movements = await lm.fetch_line_movement(_Conn(), game_pk=1, market_type="moneyline")
        names = {(m.book, m.side, m.book_name) for m in movements}
        assert ("bp:12", MarketSide.HOME, "DraftKings") in names
        assert ("bp:10", MarketSide.AWAY, "FanDuel") in names

    def test_the_api_models_carry_the_name(self):
        rows = [_ml_row(1, -120, 100, book="bp:13"), _ml_row(2, -130, 110, book="bp:13")]
        mv = lm.line_movement_from_quotes(
            rows, market_type="moneyline", side=MarketSide.HOME, book="bp:13"
        )
        model = LineMovementModel.from_dataclass(mv)
        assert model.book_name == "Caesars"
        assert [q.book_name for q in model.quotes] == ["Caesars", "Caesars"]

    def test_the_api_models_name_the_book_for_an_old_image(self):
        """The app imports betting/ from its image; a LineQuote / LineMovement
        from before SIM-555 has no book_name, and the models fill it in."""
        quote = SimpleNamespace(
            fetched_at=None,
            line_type="closing",
            book="bp:49",
            is_sharp_book=False,
            american=-110.0,
            other_american=None,
            line=None,
            implied_prob=_imp(-110.0),
            other_line=None,
        )
        assert LineQuoteModel.from_dataclass(quote).book_name == "Hard Rock"
        movement = SimpleNamespace(
            game_pk=1,
            market_type="moneyline",
            side=MarketSide.HOME,
            book="bp:14",
            quotes=(quote,),
            opening_american=-110.0,
            closing_american=-110.0,
            opening_implied_prob=0.5,
            closing_implied_prob=0.5,
            american_delta=0.0,
            implied_prob_delta=0.0,
            line_delta=None,
            step_implied_prob_deltas=(),
            step_american_deltas=(),
            implied_prob_series=(0.5,),
            direction="flat",
            clv=None,
            sharp_consensus=None,
            has_movement=False,
            beat_close=False,
        )
        assert LineMovementModel.from_dataclass(movement).book_name == "Fanatics"


# ===========================================================================
# The stored price source (pure)
# ===========================================================================


def _stored_ml(book, home_ml, away_ml, *, line_type="closing", fetched_at=1):
    return {
        "market_type": "moneyline",
        "book": book,
        "line_type": line_type,
        "fetched_at": fetched_at,
        "home_ml": home_ml,
        "away_ml": away_ml,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }


def _stored_total(book, line, over_ml, under_ml):
    row = _stored_ml(book, None, None)
    row.update(market_type="total", total_line=line, over_ml=over_ml, under_ml=under_ml)
    return row


def _stored_rl(book, home_line, home_ml, away_line, away_ml):
    row = _stored_ml(book, None, None)
    row.update(
        market_type="runline",
        home_spread=home_line,
        home_spread_ml=home_ml,
        away_spread=away_line,
        away_spread_ml=away_ml,
    )
    return row


class TestStoredRows:
    def test_only_usable_rows_of_bettable_books_are_kept(self):
        rows = [
            _stored_ml("bp:10", -145, 135),
            _stored_ml("bp:0", -140, 145),  # the blend: nobody can bet it
            _stored_ml("bp:68", -130, 150),  # a prediction market
            _stored_ml("bp:37", -130, 150),  # a daily-fantasy app
            _stored_ml("consensus", -150, 130),  # an old mixed row
            _stored_ml("bp:19", -150, None),  # a side missing
            _stored_ml("bp:13", 0, 120),  # no such American price
            _stored_rl("bp:49", 1.0, -400, 1.0, 260),  # the guard refuses it
            _stored_ml("bp:12", -150, 130),
            {"market_type": "f5_total", "book": "bp:12", "over_ml": -110},  # not a full-game market
        ]
        by_market = betting_routes._stored_rows_by_market(rows)
        assert set(by_market) == {"moneyline"}
        # In the graded order: DraftKings (first on the list), then FanDuel.
        assert [r["book"] for r in by_market["moneyline"]] == ["bp:12", "bp:10"]

    def test_a_book_off_the_list_sorts_after_the_list_by_id(self, monkeypatch):
        # Two sportsbooks off the preference list, classified for the test (since
        # 2026-09-29 every sportsbook the vocabulary lists is on the list).
        monkeypatch.setitem(BOOK_KIND, 77, "sportsbook")
        monkeypatch.setitem(BOOK_KIND, 50, "sportsbook")
        rows = [_stored_ml("bp:77", -150, 130), _stored_ml("bp:50", -150, 130)]
        rows.append(_stored_ml("bp:24", -150, 130))
        by_market = betting_routes._stored_rows_by_market(rows)
        assert [r["book"] for r in by_market["moneyline"]] == ["bp:24", "bp:50", "bp:77"]

    def test_a_book_the_vocabulary_does_not_list_is_never_offered(self):
        """Review fix (2026-09-28, VOCAB-1): an unlisted id (Bet105, 77; BetMGM
        Casino, 50), a prediction market (DraftKings Predictions, 74) or a
        pick'em app (Betr, 45) used to count as a sportsbook."""
        rows = [
            _stored_ml("bp:77", 300, -150),
            _stored_ml("bp:50", 300, -150),
            _stored_ml("bp:74", 300, -150),
            _stored_ml("bp:45", 300, -150),
            _stored_ml("bp:27", -150, 130),
        ]
        by_market = betting_routes._stored_rows_by_market(rows)
        assert [r["book"] for r in by_market["moneyline"]] == ["bp:27"]
        market = betting_routes._stored_market(by_market, "moneyline")
        assert market is not None and market.fair_book == "bp:27"
        assert {book for _price, book in market.offered.values()} == {"bp:27"}

    def test_a_current_row_is_never_read_unless_the_game_is_in_preview(self):
        # Once the game is live, a current row is an in-play price: an
        # 8th-inning price must not price the pre-game simulation. The default
        # and every status but Preview read the closing line only.
        rows = [
            _stored_ml("bp:12", -2500, 1100, line_type="current", fetched_at=2),
            _stored_ml("bp:10", -2200, 1200, line_type="current", fetched_at=2),
            _stored_ml("bp:19", -150, 130, line_type="opening"),
        ]
        assert betting_routes._stored_rows_by_market(rows) == {}
        for status in ("Live", "Final", "Postponed", None, "preview"):
            line_types = betting_routes._usable_line_types(status)
            assert line_types == ("closing",), status
            assert betting_routes._stored_rows_by_market(rows, line_types) == {}, status
        # Before first pitch the same current rows are the day's stored lines.
        # The opening row is never a usable price.
        line_types = betting_routes._usable_line_types("Preview")
        assert line_types == ("closing", "current")
        by_market = betting_routes._stored_rows_by_market(rows, line_types)
        assert [(r["book"], r["home_ml"]) for r in by_market["moneyline"]] == [
            ("bp:12", -2500),
            ("bp:10", -2200),
        ]

    def test_in_preview_a_closing_row_beats_a_later_current_row_of_its_book(self):
        rows = [
            _stored_ml("bp:12", -150, 130, line_type="closing", fetched_at=1),
            _stored_ml("bp:12", -170, 150, line_type="current", fetched_at=9),
            _stored_ml("bp:10", -145, 135, line_type="current", fetched_at=3),
            _stored_ml("bp:10", -140, 130, line_type="current", fetched_at=2),
        ]
        preview = betting_routes._usable_line_types("Preview")
        latest = betting_routes._latest_pregame_rows(rows, preview)
        assert sorted((r["book"], r["line_type"], r["home_ml"]) for r in latest) == [
            ("bp:10", "current", -145),
            ("bp:12", "closing", -150),
        ]
        # The pure step drops what the SQL would drop: a stub that passes the
        # current rows of a game not in Preview still gets none of them.
        started = betting_routes._latest_pregame_rows(rows, ("closing",))
        assert [(r["book"], r["home_ml"]) for r in started] == [("bp:12", -150)]

    def test_a_later_current_row_does_not_displace_the_books_closing_row(self):
        # A loader run with --line-types current on a finished game stores a
        # row fetched after the closing row; the closing row still counts.
        rows = [
            _stored_ml("bp:12", -150, 130, line_type="closing", fetched_at=1),
            _stored_ml("bp:12", -2500, 1100, line_type="current", fetched_at=9),
        ]
        by_market = betting_routes._stored_rows_by_market(rows)
        graded = by_market["moneyline"]
        assert [(r["home_ml"], r["away_ml"]) for r in graded] == [(-150, 130)]

    def test_a_deduplicated_current_series_does_not_displace_the_closing_row(self):
        # The writers' dedup drops a price that returns to an earlier value:
        # A (t1) -> B (t2) -> A (t3) stores only A and B, so B looks current
        # while the book offers A. A game not in Preview reads no current row,
        # so B never becomes a price. In Preview (and in every other state)
        # the book's closing row, when it has one, beats its current rows.
        series = [
            _stored_ml("bp:12", -150, 130, line_type="current", fetched_at=1),
            _stored_ml("bp:12", -160, 140, line_type="current", fetched_at=2),
        ]
        assert betting_routes._stored_rows_by_market(series) == {}
        closing = _stored_ml("bp:12", -155, 135, line_type="closing", fetched_at=0)
        for status in ("Final", "Preview"):
            line_types = betting_routes._usable_line_types(status)
            by_market = betting_routes._stored_rows_by_market([*series, closing], line_types)
            market = betting_routes._stored_market(by_market, "moneyline")
            assert market is not None, status
            assert (market.graded["home_ml"], market.graded["away_ml"]) == (-155, 135), status
            assert market.offered[MarketSide.AWAY] == (135.0, "bp:12"), status

    def test_the_status_of_the_first_row(self):
        class _Record:
            def __init__(self, row):
                self._row = row

            def keys(self):
                return self._row.keys()

            def __getitem__(self, key):
                return self._row[key]

        assert betting_routes._status_of([{"status": "Preview"}]) == "Preview"
        assert betting_routes._status_of([_Record({"status": "Live"})]) == "Live"
        # No raw.games row, a row without the column, or a NULL: no status.
        assert betting_routes._status_of([]) is None
        assert betting_routes._status_of(None) is None
        assert betting_routes._status_of([{"book": "pinnacle"}]) is None
        assert betting_routes._status_of([{"status": None}]) is None

    def test_the_latest_closing_row_of_a_book_counts(self):
        rows = [
            _stored_ml("bp:12", -150, 130, fetched_at=1),
            _stored_ml("bp:12", -160, 140, fetched_at=3),
            _stored_ml("bp:12", -170, 150, fetched_at=2),
            _stored_ml("bp:10", -145, 135, fetched_at=None),
        ]
        by_market = betting_routes._stored_rows_by_market(rows)
        assert [(r["book"], r["home_ml"]) for r in by_market["moneyline"]] == [
            ("bp:12", -160),
            ("bp:10", -145),
        ]

    def test_records_are_read_like_dicts(self):
        class _Record:
            def __init__(self, row):
                self._row = row

            def keys(self):
                return self._row.keys()

            def __getitem__(self, key):
                return self._row[key]

        by_market = betting_routes._stored_rows_by_market([_Record(_stored_ml("bp:12", -150, 130))])
        assert by_market["moneyline"][0]["home_ml"] == -150


class TestStoredMarket:
    def test_the_graded_book_gives_the_row_and_the_best_price_is_offered(self):
        by_market = betting_routes._stored_rows_by_market(
            [
                _stored_ml("bp:24", -155, 140),
                _stored_ml("bp:10", -145, 135),
                _stored_ml("bp:12", -150, 130),
            ]
        )
        market = betting_routes._stored_market(by_market, "moneyline")
        assert market is not None
        assert market.fair_book == "bp:12"
        assert (market.graded["home_ml"], market.graded["away_ml"]) == (-150, 130)
        assert market.offered[MarketSide.HOME] == (-145.0, "bp:10")
        assert market.offered[MarketSide.AWAY] == (140.0, "bp:24")

    def test_without_a_preferred_book_the_first_other_sportsbook_grades(self, monkeypatch):
        monkeypatch.setitem(BOOK_KIND, 77, "sportsbook")
        monkeypatch.setitem(BOOK_KIND, 50, "sportsbook")
        by_market = betting_routes._stored_rows_by_market(
            [_stored_ml("bp:77", -120, 100), _stored_ml("bp:50", -130, 110)]
        )
        market = betting_routes._stored_market(by_market, "moneyline")
        assert market is not None and market.fair_book == "bp:50"

    def test_the_offered_price_is_at_the_graded_line_and_a_tie_keeps_the_list_order(self):
        by_market = betting_routes._stored_rows_by_market(
            [
                _stored_total("bp:13", 1.5, -105, -125),  # ties FanDuel's over
                _stored_total("bp:19", 2.5, 100, -120),  # another line: not the same bet
                _stored_total("bp:10", 1.5, -105, -115),
                _stored_total("bp:12", 1.5, -110, -110),
            ]
        )
        market = betting_routes._stored_market(by_market, "total")
        assert market is not None and market.fair_book == "bp:12"
        # FanDuel (10) sits before Caesars (13) on the list, so it takes the tie.
        assert market.offered[MarketSide.OVER] == (-105.0, "bp:10")
        # The graded book's own price is a candidate: -110 beats -115 and -125.
        assert market.offered[MarketSide.UNDER] == (-110.0, "bp:12")

    def test_a_run_line_side_is_matched_on_its_own_spread(self):
        by_market = betting_routes._stored_rows_by_market(
            [
                _stored_rl("bp:12", -1.5, 140, 1.5, -160),
                _stored_rl("bp:10", -1.5, 150, -1.5, 170),  # home -1.5 is the same bet
                _stored_rl("bp:19", -2.5, 260, 2.5, -320),
            ]
        )
        market = betting_routes._stored_market(by_market, "runline")
        assert market is not None
        assert market.offered[MarketSide.HOME] == (150.0, "bp:10")
        assert market.offered[MarketSide.AWAY] == (-160.0, "bp:12")

    def test_no_row_no_market(self):
        assert betting_routes._stored_market({}, "moneyline") is None
        assert betting_routes._stored_market(None, "total") is None

    def test_the_offered_price_moves_the_ev_and_not_the_edge(self):
        base = moneyline_edge_report(
            SimpleNamespace(home_win_prob=0.55, away_win_prob=0.45),
            TwoWayMarket(side=MarketSide.HOME, entry=OddsQuote(side=-150.0, other=130.0)),
            side=MarketSide.HOME,
        )
        moved = betting_routes._at_offered_price(base, (-140.0, "bp:10"))
        assert moved.offered_american == -140.0
        assert moved.ev == pytest.approx(expected_value(0.55, -140.0))
        assert moved.market_fair_prob == base.market_fair_prob
        assert moved.edge == base.edge
        assert betting_routes._at_offered_price(base, None) is base

    def test_the_reference_margin_is_the_graded_books_own(self):
        by_market = betting_routes._stored_rows_by_market(
            [
                _stored_total("bp:12", 8.5, -110, -110),
                _stored_total("bp:10", 8.5, -120, 100),
                _stored_ml("bp:19", -150, 130),
            ]
        )
        assert betting_routes._stored_reference_margin(by_market, "bp:12") == pytest.approx(
            (2 * _imp(-110), "total")
        )
        # BetMGM has no total: its moneyline.
        assert betting_routes._stored_reference_margin(by_market, "bp:19") == pytest.approx(
            (_imp(-150) + _imp(130), "moneyline")
        )
        # A book with neither: the flat margin.
        assert betting_routes._stored_reference_margin(by_market, "bp:13") == (1.05, "flat")


# ===========================================================================
# The /edges and /signals routes with a stub pool
# ===========================================================================


class _StubPool:
    """A direct-connection pool: the stored read gets the canned rows.

    ``status`` is the game's ``raw.games.status`` (None: no row). The canned
    rows come back whatever line types the SQL asks for, so the route's pure
    step is what keeps a current row out."""

    def __init__(self, rows=None, *, fail=False, status=None):
        self.rows = rows or []
        self.fail = fail
        self.status = status
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "raw.game_odds" in sql:
            if self.fail:
                raise RuntimeError("the database went away")
            return list(self.rows)
        if "FROM raw.games" in sql:
            return [] if self.status is None else [{"status": self.status}]
        return []

    @property
    def odds_calls(self):
        return [(sql, args) for sql, args in self.calls if "raw.game_odds" in sql]

    @property
    def status_calls(self):
        # The game's status read (the edge route also reads the final's grid
        # to grade the bets, a separate query).
        return [(sql, args) for sql, args in self.calls if "SELECT status FROM raw.games" in sql]


def _state() -> GameState:
    state = GameState(pitcher_id=600001, bat_hand="R", season=2024)
    state.away_lineup = [101, 102, 103, 104, 105, 106, 107, 108, 109]
    state.home_lineup = [201, 202, 203, 204, 205, 206, 207, 208, 209]
    state.batter_id = 101
    state.throw_hand = "R"
    return state


@pytest.fixture
def make_client(monkeypatch):
    async def _resolve(conn, game_pk, **kwargs):
        return _state()

    monkeypatch.setattr(games_mod, "resolve_game_state", _resolve)

    def _make(pool):
        app = FastAPI()
        app.include_router(betting_router)
        app.state.pg_pool = pool
        app.state.sim_cache = None
        app.state.sim_factory_ref = NO_DB_FACTORY_REF
        return TestClient(app)

    return _make


# The no-DB rng factory scores low (totals ~0-4, margins ~-4..2), so the lines
# sit inside that range and no simulated probability is 0 or 1.
MONEYLINE_ROWS = [
    _stored_ml("bp:24", -155, 140),
    _stored_ml("bp:10", -145, 135),
    _stored_ml("bp:12", -150, 130),
]
TOTAL_ROWS = [
    _stored_total("bp:12", 1.5, -110, -110),
    _stored_total("bp:10", 1.5, -105, -115),
]


class TestStoredEdges:
    def test_the_stored_read_is_one_query_with_the_filters(self, make_client):
        pool = _StubPool(MONEYLINE_ROWS)
        resp = make_client(pool).get(
            "/api/betting/games/745001/edges?n_iterations=60&base_seed=7&markets=moneyline"
        )
        assert resp.status_code == 200, resp.text
        assert len(pool.odds_calls) == 1
        sql, args = pool.odds_calls[0]
        assert "DISTINCT ON (market_type, book)" in sql
        # The line types are a parameter: the closing line only for a game
        # not in Preview (here: no raw.games row), a closing row first.
        assert "line_type = ANY($4::varchar[])" in sql
        assert "'current'" not in sql
        # SIM-519 Part G: a book's rows sort by when the price was last seen.
        assert "(line_type = 'closing') DESC," in sql
        assert "COALESCE(last_seen_at, fetched_at) DESC" in sql
        assert "book LIKE 'bp:%'" in sql
        # Review fix (VOCAB-1): a list of the books a bettor can use, never a blacklist.
        assert "AND book = ANY($3::varchar[])" in sql and "NOT (book = ANY(" not in sql
        assert args == (
            745001,
            ["moneyline", "runline", "total"],
            bettable_labels(),
            ["closing"],
        )
        # The status is read first, for the game alone.
        assert pool.status_calls == [(betting_routes._SQL_GAME_STATUS, (745001,))]
        assert pool.calls.index(pool.status_calls[0]) < pool.calls.index(pool.odds_calls[0])

    def test_a_preview_game_asks_for_the_current_rows_too(self, make_client):
        pool = _StubPool(MONEYLINE_ROWS, status="Preview")
        resp = make_client(pool).get(
            "/api/betting/games/745001/edges?n_iterations=60&base_seed=7&markets=moneyline"
        )
        assert resp.status_code == 200, resp.text
        _sql, args = pool.odds_calls[0]
        assert args[3] == ["closing", "current"]

    def test_a_moneyline_is_fair_from_one_book_and_offered_at_the_best_price(self, make_client):
        resp = make_client(_StubPool(MONEYLINE_ROWS)).get(
            "/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=moneyline"
        )
        body = resp.json()
        assert body["odds_source"] == {"moneyline": "stored"}
        assert body["fair_book"] == {"moneyline": "bp:12"}
        assert body["fair_book_name"] == {"moneyline": "DraftKings"}
        by_side = {e["side"]: e for e in body["edges"]}
        fair_home, fair_away = devig_two_way(-150, 130)  # DraftKings' row alone
        home, away = by_side["home"], by_side["away"]
        assert home["market_fair_prob"] == pytest.approx(fair_home)
        assert away["market_fair_prob"] == pytest.approx(fair_away)
        assert (home["offered_american"], home["price_book"], home["price_book_name"]) == (
            -145.0,
            "bp:10",
            "FanDuel",
        )
        assert (away["offered_american"], away["price_book"], away["price_book_name"]) == (
            140.0,
            "bp:24",
            "bet365",
        )
        assert home["ev"] == pytest.approx(expected_value(home["sim_prob"], -145.0))
        assert home["edge"] == pytest.approx(home["sim_prob"] - fair_home)

    def test_a_total_is_priced_at_the_graded_line(self, make_client):
        rows = [*TOTAL_ROWS, _stored_total("bp:19", 2.5, 100, -120)]
        body = (
            make_client(_StubPool(rows))
            .get("/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=total")
            .json()
        )
        assert body["odds_source"] == {"total": "stored"}
        by_side = {e["side"]: e for e in body["edges"]}
        assert by_side["over"]["line"] == 1.5
        assert by_side["over"]["market_fair_prob"] == pytest.approx(0.5)
        assert (by_side["over"]["offered_american"], by_side["over"]["price_book"]) == (
            -105.0,
            "bp:10",
        )
        assert (by_side["under"]["offered_american"], by_side["under"]["price_book"]) == (
            -110.0,
            "bp:12",
        )

    def test_a_run_line_pair_de_vigs_the_graded_row(self, make_client):
        rows = [
            _stored_rl("bp:12", -0.5, 120, 0.5, -140),
            _stored_rl("bp:10", -0.5, 125, 0.5, -145),
        ]
        body = (
            make_client(_StubPool(rows))
            .get("/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=runline")
            .json()
        )
        assert body["odds_source"] == {"runline": "stored"}
        assert body["run_line_pricing"]["shape"] == "pair"
        by_side = {e["side"]: e for e in body["edges"]}
        fair_home, fair_away = devig_two_way(120, -140)
        assert by_side["home"]["market_fair_prob"] == pytest.approx(fair_home)
        assert by_side["away"]["market_fair_prob"] == pytest.approx(fair_away)
        assert {
            s: (e["line"], e["offered_american"], e["price_book"]) for s, e in by_side.items()
        } == {
            "home": (-0.5, 125.0, "bp:10"),
            "away": (0.5, -140.0, "bp:12"),
        }

    def test_two_separate_bets_read_the_graded_books_own_margin(self, make_client):
        rows = [
            _stored_rl("bp:12", 1.5, -200, 1.5, -210),
            _stored_rl("bp:10", 1.5, -190, 1.5, -220),
            _stored_total("bp:12", 1.5, -110, -110),
            _stored_total("bp:10", 1.5, -120, 100),
        ]
        body = (
            make_client(_StubPool(rows))
            .get("/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=runline")
            .json()
        )
        margin = 2 * _imp(-110)  # DraftKings' own total, not FanDuel's
        pricing = body["run_line_pricing"]
        assert pricing["shape"] == "two_bets"
        assert pricing["reference_source"] == "total"
        assert pricing["reference_margin"] == pytest.approx(margin)
        by_side = {e["side"]: e for e in body["edges"]}
        assert by_side["home"]["market_fair_prob"] == pytest.approx(_imp(-200) / margin)
        assert by_side["away"]["market_fair_prob"] == pytest.approx(_imp(-210) / margin)
        assert (by_side["home"]["offered_american"], by_side["home"]["price_book"]) == (
            -190.0,
            "bp:10",
        )
        assert (by_side["away"]["offered_american"], by_side["away"]["price_book"]) == (
            -210.0,
            "bp:12",
        )
        assert by_side["home"]["ev"] == pytest.approx(
            expected_value(by_side["home"]["sim_prob"], -190.0)
        )

    def test_an_injected_market_ignores_the_stored_lines(self, make_client):
        pool = _StubPool([*MONEYLINE_ROWS, *TOTAL_ROWS])
        body = (
            make_client(pool)
            .get(
                "/api/betting/games/745001/edges?n_iterations=60&base_seed=7"
                "&markets=moneyline,total&home_ml=-120&away_ml=100"
            )
            .json()
        )
        assert body["odds_source"] == {"moneyline": "injected", "total": "stored"}
        assert body["fair_book"] == {"total": "bp:12"}
        for e in body["edges"]:
            if e["label"] == "moneyline":
                assert e["price_book"] is None and e["price_book_name"] is None

    def test_a_fully_injected_request_does_not_read_the_store(self, make_client):
        pool = _StubPool(MONEYLINE_ROWS)
        resp = make_client(pool).get(
            "/api/betting/games/745001/edges?n_iterations=30&base_seed=7"
            "&markets=moneyline&home_ml=-120&away_ml=100"
        )
        assert resp.status_code == 200, resp.text
        assert pool.odds_calls == []

    def test_no_stored_row_gives_the_mock(self, make_client):
        body = (
            make_client(_StubPool([]))
            .get("/api/betting/games/745001/edges?n_iterations=30&base_seed=7&markets=moneyline")
            .json()
        )
        assert body["odds_source"] == {"moneyline": "mock"}
        assert body["fair_book"] == {} and body["fair_book_name"] == {}
        assert all(e["price_book"] is None for e in body["edges"])

    def test_only_non_bettable_rows_give_the_mock(self, make_client):
        rows = [_stored_ml("bp:0", -150, 130), _stored_ml("consensus", -150, 130)]
        body = (
            make_client(_StubPool(rows))
            .get("/api/betting/games/745001/edges?n_iterations=30&base_seed=7&markets=moneyline")
            .json()
        )
        assert body["odds_source"] == {"moneyline": "mock"}

    def test_a_failed_read_logs_a_warning_and_gives_the_mock(self, make_client, caplog):
        with caplog.at_level(logging.WARNING, logger="api.routes.betting"):
            resp = make_client(_StubPool(MONEYLINE_ROWS, fail=True)).get(
                "/api/betting/games/745001/edges?n_iterations=30&base_seed=7&markets=moneyline"
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["odds_source"] == {"moneyline": "mock"}
        assert any("stored odds read failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("status", ["Live", "Final", None])
    def test_in_play_current_rows_never_price_a_game_not_in_preview(self, make_client, status):
        # The review's case: two in-play current rows (the home side far
        # ahead) used to give a stored price and a +EV away bet at +1200. A
        # live or finished game, or one with no raw.games row, reads no
        # current row, even from a stub that returns them.
        rows = [
            _stored_ml("bp:12", -2500, 1100, line_type="current", fetched_at=2),
            _stored_ml("bp:10", -2200, 1200, line_type="current", fetched_at=2),
        ]
        body = (
            make_client(_StubPool(rows, status=status))
            .get("/api/betting/games/745001/signals?n_iterations=60&base_seed=11&markets=moneyline")
            .json()
        )
        assert body["odds_source"] == {"moneyline": "mock"}
        assert body["fair_book"] == {}
        for signal in body["signals"]:
            assert signal["offered_american"] not in (1100.0, 1200.0)
            assert signal["price_book"] is None

    def test_a_preview_game_is_priced_from_its_current_rows(self, make_client):
        # Before first pitch the live pipeline stores every book's current
        # line; /edges prices the upcoming game from them, books named.
        rows = [
            _stored_ml("bp:24", -155, 140, line_type="current", fetched_at=2),
            _stored_ml("bp:10", -145, 135, line_type="current", fetched_at=2),
            _stored_ml("bp:12", -150, 130, line_type="current", fetched_at=2),
            # DraftKings' earlier current row: the latest one counts.
            _stored_ml("bp:12", -170, 150, line_type="current", fetched_at=1),
        ]
        body = (
            make_client(_StubPool(rows, status="Preview"))
            .get("/api/betting/games/745001/edges?n_iterations=120&base_seed=7&markets=moneyline")
            .json()
        )
        assert body["odds_source"] == {"moneyline": "stored"}
        assert body["fair_book"] == {"moneyline": "bp:12"}
        assert body["fair_book_name"] == {"moneyline": "DraftKings"}
        by_side = {e["side"]: e for e in body["edges"]}
        fair_home, fair_away = devig_two_way(-150, 130)
        assert by_side["home"]["market_fair_prob"] == pytest.approx(fair_home)
        assert by_side["away"]["market_fair_prob"] == pytest.approx(fair_away)
        assert (
            by_side["home"]["offered_american"],
            by_side["home"]["price_book"],
            by_side["home"]["price_book_name"],
        ) == (-145.0, "bp:10", "FanDuel")
        assert (
            by_side["away"]["offered_american"],
            by_side["away"]["price_book"],
            by_side["away"]["price_book_name"],
        ) == (140.0, "bp:24", "bet365")
        assert by_side["away"]["ev"] == pytest.approx(
            expected_value(by_side["away"]["sim_prob"], 140.0)
        )

    def test_a_preview_games_closing_row_beats_its_current_row(self, make_client):
        rows = [
            _stored_ml("bp:12", -150, 130, line_type="closing", fetched_at=1),
            _stored_ml("bp:12", -170, 150, line_type="current", fetched_at=9),
            _stored_ml("bp:10", -145, 135, line_type="current", fetched_at=2),
        ]
        body = (
            make_client(_StubPool(rows, status="Preview"))
            .get("/api/betting/games/745001/edges?n_iterations=60&base_seed=7&markets=moneyline")
            .json()
        )
        assert body["fair_book"] == {"moneyline": "bp:12"}
        by_side = {e["side"]: e for e in body["edges"]}
        assert by_side["home"]["market_fair_prob"] == pytest.approx(devig_two_way(-150, 130)[0])
        assert (by_side["away"]["offered_american"], by_side["away"]["price_book"]) == (
            135.0,
            "bp:10",
        )

    def test_another_books_in_play_row_is_not_offered(self, make_client):
        # DraftKings' latest row is its pre-game closing row; FanDuel's is an
        # in-play current row of the live game. Only the closing row prices
        # either side.
        rows = [
            _stored_ml("bp:12", -150, 130, line_type="closing", fetched_at=1),
            _stored_ml("bp:10", -2200, 1200, line_type="current", fetched_at=2),
        ]
        body = (
            make_client(_StubPool(rows, status="Live"))
            .get("/api/betting/games/745001/edges?n_iterations=60&base_seed=11&markets=moneyline")
            .json()
        )
        assert body["odds_source"] == {"moneyline": "stored"}
        assert body["fair_book"] == {"moneyline": "bp:12"}
        by_side = {e["side"]: e for e in body["edges"]}
        assert (by_side["away"]["offered_american"], by_side["away"]["price_book"]) == (
            130.0,
            "bp:12",
        )
        assert (by_side["home"]["offered_american"], by_side["home"]["price_book"]) == (
            -150.0,
            "bp:12",
        )
        assert by_side["away"]["ev"] == pytest.approx(
            expected_value(by_side["away"]["sim_prob"], 130.0)
        )

    def test_signals_name_the_book_of_the_offered_price(self, make_client):
        rows = [
            _stored_ml("bp:12", 300, -400),
            _stored_ml("bp:10", 400, -450),  # the best home price: a big +EV bet
        ]
        body = (
            make_client(_StubPool(rows))
            .get(
                "/api/betting/games/745001/signals?n_iterations=60&base_seed=11"
                "&markets=moneyline&min_edge=0.0"
            )
            .json()
        )
        assert body["odds_source"] == {"moneyline": "stored"}
        assert body["fair_book"] == {"moneyline": "bp:12"}
        home = next(s for s in body["signals"] if s["side"] == "home")
        assert home["offered_american"] == 400.0
        assert (home["price_book"], home["price_book_name"]) == ("bp:10", "FanDuel")
        assert home["report"]["price_book"] == "bp:10"
        assert home["report"]["market_fair_prob"] == pytest.approx(devig_two_way(300, -400)[0])
