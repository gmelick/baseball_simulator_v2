"""The pitcher arsenal fit gives the same arsenal whatever order the pitch rows arrive in.

The seeded mixture fit (``random_state=42``) depends on the order of its input rows, and
DuckDB's parallel Postgres scan returns the rows in a new order on every read. So the
pitch-vector query of ``_compute_pitcher_profiles`` orders the rows by the table's key,
``(game_pk, at_bat_number, pitch_number)`` (SIM-580). Two layers under test:

  * the definition of done: the same pitches, stored in two different orders, give the
    same arsenals by content (the ``gmm_model`` JSON without its ``fit_date``, and the
    component rows);
  * the key: the fit receives each pitcher's rows in key order, whatever the stored order.

The fixture runs the real method over an in-memory DuckDB with a stub ``pg.raw.pitches``
(an in-memory table returns its rows in insertion order, so a shuffled insert stands in for
the parallel scan) and the real DDL of the two derived tables. A thread pool stands in for
the process pool, so no process forks in the unit lane.
"""

from __future__ import annotations

import concurrent.futures
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pytest

from pipeline.batch.player_profile_computor import PlayerProfileComputor
from pipeline.batch.profiles import pitcher as pitcher_module
from pipeline.batch.profiles.gmm import GMM_FEATURE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = (REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql").read_text(encoding="utf-8")

SEASON = 2024
PITCHERS = (101, 202)
PITCHES_PER_PITCHER = 250

# The eight raw shape columns, in the order of GMM_FEATURE_NAMES.
SHAPE_COLUMNS = (
    "release_speed",
    "break_vertical_induced",
    "break_horizontal",
    "release_spin_rate",
    "spin_axis",
    "release_pos_x",
    "release_pos_z",
    "release_extension",
)

# Three overlapping pitch shapes (a fastball, a slider, a changeup), in raw units.
SHAPE_MEANS = np.array(
    [
        [94.0, 16.0, -8.0, 2300.0, 210.0, -1.8, 5.9, 6.3],
        [86.0, 2.0, 4.0, 2450.0, 120.0, -1.9, 5.8, 6.2],
        [87.0, 8.0, -12.0, 1800.0, 240.0, -1.8, 5.8, 6.4],
    ]
)
SHAPE_SDS = np.array([1.5, 2.5, 2.5, 120.0, 20.0, 0.15, 0.15, 0.2])


def _real_ddl(table: str) -> str:
    start = SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = SCHEMA_SQL.index("\n);", start)
    return SCHEMA_SQL[start : end + 3]


def _pitch_rows() -> list[tuple]:
    """Both pitchers' pitches, in key order. Each row is one unique key."""
    rng = np.random.default_rng(580)
    rows = []
    for i in range(PITCHES_PER_PITCHER):
        # 50 pitches a game for each pitcher: ten at-bats of five pitches.
        game_pk, at_bat_slot, pitch_number = 1000 + i // 50, (i % 50) // 5, i % 5 + 1
        for p_index, pitcher in enumerate(PITCHERS):
            shape = SHAPE_MEANS[rng.integers(0, len(SHAPE_MEANS))]
            values = shape + rng.normal(0.0, 1.0, size=len(SHAPE_SDS)) * SHAPE_SDS
            events = "strikeout" if pitch_number == 5 else None
            rows.append(
                (
                    pitcher,
                    SEASON,
                    f"{SEASON}-05-01",
                    game_pk,
                    2 * at_bat_slot + p_index + 1,
                    pitch_number,
                    "R",
                    5,
                    "S" if events else "B",
                    events,
                    None,
                    0,
                    False,
                    *([False] * 6),
                    *(float(v) for v in values),
                )
            )
    return rows


def _conn(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS pg")
    c.execute("CREATE SCHEMA pg.raw")
    c.execute(
        "CREATE TABLE pg.raw.pitches ("
        "pitcher INTEGER, season SMALLINT, game_date DATE, "
        "game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER, "
        "p_throws VARCHAR, zone INTEGER, type VARCHAR, events VARCHAR, bb_type VARCHAR, "
        "earned_runs_on_pitch INTEGER, data_quality_flag BOOLEAN, "
        "sb_attempt_2b BOOLEAN, sb_attempt_3b BOOLEAN, sb_attempt_home BOOLEAN, "
        "sb_success_2b BOOLEAN, sb_success_3b BOOLEAN, sb_success_home BOOLEAN, "
        + ", ".join(f"{col} DOUBLE" for col in SHAPE_COLUMNS)
        + ")"
    )
    c.executemany(f"INSERT INTO pg.raw.pitches VALUES ({', '.join(['?'] * 27)})", rows)
    c.execute("CREATE SCHEMA derived")
    c.execute(_real_ddl("derived.pitcher_season_metrics"))
    c.execute(_real_ddl("derived.pitcher_gmm_components"))
    return c


def _shuffled(rows: list[tuple]) -> list[tuple]:
    order = np.random.default_rng(42).permutation(len(rows))
    return [rows[i] for i in order]


def _build(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = _conn(rows)
    comp._compute_pitcher_profiles([SEASON])
    return comp._conn


def _scan_keys(c: duckdb.DuckDBPyConnection) -> list[tuple]:
    """The stored order of a filtered scan: what the unordered query would see."""
    return c.execute(
        "SELECT game_pk, at_bat_number, pitch_number FROM pg.raw.pitches "
        f"WHERE season = {SEASON} AND data_quality_flag = FALSE"
    ).fetchall()


def _arsenals(c: duckdb.DuckDBPyConnection) -> dict:
    """Each pitcher's model (without the fit day) and component rows, by key."""
    out = {}
    for pitcher_id, gmm_model in c.execute(
        "SELECT pitcher_id, gmm_model FROM derived.pitcher_season_metrics "
        f"WHERE season = {SEASON} ORDER BY pitcher_id"
    ).fetchall():
        model = json.loads(gmm_model)
        model.get("fit_diagnostics", {}).pop("fit_date", None)
        components = c.execute(
            "SELECT * FROM derived.pitcher_gmm_components "
            "WHERE pitcher_id = ? AND season = ? ORDER BY component_id",
            [pitcher_id, SEASON],
        ).fetchall()
        out[pitcher_id] = {"model": model, "components": components}
    return out


@pytest.fixture(autouse=True)
def _thread_pool(monkeypatch):
    # pitcher.py imports the pool class inside the method, so patch the package's.
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", ThreadPoolExecutor)


@pytest.mark.slow
def test_two_stored_orders_of_the_same_pitches_give_the_same_arsenals():
    rows = _pitch_rows()
    in_key_order = _build(rows)
    shuffled = _build(_shuffled(rows))

    # The guard against a vacuous pass: the stored orders differ, and both pitchers fit.
    assert _scan_keys(in_key_order) != _scan_keys(shuffled)
    a, b = _arsenals(in_key_order), _arsenals(shuffled)
    assert set(a) == set(PITCHERS)
    for pitcher in PITCHERS:
        assert a[pitcher]["model"]["n_components"] >= 2
        assert len(a[pitcher]["components"]) == a[pitcher]["model"]["n_components"]

    assert a == b


def test_the_fit_receives_each_pitchers_rows_in_key_order(monkeypatch):
    recorded: dict[int, np.ndarray] = {}

    def recorder(chunk):
        for arr, pitcher_id, _season in chunk:
            recorded[pitcher_id] = arr
        return []

    monkeypatch.setattr(pitcher_module, "_fit_gmm_batch", recorder)
    rows = _pitch_rows()
    _build(_shuffled(rows))

    assert set(recorded) == set(PITCHERS)
    for pitcher in PITCHERS:
        mine = sorted((r for r in rows if r[0] == pitcher), key=lambda r: (r[3], r[4], r[5]))
        expected = np.array([r[-len(SHAPE_COLUMNS) :] for r in mine])
        assert recorded[pitcher].shape == (PITCHES_PER_PITCHER, len(GMM_FEATURE_NAMES))
        np.testing.assert_array_equal(recorded[pitcher], expected.astype(np.float32))
