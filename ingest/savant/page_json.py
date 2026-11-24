"""Parse the JSON literal that Baseball Savant embeds in an HTML page."""

from __future__ import annotations

import json
from typing import Any


class SavantScrapeError(RuntimeError):
    """Savant returned a page this parser cannot read.

    Some Savant data, such as the venue dimensions, exists only as an inline
    ``var data = …`` literal in an HTML page. The parse fails loudly. After a
    change to the page, a blind slice raises an opaque JSONDecodeError or,
    worse, parses a DIFFERENT object and writes wrong stadium dimensions.
    """


def _parse_savant_embedded_json(html: str, start_marker: str, end_marker: str, end_pad: int) -> Any:
    """Return the parsed JSON literal between ``start_marker`` and ``end_marker``.

    ``start_marker`` ends with the literal's opening bracket or brace.
    ``end_pad`` is the number of characters of ``end_marker`` that belong to
    the literal. Raises :class:`SavantScrapeError` when a marker is missing or
    the slice is not valid JSON.
    """
    start = html.find(start_marker)
    if start == -1:
        raise SavantScrapeError(
            f"start marker {start_marker!r} not found in the Savant response "
            f"({len(html)} bytes) — the page shape has changed"
        )
    payload_start = start + len(start_marker) - 1  # keep the opening bracket or brace
    end = html.find(end_marker, payload_start)
    if end == -1:
        raise SavantScrapeError(
            f"end marker {end_marker!r} not found after offset {payload_start} — "
            "the page shape has changed"
        )
    try:
        return json.loads(html[payload_start : end + end_pad])
    except json.JSONDecodeError as exc:
        raise SavantScrapeError(
            f"embedded Savant payload between {start_marker!r} and {end_marker!r} "
            f"is not valid JSON: {exc}"
        ) from exc
