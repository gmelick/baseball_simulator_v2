"""
tests/unit/test_sim443_sql_console_pool.py
==========================================
SIM-443e — the SQL console and the assistant run ONLY on the read-only pool.

The console (``POST /api/sql/run``) and the assistant (``POST
/api/assistant/ask``) used to fall back to the main pool, whose role can write,
when ``app.state.pg_pool_ro`` was missing. These tests pin the new rule:

* with no read-only pool, both routes answer 503 and the main pool is never
  touched (a sentinel pool raises if anything acquires it);
* with a read-only pool, the console runs the query on it, inside a read-only
  transaction;
* a query that reads a system catalog is rejected with 400 before any pool is
  touched (the relation allowlist, SIM-443d);
* ``redact_dsn`` masks the password in the one boot log line.

The routers are mounted on a bare app with ``require_auth`` overridden, so no
lifespan, database or credential is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import require_auth
from api.routes.ai_assistant import router as assistant_router
from api.routes.sql_runner import router as sql_router
from api.routes.sql_safety import RO_POOL_UNAVAILABLE_DETAIL
from api.state import redact_dsn

# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _SentinelPool:
    """The main pool. Any use of it fails the test."""

    def __init__(self) -> None:
        self.touched = False

    def acquire(self) -> Any:
        self.touched = True
        raise AssertionError("user SQL must never run on the main pool")

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        self.touched = True
        raise AssertionError("user SQL must never run on the main pool")


class _Attr:
    def __init__(self, name: str) -> None:
        self.name = name


class _Stmt:
    def __init__(self, columns: list[str], rows: list[list]) -> None:
        self._columns = columns
        self._rows = rows

    def get_attributes(self) -> list[_Attr]:
        return [_Attr(c) for c in self._columns]

    async def fetch(self) -> list[list]:
        return self._rows


class _Txn:
    def __init__(self, conn: _FakeConn, readonly: bool) -> None:
        conn.readonly_seen = readonly

    async def __aenter__(self) -> _Txn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.readonly_seen: bool | None = None
        self.prepared_sql: str | None = None

    def transaction(self, *, readonly: bool = False, **_: Any) -> _Txn:
        return _Txn(self, readonly)

    async def execute(self, sql: str) -> None:
        return None

    async def prepare(self, sql: str) -> _Stmt:
        self.prepared_sql = sql
        return _Stmt(["n"], [[42]])


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeRoPool:
    """The read-only pool. Records whether it ran a query."""

    def __init__(self) -> None:
        self.conn = _FakeConn()
        self.acquired = 0

    def acquire(self) -> _Acquire:
        self.acquired += 1
        return _Acquire(self.conn)


def _client(*, ro_pool: Any = None, assistant: bool = False) -> tuple[TestClient, _SentinelPool]:
    app = FastAPI()
    app.include_router(sql_router)
    app.include_router(assistant_router)
    app.dependency_overrides[require_auth] = lambda: None
    main_pool = _SentinelPool()
    app.state.pg_pool = main_pool
    app.state.pg_pool_ro = ro_pool
    app.state.anthropic_client = object() if assistant else None
    return TestClient(app), main_pool


# ---------------------------------------------------------------------------
# The SQL console
# ---------------------------------------------------------------------------


def test_console_answers_503_without_the_read_only_pool() -> None:
    client, main_pool = _client(ro_pool=None)
    resp = client.post("/api/sql/run", json={"sql": "SELECT count(*) FROM raw.pitches"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == RO_POOL_UNAVAILABLE_DETAIL
    assert main_pool.touched is False


def test_console_runs_on_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.post("/api/sql/run", json={"sql": "SELECT count(*) AS n FROM raw.pitches"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["columns"] == ["n"]
    assert body["rows"] == [[42]]
    assert ro.acquired == 1
    assert ro.conn.readonly_seen is True
    assert main_pool.touched is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_authid",
        "SELECT * FROM pg_catalog.pg_stat_activity",
        "SELECT * FROM sim.sim_runs",
        "SELECT * FROM information_schema.columns",
    ],
)
def test_console_rejects_a_system_table_before_the_database(sql: str) -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.post("/api/sql/run", json={"sql": sql})
    assert resp.status_code == 400
    assert "only raw.* tables may be read" in resp.json()["detail"]
    assert ro.acquired == 0
    assert main_pool.touched is False


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------


def test_assistant_ask_answers_503_without_the_read_only_pool() -> None:
    client, main_pool = _client(ro_pool=None, assistant=True)
    resp = client.post("/api/assistant/ask", json={"question": "Who threw the most pitches?"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == RO_POOL_UNAVAILABLE_DETAIL
    assert main_pool.touched is False


def test_assistant_ask_without_a_client_still_says_not_configured() -> None:
    client, _main = _client(ro_pool=None, assistant=False)
    resp = client.post("/api/assistant/ask", json={"question": "anything"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_assistant_status_works_without_the_read_only_pool() -> None:
    client, _main = _client(ro_pool=None, assistant=False)
    resp = client.get("/api/assistant/status")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_assistant_system_prompt_names_the_raw_only_rule() -> None:
    from api.routes.ai_assistant import _SYSTEM_PROMPT

    assert "always write `raw.<table>`" in _SYSTEM_PROMPT
    assert "unqualified table name is rejected" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# redact_dsn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://baseball_ro:s3cret@db:5432/baseball_sim",
            "postgresql://baseball_ro:***@db:5432/baseball_sim",
        ),
        (
            "postgresql://baseball_ro:s3cret@db/baseball_sim?sslmode=require&password=x",
            "postgresql://baseball_ro:***@db/baseball_sim",
        ),
        (
            "postgresql://baseball_ro@db:5432/baseball_sim",
            "postgresql://baseball_ro@db:5432/baseball_sim",
        ),
        ("not a dsn", "<redacted>"),
        ("", "<redacted>"),
        ("postgresql://u:p@db:notaport/x", "<redacted>"),
    ],
)
def test_redact_dsn(dsn: str, expected: str) -> None:
    out = redact_dsn(dsn)
    assert out == expected
    assert "s3cret" not in out
