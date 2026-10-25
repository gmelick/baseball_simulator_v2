"""Tests that the API's runs read each manager's profile from the boot snapshot.

The steal draw weighs each attempted row by the batting manager's steal-order
rate over the season's league mean. Every API route builds its kwargs with no
DuckDB connection (``app.state.sim_duckdb`` is never set), so before the
manager profiles (SIM-575) the resolver returned at once and every API run
carried empty manager dicts: the manager weight was 1.0 for every team.

The fix reads ``derived.manager_season_metrics`` and the manager rows of
``derived.league_averages`` in the same open as the park-factor prime, holds
them in memory as a :class:`ManagerProfileSnapshot`, and CLOSES the file.
``resolve_manager_profiles_onto_state(state, None)`` then reads the snapshot.

These tests use a real DuckDB file in ``tmp_path``. The lock test opens a
writer in a second PROCESS, because DuckDB's lock is per process.

The new names are read as module attributes (``sk.ManagerProfileSnapshot``,
``sk._MANAGER_PROFILES``), so the file collects before the code exists and each
test fails on its own reason.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

duckdb = pytest.importorskip("duckdb")

import simulation.sim_kwargs as sk  # noqa: E402
from simulation.game_state import GameState  # noqa: E402
from simulation.sim_kwargs import (  # noqa: E402
    MANAGER_TENDENCY_COLUMNS,
    build_sim_kwargs,
    close_park_factor_source,
    prime_park_factor_source,
    refresh_park_factor_snapshot,
    reset_park_factor_warnings,
    resolve_manager_profiles_onto_state,
)

_STEAL = "steal_order_rate_per_1b_opp"
_N = len(MANAGER_TENDENCY_COLUMNS)


@pytest.fixture(autouse=True)
def _clean_park_factor_globals():
    reset_park_factor_warnings()
    close_park_factor_source()
    yield
    reset_park_factor_warnings()
    close_park_factor_source()


class _FakePool:
    """A single asyncpg-style connection: ``fetchrow`` answers the venue."""

    def __init__(self, venue_id):
        self._venue_id = venue_id

    async def fetchrow(self, _sql, _game_pk):
        return None if self._venue_id is None else {"venue_id": self._venue_id}


# --- the file writer ----------------------------------------------------------


_PARK_ROWS = [
    (680, 2024, "R", 0.8723846078),
    (19, 2024, "R", 1.18),
]


def _profile_row(base: float) -> list[float | None]:
    """One manager-season's tendency values: ``base + i`` for column ``i``."""
    return [base + i for i in range(_N)]


def _manager_row(
    manager_id: int, season: int, values: list[float | None], *, small: bool = False
) -> tuple:
    return (manager_id, season, *values, small)


# Manager 11: a 2024 row. Manager 22: only a 2023 row (the season-before chain).
# Manager 33: a 2024 row below the minimum sample (never kept). Manager 44: a
# 2024 row with its first column None (the column is dropped).
_MANAGER_ROWS = [
    _manager_row(11, 2024, _profile_row(0.0)),
    _manager_row(22, 2023, _profile_row(100.0)),
    _manager_row(33, 2024, _profile_row(500.0), small=True),
    _manager_row(44, 2024, [None, *_profile_row(1.0)[1:]]),
]

# One league row per season; 2024 carries a null mean (dropped). No 2022 row.
_LEAGUE_ROWS = [
    ("manager", 2024, '{"steal_order_rate_per_1b_opp": 0.05, "opener_usage_rate": null}'),
    ("manager", 2023, '{"steal_order_rate_per_1b_opp": 0.04}'),
    ("batter", 2024, '{"steal_order_rate_per_1b_opp": 9.9}'),  # not a manager row
]


def _write_park_factors(con, rows) -> None:
    con.execute("DROP TABLE IF EXISTS derived.park_factors")
    con.execute(
        "CREATE TABLE derived.park_factors (venue_id INTEGER, season INTEGER, "
        "factor_type VARCHAR, regressed_factor DOUBLE)"
    )
    if rows:
        con.executemany("INSERT INTO derived.park_factors VALUES (?, ?, ?, ?)", rows)


def _write_managers(con, manager_rows, league_rows) -> None:
    con.execute("DROP TABLE IF EXISTS derived.manager_season_metrics")
    cols = ", ".join(f"{c} FLOAT" for c in MANAGER_TENDENCY_COLUMNS)
    con.execute(
        "CREATE TABLE derived.manager_season_metrics (manager_id INTEGER, season INTEGER, "
        f"{cols}, below_minimum_sample BOOLEAN)"
    )
    if manager_rows:
        marks = ", ".join(["?"] * (_N + 3))
        con.executemany(
            f"INSERT INTO derived.manager_season_metrics VALUES ({marks})", manager_rows
        )
    # The real table (pipeline/batch/profiles/league_averages.py) holds a JSON
    # column; a VARCHAR holding the JSON text is what the resolver parses.
    con.execute("DROP TABLE IF EXISTS derived.league_averages")
    con.execute(
        "CREATE TABLE derived.league_averages (entity_type VARCHAR, season INTEGER, "
        "profile_json VARCHAR)"
    )
    if league_rows:
        con.executemany("INSERT INTO derived.league_averages VALUES (?, ?, ?)", league_rows)


def _write_db(
    path: str,
    park_rows=_PARK_ROWS,
    manager_rows=_MANAGER_ROWS,
    league_rows=_LEAGUE_ROWS,
    *,
    with_managers: bool = True,
) -> None:
    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS derived")
        _write_park_factors(con, park_rows)
        if with_managers:
            _write_managers(con, manager_rows, league_rows)
    finally:
        con.close()


def _bump_mtime(path: str) -> None:
    later = time.time() + 5
    os.utime(path, (later, later))


@pytest.fixture
def sim_db(tmp_path) -> str:
    path = str(tmp_path / "baseball_sim.duckdb")
    _write_db(path)
    return path


def _state(home: int | None = 11, away: int | None = 22, season: int = 2024) -> GameState:
    s = GameState(pitcher_id=1, bat_hand="R", season=season)
    s.home_manager_id, s.away_manager_id = home, away
    return s


def _expected(base: float) -> dict[str, float]:
    return {c: pytest.approx(base + i) for i, c in enumerate(MANAGER_TENDENCY_COLUMNS)}


# --- the prime ----------------------------------------------------------------


def test_the_prime_fills_the_manager_snapshot_and_skips_a_small_sample(sim_db):
    source = prime_park_factor_source(sim_db)

    snap = sk._MANAGER_PROFILES
    assert isinstance(snap, sk.ManagerProfileSnapshot)
    assert set(snap.profiles) == {(11, 2024), (22, 2023), (44, 2024)}
    assert (33, 2024) not in snap.profiles  # below the minimum sample
    assert snap.profiles[(11, 2024)] == _expected(0.0)
    assert set(snap.league) == {2023, 2024}
    assert snap.league[2024] == {_STEAL: pytest.approx(0.05)}

    assert source.n_manager_profiles == 3
    assert source.n_manager_league_seasons == 2
    # The park verdict does not move.
    assert source.available is True
    assert source.n_rows == len(_PARK_ROWS)
    assert source.header_value() == f"duckdb:{len(_PARK_ROWS)}"


# --- the resolver on the snapshot ---------------------------------------------


def test_a_con_None_resolve_writes_both_sides_and_the_league_means(sim_db):
    prime_park_factor_source(sim_db)
    s = _state()
    resolve_manager_profiles_onto_state(s, None)

    assert s.home_manager_profile == _expected(0.0)
    assert s.away_manager_profile[_STEAL] == pytest.approx(100.0 + 7)
    assert s.manager_league_profile == {_STEAL: pytest.approx(0.05)}


def test_the_season_before_is_the_fallback(sim_db):
    prime_park_factor_source(sim_db)
    s = _state(home=22, away=None)
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == _expected(100.0)
    assert s.away_manager_profile == {}


def test_an_unknown_manager_reads_empty(sim_db):
    prime_park_factor_source(sim_db)
    s = _state(home=999, away=33)  # 33 exists only below the minimum sample
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == {}
    assert s.away_manager_profile == {}
    assert s.manager_league_profile == {_STEAL: pytest.approx(0.05)}


def test_an_id_that_is_not_a_number_reads_empty_on_both_paths(sim_db):
    # The snapshot path and the connection path give the same answer: no profile.
    prime_park_factor_source(sim_db)
    via_snapshot = _state(home=None, away=22)
    via_snapshot.home_manager_id = "x"  # type: ignore[assignment]
    resolve_manager_profiles_onto_state(via_snapshot, None)
    assert via_snapshot.home_manager_profile == {}
    assert via_snapshot.away_manager_profile[_STEAL] == pytest.approx(100.0 + 7)

    via_con = _state(home=None, away=22)
    via_con.home_manager_id = "x"  # type: ignore[assignment]
    con = duckdb.connect(sim_db, read_only=True)
    try:
        resolve_manager_profiles_onto_state(via_con, con)
    finally:
        con.close()
    assert via_con.home_manager_profile == {}
    assert via_snapshot.away_manager_profile == pytest.approx(via_con.away_manager_profile)


def test_a_season_with_no_league_row_reads_empty(sim_db):
    prime_park_factor_source(sim_db)
    s = _state(home=11, away=None, season=2022)
    resolve_manager_profiles_onto_state(s, None)
    assert s.manager_league_profile == {}
    assert s.home_manager_profile == {}  # neither 2022 nor 2021 holds manager 11


def test_a_None_column_is_dropped(sim_db):
    prime_park_factor_source(sim_db)
    s = _state(home=44, away=None)
    resolve_manager_profiles_onto_state(s, None)
    assert MANAGER_TENDENCY_COLUMNS[0] not in s.home_manager_profile
    assert len(s.home_manager_profile) == _N - 1


@pytest.mark.parametrize(
    ("home", "away", "season"),
    [
        (11, 22, 2024),
        (22, 11, 2024),
        (44, 33, 2024),
        (999, None, 2024),
        (11, 22, 2023),
        (11, 22, 2022),
    ],
)
def test_the_snapshot_gives_the_same_profiles_as_a_connection(sim_db, home, away, season):
    prime_park_factor_source(sim_db)
    via_snapshot = _state(home, away, season)
    resolve_manager_profiles_onto_state(via_snapshot, None)

    via_con = _state(home, away, season)
    con = duckdb.connect(sim_db, read_only=True)
    try:
        resolve_manager_profiles_onto_state(via_con, con)
    finally:
        con.close()

    assert via_snapshot.home_manager_profile == pytest.approx(via_con.home_manager_profile)
    assert via_snapshot.away_manager_profile == pytest.approx(via_con.away_manager_profile)
    assert via_snapshot.manager_league_profile == pytest.approx(via_con.manager_league_profile)
    assert set(via_snapshot.home_manager_profile) == set(via_con.home_manager_profile)
    assert set(via_snapshot.away_manager_profile) == set(via_con.away_manager_profile)


# --- a file with no manager table ---------------------------------------------


def test_a_file_with_no_manager_table_keeps_the_park_factors(tmp_path, caplog):
    path = str(tmp_path / "parks_only.duckdb")
    _write_db(path, with_managers=False)
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")

    source = prime_park_factor_source(path)

    assert source.available is True
    assert source.n_rows == len(_PARK_ROWS)
    assert source.n_manager_profiles == 0
    assert source.n_manager_league_seasons == 0
    snap = sk._MANAGER_PROFILES
    assert isinstance(snap, sk.ManagerProfileSnapshot)
    assert snap.profiles == {} and snap.league == {}

    s = _state()
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == {} and s.away_manager_profile == {}
    assert s.manager_league_profile == {}

    warnings = [r for r in caplog.records if r.getMessage().startswith("SIM-575")]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


def test_the_reader_never_raises_on_a_missing_table(tmp_path):
    path = str(tmp_path / "parks_only.duckdb")
    _write_db(path, with_managers=False)
    con = duckdb.connect(path, read_only=True)
    try:
        snap = sk._read_manager_profile_rows(con)
    finally:
        con.close()
    assert isinstance(snap, sk.ManagerProfileSnapshot)
    assert snap.profiles == {} and snap.league == {}


def test_the_reader_reads_a_real_connection(sim_db):
    con = duckdb.connect(sim_db, read_only=True)
    try:
        snap = sk._read_manager_profile_rows(con)
    finally:
        con.close()
    assert set(snap.profiles) == {(11, 2024), (22, 2023), (44, 2024)}
    assert snap.league[2023] == {_STEAL: pytest.approx(0.04)}


# --- the refresh --------------------------------------------------------------


async def test_a_rebuilt_file_is_re_read_without_a_restart(sim_db, monkeypatch):
    prime_park_factor_source(sim_db)
    _write_db(
        sim_db,
        manager_rows=[_manager_row(11, 2024, _profile_row(7.0))],
        league_rows=[("manager", 2024, '{"steal_order_rate_per_1b_opp": 0.06}')],
    )
    _bump_mtime(sim_db)

    # The check runs at most once a minute; make the minute pass.
    monkeypatch.setattr(sk, "_PARK_FACTORS_CHECKED_AT", time.monotonic() - 3600)
    kw = await build_sim_kwargs(_state(), pool=_FakePool(680), con=None, game_pk=777)

    assert kw["manager_profiles"][1][_STEAL] == pytest.approx(7.0 + 7)
    assert kw["manager_profiles"][0] == {}  # manager 22's rows are gone
    assert kw["manager_league_profile"] == {_STEAL: pytest.approx(0.06)}
    assert sk.park_factor_source_status().n_manager_profiles == 1


def test_a_failed_re_read_keeps_the_old_manager_snapshot(sim_db, monkeypatch):
    """A rebuild holds the write lock: the open fails, the app serves what it has."""
    prime_park_factor_source(sim_db)
    old = sk._MANAGER_PROFILES
    _bump_mtime(sim_db)
    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: None)

    assert refresh_park_factor_snapshot() is False

    assert sk._MANAGER_PROFILES is old
    s = _state()
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == _expected(0.0)


def test_a_failed_boot_prime_recovers_the_manager_snapshot_on_a_later_check(sim_db, monkeypatch):
    """A boot during a rebuild: the open fails, then the next check reads both snapshots."""
    with monkeypatch.context() as m:
        m.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: None)
        source = prime_park_factor_source(sim_db)
    assert source.available is False
    assert sk._MANAGER_PROFILES is None

    assert refresh_park_factor_snapshot() is True

    snap = sk._MANAGER_PROFILES
    assert isinstance(snap, sk.ManagerProfileSnapshot)
    assert (11, 2024) in snap.profiles
    assert sk.park_factor_source_status().n_manager_profiles == 3
    s = _state()
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == _expected(0.0)


def test_a_park_read_that_keeps_failing_logs_the_manager_warning_once(tmp_path, caplog):
    """An empty park table re-reads the file on every check; the warning stays one line."""
    path = str(tmp_path / "empty_parks.duckdb")
    _write_db(path, park_rows=[], with_managers=False)
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")

    assert prime_park_factor_source(path).available is False
    for _ in range(3):
        assert refresh_park_factor_snapshot() is False

    warnings = [r for r in caplog.records if r.getMessage().startswith("SIM-575")]
    assert len(warnings) == 1
    assert "\n" not in warnings[0].getMessage()


def test_a_writer_in_another_process_can_open_the_file_after_the_prime(sim_db):
    """The lock regression: the manager read closes the file with the park read."""
    source = prime_park_factor_source(sim_db)
    assert source.available is True
    assert source.n_manager_profiles == 3

    writer = textwrap.dedent(
        f"""
        import duckdb
        con = duckdb.connect({sim_db!r})
        con.execute("CREATE TABLE IF NOT EXISTS derived.sim575_probe (x INTEGER)")
        con.execute("INSERT INTO derived.sim575_probe VALUES (1)")
        con.close()
        print("WROTE")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", writer], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, (
        "A writer in another process could not open the file after the prime that "
        f"reads the manager profiles. stderr:\n{proc.stderr}"
    )
    assert "WROTE" in proc.stdout


# --- the unit-lane invariance and the shutdown --------------------------------


def test_nothing_primed_leaves_the_profiles_empty():
    """With no prime the resolver reads nothing, so the unit lane reads no file."""
    assert sk._MANAGER_PROFILES is None
    s = _state()
    resolve_manager_profiles_onto_state(s, None)
    assert s.home_manager_profile == {} and s.away_manager_profile == {}
    assert s.manager_league_profile == {}


def test_close_clears_the_manager_snapshot(sim_db):
    prime_park_factor_source(sim_db)
    assert sk._MANAGER_PROFILES is not None
    close_park_factor_source()
    assert sk._MANAGER_PROFILES is None
