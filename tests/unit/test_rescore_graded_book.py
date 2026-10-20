"""``scripts/probes/rescore_graded_book.py``: the re-pricing of a tiny fake report.

One of the five scripts of the one-row-per-book re-load (SIM-555, Partition E).
No test touches the network or a database.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from tests.script_loader import load_script

from betting.clv_engine import devig_multiway, devig_two_way, implied_prob_from_american
from pipeline.odds_provider import (
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    bettable_labels,
    graded_book_labels,
)

rescore = load_script("rescore_graded_book")


def _rec(game_pk, market, sim, mkt, outcome, side, other, *, market_type=None, player_id=None):
    return {
        "game_pk": game_pk,
        "market": market,
        "market_type": market_type or market,
        "sim_prob": sim,
        "market_prob": mkt,
        "outcome": outcome,
        "player_id": player_id,
        "market_side_price": side,
        "market_other_price": other,
    }


def _toy_report() -> dict[str, Any]:
    return {
        "params": {"base_seed": 0, "bootstrap_samples": 20, "bootstrap_seed": 1},
        "counters": {"games_scored": 2, "n_accuracy_records": 9},
        "accuracy_comparison": {},
        "accuracy_records": [
            _rec(1, "moneyline", 0.60, 0.55, 1, -125.0, 105.0),
            _rec(1, "f5_moneyline", 0.45, 0.44, 1, 120.0, None),
            _rec(1, "f1_moneyline", 0.30, 0.31, 0, 250.0, None),
            _rec(1, "first_inning_run", 0.52, 0.50, 1, -105.0, -115.0),
            _rec(1, "first_to_score", 0.51, 0.50, 0, -102.0, -118.0),
            _rec(1, "total", 0.48, 0.49, 0, -110.0, -110.0),
            _rec(1, "runline", 0.40, 0.42, 0, 140.0, -160.0),
            _rec(1, "K", 0.55, 0.52, 1, -120.0, 100.0, market_type="prop", player_id=7),
            _rec(2, "moneyline", 0.40, 0.45, 0, 110.0, -130.0),
        ],
    }


def _toy_store() -> Any:
    def game_row(book, **cols):
        base = dict.fromkeys(
            (
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
        )
        base["book"] = book
        base.update(cols)
        return base

    return rescore.OddsStore(
        graded_game={
            1: {
                "moneyline": game_row("bp:12", home_ml=-150.0, away_ml=130.0),
                "f5_moneyline": game_row("bp:19", home_ml=130.0, away_ml=150.0, draw_ml=450.0),
                "f1_moneyline": game_row("bp:12", home_ml=250.0, away_ml=280.0),  # no tie price
                "first_inning_run": game_row(
                    "bp:10", total_line=0.5, over_ml=100.0, under_ml=-120.0
                ),
                "total": game_row("bp:12", total_line=8.5, over_ml=-105.0, under_ml=-115.0),
                "runline": game_row(
                    "bp:12",
                    home_spread=1.5,
                    home_spread_ml=-180.0,
                    away_spread=-1.5,
                    away_spread_ml=150.0,
                ),
            }
        },
        old_game={
            1: {
                "total": game_row("consensus", total_line=8.5, over_ml=-110.0, under_ml=-110.0),
                "runline": game_row(
                    "consensus",
                    home_spread=-1.5,
                    home_spread_ml=140.0,
                    away_spread=1.5,
                    away_spread_ml=-160.0,
                ),
            }
        },
        graded_prop={
            (1, 7, "strikeouts"): {
                "book": "bp:12",
                "line": 6.5,
                "over_ml": -110.0,
                "under_ml": -110.0,
            }
        },
        old_prop={
            (1, 7, "strikeouts"): {
                "book": "consensus",
                "line": 5.5,
                "over_ml": -120.0,
                "under_ml": 100.0,
            }
        },
        loaded_games={1},
    )


def _by_market(records):
    return {(r["game_pk"], r["market"]): r for r in records}


class TestRescore:
    def test_the_fixed_line_records_are_repriced_from_the_graded_row(self):
        out, summary = rescore.rescore_report(
            _toy_report(), _toy_store(), source="x.json", date="2026-09-28"
        )
        recs = _by_market(out["accuracy_records"])
        ml = recs[(1, "moneyline")]
        assert ml["market_prob"] == pytest.approx(devig_two_way(-150.0, 130.0)[0])
        assert (ml["market_side_price"], ml["market_other_price"]) == (-150.0, 130.0)
        assert (ml["sim_prob"], ml["outcome"]) == (0.60, 1)  # the simulator's side is kept
        f5 = recs[(1, "f5_moneyline")]
        three = devig_multiway(
            [
                implied_prob_from_american(130),
                implied_prob_from_american(150),
                implied_prob_from_american(450),
            ]
        )
        assert f5["market_prob"] == pytest.approx(three[0])
        assert f5["market_other_price"] is None
        fir = recs[(1, "first_inning_run")]
        assert fir["market_prob"] == pytest.approx(devig_two_way(100.0, -120.0)[0])
        assert summary["graded_books"]["moneyline"] == {"bp:12": 1}

    def test_a_record_without_a_graded_price_is_given_up(self):
        out, summary = rescore.rescore_report(
            _toy_report(), _toy_store(), source="x.json", date="d"
        )
        recs = _by_market(out["accuracy_records"])
        assert (1, "f1_moneyline") not in recs  # the graded row lists no tie
        assert (1, "first_to_score") not in recs  # no graded row
        dropped = out["params"]["rescored_sim555"]["dropped"]
        assert dropped == {
            "f1_moneyline:dropped_no_tie_price": 1,
            "first_to_score:dropped_no_graded_row": 1,
        }

    def test_the_line_records_are_counted_not_changed(self):
        report = _toy_report()
        out, _ = rescore.rescore_report(report, _toy_store(), source="x.json", date="d")
        recs = _by_market(out["accuracy_records"])
        for key in ((1, "total"), (1, "runline"), (1, "K")):
            original = next(
                r for r in report["accuracy_records"] if (r["game_pk"], r["market"]) == key
            )
            assert recs[key] == original
        lines = out["params"]["rescored_sim555"]["line_markets"]
        assert lines["total"] == {"records": 1, "same_line": 1}
        assert lines["runline"] == {"records": 1, "line_moved": 1}
        assert lines["K"] == {"records": 1, "line_moved": 1}

    def test_a_game_not_reloaded_is_a_problem_and_keeps_its_record(self):
        report = _toy_report()
        out, summary = rescore.rescore_report(report, _toy_store(), source="x.json", date="d")
        recs = _by_market(out["accuracy_records"])
        assert recs[(2, "moneyline")] == report["accuracy_records"][-1]
        assert len(summary["problems"]) == 1 and "game 2" in summary["problems"][0]

    def test_drop_not_reloaded_drops_every_record_of_an_unmatched_game(self):
        """In a fully re-loaded season, a game with no bp: row is one the event
        matcher declines, and its old prices likely belong to another game.
        --drop-not-reloaded drops all its records."""
        report = _toy_report()
        game2 = [r for r in report["accuracy_records"] if int(r["game_pk"]) == 2]
        assert game2  # the toy report's game 2 is not re-loaded
        out, summary = rescore.rescore_report(
            report, _toy_store(), source="x.json", date="d", drop_not_reloaded=True
        )
        assert all(int(r["game_pk"]) != 2 for r in out["accuracy_records"])
        assert summary["problems"] == []
        assert summary["dropped_not_reloaded_games"] == [2]
        assert out["params"]["rescored_sim555"]["dropped_not_reloaded_games"] == [2]
        assert sum(
            v for k, v in summary["tally"].items() if k.endswith(":dropped_not_reloaded")
        ) == (len(game2))
        # The re-loaded game is re-scored as before.
        assert any(int(r["game_pk"]) == 1 for r in out["accuracy_records"])

    def test_the_output_is_stamped_and_reaggregated(self):
        out, _ = rescore.rescore_report(_toy_report(), _toy_store(), source="x.json", date="d")
        params = out["params"]
        assert params["odds_row_version"] == ODDS_ROW_VERSION
        assert params["graded_book_preference"] == list(GRADED_BOOK_PREFERENCE)
        assert params["benchmark_book"] is None
        assert params["rescored_from"] == "x.json"
        assert params["rescored_sim555"]["records"] == 3
        assert out["counters"]["n_accuracy_records"] == 7
        assert "moneyline" in json.dumps(out["accuracy_comparison"])
        json.dumps(out)

    def test_a_stamped_report_or_a_paired_read_is_refused(self):
        stamped = _toy_report()
        stamped["params"]["odds_row_version"] = ODDS_ROW_VERSION
        with pytest.raises(rescore.RescoreRefused):
            rescore.rescore_report(stamped, _toy_store(), source="x", date="d")
        with pytest.raises(rescore.RescoreRefused):
            rescore.rescore_report({"params": {}}, _toy_store(), source="x", date="d")

    def test_the_output_path_and_the_needs(self):
        assert rescore.output_path("scripts/a.json").replace("\\", "/") == "scripts/a.sim555.json"
        games, stats = rescore.report_needs(_toy_report())
        assert games == {1, 2} and stats == {"strikeouts"}

    def test_the_graded_sql_is_the_readers_rule(self):
        for sql in (rescore.GRADED_GAME_SQL, rescore.GRADED_PROP_SQL):
            assert "book LIKE 'bp:%'" in sql
            # Review fix (VOCAB-1): a list of the listed sportsbooks, never a blacklist.
            assert "AND book = ANY($2::varchar[])" in sql and "NOT (book = ANY(" not in sql
            assert "array_position($3::varchar[], book) NULLS LAST, fetched_at DESC" in sql
            assert "line_type = 'closing'" in sql
        assert "book = 'consensus'" in rescore.OLD_GAME_SQL
        assert "book = 'consensus'" in rescore.OLD_PROP_SQL

    def test_the_graded_game_sql_grades_a_tie_less_three_way_row_last(self):
        """Review item: the re-score re-prices the first-inning and first-five
        moneylines, which need the tie, with its own graded-row SQL. It now
        carries the backtest's tie-last term right after the DISTINCT ON keys,
        so a three-way row with no tie price is graded only when no book lists
        the tie. The query runs on DuckDB here, as written."""
        duckdb = pytest.importorskip("duckdb")
        from accuracy.pricing import INCOMPLETE_THREE_WAY_LAST_SQL

        assert rescore.INCOMPLETE_THREE_WAY_LAST_SQL == INCOMPLETE_THREE_WAY_LAST_SQL
        flat = " ".join(rescore.GRADED_GAME_SQL.split())
        assert (
            "ORDER BY game_pk, market_type, "
            f"{INCOMPLETE_THREE_WAY_LAST_SQL}, "
            "array_position($3::varchar[], book) NULLS LAST, fetched_at DESC"
        ) in flat
        # A prop has no tie: its read keeps the plain order.
        assert INCOMPLETE_THREE_WAY_LAST_SQL not in rescore.GRADED_PROP_SQL

        con = duckdb.connect()
        con.execute("CREATE SCHEMA raw")
        con.execute(
            "CREATE TABLE raw.game_odds (game_pk INTEGER, market_type VARCHAR, book VARCHAR, "
            "line_type VARCHAR, fetched_at INTEGER, home_ml INTEGER, away_ml INTEGER, "
            "draw_ml INTEGER, home_spread DOUBLE, home_spread_ml INTEGER, away_spread DOUBLE, "
            "away_spread_ml INTEGER, total_line DOUBLE, over_ml INTEGER, under_ml INTEGER)"
        )
        # The books play roles on the preference list, so a reorder of the
        # list never changes what the test checks.
        first, second, third = graded_book_labels()[:3]
        rows = [
            # The first book on the list lists the first-inning moneyline's two
            # teams only (as DraftKings does); the second and third list the tie,
            # the third with the newer fetch.
            (1, "f1_moneyline", first, "closing", 5, -120, 100, None),
            (1, "f1_moneyline", second, "closing", 0, 150, 190, -110),
            (1, "f1_moneyline", third, "closing", 9, 160, 180, -115),
            # No book lists the first-five tie: the tie-less row stays graded.
            (1, "f5_moneyline", first, "closing", 0, -110, -110, None),
            (1, "f5_moneyline", second, "closing", 9, -115, -105, None),
            # A two-way market is not touched: its draw_ml is always empty.
            (1, "moneyline", first, "closing", 0, -120, 100, None),
            (1, "moneyline", second, "closing", 9, -125, 105, None),
        ]
        con.executemany(
            "INSERT INTO raw.game_odds VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            rows,
        )
        args = [[1], bettable_labels(), graded_book_labels()]
        cur = con.execute(rescore.GRADED_GAME_SQL, args)
        names = [d[0] for d in cur.description]
        graded = {
            r["market_type"]: r for r in (dict(zip(names, t, strict=True)) for t in cur.fetchall())
        }
        # the first book on the list with a complete row (not the third's newer one)
        assert (graded["f1_moneyline"]["book"], graded["f1_moneyline"]["draw_ml"]) == (
            second,
            -110,
        )
        assert graded["f5_moneyline"]["book"] == first
        assert graded["f5_moneyline"]["draw_ml"] is None
        assert graded["moneyline"]["book"] == first

    def test_the_old_rows_are_read_from_the_archive_too(self):
        # The retirement moves the consensus rows to the archives: a re-run
        # after it must still read the old lines.
        for sql, live in ((rescore.OLD_GAME_SQL, "game_odds"), (rescore.OLD_PROP_SQL, "prop_odds")):
            assert re.search(rf"FROM raw\.{live}\b(?!_)", sql)
            assert f"FROM raw.{live}_archive" in sql
            assert "UNION ALL" in sql
            assert sql.count("book = 'consensus'") == 2
            assert sql.count("line_type = 'closing'") == 2
            assert "fetched_at DESC" in sql

    def test_a_reloaded_game_without_old_rows_is_a_problem(self):
        store = _toy_store()
        store.old_game.clear()
        store.old_prop.clear()
        out, summary = rescore.rescore_report(_toy_report(), store, source="x.json", date="d")
        assert any(
            p.startswith("game 1: no old consensus closing row, live or archived")
            for p in summary["problems"]
        )
        lines = out["params"]["rescored_sim555"]["line_markets"]
        assert lines["total"] == {"records": 1, "no_old_row": 1}
        # The fixed-line records need no old row: they are still re-priced.
        assert out["params"]["rescored_sim555"]["records"] == 3

    def test_a_degenerate_price_is_unpriceable(self):
        with pytest.raises(rescore.Unpriceable) as err:
            rescore.reprice_fixed_line("moneyline", {"home_ml": 0.0, "away_ml": 100.0})
        assert err.value.reason == "degenerate"
        with pytest.raises(rescore.Unpriceable) as err:
            rescore.reprice_fixed_line("moneyline", {"home_ml": None, "away_ml": 100.0})
        assert err.value.reason == "missing_side"
