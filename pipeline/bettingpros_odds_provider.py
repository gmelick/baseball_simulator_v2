"""The real odds provider: an :class:`~pipeline.odds_provider.OddsProvider`
backed by the BettingPros v3 API (https://api.bettingpros.com/v3).

The class lives here with its settings, its public reads and the graded-book
choice; its parts live in ``pipeline/bettingpros/`` (the vendor-data rules, the
offer parsing, the schedule facts, the market ids, and the transport, event
match and row mixins).

Conforms to the structural ``OddsProvider`` protocol (``get_odds`` /
``get_prop_odds`` returning the ``MockOddsAPI`` dict shapes) so it is a drop-in:
set ``ODDS_PROVIDER=bettingpros`` (+ ``ODDS_API_KEY``) and the live pipeline /
betting routes use real lines instead of the deterministic mock.

How it bridges identifiers (the provider only receives ``game_pk`` / MLB
``player_id``, never names) — both via the public MLB Stats API, cached:
  * ``game_pk`` → BettingPros event: resolve the game's date + team names from
    the MLB schedule, then match the BettingPros ``events?date=…`` entry whose
    home/visitor nicknames suffix-match the MLB team names (double-headers
    disambiguated by scheduled time). Nickname-suffix matching avoids
    abbreviation-convention drift between the two APIs. See "Postponed,
    made-up and suspended games" below for the schedule's other entries.
  * MLB ``player_id`` → prop offer: resolve the player's name from the MLB
    people endpoint, then match the BettingPros prop offer participant by
    normalized first+last name.

Markets (discovered from /v3/markets?sport=MLB; the eight newer prop ids were
verified against the owner's scraper, the twelve segment ids against a live
event on 2026-09-12; the prop-market work, SIM-421):
  game:    moneyline 122, total 175, run-line 176
  segment: f1_moneyline 278, f5_moneyline 279 (three-way: home / away / draw),
           f1_total 280, f5_total 281, f1_runline 282, f5_runline 283,
           team_total_{home,away} 277 (one offer per team), f5_team_total 407,
           first_to_score 286, first_inning_run 369 (yes / no, stored as
           over / under at 0.5)
  pitcher: strikeouts 285, earned_runs 290, walks 408, outs_recorded 405,
           hits_allowed 404
  batter:  hits 287, home_runs 299, total_bases 293, rbis 289, singles 295,
           doubles 291, triples 292, runs 288, stolen_bases 294,
           hits_runs_rbis 403
``hits`` (287) is the batter market and ``hits_allowed`` (404) the pitcher
market; the vocabulary itself lives in ``pipeline/odds_provider.py``.

One row per book (SIM-555)
--------------------------
One ``/offers`` payload lists every book's line on every side of a market. The
provider returns one row per book: :meth:`get_odds_by_book` /
:meth:`get_prop_odds_by_book`. Each row holds ONE book's prices for every side,
names the book as ``book = 'bp:<id>'`` and carries the vendor's stamp of the
row's line in ``book_line_at`` (a timezone-aware UTC datetime).

  * ``line_type="opening"``: one row at most, the opener's. The feed names ONE
    opening book per side (``opening_line.book_id``); every required side must
    name the same book, or the market gets no opening row. A three-way market's
    tie joins the row only when the same book opened it. The stamp is the
    newest ``created`` of the row's sides.
  * any other line type (``closing``, ``current``): one row per book that holds
    a usable line on every required side, in the payload's book order, the
    vendor's blend (id 0) included as ``bp:0``. A line flagged ``is_off`` or
    inactive, or without a price, is not a quote. The stamp is the newest
    ``updated`` of the row's sides. Since 2025 the vendor stamps a snapshot at
    game time, so a closing stamp sits 0-5 minutes after the scheduled start.
  * Each market fills only its own columns (the three full-game markets do
    not share one row). A total-kind or yes / no row also carries
    ``over_line`` / ``under_line``, the two sides' own lines, for the load
    guard (``pipeline/odds_row_guard.py``); ``scheduled_start``,
    ``game_date`` and ``postponed_start`` (a made-up game's original start,
    else ``None``) ride along for the guard and the logs, and are never stored.
  * The first-five markets (283 run line, 281 total, 279 moneyline) drop two
    kinds of book entry that carry first-inning prices: a book on
    :data:`F5_EXCLUDED_BOOKS` from its date, on the markets
    :data:`F5_EXCLUDED_MARKETS` lists for it (DraftKings from 2025-03-01, on
    the run line and the total; its first-five moneyline is kept, and the load
    guard refuses its bad rows), and a
    book whose first-five lines copy its own first-inning lines (the "twin"
    rule, within :data:`TWIN_PRICE_TOLERANCE` of implied probability on the
    run line and the total; identical prices on the moneyline, whose two
    markets share the line 1, so a near-even game's real prices are not taken
    for a copy). On the three-way moneyline the tie counts too: a first-five tie that the
    first-inning entry lacks, or that sits beyond the tolerance of the
    first-inning tie, shows the entry is not a copy. The opening row falls when
    its opener is excluded by either rule, or when the opener copies its own
    first-inning opener. The twin rule needs the first-inning market: when that
    read fails, the first-five market gets no rows at all (a WARNING names the
    game), because an unchecked row may be a first-inning bet.

:meth:`get_odds` / :meth:`get_prop_odds` keep the one-row protocol: ``book``
names a book (``'draftkings'``, ``'bp:12'``) and returns that book's row, or an
empty row; the default ``'consensus'`` returns the row of the first book on
``GRADED_BOOK_PREFERENCE`` that has one (then any other sportsbook). The blend,
the daily-fantasy apps, the exchanges and the prediction markets are never the
default, with or without a pin. ``prefer_book_id`` pins every call to that one
book; a pinned non-sportsbook is readable only by naming it (``book='bp:0'``).

The offers cache
----------------
One ``/offers`` response for an (event_id, market_id) pair carries EVERY
player's offer for that market, and the same response serves every book and
both the opening and the closing line (each line type is a different field of
the same selection). Without a cache the provider would fetch it again for
every (player, market, book, line type): several hundred HTTP calls a game
where 15 do. The provider keeps a per-instance cache keyed
``(event_id, market_id)`` with a time-to-live, plus a per-date cache of the
``/events`` list (every game of a date shares one).

The time-to-live matters because the live pipeline keeps ONE provider for the
process lifetime and fetches props every ``PROP_FETCH_CADENCE_S`` (60) seconds:
a cached snapshot must never be older than that cadence, or a cycle would
persist a stale line as if it were current. The default is 30 seconds (half the
cadence): long enough to collapse one cycle's calls into one fetch per market,
short enough that the next cycle always sees a fresh snapshot. Set it with the
``offers_cache_ttl_s`` constructor argument or the ``ODDS_OFFERS_CACHE_TTL_S``
environment variable (0 disables the cache); the offline historical loader
uses a longer value because its games are over and their lines are final. The
clock is ``time.monotonic()``, injectable for tests.

Both caches evict. A finished game's (event, market) key is never read again,
so a cache that only overwrote keys would hold every payload of a season-long
process (the live pipeline) or of a whole-season backfill (the historical
loader: 2,378 games × 18 markets, several MB per game, inside the app
container's 10 GB memory cap). Each store first drops every entry past the
time-to-live, so a cache holds at most one time-to-live's worth of payloads;
a time-to-live of 0 stores nothing.

A ``/offers`` market is paged. A failure on page 1 raises to the caller. A
failure on a later page logs a warning that names the event, the market and
the page, and returns the pages already in hand, so the players on page 1
keep their quotes. Neither result is cached: the next call fetches again.

A failed game, event or player lookup is kept for one time-to-live,
never for the process lifetime. A found game, event or player name is kept for
the process lifetime. So the live pipeline finds a game that BettingPros lists
later, and one failed lookup costs one read per time-to-live, not one read per
market and player (the loader asks about 410 times per game).

Postponed, made-up and suspended games
--------------------------------------
MLB lists a game once per date it touched. A game postponed and made up later
has a postponed entry (its ``gameDate`` the original start) and a played
entry. A suspended game has two "Final" entries: the original first pitch and
the resumption. The matcher reads the PLAYED entry (the first one not
postponed) and searches the vendor's slate of each later entry too, because
the vendor may move a suspended game to its resumption date. An event's gap
is its distance to the nearest start of the game. A gap of 2 hours or less
matches. The single-event rule takes a larger gap, up to 12 hours, when the
event is the two teams' only event on the played date and the game is not a
double-header: the vendor kept another start time. Measured against a
made-up game's ORIGINAL start, the vendor's events would be declined (about
350 games of 2019-2026 in the loader's logs, the main cause of a game with no
odds at all). Every row of a
made-up game carries ``postponed_start``; the load guard refuses a price
stamped at or before it (bet365 kept the price of the unplayed game).

Game 2 of a straight double-header has no listed start: MLB sets
``startTimeTBD`` and a placeholder start 5 minutes after game 1's, which sits
nearer game 1's event than game 2's. The matcher takes the later of the two
teams' two events on the played date, and the rows measure their stamps from
that event's start; matched by its start alone, game 2 would get game 1's
event and game 1's prices.

HTTP is stdlib ``urllib`` (sync — the protocol methods are sync; the module
stays importable without aiohttp). The two ``_bp_get`` / ``_mlb_get`` seams are
the only network surface and are stubbed in unit tests (fixtures captured under
``tests/fixtures/bettingpros/``); no live call is made in tests.

Retries and failed reads
------------------------
Every vendor and MLB read goes through :meth:`BettingProsOddsProvider._http_get_json`.
That method retries a transient failure: an HTTP 429 or 5xx, a time-out, a
refused or dropped connection, an unreachable network, a TLS error. The wait
before retry k is ``retry_wait_s`` times 2 to the power k − 1, at most 60
seconds; a 429 or 503 with a numeric ``Retry-After`` waits at least that long.
Any other failure (an HTTP 4xx, a body that is not JSON) raises at once. After
the last retry the original error reaches the caller. The default is no retry
(``BETTINGPROS_MAX_RETRIES`` unset = 0), so the live pipeline and the
opening-line job keep their single attempt; the historical loader turns
retries on (:meth:`set_retry_policy`).

Retries are for offline jobs only. Leave ``BETTINGPROS_MAX_RETRIES`` unset in
the API container. The live pipeline and the opening-line job call this
provider on the app's event loop, and a retry waits with ``time.sleep``. One
wait stops the whole app: the WebSocket fan-out, ``/simulate``, the health
checks.

A read that still fails is caught by the caller, logged and turned into an
empty or partial result. The provider counts each such catch in
``read_failures``, so the loader can tell a game a vendor error cut short
from a game with nothing to load, and keep the first off its done-list. The
loader also calls :meth:`BettingProsOddsProvider.forget_failed_lookups`
before each game, so a failure stored in one game never cuts the next one
short without a count.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pipeline.bettingpros.base import ProviderBase
from pipeline.bettingpros.rows import OfferRowsMixin
from pipeline.bettingpros.schedule import _ScheduleInfo
from pipeline.odds_provider import (
    GRADED_BOOK_PREFERENCE,
    book_kind,
    resolve_book,
)

log = logging.getLogger("pipeline.bettingpros_odds_provider")


class BettingProsOddsProvider(OfferRowsMixin, ProviderBase):
    """Real odds provider backed by BettingPros v3.

    ``offers_cache_ttl_s`` is the time-to-live of the offers and the
    per-date events caches, in seconds. ``None`` reads ``ODDS_OFFERS_CACHE_TTL_S``
    and falls back to 30 seconds — half the live pipeline's 60-second prop
    cadence, so a cycle never persists a stale snapshot (see the module
    docstring). ``0`` disables the caches. ``clock`` is the monotonic time
    source, injectable for tests.

    ``max_retries`` and ``retry_wait_s`` set how often a read that
    fails for a transient reason is retried, and the first wait in seconds
    (see :meth:`_http_get_json`). ``None`` reads ``BETTINGPROS_MAX_RETRIES``
    (default 0: one attempt, no retry) and ``BETTINGPROS_RETRY_WAIT_S``
    (default 2.0). :meth:`set_retry_policy` changes both later. ``sleep`` is
    the wait function, injectable for tests. Set retries for an offline job
    only. The live pipeline and the opening-line job call this provider on the
    API's event loop, and a retry's ``sleep`` blocks that loop. So leave
    ``BETTINGPROS_MAX_RETRIES`` unset in the API container.

    ``read_failures`` counts the reads that failed after their
    retries and that the provider caught, to go on with an empty or partial
    result: a game, event or player lookup, the extra slate of a resumed game,
    an offers read, a later offers page, the first-inning read of the twin
    check. It grows by one per caught failure. A legitimate absence does not
    count: no event on the slate, a matcher decline, a slate the matcher
    cannot read (a retry reads the same answer), a schedule or people answer
    without the game or the player, an empty offer list, a player with no
    offer, a first-five exclusion, a failed lookup still inside its
    time-to-live (counted once, when it failed). The historical loader reads the count before and after a game; a
    game whose count grew stays off the done-list. The loader calls
    :meth:`forget_failed_lookups` before each game, so a failure stored in one
    game is read again, and counted again, in the next.
    """

    API_KEY_ENV = "ODDS_API_KEY"
    #: Env var that sets how many times a transient read failure is retried.
    MAX_RETRIES_ENV = "BETTINGPROS_MAX_RETRIES"
    #: Env var that sets the wait before the first retry (seconds).
    RETRY_WAIT_ENV = "BETTINGPROS_RETRY_WAIT_S"
    #: No retry by default; the live pipeline keeps its single attempt.
    DEFAULT_MAX_RETRIES = 0
    #: The default wait before the first retry (seconds); it doubles per retry.
    DEFAULT_RETRY_WAIT_S = 2.0
    #: The longest wait before one retry, a ``Retry-After`` included (seconds).
    RETRY_WAIT_CAP_S = 60.0
    #: Env var that sets the offers-cache time-to-live (seconds).
    OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"
    #: The default time-to-live — half PROP_FETCH_CADENCE_S (60 s).
    DEFAULT_OFFERS_CACHE_TTL_S = 30.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        prefer_book_id: int | None = None,
        timeout: float = 15.0,
        offers_cache_ttl_s: float | None = None,
        clock: Callable[[], float] | None = None,
        max_retries: int | None = None,
        retry_wait_s: float | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.API_KEY_ENV)
        self._prefer_book_id = prefer_book_id
        self._timeout = timeout
        if offers_cache_ttl_s is None:
            offers_cache_ttl_s = float(
                os.environ.get(self.OFFERS_CACHE_TTL_ENV, self.DEFAULT_OFFERS_CACHE_TTL_S)
            )
        self._offers_cache_ttl_s = float(offers_cache_ttl_s)
        self._clock: Callable[[], float] = clock or time.monotonic
        # The retry policy of _http_get_json and the count of caught read failures.
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
        # Per-instance caches (cleared by constructing a new provider).
        # The game, event and player-name caches hold a found value
        # for the process lifetime. A failed lookup goes to _failed_lookups for
        # one time-to-live only: a long-lived process (the live pipeline,
        # polling from the morning) finds a game that BettingPros lists later,
        # and one failure costs one read per time-to-live, not one per call
        # (the loader asks about 410 times per game).
        self._event_cache: dict[int, dict[str, Any]] = {}
        self._game_meta_cache: dict[int, tuple[str, str, str, datetime | None]] = {}
        # The schedule facts of a resolved game (the played entry, the
        # resume slates, the postponed start), filled in the same read as the meta.
        self._game_schedule_cache: dict[int, _ScheduleInfo] = {}
        self._player_name_cache: dict[int, str] = {}
        self._failed_lookups: dict[tuple[str, int], tuple[float, None]] = {}
        # Time-stamped caches — (stored_at, payload), served while
        # younger than the time-to-live; every store drops the stale entries.
        self._offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}
        self._events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # The first-five exclusions already logged, per (event, market, book).
        self._f5_logged: set[tuple[int, int, str]] = set()

    def _choose_row(self, rows: list[dict[str, Any]], book: str | None) -> dict[str, Any] | None:
        """The one row the one-row methods return (see :meth:`get_odds`).

        The default never returns the blend or another non-sportsbook kind,
        with or without a pin: a pinned non-sportsbook is readable only by
        naming it.
        """
        if book is None or book == "consensus":
            by_id: dict[int, dict[str, Any]] = {}
            for row in rows:
                by_id.setdefault(row["book_id"], row)
            for book_id in GRADED_BOOK_PREFERENCE:
                if book_id in by_id:
                    return by_id[book_id]
            for row in rows:
                if book_kind(row["book"]) == "sportsbook":
                    return row
            return None
        book_id = resolve_book(book)
        if book_id is None:
            return None
        return next((row for row in rows if row["book_id"] == book_id), None)

    # ----------------------------------------------------------------- get_odds
    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """One row per book for one game market and line type.

        ``market_type`` is any value of ``GAME_MARKET_TYPES`` (``run_line`` is
        accepted as the legacy alias of ``runline`` and echoed as asked); an
        unknown value raises ``ValueError``. ``'opening'`` gives the opener's one
        row, or none when the sides name two openers or the opener is excluded;
        any other line type gives one row per book quoting every side, the
        vendor's blend (``bp:0``) included, the excluded first-five books left
        out. Each row carries every ``get_odds`` key plus ``book_id``,
        ``book_line_at``, ``scheduled_start``, ``game_date`` and
        ``postponed_start`` (a made-up game's original start, else
        ``None``), and ``over_line`` / ``under_line`` on a total-kind or
        yes / no market. An unresolvable game gives an empty list.
        """
        _base, rows = self._game_market_rows(game_pk, line_type, market_type)
        return rows

    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """One row for ``game_pk`` in the MockOddsAPI dict shape.

        ``book`` names a book (``'draftkings'``, ``'DraftKings'``, ``'bp:12'``):
        that book's row from :meth:`get_odds_by_book`, or an empty row (every
        odds field ``None``, ``book`` the requested value) when the book has none
        or the name is unknown. The default ``'consensus'`` gives the row of the
        first book on ``GRADED_BOOK_PREFERENCE`` that has one, then any other
        sportsbook in the payload's order; the vendor's blend and the other
        non-sportsbook kinds are never the default. On ``'opening'`` that is the
        opener's row. ``is_sharp_book`` is echoed. The row keeps the by-book
        keys (``book_id``, ``book_line_at``, ...).
        """
        base, rows = self._game_market_rows(game_pk, line_type, market_type)
        chosen = self._choose_row(rows, book)
        if chosen is None:
            result = dict(base)
            result["book"] = book if book is not None else "consensus"
        else:
            result = dict(chosen)
        result["is_sharp_book"] = is_sharp_book
        return result

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """One row per book for one player's prop market.

        The same rules as :meth:`get_odds_by_book` over the offer's over and
        under. ``line`` is the over's line (the under's when the over has none);
        ``over_line`` / ``under_line`` carry both for the load guard. Raises
        ``ValueError`` for an unknown ``prop_stat``; an unresolvable game or
        player, or a player with no offer, gives an empty list.
        """
        _base, rows = self._prop_market_rows(game_pk, player_id, prop_stat, line_type)
        return rows

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
        """One player-prop quote in the MockOddsAPI shape.

        ``book`` picks the row as in :meth:`get_odds`. Raises ``ValueError`` for
        an unknown ``prop_stat`` (mirrors the mock). ``line`` / ``over_ml`` /
        ``under_ml`` are ``None`` when the player or the book has no offer.
        """
        base, rows = self._prop_market_rows(game_pk, player_id, prop_stat, line_type)
        chosen = self._choose_row(rows, book)
        if chosen is None:
            result = dict(base)
            result["book"] = book if book is not None else "consensus"
        else:
            result = dict(chosen)
        result["is_sharp_book"] = is_sharp_book
        return result


__all__ = [
    "BettingProsOddsProvider",
]
