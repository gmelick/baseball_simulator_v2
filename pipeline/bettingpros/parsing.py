"""The parsing of a BettingPros offer: the sides a market needs, one book's line,
the opener, the vendor's stamp and the player-name key.
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

from pipeline.bettingpros.rules import TWIN_PRICE_TOLERANCE
from pipeline.odds_row_guard import implied_probability

log = logging.getLogger("pipeline.bettingpros_odds_provider")


_K = TypeVar("_K")


_V = TypeVar("_V")


#: SIM-555: the sides a row of each market kind must price. A market missing a
#: required side in the payload gets no row for any book.
_REQUIRED_SIDES: dict[str, tuple[str, ...]] = {
    "moneyline": ("home", "away"),
    "three_way": ("home", "away"),
    "runline": ("home", "away"),
    "total": ("over", "under"),
    "yes_no": ("yes", "no"),
    "prop": ("over", "under"),
}


#: The sides a row prices when the book quotes them: the tie of a three-way market.
_OPTIONAL_SIDES: dict[str, tuple[str, ...]] = {"three_way": ("draw",)}


#: A side's price, line and vendor stamp, as read from one book's line.
_Quote = tuple[float | None, float | None, Any]


class _FirstInningReadError(RuntimeError):
    """SIM-555: the first-inning read the twin rule needs failed.

    The first-five market then gets no rows: without the check, a book's
    first-five entry may be a first-inning bet under the wrong label.
    """


def _usable(ln: Mapping[str, Any]) -> bool:
    """True when a line is a price the book is taking (SIM-555).

    A line flagged ``is_off`` or inactive, or without a price, is not a quote.
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
    """The book's usable line on one selection, or ``None`` (SIM-555).

    The feed lists one line per book per selection. When a book lists more than
    one usable line, the one flagged ``main`` wins, else the first.
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
    """The one book that opened every side, or ``None`` (SIM-555).

    The same rule as the opening row: every side's ``opening_line`` names a
    ``book_id`` and a ``cost``, and all sides name the same book. No sides,
    a side without an opener, or two openers give ``None``.
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
    """A vendor stamp (naive UTC text, ``"2026-09-10 16:15:50"``) as an aware UTC datetime."""
    if not value:
        return None
    parsed = _parse_utc(str(value))
    return None if parsed is None else parsed.replace(tzinfo=UTC)


def _books_on(sides: Mapping[str, Mapping[str, Any]]) -> list[int]:
    """Every book id listed on the given sides, in first-appearance order."""
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
    """True when two lines carry the same line and prices within ``tolerance``.

    ``tolerance`` is an implied-probability gap; ``0.0`` asks for identical
    prices (see :func:`twin_price_tolerance`).
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
    """True when the ties show a first-five entry is not a first-inning copy (SIM-555).

    A book that lists a first-five tie the first-inning entry lacks, or a tie
    beyond ``tolerance`` of the first-inning tie, did not copy the entry. A
    first-five entry with no tie decides nothing here: a copy may drop the
    tie, so the team sides decide. On a near-even game a book's two-way
    first-five and first-inning prices sit within the tolerance, so without
    this rule a book that adds a first-five tie reads as a copy.
    """
    if f5_tie is None:
        return False
    if f1_tie is None:
        return True
    return not _same_line(f5_tie, f1_tie, tolerance)


def _parse_utc(value: str) -> datetime | None:
    """Parse a UTC timestamp from either source, or ``None`` if unparseable.

    The MLB schedule's ``gameDate`` is ISO-8601 (``"2026-09-08T22:35:00Z"``);
    BettingPros' ``scheduled`` is space-separated with no zone marker
    (``"2026-09-08 22:35:00"``) but represents the same UTC instant (verified
    against a live game: both read the identical wall-clock value). Naive
    ``datetime`` objects from both are therefore directly comparable.
    """
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _normalize_name(name: str) -> str:
    """Lower-case, strip accents + non-alphanumerics, collapse spaces."""
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
