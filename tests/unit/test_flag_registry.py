"""The ``SIM_*`` flag registry (``simulation/flags.py``).

Two guards keep the registry whole: every ``SIM_*`` name the API or the
simulator reads is registered, and every ``SIM_*`` key the app service sets in
``docker-compose.yml`` is registered. The tests check the readers on three
rules: the on and off spellings, the default for an unset flag, and the default
for a number that does not parse.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from simulation import flags

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("api", "simulation")
_FLAG_NAME = re.compile(r"^SIM_[A-Z0-9_]+$")


def _string_constants(path: Path) -> set[str]:
    """The ``SIM_*``-shaped strings in ``path``, less the names of its own globals.

    A module lists its constants by name in ``__all__`` (``"SIM_RESULT_TTL_S"``);
    such a string names a variable, not an environment flag.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    own_globals = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _FLAG_NAME.match(node.value)
        and node.value not in own_globals
    }


def test_every_flag_the_code_names_is_registered() -> None:
    unregistered: dict[str, list[str]] = {}
    for folder in SOURCE_DIRS:
        for path in sorted((REPO / folder).rglob("*.py")):
            if path.name == "flags.py" and path.parent.name == "simulation":
                continue
            for name in _string_constants(path):
                if not flags.is_registered(name):
                    unregistered.setdefault(name, []).append(str(path.relative_to(REPO)))
    assert not unregistered, (
        "these SIM_* names are read outside the registry; add each to "
        f"simulation/flags.py: {unregistered}"
    )


def test_every_flag_compose_sets_is_registered() -> None:
    compose = REPO / "docker-compose.yml"
    if not compose.is_file():
        pytest.skip("docker-compose.yml is not mounted in this container")
    doc = yaml.safe_load(compose.read_text(encoding="utf-8"))
    env = doc["services"]["app"].get("environment") or {}
    keys = env if isinstance(env, dict) else [item.split("=", 1)[0] for item in env]
    unknown = sorted(k for k in keys if k.startswith("SIM_") and not flags.is_registered(k))
    assert not unknown, f"compose sets unregistered SIM_* flags: {unknown}"


def test_every_row_is_complete() -> None:
    for flag in flags.FLAGS.values():
        assert _FLAG_NAME.match(flag.name), flag.name
        assert flag.kind in ("bool", "int", "float", "str"), flag.name
        assert flag.owner and "(SIM-" in flag.owner, flag.name
        assert flag.meaning.strip(), flag.name
        if flag.kind == "bool":
            assert isinstance(flag.default, bool), flag.name


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On"])
def test_on_spellings_read_on(raw: str) -> None:
    assert flags.read_bool("SIM_FENCE_STAGE", {"SIM_FENCE_STAGE": raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "No", " off "])
def test_off_spellings_read_off(raw: str) -> None:
    assert flags.read_bool("SIM_STEAL_PITCH_CLASS", {"SIM_STEAL_PITCH_CLASS": raw}) is False


def test_an_unset_flag_reads_its_default() -> None:
    assert flags.read_bool("SIM_STEAL_PITCH_CLASS", {}) is True
    assert flags.read_bool("SIM_MANAGER", {}) is False
    assert flags.read_float("SIM_SIT_SIGMA", {}) == 2.0
    assert flags.read_int("SIM_PITCH_MIN_CELL", {}) == 20
    assert flags.read_int("SIM_RUNNER_WORKERS", {}) is None
    assert flags.read_str("SIM_BULLPEN_SOURCE", {}) == "synthetic"


def test_a_value_neither_on_nor_off_is_refused() -> None:
    with pytest.raises(flags.FlagValueError, match="SIM_MANAGER"):
        flags.read_bool("SIM_MANAGER", {"SIM_MANAGER": "2"})


def test_a_number_that_does_not_parse_keeps_the_default() -> None:
    assert flags.read_float("SIM_BB_MARGIN_BAND", {"SIM_BB_MARGIN_BAND": "wide"}) == 0.0
    assert flags.read_int("SIM_CHANGE_MIN_CELL", {"SIM_CHANGE_MIN_CELL": "many"}) == 20
    assert flags.read_int("SIM_BB_MARGIN_MIN_ROWS", {"SIM_BB_MARGIN_MIN_ROWS": "25.0"}) == 25


def test_a_flag_read_as_the_wrong_kind_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        flags.read_float("SIM_FENCE_STAGE", {})
    with pytest.raises(KeyError):
        flags.read_bool("SIM_NOT_A_FLAG", {})


def test_the_actor_powers_keep_their_names() -> None:
    env = {
        "SIM_ACTOR_POWER_BATTER": "2",
        "SIM_ACTOR_POWER_FIELDER_SS": "3",
        "SIM_ACTOR_POWER_MANAGER_USAGE": "bad",
        "OTHER": "1",
    }
    assert flags.actor_power_overrides(env) == {"batter": 2.0, "fielder_SS": 3.0}


def test_the_registry_defaults_match_the_code_constants() -> None:
    from simulation.filter_cells import DEFAULT_MIN_CELL
    from simulation.production_factory import DEFAULT_SIT_SIGMA

    assert flags.FLAGS["SIM_PITCH_MIN_CELL"].default == DEFAULT_MIN_CELL
    assert flags.FLAGS["SIM_SIT_SIGMA"].default == DEFAULT_SIT_SIGMA
