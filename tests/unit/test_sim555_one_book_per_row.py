"""SIM-555 (2026-09-28) — every book's prices stored, one book per row, and a load guard.

These tests drive the build plan's tests 1-13
(``docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`` §7) plus the
pieces they rest on:

  * the book vocabulary in ``pipeline/odds_provider.py`` (labels, kinds, names,
    the graded-book preference, the SQL helpers) and the by-book protocol seam;
  * the BettingPros provider's one-row-per-book reads, the opener's row, the
    stamps, the first-five exclusions and the one-row methods on top;
  * the load guard (``pipeline/odds_row_guard.py``) and its tally;
  * migration 0028.

No network and no database. The provider's ``_bp_get`` / ``_mlb_get`` seams are
served from the captured payloads under ``tests/fixtures/bettingpros/`` (event
92857: SEA at DET, 2024-08-15; event 99026: TB at ATL, 2026-09-10), and from
small payloads built here for the shapes the fixtures lack (the appendix-B Hard
Rock "1 / 1" entry, a first-five twin, a late stamp).
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pipeline.bettingpros_odds_provider import (
    F5_EXCLUDED_BOOKS,
    F5_EXCLUDED_MARKETS,
    F5_TWIN_MARKETS,
    TWIN_MONEYLINE_PRICE_TOLERANCE,
    TWIN_PRICE_TOLERANCE,
    BettingProsOddsProvider,
    _book_line,
    _usable,
    f5_excluded_markets,
    is_f5_dated_excluded,
    twin_price_tolerance,
)
from pipeline.live.live_ingestion_pipeline import MockOddsAPI
from pipeline.odds_provider import (
    BETTABLE_KINDS,
    BOOK_IDS_BY_NAME,
    BOOK_KIND,
    BOOK_LABEL_PREFIX,
    BOOK_NAMES,
    FULL_GAME_MARKET_TYPES,
    GAME_ODDS_FIELDS,
    GRADED_BOOK_PREFERENCE,
    LEGACY_GAME_MARKET_TYPES,
    ODDS_ROW_VERSION,
    STORED_BOOK_FILTER_SQL,
    OddsProvider,
    RealOddsAPIProvider,
    bettable_labels,
    book_display_name,
    book_id_from_label,
    book_kind,
    book_label,
    graded_book_labels,
    graded_row_order_sql,
    is_bettable,
    non_bettable_labels,
    odds_rows_by_book,
    prop_rows_by_book,
    resolve_book,
)
from pipeline.odds_row_guard import (
    CLOSING_STAMP_GRACE,
    F5_TIE_IMPLIED_MAX,
    F5_TOTAL_LINE_MAX,
    F5_WIN_OR_TIE_SUM_MAX,
    PAIR_PRICE_BAND,
    RULES,
    TEAM_TOTAL_LINE_MAX,
    THREE_WAY_SUM_MAX,
    THREE_WAY_SUM_MIN,
    THREE_WAY_TEAM_SUM_MAX,
    Refusal,
    RefusalTally,
    check_row,
    implied_probability,
    refuse_reason,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIX = _ROOT / "tests" / "fixtures" / "bettingpros"
_MIG = _ROOT / "db" / "migrations" / "versions" / "0028_sim555_odds_book_line_stamp.py"

_OFFERS_92857 = {
    122: "offers_ml_92857.json",
    175: "offers_total_92857.json",
    176: "offers_runline_92857.json",
    285: "offers_k_92857.json",
}
_OFFERS_99026 = {
    278: "offers_f1_moneyline_99026.json",
    279: "offers_f5_moneyline_99026.json",
    280: "offers_f1_total_99026.json",
    281: "offers_f5_total_99026.json",
    282: "offers_f1_runline_99026.json",
    283: "offers_f5_runline_99026.json",
    277: "offers_team_total_99026.json",
    407: "offers_f5_team_total_99026.json",
    286: "offers_first_to_score_99026.json",
    369: "offers_first_inning_run_99026.json",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


def _utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class _Fixture92857(BettingProsOddsProvider):
    """SEA at DET, 2024-08-15 (game_pk 746437): the full-game and strikeout payloads."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(api_key="test-key", **kw)

    def _mlb_get(self, path, params):  # type: ignore[override]
        if path == "schedule":
            return _load("mlb_schedule_746437.json")
        if path.startswith("people/"):
            return {"people": [{"fullName": "Bryce Miller"}]}
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        if path == "events":
            return _load("events_2024-08-15.json")
        if path == "offers":
            return _load(_OFFERS_92857[params["market_id"]])
        raise AssertionError(f"unexpected BP path {path}")


class _Fixture99026(BettingProsOddsProvider):
    """TB at ATL (BettingPros event 99026): the twelve segment payloads.

    ``game_date`` sets the official date the dated first-five exclusion reads;
    ``None`` leaves the game's meta unresolved.
    """

    def __init__(self, *, game_date: str | None = "2024-08-14", **kw: Any) -> None:
        super().__init__(api_key="test-key", **kw)
        self._game_date = game_date
        self.offers_calls: list[int] = []

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 99026, "home": "ATL", "visitor": "TB", "scheduled": "2026-09-10 16:15:00"}

    def _resolve_game_meta(self, game_pk):  # type: ignore[override]
        if self._game_date is None:
            return None
        return (self._game_date, "Atlanta Braves", "Tampa Bay Rays", datetime(2026, 9, 10, 16, 15))

    def _mlb_get(self, path, params):  # type: ignore[override]
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        assert path == "offers", path
        self.offers_calls.append(int(params["market_id"]))
        return _load(_OFFERS_99026[params["market_id"]])


# --------------------------------------------------------------------------- synthetic payloads


def _line(line: float | None, cost: float | None, updated: str, **extra: Any) -> dict[str, Any]:
    ln: dict[str, Any] = {
        "line": line,
        "cost": cost,
        "updated": updated,
        "main": True,
        "best": False,
        "active": True,
        "is_off": False,
    }
    ln.update(extra)
    return ln


def _sel(
    *,
    participant: str | None = None,
    selection: str = "",
    opener: tuple[int, float, float, str] | None = None,
    books: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One selection. ``opener`` is (book_id, line, cost, created); ``books`` maps id → line."""
    sel: dict[str, Any] = {
        "participant": participant,
        "selection": selection,
        "label": selection.title(),
        "books": [{"id": book_id, "lines": [ln]} for book_id, ln in (books or {}).items()],
    }
    if opener is not None:
        book_id, line, cost, created = opener
        sel["opening_line"] = {"line": line, "cost": cost, "book_id": book_id, "created": created}
    return sel


def _offers(*selections: dict[str, Any]) -> dict[str, Any]:
    return {"offers": [{"selections": list(selections), "participants": []}]}


class _Synthetic(BettingProsOddsProvider):
    """SD (home) against BAL (away), with hand-built ``/offers`` payloads per market id."""

    def __init__(
        self,
        offers_by_market: dict[int, dict[str, Any]],
        *,
        game_date: str = "2025-09-03",
        start: datetime = datetime(2025, 9, 3, 20, 10),
        **kw: Any,
    ) -> None:
        super().__init__(api_key="test-key", **kw)
        self._offers_by_market = offers_by_market
        self._meta = (game_date, "San Diego Padres", "Baltimore Orioles", start)

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 97809, "home": "SD", "visitor": "BAL"}

    def _resolve_game_meta(self, game_pk):  # type: ignore[override]
        return self._meta

    def _mlb_get(self, path, params):  # type: ignore[override]
        raise AssertionError(f"unexpected MLB path {path}")

    def _bp_get(self, path, params):  # type: ignore[override]
        assert path == "offers", path
        return self._offers_by_market.get(int(params["market_id"]), {"offers": []})


#: Appendix B of the scope note: game 776465's first-inning run line (market 282),
#: every line stamped 2025-09-03 20:11:43.
_T = "2025-09-03 20:11:43"
_APPENDIX_B = _offers(
    _sel(
        participant="BAL",
        opener=(10, 1.5, -1250, "2025-09-02 18:00:00"),
        books={
            0: _line(1.5, -1250, _T),
            10: _line(1.5, -1250, _T),
            33: _line(0.5, -400, _T),
            49: _line(1, -400, _T),
        },
    ),
    _sel(
        participant="SD",
        opener=(10, -1.5, 710, "2025-09-02 18:00:00"),
        books={
            0: _line(-1.5, 230, _T),
            10: _line(-1.5, 710, _T),
            33: _line(-0.5, 230, _T),
            49: _line(1, 260, _T),
        },
    ),
)


def _by_book(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["book"]: r for r in rows}


# ===========================================================================
# The book vocabulary
# ===========================================================================


class TestBookVocabulary:
    def test_labels_round_trip(self) -> None:
        assert BOOK_LABEL_PREFIX == "bp:"
        assert book_label(12) == "bp:12"
        assert book_id_from_label("bp:12") == 12
        assert book_id_from_label("bp:0") == 0
        for label in ("consensus", None, "bp:", "bp:x1", "pinnacle", "BP:12", "bp:-1", "bp: 12"):
            assert book_id_from_label(label) is None, label
        # Review fix: str.isdigit accepts a superscript (int refuses it) and other
        # scripts' digits (int reads them); a label takes ASCII digits only.
        for label in ("bp:²", "bp:١٢", "bp:１２"):
            assert book_id_from_label(label) is None, label
            assert resolve_book(label) is None, label
            assert book_kind(label) == "unknown", label

    def test_kinds(self) -> None:
        assert book_kind("bp:0") == "blend"
        assert book_kind("bp:12") == "sportsbook"
        assert book_kind("bp:37") == "dfs"
        assert book_kind("bp:60") == "exchange"
        assert book_kind("bp:68") == "prediction"
        assert book_kind("consensus") == "unknown"
        assert book_kind(None) == "unknown"
        assert frozenset({"sportsbook"}) == BETTABLE_KINDS
        assert is_bettable("bp:10") and not is_bettable("bp:0") and not is_bettable("bp:73")
        assert not is_bettable("consensus")

    def test_an_unlisted_book_is_unknown_and_never_bettable(self) -> None:
        """Review fix (2026-09-28, finding VOCAB-1): an id the vocabulary does not
        list used to default to "sportsbook", so a prediction market or a pick'em app
        the vendor adds could become a best price or a graded row."""
        assert book_kind("bp:999") == "unknown"
        assert not is_bettable("bp:999")
        assert "bp:999" not in bettable_labels() and "bp:999" not in non_bettable_labels()

    def test_the_catalogue_s_pickem_apps_and_prediction_markets_are_not_bettable(self) -> None:
        """The vendor's /v3/books catalogue (read 2026-09-28): 74 DraftKings
        Predictions and 78 Plus500 are flagged prediction markets, 76 Underdog
        Predict is one by name; 44 ThriveFantasy, 45 Betr (seen in real MLB
        payloads), 53 Dabble, 69 FanDuel Picks and 70 DraftKings Pick6 are pick'em
        apps."""
        for book_id in (74, 76, 78):
            assert BOOK_KIND[book_id] == "prediction", book_id
        for book_id in (44, 45, 53, 69, 70):
            assert BOOK_KIND[book_id] == "dfs", book_id
        for book_id in (44, 45, 53, 69, 70, 74, 76, 78):
            label = book_label(book_id)
            assert not is_bettable(label), label
            assert label in non_bettable_labels() and label not in bettable_labels()
            assert book_display_name(label) == BOOK_NAMES[book_id]

    def test_the_bettable_labels_are_the_listed_sportsbooks(self) -> None:
        assert bettable_labels() == [
            "bp:10",
            "bp:12",
            "bp:13",
            "bp:14",
            "bp:15",
            "bp:18",
            "bp:19",
            "bp:24",
            "bp:27",
            "bp:33",
            "bp:49",
        ]
        assert set(graded_book_labels()) <= set(bettable_labels())
        assert not set(bettable_labels()) & set(non_bettable_labels())
        labels = {book_label(b) for b in BOOK_KIND}
        assert set(bettable_labels()) | set(non_bettable_labels()) == labels

    def test_display_names(self) -> None:
        assert book_display_name("bp:10") == "FanDuel"
        assert book_display_name("bp:0") == "BettingPros Consensus"
        assert book_display_name("bp:999") == "bp:999"
        assert book_display_name("consensus") == "consensus"
        assert book_display_name(None) == ""

    def test_every_named_short_name_is_a_listed_sportsbook(self) -> None:
        for name, book_id in BOOK_IDS_BY_NAME.items():
            assert book_id in BOOK_NAMES, name
            assert BOOK_KIND[book_id] == "sportsbook", name
        assert set(BOOK_KIND) == set(BOOK_NAMES)

    def test_resolve_book(self) -> None:
        for value in ("draftkings", "DraftKings", "Draft Kings", "bp:12", 12):
            assert resolve_book(value) == 12, value
        assert resolve_book("Hard Rock") == 49
        assert resolve_book("theScore") == 33
        assert resolve_book("theScore Bet") == 33
        for value in ("consensus", None, "pinnacle", "circa", ""):
            assert resolve_book(value) is None, value

    def test_every_sportsbook_display_name_resolves_back_to_its_id(self) -> None:
        """Review fix: ``resolve_book(book_display_name(label))`` round-trips for every
        sportsbook, so ``--book "theScore Bet"`` and ``get_odds(book=...)`` accept the
        name the pages show."""
        sportsbooks = [b for b in BOOK_NAMES if BOOK_KIND[b] == "sportsbook"]
        assert 33 in sportsbooks and len(sportsbooks) == 11
        for book_id in sportsbooks:
            label = book_label(book_id)
            assert resolve_book(book_display_name(label)) == book_id, label
            assert resolve_book(book_display_name(label).upper()) == book_id, label
            assert resolve_book(label) == book_id, label

    def test_the_graded_preference(self) -> None:
        # Set 2026-09-29 from the sharpness read (the plan's §12).
        assert GRADED_BOOK_PREFERENCE == (12, 19, 10, 33, 18, 24, 13, 49, 14, 15, 27)
        assert GRADED_BOOK_PREFERENCE[0] == 12  # DraftKings leads until the sharpness read
        assert all(book_kind(book_label(b)) == "sportsbook" for b in GRADED_BOOK_PREFERENCE)
        assert graded_book_labels() == [f"bp:{b}" for b in GRADED_BOOK_PREFERENCE]
        assert graded_book_labels((10, 12)) == ["bp:10", "bp:12"]

    def test_the_non_bettable_labels(self) -> None:
        assert non_bettable_labels() == [
            "bp:0",
            "bp:36",
            "bp:37",
            "bp:38",
            "bp:39",
            "bp:44",
            "bp:45",
            "bp:53",
            "bp:60",
            "bp:63",
            "bp:68",
            "bp:69",
            "bp:70",
            "bp:73",
            "bp:74",
            "bp:75",
            "bp:76",
            "bp:78",
        ]
        assert not set(non_bettable_labels()) & set(graded_book_labels())

    def test_the_sql_helpers(self) -> None:
        assert STORED_BOOK_FILTER_SQL == "book LIKE 'bp:%'"
        assert graded_row_order_sql("$3") == (
            "array_position($3::varchar[], book) NULLS LAST, fetched_at DESC"
        )
        assert ODDS_ROW_VERSION == "sim555.2"

    def test_the_full_game_alias(self) -> None:
        assert FULL_GAME_MARKET_TYPES == ("moneyline", "runline", "total")
        assert LEGACY_GAME_MARKET_TYPES is FULL_GAME_MARKET_TYPES

    def test_the_exports(self) -> None:
        from pipeline import odds_provider

        for name in (
            "BOOK_NAMES",
            "BOOK_KIND",
            "BOOK_IDS_BY_NAME",
            "BOOK_LABEL_PREFIX",
            "STORED_BOOK_FILTER_SQL",
            "GRADED_BOOK_PREFERENCE",
            "BETTABLE_KINDS",
            "ODDS_ROW_VERSION",
            "FULL_GAME_MARKET_TYPES",
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
        ):
            assert name in odds_provider.__all__, name


# ===========================================================================
# The by-book protocol seam
# ===========================================================================


class _OneRowFake:
    """A test fake with only the one-row methods (no by-book methods)."""

    def get_odds(self, game_pk, *, line_type="current", market_type="moneyline"):
        return {"game_pk": game_pk, "line_type": line_type, "market_type": market_type}

    def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current"):
        return {"game_pk": game_pk, "player_id": player_id, "prop_stat": prop_stat}


class _BrokenByBook(_OneRowFake):
    """An unconfigured mock-like object: its by-book method returns a non-list."""

    def get_odds_by_book(self, *args, **kwargs):
        return object()

    def get_prop_odds_by_book(self, *args, **kwargs):
        return object()


class TestByBookSeam:
    def test_every_provider_conforms(self) -> None:
        for provider in (
            MockOddsAPI(),
            RealOddsAPIProvider(),
            BettingProsOddsProvider(api_key="k"),
        ):
            assert isinstance(provider, OddsProvider)
            assert callable(provider.get_odds_by_book)
            assert callable(provider.get_prop_odds_by_book)

    def test_the_mock_gives_its_one_row(self) -> None:
        mock = MockOddsAPI()
        rows = mock.get_odds_by_book(745000, line_type="closing", market_type="total")
        assert rows == [MockOddsAPI.get_odds(745000, line_type="closing", market_type="total")]
        assert rows[0]["book"] == "consensus"
        # The mock keeps its all-three-markets full-game row.
        assert rows[0]["home_ml"] is not None and rows[0]["home_spread"] is not None
        props = mock.get_prop_odds_by_book(745000, 101, "strikeouts", line_type="opening")
        assert props == [MockOddsAPI.get_prop_odds(745000, 101, "strikeouts", line_type="opening")]

    def test_the_mock_by_book_methods_read_a_subclass_override(self) -> None:
        """Review fix: the by-book methods call ``self.get_odds``, so a subclass that
        overrides the one-row method (as a static or an instance method) answers."""

        class _StaticFlaky(MockOddsAPI):
            @staticmethod
            def get_odds(game_pk, *, line_type="current", market_type="moneyline", **kw):
                if market_type == "f5_total":
                    raise RuntimeError("boom")
                return MockOddsAPI.get_odds(game_pk, line_type=line_type, market_type=market_type)

        class _InstanceFlaky(MockOddsAPI):
            def get_odds(self, game_pk, *, line_type="current", market_type="moneyline", **kw):
                return {"game_pk": game_pk, "market_type": market_type, "fake": True}

            def get_prop_odds(self, game_pk, player_id, prop_stat, *, line_type="current", **kw):
                return {"player_id": player_id, "fake": True}

        with pytest.raises(RuntimeError, match="boom"):
            odds_rows_by_book(_StaticFlaky(), 745000, market_type="f5_total")
        assert len(odds_rows_by_book(_StaticFlaky(), 745000, market_type="f1_total")) == 1
        assert odds_rows_by_book(_InstanceFlaky(), 7, market_type="total") == [
            {"game_pk": 7, "market_type": "total", "fake": True}
        ]
        assert prop_rows_by_book(_InstanceFlaky(), 7, 9, "hits") == [{"player_id": 9, "fake": True}]

    def test_the_real_stub_raises(self) -> None:
        with pytest.raises(RuntimeError, match="not yet implemented"):
            RealOddsAPIProvider().get_odds_by_book(1)
        with pytest.raises(RuntimeError, match="not yet implemented"):
            RealOddsAPIProvider().get_prop_odds_by_book(1, 2, "strikeouts")

    def test_the_helpers_fall_back_to_the_one_row_methods(self) -> None:
        fake = _OneRowFake()
        assert odds_rows_by_book(fake, 7, line_type="closing", market_type="total") == [
            {"game_pk": 7, "line_type": "closing", "market_type": "total"}
        ]
        assert prop_rows_by_book(fake, 7, 9, "hits", line_type="opening") == [
            {"game_pk": 7, "player_id": 9, "prop_stat": "hits"}
        ]
        broken = _BrokenByBook()
        assert len(odds_rows_by_book(broken, 7)) == 1
        assert len(prop_rows_by_book(broken, 7, 9, "hits")) == 1

    def test_the_helpers_use_the_by_book_methods(self) -> None:
        provider = _Fixture92857()
        rows = odds_rows_by_book(provider, 746437, line_type="closing", market_type="moneyline")
        assert len(rows) == 10
        props = prop_rows_by_book(provider, 746437, 682243, "strikeouts", line_type="closing")
        assert len(props) == 8


# ===========================================================================
# The provider: one row per book (plan tests 1-7, 12, 13)
# ===========================================================================


class TestOneRowPerBook:
    def test_one_row_per_book(self) -> None:
        """Plan test 1: ten closing rows (nine sportsbooks + the blend), one opening row."""
        provider = _Fixture92857()
        rows = provider.get_odds_by_book(746437, line_type="closing", market_type="moneyline")
        labels = [r["book"] for r in rows]
        assert labels == [f"bp:{b}" for b in (0, 10, 12, 19, 13, 24, 33, 15, 18, 27)]
        assert [book_kind(label) for label in labels].count("sportsbook") == 9
        # Each side comes from that book's own line in the payload.
        sels = _load("offers_ml_92857.json")["offers"][0]["selections"]
        det = next(s for s in sels if s["participant"] == "DET")
        sea = next(s for s in sels if s["participant"] == "SEA")
        for row in rows:
            book_id = row["book_id"]
            assert row["book"] == f"bp:{book_id}"
            assert row["home_ml"] == _book_line(det, book_id)["cost"]  # DET is home
            assert row["away_ml"] == _book_line(sea, book_id)["cost"]
            assert row["line_type"] == "closing"
            assert row["source"] == "bettingpros" and row["is_mock"] is False
            assert row["is_sharp_book"] is False
        opening = provider.get_odds_by_book(746437, line_type="opening", market_type="moneyline")
        assert len(opening) == 1
        assert opening[0]["book"] == "bp:10"  # FanDuel opened both sides
        assert (opening[0]["home_ml"], opening[0]["away_ml"]) == (126, -148)

    def test_the_rows_carry_every_get_odds_key(self) -> None:
        row = _Fixture92857().get_odds_by_book(746437, line_type="closing")[0]
        for key in (
            "game_pk",
            "source",
            "is_mock",
            "book",
            "line_type",
            "market_type",
            "is_sharp_book",
            *GAME_ODDS_FIELDS,
            "book_id",
            "book_line_at",
            "scheduled_start",
            "game_date",
        ):
            assert key in row, key
        assert row["scheduled_start"] == _utc("2024-08-15 17:10:00")
        assert row["game_date"] == "2024-08-15"

    def test_the_tie_joins_the_opening_row_only_from_the_same_opener(self) -> None:
        """Plan test 2, resolved by the spec: the first-inning moneyline's home and
        away opened at Caesars, its tie at FanDuel: one Caesars opening row, no tie."""
        provider = _Fixture99026()
        opening = provider.get_odds_by_book(1, line_type="opening", market_type="f1_moneyline")
        assert len(opening) == 1
        row = opening[0]
        assert row["book"] == "bp:13"
        assert (row["home_ml"], row["away_ml"]) == (-120, -110)  # ATL home, TB away
        assert row["draw_ml"] is None
        closing = provider.get_odds_by_book(1, line_type="closing", market_type="f1_moneyline")
        assert [r["book"] for r in closing] == ["bp:0", "bp:10", "bp:13"]

    def test_two_openers_write_no_opening_row(self) -> None:
        offers = _offers(
            _sel(
                participant="SD",
                opener=(10, 1, -150, "2025-09-02 18:00:00"),
                books={12: _line(1, -145, _T)},
            ),
            _sel(
                participant="BAL",
                opener=(12, 1, 130, "2025-09-02 18:00:00"),
                books={12: _line(1, 125, _T)},
            ),
        )
        provider = _Synthetic({122: offers})
        assert provider.get_odds_by_book(1, line_type="opening") == []
        closing = provider.get_odds_by_book(1, line_type="closing")
        assert [(r["book"], r["home_ml"], r["away_ml"]) for r in closing] == [("bp:12", -145, 125)]

    def test_a_book_quoting_one_side_gives_no_row(self) -> None:
        """Plan test 3 (first half)."""
        offers = _offers(
            _sel(participant="SD", books={12: _line(1, -145, _T), 10: _line(1, -140, _T)}),
            _sel(participant="BAL", books={12: _line(1, 125, _T)}),
        )
        rows = _Synthetic({122: offers}).get_odds_by_book(1, line_type="closing")
        assert [r["book"] for r in rows] == ["bp:12"]
        # In the captured payload theScore (33) quotes only the tie: no row.
        f1 = _Fixture99026().get_odds_by_book(1, line_type="closing", market_type="f1_moneyline")
        assert "bp:33" not in {r["book"] for r in f1}

    def test_a_side_missing_from_the_payload_gives_no_row_at_all(self) -> None:
        offers = _offers(
            _sel(participant="SD", opener=(12, 1, -150, _T), books={12: _line(1, -145, _T)})
        )
        provider = _Synthetic({122: offers})
        assert provider.get_odds_by_book(1, line_type="closing") == []
        assert provider.get_odds_by_book(1, line_type="opening") == []
        assert all(provider.get_odds(1)[f] is None for f in GAME_ODDS_FIELDS)

    def test_the_blend_is_stored_and_kinded(self) -> None:
        """Plan test 3 (second half): bp:0 is a row, a blend, never the default."""
        provider = _Fixture92857()
        rows = provider.get_odds_by_book(746437, line_type="closing")
        blend = _by_book(rows)["bp:0"]
        assert book_kind(blend["book"]) == "blend"
        assert provider.get_odds(746437, line_type="closing")["book"] != "bp:0"
        # A market only the blend quotes gives an empty default row.
        offers = _offers(
            _sel(participant="SD", books={0: _line(1, -145, _T), 73: _line(1, -150, _T)}),
            _sel(participant="BAL", books={0: _line(1, 125, _T), 73: _line(1, 130, _T)}),
        )
        synthetic = _Synthetic({122: offers})
        assert [r["book"] for r in synthetic.get_odds_by_book(1)] == ["bp:0", "bp:73"]
        default = synthetic.get_odds(1)
        assert default["book"] == "consensus"
        assert all(default[f] is None for f in GAME_ODDS_FIELDS)
        # A named non-sportsbook is still readable on request.
        assert synthetic.get_odds(1, book="bp:0")["home_ml"] == -145

    def test_get_odds_returns_the_first_preferred_book(self) -> None:
        """Plan test 4."""
        provider = _Fixture92857()
        dk = provider.get_odds(746437, line_type="closing")
        assert dk["book"] == "bp:12" and dk["book_id"] == 12
        assert (dk["home_ml"], dk["away_ml"]) == (120, -142)
        fd = provider.get_odds(746437, line_type="closing", book="fanduel")
        assert fd["book"] == "bp:10" and (fd["home_ml"], fd["away_ml"]) == (116, -136)
        assert provider.get_odds(746437, line_type="closing", book="bp:13")["home_ml"] == 122
        unknown = provider.get_odds(
            746437, line_type="closing", book="pinnacle", is_sharp_book=True
        )
        assert unknown["book"] == "pinnacle"
        assert unknown["is_sharp_book"] is True
        assert all(unknown[f] is None for f in GAME_ODDS_FIELDS)
        # A known book with no row gives an empty row too.
        assert provider.get_odds(746437, line_type="closing", book="hardrock")["home_ml"] is None
        # Every book's display name reads its row (review fix: "theScore Bet").
        score = provider.get_odds(746437, line_type="closing", book="theScore Bet")
        assert score["book"] == "bp:33" and score["home_ml"] is not None

    def test_the_default_falls_to_the_next_preferred_book(self, monkeypatch) -> None:
        # The first-inning run line: the first book on the list with a row answers
        # (on the 2026-09-29 list, theScore: DraftKings, BetMGM and FanDuel have none).
        provider = _Fixture99026()
        rows = _by_book(provider.get_odds_by_book(1, line_type="closing", market_type="f1_runline"))
        graded = next(label for label in graded_book_labels() if label in rows)
        row = provider.get_odds(1, line_type="closing", market_type="f1_runline")
        assert row["book"] == graded
        # A sportsbook off the list answers when no listed book has a row. Every
        # listed sportsbook is on the list since 2026-09-29, so the test takes
        # PartyCasino (27) off it.
        monkeypatch.setattr(
            "pipeline.bettingpros_odds_provider.GRADED_BOOK_PREFERENCE",
            tuple(b for b in GRADED_BOOK_PREFERENCE if b != 27),
        )
        offers = _offers(
            _sel(participant="SD", books={0: _line(1, -145, _T), 27: _line(1, -150, _T)}),
            _sel(participant="BAL", books={0: _line(1, 125, _T), 27: _line(1, 130, _T)}),
        )
        assert _Synthetic({122: offers}).get_odds(1)["book"] == "bp:27"
        # Review fix (VOCAB-1): a pick'em app (Betr, 45) or an id the vocabulary
        # does not list (999) is never the default, even with no other book.
        for book_id in (45, 999):
            offers = _offers(
                _sel(participant="SD", books={book_id: _line(1, -150, _T)}),
                _sel(participant="BAL", books={book_id: _line(1, 130, _T)}),
            )
            row = _Synthetic({122: offers}).get_odds(1)
            assert row["book"] == "consensus" and row["home_ml"] is None, book_id

    def test_an_is_off_line_is_not_a_quote(self) -> None:
        """Plan test 5: FanDuel's first-five run line is flagged is_off."""
        rows = _Fixture99026().get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        labels = [r["book"] for r in rows]
        assert "bp:10" not in labels
        assert "bp:33" in labels
        score = _by_book(rows)["bp:33"]
        assert (score["home_spread"], score["home_spread_ml"]) == (-0.5, 105)
        assert (score["away_spread"], score["away_spread_ml"]) == (0.5, -135)

    def test_the_line_usability_rules(self) -> None:
        assert _usable({"cost": -110})
        assert not _usable({"cost": -110, "is_off": True})
        assert not _usable({"cost": -110, "active": False})
        assert not _usable({"cost": None})
        sel = {
            "books": [
                {"id": 1, "lines": [{"cost": -110, "line": 1.5}]},  # no stamp: still usable
                {"id": 2, "lines": [_line(1.5, -120, _T, main=False), _line(2.5, 110, _T)]},
                {"id": 3, "lines": [_line(1.5, -120, _T, is_off=True)]},
            ]
        }
        assert _book_line(sel, 1) == {"cost": -110, "line": 1.5}
        chosen = _book_line(sel, 2)
        assert chosen is not None and chosen["line"] == 2.5  # the main line wins
        assert _book_line(sel, 3) is None
        assert _book_line(sel, 4) is None
        assert _book_line({"books": []}, 1) is None

    def test_the_row_carries_the_stamp(self) -> None:
        """Plan test 6: the opener's 'created' opening, the newest 'updated' closing."""
        provider = _Fixture99026()
        opening = provider.get_odds_by_book(1, line_type="opening", market_type="f1_total")
        assert opening[0]["book_line_at"] == _utc("2026-09-09 17:37:43")
        closing = _by_book(
            provider.get_odds_by_book(1, line_type="closing", market_type="f1_total")
        )
        # DraftKings' over is stamped 16:15:33 and its under 16:15:35: the row takes the newer.
        assert closing["bp:12"]["book_line_at"] == _utc("2026-09-10 16:15:35")
        assert closing["bp:12"]["book_line_at"].tzinfo is not None

    def test_the_total_keeps_one_line_for_both_sides(self) -> None:
        """Plan test 7: the row carries both sides' lines; the guard refuses a mismatch."""
        dk = _Fixture92857().get_odds(746437, line_type="closing", market_type="total")
        assert (dk["total_line"], dk["over_line"], dk["under_line"]) == (8.5, 8.5, 8.5)
        assert (dk["over_ml"], dk["under_ml"]) == (-105, -115)
        assert check_row(dk) is None
        offers = _offers(
            _sel(selection="over", books={12: _line(8.5, -105, _T), 10: _line(8.5, -110, _T)}),
            _sel(selection="under", books={12: _line(9, -115, _T), 10: _line(8.5, -110, _T)}),
        )
        rows = _by_book(_Synthetic({175: offers}).get_odds_by_book(1, market_type="total"))
        mixed = rows["bp:12"]
        assert (mixed["total_line"], mixed["over_line"], mixed["under_line"]) == (8.5, 8.5, 9)
        refusal = check_row(mixed)
        assert refusal is not None and refusal.rule == "total_line_mismatch"
        assert check_row(rows["bp:10"]) is None

    def test_a_three_way_row_keeps_draw_empty_when_the_book_lists_no_tie(self) -> None:
        """Plan test 12: Caesars prices the first-inning moneyline with no tie."""
        rows = _by_book(
            _Fixture99026().get_odds_by_book(1, line_type="closing", market_type="f1_moneyline")
        )
        assert rows["bp:13"]["draw_ml"] is None
        assert (rows["bp:13"]["home_ml"], rows["bp:13"]["away_ml"]) == (-125, -105)
        assert rows["bp:10"]["draw_ml"] == -125  # FanDuel lists the tie

    def test_the_three_full_game_markets_fill_only_their_own_columns(self) -> None:
        """Plan test 13: the legacy all-three-markets row is gone."""
        provider = _Fixture92857()
        own = {
            "moneyline": {"home_ml", "away_ml"},
            "runline": {"home_spread", "home_spread_ml", "away_spread", "away_spread_ml"},
            "total": {"total_line", "over_ml", "under_ml"},
        }
        for market, fields in own.items():
            for line_type in ("opening", "closing"):
                for row in provider.get_odds_by_book(
                    746437, line_type=line_type, market_type=market
                ):
                    filled = {f for f in GAME_ODDS_FIELDS if row[f] is not None}
                    assert filled == fields, (market, line_type, row["book"], filled)
                    assert row["market_type"] == market
        # Only the total-kind rows carry over_line / under_line.
        assert "over_line" not in provider.get_odds(746437, market_type="moneyline")
        assert "over_line" in provider.get_odds(746437, market_type="total")

    def test_run_line_stays_an_alias(self) -> None:
        provider = _Fixture92857()
        rows = provider.get_odds_by_book(746437, line_type="closing", market_type="run_line")
        assert rows and all(r["market_type"] == "run_line" for r in rows)
        dk = _by_book(rows)["bp:12"]
        assert (dk["home_spread"], dk["home_spread_ml"]) == (1.5, -142)
        assert (dk["away_spread"], dk["away_spread_ml"]) == (-1.5, 120)

    def test_an_unknown_market_raises_and_an_unknown_game_is_empty(self) -> None:
        with pytest.raises(ValueError, match="Unknown market_type"):
            _Fixture92857().get_odds_by_book(746437, market_type="sixth_inning_total")

        class _NoGame(_Fixture92857):
            def _mlb_get(self, path, params):  # type: ignore[override]
                return {"dates": []}

        assert _NoGame().get_odds_by_book(999) == []
        empty = _NoGame().get_odds(999)
        assert empty["source"] == "bettingpros" and empty["home_ml"] is None

    def test_prefer_book_id_pins_every_call(self, monkeypatch) -> None:
        pinned = _Fixture92857(prefer_book_id=15)
        rows = pinned.get_odds_by_book(746437, line_type="closing")
        assert [(r["book"], r["home_ml"], r["away_ml"]) for r in rows] == [("bp:15", 125, -148)]
        assert pinned.get_odds(746437, line_type="closing")["book"] == "bp:15"
        # The opener is FanDuel, not the pinned book: no opening row.
        assert pinned.get_odds_by_book(746437, line_type="opening") == []
        # Review fix: the default never returns a non-sportsbook, pinned or not. The
        # by-book read keeps the pinned blend; naming it reads it.
        blend_pin = _Fixture92857(prefer_book_id=0)
        assert [r["book"] for r in blend_pin.get_odds_by_book(746437, line_type="closing")] == [
            "bp:0"
        ]
        default = blend_pin.get_odds(746437, line_type="closing")
        assert default["book"] == "consensus"
        assert all(default[f] is None for f in GAME_ODDS_FIELDS)
        assert blend_pin.get_odds(746437, line_type="closing", book="bp:0")["book"] == "bp:0"
        # A pinned sportsbook off the preference list is still the default. Every
        # listed sportsbook is on the list since 2026-09-29, so the test takes
        # PartyCasino (27) off it.
        monkeypatch.setattr(
            "pipeline.bettingpros_odds_provider.GRADED_BOOK_PREFERENCE",
            tuple(b for b in GRADED_BOOK_PREFERENCE if b != 27),
        )
        party = _Fixture92857(prefer_book_id=27)
        assert party.get_odds(746437, line_type="closing")["book"] == "bp:27"
        absent = _Fixture92857(prefer_book_id=999_999)
        assert absent.get_odds_by_book(746437, line_type="closing") == []
        assert absent.get_odds(746437, line_type="closing")["home_ml"] is None

    def test_current_reads_the_same_per_book_rows_as_closing(self) -> None:
        provider = _Fixture92857()
        closing = provider.get_odds_by_book(746437, line_type="closing")
        current = provider.get_odds_by_book(746437, line_type="current")
        strip = ("line_type",)
        assert [{k: v for k, v in r.items() if k not in strip} for r in closing] == [
            {k: v for k, v in r.items() if k not in strip} for r in current
        ]


class TestPropRowsByBook:
    def test_one_row_per_book_and_the_opener(self) -> None:
        provider = _Fixture92857()
        rows = provider.get_prop_odds_by_book(746437, 682243, "strikeouts", line_type="closing")
        assert [r["book"] for r in rows] == [f"bp:{b}" for b in (0, 10, 19, 37, 36, 15, 18, 27)]
        by_book = _by_book(rows)
        assert (by_book["bp:18"]["over_ml"], by_book["bp:18"]["under_ml"]) == (-108, -127)
        assert by_book["bp:18"]["book_line_at"] == _utc("2024-08-15 17:01:41")
        assert (by_book["bp:37"]["line"], by_book["bp:37"]["over_line"]) == (5, 5)
        for row in rows:
            for key in ("player_id", "prop_stat", "line", "over_line", "under_line", "book_id"):
                assert key in row
            assert row["prop_stat"] == "strikeouts" and row["player_id"] == 682243
        opening = provider.get_prop_odds_by_book(746437, 682243, "strikeouts", line_type="opening")
        assert [(r["book"], r["line"], r["over_ml"], r["under_ml"]) for r in opening] == [
            ("bp:10", 5.5, 128, -164)
        ]

    #: The strikeout prop's closing (line, over, under) per sportsbook in the
    #: captured fixture. DraftKings has no offer. BetMGM and PartyCasino post one
    #: price; so do BetRivers and SugarHouse.
    K_CLOSES = {
        "bp:10": (5.5, 100, -128),  # FanDuel
        "bp:15": (5.5, -108, -127),  # SugarHouse
        "bp:18": (5.5, -108, -127),  # BetRivers
        "bp:19": (5.5, 100, -135),  # BetMGM
        "bp:27": (5.5, 100, -135),  # PartyCasino
    }

    def test_get_prop_odds_picks_the_preferred_book(self) -> None:
        provider = _Fixture92857()
        by_book = _by_book(
            provider.get_prop_odds_by_book(746437, 682243, "strikeouts", line_type="closing")
        )
        closes = {
            book: (row["line"], row["over_ml"], row["under_ml"])
            for book, row in by_book.items()
            if is_bettable(book)
        }
        assert closes == self.K_CLOSES
        # The first book on the preference list with a row answers.
        graded = next(label for label in graded_book_labels() if label in closes)
        quote = provider.get_prop_odds(746437, 682243, "strikeouts", line_type="closing")
        assert quote["book"] == graded
        assert (quote["line"], quote["over_ml"], quote["under_ml"]) == self.K_CLOSES[graded]
        named = provider.get_prop_odds(746437, 682243, "strikeouts", book="sugarhouse")
        assert (named["book"], named["over_ml"], named["under_ml"]) == ("bp:15", -108, -127)
        unknown = provider.get_prop_odds(746437, 682243, "strikeouts", book="pinnacle")
        assert unknown["book"] == "pinnacle" and unknown["line"] is None

    def test_the_line_falls_back_to_the_under(self) -> None:
        offer = {
            "participants": [{"player": {"first_name": "Bryce", "last_name": "Miller"}}],
            "selections": [
                _sel(selection="over", books={12: _line(None, -110, _T)}),
                _sel(selection="under", books={12: _line(5.5, -110, _T)}),
            ],
        }

        class _P(_Synthetic):
            def _resolve_player_name(self, player_id):  # type: ignore[override]
                return "bryce miller"

        row = _P({285: {"offers": [offer]}}).get_prop_odds_by_book(1, 2, "strikeouts")[0]
        assert (row["line"], row["over_line"], row["under_line"]) == (5.5, None, 5.5)
        refusal = check_row(row)
        assert refusal is not None and refusal.rule == "total_line_mismatch"

    def test_unknown_stat_raises_and_unknown_player_is_empty(self) -> None:
        provider = _Fixture92857()
        with pytest.raises(ValueError, match="Unknown prop_stat"):
            provider.get_prop_odds_by_book(746437, 1, "innings_pitched")

        class _Nobody(_Fixture92857):
            def _mlb_get(self, path, params):  # type: ignore[override]
                if path.startswith("people/"):
                    return {"people": [{"fullName": "Nobody Here"}]}
                return super()._mlb_get(path, params)

        assert _Nobody().get_prop_odds_by_book(746437, 1, "strikeouts") == []


# ===========================================================================
# The guard (plan tests 7-11) and its tally
# ===========================================================================


def _runline(hs, hml, as_, aml, *, market="runline", line_type="closing", **extra):
    row = dict.fromkeys(GAME_ODDS_FIELDS)
    row.update(
        {
            "market_type": market,
            "line_type": line_type,
            "home_spread": hs,
            "home_spread_ml": hml,
            "away_spread": as_,
            "away_spread_ml": aml,
        }
    )
    row.update(extra)
    return row


class TestGuard:
    def test_implied_probability(self) -> None:
        assert implied_probability(150) == pytest.approx(0.40)
        assert implied_probability(-150) == pytest.approx(0.60)
        assert implied_probability(100) == pytest.approx(0.5)
        assert implied_probability(-100) == pytest.approx(0.5)

    def test_the_constants(self) -> None:
        assert PAIR_PRICE_BAND == (1.00, 1.10)
        assert F5_TIE_IMPLIED_MAX == 0.35
        assert F5_WIN_OR_TIE_SUM_MAX == 1.40
        assert THREE_WAY_SUM_MIN == 0.98
        assert THREE_WAY_SUM_MAX == 1.18
        assert THREE_WAY_TEAM_SUM_MAX == 1.00
        assert F5_TOTAL_LINE_MAX == 1.5
        assert TEAM_TOTAL_LINE_MAX == 1.0
        assert timedelta(minutes=15) == CLOSING_STAMP_GRACE
        assert RULES == (
            "missing_side",
            "spread_size",
            "equal_spreads_priced_like_a_pair",
            "f5_win_or_tie",
            "total_line_mismatch",
            "f5_total_first_inning_line",
            "team_total_placeholder_line",
            "f5_tie_price",
            "three_way_sum_below_one",
            "three_way_two_way_team_prices",
            "three_way_sum_above_max",
            "late_closing_stamp",
        )

    def test_the_guard_refuses_equal_spreads_priced_like_a_pair(self) -> None:
        """Plan test 8: the appendix-B payload (Hard Rock 1 / 1 at -400 / +260)."""
        rows = _by_book(
            _Synthetic({282: _APPENDIX_B}).get_odds_by_book(
                1, line_type="closing", market_type="f1_runline"
            )
        )
        hard_rock = rows["bp:49"]
        assert (hard_rock["home_spread"], hard_rock["away_spread"]) == (1, 1)
        refusal = check_row(hard_rock)
        assert refusal is not None and refusal.rule == "equal_spreads_priced_like_a_pair"
        assert "1.078" in refusal.message
        assert check_row(rows["bp:10"]) is None  # FanDuel's -1.5 / +1.5 pair
        assert check_row(rows["bp:33"]) is None  # theScore's -0.5 / +0.5 pair
        # A real "+1.5 / +1.5" listing (two separate bets at -200 each) adds 1.33: kept.
        assert check_row(_runline(1.5, -200, 1.5, -200)) is None
        # A both "-1.5" listing adds below 1.00: kept.
        assert check_row(_runline(-1.5, 280, -1.5, 150, market="f5_runline")) is None

    def test_the_guard_refuses_spreads_of_different_size(self) -> None:
        """Plan test 9."""
        refusal = check_row(_runline(-1, -120, 1.5, 100))
        assert refusal is not None and refusal.rule == "spread_size"
        assert "(-1 / +1.5)" in refusal.message
        assert check_row(_runline(-1.5, 140, 1.5, -160)) is None
        assert check_row(_runline(0, 105, 0, -125, market="f5_runline")) is None  # a pick'em pair

    def test_a_missing_side(self) -> None:
        for row in (
            _runline(-1.5, None, 1.5, -160),
            {"market_type": "moneyline", "home_ml": -150, "away_ml": None, "line_type": "closing"},
            {"market_type": "f1_moneyline", "home_ml": None, "away_ml": 120, "draw_ml": -120},
            {"market_type": "total", "total_line": None, "over_ml": -110, "under_ml": -110},
            {
                "market_type": "first_inning_run",
                "total_line": 0.5,
                "over_ml": -110,
                "under_ml": None,
            },
            {"prop_stat": "strikeouts", "line": 5.5, "over_ml": None, "under_ml": -110},
        ):
            refusal = check_row(row)
            assert refusal is not None and refusal.rule == "missing_side", row

    def test_an_empty_row_is_not_refused(self) -> None:
        assert check_row({"market_type": "runline", **dict.fromkeys(GAME_ODDS_FIELDS)}) is None
        assert (
            check_row({"prop_stat": "hits", "line": None, "over_ml": None, "under_ml": None})
            is None
        )

    def test_a_row_without_side_lines_skips_the_line_rule(self) -> None:
        # A mock or hand-built total row carries no over_line / under_line keys.
        assert check_row(MockOddsAPI.get_odds(745000, market_type="total")) is None
        mock_prop = MockOddsAPI.get_prop_odds(745000, 101, "strikeouts")
        assert check_row(mock_prop) is None

    def test_the_first_five_rules(self) -> None:
        """Plan test 10 (the guard's two first-five rules)."""
        both_plus_half = _runline(0.5, -400, 0.5, -400, market="f5_runline")
        refusal = check_row(both_plus_half)
        assert refusal is not None and refusal.rule == "f5_win_or_tie"
        # The same prices on the first-inning run line are that market's real shape.
        assert check_row(_runline(0.5, -400, 0.5, -400, market="f1_runline")) is None
        # A real first-five "+0.5 / +0.5" (both sides as favourites near even) passes.
        assert check_row(_runline(0.5, -130, 0.5, -120, market="f5_runline")) is None
        # SIM-555 census fix: the team prices add to 0.855, so with a real tie at
        # +400 (0.200) the row adds to 1.055. At +150 / +170 it added to 0.970,
        # which the three-way sum rule now refuses.
        tie = {"market_type": "f5_moneyline", "home_ml": 120, "away_ml": 150, "draw_ml": -110}
        refusal = check_row(tie)
        assert refusal is not None and refusal.rule == "f5_tie_price"
        assert check_row({**tie, "draw_ml": 400}) is None  # a real first-five tie price
        assert check_row({**tie, "draw_ml": None}) is None
        # A first-inning tie at -110 is real over one inning, with first-inning team
        # prices (the +120 / +150 pair above is a first-five pair: over one inning it
        # adds to 1.38 with the tie, which three_way_sum_above_max refuses).
        f1 = {"market_type": "f1_moneyline", "home_ml": 300, "away_ml": 320, "draw_ml": -110}
        assert check_row(f1) is None
        assert check_row({**tie, "market_type": "f1_moneyline"}).rule == "three_way_sum_above_max"

    def test_a_three_way_row_adding_below_0_98_is_refused(self) -> None:
        """SIM-555 census fix: no book sells every outcome of one market for less
        than the stake. The boundary: 0.98 exactly is kept."""

        def three_way(home, away, draw, market="f5_moneyline"):
            return {
                "market_type": market,
                "line_type": "closing",
                "home_ml": home,
                "away_ml": away,
                "draw_ml": draw,
            }

        # +100 / +150 / +1150: 0.50 + 0.40 + 0.08 = 0.98 exactly. Kept.
        assert check_row(three_way(100, 150, 1150)) is None
        # +100 / +150 / +1200: 0.50 + 0.40 + 0.077 = 0.977. Refused.
        refusal = check_row(three_way(100, 150, 1200))
        assert refusal is not None and refusal.rule == "three_way_sum_below_one"
        assert "0.977" in refusal.message and "less than the stake" in refusal.message
        # The rule reads the first-inning moneyline too.
        refusal = check_row(three_way(100, 150, 1200, market="f1_moneyline"))
        assert refusal is not None and refusal.rule == "three_way_sum_below_one"
        # A near-fair row below 1.00, as Kalshi's census rows (0.989 and 0.998), is kept:
        # +100 / +150 / +1000 add to 0.50 + 0.40 + 0.091 = 0.991.
        assert check_row(three_way(100, 150, 1000)) is None
        # A book's usual row adds above 1.00: kept.
        assert check_row(three_way(125, 150, 380)) is None  # 0.444 + 0.400 + 0.208 = 1.053
        # A row without a tie is two-way: the rule needs all three prices.
        assert check_row(three_way(300, 400, None)) is None
        # A full-game moneyline is never a three-way row.
        assert check_row({**three_way(300, 400, None), "market_type": "moneyline"}) is None

    def test_the_fanduel_2025_07_03_first_five_row_is_refused(self) -> None:
        """The census row: FanDuel's (bp:10) first-five moneyline on 2025-07-03 carried
        its first-inning team prices beside a first-five tie. Its tie (+560, 0.152)
        passes the tie rule; the three prices add to 0.670. Its own first-inning row
        (+190 / +430 / -110) adds to 1.057 and is kept."""
        f5 = {
            "book": "bp:10",
            "market_type": "f5_moneyline",
            "line_type": "closing",
            "home_ml": 210,
            "away_ml": 410,
            "draw_ml": 560,
        }
        refusal = check_row(f5)
        assert refusal is not None and refusal.rule == "three_way_sum_below_one"
        assert "0.670" in refusal.message
        f1 = {**f5, "market_type": "f1_moneyline", "home_ml": 190, "away_ml": 430, "draw_ml": -110}
        assert check_row(f1) is None

    def test_a_first_five_total_at_1_5_or_below_is_refused(self) -> None:
        """SIM-555 census fix: a first-five total of both teams at 1.5 or below is a
        first-inning total under the first-five label (a real one sits at 3.5-6.5)."""

        def total(line, market="f5_total"):
            return {
                "market_type": market,
                "line_type": "closing",
                "total_line": line,
                "over_line": line,
                "under_line": line,
                "over_ml": -110,
                "under_ml": -110,
            }

        for line in (0.5, 1.0, 1.5):
            refusal = check_row(total(line))
            assert refusal is not None and refusal.rule == "f5_total_first_inning_line", line
        assert "first-five total at 1.5" in refuse_reason(total(1.5))
        for line in (2.0, 2.5, 4.5):
            assert check_row(total(line)) is None, line
        # A row without side lines (a mock or hand-built row) is still checked.
        assert check_row({**total(0.5), "over_line": None, "under_line": None}) is not None
        # A first-five TEAM total at 0.5 is not refused by this rule.
        for market in ("f5_team_total_home", "f5_team_total_away"):
            assert check_row(total(0.5, market=market)) is None, market
        # The full-game total and the first-inning total are untouched.
        assert check_row(total(0.5, market="total")) is None
        assert check_row(total(0.5, market="f1_total")) is None
        # A prop is never a first-five total.
        prop = {"prop_stat": "strikeouts", "line": 0.5, "over_ml": -110, "under_ml": -110}
        assert check_row(prop) is None

    def test_a_full_game_team_total_at_1_or_below_is_refused(self) -> None:
        """SIM-555 (the 2024 season census): Caesars opened every 2024 team total at
        a line of 1 with prices such as -182 / -1200, a placeholder, not a bet (the
        other books closed the same games at 3.5-4.5). A line of 1.5 is a real
        alternate line (DraftKings 2025-06-19: 1.5 at -160 beside 2.5 elsewhere)."""

        def team_total(line, market="team_total_home", over=-182, under=-1200):
            return {
                "market_type": market,
                "line_type": "opening",
                "total_line": line,
                "over_line": line,
                "under_line": line,
                "over_ml": over,
                "under_ml": under,
            }

        for market in ("team_total_home", "team_total_away"):
            for line in (0.5, 1.0):
                refusal = check_row(team_total(line, market=market))
                assert refusal is not None, (market, line)
                assert refusal.rule == "team_total_placeholder_line", (market, line)
            # A real line passes, 1.5 (an alternate line) included.
            assert check_row(team_total(1.5, market=market, over=-160, under=124)) is None
            for line in (2.5, 3.5, 4.5):
                assert check_row(team_total(line, market=market, over=-110, under=-110)) is None
        # The 700 / -800 row adds to a normal margin, so only the line tells it apart.
        assert check_row(team_total(1.0, over=700, under=-800)) is not None
        assert "team total at 1" in refuse_reason(team_total(1.0))
        # A first-five team total at 0.5 is real (books post it) and is not refused.
        for market in ("f5_team_total_home", "f5_team_total_away"):
            assert check_row(team_total(0.5, market=market, over=-150, under=120)) is None
        # The game total and a prop at a low line are untouched by this rule.
        assert check_row(team_total(1.0, market="total", over=-110, under=-110)) is None
        prop = {"prop_stat": "hits", "line": 0.5, "over_ml": -200, "under_ml": 150}
        assert check_row(prop) is None

    def test_a_two_way_pair_beside_a_tie_is_refused(self) -> None:
        """SIM-555 (the book-order read, 2026-09-29): a three-way row whose team prices
        are the tie-refunded two-way pair, listed beside a separate tie price."""

        def three_way(home, away, draw, market="f1_moneyline"):
            return {
                "market_type": market,
                "line_type": "closing",
                "home_ml": home,
                "away_ml": away,
                "draw_ml": draw,
            }

        # theScore 2025-09-13, first inning: -130 / +100 / tie -145 (teams 1.065).
        refusal = check_row(three_way(-130, 100, -145))
        assert refusal is not None and refusal.rule == "three_way_two_way_team_prices"
        assert "two team prices alone add to 1.065" in refusal.message
        # theScore 2025, first five: +110 / -140 / +500 (teams 1.060, three 1.226).
        refusal = check_row(three_way(110, -140, 500, market="f5_moneyline"))
        assert refusal is not None and refusal.rule == "three_way_two_way_team_prices"
        # The same game's real three-way rows pass (DraftKings / SugarHouse).
        assert check_row(three_way(310, 300, -150)) is None
        assert check_row(three_way(295, 310, -141)) is None
        # A real first-five three-way row: teams about 0.92, the three about 1.09.
        assert check_row(three_way(140, -105, 500, market="f5_moneyline")) is None
        # The team-price limit is 1.00: +100 / +100 adds to exactly 1.00 and is refused.
        assert check_row(three_way(100, 100, 500, market="f5_moneyline")).rule == (
            "three_way_two_way_team_prices"
        )
        # A two-way row (no tie) is never checked by these rules.
        assert check_row(three_way(-130, 100, None)) is None

    def test_a_three_way_row_above_1_18_is_refused(self) -> None:
        """A real three-way row adds to 1.02-1.15; theScore's first-inning rows with a
        two-way pair below 1.00 still add to 1.35 or more."""

        def three_way(home, away, draw):
            return {
                "market_type": "f1_moneyline",
                "line_type": "closing",
                "home_ml": home,
                "away_ml": away,
                "draw_ml": draw,
            }

        # Teams 0.782 (under 1.00) with a first-inning tie: the three add to about 1.35.
        refusal = check_row(three_way(160, 175, -150))
        assert refusal is not None and refusal.rule == "three_way_sum_above_max", refusal
        # 1.18 exactly is kept; a real DraftKings row (1.09) is kept.
        assert check_row(three_way(300, 290, -130)) is None
        home, away = 250, 250  # 0.2857 each
        draw_prob = THREE_WAY_SUM_MAX - 2 * 100 / 350
        draw = -round(100 * draw_prob / (1 - draw_prob), 6)
        row = three_way(home, away, draw)
        assert implied_probability(home) * 2 + implied_probability(draw) == pytest.approx(1.18)
        assert check_row({**row, "draw_ml": draw + 0.5}) is None  # a hair under 1.18

    def test_the_closing_stamp_grace(self) -> None:
        """Plan test 11: 5 minutes after the start kept; 40 minutes refused."""
        start = "2025-04-08 23:05:00"
        offers = _offers(
            _sel(
                participant="SD",
                opener=(12, 1, -140, "2025-04-07 18:00:00"),
                books={
                    12: _line(1, -145, "2025-04-08 23:10:00"),
                    10: _line(1, -150, "2025-04-08 23:45:00"),
                },
            ),
            _sel(
                participant="BAL",
                opener=(12, 1, 120, "2025-04-07 18:00:00"),
                books={
                    12: _line(1, 125, "2025-04-08 23:10:00"),
                    10: _line(1, 130, "2025-04-08 23:45:00"),
                },
            ),
        )
        provider = _Synthetic(
            {122: offers}, game_date="2025-04-08", start=datetime(2025, 4, 8, 23, 5)
        )
        rows = _by_book(provider.get_odds_by_book(1, line_type="closing"))
        assert rows["bp:12"]["scheduled_start"] == _utc(start)
        assert check_row(rows["bp:12"]) is None
        refusal = check_row(rows["bp:10"])
        assert refusal is not None and refusal.rule == "late_closing_stamp"
        assert "40 minutes" in refusal.message
        assert refuse_reason(rows["bp:10"]) == refusal.message
        # A current or opening row is never refused for its stamp.
        current = _by_book(provider.get_odds_by_book(1, line_type="current"))
        assert check_row(current["bp:10"]) is None

    def test_a_naive_stamp_reads_as_utc(self) -> None:
        row = {
            "market_type": "moneyline",
            "line_type": "closing",
            "home_ml": -150,
            "away_ml": 130,
            "book_line_at": datetime(2025, 4, 8, 23, 25),
            "scheduled_start": _utc("2025-04-08 23:05:00"),
        }
        refusal = check_row(row)
        assert refusal is not None and refusal.rule == "late_closing_stamp"
        assert check_row({**row, "book_line_at": datetime(2025, 4, 8, 23, 19)}) is None
        assert check_row({**row, "scheduled_start": None}) is None

    def test_the_tally(self, caplog) -> None:
        tally = RefusalTally()
        tally.offered(40)
        pair = Refusal("equal_spreads_priced_like_a_pair", "x")
        late = Refusal("late_closing_stamp", "y")
        tally.add(pair, "f1_runline", "bp:49")
        tally.add(pair, "f1_runline", "bp:49")
        tally.add(late, "moneyline", "bp:10")
        assert tally.refused == 3
        assert tally.n_offered == 40
        assert tally.counts() == {
            ("equal_spreads_priced_like_a_pair", "f1_runline", "bp:49"): 2,
            ("late_closing_stamp", "moneyline", "bp:10"): 1,
        }
        summary = tally.summary()
        assert summary.splitlines()[0] == "guard refusals: 3 of 40 rows offered (7.5%)"
        assert summary.index("equal_spreads_priced_like_a_pair") < summary.index(
            "late_closing_stamp"
        )
        assert "      bp:49: 2" in summary
        logger = logging.getLogger("test_sim555_tally")
        with caplog.at_level(logging.WARNING, logger="test_sim555_tally"):
            assert tally.warn_if_share_above(0.05, log=logger) is True
        assert any("3 of 40" in r.getMessage() for r in caplog.records)
        assert tally.warn_if_share_above(0.10, log=logger) is False
        assert RefusalTally().warn_if_share_above() is False
        assert RefusalTally().summary() == "guard refusals: 0 of 0 rows offered (0.0%)"


# ===========================================================================
# The first-five exclusions (plan test 10, the provider's half)
# ===========================================================================

_F5_DAY = "2025-06-01 23:00:00"


def _f5_payloads(*, dk: tuple[float, float, float, float]) -> dict[int, dict[str, Any]]:
    """A first-five run line (283) and its first-inning run line (282), SD home.

    FanDuel (10) opens both markets at the same "+1.5 -800 / -1.5 +520" (the
    2025-06-01 copy) and its first-five closing lines are off. bet365 (24)
    posts first-five lines that copy its own first-inning lines within 0.01.
    BetMGM (19) posts a real first-five line. Caesars (13) posts both teams
    "+0.5" at -400 / -400. ``dk`` is DraftKings' (12) first-five
    (home spread, home price, away spread, away price).
    """
    hs, hml, as_, aml = dk
    f5 = _offers(
        _sel(
            participant="SD",
            opener=(10, 1.5, -800, "2025-05-31 18:00:00"),
            books={
                10: _line(1.5, -750, _F5_DAY, is_off=True),
                24: _line(0.5, -340, _F5_DAY),
                19: _line(-0.5, 120, _F5_DAY),
                13: _line(0.5, -400, _F5_DAY),
                12: _line(hs, hml, _F5_DAY),
            },
        ),
        _sel(
            participant="BAL",
            opener=(10, -1.5, 520, "2025-05-31 18:00:00"),
            books={
                10: _line(-1.5, 490, _F5_DAY, is_off=True),
                24: _line(0.5, -350, _F5_DAY),
                19: _line(0.5, -145, _F5_DAY),
                13: _line(0.5, -400, _F5_DAY),
                12: _line(as_, aml, _F5_DAY),
            },
        ),
    )
    f1 = _offers(
        _sel(
            participant="SD",
            opener=(10, 1.5, -790, "2025-05-31 17:00:00"),
            books={24: _line(0.5, -345, _F5_DAY), 19: _line(0.5, -300, _F5_DAY)},
        ),
        _sel(
            participant="BAL",
            opener=(10, -1.5, 520, "2025-05-31 17:00:00"),
            books={24: _line(0.5, -350, _F5_DAY), 19: _line(0.5, -310, _F5_DAY)},
        ),
    )
    return {283: f5, 282: f1}


def _dk_f5_payloads() -> dict[int, dict[str, Any]]:
    """The three first-five markets and their first-inning twins, SD home.

    DraftKings (12) and BetMGM (19) post real first-five lines on every
    market (none copies its own first-inning line); a first-five total at 4.5.
    """
    return {
        279: _offers(
            _sel(participant="SD", books={12: _line(1, 125, _F5_DAY), 19: _line(1, 120, _F5_DAY)}),
            _sel(participant="BAL", books={12: _line(1, 150, _F5_DAY), 19: _line(1, 155, _F5_DAY)}),
            _sel(selection="draw", books={12: _line(1, 450, _F5_DAY), 19: _line(1, 440, _F5_DAY)}),
        ),
        278: _offers(
            _sel(participant="SD", books={12: _line(1, 250, _F5_DAY)}),
            _sel(participant="BAL", books={12: _line(1, 280, _F5_DAY)}),
            _sel(selection="draw", books={12: _line(1, -125, _F5_DAY)}),
        ),
        283: _offers(
            _sel(
                participant="SD",
                books={12: _line(-0.5, 110, _F5_DAY), 19: _line(-0.5, 115, _F5_DAY)},
            ),
            _sel(
                participant="BAL",
                books={12: _line(0.5, -130, _F5_DAY), 19: _line(0.5, -135, _F5_DAY)},
            ),
        ),
        282: _offers(
            _sel(participant="SD", books={12: _line(0.5, -340, _F5_DAY)}),
            _sel(participant="BAL", books={12: _line(0.5, -350, _F5_DAY)}),
        ),
        281: _offers(
            _sel(
                selection="over",
                books={12: _line(4.5, -110, _F5_DAY), 19: _line(4.5, -105, _F5_DAY)},
            ),
            _sel(
                selection="under",
                books={12: _line(4.5, -110, _F5_DAY), 19: _line(4.5, -115, _F5_DAY)},
            ),
        ),
        280: _offers(
            _sel(selection="over", books={12: _line(0.5, 110, _F5_DAY)}),
            _sel(selection="under", books={12: _line(0.5, -140, _F5_DAY)}),
        ),
    }


class TestFirstFiveExclusions:
    def test_the_constants(self) -> None:
        assert F5_TWIN_MARKETS == {283: 282, 281: 280, 279: 278}
        assert {12: datetime(2025, 3, 1).date()} == F5_EXCLUDED_BOOKS
        assert {12: frozenset({283, 281})} == F5_EXCLUDED_MARKETS
        assert TWIN_PRICE_TOLERANCE == 0.03

    def test_the_dated_exclusion_applies_per_market(self) -> None:
        """SIM-555 census fix: DraftKings' dated exclusion covers its first-five run
        line (283) and total (281), not its first-five moneyline (279). A listed book
        with no F5_EXCLUDED_MARKETS entry covers all three."""
        on, before = datetime(2025, 3, 1).date(), datetime(2025, 2, 28).date()
        assert f5_excluded_markets(12) == frozenset({283, 281})
        assert f5_excluded_markets(99) == frozenset(F5_TWIN_MARKETS)
        assert is_f5_dated_excluded(12, 283, on) and is_f5_dated_excluded(12, 281, on)
        assert not is_f5_dated_excluded(12, 279, on)
        assert not is_f5_dated_excluded(12, 283, before)
        assert not is_f5_dated_excluded(12, 283, None)
        assert not is_f5_dated_excluded(10, 283, on)  # a book off the list

    def test_draftkings_keeps_its_first_five_moneyline(self) -> None:
        """From 2025-03-01 DraftKings' first-five moneyline row is KEPT; its first-five
        run line and total rows are dropped. Before the date all three are kept."""
        for day in ("2025-03-01", "2025-06-01"):
            provider = _Synthetic(_dk_f5_payloads(), game_date=day)
            books = {
                market: [
                    r["book"]
                    for r in provider.get_odds_by_book(1, line_type="closing", market_type=market)
                ]
                for market in ("f5_moneyline", "f5_runline", "f5_total")
            }
            assert books == {
                "f5_moneyline": ["bp:12", "bp:19"],
                "f5_runline": ["bp:19"],
                "f5_total": ["bp:19"],
            }, day
            moneyline = _by_book(
                provider.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
            )
            assert (moneyline["bp:12"]["home_ml"], moneyline["bp:12"]["draw_ml"]) == (125, 450)
            assert check_row(moneyline["bp:12"]) is None
            # DraftKings leads the preference list, so it is the graded row again.
            graded = provider.get_odds(1, line_type="closing", market_type="f5_moneyline")
            assert graded["book"] == "bp:12"
            assert (
                provider.get_odds(1, line_type="closing", market_type="f5_runline")["book"]
                == "bp:19"
            )
        before = _Synthetic(_dk_f5_payloads(), game_date="2025-02-28")
        for market in ("f5_moneyline", "f5_runline", "f5_total"):
            rows = before.get_odds_by_book(1, line_type="closing", market_type=market)
            assert "bp:12" in {r["book"] for r in rows}, market

    def test_the_moneyline_exclusion_is_neither_applied_nor_logged(self, caplog) -> None:
        provider = _Synthetic(_dk_f5_payloads(), game_date="2025-06-01")
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            for market in ("f5_moneyline", "f5_runline", "f5_total"):
                provider.get_odds_by_book(1, line_type="closing", market_type=market)
        dated = [r.getMessage() for r in caplog.records if "excluded from" in r.getMessage()]
        assert len(dated) == 2
        assert all("bp:12" in m for m in dated)
        assert not any("market 279" in m for m in dated)

    def test_an_opener_copying_its_first_inning_opener_gives_no_opening_row(self) -> None:
        provider = _Synthetic(_f5_payloads(dk=(-1.5, 280, -1.5, 150)), game_date="2025-06-01")
        assert provider.get_odds_by_book(1, line_type="opening", market_type="f5_runline") == []
        # The first-inning market's own opening row is untouched.
        f1 = provider.get_odds_by_book(1, line_type="opening", market_type="f1_runline")
        assert [r["book"] for r in f1] == ["bp:10"]

    def test_a_book_copying_its_first_inning_lines_gives_no_row(self) -> None:
        provider = _Synthetic(_f5_payloads(dk=(-1.5, 280, -1.5, 150)), game_date="2024-08-14")
        labels = [
            r["book"]
            for r in provider.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        ]
        assert "bp:24" not in labels  # bet365: the twin
        assert "bp:10" not in labels  # FanDuel: is_off
        assert "bp:19" in labels  # BetMGM: a real first-five line
        # The first-inning market keeps bet365's row: the twin rule reads the first-five market only.
        f1 = provider.get_odds_by_book(1, line_type="closing", market_type="f1_runline")
        assert "bp:24" in {r["book"] for r in f1}

    def test_draftkings_is_dated_out_from_2025_03_01(self) -> None:
        payloads = _f5_payloads(dk=(-1.5, 280, -1.5, 150))
        before = _Synthetic(payloads, game_date="2024-08-14")
        dk = _by_book(before.get_odds_by_book(1, line_type="closing", market_type="f5_runline"))
        assert "bp:12" in dk  # a real 2024 first-five entry: kept
        assert check_row(dk["bp:12"]) is None
        assert before.get_odds(1, line_type="closing", market_type="f5_runline")["book"] == "bp:12"
        for day in ("2025-03-01", "2025-06-01"):
            after = _Synthetic(payloads, game_date=day)
            rows = after.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
            assert "bp:12" not in {r["book"] for r in rows}, day
            # The graded row falls to the next preferred book with a row: BetMGM.
            assert (
                after.get_odds(1, line_type="closing", market_type="f5_runline")["book"] == "bp:19"
            )
        # The exclusion reads the first-five markets only.
        full_game = _Synthetic({176: payloads[283]}, game_date="2025-06-01")
        assert "bp:12" in {r["book"] for r in full_game.get_odds_by_book(1, market_type="runline")}

    def test_the_win_or_tie_row_is_stored_by_the_provider_and_refused_by_the_guard(self) -> None:
        rows = _by_book(
            _Synthetic(
                _f5_payloads(dk=(-1.5, 280, -1.5, 150)), game_date="2024-08-14"
            ).get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        )
        refusal = check_row(rows["bp:13"])
        assert refusal is not None and refusal.rule == "f5_win_or_tie"

    def test_the_dated_exclusion_drops_an_excluded_opener(self) -> None:
        offers = _offers(
            _sel(
                participant="SD",
                opener=(12, -0.5, 120, "2025-05-31 18:00:00"),
                books={19: _line(-0.5, 120, _F5_DAY)},
            ),
            _sel(
                participant="BAL",
                opener=(12, 0.5, -145, "2025-05-31 18:00:00"),
                books={19: _line(0.5, -145, _F5_DAY)},
            ),
        )
        after = _Synthetic({283: offers}, game_date="2025-06-01")
        assert after.get_odds_by_book(1, line_type="opening", market_type="f5_runline") == []
        before = _Synthetic({283: offers}, game_date="2024-06-01")
        opening = before.get_odds_by_book(1, line_type="opening", market_type="f5_runline")
        assert [r["book"] for r in opening] == ["bp:12"]

    def test_the_captured_first_five_markets(self) -> None:
        provider = _Fixture99026(game_date="2026-09-10", offers_cache_ttl_s=600)
        # First-five total: DraftKings dated out, FanDuel off: the blend alone.
        rows = provider.get_odds_by_book(1, line_type="closing", market_type="f5_total")
        assert [r["book"] for r in rows] == ["bp:0"]
        # The first-five moneyline's opener (Caesars) is not a twin: one opening row, tie included.
        opening = provider.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline")
        assert [(r["book"], r["home_ml"], r["away_ml"], r["draw_ml"]) for r in opening] == [
            ("bp:13", 112, 120, 460)
        ]
        # The first-five run line fetched its first-inning twin once.
        provider.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        provider.get_odds_by_book(1, line_type="opening", market_type="f5_runline")
        assert provider.offers_calls.count(282) == 1

    def test_a_failed_first_inning_read_gives_no_first_five_rows(self, caplog) -> None:
        """Review fix: the twin check fails closed. A failed first-inning read drops the
        first-five market's rows (a WARNING names the game); it used to keep every
        book, a copier included. An EMPTY first-inning market is not a failure: no
        twin is possible, so the rows stand."""
        payloads = _f5_payloads(dk=(-1.5, 280, -1.5, 150))

        class _FlakyFirstInning(_Synthetic):
            fail = True

            def _bp_get(self, path, params):  # type: ignore[override]
                if int(params["market_id"]) == 282 and self.fail:
                    raise RuntimeError("HTTP 429")
                return super()._bp_get(path, params)

        provider = _FlakyFirstInning(payloads, game_date="2024-08-14")
        with caplog.at_level(logging.WARNING, logger="pipeline.bettingpros_odds_provider"):
            for line_type in ("closing", "opening", "current"):
                assert (
                    provider.get_odds_by_book(1, line_type=line_type, market_type="f5_runline")
                    == []
                ), line_type
            empty = provider.get_odds(1, line_type="closing", market_type="f5_runline")
            assert all(empty[f] is None for f in GAME_ODDS_FIELDS)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("twin check could not run" in m and "HTTP 429" in m for m in warnings)
        # The failure is not cached: the next good read gives the checked rows.
        provider.fail = False
        labels = [
            r["book"]
            for r in provider.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        ]
        assert "bp:19" in labels and "bp:24" not in labels
        # An empty first-inning market keeps the first-five rows.
        no_f1 = _Synthetic({283: payloads[283], 282: {"offers": []}}, game_date="2024-08-14")
        labels = [
            r["book"]
            for r in no_f1.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        ]
        assert {"bp:24", "bp:19"} <= set(labels)

    def test_the_captured_copier_is_dropped_when_the_first_inning_read_fails(self) -> None:
        """Review fix, on the captured payload: theScore's (33) first-five run line set to
        its own first-inning lines. A good read drops it as a twin; a failed read now
        drops the whole market instead of storing theScore's first-inning prices."""
        f5 = _load("offers_f5_runline_99026.json")
        f1_lines = {
            s["participant"]: _book_line(s, 33)
            for s in _load("offers_f1_runline_99026.json")["offers"][0]["selections"]
        }
        for sel in f5["offers"][0]["selections"]:
            for book in sel["books"]:
                if book["id"] == 33:
                    book["lines"][0]["line"] = f1_lines[sel["participant"]]["line"]
                    book["lines"][0]["cost"] = f1_lines[sel["participant"]]["cost"]

        class _Copier(_Fixture99026):
            fail = False

            def _bp_get(self, path, params):  # type: ignore[override]
                if int(params["market_id"]) == 283:
                    return f5
                if int(params["market_id"]) == 282 and self.fail:
                    raise RuntimeError("HTTP 429")
                return super()._bp_get(path, params)

        good = _Copier()
        rows = good.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        assert rows and "bp:33" not in {r["book"] for r in rows}
        flaky = _Copier()
        flaky.fail = True
        assert flaky.get_odds_by_book(1, line_type="closing", market_type="f5_runline") == []

    def test_the_tie_decides_the_three_way_twin(self) -> None:
        """Review fix: on the first-five moneyline the tie counts. Caesars (captured)
        prices ATL -135 / TB +105 with a tie at +460; its first-inning entry is a
        two-way -125 / -105 with no tie. The team sides sit within the tolerance
        (0.019 / 0.024), but a first-five tie the first-inning entry lacks is not a
        copy: Caesars' row stays."""
        captured = _Fixture99026(game_date="2024-08-14")
        rows = captured.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
        assert [r["book"] for r in rows] == ["bp:0", "bp:10", "bp:13"]
        caesars = _by_book(rows)["bp:13"]
        assert (caesars["home_ml"], caesars["away_ml"], caesars["draw_ml"]) == (-135, 105, 460)
        opening = captured.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline")
        assert [(r["book"], r["draw_ml"]) for r in opening] == [("bp:13", 460)]

        # Synthetic: SD home, BAL away, each book's first-five against its first-inning entry.
        f5 = _offers(
            _sel(
                participant="SD",
                books={
                    24: _line(1, 250, _F5_DAY),
                    19: _line(1, -120, _F5_DAY),
                    13: _line(1, -135, _F5_DAY),
                    10: _line(1, 250, _F5_DAY),
                },
            ),
            _sel(
                participant="BAL",
                books={
                    24: _line(1, 280, _F5_DAY),
                    19: _line(1, 100, _F5_DAY),
                    13: _line(1, 105, _F5_DAY),
                    10: _line(1, 280, _F5_DAY),
                },
            ),
            _sel(
                selection="draw",
                books={24: _line(1, -120, _F5_DAY), 13: _line(1, 460, _F5_DAY)},
            ),
        )
        f1 = _offers(
            _sel(
                participant="SD",
                books={
                    24: _line(1, 255, _F5_DAY),
                    19: _line(1, -125, _F5_DAY),
                    13: _line(1, -125, _F5_DAY),
                    10: _line(1, 250, _F5_DAY),
                },
            ),
            _sel(
                participant="BAL",
                books={
                    24: _line(1, 275, _F5_DAY),
                    19: _line(1, 105, _F5_DAY),
                    13: _line(1, -105, _F5_DAY),
                    10: _line(1, 280, _F5_DAY),
                },
            ),
            _sel(
                selection="draw",
                books={24: _line(1, -125, _F5_DAY), 10: _line(1, -120, _F5_DAY)},
            ),
        )
        provider = _Synthetic({279: f5, 278: f1}, game_date="2024-08-14")
        kept = _by_book(
            provider.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
        )
        # Review fix (2026-09-28): on the moneyline a twin needs IDENTICAL prices.
        # FanDuel (10): a copy that dropped the first-inning tie, every team price
        #   equal: out (the team sides decide).
        # bet365 (24): a near copy, the tie included (-120 against -125): not a twin
        #   at identical prices, so the provider keeps it; its tie (implied 0.55) is
        #   the first-inning shape the guard refuses, so the loader never stores it.
        # BetMGM (19): two two-way entries within 0.03 but not equal: kept. The old
        #   0.03 rule dropped this real near-even line (the census found Caesars
        #   -120 / +100 dropped against its -110 / -110 first-inning line).
        # Caesars (13): a first-five tie the first-inning entry lacks: not a twin, so
        #   the provider keeps it. Its team prices (-135 / +105) add to 1.062: the
        #   tie-refunded two-way pair beside a separate tie, which the guard refuses
        #   (three_way_two_way_team_prices, the book-order read of 2026-09-29).
        assert list(kept) == ["bp:24", "bp:19", "bp:13"]
        assert kept["bp:13"]["draw_ml"] == 460
        refused = check_row(kept["bp:24"])
        assert refused is not None and refused.rule == "f5_tie_price"
        assert check_row(kept["bp:19"]) is None
        refused = check_row(kept["bp:13"])
        assert refused is not None and refused.rule == "three_way_two_way_team_prices"

    def test_the_moneyline_twin_needs_identical_prices(self) -> None:
        """Review fix (2026-09-28, finding F5-1): the two moneylines share the line 1,
        so the line test cannot tell them apart. The live probe dropped Caesars'
        real two-way first-five line on game 634368 (-120 / +100) as a copy of its
        first-inning line (-110 / -110): gaps 0.021 and 0.024, inside the 0.03 run-line
        tolerance. A moneyline twin now needs identical prices; the run line and
        the total keep 0.03."""
        assert twin_price_tolerance(279) == TWIN_MONEYLINE_PRICE_TOLERANCE == 0.0
        assert twin_price_tolerance(283) == twin_price_tolerance(281) == TWIN_PRICE_TOLERANCE

        def moneylines(f5_caesars: tuple[float, float]) -> dict[int, dict[str, Any]]:
            f5 = _offers(
                _sel(
                    participant="SD",
                    books={13: _line(1, f5_caesars[0], _F5_DAY), 19: _line(1, -110, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    books={13: _line(1, f5_caesars[1], _F5_DAY), 19: _line(1, -110, _F5_DAY)},
                ),
            )
            f1 = _offers(
                _sel(participant="SD", books={13: _line(1, -110, _F5_DAY)}),
                _sel(participant="BAL", books={13: _line(1, -110, _F5_DAY)}),
            )
            return {279: f5, 278: f1}

        near = _Synthetic(moneylines((-120, 100)), game_date="2021-04-27")
        rows = near.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
        assert [(r["book"], r["home_ml"], r["away_ml"]) for r in rows] == [
            ("bp:13", -120, 100),
            ("bp:19", -110, -110),
        ]
        copy = _Synthetic(moneylines((-110, -110)), game_date="2021-04-27")
        rows = copy.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
        assert [r["book"] for r in rows] == ["bp:19"]

        # The same gap on the first-five run line is still a twin (0.03 there).
        def runlines(f5_home: float) -> dict[int, dict[str, Any]]:
            f5 = _offers(
                _sel(participant="SD", books={13: _line(-0.5, f5_home, _F5_DAY)}),
                _sel(participant="BAL", books={13: _line(0.5, 100, _F5_DAY)}),
            )
            f1 = _offers(
                _sel(participant="SD", books={13: _line(-0.5, -110, _F5_DAY)}),
                _sel(participant="BAL", books={13: _line(0.5, -110, _F5_DAY)}),
            )
            return {283: f5, 282: f1}

        rl = _Synthetic(runlines(-120), game_date="2021-04-27")
        assert rl.get_odds_by_book(1, line_type="closing", market_type="f5_runline") == []

    def test_the_moneyline_opener_twin_needs_identical_prices(self) -> None:
        opened = "2021-04-26 18:00:00"

        def payloads(f5_open: tuple[float, float]) -> dict[int, dict[str, Any]]:
            f5 = _offers(
                _sel(
                    participant="SD",
                    opener=(13, 1, f5_open[0], opened),
                    books={13: _line(1, -125, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(13, 1, f5_open[1], opened),
                    books={13: _line(1, 105, _F5_DAY)},
                ),
            )
            f1 = _offers(
                _sel(
                    participant="SD",
                    opener=(13, 1, -110, opened),
                    books={13: _line(1, -110, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(13, 1, -110, opened),
                    books={13: _line(1, -110, _F5_DAY)},
                ),
            )
            return {279: f5, 278: f1}

        near = _Synthetic(payloads((-120, 100)), game_date="2021-04-27")
        opening = near.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline")
        assert [(r["book"], r["home_ml"], r["away_ml"]) for r in opening] == [("bp:13", -120, 100)]
        copy = _Synthetic(payloads((-110, -110)), game_date="2021-04-27")
        assert copy.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline") == []

    def test_the_tie_decides_the_three_way_opener_twin(self) -> None:
        """Review fix: the opening row's tie is the tie the same book opened. The team
        openers copy the first-inning openers; a first-five tie from the same opener
        that the first-inning opening row lacks clears the opener."""

        def payloads(f5_tie_opener: int) -> dict[int, dict[str, Any]]:
            opened = "2025-05-31 18:00:00"
            f5 = _offers(
                _sel(
                    participant="SD",
                    opener=(13, 1, 112, opened),
                    books={13: _line(1, -135, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(13, 1, 120, opened),
                    books={13: _line(1, 105, _F5_DAY)},
                ),
                _sel(
                    selection="draw",
                    opener=(f5_tie_opener, 1, 460, opened),
                    books={13: _line(1, 460, _F5_DAY)},
                ),
            )
            f1 = _offers(
                _sel(
                    participant="SD",
                    opener=(13, 1, 112, opened),
                    books={13: _line(1, -125, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(13, 1, 120, opened),
                    books={13: _line(1, -105, _F5_DAY)},
                ),
                _sel(
                    selection="draw",
                    opener=(10, 1, -120, opened),
                    books={10: _line(1, -125, _F5_DAY)},
                ),
            )
            return {279: f5, 278: f1}

        same = _Synthetic(payloads(13), game_date="2024-08-14")
        opening = same.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline")
        assert [(r["book"], r["home_ml"], r["away_ml"], r["draw_ml"]) for r in opening] == [
            ("bp:13", 112, 120, 460)
        ]
        # The tie opened elsewhere: the opening row has no tie, the team openers copy.
        other = _Synthetic(payloads(10), game_date="2024-08-14")
        assert other.get_odds_by_book(1, line_type="opening", market_type="f5_moneyline") == []
        # Caesars' closing row stays in both: its first-five tie has no first-inning match.
        for provider in (same, other):
            closing = provider.get_odds_by_book(1, line_type="closing", market_type="f5_moneyline")
            assert [r["book"] for r in closing] == ["bp:13"]

    def test_an_opener_that_copies_at_close_gives_no_opening_row(self) -> None:
        """Review fix (plan section 5.2: no opening row when "the opener is excluded"):
        bet365 opened the first-five run line at the first-inning "+1.5 -800 / -1.5
        +520" shape, and its closing first-five lines copy its first-inning lines.
        The guard cannot see a +/-1.5 shape, so the provider must drop the opening row."""
        opened = "2025-05-31 18:00:00"

        def payloads(f1_bet365: tuple[float, float]) -> dict[int, dict[str, Any]]:
            f5 = _offers(
                _sel(
                    participant="SD",
                    opener=(24, 1.5, -800, opened),
                    books={24: _line(0.5, -345, _F5_DAY), 19: _line(-0.5, 120, _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(24, -1.5, 520, opened),
                    books={24: _line(0.5, -350, _F5_DAY), 19: _line(0.5, -145, _F5_DAY)},
                ),
            )
            f1 = _offers(
                _sel(
                    participant="SD",
                    opener=(10, 1.5, -790, opened),
                    books={24: _line(0.5, f1_bet365[0], _F5_DAY)},
                ),
                _sel(
                    participant="BAL",
                    opener=(10, -1.5, 520, opened),
                    books={24: _line(0.5, f1_bet365[1], _F5_DAY)},
                ),
            )
            return {283: f5, 282: f1}

        copier = _Synthetic(payloads((-345, -350)), game_date="2025-06-01")
        closing = copier.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        assert [r["book"] for r in closing] == ["bp:19"]
        assert copier.get_odds_by_book(1, line_type="opening", market_type="f5_runline") == []
        opening_default = copier.get_odds(1, line_type="opening", market_type="f5_runline")
        assert opening_default["book"] == "consensus"
        assert opening_default["home_spread"] is None
        # When bet365 is no copier at close, its opening row stands, and the guard
        # passes it: only the provider's rule catches this shape.
        honest = _Synthetic(payloads((-200, 160)), game_date="2025-06-01")
        opening = honest.get_odds_by_book(1, line_type="opening", market_type="f5_runline")
        assert [(r["book"], r["home_spread"], r["home_spread_ml"]) for r in opening] == [
            ("bp:24", 1.5, -800)
        ]
        assert check_row(opening[0]) is None

    def test_each_exclusion_is_logged_once(self, caplog) -> None:
        provider = _Synthetic(_f5_payloads(dk=(-1.5, 280, -1.5, 150)), game_date="2025-06-01")
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            for _ in range(3):
                provider.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        messages = [
            r.getMessage() for r in caplog.records if "first-five exclusion" in r.getMessage()
        ]
        assert sum("bp:12" in m for m in messages) == 1
        assert sum("bp:24" in m for m in messages) == 1

    @pytest.mark.parametrize(
        ("case", "dk_sd", "dk_bal", "opener", "logged"),
        [
            # DraftKings posts nothing on this game's first-five run line.
            ("absent", None, None, 19, False),
            # One side only: DraftKings could not have had a row.
            ("one side", _line(-0.5, 110, _F5_DAY), None, 19, False),
            # A line taken off the board is not a usable line.
            ("off", _line(-0.5, 110, _F5_DAY, is_off=True), _line(0.5, -130, _F5_DAY), 19, False),
            # Every side quoted: the exclusion drops a row, so it is logged.
            ("quoted", _line(-0.5, 110, _F5_DAY), _line(0.5, -130, _F5_DAY), 19, True),
            # The shared opener: the exclusion drops the opening row.
            ("opener", None, None, 12, True),
        ],
    )
    def test_a_dated_exclusion_is_logged_only_when_the_book_had_a_row_to_lose(
        self, caplog, case, dk_sd, dk_bal, opener, logged
    ) -> None:
        """Review item: the dated DraftKings exclusion logged an INFO line on
        every first-five market of every game from 2025-03-01, whether
        DraftKings quoted it or not. The line is now written only when
        DraftKings had a usable line on every side, or opened every side. The
        exclusion itself does not change: DraftKings never has a row."""
        sd_books: dict[int, dict[str, Any]] = {19: _line(-0.5, 120, _F5_DAY)}
        bal_books: dict[int, dict[str, Any]] = {19: _line(0.5, -145, _F5_DAY)}
        if dk_sd is not None:
            sd_books[12] = dk_sd
        if dk_bal is not None:
            bal_books[12] = dk_bal
        offers = _offers(
            _sel(
                participant="SD",
                opener=(opener, -0.5, 120, "2025-05-31 18:00:00"),
                books=sd_books,
            ),
            _sel(
                participant="BAL",
                opener=(opener, 0.5, -145, "2025-05-31 18:00:00"),
                books=bal_books,
            ),
        )
        provider = _Synthetic({283: offers}, game_date="2025-06-01")
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            closing = provider.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
            opening = provider.get_odds_by_book(1, line_type="opening", market_type="f5_runline")
        assert [r["book"] for r in closing] == ["bp:19"], case
        assert [r["book"] for r in opening] == ([] if opener == 12 else ["bp:19"]), case
        dated = [
            r.getMessage()
            for r in caplog.records
            if "first-five exclusion" in r.getMessage() and "bp:12" in r.getMessage()
        ]
        assert len(dated) == (1 if logged else 0), (case, dated)
        if logged:
            assert "excluded from 2025-03-01" in dated[0]
        # Before the date nothing is excluded and nothing is logged.
        caplog.clear()
        before = _Synthetic({283: offers}, game_date="2025-02-28")
        with caplog.at_level(logging.INFO, logger="pipeline.bettingpros_odds_provider"):
            before.get_odds_by_book(1, line_type="closing", market_type="f5_runline")
        assert not any("excluded from" in r.getMessage() for r in caplog.records), case


# ===========================================================================
# Migration 0028
# ===========================================================================


class _RecordingOp:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> None:
        self.sql.append(" ".join(sql.split()))


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0028", _MIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration0028:
    def test_the_chain(self) -> None:
        module = _load_migration()
        assert module.revision == "0028"
        assert module.down_revision == "0027"

    def test_upgrade_adds_the_stamps_the_indexes_and_the_archives(self, monkeypatch) -> None:
        module = _load_migration()
        op = _RecordingOp()
        monkeypatch.setattr(module, "op", op)
        module.upgrade()
        sql = "\n".join(op.sql)
        for table in ("game_odds", "prop_odds"):
            assert (
                f"ALTER TABLE raw.{table} ADD COLUMN IF NOT EXISTS book_line_at TIMESTAMPTZ NULL"
            ) in sql
            assert (
                f"CREATE TABLE IF NOT EXISTS raw.{table}_archive (LIKE raw.{table} INCLUDING DEFAULTS)"
            ) in sql
            assert f"ALTER TABLE raw.{table}_archive ALTER COLUMN id DROP DEFAULT" in sql
        assert (
            "CREATE INDEX IF NOT EXISTS idx_game_odds_market_book "
            "ON raw.game_odds (game_pk, market_type, line_type, book)"
        ) in sql
        assert (
            "CREATE INDEX IF NOT EXISTS idx_prop_odds_market_book "
            "ON raw.prop_odds (game_pk, player_id, prop_stat, line_type, book)"
        ) in sql
        # Each archive drops its id default after it is created.
        create = op.sql.index(
            "CREATE TABLE IF NOT EXISTS raw.game_odds_archive (LIKE raw.game_odds INCLUDING DEFAULTS);"
        )
        drop_default = op.sql.index(
            "ALTER TABLE raw.game_odds_archive ALTER COLUMN id DROP DEFAULT;"
        )
        assert create < drop_default

    def test_downgrade_drops_everything_it_created(self, monkeypatch) -> None:
        module = _load_migration()
        op = _RecordingOp()
        monkeypatch.setattr(module, "op", op)
        module.downgrade()
        sql = "\n".join(op.sql)
        for statement in (
            "DROP TABLE IF EXISTS raw.prop_odds_archive",
            "DROP TABLE IF EXISTS raw.game_odds_archive",
            "DROP INDEX IF EXISTS raw.idx_prop_odds_market_book",
            "DROP INDEX IF EXISTS raw.idx_game_odds_market_book",
            "ALTER TABLE raw.prop_odds DROP COLUMN IF EXISTS book_line_at",
            "ALTER TABLE raw.game_odds DROP COLUMN IF EXISTS book_line_at",
        ):
            assert statement in sql, statement

    def test_the_reference_ddl_carries_the_same_shape(self) -> None:
        ddl = (_ROOT / "db" / "schemas" / "01_postgres_schema.sql").read_text(encoding="utf-8")
        for table in ("game_odds", "prop_odds"):
            block = ddl[ddl.index(f"CREATE TABLE IF NOT EXISTS raw.{table} (") :]
            block = block[: block.index("\n);")]
            assert "book_line_at    TIMESTAMPTZ" in block, table
            assert f"CREATE TABLE IF NOT EXISTS raw.{table}_archive" in ddl
        assert "idx_game_odds_market_book" in ddl and "idx_prop_odds_market_book" in ddl
