"""Parse a BettingPros offer: the sides a market needs, one book's line and the opener.

The module also reads the vendor's stamp and builds the player-name key.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from typing import (
    Any,
    TypeVar,
)

from pipeline.bettingpros.markets import TWIN_PRICE_TOLERANCE
from pipeline.odds_row_guard import implied_probability

log = logging.getLogger("pipeline.bettingpros")


_K = TypeVar("_K")
_V = TypeVar("_V")


#: The sides a row of each market kind must price.
#: A market that lacks a required side gets no row for any book.
_REQUIRED_SIDES: dict[str, tuple[str, ...]] = {
    "moneyline": ("home", "away"),
    "three_way": ("home", "away"),
    "runline": ("home", "away"),
    "total": ("over", "under"),
    "yes_no": ("yes", "no"),
    "prop": ("over", "under"),
}
#: The sides a row prices only when the book quotes them: a three-way market's tie.
_OPTIONAL_SIDES: dict[str, tuple[str, ...]] = {"three_way": ("draw",)}

#: A side's price, line and vendor stamp, read from one book's line.
_Quote = tuple[float | None, float | None, Any]


class _FirstInningReadError(RuntimeError):
    """Signal that the first-inning read failed, so the twin check cannot run.

    The first-five market then gets no rows.
    Without the check, a first-five row may be a first-inning bet under the wrong label.
    """


def _usable(ln: Mapping[str, Any]) -> bool:
    """Return True when a line is a price the book takes.

    A line flagged ``is_off`` or inactive, or a line without a price, is not a quote.
    """
    return not ln.get("is_off") and ln.get("active") is not False and ln.get("cost") is not None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _book_line(sel: Mapping[str, Any], book_id: int) -> dict[str, Any] | None:
    """Return the book's usable line on one selection, or ``None``.

    The feed normally lists one line per book per selection.
    When a book lists more than one usable line, the ``main`` line wins, else the first.
    """
    usable = [
        ln
        for book in sel.get("books") or []
        if _opt_int(book.get("id")) == book_id
        for ln in book.get("lines") or []
        if _usable(ln)
    ]
    if not usable:
        return None
    if len(usable) > 1:
        log.debug(
            "BettingPros: book %s lists %d usable lines on one selection; taking the main one",
            book_id,
            len(usable),
        )
        for ln in usable:
            if ln.get("main"):
                return ln
    return usable[0]


def _shared_opener(sides: Mapping[str, Mapping[str, Any]]) -> int | None:
    """Return the one book that opened every side, or ``None``.

    The rule matches the opening row's rule.
    Every side's ``opening_line`` must name a ``book_id`` and a ``cost``.
    All sides must name the same book.
    """
    openers: set[int] = set()
    for sel in sides.values():
        ol = sel.get("opening_line") or {}
        opener_id = _opt_int(ol.get("book_id"))
        if opener_id is None or _opt_float(ol.get("cost")) is None:
            return None
        openers.add(opener_id)
    return openers.pop() if len(openers) == 1 else None


def _stamp(value: Any) -> datetime | None:
    """Return a vendor stamp as a timezone-aware UTC datetime, or ``None``.

    The vendor writes UTC text with no zone, e.g. ``"2026-09-10 16:15:50"``.
    """
    if not value:
        return None
    parsed = _parse_utc(str(value))
    return None if parsed is None else parsed.replace(tzinfo=UTC)


def _books_on(sides: Mapping[str, Mapping[str, Any]]) -> list[int]:
    """Return every book id on the given sides, in the order the books first appear."""
    seen: list[int] = []
    for sel in sides.values():
        for book in sel.get("books") or []:
            book_id = _opt_int(book.get("id"))
            if book_id is not None and book_id not in seen:
                seen.append(book_id)
    return seen


def _same_line(
    a: Mapping[str, Any], b: Mapping[str, Any], tolerance: float = TWIN_PRICE_TOLERANCE
) -> bool:
    """Return True when two lines have the same line and prices within ``tolerance``.

    ``tolerance`` is a gap in implied probability; ``0.0`` asks for identical prices.
    """
    cost_a, cost_b = _opt_float(a.get("cost")), _opt_float(b.get("cost"))
    if cost_a is None or cost_b is None:
        return False
    if _opt_float(a.get("line")) != _opt_float(b.get("line")):
        return False
    return abs(implied_probability(cost_a) - implied_probability(cost_b)) <= tolerance


def _tie_rules_out_twin(
    f5_tie: Mapping[str, Any] | None,
    f1_tie: Mapping[str, Any] | None,
    tolerance: float = TWIN_PRICE_TOLERANCE,
) -> bool:
    """Return True when the ties show that a first-five entry is not a first-inning copy.

    A first-five tie that the first-inning entry lacks rules out a copy.
    A first-five tie beyond ``tolerance`` of the first-inning tie also rules out a copy.
    A first-five entry with no tie decides nothing; the team sides decide.
    On a near-even game the team sides match within the tolerance, so the tie must decide.
    """
    if f5_tie is None:
        return False
    if f1_tie is None:
        return True
    return not _same_line(f5_tie, f1_tie, tolerance)


def _parse_utc(value: str) -> datetime | None:
    """Parse a UTC timestamp from either source as a naive datetime, or return ``None``.

    The MLB schedule writes ``"2026-09-08T22:35:00Z"``.
    BettingPros writes ``"2026-09-08 22:35:00"``, with no zone, but also in UTC.
    So the naive datetimes from the two sources compare directly.
    """
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _normalize_name(name: str) -> str:
    """Return the name key: lower case, no accents or punctuation, single spaces."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in ascii_only)
    return " ".join(kept.lower().split())


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
