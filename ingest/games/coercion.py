"""Convert feed values to typed values, with an empty or missing value as ``None``.

The MLB Stats API and the Baseball Savant files send a missing number as an
empty string, ``None`` or (through pandas) a ``NaN`` float. These helpers map
each one to ``None``, so the row builders need no special case:

  - ``""`` / ``None``  -> ``None``
  - ``NaN`` float      -> ``None``
  - any other value    -> the converted value, or ``None`` when the conversion fails

The ``_opt_int`` helper (``ingest/load_bullpen_availability.py``) and the
``_opt_float`` helper (``betting/bettingpros/fetch_odds.py``) do NOT treat ``""``
as missing. Do not fold them in here: that changes their behaviour.
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
    """Convert a feed value to an int through :func:`to_float`; return None when that fails."""
    f = to_float(v)
    return None if f is None else int(f)


def to_bool(v: Any) -> bool:
    """Convert a feed value to a bool: 'true', '1' and 'yes' are True, and None is False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v) if v is not None else False


def to_str(v: Any) -> str | None:
    """Convert a feed value to a stripped string; return None when it is empty."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s if s else None
