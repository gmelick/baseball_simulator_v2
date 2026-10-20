"""
api/serialization.py
====================
The array-safe JSON helper of the API's response models.

The simulation and betting dataclasses (:mod:`simulation.results`,
:mod:`simulation.win_probability`, :mod:`simulation.snapshots`,
:mod:`simulation.prop_distributions`, :mod:`betting.clv_engine`) hold numpy
arrays and numpy scalars (``NDArray[np.int64]`` score arrays, a prop PMF's
``support`` and ``probabilities``). ``json.dumps`` cannot write those, and
``default=str`` turns an array into an unparseable ``"[1 2 3]"``.

:func:`to_jsonable` walks any object graph (numpy arrays and scalars,
dataclasses, dicts, sequences, datetimes, enums) and returns the same structure
built only of JSON-native types (``int``, ``float``, ``str``, ``bool``,
``None``, ``list``, ``dict``). The result survives ``json.dumps`` with no
``default=`` hook, so no numpy value reaches the wire. The converters in
:mod:`api.schemas` and the replay store use it.

DESIGN
------
  * **Pure and light.** numpy and the standard library only: no Pydantic, no
    database, no FAISS, no randomness, so the helper serves the Pydantic
    converters and any plain ``json`` path alike.
  * **Lossless for the contract's value types.** numpy ints -> ``int``, numpy
    floats -> ``float``, numpy bools -> ``bool``, arrays -> nested ``list``s,
    datetimes -> ISO-8601 strings, enums -> their ``.value``. Nothing is
    rounded, cut or dropped; a route that wants a summary trims it itself.
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
        isinstance(obj, bool | int | float | str) and not isinstance(obj, np.generic)
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
    if isinstance(obj, _dt.datetime | _dt.date | _dt.time):
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
    if isinstance(obj, list | tuple | set | frozenset):
        return [to_jsonable(x) for x in obj]

    # --- last resort ----------------------------------------------------------
    return str(obj)


def _jsonable_key(key: Any) -> Any:
    """Coerce a mapping key to a JSON-object-key-safe value.

    JSON object keys must be strings; numpy / enum keys are unwrapped to their
    Python scalar first so e.g. an ``np.int64`` player-id key becomes ``"7"``
    rather than ``"np.int64(7)"``.
    """
    if isinstance(key, np.integer):
        return int(key)
    if isinstance(key, np.floating):
        return float(key)
    if isinstance(key, np.generic):
        return key.item()
    if isinstance(key, enum.Enum):
        return key.value
    return key


__all__ = ["to_jsonable"]
