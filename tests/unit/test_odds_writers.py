"""SIM-555 (2026-09-28), SIM-645 — the writers store every book's row, guarded and batched.

These tests drive the writer half of the SIM-555 build plan
(``docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`` §5.4, test 14), on
the one load path of SIM-645 (``betting/load_odds_to_db.py``):

  * the historical loader (``betting/historical_loader.py``): ``--book`` restricts a run
    to one book; the resume query counts only the one-row-per-book rows; the
    done-file; the run's guard summary and its rows by book;
  * the live cycle (``pipeline/live/live_ingestion_pipeline.py``) persists
    every book's row of every game market and of every prop offer, one batch
    per offer, with the guard in front; the per-pitch row is not stored;
  * ``book_line_at`` reaches both INSERTs with ``odds_hash`` still the LAST
    argument, and the stamp stays out of the hash;
  * an odds row with datetimes survives ``json.dumps`` on its way to a browser;
  * the day's games get every book's current line before first pitch (the
    pre-game cycle, driven by the schedule poll);
  * a resumed load skips only the games its own kind of row marks as loaded.

No network and no database. The rows come from the real BettingPros provider
fed by recorded real responses (``tests/bp_recorded.py``): game 746437 (SEA at
DET, 2024-08-15) for the full-game markets and the props, and event 99026 for
the segment markets. No test invents a price (the owner's ruling, SIM-645).
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
from tests.bp_recorded import (
    BRYCE_MILLER,
    GAME_92857,
    GAME_99026,
    Recorded92857,
    Recorded99026,
    recorded_game_rows,
    recorded_prop_rows,
)

import betting.historical_loader as loader
from betting.bettingpros.fetch_odds import BettingProsOddsProvider
from betting.load_odds_to_db import (
    _GAME_ODDS_INSERT_SQL,
    _PROP_ODDS_INSERT_SQL,
    _odds_hash,
    _prop_odds_hash,
    _stamp_param,
    insert_game_odds_rows,
    insert_prop_odds_rows,
)
from betting.markets import (
    GAME_MARKET_TYPES,
    PROP_ROLE_BATTER,
    PROP_ROLE_BOTH,
    PROP_ROLE_PITCHER,
)
from betting.validate_odds import RefusalTally
from pipeline.live import live_ingestion_pipeline as live
from pipeline.live.ingestion.constants import (
    PREGAME_ODDS_CADENCE_S,
    PROP_FETCH_CADENCE_S,
    SCHEDULE_HYDRATE,
)
from pipeline.live.ingestion.mlb_websocket import ConnectionManager
from pipeline.live.ingestion.odds_cycle import (
    _json_default,
    _jsonable_odds,
    _odds_facts_may_be_stale,
)
from pipeline.live.live_ingestion_pipeline import LiveIngestionPipeline

#: A vendor stamp, for the stamp parser.
_T_AT = datetime(2025, 9, 3, 20, 11, 43, tzinfo=UTC)

#: The recorded game's scheduled start, as an MLB schedule entry writes it.
_GAME_DATE = "2024-08-15T17:10:00Z"

#: The current rows of game 746437: ten books on each of the three full-game
#: markets; Bryce Miller's strikeouts (eight books) and hits allowed (two).
_GAME_ROWS = 30
_MILLER_ROWS = 10


class _Counting(Recorded92857):
    """The recorded provider, counting each by-book read."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.reads: list[str] = []

    def get_odds_by_book(self, game_pk, *, line_type="current", market_type="moneyline"):
        self.reads.append(market_type)
        return super().get_odds_by_book(game_pk, line_type=line_type, market_type=market_type)

    def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, *, line_type="current"):
        self.reads.append(prop_stat)
        return super().get_prop_odds_by_book(game_pk, player_id, prop_stat, line_type=line_type)


class _RecordingPool:
    """A stand-in asyncpg pool: records every executemany (no DB)."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql: str, params: Any) -> None:
        self.batches.append((sql, [tuple(p) for p in params]))

    async def fetch(self, sql: str, *args: Any) -> list:
        return []

    async def close(self) -> None:
        return None


def _minutes_ago(minutes: float) -> datetime:
    """A cadence-clock stamp ``minutes`` before now (the clocks hold aware UTC)."""
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _bare_pipeline(**attrs: Any) -> LiveIngestionPipeline:
    """A pipeline built without ``__init__`` (the tests/conftest.py idiom).

    ``_last_seen_column`` is False: a database before Alembic 0029, so the plain
    insert SQL runs.
    """
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
    p._last_seen_column = False
    for key, value in attrs.items():
        setattr(p, key, value)
    return p


def _patch_run(monkeypatch, games: list[int], roles: Any, provider: Any) -> _RecordingPool:
    """Wire ``loader.run`` to ``games``, a lineup reader and ``provider``; no database."""
    pool = _RecordingPool()

    async def _games(dsn, seasons, max_games, **kw):
        return [{"game_pk": pk} for pk in games]

    async def _create_pool(*args, **kwargs):
        return pool

    async def _no_column(*args, **kwargs):
        return False

    import sys
    import types

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=_create_pool))
    # run() sets the offline cache time-to-live only when it is unset; pin
    # it here so monkeypatch restores the environment afterwards.
    monkeypatch.setenv("ODDS_OFFERS_CACHE_TTL_S", "0")
    monkeypatch.setattr(loader, "_fetch_final_games", _games)
    monkeypatch.setattr(loader, "_fetch_lineup_roles", roles)
    monkeypatch.setattr(loader, "pg_column_exists_async", _no_column)
    monkeypatch.setattr(loader, "odds_source", lambda: provider)
    return pool


# ===========================================================================
# The loader (plan test 14a)
# ===========================================================================


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
        monkeypatch.setattr(loader, "_fetch_final_games", _games)
        monkeypatch.setattr(loader, "odds_source", Recorded92857)
        base = [
            "--seasons",
            "2019",
            "--skip-loaded-since",
            "2026-09-28T10:00:00+00:00",
        ]
        assert await loader.run(loader.parse_args([*base, "--no-props"])) == 0
        assert seen[-1]["resume_on_game_odds"] is True
        assert seen[-1]["line_types"] == ("opening", "closing")
        assert seen[-1]["skip_loaded_since"] == self._SINCE
        assert await loader.run(loader.parse_args([*base, "--line-types", "closing"])) == 0
        assert seen[-1]["resume_on_game_odds"] is False
        assert seen[-1]["line_types"] == ("closing",)


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
        async def _roles(pool, game_pk):
            raise AssertionError("a --no-props run reads no lineup")

        pool = _patch_run(monkeypatch, [GAME_99026], _roles, Recorded99026())
        caplog.set_level(logging.INFO, logger="betting.historical_loader")
        args = loader.parse_args(
            [
                "--seasons",
                "2026",
                "--game-markets",
                "f5_moneyline",
                "--line-types",
                "closing",
                "--no-props",
            ]
        )
        assert await loader.run(args) == 0
        # One offer, one batch: FanDuel's row; the blend's and Caesars' are refused.
        ((sql, rows),) = pool.batches
        assert sql == _GAME_ODDS_INSERT_SQL
        assert [r[3] for r in rows] == ["bp:10"]
        text = caplog.text
        assert "guard refusals: 2 of 3 rows offered" in text
        assert "three_way_two_way_team_prices: 2" in text
        assert "rows written by book:" in text
        assert "bp:10 (FanDuel): 1" in text
        # 2 of 3 is above the 5% limit: the run warns.
        assert "the odds guard refused 2 of 3 rows" in text

    @pytest.mark.asyncio
    async def test_run_loads_the_game_markets_then_each_players_props(self, monkeypatch) -> None:
        async def _roles(pool, game_pk):
            return {BRYCE_MILLER: PROP_ROLE_PITCHER}

        pool = _patch_run(monkeypatch, [GAME_92857], _roles, Recorded92857())
        args = loader.parse_args(["--seasons", "2024", "--line-types", "current"])
        assert await loader.run(args) == 0
        game = [rows for sql, rows in pool.batches if "raw.game_odds" in sql]
        props = [rows for sql, rows in pool.batches if "raw.prop_odds" in sql]
        assert [len(b) for b in game] == [10, 10, 10]
        # A pitcher is asked only the pitcher markets: strikeouts and hits allowed.
        assert sorted(b[0][4] for b in props) == ["hits_allowed", "strikeouts"]
        assert sum(len(b) for b in props) == _MILLER_ROWS


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
    @pytest.mark.asyncio
    async def test_run_skips_the_listed_games_and_lists_each_finished_game(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        done = tmp_path / "load.done"
        done.write_text("776466\n", encoding="utf-8")
        loaded: list[int] = []

        async def _roles(pool, game_pk):
            loaded.append(game_pk)
            return {}

        _patch_run(monkeypatch, [776465, 776466, 776467], _roles, Recorded92857())
        caplog.set_level(logging.INFO, logger="betting.historical_loader")
        args = loader.parse_args(
            [
                "--seasons",
                "2025",
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
        """A crash inside a game (here: an error while its lineup loads) leaves the
        game off the list, so the next run loads it again."""
        done = tmp_path / "load.done"

        async def _roles(pool, game_pk):
            if game_pk == 776466:
                raise RuntimeError("the host went down")
            return {}

        _patch_run(monkeypatch, [776465, 776466], _roles, Recorded92857())
        args = loader.parse_args(
            [
                "--seasons",
                "2025",
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
    async def test_the_live_cycle_persists_every_book(self) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        written = await p._persist_game_odds_cycle(GAME_92857)
        # The moneyline, the total and the run line: ten books each.
        assert written == _GAME_ROWS
        p._db.execute.assert_not_awaited()
        batches = {call.args[1][0][5]: call.args for call in p._db.executemany.await_args_list}
        assert set(batches) == {"moneyline", "total", "runline"}
        for sql, params in batches.values():
            assert sql == _GAME_ODDS_INSERT_SQL
            assert len({row[3] for row in params}) == 10  # one row per book
            assert {row[4] for row in params} == {"current"}
        assert p._refusal_tally().n_offered == _GAME_ROWS
        assert p._refusal_tally().refused == 0

    @pytest.mark.asyncio
    async def test_the_guard_sits_in_front_of_the_live_cycle(self, caplog) -> None:
        p = _bare_pipeline(_odds=Recorded99026())
        caplog.set_level(logging.INFO, logger="betting.load_odds_to_db")
        await p._persist_game_odds_cycle(GAME_99026)
        assert p._refusal_tally().counts() == {
            ("three_way_two_way_team_prices", "f5_moneyline", "bp:0"): 1,
            ("three_way_two_way_team_prices", "f5_moneyline", "bp:13"): 1,
        }
        assert f"refused game {GAME_99026} current/f5_moneyline bp:0" in caplog.text
        f5 = [
            call.args[1]
            for call in p._db.executemany.await_args_list
            if call.args[1][0][5] == "f5_moneyline"
        ]
        assert [[row[3] for row in batch] for batch in f5] == [["bp:10"]]

    @pytest.mark.asyncio
    async def test_the_live_prop_cycle_persists_every_book(self) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        state = {"current_pitcher_id": BRYCE_MILLER, "home_lineup": [], "away_lineup": []}
        written = await p._persist_prop_odds_cycle(GAME_92857, state)
        assert written == _MILLER_ROWS
        batches = [call.args for call in p._db.executemany.await_args_list]
        assert {sql for sql, _rows in batches} == {_PROP_ODDS_INSERT_SQL}
        by_stat = {rows[0][4]: sorted(r[8] for r in rows) for _sql, rows in batches}
        assert by_stat["hits_allowed"] == ["bp:10", "bp:15"]
        assert len(by_stat["strikeouts"]) == 8

    @pytest.mark.asyncio
    async def test_a_prop_fetch_error_is_logged_and_the_cycle_goes_on(self, caplog) -> None:
        """SIM-645: a failed prop read used to escape the cycle; now it is logged."""

        class _Broken(Recorded92857):
            def get_prop_odds_by_book(self, game_pk, player_id, prop_stat, **kw):
                if prop_stat == "strikeouts":
                    raise RuntimeError("vendor down")
                return super().get_prop_odds_by_book(game_pk, player_id, prop_stat, **kw)

        p = _bare_pipeline(_odds=_Broken())
        caplog.set_level(logging.WARNING, logger="betting.load_odds_to_db")
        state = {"current_pitcher_id": BRYCE_MILLER, "home_lineup": [], "away_lineup": []}
        assert await p._persist_prop_odds_cycle(GAME_92857, state) == 2  # the hits allowed
        assert f"prop fetch failed game {GAME_92857} current/strikeouts" in caplog.text

    def test_the_tally_is_created_lazily_for_a_new_built_pipeline(self) -> None:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        tally = p._refusal_tally()
        assert isinstance(tally, RefusalTally)
        assert p._refusal_tally() is tally

    def test_the_constructor_builds_the_tally_and_the_game_odds_clock(self) -> None:
        p = LiveIngestionPipeline(dsn="postgresql://x", redis_url="redis://x:6379")
        assert isinstance(p._odds_refusals, RefusalTally)
        assert p._last_game_odds_fetch == {}
        assert isinstance(p._odds, BettingProsOddsProvider)

    def test_a_bare_pipeline_reads_bettingpros(self) -> None:
        p = LiveIngestionPipeline.__new__(LiveIngestionPipeline)
        provider = p._odds_provider()
        assert isinstance(provider, BettingProsOddsProvider)
        assert p._odds_provider() is provider

    @pytest.mark.asyncio
    async def test_a_game_with_no_offer_reaches_neither_the_guard_nor_the_database(self) -> None:
        class _NoSchedule(Recorded92857):
            def _mlb_get(self, path, params):  # type: ignore[override]
                return {"dates": []} if path == "schedule" else super()._mlb_get(path, params)

        # The game is on no schedule: every market reads no row.
        p = _bare_pipeline(_odds=_NoSchedule())
        assert await p._persist_game_odds_cycle(GAME_92857) == 0
        p._db.executemany.assert_not_awaited()
        assert p._refusal_tally().n_offered == 0

    @pytest.mark.asyncio
    async def test_every_game_market_is_asked_once_per_cycle(self) -> None:
        """The live cycle asks for all fifteen markets, the full-game three included."""
        provider = _Counting()
        p = _bare_pipeline(_odds=provider)
        await p._persist_game_odds_cycle(GAME_92857)
        assert provider.reads == list(GAME_MARKET_TYPES)


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
            682243: PROP_ROLE_BOTH,
            605400: PROP_ROLE_PITCHER,
            1: PROP_ROLE_BATTER,
            2: PROP_ROLE_BOTH,
            3: PROP_ROLE_BOTH,
            4: PROP_ROLE_BATTER,
        }
        assert list(roles) == [682243, 605400, 1, 2, 3, 4]

    def test_before_the_lineups_post_only_the_probable_pitchers_are_asked(self) -> None:
        game = {"teams": {"home": {"probablePitcher": {"id": 7}}, "away": {}}}
        assert LiveIngestionPipeline._pregame_prop_player_roles(game) == {7: PROP_ROLE_PITCHER}
        assert LiveIngestionPipeline._pregame_prop_player_roles({}) == {}
        assert LiveIngestionPipeline._pregame_prop_player_roles({"lineups": None}) == {}

    @pytest.mark.asyncio
    async def test_the_pregame_cycle_stores_every_book_before_first_pitch(self) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        game = {"teams": {"home": {"probablePitcher": {"id": BRYCE_MILLER}}}}
        # The three full-game markets, and the probable pitcher's two markets.
        assert await p._persist_pregame_odds(GAME_92857, game) == _GAME_ROWS + _MILLER_ROWS
        batches = [call.args for call in p._db.executemany.await_args_list]
        game_rows = [row for sql, rows in batches if "raw.game_odds" in sql for row in rows]
        prop_rows = [row for sql, rows in batches if "raw.prop_odds" in sql for row in rows]
        assert len(game_rows) == _GAME_ROWS
        assert {r[4] for r in game_rows} == {"current"}
        assert {(r[1], r[9]) for r in prop_rows} == {(BRYCE_MILLER, "current")}
        # The three clocks are set: a second poll inside the cadence reads nothing.
        p._db.executemany.reset_mock()
        assert await p._persist_pregame_odds(GAME_92857, game) == 0
        # The live refresh shares the two cycle clocks with the pre-game cycle.
        state = {"current_pitcher_id": BRYCE_MILLER, "home_lineup": [], "away_lineup": []}
        assert await p._persist_prop_odds_cycle(GAME_92857, state) == 0
        assert await p._persist_game_odds_cycle(GAME_92857) == 0
        p._db.executemany.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_known_player_leaves_the_prop_clock_open(self) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        assert await p._persist_pregame_odds(GAME_92857, {}) == _GAME_ROWS  # game markets only
        assert GAME_92857 not in p._last_prop_fetch
        # The probable pitcher is named. Inside the pre-game cadence the next
        # poll still reads nothing.
        game = {"teams": {"away": {"probablePitcher": {"id": BRYCE_MILLER}}}}
        assert await p._persist_pregame_odds(GAME_92857, game) == 0
        # Ten minutes on, the pass stores his props at once. The game markets
        # add nothing: only the pre-game clock is moved back here, so their
        # own one-minute clock is still closed.
        p._last_pregame_fetch[GAME_92857] = _minutes_ago(PREGAME_ODDS_CADENCE_S / 60 + 1)
        assert await p._persist_pregame_odds(GAME_92857, game) == _MILLER_ROWS
        # The live refresh can also store them at once: the prop clock stayed open.
        p2 = _bare_pipeline(_odds=Recorded92857())
        assert await p2._persist_pregame_odds(GAME_92857, {}) == _GAME_ROWS
        state = {"current_pitcher_id": BRYCE_MILLER, "home_lineup": [], "away_lineup": []}
        assert await p2._persist_prop_odds_cycle(GAME_92857, state) == _MILLER_ROWS

    @pytest.mark.asyncio
    async def test_the_pregame_cadence_is_ten_minutes_per_game(self) -> None:
        """Review item: the 30-second schedule poll started the pre-game cycle
        for every game; only the cycles' 60-second clocks throttled it, so a
        game read the vendor every minute all day. The pre-game pass now has
        its own ten-minute clock per game, checked before either cycle."""
        assert PREGAME_ODDS_CADENCE_S == 600
        assert PREGAME_ODDS_CADENCE_S > PROP_FETCH_CADENCE_S

        provider = _Counting()
        p = _bare_pipeline(_odds=provider)
        game = {"teams": {"home": {"probablePitcher": {"id": BRYCE_MILLER}}}}
        assert await p._persist_pregame_odds(GAME_92857, game) > 0
        first_pass = len(provider.reads)
        assert first_pass > 0
        # Two minutes on (past the cycles' 60-second clocks, inside the
        # pre-game cadence): no cycle runs, so the vendor is not read.
        p._last_pregame_fetch[GAME_92857] = _minutes_ago(2)
        p._last_game_odds_fetch[GAME_92857] = _minutes_ago(2)
        p._last_prop_fetch[GAME_92857] = _minutes_ago(2)
        assert await p._persist_pregame_odds(GAME_92857, game) == 0
        assert len(provider.reads) == first_pass
        # Each game keeps its own clock: another game is read at once.
        await p._persist_pregame_odds(2, game)
        assert len(provider.reads) == 2 * first_pass
        # Past the cadence the pass runs again.
        p._last_pregame_fetch[GAME_92857] = _minutes_ago(11)
        assert await p._persist_pregame_odds(GAME_92857, game) > 0
        assert len(provider.reads) == 3 * first_pass

    @pytest.mark.asyncio
    async def test_the_pregame_clock_is_created_lazily(self) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
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
        p = _bare_pipeline(_odds=Recorded92857())
        assert await p._persist_pregame_odds(GAME_92857, {}) == _GAME_ROWS
        # The game starts two minutes later: the live cycle reads again.
        p._last_game_odds_fetch[GAME_92857] = _minutes_ago(2)
        assert p._last_pregame_fetch[GAME_92857] > _minutes_ago(3)
        assert await p._persist_game_odds_cycle(GAME_92857) == _GAME_ROWS

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_not_raised(self, caplog) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        p._persist_game_odds_cycle = AsyncMock(side_effect=RuntimeError("vendor down"))
        caplog.set_level(logging.WARNING, logger="live_ingestion")
        game = {"teams": {"home": {"probablePitcher": {"id": BRYCE_MILLER}}}}
        assert await p._persist_pregame_odds(1, game) == 0
        assert "pre-game odds cycle failed for game 1: vendor down" in caplog.text

    @pytest.mark.asyncio
    async def test_the_game_row_is_upserted_before_the_first_odds_insert(self) -> None:
        """Review fix (runtime #3): ``raw.game_odds.game_pk`` references
        ``raw.games``. The schedule poll starts the pre-game task before its own
        upsert task, so on a game's first poll the odds INSERT reached Postgres
        before the game row, failed the foreign key, and the ten-minute clock
        kept the moneyline from a retry. The pass now upserts the game first."""
        p = _bare_pipeline(_odds=Recorded92857())
        order: list[str] = []

        async def _upsert(game: dict) -> None:
            assert game["gamePk"] == GAME_92857
            order.append("upsert")

        async def _many(sql: str, rows: list) -> None:
            order.append("odds")

        p._upsert_game_record = _upsert
        p._db.executemany = AsyncMock(side_effect=_many)
        game = {
            "gamePk": GAME_92857,
            "gameDate": _GAME_DATE,
            "status": {"abstractGameState": "Preview"},
        }
        assert await p._persist_pregame_odds(GAME_92857, game) == _GAME_ROWS
        assert order[0] == "upsert" and order.count("upsert") == 1
        assert order.count("odds") == 3  # the moneyline, the total and the run line
        # Inside the cadence the pass does nothing: no upsert, no odds.
        order.clear()
        assert await p._persist_pregame_odds(GAME_92857, game) == 0
        assert order == []

    @pytest.mark.asyncio
    async def test_a_failed_game_row_upsert_does_not_stop_the_odds(self, caplog) -> None:
        p = _bare_pipeline(_odds=Recorded92857())
        p._upsert_game_record = AsyncMock(side_effect=RuntimeError("db hiccup"))
        caplog.set_level(logging.WARNING, logger="live_ingestion")
        game = {"gamePk": GAME_92857, "gameDate": _GAME_DATE}
        assert await p._persist_pregame_odds(GAME_92857, game) == _GAME_ROWS
        assert f"pre-game game-row upsert failed for game {GAME_92857}: db hiccup" in caplog.text
        # A schedule entry without its game id is not upserted (a thin test payload).
        p2 = _bare_pipeline(_odds=Recorded92857())
        p2._upsert_game_record = AsyncMock()
        assert await p2._persist_pregame_odds(GAME_92857, {}) == _GAME_ROWS
        p2._upsert_game_record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_real_upsert_reaches_the_database_before_the_odds(self) -> None:
        """The same order through the real ``_upsert_game_record`` SQL."""
        p = _bare_pipeline(_odds=Recorded92857())
        calls: list[str] = []
        p._db.execute = AsyncMock(side_effect=lambda sql, *a: calls.append(sql))
        p._db.executemany = AsyncMock(side_effect=lambda sql, rows: calls.append(sql))
        game = {
            "gamePk": GAME_92857,
            "gameDate": _GAME_DATE,
            "season": "2024",
            "status": {"abstractGameState": "Preview"},
            "teams": {"home": {"team": {"id": 116}}, "away": {"team": {"id": 136}}},
        }
        await p._persist_pregame_odds(GAME_92857, game)
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
        p._write_preview_lineup = AsyncMock()  # SIM-519 Part B; its own tests cover it
        await p._sync_live_games()
        for _ in range(3):  # let the created tasks run
            await asyncio.sleep(0)
        assert seen_params[0]["hydrate"] == SCHEDULE_HYDRATE == "probablePitcher,lineups"
        p._persist_pregame_odds.assert_awaited_once_with(11, preview)
        p._write_preview_lineup.assert_awaited_once_with(preview)
        assert p._upsert_game_record.await_count == 2

    @pytest.mark.asyncio
    async def test_the_poll_makes_the_provider_forget_a_postponed_game(self) -> None:
        """SIM-555 (2026-10-01): the provider keeps a found game for the process
        lifetime. Each entry of a postponed, suspended or made-up game makes it forget
        the game, so the made-up game is matched from its new schedule."""
        postponed = {  # the original date's entry, rescheduled
            "gamePk": 21,
            "rescheduleGameDate": "2024-07-13",
            "status": {"abstractGameState": "Final", "detailedState": "Postponed"},
        }
        not_yet_rescheduled = {
            "gamePk": 22,
            "status": {"abstractGameState": "Final", "codedGameState": "D"},
        }
        made_up = {
            "gamePk": 23,
            "rescheduledFrom": "2024-05-25T00:15:00Z",
            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
        }
        made_up_final = {  # after its first pitch the game keeps its facts
            "gamePk": 24,
            "rescheduledFrom": "2024-05-25T00:15:00Z",
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
        }
        regular = {"gamePk": 25, "status": {"abstractGameState": "Preview"}}
        suspended = {
            "gamePk": 26,
            "resumeGameDate": "2024-08-28",
            "status": {"abstractGameState": "Live", "detailedState": "Suspended"},
        }
        resumed = {
            "gamePk": 27,
            "resumedFrom": "2024-08-28T00:10:00Z",
            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
        }
        schedule = {
            "dates": [
                {
                    "games": [
                        postponed,
                        not_yet_rescheduled,
                        made_up,
                        made_up_final,
                        regular,
                        suspended,
                        resumed,
                    ]
                }
            ]
        }

        class _Resp:
            async def json(self) -> dict:
                return schedule

            async def __aenter__(self) -> _Resp:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        class _Http:
            def get(self, url: str, params: dict | None = None) -> _Resp:
                return _Resp()

        provider = MagicMock(spec=BettingProsOddsProvider)
        p = _bare_pipeline(_http=_Http(), _odds=provider)
        p._upsert_game_record = AsyncMock()
        p._persist_pregame_odds = AsyncMock(return_value=0)
        await p._sync_live_games()
        for _ in range(3):  # let the created tasks run
            await asyncio.sleep(0)
        assert [c.args for c in provider.forget_game.call_args_list] == [
            (21,),
            (22,),
            (23,),
            (26,),
            (27,),
        ]
        # The rescheduled and suspended entries are still skipped; the Preview ones
        # (the made-up, the regular and the resumed game) get the pre-game cycle.
        assert [c.args[0] for c in p._persist_pregame_odds.await_args_list] == [23, 25, 27]

    def test_a_provider_without_forget_game_is_skipped(self) -> None:
        p = _bare_pipeline(_odds=object())  # a provider object with no forget_game
        p._forget_odds_game(21)  # no call, no error
        p._forget_odds_game(None)
        assert not _odds_facts_may_be_stale({"gamePk": 1, "status": {}})


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
        sample_feed["gamePk"] = GAME_92857
        p = _bare_pipeline(_db=mock_db_pool, _redis=mock_redis, _odds=Recorded92857())

        async def _fetch_feed(_pk):
            return sample_feed

        p._fetch_feed = _fetch_feed
        await p._refresh_game_state(GAME_92857)

        assert sent, "the refresh loop did not broadcast"
        odds = sent[-1][1]["odds"]
        assert odds["book"] == "bp:12"  # the graded book's moneyline
        assert (odds["home_ml"], odds["away_ml"]) == (120, -142)
        assert odds["book_line_at"] == "2024-08-15T17:05:41+00:00"
        assert "scheduled_start" not in odds and "game_date" not in odds
        assert "postponed_start" not in odds  # SIM-555: a guard-only key too
        # SIM-555: the per-pitch row is not persisted; the cycle writes batches.
        assert not any(
            "raw.game_odds" in str(c.args[0]) for c in mock_db_pool.execute.await_args_list
        )
        assert mock_db_pool.executemany.await_count >= 1


class TestTheWriters:
    @pytest.mark.asyncio
    async def test_an_empty_batch_sends_nothing(self) -> None:
        pool = _RecordingPool()
        assert await insert_game_odds_rows(pool, GAME_92857, []) == 0
        assert await insert_prop_odds_rows(pool, []) == 0
        assert pool.batches == []

    @pytest.mark.asyncio
    async def test_the_module_writers_send_one_batch_with_the_hash_last(self) -> None:
        pool = _RecordingPool()
        rows = recorded_game_rows("moneyline")
        props = recorded_prop_rows()
        assert await insert_game_odds_rows(pool, GAME_92857, rows) == len(rows)
        assert await insert_prop_odds_rows(pool, props) == len(props)
        (game_sql, game_params), (prop_sql, prop_params) = pool.batches
        assert (game_sql, prop_sql) == (_GAME_ODDS_INSERT_SQL, _PROP_ODDS_INSERT_SQL)
        assert game_sql.index("book_line_at") < game_sql.index("odds_hash")
        for row, params in zip(rows, game_params, strict=True):
            assert len(params) == 19
            assert params[3] == row["book"]  # the book label
            assert params[-2] == row["book_line_at"]  # the stamp: aware UTC
            assert params[-1] == _odds_hash(row)  # the hash, last
        for prop, params in zip(props, prop_params, strict=True):
            assert len(params) == 13
            assert params[8] == prop["book"]
            assert params[-2] == prop["book_line_at"]  # each book's own stamp
            assert params[-1] == _prop_odds_hash(prop)

    def test_the_stamp_stays_out_of_the_hash(self) -> None:
        row = recorded_game_rows("moneyline")[0]
        assert _odds_hash(row) == _odds_hash({**row, "book_line_at": None})
        assert _odds_hash(row) != _odds_hash({**row, "book": "bp:99"})
        prop = recorded_prop_rows()[0]
        assert _prop_odds_hash(prop) == _prop_odds_hash({**prop, "book_line_at": None})

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
        row = recorded_game_rows("moneyline", line_type="closing")[0]
        assert isinstance(row["book_line_at"], datetime)
        assert isinstance(row["scheduled_start"], datetime)
        with pytest.raises(TypeError):
            json.dumps(row)
        safe = _jsonable_odds(row)
        decoded = json.loads(json.dumps(safe))
        assert decoded["book_line_at"] == row["book_line_at"].isoformat()
        assert "scheduled_start" not in decoded and "game_date" not in decoded
        assert "postponed_start" in row and "postponed_start" not in decoded  # SIM-555
        assert decoded["home_ml"] == row["home_ml"]
        assert _jsonable_odds(None) == {}

    @pytest.mark.asyncio
    async def test_the_broadcast_survives_a_datetime_in_the_payload(self) -> None:
        cm = ConnectionManager()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        cm._subscriptions[GAME_92857] = {ws}
        row = recorded_prop_rows()[0]
        await cm.broadcast(GAME_92857, {"type": "game_state_update", "odds": row})
        (message,) = ws.send_text.await_args.args
        decoded = json.loads(message)
        assert decoded["odds"]["book_line_at"] == row["book_line_at"].isoformat()
        assert decoded["odds"]["scheduled_start"] == "2024-08-15T17:10:00+00:00"

    def test_the_json_default_still_refuses_an_unknown_type(self) -> None:
        with pytest.raises(TypeError):
            json.dumps({"x": object()}, default=_json_default)
