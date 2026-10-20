"""The odds book vocabulary: the stored book labels, the kinds, the bettable books and the graded-book order.

The one-row-per-book storage (the one-book-per-row work, SIM-555) names each stored row's book as
``bp:<id>``. These tests pin the vocabulary in ``pipeline/odds_provider.py`` that every writer and
reader shares: the label round trip, each id's kind, which kinds a bettor can take, and the order
in which the accuracy comparison picks its graded book.
"""

from __future__ import annotations

from pipeline.odds_provider import (
    BETTABLE_KINDS,
    BOOK_IDS_BY_NAME,
    BOOK_KIND,
    BOOK_LABEL_PREFIX,
    BOOK_NAMES,
    FULL_GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    LEGACY_GAME_MARKET_TYPES,
    ODDS_ROW_VERSION,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    book_display_name,
    book_id_from_label,
    book_kind,
    book_label,
    graded_book_labels,
    graded_row_order_sql,
    is_bettable,
    non_bettable_labels,
    resolve_book,
)

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
