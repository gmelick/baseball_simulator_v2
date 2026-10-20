"""SIM-427 — the per-game bullpen listing (Alembic 0025, ``raw.game_bullpen``).

The MLB box lists, per side, the pitchers who did not pitch (``bullpen``) and
those who did (``pitchers``, the starter first). The parser turns the two
lists into one row per arm; the persisters write them; the backfill script's
query can select the games that lack listing rows. No network, no DB.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys

import pytest

from pipeline.etl.boxscore_ingest import (
    BULLPEN_COLUMNS,
    BULLPEN_UPSERT_SQL_ASYNCPG,
    BULLPEN_UPSERT_SQL_PSYCOPG2,
    LISTED_BULLPEN,
    LISTED_PITCHED,
    LISTED_STARTED,
    BullpenListing,
    parse_bullpen_listing,
    persist_bullpen,
    persist_bullpen_sync,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_FIXTURE = os.path.join(_REPO_ROOT, "tests", "fixtures", "mlb", "boxscore_746437.json")
GAME_PK = 746437
HOME_TEAM = 116
AWAY_TEAM = 136


@pytest.fixture(scope="module")
def teams() -> dict:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["teams"]


class TestParse:
    def test_every_listed_arm_gets_one_row_with_its_list(self, teams):
        rows = parse_bullpen_listing(teams, game_pk=GAME_PK)
        home = [r for r in rows if r.side == "home"]
        away = [r for r in rows if r.side == "away"]
        assert len(home) == len(teams["home"]["bullpen"]) + len(teams["home"]["pitchers"])
        assert len(away) == len(teams["away"]["bullpen"]) + len(teams["away"]["pitchers"])
        by_id = {r.pitcher_id: r for r in home}
        assert by_id[teams["home"]["pitchers"][0]].listed == LISTED_STARTED
        for pid in teams["home"]["pitchers"][1:]:
            assert by_id[pid].listed == LISTED_PITCHED
        for pid in teams["home"]["bullpen"]:
            assert by_id[pid].listed == LISTED_BULLPEN
        assert {r.team_id for r in home} == {teams["home"]["team"]["id"]}
        assert all(r.game_pk == GAME_PK for r in rows)

    def test_the_pitchers_reading_wins_over_a_duplicate_in_bullpen(self):
        teams = {
            "home": {"team": {"id": 1}, "pitchers": [10, 11], "bullpen": [11, 12]},
            "away": {"team": {"id": 2}, "pitchers": [20], "bullpen": []},
        }
        rows = {r.pitcher_id: r.listed for r in parse_bullpen_listing(teams, game_pk=5)}
        assert rows == {
            10: LISTED_STARTED,
            11: LISTED_PITCHED,
            12: LISTED_BULLPEN,
            20: LISTED_STARTED,
        }

    def test_the_callers_team_ids_are_the_fallback(self):
        teams = {"home": {"pitchers": [10], "bullpen": [12]}, "away": {"pitchers": [20]}}
        rows = parse_bullpen_listing(teams, game_pk=5, home_team_id=7, away_team_id=8)
        assert {(r.pitcher_id, r.team_id) for r in rows} == {(10, 7), (12, 7), (20, 8)}

    def test_a_side_with_no_team_id_writes_nothing(self):
        teams = {"home": {"pitchers": [10]}, "away": {"team": {"id": 2}, "pitchers": [20]}}
        rows = parse_bullpen_listing(teams, game_pk=5)
        assert [r.pitcher_id for r in rows] == [20]

    def test_row_shape_matches_the_insert_order(self):
        r = BullpenListing(game_pk=1, pitcher_id=2, team_id=3, side="home", listed="started")
        assert BULLPEN_COLUMNS == ("game_pk", "pitcher_id", "team_id", "side", "listed")
        assert r.as_tuple() == (1, 2, 3, "home", "started")


class _FakeAsyncConn:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    async def executemany(self, sql, batch):
        self.calls.append((sql, list(batch)))


class TestPersist:
    def test_async_persist_writes_every_row_once(self, teams):
        rows = parse_bullpen_listing(teams, game_pk=GAME_PK)
        conn = _FakeAsyncConn()
        n = asyncio.run(persist_bullpen(conn, rows))
        assert n == len(rows)
        sql, batch = conn.calls[0]
        assert sql == BULLPEN_UPSERT_SQL_ASYNCPG
        assert "ON CONFLICT (game_pk, pitcher_id) DO UPDATE" in sql
        assert batch[0] == rows[0].as_tuple()

    def test_empty_writes_nothing(self):
        conn = _FakeAsyncConn()
        assert asyncio.run(persist_bullpen(conn, [])) == 0
        assert conn.calls == []
        assert persist_bullpen_sync(object(), []) == 0

    def test_sync_persist_uses_execute_batch(self, teams, monkeypatch):
        captured: dict = {}

        def fake_execute_batch(cur, sql, batch):
            captured["sql"] = sql
            captured["batch"] = list(batch)

        import psycopg2.extras

        monkeypatch.setattr(psycopg2.extras, "execute_batch", fake_execute_batch)
        rows = parse_bullpen_listing(teams, game_pk=GAME_PK)
        assert persist_bullpen_sync(object(), rows) == len(rows)
        assert captured["sql"] == BULLPEN_UPSERT_SQL_PSYCOPG2
        assert captured["batch"][-1] == rows[-1].as_tuple()


def _load_backfill_script():
    path = os.path.join(_REPO_ROOT, "scripts", "load_official_boxscores.py")
    spec = importlib.util.spec_from_file_location("load_official_boxscores", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["load_official_boxscores"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestBackfillQuery:
    def test_only_missing_can_target_the_bullpen_table(self):
        mod = _load_backfill_script()
        sql = mod.final_games_sql([2024], None, [], True, missing_table="raw.game_bullpen")
        assert "NOT EXISTS (SELECT 1 FROM raw.game_bullpen s" in sql
        default = mod.final_games_sql([2024], None, [], True)
        assert "raw.game_player_stats s" in default

    def test_bullpen_only_skips_the_player_stats_upsert(self, teams):
        mod = _load_backfill_script()

        class _Ingest:
            def fetch_boxscore(self, game_pk):
                return teams

        written: list[str] = []

        class _Conn:
            async def executemany(self, sql, batch):
                written.append(sql)

        ref = mod.GameRef(
            game_pk=GAME_PK,
            season=2024,
            game_date=mod.date(2024, 8, 15),
            home_team_id=HOME_TEAM,
            away_team_id=AWAY_TEAM,
        )
        n = asyncio.run(mod.load_game(_Ingest(), _Conn(), ref, bullpen_only=True))
        assert n == len(teams["home"]["bullpen"]) + len(teams["home"]["pitchers"]) + len(
            teams["away"]["bullpen"]
        ) + len(teams["away"]["pitchers"])
        assert written == [BULLPEN_UPSERT_SQL_ASYNCPG]
        written.clear()
        asyncio.run(mod.load_game(_Ingest(), _Conn(), ref))
        # the box rows first (their count is the run summary's), the listing second
        assert written[1] == BULLPEN_UPSERT_SQL_ASYNCPG and len(written) == 2
