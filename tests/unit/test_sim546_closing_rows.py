"""
test_sim546_closing_rows.py
===========================
SIM-546, part D: every live game's closing rows (the design's tests 15 to 22).

A closing row is the last price a book posted before first pitch. At first pitch
the live pipeline promotes, per (market, book) for game odds and per (player,
prop stat, book) for props, the latest pre-pitch row from 'current' to
'closing', through the load guard's closing-stamp rule, and rewrites the row's
dedup hash so the nightly loader's identical row deduplicates against it. The
schedule poll calls the promotion the first time it sees a game Live. Inside 15
minutes of the scheduled start the pre-game cycle reads the vendor every minute.
A nightly job runs the historical loader's closing pass over the previous two
dates as the fallback.

Idiom: the AsyncMock idiom of ``test_live_pipeline_sim348.py`` and an
in-memory stand-in for the two odds tables (``_FakeOddsDb``) that runs the
promotion's three statements the way Postgres would: the latest row per key,
the existence check on the dedup index's key, and the guarded update.

Run:
    pytest tests/unit/test_sim546_closing_rows.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.live import live_ingestion_pipeline as live  # noqa: E402
from pipeline.live.live_ingestion_pipeline import (  # noqa: E402
    LiveIngestionPipeline,
    closing_candidates,
    closing_hash,
    pregame_odds_cadence_s,
    promote_closing_game_rows,
    promote_closing_prop_rows,
)
from pipeline.odds_provider import GAME_MARKET_KIND, GAME_MARKET_TYPES  # noqa: E402

GAME_PK = 776465
SCHEDULED_START = datetime(2026, 10, 9, 23, 5, tzinfo=UTC)
FIRST_PITCH = SCHEDULED_START + timedelta(minutes=4)
#: Twelve sportsbook labels, one row each per market.
BOOKS = tuple(f"bp:{i}" for i in (12, 10, 19, 13, 15, 18, 20, 21, 24, 25, 27, 28))
#: Three pre-pitch fetch times; the last is the one that becomes the closing row.
FETCHES = (
    FIRST_PITCH - timedelta(minutes=21),
    FIRST_PITCH - timedelta(minutes=11),
    FIRST_PITCH - timedelta(minutes=1),
)


# ===========================================================================
# Rows
# ===========================================================================


def _prices(market: str, step: int) -> dict[str, Any]:
    """Prices the load guard keeps for ``market``; ``step`` moves them a little."""
    kind = GAME_MARKET_KIND[market]
    if kind == "moneyline":
        return {"home_ml": -120 - step, "away_ml": 110 + step}
    if kind == "three_way":
        if market.startswith("f5"):
            return {"home_ml": 130 + step, "away_ml": 160 + step, "draw_ml": 450}
        return {"home_ml": 250 + step, "away_ml": 280 + step, "draw_ml": -105}
    if kind == "runline":
        spread = 1.5 if market == "runline" else 0.5
        return {
            "home_spread": -spread,
            "home_spread_ml": 140 + step,
            "away_spread": spread,
            "away_spread_ml": -160 - step,
        }
    line = {
        "total": 8.5,
        "f1_total": 0.5,
        "f5_total": 4.5,
        "team_total_home": 4.5,
        "team_total_away": 3.5,
        "f5_team_total_home": 2.5,
        "f5_team_total_away": 1.5,
        "first_inning_run": 0.5,
    }[market]
    return {"total_line": line, "over_ml": -110 - step, "under_ml": -110 + step}


def _game_row(
    row_id: int,
    market: str,
    book: str,
    fetched_at: datetime,
    *,
    step: int = 0,
    line_type: str = "current",
    book_line_at: datetime | None = None,
    source: str = "bettingpros",
) -> dict[str, Any]:
    """One stored raw.game_odds row, its hash computed by the live writer's rule."""
    row: dict[str, Any] = {
        "id": row_id,
        "game_pk": GAME_PK,
        "source": source,
        "line_type": line_type,
        "book": book,
        "market_type": market,
        "is_sharp_book": False,
        "book_line_at": book_line_at,
        "fetched_at": fetched_at,
        "home_ml": None,
        "away_ml": None,
        "draw_ml": None,
        "home_spread": None,
        "home_spread_ml": None,
        "away_spread": None,
        "away_spread_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }
    row.update(_prices(market, step))
    row["odds_hash"] = LiveIngestionPipeline._odds_hash(row)
    return row


def _prop_row(
    row_id: int,
    player_id: int,
    prop_stat: str,
    book: str,
    fetched_at: datetime,
    *,
    step: int = 0,
    line_type: str = "current",
) -> dict[str, Any]:
    """One stored raw.prop_odds row, its hash computed by the live writer's rule."""
    row: dict[str, Any] = {
        "id": row_id,
        "game_pk": GAME_PK,
        "source": "bettingpros",
        "line_type": line_type,
        "player_id": player_id,
        "prop_stat": prop_stat,
        "book": book,
        "is_sharp_book": False,
        "book_line_at": None,
        "fetched_at": fetched_at,
        "line": 1.5,
        "over_ml": -115 - step,
        "under_ml": -105 + step,
    }
    row["odds_hash"] = LiveIngestionPipeline._prop_odds_hash(row)
    return row


def _slate_rows() -> list[dict[str, Any]]:
    """Twelve books x fifteen markets of current rows at three fetch times (540 rows)."""
    rows: list[dict[str, Any]] = []
    for step, fetched_at in enumerate(FETCHES):
        for market in GAME_MARKET_TYPES:
            for book in BOOKS:
                rows.append(_game_row(len(rows) + 1, market, book, fetched_at, step=step))
    return rows


def _latest_per_key(
    rows: list[dict[str, Any]],
    key: tuple[str, ...],
    *,
    first_pitch: datetime = FIRST_PITCH,
    stamp_bound: datetime | None = None,
) -> list[dict[str, Any]]:
    """The promotion's read: one pre-pitch current-or-closing row per key.

    It runs the SQL's rule: rows fetched at or before ``first_pitch``; a
    current row stamped after ``stamp_bound`` left out (``None`` = no bound);
    then per key a closing row first, else the latest fetch, else the highest id.
    """
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}

    def _rank(row: dict[str, Any]) -> tuple[Any, ...]:
        return (row["line_type"] == "closing", row["fetched_at"], row["id"])

    for row in rows:
        if row["line_type"] not in ("current", "closing") or row["fetched_at"] > first_pitch:
            continue
        stamp = row.get("book_line_at")
        if (
            row["line_type"] == "current"
            and stamp_bound is not None
            and stamp is not None
            and stamp > stamp_bound
        ):
            continue
        k = tuple(row[c] for c in key)
        best = latest.get(k)
        if best is None or _rank(row) > _rank(best):
            latest[k] = row
    return list(latest.values())


class _FakeOddsDb:
    """An in-memory raw.game_odds / raw.prop_odds for the promotion's three statements.

    It answers the DISTINCT ON read, the existence check and the guarded update
    as Postgres would, and it enforces the dedup index: an update that would
    give two rows of one (game, [player,] source) the same hash raises.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.updates = 0

    @staticmethod
    def _is_prop(sql: str) -> bool:
        return "raw.prop_odds" in sql

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "DISTINCT ON" in sql:
            game_pk, first_pitch, stamp_bound = args
            assert "(line_type = 'closing') DESC" in sql
            key = (
                ("player_id", "prop_stat", "book")
                if self._is_prop(sql)
                else ("market_type", "book")
            )
            latest = _latest_per_key(
                self.rows, key, first_pitch=first_pitch, stamp_bound=stamp_bound
            )
            return [dict(r) for r in latest if r["game_pk"] == game_pk]
        assert "odds_hash = ANY" in sql
        game_pk, source, hashes = args
        return [
            dict(r)
            for r in self.rows
            if r["game_pk"] == game_pk and r["source"] == source and r["odds_hash"] in hashes
        ]

    async def execute(self, sql: str, ids: list[int], hashes: list[str]) -> str:
        assert "UPDATE" in sql and "line_type = 'current'" in sql
        self.updates += 1
        by_id = {r["id"]: r for r in self.rows}
        n = 0
        for row_id, new_hash in zip(ids, hashes, strict=True):
            row = by_id[row_id]
            if row["line_type"] != "current":
                continue
            index_key = ("player_id", "source") if self._is_prop(sql) else ("source",)
            for other in self.rows:
                if (
                    other is not row
                    and other["odds_hash"] == new_hash
                    and all(other.get(k) == row.get(k) for k in index_key)
                ):
                    raise AssertionError("unique violation on the dedup index")
            row["line_type"] = "closing"
            row["odds_hash"] = new_hash
            n += 1
        return f"UPDATE {n}"


# ===========================================================================
# Test 15 — one closing row per (market, book)
# ===========================================================================


class TestOneClosingRowPerMarketAndBook:
    def test_one_closing_row_per_market_and_book(self) -> None:
        rows = _slate_rows()
        assert len(rows) == 3 * 15 * 12
        latest = _latest_per_key(rows, ("market_type", "book"))
        picks = closing_candidates(latest, scheduled_start=SCHEDULED_START)
        assert len(picks) == 180
        by_id = {r["id"]: r for r in rows}
        assert {by_id[i]["fetched_at"] for i, _ in picks} == {FETCHES[-1]}
        assert len({(by_id[i]["market_type"], by_id[i]["book"]) for i, _ in picks}) == 180
        for row_id, new_hash in picks:
            row = by_id[row_id]
            assert new_hash == LiveIngestionPipeline._odds_hash({**row, "line_type": "closing"})
            assert new_hash != row["odds_hash"]

    @pytest.mark.asyncio
    async def test_the_promotion_writes_them_in_one_update(self) -> None:
        db = _FakeOddsDb(_slate_rows())
        n = await promote_closing_game_rows(
            db, GAME_PK, FIRST_PITCH, scheduled_start=SCHEDULED_START
        )
        assert n == 180
        assert db.updates == 1
        closing = [r for r in db.rows if r["line_type"] == "closing"]
        assert len(closing) == 180
        assert {r["fetched_at"] for r in closing} == {FETCHES[-1]}

    def test_a_three_way_row_hashes_its_tie(self) -> None:
        row = _game_row(1, "f5_moneyline", "bp:12", FETCHES[-1])
        [(row_id, new_hash)] = closing_candidates([row], scheduled_start=SCHEDULED_START)
        assert row_id == 1
        assert new_hash == LiveIngestionPipeline._odds_hash({**row, "line_type": "closing"})
        assert new_hash != LiveIngestionPipeline._odds_hash(
            {**row, "line_type": "closing", "draw_ml": None}
        )


# ===========================================================================
# Test 16 — a second call promotes nothing
# ===========================================================================


class TestSecondCallPromotesNothing:
    def test_second_call_promotes_nothing(self) -> None:
        rows = _slate_rows()
        for row in rows:
            if row["fetched_at"] == FETCHES[-1]:
                row["line_type"] = "closing"
        latest = _latest_per_key(rows, ("market_type", "book"))
        assert closing_candidates(latest, scheduled_start=SCHEDULED_START) == []

    @pytest.mark.asyncio
    async def test_the_promotion_run_twice_changes_nothing_the_second_time(self) -> None:
        db = _FakeOddsDb(_slate_rows())
        assert (
            await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 180
        )
        before = [dict(r) for r in db.rows]
        assert await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 0
        assert db.rows == before
        assert db.updates == 1  # the second call sent no update


class TestRestartMidGame:
    """A restart mid-game calls the promotion again with a later instant. The
    live cycle has written in-play 'current' rows by then; none may become a
    second closing row."""

    @pytest.mark.asyncio
    async def test_a_restart_promotes_no_in_play_row(self) -> None:
        closing = _game_row(
            1,
            "total",
            "bp:12",
            FIRST_PITCH - timedelta(minutes=1),
            book_line_at=SCHEDULED_START - timedelta(minutes=2),
        )
        db = _FakeOddsDb([closing])
        assert (
            await promote_closing_game_rows(
                db, GAME_PK, FIRST_PITCH, scheduled_start=SCHEDULED_START
            )
            == 1
        )
        in_play_stamped = _game_row(
            2,
            "total",
            "bp:12",
            FIRST_PITCH + timedelta(minutes=8),
            step=3,
            book_line_at=SCHEDULED_START + timedelta(minutes=12),
        )
        in_play_unstamped = _game_row(
            3, "total", "bp:12", FIRST_PITCH + timedelta(minutes=9), step=4
        )
        db.rows.extend([in_play_stamped, in_play_unstamped])
        n = await promote_closing_game_rows(
            db, GAME_PK, FIRST_PITCH + timedelta(hours=2), scheduled_start=SCHEDULED_START
        )
        assert n == 0
        assert [r["id"] for r in db.rows if r["line_type"] == "closing"] == [1]
        assert db.updates == 1

    @pytest.mark.asyncio
    async def test_a_restart_minutes_later_promotes_no_moved_price(self) -> None:
        db = _FakeOddsDb([_game_row(1, "moneyline", "bp:12", FETCHES[-1])])
        assert await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 1
        db.rows.append(
            _game_row(2, "moneyline", "bp:12", FIRST_PITCH + timedelta(minutes=2), step=5)
        )
        n = await promote_closing_game_rows(
            db, GAME_PK, FIRST_PITCH + timedelta(minutes=7), scheduled_start=None
        )
        assert n == 0
        assert [(r["id"], r["line_type"]) for r in db.rows] == [(1, "closing"), (2, "current")]

    @pytest.mark.asyncio
    async def test_a_restart_promotes_no_in_play_prop_row(self) -> None:
        db = _FakeOddsDb([_prop_row(1, 100, "hits", "bp:12", FETCHES[-1])])
        assert await promote_closing_prop_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 1
        db.rows.append(
            _prop_row(2, 100, "hits", "bp:12", FIRST_PITCH + timedelta(minutes=8), step=2)
        )
        n = await promote_closing_prop_rows(
            db, GAME_PK, FIRST_PITCH + timedelta(hours=1), scheduled_start=None
        )
        assert n == 0
        assert [r["id"] for r in db.rows if r["line_type"] == "closing"] == [1]


# ===========================================================================
# Test 17 — a late stamp is refused
# ===========================================================================


def _delayed_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    """A book's line before a delay (stamped inside the grace) and its move during it."""
    early = _game_row(
        1, "total", "bp:12", FETCHES[0], book_line_at=SCHEDULED_START - timedelta(minutes=30)
    )
    late = _game_row(
        2,
        "total",
        "bp:12",
        FETCHES[-1],
        step=1,
        book_line_at=SCHEDULED_START + timedelta(minutes=20),
    )
    return early, late


class TestLateStamp:
    def test_late_stamp_is_refused(self) -> None:
        """The design's D4: a row stamped 20 minutes after the scheduled start is
        not promoted; the book's last row stamped inside the grace is."""
        early, late = _delayed_pair()
        # The guard alone refuses the late row.
        assert closing_candidates([late], scheduled_start=SCHEDULED_START) == []
        # The read leaves the late row out, so the predecessor is the candidate.
        latest = _latest_per_key(
            [early, late],
            ("market_type", "book"),
            stamp_bound=SCHEDULED_START + live.CLOSING_STAMP_GRACE,
        )
        assert [r["id"] for r in latest] == [1]
        assert [i for i, _ in closing_candidates(latest, scheduled_start=SCHEDULED_START)] == [1]

    @pytest.mark.asyncio
    async def test_a_delayed_game_keeps_the_book_line_from_before_the_delay(self) -> None:
        early, late = _delayed_pair()
        db = _FakeOddsDb([early, late])
        n = await promote_closing_game_rows(
            db, GAME_PK, FIRST_PITCH, scheduled_start=SCHEDULED_START
        )
        assert n == 1
        assert early["line_type"] == "closing"
        assert late["line_type"] == "current"

    @pytest.mark.asyncio
    async def test_the_read_carries_the_stamp_bound(self) -> None:
        db = AsyncMock()
        db.fetch.return_value = []
        await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=SCHEDULED_START)
        await promote_closing_prop_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None)
        game_args = db.fetch.await_args_list[0].args
        prop_args = db.fetch.await_args_list[1].args
        assert game_args[1:] == (
            GAME_PK,
            FIRST_PITCH,
            SCHEDULED_START + live.CLOSING_STAMP_GRACE,
        )
        assert prop_args[1:] == (GAME_PK, FIRST_PITCH, None)
        for sql in (game_args[0], prop_args[0]):
            assert "book_line_at <= $3::timestamptz" in sql
            assert "(line_type = 'closing') DESC" in sql

    def test_a_stamp_inside_the_grace_and_a_row_with_no_stamp_pass(self) -> None:
        inside = _game_row(
            1, "total", "bp:12", FETCHES[-1], book_line_at=SCHEDULED_START + timedelta(minutes=5)
        )
        unstamped = _game_row(2, "total", "bp:10", FETCHES[-1])
        picks = closing_candidates([inside, unstamped], scheduled_start=SCHEDULED_START)
        assert [i for i, _ in picks] == [1, 2]

    def test_a_naive_stamp_reads_as_utc(self) -> None:
        naive_late = _game_row(
            1,
            "total",
            "bp:12",
            FETCHES[-1],
            book_line_at=(SCHEDULED_START + timedelta(minutes=20)).replace(tzinfo=None),
        )
        assert closing_candidates([naive_late], scheduled_start=SCHEDULED_START) == []


# ===========================================================================
# Test 18 — the loader's row blocks the rewrite
# ===========================================================================


class TestExistingLoaderRow:
    @pytest.mark.asyncio
    async def test_existing_loader_row_blocks_the_rewrite(self) -> None:
        rows = [
            _game_row(1, "moneyline", "bp:12", FETCHES[-1]),
            _game_row(2, "moneyline", "bp:10", FETCHES[-1]),
        ]
        blocked_hash = closing_hash(rows[0])
        db = AsyncMock()
        db.fetch.side_effect = [rows, [{"source": "bettingpros", "odds_hash": blocked_hash}]]
        db.execute.return_value = "UPDATE 1"
        n = await promote_closing_game_rows(
            db, GAME_PK, FIRST_PITCH, scheduled_start=SCHEDULED_START
        )
        assert n == 1
        check_sql, game_pk, source, hashes = db.fetch.await_args_list[1].args
        assert "source = $2" in check_sql and "odds_hash = ANY" in check_sql
        assert (game_pk, source) == (GAME_PK, "bettingpros")
        assert hashes == [blocked_hash, closing_hash(rows[1])]
        _, ids, new_hashes = db.execute.await_args.args
        assert ids == [2]
        assert new_hashes == [closing_hash(rows[1])]

    @pytest.mark.asyncio
    async def test_a_loader_row_already_in_the_table_stays_the_only_closing_row(self) -> None:
        promoted = _game_row(1, "total", "bp:12", FETCHES[-1])
        loader_row = {
            **promoted,
            "id": 2,
            "line_type": "closing",
            "fetched_at": FIRST_PITCH + timedelta(hours=14),
        }
        loader_row["odds_hash"] = LiveIngestionPipeline._odds_hash(loader_row)
        db = _FakeOddsDb([promoted, loader_row])
        assert await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 0
        assert promoted["line_type"] == "current"
        assert db.updates == 0

    @pytest.mark.asyncio
    async def test_every_source_gets_its_own_existence_check(self) -> None:
        rows = [
            _game_row(1, "moneyline", "bp:12", FETCHES[-1]),
            _game_row(2, "moneyline", "consensus", FETCHES[-1], source="mock"),
        ]
        db = AsyncMock()
        db.fetch.side_effect = [rows, [], []]
        db.execute.return_value = "UPDATE 2"
        assert await promote_closing_game_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 2
        sources = [c.args[2] for c in db.fetch.await_args_list[1:]]
        assert sources == ["bettingpros", "mock"]

    def test_the_hash_matches_a_float_typed_writer(self) -> None:
        """The BettingPros provider hashes a price as a float (-110.0), and the
        table stores it as an INTEGER. The closing hash follows the writer's
        typing, so the nightly loader's float-typed closing row deduplicates."""
        written = _game_row(1, "total", "bp:12", FETCHES[-1])
        as_written = {
            **written,
            "over_ml": float(written["over_ml"]),
            "under_ml": float(written["under_ml"]),
        }
        stored = {**written, "odds_hash": LiveIngestionPipeline._odds_hash(as_written)}
        loaders_row = {**as_written, "line_type": "closing"}
        assert closing_hash(stored) == LiveIngestionPipeline._odds_hash(loaders_row)
        assert closing_hash(stored) != LiveIngestionPipeline._odds_hash(
            {**written, "line_type": "closing"}
        )

    def test_a_row_with_no_stored_hash_takes_the_loader_typing(self) -> None:
        row = {**_game_row(1, "total", "bp:12", FETCHES[-1]), "odds_hash": None}
        float_typed = {
            **row,
            "line_type": "closing",
            "over_ml": float(row["over_ml"]),
            "under_ml": float(row["under_ml"]),
        }
        assert closing_hash(row) == LiveIngestionPipeline._odds_hash(float_typed)


# ===========================================================================
# Test 19 — props, per (player, prop stat, book)
# ===========================================================================


class TestPropsPromote:
    @pytest.mark.asyncio
    async def test_props_promote_per_player_prop_and_book(self) -> None:
        rows: list[dict[str, Any]] = []
        players = (100, 200, 300)
        stats = ("strikeouts", "hits", "total_bases")
        for step, fetched_at in enumerate(FETCHES):
            for player_id in players:
                for stat in stats:
                    for book in BOOKS[:4]:
                        rows.append(
                            _prop_row(len(rows) + 1, player_id, stat, book, fetched_at, step=step)
                        )
        latest = _latest_per_key(rows, ("player_id", "prop_stat", "book"))
        picks = closing_candidates(latest, scheduled_start=SCHEDULED_START)
        assert len(picks) == 3 * 3 * 4
        by_id = {r["id"]: r for r in rows}
        for row_id, new_hash in picks:
            row = by_id[row_id]
            assert row["fetched_at"] == FETCHES[-1]
            assert new_hash == LiveIngestionPipeline._prop_odds_hash(
                {**row, "line_type": "closing"}
            )
        db = _FakeOddsDb(rows)
        assert await promote_closing_prop_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 36
        assert await promote_closing_prop_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 0

    @pytest.mark.asyncio
    async def test_a_prop_loader_row_blocks_only_its_own_player(self) -> None:
        rows = [
            _prop_row(1, 100, "hits", "bp:12", FETCHES[-1]),
            _prop_row(2, 200, "hits", "bp:12", FETCHES[-1]),
        ]
        blocked = closing_hash(rows[0])
        db = AsyncMock()
        db.fetch.side_effect = [
            rows,
            [{"player_id": 100, "source": "bettingpros", "odds_hash": blocked}],
        ]
        db.execute.return_value = "UPDATE 1"
        assert await promote_closing_prop_rows(db, GAME_PK, FIRST_PITCH, scheduled_start=None) == 1
        sql, ids, _ = db.execute.await_args.args
        assert "raw.prop_odds" in sql
        assert ids == [2]


# ===========================================================================
# Test 20 — the schedule poll marks the game on its turn to Live
# ===========================================================================


def _bare_pipeline(**attrs: Any) -> LiveIngestionPipeline:
    """A pipeline built without ``__init__`` (the tests/conftest.py idiom)."""
    p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
    p._db = AsyncMock()
    p._redis = None
    p._http = None
    p._sim_cb = None
    p._ws_clients = {}
    p._refresh_locks = {}
    p._last_resim_at_bat = {}
    p._completed_games = set()
    p._builders = {}
    p._last_prop_fetch = {}
    for key, value in attrs.items():
        setattr(p, key, value)
    return p


def _schedule_http(schedule: dict[str, Any]) -> Any:
    class _Resp:
        async def json(self) -> dict[str, Any]:
            return schedule

        async def __aenter__(self) -> _Resp:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _Http:
        def get(self, url: str, params: dict | None = None) -> _Resp:
            return _Resp()

    return _Http()


class TestScheduleTrigger:
    @pytest.mark.asyncio
    async def test_sync_live_games_marks_on_the_preview_to_live_turn(self) -> None:
        game = {
            "gamePk": GAME_PK,
            "gameDate": "2026-10-09T23:05:00Z",
            "status": {"abstractGameState": "Live"},
        }
        p = _bare_pipeline(_http=_schedule_http({"dates": [{"games": [game]}]}))
        order: list[str] = []

        async def _mark(game_pk: int, entry: Any) -> tuple[int, int]:
            order.append("mark")
            return 0, 0

        async def _watch(game_pk: int) -> None:
            order.append("watch")
            p._ws_clients[game_pk] = object()

        p._mark_closing_rows = AsyncMock(side_effect=_mark)
        p._start_watching = AsyncMock(side_effect=_watch)
        p._refresh_game_state = AsyncMock()
        p._upsert_game_record = AsyncMock()
        await p._sync_live_games()
        for _ in range(3):  # let the created tasks run
            await asyncio.sleep(0)
        assert order == ["mark", "watch"]
        p._mark_closing_rows.assert_awaited_once_with(GAME_PK, game)
        # The next poll: the watcher exists, so the game is not marked again.
        await p._sync_live_games()
        for _ in range(3):
            await asyncio.sleep(0)
        p._mark_closing_rows.assert_awaited_once()
        assert order == ["mark", "watch"]

    @pytest.mark.asyncio
    async def test_mark_closing_rows_passes_the_scheduled_start(self) -> None:
        p = _bare_pipeline()
        p.mark_closing_lines = AsyncMock(return_value=180)
        p.mark_closing_prop_lines = AsyncMock(return_value=900)
        before = datetime.now(UTC)
        counts = await p._mark_closing_rows(GAME_PK, {"gameDate": "2026-10-09T23:05:00Z"})
        assert counts == (180, 900)
        for mock in (p.mark_closing_lines, p.mark_closing_prop_lines):
            (game_pk, first_pitch_at), kwargs = mock.await_args
            assert game_pk == GAME_PK
            assert first_pitch_at >= before and first_pitch_at.tzinfo is not None
            assert kwargs == {"scheduled_start": SCHEDULED_START}

    @pytest.mark.asyncio
    async def test_mark_closing_rows_logs_the_counts_and_never_raises(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="live_ingestion")
        p = _bare_pipeline()
        p.mark_closing_lines = AsyncMock(side_effect=RuntimeError("db down"))
        p.mark_closing_prop_lines = AsyncMock(return_value=12)
        assert await p._mark_closing_rows(GAME_PK, {}) == (0, 12)
        assert "closing game rows not promoted for game 776465: db down" in caplog.text
        assert "closing rows promoted: game 776465, 0 game rows, 12 prop rows" in caplog.text
        # No schedule start: the stamp rule is not applied.
        assert p.mark_closing_prop_lines.await_args.kwargs == {"scheduled_start": None}

    @pytest.mark.asyncio
    async def test_mark_closing_rows_without_a_pool_does_nothing(self) -> None:
        p = _bare_pipeline(_db=None)
        p.mark_closing_lines = AsyncMock()
        assert await p._mark_closing_rows(GAME_PK, {}) == (0, 0)
        p.mark_closing_lines.assert_not_awaited()


# ===========================================================================
# Test 21 — the pre-game cadence tightens near the start (decision 3)
# ===========================================================================


class TestPregameCadence:
    def test_pregame_cadence_tightens_near_the_start(self) -> None:
        now = datetime(2026, 10, 9, 22, 55, tzinfo=UTC)
        ten_minutes_out = {"gameDate": "2026-10-09T23:05:00Z"}
        two_hours_out = {"gameDate": "2026-10-10T00:55:00Z"}
        assert pregame_odds_cadence_s(ten_minutes_out, now) == 60
        assert pregame_odds_cadence_s(two_hours_out, now) == 600
        assert live.PREGAME_NEAR_START_CADENCE_S == live.PROP_FETCH_CADENCE_S == 60
        assert live.PREGAME_NEAR_START_WINDOW_S == 15 * 60

    def test_the_window_edges_and_a_delayed_start(self) -> None:
        start = datetime(2026, 10, 9, 23, 5, tzinfo=UTC)
        game = {"gameDate": "2026-10-09T23:05:00Z"}
        assert pregame_odds_cadence_s(game, start - timedelta(minutes=15)) == 60
        assert pregame_odds_cadence_s(game, start - timedelta(minutes=16)) == 600
        # Past the start, still in Preview (a delay): one minute apart.
        assert pregame_odds_cadence_s(game, start + timedelta(minutes=40)) == 60
        # No readable start: the ten-minute cadence.
        assert pregame_odds_cadence_s({}, start) == 600
        assert pregame_odds_cadence_s({"gameDate": "not a date"}, start) == 600

    @pytest.mark.asyncio
    async def test_the_pregame_pass_reads_at_the_picked_cadence(self) -> None:
        now = datetime.now(UTC)
        near = {"gameDate": (now + timedelta(minutes=10)).isoformat()}
        far = {"gameDate": (now + timedelta(hours=2)).isoformat()}
        for game, expect_read in ((near, True), (far, False)):
            p = _bare_pipeline()
            p._last_pregame_fetch = {1: now - timedelta(seconds=90)}
            p._persist_game_odds_cycle = AsyncMock(return_value=3)
            p._persist_prop_roles_cycle = AsyncMock(return_value=0)
            written = await p._persist_pregame_odds(1, game)
            assert (written == 3) is expect_read
            assert p._persist_game_odds_cycle.await_count == (1 if expect_read else 0)


# ===========================================================================
# Test 22 — the nightly closing pass
# ===========================================================================


def _load_loader() -> Any:
    import importlib.util

    path = _ROOT / "scripts" / "load_historical_odds.py"
    spec = importlib.util.spec_from_file_location("load_historical_odds_sim546", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestLoaderGameDates:
    @pytest.mark.asyncio
    async def test_loader_game_dates_filter(self, monkeypatch) -> None:
        loader = _load_loader()
        seen: list[tuple[str, tuple]] = []

        class _Conn:
            async def fetch(self, sql: str, *params: Any) -> list:
                seen.append((sql, params))
                return []

            async def close(self) -> None:
                return None

        async def _connect(dsn: str) -> _Conn:
            return _Conn()

        import types

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))
        await loader._fetch_final_games("dsn", [2026], None)
        assert "game_date" not in seen[-1][0] and seen[-1][1] == ()
        dates = [date(2026, 10, 8), date(2026, 10, 7)]
        await loader._fetch_final_games("dsn", [2026], None, game_dates=dates)
        assert "AND game_date = ANY($1::date[])" in seen[-1][0]
        assert seen[-1][1] == ([date(2026, 10, 7), date(2026, 10, 8)],)
        # After the resume parameters, the dates take the next placeholder.
        since = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
        await loader._fetch_final_games(
            "dsn", [2026], None, skip_loaded_since=since, game_dates=dates
        )
        assert "AND game_date = ANY($3::date[])" in seen[-1][0]
        assert seen[-1][1][-1] == [date(2026, 10, 7), date(2026, 10, 8)]

    def test_parse_args_reads_the_dates(self) -> None:
        loader = _load_loader()
        args = loader.parse_args(
            [
                "--seasons",
                "2026",
                "--line-types",
                "closing",
                "--game-dates",
                "2026-10-08",
                "2026-10-07",
            ]
        )
        assert args.game_dates == [date(2026, 10, 8), date(2026, 10, 7)]
        assert loader.parse_args(["--seasons", "2026"]).game_dates is None
        with pytest.raises(SystemExit) as exc:
            loader.parse_args(["--seasons", "2026", "--game-dates", "10/08/2026"])
        assert exc.value.code == 2

    @pytest.mark.asyncio
    async def test_run_threads_the_dates_to_the_game_query(self, monkeypatch) -> None:
        loader = _load_loader()
        captured: dict[str, Any] = {}

        async def _fetch(dsn: str, seasons: list[int], max_games: Any, **kw: Any) -> list:
            captured.update(kw)
            return []

        monkeypatch.setattr(loader, "_fetch_final_games", _fetch)
        args = loader.parse_args(
            [
                "--seasons",
                "2026",
                "--provider",
                "mock",
                "--line-types",
                "closing",
                "--game-dates",
                "2026-10-08",
                "2026-10-07",
            ]
        )
        assert await loader.run(args) == 0
        assert captured["game_dates"] == [date(2026, 10, 7), date(2026, 10, 8)]
        assert captured["line_types"] == ("closing",)


_SCRIPT = _ROOT / "scripts" / "nightly_closing_lines.sh"
_SH = shutil.which("sh")


def _run_script(tmp_path: Path, **env_over: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the nightly script with a stand-in ``python`` that records its arguments."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    record = tmp_path / "python_args.txt"
    fake = fake_bin / "python"
    fake.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "' + record.as_posix() + '"\n',
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("ODDS_PROVIDER", "ODDS_API_KEY")}
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env.update(env_over)
    assert _SH is not None
    proc = subprocess.run(
        [_SH, _SCRIPT.as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, record


@pytest.mark.skipif(_SH is None, reason="no POSIX sh on this host")
class TestNightlyScript:
    def test_nightly_script_skips_the_mock_provider(self, tmp_path: Path) -> None:
        proc, record = _run_script(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "skipped" in proc.stdout and "unset" in proc.stdout
        assert not record.exists()  # the loader never ran: nothing written
        proc, record = _run_script(tmp_path, ODDS_PROVIDER="mock")
        assert proc.returncode == 0, proc.stderr
        assert not record.exists()

    def test_the_real_provider_without_a_key_fails_loudly(self, tmp_path: Path) -> None:
        proc, record = _run_script(tmp_path, ODDS_PROVIDER="bettingpros")
        assert proc.returncode == 1
        assert "ODDS_API_KEY is not set" in proc.stdout
        assert not record.exists()

    def test_the_real_provider_runs_the_closing_pass_over_two_dates(self, tmp_path: Path) -> None:
        proc, record = _run_script(tmp_path, ODDS_PROVIDER="BettingPros", ODDS_API_KEY="k")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        args = record.read_text(encoding="utf-8").split()
        assert args[0].endswith("scripts/load_historical_odds.py")
        today = datetime.now(UTC).date()
        d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
        i = args.index("--game-dates")
        assert args[i + 1 : i + 3] == [d1.isoformat(), d2.isoformat()]
        assert args[args.index("--line-types") + 1] == "closing"
        assert args[args.index("--provider") + 1] == "bettingpros"
        assert str(d1.year) in args[args.index("--seasons") + 1 : i]


def test_the_ofelia_job_runs_the_script_after_the_ingest_chain() -> None:
    """The closing pass runs inside the app container, which reads the host's
    .env and so holds the vendor key; a fresh job-run container would not."""
    text = (_ROOT / "deploy" / "ofelia" / "config.ini").read_text(encoding="utf-8")
    assert '[job-run "nightly-closing-lines"]' not in text
    assert text.index('[job-run "nightly-ingest"]') < text.index(
        '[job-exec "nightly-closing-lines"]'
    )
    job = text.split('[job-exec "nightly-closing-lines"]', 1)[1]
    assert "schedule = 0 30 9 * * *" in job
    assert "container = baseball_simulator_v2-app-1" in job
    assert "command = env ODDS_PROVIDER=bettingpros sh /app/scripts/nightly_closing_lines.sh" in job
    assert "volume" not in job
    assert "image =" not in job
    # The vendor key is a secret: it never sits in the committed config.
    assert "ODDS_API_KEY=" not in job
