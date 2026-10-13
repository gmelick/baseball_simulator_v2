"""simulation/cache_codec.py -- the JSON codec of the Redis sim cache (SIM-443f).

The Redis cache used to pickle its values. A pickle load runs code, so anyone
who can write to Redis could run code in the API. This codec stores JSON only.

Every stored value is an envelope: ``{"t": <tag>, "v": <payload>}``.

* ``summary`` -- a :class:`GameSimSummary`, written by its ``to_json``.
* ``prop_set`` -- a :class:`PropDistributionSet`, written by its ``to_json``.
* ``json`` -- any plain JSON value (a dict, a list, a string, a number).

``encode`` raises ``TypeError`` on a value JSON cannot hold; the cache then
skips the write. ``decode`` never raises: any text it cannot read (an old
pickle, a truncated value, a wrong tag) is a miss, and the caller recomputes.
"""

from __future__ import annotations

import json
from typing import Any

from simulation.prop_distributions import PropDistributionSet
from simulation.results import GameSimSummary

TAG_SUMMARY = "summary"
TAG_PROP_SET = "prop_set"
TAG_JSON = "json"

_ENVELOPE_KEYS = frozenset({"t", "v"})


def encode(value: Any) -> str:
    """Write ``value`` as the JSON text of a typed envelope.

    The encoder has no ``default=`` hook: a value JSON cannot hold raises
    ``TypeError``.
    """
    if isinstance(value, GameSimSummary):
        envelope: dict[str, Any] = {"t": TAG_SUMMARY, "v": value.to_json()}
    elif isinstance(value, PropDistributionSet):
        envelope = {"t": TAG_PROP_SET, "v": value.to_json()}
    else:
        envelope = {"t": TAG_JSON, "v": value}
    return json.dumps(envelope, separators=(",", ":"))


def decode(raw: str | bytes | bytearray | None) -> Any | None:
    """Read one stored value, or None when the text is not a valid envelope."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != _ENVELOPE_KEYS:
            return None
        tag, payload = data["t"], data["v"]
        if tag == TAG_SUMMARY:
            if not isinstance(payload, dict):
                return None
            return GameSimSummary.from_json(payload)
        if tag == TAG_PROP_SET:
            if not isinstance(payload, dict):
                return None
            return PropDistributionSet.from_json(payload)
        if tag == TAG_JSON:
            return payload
        return None
    except Exception:  # noqa: BLE001 -- any unreadable value is a cache miss
        return None


__all__ = ["TAG_JSON", "TAG_PROP_SET", "TAG_SUMMARY", "decode", "encode"]
