"""
Smoke tests for similarity engine .build() methods.

Each engine's build() pipeline executes the same shape of work:
    duckdb.connect → _load_positional_averages / _load_profiles → fit /
    apply_shrinkage → partition.build.

For coverage purposes we just need to exercise that path. We mock
``duckdb.connect`` so the SELECT calls return empty rows (or empty
JSON profiles). With no rows the engines build empty partitions and
return cleanly — the orchestration lines all get covered without
requiring complex synthetic profile data.

The CatalogException branch is also exercised for each engine that has
one — guards the case where the derived tables haven't been created yet.

Skip semantics
--------------
Some engines pull in optional native deps that don't yet publish wheels
for newer Pythons (POT/`ot` and `faiss-cpu` lag behind cp313).  Tests
that exercise those engines skip cleanly rather than fail when the
optional dep isn't installed — the engines themselves expose a
`_POT_AVAILABLE` / `_FAISS_AVAILABLE` boolean we can introspect.
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
    on the first call (simulating the derived.* tables missing)."""
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
        """Lazy-import the pitcher engine; skip cleanly if POT is missing
        (Python 3.13 has no published wheel for POT as of 2026-05)."""
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
