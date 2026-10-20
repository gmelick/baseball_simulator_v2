"""The odds rows built from an event's offers: one row per book and market, the
first-inning twin rule and the first-five exclusions.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

from pipeline.bettingpros.events import EventMatchMixin
from pipeline.bettingpros.markets import (
    _GAME_MARKET_IDS,
    _PROP_MARKET_IDS,
)
from pipeline.bettingpros.parsing import (
    _OPTIONAL_SIDES,
    _REQUIRED_SIDES,
    _book_line,
    _books_on,
    _FirstInningReadError,
    _normalize_name,
    _opt_float,
    _opt_int,
    _Quote,
    _same_line,
    _shared_opener,
    _stamp,
    _tie_rules_out_twin,
)
from pipeline.bettingpros.rules import (
    F5_EXCLUDED_BOOKS,
    F5_TWIN_MARKETS,
    is_f5_dated_excluded,
    twin_price_tolerance,
)
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_SIDE,
    GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    book_label,
)

log = logging.getLogger("pipeline.bettingpros_odds_provider")


class OfferRowsMixin(EventMatchMixin):
    """Turns an event's offers into odds rows, one per book."""

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
            base.update({"scheduled_start": None, "game_date": None, "postponed_start": None})
            log.warning("BettingPros: no event for game_pk %s — returning empty odds", game_pk)
            return base, []
        start, official, date_str = self._game_meta_fields(game_pk, event)
        base.update(
            {
                "scheduled_start": start,
                "game_date": date_str,
                "postponed_start": self._postponed_start(game_pk),  # SIM-555: for the guard
            }
        )
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
            "postponed_start": None,  # SIM-555: a made-up game's original start, for the guard
        }
        event = self._resolve_event(game_pk)
        player_name = self._resolve_player_name(player_id)
        if event is None or player_name is None:
            return base, []
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
            return base, []
        return base, self._rows(base, "prop", self._prop_sides(offer), line_type)

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
