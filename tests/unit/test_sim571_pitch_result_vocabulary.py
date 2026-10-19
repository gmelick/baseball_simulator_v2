"""
tests/unit/test_sim571_pitch_result_vocabulary.py
=================================================
The Data Lab reads each pitch result from its one-letter code (the Data Lab
vocabulary fix, SIM-571).

The Data Lab is the web app's analytics area. Four of its routes show pitch
rates: count-state, zone and the two leaderboards. A whiff is a swing and a
miss. CSW (called strikes plus whiffs) is the share of pitches that are a
called strike or a whiff. The routes matched ``raw.pitches.description``
against Statcast's snake_case names, such as ``'swinging_strike'``. A
snake_case name is a lower-case name with underscores between the words. The
loader stores the MLB feed's display text in that column ("Swinging Strike").
So on live data the swing rate read 0 and the whiff rate read null.

The column ``raw.pitches.type`` holds the feed's one-letter result code. The fix
filters on that code with the named code sets in ``pipeline/statcast_events.py``.
The profiles and the pool build read the same sets, so one vocabulary serves
the whole product.

The owner ruled on 2026-10-19 that the Data Lab shows Savant's whiff. Savant
(Baseball Savant) is the public Statcast site, and its whiff counts a foul tip.
The model's whiff does not change: a foul tip stays contact for the profiles
and the pool build.

These tests pin the rule:

* the fixture holds the 17 (description, code) pairs that the loader stores;
* each code falls in exactly one class, and the sets name no other code;
* the sets give the league's 2025 rates from the fixture's counts;
* the route fragments render from the shared sets;
* no route reads ``description``, and no route spells a code inline;
* each rate route answers 200 with the code-set filters in its SQL;
* DuckDB runs each route's own SQL over one row per loader code;
* the assistant's prompt and the console's starter queries filter on the code.

No test needs a database, the network or the app's lifespan.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import require_auth
from api.routes import analytics
from api.routes.ai_assistant import _SYSTEM_PROMPT
from pipeline import statcast_events
from pipeline.statcast_events import (
    BALL_TYPES,
    CALLED_STRIKE_TYPES,
    FOUL_TIP_TYPES,
    FOUL_TYPES,
    IN_PLAY_TYPES,
    NON_SWING_TYPES,
    SAVANT_WHIFF_TYPES,
    STRIKE_THREE_FOUL_TYPES,
    WHIFF_TYPES,
    sql_called_strike,
    sql_csw,
    sql_in_play,
    sql_savant_whiff,
    sql_swing,
)

_ROOT = Path(__file__).resolve().parents[2]
_ANALYTICS = _ROOT / "api" / "routes" / "analytics.py"
_STARTER_FILE = _ROOT / "frontend" / "src" / "components" / "lab" / "starterQueries.ts"

# ---------------------------------------------------------------------------
# The fixture: the pairs the loader stores
# ---------------------------------------------------------------------------

#: The 17 (description, code) pairs in ``raw.pitches``, with two row counts:
#: every row of 2017-2026, and the 2025 rows with ``data_quality_flag = FALSE``.
#: ``description`` holds the feed's display text; ``type`` holds its code.
#: The read, on 2026-10-19 as the read-only role ``baseball_ro``:
#:   SELECT description, TRIM(type), COUNT(*),
#:          COUNT(*) FILTER (WHERE season = 2025 AND data_quality_flag = FALSE)
#:     FROM raw.pitches GROUP BY 1, 2 ORDER BY 3 DESC
#: No row has a NULL description or a NULL code.
_LOADER_PAIRS: tuple[tuple[str, str, int, int], ...] = (
    ("Ball", "B", 2_289_907, 242_566),
    ("Foul", "F", 1_214_179, 129_863),
    ("Called Strike", "C", 1_124_477, 117_734),
    ("In play, out(s)", "X", 769_455, 82_545),
    ("Swinging Strike", "S", 709_290, 75_753),
    ("In play, no out", "D", 264_272, 27_828),
    ("Ball In Dirt", "*B", 155_777, 15_458),
    ("In play, run(s)", "E", 153_581, 16_210),
    ("Foul Tip", "T", 65_358, 7_448),
    ("Swinging Strike (Blocked)", "W", 45_384, 4_105),
    ("Hit By Pitch", "H", 19_170, 1_970),
    ("Foul Bunt", "L", 13_860, 1_272),
    ("Missed Bunt", "M", 2_718, 215),
    ("Pitchout", "P", 551, 54),
    ("Foul Tip", "O", 279, 34),
    ("Swinging Pitchout", "Q", 2, 0),
    ("Foul Pitchout", "R", 2, 0),
)

#: The pool build names the hit-by-pitch code in a branch of its own, so no
#: shared set holds it.
_HIT_BY_PITCH: tuple[str, ...] = ("H",)

_CODES: frozenset[str] = frozenset(code for _, code, _, _ in _LOADER_PAIRS)
_ROWS_2025: dict[str, int] = {code: n for _, code, _, n in _LOADER_PAIRS}

#: The model's partition of the codes: each code belongs to one class.
_CLASSES: dict[str, tuple[str, ...]] = {
    "ball": BALL_TYPES,
    "called strike": CALLED_STRIKE_TYPES,
    "whiff": WHIFF_TYPES,
    "foul": FOUL_TYPES,
    "in play": IN_PLAY_TYPES,
    "hit by pitch": _HIT_BY_PITCH,
}

#: Statcast's names for a pitch result. The old Data Lab filters matched these.
_STATCAST_NAMES: frozenset[str] = frozenset(
    {
        "ball",
        "blocked_ball",
        "called_strike",
        "foul",
        "foul_bunt",
        "foul_pitchout",
        "foul_tip",
        "hit_by_pitch",
        "hit_into_play",
        "missed_bunt",
        "pitchout",
        "swinging_pitchout",
        "swinging_strike",
        "swinging_strike_blocked",
    }
)


def _class_of(code: str) -> str:
    """Return the one class that holds ``code``. Fail when it is not one."""
    owners = [name for name, codes in _CLASSES.items() if code in codes]
    assert len(owners) == 1, f"code {code!r} falls in {owners}"
    return owners[0]


def _count_2025(codes: tuple[str, ...]) -> int:
    """Return the fixture's clean 2025 rows over ``codes``."""
    return sum(_ROWS_2025[code] for code in codes)


def _codes_in(fragment: str) -> list[str]:
    """Return the quoted pitch codes of a SQL fragment, in order."""
    return re.findall(r"'(\*?[A-Z])'", fragment)


# ---------------------------------------------------------------------------
# 1. The code sets against the fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(_CODES))
def test_every_loader_code_falls_in_exactly_one_class(code: str) -> None:
    """Each stored code belongs to exactly one class of the model's partition.

    The classes are ball, called strike, whiff, foul, in play and hit by pitch.
    """
    assert _class_of(code) in _CLASSES


def test_the_sets_name_the_loader_codes_and_nothing_else() -> None:
    """The named sets cover the 17 stored codes, and they name no other code.

    A set with a stray code filters on a value that the loader never writes. A
    stored code that no set names is a pitch that no rate can count.
    """
    assert len(_LOADER_PAIRS) == 17
    assert len(_CODES) == 17, "two fixture pairs share a code"
    named = set().union(
        BALL_TYPES,
        CALLED_STRIKE_TYPES,
        WHIFF_TYPES,
        FOUL_TYPES,
        STRIKE_THREE_FOUL_TYPES,
        NON_SWING_TYPES,
        IN_PLAY_TYPES,
        FOUL_TIP_TYPES,
        SAVANT_WHIFF_TYPES,
        _HIT_BY_PITCH,
    )
    assert named - _CODES == set(), "a set names a code that the loader never stores"
    assert _CODES - named == set(), "the loader stores a code that no set names"


def test_a_swing_is_the_complement_of_the_non_swings() -> None:
    """The swings are every stored code outside the non-swing set.

    The swings are the whiffs, the fouls and the balls in play. The non-swings
    are the balls, the called strikes and the hit by pitch.
    """
    swings = _CODES - set(NON_SWING_TYPES)
    assert swings == set(WHIFF_TYPES) | set(FOUL_TYPES) | set(IN_PLAY_TYPES)
    assert set(NON_SWING_TYPES) == set(BALL_TYPES) | set(CALLED_STRIKE_TYPES) | set(_HIT_BY_PITCH)


def test_the_savant_whiff_is_the_models_whiff_plus_the_foul_tips() -> None:
    """The Data Lab's whiff adds the two foul-tip codes to the model's whiff.

    The model's whiff stays ('S', 'W', 'M', 'Q'): a foul tip is contact for the
    profiles and the pool build. A foul tip is a foul below two strikes and
    strike three at two strikes.
    """
    assert FOUL_TIP_TYPES == ("T", "O")
    assert SAVANT_WHIFF_TYPES == WHIFF_TYPES + FOUL_TIP_TYPES
    assert SAVANT_WHIFF_TYPES == ("S", "W", "M", "Q", "T", "O")
    assert set(FOUL_TIP_TYPES) <= set(FOUL_TYPES)
    assert set(FOUL_TIP_TYPES) <= set(STRIKE_THREE_FOUL_TYPES)
    assert WHIFF_TYPES == ("S", "W", "M", "Q")
    assert not set(FOUL_TIP_TYPES) & set(WHIFF_TYPES)


def test_one_description_never_spans_two_classes() -> None:
    """Every code of one display text falls in the same class.

    "Foul Tip" is the one display text with two codes: 'T', and 'O' on a bunt.
    Both are fouls, and the two together are the foul-tip set.
    """
    by_description: dict[str, set[str]] = {}
    for description, code, _, _ in _LOADER_PAIRS:
        by_description.setdefault(description, set()).add(code)
    for description, codes in by_description.items():
        classes = {_class_of(code) for code in codes}
        assert len(classes) == 1, f"{description!r} spans {sorted(classes)}"
    assert by_description["Foul Tip"] == set(FOUL_TIP_TYPES)
    assert _class_of("T") == _class_of("O") == "foul"


def test_the_loader_stores_display_text_not_statcast_names() -> None:
    """No stored description is a Statcast snake_case name.

    This is the root defect. A filter such as description LIKE
    '%swinging_strike%' matches no stored row.
    """
    for description, code, _, _ in _LOADER_PAIRS:
        assert description not in _STATCAST_NAMES, (description, code)
        assert re.fullmatch(r"[a-z]+(?:_[a-z]+)*", description) is None, (description, code)
        assert "swinging_strike" not in description, (description, code)


def test_the_fixture_totals_match_the_live_read() -> None:
    """The fixture's 2025 column gives the totals of the live read.

    The definitions here come from the read, not from the sets, so this test
    guards the fixture against a typo:
    every pitch 723,055; swings (not B, *B, C, H or P) 345,273;
    Savant whiffs (S, W, M, Q, T, O) 87,555; called strikes 117,734;
    balls in play (X, D, E) 126,583, and 'X' alone 82,545 (65%).
    """
    rows = _ROWS_2025
    assert sum(rows.values()) == 723_055
    assert sum(n for code, n in rows.items() if code not in {"B", "*B", "C", "H", "P"}) == 345_273
    assert sum(rows[code] for code in ("S", "W", "M", "Q", "T", "O")) == 87_555
    assert rows["C"] == 117_734
    balls_in_play = sum(rows[code] for code in ("X", "D", "E"))
    assert balls_in_play == 126_583
    assert rows["X"] == 82_545
    assert round(100 * rows["X"] / balls_in_play) == 65


def test_the_league_rates_from_the_fixture_counts() -> None:
    """The sets give the league's 2025 rates from the fixture's counts.

    swing = 345,273 / 723,055 = 47.8%.
    Savant whiff = 87,555 / 345,273 = 25.4% of swings.
    called strike = 117,734 / 723,055 = 16.3%.
    CSW = (117,734 + 87,555) / 723,055 = 205,289 / 723,055 = 28.4%.
    The model's whiff, without the foul tips, = 80,073 / 345,273 = 23.2%.
    A set that drifts moves one of these reads.
    """
    total = sum(_ROWS_2025.values())
    swings = total - _count_2025(NON_SWING_TYPES)
    savant_whiffs = _count_2025(SAVANT_WHIFF_TYPES)
    called = _count_2025(CALLED_STRIKE_TYPES)
    assert swings == 345_273
    assert round(100 * swings / total, 1) == 47.8
    assert round(100 * savant_whiffs / swings, 1) == 25.4
    assert round(100 * called / total, 1) == 16.3
    assert round(100 * (called + savant_whiffs) / total, 1) == 28.4
    assert round(100 * _count_2025(WHIFF_TYPES) / swings, 1) == 23.2


# ---------------------------------------------------------------------------
# 2. The route fragments
# ---------------------------------------------------------------------------

_FRAGMENTS: list[tuple[str, str, str]] = [
    ("_SWING", sql_swing(), "type NOT IN ('B', 'C', 'H', 'P', '*B')"),
    ("_WHIFF", sql_savant_whiff(), "type IN ('S', 'W', 'M', 'Q', 'T', 'O')"),
    ("_CALLED_STRIKE", sql_called_strike(), "type IN ('C')"),
    ("_SWING_P", sql_swing("p."), "p.type NOT IN ('B', 'C', 'H', 'P', '*B')"),
    ("_WHIFF_P", sql_savant_whiff("p."), "p.type IN ('S', 'W', 'M', 'Q', 'T', 'O')"),
    (
        "_CSW_P",
        sql_csw("p."),
        "(p.type IN ('C') OR p.type IN ('S', 'W', 'M', 'Q', 'T', 'O'))",
    ),
    ("_IN_PLAY_P", sql_in_play("p."), "p.type IN ('X', 'D', 'E')"),
]
_FRAGMENT_NAMES = [name for name, _, _ in _FRAGMENTS]


@pytest.mark.parametrize(("name", "rendered", "text"), _FRAGMENTS, ids=_FRAGMENT_NAMES)
def test_the_route_fragments_render_from_the_shared_sets(
    name: str, rendered: str, text: str
) -> None:
    """Each module fragment equals its helper's render and the expected text.

    The SQL console's starter queries spell the same text, so the spacing is
    part of the contract.
    """
    assert getattr(analytics, name) == rendered
    assert rendered == text


def test_the_computor_reexports_the_shared_sets() -> None:
    """The profile computor binds the six sets from the vocabulary module.

    The names stay importable from the computor, and each one is the same
    object. So the computor holds no copy that could drift.
    """
    from pipeline.batch import player_profile_computor as computor

    for name in (
        "BALL_TYPES",
        "CALLED_STRIKE_TYPES",
        "WHIFF_TYPES",
        "FOUL_TYPES",
        "STRIKE_THREE_FOUL_TYPES",
        "NON_SWING_TYPES",
    ):
        assert getattr(computor, name) is getattr(statcast_events, name), name


def test_the_api_and_the_profiles_render_the_same_swing() -> None:
    """The Data Lab's swing is the profiles' swing, byte for byte.

    The Data Lab's whiff is the profiles' whiff plus the foul tips 'T' and
    'O', in that order, and nothing else.
    """
    from pipeline.batch import player_profile_computor as computor

    assert analytics._SWING == computor.SQL_SWING
    assert computor.SQL_WHIFF == "type IN ('S', 'W', 'M', 'Q')"
    model_codes = _codes_in(computor.SQL_WHIFF)
    assert _codes_in(analytics._WHIFF) == model_codes + list(FOUL_TIP_TYPES)


@pytest.mark.parametrize("name", _FRAGMENT_NAMES)
def test_no_fragment_carries_an_unrendered_placeholder(name: str) -> None:
    """A fragment holds no brace.

    A ``{_WHIFF}`` in a string that is not an f-string ships the braces into
    the query. That query then matches nothing, and nothing fails.
    """
    fragment = getattr(analytics, name)
    assert "{" not in fragment and "}" not in fragment


def test_the_api_loads_the_vocabulary_without_the_computor() -> None:
    """The analytics routes load the light vocabulary module and not the computor.

    The computor imports duckdb, numpy, pandas and sklearn. It also sets the
    root logger at import. A fresh interpreter proves the import graph, because
    other tests in this process load the computor.
    """
    probe = (
        "import json, sys\n"
        "import api.routes.analytics\n"
        "names = ('pipeline.statcast_events', 'pipeline.batch.player_profile_computor',"
        " 'duckdb', 'sklearn')\n"
        "print(json.dumps({name: name in sys.modules for name in names}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert loaded == {
        "pipeline.statcast_events": True,
        "pipeline.batch.player_profile_computor": False,
        "duckdb": False,
        "sklearn": False,
    }


# ---------------------------------------------------------------------------
# 3. The source guard over the analytics module
# ---------------------------------------------------------------------------

#: A ``description`` comparison in SQL, in either case.
_DESCRIPTION_COMPARE = re.compile(r"\bdescription\s*(?:=|LIKE\b|IN\b)", re.IGNORECASE)
#: The same comparison with the keywords in capitals. The prompt is prose, and
#: an English "description in" is not a filter.
_DESCRIPTION_COMPARE_UPPER = re.compile(r"\bdescription\s*(?:=|LIKE\b|IN\b)")
_STATCAST_LITERAL = re.compile(
    r"'%?(?:swinging_strike|called_strike|hit_into_play|foul_tip|foul_bunt|missed_bunt|foul)"
    r"\w*%?'"
)
_TYPE_EQUALS = re.compile(r"\btype\s*=\s*'")
_CODE_LITERAL = re.compile(r"'\*?[A-Z]'")


def _code_strings(tree: ast.Module) -> list[str]:
    """Return every string constant of the module except the docstrings.

    The SQL lives in string constants: plain strings and the parts of an
    f-string. A comment is not in the tree, and the guard skips the docstrings.
    So the module's prose may show a rendered predicate, but its SQL may not.
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


def test_analytics_reads_no_description_and_spells_no_code() -> None:
    """The analytics SQL compares no description and spells no pitch code.

    The guard reads every string constant of the module except the docstrings.
    It finds no ``description`` comparison and no Statcast snake_case literal.
    It finds no ``type = '...'`` test and no quoted one-letter code. So every
    code reaches the SQL through a shared set.
    """
    strings = _code_strings(ast.parse(_ANALYTICS.read_text(encoding="utf-8")))
    assert any("raw.pitches" in text for text in strings), "the guard found no SQL"
    for text in strings:
        assert _DESCRIPTION_COMPARE.search(text) is None, text
        assert _STATCAST_LITERAL.search(text) is None, text
        assert _TYPE_EQUALS.search(text) is None, text
        assert _CODE_LITERAL.findall(text) == [], text


@pytest.mark.parametrize(
    ("route", "placeholders"),
    [
        ("count_state", ("{_SWING}", "{_WHIFF}")),
        ("zone", ("{_SWING}", "{_WHIFF}", "{_CALLED_STRIKE}")),
        ("pitcher_leaderboard", ("{_SWING_P}", "{_WHIFF_P}", "{_CSW_P}")),
        ("batter_leaderboard", ("{_IN_PLAY_P}",)),
    ],
)
def test_each_rate_route_uses_the_fragments(route: str, placeholders: tuple[str, ...]) -> None:
    """Each rate route's source places the shared fragments in its SQL."""
    source = inspect.getsource(getattr(analytics, route))
    for placeholder in placeholders:
        assert placeholder in source, f"{route} lacks {placeholder}"


# ---------------------------------------------------------------------------
# 4. The routes answer, and DuckDB runs their SQL
# ---------------------------------------------------------------------------


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
    """Call one route on a bare app and return its one query and arguments.

    The app holds only the analytics router, with ``require_auth`` overridden,
    so the call needs no lifespan, no database and no credential.
    """
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[require_auth] = lambda: None
    pool = _RecordingPool()
    app.state.pg_pool_ro = pool
    resp = TestClient(app).get(path)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    assert len(pool.calls) == 1, pool.calls
    return pool.calls[0]


_ROUTE_CASES = [
    pytest.param(
        "/api/analytics/count-state?season=2025&pitcher=1&batter=2",
        (2025, 1, 2),
        (sql_swing(), sql_savant_whiff()),
        False,
        id="count-state",
    ),
    pytest.param(
        "/api/analytics/zone?season=2025&pitcher=1",
        (2025, 1),
        (sql_swing(), sql_savant_whiff(), sql_called_strike()),
        False,
        id="zone",
    ),
    pytest.param(
        "/api/analytics/leaderboard/pitchers?season=2025&min_pa=0",
        (2025, 0, 50),
        (sql_swing("p."), sql_savant_whiff("p."), sql_csw("p.")),
        True,
        id="pitcher-board",
    ),
    pytest.param(
        "/api/analytics/leaderboard/batters?season=2025&min_pa=0&vs_hand=R",
        (2025, "R", 0, 50),
        (sql_in_play("p."),),
        True,
        id="batter-board",
    ),
]


@pytest.mark.parametrize(("path", "args", "fragments", "joined"), _ROUTE_CASES)
def test_each_rate_route_answers_with_the_code_set_filters(
    path: str, args: tuple[Any, ...], fragments: tuple[str, ...], joined: bool
) -> None:
    """Each rate route answers 200, and its SQL filters on the code sets.

    The SQL holds each rendered fragment and never names ``description``. It
    holds no ``type = '...'`` test. In a leaderboard, which joins
    ``raw.players``, every ``type`` carries the ``p.`` prefix.
    """
    sql, got_args = _route_sql(path)
    assert got_args == args
    for fragment in fragments:
        assert fragment in sql, f"{path}: the SQL lacks {fragment!r}"
    assert "description" not in sql.lower()
    assert _TYPE_EQUALS.search(sql) is None
    if joined:
        assert re.findall(r"(?<!p\.)\btype\b", sql) == []


_PITCHES_DDL = (
    "CREATE TABLE raw.pitches (season INTEGER, balls INTEGER, strikes INTEGER, "
    "zone INTEGER, pitcher INTEGER, batter INTEGER, p_throws VARCHAR, type VARCHAR, "
    "description VARCHAR, events VARCHAR, launch_speed DOUBLE, data_quality_flag BOOLEAN)"
)
_PLAYERS_DDL = "CREATE TABLE raw.players (player_id INTEGER, full_name VARCHAR)"

#: The event and the exit velocity of the three in-play codes. Every other row
#: has neither.
_BATTED: dict[str, tuple[str, float]] = {
    "X": ("field_out", 90.0),
    "D": ("single", 88.0),
    "E": ("home_run", 101.0),
}


def _seed_rows() -> list[tuple[Any, ...]]:
    """Return one pitch per loader pair, plus two rows the default reads skip.

    Each loader row: season 2025, count 0-0, zone 5, pitcher 1, batter 2, a
    right-handed pitcher and a clean quality flag. The two extra rows are
    swinging strikes: one with the quality flag set, and one in 2024.
    """
    rows: list[tuple[Any, ...]] = []
    for description, code, _, _ in _LOADER_PAIRS:
        events, speed = _BATTED.get(code, (None, None))
        rows.append((2025, 0, 0, 5, 1, 2, "R", code, description, events, speed, False))
    rows.append((2025, 0, 0, 5, 1, 2, "R", "S", "Swinging Strike", None, None, True))
    rows.append((2024, 0, 0, 5, 1, 2, "R", "S", "Swinging Strike", None, None, False))
    return rows


def _run_in_duckdb(sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Run a route's captured SQL, unchanged, over the seed rows in DuckDB.

    DuckDB accepts the route's Postgres text as it stands: the ``$1``
    parameters, ``COUNT(*) FILTER``, ``::numeric``, ``::float8`` and
    ``NULLS LAST``. The seed rows hold the BARE codes ('S', not 'S '). The live
    column is CHAR(2), and Postgres ignores the padding when it compares a
    CHAR(2) with a text literal, so the route SQL needs no TRIM. A DuckDB
    VARCHAR keeps a trailing space, so a padded seed code would match nothing.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute(_PITCHES_DDL)
        con.execute(_PLAYERS_DDL)
        con.executemany(
            "INSERT INTO raw.pitches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _seed_rows(),
        )
        con.executemany(
            "INSERT INTO raw.players VALUES (?, ?)", [(1, "Test Pitcher"), (2, "Test Batter")]
        )
        cur = con.execute(sql, list(args))
        names = [column[0] for column in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def test_count_state_rates_over_the_loader_codes() -> None:
    """The count-state SQL reads the expected swing and whiff rates.

    The default read keeps 17 rows: the flagged row and the 2024 row drop out.
    5 codes are non-swings (B, *B, P, C, H), so 12 of 17 are swings: 70.6%.
    6 swings are Savant whiffs (S, W, M, Q, T, O): 6 / 12 = 50.0%.
    With flagged rows the read keeps 18 rows and 13 swings: 72.2%.
    The flagged row is a whiff, so 7 / 13 = 53.8%.
    """
    rows = _run_in_duckdb(*_route_sql("/api/analytics/count-state?season=2025"))
    assert rows == [{"balls": 0, "strikes": 0, "pitches": 17, "swing_pct": 70.6, "whiff_pct": 50.0}]
    rows = _run_in_duckdb(
        *_route_sql("/api/analytics/count-state?season=2025&include_flagged=true")
    )
    assert rows == [{"balls": 0, "strikes": 0, "pitches": 18, "swing_pct": 72.2, "whiff_pct": 53.8}]


def test_zone_rates_over_the_loader_codes() -> None:
    """The zone SQL reads the expected whiff and called-strike rates.

    Every seed row sits in zone 5. 6 of the 12 swings are whiffs: 50.0%.
    One of the 17 pitches is a called strike ('C'): 1 / 17 = 5.9%.
    """
    rows = _run_in_duckdb(*_route_sql("/api/analytics/zone?season=2025"))
    assert rows == [{"zone": 5, "pitches": 17, "whiff_pct": 50.0, "called_strike_pct": 5.9}]


def test_pitcher_leaderboard_rates_over_the_loader_codes() -> None:
    """The pitcher leaderboard SQL reads the expected CSW and whiff rates.

    The board counts a plate appearance on a row with an event: X, D and E,
    so 3. ``min_pa=0`` keeps the pitcher. No event is a strikeout or a walk.
    CSW is a called strike or a Savant whiff over every pitch: 7 / 17 = 41.2%.
    The whiff rate is 6 / 12 = 50.0%.
    """
    rows = _run_in_duckdb(*_route_sql("/api/analytics/leaderboard/pitchers?season=2025&min_pa=0"))
    assert rows == [
        {
            "player_id": 1,
            "name": "Test Pitcher",
            "pa": 3,
            "k_pct": 0.0,
            "bb_pct": 0.0,
            "csw_pct": 41.2,
            "whiff_pct": 50.0,
        }
    ]


def test_batter_leaderboard_counts_every_ball_in_play() -> None:
    """The batter leaderboard SQL reads the hits as well as the outs.

    The balls in play are X (90.0 mph), D (88.0) and E (101.0), so 3.
    The mean exit velocity is 279.0 / 3 = 93.0, and the most is 101.0.
    One of the three is hit at 95 mph or more: 33.3%. One event is a home run.
    A filter on 'X' alone reads 1 ball in play at 90.0 and a hard-hit rate of 0.
    Against left-handed pitchers the board has no row.
    """
    rows = _run_in_duckdb(*_route_sql("/api/analytics/leaderboard/batters?season=2025&min_pa=0"))
    assert rows == [
        {
            "player_id": 2,
            "name": "Test Batter",
            "pa": 3,
            "bip": 3,
            "avg_ev": 93.0,
            "max_ev": 101.0,
            "hard_hit_pct": 33.3,
            "k_pct": 0.0,
            "bb_pct": 0.0,
            "hr": 1,
        }
    ]
    lefties = _run_in_duckdb(
        *_route_sql("/api/analytics/leaderboard/batters?season=2025&min_pa=0&vs_hand=L")
    )
    assert lefties == []


# ---------------------------------------------------------------------------
# 5. The sibling sites: the assistant's prompt and the console's starter queries
# ---------------------------------------------------------------------------


def test_the_assistant_prompt_teaches_the_type_codes() -> None:
    """The assistant's prompt defines each rate on the ``type`` code.

    The prompt holds the rendered swing, Savant-whiff and in-play filters. It
    holds no ``description`` comparison and no Statcast LIKE pattern. It names
    the display text that ``description`` really holds.
    """
    for fragment in (sql_swing(), sql_savant_whiff(), sql_in_play()):
        assert fragment in _SYSTEM_PROMPT, fragment
    assert _DESCRIPTION_COMPARE_UPPER.search(_SYSTEM_PROMPT) is None
    assert "%swinging_strike%" not in _SYSTEM_PROMPT
    assert any(
        text in _SYSTEM_PROMPT for text in ("Called Strike", "Swinging Strike", "In play, out(s)")
    )


def _starter_queries() -> dict[str, str]:
    """Return the console's starter queries as ``{name: sql}``."""
    text = _STARTER_FILE.read_text(encoding="utf-8")
    pairs = re.findall(r"name:\s*'([^']*)',\s*sql:\s*`([^`]*)`", text)
    return dict(pairs)


def _starter(queries: dict[str, str], prefix: str) -> str:
    """Return the one starter query whose name starts with ``prefix``."""
    found = [sql for name, sql in queries.items() if name.startswith(prefix)]
    assert len(found) == 1, f"expected one starter query named {prefix!r}, found {len(found)}"
    return found[0]


def test_the_starter_queries_filter_on_type() -> None:
    """The console's starter queries filter on the code with the shared lists.

    No query compares ``description``, spells a Statcast name or keeps the
    outs-only filter ``type = 'X'``. The whiff leaders, the chase rate and the
    count-state query use the rendered swing and Savant-whiff lists. The
    batted-ball query counts every ball in play.
    """
    if not _STARTER_FILE.exists():
        pytest.skip(f"{_STARTER_FILE} is not present in this checkout")
    queries = _starter_queries()
    assert len(queries) == 9, sorted(queries)
    for name, sql in queries.items():
        assert _DESCRIPTION_COMPARE.search(sql) is None, name
        assert "%swinging_strike%" not in sql, name
        assert "'foul'" not in sql, name
        assert "hit_into_play" not in sql, name
        assert _TYPE_EQUALS.search(sql) is None, name
    whiffs = _starter(queries, "League whiff-rate leaders")
    assert sql_savant_whiff("p.") in whiffs
    assert sql_swing("p.") in whiffs
    assert sql_swing("p.") in _starter(queries, "Chase rate")
    by_count = _starter(queries, "Count-state behavior")
    assert sql_swing() in by_count
    assert sql_savant_whiff() in by_count
    assert sql_in_play("p.") in _starter(queries, "Batter batted-ball quality")
