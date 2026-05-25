"""
test_backend_sim302.py
======================
Unit tests for SIM-302 -- the ``PlayPoolSampler`` read-side API
(``simulation/play_pool_sampler.py``).

Strategy
--------
Two complementary fixtures, both on synthetic ~1k-row tiles:

1. ``round_trip_pool`` -- a TRUE round-trip: build a tiny synthetic DuckDB
   (production-shaped: ``stand`` handedness, ``outcome_type`` / ``events``
   outcome columns) and run the REAL SIM-301 builder
   (``pipeline.batch.play_pool_cache``) to materialize tiles on disk, then point
   the sampler at them.  This proves the read format matches the write format
   end-to-end, including the pitcher_id=0 fall-back tile.

2. ``synthetic_tile`` -- pure FAISS tiles written directly (no DuckDB), used for
   the geometry/weight/LRU invariants that don't need real outcome rows.  The
   outcome payload is supplied via an injected ``outcome_fetch``.

Covers the 7 invariants from spec §6.3:
  (1) sampled row is actually in the tile;
  (2) zero-distance query -> dominant weight on that vector;
  (3) fixed rng seed -> reproducible draws;
  (4) return_distribution=True sums to 1.0 (+/-1e-9) over a closed vocab;
  (5) missing specific tile -> fall-back served, fellback is True;
  (6) LRU never exceeds max_resident_tiles;
  (7) k > n_vectors is clamped, never raises.
"""

from __future__ import annotations

import io
import json
import os

import numpy as np
import pytest

faiss = pytest.importorskip("faiss")
duckdb = pytest.importorskip("duckdb")

from pipeline.batch import play_pool_cache as ppc
from simulation.play_pool_sampler import (
    FALLBACK_PITCHER_ID,
    POOL_BATTEDBALL,
    POOL_PITCH,
    PlayPoolSampler,
)

SEASON = 2024
BIG_PITCHER = 477132
TINY_PITCHER = 999001  # < MIN_TILE_ROWS -> folds into pitcher_id=0 tile
MISSING_PITCHER = 123456  # no tile on disk at all -> fall-back


# ===========================================================================
# Fixture 1 -- real SIM-301 round-trip (synthetic DuckDB -> real tiles)
# ===========================================================================

PITCH_POOL_DDL = """
CREATE TABLE sim.pitch_pool (
    pitch_id    BIGINT,
    season      SMALLINT,
    pitcher_id  INTEGER,
    stand       VARCHAR(1),
    velo        FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x   FLOAT, release_z FLOAT, release_ext FLOAT,
    plate_x     FLOAT, plate_z FLOAT,
    outcome_type VARCHAR(20),
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
    events      VARCHAR(50),
    bb_type     VARCHAR(20),
    game_date   DATE
)
"""

_PITCH_OUTCOMES = ["ball", "called_strike", "swinging_strike", "foul", "in_play"]
_BB_EVENTS = ["single", "double", "triple", "home_run", "field_out"]


def _insert_pitch_rows(
    conn, pitcher_id, bat_hand, n, *, base_id, season=SEASON, game_date="2024-06-01"
):
    rng = np.random.default_rng(abs(hash((pitcher_id, bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        vec = [
            float(rng.uniform(88, 100)),
            float(rng.uniform(-5, 20)),
            float(rng.uniform(-15, 15)),
            float(rng.uniform(1800, 2600)),
            float(rng.uniform(0, 360)),
            float(rng.uniform(-2.5, 2.5)),
            float(rng.uniform(5, 6.5)),
            float(rng.uniform(5.5, 7)),
            float(rng.uniform(-1.5, 1.5)),
            float(rng.uniform(1.5, 3.5)),
        ]
        conn.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                pid,
                season,
                pitcher_id,
                bat_hand,
                *vec,
                _PITCH_OUTCOMES[i % len(_PITCH_OUTCOMES)],
                game_date,
            ],
        )


def _insert_outcome_rows(conn, bat_hand, n, *, base_id, season=SEASON, game_date="2024-06-01"):
    rng = np.random.default_rng(abs(hash(("bb", bat_hand))) % (2**32))
    for i in range(n):
        pid = base_id + i
        ev = float(rng.uniform(60, 110))
        la = float(rng.uniform(-25, 45))
        sa = float(rng.uniform(-45, 45))
        conn.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                pid,
                season,
                600000 + (i % 20),
                bat_hand,
                ev,
                la,
                sa,
                _BB_EVENTS[i % len(_BB_EVENTS)],
                "line_drive" if la > 0 else "ground_ball",
                game_date,
            ],
        )


@pytest.fixture()
def round_trip_pool(tmp_path):
    """Build a synthetic DuckDB, run the REAL SIM-301 builder, return
    (pool_dir, duckdb_path).  ~1k pitch rows + ~240 batted-ball rows."""
    db = tmp_path / "baseball_sim.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA IF NOT EXISTS sim")
    conn.execute(PITCH_POOL_DDL)
    conn.execute(OUTCOME_POOL_DDL)

    base = 1_000_000
    for hand in ("L", "R"):
        _insert_pitch_rows(conn, BIG_PITCHER, hand, 500, base_id=base)  # ~1k total
        base += 500
    # Tiny pitcher (< MIN_TILE_ROWS) -> folds into pitcher_id=0 fall-back tile.
    for hand in ("L", "R"):
        _insert_pitch_rows(conn, TINY_PITCHER, hand, 12, base_id=base)
        base += 12

    _insert_outcome_rows(conn, "L", 240, base_id=5_000_000)
    _insert_outcome_rows(conn, "R", 240, base_id=6_000_000)
    conn.close()

    pool_dir = tmp_path / "play_pool"
    # recency_boost=False for deterministic neighbour ordering (spec §5).
    res = ppc.build_play_pool_cache(str(db), str(pool_dir), recency_boost=False)
    assert res.rebuilt > 0
    return str(pool_dir), str(db)


# ===========================================================================
# Fixture 2 -- pure synthetic FAISS tiles (no DuckDB), injected outcome_fetch
# ===========================================================================


def _write_synthetic_tile(tile_dir, bat_hand, vectors, rowids, meta_extra):
    """Write a tile in the EXACT SIM-301 on-disk format (faiss + npy + meta)."""
    os.makedirs(tile_dir, exist_ok=True)
    dim = vectors.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(np.ascontiguousarray(vectors, dtype=np.float32))
    faiss_path = os.path.join(tile_dir, f"{bat_hand}.faiss")
    faiss.write_index(index, faiss_path)

    buf = io.BytesIO()
    np.save(buf, np.asarray(rowids, dtype=np.int64), allow_pickle=False)
    with open(os.path.join(tile_dir, f"{bat_hand}.rowids.npy"), "wb") as fh:
        fh.write(buf.getvalue())

    meta = {
        "schema_version": 1,
        "bat_hand": bat_hand,
        "dim": dim,
        "n_vectors": int(vectors.shape[0]),
        "n_source_rows": int(meta_extra.get("n_source_rows", vectors.shape[0])),
        "recency_boost": False,
        "recency_boost_seasons": 2,
        "build_timestamp": "2026-05-20T08:00:00Z",
        "builder_version": "sim301.1",
        "source_max_updated_at": "2026-05-19T00:00:00Z",
        "bytes_on_disk": 0,
    }
    meta.update(meta_extra)
    with open(os.path.join(tile_dir, f"{bat_hand}.faiss.meta"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)


@pytest.fixture()
def synthetic_battedball(tmp_path):
    """One 1k-row batted-ball tile with a known closed event vocabulary,
    served with an injected outcome_fetch.  Returns (sampler, vectors, rowids,
    outcomes_by_rowid)."""
    rng = np.random.default_rng(7)
    n = 1000
    vectors = rng.uniform(-1.0, 1.0, size=(n, 3)).astype(np.float32)
    rowids = np.arange(n, dtype=np.int64) + 700_000
    vocab = ["single", "double", "field_out"]
    outcomes = {int(rid): vocab[i % len(vocab)] for i, rid in enumerate(rowids)}

    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(
        tile_dir,
        "R",
        vectors,
        rowids,
        {"pool": POOL_BATTEDBALL, "season": SEASON, "spray_column": "spray_angle"},
    )

    def fetch(pool, rids):
        return {int(r): outcomes[int(r)] for r in rids}

    sampler = PlayPoolSampler(
        pool_dir=str(tmp_path),
        duckdb_path=None,
        rng=np.random.default_rng(0),
        outcome_fetch=fetch,
    )
    return sampler, vectors, rowids, outcomes


# ===========================================================================
# Invariant (1): sampled row is actually in the tile
# ===========================================================================


def test_inv1_sampled_row_in_tile_round_trip(round_trip_pool):
    pool_dir, db = round_trip_pool
    sampler = PlayPoolSampler(pool_dir=pool_dir, duckdb_path=db, rng=np.random.default_rng(1))
    handle = sampler.load_tile(POOL_PITCH, SEASON, "L", pitcher_id=BIG_PITCHER)
    valid_ids = {int(r) for r in handle.rowids}

    q = np.zeros(10, dtype=np.float32)
    for _ in range(20):
        out = sampler.sample_pitch(BIG_PITCHER, "L", SEASON, q, k=15)
        assert out["row_id"] in valid_ids
        assert out["pitch_outcome"] in _PITCH_OUTCOMES
        assert out["tile"] == f"{SEASON}/{BIG_PITCHER}/L"
        assert out["fellback"] is False
    sampler.close()


def test_inv1_sampled_row_in_tile_battedball(synthetic_battedball):
    sampler, vectors, rowids, outcomes = synthetic_battedball
    valid_ids = {int(r) for r in rowids}
    q = np.zeros(3, dtype=np.float32)
    for _ in range(20):
        out = sampler.sample_batted_ball("R", SEASON, q, k=25)
        assert out["row_id"] in valid_ids
        assert out["event"] == outcomes[out["row_id"]]
    sampler.close()


# ===========================================================================
# Invariant (2): zero-distance query -> dominant weight on that vector
# ===========================================================================


def test_inv2_zero_distance_dominant_weight(synthetic_battedball):
    sampler, vectors, rowids, _ = synthetic_battedball
    target_pos = 321
    q = vectors[target_pos].copy()  # query == an indexed vector -> d ~ 0

    handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
    positions, weights, distances = sampler._knn(handle, q, k=25)

    # The exact-match neighbour is first, distance ~0, and dominates the weights.
    assert int(positions[0]) == target_pos
    assert distances[0] < 1e-4
    assert weights[0] == pytest.approx(weights.max())
    assert weights[0] > 0.99  # 1/(eps) swamps every finite-distance neighbour
    sampler.close()


# ===========================================================================
# Invariant (3): fixed rng seed -> reproducible draws
# ===========================================================================


def test_inv3_fixed_seed_reproducible(synthetic_battedball):
    sampler_a, vectors, _, _ = synthetic_battedball
    # Rebuild a second sampler pointed at the SAME tiles with the same seed.
    sampler_b = PlayPoolSampler(
        pool_dir=sampler_a.pool_dir,
        duckdb_path=None,
        rng=np.random.default_rng(0),
        outcome_fetch=sampler_a._outcome_fetch,
    )
    sampler_a.rng = np.random.default_rng(42)
    sampler_b.rng = np.random.default_rng(42)

    q = vectors[100].astype(np.float32) + 0.05
    draws_a = [sampler_a.sample_batted_ball("R", SEASON, q, k=20)["row_id"] for _ in range(30)]
    draws_b = [sampler_b.sample_batted_ball("R", SEASON, q, k=20)["row_id"] for _ in range(30)]
    assert draws_a == draws_b
    sampler_a.close()
    sampler_b.close()


# ===========================================================================
# Invariant (4): return_distribution=True sums to 1.0 over a closed vocab
# ===========================================================================


def test_inv4_distribution_sums_to_one(synthetic_battedball):
    sampler, vectors, _, outcomes = synthetic_battedball
    closed_vocab = set(outcomes.values())

    for seed in range(5):
        q = np.random.default_rng(seed).uniform(-1, 1, size=3).astype(np.float32)
        dist = sampler.sample_batted_ball("R", SEASON, q, k=30, return_distribution=True)
        assert isinstance(dist, dict)
        assert abs(sum(dist.values()) - 1.0) < 1e-9
        assert set(dist).issubset(closed_vocab)
        assert all(0.0 <= p <= 1.0 for p in dist.values())
    sampler.close()


def test_inv4_distribution_round_trip(round_trip_pool):
    """Distribution mode over the REAL builder's batted-ball tile + DuckDB."""
    pool_dir, db = round_trip_pool
    sampler = PlayPoolSampler(pool_dir=pool_dir, duckdb_path=db)
    q = np.array([95.0, 18.0, 5.0], dtype=np.float32)
    dist = sampler.sample_batted_ball("L", SEASON, q, k=25, return_distribution=True)
    assert abs(sum(dist.values()) - 1.0) < 1e-9
    assert set(dist).issubset(set(_BB_EVENTS))
    sampler.close()


# ===========================================================================
# Invariant (5): missing specific tile -> fall-back served, fellback is True
# ===========================================================================


def test_inv5_missing_tile_falls_back(round_trip_pool):
    pool_dir, db = round_trip_pool
    sampler = PlayPoolSampler(pool_dir=pool_dir, duckdb_path=db, rng=np.random.default_rng(3))

    # No tile exists for MISSING_PITCHER -> resolve to pitcher_id=0 fall-back.
    handle = sampler.load_tile(POOL_PITCH, SEASON, "L", pitcher_id=MISSING_PITCHER)
    assert handle.is_fallback is True
    assert handle.label == f"{SEASON}/{FALLBACK_PITCHER_ID}/L"

    q = np.zeros(10, dtype=np.float32)
    out = sampler.sample_pitch(MISSING_PITCHER, "L", SEASON, q, k=10)
    assert out["fellback"] is True
    assert out["tile"] == f"{SEASON}/{FALLBACK_PITCHER_ID}/L"
    sampler.close()


def test_inv5_too_small_specific_tile_falls_back(tmp_path):
    """A standalone tile present but with meta.n_source_rows < MIN_TILE_ROWS
    must still fall forward to pitcher_id=0 (defensive floor, spec §6.2)."""
    pool_root = str(tmp_path)
    # specific too-small tile
    _write_synthetic_tile(
        os.path.join(pool_root, str(SEASON), str(BIG_PITCHER)),
        "R",
        np.random.default_rng(1).uniform(-1, 1, (10, 10)).astype(np.float32),
        np.arange(10, dtype=np.int64),
        {
            "pool": POOL_PITCH,
            "season": SEASON,
            "pitcher_id": BIG_PITCHER,
            "n_source_rows": 10,
        },  # < MIN_TILE_ROWS
    )
    # pitcher_id=0 fall-back tile
    _write_synthetic_tile(
        os.path.join(pool_root, str(SEASON), str(FALLBACK_PITCHER_ID)),
        "R",
        np.random.default_rng(2).uniform(-1, 1, (300, 10)).astype(np.float32),
        np.arange(300, dtype=np.int64) + 9_000,
        {
            "pool": POOL_PITCH,
            "season": SEASON,
            "pitcher_id": FALLBACK_PITCHER_ID,
            "n_source_rows": 300,
        },
    )
    sampler = PlayPoolSampler(
        pool_dir=pool_root,
        duckdb_path=None,
        outcome_fetch=lambda pool, rids: {int(r): "ball" for r in rids},
    )
    handle = sampler.load_tile(POOL_PITCH, SEASON, "R", pitcher_id=BIG_PITCHER)
    assert handle.is_fallback is True
    out = sampler.sample_pitch(BIG_PITCHER, "R", SEASON, np.zeros(10, dtype=np.float32), k=5)
    assert out["fellback"] is True
    sampler.close()


# ===========================================================================
# Invariant (6): LRU never exceeds max_resident_tiles
# ===========================================================================


def test_inv6_lru_respects_cap(tmp_path):
    pool_root = str(tmp_path)
    n_tiles = 20
    cap = 4
    rng = np.random.default_rng(11)
    for s in range(n_tiles):
        season = 2000 + s
        _write_synthetic_tile(
            os.path.join(pool_root, str(season), POOL_BATTEDBALL),
            "L",
            rng.uniform(-1, 1, (50, 3)).astype(np.float32),
            np.arange(50, dtype=np.int64) + s * 1000,
            {"pool": POOL_BATTEDBALL, "season": season},
        )

    sampler = PlayPoolSampler(
        pool_dir=pool_root,
        duckdb_path=None,
        max_resident_tiles=cap,
        outcome_fetch=lambda pool, rids: {int(r): "single" for r in rids},
    )
    for s in range(n_tiles):
        sampler.load_tile(POOL_BATTEDBALL, 2000 + s, "L")
        assert sampler.resident_count <= cap
    assert sampler.resident_count == cap
    sampler.close()


def test_inv6_idempotent_load_no_growth(synthetic_battedball):
    sampler, _, _, _ = synthetic_battedball
    h1 = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
    before = sampler.resident_count
    for _ in range(10):
        h2 = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
        assert h2 is h1  # same cached handle
    assert sampler.resident_count == before
    sampler.close()


# ===========================================================================
# Invariant (7): k > n_vectors is clamped, never raises
# ===========================================================================


def test_inv7_k_larger_than_tile_clamped(tmp_path):
    pool_root = str(tmp_path)
    n = 8
    _write_synthetic_tile(
        os.path.join(pool_root, str(SEASON), POOL_BATTEDBALL),
        "L",
        np.random.default_rng(5).uniform(-1, 1, (n, 3)).astype(np.float32),
        np.arange(n, dtype=np.int64) + 50_000,
        {"pool": POOL_BATTEDBALL, "season": SEASON},
    )
    sampler = PlayPoolSampler(
        pool_dir=pool_root,
        duckdb_path=None,
        rng=np.random.default_rng(0),
        outcome_fetch=lambda pool, rids: {int(r): "double" for r in rids},
    )
    # k far exceeds the 8-vector tile -> must clamp, not raise.
    out = sampler.sample_batted_ball("L", SEASON, np.zeros(3, dtype=np.float32), k=1000)
    assert out["row_id"] in {int(r) for r in (np.arange(n) + 50_000)}

    dist = sampler.sample_batted_ball(
        "L", SEASON, np.zeros(3, dtype=np.float32), k=1000, return_distribution=True
    )
    assert abs(sum(dist.values()) - 1.0) < 1e-9
    sampler.close()


# ===========================================================================
# reload_recent (§6.2 / §5)
# ===========================================================================


def test_reload_recent_picks_up_advanced_build_timestamp(tmp_path):
    pool_root = str(tmp_path)
    tile_dir = os.path.join(pool_root, str(SEASON), POOL_BATTEDBALL)
    vecs = np.random.default_rng(9).uniform(-1, 1, (40, 3)).astype(np.float32)
    rowids = np.arange(40, dtype=np.int64) + 1000
    _write_synthetic_tile(
        tile_dir,
        "L",
        vecs,
        rowids,
        {"pool": POOL_BATTEDBALL, "season": SEASON, "build_timestamp": "2026-05-20T08:00:00Z"},
    )

    sampler = PlayPoolSampler(
        pool_dir=pool_root,
        duckdb_path=None,
        outcome_fetch=lambda pool, rids: {int(r): "single" for r in rids},
    )
    sampler.load_tile(POOL_BATTEDBALL, SEASON, "L")

    # No on-disk change yet -> nothing reloads.
    assert sampler.reload_recent(SEASON) == 0
    # A non-current season is never touched.
    assert sampler.reload_recent(1999) == 0

    # Rewrite the tile on disk with a newer build_timestamp.
    _write_synthetic_tile(
        tile_dir,
        "L",
        vecs,
        rowids,
        {"pool": POOL_BATTEDBALL, "season": SEASON, "build_timestamp": "2026-05-21T08:00:00Z"},
    )
    assert sampler.reload_recent(SEASON) == 1
    # Resident copy now carries the advanced timestamp; reloading again is a no-op.
    assert sampler.reload_recent(SEASON) == 0
    sampler.close()


# ===========================================================================
# Misc contract: env-var defaults + bad bat_hand
# ===========================================================================


def test_env_var_pool_dir_default(monkeypatch, synthetic_battedball):
    sampler, _, _, _ = synthetic_battedball
    monkeypatch.setenv("BASEBALL_PLAY_POOL_DIR", sampler.pool_dir)
    s2 = PlayPoolSampler(outcome_fetch=lambda pool, rids: {int(r): "x" for r in rids})
    assert s2.pool_dir == sampler.pool_dir
    s2.close()
    sampler.close()


def test_bad_bat_hand_rejected(synthetic_battedball):
    sampler, _, _, _ = synthetic_battedball
    with pytest.raises(ValueError):
        sampler.load_tile(POOL_BATTEDBALL, SEASON, "S")
    sampler.close()
