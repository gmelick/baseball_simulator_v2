"""The parser for the JSON that Baseball Savant embeds in its HTML pages."""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# API helpers  (preserve original retry-forever behaviour, now with cap + backoff)
# ---------------------------------------------------------------------------


class SavantScrapeError(RuntimeError):
    """Baseball-Savant returned a page this loader cannot parse.

    The venue dimensions are scraped out of an inline ``var data = …`` literal in
    an HTML page — there is no API for them. SIM-441 made that parse fail LOUDLY:
    it was previously unguarded ``resp.text.find(...)`` slicing, so a page-shape
    change would have sliced a wrong substring and either raised an opaque
    JSONDecodeError deep in the call stack or, worse, parsed a *different*
    object and written plausible-but-wrong stadium dimensions.
    """


def _parse_savant_embedded_json(html: str, start_marker: str, end_marker: str, end_pad: int) -> Any:
    """Extract and parse the inline JSON literal a Savant leaderboard embeds.

    Raises :class:`SavantScrapeError` with the surrounding context when either
    marker is missing or the slice is not valid JSON, rather than letting a
    silent mis-slice through.
    """
    start = html.find(start_marker)
    if start == -1:
        raise SavantScrapeError(
            f"start marker {start_marker!r} not found in the Savant response "
            f"({len(html)} bytes) — the page shape has changed"
        )
    payload_start = start + len(start_marker) - 1  # keep the opening bracket/brace
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
