"""
tests/unit/test_similarity_explorer_adapters.py
===============================================
SIM-439 — the generalized Similarity Explorer adapts 8 engines that each return
a DIFFERENT ``SimilarityResult`` dataclass. A wrong field name in an adapter is a
runtime 500 (``getattr`` on a missing attribute), so this test pins every
adapter's ``id_field`` / ``sample_field`` / sub-score fields / extra fields to the
REAL dataclass fields, and checks the position-/vs_hand-aware relabeling.
"""

from __future__ import annotations

import dataclasses

import pytest

from api.routes.similarity_explorer import (
    SCORE_ADAPTERS,
    EngineAdapter,
    _subscores_meta,
)

# (engine name, "module:ClassName") — the real result dataclass per engine.
_RESULT_CLASSES = {
    "pitcher": "similarity.engines.pitcher_similarity:SimilarityResult",
    "batter": "similarity.engines.batter_similarity:SimilarityResult",
    "fielder": "similarity.engines.fielder_similarity:SimilarityResult",
    "catcher": "similarity.engines.catcher_similarity:SimilarityResult",
    "baserunner": "similarity.engines.baserunner_similarity:SimilarityResult",
    "baserunner_steal": "similarity.engines.baserunner_steal_similarity:SimilarityResult",
    "pitcher_steal": "similarity.engines.pitcher_steal_similarity:SimilarityResult",
    "manager": "similarity.engines.manager_similarity:SimilarityResult",
}


def _load_fields(spec: str) -> set[str]:
    pytest.importorskip("duckdb")  # engines import duckdb/numpy/scipy
    import importlib

    mod_name, cls_name = spec.split(":")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    return {f.name for f in dataclasses.fields(cls)}


def test_registry_covers_the_eight_score_engines() -> None:
    assert set(SCORE_ADAPTERS) == set(_RESULT_CLASSES)


@pytest.mark.parametrize("name", sorted(_RESULT_CLASSES))
def test_adapter_fields_exist_on_result(name: str) -> None:
    adapter: EngineAdapter = SCORE_ADAPTERS[name]
    fields = _load_fields(_RESULT_CLASSES[name])

    assert adapter.id_field in fields, f"{name}.id_field {adapter.id_field!r} missing"
    assert adapter.sample_field in fields, f"{name}.sample_field {adapter.sample_field!r} missing"
    assert "score" in fields and "season" in fields

    for f, _label, _w in adapter.subscores:
        assert f in fields, f"{name} sub-score {f!r} not on the result dataclass"
    for f, _label in adapter.extra_fields:
        assert f in fields, f"{name} extra field {f!r} not on the result dataclass"


def test_fielder_labels_flip_infield_vs_outfield() -> None:
    adapter = SCORE_ADAPTERS["fielder"]
    inf = {m.label for m in _subscores_meta(adapter, "2B", None)}
    outf = {m.label for m in _subscores_meta(adapter, "CF", None)}
    assert "Double Play" in inf and "Specialty" in inf
    assert "Arm" in outf and "Star Plays" in outf
    # The result FIELDS are stable (only the labels change).
    inf_fields = [m.field for m in _subscores_meta(adapter, "2B", None)]
    assert inf_fields == ["range_score", "secondary_score", "tertiary_score", "quaternary_score"]


def test_batter_vs_hand_reweights_platoon() -> None:
    adapter = SCORE_ADAPTERS["batter"]
    default = {m.field: m.weight for m in _subscores_meta(adapter, None, None)}
    platoon = {m.field: m.weight for m in _subscores_meta(adapter, None, "R")}
    assert default["platoon_score"] == 0.15
    assert platoon["platoon_score"] == 0.35  # platoon becomes dominant in vs_hand mode
    assert platoon["discipline_score"] < default["discipline_score"]
