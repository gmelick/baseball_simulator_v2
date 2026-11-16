"""The odds vocabulary and the odds provider seam.

Every reader and writer of odds imports its names from here; none keeps a copy.

* The prop markets. :data:`PROP_STATS` lists the 15 values of
  ``raw.prop_odds.prop_stat``, split by role into :data:`PITCHER_PROP_STATS`
  and :data:`BATTER_PROP_STATS`.
* The game markets. :data:`GAME_MARKET_TYPES` lists the 15 values of
  ``raw.game_odds.market_type``. :data:`GAME_MARKET_KIND` says which columns a
  market fills; :data:`GAME_MARKET_SEGMENT` and :data:`GAME_MARKET_SIDE` say
  what it settles on, so no caller parses a market name.
* The books. A row holds one book's prices for every side of one market. The
  row names the book as ``'bp:<id>'``, with the BettingPros book id.
  :data:`BOOK_KIND` says what a book is; only a listed sportsbook is bettable.
  The readers grade against one book: the first on :data:`GRADED_BOOK_PREFERENCE`
  that has a row for the market.
* The provider. :class:`OddsProvider` returns one row per book.
  :func:`get_odds_provider` picks a provider by the ``ODDS_PROVIDER``
  environment variable: ``bettingpros``, or the deterministic mock by default.

The market kinds (the columns a game row fills):

* ``moneyline``: ``home_ml`` / ``away_ml``.
* ``three_way``: ``home_ml`` / ``away_ml`` / ``draw_ml``. A first-inning or
  first-five moneyline can end tied.
* ``runline``: ``home_spread`` / ``home_spread_ml`` / ``away_spread`` /
  ``away_spread_ml``.
* ``total``: ``total_line`` / ``over_ml`` / ``under_ml``.
* ``yes_no``: "a run in the first inning". Yes is ``over_ml`` and No is
  ``under_ml`` at ``total_line`` 0.5, because a run IS over 0.5 runs.

A row's ``line_type`` is ``opening``, ``current``, ``closing`` or
``bet_placement``. The CHECK constraints on both tables list the same values.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

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


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """An odds source: one row per book for a game market or a prop market.

    ``get_odds`` gives the one row the live service broadcasts.
    ``isinstance(obj, OddsProvider)`` checks only that the methods exist.
    An unknown ``market_type`` or ``prop_stat`` raises ``ValueError``.
    """

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> dict[str, Any]:
        """The graded book's row of one game market."""
        ...

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """One row per book for one game market, with every price field."""
        ...

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """One row per book for one player's prop market."""
        ...


def odds_rows_by_book(
    provider: Any,
    game_pk: int,
    *,
    line_type: str = "current",
    market_type: str = "moneyline",
) -> list[dict[str, Any]]:
    """Every book's row of one game market, from any provider.

    A provider without ``get_odds_by_book`` (a test fake) answers with its one
    ``get_odds`` row. A by-book result that is not a list or a tuple (an
    unconfigured mock object) also falls back to ``get_odds``.
    """
    by_book = getattr(provider, "get_odds_by_book", None)
    if callable(by_book):
        rows = by_book(game_pk, line_type=line_type, market_type=market_type)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return [provider.get_odds(game_pk, line_type=line_type, market_type=market_type)]


def prop_rows_by_book(
    provider: Any,
    game_pk: int,
    player_id: int,
    prop_stat: str,
    *,
    line_type: str = "current",
) -> list[dict[str, Any]]:
    """Every book's row of one player's prop market, from any provider.

    The fallback is the same as :func:`odds_rows_by_book`, to ``get_prop_odds``.
    """
    by_book = getattr(provider, "get_prop_odds_by_book", None)
    if callable(by_book):
        rows = by_book(game_pk, player_id, prop_stat, line_type=line_type)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return [provider.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)]


# ---------------------------------------------------------------------------
# The provider registry and factory
# ---------------------------------------------------------------------------

#: The environment variable that names the provider.
ODDS_PROVIDER_ENV = "ODDS_PROVIDER"
#: The provider name when the environment variable is unset or empty.
DEFAULT_PROVIDER = "mock"

# Provider name → zero-argument factory. Each factory imports its provider late.
_PROVIDER_FACTORIES: dict[str, Callable[[], OddsProvider]] = {}


def _make_mock_provider() -> OddsProvider:
    """The factory of the mock provider.

    The import is inside the function. It avoids a circular import and keeps
    this module importable without the live pipeline's dependencies.
    """
    from pipeline.live.ingestion.mock_odds import MockOddsAPI

    return MockOddsAPI()


def register_odds_provider(name: str, factory: Callable[[], OddsProvider]) -> None:
    """Register a provider factory, so ``ODDS_PROVIDER=<name>`` selects it.

    ``factory`` takes no argument. A test uses this to inject a fake provider.
    Names match case-insensitively.
    """
    _PROVIDER_FACTORIES[name.strip().lower()] = factory


def _make_bettingpros_provider() -> OddsProvider:
    """The factory of the BettingPros provider."""
    from pipeline.bettingpros.provider import BettingProsOddsProvider

    return BettingProsOddsProvider()


# The built-in providers.
register_odds_provider("mock", _make_mock_provider)
register_odds_provider("bettingpros", _make_bettingpros_provider)


def get_odds_provider(name: str | None = None) -> OddsProvider:
    """The selected odds provider.

    The name comes from ``name``, else ``ODDS_PROVIDER``, else ``"mock"``.
    An unregistered name raises ``RuntimeError``.
    """
    selected = (name or os.environ.get(ODDS_PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    factory = _PROVIDER_FACTORIES.get(selected)
    if factory is None:
        raise RuntimeError(
            f"Unknown ODDS_PROVIDER '{selected}'. Registered providers: "
            f"{', '.join(sorted(_PROVIDER_FACTORIES))}. Set {ODDS_PROVIDER_ENV} to one "
            "of these or register one via register_odds_provider()."
        )
    return factory()


__all__ = [
    "OddsProvider",
    "get_odds_provider",
    "register_odds_provider",
    "ODDS_PROVIDER_ENV",
    "DEFAULT_PROVIDER",
    # The prop markets
    "PROP_STATS",
    "PITCHER_PROP_STATS",
    "BATTER_PROP_STATS",
    "PROP_STAT_TO_MODEL_PROP",
    # The game markets
    "GAME_MARKET_TYPES",
    "FULL_GAME_MARKET_TYPES",
    "GAME_MARKET_KIND",
    "GAME_MARKET_SEGMENT",
    "GAME_MARKET_SIDE",
    "GAME_MARKET_NAMES",
    "GAME_ODDS_FIELDS",
    # The books and the by-book seam
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
    "odds_rows_by_book",
    "prop_rows_by_book",
]
