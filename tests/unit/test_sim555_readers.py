"""
tests/unit/test_sim555_readers.py
=================================
SIM-555, the readers (plan tests 15 and 16;
``docs/audit/2026-09-25-sim555-one-book-per-odds-row-plan.md`` §5.5, §7).

What changed, in plain words: the odds store now holds one row per book,
labelled ``book = 'bp:<id>'``. The accuracy comparison
(``scripts/clv_backtest.py``) grades each market against ONE book: the first
book on the preference list that has a row for the market, the same list on
every game. The vendor's blend, the daily-fantasy apps, the exchanges and the
prediction markets are never graded. Beside the graded row the comparison
keeps every sportsbook's closing row, and each record carries the best price a
bettor could take at the graded line, with its book. The report carries a
stamp (the odds-row version, the preference list, the benchmark book), and the
skill table and the paired read refuse to merge or pair reports whose stamps
differ. Three other scripts read the store with the same rule.

No database. ``_StubPool`` stands in for Postgres: it applies what each SQL
asks for (the label filter, the books to leave out, the preference order,
DISTINCT ON), so a test checks both the SQL and the parameters the reader
passes. The same SQL was run against the live database, read-only, on
synthetic rows when this file was written.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from pipeline.odds_provider import (
    BOOK_KIND,
    BOOK_NAMES,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    STORED_BOOK_FILTER_SQL,
    bettable_labels,
    graded_book_labels,
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    """Import a script by path (scripts/ is not a package). Register it in
    sys.modules first so its slots dataclasses resolve their module."""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load("clv_backtest")
skill = _load("sim548_market_skill")
calib = _load("sim548_calibrate_markets")
pair = _load("sim518_pair_accuracy")
rescore = _load("sim549_rescore_runlines")
manager_probe = _load("sim427_manager_probe")
fatigue_probe = _load("sim518_fatigue_probe")

AccuracyRecord = bt.AccuracyRecord
BETTABLE_CLOSING = bt.BETTABLE_CLOSING

# The book ids the tests use (BettingPros' ids).
BLEND = "bp:0"
FANDUEL = "bp:10"
DRAFTKINGS = "bp:12"
CAESARS = "bp:13"
BETMGM = "bp:19"
BET365 = "bp:24"
UNDERDOG = "bp:36"  # a daily-fantasy app
POLYMARKET = "bp:73"  # a prediction market

# The first four books on the graded-book preference list, by position. A test
# whose point is "the first book on the list with a row is graded" gives its
# books these ROLES, so a reorder of the list never changes what it checks
# (the order changed on 2026-09-29). On that list they are DraftKings, BetMGM,
# FanDuel and theScore.
FIRST_LISTED, SECOND_LISTED, THIRD_LISTED, FOURTH_LISTED = graded_book_labels()[:4]

_GAME_COLUMNS = (
    "home_ml",
    "away_ml",
    "draw_ml",
    "home_spread",
    "home_spread_ml",
    "away_spread",
    "away_spread_ml",
    "total_line",
    "over_ml",
    "under_ml",
)


def _game_row(
    market_type: str,
    book: str,
    *,
    line_type: str = "closing",
    fetched_at: int = 0,
    game_pk: int = 1,
    **cols: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(_GAME_COLUMNS)
    row.update(cols)
    row.update(
        game_pk=game_pk,
        market_type=market_type,
        line_type=line_type,
        book=book,
        fetched_at=fetched_at,
    )
    return row


def _prop_row(
    player_id: int,
    prop_stat: str,
    book: str,
    *,
    line: float | None,
    over_ml: float | None,
    under_ml: float | None,
    line_type: str = "closing",
    fetched_at: int = 0,
    game_pk: int = 1,
) -> dict[str, Any]:
    return {
        "game_pk": game_pk,
        "player_id": player_id,
        "prop_stat": prop_stat,
        "line_type": line_type,
        "book": book,
        "fetched_at": fetched_at,
        "line": line,
        "over_ml": over_ml,
        "under_ml": under_ml,
    }


class _StubPool:
    """A stand-in for the asyncpg pool that applies what a reader's SQL asks for.

    It checks that the SQL carries the stored-label filter, then keeps the
    rows the SQL keeps: the game (``$1``), the line types, the ``bp:``
    labels, the books to keep (``$2`` as a list: the listed sportsbooks) or
    the one benchmark book (``$2`` as a label), and orders by the preference list (``$3``,
    when the SQL names ``array_position``) and then the newest fetch. The
    first row per DISTINCT ON key wins, as in Postgres.
    """

    def __init__(self, game_rows=(), prop_rows=()):
        self.rows = {"raw.game_odds": list(game_rows), "raw.prop_odds": list(prop_rows)}
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        assert STORED_BOOK_FILTER_SQL in sql, "every reader keeps the stored-label filter"
        match = re.search(r"DISTINCT ON \(([^)]*)\)", sql)
        assert match is not None
        keys = [k.strip() for k in match.group(1).split(",")]
        table = "raw.prop_odds" if "raw.prop_odds" in sql else "raw.game_odds"
        rows = [r for r in self.rows[table] if r["game_pk"] == args[0]]
        if "line_type = 'closing'" in sql:
            rows = [r for r in rows if r["line_type"] == "closing"]
        else:
            assert "line_type IN ('opening', 'closing')" in sql
            rows = [r for r in rows if r["line_type"] in ("opening", "closing")]
        rows = [r for r in rows if r["book"].startswith("bp:")]
        assert "NOT (book = ANY(" not in sql, "a reader keeps a list of books, never a blacklist"
        if "book = ANY($2::varchar[])" in sql:
            rows = [r for r in rows if r["book"] in args[1]]
        elif "book = $2" in sql:
            rows = [r for r in rows if r["book"] == args[1]]
        else:
            raise AssertionError(f"no book filter in {sql!r}")
        pref = list(args[2]) if "array_position($3::varchar[], book)" in sql else []
        # a graded game-market read sorts a tie-less three-way row after the
        # complete ones, BEFORE the preference list
        tie_last = bt.INCOMPLETE_THREE_WAY_LAST_SQL in sql
        if tie_last and pref:
            assert sql.index(bt.INCOMPLETE_THREE_WAY_LAST_SQL) < sql.index("array_position(")

        def order(r):
            rank = pref.index(r["book"]) if r["book"] in pref else len(pref)
            incomplete = (
                tie_last
                and r.get("draw_ml") is None
                and r.get("market_type") in bt.THREE_WAY_MARKET_TYPES
            )
            return (incomplete, rank, -r["fetched_at"])

        first: dict[tuple, dict[str, Any]] = {}
        for r in sorted(rows, key=order):
            first.setdefault(tuple(r[k] for k in keys), r)
        return list(first.values())


def _four_book_game() -> list[dict[str, Any]]:
    """One game's rows from four sportsbooks and two non-sportsbooks.

    The sportsbooks play roles on the preference list. The first book on the
    list quotes nothing, so the second is the graded book, though the third
    fetched later. The third pays the most on the home side. The blend and the
    prediction market price the home side best of all; neither is a price a
    bettor can take at a sportsbook.
    """
    return [
        # the moneyline: the second book graded; the third pays the most on the home side
        _game_row("moneyline", BLEND, fetched_at=9, home_ml=-110, away_ml=-110),
        _game_row("moneyline", POLYMARKET, fetched_at=9, home_ml=-115, away_ml=105),
        _game_row("moneyline", THIRD_LISTED, fetched_at=5, home_ml=-140, away_ml=120),
        _game_row("moneyline", SECOND_LISTED, fetched_at=4, home_ml=-145, away_ml=125),
        # an older fetch of the graded book
        _game_row("moneyline", SECOND_LISTED, fetched_at=1, home_ml=-999, away_ml=999),
        _game_row("moneyline", SECOND_LISTED, line_type="opening", home_ml=-150, away_ml=130),
        # the old mixed row: never read
        _game_row("moneyline", "consensus", fetched_at=99, home_ml=-100, away_ml=-100),
        # the total: the fourth book pays more on the over, but at another line
        _game_row("total", SECOND_LISTED, total_line=8.5, over_ml=-110, under_ml=-110),
        _game_row("total", THIRD_LISTED, total_line=8.5, over_ml=-105, under_ml=-115),
        _game_row("total", FOURTH_LISTED, total_line=9.0, over_ml=100, under_ml=-120),
        _game_row("total", BLEND, total_line=8.5, over_ml=105, under_ml=-125),
    ]


def _moneyline_record() -> Any:
    return AccuracyRecord(
        game_pk=1,
        market="moneyline",
        market_type="moneyline",
        sim_prob=0.61,
        market_prob=0.58,
        outcome=1,
        market_side_price=-145.0,
        market_other_price=125.0,
    )


def _total_record() -> Any:
    return AccuracyRecord(
        game_pk=1,
        market="total",
        market_type="total",
        sim_prob=0.47,
        market_prob=0.50,
        outcome=0,
        market_side_price=-110.0,
        market_other_price=-110.0,
    )


# ---------------------------------------------------------------------------
# Plan test 15: the graded-row read and the best bettable price
# ---------------------------------------------------------------------------


async def test_the_graded_row_read():
    """Rows from four books: the graded row is the first preferred book's,
    the blend and the prediction market are skipped, and each record reports
    the best bettable price at the graded line with its book."""
    pool = _StubPool(game_rows=_four_book_game())
    odds = await bt._fetch_game_odds(pool, 1)

    closing = odds["moneyline"]["closing"]
    # the first book on the list with a row, over the third's newer fetch
    assert closing["book"] == SECOND_LISTED
    assert (closing["home_ml"], closing["away_ml"]) == (-145, 125)  # the graded book's newest fetch
    assert odds["moneyline"]["opening"]["book"] == SECOND_LISTED
    assert odds["total"]["closing"]["book"] == SECOND_LISTED
    # the sportsbooks' closing rows ride beside the graded row; the blend,
    # the prediction market and the old mixed row are not among them
    assert {r["book"] for r in odds["moneyline"][BETTABLE_CLOSING]} == {
        SECOND_LISTED,
        THIRD_LISTED,
    }
    assert {r["book"] for r in odds["total"][BETTABLE_CLOSING]} == {
        SECOND_LISTED,
        THIRD_LISTED,
        FOURTH_LISTED,
    }

    ml, total = bt.attach_best_prices([_moneyline_record(), _total_record()], odds, {})
    # the best price is another sportsbook's, not the graded row's
    assert (ml.market_best_price, ml.market_best_book) == (-140.0, THIRD_LISTED)
    # the fourth book's +100 is an over 9, not an over 8.5: another bet
    assert (total.market_best_price, total.market_best_book) == (-105.0, THIRD_LISTED)
    # the scores do not move
    assert ml.sim_prob == 0.61 and ml.market_prob == 0.58 and ml.market_side_price == -145.0
    assert total.market_prob == 0.50 and total.outcome == 0

    # two reads: the graded rows, then every sportsbook's closing rows
    assert len(pool.calls) == 2
    graded_sql, graded_args = pool.calls[0]
    assert graded_args == (1, bettable_labels(), graded_book_labels())
    assert "array_position($3::varchar[], book) NULLS LAST, fetched_at DESC" in graded_sql
    assert pool.calls[1][1] == (1, bettable_labels())


async def test_the_first_book_on_the_list_wins_over_a_newer_fetch():
    rows = _four_book_game() + [
        _game_row("moneyline", FIRST_LISTED, fetched_at=0, home_ml=-150, away_ml=130),
    ]
    odds = await bt.fetch_graded_game_odds(_StubPool(game_rows=rows), 1)
    assert odds["moneyline"]["closing"]["book"] == FIRST_LISTED
    # a market the first book does not quote still falls to the second
    assert odds["total"]["closing"]["book"] == SECOND_LISTED


async def test_a_book_off_the_list_is_graded_only_when_no_listed_book_quotes(monkeypatch):
    # A sportsbook off the preference list, classified for the test: since
    # 2026-09-29 every sportsbook the vocabulary lists is on the list.
    monkeypatch.setitem(BOOK_KIND, 77, "sportsbook")
    off_list = "bp:77"
    assert off_list in bettable_labels() and off_list not in graded_book_labels()
    rows = [_game_row("first_to_score", off_list, fetched_at=9, home_ml=-115, away_ml=-105)]
    odds = await bt.fetch_graded_game_odds(_StubPool(game_rows=rows), 1)
    assert odds["first_to_score"]["closing"]["book"] == off_list
    # the last book on the list still wins over its newer fetch
    last_listed = graded_book_labels()[-1]
    rows.append(_game_row("first_to_score", last_listed, fetched_at=0, home_ml=-110, away_ml=-110))
    odds = await bt.fetch_graded_game_odds(_StubPool(game_rows=rows), 1)
    assert odds["first_to_score"]["closing"]["book"] == last_listed


async def test_a_book_the_vocabulary_does_not_list_is_never_graded_or_offered():
    """Review fix (2026-09-28, finding VOCAB-1): the readers keep a list of
    what may be graded (the listed sportsbooks). An unlisted id, a prediction
    market (DraftKings Predictions, 74) or a pick'em app (Betr, 45) used to
    pass a blacklist and could be graded or named the best price."""
    rows = [
        _game_row("first_to_score", "bp:999", home_ml=-115, away_ml=-105),
        _game_row("first_to_score", "bp:74", home_ml=-110, away_ml=-110),
        _game_row("first_to_score", "bp:45", home_ml=-105, away_ml=-115),
    ]
    odds = await bt.fetch_graded_game_odds(_StubPool(game_rows=rows), 1)
    assert "first_to_score" not in odds
    closes = await bt.fetch_bettable_game_closes(_StubPool(game_rows=rows), 1)
    assert closes == {}
    for label in ("bp:999", "bp:74", "bp:45"):
        assert label not in bettable_labels()
    assert bt.best_bettable_price(rows, "home_ml") is None


async def test_a_game_with_only_the_old_rows_reads_as_no_odds():
    """A half-loaded season never mixes: a game whose rows are all the old
    ``consensus`` rows has no graded row, and the second read never runs."""
    pool = _StubPool(
        game_rows=[_game_row("moneyline", "consensus", home_ml=-120, away_ml=100)],
        prop_rows=[_prop_row(7, "strikeouts", "consensus", line=5.5, over_ml=-110, under_ml=-110)],
    )
    assert await bt._fetch_game_odds(pool, 1) == {}
    assert await bt._fetch_prop_odds(pool, 1) == {}
    assert len(pool.calls) == 2  # one graded read per table, no bettable read


async def test_the_benchmark_book_grades_against_that_one_book():
    pool = _StubPool(game_rows=_four_book_game())
    odds = await bt._fetch_game_odds(pool, 1, benchmark_book=BLEND)
    assert odds["moneyline"]["closing"]["book"] == BLEND
    assert odds["total"]["closing"]["book"] == BLEND
    sql, args = pool.calls[0]
    assert "AND book = $2" in sql and "array_position" not in sql
    assert args == (1, BLEND)
    # the best price is still a sportsbook's, at the blend's line
    rec = AccuracyRecord(
        game_pk=1,
        market="moneyline",
        market_type="moneyline",
        sim_prob=0.6,
        market_prob=0.5,
        outcome=1,
    )
    (out,) = bt.attach_best_prices([rec], odds, {})
    assert (out.market_best_price, out.market_best_book) == (-140.0, THIRD_LISTED)


async def test_the_prop_graded_row_and_best_price():
    """The strikeout prop: DraftKings is graded; FanDuel pays more on the over
    at the same line; BetMGM's over is another line; the blend and a
    daily-fantasy app are skipped."""
    rows = [
        _prop_row(7, "strikeouts", DRAFTKINGS, line=5.5, over_ml=-120, under_ml=-110),
        _prop_row(7, "strikeouts", FANDUEL, line=5.5, over_ml=-115, under_ml=-115),
        _prop_row(7, "strikeouts", BETMGM, line=6.5, over_ml=110, under_ml=-140),
        _prop_row(7, "strikeouts", BLEND, line=5.5, over_ml=100, under_ml=-130),
        _prop_row(7, "strikeouts", UNDERDOG, line=5.5, over_ml=-100, under_ml=-100),
        _prop_row(
            7, "strikeouts", FANDUEL, line_type="opening", line=5.5, over_ml=-125, under_ml=105
        ),
    ]
    pool = _StubPool(prop_rows=rows)
    odds = await bt._fetch_prop_odds(pool, 1)
    assert odds[(7, "strikeouts")]["closing"]["book"] == DRAFTKINGS
    assert odds[(7, "strikeouts")]["opening"]["book"] == FANDUEL
    rec = AccuracyRecord(
        game_pk=1,
        market="K",
        market_type="prop",
        sim_prob=0.55,
        market_prob=0.52,
        outcome=1,
        player_id=7,
        market_side_price=-120.0,
        market_other_price=-110.0,
    )
    (out,) = bt.attach_best_prices([rec], {}, odds)
    assert (out.market_best_price, out.market_best_book) == (-115.0, FANDUEL)


def test_a_tie_in_payout_goes_to_the_book_earlier_on_the_list():
    rows = [
        {"book": FANDUEL, "home_ml": -120},
        {"book": DRAFTKINGS, "home_ml": -120},
        {"book": "bp:999", "home_ml": -120},
    ]
    assert bt.best_bettable_price(rows, "home_ml") == (-120.0, DRAFTKINGS)
    # -100 and +100 pay the same; the list decides
    rows = [{"book": CAESARS, "home_ml": 100}, {"book": BETMGM, "home_ml": -100}]
    assert bt.best_bettable_price(rows, "home_ml") == (-100.0, BETMGM)
    # no sportsbook quotes the side: no price
    assert bt.best_bettable_price([{"book": BLEND, "home_ml": 150}], "home_ml") is None


def test_the_run_line_away_bet_is_priced_at_the_away_spread():
    """Two separate bets (home -1.5 and away -1.5): the home record takes the
    best home price at -1.5; the away record, the best away price at -1.5.
    A book whose home side sits at another spread offers another home bet."""
    graded = {
        "book": FANDUEL,
        "home_spread": -1.5,
        "home_spread_ml": 350,
        "away_spread": -1.5,
        "away_spread_ml": 150,
    }
    others = [
        graded,
        {
            "book": BETMGM,
            "home_spread": -1.5,
            "home_spread_ml": 360,
            "away_spread": -1.5,
            "away_spread_ml": 140,
        },
        {
            "book": CAESARS,
            "home_spread": -2.5,
            "home_spread_ml": 600,
            "away_spread": 2.5,
            "away_spread_ml": -900,
        },
    ]
    odds = {"f5_runline": {"closing": graded, BETTABLE_CLOSING: others}}
    home = AccuracyRecord(1, "f5_runline", "f5_runline", 0.2, 0.2, 0, market_side_price=350.0)
    away = AccuracyRecord(1, "f5_runline_away", "f5_runline", 0.3, 0.3, 1, market_side_price=150.0)
    home_out, away_out = bt.attach_best_prices([home, away], odds, {})
    assert (home_out.market_best_price, home_out.market_best_book) == (360.0, BETMGM)
    assert (away_out.market_best_price, away_out.market_best_book) == (150.0, FANDUEL)


def test_a_three_way_candidate_must_list_the_tie():
    """A home price with no tie on offer may be another bet (the tie
    refunded), so it is not a candidate on a three-way record."""
    graded = {"book": DRAFTKINGS, "home_ml": 150, "away_ml": 170, "draw_ml": 400}
    others = [
        graded,
        {"book": BETMGM, "home_ml": 190, "away_ml": 150, "draw_ml": None},
        {"book": CAESARS, "home_ml": 160, "away_ml": 165, "draw_ml": 380},
    ]
    odds = {"f5_moneyline": {"closing": graded, BETTABLE_CLOSING: others}}
    rec = AccuracyRecord(1, "f5_moneyline", "f5_moneyline", 0.4, 0.38, 1)
    (out,) = bt.attach_best_prices([rec], odds, {})
    assert (out.market_best_price, out.market_best_book) == (160.0, CAESARS)


def _implied(american: float) -> float:
    from betting.clv_engine import implied_prob_from_american

    return float(implied_prob_from_american(float(american)))


def test_the_three_way_markets_are_the_two_segment_moneylines():
    from pipeline.odds_provider import GAME_MARKET_KIND

    assert set(bt.THREE_WAY_MARKET_TYPES) == {"f1_moneyline", "f5_moneyline"}
    assert {m for m, k in GAME_MARKET_KIND.items() if k == "three_way"} == set(
        bt.THREE_WAY_MARKET_TYPES
    )
    assert bt.INCOMPLETE_THREE_WAY_LAST_SQL == (
        "(draw_ml IS NULL AND market_type IN ('f1_moneyline', 'f5_moneyline'))"
    )
    for sql in (bt.graded_game_odds_sql(), bt.graded_game_odds_sql(benchmark=True)):
        assert bt.INCOMPLETE_THREE_WAY_LAST_SQL in sql


async def test_a_three_way_row_with_no_tie_is_graded_only_when_no_book_lists_the_tie():
    """Review finding 1: the first book on the list lists the first-inning
    moneyline's two teams only (as DraftKings does: a tie refunds the bet);
    the second and third books list all three. The scorer cannot price a
    three-way market without the tie, so the second book's complete row is
    the graded row, and the market is scored. A market no book lists the tie
    on keeps its tie-less row. A two-way market is not touched: its
    ``draw_ml`` is always empty."""
    from simulation.game_market_distributions import SegmentRuns

    rows = [
        _game_row("f1_moneyline", FIRST_LISTED, fetched_at=5, home_ml=-120, away_ml=100),
        _game_row(
            "f1_moneyline", SECOND_LISTED, fetched_at=0, home_ml=150, away_ml=190, draw_ml=-110
        ),
        _game_row(
            "f1_moneyline", THIRD_LISTED, fetched_at=9, home_ml=160, away_ml=180, draw_ml=-115
        ),
        _game_row("f5_moneyline", FIRST_LISTED, home_ml=-110, away_ml=-110),
        _game_row("moneyline", FIRST_LISTED, home_ml=-120, away_ml=100),
        _game_row("moneyline", SECOND_LISTED, fetched_at=9, home_ml=-125, away_ml=105),
    ]
    pool = _StubPool(game_rows=rows)
    odds = await bt._fetch_game_odds(pool, 1)
    f1 = odds["f1_moneyline"]["closing"]
    # the first book on the list with a complete row (not the third book's newer one)
    assert (f1["book"], f1["home_ml"], f1["draw_ml"]) == (SECOND_LISTED, 150, -110)
    assert odds["f5_moneyline"]["closing"]["book"] == FIRST_LISTED
    assert odds["f5_moneyline"]["closing"]["draw_ml"] is None
    assert odds["moneyline"]["closing"]["book"] == FIRST_LISTED

    grids = [([0, 1, 0, 2, 0, 0, 1, 1, 0], [1, 0, 1, 0, 0, 0, 0, 1, 0]), ([0] * 9, [0] * 9)]
    official = {"home": [0, 2, 0, 1, 0, 0, 0, 0, 0], "away": [0, 0, 0, 0, 0, 0, 0, 0, 1]}
    recs = bt.score_segment_market_accuracy(1, SegmentRuns.from_inning_grids(grids), odds, official)
    (f1_rec,) = [r for r in recs if r.market == "f1_moneyline"]
    fair = [_implied(150), _implied(190), _implied(-110)]
    assert f1_rec.market_side_price == 150.0
    assert f1_rec.market_prob == pytest.approx(fair[0] / sum(fair))
    # the tie-less first-five row gives no record, as before
    assert not [r for r in recs if r.market == "f5_moneyline"]

    # the benchmark read keeps the rule inside its one book: an older
    # complete row before a newer tie-less one
    bench_rows = [
        _game_row("f1_moneyline", BLEND, fetched_at=9, home_ml=-120, away_ml=100),
        _game_row("f1_moneyline", BLEND, fetched_at=1, home_ml=155, away_ml=185, draw_ml=-112),
    ]
    odds = await bt.fetch_graded_game_odds(_StubPool(game_rows=bench_rows), 1, benchmark_book=BLEND)
    assert odds["f1_moneyline"]["closing"]["draw_ml"] == -112


def _two_bets_game() -> dict[str, Any]:
    """Review finding 2's game: the graded first-five run line is BetMGM's,
    listed as two separate bets (-1.5 +250 / -1.5 +400). The graded
    first-five total is FanDuel's at -125 / -125 (margin 1.111); BetMGM's own
    is -104 / -104 (margin 1.0196). BetMGM also posts a full-game moneyline."""
    rl = {
        "book": BETMGM,
        "home_spread": -1.5,
        "home_spread_ml": 250,
        "away_spread": -1.5,
        "away_spread_ml": 400,
    }
    fd_total = {"book": FANDUEL, "total_line": 4.5, "over_ml": -125, "under_ml": -125}
    mgm_total = {"book": BETMGM, "total_line": 4.5, "over_ml": -104, "under_ml": -104}
    fd_ml = {"book": FANDUEL, "home_ml": -110, "away_ml": -110}
    mgm_ml = {"book": BETMGM, "home_ml": -120, "away_ml": 102}
    return {
        "f5_runline": {"closing": rl, BETTABLE_CLOSING: [rl]},
        "f5_total": {"closing": fd_total, BETTABLE_CLOSING: [fd_total, mgm_total]},
        "moneyline": {"closing": fd_ml, BETTABLE_CLOSING: [fd_ml, mgm_ml]},
    }


def test_a_run_line_listed_as_two_bets_takes_its_own_books_margin():
    """Review finding 2: each bet is priced over the margin of the run-line
    row's OWN book, never over another book's graded total."""
    odds = _two_bets_game()
    own = 2 * _implied(-104)
    assert bt.reference_margin(odds, "f5", book=BETMGM) == (pytest.approx(own), "f5_total")
    # no book given (a row with no label): the graded rows, as before SIM-555
    assert bt.reference_margin(odds, "f5")[0] == pytest.approx(2 * _implied(-125))

    cp = bt._closing_prices(
        odds, "f5_runline", "home_spread_ml", "away_spread_ml", "home_spread", "away_spread"
    )
    recs = bt.score_run_line_market(
        1, "f5_runline", cp, odds, sim_cover=lambda _s, _l: 0.3, real_cover=lambda _s, _l: 0
    )
    by_market = {r.market: r for r in recs}
    assert by_market["f5_runline"].market_prob == pytest.approx(_implied(250) / own)
    assert by_market["f5_runline_away"].market_prob == pytest.approx(_implied(400) / own)
    # the reviewer's numbers: 0.2802 and 0.196, not 0.2571 and 0.18
    assert round(by_market["f5_runline"].market_prob, 4) == 0.2802
    assert round(by_market["f5_runline_away"].market_prob, 3) == 0.196


def test_the_own_book_margin_falls_to_its_moneyline_then_the_flat_margin():
    """Without its own first-five total, BetMGM's full-game moneyline gives
    the margin. Without that either, the flat 1.05, although FanDuel's graded
    total sits inside the band: the live /edges page does the same."""
    odds = _two_bets_game()
    odds["f5_total"][BETTABLE_CLOSING] = [odds["f5_total"]["closing"]]
    margin, source = bt.reference_margin(odds, "f5", book=BETMGM)
    assert source == "moneyline"
    assert margin == pytest.approx(_implied(-120) + _implied(102))
    odds["moneyline"][BETTABLE_CLOSING] = [odds["moneyline"]["closing"]]
    assert bt.reference_margin(odds, "f5", book=BETMGM) == (bt.DEFAULT_ONE_SIDED_MARGIN, "flat")
    # its own total out of the band is skipped, as a graded one was
    odds = _two_bets_game()
    odds["f5_total"][BETTABLE_CLOSING][1] = {
        "book": BETMGM,
        "total_line": 4.5,
        "over_ml": -300,
        "under_ml": -300,
    }
    assert bt.reference_margin(odds, "f5", book=BETMGM)[1] == "moneyline"


def test_the_benchmark_books_margin_is_its_own_graded_row():
    """A benchmark read grades the blend, which is not among the sportsbooks'
    rows: its own graded total gives the margin."""
    blend_total = {"book": BLEND, "total_line": 4.5, "over_ml": -108, "under_ml": -112}
    fd_total = {"book": FANDUEL, "total_line": 4.5, "over_ml": -125, "under_ml": -125}
    odds = {"f5_total": {"closing": blend_total, BETTABLE_CLOSING: [fd_total]}}
    assert bt.reference_margin(odds, "f5", book=BLEND) == (
        pytest.approx(_implied(-108) + _implied(-112)),
        "f5_total",
    )


def test_the_run_line_rescore_prices_over_the_run_line_books_margin():
    """The SIM-549 re-score reads the margin as the backtest does. The
    report's own first-five total record is FanDuel's graded row, another
    book's, so it does not check the margin; with FanDuel's run line it does."""
    odds = _two_bets_game()
    report = {
        "params": {"base_seed": 0, "bootstrap_samples": 20, "bootstrap_seed": 1},
        "accuracy_records": [
            {
                "game_pk": 2,
                "market": "f5_runline",
                "market_type": "f5_runline",
                "sim_prob": 0.2,
                "market_prob": 0.3,
                "outcome": 0,
                "player_id": None,
                "market_side_price": 250.0,
                "market_other_price": 400.0,
            },
            {
                "game_pk": 2,
                "market": "f5_total",
                "market_type": "f5_total",
                "sim_prob": 0.5,
                "market_prob": 0.5,
                "outcome": 1,
                "player_id": None,
                "market_side_price": -125.0,
                "market_other_price": -125.0,
            },
        ],
    }
    out, summary = rescore.rescore_report(report, {2: odds}, source="r.json", date="2026-09-28")
    (rl,) = [r for r in out["accuracy_records"] if r["market"] == "f5_runline"]
    assert rl["market_prob"] == pytest.approx(_implied(250) / (2 * _implied(-104)))
    assert summary["reference_margin_sources"] == {"f5_total": 1}
    assert "margin_checked_against_the_report" not in summary["tally"]
    # FanDuel's own run line: its own total is the graded row the report priced
    fd_rl = dict(odds["f5_runline"]["closing"], book=FANDUEL)
    odds["f5_runline"] = {"closing": fd_rl, BETTABLE_CLOSING: [fd_rl]}
    out, summary = rescore.rescore_report(report, {2: odds}, source="r.json", date="2026-09-28")
    (rl,) = [r for r in out["accuracy_records"] if r["market"] == "f5_runline"]
    assert rl["market_prob"] == pytest.approx(_implied(250) / (2 * _implied(-125)))
    assert summary["tally"]["margin_checked_against_the_report"] == 1
    assert "margin_differs_from_the_report" not in summary["tally"]


def test_a_record_without_the_other_books_keeps_no_best_price_but_its_graded_one():
    """A hand-built odds dict with no bettable rows: the graded row itself is
    the one candidate when its book is a sportsbook; the blend is not."""
    rec = _moneyline_record()
    odds = {"moneyline": {"closing": {"book": FANDUEL, "home_ml": -145, "away_ml": 125}}}
    (out,) = bt.attach_best_prices([rec], odds, {})
    assert (out.market_best_price, out.market_best_book) == (-145.0, FANDUEL)
    odds = {"moneyline": {"closing": {"book": BLEND, "home_ml": -145, "away_ml": 125}}}
    (out,) = bt.attach_best_prices([rec], odds, {})
    assert out.market_best_price is None and out.market_best_book is None
    # no graded row for the market: the record is returned as it came
    (out,) = bt.attach_best_prices([rec], {}, {})
    assert out == rec


def test_an_older_record_loads_without_the_two_new_fields():
    d = _moneyline_record().to_jsonable()
    assert d["market_best_price"] is None and d["market_best_book"] is None
    d.pop("market_best_price")
    d.pop("market_best_book")
    rec = AccuracyRecord.from_jsonable(d)
    assert rec.market_best_price is None and rec.market_best_book is None


async def test_score_one_game_puts_the_best_price_on_each_record(monkeypatch):
    """End to end through the per-game pipeline: the prop record carries the
    best price the reader found, and the benchmark book reaches the reader."""
    import simulation.prop_distributions as pd_mod
    import simulation.results as results_mod
    import simulation.sim_kwargs as sk
    import simulation.win_probability as wp_mod
    from simulation.prop_distributions import PropDistribution, PropDistributionSet

    fake_games = types.ModuleType("api.routes.games")

    async def _resolve_state(pool, game_pk):
        return types.SimpleNamespace(asof_ymd=None)

    fake_games._resolve_state_or_error = _resolve_state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "api.routes.games", fake_games)

    async def _park(state, pool, duck, game_pk):
        return 1.0

    async def _asof(pool, game_pk):
        return "2024-08-14"

    seen: dict[str, Any] = {}
    prop_odds = {
        (7, "strikeouts"): {
            "closing": {"book": DRAFTKINGS, "line": 5.5, "over_ml": -120, "under_ml": -110},
            BETTABLE_CLOSING: [
                {"book": DRAFTKINGS, "line": 5.5, "over_ml": -120, "under_ml": -110},
                {"book": FANDUEL, "line": 5.5, "over_ml": -112, "under_ml": -118},
            ],
        }
    }

    async def _prop_odds(pool, game_pk, *, benchmark_book=None):
        seen["benchmark_book"] = benchmark_book
        return prop_odds

    async def _truth(pool, game_pk):
        return bt.PropGroundTruth(
            batter_actuals={},
            pitcher_actuals={7: {"K": 7}},
            source=bt.GROUND_TRUTH_OFFICIAL,
            scorable_props=bt.BOXSCORE_SCORED_PROPS,
        )

    pset = PropDistributionSet(
        n_iterations=10,
        by_player={
            7: {
                "K": PropDistribution.from_samples(
                    player_id=7, prop="K", samples=[3, 4, 5, 5, 6, 6, 7, 7, 8, 9]
                )
            }
        },
    )
    monkeypatch.setattr(sk, "resolve_park_factor_onto_state", _park)
    monkeypatch.setattr(sk, "resolve_asof_ymd", _asof)
    monkeypatch.setattr(bt, "_fetch_prop_odds", _prop_odds)
    monkeypatch.setattr(bt, "_fetch_prop_ground_truth", _truth)
    monkeypatch.setattr(bt, "_replay_game", lambda *_a, **_k: ([object()], []))
    monkeypatch.setattr(
        results_mod.GameSimSummary, "from_results", classmethod(lambda cls, r: object())
    )
    monkeypatch.setattr(wp_mod, "win_probability", lambda summary, calibration_map=None: object())
    monkeypatch.setattr(
        pd_mod.PropDistributionSet, "from_results", classmethod(lambda cls, r: pset)
    )
    recs, status, _pf, _src = await bt._score_one_game(
        object(),
        1,
        duck=object(),
        do_game=False,
        do_props=True,
        iterations=1,
        base_seed=0,
        benchmark_book=BLEND,
    )
    assert status == "scored"
    assert seen["benchmark_book"] == BLEND
    (rec,) = recs
    assert rec.market == "K" and rec.market_side_price == -120.0  # scored on the graded row
    assert (rec.market_best_price, rec.market_best_book) == (-112.0, FANDUEL)


def test_the_worker_passes_the_benchmark_book_on(monkeypatch):
    monkeypatch.setattr(bt, "_worker_lazy_init", lambda *_a, **_k: None)
    seen: dict[str, Any] = {}

    async def _score(pool, game_pk, **kw):
        seen.update(kw)
        return [], "scored", 1.0, None

    class _Loop:
        def run_until_complete(self, coro):
            return asyncio.new_event_loop().run_until_complete(coro)

    monkeypatch.setattr(bt, "_score_one_game", _score)
    monkeypatch.setattr(bt, "_WORKER_LOOP", _Loop())
    params = {"do_game": True, "do_props": False, "iterations": 1, "base_seed": 0}
    bt._process_one_game(1, {**params, "benchmark_book": BLEND})
    assert seen["benchmark_book"] == BLEND
    bt._process_one_game(1, params)  # an older caller's params: the preference list
    assert seen["benchmark_book"] is None


# ---------------------------------------------------------------------------
# Plan test 16: every reader filters to the label; the stamps keep reads apart
# ---------------------------------------------------------------------------


def test_every_reader_sql_filters_to_the_label():
    graded = [
        bt.graded_game_odds_sql(),
        bt.graded_prop_odds_sql(),
        rescore.CLOSING_ROWS_SQL,
        manager_probe.K_LINES_SQL,
        fatigue_probe.K_LINES_SQL,
    ]
    others = [
        bt.graded_game_odds_sql(benchmark=True),
        bt.graded_prop_odds_sql(benchmark=True),
        bt.BETTABLE_GAME_CLOSES_SQL,
        bt.BETTABLE_PROP_CLOSES_SQL,
        rescore.BOOK_CLOSES_SQL,
    ]
    for sql in graded + others:
        assert STORED_BOOK_FILTER_SQL in sql
    for sql in graded:
        # only the listed sportsbooks are read, and the preference list orders
        assert "AND book = ANY($" in sql and "NOT (book = ANY(" not in sql
        assert "array_position($" in sql and "NULLS LAST, fetched_at DESC" in sql
    for sql in (bt.BETTABLE_GAME_CLOSES_SQL, bt.BETTABLE_PROP_CLOSES_SQL):
        assert "AND book = ANY($2::varchar[])" in sql and "line_type = 'closing'" in sql
    assert "AND book = ANY($3::varchar[])" in rescore.BOOK_CLOSES_SQL
    assert "line_type = 'closing'" in rescore.BOOK_CLOSES_SQL


class _Conn:
    """A stand-in for one asyncpg connection: records the query, returns rows
    (``rows_by_sql`` picks the rows by the query, for a reader with two)."""

    def __init__(self, rows=(), rows_by_sql=None):
        self.rows = list(rows)
        self.rows_by_sql = dict(rows_by_sql or {})
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if sql in self.rows_by_sql:
            return list(self.rows_by_sql[sql])
        return list(self.rows)

    async def close(self):
        return None


@pytest.mark.parametrize("probe", [manager_probe, fatigue_probe], ids=["sim427", "sim518"])
async def test_the_probes_read_the_graded_strikeout_line(monkeypatch, probe):
    import asyncpg

    conn = _Conn(rows=[{"game_pk": 1, "player_id": 9, "line": 5.5}])

    async def _connect(*_a, **_k):
        return conn

    monkeypatch.setattr(asyncpg, "connect", _connect)
    lines = await probe._k_lines([1, 2])
    assert lines == {(1, 9): 5.5}
    ((sql, args),) = conn.calls
    assert sql == probe.K_LINES_SQL
    assert args == ([1, 2], bettable_labels(), graded_book_labels())


async def test_the_run_line_rescore_reads_the_graded_row(monkeypatch):
    import asyncpg

    def total(book: str, over: float) -> dict[str, Any]:
        return {
            "game_pk": 2,
            "market_type": "f5_total",
            "book": book,
            "home_ml": None,
            "away_ml": None,
            "total_line": 4.5,
            "over_ml": over,
            "under_ml": over,
        }

    conn = _Conn(
        rows_by_sql={
            rescore.BOOK_CLOSES_SQL: [total(FANDUEL, -125), total(BETMGM, -104)],
        },
        rows=[
            {
                "game_pk": 2,
                "market_type": "f5_runline",
                "book": BETMGM,
                "home_ml": None,
                "away_ml": None,
                "draw_ml": None,
                "home_spread": -1.5,
                "home_spread_ml": 350,
                "away_spread": -1.5,
                "away_spread_ml": 150,
                "total_line": None,
                "over_ml": None,
                "under_ml": None,
            }
        ],
    )

    async def _connect(*_a, **_k):
        return conn

    monkeypatch.setattr(asyncpg, "connect", _connect)
    out = await rescore.fetch_closing_rows("postgresql://x/y", {2})
    row = out[2]["f5_runline"]["closing"]
    assert row["book"] == BETMGM
    assert row["home_spread_ml"] == 350.0 and row["away_spread"] == -1.5
    # every sportsbook's first-five total rides beside, for the run-line
    # book's own margin
    books = out[2]["f5_total"][BETTABLE_CLOSING]
    assert [(r["book"], r["over_ml"]) for r in books] == [(FANDUEL, -125.0), (BETMGM, -104.0)]
    assert bt.reference_margin(out[2], "f5", book=BETMGM) == (
        pytest.approx(2 * _implied(-104)),
        "f5_total",
    )
    (graded_sql, graded_args), (books_sql, books_args) = conn.calls
    assert graded_sql == rescore.CLOSING_ROWS_SQL
    assert graded_args[0] == [2]
    assert graded_args[2:] == (bettable_labels(), graded_book_labels())
    assert books_sql == rescore.BOOK_CLOSES_SQL
    assert books_args == (
        [2],
        ["total", "f5_total", "f1_total", "moneyline"],
        bettable_labels(),
    )


async def _run_empty_slate(monkeypatch, tmp_path, *extra: str) -> dict[str, Any]:
    import simulation.sim_kwargs as sk

    class _NoDuck:
        def close(self):
            return None

    monkeypatch.setattr(sk, "open_sim_duckdb", lambda *_a, **_k: _NoDuck())

    async def _no_games(*_a, **_k):
        return []

    monkeypatch.setattr(bt, "_fetch_final_games", _no_games)
    out = tmp_path / "clv.json"
    args = bt.parse_args(
        [
            "--seasons",
            "2024",
            "--output",
            str(out),
            "--workers",
            "1",
            "--calibration-path",
            str(tmp_path / "no_calibration.json"),
            *extra,
        ]
    )
    await bt.run(args)
    return json.loads(out.read_text(encoding="utf-8"))["params"]


async def test_the_report_carries_the_odds_row_stamp(monkeypatch, tmp_path):
    params = await _run_empty_slate(monkeypatch, tmp_path)
    assert params["odds_row_version"] == ODDS_ROW_VERSION
    assert params["graded_book_preference"] == list(GRADED_BOOK_PREFERENCE)
    assert params["benchmark_book"] is None
    params = await _run_empty_slate(monkeypatch, tmp_path, "--benchmark-book", "bp:0")
    assert params["benchmark_book"] == BLEND


def test_the_benchmark_book_takes_a_label_or_a_name():
    assert bt.parse_args(["--seasons", "2024"]).benchmark_book is None
    assert bt.parse_args(["--seasons", "2024", "--benchmark-book", "bp:0"]).benchmark_book == BLEND
    named = bt.parse_args(["--seasons", "2024", "--benchmark-book", "DraftKings"])
    assert named.benchmark_book == DRAFTKINGS
    with pytest.raises(SystemExit):
        bt.parse_args(["--seasons", "2024", "--benchmark-book", "nosuchbook"])
    with pytest.raises(SystemExit):
        bt.parse_args(["--seasons", "2024", "--benchmark-book", "consensus"])


def test_the_table_header_names_the_graded_book():
    comparison = bt.aggregate_accuracy_comparison([], n_bootstrap=20, seed=1)
    params = {"seasons": [2024], **bt.odds_row_provenance()}
    text = bt.format_accuracy_comparison(comparison, params=params)
    # every book on the list by its display name, in the list's order
    names = ", ".join(BOOK_NAMES[book_id] for book_id in GRADED_BOOK_PREFERENCE)
    assert f"in this order: {names} (odds rows {ODDS_ROW_VERSION})" in text
    text = bt.format_accuracy_comparison(
        comparison, params={"seasons": [2024], **bt.odds_row_provenance(BLEND)}
    )
    assert "BettingPros Consensus (bp:0)" in text
    # an older report's params: no line
    text = bt.format_accuracy_comparison(comparison, params={"seasons": [2024]})
    assert "graded against" not in text


def _report(params: dict[str, Any], game_offset: int) -> dict[str, Any]:
    return {
        "params": {"base_seed": 0, "run_line_scoring": "sim549.1", **params},
        "accuracy_records": [
            {
                "game_pk": 1000 + game_offset + i,
                "market": "moneyline",
                "market_type": "moneyline",
                "sim_prob": 0.55,
                "market_prob": 0.5,
                "outcome": i % 2,
                "player_id": None,
                "market_side_price": -110.0,
                "market_other_price": -110.0,
            }
            for i in range(6)
        ],
    }


def _write(tmp_path: Path, name: str, rep: dict[str, Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(rep), encoding="utf-8")
    return str(path)


def test_a_report_without_the_stamp_does_not_merge_with_one_that_has_it(tmp_path):
    old = _write(tmp_path, "old.json", _report({}, 0))
    new = _write(tmp_path, "new.json", _report(bt.odds_row_provenance(), 100))
    with pytest.raises(SystemExit, match="odds_row_version"):
        skill.load_reports([old, new], force=False)
    # two reads graded the same way merge
    new2 = _write(tmp_path, "new2.json", _report(bt.odds_row_provenance(), 200))
    records, prov = skill.load_reports([new, new2], force=False)
    assert len(records) == 12 and prov["odds_row_version"] == ODDS_ROW_VERSION
    # --force merges with a warning, as for the other stamps
    records, _ = skill.load_reports([old, new], force=True)
    assert len(records) == 12


def test_two_preference_lists_or_two_benchmarks_do_not_merge(tmp_path):
    base = bt.odds_row_provenance()
    other_list = dict(base, graded_book_preference=[10, 12, 19])
    a = _write(tmp_path, "a.json", _report(base, 0))
    b = _write(tmp_path, "b.json", _report(other_list, 100))
    with pytest.raises(SystemExit, match="graded_book_preference"):
        skill.load_reports([a, b], force=False)
    c = _write(tmp_path, "c.json", _report(bt.odds_row_provenance(BLEND), 200))
    with pytest.raises(SystemExit, match="benchmark_book"):
        skill.load_reports([a, c], force=False)


def test_two_lists_do_not_pair(tmp_path, capsys):
    """The paired read and the skill table's composite both refuse two reads
    graded against different odds rows, on the same games."""
    base = bt.odds_row_provenance()
    a = _report(base, 0)
    b = _report(dict(base, graded_book_preference=[10, 12, 19]), 0)
    problems = pair.provenance_mismatches(a, b)
    assert any("graded_book_preference" in p for p in problems)
    unstamped = _report({}, 0)
    assert any("odds_row_version" in p for p in pair.provenance_mismatches(unstamped, a))
    bench = _report(bt.odds_row_provenance(BLEND), 0)
    assert any("benchmark_book" in p for p in pair.provenance_mismatches(a, bench))
    # the composite: a base read and an arm read graded against other lists
    pa = _write(tmp_path, "base.json", a)
    pb = _write(tmp_path, "arm.json", b)
    with pytest.raises(SystemExit, match="other odds rows"):
        skill.main([pa, "--compare", pb, "--min-n", "1", "--bootstrap-samples", "10"])
    assert skill.odds_row_mismatches(skill._provenance_key(a), skill._provenance_key(b)) == [
        "graded_book_preference"
    ]
    # the same stamps pair
    same = _write(tmp_path, "same.json", _report(base, 0))
    assert skill.main([pa, "--compare", same, "--min-n", "1", "--bootstrap-samples", "10"]) == 0
    capsys.readouterr()


def test_a_report_rescored_onto_the_graded_book_stays_apart_from_a_fresh_run(tmp_path):
    """The SIM-555 re-score (``rescored_from``) re-prices the fixed-line
    records only; its totals, run lines and props keep the old rows' prices.
    It carries the same odds-row stamp as a fresh run, so the re-score mark
    is what keeps the two apart."""
    stamp = bt.odds_row_provenance()
    rescored = _report({**stamp, "rescored_from": "old.json"}, 0)
    fresh = _report(stamp, 100)
    a = _write(tmp_path, "rescored.json", rescored)
    b = _write(tmp_path, "fresh.json", fresh)
    with pytest.raises(SystemExit, match="odds_rows_rescored"):
        skill.load_reports([a, b], force=False)
    fresh_same_games = _report(stamp, 0)
    problems = pair.provenance_mismatches(rescored, fresh_same_games)
    assert any("rescored_from" in p for p in problems)
    # two re-scored reports merge
    c = _write(tmp_path, "rescored2.json", _report({**stamp, "rescored_from": "x.json"}, 200))
    records, _ = skill.load_reports([a, c], force=False)
    assert len(records) == 12


# ---------------------------------------------------------------------------
# The calibration layer's merge (scripts/sim548_calibrate_markets.py)
# ---------------------------------------------------------------------------


def test_the_calibration_layer_refuses_reports_graded_against_other_odds_rows(tmp_path):
    """Review item: ``load_records`` merged accuracy reports without checking
    they were graded against the same odds rows. It now refuses a mix, on the
    same stamps as the market-skill table's merge."""
    assert calib.ODDS_ROW_KEYS == skill.ODDS_ROW_KEYS
    stamp = bt.odds_row_provenance()
    a = _write(tmp_path, "a.json", _report(stamp, 0))
    b = _write(tmp_path, "b.json", _report(stamp, 100))
    assert len(calib.load_records([a, b])) == 12
    # a report graded before SIM-555 (no stamp)
    old = _write(tmp_path, "old.json", _report({}, 200))
    with pytest.raises(SystemExit, match=r"odds_row_version: missing \(before SIM-555\)"):
        calib.load_records([a, old])
    # another preference list, a benchmark book, a partial re-score
    other_list = _write(
        tmp_path, "list.json", _report(dict(stamp, graded_book_preference=[10, 12, 19]), 300)
    )
    with pytest.raises(SystemExit, match="graded_book_preference"):
        calib.load_records([a, other_list])
    bench = _write(tmp_path, "bench.json", _report(bt.odds_row_provenance(BLEND), 400))
    with pytest.raises(SystemExit, match="benchmark_book"):
        calib.load_records([a, bench])
    rescored = _write(tmp_path, "re.json", _report({**stamp, "rescored_from": "x.json"}, 500))
    with pytest.raises(SystemExit, match="odds_rows_rescored"):
        calib.load_records([a, rescored])
    # the key matches the market-skill table's odds-row stamps
    rep = _report({**stamp, "rescored_from": "x.json"}, 0)
    prov = skill._provenance_key(rep)
    assert calib.odds_row_key(rep["params"]) == {
        k: prov[k] for k in (*skill.ODDS_ROW_KEYS, "odds_rows_rescored")
    }


def test_the_calibration_layer_refuses_a_fit_and_a_check_on_other_odds_rows(tmp_path):
    """The map is fitted on one set and read on the other against the same
    closing lines, so the two sets must match each other too."""
    stamp = bt.odds_row_provenance()
    fit = _write(tmp_path, "fit.json", _report(stamp, 0))
    check = _write(tmp_path, "check.json", _report({}, 100))
    with pytest.raises(SystemExit, match="odds_row_version"):
        calib.main(["--fit", fit, "--check", check, "--min-n", "1"])
    fit_records, check_records = calib.load_fit_and_check(
        [fit], [_write(tmp_path, "check2.json", _report(stamp, 100))]
    )
    assert len(fit_records) == len(check_records) == 6
