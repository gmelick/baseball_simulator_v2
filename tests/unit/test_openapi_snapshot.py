"""
tests/unit/test_openapi_snapshot.py
==========================================
The frontend's API snapshot matches the API (SIM-573).

``frontend/openapi.json`` is a copy of the API's contract, its OpenAPI schema.
``scripts/export_openapi.py`` writes the copy from ``app.openapi()``.
openapi-typescript (``npm run gen:api``) turns the copy into
``frontend/src/api/schema.d.ts``, the frontend's typed API client. Nothing
checked that anyone ran the export. From 2026-10-09 to 2026-10-19 five tickets
changed the contract without one. The copy lacked six routes, one method,
fourteen schemas, fourteen fields and three query parameters.

These tests pin the rule:

* the copy equals the app's schema, apart from one key whose output depends on
  the Pydantic version (below);
* the generated types name every route of the copy.

The CI frontend job regenerates ``schema.d.ts`` from the copy and fails on any
difference, so the types cannot drift from the copy either.

**The one normalised key.** A free-form object (a ``dict[str, Any]`` field or
response) carries ``"additionalProperties": true`` under Pydantic 2.11 and
later; Pydantic 2.10 leaves the key out. JSON Schema reads the two the same
way: an absent ``additionalProperties`` allows every extra property. CI
installs the newest Pydantic that ``requirements.txt`` allows, and the image
holds the one of its build day. So the comparison drops that key where its
value is ``true``, and compares everything else exactly. The export itself
must run in the app image: the host's Pydantic 2.10 drops the key, and
openapi-typescript then types those objects as ``Record<string, never>``.

No test needs a database, the network or the app's lifespan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import fastapi
import pydantic

from api.main import app

_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _ROOT / "frontend" / "openapi.json"
_TYPES = _ROOT / "frontend" / "src" / "api" / "schema.d.ts"

_FIX = (
    "Run the export in the app image, then regenerate the types:\n"
    "  docker compose run --rm -v <checkout>:/app app python scripts/export_openapi.py\n"
    "  cd frontend && npm run gen:api\n"
    "and commit frontend/openapi.json with frontend/src/api/schema.d.ts."
)


def _normalise(node: Any) -> Any:
    """Return ``node`` with every ``"additionalProperties": true`` removed.

    The value ``true`` means the same as an absent key, so the result keeps the
    contract. A field named ``additionalProperties`` holds a schema object,
    never ``true``, so no field is lost.
    """
    if isinstance(node, dict):
        return {
            key: _normalise(value)
            for key, value in node.items()
            if not (key == "additionalProperties" and value is True)
        }
    if isinstance(node, list):
        return [_normalise(value) for value in node]
    return node


def _app_schema() -> dict[str, Any]:
    """Return the app's schema as the export writes it: through JSON."""
    return json.loads(json.dumps(app.openapi(), sort_keys=True))


def _snapshot() -> dict[str, Any]:
    """Return the committed copy."""
    return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))


def _changed(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Return the keys that one side lacks or that differ, in order."""
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def _drift_report(snapshot: dict[str, Any], live: dict[str, Any]) -> str:
    """Return a short account of how the copy differs from the app."""
    lines = [f"Pydantic {pydantic.VERSION}, FastAPI {fastapi.__version__}."]
    paths = _changed(snapshot.get("paths", {}), live.get("paths", {}))
    if paths:
        lines.append(f"Routes that differ ({len(paths)}): {paths[:12]}")
    old_schemas = snapshot.get("components", {}).get("schemas", {})
    new_schemas = live.get("components", {}).get("schemas", {})
    schemas = _changed(old_schemas, new_schemas)
    if schemas:
        lines.append(f"Schemas that differ ({len(schemas)}): {schemas[:12]}")
    rest = _changed(
        {k: v for k, v in snapshot.items() if k not in ("paths", "components")},
        {k: v for k, v in live.items() if k not in ("paths", "components")},
    )
    if rest:
        lines.append(f"Top-level keys that differ: {rest}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. The normalisation
# ---------------------------------------------------------------------------


def test_the_normalisation_drops_only_a_true_additional_properties() -> None:
    """A ``true`` value goes; ``false``, a schema value and a field of that name stay."""
    node = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "extra": {"type": "object", "additionalProperties": True},
            "strict": {"type": "object", "additionalProperties": False},
            "typed": {"type": "object", "additionalProperties": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        },
        "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
    }
    assert _normalise(node) == {
        "type": "object",
        "properties": {
            "extra": {"type": "object"},
            "strict": {"type": "object", "additionalProperties": False},
            "typed": {"type": "object", "additionalProperties": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        },
        "anyOf": [{"type": "object"}, {"type": "null"}],
    }


# ---------------------------------------------------------------------------
# 2. The copy against the app
# ---------------------------------------------------------------------------


def test_the_snapshot_matches_the_app_schema() -> None:
    """The committed copy equals the app's schema, apart from the one key.

    A failure lists the routes and the schemas that differ, and the commands
    that fix it.
    """
    snapshot = _normalise(_snapshot())
    live = _normalise(_app_schema())
    assert snapshot == live, (
        f"frontend/openapi.json does not match the API.\n{_drift_report(snapshot, live)}\n{_FIX}"
    )


def test_the_snapshot_holds_the_open_type_marker_of_the_image() -> None:
    """The copy carries ``additionalProperties: true`` on the free-form objects.

    An export on the host's Pydantic 2.10 drops the key, and the generated
    types then read ``Record<string, never>``. The Data Lab's by-season answer
    is one such object.
    """
    route = _snapshot()["paths"]["/api/analytics/by-season"]["get"]
    schema = route["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["items"] == {"additionalProperties": True, "type": "object"}


# ---------------------------------------------------------------------------
# 3. The generated types against the copy
# ---------------------------------------------------------------------------


def test_the_generated_types_name_every_route_of_the_snapshot() -> None:
    """``schema.d.ts`` holds a key for each route of the copy, and no other.

    openapi-typescript writes each route as a quoted key, such as
    ``"/api/analytics/zone": {``. A route in the copy with no key means that
    nobody ran ``npm run gen:api`` after the export.
    """
    routes = set(_snapshot()["paths"])
    keys = set(re.findall(r'^    "(/[^"]*)": \{$', _TYPES.read_text(encoding="utf-8"), re.M))
    assert routes - keys == set(), (
        f"schema.d.ts lacks these routes; run npm run gen:api: {sorted(routes - keys)}"
    )
    assert keys - routes == set(), (
        f"schema.d.ts names routes the snapshot lacks: {sorted(keys - routes)}"
    )
