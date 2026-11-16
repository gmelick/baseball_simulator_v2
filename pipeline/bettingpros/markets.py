"""BettingPros' market ids, and the rules for its mislabelled first-five entries."""

from __future__ import annotations

from datetime import date

from pipeline.odds_provider import GAME_MARKET_TYPES, PROP_STATS

# ---------------------------------------------------------------------------
# The market ids
# ---------------------------------------------------------------------------

#: Our game market name -> the BettingPros market id. The two team totals of
#: one segment share an id: the response holds one offer per team.
_GAME_MARKET_IDS: dict[str, int] = {
    "moneyline": 122,
    "total": 175,
    "runline": 176,
    "f1_moneyline": 278,
    "f5_moneyline": 279,
    "f1_total": 280,
    "f5_total": 281,
    "f1_runline": 282,
    "f5_runline": 283,
    "team_total_home": 277,
    "team_total_away": 277,
    "f5_team_total_home": 407,
    "f5_team_total_away": 407,
    "first_to_score": 286,
    "first_inning_run": 369,
}
if set(_GAME_MARKET_IDS) != set(GAME_MARKET_TYPES):  # pragma: no cover — an import-time guard
    raise RuntimeError(
        "BettingPros game market ids and GAME_MARKET_TYPES disagree: "
        f"{sorted(set(_GAME_MARKET_IDS) ^ set(GAME_MARKET_TYPES))}"
    )

#: Our prop market name -> the BettingPros market id.
_PROP_MARKET_IDS: dict[str, int] = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
    "singles": 295,
    "doubles": 291,
    "triples": 292,
    "runs": 288,
    "stolen_bases": 294,
    "hits_runs_rbis": 403,
    "outs_recorded": 405,
    "hits_allowed": 404,
}
if set(_PROP_MARKET_IDS) != set(PROP_STATS):  # pragma: no cover — an import-time guard
    raise RuntimeError(
        "BettingPros prop market ids and PROP_STATS disagree: "
        f"{sorted(set(_PROP_MARKET_IDS) ^ set(PROP_STATS))}"
    )

# ---------------------------------------------------------------------------
# The first-five rules
#
# Some books post first-inning prices under a first-five label. Two rules drop
# those entries: a dated rule for a known book, and the "twin" rule for a book
# whose first-five line copies its own first-inning line.
# ---------------------------------------------------------------------------

#: A first-five market id -> the first-inning market it may copy.
F5_TWIN_MARKETS: dict[int, int] = {283: 282, 281: 280, 279: 278}

#: A book whose first-five entries are first-inning bets from this date on.
#: DraftKings (12) from 2025-03-01, found by the 300-game census.
F5_EXCLUDED_BOOKS: dict[int, date] = {12: date(2025, 3, 1)}

#: The first-five markets each dated exclusion covers. A listed book with no
#: entry here is excluded on all three. DraftKings' first-five moneyline is
#: kept: only 5 of its 71 rows are first-inning bets, and the load guard
#: refuses all 5.
F5_EXCLUDED_MARKETS: dict[int, frozenset[int]] = {12: frozenset({283, 281})}


def f5_excluded_markets(book_id: int) -> frozenset[int]:
    """The first-five markets a book's dated exclusion covers.

    It does not say whether the book is excluded at all;
    :func:`is_f5_dated_excluded` does.
    """
    return F5_EXCLUDED_MARKETS.get(int(book_id), frozenset(F5_TWIN_MARKETS))


def is_f5_dated_excluded(book_id: int, market_id: int, game_date: date | None) -> bool:
    """True when the dated rule drops this book's row of this market on this date.

    An unknown game date excludes nothing.
    """
    start = F5_EXCLUDED_BOOKS.get(int(book_id))
    if start is None or game_date is None:
        return False
    return start <= game_date and int(market_id) in f5_excluded_markets(book_id)


#: The largest implied-probability gap at which a first-five line counts as a
#: copy of the same book's first-inning line.
TWIN_PRICE_TOLERANCE = 0.03

#: The gap for the moneyline. Both moneylines sit at the line 1, and a
#: near-even game's real first-five price can sit within 0.03 of the
#: first-inning price. The evidence for copies is run lines only, so a
#: moneyline is a copy only at identical prices.
TWIN_MONEYLINE_PRICE_TOLERANCE = 0.0

#: The first-five markets whose twin test needs identical prices.
_EXACT_TWIN_MARKETS: frozenset[int] = frozenset({279})


def twin_price_tolerance(market_id: int) -> float:
    """The twin rule's price gap for a first-five market id."""
    if int(market_id) in _EXACT_TWIN_MARKETS:
        return TWIN_MONEYLINE_PRICE_TOLERANCE
    return TWIN_PRICE_TOLERANCE
