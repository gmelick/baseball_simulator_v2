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

Closing line = the most-recently-``updated`` line (the last line posted before
game time). Expected values are computed straight off the captured fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider

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
def test_closing_moneyline_picks_latest_updated_line():
    # DET/SEA: the two latest-updated books tie at 2024-08-15 17:07:24
    # (DET book13 cost=122, SEA book13 cost=-145) — the closing line, distinct
    # from the *best* current price (DET 125 / SEA -135).
    odds = _FixtureProvider().get_odds(746437, line_type="closing", market_type="moneyline")
    assert odds["line_type"] == "closing"
    assert odds["home_ml"] == 122
    assert odds["away_ml"] == -145


def test_closing_total_and_runline():
    total = _FixtureProvider().get_odds(746437, line_type="closing", market_type="total")
    assert total["total_line"] == 8.5
    assert total["over_ml"] == -105
    assert total["under_ml"] == -115

    runline = _FixtureProvider().get_odds(746437, line_type="closing", market_type="runline")
    assert runline["home_spread"] == 1.5
    assert runline["home_spread_ml"] == -135
    assert runline["away_spread"] == -1.5
    assert runline["away_spread_ml"] == 115


def test_closing_differs_from_current_and_opening():
    prov = _FixtureProvider()
    opening = prov.get_odds(746437, line_type="opening", market_type="moneyline")
    current = prov.get_odds(746437, line_type="current", market_type="moneyline")
    closing = prov.get_odds(746437, line_type="closing", market_type="moneyline")
    # opening (126) != current best (125) != closing latest-updated (122).
    assert opening["home_ml"] == 126
    assert current["home_ml"] == 125
    assert closing["home_ml"] == 122


def test_closing_prop_picks_latest_updated_per_selection():
    # K prop: Over closes at book18 17:01:41 cost=-108; Under closes at
    # book15 16:41:42 cost=-127. (Both at line 5.5.)
    quote = _FixtureProvider(player_full_name="Bryce Miller").get_prop_odds(
        746437, 682243, "strikeouts", line_type="closing"
    )
    assert quote["line_type"] == "closing"
    assert quote["line"] == 5.5
    assert quote["over_ml"] == -108
    assert quote["under_ml"] == -127
    assert quote["is_mock"] is False


def test_closing_prefer_book_id_scopes_the_scan():
    # prefer_book_id restricts closing to that book's lines: book 15 prices DET
    # at 125 / SEA at -148 (vs the all-books closing of 122 / -145).
    odds = _FixtureProvider(prefer_book_id=15).get_odds(
        746437, line_type="closing", market_type="moneyline"
    )
    assert odds["home_ml"] == 125
    assert odds["away_ml"] == -148


def test_closing_prefer_book_id_absent_yields_none():
    odds = _FixtureProvider(prefer_book_id=999_999).get_odds(
        746437, line_type="closing", market_type="moneyline"
    )
    assert odds["home_ml"] is None
    assert odds["away_ml"] is None
    assert odds["source"] == "bettingpros"  # shape preserved


def test_closing_handles_empty_books_via_pick_line_directly():
    prov = _FixtureProvider()
    # No books at all → (None, None), not an exception.
    assert prov._pick_line({"books": []}, "closing") == (None, None)
    # A line missing its 'updated' stamp is still selectable (empty-string key).
    cost, line = prov._pick_line(
        {"books": [{"id": 1, "lines": [{"cost": -110, "line": 1.5}]}]}, "closing"
    )
    assert cost == -110
    assert line == 1.5


# ===========================================================================
# (2) The loader — driven against a fake provider + a MOCK pool (no Postgres)
# ===========================================================================
import scripts.load_historical_odds as loader  # noqa: E402


class _RecordingConn:
    """Mock asyncpg connection: records every execute()/fetch() (no DB)."""

    def __init__(self, lineup_rows: list[dict] | None = None):
        self.executes: list[tuple] = []
        self._lineup_rows = lineup_rows or []

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

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


@pytest.mark.asyncio
async def test_load_game_odds_persists_opening_and_closing_each_market():
    provider = _FakeProvider()
    persisted: list[tuple[int, dict]] = []

    async def persist(game_pk, odds):
        persisted.append((game_pk, odds))

    written = await loader._load_game_odds(provider, persist, 746437)

    # 2 line_types × 3 market_types = 6 rows (all resolve in the fake).
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
    persisted = []

    async def persist(game_pk, odds):
        persisted.append((game_pk, odds))

    written = await loader._load_game_odds(provider, persist, 1)
    assert written == 0
    assert persisted == []  # an all-null quote is never persisted


@pytest.mark.asyncio
async def test_load_prop_odds_routes_pitcher_vs_batter_markets():
    provider = _FakeProvider()
    persisted: list[dict] = []

    async def persist(quote):
        persisted.append(quote)

    players = [(111, True), (222, False)]  # one pitcher, one batter
    written = await loader._load_prop_odds(provider, persist, 999, players)

    # pitcher: 3 stats × 2 line_types = 6 ; batter: 4 stats × 2 = 8 ; total 14.
    assert written == 14
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
    persisted = []

    async def persist(quote):
        persisted.append(quote)

    written = await loader._load_prop_odds(provider, persist, 1, [(111, True)])
    assert written == 0
    assert persisted == []


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
    assert persist_game.__name__ == "_persist_odds"
    assert persist_prop.__name__ == "_persist_prop_odds"


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
