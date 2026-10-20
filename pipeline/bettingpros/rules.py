"""The vendor-data rules: which first-five entries are mislabelled first-inning
bets, and how close a twin's prices must be.
"""

from __future__ import annotations

from datetime import date

#: SIM-555: a first-five market → the first-inning market whose entries a
#: mislabelled first-five entry copies (283 run line → 282, 281 total → 280,
#: 279 moneyline → 278).
F5_TWIN_MARKETS: dict[int, int] = {283: 282, 281: 280, 279: 278}

#: SIM-555: a book whose first-five entries are first-inning bets from this
#: date on. Applies to the F5_TWIN_MARKETS keys that F5_EXCLUDED_MARKETS lists
#: for the book. The 300-game census before the re-load confirms the date and
#: finds any other book.
F5_EXCLUDED_BOOKS: dict[int, date] = {12: date(2025, 3, 1)}

#: SIM-555 (census fix 2026-09-28): the first-five markets each dated
#: exclusion applies to. A book on F5_EXCLUDED_BOOKS with no entry here is
#: excluded on all three F5_TWIN_MARKETS keys. The 300-game census read
#: DraftKings' dated rows per market: 43 of its 54 first-five run-line rows
#: (283) are first-inning-shaped, and its only first-five total rows (281) sit
#: at the line 0.5 with first-inning prices; but only 5 of its 71 first-five
#: moneyline rows (279) are, and the guard refuses all 5 (a first-inning tie
#: price). So DraftKings' first-five moneyline is kept.
F5_EXCLUDED_MARKETS: dict[int, frozenset[int]] = {12: frozenset({283, 281})}


def f5_excluded_markets(book_id: int) -> frozenset[int]:
    """The first-five market ids a book's dated exclusion applies to (SIM-555).

    The book's entry on :data:`F5_EXCLUDED_MARKETS`, else every key of
    :data:`F5_TWIN_MARKETS`. The answer does not say whether the book is on
    :data:`F5_EXCLUDED_BOOKS`; :func:`is_f5_dated_excluded` does.
    """
    return F5_EXCLUDED_MARKETS.get(int(book_id), frozenset(F5_TWIN_MARKETS))


def is_f5_dated_excluded(book_id: int, market_id: int, game_date: date | None) -> bool:
    """True when the dated rule drops this book's row of this market on this date (SIM-555).

    The book is on :data:`F5_EXCLUDED_BOOKS`, its date is on or before the
    game's, and the market is one of :func:`f5_excluded_markets`. An unknown
    game date excludes nothing.
    """
    start = F5_EXCLUDED_BOOKS.get(int(book_id))
    if start is None or game_date is None:
        return False
    return start <= game_date and int(market_id) in f5_excluded_markets(book_id)


#: SIM-555: the implied-probability gap under which a first-five line "copies"
#: the same book's first-inning line.
TWIN_PRICE_TOLERANCE = 0.03

#: SIM-555 (review fix 2026-09-28): the gap for the moneyline pair (279 → 278).
#: Both moneylines carry the line 1 on every side, so the line test cannot
#: tell them apart, and on a near-even game a book's real two-way first-five
#: price sits within 0.03 of its first-inning price (Caesars −120 / +100
#: against −110 / −110 on game 634368). The evidence for the twin is run
#: lines only, so a moneyline counts as a copy only at IDENTICAL prices.
TWIN_MONEYLINE_PRICE_TOLERANCE = 0.0

#: The first-five markets whose twin test needs identical prices.
_EXACT_TWIN_MARKETS: frozenset[int] = frozenset({279})


def twin_price_tolerance(market_id: int) -> float:
    """The twin rule's price gap for a first-five market id (SIM-555).

    The moneyline (279) needs identical prices
    (:data:`TWIN_MONEYLINE_PRICE_TOLERANCE`); the run line and the total take
    :data:`TWIN_PRICE_TOLERANCE`.
    """
    if int(market_id) in _EXACT_TWIN_MARKETS:
        return TWIN_MONEYLINE_PRICE_TOLERANCE
    return TWIN_PRICE_TOLERANCE
