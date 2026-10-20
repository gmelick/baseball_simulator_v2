"""SIM-563 — the workers read only the bundle, the app restarts on a new version,
and a boot waits for a DuckDB writer instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from api import bundle_watch
from api.state import retry_on_duckdb_lock
from pipeline.batch import engine_artifacts as ea
from simulation import production_factory as pf
from simulation import sim_kwargs as sk

duckdb = pytest.importorskip("duckdb")


# --- Part B: the reference tables ride the bundle ---------------------------


@pytest.fixture
def ref_db(tmp_path) -> str:
    path = str(tmp_path / "ref.duckdb")
    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA sim")
        con.execute("CREATE SCHEMA derived")
        con.execute(
            "CREATE TABLE sim.ibb_rates (runners_state INTEGER, outs INTEGER, is_late BOOLEAN, "
            "is_close BOOLEAN, opportunities INTEGER, issued INTEGER)"
        )
        con.execute(
            "INSERT INTO sim.ibb_rates VALUES (2, 1, true, true, 200, 10), (0, 0, false, false, 0, 0)"
        )
        con.execute(
            "CREATE TABLE derived.park_factors (venue_id INTEGER, season INTEGER, "
            "factor_type VARCHAR, regressed_factor DOUBLE)"
        )
        con.execute(
            "INSERT INTO derived.park_factors VALUES (19, 2024, 'R', 1.18), "
            "(20, 2024, 'R', 9.9), (19, 2024, 'HR', 1.3)"
        )
    finally:
        con.close()
    return path


def test_the_reference_target_writes_both_tables(ref_db, tmp_path):
    out = tmp_path / "bundle"
    con = duckdb.connect(ref_db, read_only=True)
    try:
        counts = ea.build_reference_tables(con, str(out))
    finally:
        con.close()
    assert counts == {ea.IBB_RATES_FILE: 1, ea.VENUE_RUN_FACTORS_FILE: 2}
    rows = json.loads((out / ea.IBB_RATES_FILE).read_text())["rows"]
    assert rows == [[2, 1, True, True, 200, 10]]


def test_the_readers_give_the_worker_loaders_numbers():
    assert ea.ibb_rates_from_rows([[2, 1, True, True, 200, 10]]) == {(2, 1, True, True): 0.05}
    assert ea.ibb_rates_from_rows([]) is None
    assert ea.venue_run_factors_from_rows([[19, 2024, 1.18], [20, 2024, 9.9]]) == {(19, 2024): 1.18}


def test_a_missing_table_writes_no_file(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    con = duckdb.connect(path, read_only=True)
    try:
        assert ea.build_reference_tables(con, str(tmp_path / "b")) == {}
    finally:
        con.close()
    assert not (tmp_path / "b" / ea.IBB_RATES_FILE).exists()


# --- Part B: the pinned version ----------------------------------------------


class _Spec:
    def __init__(self, kwargs):
        self.sim_kwargs = kwargs


def test_the_worker_reads_the_pinned_version(monkeypatch):
    monkeypatch.setenv(pf.PINNED_ARTIFACT_DIR_ENV, "/data/play_pool/engine_artifacts.versions/X")
    assert pf._artifact_dir(_Spec({})) == "/data/play_pool/engine_artifacts.versions/X"
    # An explicit pool dir (a script, a test) still wins over the pin.
    assert pf._artifact_dir(_Spec({"_pool_dir": "/tmp/p"})) == os.path.join(
        "/tmp/p", "engine_artifacts"
    )


def test_no_pin_reads_the_link(monkeypatch):
    monkeypatch.delenv(pf.PINNED_ARTIFACT_DIR_ENV, raising=False)
    monkeypatch.setenv("BASEBALL_PLAY_POOL_DIR", "/data/play_pool")
    assert pf._artifact_dir(_Spec({})) == os.path.join("/data/play_pool", "engine_artifacts")


def test_pin_sets_the_env_and_marks_the_version(tmp_path, monkeypatch):
    version = tmp_path / "v1"
    version.mkdir()
    monkeypatch.delenv(bundle_watch.PINNED_ENV, raising=False)
    pinned, marker = bundle_watch.pin_bundle_version(str(version))
    try:
        assert os.environ[bundle_watch.PINNED_ENV] == os.path.realpath(version)
        assert marker and os.path.exists(marker)
    finally:
        os.environ.pop(bundle_watch.PINNED_ENV, None)


def test_a_failed_ibb_open_is_not_cached(monkeypatch):
    """A rebuild holding the lock is temporary; the next build tries again."""
    pf.reset_caches()
    calls = []

    def _open(*_a, **_k):
        calls.append(1)
        return None

    monkeypatch.setattr(sk, "open_sim_duckdb", _open)
    assert pf._load_ibb_rates() is None
    assert pf._load_ibb_rates() is None
    assert len(calls) == 2
    pf.reset_caches()


# --- Part C: the restart watch ----------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _watch(link, loaded, jobs, *, writer_free=lambda: True, drain_cap=600.0):
    clock = _Clock()
    kills: list[int] = []

    async def _sleep(s):
        clock.t += s
        await asyncio.sleep(0)  # yield, so a bounded test can time the loop out

    async def _go():
        return await bundle_watch.watch_for_new_bundle(
            link=link,
            loaded=loaded,
            active_jobs=jobs,
            writer_free=writer_free,
            restart=lambda: kills.append(1),
            interval=60.0,
            drain_cap=drain_cap,
            sleep=_sleep,
            clock=clock,
        )

    return _go, kills, clock


def test_a_new_version_restarts_once_the_jobs_finish(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    pending = [3]

    def jobs():
        pending[0] = max(0, pending[0] - 1)
        return pending[0]

    go, kills, clock = _watch(str(new), os.path.realpath(old), jobs)
    assert asyncio.run(go()) == os.path.realpath(new)
    assert kills == [1]


def test_jobs_that_never_finish_restart_at_the_cap(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    go, kills, clock = _watch(str(new), os.path.realpath(old), lambda: 1, drain_cap=600.0)
    asyncio.run(go())
    assert kills == [1]
    assert clock.t >= 600.0


def test_the_restart_waits_for_the_writer(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    free = iter([False, False, True])
    go, kills, clock = _watch(
        str(new), os.path.realpath(old), lambda: 0, writer_free=lambda: next(free)
    )
    asyncio.run(go())
    assert kills == [1]
    assert clock.t >= 180.0  # the first look plus two waits on the writer


def test_an_unchanged_link_never_restarts(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    go, kills, clock = _watch(str(v), os.path.realpath(v), lambda: 0)

    async def _bounded():
        try:
            await asyncio.wait_for(go(), timeout=0.2)
        except TimeoutError:
            pass

    asyncio.run(_bounded())
    assert kills == []


def test_the_restart_is_off_by_default(monkeypatch):
    monkeypatch.delenv(bundle_watch.RESTART_ENV, raising=False)
    assert bundle_watch.restart_enabled() is False
    monkeypatch.setenv(bundle_watch.RESTART_ENV, "1")
    assert bundle_watch.restart_enabled() is True


def test_the_writer_check_reads_a_free_file_and_a_missing_one(tmp_path):
    path = str(tmp_path / "x.duckdb")
    assert bundle_watch.duckdb_writer_free(path) is True  # absent
    duckdb.connect(path).close()
    assert bundle_watch.duckdb_writer_free(path) is True


# --- Part D: the boot waits for a writer --------------------------------------


def _lock_error():
    return duckdb.IOException('IO Error: Could not set lock on file "x": Conflicting lock is held')


def test_a_lock_error_is_retried_until_the_build_succeeds():
    tries = []
    waits = []

    def build():
        tries.append(1)
        if len(tries) < 3:
            raise _lock_error()
        return "built"

    assert retry_on_duckdb_lock(build, "test", max_wait_s=600, sleep=waits.append) == "built"
    assert len(tries) == 3
    assert len(waits) == 2


def test_another_error_is_not_retried():
    def build():
        raise ValueError("bad profile")

    with pytest.raises(ValueError):
        retry_on_duckdb_lock(build, "test", max_wait_s=600, sleep=lambda _s: None)


def test_the_wait_gives_up_at_its_limit():
    clock = _Clock()

    def sleep(s):
        clock.t += s

    def build():
        raise _lock_error()

    with pytest.raises(duckdb.IOException):
        retry_on_duckdb_lock(build, "test", max_wait_s=60, sleep=sleep, clock=clock)
    assert clock.t >= 60


# --- Part D: a failed park-factor prime recovers -------------------------------


def test_a_failed_prime_recovers_on_a_later_check(tmp_path, monkeypatch):
    sk.close_park_factor_source()
    path = str(tmp_path / "pf.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA derived")
    con.execute(
        "CREATE TABLE derived.park_factors (venue_id INTEGER, season INTEGER, "
        "factor_type VARCHAR, regressed_factor DOUBLE)"
    )
    con.execute("INSERT INTO derived.park_factors VALUES (19, 2024, 'R', 1.18)")
    con.close()

    real_open = sk.open_sim_duckdb
    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: None)
    assert sk.prime_park_factor_source(path).available is False

    monkeypatch.setattr(sk, "open_sim_duckdb", real_open)
    try:
        assert sk.refresh_park_factor_snapshot() is True
        assert sk.park_factor_source_status().available is True
        assert sk._PARK_FACTORS == {(19, 2024): 1.18}
    finally:
        sk.close_park_factor_source()
