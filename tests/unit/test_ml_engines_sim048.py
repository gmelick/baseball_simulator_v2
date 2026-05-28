"""Unit tests for the SimilarityEngineRegistry (SIM-048).

Validates the unified discovery/construction surface over the 11 similarity
engines, plus the project's score-discipline contract (RBF/GMM -> similarity in
[0, 1]; KDTree/FAISS -> distance).
"""

from __future__ import annotations

import inspect

import pytest

from similarity.registry import EngineSpec, SimilarityEngineRegistry

# Canonical catalogue expectations -----------------------------------------
EXPECTED_NAMES = [
    "pitcher",
    "batter",
    "fielder",
    "baserunner",
    "baserunner_steal",
    "catcher",
    "pitcher_steal",
    "manager",
    "situation",
    "pitch_to_pitch",
    "batted_ball",
]

# score_type == "distance" engines (geometric KDTree / FAISS).
DISTANCE_ENGINES = {"situation", "pitch_to_pitch", "batted_ball"}

# Expected algorithm family per engine.
EXPECTED_FAMILY = {
    "pitcher": "gmm",
    "batter": "rbf",
    "fielder": "rbf",
    "baserunner": "rbf",
    "baserunner_steal": "rbf",
    "catcher": "rbf",
    "pitcher_steal": "rbf",
    "manager": "rbf",
    "situation": "kdtree",
    "pitch_to_pitch": "faiss",
    "batted_ball": "faiss",
}


def test_list_engines_returns_exactly_eleven_canonical_names():
    names = SimilarityEngineRegistry.list_engines()
    assert len(names) == 11
    assert names == EXPECTED_NAMES
    # No duplicates.
    assert len(set(names)) == 11


def test_every_name_resolves_to_a_class():
    for name in SimilarityEngineRegistry.list_engines():
        cls = SimilarityEngineRegistry.get_class(name)
        assert inspect.isclass(cls), f"{name} did not resolve to a class"
        # The class name should look like an engine class.
        assert cls.__name__.endswith("Engine"), cls.__name__


def test_metadata_score_type_contract():
    for name in SimilarityEngineRegistry.list_engines():
        spec = SimilarityEngineRegistry.get_spec(name)
        assert isinstance(spec, EngineSpec)
        expected = "distance" if name in DISTANCE_ENGINES else "similarity"
        assert (
            spec.score_type == expected
        ), f"{name}: expected score_type {expected!r}, got {spec.score_type!r}"


def test_metadata_family_values():
    for name, family in EXPECTED_FAMILY.items():
        spec = SimilarityEngineRegistry.get_spec(name)
        assert spec.family == family, f"{name}: expected family {family!r}, got {spec.family!r}"


def test_family_and_score_type_are_consistent():
    # RBF/GMM -> similarity; KDTree/FAISS -> distance.
    for name in SimilarityEngineRegistry.list_engines():
        spec = SimilarityEngineRegistry.get_spec(name)
        if spec.family in {"rbf", "gmm"}:
            assert spec.score_type == "similarity"
        else:
            assert spec.family in {"kdtree", "faiss"}
            assert spec.score_type == "distance"


def test_spec_carries_description():
    for name in SimilarityEngineRegistry.list_engines():
        spec = SimilarityEngineRegistry.get_spec(name)
        assert isinstance(spec.description, str) and spec.description.strip()


def test_create_builds_an_rbf_engine():
    # Construction does not touch the DB (DB is only read in build()), so an
    # in-memory path suffices to instantiate.
    engine = SimilarityEngineRegistry.create("batter", duckdb_path=":memory:")
    cls = SimilarityEngineRegistry.get_class("batter")
    assert isinstance(engine, cls)
    assert engine.__class__.__name__ == "BatterSimilarityEngine"


def test_create_passes_kwargs_through():
    # Another RBF engine, exercising kwargs passthrough.
    engine = SimilarityEngineRegistry.create("manager", duckdb_path=":memory:")
    assert engine.__class__.__name__ == "ManagerSimilarityEngine"


def test_unknown_name_raises_clear_error():
    with pytest.raises(KeyError):
        SimilarityEngineRegistry.get_class("does_not_exist")
    with pytest.raises(KeyError):
        SimilarityEngineRegistry.get_spec("does_not_exist")
    with pytest.raises(KeyError):
        SimilarityEngineRegistry.create("does_not_exist", duckdb_path=":memory:")


def test_unknown_name_error_message_lists_valid_names():
    with pytest.raises(KeyError) as exc_info:
        SimilarityEngineRegistry.get_spec("nope")
    # The message should mention a known engine to aid debugging.
    assert "batter" in str(exc_info.value)
