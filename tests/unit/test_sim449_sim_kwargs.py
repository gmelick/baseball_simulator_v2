"""
tests/unit/test_sim449_sim_kwargs.py
====================================
SIM-449 — the shared ``simulate_game`` kwargs builder + the park-factor resolver.

THE DEFECT THESE TESTS GUARD
----------------------------
``scripts/sim_stats.py`` — the validation harness — held its OWN copy of the
kwargs builder.  The copy dropped ``home_defense``, ``away_defense`` and
``park_run_factor``.  SIM_FIELDER_RBF reads the two defense maps and
SIM_PARK_FACTOR reads the park factor, so both flags ran as no-ops under the
harness, and every A/B test of either flag reported "no effect".

Two guards close that hole:

  * :data:`SIM_KWARG_KEYS` pins the full key set, so a dropped key fails CI;
  * the identity checks below fail the moment somebody reintroduces a local copy
    of either function in ``api/routes/games.py``.

WHAT SIM-449 LEFT OPEN — AND SIM-452 CLOSES
--------------------------------------------
SIM-449 unified the BUILDER. It did not unify the park-factor RESOLUTION. Three
call sites looked the venue up and put the factor on the GameState; five did not,
and shipped the dataclass default 1.0. ``SIM_PARK_FACTOR`` was therefore inert for
those five, one of which was ``scripts/clv_backtest.py`` — the Closing Line Value
backtest, the fund's gold-standard metric.

``SIM_KWARG_KEYS`` could not catch it. The key was always present, just always 1.0,
and 1.0 is ALSO the right answer for a genuinely neutral park.

SIM-452 gives the unresolved case its own value, ``UNRESOLVED_PARK_FACTOR``, and
makes the builder refuse it. The tests below prove the refusal FIRES — including
against the exact two-line calling pattern that shipped in production.

The tests use the project's in-memory mock pattern.  No live DB runs.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from simulation.game_state import GameState
from simulation.sim_kwargs import (
    SIM_KWARG_KEYS,
    UNRESOLVED_PARK_FACTOR,
    ParkFactorSource,
    UnresolvedParkFactorError,
    build_sim_kwargs,
    close_park_factor_source,
    mark_park_factor_unresolved,
    open_sim_duckdb,
    park_factor_is_resolved,
    park_factor_reason,
    park_factor_source_status,
    prime_park_factor_source,
    reset_park_factor_warnings,
    resolve_park_factor_onto_state,
    resolve_park_run_factor,
    sim_kwargs_from_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_park_factor_globals():
    """SIM-453: the warn-once memo and the primed source are process-wide.

    Reset both around every test so no test in this file can pass because an
    earlier one primed a connection or swallowed a warning.
    """
    reset_park_factor_warnings()
    close_park_factor_source()
    yield
    reset_park_factor_warnings()
    close_park_factor_source()


# --- the builder contract ----------------------------------------------------


def test_builder_returns_the_whole_contract():
    state = GameState(pitcher_id=1, bat_hand="R", season=2024)
    assert set(sim_kwargs_from_state(state)) == SIM_KWARG_KEYS
    # The count is pinned too: an added key is a behaviour change and must be a
    # deliberate edit here, not a silent drift.
    assert len(SIM_KWARG_KEYS) == 15  # SIM-486 deleted the per-tile ``k``


def test_builder_carries_defense_maps_and_park_factor():
    state = GameState(
        pitcher_id=1,
        bat_hand="R",
        season=2024,
        home_defense={"C": 2, "SS": 6},
        away_defense={"P": 9, "CF": 8},
        park_run_factor=1.15,
    )
    kw = sim_kwargs_from_state(state)
    assert kw["home_defense"] == {"C": 2, "SS": 6}
    assert kw["away_defense"] == {"P": 9, "CF": 8}
    assert kw["park_run_factor"] == 1.15


def test_builder_defaults_neutral_when_state_bare():
    # A state with no defense / no park factor -> empty maps + 1.0, so both
    # consumers stay neutral even with their flags on.
    state = GameState(pitcher_id=1, bat_hand="R", season=2024)
    kw = sim_kwargs_from_state(state)
    assert kw["home_defense"] == {} and kw["away_defense"] == {}
    assert kw["park_run_factor"] == 1.0
    assert kw["max_innings"] == 12


# --- the anti-drift guard ----------------------------------------------------


def test_api_route_reuses_the_shared_builder():
    import api.routes.games as g

    assert g._sim_kwargs_from_state is sim_kwargs_from_state


def test_api_route_reuses_the_shared_resolver():
    import api.routes.games as g

    assert g._resolve_park_run_factor is resolve_park_run_factor


# --- the park-factor resolver (two-source venue -> factor lookup) ------------


class _FakeRecord(dict):
    """asyncpg-Record-like: supports row["venue_id"]."""


class _FakePool:
    """A direct-connection-style pool (no ``.acquire``) exposing async fetchrow."""

    def __init__(self, venue_id):
        self._venue_id = venue_id

    async def fetchrow(self, sql, gid):
        return None if self._venue_id is None else _FakeRecord(venue_id=self._venue_id)


class _AcquireCtx:
    """The async context manager ``pool.acquire()`` returns."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _AcquirePool:
    """A real asyncpg-style POOL: it has ``.acquire``, so the resolver takes the
    context-manager branch.  ``app.state.pg_pool`` is this shape."""

    def __init__(self, venue_id):
        self._conn = _FakePool(venue_id)

    def acquire(self):
        return _AcquireCtx(self._conn)


class _FakeDuckRes:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeCon:
    def __init__(self, factor):
        self._factor = factor

    def execute(self, sql, params):
        return _FakeDuckRes(None if self._factor is None else (self._factor,))


async def test_park_factor_resolved_from_both_sources():
    assert await resolve_park_run_factor(_FakePool(15), _FakeCon(1.18), 777, 2024) == 1.18


async def test_park_factor_resolved_through_an_acquire_style_pool():
    # The API passes ``app.state.pg_pool``, which HAS ``.acquire``.  This is the
    # branch production takes.
    assert await resolve_park_run_factor(_AcquirePool(15), _FakeCon(1.18), 777, 2024) == 1.18


class _BoomCon:
    def execute(self, *_a, **_k):
        raise RuntimeError("duckdb down")


#: Every way the two-source lookup can fail, with a phrase the reason must contain.
#: SIM-453: each of these used to return a PLAIN 1.0, which cleared the sentinel one
#: line later and made the failure invisible. Each now returns the sentinel.
_UNAVAILABLE_CASES = [
    ("no duckdb", _FakePool(15), None, "no park-factor source"),
    ("unknown venue", _FakePool(None), _FakeCon(1.18), "no venue_id"),
    ("no factor row", _FakePool(15), _FakeCon(None), "no factor_type='R' row"),
    ("degenerate factor", _FakePool(15), _FakeCon(9.9), "outside the sane"),
    ("query error", _FakePool(15), _BoomCon(), "raised RuntimeError"),
]


@pytest.mark.parametrize(
    ("label", "pool", "con", "phrase"),
    _UNAVAILABLE_CASES,
    ids=[c[0] for c in _UNAVAILABLE_CASES],
)
async def test_every_unavailable_branch_reads_as_1_but_stays_UNRESOLVED(label, pool, con, phrase):
    f = await resolve_park_run_factor(pool, con, 777, 2024)

    # A consumer that only wants the number is untouched. This is the original
    # SIM-411/449 contract and it must not change.
    assert f == 1.0
    # ... and the failure is no longer invisible.
    assert park_factor_reason(f) is not None, f"{label}: an unavailable source returned a plain 1.0"
    assert phrase in park_factor_reason(f)


async def test_a_real_row_comes_back_a_PLAIN_float():
    """The contrast case. Only a genuine lookup produces a resolved factor."""
    f = await resolve_park_run_factor(_FakePool(15), _FakeCon(1.18), 777, 2024)
    assert type(f) is float
    assert park_factor_reason(f) is None


async def test_a_genuinely_neutral_park_is_a_PLAIN_1_point_0():
    """The case the whole ticket turns on: a real park that really measures 1.0."""
    f = await resolve_park_run_factor(_FakePool(15), _FakeCon(1.0), 777, 2024)
    assert f == 1.0
    assert park_factor_reason(f) is None  # resolved
    unavailable = await resolve_park_run_factor(_FakePool(15), None, 777, 2024)
    assert unavailable == 1.0
    assert park_factor_reason(unavailable) is not None  # NOT resolved


# --- the read-only DuckDB opener --------------------------------------------


class _StubDuckdb:
    """Stands in for the ``duckdb`` module.  Records the connect call."""

    def __init__(self, sentinel=None, boom: bool = False):
        self.sentinel = sentinel
        self.boom = boom
        self.calls: list[tuple] = []

    def connect(self, path, read_only=False):
        self.calls.append((path, read_only))
        if self.boom:
            raise RuntimeError("read-only open failed")
        return self.sentinel


def test_open_sim_duckdb_returns_the_connection(monkeypatch):
    sentinel = object()
    stub = _StubDuckdb(sentinel=sentinel)
    monkeypatch.setitem(sys.modules, "duckdb", stub)
    monkeypatch.setenv("BASEBALL_DUCKDB_PATH", "/data/from_env.duckdb")

    assert open_sim_duckdb() is sentinel
    # The harness must never take the write lock the profile computor needs.
    assert stub.calls == [("/data/from_env.duckdb", True)]


def test_open_sim_duckdb_prefers_the_explicit_path(monkeypatch):
    stub = _StubDuckdb(sentinel=object())
    monkeypatch.setitem(sys.modules, "duckdb", stub)
    monkeypatch.setenv("BASEBALL_DUCKDB_PATH", "/data/from_env.duckdb")

    open_sim_duckdb("/tmp/explicit.duckdb")
    assert stub.calls == [("/tmp/explicit.duckdb", True)]


def test_open_sim_duckdb_returns_none_on_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", _StubDuckdb(boom=True))
    # A failed open degrades to None; the caller then TELLS the operator the park
    # factor is neutral instead of hiding it.
    assert open_sim_duckdb() is None


# ===========================================================================
# SIM-452 — the unresolved-park-factor sentinel
#
# Every test in this section exists to prove the guard can go RED. A guard that
# only passes on good input is the defect this whole round is about.
# ===========================================================================


def _bare_state() -> GameState:
    return GameState(pitcher_id=1, bat_hand="R", season=2024)


# --- the sentinel itself -----------------------------------------------------


def test_the_sentinel_equals_one_but_is_not_a_resolved_one():
    # It must read as 1.0 to every consumer that only wants the number ...
    assert UNRESOLVED_PARK_FACTOR == 1.0
    assert float(UNRESOLVED_PARK_FACTOR) == 1.0
    # ... and still be tellable apart from a resolved neutral park. This is the
    # whole point: 1.0 is a legitimate answer AND the "nobody looked" value, and
    # SIM-449 could not separate the two.
    assert type(UNRESOLVED_PARK_FACTOR) is not float


def test_a_resolved_neutral_park_is_distinguishable_from_no_lookup():
    resolved = _bare_state()
    unresolved = _bare_state()
    mark_park_factor_unresolved(unresolved)
    # The resolver wrote a real neutral 1.0 onto `resolved`.
    resolved.park_run_factor = 1.0

    assert resolved.park_run_factor == unresolved.park_run_factor == 1.0
    assert park_factor_is_resolved(resolved) is True
    assert park_factor_is_resolved(unresolved) is False


def test_writing_any_factor_clears_the_sentinel():
    # The sentinel lives IN the field the lookup writes, so a real lookup clears it
    # by plain assignment. A separate "resolved" flag would need clearing by hand.
    state = _bare_state()
    mark_park_factor_unresolved(state)
    assert park_factor_is_resolved(state) is False
    state.park_run_factor = 1.0
    assert park_factor_is_resolved(state) is True


# --- CAN IT FAIL: the builder refuses an unresolved state --------------------


def test_builder_RAISES_on_an_unresolved_park_factor():
    """THE can-it-fail proof. Skip the lookup, and the builder refuses to build."""
    state = _bare_state()
    mark_park_factor_unresolved(state)

    with pytest.raises(UnresolvedParkFactorError) as exc:
        sim_kwargs_from_state(state)
    assert "park_run_factor is UNRESOLVED" in str(exc.value)


def test_builder_accepts_the_state_once_resolved():
    state = _bare_state()
    mark_park_factor_unresolved(state)
    state.park_run_factor = 1.0  # a REAL neutral park, resolved from data
    kw = sim_kwargs_from_state(state)
    assert kw["park_run_factor"] == 1.0
    assert type(kw["park_run_factor"]) is float


# --- the shared async entry point -------------------------------------------


async def test_entry_point_resolves_then_builds():
    state = _bare_state()
    mark_park_factor_unresolved(state)
    kw = await build_sim_kwargs(state, pool=_FakePool(15), con=_FakeCon(1.18), game_pk=777)
    assert kw["park_run_factor"] == 1.18
    assert set(kw) == SIM_KWARG_KEYS


async def test_entry_point_keeps_the_state_UNRESOLVED_when_no_duckdb():
    """SIM-453 — this test used to PIN the defect.

    It read:

        kw = await build_sim_kwargs(state, pool=_FakePool(15), con=None, game_pk=777)
        assert park_factor_is_resolved(state) is True

    with the comment "No DuckDB -> the lookup ANSWERS 'neutral'. That is a resolved
    answer, not a skipped lookup." That comment was wrong, and asserting it is why
    no test in this file could go red on the real defect. No DuckDB is an
    UNAVAILABLE source. It answers nothing. Calling that "resolved" is the whole
    bug: on the default stack, where api/main.py never opens app.state.sim_duckdb,
    every route shipped a neutral 1.0 and this assertion said it was fine.

    The kwargs still BUILD -- /simulate must keep answering with no sim DuckDB --
    but the state keeps the truth and the builder logs a warning.
    """
    state = _bare_state()
    mark_park_factor_unresolved(state)
    kw = await build_sim_kwargs(state, pool=_FakePool(15), con=None, game_pk=777)

    assert kw["park_run_factor"] == 1.0  # the number the sim runs with, unchanged
    assert type(kw["park_run_factor"]) is float  # a plain float reaches simulate_game
    assert park_factor_is_resolved(state) is False  # <-- the line that used to be True
    assert "no park-factor source" in (park_factor_reason(state.park_run_factor) or "")


async def test_entry_point_can_be_told_to_REFUSE_an_unavailable_source():
    """The strict mode, for a caller whose whole output is invalid park-blind."""
    state = _bare_state()
    mark_park_factor_unresolved(state)
    with pytest.raises(UnresolvedParkFactorError):
        await build_sim_kwargs(
            state,
            pool=_FakePool(15),
            con=None,
            game_pk=777,
            on_unavailable_park_factor="raise",
        )


async def test_resolver_defaults_the_season_to_the_state():
    state = GameState(pitcher_id=1, bat_hand="R", season=2019)
    seen: list[int] = []

    class _SeasonSpyCon:
        def execute(self, _sql, params):
            seen.append(int(params[1]))
            return _FakeDuckRes((1.07,))

    assert await resolve_park_factor_onto_state(state, _FakePool(15), _SeasonSpyCon(), 777) == 1.07
    assert seen == [2019]


# ===========================================================================
# SIM-452 — the historical defect, reproduced
#
# `api/routes/games.py::get_game_boxscore` shipped exactly two lines:
#
#     state = await _resolve_state_or_error(pool, game_pk)
#     sim_kwargs = _sim_kwargs_from_state(state)
#
# No park-factor lookup between them. The same two lines ran in
# `get_player_prop_edge`, in `api/routes/betting.py::_summary_and_winprob`, and —
# through `_collect_game_results` — in `scripts/clv_backtest.py` and
# `scripts/validate_props.py`. Five park-blind callers, all reporting success.
#
# These tests run that pattern against the REAL `_resolve_state_or_error` and
# assert it now goes red.
# ===========================================================================


class _StubRequest:
    """The bare shape `_get_sim_duckdb` reads: ``request.app.state.sim_duckdb``."""

    def __init__(self, con):
        self.app = SimpleNamespace(state=SimpleNamespace(sim_duckdb=con))


def _stub_resolve_game_state(monkeypatch):
    """Make ``api.routes.games.resolve_game_state`` return a bare GameState.

    The lineup resolver needs a live Postgres. The code under test here is the
    SIM-452 stamping in ``_resolve_state_or_error``, not the lineup SQL, so the
    resolver is stubbed and everything downstream of it stays real.
    """
    import api.routes.games as g

    async def _fake(_conn, _game_pk, *_a, **_k):
        return _bare_state()

    monkeypatch.setattr(g, "resolve_game_state", _fake)
    return g


async def test_state_factory_stamps_every_state_unresolved(monkeypatch):
    g = _stub_resolve_game_state(monkeypatch)
    state = await g._resolve_state_or_error(_FakePool(15), 777)
    assert park_factor_is_resolved(state) is False


async def test_PRE_FIX_boxscore_pattern_now_goes_RED(monkeypatch):
    """The exact pre-fix two-liner. It used to return park-blind kwargs. It raises now."""
    g = _stub_resolve_game_state(monkeypatch)

    state = await g._resolve_state_or_error(_FakePool(15), 777)
    with pytest.raises(UnresolvedParkFactorError):
        g._sim_kwargs_from_state(state)  # <-- the shipped line


async def test_POST_FIX_boxscore_pattern_resolves_the_real_factor(monkeypatch):
    """The same route, rewired. It resolves the venue factor and builds cleanly."""
    g = _stub_resolve_game_state(monkeypatch)
    pool = _FakePool(15)
    request = _StubRequest(_FakeCon(1.18))

    state = await g._resolve_state_or_error(pool, 777)
    kw = await g._resolved_sim_kwargs(request, pool, state, 777)

    assert kw["park_run_factor"] == 1.18  # NOT the park-blind 1.0
    assert set(kw) == SIM_KWARG_KEYS


async def test_the_pre_fix_and_post_fix_kwargs_actually_differ(monkeypatch):
    """Name the size of the defect: the two paths send different park factors.

    A hitter's park at 1.18 versus the park-blind 1.0. That difference is what
    SIM_PARK_FACTOR consumes, so under the old path the flag had nothing to act on.
    """
    g = _stub_resolve_game_state(monkeypatch)
    pool = _FakePool(15)

    pre_fix_state = await g._resolve_state_or_error(pool, 777)
    pre_fix_state.park_run_factor = 1.0  # what the old code shipped: the default
    pre_fix = g._sim_kwargs_from_state(pre_fix_state)

    post_fix_state = await g._resolve_state_or_error(pool, 777)
    post_fix = await g._resolved_sim_kwargs(_StubRequest(_FakeCon(1.18)), pool, post_fix_state, 777)

    assert pre_fix["park_run_factor"] == 1.0
    assert post_fix["park_run_factor"] == 1.18


# ===========================================================================
# SIM-452 — every call site resolves before it builds
#
# The runtime guard is the real enforcement: the API state factory stamps the
# sentinel and the builder refuses it. This section adds a SOURCE-level guard on
# top, so a new park-blind call site fails CI even if no test ever exercises it.
# ===========================================================================

#: The four modules that turn an API-resolved GameState into sim kwargs, with the
#: number of BARE builder calls and RESOLVING calls each one makes.
#:
#: A bare builder call is legitimate ONLY inside a sync replay whose async caller
#: has already resolved the state — the two scripts do exactly that, because their
#: replay runs in a worker thread with no event loop. Both counts are pinned, so
#: adding a call site of either kind is a deliberate edit here, not silent drift.
_SIM_KWARGS_CALL_SITES: dict[str, dict[str, int]] = {
    # 4 routes -> _resolved_sim_kwargs -> build_sim_kwargs. No bare call at all.
    "api/routes/games.py": {"bare": 0, "resolving": 5},
    # _summary_and_winprob -> _resolved_sim_kwargs.
    "api/routes/betting.py": {"bare": 0, "resolving": 1},
    # _score_one_game resolves; _collect_game_results builds in the worker thread.
    "scripts/clv_backtest.py": {"bare": 1, "resolving": 1},
    # run() resolves; _collect_game_results builds in the worker thread.
    "scripts/validate_props.py": {"bare": 1, "resolving": 1},
}

#: Names that build kwargs WITHOUT resolving anything.
_BARE_BUILDER_NAMES = frozenset({"sim_kwargs_from_state", "_sim_kwargs_from_state"})

#: Names that resolve the park factor onto the state.
_RESOLVING_NAMES = frozenset(
    {
        "build_sim_kwargs",
        "_build_sim_kwargs",
        "_resolved_sim_kwargs",
        "resolve_park_factor_onto_state",
    }
)


def _called_names(path: Path) -> list[str]:
    """Every function name called anywhere in ``path``, plain and attribute form."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            names.append(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.append(fn.attr)
    return names


@pytest.mark.parametrize("rel_path", sorted(_SIM_KWARGS_CALL_SITES))
def test_bare_builder_call_count_is_pinned(rel_path):
    """A new bare builder call is a new park-blind call site. Fail on sight."""
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} is missing — the guard cannot check it"
    expected = _SIM_KWARGS_CALL_SITES[rel_path]["bare"]
    n = sum(1 for name in _called_names(path) if name in _BARE_BUILDER_NAMES)
    assert n == expected, (
        f"SIM-452: {rel_path} makes {n} BARE kwargs-builder calls, expected {expected}. "
        "A bare call skips the park-factor lookup unless its async caller already "
        "resolved the state. If you added one on purpose, update "
        "_SIM_KWARGS_CALL_SITES and say in the comment which caller resolves."
    )


@pytest.mark.parametrize("rel_path", sorted(_SIM_KWARGS_CALL_SITES))
def test_resolving_call_count_is_pinned(rel_path):
    """Drop a resolve call and this goes red, whether or not a test covers that route."""
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} is missing — the guard cannot check it"
    expected = _SIM_KWARGS_CALL_SITES[rel_path]["resolving"]
    n = sum(1 for name in _called_names(path) if name in _RESOLVING_NAMES)
    assert n == expected, (
        f"SIM-452: {rel_path} makes {n} park-resolving calls, expected {expected}. "
        "If you added or removed a sim-kwargs call site, update _SIM_KWARGS_CALL_SITES."
    )


#: The eighth consumer. ``scripts/sim_stats.py`` and the acceptance lane build their
#: GameState with ``resolve_game_state`` instead of the API's state factory, so they
#: never carry the sentinel and the builder cannot reject them. They resolve by hand
#: instead, and this guard reads their source to confirm they still do.
_HAND_RESOLVING_CALLERS = ("scripts/sim_stats.py", "tests/acceptance/conftest.py")


@pytest.mark.parametrize("rel_path", _HAND_RESOLVING_CALLERS)
def test_the_hand_resolving_callers_still_resolve(rel_path):
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} is missing — the guard cannot check it"
    src = path.read_text(encoding="utf-8")
    assert "resolve_park_run_factor(" in src, (
        f"SIM-452: {rel_path} builds sim kwargs but no longer resolves the park "
        "factor, so its runs are park-blind."
    )
    assert "state.park_run_factor =" in src, (
        f"SIM-452: {rel_path} calls the resolver but never writes the result onto "
        "the state, so the builder still sees the default."
    )


# ===========================================================================
# SIM-452 — the two offline jobs refuse to run park-blind
#
# `open_sim_duckdb` returning None means every park factor becomes a neutral 1.0.
# A resolved-looking 1.0 across a whole season is exactly the silent no-op this
# round is about, so both jobs exit non-zero instead.
# ===========================================================================


_SCRIPT_CACHE: dict = {}


def _load_script(name: str):
    """Import a ``scripts/`` module by path (``scripts/`` is not a package).

    Cached, because both jobs are ~1200-line modules and every test in this section
    wants one. The module MUST land in ``sys.modules`` before ``exec_module`` so
    ``@dataclass(slots=True)`` can resolve its own namespace.
    """
    if name in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[name]
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _SCRIPT_CACHE[name] = mod
    return mod


class _ClosableCon(_FakeCon):
    """A ``_FakeCon`` the offline jobs can close in their ``finally``."""

    def close(self) -> None:
        return None


def _no_duckdb(monkeypatch):
    import simulation.sim_kwargs as sk

    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: None)


def _fake_duckdb(monkeypatch):
    import simulation.sim_kwargs as sk

    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: _ClosableCon(1.18))


async def test_clv_backtest_EXITS_NONZERO_without_park_factors(monkeypatch, tmp_path):
    """CAN IT FAIL: the CLV backtest refuses a park-blind run."""
    clv = _load_script("clv_backtest")
    _no_duckdb(monkeypatch)

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(clv, "_fetch_final_games", _no_games)
    args = clv.parse_args(
        ["--seasons", "2024", "--output", str(tmp_path / "clv.json"), "--workers", "1"]
    )
    assert await clv.run(args) == 2


async def test_clv_backtest_runs_when_park_factors_are_available(monkeypatch, tmp_path):
    """The same run, with the park-factor source present, gets PAST the park gate —
    so the test above is measuring the precondition and not some unrelated breakage.

    It then stops at a different gate: SIM-454 (a concurrent ticket, owner
    ``scripts/clv_backtest.py``) made a run that priced nothing exit
    ``EXIT_NOTHING_SCORED``, and ``_fetch_final_games`` is stubbed to return no
    games here. The two codes are what this test cares about: the run no longer
    stops for want of park factors.
    """
    clv = _load_script("clv_backtest")
    _fake_duckdb(monkeypatch)

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(clv, "_fetch_final_games", _no_games)
    out = tmp_path / "clv.json"
    args = clv.parse_args(["--seasons", "2024", "--output", str(out), "--workers", "1"])
    rc = await clv.run(args)
    assert rc != clv.EXIT_NO_PARK_SOURCE
    assert rc == clv.EXIT_NOTHING_SCORED  # the empty-slate stub, not the park gate
    assert out.is_file()


async def test_clv_backtest_honours_the_explicit_opt_out(monkeypatch, tmp_path):
    clv = _load_script("clv_backtest")
    _no_duckdb(monkeypatch)

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(clv, "_fetch_final_games", _no_games)
    args = clv.parse_args(
        [
            "--seasons",
            "2024",
            "--output",
            str(tmp_path / "clv.json"),
            "--workers",
            "1",
            "--allow-neutral-parks",
        ]
    )
    # Past the park gate. It stops at SIM-454's empty-slate gate instead.
    rc = await clv.run(args)
    assert rc != clv.EXIT_NO_PARK_SOURCE
    assert rc == clv.EXIT_NOTHING_SCORED


async def test_validate_props_EXITS_NONZERO_without_park_factors(monkeypatch, tmp_path):
    """CAN IT FAIL: the calibration-fitting job refuses a park-blind run."""
    vp = _load_script("validate_props")
    _no_duckdb(monkeypatch)

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(vp, "_fetch_final_games", _no_games)
    args = vp.parse_args(["--seasons", "2024", "--output", str(tmp_path / "vp.json")])
    assert await vp.run(args) == 2


async def test_validate_props_runs_when_park_factors_are_available(monkeypatch, tmp_path):
    vp = _load_script("validate_props")
    _fake_duckdb(monkeypatch)

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(vp, "_fetch_final_games", _no_games)
    args = vp.parse_args(["--seasons", "2024", "--output", str(tmp_path / "vp.json")])
    assert await vp.run(args) == 0


# --- CAN IT FAIL: both scripts' replay paths refuse an unresolved state ------


@pytest.mark.parametrize("script", ["clv_backtest", "validate_props"])
def test_script_replay_REFUSES_an_unresolved_state(script):
    """The exact function that ran park-blind, refusing to run park-blind.

    ``_collect_game_results`` builds the kwargs before it simulates anything, so an
    unresolved state stops it on the first line — no sim, no report, no number.
    """
    mod = _load_script(script)
    state = _bare_state()
    mark_park_factor_unresolved(state)

    with pytest.raises(UnresolvedParkFactorError):
        mod._collect_game_results(state, 1, 0)


# --- the CLV worker must not swallow the guard -------------------------------


def test_clv_worker_reraises_the_unresolved_error(monkeypatch):
    """The worker's blanket ``except`` books a bad game as 'unresolved' and carries on.

    An unresolved park factor is not a bad game, it is a code defect, and booking it
    that way would let a whole park-blind season finish looking healthy. It must
    propagate.
    """
    clv = _load_script("clv_backtest")
    monkeypatch.setattr(clv, "_worker_lazy_init", lambda *_a, **_k: None)

    class _BoomLoop:
        def run_until_complete(self, coro):
            coro.close()
            raise UnresolvedParkFactorError("simulated regression")

    monkeypatch.setattr(clv, "_WORKER_LOOP", _BoomLoop())
    params = {
        "do_game": True,
        "do_props": False,
        "iterations": 1,
        "base_seed": 0,
        "min_edge": 0.0,
        "dsn": "postgresql://x/y",
        "duckdb": "/nope.duckdb",
    }
    with pytest.raises(UnresolvedParkFactorError):
        clv._process_one_game(777, params)


def test_clv_worker_still_swallows_an_ordinary_bad_game(monkeypatch):
    """The contrast case: an ordinary failure is still one skipped game, not a stop."""
    clv = _load_script("clv_backtest")
    monkeypatch.setattr(clv, "_worker_lazy_init", lambda *_a, **_k: None)

    class _BoomLoop:
        def run_until_complete(self, coro):
            coro.close()
            raise RuntimeError("one bad game")

    monkeypatch.setattr(clv, "_WORKER_LOOP", _BoomLoop())
    params = {
        "do_game": True,
        "do_props": False,
        "iterations": 1,
        "base_seed": 0,
        "min_edge": 0.0,
        "dsn": "postgresql://x/y",
        "duckdb": "/nope.duckdb",
    }
    assert clv._process_one_game(777, params) == {
        "status": "unresolved",
        "bets": [],
        "park_run_factor": 1.0,
    }


# ===========================================================================
# SIM-453 — THE INSTRUMENT, CALIBRATED AGAINST THIS REPOSITORY
#
# Two earlier rounds of this fix failed the same way: the instrument was
# calibrated against a number somebody wrote in a brief instead of the number this
# repository records. So every threshold below is read out of the repository, and
# the source of each one is named beside it.
#
# MAGNITUDE 1 — the real park factors, from the DuckDB in the app container.
#   docker compose run --rm app python -c "
#     import duckdb; con = duckdb.connect('/data/baseball_sim.duckdb', read_only=True)
#     con.execute(\"SELECT venue_id, regressed_factor FROM derived.park_factors
#                   WHERE season=2024 AND factor_type='R' ORDER BY venue_id\").fetchall()"
#   -> 35 rows (2,952 across all seasons). Deviation from 1.0:
#        smallest 0.0025 (venue 2, factor 1.0025)
#        median   0.0273
#        largest  0.1339 (venue 19, factor 1.1339); venue 680 is 0.8724
#
# MAGNITUDE 2 — the run-conversion gap this project is chasing.
#   CLAUDE.md line 85: "Runs run ~7-8% low (down from ~12% pre-fix)".
#   The owner ruled on 2026-08-10 that 7-8% is authoritative and that
#   CLAUDE.md:465's "runs sit ~10-12% low" is stale. The repository wins over any
#   number in a brief, so 7% is the figure used below.
#
# HOW THE TWO CALIBRATE THE INSTRUMENT.
#   A silent neutral 1.0 at the WORST 2024 venue is a 13.4% error in the run
#   environment — nearly double the entire 7-8% residual the fund is trying to
#   close. That is the stake, and one test states it.
#   But the instrument does NOT key on 13.4%. It must red on the SMALLEST real
#   magnitude in the table, 0.25%, because "the source did not answer" is a defect
#   at every venue, not only the extreme ones. Passing on the small real cases
#   while failing on a large invented one was exactly the round-2 failure.
# ===========================================================================

#: Every ``derived.park_factors`` row for season 2024, factor_type='R', read from
#: the DuckDB in the app container on 2026-08-10 by the query quoted above.
#: (venue_id, regressed_factor). These are DATA, not fixtures: the instrument is
#: calibrated against them, so an invented value would decalibrate it.
_PARK_FACTORS_2024_R: tuple[tuple[int, float], ...] = (
    (1, 1.0054770708),
    (2, 1.0025453568),
    (3, 1.0288580656),
    (4, 0.9180047512),
    (5, 1.0030550957),
    (7, 1.0227950811),
    (10, 0.9726824164),
    (12, 0.9179406762),
    (14, 1.0230077505),
    (15, 1.1319301128),
    (17, 0.8880457878),
    (19, 1.1339268684),
    (22, 1.0235017538),
    (31, 1.0083988905),
    (32, 0.9916363955),
    (680, 0.8723846078),
    (2392, 1.0033223629),
    (2394, 0.9621656537),
    (2395, 0.9582160115),
    (2602, 1.0380501747),
    (2680, 1.0091216564),
    (2681, 1.0181443691),
    (2735, 0.9858149290),
    (2889, 0.9759855866),
    (3289, 1.0054286718),
    (3309, 0.9931995273),
    (3312, 1.0519319773),
    (3313, 1.0345611572),
    (3949, 1.0125206709),
    (4169, 1.0925092697),
    (4705, 0.9140601754),
    (5150, 1.0522121191),
    (5325, 0.9535119534),
    (5340, 1.0371358395),
    (5381, 1.0055409670),
)

#: The run-conversion gap on record. Source: CLAUDE.md line 85, "Runs run ~7-8%
#: low". The LOW end of the documented band, so the instrument is calibrated to the
#: least favourable reading of the repository's own number.
_DOCUMENTED_RUN_GAP = 0.07

#: The exact sentence that figure comes from. ``test_the_cited_line_still_says_it``
#: reads CLAUDE.md and fails if it stops saying this, so the citation cannot rot
#: into a number nobody can trace.
_CLAUDE_MD_GAP_PHRASE = "Runs run ~7-8% low"
_CLAUDE_MD_GAP_LINE = 85


def _repo_file_or_skip(rel_path: str) -> Path:
    """A repo-root file, or a skip naming why it is unreachable.

    ``docker-compose.yml`` bind-mounts only api/, pipeline/, similarity/,
    simulation/ and db/, so a repo-ROOT file is absent when this suite runs inside
    the app container. CI runs the unit lane on the host checkout, where every one
    of these guards executes. The skip reason says which situation you are in, so a
    skipped guard is never mistaken for a passing one.
    """
    path = _REPO_ROOT / rel_path
    if not path.is_file():
        pytest.skip(
            f"{rel_path} is not reachable at {path} — repo-root files are not "
            "bind-mounted into the app container. This guard runs on the host "
            '(CI unit lane), or add -v "$PWD/' + rel_path + f':/app/{rel_path}".'
        )
    return path


def test_the_cited_line_still_says_it():
    """The citation is checkable. If CLAUDE.md:85 changes, re-calibrate on purpose."""
    lines = _repo_file_or_skip("CLAUDE.md").read_text(encoding="utf-8").splitlines()
    hits = [i + 1 for i, line in enumerate(lines) if _CLAUDE_MD_GAP_PHRASE in line]
    assert hits, (
        f"SIM-453: CLAUDE.md no longer contains {_CLAUDE_MD_GAP_PHRASE!r}. "
        f"_DOCUMENTED_RUN_GAP={_DOCUMENTED_RUN_GAP} was calibrated to it — find the "
        "new documented run-conversion gap and re-calibrate both."
    )
    assert _CLAUDE_MD_GAP_LINE in hits, (
        f"SIM-453: {_CLAUDE_MD_GAP_PHRASE!r} moved from CLAUDE.md:{_CLAUDE_MD_GAP_LINE} "
        f"to line(s) {hits}. Update _CLAUDE_MD_GAP_LINE so the citation stays exact."
    )


def test_the_measured_park_factors_match_what_the_calibration_claims():
    """Guard the calibration constants themselves against a typo or a quiet edit."""
    devs = sorted(abs(f - 1.0) for _v, f in _PARK_FACTORS_2024_R)
    assert len(_PARK_FACTORS_2024_R) == 35
    assert devs[0] == pytest.approx(0.0025, abs=5e-4)  # the most neutral real park
    assert devs[-1] == pytest.approx(0.1339, abs=5e-4)  # the most extreme real park
    # The stake, in the repository's own units: the worst venue's silent-neutral
    # error is bigger than the ENTIRE documented run-conversion gap.
    assert devs[-1] > _DOCUMENTED_RUN_GAP


@pytest.mark.parametrize(
    ("venue_id", "factor"),
    _PARK_FACTORS_2024_R,
    ids=[f"venue{v}" for v, _f in _PARK_FACTORS_2024_R],
)
async def test_the_instrument_reds_at_EVERY_real_2024_venue(venue_id, factor):
    """CALIBRATION. Not one of the 35 real parks may pass as "resolved" when the
    source is dark — including the most neutral one, whose factor is 1.0025.

    An instrument that only reds on the extreme venues reports health for 27 of the
    35 parks. Sizing the failure is a separate job (below); telling "looked" from
    "did not look" is this one, and it does not depend on size.
    """
    resolved = await resolve_park_run_factor(_FakePool(venue_id), _FakeCon(factor), 777, 2024)
    assert resolved == pytest.approx(factor)
    assert park_factor_reason(resolved) is None  # a real lookup: resolved

    dark = await resolve_park_run_factor(_FakePool(venue_id), None, 777, 2024)
    assert dark == 1.0  # the number the sim would have used
    assert park_factor_reason(dark) is not None, (
        f"SIM-453: venue {venue_id} (real factor {factor}) reports RESOLVED with no "
        "park-factor source. That is the defect: a dark source reading as an answer."
    )


async def test_the_silent_neutral_error_exceeds_the_documented_run_gap():
    """SIZE THE DEFECT in the repository's own units.

    Venue 680 measures 0.8724 in 2024. A dark source sends 1.0 instead. That is a
    12.8% error in the run environment of every game played there — larger than the
    ~7-8% run-conversion residual CLAUDE.md:85 records as the open modelling gap.
    A dark park factor is therefore not a rounding concern. It is bigger than the
    thing the fund is currently trying to fix.
    """
    venue_id, real_factor = 680, 0.8723846078

    resolved = await resolve_park_run_factor(_FakePool(venue_id), _FakeCon(real_factor), 777, 2024)
    dark = await resolve_park_run_factor(_FakePool(venue_id), None, 777, 2024)

    error = abs(float(dark) - float(resolved))
    assert error > _DOCUMENTED_RUN_GAP, (
        f"expected the silent-neutral error at venue {venue_id} to exceed the "
        f"documented {_DOCUMENTED_RUN_GAP:.0%} run gap (CLAUDE.md:{_CLAUDE_MD_GAP_LINE})"
    )
    assert park_factor_reason(dark) is not None


# --- CAN IT FAIL: run the PRE-FIX resolver through the same instrument -------


async def _pre_fix_resolve_park_run_factor(pool, con, game_pk, season):
    """The shipped SIM-452 resolver, verbatim in behaviour.

    Copied from ``simulation/sim_kwargs.py`` before this change: every failure
    branch returns a PLAIN 1.0. Kept here so the instrument is proved against the
    real old code and not against a guess about it.
    """
    if con is None:
        return 1.0
    try:
        row = await pool.fetchrow("SELECT venue_id FROM raw.games WHERE game_pk = $1", int(game_pk))
        venue_id = None if row is None else row["venue_id"]
        if venue_id is None:
            return 1.0
        res = con.execute("...", [int(venue_id), int(season)]).fetchone()
        if not res or res[0] is None:
            return 1.0
        f = float(res[0])
        return f if 0.5 <= f <= 2.0 else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


@pytest.mark.parametrize(
    ("venue_id", "factor"),
    _PARK_FACTORS_2024_R,
    ids=[f"venue{v}" for v, _f in _PARK_FACTORS_2024_R],
)
async def test_PRE_FIX_resolver_FAILS_the_instrument_at_every_real_venue(venue_id, factor):
    """The proof the instrument can go red.

    Run the OLD resolver through the exact assertion the new one passes. It fails
    at all 35 real venues, including the most neutral. A guard that only passes on
    the fixed code proves nothing; this one is shown failing on the broken code.
    """
    dark = await _pre_fix_resolve_park_run_factor(_FakePool(venue_id), None, 777, 2024)
    assert dark == 1.0
    assert park_factor_reason(dark) is None  # the old code: reads as resolved
    assert factor != 1.0  # every real park differs from the neutral it shipped

    with pytest.raises(AssertionError):
        assert park_factor_reason(dark) is not None, "the pre-fix resolver hides the failure"


async def test_PRE_FIX_onto_state_ERASED_the_sentinel_with_a_float_call():
    """Name the single line that caused it: ``float(await resolve_park_run_factor(...))``.

    ``float()`` on a float subclass returns a plain float, so that one call threw
    the sentinel away one line after the resolver produced it.
    """
    erased = float(UNRESOLVED_PARK_FACTOR)
    assert erased == 1.0
    assert park_factor_reason(erased) is None  # the sentinel is gone

    state = _bare_state()
    state.park_run_factor = erased
    assert park_factor_is_resolved(state) is True  # the pre-fix outcome

    # The fixed path writes the value through, so the state keeps the truth.
    state2 = _bare_state()
    mark_park_factor_unresolved(state2)
    await resolve_park_factor_onto_state(state2, _FakePool(15), None, 777)
    assert park_factor_is_resolved(state2) is False


def test_the_sentinel_survives_a_pickle_round_trip():
    """A GameState crosses to a forkserver worker by pickle (SIM-430).

    Without ``__reduce__`` the sentinel comes back a plain 1.0 on the far side, and
    the defect reappears inside the worker where nobody is looking.
    """
    import pickle

    revived = pickle.loads(pickle.dumps(UNRESOLVED_PARK_FACTOR))
    assert revived == 1.0
    assert park_factor_reason(revived) is not None


# --- the LOUD path: warnings, and no implicit park-blind run -----------------


async def test_an_unavailable_source_logs_at_WARNING(caplog):
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")
    await resolve_park_run_factor(_FakePool(15), None, 777, 2024)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "an unavailable park-factor source logged nothing"
    assert "UNRESOLVED" in warnings[0].getMessage()
    assert "no park-factor source" in warnings[0].getMessage()


async def test_the_warning_is_deduplicated_by_cause(caplog):
    """One line per CAUSE. A 2,378-game backtest must not print 2,378 copies."""
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")
    for game_pk in range(20):
        await resolve_park_run_factor(_FakePool(15), None, game_pk, 2024)
    assert len([r for r in caplog.records if "no park-factor source" in r.getMessage()]) == 1


def test_the_park_blind_build_is_never_implicit(caplog):
    """The builder refuses by default. Proceeding is a named argument, and it warns."""
    state = _bare_state()
    mark_park_factor_unresolved(state)

    with pytest.raises(UnresolvedParkFactorError):
        sim_kwargs_from_state(state)

    caplog.set_level("WARNING", logger="simulation.sim_kwargs")
    kw = sim_kwargs_from_state(state, allow_unavailable_park_factor=True)
    assert kw["park_run_factor"] == 1.0
    assert type(kw["park_run_factor"]) is float
    assert any("park-blind" in r.getMessage() for r in caplog.records)


async def test_build_sim_kwargs_rejects_an_unknown_mode():
    state = _bare_state()
    with pytest.raises(ValueError, match="on_unavailable_park_factor"):
        await build_sim_kwargs(
            state, pool=_FakePool(15), con=None, game_pk=777, on_unavailable_park_factor="maybe"
        )


# --- the REAL route, on the DEFAULT stack (no sim DuckDB) --------------------


async def test_the_REAL_route_on_a_stack_with_no_sim_duckdb(monkeypatch, caplog):
    """The documented-defect proof, as a test.

    ``api/routes/games.py::_resolved_sim_kwargs`` is the only way any route builds
    sim kwargs, and it reads ``app.state.sim_duckdb`` — which the default stack
    never sets. Run it exactly as production does, with that attribute None:

      * it still returns kwargs, so /simulate does not become a 500;
      * the state still reports the park factor UNRESOLVED;
      * the log says so at WARNING.

    Before SIM-453 the second and third points were false.
    """
    g = _stub_resolve_game_state(monkeypatch)
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")
    pool = _FakePool(680)  # a real 2024 venue: its factor is 0.8724, not 1.0
    request = _StubRequest(None)  # REPLAY_PERSISTENCE_ENABLED unset -> no connection

    state = await g._resolve_state_or_error(pool, 777)
    kw = await g._resolved_sim_kwargs(request, pool, state, 777)

    assert set(kw) == SIM_KWARG_KEYS  # no 500: the route still builds
    assert kw["park_run_factor"] == 1.0
    assert park_factor_is_resolved(state) is False
    assert any("PARK-BLIND" in r.getMessage() for r in caplog.records)


async def test_the_REAL_route_with_a_source_resolves_the_venue(monkeypatch):
    """The contrast case, so the test above measures the source and not a break."""
    g = _stub_resolve_game_state(monkeypatch)
    pool = _FakePool(680)
    request = _StubRequest(_FakeCon(0.8723846078))

    state = await g._resolve_state_or_error(pool, 777)
    kw = await g._resolved_sim_kwargs(request, pool, state, 777)

    assert kw["park_run_factor"] == pytest.approx(0.8723846078)
    assert park_factor_is_resolved(state) is True


# --- the primed read-only source (requirement 3) -----------------------------


class _PrimedCon:
    """A read-only DuckDB stand-in that answers the row count and the factor."""

    def __init__(self, n_rows: int, factor: float | None) -> None:
        self._n_rows = n_rows
        self._factor = factor
        self.closed = False

    def execute(self, sql, params=None):
        if "count(*)" in sql:
            return _FakeDuckRes((self._n_rows,))
        return _FakeDuckRes(None if self._factor is None else (self._factor,))

    def close(self) -> None:
        self.closed = True


def _prime_with(monkeypatch, con):
    import simulation.sim_kwargs as sk

    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: con)


def test_priming_reports_the_row_count(monkeypatch):
    _prime_with(monkeypatch, _PrimedCon(2952, 0.8723846078))
    source = prime_park_factor_source("/data/baseball_sim.duckdb")
    assert source.available is True
    assert source.n_rows == 2952
    assert source.header_value() == "duckdb:2952"
    assert park_factor_source_status() is source


def test_an_empty_table_is_an_UNAVAILABLE_source_not_a_neutral_one(monkeypatch):
    """0 rows answers nothing. Calling that "available" is the same defect one level up."""
    con = _PrimedCon(0, None)
    _prime_with(monkeypatch, con)
    source = prime_park_factor_source("/data/baseball_sim.duckdb")
    assert source.available is False
    assert source.header_value() == "unavailable"
    assert con.closed is True


def test_a_failed_open_is_reported_not_swallowed(monkeypatch):
    _prime_with(monkeypatch, None)
    source = prime_park_factor_source("/data/nope.duckdb")
    assert source.available is False
    assert "open failed" in source.detail


async def test_the_primed_source_serves_a_route_that_passes_con_None(monkeypatch):
    """THE fix for requirement 3.

    Every ``api/routes/games.py`` route passes ``con=app.state.sim_duckdb``, which
    is None on the default stack. Once the lifespan primes the read-only source,
    that same call resolves the real venue factor instead of shipping a neutral 1.0.
    """
    _prime_with(monkeypatch, _PrimedCon(2952, 0.8723846078))
    prime_park_factor_source("/data/baseball_sim.duckdb")

    f = await resolve_park_run_factor(_FakePool(680), None, 777, 2024)
    assert f == pytest.approx(0.8723846078)
    assert park_factor_reason(f) is None


def test_nothing_is_primed_until_somebody_primes_it():
    """The fallback must not fire in the unit lane. Otherwise ``con=None`` stops
    meaning "unavailable" and every test above quietly starts reading a real file."""
    assert park_factor_source_status() is None


# --- requirement 3: the source open is SEPARATE from replay persistence ------


def _lifespan_ast():
    tree = ast.parse((_REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("api/main.py has no lifespan function")


def _referenced_names(node) -> list[str]:
    """Every bare name mentioned under ``node``.

    Names, not calls: ``api/main.py`` primes through
    ``await asyncio.to_thread(prime_park_factor_source)``, where the function is an
    ARGUMENT and never appears as ``Call.func``.
    """
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def test_the_park_factor_source_is_primed_OUTSIDE_the_replay_gate():
    """Requirement 3, as a source guard.

    Reading park factors must not be gated on REPLAY_PERSISTENCE_ENABLED. If a
    future edit moves the prime back inside that ``if``, the default stack goes
    park-blind again in silence — so the guard reads the source, not the behaviour.
    """
    lifespan = _lifespan_ast()

    assert "prime_park_factor_source" in _referenced_names(lifespan), (
        "api/main.py's lifespan no longer primes the park-factor source; the "
        "default stack is park-blind again."
    )

    gated: list[str] = []
    for node in ast.walk(lifespan):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "replay_enabled"
        ):
            gated += _referenced_names(node)
    assert "prime_park_factor_source" not in gated, (
        "SIM-453: the park-factor source is opened inside `if replay_enabled:` "
        "again. Reading park factors is not replay persistence, and that "
        "conflation is why the factor was dark on the default stack."
    )


def test_docker_compose_does_not_reach_for_the_replay_flag():
    """The alternative we rejected, pinned so nobody quietly takes it.

    Setting REPLAY_PERSISTENCE_ENABLED=true would open a WRITABLE DuckDB
    connection, and the file has no sim.play_stream / sim.state_snapshots tables —
    /plays, /state, /linescore and /card would 500 instead of a clean 503.
    """
    compose = _repo_file_or_skip("docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("REPLAY_PERSISTENCE_ENABLED"), (
            "SIM-453: docker-compose.yml sets REPLAY_PERSISTENCE_ENABLED. That is "
            "the rejected fix — it opens a writable connection and breaks the "
            "replay routes' 503 contract. The park-factor source is opened "
            "read-only in api/main.py instead."
        )
    assert "BASEBALL_DUCKDB_PATH" in compose, (
        "SIM-453: docker-compose.yml no longer names the park-factor source path."
    )


# --- requirement 2: the caller can SEE it ------------------------------------


def _header_for(source):
    from fastapi.testclient import TestClient

    from api.main import create_app

    app = create_app()
    app.state.park_factor_source = source
    # No ``with``: the lifespan is not run here. This exercises the middleware on
    # the real app object, not a boot that needs Postgres, Redis and the engines.
    return TestClient(app).get("/health").headers.get("X-Park-Factor-Source")


def test_the_response_tells_the_caller_the_source_was_unavailable():
    unavailable = ParkFactorSource(
        available=False, path="/data/baseball_sim.duckdb", n_rows=0, detail="open failed"
    )
    assert _header_for(unavailable) == "unavailable"


def test_the_response_tells_the_caller_the_source_was_real():
    available = ParkFactorSource(
        available=True, path="/data/baseball_sim.duckdb", n_rows=2952, detail="read-only"
    )
    assert _header_for(available) == "duckdb:2952"


def test_the_two_header_values_actually_differ():
    """The header is only an instrument if the two states read differently."""
    unavailable = ParkFactorSource(False, "/x", 0, "open failed")
    available = ParkFactorSource(True, "/x", 2952, "read-only")
    assert unavailable.header_value() != available.header_value()


# --- the live file, when this test runs in the app container -----------------


def test_the_real_duckdb_matches_the_calibration_constants():
    """Run in the app container, this proves the constants above are still the data.

    Skipped where /data is not mounted (the CI unit lane), so it never fails for the
    wrong reason — but in the container it is the check that keeps the calibration
    honest as the park-factor table is rebuilt.
    """
    duckdb = pytest.importorskip("duckdb")
    path = Path("/data/baseball_sim.duckdb")
    if not path.exists():
        pytest.skip("no /data/baseball_sim.duckdb — not running in the app container")

    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT venue_id, regressed_factor FROM derived.park_factors "
            "WHERE season = 2024 AND factor_type = 'R' ORDER BY venue_id"
        ).fetchall()
    finally:
        con.close()

    live = {int(v): float(f) for v, f in rows}
    pinned = dict(_PARK_FACTORS_2024_R)
    assert set(live) == set(pinned), (
        "SIM-453: the 2024 venue set moved. Re-read derived.park_factors and "
        "re-pin _PARK_FACTORS_2024_R — the instrument is calibrated to it."
    )
    for venue_id, factor in pinned.items():
        assert live[venue_id] == pytest.approx(factor, abs=1e-6), f"venue {venue_id} moved"
