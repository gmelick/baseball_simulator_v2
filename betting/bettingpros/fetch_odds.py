"""Get BettingPros' odds for an MLB game and build one row per book.

The module holds BettingPros' market ids, the first-five rules, the offer reads, the
row building and the provider class.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from betting.bettingpros.match_games import (
    EventMatchMixin,
    ProviderBase,
    _normalize_name,
    _parse_utc,
    _ScheduleInfo,
)
from betting.markets import (
    GAME_MARKET_KIND,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    GRADED_BOOK_PREFERENCE,
    PROP_STATS,
    book_kind,
    book_label,
)
from betting.validate import implied_probability

log = logging.getLogger("betting.bettingpros")


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


#: The sides a row of each market kind must price.
#: A market that lacks a required side gets no row for any book.
_REQUIRED_SIDES: dict[str, tuple[str, ...]] = {
    "moneyline": ("home", "away"),
    "three_way": ("home", "away"),
    "runline": ("home", "away"),
    "total": ("over", "under"),
    "yes_no": ("yes", "no"),
    "prop": ("over", "under"),
}


#: The sides a row prices only when the book quotes them: a three-way market's tie.
_OPTIONAL_SIDES: dict[str, tuple[str, ...]] = {"three_way": ("draw",)}


#: A side's price, line and vendor stamp, read from one book's line.
_Quote = tuple[float | None, float | None, Any]


class _FirstInningReadError(RuntimeError):
    """Signal that the first-inning read failed, so the twin check cannot run.

    The first-five market then gets no rows.
    Without the check, a first-five row may be a first-inning bet under the wrong label.
    """


def _usable(ln: Mapping[str, Any]) -> bool:
    """Return True when a line is a price the book takes.

    A line flagged ``is_off`` or inactive, or a line without a price, is not a quote.
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
    """Return the book's usable line on one selection, or ``None``.

    The feed normally lists one line per book per selection.
    When a book lists more than one usable line, the ``main`` line wins, else the first.
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
    """Return the one book that opened every side, or ``None``.

    The rule matches the opening row's rule.
    Every side's ``opening_line`` must name a ``book_id`` and a ``cost``.
    All sides must name the same book.
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
    """Return a vendor stamp as a timezone-aware UTC datetime, or ``None``.

    The vendor writes UTC text with no zone, e.g. ``"2026-09-10 16:15:50"``.
    """
    if not value:
        return None
    parsed = _parse_utc(str(value))
    return None if parsed is None else parsed.replace(tzinfo=UTC)


def _books_on(sides: Mapping[str, Mapping[str, Any]]) -> list[int]:
    """Return every book id on the given sides, in the order the books first appear."""
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
    """Return True when two lines have the same line and prices within ``tolerance``.

    ``tolerance`` is a gap in implied probability; ``0.0`` asks for identical prices.
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
    """Return True when the ties show that a first-five entry is not a first-inning copy.

    A first-five tie that the first-inning entry lacks rules out a copy.
    A first-five tie beyond ``tolerance`` of the first-inning tie also rules out a copy.
    A first-five entry with no tie decides nothing; the team sides decide.
    On a near-even game the team sides match within the tolerance, so the tie must decide.
    """
    if f5_tie is None:
        return False
    if f1_tie is None:
        return True
    return not _same_line(f5_tie, f1_tie, tolerance)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: The most ``/offers`` pages read for one (event, market). The API pages
#: at 10 offers, and a batter market fills two to three pages. The cap only
#: stops a pager that never ends.
_MAX_OFFER_PAGES = 20


class OfferRowsMixin(EventMatchMixin):
    """Turn an event's offers into odds rows, one row per book."""

    def _offers(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """Every offer of one (event, market), from the offers cache while fresh.

        Only a complete market is cached.
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
        """GET every ``/offers`` page of one (event, market).

        The vendor pages at 10 offers, so a batter market needs every page
        to list every hitter. Returns ``(offers, complete)``. A failure on
        page 1 raises. A failure on a later page logs a warning and returns
        the offers in hand with ``complete=False``. A ``_pagination`` block
        that is not a dict counts as one page.
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
                self.read_failures += 1  # the market comes back partial
                return offers, False
            offers.extend(more.get("offers", []))
        return offers, True

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name.

        A found name stays cached for the process lifetime. A failure stays
        for one time-to-live. A read that raised counts in ``read_failures``.
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
                self.read_failures += 1  # the player read failed
        if name is None:
            self._note_failed_lookup("player", player_id)
        else:
            self._player_name_cache[player_id] = name
        return name

    @staticmethod
    def _game_sides(
        kind: str,
        selections: list[dict[str, Any]],
        home_abbrev: Any,
        away_abbrev: Any,
    ) -> dict[str, dict[str, Any]]:
        """Return the market's selections keyed by side; the first selection of a side wins.

        A moneyline or run line names the team in ``participant``.
        The tie of a three-way moneyline carries ``selection == 'draw'``.
        The vendor sometimes adds an unrelated yes / no pair to the first-five moneyline.
        Those selections have no participant and no ``draw``, so the method skips them.
        A total has an over and an under side; a yes / no market has a yes and a no side.
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
        """Return a prop offer's over and under selections, keyed ``over`` / ``under``."""
        sides: dict[str, dict[str, Any]] = {}
        for sel in offer.get("selections", []):
            label = str(sel.get("label") or sel.get("selection") or "").lower()
            key = "over" if "over" in label else "under" if "under" in label else None
            if key is not None and key not in sides:
                sides[key] = sel
        return sides

    @staticmethod
    def _fill(kind: str, row: dict[str, Any], quote: Mapping[str, _Quote]) -> None:
        """Write one book's quote into the columns of the market kind.

        Each market fills only its own columns; two markets never share one row.
        A total, yes / no or prop row also gets ``over_line`` / ``under_line``.
        The load guard (``betting/validate.py``) reads those two lines.
        """

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
            # "A run in the first inning: yes" is "over 0.5 first-inning runs".
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
        """Return one row per book for a market's sides.

        Each row is a copy of ``base`` with one book's prices and ``book = 'bp:<id>'``.
        A market that lacks a required side gets no rows.

        On ``'opening'`` the method returns at most one row, the opener's.
        Every required side must name the same opening book, or the market gets no row.
        A three-way tie joins the row only when the same book opened the tie.
        An excluded opener, or an opener that copies its first-inning opener, gets no row.
        The stamp ``book_line_at`` is the newest ``created`` of the row's sides.

        On another line type, each book with a usable line on every required side gets a row.
        The rows keep the payload's book order; the vendor's blend appears as ``bp:0``.
        The stamp is the newest ``updated`` of the row's sides.
        """
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
                return []  # two openers: no one book opened the market
            opener = openers.pop()
            if opener in excluded or opener_is_twin:
                return []
            for key in optional:
                ol = sides[key].get("opening_line") or {}
                cost = _opt_float(ol.get("cost"))
                if _opt_int(ol.get("book_id")) == opener and cost is not None:
                    quote[key] = (cost, _opt_float(ol.get("line")), ol.get("created"))
            return [make_row(opener, quote)]

        book_ids = _books_on({k: sides[k] for k in required})
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
        """Return the selections of the first-inning market that matches a first-five market.

        An empty market returns ``[]``: no twin is possible, so the first-five rows stand.
        A failed read raises :class:`_FirstInningReadError`, and the caller drops the rows.
        ``_offers`` does not cache a failure, so the next call reads again.
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
        """Return the first-inning selections that match a first-five market's sides.

        A team side matches by ``participant``; an over or under side matches by label.
        The first-inning tie, when the market lists one, comes back under ``'draw'``.
        The method returns ``None`` when the first-inning market lacks a required side.
        A failed first-inning read raises :class:`_FirstInningReadError`.
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
        """Log a first-five exclusion at INFO, once per event, market and book."""
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
        """Return the books that the dated list excludes from this first-five market.

        A book on :data:`F5_EXCLUDED_BOOKS` drops from its start date on.
        The exclusion covers only the markets :data:`F5_EXCLUDED_MARKETS` lists for the book.
        Example: DraftKings drops from 2025-03-01 on the run line and the total.
        An unknown game date excludes nothing.
        The method logs an exclusion only when the book quoted the market or opened it.
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
        """Return the books whose first-five entry is really a first-inning bet.

        The method applies two rules, to the three first-five markets only.
        Rule 1 is the dated list (:meth:`_f5_dated_exclusions`).
        Rule 2 is the twin: a first-five line that copies the same book's first-inning line.
        A twin has the same line on every required side, and prices within the tolerance.
        The tolerance is :func:`twin_price_tolerance`, a gap in implied probability.
        The moneyline asks for identical prices, so a near-even game is not taken for a copy.
        ``tie`` is a three-way market's first-five tie; :func:`_tie_rules_out_twin` checks it.
        A failed first-inning read raises :class:`_FirstInningReadError`.
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
        """Return True when the first-five opener copies the first-inning opener.

        A copy has the same opening book on every required side.
        Each side also has the same line, and prices within :func:`twin_price_tolerance`.
        On a three-way market, only a tie that the same book opened counts.
        A first-five opening tie that rules out a copy clears the opener.
        A market with a copied opener gets no opening row.
        A failed first-inning read raises :class:`_FirstInningReadError`.
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

    def _game_market_rows(
        self, game_pk: int, line_type: str, market_type: str
    ) -> list[dict[str, Any]]:
        """Return one row per book for one game market and line type.

        Each row carries ``scheduled_start``, ``game_date`` and ``postponed_start``.
        The load guard and the logs read those fields; the database never stores them.
        When the first-inning read fails, a first-five market gets no rows at all.
        An unchecked row may be a first-inning bet; a WARNING names the game.
        """
        if market_type not in GAME_MARKET_KIND:
            known = ", ".join(GAME_MARKET_TYPES)
            raise ValueError(f"Unknown market_type '{market_type}'. Known values: {known}")
        kind = GAME_MARKET_KIND[market_type]
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
            log.warning("BettingPros: no event for game_pk %s — returning empty odds", game_pk)
            return []
        start, official, date_str = self._game_meta_fields(game_pk, event)
        base.update(
            {
                "scheduled_start": start,
                "game_date": date_str,
                "postponed_start": self._postponed_start(game_pk),  # For the guard
            }
        )
        event_id = event["id"]
        home_abbrev, away_abbrev = event.get("home"), event.get("visitor")
        market_id = _GAME_MARKET_IDS[market_type]
        side = GAME_MARKET_SIDE[market_type]
        if kind == "total" and side is not None:
            team_abbrev = home_abbrev if side == "home" else away_abbrev
            selections = self._team_offer_selections(event_id, market_id, team_abbrev)
        else:
            selections = self._selections(event_id, market_id)
        sides = self._game_sides(kind, selections, home_abbrev, away_abbrev)
        required = _REQUIRED_SIDES[kind]
        if any(k not in sides for k in required):
            return []
        required_sides = {k: sides[k] for k in required}
        tie = sides.get("draw")
        try:
            # The opening row drops when either rule excludes its opener.
            # It also drops when the opener copies its own first-inning opener.
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
            self.read_failures += 1  # The first-inning read failed
            return []
        rows = self._rows(
            base, kind, sides, line_type, excluded=excluded, opener_is_twin=opener_is_twin
        )
        return rows

    # -------------------------------------------------------------- prop rows
    def _prop_market_rows(
        self, game_pk: int, player_id: int, prop_stat: str, line_type: str
    ) -> list[dict[str, Any]]:
        """Return one row per book for one player's prop market."""
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
            "postponed_start": None,  # A made-up game's original start; the guard reads it
        }
        event = self._resolve_event(game_pk)
        player_name = self._resolve_player_name(player_id)
        if event is None or player_name is None:
            return []
        start, _official, date_str = self._game_meta_fields(game_pk, event)
        base.update(
            {
                "scheduled_start": start,
                "game_date": date_str,
                "postponed_start": self._postponed_start(game_pk),
            }
        )
        offer = self._find_player_offer(event["id"], _PROP_MARKET_IDS[prop_stat], player_name)
        if offer is None:
            return []
        return self._rows(base, "prop", self._prop_sides(offer), line_type)

    # --------------------------------------------------------------- internals
    def _selections(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """Return the selections of the one offer of a game market."""
        try:
            offers = self._offers(event_id, market_id)  # Cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1
            return []
        return offers[0].get("selections", []) if offers else []

    def _team_offer_selections(
        self, event_id: int, market_id: int, team_abbrev: Any
    ) -> list[dict[str, Any]]:
        """Return the selections of one team's offer in a team-total market.

        A team-total market holds one offer per team.
        Each offer names its team in ``team_id`` and again as its only participant.
        The name is the abbreviation the event uses for ``home`` or ``visitor``.
        The method returns ``[]`` when the team has no offer.
        """
        try:
            offers = self._offers(event_id, market_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: team offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1
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
        """Return the prop offer whose participant matches ``player_name``, or ``None``."""
        try:
            offers = self._offers(event_id, market_id)  # Cached per (event, market)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: prop offers fetch failed (market %s): %s", market_id, exc)
            self.read_failures += 1
            return None
        for offer in offers:
            for part in offer.get("participants", []):
                player = part.get("player") or {}
                first = player.get("first_name", "")
                last = player.get("last_name", "")
                if _normalize_name(f"{first} {last}") == player_name:
                    return offer
                # Without a first and last name, use the participant's display name.
                if part.get("name") and _normalize_name(part["name"]) == player_name:
                    return offer
        return None


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
