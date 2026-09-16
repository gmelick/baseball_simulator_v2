"""
tests/unit/test_sim545_boxscore_ingest.py
=========================================
The official box-score ground truth (SIM-545): the parser over a REAL captured
payload, the persist seams over in-memory fakes, and the fetch seam's retry.

No network, no DB. The fixture ``tests/fixtures/mlb/boxscore_746437.json`` is
the ``/api/v1/game/746437/boxscore`` response for SEA @ DET, 2024-08-15.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
from datetime import date

import pytest

from pipeline.etl.boxscore_ingest import (
    COLUMNS,
    UPSERT_SQL_ASYNCPG,
    UPSERT_SQL_PSYCOPG2,
    BoxscoreIngest,
    PlayerGameStats,
    parse_boxscore,
    persist,
    persist_sync,
)

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "mlb",
    "boxscore_746437.json",
)

GAME_PK = 746437
SEASON = 2024
GAME_DATE = date(2024, 8, 15)
HOME_TEAM = 116  # DET
AWAY_TEAM = 136  # SEA


@pytest.fixture(scope="module")
def teams() -> dict:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["teams"]


@pytest.fixture(scope="module")
def rows(teams) -> list[PlayerGameStats]:
    return parse_boxscore(
        teams,
        game_pk=GAME_PK,
        season=SEASON,
        game_date=GAME_DATE,
        home_team_id=HOME_TEAM,
        away_team_id=AWAY_TEAM,
    )


# ===========================================================================
# parse_boxscore on the real payload
# ===========================================================================


class TestParseRealPayload:
    def test_row_counts_per_side(self, rows):
        """Away: 9 batters + 3 pitchers. Home: 11 batters + 4 pitchers."""
        away = [r for r in rows if r.side == "away"]
        home = [r for r in rows if r.side == "home"]
        assert sum(r.played_bat for r in away) == 9
        assert sum(r.played_pitch for r in away) == 3
        assert sum(r.played_bat for r in home) == 11
        assert sum(r.played_pitch for r in home) == 4
        assert len(rows) == 27

    def test_non_participants_produce_no_rows(self, rows, teams):
        """The bench + bullpen (empty stats dicts) never become rows."""
        ids = {r.player_id for r in rows}
        for side in ("home", "away"):
            for pid in teams[side]["bench"] + teams[side]["bullpen"]:
                assert int(pid) not in ids
        # And every row really appeared: at least one slot is on.
        assert all(r.played_bat or r.played_pitch for r in rows)

    def test_team_ids_and_keys(self, rows):
        by_side = {r.side: r.team_id for r in rows}
        assert by_side == {"home": HOME_TEAM, "away": AWAY_TEAM}
        assert all(r.game_pk == GAME_PK for r in rows)
        assert all(r.season == SEASON for r in rows)
        assert all(r.game_date == GAME_DATE for r in rows)
        assert len({(r.game_pk, r.player_id) for r in rows}) == len(rows)

    def test_victor_robles_batting_line(self, rows):
        """Robles (645302): 1-4 with a double, a stolen base, 2 total bases."""
        robles = next(r for r in rows if r.player_id == 645302)
        assert robles.side == "away"
        assert robles.played_bat is True
        assert robles.played_pitch is False
        assert robles.batting_order == 1
        assert robles.position_code == "CF"
        assert (robles.h, robles.b2, robles.sb, robles.tb) == (1, 1, 1, 2)
        assert (robles.pa, robles.ab, robles.k, robles.bb) == (4, 4, 1, 0)
        assert robles.p_outs == 0  # no pitching line

    def test_shelby_miller_pitching_line(self, rows):
        """Miller (571946): 4 outs on 22 pitches, a reliever (not the starter)."""
        miller = next(r for r in rows if r.player_id == 571946)
        assert miller.side == "home"
        assert miller.played_pitch is True
        assert miller.played_bat is False  # DH game: the pitcher never batted
        assert miller.batting_order is None
        assert miller.position_code == "P"
        assert (miller.p_outs, miller.p_pitches, miller.p_batters_faced) == (4, 22, 4)
        assert miller.p_started is False

    def test_starters_flagged(self, rows):
        starters = sorted(r.player_id for r in rows if r.p_started)
        assert starters == [656412, 682243]  # Faedo (DET), B. Miller (SEA)

    def test_substitute_batting_order_slot(self, rows):
        """A sub's battingOrder '701' reads slot 7, the same as the starter's '700'."""
        mckinstry = next(r for r in rows if r.player_id == 656716)
        assert mckinstry.batting_order == 7
        ibanez = next(r for r in rows if r.player_id == 628451)
        assert ibanez.batting_order == 7

    def test_side_totals_match_the_box(self, rows, teams):
        """The per-side sum of hits equals the box's own team batting total."""
        for side in ("home", "away"):
            box_hits = int(teams[side]["teamStats"]["batting"]["hits"])
            assert sum(r.h for r in rows if r.side == side and r.played_bat) == box_hits


# ===========================================================================
# parse_boxscore on hand-built payloads (the edge cases the fixture lacks)
# ===========================================================================


def _player(pid: int, *, batting: dict | None = None, pitching: dict | None = None, **extra):
    return {
        "person": {"id": pid},
        "position": {"abbreviation": extra.get("pos", "1B")},
        **({"battingOrder": extra["bo"]} if "bo" in extra else {}),
        "stats": {"batting": batting or {}, "pitching": pitching or {}, "fielding": {}},
    }


class TestParseHandBuilt:
    def test_two_way_row_keeps_both_slots(self):
        teams = {
            "home": {
                "team": {"id": 108},
                "players": {
                    "ID660271": _player(
                        660271,
                        batting={"gamesPlayed": 1, "hits": 2, "homeRuns": 1, "totalBases": 5},
                        pitching={"gamesPlayed": 1, "gamesStarted": 1, "outs": 18, "strikeOuts": 9},
                        bo="200",
                        pos="TWP",
                    )
                },
            },
            "away": {"team": {"id": 109}, "players": {}},
        }
        rows = parse_boxscore(teams, game_pk=1, season=2024, game_date=date(2024, 6, 1))
        assert len(rows) == 1
        (r,) = rows
        assert r.played_bat and r.played_pitch
        assert (r.h, r.hr, r.tb) == (2, 1, 5)
        assert (r.p_outs, r.p_k, r.p_started) == (18, 9, True)
        assert r.batting_order == 2
        assert r.position_code == "TWP"

    def test_team_id_falls_back_to_the_caller(self):
        teams = {
            "home": {"players": {"ID1": _player(1, batting={"gamesPlayed": 1})}},
            "away": {"players": {"ID2": _player(2, pitching={"gamesPlayed": 1})}},
        }
        rows = parse_boxscore(
            teams,
            game_pk=5,
            season=2024,
            game_date=date(2024, 6, 1),
            home_team_id=111,
            away_team_id=222,
        )
        assert {r.player_id: r.team_id for r in rows} == {1: 111, 2: 222}

    def test_no_team_id_anywhere_skips_the_player(self, caplog):
        teams = {"home": {"players": {"ID1": _player(1, batting={"gamesPlayed": 1})}}}
        rows = parse_boxscore(teams, game_pk=5, season=2024, game_date=date(2024, 6, 1))
        assert rows == []

    def test_empty_and_missing_blocks(self):
        assert parse_boxscore({}, game_pk=1, season=2024, game_date=date(2024, 6, 1)) == []
        teams = {"home": {"team": {"id": 1}}, "away": None}
        assert parse_boxscore(teams, game_pk=1, season=2024, game_date=date(2024, 6, 1)) == []

    def test_odd_values_read_zero_or_none(self):
        teams = {
            "home": {
                "team": {"id": 1},
                "players": {
                    "IDx": _player(
                        7, batting={"gamesPlayed": "1", "hits": None, "rbi": "abc"}, bo="x"
                    )
                },
            }
        }
        (r,) = parse_boxscore(teams, game_pk=1, season=2024, game_date=date(2024, 6, 1))
        assert r.played_bat is True
        assert (r.h, r.rbi) == (0, 0)
        assert r.batting_order is None

    def test_position_code_truncated_to_five(self):
        teams = {
            "home": {
                "team": {"id": 1},
                "players": {"IDx": _player(7, batting={"gamesPlayed": 1}, pos="LONGPOS")},
            }
        }
        (r,) = parse_boxscore(teams, game_pk=1, season=2024, game_date=date(2024, 6, 1))
        assert r.position_code == "LONGP"


# ===========================================================================
# The row shape + the two upserts
# ===========================================================================


class TestRowShape:
    def test_columns_match_the_dataclass_and_the_table(self, rows):
        assert COLUMNS[:2] == ("game_pk", "player_id")
        expected = (
            "game_pk",
            "player_id",
            "team_id",
            "side",
            "season",
            "game_date",
            "batting_order",
            "position_code",
            "played_bat",
            "played_pitch",
            "pa",
            "ab",
            "r",
            "h",
            "b2",
            "b3",
            "hr",
            "rbi",
            "sb",
            "cs",
            "bb",
            "k",
            "hbp",
            "sf",
            "tb",
            "p_outs",
            "p_h",
            "p_r",
            "p_er",
            "p_bb",
            "p_k",
            "p_hr",
            "p_pitches",
            "p_batters_faced",
            "p_started",
        )
        assert expected == COLUMNS
        r = rows[0]
        assert len(r.as_tuple()) == len(COLUMNS)
        assert tuple(r.as_dict()) == COLUMNS

    def test_upsert_sql_refreshes_every_non_key_column(self):
        for sql in (UPSERT_SQL_ASYNCPG, UPSERT_SQL_PSYCOPG2):
            assert "INSERT INTO raw.game_player_stats" in sql
            assert "ON CONFLICT (game_pk, player_id) DO UPDATE" in sql
            for col in COLUMNS[2:]:
                assert f"{col} = EXCLUDED.{col}" in sql
            assert "game_pk = EXCLUDED" not in sql
            assert "fetched_at = now()" in sql
        assert f"${len(COLUMNS)}" in UPSERT_SQL_ASYNCPG
        assert UPSERT_SQL_PSYCOPG2.count("%s") == len(COLUMNS)

    def test_rows_are_frozen(self, rows):
        with pytest.raises(AttributeError):
            rows[0].h = 99  # type: ignore[misc]


class _FakeAsyncConn:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    async def executemany(self, sql, batch):
        self.calls.append((sql, list(batch)))


class TestPersistSeams:
    def test_async_persist_writes_every_row_once(self, rows):
        conn = _FakeAsyncConn()
        n = asyncio.run(persist(conn, rows))
        assert n == len(rows) == 27
        (sql, batch) = conn.calls[0]
        assert sql == UPSERT_SQL_ASYNCPG
        assert batch[0] == rows[0].as_tuple()

    def test_async_persist_empty_writes_nothing(self):
        conn = _FakeAsyncConn()
        assert asyncio.run(persist(conn, [])) == 0
        assert conn.calls == []

    def test_sync_persist_uses_execute_batch(self, rows, monkeypatch):
        captured: dict = {}

        def fake_execute_batch(cur, sql, batch):
            captured["cur"] = cur
            captured["sql"] = sql
            captured["batch"] = list(batch)

        import psycopg2.extras

        monkeypatch.setattr(psycopg2.extras, "execute_batch", fake_execute_batch)
        cur = object()
        assert persist_sync(cur, rows) == 27
        assert captured["cur"] is cur
        assert captured["sql"] == UPSERT_SQL_PSYCOPG2
        assert captured["batch"][-1] == rows[-1].as_tuple()

    def test_sync_persist_empty_writes_nothing(self):
        assert persist_sync(object(), []) == 0

    def test_ingest_persist_method_delegates(self, rows):
        conn = _FakeAsyncConn()
        n = asyncio.run(BoxscoreIngest().persist(conn, rows[:3]))
        assert n == 3


# ===========================================================================
# The fetch seam
# ===========================================================================


class TestFetch:
    def test_fetch_rows_goes_through_the_one_seam(self, teams):
        seen: list[tuple[str, dict]] = []

        class Stub(BoxscoreIngest):
            def _mlb_get(self, path, params=None):
                seen.append((path, dict(params or {})))
                return {"teams": teams}

        rows = Stub().fetch_rows(GAME_PK, season=SEASON, game_date=GAME_DATE)
        assert seen == [(f"game/{GAME_PK}/boxscore", {})]
        assert len(rows) == 27

    def test_fetch_boxscore_rejects_a_payload_without_teams(self):
        class Stub(BoxscoreIngest):
            def _mlb_get(self, path, params=None):
                return {"message": "not found"}

        with pytest.raises(ValueError):
            Stub().fetch_boxscore(1)

    def test_retry_on_503_then_success(self, monkeypatch):
        attempts: list[str] = []

        def flaky(self, url):
            attempts.append(url)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(url, 503, "busy", hdrs=None, fp=None)
            return {"teams": {}}

        monkeypatch.setattr(BoxscoreIngest, "_http_get_json", flaky)
        monkeypatch.setattr("pipeline.etl.boxscore_ingest.time.sleep", lambda s: None)
        ing = BoxscoreIngest(max_attempts=4, backoff=0.0)
        assert ing._mlb_get("game/1/boxscore") == {"teams": {}}
        assert len(attempts) == 3

    def test_no_retry_on_404(self, monkeypatch):
        attempts: list[str] = []

        def gone(self, url):
            attempts.append(url)
            raise urllib.error.HTTPError(url, 404, "gone", hdrs=None, fp=None)

        monkeypatch.setattr(BoxscoreIngest, "_http_get_json", gone)
        with pytest.raises(urllib.error.HTTPError):
            BoxscoreIngest(max_attempts=4, backoff=0.0)._mlb_get("game/1/boxscore")
        assert len(attempts) == 1

    def test_attempts_exhausted_reraises_last(self, monkeypatch):
        def timeout(self, url):
            raise TimeoutError("slow")

        monkeypatch.setattr(BoxscoreIngest, "_http_get_json", timeout)
        monkeypatch.setattr("pipeline.etl.boxscore_ingest.time.sleep", lambda s: None)
        with pytest.raises(TimeoutError):
            BoxscoreIngest(max_attempts=2, backoff=0.0)._mlb_get("game/1/boxscore")

    @staticmethod
    def _sleeps_for(monkeypatch, *, code: int, retry_after: str | None) -> list[float]:
        """Run a 3-attempt fetch that fails every time; return the waits it slept."""
        from email.message import Message

        slept: list[float] = []

        def limited(self, url):
            hdrs = Message()
            if retry_after is not None:
                hdrs["Retry-After"] = retry_after
            raise urllib.error.HTTPError(url, code, "limited", hdrs=hdrs, fp=None)

        monkeypatch.setattr(BoxscoreIngest, "_http_get_json", limited)
        monkeypatch.setattr("pipeline.etl.boxscore_ingest.time.sleep", slept.append)
        with pytest.raises(urllib.error.HTTPError):
            BoxscoreIngest(max_attempts=3, backoff=1.0)._mlb_get("game/1/boxscore")
        return slept

    def test_429_honours_a_numeric_retry_after(self, monkeypatch):
        """A rate-limited answer waits what the server asks, not the 1 s / 2 s ladder."""
        assert self._sleeps_for(monkeypatch, code=429, retry_after="30") == [30.0, 30.0]

    def test_retry_after_is_capped_at_sixty_seconds(self, monkeypatch):
        assert self._sleeps_for(monkeypatch, code=429, retry_after="600") == [60.0, 60.0]

    def test_retry_after_http_date_falls_back_to_the_doubling_wait(self, monkeypatch):
        slept = self._sleeps_for(monkeypatch, code=503, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
        assert slept == [1.0, 2.0]

    def test_no_retry_after_keeps_the_doubling_wait(self, monkeypatch):
        assert self._sleeps_for(monkeypatch, code=429, retry_after=None) == [1.0, 2.0]


# ===========================================================================
# The historical loader's hook (one non-fatal call per game load)
# ===========================================================================


class TestLoaderHook:
    """``HistoricalDataLoader._ensure_game_player_stats`` over a mocked connection."""

    @staticmethod
    def _loader(*, table_exists: bool = True):
        from unittest.mock import MagicMock

        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        cur = MagicMock()
        cur.fetchone.return_value = ("raw.game_player_stats",) if table_exists else (None,)
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        loader._get_conn = MagicMock(return_value=ctx)
        return loader, conn, cur

    @staticmethod
    def _game_dict(teams) -> dict:
        return {"liveData": {"boxscore": {"teams": teams}}}

    def test_no_box_block_touches_no_db(self):
        loader, conn, _ = self._loader()
        loader._ensure_game_player_stats(1, 2024, "2024-08-15", {"gameData": {}}, 1, 2)
        assert not loader._get_conn.called
        assert not conn.commit.called

    def test_writes_the_rows_and_commits(self, teams, monkeypatch):
        captured: dict = {}

        def fake_persist(cur, rows):
            captured["rows"] = list(rows)
            return len(captured["rows"])

        monkeypatch.setattr("pipeline.etl.etl_historical_loader.persist_sync", fake_persist)
        loader, conn, _ = self._loader()
        loader._ensure_game_player_stats(
            GAME_PK, SEASON, "2024-08-15", self._game_dict(teams), HOME_TEAM, AWAY_TEAM
        )
        assert len(captured["rows"]) == 27
        assert captured["rows"][0].game_date == GAME_DATE
        assert conn.commit.called
        assert not conn.rollback.called
        assert loader._game_player_stats_table_exists is True

    def test_table_absent_warns_and_writes_nothing(self, teams, monkeypatch, caplog):
        called = []
        monkeypatch.setattr(
            "pipeline.etl.etl_historical_loader.persist_sync",
            lambda cur, rows: called.append(1),
        )
        loader, conn, _ = self._loader(table_exists=False)
        with caplog.at_level("WARNING"):
            loader._ensure_game_player_stats(
                GAME_PK, SEASON, "2024-08-15", self._game_dict(teams), HOME_TEAM, AWAY_TEAM
            )
        assert called == []
        assert not conn.commit.called
        assert loader._game_player_stats_table_exists is False
        assert any("Alembic 0023" in r.message for r in caplog.records)

    def test_a_failure_never_raises(self, teams, monkeypatch, caplog):
        def boom(cur, rows):
            raise RuntimeError("db down")

        monkeypatch.setattr("pipeline.etl.etl_historical_loader.persist_sync", boom)
        loader, conn, _ = self._loader()
        with caplog.at_level("WARNING"):
            loader._ensure_game_player_stats(
                GAME_PK, SEASON, "2024-08-15", self._game_dict(teams), HOME_TEAM, AWAY_TEAM
            )
        assert conn.rollback.called
        assert not conn.commit.called
        assert any("SIM-545" in r.message and "db down" in r.message for r in caplog.records)

    def test_prerequisites_call_the_hook(self, teams):
        """The hook runs once per game load, after the lineups."""
        from unittest.mock import MagicMock

        from pipeline.etl.etl_historical_loader import HistoricalDataLoader

        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        for name in (
            "_ensure_venue",
            "_ensure_teams",
            "_ensure_players",
            "_ensure_managers",
            "_ensure_game",
            "_ensure_game_lineups",
            "_ensure_game_player_stats",
        ):
            setattr(loader, name, MagicMock())
        game_dict = {
            "gameData": {
                "datetime": {"officialDate": "2024-08-15"},
                "venue": {"id": 2394},
                "teams": {"home": {"id": HOME_TEAM}, "away": {"id": AWAY_TEAM}},
            },
            "liveData": {"boxscore": {"teams": teams}},
            "_managers": {},
        }
        loader._ensure_prerequisites(GAME_PK, game_dict)
        loader._ensure_game_player_stats.assert_called_once_with(
            GAME_PK, SEASON, "2024-08-15", game_dict, HOME_TEAM, AWAY_TEAM
        )


# ===========================================================================
# scripts/load_official_boxscores.py — the backfill CLI (no network, no DB)
# ===========================================================================

import importlib.util  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CLI_PATH = os.path.join(_REPO_ROOT, "scripts", "load_official_boxscores.py")
_cli_spec = importlib.util.spec_from_file_location("load_official_boxscores", _CLI_PATH)
assert _cli_spec is not None and _cli_spec.loader is not None
cli = importlib.util.module_from_spec(_cli_spec)
sys.modules["load_official_boxscores"] = cli
_cli_spec.loader.exec_module(cli)


class _FakePgConn:
    """asyncpg stand-in: ``fetch`` returns the game refs, ``executemany`` records writes."""

    def __init__(self, game_rows):
        self.game_rows = game_rows
        self.writes: list[tuple[str, list]] = []
        self.closed = False

    async def fetch(self, sql, *params):
        return self.game_rows

    async def executemany(self, sql, batch):
        self.writes.append((sql, list(batch)))

    async def close(self):
        self.closed = True


def _game_row(game_pk: int) -> dict:
    return {
        "game_pk": game_pk,
        "season": SEASON,
        "game_date": GAME_DATE,
        "home_team_id": HOME_TEAM,
        "away_team_id": AWAY_TEAM,
    }


class TestBackfillCli:
    def test_final_games_sql_shapes(self):
        sql = cli.final_games_sql([2024, 2023], 50, [], True)
        assert "status = 'Final'" in sql
        assert "season IN (2024, 2023)" in sql
        assert "NOT EXISTS (SELECT 1 FROM raw.game_player_stats" in sql
        assert "LIMIT 50" in sql
        assert "ORDER BY game_pk" in sql
        sql = cli.final_games_sql([], None, [746437, 1], False)
        assert "game_pk IN (746437, 1)" in sql
        assert "season IN" not in sql
        assert "NOT EXISTS" not in sql
        assert "LIMIT" not in sql

    def test_parse_args_defaults(self):
        args = cli.parse_args(["--seasons", "2024"])
        assert args.only_missing is True
        assert args.sleep == 0.25
        assert args.max_games is None
        assert args.max_consecutive_failures == cli.DEFAULT_MAX_CONSECUTIVE_FAILURES == 5
        args = cli.parse_args(["--game-pks", "1", "2", "--no-only-missing", "--sleep", "0"])
        assert args.only_missing is False
        assert args.game_pks == [1, 2]

    def test_load_game_writes_the_parsed_rows(self, teams):
        class Stub(BoxscoreIngest):
            def _mlb_get(self, path, params=None):
                return {"teams": teams}

        conn = _FakePgConn([])
        ref = cli.GameRef(GAME_PK, SEASON, GAME_DATE, HOME_TEAM, AWAY_TEAM)
        written = asyncio.run(cli.load_game(Stub(), conn, ref))
        assert written == 27
        (sql, batch) = conn.writes[0]
        assert sql == UPSERT_SQL_ASYNCPG
        assert {row[0] for row in batch} == {GAME_PK}

    def test_run_counts_and_never_dies_on_one_failure(self, teams, monkeypatch, caplog):
        calls: list[int] = []

        def fake_get(self, path, params=None):
            game_pk = int(path.split("/")[1])
            calls.append(game_pk)
            if game_pk == 2:
                raise RuntimeError("boom")
            if game_pk == 3:
                return {"teams": {}}  # an empty box: skipped, not a failure
            return {"teams": teams}

        monkeypatch.setattr(BoxscoreIngest, "_mlb_get", fake_get)
        conn = _FakePgConn([_game_row(1), _game_row(2), _game_row(3), _game_row(4)])

        async def fake_connect(dsn):
            return conn

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fake_connect))
        args = cli.parse_args(["--seasons", "2024", "--sleep", "0", "--dsn", "postgresql://x"])
        with caplog.at_level("INFO"):
            rc = asyncio.run(cli.run(args))
        assert rc == 0
        assert calls == [1, 2, 3, 4]
        # games 1 and 4 — the box rows (each game also writes its SIM-427 bullpen listing)
        assert sum(1 for sql, _ in conn.writes if sql == UPSERT_SQL_ASYNCPG) == 2
        assert conn.closed
        final = [r.message for r in caplog.records if "backfill complete" in r.message]
        assert final and "2 games fetched, 54 rows written, 1 games skipped, 1 failures" in final[0]

    def test_run_requires_a_selector(self):
        args = cli.parse_args(["--dsn", "postgresql://x"])
        assert asyncio.run(cli.run(args)) == 2

    @staticmethod
    def _run_with(monkeypatch, fake_get, game_pks: list[int], *extra: str):
        """Run the CLI over ``game_pks`` with ``fake_get`` as the network; return (rc, conn)."""
        monkeypatch.setattr(BoxscoreIngest, "_mlb_get", fake_get)
        conn = _FakePgConn([_game_row(pk) for pk in game_pks])

        async def fake_connect(dsn):
            return conn

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fake_connect))
        argv = ["--seasons", "2024", "--sleep", "0", "--dsn", "postgresql://x", *extra]
        return asyncio.run(cli.run(cli.parse_args(argv))), conn

    def test_run_stops_after_consecutive_failures_and_exits_one(self, monkeypatch, caplog):
        """Every game failing is an outage, not one bad game: stop at the limit, exit 1."""
        calls: list[int] = []

        def always_fails(self, path, params=None):
            calls.append(int(path.split("/")[1]))
            raise RuntimeError("HTTP 429")

        with caplog.at_level("INFO"):
            rc, conn = self._run_with(monkeypatch, always_fails, list(range(1, 21)))
        assert rc == 1
        assert calls == [1, 2, 3, 4, 5]  # the default limit, not all twenty games
        assert conn.writes == []
        assert conn.closed
        assert any("aborting: 5 consecutive game failures" in r.message for r in caplog.records)
        assert any("backfill FAILED" in r.message for r in caplog.records)

    def test_consecutive_limit_zero_never_stops(self, monkeypatch):
        calls: list[int] = []

        def always_fails(self, path, params=None):
            calls.append(int(path.split("/")[1]))
            raise RuntimeError("HTTP 429")

        rc, _ = self._run_with(
            monkeypatch, always_fails, list(range(1, 9)), "--max-consecutive-failures", "0"
        )
        assert calls == list(range(1, 9))
        assert rc == 1  # nothing written + failures: still a failed run

    def test_a_success_resets_the_consecutive_count(self, teams, monkeypatch):
        """Failures split by a good game never reach the limit; rows written → exit 0."""
        calls: list[int] = []

        def flaky(self, path, params=None):
            game_pk = int(path.split("/")[1])
            calls.append(game_pk)
            if game_pk % 3 == 0:
                return {"teams": teams}
            raise RuntimeError("boom")

        rc, conn = self._run_with(
            monkeypatch, flaky, list(range(1, 13)), "--max-consecutive-failures", "3"
        )
        assert rc == 0
        assert calls == list(range(1, 13))
        # games 3, 6, 9, 12 — the box rows; each game also writes its bullpen
        # listing (SIM-427), so count the player-stats upserts only.
        assert sum(1 for sql, _ in conn.writes if sql == UPSERT_SQL_ASYNCPG) == 4

    def test_nothing_written_and_one_failure_exits_one(self, monkeypatch, caplog):
        """A run below the consecutive limit still fails when it loaded nothing."""

        def one_bad_one_empty(self, path, params=None):
            if path.startswith("game/1/"):
                raise RuntimeError("boom")
            return {"teams": {}}  # an empty box: skipped, not a failure

        with caplog.at_level("ERROR"):
            rc, conn = self._run_with(monkeypatch, one_bad_one_empty, [1, 2])
        assert rc == 1
        assert conn.writes == []
        assert any("nothing was written" in r.message for r in caplog.records)

    def test_only_empty_boxes_and_no_failures_exits_zero(self, monkeypatch):
        def empty(self, path, params=None):
            return {"teams": {}}

        rc, conn = self._run_with(monkeypatch, empty, [1, 2])
        assert rc == 0
        assert conn.writes == []
