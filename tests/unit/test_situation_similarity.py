"""
The situation engine, similarity/engines/situation_similarity.py.

The engine finds the historical game states nearest a query state through a
KD-tree (a tree that splits the space one feature at a time, for fast
nearest-neighbour lookup). The tests cover the data classes, the
SituationNormalizer, the build and its load (with a mocked DuckDB), query and
query_batch, and the build_coverage_report helper.

The mock replaces ``duckdb.connect()`` where the engine calls it, so build()
runs with no real database. The mock returns rows in the column order of the
SELECT in _load_situations().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from similarity.engines.situation_similarity import (
    DEFAULT_K,
    FEATURE_NAMES,
    FEATURE_SCALE,
    FEATURE_WEIGHTS,
    MIN_INDEX_SIZE,
    SCORE_DIFF_CLIP,
    SITUATION_FEATURES,
    ColumnarSituationMeta,
    NearestSituation,
    SituationNormalizer,
    SituationSimilarityEngine,
    SituationVector,
    build_coverage_report,
    sorted_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vec(**overrides) -> SituationVector:
    """Build a SituationVector with sensible defaults; override any field."""
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
    """Build a single row matching the SELECT column order in _load_situations."""
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
    """Return a context manager that patches `duckdb.connect` to yield given rows."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = rows
    return patch(
        "similarity.engines.situation_similarity.duckdb.connect",
        return_value=mock_conn,
    )


# ===========================================================================
# Module-level constants & sanity
# ===========================================================================


class TestModuleConstants:
    def test_feature_names_have_expected_count(self):
        assert len(SITUATION_FEATURES) == 11
        assert len(FEATURE_NAMES) == 11
        assert FEATURE_NAMES[0] == "inning"
        assert "leverage_index" in FEATURE_NAMES
        assert "park_factor_runs" in FEATURE_NAMES

    def test_feature_weights_and_scale_align(self):
        assert FEATURE_WEIGHTS.shape == (11,)
        assert FEATURE_SCALE.shape == (11,)
        np.testing.assert_allclose(FEATURE_SCALE, np.sqrt(FEATURE_WEIGHTS))

    def test_score_diff_clip_is_positive(self):
        assert SCORE_DIFF_CLIP > 0

    def test_default_k_below_min_index_size(self):
        # The default query K stays below the smallest index the build
        # accepts without a warning.
        assert DEFAULT_K < MIN_INDEX_SIZE


# ===========================================================================
# SituationVector
# ===========================================================================


class TestSituationVector:
    def test_to_array_matches_field_order(self):
        v = _make_vec(inning=3, outs=2, leverage_index=2.5)
        arr = v.to_array()
        assert arr.dtype == np.float64
        assert arr.shape == (11,)
        assert arr[0] == 3.0
        assert arr[2] == 2.0
        assert arr[7] == 2.5

    def test_to_array_clips_score_differential_positive(self):
        v = _make_vec(score_differential=20.0)
        arr = v.to_array()
        assert arr[6] == float(SCORE_DIFF_CLIP)

    def test_to_array_clips_score_differential_negative(self):
        v = _make_vec(score_differential=-20.0)
        arr = v.to_array()
        assert arr[6] == float(-SCORE_DIFF_CLIP)

    def test_to_array_runners_encoded_as_zero_or_one(self):
        v = _make_vec(runner_on_1b=1, runner_on_2b=0, runner_on_3b=1)
        arr = v.to_array()
        assert arr[3] == 1.0
        assert arr[4] == 0.0
        assert arr[5] == 1.0


# ===========================================================================
# NearestSituation
# ===========================================================================


class TestNearestSituation:
    def test_frozen_dataclass(self):
        ns = NearestSituation(
            play_id="abc",
            game_pk=1,
            distance=0.5,
            inning=7,
            outs=2,
            runners=0b010,
            leverage_index=2.0,
            score_differential=1.0,
        )
        with pytest.raises(Exception):  # noqa: B017 — frozen-dataclass raises FrozenInstanceError
            ns.distance = 0.0  # type: ignore[misc]


# ===========================================================================
# SituationNormalizer
# ===========================================================================


class TestSituationNormalizer:
    def test_normalize_uninitialized_just_scales(self):
        """Before fit(), normalize() applies FEATURE_SCALE only."""
        norm = SituationNormalizer()
        vec = np.ones(11, dtype=np.float64)
        out = norm.normalize(vec)
        np.testing.assert_allclose(out, FEATURE_SCALE)

    def test_normalize_batch_uninitialized_just_scales(self):
        """Before fit(), normalize_batch() returns early and applies FEATURE_SCALE only."""
        norm = SituationNormalizer()
        matrix = np.ones((3, 11), dtype=np.float64)
        out = norm.normalize_batch(matrix)
        # Each row equals FEATURE_SCALE
        np.testing.assert_allclose(out[0], FEATURE_SCALE)
        np.testing.assert_allclose(out[1], FEATURE_SCALE)
        np.testing.assert_allclose(out[2], FEATURE_SCALE)

    def test_fit_then_normalize_zero_mean_unit_std(self):
        norm = SituationNormalizer()
        # A matrix with a known mean and std per column.
        rng = np.random.default_rng(42)
        matrix = rng.normal(loc=5.0, scale=2.0, size=(500, 11))
        norm.fit(matrix)
        normed = norm.normalize_batch(matrix)
        # After the z-score and the FEATURE_SCALE weighting, each column's
        # std is about its FEATURE_SCALE.
        col_stds = normed.std(axis=0)
        np.testing.assert_allclose(col_stds, FEATURE_SCALE, rtol=0.1)

    def test_fit_zero_variance_column_clamped(self):
        """A column with zero std is clamped to 1.0, so the z-score never
        divides by zero."""
        norm = SituationNormalizer()
        matrix = np.zeros((10, 11), dtype=np.float64)
        matrix[:, 0] = 5.0  # constant column
        norm.fit(matrix)
        # Normalizing produces no NaN or Inf.
        out = norm.normalize_batch(matrix)
        assert np.isfinite(out).all()

    def test_normalize_replaces_nan_with_zero(self):
        norm = SituationNormalizer()
        norm.fit(np.zeros((10, 11), dtype=np.float64) + 1.0)
        vec = np.full(11, np.nan, dtype=np.float64)
        out = norm.normalize(vec)
        assert np.isfinite(out).all()


# ===========================================================================
# SituationSimilarityEngine — init + build (mocked DuckDB)
# ===========================================================================


class TestEngineInit:
    def test_initial_state(self):
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        assert eng.index_size == 0
        assert eng.is_built() is False

    def test_query_before_build_raises(self):
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        with pytest.raises(RuntimeError, match="not built"):
            eng.query(_make_vec(), k=5)

    def test_query_batch_before_build_raises(self):
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        with pytest.raises(RuntimeError, match="not built"):
            eng.query_batch([_make_vec(), _make_vec()], k=5)


class TestEngineBuild:
    def _make_synthetic_rows(self, n: int = 1500) -> list[tuple]:
        """A spread of rows, so the KD-tree has variety."""
        rows = []
        rng = np.random.default_rng(7)
        for i in range(n):
            rows.append(
                _fake_row(
                    play_id=f"p{i}",
                    game_pk=700000 + i,
                    inning=int(rng.integers(1, 10)),
                    top_or_bottom=int(rng.integers(0, 2)),
                    outs=int(rng.integers(0, 3)),
                    on_1b=int(rng.integers(0, 2)),
                    on_2b=int(rng.integers(0, 2)),
                    on_3b=int(rng.integers(0, 2)),
                    score_diff=float(rng.integers(-4, 5)),
                    li=float(rng.uniform(0.1, 4.0)),
                    pc=int(rng.integers(0, 120)),
                    pa_count=int(rng.integers(1, 5)),
                    park_factor=float(rng.uniform(0.85, 1.15)),
                )
            )
        return rows

    def test_build_indexes_all_rows(self):
        rows = self._make_synthetic_rows(1500)
        with _patch_duckdb_with_rows(rows):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            eng.build()
        assert eng.is_built() is True
        assert eng.index_size == 1500

    def test_build_with_seasons_filter_appends_clause(self):
        """With seasons=[2024, 2025], the SQL text names both seasons."""
        rows = self._make_synthetic_rows(1100)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = rows
        with patch(
            "similarity.engines.situation_similarity.duckdb.connect",
            return_value=mock_conn,
        ):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            eng.build(seasons=[2024, 2025])
        sql_called = mock_conn.execute.call_args.args[0]
        assert "2024" in sql_called
        assert "2025" in sql_called

    def test_build_below_min_index_size_still_builds_and_warns(self, caplog):
        """Fewer than MIN_INDEX_SIZE rows still build (the build also logs a
        warning, which this test does not check)."""
        rows = self._make_synthetic_rows(50)
        with _patch_duckdb_with_rows(rows):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            with caplog.at_level("WARNING"):
                eng.build()
        assert eng.is_built() is True
        assert eng.index_size == 50

    def test_build_missing_catalog_raises(self):
        """A missing derived.at_bat_situations makes build() raise.

        The load swallows the CatalogException and returns no rows, and an
        empty index would z-score to NaN. The raise lets build_all_engines mark
        the engine failed and skip it, as it does for any engine whose source
        table is missing.
        """
        import duckdb as ddb_mod

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = ddb_mod.CatalogException("derived.at_bat_situations")
        with patch(
            "similarity.engines.situation_similarity.duckdb.connect",
            return_value=mock_conn,
        ):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            with pytest.raises(RuntimeError, match="no situations loaded"):
                eng.build()
        assert eng.is_built() is False  # left unbuilt → registry skips it

    def test_build_zero_rows_raises(self):
        """An empty situations table that exists also makes build() raise."""
        with _patch_duckdb_with_rows([]):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            with pytest.raises(RuntimeError, match="degenerate empty index"):
                eng.build()
        assert eng.is_built() is False

    def test_build_row_unpack_handles_null_optionals(self):
        """A row with None values takes the defaults and does not raise."""
        rows = [
            _fake_row(play_id=None, game_pk=None, inning=None, outs=None, li=None)
            for _ in range(1200)
        ]
        with _patch_duckdb_with_rows(rows):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            eng.build()
        # Every meta entry has an inning of 1, the default for None.
        assert all(m.inning == 1 for m in eng._index_meta)

    def test_build_score_diff_clipped_in_meta(self):
        rows = [_fake_row(score_diff=99.0) for _ in range(1200)]
        with _patch_duckdb_with_rows(rows):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            eng.build()
        assert all(m.score_differential == float(SCORE_DIFF_CLIP) for m in eng._index_meta)

    def test_build_runners_bitmask(self):
        """The bitmask in NearestSituation.runners is 1B=1, 2B=2, 3B=4."""
        rows = [
            _fake_row(on_1b=1, on_2b=1, on_3b=0),
            _fake_row(on_1b=0, on_2b=0, on_3b=1),
            _fake_row(on_1b=1, on_2b=1, on_3b=1),
        ] * 400
        with _patch_duckdb_with_rows(rows):
            eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
            eng.build()
        bitmasks = {m.runners for m in eng._index_meta}
        assert 0b011 in bitmasks  # 1B + 2B
        assert 0b100 in bitmasks  # 3B only
        assert 0b111 in bitmasks  # bases loaded


# ===========================================================================
# Query / Query Batch (using built engine)
# ===========================================================================


@pytest.fixture()
def built_engine() -> SituationSimilarityEngine:
    """Provide a small engine built from a deterministic synthetic index."""
    rng = np.random.default_rng(123)
    rows = []
    for i in range(1500):
        rows.append(
            _fake_row(
                play_id=f"p{i}",
                game_pk=700000 + i,
                inning=int(rng.integers(1, 10)),
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
    with _patch_duckdb_with_rows(rows):
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        eng.build()
    return eng


class TestQuery:
    def test_query_returns_k_results(self, built_engine):
        out = built_engine.query(_make_vec(), k=20)
        assert len(out) == 20
        assert all(isinstance(r, NearestSituation) for r in out)

    def test_query_results_sorted_by_distance(self, built_engine):
        out = built_engine.query(_make_vec(), k=20)
        distances = [r.distance for r in out]
        assert distances == sorted(distances)

    def test_query_k_larger_than_index_capped(self, built_engine):
        """With K > index_size, query returns index_size results."""
        out = built_engine.query(_make_vec(), k=10_000_000)
        assert len(out) == built_engine.index_size

    def test_query_k_equals_one(self, built_engine):
        """At k=1 the KD-tree returns scalars; query wraps them as arrays."""
        out = built_engine.query(_make_vec(), k=1)
        assert len(out) == 1
        assert isinstance(out[0].distance, float)

    def test_query_empty_index_returns_empty(self):
        """An empty index makes query return [] (a defensive guard).

        build() raises on an empty index, so the test sets the empty state by
        hand to reach the ``_index_size == 0`` early return in query().
        """
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        eng._kdtree = object()  # non-None so the "not built" guard passes
        eng._index_meta = ColumnarSituationMeta.empty()
        eng._index_size = 0
        assert eng.query(_make_vec(), k=10) == []


class TestQueryBatch:
    def test_query_batch_returns_per_query_results(self, built_engine):
        qs = [_make_vec(inning=i) for i in [3, 5, 7]]
        out = built_engine.query_batch(qs, k=10)
        assert len(out) == 3
        for row in out:
            assert len(row) == 10
            assert all(isinstance(r, NearestSituation) for r in row)

    def test_query_batch_k_equals_one(self, built_engine):
        """At k=1 the KD-tree drops the K axis; query_batch restores it."""
        out = built_engine.query_batch([_make_vec(), _make_vec(outs=2)], k=1)
        assert len(out) == 2
        assert all(len(row) == 1 for row in out)

    def test_query_batch_empty_index_returns_empty_rows(self):
        """An empty index gives one [] per query (a defensive guard).

        The test sets the empty state by hand: build() raises on an empty index.
        """
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        eng._kdtree = object()  # non-None so the "not built" guard passes
        eng._index_meta = ColumnarSituationMeta.empty()
        eng._index_size = 0
        out = eng.query_batch([_make_vec(), _make_vec()], k=5)
        assert out == [[], []]


# ===========================================================================
# Utility methods
# ===========================================================================


class TestUtilities:
    def test_situation_count_by_outs(self, built_engine):
        counts = built_engine.situation_count_by_outs()
        assert set(counts.keys()) >= {0, 1, 2}
        assert sum(counts.values()) == built_engine.index_size

    def test_situation_count_by_inning_sorted(self, built_engine):
        counts = built_engine.situation_count_by_inning()
        keys = list(counts.keys())
        assert keys == sorted(keys)

    def test_sorted_dict_helper(self):
        out = sorted_dict({3: "c", 1: "a", 2: "b"})
        assert list(out.keys()) == [1, 2, 3]


# ===========================================================================
# Coverage report
# ===========================================================================


class TestCoverageReport:
    def test_build_coverage_report_text(self, built_engine):
        report = build_coverage_report(built_engine)
        assert "SituationSimilarityEngine Coverage Report" in report
        assert "Total indexed situations" in report
        assert "Inning" in report and "Outs=0" in report
        # At least one inning row carries data.
        lines = report.splitlines()
        data_lines = [
            line for line in lines if line and line[0].isdigit() and len(line.split()) >= 4
        ]
        assert len(data_lines) >= 1

    def test_build_coverage_report_empty_engine(self):
        """An empty engine still gives a report header, with no per-inning rows.

        The test sets the empty state by hand: build() raises on an empty index.
        """
        eng = SituationSimilarityEngine(duckdb_path="/tmp/whatever.duckdb")
        eng._index_meta = ColumnarSituationMeta.empty()
        eng._index_size = 0
        report = build_coverage_report(eng)
        assert "Total indexed situations: 0" in report
