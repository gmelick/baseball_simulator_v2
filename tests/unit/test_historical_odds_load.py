"""
test_historical_odds_load.py
==============================
SIM-435 — unit tests for (1) the BettingPros provider's CLOSING-line parsing and
(2) the historical-odds loader (betting/history.py).

No network and no live DB:
  * the provider's _bp_get / _mlb_get seams are stubbed with the captured
    fixtures (tests/fixtures/bettingpros/) — same pattern as the SIM-405 tests;
  * the loader's load path (betting.load.load_odds) runs the real provider over
    the recorded responses (tests/bp_recorded.py) into a recording pool, so the
    opening and closing writes are asserted without Postgres.

Closing line (SIM-555, 2026-09-28): one row per book, each side from that book;
the one-row ``get_odds`` returns the first book on ``GRADED_BOOK_PREFERENCE``.
Expected values are computed straight off the captured fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from betting.bettingpros.fetch_odds import BettingProsOddsProvider
from betting.markets import is_bettable

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"

_OFFERS_BY_MARKET = {
    122: "offers_ml_92857.json",
    175: "offers_total_92857.json",
    176: "offers_runline_92857.json",
    285: "offers_k_92857.json",
}


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


class _FixtureProvider(BettingProsOddsProvider):
    """Provider with the HTTP seams wired to the captured fixtures (no network)."""

    def __init__(self, *, player_full_name: str = "Bryce Miller", **kw):
        super().__init__(api_key="test-key", **kw)
        self._player_full_name = player_full_name

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return _load("mlb_schedule_746437.json")
        if path.startswith("people/"):
            return {"people": [{"fullName": self._player_full_name}]}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            return _load("events_2024-08-15.json")
        if path == "offers":
            return _load(_OFFERS_BY_MARKET[params["market_id"]])
        raise AssertionError(f"unexpected BP path {path}")


# ===========================================================================
# (1) CLOSING-line parsing on the provider
# ===========================================================================
# SIM-555 (2026-09-28) rewrote these pins on purpose. The closing line used to be
# picked per side: the line with the newest stamp across EVERY book, ties to the
# last-listed book, so the two sides of one row could come from two books. The
# provider now returns one closing row per book, each side from that book;
# get_odds returns the first book on GRADED_BOOK_PREFERENCE (DraftKings here).
def test_closing_moneyline_is_one_row_per_book():
    prov = _FixtureProvider()
    rows = {
        r["book"]: r
        for r in prov.get_odds_by_book(746437, line_type="closing", market_type="moneyline")
    }
    assert len(rows) == 10  # nine sportsbooks and the vendor's blend (bp:0)
    # Caesars (13), the old rule's pick, is one book's row: both sides its own.
    assert (rows["bp:13"]["home_ml"], rows["bp:13"]["away_ml"]) == (122, -145)
    odds = prov.get_odds(746437, line_type="closing", market_type="moneyline")
    assert odds["line_type"] == "closing"
    assert odds["book"] == "bp:12"
    assert odds["home_ml"] == 120
    assert odds["away_ml"] == -142


def test_closing_total_and_runline():
    total = _FixtureProvider().get_odds(746437, line_type="closing", market_type="total")
    assert total["book"] == "bp:12"
    assert total["total_line"] == 8.5
    assert total["over_ml"] == -105
    assert total["under_ml"] == -115

    runline = _FixtureProvider().get_odds(746437, line_type="closing", market_type="runline")
    assert runline["book"] == "bp:12"
    assert runline["home_spread"] == 1.5
    assert runline["home_spread_ml"] == -142
    assert runline["away_spread"] == -1.5
    assert runline["away_spread_ml"] == 120


def test_closing_and_current_read_the_same_book_and_opening_reads_the_opener():
    prov = _FixtureProvider()
    opening = prov.get_odds(746437, line_type="opening", market_type="moneyline")
    current = prov.get_odds(746437, line_type="current", market_type="moneyline")
    closing = prov.get_odds(746437, line_type="closing", market_type="moneyline")
    # The opener's row (FanDuel, 126); current and closing are DraftKings' one line (120).
    assert (opening["book"], opening["home_ml"]) == ("bp:10", 126)
    assert (current["book"], current["home_ml"]) == ("bp:12", 120)
    assert (closing["book"], closing["home_ml"]) == ("bp:12", 120)


#: The strikeout prop's closing (line, over, under) per sportsbook in the
#: captured fixture. DraftKings has no offer. BetMGM and PartyCasino post one
#: price; so do BetRivers and SugarHouse.
_K_CLOSES = {
    "bp:10": (5.5, 100, -128),  # FanDuel
    "bp:15": (5.5, -108, -127),  # SugarHouse
    "bp:18": (5.5, -108, -127),  # BetRivers
    "bp:19": (5.5, 100, -135),  # BetMGM
    "bp:27": (5.5, 100, -135),  # PartyCasino
}


def test_closing_prop_is_one_row_per_book():
    # K prop: each row's two sides come from its own book (all at line 5.5).
    prov = _FixtureProvider(player_full_name="Bryce Miller")
    rows = {
        r["book"]: r
        for r in prov.get_prop_odds_by_book(746437, 682243, "strikeouts", line_type="closing")
    }
    closes = {
        book: (row["line"], row["over_ml"], row["under_ml"])
        for book, row in rows.items()
        if is_bettable(book)
    }
    assert closes == _K_CLOSES
    assert all(row["line_type"] == "closing" for row in rows.values())
    assert all(row["is_mock"] is False for row in rows.values())


def test_closing_sugarhouse_row_is_its_own_book():
    # SugarHouse (15) prices DET at 125 / SEA at -148.
    rows = _FixtureProvider().get_odds_by_book(746437, line_type="closing", market_type="moneyline")
    (odds,) = [r for r in rows if r["book"] == "bp:15"]
    assert odds["home_ml"] == 125
    assert odds["away_ml"] == -148


def test_book_line_handles_empty_books_and_unstamped_lines():
    from betting.bettingpros.fetch_odds import _book_line

    # No books at all → None, not an exception.
    assert _book_line({"books": []}, 1) is None
    # A line missing its 'updated' stamp is still a quote.
    line = _book_line({"books": [{"id": 1, "lines": [{"cost": -110, "line": 1.5}]}]}, 1)
    assert line is not None
    assert line["cost"] == -110
    assert line["line"] == 1.5
    # A line flagged is_off is not a quote.
    off = {"books": [{"id": 1, "lines": [{"cost": -110, "line": 1.5, "is_off": True}]}]}
    assert _book_line(off, 1) is None


# ===========================================================================
# (2) The loader's load path — the recorded provider + a recording pool (no Postgres)
# ===========================================================================
from tests.bp_recorded import (  # noqa: E402
    BRYCE_MILLER,
    GAME_92857,
    JULIO_RODRIGUEZ,
    Recorded92857,
)

import betting.history as loader  # noqa: E402
from betting.load import load_odds  # noqa: E402
from betting.markets import (  # noqa: E402
    BATTER_PROP_STATS,
    PITCHER_PROP_STATS,
    PROP_ROLE_BATTER,
    PROP_ROLE_PITCHER,
)
from betting.validate import RefusalTally  # noqa: E402


class _RecordingPool:
    """A stand-in asyncpg pool: records every executemany (no DB)."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql: str, params) -> None:
        self.batches.append((sql, [tuple(p) for p in params]))


@pytest.mark.asyncio
async def test_the_loader_line_types_store_the_opener_and_every_closing_row():
    pool = _RecordingPool()
    written = await load_odds(
        pool,
        Recorded92857(),
        GAME_92857,
        tally=RefusalTally(),
        restamp=False,
        line_types=loader.LINE_TYPES,
    )
    game = [(sql, rows) for sql, rows in pool.batches if "INSERT INTO raw.game_odds" in sql]
    # The opener (FanDuel) on each full-game market, then ten books' closing rows.
    assert written == 3 * (1 + 10)
    assert len(game) == len(pool.batches) == 6
    assert all("ON CONFLICT" in sql for sql, _rows in game)  # idempotent dedup
    assert {row[4] for _sql, rows in game for row in rows} == {"opening", "closing"}
    assert {row[5] for _sql, rows in game for row in rows} == {"moneyline", "runline", "total"}
    openers = [row for _sql, rows in game for row in rows if row[4] == "opening"]
    assert {row[3] for row in openers} == {"bp:10"}


@pytest.mark.asyncio
async def test_the_loader_asks_a_pitcher_and_a_batter_only_their_own_markets():
    pool = _RecordingPool()
    provider = Recorded92857()
    roles = {BRYCE_MILLER: PROP_ROLE_PITCHER, JULIO_RODRIGUEZ: PROP_ROLE_BATTER}
    await load_odds(
        pool,
        provider,
        GAME_92857,
        tally=RefusalTally(),
        restamp=False,
        line_types=("closing",),
        game_markets=None,
        roles=roles,
    )
    rows = [row for sql, batch in pool.batches if "raw.prop_odds" in sql for row in batch]
    pitcher = {row[4] for row in rows if row[1] == BRYCE_MILLER}
    batter = {row[4] for row in rows if row[1] == JULIO_RODRIGUEZ}
    assert pitcher == {"strikeouts", "hits_allowed"} <= set(PITCHER_PROP_STATS)
    assert batter == {"singles"} <= set(BATTER_PROP_STATS)
    # Each player reads only his own markets: the pitcher five, the batter ten.
    market_ids = [p["market_id"] for path, p in provider.bp_calls if path == "offers"]
    assert len(set(market_ids)) == len(PITCHER_PROP_STATS) + len(BATTER_PROP_STATS)
