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

Markets (discovered from /v3/markets?sport=MLB):
  game:  moneyline 122, total 175, run-line 176
  props: strikeouts 285, hits 287, home_runs 299, earned_runs 290,
         walks 408, total_bases 293, rbis 289

``line_type``: ``"opening"`` reads each selection's ``opening_line``; any other
value reads the current best/main book line. CLV is therefore available by
comparing opening vs current. ``book``/``is_sharp_book`` are echoed through; a
specific BettingPros ``book_id`` can be preferred via ``prefer_book_id``.

HTTP is stdlib ``urllib`` (sync — the protocol methods are sync; the module
stays importable without aiohttp). The two ``_bp_get`` / ``_mlb_get`` seams are
the only network surface and are stubbed in unit tests (fixtures captured under
``tests/fixtures/bettingpros/``); no live call is made in tests.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("pipeline.bettingpros_odds_provider")

_BP_BASE = "https://api.bettingpros.com/v3"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"

#: BettingPros game-odds market ids.
_GAME_MARKET_IDS: dict[str, int] = {
    "moneyline": 122,
    "total": 175,
    "runline": 176,
    "run_line": 176,
}

#: prop_stat (MockOddsAPI vocabulary) → BettingPros market id.
_PROP_MARKET_IDS: dict[str, int] = {
    "strikeouts": 285,
    "hits": 287,
    "home_runs": 299,
    "earned_runs": 290,
    "walks": 408,
    "total_bases": 293,
    "rbis": 289,
}


def _normalize_name(name: str) -> str:
    """Lower-case, strip accents + non-alphanumerics, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in ascii_only)
    return " ".join(kept.lower().split())


class BettingProsOddsProvider:
    """Real odds provider backed by BettingPros v3 (SIM-405)."""

    API_KEY_ENV = "ODDS_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        prefer_book_id: int | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.API_KEY_ENV)
        self._prefer_book_id = prefer_book_id
        self._timeout = timeout
        # Per-instance caches (cleared by constructing a new provider).
        self._event_cache: dict[int, dict[str, Any] | None] = {}
        self._game_meta_cache: dict[int, tuple[str, str, str] | None] = {}
        self._player_name_cache: dict[int, str | None] = {}

    # ----------------------------------------------------------------- HTTP
    def _http_get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed hosts
            return json.loads(resp.read().decode("utf-8"))

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
    def _resolve_game_meta(self, game_pk: int) -> tuple[str, str, str] | None:
        """``game_pk`` → (date 'YYYY-MM-DD', home_team_name, away_team_name)."""
        if game_pk in self._game_meta_cache:
            return self._game_meta_cache[game_pk]
        meta: tuple[str, str, str] | None = None
        try:
            data = self._mlb_get("schedule", {"sportId": 1, "gamePk": game_pk})
            game = data["dates"][0]["games"][0]
            date_str = str(game["gameDate"])[:10]
            home = str(game["teams"]["home"]["team"]["name"])
            away = str(game["teams"]["away"]["team"]["name"])
            meta = (date_str, home, away)
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve game_pk %s: %s", game_pk, exc)
        self._game_meta_cache[game_pk] = meta
        return meta

    def _resolve_event(self, game_pk: int) -> dict[str, Any] | None:
        """``game_pk`` → the matching BettingPros event dict (or None)."""
        if game_pk in self._event_cache:
            return self._event_cache[game_pk]
        event: dict[str, Any] | None = None
        meta = self._resolve_game_meta(game_pk)
        if meta is not None:
            date_str, home_name, away_name = meta
            home_n, away_n = _normalize_name(home_name), _normalize_name(away_name)
            try:
                data = self._bp_get("events", {"sport": "MLB", "date": date_str})
                candidates = []
                for e in data.get("events", []):
                    parts = {p["id"]: _normalize_name(p["name"]) for p in e.get("participants", [])}
                    home_nick = parts.get(e.get("home"), "")
                    away_nick = parts.get(e.get("visitor"), "")
                    # Nickname suffix-matches the MLB full name ("Tigers" ⊂ "Detroit Tigers").
                    if home_n.endswith(home_nick) and away_n.endswith(away_nick) and home_nick:
                        candidates.append(e)
                if len(candidates) == 1:
                    event = candidates[0]
                elif len(candidates) > 1:
                    # Double-header: pick the earliest scheduled (game 1) by default.
                    event = sorted(candidates, key=lambda e: str(e.get("scheduled", "")))[0]
                    log.info(
                        "BettingPros: %d events matched game_pk %s (double-header); "
                        "picked scheduled=%s",
                        len(candidates),
                        game_pk,
                        event.get("scheduled"),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("BettingPros: event lookup failed for game_pk %s: %s", game_pk, exc)
        self._event_cache[game_pk] = event
        return event

    def _resolve_player_name(self, player_id: int) -> str | None:
        """MLB ``player_id`` → normalized full name (cached)."""
        if player_id in self._player_name_cache:
            return self._player_name_cache[player_id]
        name: str | None = None
        try:
            data = self._mlb_get(f"people/{player_id}", {})
            full = data["people"][0]["fullName"]
            name = _normalize_name(str(full))
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: could not resolve player_id %s: %s", player_id, exc)
        self._player_name_cache[player_id] = name
        return name

    # ------------------------------------------------------------ line picking
    def _pick_line(
        self, selection: dict[str, Any], line_type: str
    ) -> tuple[float | None, float | None]:
        """Return (american_cost, line) for a selection at the requested line_type.

        ``opening`` reads ``selection.opening_line``; otherwise the preferred
        book's line (``prefer_book_id`` if set, else the ``best`` then ``main``
        line, else the first available).
        """
        if line_type == "opening":
            ol = selection.get("opening_line") or {}
            return _opt_float(ol.get("cost")), _opt_float(ol.get("line"))

        chosen: dict[str, Any] | None = None
        for book in selection.get("books", []):
            lines = book.get("lines") or []
            if self._prefer_book_id is not None and book.get("id") == self._prefer_book_id:
                chosen = lines[0] if lines else None
                break
            for ln in lines:
                if ln.get("best"):
                    chosen = ln
                    break
                if ln.get("main") and chosen is None:
                    chosen = ln
            if chosen is not None and chosen.get("best"):
                break
        if chosen is None:
            # Fall back to the very first line available.
            for book in selection.get("books", []):
                if book.get("lines"):
                    chosen = book["lines"][0]
                    break
        if chosen is None:
            return None, None
        return _opt_float(chosen.get("cost")), _opt_float(chosen.get("line"))

    # ----------------------------------------------------------------- get_odds
    def get_odds(
        self,
        game_pk: int,
        *,
        line_type: str = "current",
        market_type: str = "moneyline",
        book: str = "consensus",
        is_sharp_book: bool = False,
    ) -> dict[str, Any]:
        """Game-level lines for ``game_pk`` in the MockOddsAPI dict shape (SIM-405).

        Populates all moneyline / total / run-line fields it can resolve;
        unresolved fields are ``None``. ``source='bettingpros'``, ``is_mock=False``.
        """
        result: dict[str, Any] = {
            "game_pk": game_pk,
            "source": "bettingpros",
            "is_mock": False,
            "book": book,
            "line_type": line_type,
            "market_type": market_type,
            "is_sharp_book": is_sharp_book,
            "home_ml": None,
            "away_ml": None,
            "home_spread": None,
            "home_spread_ml": None,
            "away_spread": None,
            "away_spread_ml": None,
            "total_line": None,
            "over_ml": None,
            "under_ml": None,
        }
        event = self._resolve_event(game_pk)
        if event is None:
            log.warning("BettingPros: no event for game_pk %s — returning empty odds", game_pk)
            return result
        event_id = event["id"]
        home_abbrev, away_abbrev = event.get("home"), event.get("visitor")

        # Moneyline
        for sel in self._selections(event_id, _GAME_MARKET_IDS["moneyline"]):
            cost, _ = self._pick_line(sel, line_type)
            if sel.get("participant") == home_abbrev:
                result["home_ml"] = cost
            elif sel.get("participant") == away_abbrev:
                result["away_ml"] = cost

        # Run line (spread)
        for sel in self._selections(event_id, _GAME_MARKET_IDS["runline"]):
            cost, line = self._pick_line(sel, line_type)
            if sel.get("participant") == home_abbrev:
                result["home_spread"], result["home_spread_ml"] = line, cost
            elif sel.get("participant") == away_abbrev:
                result["away_spread"], result["away_spread_ml"] = line, cost

        # Total (over/under)
        for sel in self._selections(event_id, _GAME_MARKET_IDS["total"]):
            cost, line = self._pick_line(sel, line_type)
            label = (sel.get("label") or sel.get("selection") or "").lower()
            if line is not None:
                result["total_line"] = line
            if "over" in label:
                result["over_ml"] = cost
            elif "under" in label:
                result["under_ml"] = cost

        return result

    # ------------------------------------------------------------ get_prop_odds
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
        """Single player-prop quote for ``player_id`` in the MockOddsAPI shape.

        Raises ``ValueError`` for an unknown ``prop_stat`` (mirrors the mock).
        ``line``/``over_ml``/``under_ml`` are ``None`` when the player/market has
        no offer. ``source='bettingpros'``, ``is_mock=False``.
        """
        if prop_stat not in _PROP_MARKET_IDS:
            known = ", ".join(sorted(_PROP_MARKET_IDS))
            raise ValueError(f"Unknown prop_stat '{prop_stat}'. Known values: {known}")

        result: dict[str, Any] = {
            "game_pk": game_pk,
            "player_id": player_id,
            "prop_stat": prop_stat,
            "line": None,
            "over_ml": None,
            "under_ml": None,
            "book": book,
            "line_type": line_type,
            "is_sharp_book": is_sharp_book,
            "source": "bettingpros",
            "is_mock": False,
        }
        event = self._resolve_event(game_pk)
        player_name = self._resolve_player_name(player_id)
        if event is None or player_name is None:
            return result

        offer = self._find_player_offer(event["id"], _PROP_MARKET_IDS[prop_stat], player_name)
        if offer is None:
            return result

        for sel in offer.get("selections", []):
            cost, line = self._pick_line(sel, line_type)
            if line is not None:
                result["line"] = line
            label = (sel.get("label") or sel.get("selection") or "").lower()
            if "over" in label:
                result["over_ml"] = cost
            elif "under" in label:
                result["under_ml"] = cost
        return result

    # --------------------------------------------------------------- internals
    def _selections(self, event_id: int, market_id: int) -> list[dict[str, Any]]:
        """All selections of the (single) offer for a game-level market."""
        try:
            data = self._bp_get(
                "offers", {"sport": "MLB", "market_id": market_id, "event_id": event_id}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: offers fetch failed (market %s): %s", market_id, exc)
            return []
        offers = data.get("offers", [])
        return offers[0].get("selections", []) if offers else []

    def _find_player_offer(
        self, event_id: int, market_id: int, player_name: str
    ) -> dict[str, Any] | None:
        """The prop offer whose participant matches ``player_name`` (normalized)."""
        try:
            data = self._bp_get(
                "offers", {"sport": "MLB", "market_id": market_id, "event_id": event_id}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("BettingPros: prop offers fetch failed (market %s): %s", market_id, exc)
            return None
        for offer in data.get("offers", []):
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


__all__ = ["BettingProsOddsProvider"]
