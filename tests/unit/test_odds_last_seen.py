"""SIM-519 Part G — odds currency: the last-seen stamp.

The writers stored a row per (game, market, book, price) and skipped an
identical one, so a price that moved A -> B -> A kept B as its newest row, and a
withdrawn price kept its last row forever. The live writers now re-stamp a row
the book still posts; the pre-game read orders a book's rows by that stamp and
drops a current row not seen within 20 minutes of the game's newest pass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from api.routes import betting as b
from pipeline.live import live_ingestion_pipeline as live

T0 = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)


def _ml(
    book: str,
    home: float,
    away: float,
    fetched: datetime,
    seen: datetime | None,
    line_type: str = "current",
):
    return {
        "market_type": "moneyline",
        "book": book,
        "line_type": line_type,
        "fetched_at": fetched,
        "last_seen_at": seen,
        "home_ml": home,
        "away_ml": away,
    }


def test_a_b_a_reads_a() -> None:
    # Pass 1 stores A, pass 2 stores B, pass 3 sees A again: A's row is re-stamped.
    a = _ml("bp:12", -150, 130, fetched=T0, seen=T0 + timedelta(minutes=20))
    b_row = _ml(
        "bp:12", -140, 120, fetched=T0 + timedelta(minutes=10), seen=T0 + timedelta(minutes=10)
    )
    rows = b._latest_pregame_rows([a, b_row], ("current", "closing"))
    assert [r["home_ml"] for r in rows] == [-150]


def test_a_withdrawn_price_ages_out() -> None:
    fresh = _ml("bp:12", -150, 130, fetched=T0, seen=T0 + timedelta(minutes=40))
    withdrawn = _ml("bp:10", -160, 140, fetched=T0, seen=T0 + timedelta(minutes=10))
    rows = b._latest_pregame_rows([fresh, withdrawn], ("current", "closing"))
    assert {r["book"] for r in rows} == {"bp:12"}


def test_within_twenty_minutes_both_stay() -> None:
    one = _ml("bp:12", -150, 130, fetched=T0, seen=T0 + timedelta(minutes=30))
    two = _ml("bp:10", -160, 140, fetched=T0, seen=T0 + timedelta(minutes=15))
    assert len(b._latest_pregame_rows([one, two], ("current", "closing"))) == 2


def test_a_closing_row_always_counts() -> None:
    fresh = _ml("bp:12", -150, 130, fetched=T0, seen=T0 + timedelta(hours=3))
    closing = _ml("bp:10", -160, 140, fetched=T0, seen=T0, line_type="closing")
    rows = b._latest_pregame_rows([fresh, closing], ("current", "closing"))
    assert {r["book"] for r in rows} == {"bp:12", "bp:10"}


def test_rows_without_the_stamp_are_all_kept() -> None:
    one = _ml("bp:12", -150, 130, fetched=T0, seen=None)
    two = _ml("bp:10", -160, 140, fetched=T0 - timedelta(days=2), seen=None)
    assert len(b._latest_pregame_rows([one, two], ("current", "closing"))) == 2


def test_the_restamp_sql_updates_last_seen() -> None:
    assert "DO UPDATE SET last_seen_at = NOW()" in live._GAME_ODDS_RESTAMP_SQL
    assert "DO UPDATE SET last_seen_at = NOW()" in live._PROP_ODDS_RESTAMP_SQL
    # The insert itself is the same: only the conflict clause differs.
    assert (
        live._GAME_ODDS_RESTAMP_SQL.split("ON CONFLICT")[0]
        == live._GAME_ODDS_INSERT_SQL.split("ON CONFLICT")[0]
    )


def test_the_writer_restamps_only_when_the_column_exists() -> None:
    conn = MagicMock()
    conn.executemany = AsyncMock()
    row = {
        "book": "bp:12",
        "line_type": "current",
        "market_type": "moneyline",
        "home_ml": -150,
        "away_ml": 130,
    }
    asyncio.run(live.insert_game_odds_rows(conn, 1, [row], restamp=True))
    assert "DO UPDATE SET last_seen_at" in conn.executemany.await_args.args[0]
    asyncio.run(live.insert_game_odds_rows(conn, 1, [row]))
    assert "DO NOTHING" in conn.executemany.await_args.args[0]


def test_the_read_falls_back_before_0029() -> None:
    calls: list[str] = []

    class _Conn:
        async def fetch(self, sql: str, *args):
            calls.append(sql)
            if sql is b._SQL_GAME_STATUS:
                return [{"status": "Preview"}]
            if sql is b._SQL_STORED_GAME_ODDS_SEEN:
                raise RuntimeError('column "last_seen_at" does not exist')
            return []

    line_types, rows = asyncio.run(b._fetch_stored_rows(_Conn(), 1, ("moneyline",)))
    assert rows == []
    assert calls[-1] is b._SQL_STORED_GAME_ODDS
