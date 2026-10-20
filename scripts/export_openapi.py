"""
scripts/export_openapi.py
=========================
Export the FastAPI OpenAPI schema to ``frontend/openapi.json``.

``npm run gen:api`` (``openapi-typescript``) generates the frontend's typed API
client FROM this snapshot. Run the export whenever the API contract changes.
Run it in the app image with the checkout mounted, never on the host: the
host's Pydantic 2.10 drops ``additionalProperties: true`` (the API snapshot,
SIM-573). Then regenerate the TypeScript types, and commit both files:

    docker compose run --rm -v <checkout>:/app app python scripts/export_openapi.py
    cd frontend && npm run gen:api

The script imports the app and serializes ``app.openapi()``. It needs no server
and no database: ``create_app`` only registers the routes. It writes indented
JSON with sorted keys, so the committed snapshot diffs cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Put the repo root on the path: ``python scripts/export_openapi.py`` puts
# scripts/ there, not the root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.main import app  # noqa: E402 — import after the sys.path shim above

OUT = _ROOT / "frontend" / "openapi.json"


def main() -> None:
    schema = app.openapi()
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    n_paths = len(schema.get("paths", {}))
    print(f"Wrote {OUT} ({n_paths} paths, OpenAPI {schema.get('openapi')})")


if __name__ == "__main__":
    main()
