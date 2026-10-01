"""
pipeline/odds_provider.py
=========================
SIM-370 — Odds/prop provider abstraction (real-provider swap seam).

Phase 5 wired multi-book ingestion, a sharp-book flag, and a fetch cadence
(SIM-340) on top of :class:`MockOddsAPI` in
``pipeline/live/live_ingestion_pipeline.py``.  This module adds the *seam* that
lets a REAL odds/prop provider drop in behind that mock without touching any
ingestion, CLV, or persistence code:

  * :class:`OddsProvider` — a ``typing.Protocol`` describing the exact methods
    the live pipeline consumes (:meth:`get_odds`, :meth:`get_prop_odds`).  Any
    object that structurally matches it (the existing ``MockOddsAPI``, a real
    HTTP-backed provider, or a test fake) is a drop-in source.
  * :func:`get_odds_provider` — a factory that selects the provider by the
    ``ODDS_PROVIDER`` environment variable, defaulting to the deterministic
    ``MockOddsAPI`` so all existing behaviour and tests are unchanged.
  * :class:`RealOddsAPIProvider` — a conforming STUB marking exactly where a
    live integration (e.g. The Odds API) plugs in.  It raises a clear,
    actionable error until it is configured and implemented; there is no real
    odds feed in this environment.
  * :func:`register_odds_provider` — lets tests (and future real providers)
    register a named factory so ``ODDS_PROVIDER=<name>`` resolves to them.

Provider contract (what a real provider MUST honour)
----------------------------------------------------
Both methods must return the same dict shapes ``MockOddsAPI`` returns, because
``raw.game_odds`` / ``raw.prop_odds`` inserts and the CLV engine (SIM-339)
depend on them:

``get_odds(game_pk, *, line_type, market_type, book, is_sharp_book) -> dict``
    Keys: ``game_pk, source, is_mock, book, line_type, market_type,
    is_sharp_book, home_ml, away_ml, draw_ml, home_spread, home_spread_ml,
    away_spread, away_spread_ml, total_line, over_ml, under_ml``. ``market_type``
    is one of :data:`GAME_MARKET_TYPES`; :data:`GAME_MARKET_KIND` says which of
    those keys the market fills (see "The game-market vocabulary" below).

``get_prop_odds(game_pk, player_id, prop_stat, *, line_type, book,
is_sharp_book) -> dict``
    Keys: ``game_pk, player_id, prop_stat, line, over_ml, under_ml, book,
    line_type, is_sharp_book, source, is_mock``.  Must raise ``ValueError`` on
    an unknown ``prop_stat`` (the 15 markets in ``PROP_STATS``), mirroring
    ``MockOddsAPI``.

The prop-market vocabulary (SIM-421)
------------------------------------
This module is the ONE place the ``raw.prop_odds.prop_stat`` vocabulary is
written down. :data:`PITCHER_PROP_STATS` and :data:`BATTER_PROP_STATS` split
the 15 markets by the role they price; :data:`PROP_STATS` is their union in
the canonical order. The mock provider, the BettingPros provider, the live
pipeline, the nightly opening-line job and the historical loader all import
these tuples — none carries a private copy. The CHECK constraint on
``raw.prop_odds`` (Alembic migration 0022) lists the same 15 values.
:data:`PROP_STAT_TO_MODEL_PROP` maps each market to the prop name the
simulator emits (``simulation/prop_distributions.py``).

The game-market vocabulary (SIM-421, owner ruling 2026-09-12)
------------------------------------------------------------
The owner's ruling: the odds tables carry EVERY market the book posts, because
the simulator can price every one of them. :data:`GAME_MARKET_TYPES` is the
``raw.game_odds.market_type`` vocabulary: the three full-game markets
(moneyline, run line, total) plus the twelve segment and team markets
BettingPros posts on every game (the first-inning and first-five-innings
moneyline / total / run line, the full-game and first-five team totals for
each side, the first team to score, and "a run in the first inning").
Every market is stored in the SAME row layout; :data:`GAME_MARKET_KIND` says
which columns a market fills:

  * ``moneyline``  — ``home_ml`` / ``away_ml`` (the full-game moneyline, the
    first team to score).
  * ``three_way``  — ``home_ml`` / ``away_ml`` / ``draw_ml`` (the first-inning
    and first-five moneylines, which can end tied; ``draw_ml`` is migration
    0024's column).
  * ``runline``    — ``home_spread`` / ``home_spread_ml`` / ``away_spread`` /
    ``away_spread_ml``.
  * ``total``      — ``total_line`` / ``over_ml`` / ``under_ml`` (the full-game,
    first-inning and first-five totals, and each side's team total — the
    market name says whose runs the line counts).
  * ``yes_no``     — "a run in the first inning": Yes is stored as ``over_ml``
    and No as ``under_ml`` at ``total_line`` 0.5, because "yes, a run scores"
    IS "over 0.5 first-inning runs". One layout, no special column.

The CHECK constraint on ``raw.game_odds.market_type`` (migration 0024) lists
the same 15 values. :data:`GAME_MARKET_SEGMENT` names the slice of the game a
market settles on (``game``, ``f1`` = the first inning, ``f5`` = the first
five innings) and :data:`GAME_MARKET_SIDE` the team a team total counts
(``home`` / ``away``; ``None`` for both-team markets), so a consumer never
parses the market name.

Multi-book / sharp-flag / line_type rules the provider MUST preserve:
  * ``book`` names the book a row's prices come from (see "The book
    vocabulary" below). The mock echoes the requested ``book`` verbatim.
  * ``is_sharp_book`` is carried through unchanged.
  * ``line_type`` is one of ``opening | current | closing | bet_placement`` and
    is echoed through so opening/closing-line capture stamps the right value.
  * Real providers should set ``source=<provider-name>`` and ``is_mock=False``.

The book vocabulary (SIM-555, 2026-09-28)
-----------------------------------------
A row holds ONE book's prices for every side of one market, and names the book
in ``book`` as ``'bp:<id>'`` (the id is BettingPros', from ``/v3/books``).
:data:`BOOK_NAMES` gives each id its display name; :data:`BOOK_KIND` says what
the id is: a sportsbook, the vendor's blended line (``blend``, id 0), a
daily-fantasy app (``dfs``), an exchange or a prediction market. An id the
table does not list is ``unknown``. Every kind is stored; :data:`BETTABLE_KINDS`
says which kinds count as a price a bettor can take, so an unlisted id is never
bettable: the readers keep the listed sportsbooks (:func:`bettable_labels`), a
list of what may be graded, not a list of what may not. The readers grade against ONE book: the first book on
:data:`GRADED_BOOK_PREFERENCE` that has a row for the market, the same list on
every game. :data:`ODDS_ROW_VERSION` stamps the reports built on this scheme.

A provider that stores every book implements two more methods:
``get_odds_by_book(game_pk, *, line_type, market_type) -> list[dict]`` and
``get_prop_odds_by_book(game_pk, player_id, prop_stat, *, line_type) ->
list[dict]``, one dict per book in the ``get_odds`` / ``get_prop_odds`` shape.
A writer calls them through :func:`odds_rows_by_book` /
:func:`prop_rows_by_book`, which fall back to the one-row methods for a
provider (a test fake) that has only those.

Owned by Data Engineer (Agent 4) with Betting Analyst (Agent 8) input.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# SIM-421: the prop-market vocabulary (the single source; see the module docstring)
# ---------------------------------------------------------------------------

#: The pitcher markets. A pitcher gets these and never a batter market.
PITCHER_PROP_STATS: tuple[str, ...] = (
    "strikeouts",
    "earned_runs",
    "walks",
    "outs_recorded",
    "hits_allowed",
)

#: The batter markets. A hitter gets these and never a pitcher market.
#: ``hits`` is the BATTER market (hits recorded); ``hits_allowed`` above is the
#: PITCHER market. They price different stat lines and must never be swapped.
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

#: Every prop market, in the canonical order: the seven original markets
#: (SIM-134) first, then the eight SIM-421 markets. A tuple so a caller cannot
#: mutate the order.
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

#: prop_stat → the model-side prop name the simulator emits
#: (``PITCHER_PROPS`` / ``BATTER_PROPS`` in ``simulation/prop_distributions.py``).
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
# SIM-421 (owner ruling 2026-09-12): the game-market vocabulary — every market
# the book posts on a game, in one row layout (see the module docstring)
# ---------------------------------------------------------------------------

#: The three full-game markets (moneyline, run line, total).
FULL_GAME_MARKET_TYPES: tuple[str, ...] = ("moneyline", "runline", "total")

#: SIM-555: an alias of :data:`FULL_GAME_MARKET_TYPES`, kept for the readers
#: that name it. Before SIM-555 a row for one of these three markets carried all
#: three markets' columns. The mock still fills all three on one row; the
#: BettingPros provider no longer does: each market fills only its own columns.
LEGACY_GAME_MARKET_TYPES: tuple[str, ...] = FULL_GAME_MARKET_TYPES

#: Every ``raw.game_odds.market_type`` value, in the canonical order: the three
#: full-game markets first, then the twelve segment and team markets.
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

#: market_type → the column layout it fills (the kinds are explained in the
#: module docstring).
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

#: market_type → the slice of the game the market settles on.
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

#: market_type → the team whose runs a team total counts; ``None`` for a
#: both-team market.
GAME_MARKET_SIDE: dict[str, str | None] = {
    m: ("home" if m.endswith("_home") else "away" if m.endswith("_away") else None)
    for m in GAME_MARKET_TYPES
}

#: The markets whose ``draw_ml`` column is meaningful.
THREE_WAY_GAME_MARKET_TYPES: tuple[str, ...] = tuple(
    m for m in GAME_MARKET_TYPES if GAME_MARKET_KIND[m] == "three_way"
)

#: The odds fields a ``get_odds`` dict carries, in the ``raw.game_odds`` column
#: order. Every provider returns all of them (``None`` when unresolved).
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
# SIM-555 (2026-09-28): the book vocabulary — one source, as for the markets
# ---------------------------------------------------------------------------

#: The books the platform names. The ids are BettingPros' (``/v3/books``).
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
    # Review fix (2026-09-28): the pick'em apps and prediction markets of the
    # vendor's /v3/books catalogue, so none of them passes as a sportsbook.
    44: "ThriveFantasy",
    45: "Betr",
    53: "Dabble",
    69: "FanDuel Picks",
    70: "DraftKings Pick6",
    74: "DraftKings Predictions",
    76: "Underdog Predict",
    78: "Plus500",
}

#: What a book id is. The vendor's blend (0) is a mix of other books' prices
#: that nobody can bet. Every sportsbook is listed by name. An id this table does
#: not list is ``unknown`` (see :func:`book_kind`), so a book the vendor adds
#: later is never taken as a price a bettor can use until someone classifies it
#: here. The vendor's ``/v3/books`` catalogue (read 2026-09-28) flags 60, 38,
#: 74, 68, 73, 75 and 78 as prediction markets; 36, 37, 39, 44, 45, 53, 63, 69
#: and 70 are daily-fantasy pick'em apps.
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
#: Every sportsbook's display name in :data:`BOOK_NAMES`, lower-cased with its
#: spaces removed, is a key too, so ``resolve_book(book_display_name(label))``
#: gives the label's id back (``'theScore Bet'`` → ``'thescorebet'``).
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

#: The filter every reader appends until the old ``consensus`` rows are gone.
STORED_BOOK_FILTER_SQL = "book LIKE 'bp:%'"

#: SIM-555 (decision 1): the graded book, in order. A reader grades against the
#: first book on the list with a valid closing row for the market, the same list
#: on every game. Set 2026-09-29 from the sharpness read on 2024-2025
#: (scripts/sim555_book_sharpness.txt) and its verification (the plan's §12):
#:   1. DraftKings: the sharpest closing line; no view ranks any book ahead of it.
#:   2. BetMGM: a sharper benchmark than theScore on the player props DraftKings
#:      lacks (home runs, doubles, runs, RBIs; gaps outside their 90% ranges).
#:   3. FanDuel, 4. theScore: a tie on sharpness; FanDuel's 2024 closes are fresh
#:      (a median 24 minutes before the latest book's), theScore's stale (140).
#:   5-8. BetRivers, bet365, Caesars, Hard Rock: ties, ordered by coverage (Hard
#:      Rock after Caesars: resolved behind it in 2025). 9. Fanatics: unranked.
#:   10. SugarHouse (BetRivers' lines), 11. PartyCasino (BetMGM's lines): copies last.
#: Positions 3-11 are near-ties; re-read the order once 2023 and 2026 are loaded.
GRADED_BOOK_PREFERENCE: tuple[int, ...] = (12, 19, 10, 33, 18, 24, 13, 49, 14, 15, 27)

#: SIM-555 (decision 6): the kinds whose rows count as a price a bettor can take.
BETTABLE_KINDS: frozenset[str] = frozenset({"sportsbook"})

#: The report stamp of this odds-row scheme. The readers write it into a
#: report's provenance; the skill table refuses to merge reports across it.
ODDS_ROW_VERSION = "sim555.2"


def book_label(book_id: int) -> str:
    """The stored label of a book id: ``12`` → ``'bp:12'``."""
    return f"{BOOK_LABEL_PREFIX}{int(book_id)}"


def book_id_from_label(label: str | None) -> int | None:
    """The book id in a stored label: ``'bp:12'`` → ``12``.

    ``'consensus'``, ``None`` and any label that is not ``bp:<digits>`` give
    ``None``. The digits must be ASCII: ``str.isdigit`` also accepts ``'²'``
    (which ``int`` refuses) and other scripts' digits (which ``int`` reads).
    """
    if not isinstance(label, str) or not label.startswith(BOOK_LABEL_PREFIX):
        return None
    digits = label[len(BOOK_LABEL_PREFIX) :]
    if not (digits.isascii() and digits.isdigit()):
        return None
    return int(digits)


def book_kind(label: str | None) -> str:
    """The kind of the book a label names.

    ``'bp:0'`` → ``'blend'``; ``'bp:12'`` → ``'sportsbook'``; an id that
    :data:`BOOK_KIND` does not list (``'bp:999'``) → ``'unknown'``, as does a
    label that is not a ``bp:`` label (``'consensus'``, ``None``). An unknown
    book is never bettable.
    """
    book_id = book_id_from_label(label)
    if book_id is None:
        return "unknown"
    return BOOK_KIND.get(book_id, "unknown")


def is_bettable(label: str | None) -> bool:
    """True when the label's book is a kind a bettor can take a price at."""
    return book_kind(label) in BETTABLE_KINDS


def book_display_name(label: str | None) -> str:
    """The book's display name: ``'bp:10'`` → ``'FanDuel'``.

    An id :data:`BOOK_NAMES` does not list keeps its label (``'bp:999'``); a
    label that is not a ``bp:`` label comes back as it is (``'consensus'``);
    ``None`` gives an empty string.
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
    A name is matched lower-cased with its spaces removed against
    :data:`BOOK_IDS_BY_NAME`. ``'consensus'``, ``None`` and an unknown name give
    ``None``; a caller treats ``None`` as "no such book".
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
    """The labels of every listed book whose kind is bettable, by id.

    ``['bp:10', 'bp:12', 'bp:13', ...]``: the listed sportsbooks. A reader
    keeps only these rows for the graded row and the best price
    (``book = ANY($n)``), so a book the vendor adds later, which
    :data:`BOOK_KIND` does not list, is never graded or offered.
    """
    return [
        book_label(book_id) for book_id, kind in sorted(BOOK_KIND.items()) if kind in BETTABLE_KINDS
    ]


def non_bettable_labels() -> list[str]:
    """The labels of every listed book whose kind is not bettable, by id.

    ``['bp:0', 'bp:36', 'bp:37', ...]``: the blend, the daily-fantasy apps, the
    exchanges and the prediction markets. An unlisted id is not bettable
    either, but no list can name it: a reader filters with
    :func:`bettable_labels` instead.
    """
    return [
        book_label(book_id)
        for book_id, kind in sorted(BOOK_KIND.items())
        if kind not in BETTABLE_KINDS
    ]


def graded_row_order_sql(param: str) -> str:
    """The ORDER BY tail that puts the graded book's row first.

    ``param`` is the query parameter that carries :func:`graded_book_labels`
    (``'$3'``): the book earliest on the list sorts first, a book not on the
    list sorts last, and a tie goes to the newest fetch.
    """
    return f"array_position({param}::varchar[], book) NULLS LAST, fetched_at DESC"


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """
    Structural interface for an odds/prop source consumed by the live pipeline.

    ``@runtime_checkable`` so ``isinstance(obj, OddsProvider)`` works as a
    duck-typed presence check (it verifies the methods exist, not their
    signatures — the return-shape contract is enforced by tests).  ``MockOddsAPI``
    satisfies this implicitly: its staticmethods are callable on an *instance*,
    which is how the pipeline invokes them.

    SIM-555: the two ``*_by_book`` methods return one row per book. A structural
    protocol cannot give a non-subclass a default body, so a writer calls them
    through :func:`odds_rows_by_book` / :func:`prop_rows_by_book`; those fall
    back to the one-row methods for a test fake that has only ``get_odds`` /
    ``get_prop_odds``.
    """

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """Return game-level betting lines for ``game_pk`` (see module docstring)."""
        ...

    def get_prop_odds(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """Return a single player-prop quote (see module docstring)."""
        ...

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """SIM-555: one ``get_odds``-shaped row per book for one market and line type."""
        ...

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """SIM-555: one ``get_prop_odds``-shaped row per book for one player and market."""
        ...


def odds_rows_by_book(
    provider: Any,
    game_pk: int,
    *,
    line_type: str = "current",
    market_type: str = "moneyline",
) -> list[dict[str, Any]]:
    """SIM-555: every book's row of one game market, from any provider.

    The provider's ``get_odds_by_book`` when it has one; otherwise its one
    ``get_odds`` row in a list (a test fake with only the one-row method). A
    by-book result that is not a list or a tuple (an unconfigured mock object)
    counts as "no by-book method", so the one-row method answers.
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
    """SIM-555: every book's row of one player's prop market, from any provider.

    The same fallback as :func:`odds_rows_by_book`, over ``get_prop_odds``.
    """
    by_book = getattr(provider, "get_prop_odds_by_book", None)
    if callable(by_book):
        rows = by_book(game_pk, player_id, prop_stat, line_type=line_type)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return [provider.get_prop_odds(game_pk, player_id, prop_stat, line_type=line_type)]


# ---------------------------------------------------------------------------
# Real-provider STUB — the obvious place a live integration plugs in
# ---------------------------------------------------------------------------


class RealOddsAPIProvider:
    """
    SIM-370 TEMPLATE for a real odds/prop provider (e.g. The Odds API).

    Conforms to :class:`OddsProvider` so it is a structural drop-in, but every
    method raises until a live integration is actually wired.  This is a
    reference template only: no ``ODDS_PROVIDER`` value resolves to it — both
    ``ODDS_PROVIDER=bettingpros`` and ``ODDS_PROVIDER=real`` now map to the live
    :class:`~pipeline.bettingpros_odds_provider.BettingProsOddsProvider`
    (SIM-405).  It marks *exactly* where the HTTP calls go and what
    configuration they need, should a third provider ever be added.

    To implement (as a new named provider):
      1. Read the API key from ``ODDS_PROVIDER_API_KEY`` (see ``__init__``).
      2. In ``get_odds`` issue the game-odds request and map the response onto
         the dict shape documented in this module / produced by ``MockOddsAPI``
         (set ``source=<name>``, ``is_mock=False``; echo book / line_type /
         market_type / is_sharp_book through unchanged).
      3. In ``get_prop_odds`` do the same per (book, player, prop_stat); raise
         ``ValueError`` for an unknown ``prop_stat``.
    """

    #: env var the real integration reads its credentials from.
    API_KEY_ENV = "ODDS_PROVIDER_API_KEY"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get(self.API_KEY_ENV)

    def _not_configured(self) -> RuntimeError:
        return RuntimeError(
            "RealOddsAPIProvider is a SIM-370 stub and is not yet implemented. "
            f"To use a real odds feed: set {self.API_KEY_ENV} and implement the "
            "fetch in RealOddsAPIProvider.get_odds()/get_prop_odds() "
            "(map the provider response onto the MockOddsAPI dict shape; see "
            "pipeline/odds_provider.py for the contract). Until then keep "
            "ODDS_PROVIDER=mock (the default)."
        )

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        raise self._not_configured()

    def get_prop_odds(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        raise self._not_configured()

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        raise self._not_configured()

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        raise self._not_configured()


# ---------------------------------------------------------------------------
# Provider registry + factory
# ---------------------------------------------------------------------------

#: env var that selects which provider :func:`get_odds_provider` returns.
ODDS_PROVIDER_ENV = "ODDS_PROVIDER"
#: default provider name when the env var is unset / empty.
DEFAULT_PROVIDER = "mock"

# Registry of provider-name -> zero-arg factory.  Populated lazily for "mock"
# (to avoid importing the heavy live pipeline at module-import time) and
# eagerly for the real stub.  Tests register fakes via register_odds_provider().
_PROVIDER_FACTORIES: dict[str, Callable[[], OddsProvider]] = {}


def _make_mock_provider() -> OddsProvider:
    """Lazy factory for the default mock provider.

    Imported inside the function so ``pipeline.odds_provider`` stays importable
    even where the live pipeline's heavy deps (aiohttp, asyncpg, websockets…)
    are unavailable, and to avoid a circular import once the pipeline imports
    this module.
    """
    from pipeline.live.live_ingestion_pipeline import MockOddsAPI

    return MockOddsAPI()


def register_odds_provider(name: str, factory: Callable[[], OddsProvider]) -> None:
    """
    Register a named provider factory so ``ODDS_PROVIDER=<name>`` resolves to it.

    ``factory`` is a zero-arg callable returning an :class:`OddsProvider`.  Used
    by real providers at import time and by tests to inject a fake provider.
    Names are matched case-insensitively.
    """
    _PROVIDER_FACTORIES[name.strip().lower()] = factory


def _make_bettingpros_provider() -> OddsProvider:
    """Lazy factory for the SIM-405 BettingPros provider (stdlib-only import)."""
    from pipeline.bettingpros_odds_provider import BettingProsOddsProvider

    return BettingProsOddsProvider()


# Built-in providers.
register_odds_provider("mock", _make_mock_provider)
# SIM-405: "bettingpros" is the live BettingPros v3 integration; "real" now
# points to it (was the unimplemented SIM-370 stub) so ODDS_PROVIDER=real works.
register_odds_provider("bettingpros", _make_bettingpros_provider)
register_odds_provider("real", _make_bettingpros_provider)


def available_providers() -> list[str]:
    """Sorted list of registered provider names (for error messages / introspection)."""
    return sorted(_PROVIDER_FACTORIES)


def get_odds_provider(name: str | None = None) -> OddsProvider:
    """
    Return the configured :class:`OddsProvider`.

    Selection order:
      1. explicit ``name`` argument, if given;
      2. the ``ODDS_PROVIDER`` environment variable;
      3. ``"mock"`` (the deterministic default — unchanged behaviour).

    Raises
    ------
    RuntimeError
        If the selected name is not registered (lists the known names).
    """
    selected = (name or os.environ.get(ODDS_PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    factory = _PROVIDER_FACTORIES.get(selected)
    if factory is None:
        raise RuntimeError(
            f"Unknown ODDS_PROVIDER '{selected}'. Registered providers: "
            f"{', '.join(available_providers())}. Set {ODDS_PROVIDER_ENV} to one "
            "of these or register one via register_odds_provider()."
        )
    return factory()


__all__ = [
    "OddsProvider",
    "RealOddsAPIProvider",
    "get_odds_provider",
    "register_odds_provider",
    "available_providers",
    "ODDS_PROVIDER_ENV",
    "DEFAULT_PROVIDER",
    # SIM-421: the prop-market vocabulary
    "PROP_STATS",
    "PITCHER_PROP_STATS",
    "BATTER_PROP_STATS",
    "PROP_STAT_TO_MODEL_PROP",
    # SIM-421 (2026-09-12): the game-market vocabulary
    "GAME_MARKET_TYPES",
    "FULL_GAME_MARKET_TYPES",
    "LEGACY_GAME_MARKET_TYPES",
    "GAME_MARKET_KIND",
    "GAME_MARKET_SEGMENT",
    "GAME_MARKET_SIDE",
    "THREE_WAY_GAME_MARKET_TYPES",
    "GAME_ODDS_FIELDS",
    # SIM-555 (2026-09-28): the book vocabulary and the by-book seam
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
    "non_bettable_labels",
    "graded_row_order_sql",
    "odds_rows_by_book",
    "prop_rows_by_book",
]
