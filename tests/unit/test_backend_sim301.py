"""
test_backend_sim301.py
======================
Unit tests for SIM-301 -- the play-pool nightly cache serializer
(``pipeline/batch/play_pool_cache.py``).

These build a tiny TEMPORARY DuckDB with ``sim.pitch_pool`` + ``sim.outcome_pool``
(production-shaped: ``stand`` handedness column, no ``updated_at``) populated
with synthetic rows across 2 full-size pitchers x both hands plus one tiny
pitcher (< MIN_TILE_ROWS) to exercise the fall-forward, point
``BASEBALL_PLAY_POOL_DIR`` at a tmp dir, and assert the spec contract:

  (a) tiles written at the spec'd paths;
  (b) .meta JSON has the required fields with correct n_vectors / recency flag;
  (c) re-running immediately rebuilds NOTHING (idempotent no-op);
  (d) bumping the source data marks that tile stale and rebuilds only it;
  (e) the <50-row pitcher's rows land in the pitcher_id=0 fall-back tile,
      not a standalone tile.
"""

from __future__ import annotations

import json
import os

import duckdb
import numpy as np
import pytest

from pipeline.batch import play_pool_cache as ppc

# ---------------------------------------------------------------------------
# Synthetic data construction
# ---------------------------------------------------------------------------

SEASON = 2024
BIG_PITCHER_A = 477132
BIG_PITCHER_B = 592789
TINY_PITCHER = 999001  # < MIN_TILE_ROWS rows -> falls into pitcher_id=0 tile

ROWS_PER_BIG_TILE = 80        # >= MIN_TILE_ROWS (50)
ROWS_TINY = 12                # < MIN_TILE_ROWS

PITCH_POOL_DDL = """
CREATE TABLE sim.pitch_pool (
    pitch_id    BIGINT,
    season      SMALLINT,
    pitcher_id  INTEGER,
    stand       VARCHAR(1),
    velo        FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x   FLOAT, release_z FLOAT, release_ext FLOAT,
    plate_x     FLOAT, plate_z FLOAT,
    game_date   DATE
)
"""

OUTCOME_POOL_DDL = """
CREATE TABLE sim.outcome_pool (
    pitch_id    BIGINT,
    season      SMALLINT,
    pitcher_id  INTEGER,
    stand       VARCHAR(1),
    exit_velo   FLOAT, launch_angle FLOAT, spray_angle FLOAT,
    bb_type     VARCHAR, result_hits SMALLINT,
    game_date   DATE
)
"""


def _insert_pitch_rows(conn, pitcher_id, bat_hand, n, *, base_id, season=SEASON,
                       game_date="2024-06-01"):
    rng = np.random.default_rng(abs(hash((pitcher_id, bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        vec = [
            float(rng.uniform(88, 100)),       # velo
            float(rng.uniform(-5, 20)),        # ivb
            float(rng.uniform(-15, 15)),       # hb
            float(rng.uniform(1800, 2600)),    # spin_rate
            float(rng.uniform(0, 360)),        # spin_axis
            float(rng.uniform(-2.5, 2.5)),     # release_x
            float(rng.uniform(5, 6.5)),        # release_z
            float(rng.uniform(5.5, 7)),        # release_ext
            float(rng.uniform(-1.5, 1.5)),     # plate_x
            float(rng.uniform(1.5, 3.5)),      # plate_z
        ]
        conn.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [pid, season, pitcher_id, bat_hand, *vec, game_date],
        )


def _insert_outcome_rows(conn, bat_hand, n, *, base_id, season=SEASON,
                         game_date="2024-06-01"):
    rng = np.random.default_rng(abs(hash(("bb", bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        ev = float(rng.uniform(60, 110))
        la = float(rng.uniform(-25, 45))
        sa = float(rng.uniform(-45, 45))
        conn.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?,?,?,?,?,?,?)",
            [pid, season, 600000 + (i % 20), bat_hand, ev, la, sa,
             "line_drive" if la > 0 else "ground_ball", int(rng.integers(0, 5)),
             game_date],
        )


@pytest.fixture()
def duckdb_path(tmp_path):
    """A temporary DuckDB file with sim.pitch_pool + sim.outcome_pool."""
    db = tmp_path / "baseball_sim.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA IF NOT EXISTS sim")
    conn.execute(PITCH_POOL_DDL)
    conn.execute(OUTCOME_POOL_DDL)

    # Two full-size pitchers x both hands.
    base = 1_000_000
    for pid in (BIG_PITCHER_A, BIG_PITCHER_B):
        for hand in ("L", "R"):
            _insert_pitch_rows(conn, pid, hand, ROWS_PER_BIG_TILE, base_id=base)
            base += ROWS_PER_BIG_TILE
    # One tiny pitcher (both hands) below MIN_TILE_ROWS -> fall-back tile.
    for hand in ("L", "R"):
        _insert_pitch_rows(conn, TINY_PITCHER, hand, ROWS_TINY, base_id=base)
        base += ROWS_TINY

    # Batted-ball rows, both hands.
    _insert_outcome_rows(conn, "L", 120, base_id=5_000_000)
    _insert_outcome_rows(conn, "R", 120, base_id=6_000_000)

    conn.close()
    return str(db)


@pytest.fixture()
def pool_dir(tmp_path, monkeypatch):
    d = tmp_path / "play_pool"
    monkeypatch.setenv("BASEBALL_PLAY_POOL_DIR", str(d))
    return str(d)


# ---------------------------------------------------------------------------
# (a) tiles written at the spec'd paths
# ---------------------------------------------------------------------------


def test_tiles_written_at_spec_paths(duckdb_path, pool_dir):
    res = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert res.rebuilt > 0

    for hand in ("L", "R"):
        for pid in (BIG_PITCHER_A, BIG_PITCHER_B):
            base = os.path.join(pool_dir, str(SEASON), str(pid))
            assert os.path.exists(os.path.join(base, hand + ".faiss"))
            assert os.path.exists(os.path.join(base, hand + ".faiss.meta"))
            assert os.path.exists(os.path.join(base, hand + ".rowids.npy"))

        fb = os.path.join(pool_dir, str(SEASON), "0")
        assert os.path.exists(os.path.join(fb, hand + ".faiss"))

        bb = os.path.join(pool_dir, str(SEASON), "battedball")
        assert os.path.exists(os.path.join(bb, hand + ".faiss"))
        assert os.path.exists(os.path.join(bb, hand + ".faiss.meta"))
        assert os.path.exists(os.path.join(bb, hand + ".rowids.npy"))


# ---------------------------------------------------------------------------
# (b) .meta JSON has required fields, correct n_vectors / recency flag
# ---------------------------------------------------------------------------


def _load_meta(pool_dir, *parts):
    with open(os.path.join(pool_dir, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def test_meta_required_fields_and_counts(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)

    required = {
        "schema_version", "pool", "season", "bat_hand", "dim", "n_vectors",
        "n_source_rows", "recency_boost", "recency_boost_seasons",
        "source_max_updated_at", "build_timestamp", "builder_version",
        "bytes_on_disk",
    }

    pitch_meta = _load_meta(pool_dir, str(SEASON), str(BIG_PITCHER_A), "L.faiss.meta")
    assert required.issubset(pitch_meta.keys())
    assert pitch_meta["pool"] == "pitch"
    assert pitch_meta["pitcher_id"] == BIG_PITCHER_A
    assert pitch_meta["dim"] == 10
    assert pitch_meta["bat_hand"] == "L"
    assert pitch_meta["recency_boost"] is False
    assert pitch_meta["recency_boost_seasons"] == ppc.RECENCY_BOOST_SEASONS
    assert pitch_meta["n_source_rows"] == ROWS_PER_BIG_TILE
    assert pitch_meta["n_vectors"] == ROWS_PER_BIG_TILE
    assert pitch_meta["builder_version"] == ppc.BUILDER_VERSION

    bb_meta = _load_meta(pool_dir, str(SEASON), "battedball", "R.faiss.meta")
    assert required.issubset(bb_meta.keys())
    assert bb_meta["pool"] == "battedball"
    assert bb_meta["dim"] == 3
    assert "spray_column" in bb_meta
    assert bb_meta["spray_column"] in ("pull_relative_spray_angle", "spray_angle")
    assert "pitcher_id" not in bb_meta


def test_recency_boost_doubles_recent_rows(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=True)
    meta = _load_meta(pool_dir, str(SEASON), str(BIG_PITCHER_A), "L.faiss.meta")
    assert meta["recency_boost"] is True
    assert meta["n_source_rows"] == ROWS_PER_BIG_TILE
    assert meta["n_vectors"] == 2 * ROWS_PER_BIG_TILE


# ---------------------------------------------------------------------------
# (c) re-running immediately rebuilds NOTHING (idempotent no-op)
# ---------------------------------------------------------------------------


def test_rerun_is_idempotent_noop(duckdb_path, pool_dir):
    first = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert first.rebuilt > 0

    faiss_path = os.path.join(pool_dir, str(SEASON), str(BIG_PITCHER_A), "L.faiss")
    mtime_before = os.path.getmtime(faiss_path)

    second = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert second.rebuilt == 0
    assert second.skipped == first.rebuilt
    assert os.path.getmtime(faiss_path) == mtime_before


# ---------------------------------------------------------------------------
# (d) bumping source data marks that tile stale and rebuilds ONLY it
# ---------------------------------------------------------------------------


def test_new_source_data_rebuilds_only_affected_tile(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)

    conn = duckdb.connect(duckdb_path)
    _insert_pitch_rows(conn, BIG_PITCHER_A, "L", 1, base_id=9_000_000,
                       game_date="2024-09-30")
    conn.close()

    res = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert res.rebuilt == 1
    label = str(SEASON) + "/" + str(BIG_PITCHER_A) + "/L"
    assert res.rebuilt_tiles == [label]

    meta = _load_meta(pool_dir, str(SEASON), str(BIG_PITCHER_A), "L.faiss.meta")
    assert meta["n_source_rows"] == ROWS_PER_BIG_TILE + 1


def test_builder_version_change_forces_rebuild(duckdb_path, pool_dir, monkeypatch):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    noop = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert noop.rebuilt == 0

    monkeypatch.setattr(ppc, "BUILDER_VERSION", "sim301.test-bump")
    bumped = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    assert bumped.rebuilt > 0


def test_recency_flag_change_forces_rebuild(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    res = ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=True)
    assert res.rebuilt > 0
    assert res.skipped == 0


# ---------------------------------------------------------------------------
# (e) <50-row pitcher's rows land in the pitcher_id=0 fall-back tile
# ---------------------------------------------------------------------------


def test_tiny_pitcher_falls_into_fallback_tile(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)

    tiny_dir = os.path.join(pool_dir, str(SEASON), str(TINY_PITCHER))
    assert not os.path.exists(tiny_dir)

    for hand in ("L", "R"):
        fb_meta = _load_meta(pool_dir, str(SEASON), "0", hand + ".faiss.meta")
        assert fb_meta["pitcher_id"] == 0
        assert fb_meta["n_source_rows"] == ROWS_TINY


# ---------------------------------------------------------------------------
# Spray-column reuse (3.1) -- fall back to raw spray_angle when SIM-051 absent
# ---------------------------------------------------------------------------


def test_battedball_spray_column_reuses_engine_helper(duckdb_path, pool_dir):
    ppc.build_play_pool_cache(duckdb_path, pool_dir, recency_boost=False)
    bb_meta = _load_meta(pool_dir, str(SEASON), "battedball", "L.faiss.meta")
    assert bb_meta["spray_column"] == "spray_angle"


# ---------------------------------------------------------------------------
# CLI smoke -- argparse wiring + --no-recency-boost flag
# ---------------------------------------------------------------------------


def test_cli_main_runs(duckdb_path, pool_dir):
    rc = ppc.main([
        "--duckdb-path", duckdb_path,
        "--pool-dir", pool_dir,
        "--no-recency-boost",
        "--seasons", str(SEASON),
    ])
    assert rc == 0
    assert os.path.exists(
        os.path.join(pool_dir, str(SEASON), str(BIG_PITCHER_A), "L.faiss")
    )
