"""
test_ml_engines_sim313.py
=========================
SIM-313 (P0 LIVE BUG) -- wire per-row ``recency_weight`` into the
``PlayPoolSampler`` k-NN sampling distribution.

The SIM-111 query contract §8 (SIM-076) requires the sampler to multiply each
neighbour's FAISS-distance weight by that neighbour's ``recency_weight`` before
normalizing.  Before this ticket the sampler used pure ``1/(d+EPS)`` weights and
ignored recency entirely (a read-side no-op).

These tests use injected ``outcome_fetch`` / ``recency_fetch`` (no live DB),
mirroring the SIM-302 synthetic-tile pattern, and assert:

  (A) uniform recency_weight=1.0 -> draws/weights identical to pure distance
      (strict generalization);
  (B) a higher recency_weight on a neighbour strictly increases its selection
      probability, monotonically with the boost;
  (C) the ``return_distribution=True`` path reweights by recency correctly;
  (D) missing / None recency resolves to 1.0 (no crash, recency-neutral);
  (E) the pitch path (``sample_pitch``) applies recency too.
"""

from __future__ import annotations

import io
import json
import os

import numpy as np
import pytest

faiss = pytest.importorskip("faiss")

from simulation.play_pool_sampler import (
    EPS,
    POOL_BATTEDBALL,
    POOL_PITCH,
    PlayPoolSampler,
)

SEASON = 2024
PITCHER = 477132


# ---------------------------------------------------------------------------
# Helpers: write a synthetic tile in the exact SIM-301 on-disk format.
# ---------------------------------------------------------------------------


def _write_synthetic_tile(tile_dir, bat_hand, vectors, rowids, meta_extra):
    os.makedirs(tile_dir, exist_ok=True)
    dim = vectors.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(np.ascontiguousarray(vectors, dtype=np.float32))
    faiss.write_index(index, os.path.join(tile_dir, f"{bat_hand}.faiss"))

    buf = io.BytesIO()
    np.save(buf, np.asarray(rowids, dtype=np.int64), allow_pickle=False)
    with open(os.path.join(tile_dir, f"{bat_hand}.rowids.npy"), "wb") as fh:
        fh.write(buf.getvalue())

    meta = {
        "schema_version": 1, "bat_hand": bat_hand, "dim": dim,
        "n_vectors": int(vectors.shape[0]),
        "n_source_rows": int(meta_extra.get("n_source_rows", vectors.shape[0])),
        "recency_boost": False, "recency_boost_seasons": 2,
        "build_timestamp": "2026-05-20T08:00:00Z", "builder_version": "sim301.1",
    }
    meta.update(meta_extra)
    with open(os.path.join(tile_dir, f"{bat_hand}.faiss.meta"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)


def _make_battedball_sampler(tmp_path, *, recency=None, n=200, seed=7,
                             rng_seed=0):
    """Build a synthetic batted-ball tile + sampler.  ``recency`` is an optional
    {row_id: float} map injected via recency_fetch; when None, no recency_fetch
    is injected (recency-neutral, since an outcome_fetch is supplied)."""
    rng = np.random.default_rng(seed)
    vectors = rng.uniform(-1.0, 1.0, size=(n, 3)).astype(np.float32)
    rowids = np.arange(n, dtype=np.int64) + 700_000
    vocab = ["single", "double", "field_out"]
    outcomes = {int(rid): vocab[i % len(vocab)] for i, rid in enumerate(rowids)}

    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_BATTEDBALL, "season": SEASON})

    def outcome_fetch(pool, rids):
        return {int(r): outcomes[int(r)] for r in rids}

    recency_fetch = None
    if recency is not None:
        def recency_fetch(pool, rids):  # noqa: E731
            return {int(r): recency.get(int(r)) for r in rids}

    sampler = PlayPoolSampler(
        pool_dir=str(tmp_path), duckdb_path=None,
        rng=np.random.default_rng(rng_seed),
        outcome_fetch=outcome_fetch, recency_fetch=recency_fetch,
    )
    return sampler, vectors, rowids, outcomes


# ===========================================================================
# (A) uniform recency_weight=1.0 == pure distance behaviour
# ===========================================================================


def test_uniform_recency_matches_pure_distance_weights(tmp_path):
    """With every recency_weight = 1.0 the normalized weights returned by _knn +
    _apply_recency are identical to the pure-distance weights from _knn alone."""
    sampler_unit, vectors, rowids, _ = _make_battedball_sampler(
        tmp_path, recency={int(r): 1.0 for r in (np.arange(200) + 700_000)})

    handle = sampler_unit.load_tile(POOL_BATTEDBALL, SEASON, "R")
    q = (vectors[42].astype(np.float32) + 0.1)
    positions, dist_weights, _d = sampler_unit._knn(handle, q, k=25)
    recency_weights = sampler_unit._apply_recency(handle, positions, dist_weights)

    # Multiplying by 1.0 then renormalizing is a strict generalization of the
    # pure-distance weights, modulo float re-division drift.
    np.testing.assert_allclose(recency_weights, dist_weights, rtol=1e-6, atol=1e-12)
    assert abs(float(recency_weights.sum()) - 1.0) < 1e-9


def test_uniform_recency_same_draws_as_no_recency_fetch(tmp_path):
    """Same tile, same seed: a sampler with uniform recency_fetch and one with NO
    recency_fetch (recency-neutral) must produce identical draw sequences."""
    sampler_a, vectors, _, _ = _make_battedball_sampler(
        tmp_path, recency={int(r): 1.0 for r in (np.arange(200) + 700_000)},
        rng_seed=99)
    sampler_b, _, _, _ = _make_battedball_sampler(
        tmp_path, recency=None, rng_seed=99)

    q = vectors[10].astype(np.float32) + 0.05
    draws_a = [sampler_a.sample_batted_ball("R", SEASON, q, k=20)["row_id"]
               for _ in range(40)]
    draws_b = [sampler_b.sample_batted_ball("R", SEASON, q, k=20)["row_id"]
               for _ in range(40)]
    assert draws_a == draws_b


# ===========================================================================
# (B) higher recency_weight -> monotonically higher selection probability
# ===========================================================================


def test_higher_recency_increases_selection_probability(tmp_path):
    """Boosting one neighbour's recency_weight strictly raises its normalized
    sampling probability, monotonically with the boost factor."""
    # Build the geometry once; vary only the recency map between samplers.
    rng = np.random.default_rng(7)
    n = 60
    vectors = rng.uniform(-1.0, 1.0, size=(n, 3)).astype(np.float32)
    rowids = np.arange(n, dtype=np.int64) + 700_000
    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_BATTEDBALL, "season": SEASON})

    def outcome_fetch(pool, rids):
        return {int(r): "single" for r in rids}

    q = vectors[5].astype(np.float32) + 0.2  # not an exact match -> spread weights

    def prob_of_target_with_boost(boost):
        recency = {int(r): 1.0 for r in rowids}
        # Find the target neighbour: the 3rd-nearest so it's not already dominant.
        sampler = PlayPoolSampler(
            pool_dir=str(tmp_path), duckdb_path=None,
            rng=np.random.default_rng(0), outcome_fetch=outcome_fetch,
            recency_fetch=lambda pool, rids: {int(r): recency[int(r)] for r in rids},
        )
        handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
        positions, dist_w, _d = sampler._knn(handle, q, k=15)
        target_pos = int(positions[3])
        target_row = int(handle.rowids[target_pos])
        recency[target_row] = boost
        weights = sampler._apply_recency(handle, positions, dist_w)
        # locate target among returned positions
        idx = list(int(p) for p in positions).index(target_pos)
        return float(weights[idx])

    p1 = prob_of_target_with_boost(1.0)
    p2 = prob_of_target_with_boost(2.0)
    p4 = prob_of_target_with_boost(4.0)
    assert p1 < p2 < p4, (p1, p2, p4)


def test_two_equidistant_neighbours_split_by_recency(tmp_path):
    """Two neighbours at the SAME distance: their post-recency probabilities are
    in exact proportion to their recency_weights."""
    # Place two vectors symmetric about the query so distances are identical.
    vectors = np.array([
        [1.0, 0.0, 0.0],
        [-1.0, 0.0, 0.0],
    ], dtype=np.float32)
    rowids = np.array([700_000, 700_001], dtype=np.int64)
    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_BATTEDBALL, "season": SEASON})

    recency = {700_000: 3.0, 700_001: 1.0}
    sampler = PlayPoolSampler(
        pool_dir=str(tmp_path), duckdb_path=None,
        rng=np.random.default_rng(0),
        outcome_fetch=lambda pool, rids: {int(r): "single" for r in rids},
        recency_fetch=lambda pool, rids: {int(r): recency[int(r)] for r in rids},
    )
    handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
    q = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    positions, dist_w, dists = sampler._knn(handle, q, k=2)
    # Equidistant -> equal distance weights.
    assert dist_w[0] == pytest.approx(dist_w[1])
    weights = sampler._apply_recency(handle, positions, dist_w)
    # Map back to row ids to compare against the 3:1 recency ratio.
    by_row = {int(handle.rowids[int(p)]): float(w) for p, w in zip(positions, weights)}
    assert by_row[700_000] == pytest.approx(0.75, abs=1e-9)  # 3/(3+1)
    assert by_row[700_001] == pytest.approx(0.25, abs=1e-9)  # 1/(3+1)


# ===========================================================================
# (C) return_distribution=True reweights by recency
# ===========================================================================


def test_distribution_reweights_by_recency(tmp_path):
    """Distribution path: boosting all rows of one event type shifts probability
    mass toward that event, relative to the uniform-recency distribution."""
    rng = np.random.default_rng(3)
    n = 90
    vectors = rng.uniform(-1.0, 1.0, size=(n, 3)).astype(np.float32)
    rowids = np.arange(n, dtype=np.int64) + 700_000
    vocab = ["single", "double", "field_out"]
    outcomes = {int(rid): vocab[i % len(vocab)] for i, rid in enumerate(rowids)}
    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_BATTEDBALL, "season": SEASON})

    def outcome_fetch(pool, rids):
        return {int(r): outcomes[int(r)] for r in rids}

    q = vectors[20].astype(np.float32) + 0.15

    def dist_with(recency):
        sampler = PlayPoolSampler(
            pool_dir=str(tmp_path), duckdb_path=None,
            rng=np.random.default_rng(0), outcome_fetch=outcome_fetch,
            recency_fetch=lambda pool, rids: {int(r): recency[int(r)] for r in rids},
        )
        return sampler.sample_batted_ball("R", SEASON, q, k=30,
                                          return_distribution=True)

    uniform = {int(r): 1.0 for r in rowids}
    boost_singles = {int(r): (5.0 if outcomes[int(r)] == "single" else 1.0)
                     for r in rowids}

    d_uniform = dist_with(uniform)
    d_boost = dist_with(boost_singles)

    # Both are valid probability distributions over the closed vocab.
    assert abs(sum(d_uniform.values()) - 1.0) < 1e-9
    assert abs(sum(d_boost.values()) - 1.0) < 1e-9
    # Boosting singles raises P(single) and lowers the others' aggregate share.
    assert d_boost.get("single", 0.0) > d_uniform.get("single", 0.0)


def test_distribution_two_events_exact_recency_ratio(tmp_path):
    """Two equidistant neighbours, distinct events: distribution probabilities
    equal the normalized recency_weights exactly."""
    vectors = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]], dtype=np.float32)
    rowids = np.array([700_000, 700_001], dtype=np.int64)
    outcomes = {700_000: "single", 700_001: "field_out"}
    tile_dir = os.path.join(str(tmp_path), str(SEASON), POOL_BATTEDBALL)
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_BATTEDBALL, "season": SEASON})

    recency = {700_000: 2.0, 700_001: 1.0}
    sampler = PlayPoolSampler(
        pool_dir=str(tmp_path), duckdb_path=None,
        rng=np.random.default_rng(0),
        outcome_fetch=lambda pool, rids: {int(r): outcomes[int(r)] for r in rids},
        recency_fetch=lambda pool, rids: {int(r): recency[int(r)] for r in rids},
    )
    dist = sampler.sample_batted_ball("R", SEASON, np.zeros(3, dtype=np.float32),
                                      k=2, return_distribution=True)
    assert dist["single"] == pytest.approx(2.0 / 3.0, abs=1e-9)
    assert dist["field_out"] == pytest.approx(1.0 / 3.0, abs=1e-9)


# ===========================================================================
# (D) missing / None recency resolves to 1.0 (recency-neutral, no crash)
# ===========================================================================


def test_none_recency_defaults_to_one(tmp_path):
    """A recency_fetch returning None for a row must default that row to 1.0 and
    therefore reproduce the pure-distance weights."""
    sampler, vectors, rowids, _ = _make_battedball_sampler(
        tmp_path, recency={int(r): None for r in (np.arange(200) + 700_000)})
    handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
    q = vectors[7].astype(np.float32) + 0.1
    positions, dist_w, _d = sampler._knn(handle, q, k=20)
    weights = sampler._apply_recency(handle, positions, dist_w)
    np.testing.assert_allclose(weights, dist_w, rtol=1e-6, atol=1e-12)


def test_partial_recency_map_defaults_missing_rows(tmp_path):
    """recency_fetch that omits some row ids -> those rows default to 1.0; the
    sampler must not raise and weights remain a valid probability vector."""
    sampler, vectors, rowids, _ = _make_battedball_sampler(
        tmp_path, recency={int(rowids := 700_000): 5.0})  # only one row mapped
    handle = sampler.load_tile(POOL_BATTEDBALL, SEASON, "R")
    q = vectors[3].astype(np.float32) + 0.1
    positions, dist_w, _d = sampler._knn(handle, q, k=20)
    weights = sampler._apply_recency(handle, positions, dist_w)
    assert abs(float(weights.sum()) - 1.0) < 1e-9
    assert np.all(weights >= 0.0)


# ===========================================================================
# (E) the pitch path applies recency too
# ===========================================================================


def test_sample_pitch_applies_recency(tmp_path):
    """sample_pitch must route through _apply_recency: two equidistant pitch
    neighbours split selection probability by their recency_weights."""
    vectors = np.array([
        [1.0] + [0.0] * 9,
        [-1.0] + [0.0] * 9,
    ], dtype=np.float32)
    rowids = np.array([800_000, 800_001], dtype=np.int64)
    tile_dir = os.path.join(str(tmp_path), str(SEASON), str(PITCHER))
    _write_synthetic_tile(tile_dir, "R", vectors, rowids,
                          {"pool": POOL_PITCH, "season": SEASON,
                           "pitcher_id": PITCHER, "n_source_rows": 200})

    recency = {800_000: 9.0, 800_001: 1.0}
    sampler = PlayPoolSampler(
        pool_dir=str(tmp_path), duckdb_path=None,
        rng=np.random.default_rng(123),
        outcome_fetch=lambda pool, rids: {int(r): "ball" for r in rids},
        recency_fetch=lambda pool, rids: {int(r): recency[int(r)] for r in rids},
    )
    q = np.zeros(10, dtype=np.float32)
    n_draws = 2000
    counts = {800_000: 0, 800_001: 0}
    for _ in range(n_draws):
        out = sampler.sample_pitch(PITCHER, "R", SEASON, q, k=2)
        counts[out["row_id"]] += 1
    # Expected ~0.9 for the 9:1-boosted neighbour; allow sampling slack.
    frac = counts[800_000] / n_draws
    assert 0.85 < frac < 0.95, (counts, frac)
