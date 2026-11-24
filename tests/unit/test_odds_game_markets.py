"""Every game market the book posts (the prop-market work, SIM-421, owner ruling 2026-09-12).

The odds tables carry all 15 game markets: the three full-game markets and the
twelve segment / team markets BettingPros posts on every game. These tests
drive the vocabulary, the real provider's parsers (against captured payloads
from a live event on 2026-09-12: ATL vs TB, BettingPros event 99026), the dedup
hash, the insert, the live cycle, the load path and migration 0024. Every row
comes from the real provider over the recorded responses; no test makes up a price.

Fixtures: ``tests/fixtures/bettingpros/offers_<market>_99026.json`` — the
``/offers`` response per market, books trimmed to three, values untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tests.bp_recorded import GAME_99026, Recorded99026

import betting.historical_loader
from betting.bettingpros.fetch_odds import _GAME_MARKET_IDS, BettingProsOddsProvider
from betting.load_odds_to_db import _odds_hash, insert_game_odds_rows, load_odds
from betting.markets import (
    FULL_GAME_MARKET_TYPES,
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
)
from betting.validate_odds import RefusalTally
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"
_MIGRATION = (
    Path(__file__).resolve().parent.parent.parent
    / "db"
    / "migrations"
    / "versions"
    / "0024_sim421_game_odds_all_markets.py"
)
_DDL = Path(__file__).resolve().parent.parent.parent / "db" / "schemas" / "01_postgres_schema.sql"

#: BettingPros market id → the captured fixture for event 99026.
_FIXTURE_BY_MARKET = {
    278: "offers_f1_moneyline_99026.json",
    279: "offers_f5_moneyline_99026.json",
    280: "offers_f1_total_99026.json",
    281: "offers_f5_total_99026.json",
    282: "offers_f1_runline_99026.json",
    283: "offers_f5_runline_99026.json",
    277: "offers_team_total_99026.json",
    407: "offers_f5_team_total_99026.json",
    286: "offers_first_to_score_99026.json",
    369: "offers_first_inning_run_99026.json",
}

SEGMENT_MARKETS = tuple(m for m in GAME_MARKET_TYPES if m not in FULL_GAME_MARKET_TYPES)


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


class _SegmentFixtureProvider(BettingProsOddsProvider):
    """The real provider with the event resolved to ATL (home) vs TB (away) and
    the ``/offers`` seam served from the captured payloads."""

    def __init__(self, **kw):
        super().__init__(api_key="test-key", **kw)
        self.offers_calls: list[tuple[int, int]] = []

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 99026, "home": "ATL", "visitor": "TB", "scheduled": "2026-09-10 16:15:00"}

    def _mlb_get(self, path, params):  # type: ignore[override]
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        assert path == "offers", path
        self.offers_calls.append((int(params["event_id"]), int(params["market_id"])))
        return _load(_FIXTURE_BY_MARKET[params["market_id"]])


# ===========================================================================
# The vocabulary
# ===========================================================================


class TestVocabulary:
    def test_fifteen_markets_in_canonical_order(self):
        assert len(GAME_MARKET_TYPES) == 15
        assert GAME_MARKET_TYPES[:3] == FULL_GAME_MARKET_TYPES
        assert len(set(GAME_MARKET_TYPES)) == 15

    def test_every_market_has_a_kind_a_segment_and_a_side(self):
        for m in GAME_MARKET_TYPES:
            assert GAME_MARKET_KIND[m] in {"moneyline", "three_way", "runline", "total", "yes_no"}
            assert GAME_MARKET_SEGMENT[m] in {"game", "f1", "f5"}
            assert GAME_MARKET_SIDE[m] in {None, "home", "away"}
        assert set(GAME_MARKET_KIND) == set(GAME_MARKET_TYPES)

    def test_team_totals_name_their_side_and_nothing_else_does(self):
        sided = {m for m in GAME_MARKET_TYPES if GAME_MARKET_SIDE[m] is not None}
        assert sided == {
            "team_total_home",
            "team_total_away",
            "f5_team_total_home",
            "f5_team_total_away",
        }
        assert GAME_MARKET_SIDE["team_total_home"] == "home"
        assert GAME_MARKET_SIDE["f5_team_total_away"] == "away"

    def test_market_ids_cover_the_vocabulary(self):
        for m in GAME_MARKET_TYPES:
            assert m in _GAME_MARKET_IDS
        # Both team-total sides read the same BettingPros market.
        assert _GAME_MARKET_IDS["team_total_home"] == _GAME_MARKET_IDS["team_total_away"] == 277
        assert _GAME_MARKET_IDS["f5_team_total_home"] == _GAME_MARKET_IDS["f5_team_total_away"]

    def test_migration_0024_lists_the_same_fifteen_values(self):
        text = _MIGRATION.read_text(encoding="utf-8")
        assert 'revision = "0024"' in text and 'down_revision = "0023"' in text
        for m in GAME_MARKET_TYPES:
            assert f'"{m}"' in text, m
        assert "draw_ml" in text
        assert "DELETE FROM raw.game_odds" in text  # the documented destructive downgrade

    def test_reference_ddl_lists_the_same_fifteen_values(self):
        text = _DDL.read_text(encoding="utf-8")
        block = text[text.index("CREATE TABLE IF NOT EXISTS raw.game_odds") :]
        block = block[: block.index(");")]
        for m in GAME_MARKET_TYPES:
            assert f"'{m}'" in block, m
        assert re.search(r"draw_ml\s+INTEGER", block)


# ===========================================================================
# The real provider on captured payloads
# ===========================================================================


class TestProviderSegmentMarkets:
    """Every expected number below is the selection's ``opening_line`` in the
    captured payload, or, for the current and closing lines, one named book's
    own line on the named selection (one row per book), so each test
    checks WHICH selection landed in WHICH column."""

    @staticmethod
    def _book_cost(market_id: int, book_id: int, *, participant=None, selection=None):
        from betting.bettingpros.fetch_odds import _book_line

        sel_list = _load(_FIXTURE_BY_MARKET[market_id])["offers"][0]["selections"]
        for sel in sel_list:
            if participant is not None and sel.get("participant") == participant:
                return _book_line(sel, book_id)["cost"]
            if selection is not None and (sel.get("selection") or "").lower() == selection:
                return _book_line(sel, book_id)["cost"]
        raise AssertionError("selection not in fixture")

    def test_first_inning_moneyline_is_three_way_opening(self):
        odds = _SegmentFixtureProvider().get_odds(
            1, market_type="f1_moneyline", line_type="opening"
        )
        # ATL is home, TB away; Caesars opened both. The tie opened at FanDuel,
        # another book, so the Caesars opening row carries no tie.
        assert odds["book"] == "bp:13"
        assert odds["home_ml"] == -120.0 and odds["away_ml"] == -110.0
        assert odds["draw_ml"] is None
        assert odds["total_line"] is None and odds["home_spread"] is None
        assert odds["market_type"] == "f1_moneyline"

    def test_first_inning_moneyline_current_routes_each_selection(self):
        p = _SegmentFixtureProvider()
        odds = p.get_odds(1, market_type="f1_moneyline")
        # FanDuel is the first book on the preference with a row; every side is its own.
        assert odds["book"] == "bp:10"
        assert odds["home_ml"] == self._book_cost(278, 10, participant="ATL")
        assert odds["away_ml"] == self._book_cost(278, 10, participant="TB")
        assert odds["draw_ml"] == self._book_cost(278, 10, selection="draw")

    def test_first_five_moneyline_ignores_the_folded_yes_no_pair(self):
        # The live payload carries Yes / No selections beside Home / Away / Draw;
        # they are not a moneyline price and must not land in any column.
        odds = _SegmentFixtureProvider().get_odds(
            1, market_type="f5_moneyline", line_type="opening"
        )
        assert odds["home_ml"] == 112.0 and odds["away_ml"] == 120.0
        assert odds["draw_ml"] == 460.0
        assert odds["over_ml"] is None and odds["under_ml"] is None

    def test_first_inning_and_first_five_totals(self):
        p = _SegmentFixtureProvider()
        f1 = p.get_odds(1, market_type="f1_total", line_type="opening")
        assert (f1["total_line"], f1["over_ml"], f1["under_ml"]) == (1.5, 240.0, -340.0)
        f5 = p.get_odds(1, market_type="f5_total", line_type="opening")
        assert (f5["total_line"], f5["over_ml"], f5["under_ml"]) == (4.5, -125.0, -105.0)
        for o in (f1, f5):
            assert o["home_ml"] is None and o["draw_ml"] is None

    def test_segment_run_lines_carry_each_side_spread(self):
        p = _SegmentFixtureProvider()
        f5 = p.get_odds(1, market_type="f5_runline", line_type="opening")
        assert (f5["home_spread"], f5["home_spread_ml"]) == (-0.5, 115.0)
        assert (f5["away_spread"], f5["away_spread_ml"]) == (0.5, -150.0)
        f1 = p.get_odds(1, market_type="f1_runline", line_type="opening")
        assert (f1["home_spread"], f1["home_spread_ml"]) == (-0.5, 280.0)
        assert (f1["away_spread"], f1["away_spread_ml"]) == (0.5, -400.0)

    def test_team_totals_pick_the_side_offer(self):
        p = _SegmentFixtureProvider()
        home = p.get_odds(1, market_type="team_total_home", line_type="opening")
        away = p.get_odds(1, market_type="team_total_away", line_type="opening")
        # ATL (home) opened at 4.5, TB (away) at 3.5 — two different offers.
        assert (home["total_line"], home["over_ml"], home["under_ml"]) == (4.5, 110.0, -145.0)
        assert (away["total_line"], away["over_ml"], away["under_ml"]) == (3.5, -140.0, 105.0)
        f5h = p.get_odds(1, market_type="f5_team_total_home", line_type="opening")
        f5a = p.get_odds(1, market_type="f5_team_total_away", line_type="opening")
        assert (f5h["total_line"], f5h["over_ml"], f5h["under_ml"]) == (2.5, 120.0, -150.0)
        assert (f5a["total_line"], f5a["over_ml"], f5a["under_ml"]) == (1.5, -150.0, 120.0)

    def test_both_team_total_sides_share_one_fetch(self):
        p = _SegmentFixtureProvider()
        p.get_odds(1, market_type="team_total_home")
        p.get_odds(1, market_type="team_total_away")
        assert p.offers_calls == [(99026, 277)]

    def test_first_team_to_score_is_a_two_way_moneyline(self):
        odds = _SegmentFixtureProvider().get_odds(
            1, market_type="first_to_score", line_type="opening"
        )
        assert odds["home_ml"] == 115.0 and odds["away_ml"] == -150.0
        assert odds["draw_ml"] is None

    def test_run_in_first_inning_is_stored_as_over_under_at_half(self):
        odds = _SegmentFixtureProvider().get_odds(
            1, market_type="first_inning_run", line_type="opening"
        )
        assert odds["total_line"] == 0.5
        assert odds["over_ml"] == -128.0  # Yes
        assert odds["under_ml"] == 100.0  # No
        assert odds["home_ml"] is None

    def test_closing_is_one_row_per_book(self):
        # The closing line is one row per book, each side from that book.
        p = _SegmentFixtureProvider()
        rows = p.get_odds_by_book(1, market_type="f1_total", line_type="closing")
        assert [r["book"] for r in rows] == ["bp:0", "bp:12", "bp:14"]
        for row in rows:
            book_id = row["book_id"]
            assert row["over_ml"] == self._book_cost(280, book_id, selection="over")
            assert row["under_ml"] == self._book_cost(280, book_id, selection="under")
            assert row["total_line"] == 1.5
        odds = p.get_odds(1, market_type="f1_total", line_type="closing")
        assert odds["book"] == "bp:12"  # DraftKings: first on the preference
        assert (odds["total_line"], odds["over_ml"], odds["under_ml"]) == (1.5, 205.0, -275.0)

    def test_a_segment_market_never_fills_another_market_field(self):
        p = _SegmentFixtureProvider()
        for m in SEGMENT_MARKETS:
            odds = p.get_odds(1, market_type=m)
            kind = GAME_MARKET_KIND[m]
            filled = {f for f in GAME_ODDS_FIELDS if odds[f] is not None}
            allowed = {
                "moneyline": {"home_ml", "away_ml"},
                "three_way": {"home_ml", "away_ml", "draw_ml"},
                "runline": {"home_spread", "home_spread_ml", "away_spread", "away_spread_ml"},
                "total": {"total_line", "over_ml", "under_ml"},
                "yes_no": {"total_line", "over_ml", "under_ml"},
            }[kind]
            assert filled <= allowed, (m, filled)
            assert filled, m  # every captured market resolved something

    def test_unknown_market_type_raises_like_a_prop(self):
        with pytest.raises(ValueError, match="Unknown market_type"):
            _SegmentFixtureProvider().get_odds(1, market_type="sixth_inning_total")

    def test_missing_team_offer_leaves_the_market_empty(self):
        p = _SegmentFixtureProvider()
        p._resolve_event = lambda game_pk: {"id": 99026, "home": "NYY", "visitor": "TB"}  # type: ignore[method-assign]
        odds = p.get_odds(1, market_type="team_total_home")
        assert all(odds[f] is None for f in GAME_ODDS_FIELDS)


# ===========================================================================
# The hash and the insert
# ===========================================================================


def _three_way_row() -> dict:
    """A recorded first-inning moneyline row that prices the tie."""
    rows = Recorded99026().get_odds_by_book(GAME_99026, market_type="f1_moneyline")
    return next(r for r in rows if r["draw_ml"] is not None)


class TestHashAndPersist:
    def test_two_way_hash_is_unchanged_by_the_new_column(self):
        # A two-way payload hashes the same with or without the draw_ml key,
        # so every stored two-way hash still deduplicates.
        odds = Recorded99026().get_odds_by_book(GAME_99026, market_type="f1_total")[0]
        without = {k: v for k, v in odds.items() if k != "draw_ml"}
        assert _odds_hash(odds) == _odds_hash(without)

    def test_draw_price_changes_the_hash(self):
        odds = _three_way_row()
        moved = dict(odds, draw_ml=odds["draw_ml"] + 10)
        assert _odds_hash(odds) != _odds_hash(moved)

    @pytest.mark.asyncio
    async def test_insert_writes_draw_ml_and_keeps_the_hash_last(self):
        conn = AsyncMock()
        odds = _three_way_row()
        await insert_game_odds_rows(conn, GAME_99026, [odds])
        sql, params = conn.executemany.await_args.args
        (args,) = params
        assert "draw_ml" in sql
        assert args[-1] == _odds_hash(odds)
        # book_line_at sits between the tie price and the hash.
        assert args[-2] == odds["book_line_at"]
        assert args[-3] == odds["draw_ml"]
        assert args[5] == "f1_moneyline"  # market_type keeps its slot


# ===========================================================================
# The live cycle: every game market, every book, on the cadence
# ===========================================================================

#: The recorded segment rows the load guard keeps across the twelve current
#: offers: 32 rows (it refuses two first-five moneyline rows).
_KEPT_CURRENT_ROWS = 32
_CURRENT_OFFERS = 12


class TestLiveGameMarketCycle:
    """The live cycle persists every book's row of all fifteen markets on the
    cadence, one batch per market. It stores no per-pitch row."""

    def _pipeline(self, provider=None) -> LiveIngestionPipeline:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        p._db = AsyncMock()
        p._odds = provider or Recorded99026()
        p._last_game_odds_fetch = {}
        p._last_seen_column = False
        return p

    @pytest.mark.asyncio
    async def test_cycle_persists_every_market_then_respects_the_cadence(self):
        p = self._pipeline()
        assert await p._persist_game_odds_cycle(GAME_99026) == _KEPT_CURRENT_ROWS
        # One executemany per market; no single-row insert.
        assert p._db.executemany.await_count == _CURRENT_OFFERS
        p._db.execute.assert_not_awaited()
        markets = [call.args[1][0][5] for call in p._db.executemany.await_args_list]
        assert markets == list(SEGMENT_MARKETS)
        # The gate is closed until the cadence elapses: nothing more is written.
        assert await p._persist_game_odds_cycle(GAME_99026) == 0
        assert p._db.executemany.await_count == _CURRENT_OFFERS

    @pytest.mark.asyncio
    async def test_a_failing_market_does_not_stop_the_others(self):
        class _Flaky(Recorded99026):
            def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline"):
                if market_type == "f5_total":
                    raise RuntimeError("boom")
                return super().get_odds_by_book(
                    game_pk, line_type=line_type, market_type=market_type
                )

        p = self._pipeline(_Flaky())
        # The f5 total's two recorded rows are missing; every other market lands.
        assert await p._persist_game_odds_cycle(GAME_99026) == _KEPT_CURRENT_ROWS - 2
        markets = [call.args[1][0][5] for call in p._db.executemany.await_args_list]
        assert "f5_total" not in markets
        assert len(markets) == _CURRENT_OFFERS - 1

    @pytest.mark.asyncio
    async def test_a_failing_batch_does_not_stop_the_others(self):
        p = self._pipeline()
        calls: list[int] = []

        async def _executemany(sql, params):
            calls.append(len(params))
            if len(calls) == 2:
                raise RuntimeError("db hiccup")

        p._db.executemany = _executemany
        written = await p._persist_game_odds_cycle(GAME_99026)
        assert len(calls) == _CURRENT_OFFERS
        assert written == _KEPT_CURRENT_ROWS - calls[1]


# ===========================================================================
# The loader
# ===========================================================================


class TestLoaderGameMarkets:
    def _loader(self):
        return betting.historical_loader

    def test_default_is_every_market(self):
        loader = self._loader()
        assert loader._select_game_markets(None) == GAME_MARKET_TYPES

    def test_subset_keeps_canonical_order_and_rejects_unknown(self):
        loader = self._loader()
        assert loader._select_game_markets(["f5_total", "f1_total"]) == ("f1_total", "f5_total")
        with pytest.raises(ValueError, match="Unknown market_type"):
            loader._select_game_markets(["f1_total", "seventh_inning_total"])

    def test_parse_args_accepts_game_markets(self):
        loader = self._loader()
        args = loader.parse_args(
            ["--seasons", "2024", "--game-markets", "first_inning_run", "team_total_home"]
        )
        assert args.game_markets == ["first_inning_run", "team_total_home"]

    @pytest.mark.asyncio
    async def test_load_odds_writes_every_market_and_line_type(self):
        conn = AsyncMock()
        written = await load_odds(
            conn,
            Recorded99026(),
            GAME_99026,
            tally=RefusalTally(),
            restamp=False,
            line_types=("opening", "closing"),
        )
        rows = [args for call in conn.executemany.await_args_list for args in call.args[1]]
        assert written == len(rows)
        # One batch per (line type, market): twelve openers and twelve closers.
        assert conn.executemany.await_count == 24
        assert {r[5] for r in rows} == set(SEGMENT_MARKETS)
        assert {r[4] for r in rows} == {"opening", "closing"}

    @pytest.mark.asyncio
    async def test_a_narrowed_market_set_writes_only_those(self):
        conn = AsyncMock()
        provider = Recorded99026()
        written = await load_odds(
            conn,
            provider,
            GAME_99026,
            tally=RefusalTally(),
            restamp=False,
            line_types=("closing",),
            game_markets=("first_inning_run", "f5_total"),
        )
        rows = [args for call in conn.executemany.await_args_list for args in call.args[1]]
        assert written == len(rows) == 3 + 2
        assert [r[5] for r in rows] == ["first_inning_run"] * 3 + ["f5_total"] * 2
        # The first-five total also reads the first-inning total (280) to rule out a copy.
        assert set(provider.offers_calls) == {369, 281, 280}
