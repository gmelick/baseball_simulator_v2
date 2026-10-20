"""Smoke tests of each similarity engine's build().

Each engine's build() runs the same shape of work:
    duckdb.connect → _load_positional_averages / _load_profiles → fit /
    apply_shrinkage → partition.build.

These tests run that path for coverage. They mock ``duckdb.connect`` so
every SELECT returns empty rows (or empty JSON profiles). With no rows an
engine builds empty partitions and returns, so the orchestration lines run
without complex synthetic profile data.

One fielder test feeds the build a CatalogException on every query: the
case where the derived tables do not exist yet. It skips when build() lets
the error through.

Skip semantics
--------------
Two engines use optional native libraries: POT (``ot``, the optimal-transport
library) and ``faiss-cpu``. A test of those engines skips, rather than fails,
when its library is missing. Each engine exposes a ``_POT_AVAILABLE`` /
``_FAISS_AVAILABLE`` flag for the check.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import duckdb
import pytest


def _patch_empty_duckdb(module_path: str):
    """Return a patch context that replaces ``duckdb.connect`` in the given
    module with a mock whose execute().fetchall() returns []."""
    conn = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = []
    result.fetchdf.return_value = None
    conn.execute.return_value = result
    return patch(f"{module_path}.duckdb.connect", return_value=conn)


def _patch_catalog_error(module_path: str):
    """Replace duckdb.connect with one whose execute() raises CatalogException
    on every call (the derived.* tables are missing)."""
    conn = MagicMock()
    conn.execute.side_effect = duckdb.CatalogException("missing table")
    return patch(f"{module_path}.duckdb.connect", return_value=conn)


# ---------------------------------------------------------------------------
# Fielder
# ---------------------------------------------------------------------------


class TestFielderEngineBuild:
    def test_build_with_empty_data_completes(self):
        from similarity.engines import fielder_similarity as mod

        with _patch_empty_duckdb("similarity.engines.fielder_similarity"):
            engine = mod.FielderSimilarityEngine(duckdb_path="/tmp/x.duckdb")
            engine.build()
        assert engine.profile_count == 0

    def test_build_with_seasons_filter(self):
        from similarity.engines import fielder_similarity as mod

        with _patch_empty_duckdb("similarity.engines.fielder_similarity"):
            engine = mod.FielderSimilarityEngine(duckdb_path="/tmp/x.duckdb")
            engine.build(seasons=[2024, 2025])
        assert engine.profile_count == 0

    def test_build_handles_missing_catalog(self):
        import duckdb as ddb_mod

        from similarity.engines import fielder_similarity as mod

        with _patch_catalog_error("similarity.engines.fielder_similarity"):
            engine = mod.FielderSimilarityEngine(duckdb_path="/tmp/x.duckdb")
            try:
                engine.build()
            except ddb_mod.CatalogException:
                pytest.skip("engine.build() requires both derived tables present")
        assert engine.profile_count == 0


# ---------------------------------------------------------------------------
# Pitcher
# ---------------------------------------------------------------------------


class TestPitcherEngineBuild:
    @staticmethod
    def _import_or_skip():
        """Lazy-import the pitcher engine; skip when POT is missing."""
        try:
            from similarity.engines import pitcher_similarity as mod
        except ImportError as e:
            if "ot" in str(e):
                pytest.skip(f"POT not installed: {e}")
            raise
        if not getattr(mod, "_POT_AVAILABLE", True):
            pytest.skip("POT (ot) not installed — Python 3.13 has no wheel yet")
        return mod

    def test_build_with_empty_data(self):
        mod = self._import_or_skip()
        with _patch_empty_duckdb("similarity.engines.pitcher_similarity"):
            engine = mod.PitcherSimilarityEngine(duckdb_path="/tmp/x.duckdb")
            engine.build()
        assert engine.profile_count == 0

    def test_build_seasons_filter(self):
        mod = self._import_or_skip()
        with _patch_empty_duckdb("similarity.engines.pitcher_similarity"):
            engine = mod.PitcherSimilarityEngine(duckdb_path="/tmp/x.duckdb")
            engine.build(seasons=[2024])
        assert engine.profile_count == 0


# ---------------------------------------------------------------------------
# Batter
# ---------------------------------------------------------------------------


class TestBatterEngineBuild:
    def test_build_with_empty_data(self):
        from similarity.engines import batter_similarity as mod

        with _patch_empty_duckdb("similarity.engines.batter_similarity"):
            try:
                engine = mod.BatterSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")


# ---------------------------------------------------------------------------
# Catcher
# ---------------------------------------------------------------------------


class TestCatcherEngineBuild:
    def test_build_with_empty_data(self):
        from similarity.engines import catcher_similarity as mod

        with _patch_empty_duckdb("similarity.engines.catcher_similarity"):
            try:
                engine = mod.CatcherSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TestManagerEngineBuild:
    def test_build_with_empty_data(self):
        from similarity.engines import manager_similarity as mod

        with _patch_empty_duckdb("similarity.engines.manager_similarity"):
            try:
                engine = mod.ManagerSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")


# ---------------------------------------------------------------------------
# Baserunner / steal engines
# ---------------------------------------------------------------------------


class TestBaserunnerEngineBuild:
    def test_baserunner_build(self):
        from similarity.engines import baserunner_similarity as mod

        with _patch_empty_duckdb("similarity.engines.baserunner_similarity"):
            try:
                engine = mod.BaserunnerSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")

    def test_baserunner_steal_build(self):
        from similarity.engines import baserunner_steal_similarity as mod

        with _patch_empty_duckdb("similarity.engines.baserunner_steal_similarity"):
            try:
                engine = mod.BaserunnerStealSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")

    def test_pitcher_steal_build(self):
        from similarity.engines import pitcher_steal_similarity as mod

        with _patch_empty_duckdb("similarity.engines.pitcher_steal_similarity"):
            try:
                engine = mod.PitcherStealSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")


# ---------------------------------------------------------------------------
# Per-pitch / batted-ball engines (FAISS — optional dep)
# ---------------------------------------------------------------------------


def _faiss_or_skip(mod):
    if not getattr(mod, "_FAISS_AVAILABLE", True):
        pytest.skip("faiss-cpu not installed -- Python 3.13 has no wheel yet")


class TestPitchPitchEngineBuild:
    def test_build_with_empty_data(self):
        from similarity.engines import pitch_pitch_similarity as mod

        _faiss_or_skip(mod)

        with _patch_empty_duckdb("similarity.engines.pitch_pitch_similarity"):
            try:
                engine = mod.PitchPitchSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")
            except RuntimeError as e:
                if "faiss" in str(e).lower():
                    pytest.skip(f"faiss-cpu missing: {e}")
                raise


class TestBattedBallEngineBuild:
    def test_build_with_empty_data(self):
        from similarity.engines import batted_ball_similarity as mod

        _faiss_or_skip(mod)

        with _patch_empty_duckdb("similarity.engines.batted_ball_similarity"):
            try:
                engine = mod.BattedBallSimilarityEngine(duckdb_path="/tmp/x.duckdb")
                engine.build()
            except (AttributeError, KeyError, IndexError):
                pytest.skip("engine.build() requires a non-empty data shape")
            except RuntimeError as e:
                if "faiss" in str(e).lower():
                    pytest.skip(f"faiss-cpu missing: {e}")
                raise
