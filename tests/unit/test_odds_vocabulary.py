"""
test_odds_vocabulary.py
==============================
SIM-421 — the prop-bet types the market already offers but the platform did
not price: the odds vocabulary, the BettingPros provider cache, the loaders
and the CHECK-constraint migration.

What this file pins:

  1. The vocabulary has ONE source (``betting/markets.py``). The BettingPros
     market map and the historical loader agree with it.
  2. Every market hashes to its own dedup key.
  3. Alembic migration 0022 lists the 15 values and its downgrade deletes the
     rows of the eight new markets before it restores the seven-value CHECK.
  4. ``prop_markets_for`` asks a pitcher for the pitcher markets only and a
     hitter for the batter markets only; an unknown role gets every market. The
     live pipeline's roles and ``prop_offers`` follow it, over the real provider
     fed by recorded responses (``tests/bp_recorded.py``).
  5. The BettingPros provider prices a new batter market and a new pitcher
     market from captured-shape fixtures; its offers cache collapses repeat
     calls for the same (event, market) and expires after the time-to-live;
     the per-date events cache serves every game on a date from one fetch;
     both caches drop their stale entries on every store, so a season-long
     provider stays bounded; a paged batter market reaches the players on
     page 2, and a failure on page 2 keeps the page-1 players priced without
     caching the partial market.
  6. The historical loader's ``--prop-stats`` narrows the markets (an unknown
     value stops the run), ``--line-types`` narrows the line types, and the
     offline cache time-to-live is applied only when the operator left it unset.

No network and no live DB: the provider's ``_bp_get`` / ``_mlb_get`` seams are
stubbed with fixtures under ``tests/fixtures/bettingpros/``; the pipeline is
built with ``__new__`` (the tests/conftest.py idiom).
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.bp_recorded import (  # noqa: E402
    BRYCE_MILLER,
    GAME_92857,
    JULIO_RODRIGUEZ,
    Recorded92857,
    recorded_prop_rows,
)

import betting.history as loader  # noqa: E402
from betting import markets as odds_provider  # noqa: E402
from betting.bettingpros.fetch_odds import (
    _PROP_MARKET_IDS,  # noqa: E402
    BettingProsOddsProvider,  # noqa: E402
)
from betting.load import _prop_odds_hash, prop_offers  # noqa: E402
from betting.markets import (  # noqa: E402
    BATTER_PROP_STATS,
    PITCHER_PROP_STATS,
    PROP_ROLE_BATTER,
    PROP_ROLE_BOTH,
    PROP_ROLE_PITCHER,
    PROP_STAT_TO_MODEL_PROP,
    PROP_STATS,
    prop_markets_for,
)
from pipeline.live.ingestion.constants import PROP_FETCH_CADENCE_S  # noqa: E402
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline  # noqa: E402

_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "bettingpros"
_MIG = _ROOT / "db" / "migrations" / "versions" / "0022_sim421_prop_odds_market_vocabulary.py"

#: The contract's vocabulary, spelled out so a drift in the source is caught.
_ORIGINAL_SEVEN = ("strikeouts", "hits", "home_runs", "earned_runs", "walks", "total_bases", "rbis")
_NEW_EIGHT = (
    "singles",
    "doubles",
    "triples",
    "runs",
    "stolen_bases",
    "hits_runs_rbis",
    "outs_recorded",
    "hits_allowed",
)
_CONTRACT_MARKET_IDS = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
    "singles": 295,
    "doubles": 291,
    "triples": 292,
    "runs": 288,
    "stolen_bases": 294,
    "hits_runs_rbis": 403,
    "outs_recorded": 405,
    "hits_allowed": 404,
}
_CONTRACT_MODEL_MAP = {
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "outs_recorded": "OUTS",
    "hits_allowed": "H_ALLOWED",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
    "singles": "1B",
    "doubles": "2B",
    "triples": "3B",
    "runs": "R",
    "stolen_bases": "SB",
    "hits_runs_rbis": "HRR",
}


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


# ===========================================================================
# 1. One vocabulary source
# ===========================================================================


class TestVocabularySingleSource:
    def test_prop_stats_is_the_seven_originals_then_the_eight_new(self) -> None:
        assert PROP_STATS == _ORIGINAL_SEVEN + _NEW_EIGHT
        assert len(PROP_STATS) == 15

    def test_pitcher_and_batter_tuples_partition_the_vocabulary(self) -> None:
        assert set(PITCHER_PROP_STATS) | set(BATTER_PROP_STATS) == set(PROP_STATS)
        assert not set(PITCHER_PROP_STATS) & set(BATTER_PROP_STATS)
        assert PITCHER_PROP_STATS == (
            "strikeouts",
            "earned_runs",
            "walks",
            "outs_recorded",
            "hits_allowed",
        )
        assert BATTER_PROP_STATS == (
            "hits",
            "home_runs",
            "total_bases",
            "rbis",
            "singles",
            "doubles",
            "triples",
            "runs",
            "stolen_bases",
            "hits_runs_rbis",
        )

    def test_hits_is_a_batter_market_and_hits_allowed_a_pitcher_market(self) -> None:
        assert "hits" in BATTER_PROP_STATS
        assert "hits_allowed" in PITCHER_PROP_STATS
        assert PROP_STAT_TO_MODEL_PROP["hits"] != PROP_STAT_TO_MODEL_PROP["hits_allowed"]

    def test_model_prop_map_matches_the_contract(self) -> None:
        assert PROP_STAT_TO_MODEL_PROP == _CONTRACT_MODEL_MAP
        assert set(PROP_STAT_TO_MODEL_PROP) == set(PROP_STATS)

    def test_bettingpros_market_ids_equal_the_vocabulary_and_the_contract(self) -> None:
        assert set(_PROP_MARKET_IDS) == set(PROP_STATS)
        assert _PROP_MARKET_IDS == _CONTRACT_MARKET_IDS
        # Every market id is distinct — two stats never share a BettingPros market.
        assert len(set(_PROP_MARKET_IDS.values())) == len(_PROP_MARKET_IDS)

    def test_source_exports_the_vocabulary(self) -> None:
        for name in (
            "PROP_STATS",
            "PITCHER_PROP_STATS",
            "BATTER_PROP_STATS",
            "PROP_STAT_TO_MODEL_PROP",
            "PROP_ROLE_PITCHER",
            "PROP_ROLE_BATTER",
            "PROP_ROLE_BOTH",
            "prop_markets_for",
        ):
            assert name in odds_provider.__all__

    def test_historical_loader_imports_the_source_tuple(self) -> None:
        assert loader.PROP_STATS is odds_provider.PROP_STATS


# ===========================================================================
# 2. Every market has its own dedup key
# ===========================================================================


class TestEveryMarketHashesApart:
    def test_every_market_hashes_differently_for_the_dedup(self) -> None:
        """One recorded strikeout row, relabelled per market, gives 15 hashes."""
        row = recorded_prop_rows(BRYCE_MILLER, "strikeouts")[0]
        hashes = {_prop_odds_hash({**row, "prop_stat": s}) for s in PROP_STATS}
        assert len(hashes) == len(PROP_STATS)


# ===========================================================================
# 3. Migration 0022
# ===========================================================================


class TestMigration0022:
    def test_file_chains_off_0021(self) -> None:
        text = _MIG.read_text(encoding="utf-8")
        assert re.search(r'^revision = "0022"$', text, re.M)
        assert re.search(r'^down_revision = "0021"$', text, re.M)

    def test_upgrade_lists_all_fifteen_values(self) -> None:
        text = _MIG.read_text(encoding="utf-8")
        for stat in PROP_STATS:
            assert f'"{stat}"' in text, stat
        assert "ck_prop_odds_prop_stat" in text
        assert "DO $$" in text  # the idempotent migration-0004 style

    def test_downgrade_deletes_the_new_rows_and_says_so(self) -> None:
        text = _MIG.read_text(encoding="utf-8")
        assert "DELETE FROM raw.prop_odds" in text
        assert "_NEW_PROP_STATS" in text
        # The docstring states the deletion plainly.
        assert "DOWNGRADE DELETES ROWS" in text

    def test_module_imports_and_the_sql_carries_every_value(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("mig_0022", _MIG)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._ORIGINAL_PROP_STATS + mod._NEW_PROP_STATS == PROP_STATS
        sql = mod._replace_check(mod._ORIGINAL_PROP_STATS + mod._NEW_PROP_STATS)
        for stat in PROP_STATS:
            assert f"'{stat}'" in sql
        old = mod._replace_check(mod._ORIGINAL_PROP_STATS)
        assert "'singles'" not in old


# ===========================================================================
# 4. The live pipeline's pitcher / batter split
# ===========================================================================


def _make_pipeline() -> LiveIngestionPipeline:
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    p._last_seen_column = False
    return p


class TestLivePipelineRoleSplit:
    def test_roles_from_positions(self) -> None:
        state = {
            "current_pitcher_id": 999,
            "home_lineup": [
                {"player_id": 1, "position": "CF"},
                {"player_id": 2, "position": "DH"},
                {"player_id": 3},  # no position → unknown
                {"player_id": 4, "position": "P"},  # a two-way player batting
            ],
            "away_lineup": [{"player_id": 5, "position": "c"}],  # lower case tolerated
        }
        roles = LiveIngestionPipeline._collect_prop_player_roles(state)
        assert list(roles) == [999, 1, 2, 3, 4, 5]  # order-stable, pitcher first
        assert roles[999] == PROP_ROLE_PITCHER
        assert roles[1] == PROP_ROLE_BATTER
        assert roles[2] == PROP_ROLE_BATTER
        assert roles[3] == PROP_ROLE_BOTH
        assert roles[4] == PROP_ROLE_BOTH
        assert roles[5] == PROP_ROLE_BATTER

    def test_current_pitcher_who_also_bats_gets_both(self) -> None:
        state = {
            "current_pitcher_id": 660271,
            "home_lineup": [{"player_id": 660271, "position": "DH"}],
            "away_lineup": [],
        }
        roles = LiveIngestionPipeline._collect_prop_player_roles(state)
        assert roles == {660271: PROP_ROLE_BOTH}

    def test_prop_markets_for_each_role(self) -> None:
        assert prop_markets_for(PROP_ROLE_PITCHER) == PITCHER_PROP_STATS
        assert prop_markets_for(PROP_ROLE_BATTER) == BATTER_PROP_STATS
        assert prop_markets_for(PROP_ROLE_BOTH) == PROP_STATS
        assert prop_markets_for("something-else") == PROP_STATS

    def test_prop_markets_for_keeps_a_subset_in_the_callers_order(self) -> None:
        narrowed = ("hits_allowed", "singles", "strikeouts")
        assert prop_markets_for(PROP_ROLE_PITCHER, narrowed) == ("hits_allowed", "strikeouts")
        assert prop_markets_for(PROP_ROLE_BATTER, narrowed) == ("singles",)
        assert prop_markets_for(PROP_ROLE_BOTH, narrowed) == narrowed

    def test_hits_and_hits_allowed_never_cross(self) -> None:
        both = ("hits", "hits_allowed")
        assert prop_markets_for(PROP_ROLE_PITCHER, both) == ("hits_allowed",)
        assert prop_markets_for(PROP_ROLE_BATTER, both) == ("hits",)

    def test_prop_offers_asks_each_role_for_its_markets(self) -> None:
        provider = _SpyRecorded()
        roles = {BRYCE_MILLER: PROP_ROLE_PITCHER, JULIO_RODRIGUEZ: PROP_ROLE_BATTER, 7: "both"}
        offers = prop_offers(provider, GAME_92857, roles, line_types=("current", "closing"))
        asked: dict[int, set[str]] = {}
        for player_id, prop_stat, _ in provider.prop_calls:
            asked.setdefault(player_id, set()).add(prop_stat)
        assert asked[BRYCE_MILLER] == set(PITCHER_PROP_STATS)
        assert asked[JULIO_RODRIGUEZ] == set(BATTER_PROP_STATS)
        assert asked[7] == set(PROP_STATS)
        assert {lt for _, _, lt in provider.prop_calls} == {"current", "closing"}
        # The recording prices Miller's strikeouts and hits allowed and Rodriguez's
        # singles; an offer with no priced row is left out.
        stats = {(rows[0]["player_id"], rows[0]["prop_stat"]) for _, rows in offers}
        assert stats == {
            (BRYCE_MILLER, "strikeouts"),
            (BRYCE_MILLER, "hits_allowed"),
            (JULIO_RODRIGUEZ, "singles"),
        }
        assert all(r["source"] == "bettingpros" for _, rows in offers for r in rows)

    @pytest.mark.asyncio
    async def test_cycle_never_asks_a_hitter_for_a_pitcher_market(self) -> None:
        pipeline = _make_pipeline()
        provider = _SpyRecorded()
        pipeline._odds = provider
        state = {
            "game_pk": GAME_92857,
            "current_pitcher_id": BRYCE_MILLER,
            "home_lineup": [{"player_id": JULIO_RODRIGUEZ, "position": "CF"}],
            "away_lineup": [{"player_id": 2, "position": "SS"}],
        }
        written = await pipeline._persist_prop_odds_cycle(GAME_92857, state)
        for player_id, prop_stat, _ in provider.prop_calls:
            if player_id == BRYCE_MILLER:
                assert prop_stat in PITCHER_PROP_STATS
            else:
                assert prop_stat in BATTER_PROP_STATS
        sent = [row for call in pipeline._db.executemany.await_args_list for row in call.args[1]]
        assert written == len(sent) > 0


class _SpyRecorded(Recorded92857):
    """The recorded provider, with every prop request listed."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.prop_calls: list[tuple[int, str, str]] = []

    def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, *, line_type="current"):
        self.prop_calls.append((int(player_id), prop_stat, line_type))
        return super().get_prop_odds_by_book(game_pk, player_id, prop_stat, line_type=line_type)


# ===========================================================================
# 5. The BettingPros provider: new markets, the caches, pagination
# ===========================================================================

_OFFERS_BY_MARKET = {
    122: "offers_ml_92857.json",
    285: "offers_k_92857.json",
    295: "offers_singles_92857.json",
    404: "offers_hits_allowed_92857.json",
}
_PAGED_HITS = {1: "offers_hits_92857_page1.json", 2: "offers_hits_92857_page2.json"}


class _FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _CountingProvider(BettingProsOddsProvider):
    """Provider on the fixtures that counts every BettingPros HTTP call.

    ``fail_offers_once`` fails the next offers call. ``fail_offers_page`` fails
    EVERY offers call for that page number (a persistent later-page failure).
    ``pagination_override`` replaces the ``_pagination`` block of every offers
    response (a malformed block from the API).
    """

    def __init__(
        self,
        *,
        names: dict[int, str] | None = None,
        fail_offers_once: bool = False,
        fail_offers_page: int | None = None,
        pagination_override: object = None,
        **kw,
    ):
        super().__init__(api_key="test-key", **kw)
        self._names = names or {45184: "Bryce Miller", 61001: "Julio Rodriguez"}
        self._fail_offers_once = fail_offers_once
        self._fail_offers_page = fail_offers_page
        self._pagination_override = pagination_override
        self.bp_calls: list[tuple[str, dict]] = []

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return _load("mlb_schedule_746437.json")
        if path.startswith("people/"):
            pid = int(path.split("/")[1])
            return {"people": [{"fullName": self._names.get(pid, "Nobody Here")}]}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        self.bp_calls.append((path, dict(params)))
        if path == "events":
            return _load("events_2024-08-15.json")
        if path == "offers":
            if self._fail_offers_once:
                self._fail_offers_once = False
                raise OSError("simulated transport failure")
            page = int(params.get("page", 1))
            if self._fail_offers_page == page:
                raise TimeoutError(f"simulated timeout on page {page}")
            market_id = params["market_id"]
            if market_id == 287:
                data = _load(_PAGED_HITS[page])
            else:
                data = _load(_OFFERS_BY_MARKET[market_id])
            if self._pagination_override is not None:
                data["_pagination"] = self._pagination_override
            return data
        raise AssertionError(f"unexpected BP path {path}")

    def offers_calls(self, market_id: int | None = None) -> list[dict]:
        return [
            p
            for path, p in self.bp_calls
            if path == "offers" and (market_id is None or p["market_id"] == market_id)
        ]


def _row(rows: list[dict], book: str) -> dict:
    """The one row of ``book`` among a by-book read's rows."""
    (row,) = [r for r in rows if r["book"] == book]
    return row


class TestBettingProsNewMarkets:
    def test_singles_is_priced_as_a_batter_market(self) -> None:
        prov = _CountingProvider(offers_cache_ttl_s=0)
        quote = _row(prov.get_prop_odds_by_book(746437, 61001, "singles"), "bp:10")
        assert quote["prop_stat"] == "singles"
        assert quote["line"] == 0.5
        # SIM-555: FanDuel's (book 10) own over and under, no longer the per-side
        # "best" line (book 15).
        assert quote["over_ml"] == -150
        assert quote["under_ml"] == 115
        assert quote["is_mock"] is False
        assert prov.offers_calls()[0]["market_id"] == 295

    def test_singles_opening_and_closing_lines(self) -> None:
        prov = _CountingProvider(offers_cache_ttl_s=0)
        (opening,) = prov.get_prop_odds_by_book(746437, 61001, "singles", line_type="opening")
        assert (opening["over_ml"], opening["under_ml"]) == (-145, 110)
        # SIM-555: one closing row per book; BetRivers (18) is one of them.
        rows = {
            r["book"]: r
            for r in prov.get_prop_odds_by_book(746437, 61001, "singles", line_type="closing")
        }
        assert set(rows) == {"bp:10", "bp:15", "bp:18"}
        assert (rows["bp:18"]["over_ml"], rows["bp:18"]["under_ml"]) == (-155, 118)
        assert (rows["bp:10"]["over_ml"], rows["bp:10"]["under_ml"]) == (-150, 115)

    def test_hits_allowed_is_priced_as_a_pitcher_market(self) -> None:
        prov = _CountingProvider(offers_cache_ttl_s=0)
        quote = _row(prov.get_prop_odds_by_book(746437, 45184, "hits_allowed"), "bp:10")
        assert quote["line"] == 4.5
        # SIM-555: FanDuel's own over and under (was the per-side best: -102 / -112).
        assert quote["over_ml"] == -110
        assert quote["under_ml"] == -112
        assert prov.offers_calls()[0]["market_id"] == 404

    def test_hits_and_hits_allowed_hit_different_markets(self) -> None:
        prov = _CountingProvider(offers_cache_ttl_s=0)
        prov.get_prop_odds_by_book(746437, 61001, "hits")
        prov.get_prop_odds_by_book(746437, 45184, "hits_allowed")
        # The hits market (287) spans two pages in the fixture; hits_allowed is 404.
        assert [c["market_id"] for c in prov.offers_calls()] == [287, 287, 404]

    @pytest.mark.parametrize("prop_stat", _NEW_EIGHT)
    def test_every_new_market_is_accepted_by_the_provider(self, prop_stat: str) -> None:
        prov = _CountingProvider()
        # An unknown player yields no rows, never a ValueError.
        assert prov.get_prop_odds_by_book(746437, 111111, prop_stat) == []


class TestBettingProsOffersCache:
    def test_second_player_on_the_same_event_and_market_makes_no_http_call(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        first = prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        n_after_first = len(prov.offers_calls(285))
        second = prov.get_prop_odds_by_book(746437, 111111, "strikeouts")  # another player
        assert n_after_first == 1
        assert len(prov.offers_calls(285)) == 1
        assert _row(first, "bp:10")["line"] == 5.5
        assert second == []  # no offer for that name — still no re-fetch

    def test_line_types_share_one_fetch(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        for line_type in ("opening", "current", "closing"):
            prov.get_prop_odds_by_book(746437, 45184, "strikeouts", line_type=line_type)
        assert len(prov.offers_calls(285)) == 1

    def test_a_different_market_is_its_own_entry(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        prov.get_prop_odds_by_book(746437, 45184, "hits_allowed")
        assert len(prov.offers_calls(285)) == 1
        assert len(prov.offers_calls(404)) == 1

    def test_expired_entry_is_fetched_again(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        clock.now += 29.0
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert len(prov.offers_calls(285)) == 1  # still fresh
        clock.now += 1.5
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert len(prov.offers_calls(285)) == 2  # past the time-to-live

    def test_ttl_zero_disables_the_cache(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), offers_cache_ttl_s=0)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert len(prov.offers_calls(285)) == 2

    def test_game_odds_share_the_cache_too(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        prov.get_odds(746437, market_type="moneyline")
        prov.get_odds(746437, market_type="moneyline", line_type="closing")
        assert len(prov.offers_calls(122)) == 1

    def test_a_failed_fetch_is_not_cached(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), fail_offers_once=True)
        failed = prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert failed == []
        ok = prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert _row(ok, "bp:10")["line"] == 5.5
        assert len(prov.offers_calls(285)) == 2

    def test_default_ttl_is_below_the_live_prop_cadence(self, monkeypatch) -> None:
        monkeypatch.delenv(BettingProsOddsProvider.OFFERS_CACHE_TTL_ENV, raising=False)
        prov = BettingProsOddsProvider(api_key="k")
        assert prov._offers_cache_ttl_s == 30.0
        assert prov._offers_cache_ttl_s < PROP_FETCH_CADENCE_S

    def test_env_sets_the_ttl_and_the_argument_wins(self, monkeypatch) -> None:
        monkeypatch.setenv(BettingProsOddsProvider.OFFERS_CACHE_TTL_ENV, "12.5")
        assert BettingProsOddsProvider(api_key="k")._offers_cache_ttl_s == 12.5
        assert BettingProsOddsProvider(api_key="k", offers_cache_ttl_s=3)._offers_cache_ttl_s == 3.0

    def test_events_for_a_date_are_fetched_once_for_every_game_on_it(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        assert prov._resolve_event(746437) is not None
        # A second game_pk on the same date: the slate is served from the cache.
        # (The schedule stub returns the same date for every game_pk.)
        prov._resolve_event(746438)
        assert len([c for c in prov.bp_calls if c[0] == "events"]) == 1

    def test_events_cache_expires_with_the_ttl(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov._events_for_date("2024-08-15")
        clock.now += 31
        prov._events_for_date("2024-08-15")
        assert len([c for c in prov.bp_calls if c[0] == "events"]) == 2


class TestBettingProsCacheEviction:
    """The caches drop stale entries on every store, so a long-lived provider stays bounded.

    A finished game's (event, market) key is never read again. Without the
    sweep the live pipeline (one provider per process) and the historical
    loader (one provider per whole-season backfill) would keep every payload
    for the process lifetime.
    """

    def test_a_store_drops_every_offers_entry_past_the_ttl(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")  # (92857, 285)
        prov.get_prop_odds_by_book(746437, 45184, "hits_allowed")  # (92857, 404)
        prov.get_prop_odds_by_book(746437, 61001, "singles")  # (92857, 295)
        prov.get_odds(746437, market_type="moneyline")  # (92857, 122)
        assert len(prov._offers_cache) == 4
        clock.now += 31  # every entry is now stale
        prov.get_prop_odds_by_book(746437, 61001, "hits")  # a new key: (92857, 287)
        assert set(prov._offers_cache) == {(92857, 287)}

    def test_fresh_entries_survive_the_sweep(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        clock.now += 20
        prov.get_prop_odds_by_book(746437, 45184, "hits_allowed")
        clock.now += 15  # strikeouts is 35 s old (stale); hits_allowed 15 s (fresh)
        prov.get_prop_odds_by_book(746437, 61001, "singles")
        assert set(prov._offers_cache) == {(92857, 404), (92857, 295)}

    def test_a_stale_hit_is_replaced_not_duplicated(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        clock.now += 31
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")  # stale → re-fetched and re-stored
        assert set(prov._offers_cache) == {(92857, 285)}
        assert len(prov.offers_calls(285)) == 2

    def test_a_store_drops_every_events_entry_past_the_ttl(self) -> None:
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=30)
        prov._events_for_date("2024-08-15")
        prov._events_for_date("2024-08-16")
        assert len(prov._events_by_date_cache) == 2
        clock.now += 31
        prov._events_for_date("2024-08-17")
        assert set(prov._events_by_date_cache) == {"2024-08-17"}

    def test_ttl_zero_stores_nothing(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), offers_cache_ttl_s=0)
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        prov.get_prop_odds_by_book(746437, 61001, "singles")
        prov.get_odds(746437, market_type="moneyline")
        assert prov._offers_cache == {}
        assert prov._events_by_date_cache == {}

    def test_a_season_of_games_stays_bounded(self) -> None:
        """One provider walks many games, one after another, as the loader does."""
        clock = _FakeClock()
        prov = _CountingProvider(clock=clock, offers_cache_ttl_s=600)
        for game in range(50):
            # Each game gets its own event id and its five fixture markets.
            for market_id in (122, 285, 295, 404, 287):
                prov._offers(90000 + game, market_id)
            clock.now += 60  # a game a minute → ten games fit in one time-to-live
        assert len(prov._offers_cache) <= 5 * 10
        assert (90000 + 49, 287) in prov._offers_cache
        assert (90000, 122) not in prov._offers_cache


class TestBettingProsPagination:
    def test_player_on_page_two_is_found(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names={62011: "Kerry Carpenter"})
        quote = _row(prov.get_prop_odds_by_book(746437, 62011, "hits"), "bp:10")
        assert quote["line"] == 0.5
        assert quote["over_ml"] == -175
        assert quote["under_ml"] == 135
        pages = [c.get("page", 1) for c in prov.offers_calls(287)]
        assert pages == [1, 2]

    def test_all_pages_land_in_one_cache_entry(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names={62011: "Kerry Carpenter"})
        prov.get_prop_odds_by_book(746437, 62011, "hits")
        prov.get_prop_odds_by_book(746437, 62001, "hits")  # page-1 player, served from the cache
        assert len(prov.offers_calls(287)) == 2  # page 1 + page 2, once
        assert len(prov._offers_cache[(92857, 287)][1]) == 5

    def test_single_page_market_makes_one_call(self) -> None:
        prov = _CountingProvider(clock=_FakeClock())
        prov.get_prop_odds_by_book(746437, 45184, "strikeouts")
        assert [c.get("page", 1) for c in prov.offers_calls(285)] == [1]


class TestBettingProsLaterPageFailure:
    """A failure on page 2+ keeps the page-1 players priced and caches nothing.

    Before SIM-421 the provider read page 1 only, so a page-1 player was
    always priced. The pager must not turn a later-page failure into a loss
    of the whole market.
    """

    _NAMES = {62001: "Batter Pageone1", 62011: "Kerry Carpenter"}

    def test_page_one_players_keep_their_quotes(self, caplog) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names=self._NAMES, fail_offers_page=2)
        with caplog.at_level("WARNING", logger="betting.bettingpros"):
            page_one = _row(prov.get_prop_odds_by_book(746437, 62001, "hits"), "bp:10")
        assert page_one["line"] == 0.5
        assert page_one["over_ml"] == -200
        # The warning names the page, the event and the market.
        messages = [r.getMessage() for r in caplog.records]
        assert any("page 2 of 2" in m and "92857" in m and "287" in m for m in messages)

    def test_page_two_players_get_no_rows(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names=self._NAMES, fail_offers_page=2)
        assert prov.get_prop_odds_by_book(746437, 62011, "hits") == []

    def test_the_partial_market_is_not_cached(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names=self._NAMES, fail_offers_page=2)
        prov.get_prop_odds_by_book(746437, 62001, "hits")
        assert (92857, 287) not in prov._offers_cache
        # The next call fetches again: page 1 succeeds, page 2 fails again.
        prov.get_prop_odds_by_book(746437, 62001, "hits")
        assert [c.get("page", 1) for c in prov.offers_calls(287)] == [1, 2, 1, 2]

    def test_page_two_recovering_prices_everyone_and_caches(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names=self._NAMES, fail_offers_page=2)
        prov.get_prop_odds_by_book(746437, 62001, "hits")
        prov._fail_offers_page = None  # the transport recovers
        quote = _row(prov.get_prop_odds_by_book(746437, 62011, "hits"), "bp:10")
        assert quote["line"] == 0.5
        assert len(prov._offers_cache[(92857, 287)][1]) == 5

    def test_a_page_one_failure_still_raises_to_the_caller(self) -> None:
        prov = _CountingProvider(clock=_FakeClock(), names=self._NAMES, fail_offers_page=1)
        with pytest.raises(TimeoutError):
            prov._offers(92857, 287)
        assert prov._offers_cache == {}

    def test_a_malformed_pagination_block_counts_as_one_page(self) -> None:
        prov = _CountingProvider(
            clock=_FakeClock(), names=self._NAMES, pagination_override=["not", "a", "dict"]
        )
        quote = _row(prov.get_prop_odds_by_book(746437, 62001, "hits"), "bp:10")
        assert quote["line"] == 0.5
        assert [c.get("page", 1) for c in prov.offers_calls(287)] == [1]
        assert (92857, 287) in prov._offers_cache  # one page is a complete market

    def test_a_pagination_block_with_a_bad_total_counts_as_one_page(self) -> None:
        prov = _CountingProvider(
            clock=_FakeClock(), names=self._NAMES, pagination_override={"total_pages": "many"}
        )
        prov.get_prop_odds_by_book(746437, 62001, "hits")
        assert [c.get("page", 1) for c in prov.offers_calls(287)] == [1]


# ===========================================================================
# 6. The historical loader
# ===========================================================================


class TestLoaderSelection:
    def test_no_prop_stats_means_every_market(self) -> None:
        assert loader._select_prop_stats(None) is PROP_STATS
        assert loader._select_prop_stats([]) is PROP_STATS

    def test_subset_keeps_the_canonical_order(self) -> None:
        assert loader._select_prop_stats(["hits_allowed", "singles", "strikeouts"]) == (
            "strikeouts",
            "singles",
            "hits_allowed",
        )

    def test_unknown_prop_stat_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown prop_stat") as exc:
            loader._select_prop_stats(["singles", "innings_pitched"])
        assert "innings_pitched" in str(exc.value)
        assert "hits_runs_rbis" in str(exc.value)  # the vocabulary is listed

    def test_parse_args_rejects_an_unknown_prop_stat(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            loader.parse_args(["--seasons", "2024", "--prop-stats", "nope"])
        assert exc.value.code == 2
        assert "Unknown prop_stat" in capsys.readouterr().err

    def test_parse_args_accepts_the_new_markets_pass(self) -> None:
        args = loader.parse_args(
            ["--seasons", "2024", "--no-game-odds", "--prop-stats", *_NEW_EIGHT]
        )
        assert args.no_game_odds is True
        assert loader._select_prop_stats(args.prop_stats) == _NEW_EIGHT
        assert args.line_types is None
        assert loader._select_line_types(args.line_types) == ("opening", "closing")

    def test_line_types_default_subset_and_unknown(self) -> None:
        assert loader._select_line_types(None) == ("opening", "closing")
        assert loader._select_line_types(["closing"]) == ("closing",)
        assert loader._select_line_types(["closing", "opening"]) == ("opening", "closing")
        with pytest.raises(ValueError, match="Unknown line_type"):
            loader._select_line_types(["bet_placement"])
        with pytest.raises(SystemExit):
            loader.parse_args(["--seasons", "2024", "--line-types", "nope"])

    def test_offline_cache_ttl_applies_only_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("ODDS_OFFERS_CACHE_TTL_S", raising=False)
        assert loader._configure_offline_cache() == 600.0
        assert loader.OFFLINE_OFFERS_CACHE_TTL_S == 600
        # The provider built afterwards reads the value.
        assert BettingProsOddsProvider(api_key="k")._offers_cache_ttl_s == 600.0
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "45")
        assert loader._configure_offline_cache() == 45.0
