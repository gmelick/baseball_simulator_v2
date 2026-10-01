#!/usr/bin/env python
"""
scripts/sim555_book_probe.py — SIM-555: the read-only census of the vendor's
books, the gate before the re-load.

WHY THIS EXISTS
---------------
The re-load stores one odds row per book (``book = 'bp:<id>'``). Before it runs,
this probe reads a few hundred games from the vendor through the provider's
own by-book methods (``BettingProsOddsProvider.get_odds_by_book`` /
``get_prop_odds_by_book``) and the load guard (``pipeline/odds_row_guard.py``),
exactly as the loader will, and writes nothing to the database. It answers
three questions the plan (§8 step 3) makes a gate:

  (a) Does every two-way offer carry a price from at least one book on
      ``GRADED_BOOK_PREFERENCE``? Otherwise the accuracy comparison loses games.
  (b) After the first-five rules (the dated exclusion and the "twin" rule in
      the provider), is the share of first-five rows that look like first-inning
      bets below 0.5% for every market, line type and bettable book? A row of
      the blend, a pick'em app, an exchange or a prediction market is stored
      but never graded or offered, so (b) and (c) do not grade it: its flags
      go to an informational list (``non_bettable_flags``), printed beside
      the gate, which never fails it.
  (c) Is the dated exclusion right (``F5_EXCLUDED_BOOKS``, each book on the
      markets ``F5_EXCLUDED_MARKETS`` lists for it)? A book off the list
      that fails (b) needs adding, with a date; a market off a listed book's
      list that fails (b) needs adding to its markets; a book on the list
      with any flagged row on one of its markets keeps rows before its date
      that look like first-inning bets, so its date moves earlier. The probe
      also reads the rows the dated exclusion drops (it keeps them apart,
      marked ``dated_excluded``, and never counts them as loaded; a row is
      dated only on the markets the exclusion applies to): a listed book
      whose closing rows from its date carry fewer than 0.5%
      first-inning-shaped rows on a market shows the exclusion is not needed
      there. The printout names each fix; make it and run the probe again.

The provider turns a failed vendor read into an empty market. The probe
records every such failure, so a gate never passes on a read that failed: a
gate item with no failure but a failed read reads UNKNOWN and lists the games
to read again (``--game-pks``).

A first-five row "looks like a first-inning bet" when any of these holds:

  * a side's price sits more than 0.15 implied probability from the median of
    the other sportsbooks at the same line on that side (the three-way
    moneyline compares books with a tie price among themselves, and books
    without one among themselves);
  * the row equals the same book's first-inning row (the same lines, every
    price within the provider's twin tolerance: ``TWIN_PRICE_TOLERANCE``,
    identical prices on the moneyline): the provider's twin rule, checked
    again at the row level;
  * the guard names its shape: a "+0.5 / +0.5" pair adding above 1.40, a
    tie priced above 0.35, a three-way row whose three prices add below 0.98,
    or a first-five total at a line of 1.5 or below.

The probe prints PASS, FAIL or UNKNOWN for each gate item and exits 0 either
way; the coordinator reads the result.

THE REGRADE (``--regate FILE``)
-------------------------------
A census costs about an hour of vendor reads. When the load guard or the dated
exclusion changes, ``--regate`` re-grades a saved census (the ``--out`` file
of an earlier run) without a vendor read or a database read: it runs the
CURRENT guard on every stored row and the CURRENT dated exclusion on every
first-five row (it moves a row between ``rows`` and ``dated_rows``), then
prints what moved, the same summary and the gate, and with ``--out`` writes
the re-graded JSON. The provider's twin rule is not re-run: its exclusions in
the saved file stand.

    MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim555_book_probe.py --regate scripts/sim555_probe_300.json \\
        --out scripts/sim555_probe_300_regated.json

WHAT IT READS AND PRINTS
------------------------
``--games`` (default 300) Final games with a stored closing moneyline, spread
evenly over the seasons (2019-2026 by default) and, within a season, over the
months. For each game: the moneyline, run line, total, and the first-inning and
first-five moneyline, run line and total, each at the opening and the closing
line; and the home starter's strikeout prop (the starter from the official box
score, else the lineup). The vendor is read at ``--rate`` reads a second (default
1.0; one offers read serves both line types). The summary gives, per market: the
offers, the books quoting (mean and range), the opener's coverage, the rows by
book and the graded book; the closing stamp's distance from the scheduled start
by season (min / median / max; the share of offers with one stamp); the guard's
refusals by rule, market and book; the provider's first-five exclusions (dated,
twin, opener twin) by book, a dated one counted only on a game where the book
quoted the market, each twin with its prices beside its own first-inning prices
and the other sportsbooks' median (so a false twin shows); and the gate. ``--out`` writes every row read, the
summary and the gate as JSON.

It needs ``ODDS_API_KEY`` (the app container has it) and ``BASEBALL_DB_DSN``:

    MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim555_book_probe.py --games 300 --out scripts/sim555_probe_300.json

The plan: docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md §5.6 and §8 step 3.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import random
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.bettingpros_odds_provider import (  # noqa: E402
    F5_EXCLUDED_BOOKS,
    F5_TWIN_MARKETS,
    TWIN_MONEYLINE_PRICE_TOLERANCE,
    TWIN_PRICE_TOLERANCE,
    BettingProsOddsProvider,
    _book_line,
    _opt_float,
    _opt_int,
    f5_excluded_markets,
    is_f5_dated_excluded,
    twin_price_tolerance,
)
from pipeline.odds_provider import (  # noqa: E402
    GAME_MARKET_KIND,
    GAME_ODDS_FIELDS,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    book_display_name,
    book_id_from_label,
    book_kind,
    book_label,
    is_bettable,
)
from pipeline.odds_row_guard import (  # noqa: E402
    F5_TIE_IMPLIED_MAX,
    F5_TOTAL_LINE_MAX,
    F5_WIN_OR_TIE_SUM_MAX,
    THREE_WAY_SUM_MIN,
    Refusal,
    RefusalTally,
    check_row,
    implied_probability,
)

DEFAULT_SEASONS: tuple[int, ...] = tuple(range(2019, 2027))
DEFAULT_GAMES = 300
DEFAULT_RATE = 1.0
DEFAULT_SEED = 555
#: The probe's offers cache: long enough for one game's reads, so the opening
#: and the closing read of a market share one vendor read.
OFFERS_CACHE_TTL_S = 600.0

#: The game markets the probe reads, in order.
GAME_MARKETS: tuple[str, ...] = (
    "moneyline",
    "runline",
    "total",
    "f1_moneyline",
    "f5_moneyline",
    "f1_runline",
    "f5_runline",
    "f1_total",
    "f5_total",
)
#: The one prop the probe reads per game (the home starter's).
PROP_MARKET = "strikeouts"
LINE_TYPES: tuple[str, ...] = ("opening", "closing")

#: A first-five market → the first-inning market its twin copies.
F5_TWIN_OF: dict[str, str] = {
    "f5_runline": "f1_runline",
    "f5_total": "f1_total",
    "f5_moneyline": "f1_moneyline",
}
#: The vendor's first-five market ids (the provider logs exclusions by id).
F5_MARKET_NAMES: dict[int, str] = {283: "f5_runline", 281: "f5_total", 279: "f5_moneyline"}
#: A first-five market name → its vendor id.
F5_MARKET_IDS: dict[str, int] = {name: market_id for market_id, name in F5_MARKET_NAMES.items()}
if set(F5_MARKET_NAMES) != set(F5_TWIN_MARKETS):  # pragma: no cover — an import-time guard
    raise RuntimeError("sim555_book_probe: F5_MARKET_NAMES is out of step with F5_TWIN_MARKETS")

#: A side priced this far (implied probability) from the other books' median is first-inning-shaped.
MEDIAN_DEVIATION_MAX = 0.15
#: The fewest other sportsbooks at the same line for a median check.
MIN_PEERS = 2
#: Gate (b): the largest share of first-inning-shaped first-five rows a (market, book) may carry.
FIRST_INNING_SHARE_MAX = 0.005
#: The guard's rules whose refusal of a first-five row names a first-inning shape.
FIRST_INNING_SHAPE_RULES: frozenset[str] = frozenset(
    {"f5_win_or_tie", "f5_tie_price", "three_way_sum_below_one", "f5_total_first_inning_line"}
)

BLEND = book_label(0)


# ---------------------------------------------------------------------------
# The vendor read: the provider, rate-limited, with the first-five exclusions recorded
# ---------------------------------------------------------------------------


class ProbeProvider(BettingProsOddsProvider):
    """The BettingPros provider with a rate limit on vendor reads (SIM-555).

    Every ``_bp_get`` waits until ``1 / rate`` seconds have passed since the
    last read. The provider's first-five exclusions (its INFO log hook) are
    recorded against the game being read (``current_game``), once per
    (game, market, book, reason); a dated exclusion is recorded only for a
    book that quotes the market (it lost a row). The pick of every row stays
    the provider's.

    The provider turns a failed read into an empty market (it logs a warning
    and returns no row). The probe records each such failure against the
    offer being read (``current_offer``), so the gate can tell a failed read
    from a market nobody quotes: a failed offers read (a first-inning read
    included, after which the provider drops the first-five rows), a partial
    one (a later page failed), a failed player lookup (``read_errors``), and a
    game with no vendor event (``game_errors``).

    Review fix (2026-09-28): the dated first-five exclusion is recorded but
    NOT applied, so the rows it would drop reach the probe. The provider's
    other rules (the twin, the opener twin) still apply to them.
    :func:`_offer` puts those rows apart (``dated_rows``), and gate (c)
    grades them to show whether the exclusion is needed. ``dated_books``
    holds the books the dated rule named while the current offer was read;
    the provider's rule names a book only on the markets
    ``F5_EXCLUDED_MARKETS`` lists for it, so a row is dated only there.
    """

    def __init__(
        self,
        *,
        rate: float = DEFAULT_RATE,
        sleep: Callable[[float], None] = time.sleep,
        rate_clock: Callable[[], float] = time.monotonic,
        offers_cache_ttl_s: float = OFFERS_CACHE_TTL_S,
        **kwargs: Any,
    ) -> None:
        super().__init__(offers_cache_ttl_s=offers_cache_ttl_s, **kwargs)
        self._min_gap = 1.0 / rate if rate > 0 else 0.0
        self._sleep = sleep
        self._rate_clock = rate_clock
        self._last_read: float | None = None
        self.vendor_reads = 0
        self.current_game: int | None = None
        self.current_offer: tuple[str, str] | None = None
        self.exclusions: list[dict[str, Any]] = []
        self._seen_exclusions: set[tuple[Any, ...]] = set()
        self.read_errors: list[dict[str, Any]] = []
        self.game_errors: dict[int, str] = {}
        #: The books that quote the first-five market being checked (a row to lose).
        self._f5_quoting: frozenset[int] | None = None
        #: The (event, market, sides) of the first-five market being checked, for a twin's prices.
        self._f5_context: tuple[int, int, Mapping[str, Mapping[str, Any]]] | None = None
        #: The books the dated rule named while the current offer was read (not applied).
        self.dated_books: frozenset[int] = frozenset()

    def _throttle(self) -> None:
        now = self._rate_clock()
        if self._last_read is not None:
            wait = self._last_read + self._min_gap - now
            if wait > 0:
                self._sleep(wait)
                now = self._rate_clock()
        self._last_read = now

    def _bp_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._throttle()
        self.vendor_reads += 1
        return super()._bp_get(path, params)

    # ------------------------------------------------------------ read failures
    def _note_read_error(self, message: str) -> None:
        market, line_type = self.current_offer or (None, None)
        self.read_errors.append(
            {
                "game_pk": self.current_game,
                "market": market,
                "line_type": line_type,
                "error": message,
            }
        )

    def _offers(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        try:
            return super()._offers(event_id, market_id)
        except Exception as exc:
            what = "first-inning " if market_id in F5_TWIN_MARKETS.values() else ""
            self._note_read_error(
                f"the {what}offers read (market {market_id}) failed: {type(exc).__name__}: {exc}"
            )
            raise

    def _fetch_offers_all_pages(
        self, event_id: int, market_id: int
    ) -> tuple[list[dict[str, Any]], bool]:
        offers, complete = super()._fetch_offers_all_pages(event_id, market_id)
        if not complete:
            self._note_read_error(
                f"a later offers page (market {market_id}) failed: the market is partial"
            )
        return offers, complete

    def _resolve_player_name(self, player_id: int) -> str | None:
        name = super()._resolve_player_name(player_id)
        if name is None:
            self._note_read_error(f"the player lookup (player {player_id}) failed")
        return name

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        event = super()._resolve_event(game_pk)
        if event is None:
            self.game_errors.setdefault(
                int(game_pk), "no vendor event: the lookup failed or matched no event"
            )
        return event

    # ------------------------------------------------------- first-five exclusions
    def _f5_exclusions(
        self,
        event_id: int,
        market_id: int,
        required_sides: Mapping[str, Mapping[str, Any]],
        game_date: date | None,
        *,
        tie: Mapping[str, Any] | None = None,
    ) -> frozenset[int]:
        self._f5_quoting = quoting_books(required_sides)
        self._f5_context = (int(event_id), int(market_id), required_sides)
        try:
            return super()._f5_exclusions(event_id, market_id, required_sides, game_date, tie=tie)
        finally:
            self._f5_quoting = None
            self._f5_context = None

    def _f5_dated_exclusions(
        self,
        event_id: int,
        market_id: int,
        required_sides: Mapping[str, Mapping[str, Any]],
        game_date: date | None,
    ) -> frozenset[int]:
        """Record the dated books (and their log line); apply none of them.

        The provider's rule names the books, each on its own markets
        (``F5_EXCLUDED_MARKETS``); the probe keeps their rows so gate (c) can
        grade them (see the class docstring).
        """
        dated = super()._f5_dated_exclusions(event_id, market_id, required_sides, game_date)
        self.dated_books = self.dated_books | dated
        return frozenset()

    def _log_f5_exclusion(self, event_id: int, market_id: int, who: str, why: str) -> None:
        kind = exclusion_kind(who, why)
        book_id = book_id_from_label(who)
        lost_a_row = not (
            kind == "dated" and self._f5_quoting is not None and book_id not in self._f5_quoting
        )
        key = (self.current_game, int(market_id), who, why)
        if lost_a_row and key not in self._seen_exclusions:
            self._seen_exclusions.add(key)
            entry: dict[str, Any] = {
                "game_pk": self.current_game,
                "market": F5_MARKET_NAMES.get(int(market_id), str(market_id)),
                "who": who,
                "kind": kind,
                "why": why,
            }
            if kind == "twin" and book_id is not None and self._f5_context is not None:
                entry["sides"] = self._twin_sides_detail(book_id)
            self.exclusions.append(entry)
        super()._log_f5_exclusion(event_id, market_id, who, why)

    def _twin_sides_detail(self, book_id: int) -> dict[str, dict[str, Any]]:
        """A twin's prices beside its own first-inning prices and its peers' median.

        The first-inning read is the provider's cached one, so it costs no new
        vendor read inside the time-to-live. A failed read leaves the
        first-inning prices out.
        """
        if self._f5_context is None:
            return {}
        event_id, market_id, required_sides = self._f5_context
        try:
            f1_sides = self._f1_twin_sides(event_id, market_id, required_sides)
        except Exception:  # noqa: BLE001 — the detail is for the printout only
            f1_sides = None
        return twin_detail(required_sides, book_id, f1_sides)


def quoting_books(required_sides: Mapping[str, Mapping[str, Any]]) -> frozenset[int]:
    """PURE: the books with a row to lose on a market's required sides.

    A book with a usable line on every required side (it would give a
    closing row), and the opener when every required side names the same one
    (it would give the opening row). The line pick is the provider's own
    (``_book_line``).
    """
    sides = list(required_sides.values())
    ids: set[int] = set()
    for sel in sides:
        for book in sel.get("books") or []:
            book_id = _opt_int(book.get("id"))
            if book_id is not None:
                ids.add(book_id)
    quoting = {b for b in ids if all(_book_line(sel, b) is not None for sel in sides)}
    openers = {_opt_int((sel.get("opening_line") or {}).get("book_id")) for sel in sides}
    if sides and len(openers) == 1 and None not in openers:
        quoting |= {b for b in openers if b is not None}
    return frozenset(quoting)


def twin_detail(
    required_sides: Mapping[str, Mapping[str, Any]],
    book_id: int,
    f1_sides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """PURE: per side, a twin-excluded book's first-five price and line, its own
    first-inning price, and the other sportsbooks' median at the same line.

    ``deviation`` is the book's implied probability minus that median. A real
    first-five price sits near the median; a first-inning price sits far from
    it. So a twin near the median on every side reads as a false exclusion.
    """
    out: dict[str, dict[str, Any]] = {}
    for side, sel in required_sides.items():
        ln = _book_line(sel, book_id)
        price = None if ln is None else _opt_float(ln.get("cost"))
        line = None if ln is None else _opt_float(ln.get("line"))
        f1_ln = None
        if f1_sides is not None and side in f1_sides:
            f1_ln = _book_line(f1_sides[side], book_id)
        peers: list[float] = []
        for book in sel.get("books") or []:
            other = _opt_int(book.get("id"))
            if other is None or other == book_id or not is_bettable(book_label(other)):
                continue
            other_ln = _book_line(sel, other)
            if other_ln is None or _opt_float(other_ln.get("line")) != line:
                continue
            other_cost = _opt_float(other_ln.get("cost"))
            if other_cost is not None:
                peers.append(implied_probability(other_cost))
        median = statistics.median(peers) if peers else None
        out[side] = {
            "price": price,
            "line": line,
            "f1_price": None if f1_ln is None else _opt_float(f1_ln.get("cost")),
            "peer_median": median,
            "n_peers": len(peers),
            "deviation": (
                None if price is None or median is None else implied_probability(price) - median
            ),
        }
    return out


def exclusion_kind(who: str, why: str) -> str:
    """``dated`` (the dated list), ``opener_twin`` (the opener copies its first inning) or ``twin``."""
    if who == "opener":
        return "opener_twin"
    if why.startswith("excluded from"):
        return "dated"
    return "twin"


# ---------------------------------------------------------------------------
# The game sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeGame:
    """One sampled game: its id, season, month and official (local) date."""

    game_pk: int
    season: int
    month: int
    game_date: str | None = None


def stratified_sample(
    games: Sequence[ProbeGame], n: int, seed: int = DEFAULT_SEED
) -> list[ProbeGame]:
    """PURE: ``n`` games spread evenly over the seasons, then over each season's months.

    A round robin: season by season in order, each season takes one game from
    its next month in turn, each month's games in a seeded random order. A
    season or month that runs out drops out of the rotation.
    """
    rng = random.Random(seed)
    cells: dict[int, dict[int, list[ProbeGame]]] = {}
    for g in sorted(games, key=lambda x: x.game_pk):
        cells.setdefault(g.season, {}).setdefault(g.month, []).append(g)
    for months in cells.values():
        for bucket in months.values():
            rng.shuffle(bucket)
    month_turn = dict.fromkeys(cells, 0)
    picked: list[ProbeGame] = []
    while len(picked) < n:
        progressed = False
        for season in sorted(cells):
            if len(picked) >= n:
                break
            months = [m for m in sorted(cells[season]) if cells[season][m]]
            if not months:
                continue
            month = months[month_turn[season] % len(months)]
            month_turn[season] += 1
            picked.append(cells[season][month].pop())
            progressed = True
        if not progressed:
            break
    return picked


# ---------------------------------------------------------------------------
# One game's read
# ---------------------------------------------------------------------------

_ROW_KEYS: tuple[str, ...] = (
    "book",
    "book_id",
    "line_type",
    "market_type",
    "prop_stat",
    "player_id",
    *GAME_ODDS_FIELDS,
    "over_line",
    "under_line",
    "line",
)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def row_record(row: Mapping[str, Any], refusal: Refusal | None) -> dict[str, Any]:
    """PURE: a provider row as plain JSON, with the guard's verdict and the stamp's distance.

    ``stamp_minus_start_min`` is the row's vendor stamp minus the scheduled
    start, in minutes. A field with no value is left out, so a reader uses
    ``.get``; ``book`` is always present. 300 games write about 60,000 rows.
    SIM-555: a made-up game's row keeps ``postponed_start``, so a re-grade
    applies the guard's postponement rule.
    """
    out: dict[str, Any] = {k: row.get(k) for k in _ROW_KEYS if k in row}
    stamp, start = _aware(row.get("book_line_at")), _aware(row.get("scheduled_start"))
    postponed = _aware(row.get("postponed_start"))
    out["book_line_at"] = None if stamp is None else stamp.isoformat()
    out["scheduled_start"] = None if start is None else start.isoformat()
    out["postponed_start"] = None if postponed is None else postponed.isoformat()
    out["stamp_minus_start_min"] = (
        None if stamp is None or start is None else (stamp - start).total_seconds() / 60.0
    )
    out["refusal"] = None if refusal is None else refusal.rule
    out["refusal_message"] = None if refusal is None else refusal.message
    return {k: v for k, v in out.items() if v is not None}


def _offer(
    prov: Any, market: str, line_type: str, read: Callable[[], list[dict[str, Any]]]
) -> dict[str, Any]:
    """One offer's rows with the guard's verdicts, and its read error (``None`` when clean).

    The error joins an exception the read raised and every failure the
    provider recorded while it read (see :class:`ProbeProvider`). The rows
    of a book the dated first-five rule names go to ``dated_rows``, each
    marked ``dated_excluded``: the loader drops them, so only gate (c)
    reads them.
    """
    if hasattr(prov, "current_offer"):
        prov.current_offer = (market, line_type)
    if hasattr(prov, "dated_books"):
        prov.dated_books = frozenset()
    recorded: list[dict[str, Any]] = getattr(prov, "read_errors", [])
    before = len(recorded)
    errors: list[str] = []
    try:
        rows = read()
    except Exception as exc:  # noqa: BLE001 — one market's failure must not stop the probe
        rows = []
        errors.append(f"{type(exc).__name__}: {exc}")
    errors = [str(e["error"]) for e in recorded[before:]] + errors
    if hasattr(prov, "current_offer"):
        prov.current_offer = None
    dated: frozenset[int] = getattr(prov, "dated_books", frozenset())
    kept = [r for r in rows if r.get("book_id") not in dated]
    dropped = [r for r in rows if r.get("book_id") in dated]
    return {
        "market": market,
        "line_type": line_type,
        "rows": [row_record(r, check_row(r)) for r in kept],
        "dated_rows": [{**row_record(r, check_row(r)), "dated_excluded": True} for r in dropped],
        "error": "; ".join(errors) if errors else None,
    }


def collect_game(
    prov: Any,
    game: ProbeGame,
    *,
    markets: Sequence[str] = GAME_MARKETS,
    prop_player: int | None = None,
) -> dict[str, Any]:
    """Read one game's markets through the provider's by-book methods and run the guard.

    ``prov`` is a :class:`ProbeProvider` (or any object with the two by-book
    methods). ``game_error`` names a game-level failure (no vendor event): every
    offer of such a game is unknown to the gate. Nothing is written anywhere.
    """
    if hasattr(prov, "current_game"):
        prov.current_game = game.game_pk
    offers: list[dict[str, Any]] = []
    for market in markets:
        for lt in LINE_TYPES:
            read = functools.partial(
                prov.get_odds_by_book, game.game_pk, line_type=lt, market_type=market
            )
            offers.append(_offer(prov, market, lt, read))
    if prop_player is not None:
        for lt in LINE_TYPES:
            read = functools.partial(
                prov.get_prop_odds_by_book,
                game.game_pk,
                int(prop_player),
                PROP_MARKET,
                line_type=lt,
            )
            offers.append(_offer(prov, PROP_MARKET, lt, read))
    exclusions = [e for e in getattr(prov, "exclusions", []) if e.get("game_pk") == game.game_pk]
    return {
        "game_pk": game.game_pk,
        "season": game.season,
        "month": game.month,
        "game_date": game.game_date,
        "home_starter": prop_player,
        "offers": offers,
        "f5_exclusions": exclusions,
        "game_error": getattr(prov, "game_errors", {}).get(game.game_pk),
    }


# ---------------------------------------------------------------------------
# PURE: the summary
# ---------------------------------------------------------------------------


def _offers_by_market(game: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{market: {line_type: offer}}`` of one game."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for o in game.get("offers", []):
        out.setdefault(o["market"], {})[o["line_type"]] = o
    return out


def _iter_markets(
    games: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[Mapping[str, Any], str, dict[str, dict[str, Any]]]]:
    for game in games:
        for market, by_lt in _offers_by_market(game).items():
            yield game, market, by_lt


def _rows(by_lt: Mapping[str, Mapping[str, Any]], line_type: str) -> list[dict[str, Any]]:
    offer = by_lt.get(line_type)
    return list(offer["rows"]) if offer else []


def _dated_rows(by_lt: Mapping[str, Mapping[str, Any]], line_type: str) -> list[dict[str, Any]]:
    """The rows the dated first-five rule drops (see :func:`_offer`)."""
    offer = by_lt.get(line_type)
    return list(offer.get("dated_rows") or []) if offer else []


def _kept(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows the loader writes: not refused, and not dropped by the dated rule."""
    return [r for r in rows if r.get("refusal") is None and not r.get("dated_excluded")]


def graded_book(rows: Iterable[Mapping[str, Any]], market: str | None = None) -> str | None:
    """The book the readers grade: the first book on the preference list with a kept row.

    Only sportsbooks count. On a three-way market (the first-inning and
    first-five moneylines) a row that lists the tie comes first, as in the
    backtest (``INCOMPLETE_THREE_WAY_LAST_SQL``): a tie-less row is graded
    only when no sportsbook lists the tie. ``market`` defaults to the rows'
    own ``market_type``.
    """
    kept = [r for r in _kept(rows) if is_bettable(str(r["book"]))]
    if market is None and kept:
        market = kept[0].get("market_type")
    if market is not None and GAME_MARKET_KIND.get(str(market)) == "three_way":
        kept = [r for r in kept if r.get("draw_ml") is not None] or kept
    labels = {str(r["book"]) for r in kept}
    for book_id in GRADED_BOOK_PREFERENCE:
        if book_label(book_id) in labels:
            return book_label(book_id)
    others = sorted(labels, key=lambda b: book_id_from_label(b) or 0)
    return others[0] if others else None


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {"mean": statistics.fmean(values), "min": min(values), "max": max(values)}


def market_summary(games: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """PURE: per market — offers, books quoting, the opener's coverage, rows by book, the graded book."""
    acc: dict[str, dict[str, Any]] = {}
    for game, market, by_lt in _iter_markets(games):
        s = acc.setdefault(
            market,
            {
                "games_read": 0,
                "offered": 0,
                "no_row": 0,
                "errors": 0,
                "_books": [],
                "_sportsbooks": [],
                "with_opener": 0,
                "openers_by_book": Counter(),
                "closing_rows_by_book": Counter(),
                "kept_closing_rows_by_book": Counter(),
                "graded_book": Counter(),
            },
        )
        s["games_read"] += 1
        s["errors"] += sum(1 for o in by_lt.values() if _read_error(game, o))
        closing, opening = _rows(by_lt, "closing"), _rows(by_lt, "opening")
        if not closing and not opening:
            s["no_row"] += 1
            continue
        s["offered"] += 1
        s["_books"].append(len(closing))
        s["_sportsbooks"].append(sum(1 for r in closing if is_bettable(str(r["book"]))))
        if opening:
            s["with_opener"] += 1
            s["openers_by_book"].update(str(r["book"]) for r in opening)
        s["closing_rows_by_book"].update(str(r["book"]) for r in closing)
        s["kept_closing_rows_by_book"].update(str(r["book"]) for r in _kept(closing))
        s["graded_book"][graded_book(closing, market) or "none"] += 1
    out: dict[str, dict[str, Any]] = {}
    for market, s in acc.items():
        out[market] = {
            "games_read": s["games_read"],
            "offered": s["offered"],
            "no_row": s["no_row"],
            "errors": s["errors"],
            "books_quoting": _spread(s["_books"]),
            "sportsbooks_quoting": _spread(s["_sportsbooks"]),
            "opener_coverage": (s["with_opener"] / s["offered"]) if s["offered"] else None,
            "openers_by_book": dict(s["openers_by_book"].most_common()),
            "closing_rows_by_book": dict(s["closing_rows_by_book"].most_common()),
            "kept_closing_rows_by_book": dict(s["kept_closing_rows_by_book"].most_common()),
            "graded_book": dict(s["graded_book"].most_common()),
        }
    return out


def stamp_summary(games: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """PURE: the closing stamp's distance from the scheduled start, by season.

    Per offer, the NEWEST closing stamp minus the scheduled start, in minutes
    (min / median / max over the season's offers); the share of offers whose
    newest stamp is after the start; and the share of offers whose books (the
    blend left out) all carry ONE stamp.
    """
    acc: dict[int, dict[str, Any]] = {}
    for game, _market, by_lt in _iter_markets(games):
        closing = _rows(by_lt, "closing")
        minutes = [
            r["stamp_minus_start_min"]
            for r in closing
            if r.get("stamp_minus_start_min") is not None
        ]
        if not minutes:
            continue
        s = acc.setdefault(int(game["season"]), {"newest": [], "one_stamp": 0})
        s["newest"].append(max(minutes))
        stamps = {
            r.get("book_line_at")
            for r in closing
            if r.get("book") != BLEND and r.get("book_line_at")
        }
        if len(stamps) == 1:
            s["one_stamp"] += 1
    out: dict[str, dict[str, Any]] = {}
    for season in sorted(acc):
        s = acc[season]
        newest = s["newest"]
        out[str(season)] = {
            "offers": len(newest),
            "newest_min": min(newest),
            "newest_median": statistics.median(newest),
            "newest_max": max(newest),
            "after_start_share": sum(1 for m in newest if m > 0) / len(newest),
            "one_stamp_share": s["one_stamp"] / len(newest),
        }
    return out


def refusal_tally(games: Iterable[Mapping[str, Any]]) -> RefusalTally:
    """PURE: the guard's refusals of every row read, by rule, market and book."""
    tally = RefusalTally()
    for _game, market, by_lt in _iter_markets(games):
        for offer in by_lt.values():
            tally.offered(len(offer["rows"]))
            for r in offer["rows"]:
                if r.get("refusal"):
                    tally.add(
                        Refusal(r["refusal"], r.get("refusal_message") or ""),
                        market,
                        str(r["book"]),
                    )
    return tally


def read_error_summary(games: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """PURE: the failed reads — offers with an error, by market, and the games with no event."""
    by_market: Counter[str] = Counter()
    no_event: list[int] = []
    for game in games:
        if game.get("game_error"):
            no_event.append(int(game["game_pk"]))
            continue
        for o in game.get("offers", []):
            if o.get("error"):
                by_market[f"{o['market']} {o['line_type']}"] += 1
    return {
        "offers_errored": sum(by_market.values()),
        "by_market": dict(sorted(by_market.items())),
        "games_without_event": no_event,
    }


def exclusion_summary(games: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """PURE: the provider's first-five exclusions, ``{market: {book: {kind: n}}}``."""
    out: dict[str, dict[str, Counter[str]]] = {}
    for game in games:
        for e in game.get("f5_exclusions", []):
            out.setdefault(e["market"], {}).setdefault(e["who"], Counter())[e["kind"]] += 1
    return {m: {b: dict(c) for b, c in sorted(books.items())} for m, books in sorted(out.items())}


def twin_exclusions(games: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """PURE: every twin exclusion with its prices (review fix 2026-09-28).

    Each entry names the game, the market and the book, and per side the
    book's first-five price, its own first-inning price and the other
    sportsbooks' median (:func:`twin_detail`). A twin whose prices sit near
    the median on every side is a real first-five line the rule dropped.
    """
    out: list[dict[str, Any]] = []
    for game in games:
        for e in game.get("f5_exclusions", []):
            if e.get("kind") == "twin":
                out.append(
                    {
                        "game_pk": game.get("game_pk"),
                        "date": game.get("game_date"),
                        "market": e["market"],
                        "book": e["who"],
                        "sides": e.get("sides") or {},
                    }
                )
    return out


def _twin_line(entry: Mapping[str, Any]) -> str:
    """One twin exclusion for the printout: its prices, its first-inning prices, the peers."""
    parts = []
    for side, d in sorted(entry["sides"].items()):
        median = d.get("peer_median")
        dev = d.get("deviation")
        parts.append(
            f"{side} {d.get('price')} (f1 {d.get('f1_price')}; peers "
            f"{'-' if median is None else f'{median:.3f}'} n={d.get('n_peers')}; "
            f"off {'-' if dev is None else f'{dev:+.3f}'})"
        )
    return f"{entry['game_pk']} {entry['date']} {entry['market']} {entry['book']}: " + ", ".join(
        parts
    )


# ---------------------------------------------------------------------------
# PURE: the gate
# ---------------------------------------------------------------------------


def _is_two_way(market: str) -> bool:
    return market == PROP_MARKET or GAME_MARKET_KIND.get(market) != "three_way"


def _read_error(game: Mapping[str, Any], *offers: Mapping[str, Any] | None) -> str | None:
    """The game's read failure, else the first failure among ``offers``, else ``None``."""
    if game.get("game_error"):
        return str(game["game_error"])
    for offer in offers:
        if offer and offer.get("error"):
            return str(offer["error"])
    return None


def _verdict(failed: bool, unknown: bool) -> str:
    """FAIL on a failure; else UNKNOWN when a read failed; else PASS."""
    return "FAIL" if failed else "UNKNOWN" if unknown else "PASS"


def gate_graded_coverage(games: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """PURE: gate (a) — every two-way offer has a kept closing row from a preferred book.

    An offer is a (game, market) with any row at either line type. The three-way
    moneylines are left out (the gate names two-way offers). A (game, market)
    without a preferred closing row whose read failed (the closing read, or any
    read of a market with no row at all, or the game's event) is UNKNOWN, not
    "not offered": the gate cannot pass on it. Re-read those games with
    ``--game-pks``.
    """
    preferred = {book_label(b) for b in GRADED_BOOK_PREFERENCE}
    offers = 0
    failures: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for game, market, by_lt in _iter_markets(games):
        if not _is_two_way(market):
            continue
        closing, opening = _rows(by_lt, "closing"), _rows(by_lt, "opening")
        if any(str(r["book"]) in preferred for r in _kept(closing)):
            offers += 1
            continue
        closing_error = _read_error(game, by_lt.get("closing"))
        any_error = _read_error(game, *by_lt.values())
        if closing_error or (any_error and not closing and not opening):
            offers += 1
            unknown.append(
                {
                    "game_pk": game["game_pk"],
                    "season": game["season"],
                    "market": market,
                    "error": closing_error or any_error,
                }
            )
            continue
        if not closing and not opening:
            continue
        offers += 1
        failures.append(
            {
                "game_pk": game["game_pk"],
                "season": game["season"],
                "market": market,
                "closing_books": sorted(str(r["book"]) for r in closing),
                "refused": sorted(
                    f"{r['book']}:{r['refusal']}" for r in closing if r.get("refusal")
                ),
            }
        )
    return {
        "offers": offers,
        "covered": offers - len(failures) - len(unknown),
        "n_failures": len(failures),
        "failures": failures,
        "n_unknown": len(unknown),
        "unknown": unknown,
        "unknown_games": sorted({int(u["game_pk"]) for u in unknown}),
        "verdict": _verdict(bool(failures), bool(unknown)),
        "pass": not failures and not unknown,
    }


def _side_quotes(market: str, row: Mapping[str, Any]) -> list[tuple[str, Any, float | None]]:
    """(side, the side's line or structure, price) of one first-five row."""
    kind = GAME_MARKET_KIND[market]
    if kind == "runline":
        return [
            ("home", row.get("home_spread"), row.get("home_spread_ml")),
            ("away", row.get("away_spread"), row.get("away_spread_ml")),
        ]
    if kind == "total":
        # A provider row carries each side's own line; a stored row keeps one.
        line = row.get("total_line")
        over_line = row.get("over_line")
        under_line = row.get("under_line")
        return [
            ("over", line if over_line is None else over_line, row.get("over_ml")),
            ("under", line if under_line is None else under_line, row.get("under_ml")),
        ]
    structure = "tie" if row.get("draw_ml") is not None else "no_tie"
    quotes: list[tuple[str, Any, float | None]] = [
        ("home", structure, row.get("home_ml")),
        ("away", structure, row.get("away_ml")),
    ]
    if row.get("draw_ml") is not None:
        quotes.append(("draw", structure, row.get("draw_ml")))
    return quotes


def median_deviation(market: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """PURE: ``{book: [side, ...]}`` for the sides priced far from the other books.

    A side is far when its implied probability sits more than
    ``MEDIAN_DEVIATION_MAX`` from the median of at least ``MIN_PEERS`` other
    sportsbooks' kept rows at the same line on the same side.
    """
    peers_by_side: dict[tuple[str, Any], list[tuple[str, float]]] = {}
    for r in _kept(rows):
        book = str(r["book"])
        if book_kind(book) != "sportsbook":
            continue
        for side, line, price in _side_quotes(market, r):
            if line is not None and price is not None:
                peers_by_side.setdefault((side, line), []).append(
                    (book, implied_probability(price))
                )
    out: dict[str, list[str]] = {}
    for r in rows:
        book = str(r["book"])
        for side, line, price in _side_quotes(market, r):
            if line is None or price is None:
                continue
            peers = [p for b, p in peers_by_side.get((side, line), []) if b != book]
            if len(peers) < MIN_PEERS:
                continue
            if abs(implied_probability(price) - statistics.median(peers)) > MEDIAN_DEVIATION_MAX:
                out.setdefault(book, []).append(side)
    return out


def rows_are_twins(market: str, f5_row: Mapping[str, Any], f1_row: Mapping[str, Any]) -> bool:
    """PURE: does a first-five row copy the same book's first-inning row?

    Every side at the same line (or structure) with implied probabilities within
    the provider's twin tolerance for the market (``TWIN_PRICE_TOLERANCE``;
    identical prices on the moneyline, ``TWIN_MONEYLINE_PRICE_TOLERANCE``). On
    the three-way moneyline a first-five tie price that the first-inning row
    lacks, or that differs, rules the copy out (as in the provider); a
    first-five row with no tie leaves the teams to decide.
    """
    kind = GAME_MARKET_KIND[market]
    tolerance = twin_price_tolerance(F5_MARKET_IDS[market])
    f5 = {side: (line, price) for side, line, price in _side_quotes(market, f5_row)}
    f1 = {side: (line, price) for side, line, price in _side_quotes(F5_TWIN_OF[market], f1_row)}
    for side in ("over", "under") if kind == "total" else ("home", "away"):
        a, b = f5.get(side), f1.get(side)
        if a is None or b is None or a[1] is None or b[1] is None:
            return False
        # A three-way side's "line" is its structure (a tie or none); the tie
        # is compared below, so only the lines of the other kinds must match.
        if kind != "three_way" and a[0] != b[0]:
            return False
        if abs(implied_probability(a[1]) - implied_probability(b[1])) > tolerance:
            return False
    if kind == "three_way" and f5_row.get("draw_ml") is not None:
        f1_draw = f1_row.get("draw_ml")
        if f1_draw is None:
            return False
        if abs(implied_probability(f5_row["draw_ml"]) - implied_probability(f1_draw)) > tolerance:
            return False
    return True


def first_inning_flags(game: Mapping[str, Any]) -> list[dict[str, Any]]:
    """PURE: every first-five row of one game (opening and closing), with its first-inning flags.

    ``median_dev`` (closing rows only), ``twin_f1`` and ``guard_shape``; a row
    with no flag carries an empty list. The rows the dated rule drops are
    flagged too, against the kept books' median, and carry ``dated: True``.
    """
    by_market = _offers_by_market(game)
    out: list[dict[str, Any]] = []
    for market, twin_market in F5_TWIN_OF.items():
        by_lt = by_market.get(market, {})
        f1_by_lt = by_market.get(twin_market, {})
        for lt in LINE_TYPES:
            rows = _rows(by_lt, lt) + _dated_rows(by_lt, lt)
            if not rows:
                continue
            far = median_deviation(market, rows) if lt == "closing" else {}
            f1_rows = {str(r["book"]): r for r in _rows(f1_by_lt, lt)}
            for r in rows:
                book = str(r["book"])
                flags: list[str] = []
                if book in far:
                    flags.append("median_dev")
                if book in f1_rows and rows_are_twins(market, r, f1_rows[book]):
                    flags.append("twin_f1")
                if r.get("refusal") in FIRST_INNING_SHAPE_RULES:
                    flags.append("guard_shape")
                out.append(
                    {
                        "game_pk": game["game_pk"],
                        "season": game["season"],
                        "date": game.get("game_date"),
                        "market": market,
                        "line_type": lt,
                        "book": book,
                        "flags": flags,
                        "dated": bool(r.get("dated_excluded")),
                        "refused": r.get("refusal") is not None,
                    }
                )
    return out


def dated_rule_covers(book: str, market: str) -> bool:
    """PURE: True when the dated exclusion names this book on this first-five market.

    The book is on ``F5_EXCLUDED_BOOKS`` and the market is one of the
    markets ``F5_EXCLUDED_MARKETS`` lists for it (every first-five market
    when it lists none).
    """
    book_id = book_id_from_label(book)
    market_id = F5_MARKET_IDS.get(market)
    if book_id is None or market_id is None or book_id not in F5_EXCLUDED_BOOKS:
        return False
    return market_id in f5_excluded_markets(book_id)


def excluded_listing() -> dict[str, str]:
    """The dated exclusion for the printout and the params: ``{'bp:12': '2025-03-01 on ...'}``."""
    out: dict[str, str] = {}
    for book_id, start in F5_EXCLUDED_BOOKS.items():
        markets = [F5_MARKET_NAMES[m] for m in F5_TWIN_MARKETS if m in f5_excluded_markets(book_id)]
        out[book_label(book_id)] = f"{start.isoformat()} on {', '.join(markets)}"
    return out


def _date_fix(entry: Mapping[str, Any]) -> str:
    """The fix gate (c) names for one flagged (market, line type, book) entry."""
    book, name = entry["book"], entry["name"]
    first = entry["first_flag_date"]
    counts = (
        f"{entry['flagged']} of its {entry['n']} {entry['market']} {entry['line_type']} rows "
        "are first-inning-shaped"
    )
    book_id = book_id_from_label(book)
    start = F5_EXCLUDED_BOOKS.get(book_id) if book_id is not None else None
    if start is None:
        return (
            f"add {book} ({name}) to F5_EXCLUDED_BOOKS, dated on or before "
            f"{first or 'its first flagged game'}: {counts}"
        )
    if not dated_rule_covers(book, entry["market"]):
        return (
            f"add {entry['market']} to the F5_EXCLUDED_MARKETS of {book} ({name}), whose "
            f"exclusion from {start} leaves that market out: {counts} (first {first})"
        )
    if first is not None and first < start.isoformat():
        return f"move {book} ({name})'s date from {start} to {first}: {counts} before {start}"
    return (
        f"{book} ({name}) is on F5_EXCLUDED_BOOKS from {start}, yet {counts} and the dated "
        f"rule did not drop them (first {first}): check those games' dates"
    )


def _flag_table(
    per: Mapping[tuple[str, str, str], Mapping[str, Any]], share_max: float
) -> list[dict[str, Any]]:
    """The per (market, line type, book) rows of a flag count, sorted."""
    table: list[dict[str, Any]] = []
    for (market, lt, book), s in sorted(per.items()):
        share = s["flagged"] / s["n"] if s["n"] else 0.0
        dates = sorted(s["flag_dates"])
        table.append(
            {
                "market": market,
                "line_type": lt,
                "book": book,
                "name": book_display_name(book),
                "kind": book_kind(book),
                "n": s["n"],
                "flagged": s["flagged"],
                "share": share,
                "fails": share >= share_max,
                "reasons": dict(s["reasons"]),
                "by_season": s["by_season"],
                "first_flag_date": dates[0] if dates else None,
                "last_flag_date": dates[-1] if dates else None,
            }
        )
    return table


def _dated_fix(entry: Mapping[str, Any], share_max: float) -> str:
    """The fix gate (c) names for a dated exclusion that its own rows do not bear out."""
    book_id = book_id_from_label(entry["book"])
    start = F5_EXCLUDED_BOOKS.get(book_id) if book_id is not None else None
    return (
        f"the dated exclusion of {entry['book']} ({entry['name']}) is not borne out on "
        f"{entry['market']}: {entry['flagged']} of its {entry['n']} closing rows from {start} "
        f"are first-inning-shaped (below {share_max:.1%}); take {entry['market']} out of "
        "its F5_EXCLUDED_MARKETS, or check those rows by hand"
    )


def gate_first_five(
    games: Iterable[Mapping[str, Any]], *, share_max: float = FIRST_INNING_SHARE_MAX
) -> dict[str, Any]:
    """PURE: gates (b) and (c) — first-inning-shaped first-five rows per market, line type and book.

    (b) passes when every (market, line type, book) carries a flagged share
    below ``share_max`` and every first-five read succeeded. The share is
    graded per line type: the median check runs on the closing rows only, so a
    book's opening rows never dilute its closing share. A (game, market, line
    type) whose first-five read or first-inning read failed is UNKNOWN: its
    rows could not be checked. (b) reads the rows the loader writes only: a
    row the load guard refuses is never stored, so it is left out of the
    share and counted in ``guard_refused`` instead (its shape is still worth
    reading: a book whose first-five ties are refused game after game posts
    first-inning bets under the first-five label).

    (b) and (c) grade the bettable books only (:func:`is_bettable`). A row
    of the blend, a pick'em app, an exchange or a prediction market is
    stored but never graded or offered, so its flags go to
    ``non_bettable_flags`` (census fix 2026-09-28): printed beside the gate,
    never failing it.

    (c) confirms the dated exclusion (``F5_EXCLUDED_BOOKS``, each book on the
    markets ``F5_EXCLUDED_MARKETS`` lists for it). It fails for a book off
    the list that fails (b): add it, dated on or before its first flagged
    game; and for a listed book that fails (b) on a market its exclusion
    leaves out: add the market. It fails for a book on the list with ANY
    flagged row, among the rows the loader writes, on one of its markets:
    the provider drops that book's rows there from its date on, so a
    flagged row the loader would still write sits before the date. Move
    the date to the first flagged game. Review fix (2026-09-28): it also
    fails when the rows the dated rule drops do not bear it out, a listed
    book whose closing rows from its date carry a flagged share below
    ``share_max`` on a market (``dated_table``; the exclusion then drops real
    first-five rows of that market). ``fixes`` names each fix.
    """
    games = list(games)
    per: dict[tuple[str, str, str], dict[str, Any]] = {}
    per_dated: dict[tuple[str, str, str], dict[str, Any]] = {}
    per_other: dict[tuple[str, str, str], dict[str, Any]] = {}
    guard_refused: Counter[tuple[str, str, str]] = Counter()
    unknown: list[dict[str, Any]] = []
    for game in games:
        by_market = _offers_by_market(game)
        for market, twin_market in F5_TWIN_OF.items():
            for lt in LINE_TYPES:
                f5_offer = by_market.get(market, {}).get(lt)
                if f5_offer is None:
                    continue  # the market was not read
                error = _read_error(game, f5_offer, by_market.get(twin_market, {}).get(lt))
                if error:
                    unknown.append(
                        {
                            "game_pk": game["game_pk"],
                            "market": market,
                            "line_type": lt,
                            "error": error,
                        }
                    )
        for f in first_inning_flags(game):
            if f.get("refused") and not f.get("dated"):
                # Never stored: the guard refused it. Counted, not graded.
                guard_refused[(f["market"], f["line_type"], f["book"])] += 1
                continue
            if not is_bettable(f["book"]):
                target = per_other  # stored, never graded or offered: listed apart
            elif f.get("dated"):
                target = per_dated
            else:
                target = per
            s = target.setdefault(
                (f["market"], f["line_type"], f["book"]),
                {"n": 0, "flagged": 0, "reasons": Counter(), "by_season": {}, "flag_dates": []},
            )
            s["n"] += 1
            season = s["by_season"].setdefault(str(f["season"]), [0, 0])
            season[0] += 1
            if f["flags"]:
                s["flagged"] += 1
                season[1] += 1
                s["reasons"].update(f["flags"])
                if f.get("date"):
                    s["flag_dates"].append(str(f["date"]))
    table = _flag_table(per, share_max)
    dated_table = _flag_table(per_dated, share_max)
    non_bettable = [
        {k: v for k, v in e.items() if k != "fails"}
        for e in _flag_table(per_other, share_max)
        if e["flagged"]
    ]
    failing = [e for e in table if e["fails"]]
    outside = [e for e in failing if not dated_rule_covers(e["book"], e["market"])]
    listed_flagged = [
        e for e in table if e["flagged"] and dated_rule_covers(e["book"], e["market"])
    ]
    # The median check runs on the closing rows only, so only they can show
    # that the dated rule is not needed.
    not_needed = [e for e in dated_table if e["line_type"] == "closing" and not e["fails"]]
    verdict_b = _verdict(bool(failing), bool(unknown))
    verdict_c = _verdict(bool(outside or listed_flagged or not_needed), bool(unknown))
    return {
        "share_max": share_max,
        "table": table,
        "dated_table": dated_table,
        "failing": [(e["market"], e["line_type"], e["book"]) for e in failing],
        "failing_outside_excluded": [(e["market"], e["line_type"], e["book"]) for e in outside],
        "flagged_on_the_list": [(e["market"], e["line_type"], e["book"]) for e in listed_flagged],
        "dated_not_needed": [(e["market"], e["line_type"], e["book"]) for e in not_needed],
        "guard_refused": [
            {"market": m, "line_type": lt, "book": b, "n": n}
            for (m, lt, b), n in sorted(guard_refused.items())
        ],
        "non_bettable_flags": non_bettable,
        "fixes": [
            *(_date_fix(e) for e in (*outside, *listed_flagged)),
            *(_dated_fix(e, share_max) for e in not_needed),
        ],
        "n_unknown": len(unknown),
        "unknown": unknown,
        "unknown_games": sorted({int(u["game_pk"]) for u in unknown}),
        "verdict_b": verdict_b,
        "verdict_c": verdict_c,
        "pass_b": verdict_b == "PASS",
        "pass_c": verdict_c == "PASS",
    }


def summarise(games: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """PURE: the whole summary and the gate from the games read."""
    tally = refusal_tally(games)
    return {
        "markets": market_summary(games),
        "stamps": stamp_summary(games),
        "refusals": {
            "offered": tally.n_offered,
            "refused": tally.refused,
            "counts": [
                {"rule": rule, "market": market, "book": book, "n": n}
                for (rule, market, book), n in sorted(tally.counts().items())
            ],
            "text": tally.summary(),
        },
        "f5_exclusions": exclusion_summary(games),
        "f5_twins": twin_exclusions(games),
        "read_errors": read_error_summary(games),
        "gate": {
            "a_graded_coverage": gate_graded_coverage(games),
            "bc_first_five": gate_first_five(games),
        },
    }


# ---------------------------------------------------------------------------
# The printout
# ---------------------------------------------------------------------------


def format_summary(summary: Mapping[str, Any]) -> str:
    lines: list[str] = ["== Markets"]
    for market, s in summary["markets"].items():
        bq, sq = s["books_quoting"], s["sportsbooks_quoting"]
        cov = s["opener_coverage"]
        lines.append(
            f"  {market:13s} offered {s['offered']:4d}/{s['games_read']:4d}"
            f"  books {bq['mean'] or 0:.1f} ({bq['min']}-{bq['max']})"
            f"  sportsbooks {sq['mean'] or 0:.1f} ({sq['min']}-{sq['max']})"
            f"  opener {0 if cov is None else cov:.0%}  errors {s['errors']}"
        )
        lines.append(f"      graded book: {s['graded_book']}")
        lines.append(f"      closing rows by book: {s['closing_rows_by_book']}")
        lines.append(f"      openers: {s['openers_by_book']}")
    lines.append("== The newest closing stamp minus the scheduled start (minutes), by season")
    for season, s in summary["stamps"].items():
        lines.append(
            f"  {season}: offers {s['offers']:4d}  min {s['newest_min']:+8.1f}  median "
            f"{s['newest_median']:+8.1f}  max {s['newest_max']:+8.1f}  after start "
            f"{s['after_start_share']:.0%}  one stamp {s['one_stamp_share']:.0%}"
        )
    lines.append("== The guard")
    lines.append(summary["refusals"]["text"])
    lines.append("== The provider's first-five exclusions (market: book: kind count)")
    for market, books in summary["f5_exclusions"].items():
        lines.append(f"  {market}: {books}")
    twins = summary.get("f5_twins") or []
    if twins:
        lines.append(
            "  the twins, each side's price (its own first-inning price; the other "
            "sportsbooks' median implied probability; the gap):"
        )
        for entry in twins[:40]:
            lines.append(f"    {_twin_line(entry)}")
    errs = summary["read_errors"]
    lines.append(
        f"== Failed reads: {errs['offers_errored']} offers, {len(errs['games_without_event'])} "
        f"games with no vendor event"
    )
    for market, n in errs["by_market"].items():
        lines.append(f"  {market}: {n}")
    gate = summary["gate"]
    a = gate["a_graded_coverage"]
    lines.append("== THE GATE")
    lines.append(
        f"  (a) every two-way offer quoted by a preferred book: {a['verdict']} "
        f"({a['covered']}/{a['offers']} offers; {a['n_failures']} failures, "
        f"{a['n_unknown']} unknown after a failed read)"
    )
    for f in a["failures"][:15]:
        lines.append(f"      {f}")
    if a["unknown_games"]:
        lines.append(f"      read again: --game-pks {' '.join(map(str, a['unknown_games']))}")
    bc = gate["bc_first_five"]
    lines.append(
        f"  (b) first-inning-shaped first-five rows below {bc['share_max']:.1%} per market, "
        f"line type and bettable book: {bc['verdict_b']} ({len(bc['failing'])} failing, "
        f"{bc['n_unknown']} unknown after a failed read)"
    )
    for e in bc["table"]:
        if e["flagged"]:
            lines.append(
                f"      {e['market']:13s} {e['line_type']:7s} {e['name'][:14]:14s} {e['book']:6s} "
                f"{e['flagged']:3d}/{e['n']:3d} ({e['share']:.1%}) {e['reasons']} first "
                f"{e['first_flag_date']} last {e['last_flag_date']}"
                f"{'  FAILS' if e['fails'] else ''}"
            )
    for g in bc.get("guard_refused", []):
        lines.append(
            f"      refused by the load guard, not stored: {g['market']:13s} {g['line_type']:7s} "
            f"{g['book']:6s} {g['n']:3d}"
        )
    non_bettable = bc.get("non_bettable_flags", [])
    if non_bettable:
        lines.append(
            "      not graded (the blend, a pick'em app, an exchange or a prediction market: "
            "stored, never graded or offered):"
        )
    for e in non_bettable:
        lines.append(
            f"        {e['market']:13s} {e['line_type']:7s} {e['name'][:14]:14s} {e['book']:6s} "
            f"{e['flagged']:3d}/{e['n']:3d} ({e['share']:.1%}) {e['reasons']} first "
            f"{e['first_flag_date']} last {e['last_flag_date']}"
        )
    if bc["unknown_games"]:
        lines.append(f"      read again: --game-pks {' '.join(map(str, bc['unknown_games']))}")
    lines.append(
        f"  (c) the dated exclusion (F5_EXCLUDED_BOOKS on F5_EXCLUDED_MARKETS) "
        f"{excluded_listing()} is right: {bc['verdict_c']}"
    )
    for e in bc.get("dated_table", []):
        lines.append(
            f"      dated rows: {e['market']:13s} {e['line_type']:7s} {e['name'][:14]:14s} "
            f"{e['book']:6s} {e['flagged']:3d}/{e['n']:3d} ({e['share']:.1%}) {e['reasons']} "
            f"first {e['first_flag_date']} last {e['last_flag_date']}"
        )
    for fix in bc["fixes"]:
        lines.append(f"      {fix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PURE: the regrade of a saved census (--regate)
# ---------------------------------------------------------------------------

#: The keys a saved row keeps its verdicts under; the regrade writes them afresh.
_VERDICT_KEYS: frozenset[str] = frozenset({"refusal", "refusal_message", "dated_excluded"})


def rule_params() -> dict[str, Any]:
    """The rules a census or a regrade runs under, for the saved params."""
    return {
        "graded_book_preference": list(GRADED_BOOK_PREFERENCE),
        "f5_excluded_books": {str(k): v.isoformat() for k, v in F5_EXCLUDED_BOOKS.items()},
        "f5_excluded_markets": {
            str(book_id): sorted(F5_MARKET_NAMES[m] for m in f5_excluded_markets(book_id))
            for book_id in F5_EXCLUDED_BOOKS
        },
        "twin_price_tolerance": TWIN_PRICE_TOLERANCE,
        "twin_moneyline_price_tolerance": TWIN_MONEYLINE_PRICE_TOLERANCE,
        "median_deviation_max": MEDIAN_DEVIATION_MAX,
        "first_inning_share_max": FIRST_INNING_SHARE_MAX,
        "f5_win_or_tie_sum_max": F5_WIN_OR_TIE_SUM_MAX,
        "f5_tie_implied_max": F5_TIE_IMPLIED_MAX,
        "three_way_sum_min": THREE_WAY_SUM_MIN,
        "f5_total_line_max": F5_TOTAL_LINE_MAX,
    }


def _parse_stamp(value: Any) -> datetime | None:
    """A saved ISO stamp as an aware datetime (a naive one reads as UTC); ``None`` if unreadable."""
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def guard_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """PURE: a saved row as the load guard reads it.

    :func:`row_record` wrote the stamps as ISO text (the vendor stamp, the
    scheduled start and, SIM-555, a made-up game's postponed start); the
    guard's stamp rules need datetimes, so they come back as aware datetimes.
    It also left out every field with no value: a total that kept one side's
    line gets the other side's back as ``None``, so the guard still compares
    the two.
    """
    row = {k: v for k, v in record.items() if k not in _VERDICT_KEYS}
    row["book_line_at"] = _parse_stamp(record.get("book_line_at"))
    row["scheduled_start"] = _parse_stamp(record.get("scheduled_start"))
    row["postponed_start"] = _parse_stamp(record.get("postponed_start"))
    if "over_line" in row or "under_line" in row:
        row.setdefault("over_line", None)
        row.setdefault("under_line", None)
    return row


def _game_day(game: Mapping[str, Any]) -> date | None:
    """The game's official date, or ``None`` when the saved game has none."""
    value = game.get("game_date")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _is_dated(record: Mapping[str, Any], market: str, day: date | None) -> bool:
    """True when the CURRENT dated exclusion drops this row (a first-five market only)."""
    market_id = F5_MARKET_IDS.get(market)
    if market_id is None:
        return False
    book_id = _opt_int(record.get("book_id"))
    if book_id is None:
        book_id = book_id_from_label(record.get("book"))
    return book_id is not None and is_f5_dated_excluded(book_id, market_id, day)


def _regate_exclusions(
    game: Mapping[str, Any], offers: Sequence[Mapping[str, Any]], day: date | None
) -> list[dict[str, Any]]:
    """The game's first-five exclusions under the CURRENT dated rule.

    The twin and opener-twin entries stand. A saved dated entry stays when the
    current rule still drops that book on that market; a (market, book) with
    a dated row and no entry gets one.
    """
    out: list[dict[str, Any]] = []
    for e in game.get("f5_exclusions", []):
        if e.get("kind") != "dated":
            out.append(dict(e))
            continue
        book_id = book_id_from_label(e.get("who"))
        market_id = F5_MARKET_IDS.get(str(e.get("market")))
        if (
            book_id is not None
            and market_id is not None
            and is_f5_dated_excluded(book_id, market_id, day)
        ):
            out.append(dict(e))
    have = {(e["market"], e["who"]) for e in out if e.get("kind") == "dated"}
    for offer in offers:
        for r in offer.get("dated_rows") or []:
            key = (str(offer["market"]), str(r["book"]))
            book_id = book_id_from_label(key[1])
            if key in have or book_id is None or book_id not in F5_EXCLUDED_BOOKS:
                continue
            have.add(key)
            out.append(
                {
                    "game_pk": game.get("game_pk"),
                    "market": key[0],
                    "who": key[1],
                    "kind": "dated",
                    "why": f"excluded from {F5_EXCLUDED_BOOKS[book_id]}",
                }
            )
    return out


def regate_game(
    game: Mapping[str, Any], changes: dict[str, Counter[tuple[str, str, str]]] | None = None
) -> dict[str, Any]:
    """PURE: one saved game under the CURRENT load guard and the CURRENT dated rule.

    Every stored row, kept or dated, is shown to :func:`check_row` again (its
    stamps parsed back, see :func:`guard_view`) and gets a fresh verdict. Every
    row of a first-five market is then put in ``rows`` or in ``dated_rows``
    (marked ``dated_excluded``) by :func:`is_f5_dated_excluded` on the game's
    date. ``changes``, when given, counts what moved: a refusal added or
    removed ``(rule, market, book)``, a row moved to the kept rows or to the
    dated rows ``(market, line type, book)``.
    """
    day = _game_day(game)
    offers: list[dict[str, Any]] = []
    for offer in game.get("offers", []):
        market, line_type = str(offer["market"]), str(offer["line_type"])
        kept: list[dict[str, Any]] = []
        dated: list[dict[str, Any]] = []
        saved = [(False, r) for r in offer.get("rows", [])]
        saved += [(True, r) for r in offer.get("dated_rows") or []]
        for was_dated, record in saved:
            out = {k: v for k, v in record.items() if k not in _VERDICT_KEYS}
            refusal = check_row(guard_view(record))
            if refusal is not None:
                out["refusal"] = refusal.rule
                out["refusal_message"] = refusal.message
            now_dated = _is_dated(record, market, day)
            if changes is not None:
                book = str(record.get("book"))
                old_rule, new_rule = record.get("refusal"), out.get("refusal")
                if old_rule != new_rule:
                    if old_rule:
                        changes["refusals_removed"][(str(old_rule), market, book)] += 1
                    if new_rule:
                        changes["refusals_added"][(str(new_rule), market, book)] += 1
                if now_dated != was_dated:
                    moved = "rows_to_dated" if now_dated else "rows_to_kept"
                    changes[moved][(market, line_type, book)] += 1
            if now_dated:
                dated.append({**out, "dated_excluded": True})
            else:
                kept.append(out)
        new_offer = {k: v for k, v in offer.items() if k not in ("rows", "dated_rows")}
        new_offer["rows"] = kept
        new_offer["dated_rows"] = dated
        offers.append(new_offer)
    regated = {k: v for k, v in game.items() if k not in ("offers", "f5_exclusions")}
    regated["offers"] = offers
    regated["f5_exclusions"] = _regate_exclusions(game, offers, day)
    return regated


#: The four counts a regrade reports, in print order.
REGATE_CHANGES: tuple[str, ...] = (
    "refusals_added",
    "refusals_removed",
    "rows_to_kept",
    "rows_to_dated",
)


def regate(saved: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """PURE: a saved census (the ``--out`` JSON) re-graded under the CURRENT rules.

    Returns the same shape as a census run: ``params`` (the saved ones, the
    current rules, ``regated_from``), ``summary`` (with the gate, plus
    ``regate``: what moved) and ``games``. No vendor read, no database read.
    """
    changes: dict[str, Counter[tuple[str, str, str]]] = {k: Counter() for k in REGATE_CHANGES}
    games = [regate_game(g, changes) for g in saved.get("games", [])]
    params = dict(saved.get("params") or {})
    params.update(rule_params())
    params["regated_from"] = source
    summary = summarise(games)
    key_names = {
        "refusals_added": ("rule", "market", "book"),
        "refusals_removed": ("rule", "market", "book"),
        "rows_to_kept": ("market", "line_type", "book"),
        "rows_to_dated": ("market", "line_type", "book"),
    }
    summary["regate"] = {
        name: [
            {**dict(zip(key_names[name], key, strict=True)), "n": n}
            for key, n in sorted(changes[name].items())
        ]
        for name in REGATE_CHANGES
    }
    return {"params": params, "summary": summary, "games": games}


def format_regate_changes(regate_summary: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """What a regrade moved, for the printout."""
    lines = ["== The regrade: what the current rules changed"]
    for name in REGATE_CHANGES:
        entries = regate_summary.get(name) or []
        lines.append(f"  {name.replace('_', ' ')}: {sum(int(e['n']) for e in entries)}")
        for e in entries:
            head = e.get("rule") or e.get("line_type")
            lines.append(f"    {head:28s} {e['market']:13s} {e['book']:6s} {e['n']:4d}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database reads (SELECT only, one read-only transaction)
# ---------------------------------------------------------------------------

CANDIDATES_SQL = """
SELECT g.game_pk, g.season, EXTRACT(MONTH FROM g.game_date)::int AS month, g.game_date
FROM raw.games g
WHERE g.status = 'Final' AND g.season = ANY($1::int[])
  AND EXISTS (
      SELECT 1 FROM raw.game_odds o
      WHERE o.game_pk = g.game_pk AND o.market_type = 'moneyline' AND o.line_type = 'closing')
ORDER BY g.game_pk
"""

#: The home starter: the official box score first.
BOX_STARTER_SQL = """
SELECT game_pk, player_id
FROM raw.game_player_stats
WHERE game_pk = ANY($1::int[]) AND side = 'home' AND p_started
"""

#: Then the lineup's starting pitcher.
LINEUP_STARTER_SQL = """
SELECT DISTINCT ON (l.game_pk) l.game_pk, l.player_id
FROM raw.game_lineups l
JOIN raw.games g ON g.game_pk = l.game_pk
WHERE l.game_pk = ANY($1::int[]) AND l.team_id = g.home_team_id
  AND l.position_code = 'P' AND l.is_starter
ORDER BY l.game_pk, l.sequence
"""


async def load_sample(
    dsn: str, seasons: Sequence[int], n_games: int, seed: int, game_pks: Sequence[int] | None
) -> tuple[list[ProbeGame], dict[int, int]]:
    """The sampled games and their home starters, read in one read-only transaction."""
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        async with conn.transaction(readonly=True):
            candidates = [
                ProbeGame(int(r["game_pk"]), int(r["season"]), int(r["month"]), str(r["game_date"]))
                for r in await conn.fetch(CANDIDATES_SQL, list(seasons))
            ]
            if game_pks:
                wanted = {int(g) for g in game_pks}
                sample = [g for g in candidates if g.game_pk in wanted]
            else:
                sample = stratified_sample(candidates, n_games, seed)
            pks = [g.game_pk for g in sample]
            starters = {
                int(r["game_pk"]): int(r["player_id"])
                for r in await conn.fetch(LINEUP_STARTER_SQL, pks)
            }
            starters.update(
                {
                    int(r["game_pk"]): int(r["player_id"])
                    for r in await conn.fetch(BOX_STARTER_SQL, pks)
                }
            )
    finally:
        await conn.close()
    return sample, starters


def _progress(i: int, n: int, game: Mapping[str, Any]) -> str:
    counts = []
    for o in game["offers"]:
        if o["line_type"] == "closing":
            counts.append(f"{o['market']}={len(o['rows'])}{'!' if o['error'] else ''}")
    note = f" [{game['game_error']}]" if game.get("game_error") else ""
    return f"[{i}/{n}] {game['game_pk']} {game['game_date']}{note}: " + " ".join(counts)


def _write(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), default=str)


def run(args: argparse.Namespace) -> int:
    if not os.environ.get(BettingProsOddsProvider.API_KEY_ENV):
        print(
            f"no vendor key: set {BettingProsOddsProvider.API_KEY_ENV} (the app container has it)"
        )
        return 2
    sample, starters = asyncio.run(
        load_sample(args.dsn, args.seasons, args.games, args.seed, args.game_pks)
    )
    prov = ProbeProvider(rate=args.rate)
    games: list[dict[str, Any]] = []
    started = time.monotonic()
    params: dict[str, Any] = {
        "seasons": list(args.seasons),
        "games_asked": args.games,
        "games_sampled": len(sample),
        "seed": args.seed,
        "rate": args.rate,
        "markets": [*GAME_MARKETS, *([] if args.no_prop else [PROP_MARKET])],
        "odds_row_version": ODDS_ROW_VERSION,
        **rule_params(),
    }
    try:
        for i, game in enumerate(sample, start=1):
            record = collect_game(
                prov,
                game,
                prop_player=None if args.no_prop else starters.get(game.game_pk),
            )
            games.append(record)
            print(_progress(i, len(sample), record), flush=True)
    except KeyboardInterrupt:
        print(f"interrupted after {len(games)} games; summarising what was read")
    params["games_read"] = len(games)
    params["vendor_reads"] = prov.vendor_reads
    params["elapsed_s"] = round(time.monotonic() - started, 1)
    summary = summarise(games)
    print(format_summary(summary))
    print(f"vendor reads {prov.vendor_reads} in {params['elapsed_s']} s")
    if args.out:
        _write(args.out, {"params": params, "summary": summary, "games": games})
        print(f"wrote {args.out}")
    return 0


def run_regate(args: argparse.Namespace) -> int:
    """Re-grade the saved census ``args.regate`` (no vendor read, no database read)."""
    with open(args.regate, encoding="utf-8") as fh:
        saved = json.load(fh)
    payload = regate(saved, source=args.regate)
    print(f"regrading {args.regate}: {len(payload['games'])} games")
    print(format_regate_changes(payload["summary"]["regate"]))
    print(format_summary(payload["summary"]))
    if args.out:
        _write(args.out, payload)
        print(f"wrote {args.out}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--games", type=int, default=DEFAULT_GAMES)
    ap.add_argument("--seasons", type=int, nargs="+", default=list(DEFAULT_SEASONS))
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE, help="vendor reads a second")
    ap.add_argument(
        "--game-pks",
        type=int,
        nargs="+",
        default=None,
        help="read these games only (Final games of --seasons with a stored closing moneyline)",
    )
    ap.add_argument("--no-prop", action="store_true", help="skip the strikeout prop")
    ap.add_argument("--out", default="", help="write every row, the summary and the gate as JSON")
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN", ""))
    ap.add_argument(
        "--regate",
        default="",
        metavar="FILE",
        help="re-grade a saved census (an earlier --out file) under the current guard and "
        "dated exclusion; no vendor read, no database read",
    )
    args = ap.parse_args(argv)
    if not args.regate and not args.dsn:
        ap.error("no DSN: pass --dsn or set BASEBALL_DB_DSN")
    if args.rate <= 0:
        ap.error("--rate must be above 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_regate(args) if args.regate else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
