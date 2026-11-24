"""Fetch each park's published fence distances from the MLB Stats API for the fence prior.

The venues call (``VENUES_URL``, ``hydrate=fieldInfo``) returns every venue of a
season with its ``fieldInfo``: the five published distances (``leftLine``,
``leftCenter``, ``center``, ``rightCenter``, ``rightLine``), the two off-alley
points (``left``, ``right``) some parks publish, the roof and the turf.

The geometry builder (``build_park_geometry`` in
``pipeline/batch/artifacts/park_geometry.py``) interpolates the five distances
at a sector's middle angle and adds the league's offset. The result is the
prior: the fence line of a sector that the pool's own balls cannot support.

The bundle keeps the last answer in ``<out_dir>/venue_dimensions.json``
(``{"fetched": {season: "ok" | "failed"}, "venues": {venue_id: {...}}}``). When
the API is down, the builder keeps that copy and logs a warning. With no copy
and no answer, the prior is absent and the league line stands.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from ingest.mlb import client as mlb_client

log = logging.getLogger(__name__)

#: The Stats API venues call, with each venue's field dimensions.
VENUES_URL = mlb_client.VENUES_URL + "?sportId=1&season={season}&hydrate=fieldInfo"
#: The bundle file that keeps the last answer.
VENUE_DIMENSIONS_FILE = "venue_dimensions.json"

#: The five published distances the prior interpolates (feet), in field order.
PUBLISHED_DISTANCES = ("leftLine", "leftCenter", "center", "rightCenter", "rightLine")


def _as_feet(value: Any) -> float | None:
    """Return a published distance in feet; ``None`` when absent, not a number or not positive."""
    if value is None or isinstance(value, bool):
        return None
    try:
        feet = float(value)
    except (TypeError, ValueError):
        return None
    return feet if feet > 0 else None


def _venue_record(raw: dict) -> dict:
    """Return one venue's record.

    The record holds the name and the five distances (``None`` when missing),
    plus the off-alley points, the roof and the turf when present.
    """
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
    """Return the published dimensions of every venue of ``season``, keyed by venue id.

    Raises ``urllib.error.URLError``, ``OSError`` or ``ValueError`` when the
    call fails or the body is not the venues shape. Raises
    ``http.client.HTTPException`` when the answer breaks mid-stream: a
    truncated body or a malformed status line. That exception is not an
    ``OSError``, so a caller must catch it by name.
    """
    body = mlb_client.get_json(
        VENUES_URL.format(season=int(season)),
        headers={"User-Agent": "baseball-sim/SIM-478"},
        timeout_s=timeout,
        policy=mlb_client.DEFAULT_POLICY,
        logger=log,
        label="The venue-dimension load",
    )
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
    """Return the bundle's copy, keyed by venue id; ``{}`` when the file is absent or unreadable."""
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
    """Fetch every season of the window into the bundle's copy. Return the venues.

    The answers merge over the existing copy, and a venue's latest season wins.
    A failed season keeps the last copy of its venues and logs a warning. With
    no copy and no answer, the result is ``{}`` and a warning.
    """
    fetch_fn = fetch if fetch is not None else fetch_venue_dimensions
    venues: dict[int, dict] = read_venue_dimensions(out_dir)
    had_copy = bool(venues)
    fetched: dict[str, str] = {}
    for season in sorted(int(s) for s in seasons):
        try:
            answer = fetch_fn(season)
        except (OSError, http.client.HTTPException, ValueError) as exc:
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
