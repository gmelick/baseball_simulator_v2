"""
scripts/export_openapi.py
=========================
SIM-420 — export the FastAPI OpenAPI schema to ``frontend/openapi.json``.

The frontend's typed API client is generated FROM this snapshot
(``npm run gen:api`` → ``openapi-typescript``). Run this whenever the API
contract changes, then regenerate + commit the TS types:

    python scripts/export_openapi.py
    cd frontend && npm run gen:api

Imports the app and serializes ``app.openapi()`` — no server / DB needed
(``create_app`` only registers routes). Writes pretty-printed, stable-key JSON
so the committed snapshot diffs cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the repo root importable regardless of how the script is invoked
# (``python scripts/export_openapi.py`` puts scripts/ — not the root — on the path).
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
