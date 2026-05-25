"""
api/serialization.py
====================
SIM-350 -- the **array-safe JSON helper** under the Phase-5 API response
contract (Phase 5, Sprint 1).

WHAT THIS IS
------------
The Phase-4 simulation output dataclasses
(:mod:`simulation.results` / :mod:`simulation.win_probability` /
:mod:`simulation.snapshots` / :mod:`simulation.prop_distributions` /
:mod:`betting.clv_engine`) hold **numpy arrays and numpy scalars**
(``NDArray[np.int64]`` score arrays, prop PMF ``support`` / ``probabilities``,
etc.).  ``json.dumps`` cannot serialise those, and the only pre-SIM-350 JSON
path stringified them with ``default=str`` -- which turns an array into an
unparseable ``"[1 2 3]"`` repr (a numpy str, NOT JSON).

This module is the single, small, reusable building block the SIM-350 Pydantic
converters in :mod:`api.schemas` lean on: :func:`to_jsonable` recursively walks
an arbitrary object graph (numpy arrays / numpy scalars / dataclasses / dicts /
sequences / datetimes / enums) and returns an equivalent structure built
ENTIRELY from JSON-native Python types (``int`` / ``float`` / ``str`` / ``bool``
/ ``None`` / ``list`` / ``dict``).  The result is guaranteed to survive
``json.dumps`` with NO custom ``default=`` hook -- i.e. no numpy ever leaks into
the wire format.

DESIGN
------
  * **Pure + dependency-light.**  numpy + stdlib only; no Pydantic, no DB, no
    FAISS, no RNG.  This keeps the helper usable both inside the Pydantic
    converters and in any plain-``json`` code path.
  * **Lossless for the contract's value types.**  numpy ints -> ``int``, numpy
    floats -> ``float``, numpy bools -> ``bool``, arrays -> nested ``list``s,
    datetimes -> ISO-8601 strings, enums -> their ``.value``.  We do NOT round,
    truncate, or drop any element -- the contract layer must never silently lose
    data (a consumer that wants a summary-only view trims at the endpoint, not
    here).
  * **Recursion-safe shapes.**  dataclass instances are expanded field-by-field
    (so a nested ``ConfidenceInterval`` inside a ``GameSimSummary`` is handled),
    and mapping keys are coerced to ``str`` (JSON object keys must be strings).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
from typing import Any

import numpy as np


def to_jsonable(obj: Any) -> Any:
    """Recursively convert ``obj`` to a structure of JSON-native Python types.

    Handles, in order:
      * ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` -> returned as-is
        (Python natives are already JSON-safe).
      * numpy scalars (``np.integer`` / ``np.floating`` / ``np.bool_``) ->
        their Python equivalents (``int`` / ``float`` / ``bool``).
      * numpy arrays (``np.ndarray``) -> a (possibly nested) ``list`` whose
        leaves are JSON-native (via ``.tolist()`` then a defensive re-walk).
      * :class:`datetime.datetime` / ``date`` / ``time`` -> ISO-8601 ``str``.
      * :class:`enum.Enum` -> its ``.value`` (recursively made jsonable).
      * dataclass INSTANCES -> a ``dict`` of ``{field_name: to_jsonable(value)}``
        (NOT ``dataclasses.asdict`` -- that would choke on the numpy arrays
        before we get to convert them).
      * mappings -> a ``dict`` with ``str`` keys and jsonable values.
      * other iterables (list / tuple / set) -> a ``list`` of jsonable items.

    Anything else falls through to ``str(obj)`` as a last resort so the helper
    never raises mid-serialisation; the contract types above are all covered
    explicitly, so that branch is a safety net, not a normal path.

    The return value is guaranteed to be accepted by ``json.dumps`` with no
    custom ``default=`` -- no numpy object survives.
    """
    # --- JSON-native scalars (fast path) --------------------------------------
    # NB: ``np.float64`` subclasses Python ``float`` (and ``np.str_`` subclasses
    # ``str``), so we must EXCLUDE numpy scalars here or they would be returned
    # unconverted and leak into the wire format.  They fall through to the numpy
    # branch below.
    if obj is None or (
        isinstance(obj, (bool, int, float, str)) and not isinstance(obj, np.generic)
    ):
        # bool is a subclass of int; handled here so True/False stay JSON bools.
        return obj

    # --- numpy scalars --------------------------------------------------------
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    # numpy 0-d / generic scalar fallback (covers np.str_, etc.).
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())

    # --- numpy arrays ---------------------------------------------------------
    if isinstance(obj, np.ndarray):
        # .tolist() already yields Python scalars for numeric dtypes; re-walk so
        # object-dtype arrays (which can hold numpy scalars / dataclasses) are
        # also fully converted.
        return [to_jsonable(x) for x in obj.tolist()]

    # --- datetimes ------------------------------------------------------------
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()

    # --- enums ----------------------------------------------------------------
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)

    # --- dataclass instances (not classes) ------------------------------------
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}

    # --- mappings -------------------------------------------------------------
    if isinstance(obj, dict):
        return {str(_jsonable_key(k)): to_jsonable(v) for k, v in obj.items()}

    # --- other iterables (list / tuple / set / frozenset) ---------------------
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(x) for x in obj]

    # --- last resort ----------------------------------------------------------
    return str(obj)


def _jsonable_key(key: Any) -> Any:
    """Coerce a mapping key to a JSON-object-key-safe value.

    JSON object keys must be strings; numpy / enum keys are unwrapped to their
    Python scalar first so e.g. an ``np.int64`` player-id key becomes ``"7"``
    rather than ``"np.int64(7)"``.
    """
    if isinstance(key, (np.integer,)):
        return int(key)
    if isinstance(key, (np.floating,)):
        return float(key)
    if isinstance(key, np.generic):
        return key.item()
    if isinstance(key, enum.Enum):
        return key.value
    return key


__all__ = ["to_jsonable"]
