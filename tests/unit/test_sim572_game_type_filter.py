"""
tests/unit/test_sim572_game_type_filter.py
==========================================
The Data Lab reads the regular season alone by default (the Data Lab game-type
filter, SIM-572).

The Data Lab is the web app's analytics area. Its routes bounded each read with
``season = $1`` alone. A season of ``raw.pitches`` holds the regular season and
the postseason, so the leaderboards and the league rates mixed the two. In 2025
the postseason holds 47 games and 14,085 of the 723,055 clean pitches. The
batter board showed Shohei Ohtani with 63 home runs; his regular season ended
at 55.

``raw.games.game_type`` marks the type of each game: 'R' is the regular season,
and 'F', 'D', 'L' and 'W' are the four postseason rounds. Each of the seven
routes that take ``season`` now takes ``game_type``. 'R', the default, keeps the
pitches of regular-season games. 'all' adds no filter, so it reads every game of
the season, as the routes did before. The filter is a semi-join on ``game_pk``:
it keeps a pitch when its game has the type. The two coverage routes (kpis,
by-season) describe the whole store and take no game type.

These tests pin the rule:

* every route that takes ``season`` takes ``game_type``, typed 'R' or 'all',
  with 'R' the default; the coverage routes take neither;
* the OpenAPI schema lists the parameter on the seven paths;
* each route's captured SQL carries the filter by default, with the type as a
  query argument, and drops it with 'all';
* an unknown game type answers 422 and runs no query;
* DuckDB runs each route's own SQL over a regular-season game and a postseason
  game: the default reads the first game alone, and 'all' reads both.

No test needs a database, the network or the app's lifespan.
"""

from __future__ import annotations

import inspect
import re
import typing
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.auth import require_auth
from api.routes import analytics

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: The filter that the default adds, with the column prefix and the argument
#: number left open.
_FILTER = re.compile(
    r"(?P<prefix>(?:p\.)?)game_pk IN \(SELECT g\.game_pk FROM raw\.games g "
    r"WHERE g\.game_type = \$(?P<n>\d+)\)"
)

#: The seven routes that read the pitches of a season, by function name.
_SEASON_ROUTES = (
    "pitch_type_mix",
    "pa_outcomes",
    "count_state",
    "velo_distribution",
    "zone",
    "pitcher_leaderboard",
    "batter_leaderboard",
)
#: The two coverage routes. They describe the whole store.
_COVERAGE_ROUTES = ("get_kpis", "by_season")


def _api_routes() -> list[APIRoute]:
    """Return every route of the analytics router."""
    return [route for route in analytics.router.routes if isinstance(route, APIRoute)]


def _app(pool: Any) -> FastAPI:
    """Return a bare app with the analytics router and ``pool`` as the read-only pool.

    ``require_auth`` is overridden, so a call needs no lifespan, no database
    and no credential.
    """
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.pg_pool_ro = pool
    return app


class _RecordingPool:
    """A read-only pool that records each query and its arguments.

    It has no ``acquire``, so the routes call ``fetch`` on it directly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return []


def _route_sql(path: str) -> tuple[str, tuple[Any, ...]]:
    """Call one route and return its one query and the query's arguments."""
    pool = _RecordingPool()
    resp = TestClient(_app(pool)).get(path)
    assert resp.status_code == 200, resp.text
    assert len(pool.calls) == 1, pool.calls
    return pool.calls[0]


# ---------------------------------------------------------------------------
# 1. The parameter sits on every season route
# ---------------------------------------------------------------------------


def test_the_router_holds_the_seven_season_routes_and_the_two_coverage_routes() -> None:
    """The router holds nine routes: the seven season routes and two coverage routes.

    A new route fails this test until it joins one of the two lists, so a new
    season route cannot ship without the game type.
    """
    names = sorted(route.endpoint.__name__ for route in _api_routes())
    assert names == sorted(_SEASON_ROUTES + _COVERAGE_ROUTES)


@pytest.mark.parametrize("route", _api_routes(), ids=lambda route: route.path)
def test_a_route_takes_game_type_exactly_when_it_takes_season(route: APIRoute) -> None:
    """A route with ``season`` takes ``game_type``; a route without it takes neither.

    The parameter is typed 'R' or 'all', and 'R' is the default.
    """
    params = inspect.signature(route.endpoint).parameters
    hints = typing.get_type_hints(route.endpoint, include_extras=True)
    if "season" not in params:
        assert route.endpoint.__name__ in _COVERAGE_ROUTES
        assert "game_type" not in params
        return
    assert route.endpoint.__name__ in _SEASON_ROUTES
    assert "game_type" in params, f"{route.path} takes season but no game_type"
    assert params["game_type"].default == "R"
    assert params["game_type"].default == analytics.REGULAR_SEASON
    base, *_ = typing.get_args(hints["game_type"])
    assert typing.get_args(base) == ("R", "all")


def test_the_openapi_schema_lists_the_parameter_on_the_seven_paths() -> None:
    """The schema shows ``game_type`` on each season path, with its two values.

    The coverage paths list no such parameter.
    """
    schema = _app(_RecordingPool()).openapi()
    for route in _api_routes():
        params = {p["name"]: p for p in schema["paths"][route.path]["get"].get("parameters", [])}
        if route.endpoint.__name__ in _COVERAGE_ROUTES:
            assert "game_type" not in params, route.path
            continue
        param = params["game_type"]
        assert param["in"] == "query"
        assert param["required"] is False
        assert param["schema"]["enum"] == ["R", "all"]
        assert param["schema"]["default"] == "R"
        assert "regular season" in param["description"]


# ---------------------------------------------------------------------------
# 2. The filter helper
# ---------------------------------------------------------------------------


def test_the_helper_adds_the_filter_and_its_argument_for_the_regular_season() -> None:
    """With 'R' the helper appends the type and a filter that reads it."""
    where = ["p.season = $1"]
    args: list[Any] = [2025, "L"]
    analytics._filter_game_type(where, args, "R", "p.")
    assert args == [2025, "L", "R"]
    assert where == [
        "p.season = $1",
        "p.game_pk IN (SELECT g.game_pk FROM raw.games g WHERE g.game_type = $3)",
    ]


def test_the_helper_adds_nothing_for_all() -> None:
    """With 'all' the helper leaves the filter list and the arguments alone."""
    where = ["season = $1"]
    args: list[Any] = [2025]
    analytics._filter_game_type(where, args, "all")
    assert where == ["season = $1"]
    assert args == [2025]


def test_the_filter_reads_raw_games_alone() -> None:
    """The filter's one table is ``raw.games``, which the read-only role can read."""
    where: list[str] = []
    analytics._filter_game_type(where, [], "R")
    assert re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", where[0]) == ["raw.games"]


# ---------------------------------------------------------------------------
# 3. The captured SQL
# ---------------------------------------------------------------------------

#: Each season route: a path, the arguments before the game type, the arguments
#: after it, and the column prefix of the filter. The leaderboards join
#: ``raw.players``, so their filter carries the ``p.`` prefix.
_SQL_CASES = [
    pytest.param(
        "/api/analytics/pitch-type-mix?season=2025&pitcher=1&stand=L",
        (2025, 1, "L"),
        (),
        "",
        id="pitch-type-mix",
    ),
    pytest.param(
        "/api/analytics/pa-outcomes?season=2025&batter=2&p_throws=R",
        (2025, 2, "R"),
        (),
        "",
        id="pa-outcomes",
    ),
    pytest.param(
        "/api/analytics/count-state?season=2025&pitcher=1&batter=2",
        (2025, 1, 2),
        (),
        "",
        id="count-state",
    ),
    pytest.param(
        "/api/analytics/velo-distribution?season=2025&pitcher=1&pitch_type=FF",
        (2025, 1, "FF"),
        (),
        "",
        id="velo-distribution",
    ),
    pytest.param(
        "/api/analytics/zone?season=2025&pitcher=1",
        (2025, 1),
        (),
        "",
        id="zone",
    ),
    pytest.param(
        "/api/analytics/leaderboard/pitchers?season=2025&min_pa=10&limit=5",
        (2025,),
        (10, 5),
        "p.",
        id="pitcher-board",
    ),
    pytest.param(
        "/api/analytics/leaderboard/batters?season=2025&min_pa=10&limit=5&vs_hand=L",
        (2025, "L"),
        (10, 5),
        "p.",
        id="batter-board",
    ),
]


@pytest.mark.parametrize(("path", "before", "after", "prefix"), _SQL_CASES)
def test_the_default_sql_keeps_the_regular_season(
    path: str, before: tuple[Any, ...], after: tuple[Any, ...], prefix: str
) -> None:
    """By default the SQL carries one filter, and its argument is 'R'.

    The filter reads the argument right after the route's own WHERE arguments.
    The leaderboards' ``min_pa`` and ``limit`` follow it.
    """
    sql, args = _route_sql(path)
    assert args == (*before, "R", *after)
    found = list(_FILTER.finditer(sql))
    assert len(found) == 1, sql
    assert found[0]["prefix"] == prefix
    assert int(found[0]["n"]) == len(before) + 1


@pytest.mark.parametrize(("path", "before", "after", "prefix"), _SQL_CASES)
def test_all_sql_carries_no_filter(
    path: str, before: tuple[Any, ...], after: tuple[Any, ...], prefix: str
) -> None:
    """With 'all' the SQL names no ``raw.games`` and takes no game-type argument."""
    sql, args = _route_sql(f"{path}&game_type=all")
    assert args == (*before, *after)
    assert "raw.games" not in sql
    assert "game_type" not in sql


def test_the_explicit_regular_season_equals_the_default() -> None:
    """``game_type=R`` sends the same query and arguments as no parameter."""
    path = "/api/analytics/leaderboard/batters?season=2025"
    assert _route_sql(f"{path}&game_type=R") == _route_sql(path)


@pytest.mark.parametrize("value", ["P", "W", "r", "ALL", "postseason", ""])
def test_an_unknown_game_type_answers_422_and_runs_no_query(value: str) -> None:
    """A value other than 'R' or 'all' fails validation, so no query runs."""
    pool = _RecordingPool()
    resp = TestClient(_app(pool)).get(f"/api/analytics/zone?season=2025&game_type={value}")
    assert resp.status_code == 422, resp.text
    assert pool.calls == []


# ---------------------------------------------------------------------------
# 4. DuckDB runs each route's own SQL
# ---------------------------------------------------------------------------

_PITCHES_DDL = (
    "CREATE TABLE raw.pitches (game_pk INTEGER, season INTEGER, balls INTEGER, "
    "strikes INTEGER, zone INTEGER, pitcher INTEGER, batter INTEGER, stand VARCHAR, "
    "p_throws VARCHAR, pitch_type VARCHAR, release_speed DOUBLE, "
    "break_vertical_induced DOUBLE, break_horizontal DOUBLE, release_spin_rate DOUBLE, "
    "type VARCHAR, events VARCHAR, launch_speed DOUBLE, data_quality_flag BOOLEAN)"
)
_GAMES_DDL = "CREATE TABLE raw.games (game_pk INTEGER, season INTEGER, game_type VARCHAR)"
_PLAYERS_DDL = "CREATE TABLE raw.players (player_id INTEGER, full_name VARCHAR)"

#: Game 1 is a 2025 regular-season game. Game 2 is a 2025 World Series game.
_GAMES: list[tuple[int, int, str]] = [(1, 2025, "R"), (2, 2025, "W")]

#: Each game's ten pitches: (code, event, exit velocity) and a count of rows.
#: Game 1 throws ten four-seam fastballs at 95.0 mph; game 2 ten sliders at 88.0.
_GAME_PITCHES: dict[int, tuple[str, float, list[tuple[str, str | None, float | None, int]]]] = {
    1: (
        "FF",
        95.0,
        [
            ("E", "home_run", 101.0, 1),
            ("X", "field_out", 90.0, 1),
            ("S", "strikeout", None, 1),
            ("B", None, None, 7),
        ],
    ),
    2: (
        "SL",
        88.0,
        [
            ("E", "home_run", 104.0, 1),
            ("S", "strikeout", None, 1),
            ("B", None, None, 8),
        ],
    ),
}


def _seed_rows() -> list[tuple[Any, ...]]:
    """Return the twenty seed pitches.

    Each pitch: season 2025, count 0-0, zone 5, pitcher 1, batter 2, a
    left-handed batter, a right-handed pitcher and a clean quality flag.
    """
    rows: list[tuple[Any, ...]] = []
    for game_pk, (pitch_type, speed, plan) in _GAME_PITCHES.items():
        for code, event, launch, count in plan:
            for _ in range(count):
                rows.append(
                    (game_pk, 2025, 0, 0, 5, 1, 2, "L", "R", pitch_type, speed)
                    + (None, None, None, code, event, launch, False)
                )
    return rows


def _run_in_duckdb(sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Run a route's captured SQL, unchanged, over the seed rows in DuckDB.

    DuckDB accepts the route's Postgres text as it stands: the ``$n``
    parameters, ``COUNT(*) FILTER``, the subquery, ``::numeric``, ``::float8``
    and ``NULLS LAST``. ``raw.games.game_type`` is VARCHAR(2) on the live
    table, so the bare seed code 'R' compares as Postgres compares it.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute(_PITCHES_DDL)
        con.execute(_GAMES_DDL)
        con.execute(_PLAYERS_DDL)
        con.executemany(f"INSERT INTO raw.pitches VALUES ({', '.join(['?'] * 18)})", _seed_rows())
        con.executemany("INSERT INTO raw.games VALUES (?, ?, ?)", _GAMES)
        con.executemany(
            "INSERT INTO raw.players VALUES (?, ?)", [(1, "Test Pitcher"), (2, "Test Batter")]
        )
        cur = con.execute(sql, list(args))
        names = [column[0] for column in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def _read(path: str) -> list[dict[str, Any]]:
    """Return the rows of one route's SQL over the seed."""
    return _run_in_duckdb(*_route_sql(path))


def test_the_seed_holds_ten_pitches_a_game() -> None:
    """The seed holds twenty pitches, ten in each game."""
    rows = _seed_rows()
    assert len(rows) == 20
    assert sum(1 for row in rows if row[0] == 1) == 10


def test_the_batter_board_counts_the_postseason_home_run_only_with_all() -> None:
    """The batter board reads one home run by default and two with 'all'.

    This is Ohtani's case in small: 55 home runs in the regular season, 63 with
    the postseason. The plate appearances are the rows with an event: E, X and
    S in game 1 (3); E and S in game 2 (2 more).
    """
    path = "/api/analytics/leaderboard/batters?season=2025&min_pa=0"
    regular = _read(path)
    assert [(r["player_id"], r["pa"], r["hr"], r["max_ev"]) for r in regular] == [(2, 3, 1, 101.0)]
    every = _read(f"{path}&game_type=all")
    assert [(r["player_id"], r["pa"], r["hr"], r["max_ev"]) for r in every] == [(2, 5, 2, 104.0)]


def test_the_pitcher_board_counts_the_postseason_strikeout_only_with_all() -> None:
    """The pitcher board reads 1 strikeout in 3 plate appearances by default.

    With 'all' it reads 2 in 5: 33.3% and then 40.0%.
    """
    path = "/api/analytics/leaderboard/pitchers?season=2025&min_pa=0"
    assert [(r["pa"], r["k_pct"]) for r in _read(path)] == [(3, 33.3)]
    assert [(r["pa"], r["k_pct"]) for r in _read(f"{path}&game_type=all")] == [(5, 40.0)]


def test_the_pitch_type_mix_drops_the_postseason_sliders() -> None:
    """The default mix holds the ten fastballs alone; 'all' adds the ten sliders.

    The route keeps a pitch type with ten pitches or more.
    """
    path = "/api/analytics/pitch-type-mix?season=2025"
    regular = _read(path)
    assert [(r["pitch_type"], r["n"], r["usage_pct"], r["avg_velo"]) for r in regular] == [
        ("FF", 10, 100.0, 95.0)
    ]
    every = sorted(_read(f"{path}&game_type=all"), key=lambda r: r["pitch_type"])
    assert [(r["pitch_type"], r["n"], r["usage_pct"], r["avg_velo"]) for r in every] == [
        ("FF", 10, 50.0, 95.0),
        ("SL", 10, 50.0, 88.0),
    ]


def test_the_outcomes_drop_the_postseason_events() -> None:
    """The default outcomes hold game 1's three events; 'all' adds game 2's two."""
    path = "/api/analytics/pa-outcomes?season=2025"
    assert {r["event"]: r["n"] for r in _read(path)} == {
        "home_run": 1,
        "field_out": 1,
        "strikeout": 1,
    }
    assert {r["event"]: r["n"] for r in _read(f"{path}&game_type=all")} == {
        "home_run": 2,
        "field_out": 1,
        "strikeout": 2,
    }


def test_the_count_state_counts_the_postseason_pitches_only_with_all() -> None:
    """The 0-0 count holds 10 pitches by default and 20 with 'all'.

    Game 1 swings 3 times (E, X, S) and whiffs once: swing 30.0, whiff 33.3.
    Game 2 adds 2 swings (E, S) and 1 whiff: 5 of 20 is 25.0; 2 of 5 is 40.0.
    """
    path = "/api/analytics/count-state?season=2025"
    assert _read(path) == [
        {"balls": 0, "strikes": 0, "pitches": 10, "swing_pct": 30.0, "whiff_pct": 33.3}
    ]
    assert _read(f"{path}&game_type=all") == [
        {"balls": 0, "strikes": 0, "pitches": 20, "swing_pct": 25.0, "whiff_pct": 40.0}
    ]


def test_the_velocity_histogram_drops_the_postseason_bucket() -> None:
    """The default histogram holds the 95 mph bucket; 'all' adds the 88 mph bucket."""
    path = "/api/analytics/velo-distribution?season=2025"
    assert _read(path) == [{"velo_bucket": 95, "n": 10}]
    assert _read(f"{path}&game_type=all") == [
        {"velo_bucket": 88, "n": 10},
        {"velo_bucket": 95, "n": 10},
    ]


def test_the_zone_counts_the_postseason_pitches_only_with_all() -> None:
    """Zone 5 holds 10 pitches by default and 20 with 'all'.

    No seed pitch is a called strike, so the called-strike rate reads 0.0.
    """
    path = "/api/analytics/zone?season=2025"
    assert [(r["zone"], r["pitches"], r["called_strike_pct"]) for r in _read(path)] == [
        (5, 10, 0.0)
    ]
    every = _read(f"{path}&game_type=all")
    assert [(r["zone"], r["pitches"], r["called_strike_pct"]) for r in every] == [(5, 20, 0.0)]
