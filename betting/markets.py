"""Our market names and our book labels: the vocabulary every odds reader and writer shares."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The prop markets
# ---------------------------------------------------------------------------

#: The pitcher markets. A pitcher gets these and never a batter market.
PITCHER_PROP_STATS: tuple[str, ...] = (
    "strikeouts",
    "earned_runs",
    "walks",
    "outs_recorded",
    "hits_allowed",
)


#: The batter markets. A batter gets these and never a pitcher market.
#: Trap: ``hits`` is the batter market; ``hits_allowed`` is the pitcher market.
BATTER_PROP_STATS: tuple[str, ...] = (
    "hits",
    "home_runs",
    "total_bases",
    "rbis",
    "singles",
    "doubles",
    "triples",
    "runs",
    "stolen_bases",
    "hits_runs_rbis",
)


#: Every prop market, in the canonical order.
PROP_STATS: tuple[str, ...] = (
    "strikeouts",
    "hits",
    "home_runs",
    "earned_runs",
    "walks",
    "total_bases",
    "rbis",
    "singles",
    "doubles",
    "triples",
    "runs",
    "stolen_bases",
    "hits_runs_rbis",
    "outs_recorded",
    "hits_allowed",
)


#: prop_stat → the prop name the simulator emits
#: (``simulation/prop_distributions.py``).
PROP_STAT_TO_MODEL_PROP: dict[str, str] = {
    "strikeouts": "K",
    "walks": "BB",
    "earned_runs": "ER",
    "outs_recorded": "OUTS",
    "hits_allowed": "H_ALLOWED",
    "hits": "H",
    "home_runs": "HR",
    "total_bases": "TB",
    "rbis": "RBI",
    "singles": "1B",
    "doubles": "2B",
    "triples": "3B",
    "runs": "R",
    "stolen_bases": "SB",
    "hits_runs_rbis": "HRR",
}


#: The role of a prop-eligible player. A pitcher is asked only for the pitcher
#: markets and a hitter only for the batter markets. "both" is the safe default:
#: a player whose role is unknown, or a two-way player, is asked for every market.
PROP_ROLE_PITCHER = "pitcher"
PROP_ROLE_BATTER = "batter"
PROP_ROLE_BOTH = "both"


def prop_markets_for(role: str, prop_stats: tuple[str, ...] = PROP_STATS) -> tuple[str, ...]:
    """The markets of ``prop_stats`` a player in ``role`` is asked for.

    A pitcher gets the pitcher markets and a hitter the batter markets, so ``hits``
    (batter) and ``hits_allowed`` (pitcher) never cross. Any other role gets every market.
    """
    if role == PROP_ROLE_PITCHER:
        return tuple(s for s in prop_stats if s in PITCHER_PROP_STATS)
    if role == PROP_ROLE_BATTER:
        return tuple(s for s in prop_stats if s in BATTER_PROP_STATS)
    return prop_stats


# ---------------------------------------------------------------------------
# The game markets
# ---------------------------------------------------------------------------

#: The three full-game markets.
FULL_GAME_MARKET_TYPES: tuple[str, ...] = ("moneyline", "runline", "total")


#: Every game market, in the canonical order: the full-game markets first.
GAME_MARKET_TYPES: tuple[str, ...] = (
    "moneyline",
    "runline",
    "total",
    "f1_moneyline",
    "f5_moneyline",
    "f1_total",
    "f5_total",
    "f1_runline",
    "f5_runline",
    "team_total_home",
    "team_total_away",
    "f5_team_total_home",
    "f5_team_total_away",
    "first_to_score",
    "first_inning_run",
)


#: market_type → the columns it fills (the module docstring lists the kinds).
GAME_MARKET_KIND: dict[str, str] = {
    "moneyline": "moneyline",
    "runline": "runline",
    "total": "total",
    "f1_moneyline": "three_way",
    "f5_moneyline": "three_way",
    "f1_total": "total",
    "f5_total": "total",
    "f1_runline": "runline",
    "f5_runline": "runline",
    "team_total_home": "total",
    "team_total_away": "total",
    "f5_team_total_home": "total",
    "f5_team_total_away": "total",
    "first_to_score": "moneyline",
    "first_inning_run": "yes_no",
}


#: market_type → the part of the game it settles on: ``game``, ``f1`` (the
#: first inning) or ``f5`` (the first five innings).
GAME_MARKET_SEGMENT: dict[str, str] = {
    "moneyline": "game",
    "runline": "game",
    "total": "game",
    "f1_moneyline": "f1",
    "f5_moneyline": "f5",
    "f1_total": "f1",
    "f5_total": "f5",
    "f1_runline": "f1",
    "f5_runline": "f5",
    "team_total_home": "game",
    "team_total_away": "game",
    "f5_team_total_home": "f5",
    "f5_team_total_away": "f5",
    "first_to_score": "game",
    "first_inning_run": "f1",
}


#: market_type → the team a team total counts; ``None`` for a both-team market.
GAME_MARKET_SIDE: dict[str, str | None] = {
    m: ("home" if m.endswith("_home") else "away" if m.endswith("_away") else None)
    for m in GAME_MARKET_TYPES
}


#: market_type → the plain name the API and the logs show.
GAME_MARKET_NAMES: dict[str, str] = {
    "moneyline": "Moneyline",
    "runline": "Run line",
    "total": "Total",
    "f1_moneyline": "First inning moneyline",
    "f5_moneyline": "First five moneyline",
    "f1_total": "First inning total",
    "f5_total": "First five total",
    "f1_runline": "First inning run line",
    "f5_runline": "First five run line",
    "team_total_home": "Home team total",
    "team_total_away": "Away team total",
    "f5_team_total_home": "Home team first five total",
    "f5_team_total_away": "Away team first five total",
    "first_to_score": "First team to score",
    "first_inning_run": "A run in the first inning",
}


#: The price fields of a game row, in the ``raw.game_odds`` column order.
#: A provider returns every field, with ``None`` where the market fills none.
GAME_ODDS_FIELDS: tuple[str, ...] = (
    "home_ml",
    "away_ml",
    "draw_ml",
    "home_spread",
    "home_spread_ml",
    "away_spread",
    "away_spread_ml",
    "total_line",
    "over_ml",
    "under_ml",
)


# ---------------------------------------------------------------------------
# The books
# ---------------------------------------------------------------------------

#: Book id → display name. The ids are BettingPros' (``/v3/books``).
BOOK_NAMES: dict[int, str] = {
    0: "BettingPros Consensus",
    10: "FanDuel",
    12: "DraftKings",
    13: "Caesars",
    14: "Fanatics",
    15: "SugarHouse",
    18: "BetRivers",
    19: "BetMGM",
    24: "bet365",
    27: "PartyCasino",
    33: "theScore Bet",
    36: "Underdog",
    37: "PrizePicks",
    38: "ProphetX",
    39: "Fliff",
    49: "Hard Rock",
    60: "Novig",
    63: "Sleeper",
    68: "Kalshi",
    73: "Polymarket",
    75: "Polymarket US",
    # The pick'em apps and prediction markets, so none passes as a sportsbook.
    44: "ThriveFantasy",
    45: "Betr",
    53: "Dabble",
    69: "FanDuel Picks",
    70: "DraftKings Pick6",
    74: "DraftKings Predictions",
    76: "Underdog Predict",
    78: "Plus500",
}


#: Book id → what the book is. ``blend`` (id 0) is the vendor's mix of other
#: books' prices; nobody can bet it. ``dfs`` is a daily-fantasy pick'em app.
#: An id not listed here is ``unknown``, so a new book is never bettable
#: until someone adds it.
BOOK_KIND: dict[int, str] = {
    0: "blend",
    10: "sportsbook",
    12: "sportsbook",
    13: "sportsbook",
    14: "sportsbook",
    15: "sportsbook",
    18: "sportsbook",
    19: "sportsbook",
    24: "sportsbook",
    27: "sportsbook",
    33: "sportsbook",
    49: "sportsbook",
    36: "dfs",
    37: "dfs",
    39: "dfs",
    44: "dfs",
    45: "dfs",
    53: "dfs",
    63: "dfs",
    69: "dfs",
    70: "dfs",
    38: "exchange",
    60: "exchange",
    68: "prediction",
    73: "prediction",
    74: "prediction",
    75: "prediction",
    76: "prediction",
    78: "prediction",
}


#: A book's short name (lower case, no spaces) → its id, for ``--book NAME``.
#: Each sportsbook's display name, in that form, is a key too, so
#: ``resolve_book(book_display_name(label))`` gives the id back.
BOOK_IDS_BY_NAME: dict[str, int] = {
    "fanduel": 10,
    "draftkings": 12,
    "caesars": 13,
    "fanatics": 14,
    "sugarhouse": 15,
    "betrivers": 18,
    "betmgm": 19,
    "bet365": 24,
    "partycasino": 27,
    "thescore": 33,
    "thescorebet": 33,
    "hardrock": 49,
}


#: The prefix of every stored book label (``'bp:12'``).
BOOK_LABEL_PREFIX = "bp:"


#: The SQL filter every reader appends, so it grades only a book's row.
STORED_BOOK_FILTER_SQL = "book LIKE 'bp:%'"


#: The graded book, in order. A reader grades against the first book on the
#: list with a row for the market, the same list on every game. The order
#: ranks the books by how sharp their closing line is; DraftKings is first.
#: Positions 3-11 are near-ties; re-read the order once 2023 and 2026 are loaded.
#: SugarHouse and PartyCasino copy other books' lines, so they come last.
#: The record is ``docs/audit/records/sim555_book_sharpness.txt``.
GRADED_BOOK_PREFERENCE: tuple[int, ...] = (12, 19, 10, 33, 18, 24, 13, 49, 14, 15, 27)


#: The book kinds whose prices a bettor can take.
BETTABLE_KINDS: frozenset[str] = frozenset({"sportsbook"})


#: The stamp of this row scheme. The readers write it into each report;
#: the skill table refuses to merge reports with different stamps.
ODDS_ROW_VERSION = "sim555.2"


def book_label(book_id: int) -> str:
    """The stored label of a book id: ``12`` → ``'bp:12'``."""
    return f"{BOOK_LABEL_PREFIX}{int(book_id)}"


def book_id_from_label(label: str | None) -> int | None:
    """The book id in a stored label: ``'bp:12'`` → ``12``.

    A label that is not ``bp:<digits>`` gives ``None``. The digits must be ASCII,
    because ``str.isdigit`` also accepts ``'²'`` and other scripts' digits.
    """
    if not isinstance(label, str) or not label.startswith(BOOK_LABEL_PREFIX):
        return None
    digits = label[len(BOOK_LABEL_PREFIX) :]
    if not (digits.isascii() and digits.isdigit()):
        return None
    return int(digits)


def book_kind(label: str | None) -> str:
    """The kind of the book a label names: ``'bp:12'`` → ``'sportsbook'``.

    An unlisted id or a label that is not ``bp:<id>`` gives ``'unknown'``.
    """
    book_id = book_id_from_label(label)
    if book_id is None:
        return "unknown"
    return BOOK_KIND.get(book_id, "unknown")


def is_bettable(label: str | None) -> bool:
    """True when a bettor can take the book's prices."""
    return book_kind(label) in BETTABLE_KINDS


def book_display_name(label: str | None) -> str:
    """The book's display name: ``'bp:10'`` → ``'FanDuel'``.

    An unknown label comes back unchanged; ``None`` gives an empty string.
    """
    if label is None:
        return ""
    book_id = book_id_from_label(label)
    if book_id is not None and book_id in BOOK_NAMES:
        return BOOK_NAMES[book_id]
    return label


def resolve_book(book: str | int | None) -> int | None:
    """A book id from a label, a short name or an id.

    ``'bp:12'``, ``'draftkings'``, ``'DraftKings'`` and ``12`` all give ``12``.
    A name matches lower-cased with its spaces removed.
    An unknown book gives ``None``.
    """
    if book is None or isinstance(book, bool):
        return None
    if isinstance(book, int):
        return book
    book_id = book_id_from_label(book)
    if book_id is not None:
        return book_id
    return BOOK_IDS_BY_NAME.get(book.lower().replace(" ", ""))


def graded_book_labels(preference: tuple[int, ...] = GRADED_BOOK_PREFERENCE) -> list[str]:
    """The preference list as stored labels: ``['bp:12', 'bp:19', ...]``."""
    return [book_label(book_id) for book_id in preference]


def bettable_labels() -> list[str]:
    """The labels of the bettable books, sorted by id.

    A reader keeps only these rows for the graded row and the best price.
    """
    return [
        book_label(book_id) for book_id, kind in sorted(BOOK_KIND.items()) if kind in BETTABLE_KINDS
    ]


def graded_row_order_sql(param: str) -> str:
    """The SQL ORDER BY tail that puts the graded book's row first.

    ``param`` is the query parameter that holds :func:`graded_book_labels`
    (``'$3'``). A book not on the list sorts last.
    A tie goes to the newest fetch.
    """
    return f"array_position({param}::varchar[], book) NULLS LAST, fetched_at DESC"


__all__ = [
    "PROP_ROLE_PITCHER",
    "PROP_ROLE_BATTER",
    "PROP_ROLE_BOTH",
    "prop_markets_for",
    "PROP_STATS",
    "PITCHER_PROP_STATS",
    "BATTER_PROP_STATS",
    "PROP_STAT_TO_MODEL_PROP",
    "GAME_MARKET_TYPES",
    "FULL_GAME_MARKET_TYPES",
    "GAME_MARKET_KIND",
    "GAME_MARKET_SEGMENT",
    "GAME_MARKET_SIDE",
    "GAME_MARKET_NAMES",
    "GAME_ODDS_FIELDS",
    "BOOK_NAMES",
    "BOOK_KIND",
    "BOOK_IDS_BY_NAME",
    "BOOK_LABEL_PREFIX",
    "STORED_BOOK_FILTER_SQL",
    "GRADED_BOOK_PREFERENCE",
    "BETTABLE_KINDS",
    "ODDS_ROW_VERSION",
    "book_label",
    "book_id_from_label",
    "book_kind",
    "is_bettable",
    "book_display_name",
    "resolve_book",
    "graded_book_labels",
    "bettable_labels",
    "graded_row_order_sql",
]
