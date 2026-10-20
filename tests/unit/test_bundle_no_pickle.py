"""The engine-artifact bundle loads without a pickle (SIM-565).

A pickle can run code when numpy loads it. The bundle holds only plain
arrays: floats, integers and JSON text. So every ``np.load`` in the bundle
loader passes ``allow_pickle=False``, and the builder refuses to write an
object array, which would need a pickle. These tests hold both rules. A
static check reads the loader's source, so a new load that forgets the
keyword fails here before it reaches the app.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pytest

import pipeline.batch.engine_artifacts as ea
from pipeline.batch.artifacts.common import _savez_plain

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The bundle loader: ``EngineArtifacts.load`` and the per-artifact loaders it calls.
LOADER_FILES = (
    REPO_ROOT / "pipeline" / "batch" / "engine_artifacts.py",
    *sorted((REPO_ROOT / "pipeline" / "batch" / "artifacts").glob("*.py")),
)


def _numpy_load_names(tree: ast.AST) -> set[str]:
    """Return the bare names that ``from numpy import load`` binds."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "numpy":
            for alias in node.names:
                if alias.name == "load":
                    names.add(alias.asname or alias.name)
    return names


def _is_np_load(call: ast.Call, bare_names: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "load":
        return isinstance(func.value, ast.Name) and func.value.id in ("np", "numpy")
    return isinstance(func, ast.Name) and func.id in bare_names


def _np_load_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    bare = _numpy_load_names(tree)
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_np_load(n, bare)]


def _np_load_calls_without_false(source: str) -> list[int]:
    """Return the line of every numpy load that does not pass allow_pickle=False.

    A call with no keyword counts as a failure. The test does not rely on
    numpy's default.
    """
    bad: list[int] = []
    for call in _np_load_calls(source):
        ok = any(
            kw.arg == "allow_pickle"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in call.keywords
        )
        if not ok:
            bad.append(call.lineno)
    return sorted(bad)


def test_every_np_load_in_the_loader_refuses_pickle():
    calls = 0
    bad: list[str] = []
    for path in LOADER_FILES:
        source = path.read_text(encoding="utf-8")
        calls += len(_np_load_calls(source))
        bad += [f"{path.name}:{line}" for line in _np_load_calls_without_false(source)]
    assert calls >= 8, "the loader lost its np.load calls"
    assert bad == [], f"np.load without allow_pickle=False at {bad}"


def test_detector_can_fail():
    with_true = "import numpy as np\np = 'x'\nz = np.load(p, allow_pickle=True)\n"
    bare = "import numpy as np\np = 'x'\nz = np.load(p)\n"
    imported = "from numpy import load\np = 'x'\nz = load(p)\n"
    qualified = "import numpy\np = 'x'\nz = numpy.load(p)\n"
    good = "import numpy as np\np = 'x'\nz = np.load(p, allow_pickle=False)\n"
    assert _np_load_calls_without_false(with_true) == [3]
    assert _np_load_calls_without_false(bare) == [3]
    assert _np_load_calls_without_false(imported) == [3]
    assert _np_load_calls_without_false(qualified) == [3]
    assert _np_load_calls_without_false(good) == []


def test_loader_imports_no_pickle():
    names: set[str] = set()
    for path in LOADER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    assert "pickle" not in names
    assert "dill" not in names
    assert "cloudpickle" not in names


def test_savez_plain_refuses_an_object_array(tmp_path):
    bad = tmp_path / "bad.npz"
    with pytest.raises(ValueError, match="'vecs'"):
        _savez_plain(str(bad), vecs=np.array(["a", None], dtype=object))
    assert not bad.exists()

    good = tmp_path / "good.npz"
    vecs = np.arange(6, dtype=np.float32).reshape(2, 3)
    keys = json.dumps(["1:2024", "2:2024"])
    _savez_plain(str(good), vecs=vecs, keys=keys)
    with np.load(good, allow_pickle=False) as z:
        np.testing.assert_array_equal(z["vecs"], vecs)
        assert z["vecs"].dtype == np.float32
        assert json.loads(str(z["keys"])) == ["1:2024", "2:2024"]


def test_actor_sim_round_trip_without_pickle(tmp_path):
    index = {"1:2024": 0, "2:2024": 1}
    mat = np.array([[1.0, 0.25], [0.25, 1.0]], dtype=np.float32)
    ea._write_actor_sim(str(tmp_path), "batter", index, mat)
    with np.load(tmp_path / "batter.npz", allow_pickle=False) as z:
        assert json.loads(str(z["index"])) == index
        np.testing.assert_array_equal(z["matrix"], mat)
