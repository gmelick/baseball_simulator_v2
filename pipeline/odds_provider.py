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
    is_sharp_book, home_ml, away_ml, home_spread, home_spread_ml, away_spread,
    away_spread_ml, total_line, over_ml, under_ml``.

``get_prop_odds(game_pk, player_id, prop_stat, *, line_type, book,
is_sharp_book) -> dict``
    Keys: ``game_pk, player_id, prop_stat, line, over_ml, under_ml, book,
    line_type, is_sharp_book, source, is_mock``.  Must raise ``ValueError`` on
    an unknown ``prop_stat`` (the 7 Betting-Analyst markets in
    ``PROP_STATS``), mirroring ``MockOddsAPI``.

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
    SIM-370 stub for a real odds/prop provider (e.g. The Odds API).

    Conforms to :class:`OddsProvider` so it is a structural drop-in, but every
    method raises until a live integration is actually wired.  There is no real
    odds feed in this environment; this class marks *exactly* where the HTTP
    calls go and what configuration they need.

    To implement:
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
]
