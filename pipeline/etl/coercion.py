"""
coercion.py
===========
MLB Baseball Simulation Platform

Shared "empty/missing -> None" type-coercion helpers for the ETL loaders.

Both the historical Statcast loader (`etl_historical_loader.py`) and the Savant
sprint-speed loader (`etl_sprint_speed_loader.py`) ingest external feeds (MLB
Stats API JSON, Baseball Savant CSV) whose numeric fields arrive as empty
strings, ``None``, or — from pandas-backed sources — ``NaN`` floats.  These
helpers coerce such values to a typed result or ``None`` so the row builders
never have to special-case missing data.

Semantics (single source of truth — previously duplicated per-loader, SIM-437):
  - ``""`` / ``None``   -> ``None``
  - ``NaN`` float       -> ``None``  (was a latent gap in the sprint loader's copy)
  - otherwise           -> the coerced value, or ``None`` if coercion fails

NOTE: the ``_opt_int`` (`pipeline/live/bullpen_availability_ingest.py`) and
``_opt_float`` (`pipeline/bettingpros_odds_provider.py`) helpers are a *different*
family — they do NOT treat ``""`` as missing — and are intentionally left
separate.  Folding them in here would be a behavior change, not a refactor.
"""

from __future__ import annotations

import math
from typing import Any


def to_float(v: Any) -> float | None:
    """Convert a feed value to float; return None for empty/missing/NaN."""
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    """Convert a feed value to int via to_float; return None if conversion fails."""
    f = to_float(v)
    return None if f is None else int(f)


def to_bool(v: Any) -> bool:
    """Convert various feed types to bool (str 'true'/'1'/'yes' -> True)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v) if v is not None else False


def to_str(v: Any) -> str | None:
    """Convert a feed value to a stripped string; return None if empty."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s if s else None
