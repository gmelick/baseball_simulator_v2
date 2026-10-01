"""
pipeline/bettingpros_odds_provider.py
=====================================
SIM-405 — a real :class:`~pipeline.odds_provider.OddsProvider` backed by the
BettingPros v3 API (https://api.bettingpros.com/v3), replacing the SIM-370 stub.

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
    abbreviation-convention drift between the two APIs.
  * MLB ``player_id`` → prop offer: resolve the player's name from the MLB
    people endpoint, then match the BettingPros prop offer participant by
    normalized first+last name.

Markets (discovered from /v3/markets?sport=MLB; the eight SIM-421 prop ids
were verified against the owner's scraper, the twelve segment ids against a
live event on 2026-09-12):
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

One row per book (SIM-555, 2026-09-28)
--------------------------------------
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
  * Each market fills only its own columns (the three full-game markets no
    longer share one row). A total-kind or yes / no row also carries
    ``over_line`` / ``under_line``, the two sides' own lines, for the load
    guard (``pipeline/odds_row_guard.py``); ``scheduled_start`` and
    ``game_date`` ride along for the guard and the logs, and are never stored.
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

The offers cache (SIM-421)
--------------------------
One ``/offers`` response for an (event_id, market_id) pair carries EVERY
player's offer for that market, and the same response serves every book and
both the opening and the closing line (each line type is a different field of
the same selection). Before SIM-421 the provider fetched it again for every
(player, market, book, line type) — several hundred HTTP calls per game where
15 would do. The provider now keeps a per-instance cache keyed
``(event_id, market_id)`` with a time-to-live, plus a per-date cache of the
``/events`` list (``_resolve_event`` used to re-fetch the whole day's slate for
every game on that date).

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

A failed game, event or player lookup (SIM-555) is kept for one time-to-live,
never for the process lifetime. A found game, event or player name is kept for
the process lifetime. So the live pipeline finds a game that BettingPros lists
later, and one failed lookup costs one read per time-to-live, not one read per
market and player (the loader asks about 410 times per game).

HTTP is stdlib ``urllib`` (sync — the protocol methods are sync; the module
stays importable without aiohttp). The two ``_bp_get`` / ``_mlb_get`` seams are
the only network surface and are stubbed in unit tests (fixtures captured under
``tests/fixtures/bettingpros/``); no live call is made in tests.

Retries and failed reads (SIM-555, 2026-10-01)
----------------------------------------------
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

import http.client
import json
import logging
import math
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    GRADED_BOOK_PREFERENCE,
    PROP_STATS,
    book_kind,
    book_label,
    resolve_book,
)
from pipeline.odds_row_guard import implied_probability

_K = TypeVar("_K")
_V = TypeVar("_V")

log = logging.getLogger("pipeline.bettingpros_odds_provider")

_BP_BASE = "https://api.bettingpros.com/v3"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"

#: SIM-536: the largest gap allowed between the actual game's start time and the
#: BettingPros event we matched it to. A match beyond this is treated as no
#: match at all, rather than silently priced against the wrong game.
_MAX_EVENT_TIME_DELTA = timedelta(hours=2)

#: SIM-421: the most ``/offers`` pages read for one (event, market). The API
#: pages at 10 offers; a batter market lists every hitter in the game (two to
#: three pages), so the cap only guards against a runaway pager.
_MAX_OFFER_PAGES = 20

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


#: market_type (the GAME_MARKET_TYPES vocabulary) → BettingPros market id.
#: ``run_line`` is a legacy alias of ``runline``. Both team-total markets of a
#: side pair share one BettingPros market: the response holds one offer per
#: team, and :meth:`_team_offer_selections` picks the side's offer.
_GAME_MARKET_IDS: dict[str, int] = {
    "moneyline": 122,
    "total": 175,
    "runline": 176,
    "run_line": 176,
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
if set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS):
    raise RuntimeError(
        "bettingpros_odds_provider: _GAME_MARKET_IDS lacks a market id for "
        f"{sorted(set(GAME_MARKET_TYPES) - set(_GAME_MARKET_IDS))} — keep it in step "
        "with pipeline.odds_provider.GAME_MARKET_TYPES"
    )

#: prop_stat (the PROP_STATS vocabulary) → BettingPros market id.
_PROP_MARKET_IDS: dict[str, int] = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
    # SIM-421: the eight markets the book already offers.
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
        "BettingPros market ids and PROP_STATS disagree: "
        f"{sorted(set(_PROP_MARKET_IDS) ^ set(PROP_STATS))}"
    )


def _normalize_name(name: str) -> str:
    """Lower-case, strip accents + non-alphanumerics, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in ascii_only)
    return " ".join(kept.lower().split())


#: SIM-555: the error types a read may retry. ``OSError`` is the base of
#: ``URLError`` (a time-out or a refused connection inside ``urlopen``; also
#: the base of ``HTTPError``, whose status :func:`_is_transient` checks), of
#: ``TimeoutError`` (``socket.timeout`` since Python 3.10) and of
#: ``ConnectionError``. ``OSError`` also covers what urllib does not wrap.
#: urllib wraps an ``OSError`` in ``URLError`` only around the request. An
#: error raised while the status line or the body is read comes through bare:
#: an ``ssl.SSLError``, or an ``EHOSTUNREACH``, ``ENETUNREACH`` or ``ENETDOWN``.
#: ``HTTPException`` covers ``RemoteDisconnected`` and ``IncompleteRead``.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (OSError, http.client.HTTPException)

#: SIM-555: the statuses whose numeric ``Retry-After`` sets a floor on the wait.
_RETRY_AFTER_STATUSES = frozenset({429, 503})


def _is_transient(exc: BaseException) -> bool:
    """True when a failed read may succeed on a later attempt (SIM-555).

    An HTTP 429 or 5xx, a time-out, a refused or dropped connection, an
    unreachable network, a TLS error. Any other HTTP status (a 4xx) is the
    server's final answer.
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code if isinstance(exc.code, int) else 0
        return code == 429 or code >= 500
    return isinstance(exc, _RETRYABLE_ERRORS)


def _retry_after_s(exc: BaseException) -> float | None:
    """The numeric ``Retry-After`` of a 429 or 503, in seconds, or ``None`` (SIM-555).

    ``None`` when the status is another one, or the header is absent, an
    HTTP date, negative or not finite.
    """
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in _RETRY_AFTER_STATUSES:
        return None
    headers = exc.headers
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _log_target(url: str) -> str:
    """The host and path of a URL, for a log line: no query string, no user part (SIM-555)."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname or ''}{parts.path}"


class BettingProsOddsProvider:
    """Real odds provider backed by BettingPros v3 (SIM-405).

    ``offers_cache_ttl_s`` (SIM-421) is the time-to-live of the offers and the
    per-date events caches, in seconds. ``None`` reads ``ODDS_OFFERS_CACHE_TTL_S``
    and falls back to 30 seconds — half the live pipeline's 60-second prop
    cadence, so a cycle never persists a stale snapshot (see the module
    docstring). ``0`` disables the caches. ``clock`` is the monotonic time
    source, injectable for tests.

    ``max_retries`` and ``retry_wait_s`` (SIM-555) set how often a read that
    fails for a transient reason is retried, and the first wait in seconds
    (see :meth:`_http_get_json`). ``None`` reads ``BETTINGPROS_MAX_RETRIES``
    (default 0: one attempt, no retry) and ``BETTINGPROS_RETRY_WAIT_S``
    (default 2.0). :meth:`set_retry_policy` changes both later. ``sleep`` is
    the wait function, injectable for tests. Set retries for an offline job
    only. The live pipeline and the opening-line job call this provider on the
    API's event loop, and a retry's ``sleep`` blocks that loop. So leave
    ``BETTINGPROS_MAX_RETRIES`` unset in the API container.

    ``read_failures`` (SIM-555) counts the reads that failed after their
    retries and that the provider caught, to go on with an empty or partial
    result: a game, event or player lookup, an offers read, a later offers
    page, the first-inning read of the twin check. It grows by one per caught
    failure. A legitimate absence does not count: no event on the slate, a
    matcher decline, a slate the matcher cannot read (a retry reads the same
    answer), a schedule or people answer without the game or the player, an
    empty offer list, a player with no offer, a first-five exclusion, a
    failed lookup still inside its time-to-live (counted once, when it
    failed). The historical loader reads the count before and after a game; a
    game whose count grew stays off the done-list. The loader calls
    :meth:`forget_failed_lookups` before each game, so a failure stored in one
    game is read again, and counted again, in the next.
    """

    API_KEY_ENV = "ODDS_API_KEY"
    #: SIM-555: env var that sets how many times a transient read failure is retried.
    MAX_RETRIES_ENV = "BETTINGPROS_MAX_RETRIES"
    #: SIM-555: env var that sets the wait before the first retry (seconds).
    RETRY_WAIT_ENV = "BETTINGPROS_RETRY_WAIT_S"
    #: SIM-555: no retry by default; the live pipeline keeps its single attempt.
    DEFAULT_MAX_RETRIES = 0
    #: SIM-555: the default wait before the first retry (seconds); it doubles per retry.
    DEFAULT_RETRY_WAIT_S = 2.0
    #: SIM-555: the longest wait before one retry, a ``Retry-After`` included (seconds).
    RETRY_WAIT_CAP_S = 60.0
    #: SIM-421: env var that sets the offers-cache time-to-live (seconds).
    OFFERS_CACHE_TTL_ENV = "ODDS_OFFERS_CACHE_TTL_S"
    #: SIM-421: the default time-to-live — half PROP_FETCH_CADENCE_S (60 s).
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
        # SIM-555: the retry policy of _http_get_json and the count of caught read failures.
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
        # SIM-555: the game, event and player-name caches hold a found value
        # for the process lifetime. A failed lookup goes to _failed_lookups for
        # one time-to-live only: a long-lived process (the live pipeline,
        # polling from the morning) finds a game that BettingPros lists later,
        # and one failure costs one read per time-to-live, not one per call
        # (the loader asks about 410 times per game).
        self._event_cache: dict[int, dict[str, Any]] = {}
        self._game_meta_cache: dict[int, tuple[str, str, str, datetime | None]] = {}
        self._player_name_cache: dict[int, str] = {}
        self._failed_lookups: dict[tuple[str, int], tuple[float, None]] = {}
        # SIM-421: time-stamped caches — (stored_at, payload), served while
        # younger than the time-to-live; every store drops the stale entries.
        self._offers_cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}
        self._events_by_date_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # SIM-555: the first-five exclusions already logged, per (event, market, book).
        self._f5_logged: set[tuple[int, int, str]] = set()

    # ----------------------------------------------------------------- HTTP
    def set_retry_policy(self, max_retries: int, retry_wait_s: float) -> None:
        """Set the retries of a transient read failure and the first wait (SIM-555).

        ``max_retries`` is a whole number 0 or more (0 = one attempt);
        ``retry_wait_s`` is 0 or more seconds. Raises ``ValueError`` otherwise.
        """
        if max_retries < 0 or int(max_retries) != max_retries:
            raise ValueError(f"max_retries must be a whole number >= 0, not {max_retries!r}")
        if not retry_wait_s >= 0:  # a NaN fails this test too
            raise ValueError(f"retry_wait_s must be >= 0 seconds, not {retry_wait_s!r}")
        self._max_retries = int(max_retries)
        self._retry_wait_s = float(retry_wait_s)

    def _retry_wait(self, retry: int, exc: BaseException) -> float:
        """The wait in seconds before retry ``retry`` (1, 2, ...) after ``exc`` (SIM-555).

        ``retry_wait_s`` doubles per retry. A 429 or 503 with a numeric
        ``Retry-After`` waits at least that long. Both stop at
        :attr:`RETRY_WAIT_CAP_S`.
        """
        # The exponent stops at 64: a larger one adds nothing under the cap.
        wait = self._retry_wait_s * 2.0 ** min(retry - 1, 64)
        retry_after = _retry_after_s(exc)
        if retry_after is not None:
            wait = max(wait, retry_after)
        return min(wait, self.RETRY_WAIT_CAP_S)

    def _describe_error(self, exc: BaseException, url: str) -> str:
        """The error for a log line, with the URL's query and the API key taken out (SIM-555)."""
        text = f"{type(exc).__name__}: {exc}".replace(url, _log_target(url))
        if self._api_key:
            text = text.replace(self._api_key, "<api key>")
        return text

    def _http_get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """GET ``url`` and decode its JSON body; the one read of every vendor and MLB call.

        SIM-555: a transient failure (an HTTP 429 or 5xx, a time-out, a refused
        or dropped connection) is retried up to ``max_retries`` times, after
        the wait of :meth:`_retry_wait`. Each retry logs one INFO line with the
        attempt, the error, the host and path (no query, no key) and the wait.
        Any other failure raises at once. After the last retry the original
        error propagates unchanged, so the callers' handlers still catch it.
        """
        retry = 0
        while True:
            try:
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed hosts
                    return json.loads(resp.read().decode("utf-8"))
            except _RETRYABLE_ERRORS as exc:
                if retry >= self._max_retries or not _is_transient(exc):
                    raise
                retry += 1
                wait = self._retry_wait(retry, exc)
                log.info(
                    "BettingPros: %s failed on attempt %d of %d (%s); retrying in %.1f s",
                    _log_target(url),
                    retry,
                    self._max_retries + 1,
                    self._describe_error(exc, url),
                    wait,
                )
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # free the error response's connection before the next attempt
                self._retry_sleep(wait)

    def _bp_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a BettingPros v3 endpoint (the network seam stubbed in tests)."""
        if not self._api_key:
            raise RuntimeError(
                f"BettingProsOddsProvider needs an API key — set {self.API_KEY_ENV}."
            )
        qs = urllib.parse.urlencode(params)
        url = f"{_BP_BASE}/{path}?{qs}"
        return self._http_get_json(
            url, headers={"x-api-key": self._api_key, "Content-Type": "application/json"}
        )

    def _mlb_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET an MLB Stats API endpoint (the network seam stubbed in tests)."""
        qs = urllib.parse.urlencode(params)
        url = f"{_MLB_BASE}/{path}?{qs}"
        return self._http_get_json(url)

    # ------------------------------------------------------ identifier bridges
    def _resolve_game_meta(self, game_pk: int) -> tuple[str, str, str, datetime | None] | None:
        """``game_pk`` → (official local date, home name, away name, UTC start time).

        SIM-536: the date MUST come from ``officialDate`` (the schedule's own
        local-calendar-date field), never from truncating ``gameDate`` (a UTC
        timestamp). For any West-/Mountain-time night game, the UTC clock has
        already rolled past midnight into the next calendar day while the game
        is still being played on ``officialDate`` — truncating ``gameDate``
        then queries BettingPros for the WRONG day's slate, which most often
        pairs the game with a different one between the same two teams the
        following day. A live check (2026-09-08) found this on 5 of 15 games
        that day — every West-Coast game, exactly the affected set.

        The UTC start time (from ``gameDate``, unlike ``officialDate`` this one
        legitimately needs to stay in UTC) is returned too, so
        :meth:`_resolve_event` can pick the right BettingPros event by actual
        start time instead of guessing.

        SIM-555: a resolved game is cached for the process lifetime. A failed
        read returns ``None`` and is kept for one time-to-live
        (:meth:`_failed_recently`); the first call after that reads the
        schedule again. A read that raised counts in ``read_failures``; a
        schedule answer without the game does not (the game is absent).
        """
        if game_pk in self._game_meta_cache:
            return self._game_meta_cache[game_pk]
        if self._failed_recently("game", game_pk):
            return None
        meta: tuple[str, str, str, datetime | None] | None = None
        read_done = False
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            read_done = True
            game = data["dates"][0]["games"][0]
            date_str = str(game["officialDate"])
            home = str(game["teams"]["home"]["team"]["name"])
            away = str(game["teams"]["away"]["team"]["name"])
            game_dt = _parse_utc(str(game.get("gameDate", "")))
            meta = (date_str, home, away, game_dt)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve game_pk %s: %s", game_pk, exc)
            if not read_done:
                self.read_failures += 1  # SIM-555: the schedule read failed
        if meta is not None:
            self._game_meta_cache[game_pk] = meta
        else:
            self._note_failed_lookup("game", game_pk)
        return meta

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        """``game_pk`` → the matching BettingPros event dict (or None).

        SIM-536: when more than one event matches by team name (a
        double-header), the earlier code always picked the earliest-scheduled
        one — silently returning game 1's odds even when ``game_pk`` was game
        2. It now picks whichever candidate's ``scheduled`` time is CLOSEST to
        the actual game's own start time (from :meth:`_resolve_game_meta`),
        which is what genuinely tells the two games apart. That same
        start-time check also acts as a sanity gate on every match, not only
        double-headers: a match more than :data:`_MAX_EVENT_TIME_DELTA` away
        from the real first pitch is treated as no match, rather than priced
        against whatever the closest name match happened to be.

        SIM-555: a matched event is cached for the process lifetime. No match
        (or a failed read) is kept for one time-to-live
        (:meth:`_failed_recently`), so a repeat call inside that window reads
        nothing, and the first call after it looks again. A failed slate read
        counts in ``read_failures``; no match does not, nor does a slate the
        matcher cannot read (a retry would read the same answer).
        """
        if game_pk in self._event_cache:
            return self._event_cache[game_pk]
        if self._failed_recently("event", game_pk):
            return None
        event: dict[str, Any] | None = None
        meta = self._resolve_game_meta(game_pk)
        if meta is not None:
            date_str, home_name, away_name, game_dt = meta
            home_n, away_n = _normalize_name(home_name), _normalize_name(away_name)
            read_done = False
            try:
                slate = self._events_for_date(date_str)
                read_done = True
                candidates = []
                for e in slate:
                    parts = {p["id"]: _normalize_name(p["name"]) for p in e.get("participants", [])}
                    home_nick = parts.get(e.get("home"), "")
                    away_nick = parts.get(e.get("visitor"), "")
                    # Nickname suffix-matches the MLB full name ("Tigers" ⊂ "Detroit Tigers").
                    if home_n.endswith(home_nick) and away_n.endswith(away_nick) and home_nick:
                        candidates.append(e)
                if candidates and game_dt is not None:

                    def _delta(candidate: dict[str, Any]) -> timedelta:
                        c_dt = _parse_utc(str(candidate.get("scheduled", "")))
                        return timedelta.max if c_dt is None else abs(c_dt - game_dt)

                    best = min(candidates, key=_delta)
                    best_delta = _delta(best)
                    if best_delta > _MAX_EVENT_TIME_DELTA:
                        log.warning(
                            "BettingPros: closest event (scheduled=%s) for game_pk %s is "
                            "%s from the real start time — over the %s sanity limit, "
                            "treating as unmatched rather than risk the wrong game",
                            best.get("scheduled"),
                            game_pk,
                            best_delta,
                            _MAX_EVENT_TIME_DELTA,
                        )
                    else:
                        event = best
                        if len(candidates) > 1:
                            log.info(
                                "BettingPros: %d events matched game_pk %s (double-header); "
                                "picked scheduled=%s (%s from the real start time)",
                                len(candidates),
                                game_pk,
                                event.get("scheduled"),
                                best_delta,
                            )
                elif len(candidates) == 1:
                    # No usable start time to sanity-check against (gameDate was
                    # missing/unparseable) but only one name match anyway — accept it,
                    # matching the pre-SIM-536 behaviour for the common case.
                    event = candidates[0]
                elif len(candidates) > 1:
                    # Multiple candidates and no start time to tell them apart by —
                    # guessing "game 1" is exactly the bug this fix removes, so refuse.
                    log.warning(
                        "BettingPros: %d events matched game_pk %s (double-header) but "
                        "the game's own start time is unknown — refusing to guess which",
                        len(candidates),
                        game_pk,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("BettingPros: event lookup failed for game_pk %s: %s", game_pk, exc)
                if not read_done:
                    self.read_failures += 1  # SIM-555: the events read failed
        if event is not None:
            self._event_cache[game_pk] = event
        else:
            self._note_failed_lookup("event", game_pk)
        return event

    # ------------------------------------------------------- SIM-421 caches
    def _is_fresh(self, stored_at: float) -> bool:
        """True while a cache entry stored at ``stored_at`` is inside the time-to-live."""
        return self._clock() - stored_at < self._offers_cache_ttl_s

    def _failed_recently(self, kind: str, key: int) -> bool:
        """True while a failed ``kind`` lookup of ``key`` is inside the time-to-live (SIM-555).

        ``kind`` is ``'game'`` (the MLB schedule), ``'event'`` (the vendor
        event) or ``'player'`` (the MLB name). A time-to-live of 0 keeps no
        failure, so every call reads again.
        """
        hit = self._failed_lookups.get((kind, int(key)))
        return hit is not None and self._is_fresh(hit[0])

    def _note_failed_lookup(self, kind: str, key: int) -> None:
        """Keep a failed lookup for one time-to-live (SIM-555; see :meth:`_failed_recently`)."""
        self._store(self._failed_lookups, (kind, int(key)), None)

    def forget_failed_lookups(self) -> None:
        """Drop every stored failed lookup, so the next call reads again (SIM-555).

        The historical loader calls this before each game. A failed player
        lookup is stored for one time-to-live (600 s in the loader, about 33
        games) and counted once, in the game where it failed. Without this
        call, a later game that names the same player inside that window gets
        the stored failure: no read, no count, no props for the player, and
        the game goes on the done-list. The cost is one more read per failed
        player per game. A found game, event or name stays cached.
        """
        self._failed_lookups.clear()

    def _store(self, cache: dict[_K, tuple[float, _V]], key: _K, payload: _V) -> None:
        """Put ``payload`` in ``cache`` under ``key`` after dropping every stale entry.

        The sweep bounds the cache to one time-to-live's worth of entries. A
        long-lived provider (the live pipeline, the historical loader) would
        otherwise keep every finished game's payload for the process lifetime.
        A time-to-live of 0 stores nothing.
        """
        if self._offers_cache_ttl_s <= 0:
            return
        stale = [k for k, (stored_at, _) in cache.items() if not self._is_fresh(stored_at)]
        for k in stale:
            del cache[k]
        cache[key] = (self._clock(), payload)

    def _events_for_date(self, date_str: str) -> list[dict[str, Any]]:
        """The BettingPros events of one date, served from the per-date cache while fresh.

        Every game on a date shares one ``/events`` response; before SIM-421
        each game_pk fetched the whole slate again. A fetch failure raises to
        the caller (``_resolve_event`` logs it) and is not cached, so the next
        game retries.
        """
        hit = self._events_by_date_cache.get(date_str)
        if hit is not None and self._is_fresh(hit[0]):
            return hit[1]
        data = self._bp_get("events", {"sport": "MLB", "date": date_str})
        events = list(data.get("events", []))
        self._store(self._events_by_date_cache, date_str, events)
        return events

    def _offers(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """Every offer of one (event, market), served from the offers cache while fresh.

        One response carries every player's offer for the market and serves
        every book and both line types, so one fetch per (event, market) per
        time-to-live replaces one fetch per (player, market, book, line type).
        A page-1 fetch failure raises to the caller. A later-page failure
        returns the pages in hand. Neither result is cached, so the next call
        fetches again; only a complete market is stored.
        """
        key = (int(event_id), int(market_id))
        hit = self._offers_cache.get(key)
        if hit is not None and self._is_fresh(hit[0]):
            return hit[1]
        offers, complete = self._fetch_offers_all_pages(event_id, market_id)
        if complete:
            self._store(self._offers_cache, key, offers)
        return offers

    def _fetch_offers_all_pages(
        self, event_id: int, market_id: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """GET ``/offers`` for one (event, market) and follow its pagination.

        The API pages at 10 offers (``_pagination.total_pages``). A batter
        market lists every hitter in the game, so page 1 alone silently
        dropped the players on later pages — their quotes came back empty.

        Returns ``(offers, complete)``. Page 1 raises on failure. A later page
        that fails logs a warning naming the event, the market and the page,
        and the offers already in hand come back with ``complete=False`` so
        the players on the earlier pages keep their quotes. A ``_pagination``
        block that is not a dict counts as one page.
        """
        params: dict[str, Any] = {"sport": "MLB", "market_id": market_id, "event_id": event_id}
        data = self._bp_get("offers", params)
        offers = list(data.get("offers", []))
        pagination = data.get("_pagination")
        total_pages = 1
        if isinstance(pagination, dict):
            try:
                total_pages = int(pagination.get("total_pages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
        for page in range(2, min(total_pages, _MAX_OFFER_PAGES) + 1):
            try:
                more = self._bp_get("offers", {**params, "page": page})
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "BettingPros: offers page %d of %d failed (event %s, market %s); "
                    "using the %d offers in hand: %s",
                    page,
                    total_pages,
                    event_id,
                    market_id,
                    len(offers),
                    exc,
                )
                self.read_failures += 1  # SIM-555: the market comes back partial
                return offers, False
            offers.extend(more.get("offers", []))
        return offers, True

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name.

        SIM-555 (review fix 2026-09-28): a found name is cached for the process
        lifetime. A failed lookup is kept for one time-to-live only
        (:meth:`_failed_recently`); it used to be cached as ``None`` for the
        process lifetime, so one timeout cost the player every prop for the
        rest of a season's load or of the live pipeline's day. A read that
        raised counts in ``read_failures``; an answer without the player does
        not (SIM-555).
        """
        if player_id in self._player_name_cache:
            return self._player_name_cache[player_id]
        if self._failed_recently("player", player_id):
            return None
        name: str | None = None
        read_done = False
        try:
            data = self._mlb_get(f"people/{player_id}", {})
            read_done = True
            full = data["people"][0]["fullName"]
            name = _normalize_name(str(full))
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve player_id %s: %s", player_id, exc)
            if not read_done:
                self.read_failures += 1  # SIM-555: the people read failed
        if name is None:
            self._note_failed_lookup("player", player_id)
        else:
            self._player_name_cache[player_id] = name
        return name

    # ------------------------------------------------ SIM-555: one row per book
    def _game_meta_fields(self, game_pk: int) -> tuple[datetime | None, date | None, str | None]:
        """(scheduled start as aware UTC, official date, official date text) for a game.

        Read from :meth:`_resolve_game_meta` (cached; :meth:`_resolve_event`
        fills it first). The start is for the load guard only; the date drives
        the dated first-five exclusion.
        """
        meta = self._resolve_game_meta(game_pk)
        if meta is None:
            return None, None, None
        date_str, _home, _away, game_dt = meta
        start = None if game_dt is None else game_dt.replace(tzinfo=UTC)
        try:
            official = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            official = None
        return start, official, date_str

    @staticmethod
    def _game_sides(
        kind: str,
        selections: list[dict[str, Any]],
        home_abbrev: Any,
        away_abbrev: Any,
    ) -> dict[str, dict[str, Any]]:
        """The market's selections keyed by side (the first selection of each side wins).

        Moneylines and run lines name the team in ``participant``; the tie of a
        three-way moneyline carries ``selection == 'draw'``. BettingPros folds an
        unrelated yes / no pair into the first-five moneyline (seen live
        2026-09-12); those carry neither a participant nor ``draw`` and are
        ignored. A total's sides are its over and under; a yes / no market's
        its yes and no.
        """
        sides: dict[str, dict[str, Any]] = {}
        for sel in selections:
            key: str | None = None
            if kind in ("moneyline", "three_way", "runline"):
                participant = sel.get("participant")
                if participant and participant == home_abbrev:
                    key = "home"
                elif participant and participant == away_abbrev:
                    key = "away"
                elif kind == "three_way":
                    name = str(sel.get("selection") or sel.get("label") or "").lower()
                    key = "draw" if name == "draw" else None
            elif kind == "total":
                label = str(sel.get("label") or sel.get("selection") or "").lower()
                key = "over" if "over" in label else "under" if "under" in label else None
            elif kind == "yes_no":
                name = str(sel.get("selection") or sel.get("label") or "").lower()
                key = name if name in ("yes", "no") else None
            if key is not None and key not in sides:
                sides[key] = sel
        return sides

    @staticmethod
    def _prop_sides(offer: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """A prop offer's over and under selections, keyed ``over`` / ``under``."""
        sides: dict[str, dict[str, Any]] = {}
        for sel in offer.get("selections", []):
            label = str(sel.get("label") or sel.get("selection") or "").lower()
            key = "over" if "over" in label else "under" if "under" in label else None
            if key is not None and key not in sides:
                sides[key] = sel
        return sides

    @staticmethod
    def _fill(kind: str, row: dict[str, Any], quote: Mapping[str, _Quote]) -> None:
        """Write one book's quote into the row's columns for the market kind."""

        def cost(key: str) -> float | None:
            return quote[key][0] if key in quote else None

        def line(key: str) -> float | None:
            return quote[key][1] if key in quote else None

        if kind in ("moneyline", "three_way"):
            row["home_ml"], row["away_ml"] = cost("home"), cost("away")
            if kind == "three_way":
                row["draw_ml"] = cost("draw")
        elif kind == "runline":
            row["home_spread"], row["home_spread_ml"] = line("home"), cost("home")
            row["away_spread"], row["away_spread_ml"] = line("away"), cost("away")
        elif kind == "total":
            row["over_ml"], row["under_ml"] = cost("over"), cost("under")
            row["over_line"], row["under_line"] = line("over"), line("under")
            row["total_line"] = line("over")
        elif kind == "yes_no":
            # "A run in the first inning: yes" IS "over 0.5 first-inning runs".
            row["over_ml"], row["under_ml"] = cost("yes"), cost("no")
            row["over_line"], row["under_line"] = line("yes"), line("no")
            priced = row["over_ml"] is not None or row["under_ml"] is not None
            row["total_line"] = 0.5 if priced else None
        elif kind == "prop":
            row["over_ml"], row["under_ml"] = cost("over"), cost("under")
            row["over_line"], row["under_line"] = line("over"), line("under")
            row["line"] = line("over") if line("over") is not None else line("under")

    def _rows(
        self,
        base: Mapping[str, Any],
        kind: str,
        sides: Mapping[str, dict[str, Any]],
        line_type: str,
        *,
        excluded: frozenset[int] = frozenset(),
        opener_is_twin: bool = False,
    ) -> list[dict[str, Any]]:
        """One row per book for a market's sides (the opener's single row on 'opening')."""
        required = _REQUIRED_SIDES[kind]
        optional = tuple(k for k in _OPTIONAL_SIDES.get(kind, ()) if k in sides)
        if any(k not in sides for k in required):
            return []

        def make_row(book_id: int, quote: Mapping[str, _Quote]) -> dict[str, Any]:
            row = dict(base)
            row["book"] = book_label(book_id)
            row["book_id"] = book_id
            self._fill(kind, row, quote)
            stamps = [s for s in (_stamp(q[2]) for q in quote.values()) if s is not None]
            row["book_line_at"] = max(stamps) if stamps else None
            return row

        if line_type == "opening":
            quote: dict[str, _Quote] = {}
            openers: set[int] = set()
            for key in required:
                ol = sides[key].get("opening_line") or {}
                opener_id, cost = _opt_int(ol.get("book_id")), _opt_float(ol.get("cost"))
                if opener_id is None or cost is None:
                    return []
                openers.add(opener_id)
                quote[key] = (cost, _opt_float(ol.get("line")), ol.get("created"))
            if len(openers) != 1:
                return []  # two openers: the feed gives no one-book opening row
            opener = openers.pop()
            if self._prefer_book_id is not None and opener != self._prefer_book_id:
                return []
            if opener in excluded or opener_is_twin:
                return []
            for key in optional:
                ol = sides[key].get("opening_line") or {}
                cost = _opt_float(ol.get("cost"))
                if _opt_int(ol.get("book_id")) == opener and cost is not None:
                    quote[key] = (cost, _opt_float(ol.get("line")), ol.get("created"))
            return [make_row(opener, quote)]

        book_ids = _books_on({k: sides[k] for k in required})
        if self._prefer_book_id is not None:
            book_ids = [b for b in book_ids if b == self._prefer_book_id]
        rows: list[dict[str, Any]] = []
        for book_id in book_ids:
            if book_id in excluded:
                continue
            lines = {k: _book_line(sides[k], book_id) for k in (*required, *optional)}
            if any(lines[k] is None for k in required):
                continue
            quote = {
                k: (_opt_float(ln.get("cost")), _opt_float(ln.get("line")), ln.get("updated"))
                for k, ln in lines.items()
                if ln is not None
            }
            rows.append(make_row(book_id, quote))
        return rows

    def _f1_selections(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """The selections of the first-inning market a first-five market is checked against.

        Reads :meth:`_offers` directly (cached per (event, market)), so a
        failed read is told apart from an empty market: an empty market gives
        ``[]`` (no twin is possible, the first-five rows stand); a failed read
        raises :class:`_FirstInningReadError` (the caller drops the first-five
        rows). ``_offers`` does not cache a failure, so the next call reads
        again.
        """
        f1_market = F5_TWIN_MARKETS[market_id]
        try:
            offers = self._offers(event_id, f1_market)
        except Exception as exc:  # noqa: BLE001
            raise _FirstInningReadError(
                f"the first-inning read (event {event_id}, market {f1_market}) failed: {exc}"
            ) from exc
        return offers[0].get("selections", []) if offers else []

    def _f1_twin_sides(
        self, event_id: int, market_id: int, required_sides: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]] | None:
        """The first-inning selections that match a first-five market's sides.

        A team side matches by ``participant`` (the first-five side names the
        same team); an over / under matches by label. The first-inning tie, when
        the market lists one, comes back under ``'draw'``. ``None`` when the
        first-inning market lacks a required side (then nothing can be a twin).
        Raises :class:`_FirstInningReadError` when the first-inning read fails.
        """
        f1_selections = self._f1_selections(event_id, market_id)
        matched: dict[str, dict[str, Any]] = {}
        for key, f5_sel in required_sides.items():
            for sel in f1_selections:
                if key in ("home", "away"):
                    hit = bool(sel.get("participant")) and (
                        sel.get("participant") == f5_sel.get("participant")
                    )
                else:
                    hit = key in str(sel.get("label") or sel.get("selection") or "").lower()
                if hit:
                    matched[key] = sel
                    break
            else:
                return None
        for sel in f1_selections:
            if str(sel.get("selection") or sel.get("label") or "").lower() == "draw":
                matched["draw"] = sel
                break
        return matched

    def _log_f5_exclusion(self, event_id: int, market_id: int, who: str, why: str) -> None:
        """Log a first-five exclusion at INFO, once per (event, market, book)."""
        key = (int(event_id), int(market_id), who)
        if key in self._f5_logged:
            return
        self._f5_logged.add(key)
        log.info(
            "BettingPros: first-five exclusion (event %s, market %s, %s): %s",
            event_id,
            market_id,
            who,
            why,
        )

    def _f5_dated_exclusions(
        self,
        event_id: int,
        market_id: int,
        required_sides: Mapping[str, Mapping[str, Any]],
        game_date: date | None,
    ) -> frozenset[int]:
        """The books on F5_EXCLUDED_BOOKS whose date is on or before the game's (SIM-555).

        Only for the first-five markets of F5_TWIN_MARKETS, and for each book
        only on the markets :data:`F5_EXCLUDED_MARKETS` lists for it (see
        :func:`is_f5_dated_excluded`); an unknown game date excludes nothing.
        The exclusion itself does not depend on the payload. The INFO line
        does: it is written only when the excluded book had something to lose
        on this market, a usable line on every required side (a closing or
        current row) or the shared opener (the opening row). Otherwise every
        first-five market of every game from the date would log an exclusion
        of a book that quoted nothing.
        """
        if market_id not in F5_TWIN_MARKETS or game_date is None:
            return frozenset()
        excluded: set[int] = set()
        opener = _shared_opener(required_sides)
        for book_id, start in F5_EXCLUDED_BOOKS.items():
            if is_f5_dated_excluded(book_id, market_id, game_date):
                excluded.add(book_id)
                quoted = all(
                    _book_line(sel, book_id) is not None for sel in required_sides.values()
                )
                if quoted or opener == book_id:
                    self._log_f5_exclusion(
                        event_id, market_id, book_label(book_id), f"excluded from {start}"
                    )
        return frozenset(excluded)

    def _f5_exclusions(
        self,
        event_id: int,
        market_id: int,
        required_sides: Mapping[str, Mapping[str, Any]],
        game_date: date | None,
        *,
        tie: Mapping[str, Any] | None = None,
    ) -> frozenset[int]:
        """The books whose first-five entry is a first-inning bet (SIM-555).

        Only for the three first-five markets of F5_TWIN_MARKETS. Two rules:
        (1) a book on F5_EXCLUDED_BOOKS whose date is on or before the game's
        date, on the markets F5_EXCLUDED_MARKETS lists for it; (2) a book
        whose usable first-five lines equal its own usable
        first-inning lines on every required side (the same line, the implied
        probabilities within :func:`twin_price_tolerance` — identical prices
        on the moneyline): the "twin". ``tie`` is the first-five tie selection
        of a three-way market: a book whose tie rules out a copy (see
        :func:`_tie_rules_out_twin`) is not a twin. The first-inning market is
        one more cached vendor read per game; a failed read raises
        :class:`_FirstInningReadError`.
        """
        if market_id not in F5_TWIN_MARKETS:
            return frozenset()
        excluded = set(self._f5_dated_exclusions(event_id, market_id, required_sides, game_date))
        f1_sides = self._f1_twin_sides(event_id, market_id, required_sides)
        if f1_sides is None:
            return frozenset(excluded)
        f1_tie = f1_sides.get("draw")
        tolerance = twin_price_tolerance(market_id)
        for book_id in _books_on(required_sides):
            if book_id in excluded:
                continue
            twin = True
            for key, f5_sel in required_sides.items():
                f5_line = _book_line(f5_sel, book_id)
                f1_line = _book_line(f1_sides[key], book_id)
                if (
                    f5_line is None
                    or f1_line is None
                    or not _same_line(f5_line, f1_line, tolerance)
                ):
                    twin = False
                    break
            if twin and _tie_rules_out_twin(
                None if tie is None else _book_line(tie, book_id),
                None if f1_tie is None else _book_line(f1_tie, book_id),
                tolerance,
            ):
                twin = False
            if twin:
                excluded.add(book_id)
                self._log_f5_exclusion(
                    event_id,
                    market_id,
                    book_label(book_id),
                    "its first-five lines copy its own first-inning lines",
                )
        return frozenset(excluded)

    def _f5_opener_is_twin(
        self,
        event_id: int,
        market_id: int,
        required_sides: Mapping[str, Mapping[str, Any]],
        *,
        tie: Mapping[str, Any] | None = None,
    ) -> bool:
        """True when the first-five opener copies the first-inning opener (SIM-555).

        On every required side: the same opening book, the same line, the
        implied probabilities within :func:`twin_price_tolerance` (identical
        prices on the moneyline). On a three-way market the opening row's tie
        is the tie that the same book opened; a first-five opening tie that
        rules out a copy (see :func:`_tie_rules_out_twin`) clears the opener.
        Such a market gets no opening row. A failed first-inning read raises
        :class:`_FirstInningReadError`.
        """
        if market_id not in F5_TWIN_MARKETS:
            return False
        f1_sides = self._f1_twin_sides(event_id, market_id, required_sides)
        if f1_sides is None:
            return False
        tolerance = twin_price_tolerance(market_id)
        opener: int | None = None
        for key, f5_sel in required_sides.items():
            f5_open = f5_sel.get("opening_line") or {}
            f1_open = f1_sides[key].get("opening_line") or {}
            f5_book = _opt_int(f5_open.get("book_id"))
            if f5_book is None or f5_book != _opt_int(f1_open.get("book_id")):
                return False
            if not _same_line(f5_open, f1_open, tolerance):
                return False
            opener = f5_book

        def opening_tie(sel: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
            ol = None if sel is None else sel.get("opening_line")
            if not ol or _opt_int(ol.get("book_id")) != opener or ol.get("cost") is None:
                return None
            return ol

        if _tie_rules_out_twin(opening_tie(tie), opening_tie(f1_sides.get("draw")), tolerance):
            return False
        self._log_f5_exclusion(
            event_id,
            market_id,
            "opener",
            "the first-five opener copies the first-inning opener; no opening row",
        )
        return True

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

    def _game_market_rows(
        self, game_pk: int, line_type: str, market_type: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """(the empty row, one row per book) for one game market and line type."""
        canonical = "runline" if market_type == "run_line" else market_type
        if canonical not in GAME_MARKET_KIND:
            known = ", ".join(GAME_MARKET_TYPES)
            raise ValueError(f"Unknown market_type '{market_type}'. Known values: {known}")
        kind = GAME_MARKET_KIND[canonical]
        base: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "bettingpros",
            "is_mock": False,
            "book": "consensus",
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": False,
        }
        for field in GAME_ODDS_FIELDS:
            base[field] = None
        if kind in ("total", "yes_no"):
            base["over_line"] = None
            base["under_line"] = None
        base.update({"book_id": None, "book_line_at": None})
        event = self._resolve_event(game_pk)
        if event is None:
            base.update({"scheduled_start": None, "game_date": None})
            log.warning("BettingPros: no event for game_pk %s — returning empty odds", game_pk)
            return base, []
        start, official, date_str = self._game_meta_fields(game_pk)
        base.update({"scheduled_start": start, "game_date": date_str})
        event_id = event["id"]
        home_abbrev, away_abbrev = event.get("home"), event.get("visitor")
        market_id = _GAME_MARKET_IDS[canonical]
        side = GAME_MARKET_SIDE[canonical]
        if kind == "total" and side is not None:
            team_abbrev = home_abbrev if side == "home" else away_abbrev
            selections = self._team_offer_selections(event_id, market_id, team_abbrev)
        else:
            selections = self._selections(event_id, market_id)
        sides = self._game_sides(kind, selections, home_abbrev, away_abbrev)
        required = _REQUIRED_SIDES[kind]
        if any(k not in sides for k in required):
            return base, []
        required_sides = {k: sides[k] for k in required}
        tie = sides.get("draw")
        try:
            # The opening row falls when its opener is excluded by either rule
            # (the dated list, or a book whose closing first-five lines copy its
            # first-inning lines), or when the opener copies its own
            # first-inning opener.
            excluded = self._f5_exclusions(event_id, market_id, required_sides, official, tie=tie)
            opener_is_twin = line_type == "opening" and self._f5_opener_is_twin(
                event_id, market_id, required_sides, tie=tie
            )
        except _FirstInningReadError as exc:
            log.warning(
                "BettingPros: game_pk %s %s/%s: no first-five rows, the twin check "
                "could not run: %s",
                game_pk,
                line_type,
                market_type,
                exc,
            )
            self.read_failures += 1  # SIM-555: the first-inning read failed
            return base, []
        rows = self._rows(
            base, kind, sides, line_type, excluded=excluded, opener_is_twin=opener_is_twin
        )
        return base, rows

    # ----------------------------------------------------------------- get_odds
    def get_odds_by_book(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
    ) -> list[dict[str, Any]]:
        """One row per book for one game market and line type (SIM-555).

        ``market_type`` is any value of ``GAME_MARKET_TYPES`` (``run_line`` is
        accepted as the legacy alias of ``runline`` and echoed as asked); an
        unknown value raises ``ValueError``. ``'opening'`` gives the opener's one
        row, or none when the sides name two openers or the opener is excluded;
        any other line type gives one row per book quoting every side, the
        vendor's blend (``bp:0``) included, the excluded first-five books left
        out. Each row carries every ``get_odds`` key plus ``book_id``,
        ``book_line_at``, ``scheduled_start`` and ``game_date`` (and
        ``over_line`` / ``under_line`` on a total-kind or yes / no market). An
        unresolvable game gives an empty list.
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
        """One row for ``game_pk`` in the MockOddsAPI dict shape (SIM-405; SIM-555).

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

    # ------------------------------------------------------------ get_prop_odds
    def _prop_market_rows(
        self, game_pk: int, player_id: int, prop_stat: str, line_type: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """(the empty row, one row per book) for one player's prop market."""
        if prop_stat not in _PROP_MARKET_IDS:
            known = ", ".join(sorted(_PROP_MARKET_IDS))
            raise ValueError(f"Unknown prop_stat '{prop_stat}'. Known values: {known}")
        base: dict[str, Any] = {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": None,
            "over_ml": None,
            "under_ml": None,
            "book": "consensus",
            "line_type": line_type,
            "is_sharp_book": False,
            "source": "bettingpros",
            "is_mock": False,
            "book_id": None,
            "book_line_at": None,
            "scheduled_start": None,
            "over_line": None,
            "under_line": None,
            "game_date": None,
        }
        event = self._resolve_event(game_pk)
        player_name = self._resolve_player_name(player_id)
        if event is None or player_name is None:
            return base, []
        start, _official, date_str = self._game_meta_fields(game_pk)
        base.update({"scheduled_start": start, "game_date": date_str})
        offer = self._find_player_offer(event["id"], _PROP_MARKET_IDS[prop_stat], player_name)
        if offer is None:
            return base, []
        return base, self._rows(base, "prop", self._prop_sides(offer), line_type)

    def get_prop_odds_by_book(
        self,
        game_pk: int,
        player_id: int,
        prop_stat: str,
        *,
        line_type: str = "current",
    ) -> list[dict[str, Any]]:
        """One row per book for one player's prop market (SIM-555).

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
        """One player-prop quote in the MockOddsAPI shape (SIM-405; SIM-555).

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

    # --------------------------------------------------------------- internals
    def _selections(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """All selections of the (single) offer for a game-level market."""
        try:
            offers = self._offers(event_id, market_id)  # SIM-421: cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1  # SIM-555
            return []
        return offers[0].get("selections", []) if offers else []

    def _team_offer_selections(
        self, event_id: int, market_id: int, team_abbrev: Any
    ) -> list[dict[str, Any]]:
        """The selections of the offer for ONE team in a per-team market.

        A team-total market holds one offer per team; each offer names its
        team in ``team_id`` (the abbreviation the event uses for ``home`` /
        ``visitor``) and again as its only participant. Returns ``[]`` when the
        team has no offer.
        """
        try:
            offers = self._offers(event_id, market_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: team offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1  # SIM-555
            return []
        if not team_abbrev:
            return []
        for offer in offers:
            if offer.get("team_id") == team_abbrev:
                return offer.get("selections", [])
            ids = {p.get("id") for p in offer.get("participants", [])}
            if team_abbrev in ids:
                return offer.get("selections", [])
        return []

    def _find_player_offer(
        self, event_id: int, market_id: int, player_name: str
    ) -> dict[str, Any] | None:
        """The prop offer whose participant matches ``player_name`` (normalized)."""
        try:
            offers = self._offers(event_id, market_id)  # SIM-421: cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: prop offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1  # SIM-555
            return None
        for offer in offers:
            for part in offer.get("participants", []):
                player = part.get("player") or {}
                first = player.get("first_name", "")
                last = player.get("last_name", "")
                if _normalize_name(f"{first} {last}") == player_name:
                    return offer
                # Fall back to the participant display name if first/last absent.
                if part.get("name") and _normalize_name(part["name"]) == player_name:
                    return offer
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "F5_EXCLUDED_BOOKS",
    "F5_EXCLUDED_MARKETS",
    "F5_TWIN_MARKETS",
    "TWIN_MONEYLINE_PRICE_TOLERANCE",
    "TWIN_PRICE_TOLERANCE",
    "BettingProsOddsProvider",
    "f5_excluded_markets",
    "is_f5_dated_excluded",
    "twin_price_tolerance",
]
