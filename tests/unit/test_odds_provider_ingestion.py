"""
test_odds_provider_ingestion.py
============================
SIM-340 — Real odds provider + prop ingestion (multi-book, sharp flag, cadence)

Permanent regression suite for the live odds cycle. SIM-645 moved the load into
``betting.load_odds_to_db``: the cycle runs ``game_offers`` / ``prop_offers`` on the vendor
thread, then ``write_offers`` into the pool. These tests check:

  1. The prop cycle writes its rows into raw.prop_odds, one batch per offer.
  2. ``mark_closing_prop_lines`` stamps the closing prop line.
  3. Every book the provider names is stored under its own label, with the
     provider's ``is_sharp_book`` flag unchanged.
  4. The load guard's refusals are counted and never written; a failed fetch or
     a failed write is logged and the cycle goes on.
  5. The fetch cadence throttles the fetches to PROP_FETCH_CADENCE_S per game.
  6. The dedup hash tells books and line types apart.

No made-up price: the provider is the real BettingPros provider over the
recorded responses (``tests/bp_recorded.py``). The pool is an AsyncMock.

Owned by Data Engineer (Agent 4) + Betting Analyst (Agent 8).

Run:
    pytest tests/unit/test_odds_provider_ingestion.py -v
"""

from __future__ import annotations

import logging
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.bp_recorded import (  # noqa: E402
    BRYCE_MILLER,
    GAME_92857,
    GAME_99026,
    JULIO_RODRIGUEZ,
    KERRY_CARPENTER,
    RILEY_GREENE,
    Recorded92857,
    Recorded99026,
    recorded_prop_rows,
)

from betting.bettingpros.fetch_odds import BettingProsOddsProvider  # noqa: E402
from betting.load_odds_to_db import _prop_odds_hash, game_offers, prop_offers  # noqa: E402
from betting.markets import BATTER_PROP_STATS, PITCHER_PROP_STATS  # noqa: E402
from betting.validate_odds import check_row  # noqa: E402
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline  # noqa: E402

# ===========================================================================
# Helpers
# ===========================================================================


def _make_pipeline(provider: Any = None) -> LiveIngestionPipeline:
    """Construct a pipeline without running __init__ (no DSN/Redis needed).

    The odds source is the real provider over the recorded game 746437.
    """
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._db.fetchval.return_value = False  # no last_seen_at column: the plain insert
    p._last_prop_fetch = {}
    p._odds = provider if provider is not None else Recorded92857()
    return p


def _make_game_state() -> dict:
    """A game_state with the recorded game's players.

    Bryce Miller pitches; three hitters bat at position-player codes, so the
    cycle splits the markets by role (SIM-421).
    """
    return {
        "game_pk": GAME_92857,
        "current_pitcher_id": BRYCE_MILLER,
        "home_lineup": [
            {"player_id": RILEY_GREENE, "position": "LF"},
            {"player_id": KERRY_CARPENTER, "position": "RF"},
        ],
        "away_lineup": [{"player_id": JULIO_RODRIGUEZ, "position": "CF"}],
    }


def _expected_offers() -> list:
    """The prop offers the recorded game gives for :func:`_make_game_state`."""
    roles = LiveIngestionPipeline._collect_prop_player_roles(_make_game_state())
    return prop_offers(Recorded92857(), GAME_92857, roles)


def _kept(offers) -> int:
    return sum(1 for _label, rows in offers for row in rows if check_row(row) is None)


def _sent(db: AsyncMock) -> list[tuple]:
    """Every bind tuple the pool's ``executemany`` received, in call order."""
    return [row for call in db.executemany.await_args_list for row in call.args[1]]


class _RaisingPropProvider:
    """A control-flow stub: one prop market raises, the rest are the recording's rows."""

    def __init__(self, failing_stat: str) -> None:
        self._real = Recorded92857()
        self._failing_stat = failing_stat
        self.calls: list[str] = []

    def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, *, line_type="current"):
        self.calls.append(prop_stat)
        if prop_stat == self._failing_stat:
            raise TimeoutError("simulated vendor time-out")
        return self._real.get_prop_odds_by_book(game_pk, player_id, prop_stat, line_type=line_type)


# ===========================================================================
# AC#1 — the prop cycle writes its rows
# ===========================================================================


class TestPropCycleWrites:
    @pytest.mark.asyncio
    async def test_cycle_writes_every_kept_row_one_batch_per_offer(self) -> None:
        """The core SIM-340 regression: the live cycle MUST persist its prop rows."""
        pipeline = _make_pipeline()
        written = await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        offers = _expected_offers()
        assert written == _kept(offers) > 0
        assert pipeline._db.executemany.await_count == len(offers)
        assert len(_sent(pipeline._db)) == written

    @pytest.mark.asyncio
    async def test_cycle_writes_to_prop_odds_table(self) -> None:
        pipeline = _make_pipeline()
        await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        sql = pipeline._db.executemany.await_args_list[0].args[0]
        assert "raw.prop_odds" in sql
        assert "odds_hash" in sql, "SIM-340 dedup column not written"
        assert "ON CONFLICT" in sql, "SIM-340 dedup ON CONFLICT not used"
        assert "book_line_at" in sql, "SIM-555 stamp column not written"
        assert "last_seen_at" not in sql  # no Alembic 0029 column: the plain insert

    @pytest.mark.asyncio
    async def test_restamp_when_the_last_seen_column_exists(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.fetchval.return_value = True
        await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        assert "last_seen_at" in pipeline._db.executemany.await_args_list[0].args[0]

    @pytest.mark.asyncio
    async def test_the_markets_follow_the_role(self) -> None:
        pipeline = _make_pipeline()
        await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        for bind in _sent(pipeline._db):
            player_id, prop_stat = bind[1], bind[4]
            if player_id == BRYCE_MILLER:
                assert prop_stat in PITCHER_PROP_STATS
            else:
                assert prop_stat in BATTER_PROP_STATS

    @pytest.mark.asyncio
    async def test_cycle_noop_when_no_players(self) -> None:
        """No eligible players (lineups not posted) → no prop writes, cadence
        clock NOT stamped so the next signal retries promptly."""
        pipeline = _make_pipeline()
        empty_state = {"current_pitcher_id": None, "home_lineup": [], "away_lineup": []}
        written = await pipeline._persist_prop_odds_cycle(GAME_92857, empty_state)
        assert written == 0
        pipeline._db.executemany.assert_not_awaited()
        assert GAME_92857 not in pipeline._last_prop_fetch


# ===========================================================================
# AC#3 — every book the provider names, and its sharp flag
# ===========================================================================


class TestMultiBookSharpFlag:
    @pytest.mark.asyncio
    async def test_every_book_is_stored_under_its_own_label(self) -> None:
        pipeline = _make_pipeline()
        roles = {BRYCE_MILLER: "pitcher"}
        await pipeline._persist_prop_roles_cycle(GAME_92857, roles)
        books = {bind[8] for bind in _sent(pipeline._db) if bind[4] == "strikeouts"}
        recorded = {r["book"] for r in recorded_prop_rows() if check_row(r) is None}
        assert books == recorded
        assert len(books) > 1

    @pytest.mark.asyncio
    async def test_each_batch_is_one_player_and_one_market(self) -> None:
        pipeline = _make_pipeline()
        await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        for call in pipeline._db.executemany.await_args_list:
            assert len({(bind[1], bind[4]) for bind in call.args[1]}) == 1

    @pytest.mark.asyncio
    async def test_the_sharp_flag_is_the_providers(self) -> None:
        """is_sharp_book (the eleventh bind) is the provider's flag, unchanged."""
        pipeline = _make_pipeline()
        await pipeline._persist_prop_roles_cycle(GAME_92857, {BRYCE_MILLER: "pitcher"})
        flags = {r["book"]: r["is_sharp_book"] for r in recorded_prop_rows()}
        for bind in _sent(pipeline._db):
            if bind[4] == "strikeouts":
                assert bind[10] is flags[bind[8]]


# ===========================================================================
# AC#4 — refusals, failed fetches and failed writes
# ===========================================================================


class TestGuardAndFailures:
    @pytest.mark.asyncio
    async def test_the_guard_counts_a_refusal_and_never_writes_it(self) -> None:
        """The segment recording holds two rows the load guard refuses."""
        pipeline = _make_pipeline(Recorded99026())
        written = await pipeline._persist_game_odds_cycle(GAME_99026)
        offers = game_offers(Recorded99026(), GAME_99026)
        refused = sum(1 for _l, rows in offers for row in rows if check_row(row) is not None)
        assert refused > 0
        tally = pipeline._refusal_tally()
        assert tally.refused == refused
        assert written == _kept(offers) == len(_sent(pipeline._db))

    @pytest.mark.asyncio
    async def test_a_failed_prop_fetch_is_logged_and_the_cycle_goes_on(self, caplog) -> None:
        """SIM-645: a prop fetch error no longer escapes the live cycle."""
        provider = _RaisingPropProvider("strikeouts")
        pipeline = _make_pipeline(provider)
        with caplog.at_level(logging.WARNING, logger="betting.load_odds_to_db"):
            written = await pipeline._persist_prop_roles_cycle(
                GAME_92857, {BRYCE_MILLER: "pitcher", JULIO_RODRIGUEZ: "batter"}
            )
        assert "strikeouts" in provider.calls
        assert "prop fetch failed" in caplog.text
        assert written > 0
        assert all(bind[4] != "strikeouts" for bind in _sent(pipeline._db))

    @pytest.mark.asyncio
    async def test_a_failed_write_is_logged_and_the_next_offer_still_writes(self, caplog) -> None:
        pipeline = _make_pipeline()
        offers = _expected_offers()
        first_batch = sum(1 for row in offers[0][1] if check_row(row) is None)
        pipeline._db.executemany.side_effect = [OSError("connection lost")] + [None] * 100
        with caplog.at_level(logging.WARNING, logger="betting.load_odds_to_db"):
            written = await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        assert "odds write failed" in caplog.text
        assert written == _kept(offers) - first_batch

    def test_a_bare_pipeline_falls_back_to_the_bettingpros_provider(self) -> None:
        pipeline = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        provider = pipeline._odds_provider()
        assert isinstance(provider, BettingProsOddsProvider)
        assert pipeline._odds_provider() is provider


# ===========================================================================
# AC#2 — mark_closing_prop_lines stamps closing lines
# ===========================================================================


def _current_prop_row(row_id: int, player_id: int, *, line_type: str = "current") -> dict:
    """SIM-546: one stored prop row as the closing promotion reads it."""
    row = {
        "id": row_id,
        "source": "bettingpros",
        "line_type": line_type,
        "player_id": player_id,
        "prop_stat": "strikeouts",
        "book": "bp:12",
        "is_sharp_book": False,
        "book_line_at": None,
        "line": 5.5,
        "over_ml": -115,
        "under_ml": -105,
    }
    row["odds_hash"] = _prop_odds_hash(row)
    return row


class TestMarkClosingPropLines:
    """SIM-546 rewrote the marker: one candidate per (player, prop, book), the
    hash rewritten to the closing hash, and a second call promotes nothing."""

    @pytest.mark.asyncio
    async def test_marks_closing_prop_lines(self) -> None:
        """mark_closing_prop_lines promotes raw.prop_odds current->closing."""
        pipeline = _make_pipeline()
        rows = [_current_prop_row(1, 100), _current_prop_row(2, 200)]
        pipeline._db.fetch.side_effect = [rows, []]  # the read, then the existence check
        pipeline._db.execute.return_value = "UPDATE 2"
        first_pitch = datetime(2024, 8, 15, 19, 5, tzinfo=UTC)

        updated = await pipeline.mark_closing_prop_lines(745000, first_pitch)

        assert updated == 2
        read_sql = pipeline._db.fetch.await_args_list[0].args[0]
        assert "raw.prop_odds" in read_sql
        # Per-prop fan-out: the latest row per (player, prop, book), current or closing.
        assert "DISTINCT ON (player_id, prop_stat, book)" in read_sql
        assert "'current', 'closing'" in read_sql
        sql, ids, hashes = pipeline._db.execute.await_args.args
        assert "raw.prop_odds" in sql
        assert "line_type = 'closing'" in sql
        assert "line_type = 'current'" in sql
        assert ids == [1, 2]
        assert hashes == [_prop_odds_hash({**row, "line_type": "closing"}) for row in rows]

    @pytest.mark.asyncio
    async def test_closing_returns_zero_when_no_rows(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.fetch.return_value = []
        updated = await pipeline.mark_closing_prop_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert updated == 0
        pipeline._db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_call_promotes_nothing(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.fetch.return_value = [_current_prop_row(1, 100, line_type="closing")]
        updated = await pipeline.mark_closing_prop_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert updated == 0
        pipeline._db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closing_passes_game_and_pitch_time(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.fetch.side_effect = [[_current_prop_row(1, 100)], []]
        pipeline._db.execute.return_value = "UPDATE 1"
        first_pitch = datetime(2024, 8, 15, 19, 5, tzinfo=UTC)
        await pipeline.mark_closing_prop_lines(777, first_pitch)
        args = pipeline._db.fetch.await_args_list[0].args
        assert 777 in args
        assert first_pitch in args


# ===========================================================================
# ===========================================================================
# AC#5 — Fetch cadence throttling
# ===========================================================================


class TestFetchCadence:
    @pytest.mark.asyncio
    async def test_cadence_skips_second_immediate_call(self) -> None:
        """A second cycle within PROP_FETCH_CADENCE_S must be a no-op."""
        pipeline = _make_pipeline()
        state = _make_game_state()

        first = await pipeline._persist_prop_odds_cycle(GAME_92857, state)
        assert first > 0
        calls_after_first = pipeline._db.executemany.await_count

        second = await pipeline._persist_prop_odds_cycle(GAME_92857, state)
        assert second == 0, "cadence gate did not throttle the immediate re-fetch"
        assert pipeline._db.executemany.await_count == calls_after_first

    @pytest.mark.asyncio
    async def test_cadence_allows_call_after_window(self) -> None:
        """Once PROP_FETCH_CADENCE_S has elapsed, the next cycle fetches again."""
        pipeline = _make_pipeline()
        state = _make_game_state()

        await pipeline._persist_prop_odds_cycle(GAME_92857, state)
        # Backdate the last-fetch stamp well beyond the cadence window.
        pipeline._last_prop_fetch[GAME_92857] = datetime.now(UTC) - timedelta(hours=1)

        again = await pipeline._persist_prop_odds_cycle(GAME_92857, state)
        assert again > 0, "cadence gate stayed closed after the window elapsed"

    @pytest.mark.asyncio
    async def test_the_game_cycle_has_its_own_clock(self) -> None:
        pipeline = _make_pipeline()
        await pipeline._persist_prop_odds_cycle(GAME_92857, _make_game_state())
        assert await pipeline._persist_game_odds_cycle(GAME_92857) > 0
        assert await pipeline._persist_game_odds_cycle(GAME_92857) == 0


# ===========================================================================
# AC#6 — Dedup hash
# ===========================================================================


class TestDedupHash:
    def test_identical_quotes_same_hash(self) -> None:
        a, b = recorded_prop_rows()[0], recorded_prop_rows()[0]
        assert _prop_odds_hash(a) == _prop_odds_hash(b)

    def test_different_book_different_hash(self) -> None:
        rows = recorded_prop_rows()
        assert len({_prop_odds_hash(r) for r in rows}) == len(rows)

    def test_different_line_type_different_hash(self) -> None:
        current = {r["book"]: r for r in recorded_prop_rows()}
        (opening,) = recorded_prop_rows(line_type="opening")
        same_book = current[opening["book"]]
        assert _prop_odds_hash({**same_book, "line_type": "opening"}) != _prop_odds_hash(same_book)
        assert _prop_odds_hash(opening) != _prop_odds_hash(same_book)
