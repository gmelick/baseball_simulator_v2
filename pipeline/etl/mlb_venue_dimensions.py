"""
pipeline/etl/mlb_venue_dimensions.py
====================================
SIM-478 (the fence certification plan,
``docs/audit/2026-09-20-sim478-480-fence-certification-plan.md``, §4 and §5.1):
the MLB Stats API's PUBLISHED fence distances, the floor under a new park's
fence line.

The venues call (``VENUES_URL``, ``hydrate=fieldInfo``) answers with every
venue of the season and its ``fieldInfo``: the five published distances
(``leftLine``, ``leftCenter``, ``center``, ``rightCenter``, ``rightLine``),
the two off-alley points (``left``, ``right``) some parks publish, the roof
and the turf. The geometry builder (``pipeline/batch/engine_artifacts.py``,
``build_park_geometry``) interpolates the five distances at a sector's
midpoint angle and adds the league's offset to build the PRIOR line of a
sector the pool's own balls cannot support.

The bundle keeps the last answer in ``<out_dir>/venue_dimensions.json``
(``{"fetched": {season: "ok" | "failed"}, "venues": {venue_id: {...}}}``).
When the API is down the builder keeps that copy and logs a warning; with no
copy and no answer the prior is simply absent and the league line stands.

The functions:

* :func:`fetch_venue_dimensions` — one season's answer, over ``urllib``;
* :func:`write_venue_dimensions` — every season of the window, merged over the
  existing copy, written to the bundle;
* :func:`read_venue_dimensions` — the copy, ``{}`` when there is none.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger("mlb_venue_dimensions")

#: The Stats API venues call the design session used (2026-09-20).
VENUES_URL = "https://statsapi.mlb.com/api/v1/venues?sportId=1&season={season}&hydrate=fieldInfo"
VENUE_DIMENSIONS_FILE = "venue_dimensions.json"

#: The five published distances the prior interpolates (feet), in field order.
PUBLISHED_DISTANCES = ("leftLine", "leftCenter", "center", "rightCenter", "rightLine")
#: The other fields kept when the API carries them.
_EXTRA_FIELDS = ("left", "right", "roofType", "turfType")


def _as_feet(value: Any) -> float | None:
    """A published distance as a float, ``None`` when absent or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return None
    return feet if feet > 0 else None


def _venue_record(raw: dict) -> dict:
    """One venue's record: the five distances (``None`` when missing), plus
    the name, the off-alley points and the roof / turf when present."""
    info = raw.get("fieldInfo") or {}
    rec: dict[str, Any] = {"name": raw.get("name")}
    for key in PUBLISHED_DISTANCES:
        rec[key] = _as_feet(info.get(key))
    for key in ("left", "right"):
        feet = _as_feet(info.get(key))
        if feet is not None:
            rec[key] = feet
    for key in ("roofType", "turfType"):
        if info.get(key) is not None:
            rec[key] = info[key]
    return rec


def fetch_venue_dimensions(season: int, timeout: float = 30) -> dict[int, dict]:
    """The published dimensions of every venue of ``season``, keyed by venue
    id. Raises ``urllib.error.URLError`` (or ``OSError`` / ``ValueError``)
    when the call fails or the body is not the venues shape, and
    ``http.client.HTTPException`` (not an ``OSError``: ``IncompleteRead`` on
    a truncated body, ``BadStatusLine`` on a malformed status) when the
    answer breaks mid-stream."""
    url = VENUES_URL.format(season=int(season))
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-sim/SIM-478"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        body = json.loads(resp.read().decode("utf-8"))
    venues = body.get("venues") if isinstance(body, dict) else None
    if not isinstance(venues, list):
        raise ValueError(f"venues call for {season}: no 'venues' list in the answer")
    out: dict[int, dict] = {}
    for raw in venues:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        out[int(raw["id"])] = _venue_record(raw)
    return out


def read_venue_dimensions(out_dir: str) -> dict[int, dict]:
    """The bundle's copy of the published dimensions, keyed by venue id;
    ``{}`` when the file is absent or unreadable."""
    path = os.path.join(out_dir, VENUE_DIMENSIONS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("venue_dimensions: cannot read %s (%s); no published distances", path, exc)
        return {}
    venues = doc.get("venues") if isinstance(doc, dict) else None
    if not isinstance(venues, dict):
        return {}
    return {int(k): dict(v) for k, v in venues.items() if isinstance(v, dict)}


def write_venue_dimensions(
    out_dir: str,
    seasons: list[int],
    fetch: Callable[[int], dict[int, dict]] | None = None,
) -> dict[int, dict]:
    """Fetch every season of the window (a venue's latest season wins), merge
    the answers over the bundle's existing copy, write the copy and return
    the venues dict. A failed season keeps the last copy for its venues and
    logs a warning; no copy and no answer gives ``{}`` and a warning."""
    fetch_fn = fetch if fetch is not None else fetch_venue_dimensions
    venues: dict[int, dict] = read_venue_dimensions(out_dir)
    had_copy = bool(venues)
    fetched: dict[str, str] = {}
    for season in sorted(int(s) for s in seasons):
        try:
            answer = fetch_fn(season)
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
            TimeoutError,
        ) as exc:
            log.warning("venue_dimensions: the venues call for %d failed (%s)", season, exc)
            fetched[str(season)] = "failed"
            continue
        fetched[str(season)] = "ok"
        venues.update(answer)
    if not any(v == "ok" for v in fetched.values()):
        if had_copy:
            log.warning("venue_dimensions: every season failed; keeping the last copy")
        else:
            log.warning("venue_dimensions: every season failed and no copy exists; no prior")
    os.makedirs(out_dir, exist_ok=True)
    doc = {"fetched": fetched, "venues": {str(k): v for k, v in sorted(venues.items())}}
    with open(os.path.join(out_dir, VENUE_DIMENSIONS_FILE), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return venues
