"""The existence probe (``db/introspect.py``), on a real in-memory DuckDB.

The Postgres helpers run on a fake cursor and a fake asyncpg connection that
answer the catalog query, so no database is needed.
"""

from __future__ import annotations

import asyncio

import pytest

from db import introspect

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def con():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA derived")
    conn.execute("CREATE TABLE derived.metrics (player_id INTEGER, asof_date DATE)")
    yield conn
    conn.close()


def test_duck_columns_names_every_column(con) -> None:
    assert introspect.duck_columns(con, "derived", "metrics") == {"player_id", "asof_date"}


def test_duck_columns_of_a_missing_table_is_empty(con) -> None:
    assert introspect.duck_columns(con, "derived", "absent") == set()
    assert introspect.duck_columns(con, "derived", "metrics", this_file_only=True) == {
        "player_id",
        "asof_date",
    }


def test_duck_table_exists(con) -> None:
    assert introspect.duck_table_exists(con, "derived", "metrics") is True
    assert introspect.duck_table_exists(con, "sim", "metrics") is False


def test_duck_relation_readable(con) -> None:
    assert introspect.duck_relation_readable(con, "derived.metrics") is True
    assert introspect.duck_relation_readable(con, "pg.raw.play_events") is False


@pytest.mark.parametrize("bad", ["metrics; DROP TABLE x", "a'b", "", "1abc"])
def test_a_name_that_is_not_an_identifier_is_refused(con, bad: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        introspect.duck_columns(con, "derived", bad)


class _Cursor:
    def __init__(self, answer):
        self.answer = answer
        self.sql: list[str] = []

    def execute(self, sql: str) -> None:
        self.sql.append(sql)

    def fetchone(self):
        return self.answer


def test_pg_column_exists_reads_the_catalog() -> None:
    cur = _Cursor((1,))
    assert introspect.pg_column_exists(cur, "raw", "game_lineups", "source") is True
    assert "information_schema.columns" in cur.sql[0]
    assert "column_name = 'source'" in cur.sql[0]
    assert introspect.pg_column_exists(_Cursor(None), "raw", "game_lineups", "source") is False


def test_pg_relation_exists_reads_to_regclass() -> None:
    cur = _Cursor(("raw.play_events",))
    assert introspect.pg_relation_exists(cur, "raw.play_events") is True
    assert cur.sql == ["SELECT to_regclass('raw.play_events')"]
    assert introspect.pg_relation_exists(_Cursor((None,)), "raw.play_events") is False
    assert introspect.pg_relation_exists(_Cursor(None), "raw.play_events") is False


def test_pg_column_exists_async() -> None:
    class _Db:
        def __init__(self, value):
            self.value = value

        async def fetchval(self, sql: str):
            assert "information_schema.columns" in sql
            return self.value

    assert asyncio.run(introspect.pg_column_exists_async(_Db(1), "raw", "games", "x")) is True
    assert asyncio.run(introspect.pg_column_exists_async(_Db(None), "raw", "games", "x")) is False
