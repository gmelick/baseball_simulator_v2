"""The BettingPros odds provider: every book's prices for a game, from the v3 API.

Set ``ODDS_PROVIDER=bettingpros`` and ``ODDS_API_KEY`` to use it. The class is
built from the parts in this folder: ``transport`` (the HTTP reads),
``events`` (the game-to-event match and the caches) and ``rows`` (the rows).

**The reads.** :meth:`get_odds_by_book` and :meth:`get_prop_odds_by_book` give
one row per book. A row holds one book's prices for every side of one market
and names the book as ``'bp:<id>'``. :meth:`get_odds` gives the graded book's
one row, which the live service broadcasts.

* An opening read gives at most one row: the opener's. Every side must name
  the same opening book, or the market gets no opening row.
* Any other read gives one row per book that prices every side, the vendor's
  blended line (``bp:0``) included. A line flagged off or inactive is skipped.
* Each row carries the vendor's stamp of its line (``book_line_at``), and the
  game facts the load guard needs (``scheduled_start``, ``game_date``,
  ``postponed_start``). Those facts are never stored.

**The match.** The provider gets only MLB ids. It reads the game's date and
teams from the MLB schedule and matches the BettingPros event by team
nickname and start time. It matches a player by name from the MLB people
endpoint. ``events.py`` covers double-headers, postponed and suspended games.

**The caches.** One ``/offers`` response serves every player, every book and
both line types of one market. The provider caches it for
``offers_cache_ttl_s`` seconds (default 30, half the live pipeline's 60-second
prop cadence, so a cycle never stores a stale line; 0 turns the cache off).
Found games, events and players stay cached for the process lifetime. A
failed lookup stays cached for one time-to-live, so a game listed later is
still found.

**Retries.** A transient failure (HTTP 429 or 5xx, a time-out, a dropped
connection) is retried ``max_retries`` times with a doubling wait. The default
is 0. Turn retries on only in an offline job. A retry waits with
``time.sleep``: the opening-line job would stall the app's event loop, and the
live pipeline would stall its one odds thread.
A read that still fails is logged, counted in ``read_failures`` and returns an
empty or partial result.

No test makes a network call: the tests stub ``_bp_get`` and ``_mlb_get``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pipeline.bettingpros.base import ProviderBase
from pipeline.bettingpros.rows import OfferRowsMixin
from pipeline.bettingpros.schedule import _ScheduleInfo
from pipeline.odds_provider import GAME_ODDS_FIELDS, GRADED_BOOK_PREFERENCE, book_kind


class BettingProsOddsProvider(OfferRowsMixin, ProviderBase):
    """The BettingPros odds provider.

    ``offers_cache_ttl_s`` is the cache lifetime in seconds; ``None`` reads
    ``ODDS_OFFERS_CACHE_TTL_S`` (default 30). ``max_retries`` and
    ``retry_wait_s`` set the retries and the first wait; ``None`` reads
    ``BETTINGPROS_MAX_RETRIES`` (default 0) and ``BETTINGPROS_RETRY_WAIT_S``
    (default 2.0). ``clock`` and ``sleep`` are injectable for tests.

    ``read_failures`` counts each failed read the provider caught. A normal
    absence (no event, no offer, a matcher decline) does not count. The
    historical loader keeps a game whose count grew off its done-list.
    """

    API_KEY_ENV = "ODDS_API_KEY"
    MAX_RETRIES_ENV = "BETTINGPROS_MAX_RETRIES"
    RETRY_WAIT_ENV = "BETTINGPROS_RETRY_WAIT_S"
    DEFAULT_MAX_RETRIES = 0
    #: The first wait in seconds; it doubles per retry.
    DEFAULT_RETRY_WAIT_S = 2.0
    #: The longest wait before one retry, a ``Retry-After`` included.
    RETRY_WAIT_CAP_S = 60.0
    OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"
    DEFAULT_OFFERS_CACHE_TTL_S = 30.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 15.0,
        offers_cache_ttl_s: float | None = None,
        clock: Callable[[], float] | None = None,
        max_retries: int | None = None,
        retry_wait_s: float | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.API_KEY_ENV)
        self._timeout = timeout
        if offers_cache_ttl_s is None:
            offers_cache_ttl_s = float(
                os.environ.get(self.OFFERS_CACHE_TTL_ENV, self.DEFAULT_OFFERS_CACHE_TTL_S)
            )
        self._offers_cache_ttl_s = float(offers_cache_ttl_s)
        self._clock: Callable[[], float] = clock or time.monotonic
        if max_retries is None:
            max_retries = int(os.environ.get(self.MAX_RETRIES_ENV, self.DEFAULT_MAX_RETRIES))
        if retry_wait_s is None:
            retry_wait_s = float(os.environ.get(self.RETRY_WAIT_ENV, self.DEFAULT_RETRY_WAIT_S))
        self._max_retries: int = self.DEFAULT_MAX_RETRIES
        self._retry_wait_s: float = self.DEFAULT_RETRY_WAIT_S
        self.set_retry_policy(max_retries, retry_wait_s)
        # Not _sleep: the census probe's subclass uses that name for its rate limit.
        self._retry_sleep: Callable[[float], None] = sleep or time.sleep
        self.read_failures = 0

        # Found lookups, kept for the process lifetime.
        self._event_cache: dict[int, dict[str, Any]] = {}
        self._game_meta_cache: dict[int, tuple[str, str, str, datetime | None]] = {}
        self._game_schedule_cache: dict[int, _ScheduleInfo] = {}
        self._player_name_cache: dict[int, str] = {}
        # Failed lookups and payloads, kept for one time-to-live: (stored_at, payload).
        self._failed_lookups: dict[tuple[str, int], tuple[float, None]] = {}
        self._offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}
        self._events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # The first-five exclusions already logged, per (event, market, book).
        self._f5_logged: set[tuple[int, int, str]] = set()

    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """One row per book for one game market and line type.

        ``market_type`` is a value of ``GAME_MARKET_TYPES``; another value
        raises ``ValueError``. A game with no match gives an empty list.
        """
        return self._game_market_rows(game_pk, line_type, market_type)

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> dict[str, Any]:
        """The graded book's row of one game market: the row the live service broadcasts.

        The graded book is the first book on ``GRADED_BOOK_PREFERENCE`` with a
        row, else the first other sportsbook. The blend, the fantasy apps and
        the exchanges never count. With no such row, every price is ``None``.
        """
        rows = self._game_market_rows(game_pk, line_type, market_type)
        by_id = {row["book_id"]: row for row in reversed(rows)}
        for book_id in GRADED_BOOK_PREFERENCE:
            if book_id in by_id:
                return dict(by_id[book_id])
        for row in rows:
            if book_kind(row["book"]) == "sportsbook":
                return dict(row)
        empty: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "bettingpros",
            "is_mock": False,
            "book": "consensus",
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": False,
        }
        empty.update(dict.fromkeys(GAME_ODDS_FIELDS))
        return empty

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """One row per book for one player's prop market.

        ``line`` is the over's line, or the under's when the over has none.
        An unknown ``prop_stat`` raises ``ValueError``. A game or player with
        no match, or a player with no offer, gives an empty list.
        """
        return self._prop_market_rows(game_pk, player_id, prop_stat, line_type)


__all__ = [
    "BettingProsOddsProvider",
]
