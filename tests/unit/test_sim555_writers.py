"""SIM-555 (2026-09-28) — the writers store every book's row, guarded and batched.

These tests drive the writer half of the build plan
(``docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`` §5.4, test 14):

  * the historical loader (``scripts/load_historical_odds.py``) reads every
    book's row of an offer, drops the empty rows, runs the load guard, sends
    the kept rows in ONE batch, and counts the refusals and the written rows by
    book; ``--book`` restricts a run to one book; the resume query counts only
    the one-row-per-book rows;
  * the live cycle (``pipeline/live/live_ingestion_pipeline.py``) persists
    every book's row of every game market and of every prop offer, one batch
    per offer, with the guard in front; the per-pitch row is no longer stored;
  * ``book_line_at`` reaches both INSERTs with ``odds_hash`` still the LAST
    argument, and the stamp stays out of the hash;
  * an odds row with datetimes survives ``json.dumps`` on its way to a browser;
  * the opening-line job (``pipeline/etl/opening_line_job.py``) stores every
    kept by-book row and skips an empty one (an empty row used to abort the
    run on the NOT NULL prop line, or block tomorrow's retry), through the live
    writers, so every game row carries the NOT NULL ``odds_hash``;
  * the day's games get every book's current line before first pitch (the
    pre-game cycle, driven by the schedule poll);
  * a resumed load skips only the games its own kind of row marks as loaded.

No network and no database. The BettingPros provider's ``_bp_get`` seam is
served from small payloads built here: the scope note's appendix-B first-inning
run line (Hard Rock's "1 / 1" entry priced like a pair), a three-book
moneyline and a three-book strikeout prop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import scripts.load_historical_odds as loader

from pipeline.bettingpros_odds_provider import BettingProsOddsProvider
from pipeline.etl.opening_line_job import OpeningLineJob
from pipeline.live import live_ingestion_pipeline as live
from pipeline.live.live_ingestion_pipeline import (
    ConnectionManager,
    LiveIngestionPipeline,
    MockOddsAPI,
    _jsonable_odds,
    _stamp_param,
)
from pipeline.odds_provider import GAME_MARKET_TYPES, register_odds_provider
from pipeline.odds_row_guard import RefusalTally

# ===========================================================================
# Synthetic BettingPros payloads
# ===========================================================================

#: Every line of the appendix-B payload carries this stamp.
_T = "2025-09-03 20:11:43"
_T_AT = datetime(2025, 9, 3, 20, 11, 43, tzinfo=UTC)


def _line(line: float | None, cost: float | None, updated: str) -> dict[str, Any]:
    return {
        "line": line,
        "cost": cost,
        "updated": updated,
        "main": True,
        "best": False,
        "active": True,
        "is_off": False,
    }


def _sel(
    *,
    participant: str | None = None,
    selection: str = "",
    opener: tuple[int, float | None, float, str] | None = None,
    books: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One selection. ``opener`` is (book_id, line, cost, created); ``books`` maps id → line."""
    sel: dict[str, Any] = {
        "participant": participant,
        "selection": selection,
        "label": selection.title(),
        "books": [{"id": book_id, "lines": [ln]} for book_id, ln in (books or {}).items()],
    }
    if opener is not None:
        book_id, line, cost, created = opener
        sel["opening_line"] = {"line": line, "cost": cost, "book_id": book_id, "created": created}
    return sel


def _offers(*selections: dict[str, Any], participants: list | None = None) -> dict[str, Any]:
    return {"offers": [{"selections": list(selections), "participants": participants or []}]}


#: Appendix B of the scope note (game 776465's first-inning run line, market
#: 282): Hard Rock (49) lists both teams at "+1" priced like one pair.
_APPENDIX_B = _offers(
    _sel(
        participant="BAL",
        opener=(10, 1.5, -1250, "2025-09-02 18:00:00"),
        books={
            0: _line(1.5, -1250, _T),
            10: _line(1.5, -1250, _T),
            33: _line(0.5, -400, _T),
            49: _line(1, -400, _T),
        },
    ),
    _sel(
        participant="SD",
        opener=(10, -1.5, 710, "2025-09-02 18:00:00"),
        books={
            0: _line(-1.5, 230, _T),
            10: _line(-1.5, 710, _T),
            33: _line(-0.5, 230, _T),
            49: _line(1, 260, _T),
        },
    ),
)

#: A three-book full-game moneyline (market 122), FanDuel the opener.
_MONEYLINE = _offers(
    _sel(
        participant="SD",
        opener=(10, None, -150, "2025-09-01 12:00:00"),
        books={
            12: _line(None, -145, "2025-09-03 20:05:00"),
            10: _line(None, -148, "2025-09-03 20:05:00"),
            0: _line(None, -146, "2025-09-03 20:05:00"),
        },
    ),
    _sel(
        participant="BAL",
        opener=(10, None, 130, "2025-09-01 12:00:00"),
        books={
            12: _line(None, 125, "2025-09-03 20:05:00"),
            10: _line(None, 128, "2025-09-03 20:05:00"),
            0: _line(None, 126, "2025-09-03 20:05:00"),
        },
    ),
)

#: A three-book strikeout prop (market 285) for "Bryce Miller". BetMGM (19)
#: posts the over and the under at two different lines: no one book's bet.
_STRIKEOUTS = _offers(
    _sel(
        selection="over",
        opener=(12, 5.5, -112, "2025-09-02 09:00:00"),
        books={
            12: _line(5.5, -115, "2025-09-03 19:50:00"),
            10: _line(5.5, -110, "2025-09-03 19:55:00"),
            19: _line(5.5, -120, "2025-09-03 19:55:00"),
        },
    ),
    _sel(
        selection="under",
        opener=(12, 5.5, -108, "2025-09-02 09:00:00"),
        books={
            12: _line(5.5, -105, "2025-09-03 19:50:00"),
            10: _line(5.5, -110, "2025-09-03 19:55:00"),
            19: _line(6.5, 100, "2025-09-03 19:55:00"),
        },
    ),
    participants=[{"name": "Bryce Miller"}],
)


class _Synthetic(BettingProsOddsProvider):
    """SD (home) against BAL (away) on 2025-09-03, with hand-built ``/offers`` payloads.

    ``start`` is the scheduled start the load guard's stamp rule reads. Every
    player resolves to "Bryce Miller", so the strikeout payload serves any
    pitcher id.
    """

    def __init__(
        self,
        offers_by_market: dict[int, dict[str, Any]],
        *,
        start: datetime = datetime(2025, 9, 3, 20, 10),
        **kw: Any,
    ) -> None:
        super().__init__(api_key="test-key", offers_cache_ttl_s=0, **kw)
        self._offers_by_market = offers_by_market
        self._meta = ("2025-09-03", "San Diego Padres", "Baltimore Orioles", start)

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 97809, "home": "SD", "visitor": "BAL"}

    def _resolve_game_meta(self, game_pk):  # type: ignore[override]
        return self._meta

    def _resolve_player_name(self, player_id):  # type: ignore[override]
        return "bryce miller"

    def _mlb_get(self, path, params):  # type: ignore[override]
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        assert path == "offers", path
        return self._offers_by_market.get(int(params["market_id"]), {"offers": []})


def _every_payload(**kw: Any) -> _Synthetic:
    return _Synthetic({282: _APPENDIX_B, 122: _MONEYLINE, 285: _STRIKEOUTS}, **kw)


class _BatchRecorder:
    """Records each batch a loader hands its writer; returns the batch size."""

    def __init__(self) -> None:
        self.game_batches: list[tuple[int, list[dict]]] = []
        self.prop_batches: list[list[dict]] = []

    async def game(self, game_pk: int, rows: list[dict]) -> int:
        self.game_batches.append((game_pk, list(rows)))
        return len(rows)

    async def prop(self, rows: list[dict]) -> int:
        self.prop_batches.append(list(rows))
        return len(rows)


class _NotNullViolation(Exception):
    """The stand-in for asyncpg's NotNullViolationError."""


def _enforce_game_odds_hash(sql: str, rows: list[tuple]) -> None:
    """Refuse a raw.game_odds INSERT without a hash, as the live schema does.

    ``raw.game_odds.odds_hash`` is NOT NULL with no default since migration
    0012 (the checked-in DDL file says so too since 2026-09-28). Review finding 4:
    the opening-line job sent no hash, and a stand-in that enforced nothing hid
    it. The hash is the LAST bind value of every writer's INSERT.
    """
    if "INSERT INTO raw.game_odds" not in sql:
        return
    if "odds_hash" not in sql:
        raise _NotNullViolation("null value in column odds_hash of relation game_odds")
    for row in rows:
        if not (isinstance(row[-1], str) and len(row[-1]) == 64):
            raise _NotNullViolation("null value in column odds_hash of relation game_odds")


class _RecordingConn:
    """A stand-in asyncpg pool: records every execute / executemany (no DB).

    It enforces the one live constraint the writers can miss: a raw.game_odds
    INSERT must carry ``odds_hash`` (see :func:`_enforce_game_odds_hash`).
    """

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.batches: list[tuple[str, list[tuple]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        _enforce_game_odds_hash(sql, [args])
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def executemany(self, sql: str, params: Any) -> None:
        rows = [tuple(p) for p in params]
        _enforce_game_odds_hash(sql, rows)
        self.batches.append((sql, rows))

    async def fetch(self, sql: str, *args: Any) -> list:
        return []

    async def fetchrow(self, sql: str, *args: Any) -> None:
        return None


def _minutes_ago(minutes: float) -> datetime:
    """A cadence-clock stamp ``minutes`` before now (the clocks hold aware UTC)."""
    return datetime.now(UTC) - timedelta(minutes=minutes)


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


# ===========================================================================
# The loader (plan test 14a)
# ===========================================================================


class TestLoaderBatchesAndGuards:
    @pytest.mark.asyncio
    async def test_the_loader_batches_one_offer_and_counts_refusals_by_book(self) -> None:
        rec = _BatchRecorder()
        tally = RefusalTally()
        by_book: Counter[str] = Counter()
        written = await loader._load_game_odds(
            _every_payload(),
            rec.game,
            776465,
            line_types=("closing",),
            game_markets=("f1_runline",),
            tally=tally,
            written_by_book=by_book,
        )
        # One offer → one batch: the blend, FanDuel and theScore, each its own row.
        assert len(rec.game_batches) == 1
        game_pk, batch = rec.game_batches[0]
        assert game_pk == 776465
        assert sorted(r["book"] for r in batch) == ["bp:0", "bp:10", "bp:33"]
        assert written == 3
        # Hard Rock's "1 / 1" pair is refused and counted by rule, market and book.
        assert tally.counts() == {("equal_spreads_priced_like_a_pair", "f1_runline", "bp:49"): 1}
        assert tally.n_offered == 4 and tally.refused == 1
        assert by_book == Counter({"bp:0": 1, "bp:10": 1, "bp:33": 1})

    @pytest.mark.asyncio
    async def test_the_opening_row_is_its_own_batch(self) -> None:
        rec = _BatchRecorder()
        written = await loader._load_game_odds(
            _every_payload(),
            rec.game,
            776465,
            line_types=("opening", "closing"),
            game_markets=("f1_runline",),
        )
        assert written == 4
        assert [len(b) for _pk, b in rec.game_batches] == [1, 3]
        opening = rec.game_batches[0][1][0]
        assert (opening["book"], opening["line_type"]) == ("bp:10", "opening")

    @pytest.mark.asyncio
    async def test_a_late_closing_stamp_is_refused_and_counted_by_book(self, caplog) -> None:
        # The lines carry 20:11:43; a 19:30 start puts them 42 minutes after it.
        rec = _BatchRecorder()
        tally = RefusalTally()
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        written = await loader._load_game_odds(
            _every_payload(start=datetime(2025, 9, 3, 19, 30)),
            rec.game,
            776465,
            line_types=("closing",),
            game_markets=("f1_runline",),
            tally=tally,
        )
        assert written == 0 and rec.game_batches == []
        counts = tally.counts()
        for book in ("bp:0", "bp:10", "bp:33"):
            assert counts[("late_closing_stamp", "f1_runline", book)] == 1
        # The pair rule runs before the stamp rule.
        assert counts[("equal_spreads_priced_like_a_pair", "f1_runline", "bp:49")] == 1
        assert "refused game 776465 closing/f1_runline bp:10: stamped 42 minutes" in caplog.text

    @pytest.mark.asyncio
    async def test_the_book_filter_keeps_one_book(self) -> None:
        rec = _BatchRecorder()
        tally = RefusalTally()
        written = await loader._load_game_odds(
            _every_payload(),
            rec.game,
            776465,
            line_types=("closing",),
            game_markets=("f1_runline", "moneyline"),
            tally=tally,
            book_id=33,
        )
        # theScore quotes the run line and not the moneyline.
        assert written == 1
        assert [r["book"] for _pk, b in rec.game_batches for r in b] == ["bp:33"]
        # The other books' rows never reach the guard.
        assert tally.n_offered == 1 and tally.refused == 0

    @pytest.mark.asyncio
    async def test_a_prop_offer_is_one_batch_and_a_line_mismatch_is_refused(self) -> None:
        rec = _BatchRecorder()
        tally = RefusalTally()
        by_book: Counter[str] = Counter()
        written = await loader._load_prop_odds(
            _every_payload(),
            rec.prop,
            776465,
            [(682243, True)],
            prop_stats=("strikeouts",),
            line_types=("closing",),
            tally=tally,
            written_by_book=by_book,
        )
        assert written == 2
        assert len(rec.prop_batches) == 1
        assert sorted(r["book"] for r in rec.prop_batches[0]) == ["bp:10", "bp:12"]
        assert tally.counts() == {("total_line_mismatch", "strikeouts", "bp:19"): 1}
        assert by_book == Counter({"bp:12": 1, "bp:10": 1})

    @pytest.mark.asyncio
    async def test_a_failed_batch_is_logged_and_the_next_offer_still_runs(self, caplog) -> None:
        calls: list[int] = []

        async def flaky(game_pk: int, rows: list[dict]) -> int:
            calls.append(len(rows))
            if len(calls) == 1:
                raise RuntimeError("db hiccup")
            return len(rows)

        caplog.set_level(logging.WARNING, logger="load_historical_odds")
        by_book: Counter[str] = Counter()
        written = await loader._load_game_odds(
            _every_payload(),
            flaky,
            776465,
            line_types=("closing",),
            game_markets=("f1_runline", "moneyline"),
            written_by_book=by_book,
        )
        assert calls == [3, 3]
        assert written == 3  # only the moneyline batch counts
        assert by_book == Counter({"bp:12": 1, "bp:10": 1, "bp:0": 1})
        assert "game odds persist failed game 776465 closing/f1_runline (3 rows)" in caplog.text


class TestLoaderThroughTheRealWriters:
    @pytest.mark.asyncio
    async def test_book_line_at_reaches_the_insert_and_the_hash_stays_last(self) -> None:
        conn = _RecordingConn()
        persist_game, persist_prop = loader._build_persisters("postgresql://x", conn)
        await loader._load_game_odds(
            _every_payload(),
            persist_game,
            776465,
            line_types=("closing",),
            game_markets=("f1_runline",),
        )
        await loader._load_prop_odds(
            _every_payload(),
            persist_prop,
            776465,
            [(682243, True)],
            prop_stats=("strikeouts",),
            line_types=("closing",),
        )
        assert conn.executes == []  # every write is a batch
        (game_sql, game_params), (prop_sql, prop_params) = conn.batches
        assert "INSERT INTO raw.game_odds" in game_sql and "book_line_at" in game_sql
        assert "INSERT INTO raw.prop_odds" in prop_sql and "book_line_at" in prop_sql
        assert game_sql.index("book_line_at") < game_sql.index("odds_hash")
        assert len(game_params) == 3 and len(prop_params) == 2
        for params in game_params:
            assert len(params) == 19
            assert params[3].startswith("bp:")  # the book label
            assert params[-2] == _T_AT  # the stamp: aware UTC
            assert isinstance(params[-1], str) and len(params[-1]) == 64  # the hash, last
        stamps = {"bp:12": datetime(2025, 9, 3, 19, 50, tzinfo=UTC)}
        stamps["bp:10"] = datetime(2025, 9, 3, 19, 55, tzinfo=UTC)
        for params in prop_params:
            assert len(params) == 13
            assert params[-2] == stamps[params[8]]  # each book's own stamp
            assert isinstance(params[-1], str) and len(params[-1]) == 64


class TestLoaderBookFlagAndResume:
    def test_select_book(self) -> None:
        assert loader._select_book(None) is None
        assert loader._select_book("  ") is None
        assert loader._select_book("draftkings") == 12
        assert loader._select_book("DraftKings") == 12
        assert loader._select_book("bp:12") == 12
        assert loader._select_book("Hard Rock") == 49
        with pytest.raises(ValueError, match="Unknown book"):
            loader._select_book("pinnacle-ish")
        with pytest.raises(ValueError, match="Unknown book"):
            loader._select_book("consensus")

    def test_parse_args_accepts_a_book_and_rejects_an_unknown_one(self, capsys) -> None:
        args = loader.parse_args(["--seasons", "2024", "--book", "FanDuel"])
        assert args.book == "FanDuel"
        assert loader.parse_args(["--seasons", "2024"]).book is None
        with pytest.raises(SystemExit) as exc:
            loader.parse_args(["--seasons", "2024", "--book", "nobook"])
        assert exc.value.code == 2
        assert "Unknown book" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_the_resume_clause_counts_only_one_row_per_book_rows(self, monkeypatch) -> None:
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
        since = loader.parse_skip_loaded_since("2026-09-28T10:00:00+00:00")
        await loader._fetch_final_games("dsn", [2024], None)
        assert "NOT EXISTS" not in seen[-1][0] and seen[-1][1] == ()
        await loader._fetch_final_games("dsn", [2024], None, skip_loaded_since=since)
        sql = seen[-1][0]
        assert "FROM raw.prop_odds p" in sql
        assert "AND p.book LIKE 'bp:%'" in sql
        # Review finding 2: only the run's own line types count (never the live
        # cycle's 'current' rows), and only a load of two or more books.
        assert "AND p.line_type = ANY($2::varchar[])" in sql
        assert "HAVING COUNT(DISTINCT p.book) >= 2" in sql
        assert seen[-1][1] == (since, ["opening", "closing"])
        # A --book run counts only that book's rows, at the run's line types.
        await loader._fetch_final_games(
            "dsn", [2024], None, skip_loaded_since=since, book_id=12, line_types=("closing",)
        )
        sql = seen[-1][0]
        assert "AND p.book = $3" in sql and "LIKE" not in sql and "HAVING" not in sql
        assert seen[-1][1] == (since, ["closing"], "bp:12")
        # Review finding 3: a --no-props run's resume reads the game rows.
        await loader._fetch_final_games(
            "dsn", [2024], None, skip_loaded_since=since, resume_on_game_odds=True
        )
        assert "FROM raw.game_odds p" in seen[-1][0] and "raw.prop_odds" not in seen[-1][0]

    def test_the_resume_clause_refuses_an_unknown_table(self) -> None:
        with pytest.raises(ValueError, match="resume table"):
            loader._resume_clause(table="raw.games; DROP TABLE x", book_id=None)


class TestLoaderResumeRunsTheQuery:
    """The resume query run on an in-memory DuckDB (review findings 2 and 3).

    The loader's SQL is plain enough for both Postgres and DuckDB (the same
    text ran on the live Postgres as a read-only PREPARE / EXECUTE when the
    fix landed). Each game below is one way a game can look after a run died.
    """

    _SINCE = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    _AFTER = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    _BEFORE = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)

    @pytest.fixture
    def duck(self, monkeypatch):
        import sys
        import types

        import duckdb

        con = duckdb.connect()
        con.execute("CREATE SCHEMA raw")
        con.execute(
            "CREATE TABLE raw.games (game_pk INTEGER, status VARCHAR, season INTEGER, "
            "home_score_final INTEGER, away_score_final INTEGER)"
        )
        for table in ("prop_odds", "game_odds"):
            con.execute(
                f"CREATE TABLE raw.{table} (game_pk INTEGER, fetched_at TIMESTAMPTZ, "
                "line_type VARCHAR, book VARCHAR)"
            )
        for pk in range(1, 7):
            con.execute("INSERT INTO raw.games VALUES (?, 'Final', 2026, 3, 2)", [pk])
        after, before = self._AFTER, self._BEFORE
        props = [
            # 1: a finished every-book load.
            (1, after, "closing", "bp:12"),
            (1, after, "closing", "bp:0"),
            (1, after, "opening", "bp:10"),
            # 2: the live cycle's rows only (two books, 'current').
            (2, after, "current", "bp:12"),
            (2, after, "current", "bp:10"),
            # 3: a --book draftkings smoke's rows only.
            (3, after, "opening", "bp:12"),
            (3, after, "closing", "bp:12"),
            # 4: the old consensus rows only.
            (4, after, "closing", "consensus"),
            (4, after, "opening", "consensus"),
            # 5: a finished load from before the resume instant.
            (5, before, "closing", "bp:12"),
            (5, before, "closing", "bp:0"),
        ]
        con.executemany("INSERT INTO raw.prop_odds VALUES (?, ?, ?, ?)", props)
        # 6: the game rows of a finished --no-props run, and no prop rows.
        con.executemany(
            "INSERT INTO raw.game_odds VALUES (?, ?, ?, ?)",
            [(6, after, "closing", "bp:12"), (6, after, "closing", "bp:0")],
        )

        class _Conn:
            async def fetch(self, sql, *params):
                cur = con.execute(sql, list(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

            async def close(self):
                return None

        async def _connect(dsn):
            return _Conn()

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))
        return con

    async def _pks(self, **kw: Any) -> list[int]:
        games = await loader._fetch_final_games(
            "dsn", [2026], None, skip_loaded_since=self._SINCE, **kw
        )
        return [g["game_pk"] for g in games]

    @pytest.mark.asyncio
    async def test_an_every_book_resume_skips_only_a_finished_load(self, duck) -> None:
        # Finding 2: the live 'current' rows (2) and a one-book smoke (3) no
        # longer mark a game as loaded; the old consensus rows (4), rows older
        # than the instant (5) and game rows only (6) never did.
        assert await self._pks() == [2, 3, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_a_book_resume_skips_the_games_with_that_books_rows(self, duck) -> None:
        # Game 2's DraftKings row is a live 'current' row: it does not count.
        assert await self._pks(book_id=12) == [2, 4, 5, 6]
        # Game 1's FanDuel row is an opening row: it counts only when the run
        # loads opening lines.
        assert await self._pks(book_id=10) == [2, 3, 4, 5, 6]
        assert await self._pks(book_id=10, line_types=("closing",)) == [1, 2, 3, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_a_no_props_resume_reads_the_game_rows(self, duck) -> None:
        # Finding 3: a --no-props run writes no prop rows, so the prop table
        # could never mark one of its games as loaded.
        assert await self._pks(resume_on_game_odds=True) == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_run_passes_its_line_types_and_the_no_props_switch(self, monkeypatch) -> None:
        seen: list[dict[str, Any]] = []

        async def _games(dsn, seasons, max_games, **kw):
            seen.append(kw)
            return []

        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
        monkeypatch.delenv("ODDS_PROVIDER", raising=False)
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        base = [
            "--seasons",
            "2019",
            "--skip-loaded-since",
            "2026-09-28T10:00:00+00:00",
            "--provider",
            "mock",
        ]
        assert await loader.run(loader.parse_args([*base, "--no-props"])) == 0
        assert seen[-1]["resume_on_game_odds"] is True
        assert seen[-1]["line_types"] == ("opening", "closing")
        assert seen[-1]["skip_loaded_since"] == self._SINCE
        assert await loader.run(loader.parse_args([*base, "--line-types", "closing"])) == 0
        assert seen[-1]["resume_on_game_odds"] is False
        assert seen[-1]["line_types"] == ("closing",)


class TestLoaderRefusesTheImplicitMock:
    """Review fix (RB-1): the app container leaves ODDS_PROVIDER unset, and the
    registry's default is the mock. A run book step with no ``--provider``
    wrote about 2,472 games of made-up ``consensus`` prices into a real season,
    with nothing to warn the operator."""

    def test_the_provider_selection(self, monkeypatch) -> None:
        monkeypatch.delenv("ODDS_PROVIDER", raising=False)
        with pytest.raises(ValueError, match="--provider bettingpros"):
            loader._select_provider(None)
        assert loader._select_provider("mock") == "mock"
        assert loader._select_provider(" BettingPros ") == "bettingpros"
        monkeypatch.setenv("ODDS_PROVIDER", "mock")
        with pytest.raises(ValueError, match="resolves to the mock"):
            loader._select_provider(None)
        assert loader._select_provider("mock") == "mock"
        monkeypatch.setenv("ODDS_PROVIDER", "bettingpros")
        assert loader._select_provider(None) == "bettingpros"

    @pytest.mark.asyncio
    async def test_run_stops_before_any_work(self, monkeypatch, caplog) -> None:
        touched: list[str] = []

        async def _games(*a: Any, **kw: Any) -> list:
            touched.append("games")
            return []

        monkeypatch.delenv("ODDS_PROVIDER", raising=False)
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        caplog.set_level(logging.ERROR, logger="load_historical_odds")
        assert await loader.run(loader.parse_args(["--seasons", "2024"])) == 2
        assert touched == []
        assert "--provider bettingpros" in caplog.text
        # Named on the command line, the mock runs (the no-network smoke).
        args = loader.parse_args(["--seasons", "2024", "--provider", "mock"])
        assert await loader.run(args) == 0
        assert touched == ["games"]


class TestLoaderRunSummary:
    def test_rows_by_book_summary_names_each_book(self) -> None:
        text = loader._written_by_book_summary(Counter({"bp:10": 5, "bp:0": 7, "consensus": 1}))
        lines = text.splitlines()
        assert lines[0] == "rows written by book:"
        assert lines[1] == "  bp:0 (BettingPros Consensus): 7"
        assert lines[2] == "  bp:10 (FanDuel): 5"
        assert lines[3] == "  consensus: 1"
        assert loader._written_by_book_summary(Counter()) == "rows written by book: none"

    @pytest.mark.asyncio
    async def test_run_logs_the_guard_summary_and_the_rows_by_book(
        self, monkeypatch, caplog
    ) -> None:
        register_odds_provider("sim555_writers_synthetic", _every_payload)
        rec = _BatchRecorder()

        async def _games(dsn, seasons, max_games, **kw):
            return [{"game_pk": 776465}]

        class _Pool:
            async def close(self) -> None:
                return None

        async def _create_pool(*args, **kwargs):
            return _Pool()

        async def _players(pool, game_pk):
            return [(682243, True)]

        import sys
        import types

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=_create_pool))
        # run() sets the offline cache time-to-live only when it is unset; pin
        # it here so monkeypatch restores the environment afterwards.
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        monkeypatch.setattr(loader, "_fetch_lineup_players", _players)
        monkeypatch.setattr(loader, "_build_persisters", lambda dsn, pool: (rec.game, rec.prop))
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        args = loader.parse_args(
            [
                "--seasons",
                "2025",
                "--provider",
                "sim555_writers_synthetic",
                "--game-markets",
                "f1_runline",
                "--prop-stats",
                "strikeouts",
                "--line-types",
                "closing",
            ]
        )
        assert await loader.run(args) == 0
        assert [len(b) for _pk, b in rec.game_batches] == [3]
        assert [len(b) for b in rec.prop_batches] == [2]
        text = caplog.text
        assert "guard refusals: 2 of 7 rows offered" in text
        assert "equal_spreads_priced_like_a_pair: 1" in text
        assert "total_line_mismatch: 1" in text
        assert "rows written by book:" in text
        assert "bp:10 (FanDuel): 2" in text
        # 2 of 7 is above the 5% limit: the run warns.
        assert "the odds guard refused 2 of 7 rows" in text


class TestLoaderDoneFile:
    """SIM-555: the crash-safe resume. The host blue-screens often (2026-09-29:
    twice in 46 minutes), so a restarted season load must skip exactly the games
    it finished and load again the one a crash cut off."""

    def test_read_and_append_round_trip(self, tmp_path) -> None:
        path = str(tmp_path / "load.done")
        assert loader.read_done_file(path) == set()  # a missing file is empty
        assert loader.read_done_file(None) == set()
        loader.append_done(path, 746001)
        loader.append_done(path, 746002)
        # A line a crash cut short, a blank line and junk are ignored.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("7460\n\nnot-a-game\n74")
        assert loader.read_done_file(path) == {746001, 746002, 7460, 74}
        assert open(path, encoding="utf-8").read().startswith("746001\n746002\n")

    def test_parse_args_takes_the_done_file(self) -> None:
        args = loader.parse_args(["--seasons", "2024", "--done-file", "scripts/x.done"])
        assert args.done_file == "scripts/x.done"
        assert loader.parse_args(["--seasons", "2024"]).done_file is None

    @pytest.mark.asyncio
    async def test_run_skips_the_listed_games_and_lists_each_finished_game(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        register_odds_provider("sim555_writers_synthetic", _every_payload)
        rec = _BatchRecorder()
        done = tmp_path / "load.done"
        done.write_text("776466\n", encoding="utf-8")
        loaded: list[int] = []

        async def _games(dsn, seasons, max_games, **kw):
            return [{"game_pk": 776465}, {"game_pk": 776466}, {"game_pk": 776467}]

        class _Pool:
            async def close(self) -> None:
                return None

        async def _create_pool(*args, **kwargs):
            return _Pool()

        async def _players(pool, game_pk):
            loaded.append(game_pk)
            return []

        import sys
        import types

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=_create_pool))
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        monkeypatch.setattr(loader, "_fetch_lineup_players", _players)
        monkeypatch.setattr(loader, "_build_persisters", lambda dsn, pool: (rec.game, rec.prop))
        caplog.set_level(logging.INFO, logger="load_historical_odds")
        args = loader.parse_args(
            [
                "--seasons",
                "2025",
                "--provider",
                "sim555_writers_synthetic",
                "--game-markets",
                "moneyline",
                "--line-types",
                "closing",
                "--done-file",
                str(done),
            ]
        )
        assert await loader.run(args) == 0
        # The listed game is skipped; the other two load, in order.
        assert loaded == [776465, 776467]
        assert done.read_text(encoding="utf-8").split() == ["776466", "776465", "776467"]
        assert "1 games already finished, 1 skipped here" in caplog.text

    @pytest.mark.asyncio
    async def test_a_game_cut_off_is_not_listed(self, monkeypatch, tmp_path) -> None:
        """A crash inside a game (here: an error while its props load) leaves the
        game off the list, so the next run loads it again."""
        register_odds_provider("sim555_writers_synthetic", _every_payload)
        rec = _BatchRecorder()
        done = tmp_path / "load.done"

        async def _games(dsn, seasons, max_games, **kw):
            return [{"game_pk": 776465}, {"game_pk": 776466}]

        class _Pool:
            async def close(self) -> None:
                return None

        async def _create_pool(*args, **kwargs):
            return _Pool()

        async def _players(pool, game_pk):
            if game_pk == 776466:
                raise RuntimeError("the host went down")
            return []

        import sys
        import types

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=_create_pool))
        monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        monkeypatch.setattr(loader, "_fetch_lineup_players", _players)
        monkeypatch.setattr(loader, "_build_persisters", lambda dsn, pool: (rec.game, rec.prop))
        args = loader.parse_args(
            [
                "--seasons",
                "2025",
                "--provider",
                "sim555_writers_synthetic",
                "--game-markets",
                "moneyline",
                "--line-types",
                "closing",
                "--done-file",
                str(done),
            ]
        )
        with pytest.raises(RuntimeError):
            await loader.run(args)
        assert loader.read_done_file(str(done)) == {776465}


# ===========================================================================
# The live pipeline (plan test 14b)
# ===========================================================================


class TestLiveCyclePersistsEveryBook:
    @pytest.mark.asyncio
    async def test_the_live_cycle_persists_every_book(self, caplog) -> None:
        p = _bare_pipeline(_odds=_every_payload())
        caplog.set_level(logging.INFO, logger="live_ingestion")
        written = await p._persist_game_odds_cycle(776465)
        # The moneyline (three books) and the first-inning run line (three kept).
        assert written == 6
        p._db.execute.assert_not_awaited()
        batches = {call.args[1][0][5]: call.args[1] for call in p._db.executemany.await_args_list}
        assert set(batches) == {"moneyline", "f1_runline"}
        assert sorted(params[3] for params in batches["moneyline"]) == ["bp:0", "bp:10", "bp:12"]
        assert sorted(params[3] for params in batches["f1_runline"]) == ["bp:0", "bp:10", "bp:33"]
        assert {params[4] for b in batches.values() for params in b} == {"current"}
        # The guard sits in front: Hard Rock's pair is refused, logged and counted.
        assert p._refusal_tally().counts() == {
            ("equal_spreads_priced_like_a_pair", "f1_runline", "bp:49"): 1
        }
        assert "refused game 776465 current/f1_runline bp:49" in caplog.text

    @pytest.mark.asyncio
    async def test_the_live_prop_cycle_persists_every_book(self) -> None:
        p = _bare_pipeline(_odds=_every_payload())
        state = {"current_pitcher_id": 682243, "home_lineup": [], "away_lineup": []}
        written = await p._persist_prop_odds_cycle(776465, state)
        assert written == 2
        (call,) = p._db.executemany.await_args_list
        assert sorted(params[8] for params in call.args[1]) == ["bp:10", "bp:12"]
        assert p._refusal_tally().counts() == {("total_line_mismatch", "strikeouts", "bp:19"): 1}

    def test_the_tally_is_created_lazily_for_a_new_built_pipeline(self) -> None:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        tally = p._refusal_tally()
        assert isinstance(tally, RefusalTally)
        assert p._refusal_tally() is tally

    def test_the_constructor_builds_the_tally_and_the_game_odds_clock(self) -> None:
        p = LiveIngestionPipeline(dsn="postgresql://x", redis_url="redis://x:6379")
        assert isinstance(p._odds_refusals, RefusalTally)
        assert p._last_game_odds_fetch == {}
        assert not hasattr(p, "_last_segment_fetch")

    @pytest.mark.asyncio
    async def test_an_empty_row_never_reaches_the_guard_or_the_database(self) -> None:
        class _Empty(MockOddsAPI):
            def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline"):
                row = MockOddsAPI.get_odds(game_pk, line_type=line_type, market_type=market_type)
                return [{**row, **dict.fromkeys(live.GAME_ODDS_FIELDS)}]

        p = _bare_pipeline(_odds=_Empty())
        assert await p._persist_game_odds_cycle(1) == 0
        p._db.executemany.assert_not_awaited()
        assert p._refusal_tally().n_offered == 0


class TestPregameCycle:
    """Review finding 1: the day's games get every book's line before first pitch.

    The live cycle used to run only inside the live refresh, so a game in the
    ``Preview`` state stored no ``current`` row until it started.
    """

    def test_the_roles_come_from_the_hydrated_schedule(self) -> None:
        game = {
            "teams": {
                "home": {"probablePitcher": {"id": 682243}},
                "away": {"probablePitcher": {"id": 605400}},
            },
            "lineups": {
                "homePlayers": [
                    {"id": 1, "primaryPosition": {"abbreviation": "DH"}},
                    {"id": 2, "primaryPosition": {"abbreviation": "twp"}},  # a two-way player
                    {"id": 3},  # no position: the safe default
                    {"id": 682243, "primaryPosition": {"abbreviation": "P"}},  # the starter bats
                ],
                "awayPlayers": [{"id": 4, "primaryPosition": {"abbreviation": "SS"}}, {"id": None}],
            },
        }
        roles = LiveIngestionPipeline._pregame_prop_player_roles(game)
        assert roles == {
            682243: live.PROP_ROLE_BOTH,
            605400: live.PROP_ROLE_PITCHER,
            1: live.PROP_ROLE_BATTER,
            2: live.PROP_ROLE_BOTH,
            3: live.PROP_ROLE_BOTH,
            4: live.PROP_ROLE_BATTER,
        }
        assert list(roles) == [682243, 605400, 1, 2, 3, 4]

    def test_before_the_lineups_post_only_the_probable_pitchers_are_asked(self) -> None:
        game = {"teams": {"home": {"probablePitcher": {"id": 7}}, "away": {}}}
        assert LiveIngestionPipeline._pregame_prop_player_roles(game) == {7: live.PROP_ROLE_PITCHER}
        assert LiveIngestionPipeline._pregame_prop_player_roles({}) == {}
        assert LiveIngestionPipeline._pregame_prop_player_roles({"lineups": None}) == {}

    @pytest.mark.asyncio
    async def test_the_pregame_cycle_stores_every_book_before_first_pitch(self) -> None:
        p = _bare_pipeline(_odds=_every_payload())
        game = {"teams": {"home": {"probablePitcher": {"id": 682243}}}}
        # The moneyline and the first-inning run line (three books each), and
        # the probable pitcher's strikeouts (two books).
        assert await p._persist_pregame_odds(776465, game) == 8
        batches = [call.args for call in p._db.executemany.await_args_list]
        game_rows = [row for sql, rows in batches if "raw.game_odds" in sql for row in rows]
        prop_rows = [row for sql, rows in batches if "raw.prop_odds" in sql for row in rows]
        assert sorted(r[3] for r in game_rows) == [
            "bp:0",
            "bp:0",
            "bp:10",
            "bp:10",
            "bp:12",
            "bp:33",
        ]
        assert {r[4] for r in game_rows} == {"current"}
        assert sorted(r[8] for r in prop_rows) == ["bp:10", "bp:12"]
        assert {(r[1], r[9]) for r in prop_rows} == {(682243, "current")}
        # The three clocks are set: a second poll inside the cadence reads nothing.
        p._db.executemany.reset_mock()
        assert await p._persist_pregame_odds(776465, game) == 0
        # The live refresh shares the two cycle clocks with the pre-game cycle.
        state = {"current_pitcher_id": 682243, "home_lineup": [], "away_lineup": []}
        assert await p._persist_prop_odds_cycle(776465, state) == 0
        assert await p._persist_game_odds_cycle(776465) == 0
        p._db.executemany.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_known_player_leaves_the_prop_clock_open(self) -> None:
        p = _bare_pipeline(_odds=_every_payload())
        assert await p._persist_pregame_odds(776465, {}) == 6  # the game markets only
        assert 776465 not in p._last_prop_fetch
        # The probable pitcher is named. Inside the pre-game cadence the next
        # poll still reads nothing.
        game = {"teams": {"away": {"probablePitcher": {"id": 682243}}}}
        assert await p._persist_pregame_odds(776465, game) == 0
        # Ten minutes on, the pass stores his props at once. The game markets
        # add nothing: only the pre-game clock is moved back here, so their
        # own one-minute clock is still closed.
        p._last_pregame_fetch[776465] = _minutes_ago(live.PREGAME_ODDS_CADENCE_S / 60 + 1)
        assert await p._persist_pregame_odds(776465, game) == 2
        # The live refresh can also store them at once: the prop clock stayed open.
        p2 = _bare_pipeline(_odds=_every_payload())
        assert await p2._persist_pregame_odds(776465, {}) == 6
        state = {"current_pitcher_id": 682243, "home_lineup": [], "away_lineup": []}
        assert await p2._persist_prop_odds_cycle(776465, state) == 2

    @pytest.mark.asyncio
    async def test_the_pregame_cadence_is_ten_minutes_per_game(self) -> None:
        """Review item: the 30-second schedule poll started the pre-game cycle
        for every game; only the cycles' 60-second clocks throttled it, so a
        game read the vendor every minute all day. The pre-game pass now has
        its own ten-minute clock per game, checked before either cycle."""
        assert live.PREGAME_ODDS_CADENCE_S == 600
        assert live.PREGAME_ODDS_CADENCE_S > live.PROP_FETCH_CADENCE_S

        class _Counting(MockOddsAPI):
            reads = 0

            def get_odds_by_book(self, *args: Any, **kwargs: Any) -> list[dict]:
                type(self).reads += 1
                return super().get_odds_by_book(*args, **kwargs)

            def get_prop_odds_by_book(self, *args: Any, **kwargs: Any) -> list[dict]:
                type(self).reads += 1
                return super().get_prop_odds_by_book(*args, **kwargs)

        p = _bare_pipeline(_odds=_Counting())
        game = {"teams": {"home": {"probablePitcher": {"id": 7}}}}
        assert await p._persist_pregame_odds(1, game) > 0
        first_pass = _Counting.reads
        assert first_pass > 0
        # Two minutes on (past the cycles' 60-second clocks, inside the
        # pre-game cadence): no cycle runs, so the vendor is not read.
        p._last_pregame_fetch[1] = _minutes_ago(2)
        p._last_game_odds_fetch[1] = _minutes_ago(2)
        p._last_prop_fetch[1] = _minutes_ago(2)
        assert await p._persist_pregame_odds(1, game) == 0
        assert _Counting.reads == first_pass
        # Each game keeps its own clock: another game is read at once.
        assert await p._persist_pregame_odds(2, game) > 0
        assert _Counting.reads == 2 * first_pass
        # Past the cadence the pass runs again.
        p._last_pregame_fetch[1] = _minutes_ago(11)
        assert await p._persist_pregame_odds(1, game) > 0
        assert _Counting.reads == 3 * first_pass

    @pytest.mark.asyncio
    async def test_the_pregame_clock_is_created_lazily(self) -> None:
        p = _bare_pipeline(_odds=MockOddsAPI())
        assert not hasattr(p, "_last_pregame_fetch")
        await p._persist_pregame_odds(1, {})
        assert set(p._last_pregame_fetch) == {1}

    def test_the_constructor_builds_the_pregame_clock(self) -> None:
        p = LiveIngestionPipeline(dsn="postgresql://x", redis_url="redis://x:6379")
        assert p._last_pregame_fetch == {}

    @pytest.mark.asyncio
    async def test_the_live_cycle_keeps_its_one_minute_cadence(self) -> None:
        """The in-play refresh does not read the pre-game clock: a game that
        has just started refreshes its lines on the 60-second cadence."""
        p = _bare_pipeline(_odds=_every_payload())
        assert await p._persist_pregame_odds(776465, {}) == 6
        # The game starts two minutes later: the live cycle reads again.
        p._last_game_odds_fetch[776465] = _minutes_ago(2)
        assert p._last_pregame_fetch[776465] > _minutes_ago(3)
        assert await p._persist_game_odds_cycle(776465) == 6

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_not_raised(self, caplog) -> None:
        class _Broken(MockOddsAPI):
            def get_prop_odds_by_book(self, *args, **kwargs):
                raise RuntimeError("vendor down")

        p = _bare_pipeline(_odds=_Broken())
        caplog.set_level(logging.WARNING, logger="live_ingestion")
        game = {"teams": {"home": {"probablePitcher": {"id": 7}}}}
        # The game markets were stored before the prop read failed.
        assert await p._persist_pregame_odds(1, game) == len(GAME_MARKET_TYPES)
        assert "pre-game odds cycle failed for game 1: vendor down" in caplog.text

    @pytest.mark.asyncio
    async def test_the_game_row_is_upserted_before_the_first_odds_insert(self) -> None:
        """Review fix (runtime #3): ``raw.game_odds.game_pk`` references
        ``raw.games``. The schedule poll starts the pre-game task before its own
        upsert task, so on a game's first poll the odds INSERT reached Postgres
        before the game row, failed the foreign key, and the ten-minute clock
        kept the moneyline from a retry. The pass now upserts the game first."""
        p = _bare_pipeline(_odds=_every_payload())
        order: list[str] = []

        async def _upsert(game: dict) -> None:
            assert game["gamePk"] == 776465
            order.append("upsert")

        async def _many(sql: str, rows: list) -> None:
            order.append("odds")

        p._upsert_game_record = _upsert
        p._db.executemany = AsyncMock(side_effect=_many)
        game = {
            "gamePk": 776465,
            "gameDate": "2025-09-03T20:10:00Z",
            "status": {"abstractGameState": "Preview"},
        }
        assert await p._persist_pregame_odds(776465, game) == 6
        assert order[0] == "upsert" and order.count("upsert") == 1
        assert order.count("odds") == 2  # the moneyline and the first-inning run line
        # Inside the cadence the pass does nothing: no upsert, no odds.
        order.clear()
        assert await p._persist_pregame_odds(776465, game) == 0
        assert order == []

    @pytest.mark.asyncio
    async def test_a_failed_game_row_upsert_does_not_stop_the_odds(self, caplog) -> None:
        p = _bare_pipeline(_odds=_every_payload())
        p._upsert_game_record = AsyncMock(side_effect=RuntimeError("db hiccup"))
        caplog.set_level(logging.WARNING, logger="live_ingestion")
        game = {"gamePk": 776465, "gameDate": "2025-09-03T20:10:00Z"}
        assert await p._persist_pregame_odds(776465, game) == 6
        assert "pre-game game-row upsert failed for game 776465: db hiccup" in caplog.text
        # A schedule entry without its game id is not upserted (a thin test payload).
        p2 = _bare_pipeline(_odds=_every_payload())
        p2._upsert_game_record = AsyncMock()
        assert await p2._persist_pregame_odds(776465, {}) == 6
        p2._upsert_game_record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_real_upsert_reaches_the_database_before_the_odds(self) -> None:
        """The same order through the real ``_upsert_game_record`` SQL."""
        p = _bare_pipeline(_odds=_every_payload())
        calls: list[str] = []
        p._db.execute = AsyncMock(side_effect=lambda sql, *a: calls.append(sql))
        p._db.executemany = AsyncMock(side_effect=lambda sql, rows: calls.append(sql))
        game = {
            "gamePk": 776465,
            "gameDate": "2025-09-03T20:10:00Z",
            "season": "2025",
            "status": {"abstractGameState": "Preview"},
            "teams": {"home": {"team": {"id": 135}}, "away": {"team": {"id": 110}}},
        }
        await p._persist_pregame_odds(776465, game)
        assert "INSERT INTO raw.games" in calls[0]
        assert any("raw.game_odds" in sql for sql in calls[1:])

    @pytest.mark.asyncio
    async def test_the_schedule_poll_starts_the_pregame_cycle(self) -> None:
        preview = {
            "gamePk": 11,
            "status": {"abstractGameState": "Preview"},
            "teams": {"home": {"probablePitcher": {"id": 7}}},
        }
        final = {"gamePk": 12, "status": {"abstractGameState": "Final"}}
        schedule = {"dates": [{"games": [preview, final]}]}
        seen_params: list[dict] = []

        class _Resp:
            async def json(self) -> dict:
                return schedule

            async def __aenter__(self) -> _Resp:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        class _Http:
            def get(self, url: str, params: dict | None = None) -> _Resp:
                seen_params.append(dict(params or {}))
                return _Resp()

        p = _bare_pipeline(_http=_Http())
        p._upsert_game_record = AsyncMock()
        p._persist_pregame_odds = AsyncMock(return_value=0)
        await p._sync_live_games()
        for _ in range(3):  # let the created tasks run
            await asyncio.sleep(0)
        assert seen_params[0]["hydrate"] == live.SCHEDULE_HYDRATE == "probablePitcher,lineups"
        p._persist_pregame_odds.assert_awaited_once_with(11, preview)
        assert p._upsert_game_record.await_count == 2


class TestRefreshLoop:
    @pytest.mark.asyncio
    async def test_the_per_pitch_row_is_broadcast_json_safe_and_not_persisted(
        self, mock_db_pool, mock_redis, sample_feed, monkeypatch
    ) -> None:
        sent: list[tuple[int, dict]] = []

        async def _capture(game_pk, payload):
            json.dumps(payload)  # the wire format: a bare dumps must succeed
            sent.append((game_pk, payload))

        monkeypatch.setattr(live.connection_manager, "broadcast", _capture)
        p = _bare_pipeline(_db=mock_db_pool, _redis=mock_redis, _odds=_every_payload())
        p._persist_odds = AsyncMock()

        async def _fetch_feed(_pk):
            return sample_feed

        p._fetch_feed = _fetch_feed
        await p._refresh_game_state(sample_feed["gamePk"])

        assert sent, "the refresh loop did not broadcast"
        odds = sent[-1][1]["odds"]
        assert odds["book"] == "bp:12"  # the graded book's moneyline
        assert odds["book_line_at"] == "2025-09-03T20:05:00+00:00"
        assert "scheduled_start" not in odds and "game_date" not in odds
        # SIM-555: the per-pitch row is not persisted; the cycle writes batches.
        p._persist_odds.assert_not_awaited()
        assert mock_db_pool.executemany.await_count >= 1


class TestPersistMany:
    @pytest.mark.asyncio
    async def test_an_empty_batch_sends_nothing(self) -> None:
        p = _bare_pipeline()
        assert await p._persist_odds_many(1, []) == 0
        assert await p._persist_prop_odds_many([]) == 0
        p._db.executemany.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_executemany_per_call_with_the_single_row_sql(self) -> None:
        p = _bare_pipeline()
        rows = [MockOddsAPI.get_odds(9, market_type="f5_total", book=b) for b in ("a", "b")]
        assert await p._persist_odds_many(9, rows) == 2
        (call,) = p._db.executemany.await_args_list
        sql, params = call.args
        assert sql == live._GAME_ODDS_INSERT_SQL
        assert [t[3] for t in params] == ["a", "b"]
        assert [t[-1] for t in params] == [LiveIngestionPipeline._odds_hash(r) for r in rows]
        # The one-row writer sends the same binds.
        await p._persist_odds(9, rows[0])
        assert p._db.execute.await_args.args == (sql, *params[0])

        props = [MockOddsAPI.get_prop_odds(9, 7, "walks", book=b) for b in ("a", "b")]
        assert await p._persist_prop_odds_many(props) == 2
        sql, params = p._db.executemany.await_args.args
        assert sql == live._PROP_ODDS_INSERT_SQL
        assert [t[-1] for t in params] == [LiveIngestionPipeline._prop_odds_hash(r) for r in props]

    def test_the_module_binders_are_the_pipelines(self) -> None:
        p = _bare_pipeline()
        row = {
            **MockOddsAPI.get_odds(9, market_type="moneyline", book="bp:12"),
            "book_line_at": _T_AT,
        }
        assert live.game_odds_insert_args(9, row) == p._odds_params(9, row)
        prop = MockOddsAPI.get_prop_odds(9, 7, "walks", book="bp:12")
        assert live.prop_odds_insert_args(prop) == p._prop_odds_params(prop)

    @pytest.mark.asyncio
    async def test_the_module_writers_send_one_batch(self) -> None:
        conn = _RecordingConn()
        rows = [MockOddsAPI.get_odds(9, book=b) for b in ("bp:10", "bp:12")]
        assert await live.insert_game_odds_rows(conn, 9, rows) == 2
        assert await live.insert_game_odds_rows(conn, 9, []) == 0
        props = [MockOddsAPI.get_prop_odds(9, 7, "walks", book="bp:12")]
        assert await live.insert_prop_odds_rows(conn, props) == 1
        assert await live.insert_prop_odds_rows(conn, []) == 0
        assert [(sql, len(params)) for sql, params in conn.batches] == [
            (live._GAME_ODDS_INSERT_SQL, 2),
            (live._PROP_ODDS_INSERT_SQL, 1),
        ]

    def test_the_stamp_stays_out_of_the_hash(self) -> None:
        row = MockOddsAPI.get_odds(9, market_type="moneyline", book="bp:12")
        stamped = {**row, "book_line_at": _T_AT}
        assert LiveIngestionPipeline._odds_hash(row) == LiveIngestionPipeline._odds_hash(stamped)
        other_book = {**row, "book": "bp:10"}
        assert LiveIngestionPipeline._odds_hash(row) != LiveIngestionPipeline._odds_hash(other_book)
        prop = MockOddsAPI.get_prop_odds(9, 7, "walks", book="bp:12")
        assert LiveIngestionPipeline._prop_odds_hash(prop) == LiveIngestionPipeline._prop_odds_hash(
            {**prop, "book_line_at": _T_AT}
        )

    def test_stamp_param(self) -> None:
        assert _stamp_param(None) is None
        assert _stamp_param(_T_AT) is _T_AT
        assert _stamp_param(datetime(2025, 9, 3, 20, 11, 43)) == _T_AT  # naive = UTC
        assert _stamp_param("2025-09-03T20:11:43") == _T_AT
        assert _stamp_param("2025-09-03T20:11:43+00:00") == _T_AT
        assert _stamp_param("not a time") is None
        assert _stamp_param(12345) is None


class TestJsonSafety:
    def test_a_bettingpros_row_needs_the_conversion(self) -> None:
        row = _every_payload().get_odds_by_book(776465, line_type="closing")[0]
        assert isinstance(row["book_line_at"], datetime)
        assert isinstance(row["scheduled_start"], datetime)
        with pytest.raises(TypeError):
            json.dumps(row)
        safe = _jsonable_odds(row)
        decoded = json.loads(json.dumps(safe))
        assert decoded["book_line_at"] == "2025-09-03T20:05:00+00:00"
        assert "scheduled_start" not in decoded and "game_date" not in decoded
        assert decoded["home_ml"] == row["home_ml"]
        assert _jsonable_odds(None) == {}

    @pytest.mark.asyncio
    async def test_the_broadcast_survives_a_datetime_in_the_payload(self) -> None:
        cm = ConnectionManager()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        cm._subscriptions[776465] = {ws}
        row = _every_payload().get_prop_odds_by_book(776465, 682243, "strikeouts")[0]
        await cm.broadcast(776465, {"type": "game_state_update", "odds": row})
        (message,) = ws.send_text.await_args.args
        decoded = json.loads(message)
        assert decoded["odds"]["book_line_at"].startswith("2025-09-03T19:5")
        assert decoded["odds"]["scheduled_start"] == "2025-09-03T20:10:00+00:00"

    def test_the_json_default_still_refuses_an_unknown_type(self) -> None:
        with pytest.raises(TypeError):
            json.dumps({"x": object()}, default=live._json_default)


# ===========================================================================
# The opening-line job
# ===========================================================================


class _EmptyOneRow:
    """A provider with only the one-row methods, and every price empty.

    It stands for the BettingPros case the job used to trip on: two sides
    naming two openers give an empty row.
    """

    def get_odds(self, game_pk, *, line_type="current", market_type="moneyline", **kw):
        row = MockOddsAPI.get_odds(game_pk, line_type=line_type, market_type=market_type)
        return {**row, **dict.fromkeys(live.GAME_ODDS_FIELDS), "source": "bettingpros"}

    def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current", **kw):
        row = MockOddsAPI.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)
        return {**row, "line": None, "over_ml": None, "under_ml": None}


def _job(provider: Any, *, dry_run: bool = False) -> OpeningLineJob:
    job = OpeningLineJob(dsn="postgresql://unused", dry_run=dry_run)
    job._provider = provider
    job._db = _RecordingConn()
    return job


class TestOpeningLineJob:
    def test_an_empty_opening_row_is_not_stored(self) -> None:
        job = _job(_EmptyOneRow())
        assert job._fetch_opening_rows(1) == []
        assert job.refusals.n_offered == 0

    @pytest.mark.asyncio
    async def test_an_empty_prop_row_never_reaches_the_not_null_line(self) -> None:
        job = _job(_EmptyOneRow())
        inserted = await job._capture_prop_opening_lines(1, 682243, None)
        assert inserted == 0
        assert job._db.executes == [] and job._db.batches == []

    @pytest.mark.asyncio
    async def test_every_kept_row_is_stored_with_its_label_stamp_and_hash(self) -> None:
        job = _job(_every_payload())
        rows = job._fetch_opening_rows(776465)
        assert [r["book"] for r in rows] == ["bp:10"]  # FanDuel opened the moneyline
        await job._store_opening_line(776465, rows[0])
        # Review finding 4: the job binds through the live writer, so the row
        # carries the odds_hash the live schema requires, and the dedup.
        assert job._db.executes == []
        ((sql, params),) = job._db.batches
        assert sql == live._GAME_ODDS_INSERT_SQL
        assert "ON CONFLICT (game_pk, source, odds_hash)" in sql
        (args,) = params
        assert len(args) == 19
        assert args[3] == "bp:10" and args[4] == "opening"
        assert args[-2] == datetime(2025, 9, 1, 12, 0, tzinfo=UTC)  # the opener's created
        assert args[-1] == LiveIngestionPipeline._odds_hash(rows[0])

    @pytest.mark.asyncio
    async def test_the_prop_opening_row_carries_its_label_stamp_and_hash(self) -> None:
        job = _job(_every_payload())
        inserted = await job._capture_prop_opening_lines(776465, 682243, None)
        # Only the strikeout market has an offer; DraftKings opened it.
        assert inserted == 1
        assert job._db.executes == []
        ((sql, params),) = job._db.batches
        assert sql == live._PROP_ODDS_INSERT_SQL
        (args,) = params
        assert args[0] == 776465 and args[1] == 682243
        assert args[4] == "strikeouts" and args[5] == 5.5
        assert args[8] == "bp:12" and args[9] == "opening"
        assert args[-2] == datetime(2025, 9, 2, 9, 0, tzinfo=UTC)
        assert isinstance(args[-1], str) and len(args[-1]) == 64

    @pytest.mark.asyncio
    async def test_the_old_insert_without_a_hash_fails_the_stand_in(self) -> None:
        # The pre-fix INSERT: no odds_hash column. The live schema refuses it,
        # and so does the stand-in now.
        conn = _RecordingConn()
        with pytest.raises(_NotNullViolation):
            await conn.execute(
                "INSERT INTO raw.game_odds (game_pk, book, book_line_at) VALUES ($1,$2,$3)",
                1,
                "bp:10",
                None,
            )

    def test_a_non_sportsbook_opener_is_stored_under_its_label(self) -> None:
        # The provider does not ask what kind of book opened the market; the
        # job stores the row as the loader does, and the readers keep only the
        # bettable books.
        kalshi = _offers(
            _sel(participant="SD", opener=(68, None, -150, "2025-09-01 12:00:00")),
            _sel(participant="BAL", opener=(68, None, 130, "2025-09-01 12:00:00")),
        )
        job = _job(_Synthetic({122: kalshi}))
        rows = job._fetch_opening_rows(776465)
        assert [r["book"] for r in rows] == ["bp:68"]

    @pytest.mark.asyncio
    async def test_a_row_without_a_line_type_is_stored_as_opening(self) -> None:
        job = _job(MockOddsAPI())
        row = MockOddsAPI.get_odds(745000, line_type="opening", market_type="moneyline")
        del row["line_type"]
        await job._store_opening_line(745000, row)
        ((_sql, (args,)),) = job._db.batches
        assert args[4] == "opening"
        assert args[-1] == LiveIngestionPipeline._odds_hash({**row, "line_type": "opening"})

    @pytest.mark.asyncio
    async def test_a_refused_prop_row_is_counted_not_stored(self) -> None:
        class _Mismatch(MockOddsAPI):
            def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, *, line_type="current"):
                row = MockOddsAPI.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)
                return [{**row, "book": "bp:19", "over_line": 5.5, "under_line": 6.5}]

        job = _job(_Mismatch())
        assert await job._capture_prop_opening_lines(1, 682243, None) == 0
        assert job._db.executes == [] and job._db.batches == []
        assert {rule for rule, _m, book in job.refusals.counts()} == {"total_line_mismatch"}
        assert job.refusals.refused == 5  # every pitcher market

    @pytest.mark.asyncio
    async def test_the_mock_keeps_its_behaviour(self) -> None:
        job = _job(MockOddsAPI())
        rows = job._fetch_opening_rows(745000)
        assert len(rows) == 1
        assert rows[0] == MockOddsAPI.get_odds(745000, line_type="opening", market_type="moneyline")
        await job._store_opening_line(745000, rows[0])
        ((_sql, (args,)),) = job._db.batches
        assert args[3] == "consensus" and args[2] is True and args[-2] is None
        inserted = await job._capture_prop_opening_lines(745000, 682243, 682244)
        assert inserted == 2 * len(live.PITCHER_PROP_STATS)
        # One batch per (pitcher, market).
        assert len(job._db.batches) == 1 + 2 * len(live.PITCHER_PROP_STATS)

    @pytest.mark.asyncio
    async def test_a_dry_run_writes_nothing(self) -> None:
        job = _job(MockOddsAPI(), dry_run=True)
        await job._store_opening_line(1, job._fetch_opening_rows(1)[0])
        assert await job._capture_prop_opening_lines(1, 682243, None) == 0
        assert job._db.executes == [] and job._db.batches == []

    @pytest.mark.asyncio
    async def test_run_skips_a_game_without_an_opening_row(self, monkeypatch) -> None:
        job = _job(_EmptyOneRow())
        stored: list[int] = []

        async def _noop(*a, **k):
            return None

        async def _games():
            return [{"game_pk": 1, "home_pitcher_id": None, "away_pitcher_id": None}]

        async def _no_opening(game_pk):
            return False

        async def _store(game_pk, odds):
            stored.append(game_pk)

        async def _start():
            return 1

        monkeypatch.setattr(job, "_connect", _noop)
        monkeypatch.setattr(job, "_close", _noop)
        monkeypatch.setattr(job, "_start_log_row", _start)
        monkeypatch.setattr(job, "_finish_log_row", _noop)
        monkeypatch.setattr(job, "_fetch_upcoming_games", _games)
        monkeypatch.setattr(job, "_has_opening_line", _no_opening)
        monkeypatch.setattr(job, "_store_opening_line", _store)
        summary = await job.run()
        assert stored == []
        assert summary["games_checked"] == 1
        assert summary["opening_line_games_captured"] == 0

    def test_the_job_builds_one_provider(self, monkeypatch) -> None:
        built: list[int] = []

        def _factory(name=None):
            built.append(1)
            return MockOddsAPI()

        import pipeline.etl.opening_line_job as olj

        monkeypatch.setattr(olj, "get_odds_provider", _factory)
        job = OpeningLineJob(dsn="postgresql://unused")
        job._fetch_opening_rows(1)
        job._fetch_opening_rows(2)
        assert built == [1]


def test_every_game_market_is_asked_once_per_cycle() -> None:
    """The live cycle asks for all fifteen markets, the full-game three included."""
    asked: list[str] = []

    class _Counting(MockOddsAPI):
        def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline"):
            asked.append(market_type)
            return super().get_odds_by_book(game_pk, line_type=line_type, market_type=market_type)

    p = _bare_pipeline(_odds=_Counting())
    rows = p._fetch_game_market_odds(1)
    assert asked == list(GAME_MARKET_TYPES)
    assert len(rows) == len(GAME_MARKET_TYPES)
