"""SIM-421 (owner ruling 2026-09-12) — every game market the book posts.

The odds tables carry all 15 game markets: the three full-game markets and the
twelve segment / team markets BettingPros posts on every game. These tests
drive the vocabulary, the real provider's parsers (against captured payloads
from a live event on 2026-09-12: ATL vs TB, BettingPros event 99026), the mock
provider, the dedup hash, the insert, the live cycle, the loader's routing and
migration 0024.

Fixtures: ``tests/fixtures/bettingpros/offers_<market>_99026.json`` — the
``/offers`` response per market, books trimmed to three, values untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pipeline.bettingpros_odds_provider import _GAME_MARKET_IDS, BettingProsOddsProvider
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline, MockOddsAPI
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SEGMENT,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    LEGACY_GAME_MARKET_TYPES,
    THREE_WAY_GAME_MARKET_TYPES,
)

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

SEGMENT_MARKETS = tuple(m for m in GAME_MARKET_TYPES if m not in LEGACY_GAME_MARKET_TYPES)


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
        assert GAME_MARKET_TYPES[:3] == LEGACY_GAME_MARKET_TYPES
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

    def test_three_way_markets_are_the_two_segment_moneylines(self):
        assert THREE_WAY_GAME_MARKET_TYPES == ("f1_moneyline", "f5_moneyline")

    def test_market_ids_cover_the_vocabulary(self):
        for m in GAME_MARKET_TYPES:
            assert m in _GAME_MARKET_IDS
        # Both team-total sides read the same BettingPros market.
        assert _GAME_MARKET_IDS["team_total_home"] == _GAME_MARKET_IDS["team_total_away"] == 277
        assert _GAME_MARKET_IDS["f5_team_total_home"] == _GAME_MARKET_IDS["f5_team_total_away"]
        assert _GAME_MARKET_IDS["run_line"] == _GAME_MARKET_IDS["runline"]

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
    captured payload — the one price that does not depend on which book the
    current-line pick prefers — or, for the current line, the provider's own
    ``_pick_line`` on the named selection, so each test checks WHICH selection
    landed in WHICH column."""

    @staticmethod
    def _current(p, market_id: int, *, offer: int = 0, participant=None, selection=None):
        sel_list = _load(_FIXTURE_BY_MARKET[market_id])["offers"][offer]["selections"]
        for sel in sel_list:
            if participant is not None and sel.get("participant") == participant:
                return p._pick_line(sel, "current")
            if selection is not None and (sel.get("selection") or "").lower() == selection:
                return p._pick_line(sel, "current")
        raise AssertionError("selection not in fixture")

    def test_first_inning_moneyline_is_three_way_opening(self):
        odds = _SegmentFixtureProvider().get_odds(
            1, market_type="f1_moneyline", line_type="opening"
        )
        # ATL is home, TB away; the tie is its own price.
        assert odds["home_ml"] == -120.0 and odds["away_ml"] == -110.0
        assert odds["draw_ml"] == -120.0
        assert odds["total_line"] is None and odds["home_spread"] is None
        assert odds["market_type"] == "f1_moneyline"

    def test_first_inning_moneyline_current_routes_each_selection(self):
        p = _SegmentFixtureProvider()
        odds = p.get_odds(1, market_type="f1_moneyline")
        assert odds["home_ml"] == self._current(p, 278, participant="ATL")[0]
        assert odds["away_ml"] == self._current(p, 278, participant="TB")[0]
        assert odds["draw_ml"] == self._current(p, 278, selection="draw")[0]

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

    def test_closing_line_is_the_latest_update_across_books(self):
        p = _SegmentFixtureProvider()
        odds = p.get_odds(1, market_type="f1_total", line_type="closing")
        sels = _load(_FIXTURE_BY_MARKET[280])["offers"][0]["selections"]
        over = next(s for s in sels if s["selection"] == "over")
        assert odds["over_ml"] == p._pick_line(over, "closing")[0]
        assert odds["total_line"] == 1.5

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
# The mock provider
# ===========================================================================


class TestMockSegmentMarkets:
    def test_every_market_type_is_served_and_deterministic(self):
        for m in GAME_MARKET_TYPES:
            a = MockOddsAPI.get_odds(745000, market_type=m)
            b = MockOddsAPI.get_odds(745000, market_type=m)
            assert a == b
            assert a["market_type"] == m
            assert set(GAME_ODDS_FIELDS) <= set(a)

    def test_legacy_markets_keep_the_full_game_shape(self):
        for m in LEGACY_GAME_MARKET_TYPES:
            odds = MockOddsAPI.get_odds(745000, market_type=m)
            assert odds["home_ml"] is not None and odds["total_line"] is not None
            assert odds["home_spread"] is not None
            assert odds["draw_ml"] is None

    def test_three_way_mock_prices_the_tie(self):
        odds = MockOddsAPI.get_odds(745000, market_type="f1_moneyline")
        assert odds["draw_ml"] is not None
        assert odds["home_ml"] is not None and odds["away_ml"] is not None
        assert odds["total_line"] is None

    def test_yes_no_mock_sits_at_half(self):
        odds = MockOddsAPI.get_odds(745000, market_type="first_inning_run")
        assert odds["total_line"] == 0.5
        assert odds["over_ml"] is not None and odds["under_ml"] is not None

    def test_team_total_lines_are_team_sized(self):
        for m in ("team_total_home", "team_total_away"):
            assert 3.5 <= MockOddsAPI.get_odds(745000, market_type=m)["total_line"] <= 5.0
        for m in ("f5_team_total_home", "f5_team_total_away"):
            assert 1.5 <= MockOddsAPI.get_odds(745000, market_type=m)["total_line"] <= 2.5

    def test_two_markets_of_one_game_do_not_share_numbers(self):
        f1 = MockOddsAPI.get_odds(745000, market_type="f1_total")
        f5 = MockOddsAPI.get_odds(745000, market_type="f5_total")
        assert (f1["total_line"], f1["over_ml"]) != (f5["total_line"], f5["over_ml"])

    def test_unknown_market_type_raises(self):
        with pytest.raises(ValueError, match="Unknown market_type"):
            MockOddsAPI.get_odds(745000, market_type="nope")


# ===========================================================================
# The hash and the insert
# ===========================================================================


class TestHashAndPersist:
    def test_two_way_hash_is_unchanged_by_the_new_column(self):
        # A payload without draw_ml hashes exactly as it did before the column
        # existed (every stored hash still deduplicates).
        odds = MockOddsAPI.get_odds(745000)
        without = {k: v for k, v in odds.items() if k != "draw_ml"}
        assert LiveIngestionPipeline._odds_hash(odds) == LiveIngestionPipeline._odds_hash(without)

    def test_draw_price_changes_the_hash(self):
        odds = MockOddsAPI.get_odds(745000, market_type="f1_moneyline")
        moved = dict(odds, draw_ml=odds["draw_ml"] + 10)
        assert LiveIngestionPipeline._odds_hash(odds) != LiveIngestionPipeline._odds_hash(moved)

    @pytest.mark.asyncio
    async def test_insert_writes_draw_ml_and_keeps_the_hash_last(self):
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        pipeline._db = AsyncMock()
        odds = MockOddsAPI.get_odds(745000, market_type="f5_moneyline")
        await pipeline._persist_odds(745000, odds)
        sql = pipeline._db.execute.await_args.args[0]
        args = pipeline._db.execute.await_args.args[1:]
        assert "draw_ml" in sql
        assert args[-1] == LiveIngestionPipeline._odds_hash(odds)
        assert args[-2] == odds["draw_ml"]
        assert args[5] == "f5_moneyline"  # market_type keeps its slot


# ===========================================================================
# The live cycle
# ===========================================================================


class TestLiveSegmentCycle:
    def _pipeline(self) -> LiveIngestionPipeline:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        p._db = AsyncMock()
        p._odds = MockOddsAPI()
        p._last_segment_fetch = {}
        return p

    def test_segment_market_list_is_every_non_legacy_market(self):
        assert LiveIngestionPipeline.SEGMENT_MARKET_TYPES == SEGMENT_MARKETS
        assert len(LiveIngestionPipeline.SEGMENT_MARKET_TYPES) == 12

    def test_fetch_returns_one_quote_per_segment_market(self):
        quotes = self._pipeline()._fetch_segment_odds(745000)
        assert [q["market_type"] for q in quotes] == list(SEGMENT_MARKETS)

    @pytest.mark.asyncio
    async def test_cycle_persists_twelve_rows_then_respects_the_cadence(self):
        p = self._pipeline()
        assert await p._persist_segment_odds_cycle(745000) == 12
        assert p._db.execute.await_count == 12
        # The gate is closed until the cadence elapses: nothing more is written.
        assert await p._persist_segment_odds_cycle(745000) == 0
        assert p._db.execute.await_count == 12

    @pytest.mark.asyncio
    async def test_a_failing_market_does_not_stop_the_others(self):
        p = self._pipeline()

        class _Flaky(MockOddsAPI):
            @staticmethod
            def get_odds(game_pk, *, line_type="current", market_type="moneyline", **kw):
                if market_type == "f5_total":
                    raise RuntimeError("boom")
                return MockOddsAPI.get_odds(game_pk, line_type=line_type, market_type=market_type)

        p._odds = _Flaky()
        assert await p._persist_segment_odds_cycle(745000) == 11


# ===========================================================================
# The loader
# ===========================================================================


class TestLoaderGameMarkets:
    def _loader(self):
        import importlib.util
        import sys

        path = Path(__file__).resolve().parent.parent.parent / "scripts" / "load_historical_odds.py"
        spec = importlib.util.spec_from_file_location("load_historical_odds_sim421_gm", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

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
    async def test_load_game_odds_writes_one_row_per_market_and_line_type(self):
        loader = self._loader()
        persisted = []

        async def persist(game_pk, odds):
            persisted.append(odds)

        written = await loader._load_game_odds(
            MockOddsAPI(), persist, 745000, line_types=("opening", "closing")
        )
        assert written == 30
        assert {o["market_type"] for o in persisted} == set(GAME_MARKET_TYPES)
        assert {o["line_type"] for o in persisted} == {"opening", "closing"}

    @pytest.mark.asyncio
    async def test_a_narrowed_market_set_writes_only_those(self):
        loader = self._loader()
        persisted = []

        async def persist(game_pk, odds):
            persisted.append(odds)

        written = await loader._load_game_odds(
            MockOddsAPI(),
            persist,
            745000,
            line_types=("closing",),
            game_markets=("first_inning_run", "f5_total"),
        )
        assert written == 2
        assert [o["market_type"] for o in persisted] == ["first_inning_run", "f5_total"]


class TestLoaderResume:
    """SIM-421 resume: ``--skip-loaded-since`` skips the games a dead run finished."""

    def _loader(self):
        return TestLoaderGameMarkets._loader(TestLoaderGameMarkets())

    def test_naive_timestamp_reads_as_local_time(self):
        loader = self._loader()
        dt = loader.parse_skip_loaded_since("2026-09-12T23:40:00")
        assert dt.tzinfo is not None
        assert (dt.hour, dt.minute) == (23, 40)
        assert loader.parse_skip_loaded_since(None) is None
        assert loader.parse_skip_loaded_since("  ") is None

    def test_offset_timestamp_is_kept(self):
        loader = self._loader()
        dt = loader.parse_skip_loaded_since("2026-09-13T03:40:00+00:00")
        assert dt.utcoffset().total_seconds() == 0

    def test_parse_args_accepts_the_flag(self):
        loader = self._loader()
        args = loader.parse_args(
            ["--seasons", "2025", "--skip-loaded-since", "2026-09-12T23:40:00"]
        )
        assert args.skip_loaded_since == "2026-09-12T23:40:00"

    @pytest.mark.asyncio
    async def test_resume_clause_is_added_only_with_the_flag(self, monkeypatch):
        loader = self._loader()
        seen: list[tuple[str, tuple]] = []

        class _Conn:
            async def fetch(self, sql, *params):
                seen.append((sql, params))
                return []

            async def close(self):
                return None

        async def _connect(dsn):
            return _Conn()

        import sys
        import types

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))
        await loader._fetch_final_games("dsn", [2025], None)
        assert "NOT EXISTS" not in seen[-1][0] and seen[-1][1] == ()
        since = loader.parse_skip_loaded_since("2026-09-12T23:40:00")
        await loader._fetch_final_games("dsn", [2025], None, skip_loaded_since=since)
        assert "NOT EXISTS" in seen[-1][0] and "raw.prop_odds" in seen[-1][0]
        assert seen[-1][1] == (since,)
