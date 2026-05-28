"""
test_data_engineer_sim340.py
============================
SIM-340 — Real odds provider + prop ingestion (multi-book, sharp flag, cadence)

Permanent regression suite for the SIM-340 wiring:

  1. ``_persist_prop_odds`` is actually invoked on a simulated live fetch cycle
     (it existed but was NEVER called before this ticket).
  2. ``mark_closing_prop_lines`` stamps the closing prop line (mirror of the
     game-level ``mark_closing_lines``).
  3. Multi-book ingestion + an ``is_sharp_book`` flag are persisted.
  4. Opening-line capture (the SIM-138 nightly hook) writes line_type='opening'.
  5. The fetch cadence throttles prop fetches to PROP_FETCH_CADENCE_S per game.
  6. Dedup hash collapses identical snapshots (ON CONFLICT DO NOTHING).

Idiom: async tests with AsyncMock for the asyncpg pool and a mock provider —
no live DB / server, matching tests/unit/test_live_pipeline_bugs.py.

Owned by Data Engineer (Agent 4) + Betting Analyst (Agent 8).

Run:
    pytest tests/unit/test_data_engineer_sim340.py -v
"""

from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so pipeline imports work
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    PROP_BOOKS,
    PROP_STATS,
    LiveIngestionPipeline,
    MockOddsAPI,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _make_pipeline() -> LiveIngestionPipeline:
    """Construct a pipeline without running __init__ (no DSN/Redis needed)."""
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    return p


def _make_game_state(
    current_pitcher_id: int = 999,
    home_lineup_ids: list[int] | None = None,
    away_lineup_ids: list[int] | None = None,
) -> dict:
    """Minimal game_state with the keys _collect_prop_player_ids() reads."""
    home_lineup_ids = home_lineup_ids if home_lineup_ids is not None else [101, 102]
    away_lineup_ids = away_lineup_ids if away_lineup_ids is not None else [201]
    return {
        "game_pk": 745000,
        "current_pitcher_id": current_pitcher_id,
        "home_lineup": [{"player_id": pid} for pid in home_lineup_ids],
        "away_lineup": [{"player_id": pid} for pid in away_lineup_ids],
    }


# ===========================================================================
# AC#1 — _persist_prop_odds is invoked on a simulated fetch cycle
# ===========================================================================


class TestSIM340PersistPropOddsWired:
    @pytest.mark.asyncio
    async def test_cycle_calls_persist_prop_odds(self) -> None:
        """
        The previously-unwired _persist_prop_odds MUST be called when the live
        cycle runs.  This is the core SIM-340 regression: before the ticket the
        method existed but was never invoked anywhere.
        """
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds = AsyncMock()

        written = await pipeline._persist_prop_odds_cycle(745000, _make_game_state())

        assert (
            pipeline._persist_prop_odds.await_count > 0
        ), "_persist_prop_odds was never called — the SIM-340 live wiring is broken."
        # players(4) × stats(7) × books(4) = 112 quotes for the default fixture.
        expected = 4 * len(PROP_STATS) * len(PROP_BOOKS)
        assert pipeline._persist_prop_odds.await_count == expected
        assert written == expected

    @pytest.mark.asyncio
    async def test_cycle_writes_to_prop_odds_table(self) -> None:
        """End-to-end through the real _persist_prop_odds: the INSERT targets
        raw.prop_odds and carries the odds_hash dedup column."""
        pipeline = _make_pipeline()

        await pipeline._persist_prop_odds_cycle(745000, _make_game_state())

        assert pipeline._db.execute.await_count > 0
        sql = pipeline._db.execute.await_args_list[0].args[0]
        assert "raw.prop_odds" in sql
        assert "odds_hash" in sql, "SIM-340 dedup column not written"
        assert "ON CONFLICT" in sql, "SIM-340 dedup ON CONFLICT not used"

    @pytest.mark.asyncio
    async def test_cycle_noop_when_no_players(self) -> None:
        """No eligible players (lineups not posted) → no prop writes, cadence
        clock NOT stamped so the next signal retries promptly."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds = AsyncMock()
        empty_state = {
            "game_pk": 745000,
            "current_pitcher_id": None,
            "home_lineup": [],
            "away_lineup": [],
        }

        written = await pipeline._persist_prop_odds_cycle(745000, empty_state)

        assert written == 0
        pipeline._persist_prop_odds.assert_not_awaited()
        assert 745000 not in pipeline._last_prop_fetch


# ===========================================================================
# AC#3 — Multi-book + sharp flag persisted
# ===========================================================================


class TestSIM340MultiBookSharpFlag:
    def test_prop_books_include_sharp_and_soft(self) -> None:
        """PROP_BOOKS must contain at least one sharp and one soft book."""
        sharp = [b for b, is_sharp in PROP_BOOKS if is_sharp]
        soft = [b for b, is_sharp in PROP_BOOKS if not is_sharp]
        assert sharp, "no sharp books configured — CLV reference line missing"
        assert soft, "no soft books configured — retail line missing"

    def test_fetch_prop_odds_covers_all_books(self) -> None:
        """Every (player, stat, book) combination is quoted."""
        pipeline = _make_pipeline()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        books_seen = {q["book"] for q in quotes}
        assert books_seen == {b for b, _ in PROP_BOOKS}
        # One quote per stat per book for the single player.
        assert len(quotes) == len(PROP_STATS) * len(PROP_BOOKS)

    def test_sharp_flag_propagates_to_quotes(self) -> None:
        """is_sharp_book on each quote matches the PROP_BOOKS classification."""
        pipeline = _make_pipeline()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        classification = dict(PROP_BOOKS)
        for q in quotes:
            assert (
                q["is_sharp_book"] == classification[q["book"]]
            ), f"book {q['book']} sharp flag mismatch"

    @pytest.mark.asyncio
    async def test_sharp_flag_persisted_to_db(self) -> None:
        """The is_sharp_book value reaches the INSERT argument list."""
        pipeline = _make_pipeline()
        sharp_quote = MockOddsAPI.get_prop_odds(
            745000, 999, "strikeouts", book="pinnacle", is_sharp_book=True
        )
        await pipeline._persist_prop_odds(sharp_quote)
        args = pipeline._db.execute.await_args.args
        # is_sharp_book is the 11th positional bind ($11) -> index 11 incl. sql.
        assert True in args, "is_sharp_book=True not present in INSERT binds"
        assert "pinnacle" in args, "book='pinnacle' not present in INSERT binds"


# ===========================================================================
# AC#2 — mark_closing_prop_lines stamps closing lines
# ===========================================================================


class TestSIM340MarkClosingPropLines:
    @pytest.mark.asyncio
    async def test_marks_closing_prop_lines(self) -> None:
        """mark_closing_prop_lines must UPDATE raw.prop_odds current->closing."""
        pipeline = _make_pipeline()
        pipeline._db.execute.return_value = "UPDATE 12"
        first_pitch = datetime(2024, 8, 15, 19, 5, tzinfo=UTC)

        updated = await pipeline.mark_closing_prop_lines(745000, first_pitch)

        assert updated == 12
        sql = pipeline._db.execute.await_args.args[0]
        assert "raw.prop_odds" in sql
        assert "line_type = 'closing'" in sql
        assert "line_type = 'current'" in sql
        # Per-prop fan-out: DISTINCT ON, not a single LIMIT 1.
        assert "DISTINCT ON" in sql

    @pytest.mark.asyncio
    async def test_closing_returns_zero_when_no_rows(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.execute.return_value = "UPDATE 0"
        updated = await pipeline.mark_closing_prop_lines(745000, datetime(2024, 8, 15, tzinfo=UTC))
        assert updated == 0

    @pytest.mark.asyncio
    async def test_closing_passes_game_and_pitch_time(self) -> None:
        pipeline = _make_pipeline()
        pipeline._db.execute.return_value = "UPDATE 1"
        first_pitch = datetime(2024, 8, 15, 19, 5, tzinfo=UTC)
        await pipeline.mark_closing_prop_lines(777, first_pitch)
        args = pipeline._db.execute.await_args.args
        assert 777 in args
        assert first_pitch in args


# ===========================================================================
# AC#4 — Opening-line capture (SIM-138 hook)
# ===========================================================================


class TestSIM340OpeningLineCapture:
    @pytest.mark.asyncio
    async def test_capture_opening_writes_opening_line_type(self) -> None:
        """capture_opening_prop_lines must persist rows with line_type='opening'."""
        pipeline = _make_pipeline()
        captured_quotes: list[dict] = []

        async def _spy(prop: dict) -> None:
            captured_quotes.append(prop)

        pipeline._persist_prop_odds = AsyncMock(side_effect=_spy)

        written = await pipeline.capture_opening_prop_lines(745000, [999])

        assert written == len(PROP_STATS) * len(PROP_BOOKS)
        assert captured_quotes, "no opening quotes captured"
        assert all(
            q["line_type"] == "opening" for q in captured_quotes
        ), "opening capture wrote a non-opening line_type"

    @pytest.mark.asyncio
    async def test_capture_opening_multi_book(self) -> None:
        """Opening capture also fans out across all books (multi-book opening)."""
        pipeline = _make_pipeline()
        captured: list[dict] = []
        pipeline._persist_prop_odds = AsyncMock(
            side_effect=lambda prop: captured.append(prop)  # type: ignore[func-returns-value]
        )
        await pipeline.capture_opening_prop_lines(745000, [999])
        assert {q["book"] for q in captured} == {b for b, _ in PROP_BOOKS}


# ===========================================================================
# AC#5 — Fetch cadence throttling
# ===========================================================================


class TestSIM340FetchCadence:
    @pytest.mark.asyncio
    async def test_cadence_skips_second_immediate_call(self) -> None:
        """A second cycle within PROP_FETCH_CADENCE_S must be a no-op."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds = AsyncMock()
        state = _make_game_state()

        first = await pipeline._persist_prop_odds_cycle(745000, state)
        assert first > 0
        calls_after_first = pipeline._persist_prop_odds.await_count

        second = await pipeline._persist_prop_odds_cycle(745000, state)
        assert second == 0, "cadence gate did not throttle the immediate re-fetch"
        assert pipeline._persist_prop_odds.await_count == calls_after_first

    @pytest.mark.asyncio
    async def test_cadence_allows_call_after_window(self) -> None:
        """Once PROP_FETCH_CADENCE_S has elapsed, the next cycle fetches again."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds = AsyncMock()
        state = _make_game_state()

        await pipeline._persist_prop_odds_cycle(745000, state)
        # Backdate the last-fetch stamp well beyond the cadence window.
        pipeline._last_prop_fetch[745000] = datetime.now(UTC) - timedelta(hours=1)

        again = await pipeline._persist_prop_odds_cycle(745000, state)
        assert again > 0, "cadence gate stayed closed after the window elapsed"


# ===========================================================================
# AC#6 — Dedup hash
# ===========================================================================


class TestSIM340DedupHash:
    def test_identical_quotes_same_hash(self) -> None:
        q1 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", book="pinnacle")
        q2 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", book="pinnacle")
        assert LiveIngestionPipeline._prop_odds_hash(q1) == LiveIngestionPipeline._prop_odds_hash(
            q2
        )

    def test_different_book_different_hash(self) -> None:
        q1 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", book="pinnacle")
        q2 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", book="draftkings")
        assert LiveIngestionPipeline._prop_odds_hash(q1) != LiveIngestionPipeline._prop_odds_hash(
            q2
        )

    def test_different_line_type_different_hash(self) -> None:
        q1 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", line_type="opening")
        q2 = MockOddsAPI.get_prop_odds(745000, 999, "strikeouts", line_type="closing")
        assert LiveIngestionPipeline._prop_odds_hash(q1) != LiveIngestionPipeline._prop_odds_hash(
            q2
        )
