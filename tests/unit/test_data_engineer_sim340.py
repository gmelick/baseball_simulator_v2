"""
test_data_engineer_sim340.py
============================
SIM-340 — Real odds provider + prop ingestion (multi-book, sharp flag, cadence)

Permanent regression suite for the SIM-340 wiring:

  1. The prop persist path is actually invoked on a simulated live fetch cycle
     (it existed but was NEVER called before this ticket). SIM-555: the cycle
     persists each offer's rows in one batch (``_persist_prop_odds_many``).
  2. ``mark_closing_prop_lines`` stamps the closing prop line (mirror of the
     game-level ``mark_closing_lines``).
  3. Multi-book ingestion + an ``is_sharp_book`` flag are persisted. SIM-555
     changed this on purpose: the books come from the provider (one row per
     book, each under its own label), not from a fixed PROP_BOOKS list.
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

from pipeline.live import live_ingestion_pipeline as live  # noqa: E402
from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    BATTER_PROP_STATS,
    PITCHER_PROP_STATS,
    PROP_STATS,
    LiveIngestionPipeline,
    MockOddsAPI,
)

# ===========================================================================
# Helpers
# ===========================================================================

#: SIM-555: the labels the three-book fake provider names.
_THREE_BOOKS = ("bp:12", "bp:10", "bp:0")


class _ThreeBookProvider(MockOddsAPI):
    """The mock with three books: each by-book call gives one row per label.

    Every row is the mock's quote relabelled, so the prices are real mock
    prices. ``bp:0`` (the vendor's blend) carries ``is_sharp_book=True`` to
    prove the pipeline keeps the provider's flag.
    """

    def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, *, line_type="current"):
        return [
            MockOddsAPI.get_prop_odds(
                game_pk,
                player_id,
                prop_stat,
                line_type=line_type,
                book=label,
                is_sharp_book=(label == "bp:0"),
            )
            for label in _THREE_BOOKS
        ]


def _make_pipeline() -> LiveIngestionPipeline:
    """Construct a pipeline without running __init__ (no DSN/Redis needed)."""
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._last_prop_fetch = {}
    return p


def _batch_writer() -> AsyncMock:
    """A stand-in for ``_persist_prop_odds_many``: returns the rows it was sent."""
    return AsyncMock(side_effect=lambda rows: len(rows))


def _rows_sent(mock: AsyncMock) -> list[dict]:
    """Every row a mocked ``_persist_prop_odds_many`` received, in call order."""
    return [row for call in mock.await_args_list for row in call.args[0]]


def _make_game_state(
    current_pitcher_id: int = 999,
    home_lineup_ids: list[int] | None = None,
    away_lineup_ids: list[int] | None = None,
) -> dict:
    """Minimal game_state with the keys _collect_prop_player_roles() reads.

    SIM-421: every lineup entry carries a position-player code so the cycle
    exercises the pitcher / batter market split (the pitcher gets the
    PITCHER_PROP_STATS, each hitter the BATTER_PROP_STATS).
    """
    home_lineup_ids = home_lineup_ids if home_lineup_ids is not None else [101, 102]
    away_lineup_ids = away_lineup_ids if away_lineup_ids is not None else [201]
    return {
        "game_pk": 745000,
        "current_pitcher_id": current_pitcher_id,
        "home_lineup": [{"player_id": pid, "position": "CF"} for pid in home_lineup_ids],
        "away_lineup": [{"player_id": pid, "position": "1B"} for pid in away_lineup_ids],
    }


#: SIM-421: the offer count the default fixture yields — one pitcher × the
#: pitcher markets + three hitters × the batter markets. SIM-555: the mock is
#: one book, so each offer is one row.
_DEFAULT_FIXTURE_OFFERS = len(PITCHER_PROP_STATS) + 3 * len(BATTER_PROP_STATS)


# ===========================================================================
# AC#1 — the prop persist path is invoked on a simulated fetch cycle
# ===========================================================================


class TestSIM340PersistPropOddsWired:
    @pytest.mark.asyncio
    async def test_cycle_calls_persist_prop_odds(self) -> None:
        """
        The live cycle MUST persist its prop rows.  This is the core SIM-340
        regression: before the ticket the persist method existed but was never
        invoked anywhere.
        """
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds_many = _batch_writer()

        written = await pipeline._persist_prop_odds_cycle(745000, _make_game_state())

        assert pipeline._persist_prop_odds_many.await_count > 0, (
            "the prop cycle persisted nothing — the SIM-340 live wiring is broken."
        )
        # SIM-555 changed this pin deliberately. It was (5 + 3 × 10) markets ×
        # 4 PROP_BOOKS = 140 single-row writes; the mock is one book, so the 35
        # offers give 35 rows, one batch per offer.
        assert _DEFAULT_FIXTURE_OFFERS == 35
        assert pipeline._persist_prop_odds_many.await_count == _DEFAULT_FIXTURE_OFFERS
        assert len(_rows_sent(pipeline._persist_prop_odds_many)) == _DEFAULT_FIXTURE_OFFERS
        assert written == _DEFAULT_FIXTURE_OFFERS

    @pytest.mark.asyncio
    async def test_cycle_writes_to_prop_odds_table(self) -> None:
        """End-to-end through the real batch writer: the INSERT targets
        raw.prop_odds and carries the odds_hash dedup column."""
        pipeline = _make_pipeline()

        await pipeline._persist_prop_odds_cycle(745000, _make_game_state())

        assert pipeline._db.executemany.await_count > 0
        sql = pipeline._db.executemany.await_args_list[0].args[0]
        assert "raw.prop_odds" in sql
        assert "odds_hash" in sql, "SIM-340 dedup column not written"
        assert "ON CONFLICT" in sql, "SIM-340 dedup ON CONFLICT not used"
        assert "book_line_at" in sql, "SIM-555 stamp column not written"

    @pytest.mark.asyncio
    async def test_cycle_noop_when_no_players(self) -> None:
        """No eligible players (lineups not posted) → no prop writes, cadence
        clock NOT stamped so the next signal retries promptly."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds_many = AsyncMock()
        empty_state = {
            "game_pk": 745000,
            "current_pitcher_id": None,
            "home_lineup": [],
            "away_lineup": [],
        }

        written = await pipeline._persist_prop_odds_cycle(745000, empty_state)

        assert written == 0
        pipeline._persist_prop_odds_many.assert_not_awaited()
        assert 745000 not in pipeline._last_prop_fetch


# ===========================================================================
# AC#3 — Multi-book + sharp flag persisted (SIM-555: the provider's books)
# ===========================================================================


class TestSIM340MultiBookSharpFlag:
    def test_the_fixed_book_list_is_gone(self) -> None:
        """SIM-555 rewrote this on purpose. It pinned a sharp and a soft book on
        PROP_BOOKS; that list wrote one price set four times under four names.
        The provider now names the books, so the list must not come back."""
        assert not hasattr(live, "PROP_BOOKS")

    def test_fetch_prop_odds_covers_every_book_the_provider_names(self) -> None:
        """Every (player, stat) is quoted at every book the provider names."""
        pipeline = _make_pipeline()
        pipeline._odds = _ThreeBookProvider()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        assert {q["book"] for q in quotes} == set(_THREE_BOOKS)
        # One row per stat per book for the single player. No ``roles`` were
        # passed, so the player gets every market (the SIM-421 safe default).
        assert len(quotes) == len(PROP_STATS) * len(_THREE_BOOKS)

    def test_the_one_book_mock_gives_one_row_per_market(self) -> None:
        pipeline = _make_pipeline()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        assert len(quotes) == len(PROP_STATS)
        assert {q["book"] for q in quotes} == {"consensus"}

    @pytest.mark.asyncio
    async def test_the_cycle_persists_every_book_in_one_batch_per_offer(self) -> None:
        pipeline = _make_pipeline()
        pipeline._odds = _ThreeBookProvider()
        pipeline._persist_prop_odds_many = _batch_writer()

        written = await pipeline._persist_prop_odds_cycle(745000, _make_game_state())

        assert written == _DEFAULT_FIXTURE_OFFERS * len(_THREE_BOOKS)
        assert pipeline._persist_prop_odds_many.await_count == _DEFAULT_FIXTURE_OFFERS
        for call in pipeline._persist_prop_odds_many.await_args_list:
            batch = call.args[0]
            assert [r["book"] for r in batch] == list(_THREE_BOOKS)
            assert len({(r["player_id"], r["prop_stat"]) for r in batch}) == 1

    def test_sharp_flag_propagates_to_quotes(self) -> None:
        """is_sharp_book on each quote is the provider's flag, carried unchanged."""
        pipeline = _make_pipeline()
        pipeline._odds = _ThreeBookProvider()
        quotes = pipeline._fetch_prop_odds(745000, [101], line_type="current")
        for q in quotes:
            assert q["is_sharp_book"] == (q["book"] == "bp:0"), (
                f"book {q['book']} sharp flag mismatch"
            )

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
    row["odds_hash"] = LiveIngestionPipeline._prop_odds_hash(row)
    return row


class TestSIM340MarkClosingPropLines:
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
        assert hashes == [
            LiveIngestionPipeline._prop_odds_hash({**row, "line_type": "closing"}) for row in rows
        ]

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
# AC#4 — Opening-line capture (SIM-138 hook)
# ===========================================================================


class TestSIM340OpeningLineCapture:
    @pytest.mark.asyncio
    async def test_capture_opening_writes_opening_line_type(self) -> None:
        """capture_opening_prop_lines must persist rows with line_type='opening'."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds_many = _batch_writer()

        written = await pipeline.capture_opening_prop_lines(745000, [999])

        captured_quotes = _rows_sent(pipeline._persist_prop_odds_many)
        # SIM-555: the one-book mock gives one opening row per market (was × 4
        # PROP_BOOKS).
        assert written == len(PROP_STATS)
        assert captured_quotes, "no opening quotes captured"
        assert all(q["line_type"] == "opening" for q in captured_quotes), (
            "opening capture wrote a non-opening line_type"
        )

    @pytest.mark.asyncio
    async def test_capture_opening_multi_book(self) -> None:
        """Opening capture persists every book the provider names, under its label."""
        pipeline = _make_pipeline()
        pipeline._odds = _ThreeBookProvider()
        pipeline._persist_prop_odds_many = _batch_writer()
        await pipeline.capture_opening_prop_lines(745000, [999])
        captured = _rows_sent(pipeline._persist_prop_odds_many)
        assert {q["book"] for q in captured} == set(_THREE_BOOKS)

    @pytest.mark.asyncio
    async def test_capture_opening_takes_no_books_argument(self) -> None:
        """SIM-555: the ``books`` argument left with PROP_BOOKS."""
        pipeline = _make_pipeline()
        with pytest.raises(TypeError):
            await pipeline.capture_opening_prop_lines(  # type: ignore[call-arg]
                745000, [999], books=[("x", False)]
            )


# ===========================================================================
# AC#5 — Fetch cadence throttling
# ===========================================================================


class TestSIM340FetchCadence:
    @pytest.mark.asyncio
    async def test_cadence_skips_second_immediate_call(self) -> None:
        """A second cycle within PROP_FETCH_CADENCE_S must be a no-op."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds_many = _batch_writer()
        state = _make_game_state()

        first = await pipeline._persist_prop_odds_cycle(745000, state)
        assert first > 0
        calls_after_first = pipeline._persist_prop_odds_many.await_count

        second = await pipeline._persist_prop_odds_cycle(745000, state)
        assert second == 0, "cadence gate did not throttle the immediate re-fetch"
        assert pipeline._persist_prop_odds_many.await_count == calls_after_first

    @pytest.mark.asyncio
    async def test_cadence_allows_call_after_window(self) -> None:
        """Once PROP_FETCH_CADENCE_S has elapsed, the next cycle fetches again."""
        pipeline = _make_pipeline()
        pipeline._persist_prop_odds_many = _batch_writer()
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
