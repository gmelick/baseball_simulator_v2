"""Every ``SIM_*`` name the operational scripts use is a registered flag
(``simulation/flags.py``).

A script reads a flag through the registry's readers, or records its raw value
in a report's provenance (``accuracy.selection._flag_as_set``, the flag list of
``scripts/sim_stats.py``). Either way, a misspelt or retired name reads as unset
and nothing fails, so this test fails instead. The probes in ``scripts/probes/``
are one-off tools and stay out of the check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from simulation import flags

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
_FLAG_NAME = re.compile(r"^SIM_[A-Z0-9_]+$")


def _flag_names(path: Path) -> set[str]:
    """The ``SIM_*``-shaped strings in ``path``, less the names of its own globals."""
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


def _script_files() -> list[Path]:
    return [
        p for p in sorted(SCRIPTS.rglob("*.py")) if "probes" not in p.relative_to(SCRIPTS).parts
    ]


def test_the_scripts_name_only_registered_flags() -> None:
    unregistered: dict[str, list[str]] = {}
    for path in _script_files():
        for name in _flag_names(path):
            if not flags.is_registered(name):
                unregistered.setdefault(name, []).append(str(path.relative_to(REPO)))
    assert not unregistered, (
        "these SIM_* names in scripts/ are not registered flags; add each to "
        f"simulation/flags.py or fix the spelling: {unregistered}"
    )


def test_the_check_sees_the_scripts_flag_lists() -> None:
    """The check reads the two provenance lists, so an empty scan cannot pass."""
    names = _flag_names(SCRIPTS / "sim_stats.py") | _flag_names(
        SCRIPTS / "accuracy" / "selection.py"
    )
    assert {"SIM_MANAGER_DRAW", "SIM_PITCH_RESULT_SPLIT", "SIM_FATIGUE_PC_SIGMA"} <= names
