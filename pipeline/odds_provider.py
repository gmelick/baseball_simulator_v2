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
  * ``book`` is echoed through verbatim — the pipeline iterates ``PROP_BOOKS``
    (and per-game book lists) and expects one quote per (book, player, prop).
  * ``is_sharp_book`` is carried through unchanged so the CLV engine can split
    sharp (Pinnacle, Circa) vs. soft (DraftKings, FanDuel) lines.
  * ``line_type`` is one of ``opening | current | closing | bet_placement`` and
    is echoed through so opening/closing-line capture stamps the right value.
  * Real providers should set ``source=<provider-name>`` and ``is_mock=False``.

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

#: The three full-game markets the platform stored before 2026-09-12. A row for
#: one of these carries ALL three markets' columns (the provider fills every
#: full-game field it can resolve), which is what the stored dedup hashes
#: expect; keep that behaviour for these three.
LEGACY_GAME_MARKET_TYPES: tuple[str, ...] = ("moneyline", "runline", "total")

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
# Provider interface
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """
    Structural interface for an odds/prop source consumed by the live pipeline.

    ``@runtime_checkable`` so ``isinstance(obj, OddsProvider)`` works as a
    duck-typed presence check (it verifies the methods exist, not their
    signatures — the return-shape contract is enforced by tests).  ``MockOddsAPI``
    satisfies this implicitly: its ``get_odds`` / ``get_prop_odds`` staticmethods
    are callable on an *instance*, which is how the pipeline invokes them.
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
    "LEGACY_GAME_MARKET_TYPES",
    "GAME_MARKET_KIND",
    "GAME_MARKET_SEGMENT",
    "GAME_MARKET_SIDE",
    "THREE_WAY_GAME_MARKET_TYPES",
    "GAME_ODDS_FIELDS",
]
