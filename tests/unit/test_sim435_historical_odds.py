"""
test_sim435_historical_odds.py
==============================
SIM-435 — unit tests for (1) the BettingPros provider's CLOSING-line parsing and
(2) the historical-odds loader (scripts/load_historical_odds.py).

No network and no live DB:
  * the provider's _bp_get / _mlb_get seams are stubbed with the captured
    fixtures (tests/fixtures/bettingpros/) — same pattern as the SIM-405 tests;
  * the loader is driven against a fake provider + a MOCK asyncpg pool/connection
    (records every INSERT) so the per-game opening+closing persist calls are
    asserted without Postgres.

Closing line (SIM-555, 2026-09-28): one row per book, each side from that book;
the one-row ``get_odds`` returns the first book on ``GRADED_BOOK_PREFERENCE``.
Expected values are computed straight off the captured fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider
from pipeline.odds_provider import graded_book_labels, is_bettable

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
    # The default row is the first book on GRADED_BOOK_PREFERENCE with a row
    # (no DraftKings offer here).
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
    quote = prov.get_prop_odds(746437, 682243, "strikeouts", line_type="closing")
    assert quote["line_type"] == "closing"
    graded = next(label for label in graded_book_labels() if label in closes)
    assert quote["book"] == graded
    assert (quote["line"], quote["over_ml"], quote["under_ml"]) == _K_CLOSES[graded]
    assert quote["is_mock"] is False


def test_closing_prefer_book_id_pins_the_book():
    # prefer_book_id pins every call to that book: SugarHouse (15) prices DET at
    # 125 / SEA at -148.
    prov = _FixtureProvider(prefer_book_id=15)
    rows = prov.get_odds_by_book(746437, line_type="closing", market_type="moneyline")
    assert [r["book"] for r in rows] == ["bp:15"]
    odds = prov.get_odds(746437, line_type="closing", market_type="moneyline")
    assert odds["home_ml"] == 125
    assert odds["away_ml"] == -148


def test_closing_prefer_book_id_absent_yields_none():
    odds = _FixtureProvider(prefer_book_id=999_999).get_odds(
        746437, line_type="closing", market_type="moneyline"
    )
    assert odds["home_ml"] is None
    assert odds["away_ml"] is None
    assert odds["source"] == "bettingpros"  # shape preserved


def test_book_line_handles_empty_books_and_unstamped_lines():
    from pipeline.bettingpros_odds_provider import _book_line

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
# (2) The loader — driven against a fake provider + a MOCK pool (no Postgres)
# ===========================================================================
import scripts.load_historical_odds as loader  # noqa: E402


class _RecordingConn:
    """Mock asyncpg connection: records every execute()/executemany()/fetch() (no DB).

    SIM-555: the loader writes one offer's rows in one ``executemany``; each
    row's bind tuple is recorded in ``executes`` as if it were its own
    ``execute``, and ``batches`` counts the executemany calls.
    """

    def __init__(self, lineup_rows: list[dict] | None = None):
        self.executes: list[tuple] = []
        self.batches: list[tuple[str, int]] = []
        self._lineup_rows = lineup_rows or []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def executemany(self, sql: str, params):
        params = list(params)
        self.batches.append((sql, len(params)))
        for args in params:
            self.executes.append((sql, tuple(args)))

    async def fetch(self, sql: str, *args):
        return self._lineup_rows


class _FakeProvider:
    """Provider stub returning a fixed line per (line_type) so we can assert
    that the loader requests + persists BOTH opening and closing per game."""

    def __init__(self):
        self.game_calls: list[tuple] = []
        self.prop_calls: list[tuple] = []

    def get_odds(self, game_pk, *, line_type="current", market_type="moneyline"):
        self.game_calls.append((game_pk, line_type, market_type))
        # moneyline carries ml; other markets carry a line so _has_line is True.
        base = {
            "game_pk": game_pk,
            "source": "bettingpros",
            "is_mock": False,
            "book": "consensus",
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": False,
            "home_ml": -110 if market_type == "moneyline" else None,
            "away_ml": 100 if market_type == "moneyline" else None,
            "home_spread": 1.5 if market_type == "runline" else None,
            "home_spread_ml": -120 if market_type == "runline" else None,
            "away_spread": -1.5 if market_type == "runline" else None,
            "away_spread_ml": 100 if market_type == "runline" else None,
            "total_line": 8.5 if market_type == "total" else None,
            "over_ml": -105 if market_type == "total" else None,
            "under_ml": -115 if market_type == "total" else None,
        }
        return base

    def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
        self.prop_calls.append((game_pk, player_id, prop_stat, line_type))
        return {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": 5.5,
            "over_ml": -110,
            "under_ml": -110,
            "book": "consensus",
            "line_type": line_type,
            "is_sharp_book": False,
            "source": "bettingpros",
            "is_mock": False,
        }


async def _batch(sink: list, game_pk: int, rows: list[dict]) -> int:
    """SIM-555: a game batch writer stand-in — one entry per row, returns the count."""
    sink.extend((game_pk, odds) for odds in rows)
    return len(rows)


@pytest.mark.asyncio
async def test_load_game_odds_persists_opening_and_closing_each_market():
    provider = _FakeProvider()
    persisted: list[tuple[int, dict]] = []

    async def persist(game_pk, rows):
        return await _batch(persisted, game_pk, rows)

    written = await loader._load_game_odds(provider, persist, 746437)

    # 2 line_types × 3 market_types = 6 rows (all resolve in the fake; the
    # twelve segment markets come back empty from it and are skipped).
    assert written == 6
    assert len(persisted) == 6
    line_types = {odds["line_type"] for _, odds in persisted}
    assert line_types == {"opening", "closing"}
    markets = {odds["market_type"] for _, odds in persisted}
    assert markets == {"moneyline", "runline", "total"}
    # The provider was asked for both opening and closing on each market.
    assert provider.game_calls[0][1:] == ("opening", "moneyline")
    assert any(c == (746437, "closing", "total") for c in provider.game_calls)


@pytest.mark.asyncio
async def test_load_game_odds_skips_empty_quotes():
    class _EmptyProvider(_FakeProvider):
        def get_odds(self, game_pk, *, line_type="current", market_type="moneyline"):
            self.game_calls.append((game_pk, line_type, market_type))
            base = super().get_odds(game_pk, line_type=line_type, market_type=market_type)
            for k in (
                "home_ml",
                "away_ml",
                "home_spread",
                "home_spread_ml",
                "away_spread",
                "away_spread_ml",
                "total_line",
                "over_ml",
                "under_ml",
            ):
                base[k] = None
            return base

    provider = _EmptyProvider()
    persisted: list = []
    tally = loader.RefusalTally()

    async def persist(game_pk, rows):
        return await _batch(persisted, game_pk, rows)

    written = await loader._load_game_odds(provider, persist, 1, tally=tally)
    assert written == 0
    assert persisted == []  # an all-null quote is never persisted
    # SIM-555: an empty row is skipped before the guard, never counted as refused.
    assert tally.n_offered == 0 and tally.refused == 0


@pytest.mark.asyncio
async def test_load_prop_odds_routes_pitcher_vs_batter_markets():
    provider = _FakeProvider()
    persisted: list[dict] = []

    async def persist(rows):
        persisted.extend(rows)
        return len(rows)

    players = [(111, True), (222, False)]  # one pitcher, one batter
    written = await loader._load_prop_odds(provider, persist, 999, players)

    # SIM-421 changed this pin deliberately: it was pitcher 3 + batter 4 markets
    # (14 rows). The vocabulary is now 5 pitcher + 10 batter markets, each at 2
    # line_types: 10 + 20 = 30.
    assert len(loader.PITCHER_PROP_STATS) == 5
    assert len(loader.BATTER_PROP_STATS) == 10
    assert written == 30
    pitcher_stats = {q["prop_stat"] for q in persisted if q["player_id"] == 111}
    batter_stats = {q["prop_stat"] for q in persisted if q["player_id"] == 222}
    assert pitcher_stats == set(loader.PITCHER_PROP_STATS)
    assert batter_stats == set(loader.BATTER_PROP_STATS)
    # both line_types captured per player.
    assert {q["line_type"] for q in persisted} == {"opening", "closing"}


@pytest.mark.asyncio
async def test_load_prop_odds_skips_null_line():
    class _NullLineProvider(_FakeProvider):
        def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
            q = super().get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)
            return {**q, "line": None}

    provider = _NullLineProvider()
    persisted: list[dict] = []
    tally = loader.RefusalTally()

    async def persist(rows):
        persisted.extend(rows)
        return len(rows)

    written = await loader._load_prop_odds(provider, persist, 1, [(111, True)], tally=tally)
    assert written == 0
    assert persisted == []
    # SIM-555: a quote with prices but no line is not empty, so the load guard
    # sees it and refuses it (the line is missing): 5 pitcher markets × 2 line types.
    assert tally.refused == 10
    assert {rule for rule, _market, _book in tally.counts()} == {"missing_side"}


@pytest.mark.asyncio
async def test_load_prop_odds_skips_an_all_empty_quote_silently():
    class _EmptyPropProvider(_FakeProvider):
        def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
            q = super().get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)
            return {**q, "line": None, "over_ml": None, "under_ml": None}

    tally = loader.RefusalTally()

    async def persist(rows):  # pragma: no cover - never reached
        raise AssertionError("an empty quote must never reach the writer")

    written = await loader._load_prop_odds(
        _EmptyPropProvider(), persist, 1, [(111, True)], tally=tally
    )
    assert written == 0
    assert tally.n_offered == 0 and tally.refused == 0


@pytest.mark.asyncio
async def test_load_prop_odds_propagates_unknown_stat_valueerror():
    class _BadStatProvider(_FakeProvider):
        def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
            raise ValueError(f"Unknown prop_stat '{prop_stat}'")

    provider = _BadStatProvider()

    async def persist(quote):  # pragma: no cover - never reached
        raise AssertionError("should not persist on a programming error")

    with pytest.raises(ValueError, match="Unknown prop_stat"):
        await loader._load_prop_odds(provider, persist, 1, [(111, True)])


@pytest.mark.asyncio
async def test_fetch_lineup_players_classifies_pitchers():
    conn = _RecordingConn(
        lineup_rows=[
            {"player_id": 1, "position_code": "P"},
            {"player_id": 2, "position_code": "SP"},
            {"player_id": 3, "position_code": "CF"},
            {"player_id": 4, "position_code": None},
        ]
    )
    players = await loader._fetch_lineup_players(conn, 746437)
    by_id = dict(players)
    assert by_id[1] is True
    assert by_id[2] is True
    assert by_id[3] is False
    assert by_id[4] is False


def test_build_persisters_attaches_pool_without_starting_pipeline():
    sentinel_pool = object()
    persist_game, persist_prop = loader._build_persisters("postgresql://x", sentinel_pool)
    # Both are bound methods on a LiveIngestionPipeline whose _db is our pool —
    # i.e. the real persist path, never started (no Redis/HTTP/WS).
    assert persist_game.__self__ is persist_prop.__self__
    assert persist_game.__self__._db is sentinel_pool
    # SIM-555 changed these names on purpose: the loader writes one offer's
    # rows in one batch.
    assert persist_game.__name__ == "_persist_odds_many"
    assert persist_prop.__name__ == "_persist_prop_odds_many"


@pytest.mark.asyncio
async def test_persisters_emit_game_and_prop_inserts_against_mock_conn():
    # End-to-end through the REAL _persist_odds/_persist_prop_odds against a mock
    # connection: asserts the loader's persist path writes raw.game_odds /
    # raw.prop_odds INSERTs carrying the line_type we pass (opening + closing).
    conn = _RecordingConn()
    persist_game, persist_prop = loader._build_persisters("postgresql://x", conn)

    provider = _FakeProvider()
    await loader._load_game_odds(provider, persist_game, 555)
    await loader._load_prop_odds(provider, persist_prop, 555, [(111, True)])

    sqls = [sql for sql, _ in conn.executes]
    assert any("INSERT INTO raw.game_odds" in s for s in sqls)
    assert any("INSERT INTO raw.prop_odds" in s for s in sqls)
    assert all("ON CONFLICT" in s for s in sqls)  # idempotent dedup on every write
    # opening + closing both reached the DB (line_type is positional arg #5 in
    # the game-odds INSERT; assert both appear across the recorded executes).
    game_line_types = {args[4] for sql, args in conn.executes if "raw.game_odds" in sql}
    assert game_line_types == {"opening", "closing"}
    # SIM-555: every write went through executemany (one batch per offer).
    assert len(conn.batches) == len(conn.executes)
