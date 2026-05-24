"""
Unit tests for SIM-334 — columnar situation metadata store.
============================================================
Verifies that replacing the situation engine's ``list[NearestSituation]``
metadata with parallel NumPy column arrays (``ColumnarSituationMeta``):

  1. Keeps the public ``query`` / ``query_batch`` API byte-for-byte
     equivalent: every returned ``NearestSituation`` carries the same field
     values (play_id, game_pk, inning, outs, runners, leverage_index,
     score_differential) and the same query-time distance as a reference
     reconstructed straight from the columnar arrays.
  2. Stores metadata as NumPy arrays (one per field, correct dtypes), NOT a
     ``list`` of Python objects, and the arrays are read-only.
  3. Handles edge cases: empty index, single row, k=1, k > index_size.

Mocking strategy mirrors test_situation_similarity.py: DuckDB is patched at
``duckdb.connect`` so build() runs without a real database.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from similarity.engines.situation_similarity import (
    ColumnarSituationMeta,
    NearestSituation,
    SituationSimilarityEngine,
    SituationVector,
)

# ---------------------------------------------------------------------------
# Helpers (shared shape with the existing situation tests)
# ---------------------------------------------------------------------------


def _make_vec(**overrides) -> SituationVector:
    base = {
        "inning": 5,
        "top_or_bottom": 0,
        "outs": 1,
        "runner_on_1b": 0,
        "runner_on_2b": 0,
        "runner_on_3b": 0,
        "score_differential": 0.0,
        "leverage_index": 1.0,
        "pitcher_pitch_count": 50,
        "batter_pa_count": 2,
        "park_factor_runs": 1.0,
    }
    base.update(overrides)
    return SituationVector(**base)


def _fake_row(
    *,
    play_id: str = "p1",
    game_pk: int = 700001,
    inning: int = 5,
    top_or_bottom: int = 0,
    outs: int = 1,
    on_1b: int = 0,
    on_2b: int = 0,
    on_3b: int = 0,
    score_diff: float = 0.0,
    li: float = 1.0,
    pc: int = 50,
    pa_count: int = 2,
    park_factor: float = 1.0,
) -> tuple:
    return (
        play_id,
        game_pk,
        inning,
        top_or_bottom,
        outs,
        on_1b,
        on_2b,
        on_3b,
        score_diff,
        li,
        pc,
        pa_count,
        park_factor,
    )


def _patch_duckdb_with_rows(rows: list[tuple]):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = rows
    return patch(
        "similarity.engines.situation_similarity.duckdb.connect",
        return_value=mock_conn,
    )


def _build_engine(rows: list[tuple]) -> SituationSimilarityEngine:
    with _patch_duckdb_with_rows(rows):
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        eng.build()
    return eng


def _varied_rows(n: int, seed: int = 99) -> list[tuple]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        rows.append(
            _fake_row(
                play_id=f"play_{i:06d}",
                game_pk=700000 + i,
                inning=int(rng.integers(1, 10)),
                top_or_bottom=int(rng.integers(0, 2)),
                outs=int(rng.integers(0, 3)),
                on_1b=int(rng.integers(0, 2)),
                on_2b=int(rng.integers(0, 2)),
                on_3b=int(rng.integers(0, 2)),
                score_diff=float(rng.integers(-4, 5)),
                li=float(rng.uniform(0.5, 3.0)),
                pc=int(rng.integers(10, 100)),
                pa_count=int(rng.integers(1, 5)),
                park_factor=float(rng.uniform(0.9, 1.1)),
            )
        )
    return rows


def _assert_situations_equal(a: NearestSituation, b: NearestSituation) -> None:
    assert a.play_id == b.play_id
    assert a.game_pk == b.game_pk
    assert a.inning == b.inning
    assert a.outs == b.outs
    assert a.runners == b.runners
    assert a.leverage_index == pytest.approx(b.leverage_index)
    assert a.score_differential == pytest.approx(b.score_differential)
    assert a.distance == pytest.approx(b.distance)


@pytest.fixture()
def built_engine() -> SituationSimilarityEngine:
    return _build_engine(_varied_rows(1500, seed=123))


# ===========================================================================
# The store IS columnar NumPy arrays, not a list of objects
# ===========================================================================


class TestColumnarStorage:
    def test_index_meta_is_columnar_not_list(self, built_engine):
        meta = built_engine._index_meta
        assert isinstance(meta, ColumnarSituationMeta)
        assert not isinstance(meta, list)

    def test_each_field_is_a_numpy_array(self, built_engine):
        meta = built_engine._index_meta
        for field in (
            "play_id",
            "game_pk",
            "inning",
            "outs",
            "runners",
            "leverage_index",
            "score_differential",
        ):
            arr = getattr(meta, field)
            assert isinstance(arr, np.ndarray), f"{field} is not an ndarray"
            assert arr.shape[0] == built_engine.index_size

    def test_column_dtypes(self, built_engine):
        meta = built_engine._index_meta
        assert meta.play_id.dtype.kind == "U"  # fixed-width unicode
        assert meta.game_pk.dtype == np.int64
        assert meta.inning.dtype == np.int16
        assert meta.outs.dtype == np.int8
        assert meta.runners.dtype == np.int8
        assert meta.leverage_index.dtype == np.float64
        assert meta.score_differential.dtype == np.float64

    def test_columns_are_read_only(self, built_engine):
        """Read-only/lazy store: the underlying arrays must not be writeable."""
        meta = built_engine._index_meta
        assert meta.game_pk.flags.writeable is False
        assert meta.play_id.flags.writeable is False
        assert meta.leverage_index.flags.writeable is False
        with pytest.raises(ValueError):
            meta.game_pk[0] = 12345

    def test_no_python_object_dtype_columns(self, built_engine):
        """Guards against a regression to dtype=object (i.e. boxed Python objs)."""
        meta = built_engine._index_meta
        for field in (
            "play_id",
            "game_pk",
            "inning",
            "outs",
            "runners",
            "leverage_index",
            "score_differential",
        ):
            assert getattr(meta, field).dtype != np.dtype("O")


# ===========================================================================
# query() returns results identical to a columnar reference
# ===========================================================================


class TestQueryEquivalence:
    def test_query_results_match_columnar_reference(self, built_engine):
        """Every returned NearestSituation must equal a reference reconstructed
        directly from the columnar arrays at the same KDTree index."""
        meta = built_engine._index_meta
        vec = _make_vec(inning=7, outs=2, leverage_index=2.3)

        # Drive the KDTree directly to get the ground-truth (dist, idx) pairs.
        query_vec = built_engine._normalizer.normalize(vec.to_array())
        distances, indices = built_engine._kdtree.query(query_vec, k=25)

        out = built_engine.query(vec, k=25)
        assert len(out) == 25

        for result, dist, idx in zip(out, distances, indices, strict=True):
            ref = meta.row(int(idx), distance=float(dist))
            _assert_situations_equal(result, ref)

    def test_query_distance_is_float(self, built_engine):
        out = built_engine.query(_make_vec(), k=10)
        for r in out:
            assert type(r.distance) is float

    def test_query_fields_have_native_python_types(self, built_engine):
        """Reconstructed fields must be plain Python scalars, not numpy types,
        so results are byte-for-byte equivalent to the old object path."""
        r = built_engine.query(_make_vec(), k=1)[0]
        assert type(r.play_id) is str
        assert type(r.game_pk) is int
        assert type(r.inning) is int
        assert type(r.outs) is int
        assert type(r.runners) is int
        assert type(r.leverage_index) is float
        assert type(r.score_differential) is float


# ===========================================================================
# query_batch() matches per-query single query()
# ===========================================================================


class TestQueryBatchEquivalence:
    def test_batch_matches_single_query(self, built_engine):
        vecs = [
            _make_vec(inning=3, outs=0),
            _make_vec(inning=6, outs=1, leverage_index=2.5),
            _make_vec(inning=9, outs=2, runner_on_2b=1),
        ]
        batch = built_engine.query_batch(vecs, k=15)
        assert len(batch) == 3
        for vec, batch_row in zip(vecs, batch, strict=True):
            single = built_engine.query(vec, k=15)
            assert len(batch_row) == len(single) == 15
            for b, s in zip(batch_row, single, strict=True):
                _assert_situations_equal(b, s)

    def test_batch_matches_columnar_reference(self, built_engine):
        meta = built_engine._index_meta
        vecs = [_make_vec(inning=i) for i in (2, 5, 8)]
        query_matrix = np.array(
            [built_engine._normalizer.normalize(v.to_array()) for v in vecs],
            dtype=np.float64,
        )
        all_d, all_i = built_engine._kdtree.query(query_matrix, k=12)
        batch = built_engine.query_batch(vecs, k=12)
        for row, dists, idxs in zip(batch, all_d, all_i, strict=True):
            for result, dist, idx in zip(row, dists, idxs, strict=True):
                _assert_situations_equal(result, meta.row(int(idx), distance=float(dist)))


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_empty_index_query_returns_empty(self):
        eng = _build_engine([])
        assert eng.index_size == 0
        assert isinstance(eng._index_meta, ColumnarSituationMeta)
        assert len(eng._index_meta) == 0
        assert eng.query(_make_vec(), k=10) == []

    def test_empty_index_query_batch_returns_empty_rows(self):
        eng = _build_engine([])
        assert eng.query_batch([_make_vec(), _make_vec()], k=5) == [[], []]

    def test_single_row_index(self):
        """A one-row index: k=1 returns exactly that row's metadata."""
        row = _fake_row(
            play_id="solo",
            game_pk=999,
            inning=8,
            outs=2,
            on_1b=1,
            on_2b=0,
            on_3b=1,
            score_diff=2.0,
            li=3.3,
        )
        eng = _build_engine([row])
        assert eng.index_size == 1
        out = eng.query(_make_vec(), k=1)
        assert len(out) == 1
        r = out[0]
        assert r.play_id == "solo"
        assert r.game_pk == 999
        assert r.inning == 8
        assert r.outs == 2
        assert r.runners == 0b101  # 1B + 3B
        assert r.leverage_index == pytest.approx(3.3)
        assert r.score_differential == pytest.approx(2.0)

    def test_k_equals_one_scalar_branch(self, built_engine):
        out = built_engine.query(_make_vec(), k=1)
        assert len(out) == 1
        assert isinstance(out[0], NearestSituation)

    def test_k_larger_than_index_capped(self):
        eng = _build_engine(_varied_rows(40, seed=7))
        assert eng.index_size == 40
        out = eng.query(_make_vec(), k=10_000)
        assert len(out) == eng.index_size

    def test_k_larger_than_index_batch_capped(self):
        eng = _build_engine(_varied_rows(40, seed=11))
        out = eng.query_batch([_make_vec(), _make_vec(outs=2)], k=10_000)
        assert len(out) == 2
        for r in out:
            assert len(r) == eng.index_size

    def test_query_batch_k_equals_one_single_row_index(self):
        eng = _build_engine([_fake_row(play_id="x", game_pk=1)])
        out = eng.query_batch([_make_vec(), _make_vec(outs=0)], k=1)
        assert len(out) == 2
        for r in out:
            assert len(r) == 1
            assert r[0].play_id == "x"


# ===========================================================================
# ColumnarSituationMeta unit behavior
# ===========================================================================


class TestColumnarStoreUnit:
    def test_empty_store(self):
        store = ColumnarSituationMeta.empty()
        assert len(store) == 0
        assert list(store) == []

    def test_from_columns_roundtrip(self):
        store = ColumnarSituationMeta.from_columns(
            play_id=["a", "bb", "ccc"],
            game_pk=[1, 2, 3],
            inning=[1, 5, 9],
            outs=[0, 1, 2],
            runners=[0, 3, 7],
            leverage_index=[0.5, 1.0, 2.5],
            score_differential=[-3.0, 0.0, 4.0],
        )
        assert len(store) == 3
        r = store.row(1, distance=1.25)
        assert r.play_id == "bb"
        assert r.game_pk == 2
        assert r.inning == 5
        assert r.outs == 1
        assert r.runners == 3
        assert r.leverage_index == pytest.approx(1.0)
        assert r.score_differential == pytest.approx(0.0)
        assert r.distance == pytest.approx(1.25)

    def test_iteration_yields_nearest_situations(self):
        store = ColumnarSituationMeta.from_columns(
            play_id=["a", "b"],
            game_pk=[10, 20],
            inning=[3, 4],
            outs=[1, 2],
            runners=[1, 2],
            leverage_index=[1.1, 2.2],
            score_differential=[0.0, 1.0],
        )
        rows = list(store)
        assert all(isinstance(r, NearestSituation) for r in rows)
        assert [r.game_pk for r in rows] == [10, 20]
        # default distance sentinel is 0.0 during iteration
        assert all(r.distance == 0.0 for r in rows)
