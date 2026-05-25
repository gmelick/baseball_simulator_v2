"""
test_line_movement_sim368.py
============================
Unit tests for SIM-368 -- the **CLV / line-movement time-series surface**
(:mod:`betting.line_movement`, Phase 5, Sprint 5, Wave 2).

Two paths, mirroring the repo's pure-logic + mockable-DB split:

  * **Pure** (:func:`betting.line_movement.line_movement_from_quotes`): a
    synthetic ORDERED quote sequence (opening -120 -> ... -> closing -150) builds
    a :class:`LineMovement` whose running implied-prob series, per-step + net
    deltas, steam ``direction`` and entry-vs-close :class:`CLV` are hand-checked
    against :mod:`betting.clv_engine`; a SINGLE quote yields no movement; an
    OUT-OF-ORDER input is sorted by ``fetched_at`` before deriving.
  * **DB** (:func:`betting.line_movement.fetch_line_movement`): a fake asyncpg
    connection (canned ``raw.game_odds`` rows, ``_StubConn`` mirroring
    test_sim_store.py) is read, grouped per (side, book), and built into the
    series; the expected query (``raw.game_odds`` ordered by ``fetched_at``) is
    asserted.

Owned by Betting / Markets Analyst.
"""

from __future__ import annotations

import pytest

from betting.clv_engine import (
    MarketSide,
    clv_from_odds,
    implied_prob_from_american,
)
from betting.line_movement import (
    LineMovement,
    LineQuote,
    fetch_line_movement,
    line_movement_from_quotes,
)


# ---------------------------------------------------------------------------
# Synthetic-quote helpers
# ---------------------------------------------------------------------------


def _ml_quote(*, t, american, other, line_type, book="consensus", sharp=False):
    """A moneyline HOME LineQuote at integer time ``t`` (other = away price)."""
    return LineQuote.from_american(
        fetched_at=t,
        line_type=line_type,
        book=book,
        is_sharp_book=sharp,
        american=american,
        other_american=other,
    )


def _home_ml_series():
    """An ordered HOME moneyline series: -120 -> -135 -> -150 (steam toward home).

    Opposite (away) prices: +100 -> +115 -> +130.  These give a clean, hand-
    checkable opening->closing CLV via clv_from_odds.
    """
    return [
        _ml_quote(t=1, american=-120, other=+100, line_type="opening"),
        _ml_quote(t=2, american=-135, other=+115, line_type="current"),
        _ml_quote(t=3, american=-150, other=+130, line_type="closing"),
    ]


# ===========================================================================
# Pure path
# ===========================================================================


class TestPureSeries:
    def test_running_implied_prob_series_matches_clv_engine(self):
        quotes = _home_ml_series()
        mv = line_movement_from_quotes(
            quotes, market_type="moneyline", side=MarketSide.HOME
        )
        expected = [
            implied_prob_from_american(-120),
            implied_prob_from_american(-135),
            implied_prob_from_american(-150),
        ]
        assert list(mv.implied_prob_series) == pytest.approx(expected)
        # Each quote's own implied_prob is the raw single-side implied prob.
        assert [q.implied_prob for q in mv.quotes] == pytest.approx(expected)

    def test_net_deltas_and_direction_steam_toward(self):
        mv = line_movement_from_quotes(
            _home_ml_series(), market_type="moneyline", side=MarketSide.HOME
        )
        ip_open = implied_prob_from_american(-120)
        ip_close = implied_prob_from_american(-150)
        assert mv.opening_american == -120
        assert mv.closing_american == -150
        assert mv.american_delta == pytest.approx(-30.0)  # -150 - (-120)
        assert mv.implied_prob_delta == pytest.approx(ip_close - ip_open)
        assert mv.implied_prob_delta > 0  # odds shortened -> moved TOWARD home
        assert mv.direction == "toward"
        assert mv.has_movement is True

    def test_per_step_deltas(self):
        mv = line_movement_from_quotes(
            _home_ml_series(), market_type="moneyline", side=MarketSide.HOME
        )
        ips = [
            implied_prob_from_american(-120),
            implied_prob_from_american(-135),
            implied_prob_from_american(-150),
        ]
        assert len(mv.step_implied_prob_deltas) == 2
        assert mv.step_implied_prob_deltas == pytest.approx(
            [ips[1] - ips[0], ips[2] - ips[1]]
        )
        assert mv.step_american_deltas == pytest.approx([-15.0, -15.0])

    def test_entry_vs_close_clv_matches_clv_from_odds(self):
        mv = line_movement_from_quotes(
            _home_ml_series(), market_type="moneyline", side=MarketSide.HOME
        )
        expected = clv_from_odds(
            entry_side_american=-120,
            entry_other_american=+100,
            close_side_american=-150,
            close_other_american=+130,
        )
        assert mv.clv is not None
        assert mv.clv.clv_prob == pytest.approx(expected.clv_prob)
        assert mv.clv.entry_fair_prob == pytest.approx(expected.entry_fair_prob)
        assert mv.clv.close_fair_prob == pytest.approx(expected.close_fair_prob)
        # Opening -120 vs closing -150: the open was the better (longer) price,
        # so a bet at the open beat the close.
        assert mv.clv.beat_close is True
        assert mv.beat_close is True

    def test_steam_away_when_odds_lengthen(self):
        """Reverse the series (-150 -> -120): the market cooled on home."""
        quotes = [
            _ml_quote(t=1, american=-150, other=+130, line_type="opening"),
            _ml_quote(t=2, american=-120, other=+100, line_type="closing"),
        ]
        mv = line_movement_from_quotes(
            quotes, market_type="moneyline", side=MarketSide.HOME
        )
        assert mv.implied_prob_delta < 0
        assert mv.direction == "away"
        # Opening -150 vs closing -120: you bet the WORSE price -> did NOT beat.
        assert mv.clv is not None
        assert mv.clv.beat_close is False

    def test_single_quote_no_movement(self):
        mv = line_movement_from_quotes(
            [_ml_quote(t=1, american=-120, other=+100, line_type="opening")],
            market_type="moneyline",
            side=MarketSide.HOME,
        )
        assert len(mv.quotes) == 1
        assert mv.step_implied_prob_deltas == ()
        assert mv.step_american_deltas == ()
        assert mv.american_delta == pytest.approx(0.0)
        assert mv.implied_prob_delta == pytest.approx(0.0)
        assert mv.direction == "flat"
        assert mv.has_movement is False
        assert mv.clv is None  # < 2 quotes -> no entry-vs-close CLV
        assert mv.beat_close is False

    def test_empty_input(self):
        mv = line_movement_from_quotes(
            [], market_type="moneyline", side=MarketSide.HOME
        )
        assert mv.quotes == ()
        assert mv.implied_prob_series == ()
        assert mv.clv is None
        assert mv.has_movement is False

    def test_out_of_order_rows_are_sorted(self):
        """Shuffled fetched_at times still derive opening=-120, closing=-150."""
        a, b, c = _home_ml_series()
        shuffled = [c, a, b]  # closing, opening, middle
        mv = line_movement_from_quotes(
            shuffled, market_type="moneyline", side=MarketSide.HOME
        )
        assert [q.fetched_at for q in mv.quotes] == [1, 2, 3]
        assert mv.opening_american == -120
        assert mv.closing_american == -150
        assert mv.direction == "toward"

    def test_missing_opposite_price_drops_clv_but_keeps_series(self):
        """No other-side price at an endpoint -> CLV None, series still built."""
        quotes = [
            LineQuote.from_american(
                fetched_at=1, line_type="opening", book="b", is_sharp_book=False,
                american=-120, other_american=None,
            ),
            LineQuote.from_american(
                fetched_at=2, line_type="closing", book="b", is_sharp_book=False,
                american=-150, other_american=None,
            ),
        ]
        mv = line_movement_from_quotes(
            quotes, market_type="moneyline", side=MarketSide.HOME
        )
        assert len(mv.quotes) == 2
        assert mv.implied_prob_delta > 0
        assert mv.direction == "toward"
        assert mv.clv is None  # can't de-vig without the opposite side

    def test_total_line_delta_tracked(self):
        """A total OVER series tracks the line move (8.5 -> 9.0) too."""
        quotes = [
            {
                "fetched_at": 1, "line_type": "opening", "book": "consensus",
                "is_sharp_book": False, "over_ml": -110, "under_ml": -110,
                "total_line": 8.5,
            },
            {
                "fetched_at": 2, "line_type": "closing", "book": "consensus",
                "is_sharp_book": False, "over_ml": -115, "under_ml": -105,
                "total_line": 9.0,
            },
        ]
        mv = line_movement_from_quotes(
            quotes, market_type="total", side=MarketSide.OVER
        )
        assert mv.line_delta == pytest.approx(0.5)
        assert mv.opening_american == -110
        assert mv.closing_american == -115


# ===========================================================================
# DB path -- fake asyncpg connection
# ===========================================================================


class _StubConn:
    """asyncpg connection stand-in. Records SQL/args, serves canned fetch rows."""

    def __init__(self, *, fetch=None):
        self._fetch = fetch or []
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch


def _ml_row(*, t, home_ml, away_ml, line_type, book="consensus", sharp=False):
    """A canned raw.game_odds moneyline row dict (as a stub Record)."""
    return {
        "fetched_at": t,
        "line_type": line_type,
        "book": book,
        "is_sharp_book": sharp,
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


@pytest.mark.asyncio
class TestFetchLineMovement:
    async def test_issues_expected_query_and_args(self):
        conn = _StubConn(fetch=[])
        await fetch_line_movement(conn, game_pk=777001, market_type="moneyline")
        assert len(conn.calls) == 1
        sql, args = conn.calls[0]
        assert "FROM raw.game_odds" in sql
        assert "ORDER BY fetched_at ASC" in sql
        assert "WHERE game_pk = $1 AND market_type = $2" in sql
        assert args == (777001, "moneyline")

    async def test_book_filter_adds_third_arg(self):
        conn = _StubConn(fetch=[])
        await fetch_line_movement(
            conn, game_pk=5, market_type="moneyline", book="pinnacle"
        )
        sql, args = conn.calls[0]
        assert "AND book = $3" in sql
        assert args == (5, "moneyline", "pinnacle")

    async def test_groups_into_per_side_series_and_builds_clv(self):
        rows = [
            _ml_row(t=1, home_ml=-120, away_ml=+100, line_type="opening"),
            _ml_row(t=2, home_ml=-135, away_ml=+115, line_type="current"),
            _ml_row(t=3, home_ml=-150, away_ml=+130, line_type="closing"),
        ]
        conn = _StubConn(fetch=rows)
        movements = await fetch_line_movement(
            conn, game_pk=1, market_type="moneyline"
        )
        # One series per side (HOME, AWAY) for the single book.
        sides = {m.side for m in movements}
        assert sides == {MarketSide.HOME, MarketSide.AWAY}

        home = next(m for m in movements if m.side is MarketSide.HOME)
        assert home.book == "consensus"
        assert home.game_pk == 1
        assert [q.fetched_at for q in home.quotes] == [1, 2, 3]
        assert home.opening_american == -120
        assert home.closing_american == -150
        assert home.direction == "toward"
        expected = clv_from_odds(
            entry_side_american=-120, entry_other_american=+100,
            close_side_american=-150, close_other_american=+130,
        )
        assert home.clv is not None
        assert home.clv.clv_prob == pytest.approx(expected.clv_prob)

        # AWAY moved the opposite way (its odds lengthened +100 -> +130).
        away = next(m for m in movements if m.side is MarketSide.AWAY)
        assert away.opening_american == +100
        assert away.closing_american == +130
        assert away.direction == "away"

    async def test_multiple_books_yield_separate_series(self):
        rows = [
            _ml_row(t=1, home_ml=-120, away_ml=+100, line_type="opening", book="dk"),
            _ml_row(t=3, home_ml=-150, away_ml=+130, line_type="closing", book="dk"),
            _ml_row(t=1, home_ml=-118, away_ml=-102, line_type="opening",
                    book="pinnacle", sharp=True),
            _ml_row(t=3, home_ml=-148, away_ml=+128, line_type="closing",
                    book="pinnacle", sharp=True),
        ]
        conn = _StubConn(fetch=rows)
        movements = await fetch_line_movement(
            conn, game_pk=1, market_type="moneyline"
        )
        # 2 sides x 2 books = 4 series.
        assert len(movements) == 4
        home_books = {m.book for m in movements if m.side is MarketSide.HOME}
        assert home_books == {"dk", "pinnacle"}

    async def test_sharp_consensus_flag_set_when_sharp_agrees(self):
        """Sharp (pinnacle) and the overall market both steam toward home -> True."""
        rows = [
            _ml_row(t=1, home_ml=-120, away_ml=+100, line_type="opening", book="dk"),
            _ml_row(t=3, home_ml=-150, away_ml=+130, line_type="closing", book="dk"),
            _ml_row(t=1, home_ml=-118, away_ml=-102, line_type="opening",
                    book="pinnacle", sharp=True),
            _ml_row(t=3, home_ml=-150, away_ml=+130, line_type="closing",
                    book="pinnacle", sharp=True),
        ]
        conn = _StubConn(fetch=rows)
        movements = await fetch_line_movement(
            conn, game_pk=1, market_type="moneyline"
        )
        home = [m for m in movements if m.side is MarketSide.HOME]
        assert all(m.sharp_consensus is True for m in home)

    async def test_empty_result_for_no_odds(self):
        conn = _StubConn(fetch=[])
        movements = await fetch_line_movement(
            conn, game_pk=999, market_type="moneyline"
        )
        assert movements == []

    async def test_unknown_market_type_raises(self):
        conn = _StubConn(fetch=[])
        with pytest.raises(ValueError):
            await fetch_line_movement(conn, game_pk=1, market_type="nonsense")
