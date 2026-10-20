"""Load a script from ``scripts/`` as a module, for a test.

``scripts/`` is not a package, so a test imports a script by its file path. A
general tool lives in ``scripts/``; a one-off probe, rebuild or backfill lives in
``scripts/probes/``. :func:`load_script` looks in both, puts the repository root
and ``scripts/`` on ``sys.path`` (a script imports its siblings, such as
``sim_stats``, by name), and registers the module in ``sys.modules`` before it
runs, so a slots dataclass in the script can resolve its own module.

``scripts/`` is baked into the app image and is not bind-mounted, so a container
run without the checkout mounted over ``/app`` reads an old copy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
PROBES = SCRIPTS / "probes"


def script_path(name: str) -> Path:
    """The file of script ``name`` (no extension), in ``scripts/`` or ``scripts/probes/``."""
    for folder in (SCRIPTS, PROBES):
        path = folder / f"{name}.py"
        if path.is_file():
            return path
    raise FileNotFoundError(f"no script {name!r} in {SCRIPTS} or {PROBES}")


def load_script(name: str, *, module_name: str | None = None) -> ModuleType:
    """Import script ``name`` and register it as ``module_name`` (default: ``name``)."""
    for entry in (str(REPO), str(SCRIPTS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    path = script_path(name)
    spec = importlib.util.spec_from_file_location(module_name or name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
