"""SIM-524 — the app holds no handle on the statistics DuckDB file.

DuckDB lets a WRITER open a file only when no other process holds it, read-only
handles included. The park-factor prime kept a read-only connection open for the
life of the app, so every rebuild (a profile recompute, a pool rebuild, the
nightly job) failed with "Conflicting lock is held" until somebody stopped the
app. The prime now reads the run factors into memory and closes the file.

These tests use a real DuckDB file. The lock test opens the writer in a second
PROCESS, because DuckDB's lock is per process: a writer in the same process
would not prove anything.
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
from simulation.sim_kwargs import (  # noqa: E402
    close_park_factor_source,
    park_factor_reason,
    prime_park_factor_source,
    refresh_park_factor_snapshot,
    reset_park_factor_warnings,
    resolve_park_run_factor,
)


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


def _write_park_factors(path: str, rows: list[tuple[int, int, str, float | None]]) -> None:
    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS derived")
        con.execute("DROP TABLE IF EXISTS derived.park_factors")
        con.execute(
            "CREATE TABLE derived.park_factors (venue_id INTEGER, season INTEGER, "
            "factor_type VARCHAR, regressed_factor DOUBLE)"
        )
        if rows:
            con.executemany("INSERT INTO derived.park_factors VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()


_ROWS = [
    (680, 2024, "R", 0.8723846078),
    (680, 2024, "HR", 0.80),  # a non-run row: counted, never served
    (19, 2024, "R", 1.18),
    (20, 2024, "R", None),  # the row exists, the factor does not
    (21, 2024, "R", 9.9),  # outside the sane range
]


@pytest.fixture
def park_db(tmp_path) -> str:
    path = str(tmp_path / "baseball_sim.duckdb")
    _write_park_factors(path, _ROWS)
    return path


def test_a_writer_in_another_process_can_open_the_file_after_the_prime(park_db):
    """THE regression test for the lock."""
    source = prime_park_factor_source(park_db)
    assert source.available is True

    writer = textwrap.dedent(
        f"""
        import duckdb
        con = duckdb.connect({park_db!r})
        con.execute("CREATE TABLE IF NOT EXISTS derived.sim524_probe (x INTEGER)")
        con.execute("INSERT INTO derived.sim524_probe VALUES (1)")
        con.close()
        print("WROTE")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", writer], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, (
        "SIM-524: a writer in another process could not open the file after the "
        f"park-factor prime. stderr:\n{proc.stderr}"
    )
    assert "WROTE" in proc.stdout


def test_the_prime_counts_every_row_and_keeps_the_run_factors(park_db):
    source = prime_park_factor_source(park_db)
    assert source.n_rows == len(_ROWS)
    assert source.header_value() == f"duckdb:{len(_ROWS)}"
    assert sk._PARK_FACTORS == {
        (680, 2024): 0.8723846078,
        (19, 2024): 1.18,
        (20, 2024): None,
        (21, 2024): 9.9,
    }


async def test_a_con_None_lookup_reads_the_snapshot(park_db):
    prime_park_factor_source(park_db)
    f = await resolve_park_run_factor(_FakePool(680), None, 777, 2024)
    assert f == pytest.approx(0.8723846078)
    assert park_factor_reason(f) is None


@pytest.mark.parametrize(
    ("venue", "season", "phrase"),
    [
        (None, 2024, "no venue_id"),
        (999, 2024, "no factor_type='R' row"),
        (680, 2023, "no factor_type='R' row"),
        (20, 2024, "no factor_type='R' row"),
        (21, 2024, "outside the sane"),
    ],
)
async def test_the_snapshot_gives_the_same_reasons_as_a_connection(park_db, venue, season, phrase):
    prime_park_factor_source(park_db)
    via_snapshot = await resolve_park_run_factor(_FakePool(venue), None, 777, season)

    con = duckdb.connect(park_db, read_only=True)
    try:
        via_con = await resolve_park_run_factor(_FakePool(venue), con, 777, season)
    finally:
        con.close()

    assert via_snapshot == 1.0
    assert phrase in park_factor_reason(via_snapshot)
    assert park_factor_reason(via_snapshot) == park_factor_reason(via_con)


def test_an_empty_table_is_unavailable(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    _write_park_factors(path, [])
    source = prime_park_factor_source(path)
    assert source.available is False
    assert sk._PARK_FACTORS is None


def test_a_missing_file_is_unavailable(tmp_path):
    source = prime_park_factor_source(str(tmp_path / "nope.duckdb"))
    assert source.available is False
    assert "open failed" in source.detail


def _bump_mtime(path: str) -> None:
    later = time.time() + 5
    os.utime(path, (later, later))


async def test_a_rebuilt_file_is_re_read_without_a_restart(park_db, monkeypatch):
    prime_park_factor_source(park_db)
    _write_park_factors(park_db, [(680, 2024, "R", 1.05)])
    _bump_mtime(park_db)

    # The check runs at most once a minute; make the minute pass.
    monkeypatch.setattr(sk, "_PARK_FACTORS_CHECKED_AT", time.monotonic() - 3600)
    f = await resolve_park_run_factor(_FakePool(680), None, 777, 2024)

    assert f == pytest.approx(1.05)
    assert sk.park_factor_source_status().n_rows == 1


async def test_no_check_runs_inside_the_minute(park_db):
    prime_park_factor_source(park_db)
    _write_park_factors(park_db, [(680, 2024, "R", 1.05)])
    _bump_mtime(park_db)

    f = await resolve_park_run_factor(_FakePool(680), None, 777, 2024)
    assert f == pytest.approx(0.8723846078)


def test_a_failed_re_read_keeps_the_old_snapshot(park_db, monkeypatch, caplog):
    """A rebuild holds the write lock: the open fails, the app serves what it has."""
    prime_park_factor_source(park_db)
    old_source = sk.park_factor_source_status()
    _bump_mtime(park_db)
    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: None)
    caplog.set_level("WARNING", logger="simulation.sim_kwargs")

    assert refresh_park_factor_snapshot() is False
    assert refresh_park_factor_snapshot() is False

    assert sk.park_factor_source_status() is old_source
    assert sk._PARK_FACTORS[(680, 2024)] == pytest.approx(0.8723846078)
    # One line for the whole streak, not one per check.
    assert len([r for r in caplog.records if "SIM-524" in r.getMessage()]) == 1


def test_an_unchanged_file_is_not_re_read(park_db, monkeypatch):
    prime_park_factor_source(park_db)
    calls = []
    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *a, **k: calls.append(a))
    assert refresh_park_factor_snapshot() is False
    assert calls == []
