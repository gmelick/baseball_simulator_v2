"""
tests/unit/test_read_only_routes.py
==========================================
The Data Lab read routes run ONLY on the read-only pool (SIM-566).

The SQL console and the assistant already run on ``app.state.pg_pool_ro``, the
``baseball_ro`` login that can only read ``raw.*`` (SIM-443e). Before this change,
four route groups with fixed queries read through the main pool, whose login can
write: the schema tree, the nine analytics routes, the two player routes and the
data-freshness route. These tests pin the new rule:

* with no read-only pool, every route answers 503 with the shared detail text,
  and the main pool is never touched (a sentinel pool raises on any use);
* with a fake read-only pool, each group answers 200 from canned rows, and every
  query runs on the read-only pool;
* the schema tree caches its answer, so a second read runs no query;
* ``_get_ro_pool`` returns the pool or raises 503, and never falls back;
* a source guard: no route module imports or calls ``_get_pool``;
* a query guard: every table the route modules read is a ``raw`` table or one of
  the two catalog reads, and the typeahead calls ``public.similarity``.

The routers sit on a bare app with ``require_auth`` overridden, so the tests need
no lifespan, no database and no credential.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.auth import require_auth
from api.routes import _common, players, sql_safety
from api.routes._common import RO_POOL_UNAVAILABLE_DETAIL, _get_ro_pool
from api.routes.analytics import router as analytics_router
from api.routes.data_health import router as data_health_router
from api.routes.players import router as players_router
from api.routes.schema_introspect import router as schema_router
from api.routes.sql_runner import router as sql_router

_ROUTES_DIR = Path(__file__).resolve().parents[2] / "api" / "routes"

# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class _SentinelPool:
    """The main pool. Any use of it fails the test."""

    def __init__(self) -> None:
        self.touched = False

    def _fail(self) -> None:
        self.touched = True
        raise AssertionError("a Data Lab read route must never use the main pool")

    def acquire(self) -> Any:
        self._fail()

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        self._fail()

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        self._fail()


_AGG_ROW = {
    "last_ingest_at": "2026-10-17T06:00:00",
    "latest_game_date": "2026-10-16",
    "tracked_entities": 51,
    "total_games": 2472,
    "total_pitches": 712000,
}


def _canned_rows(sql: str) -> list[dict[str, Any]]:
    """Return the canned rows for one query, chosen by a fragment of its SQL.

    An unknown query fails the test, so a new query cannot pass unseen.
    """
    if "pg_class" in sql:
        return [{"n": 6588765}]
    if "information_schema.columns" in sql:
        return [
            {"table_name": "games", "column_name": "game_pk", "data_type": "integer"},
            {"table_name": "games", "column_name": "season", "data_type": "integer"},
            {"table_name": "pitches", "column_name": "pitcher", "data_type": "integer"},
        ]
    if "similarity(" in sql:
        return [
            {
                "player_id": 592450,
                "name": "Aaron Judge",
                "bats": "R",
                "throws": "R",
                "position": "RF",
            }
        ]
    if "UNION" in sql:
        return [{"season": 2024}, {"season": 2025}]
    if "WHERE player_id = $1" in sql:
        return [
            {
                "player_id": 1,
                "name": "Test Player",
                "bats": "L",
                "throws": "R",
                "position": "SS",
            }
        ]
    if "JOIN raw.players" in sql:
        return [{"player_id": 543037, "name": "Gerrit Cole", "pa": 700, "k_pct": 27.5}]
    if "MIN(game_date)" in sql:
        return [{"games": 2472, "first_date": "2017-04-02", "last_date": "2026-10-01"}]
    if "DISTINCT season FROM raw.games" in sql:
        return [{"season": 2024}, {"season": 2025}]
    if "COUNT(DISTINCT game_pk)" in sql:
        return [
            {"season": 2024, "games": 2430, "pitches": 700000},
            {"season": 2025, "games": 42, "pitches": 12000},
        ]
    raise AssertionError(f"the fake read-only pool has no rows for this query: {sql!r}")


class _FakeRoPool:
    """The read-only pool. It returns canned rows and records each query."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return _canned_rows(sql)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.queries.append(sql)
        if "raw.etl_data_freshness" in sql:
            return dict(_AGG_ROW)
        rows = _canned_rows(sql)
        return rows[0]


def _client(*, ro_pool: Any = None) -> tuple[TestClient, _SentinelPool]:
    """Build a fresh app with the five routers. The schema cache starts empty."""
    app = FastAPI()
    app.include_router(schema_router)
    app.include_router(analytics_router)
    app.include_router(players_router)
    app.include_router(data_health_router)
    app.include_router(sql_router)
    app.dependency_overrides[require_auth] = lambda: None
    main_pool = _SentinelPool()
    app.state.pg_pool = main_pool
    app.state.pg_pool_ro = ro_pool
    return TestClient(app), main_pool


# ---------------------------------------------------------------------------
# 1. No read-only pool: every route answers 503
# ---------------------------------------------------------------------------

_GET_PATHS = [
    "/api/schema",
    "/api/analytics/kpis",
    "/api/analytics/by-season",
    "/api/analytics/pitch-type-mix?season=2025",
    "/api/analytics/pa-outcomes?season=2025",
    "/api/analytics/count-state?season=2025",
    "/api/analytics/velo-distribution?season=2025",
    "/api/analytics/zone?season=2025",
    "/api/analytics/leaderboard/pitchers?season=2025",
    "/api/analytics/leaderboard/batters?season=2025",
    "/api/players/search?q=judge",
    "/api/players/1",
    "/api/data/freshness",
]


@pytest.mark.parametrize("path", [*_GET_PATHS, "POST /api/sql/run"])
def test_each_route_answers_503_without_the_read_only_pool(path: str) -> None:
    client, main_pool = _client(ro_pool=None)
    if path.startswith("POST "):
        resp = client.post(path[5:], json={"sql": "SELECT count(*) FROM raw.pitches"})
    else:
        resp = client.get(path)
    assert resp.status_code == 503, f"{path}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == RO_POOL_UNAVAILABLE_DETAIL
    assert main_pool.touched is False


# ---------------------------------------------------------------------------
# 2. A read-only pool: each group answers from it
# ---------------------------------------------------------------------------


def test_kpis_read_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/analytics/kpis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_pitches"] == 6588765
    assert body["total_pitches_estimated"] is True
    assert body["total_games"] == 2472
    assert body["seasons"] == [2024, 2025]
    assert body["first_date"] == "2017-04-02"
    assert body["last_date"] == "2026-10-01"
    assert len(ro.queries) == 3
    assert main_pool.touched is False


def test_by_season_reads_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/analytics/by-season")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"season": 2024, "games": 2430, "pitches": 700000},
        {"season": 2025, "games": 42, "pitches": 12000},
    ]
    assert len(ro.queries) == 1
    assert main_pool.touched is False


def test_pitcher_leaderboard_reads_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/analytics/leaderboard/pitchers?season=2025&limit=5")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"player_id": 543037, "name": "Gerrit Cole", "pa": 700, "k_pct": 27.5}]
    assert len(ro.queries) == 1
    assert main_pool.touched is False


def test_player_search_ranks_on_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/players/search?q=judge&limit=5")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {
            "player_id": 592450,
            "name": "Aaron Judge",
            "bats": "R",
            "throws": "R",
            "position": "RF",
        }
    ]
    # One query: the ranked search ran, and the ILIKE fallback did not.
    assert len(ro.queries) == 1
    assert "public.similarity(" in ro.queries[0]
    assert main_pool.touched is False


def test_player_detail_reads_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/players/1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "player_id": 1,
        "name": "Test Player",
        "bats": "L",
        "throws": "R",
        "position": "SS",
        "seasons": [2024, 2025],
    }
    assert len(ro.queries) == 2
    assert main_pool.touched is False


def test_schema_tree_reads_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/schema")
    assert resp.status_code == 200, resp.text
    schemas = resp.json()["schemas"]
    assert [s["name"] for s in schemas] == ["raw"]
    tables = {t["name"]: [c["name"] for c in t["columns"]] for t in schemas[0]["tables"]}
    assert tables == {"games": ["game_pk", "season"], "pitches": ["pitcher"]}
    assert len(ro.queries) == 1
    assert main_pool.touched is False


def test_freshness_reads_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    resp = client.get("/api/data/freshness")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_games"] == 2472
    assert body["total_pitches"] == 712000
    assert body["has_data"] is True
    assert [s["season"] for s in body["seasons"]] == [2024, 2025]
    assert len(ro.queries) == 2
    assert main_pool.touched is False


# ---------------------------------------------------------------------------
# 3. The schema cache
# ---------------------------------------------------------------------------


def test_second_schema_read_runs_no_query() -> None:
    ro = _FakeRoPool()
    client, main_pool = _client(ro_pool=ro)
    first = client.get("/api/schema")
    assert first.status_code == 200, first.text
    assert len(ro.queries) == 1
    second = client.get("/api/schema")
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert len(ro.queries) == 1
    assert main_pool.touched is False


# ---------------------------------------------------------------------------
# 4. The helper itself
# ---------------------------------------------------------------------------


def _request(**state: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_get_ro_pool_returns_the_read_only_pool() -> None:
    ro = _FakeRoPool()
    assert _get_ro_pool(_request(pg_pool_ro=ro, pg_pool=_SentinelPool())) is ro


@pytest.mark.parametrize(
    "state",
    [
        {"pg_pool_ro": None, "pg_pool": _SentinelPool()},
        {"pg_pool": _SentinelPool()},
    ],
    ids=["pool-is-none", "pool-is-missing"],
)
def test_get_ro_pool_raises_503_and_never_falls_back(state: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as info:
        _get_ro_pool(_request(**state))
    assert info.value.status_code == 503
    assert info.value.detail == RO_POOL_UNAVAILABLE_DETAIL
    assert state["pg_pool"].touched is False


def test_the_detail_text_has_one_home() -> None:
    assert sql_safety.RO_POOL_UNAVAILABLE_DETAIL is _common.RO_POOL_UNAVAILABLE_DETAIL
    assert "RO_POOL_UNAVAILABLE_DETAIL" in sql_safety.__all__


# ---------------------------------------------------------------------------
# 5. The source guard
# ---------------------------------------------------------------------------

_READ_MODULES = [
    "analytics.py",
    "players.py",
    "schema_introspect.py",
    "data_health.py",
    "sql_runner.py",
]


def _names_get_pool(node: ast.AST) -> bool:
    """Return True when the node is the bare name or the attribute ``_get_pool``."""
    if isinstance(node, ast.Name):
        return node.id == "_get_pool"
    if isinstance(node, ast.Attribute):
        return node.attr == "_get_pool"
    return False


@pytest.mark.parametrize("module", _READ_MODULES)
def test_route_module_never_names_the_main_pool_helper(module: str) -> None:
    tree = ast.parse((_ROUTES_DIR / module).read_text(encoding="utf-8"))
    offenders: list[int] = []
    calls_ro = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "_get_pool" for alias in node.names):
                offenders.append(node.lineno)
        elif _names_get_pool(node):
            offenders.append(node.lineno)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "_get_ro_pool":
                calls_ro = True
    assert offenders == [], f"{module} names _get_pool at lines {offenders}"
    assert calls_ro, f"{module} never calls _get_ro_pool"


# ---------------------------------------------------------------------------
# 6. The query guard
# ---------------------------------------------------------------------------

_QUERY_MODULES = ["analytics.py", "players.py", "schema_introspect.py", "data_health.py"]
_TARGET = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_ALLOWED_CATALOGS = {"pg_class", "information_schema.columns"}
_RAW_TABLE = re.compile(r"raw\.[a-z_][a-z0-9_]*")


def _sql_strings(tree: ast.Module) -> list[str]:
    """Return every string constant of the module except the docstrings.

    The SQL lives in string constants (plain strings and the parts of an
    f-string). A docstring is prose, so the guard skips it; a comment is not in
    the tree at all. The guard reads the SQL in any case, so a lower-case
    ``from sim.x`` cannot slip past it.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("module", _QUERY_MODULES)
def test_route_module_reads_only_raw_tables(module: str) -> None:
    tree = ast.parse((_ROUTES_DIR / module).read_text(encoding="utf-8"))
    targets = [t for text in _sql_strings(tree) for t in _TARGET.findall(text)]
    assert targets, f"{module}: the guard found no FROM or JOIN target"
    bad = [
        t
        for t in targets
        if t.lower() not in _ALLOWED_CATALOGS and not _RAW_TABLE.fullmatch(t.lower())
    ]
    assert bad == [], f"{module} reads a table outside raw: {bad}"


def test_typeahead_calls_the_schema_qualified_similarity() -> None:
    # The read-only pool's search_path is raw, and pg_trgm lives in public.
    assert "public.similarity(" in players._SEARCH_TRGM
    assert re.search(r"(?<!public\.)\bsimilarity\(", players._SEARCH_TRGM) is None
