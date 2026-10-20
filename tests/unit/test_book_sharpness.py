"""``scripts/probes/book_sharpness.py``: the odds-against-outcome read that ranks
the books (plan test 17), on a synthetic season.

One of the five scripts of the one-row-per-book re-load (SIM-555, Partition E).
No test touches the network or a database.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import numpy as np
import pytest
from tests.script_loader import load_script

from betting.clv_engine import devig_multiway, devig_two_way, implied_prob_from_american
from pipeline.odds_provider import (
    GRADED_BOOK_PREFERENCE,
)

sharp = load_script("book_sharpness")


def _american(q: float) -> int:
    """A price whose implied probability is ``q`` (rounded as the store keeps it)."""
    if q >= 0.5:
        return int(round(-100.0 * q / (1.0 - q)))
    return int(round(100.0 * (1.0 - q) / q))


def _ml_row(game_pk: int, book: str, q_home: float, *, margin: float = 0.02, market="moneyline"):
    return {
        "game_pk": game_pk,
        "market_type": market,
        "book": book,
        "home_ml": _american(q_home + margin),
        "away_ml": _american(1.0 - q_home + margin),
        "draw_ml": None,
        "total_line": None,
        "over_ml": None,
        "under_ml": None,
    }


def _synthetic_season(n_games: int = 400, seed: int = 7):
    """A season where BetMGM prices the truth, DraftKings prices noise and the blend the truth."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    results: dict[int, Any] = {}
    for g in range(1, n_games + 1):
        p = float(rng.uniform(0.3, 0.7))
        home_wins = bool(rng.random() < p)
        results[g] = sharp.GameResult(
            home_score=5 if home_wins else 2, away_score=2 if home_wins else 5
        )
        rows.append(_ml_row(g, "bp:19", p))  # BetMGM: tracks the outcome
        rows.append(_ml_row(g, "bp:12", float(rng.uniform(0.3, 0.7))))  # DraftKings: noise
        rows.append(_ml_row(g, "bp:0", p))  # the blend
        rows.append(_ml_row(g, "bp:68", p))  # a prediction market: never read
        if g % 4 == 0:
            rows.append(_ml_row(g, "bp:13", p))  # Caesars: a quarter of the games
    return rows, results


class TestSharpness:
    def test_the_sharpness_read_ranks_books_without_the_simulator(self):
        rows, results = _synthetic_season()
        report = sharp.build_report(rows, [], results, {}, n_boot=200, seed=1)
        read = report["markets"]["moneyline"]
        ranking = [e["book"] for e in read["ranking"]]
        # The book whose prices track the outcomes ranks first, although
        # DraftKings leads the preference list.
        assert ranking == ["bp:19", "bp:12"]
        leader, second = read["ranking"]
        assert leader["rank"] == 1 and second["rank"] == 2
        assert leader["brier"] < second["brier"]
        lo, hi = second["gap_range90"]
        assert lo > 0  # the gap is real, not noise
        assert leader["gap_range90"] is None

    def test_the_blend_is_reported_apart_and_other_kinds_are_not_read(self):
        rows, results = _synthetic_season()
        read = sharp.build_report(rows, [], results, {}, n_boot=50)["markets"]["moneyline"]
        assert read["blend"]["book"] == "bp:0"
        assert "bp:0" not in [e["book"] for e in read["ranking"]]
        assert "bp:0" not in read["books"] and "bp:68" not in read["books"]
        assert read["blend"]["n"] == read["n_common_units"]

    def test_a_thin_book_is_scored_alone_but_not_ranked(self):
        rows, results = _synthetic_season()
        read = sharp.build_report(rows, [], results, {}, n_boot=50)["markets"]["moneyline"]
        assert "bp:13" in read["excluded"]
        assert read["books"]["bp:13"]["coverage"] == pytest.approx(0.25)
        assert read["books"]["bp:13"]["n"] == 100
        # The common games are the games every compared book quoted.
        assert read["n_common_units"] == 400
        report = sharp.build_report(rows, [], results, {}, n_boot=50, min_coverage=0.2)
        low = report["markets"]["moneyline"]
        assert "bp:13" in low["compared"] and low["n_common_units"] == 100

    def test_the_three_way_probability_uses_the_tie(self):
        row = {"home_ml": 150, "away_ml": 170, "draw_ml": 400}
        expected = devig_multiway(
            [
                implied_prob_from_american(150),
                implied_prob_from_american(170),
                implied_prob_from_american(400),
            ]
        )[0]
        assert sharp.reference_prob("f5_moneyline", row) == pytest.approx(expected)
        assert sharp.reference_prob("f5_moneyline", {"home_ml": 150, "away_ml": 170}) is None
        assert sharp.reference_prob(
            "moneyline", {"home_ml": -150, "away_ml": 130}
        ) == pytest.approx(devig_two_way(-150, 130)[0])
        assert sharp.reference_prob("first_inning_run", {"over_ml": 110, "under_ml": -130}) == (
            pytest.approx(devig_two_way(110, -130)[0])
        )

    def test_segment_outcomes_read_the_grid(self):
        grid = {"home": [0, 1, 0, 0, 2, 0, 0, 0, 0], "away": [1, 0, 0, 0, 0, 0, 0, 0, 0]}
        result = sharp.GameResult(home_score=3, away_score=1, grid=grid)
        assert sharp.game_outcome("f5_moneyline", result) == 1  # 3-1 after five
        assert sharp.game_outcome("f1_moneyline", result) == 0  # the away team led after one
        assert sharp.game_outcome("first_to_score", result) == 0  # the away team scored first
        assert sharp.game_outcome("first_inning_run", result, 0.5) == 1
        assert sharp.game_outcome("f5_total", result, 4.0) is None  # a push
        assert sharp.game_outcome("f5_moneyline", sharp.GameResult(3, 1, None)) is None

    def test_the_totals_keep_the_shared_line_and_drop_a_push(self):
        results = {
            1: sharp.GameResult(5, 4),  # 9 runs
            2: sharp.GameResult(4, 4),  # 8 runs: a push at 8
        }

        def total(game_pk, book, line, over=-110, under=-110):
            return {
                "game_pk": game_pk,
                "market_type": "total",
                "book": book,
                "total_line": line,
                "over_ml": over,
                "under_ml": under,
            }

        rows = [
            total(1, "bp:12", 8.5),
            total(1, "bp:10", 8.5),
            total(1, "bp:19", 9.0),  # off the shared line
            total(1, "bp:0", 8.5),
            total(2, "bp:12", 8.0),
            total(2, "bp:10", 8.0),
        ]
        skipped: Counter[str] = Counter()
        obs = sharp.shared_line_observations(rows, results, skipped)
        assert sorted(o.book for o in obs) == ["bp:0", "bp:10", "bp:12"]
        assert all(o.game_pk == 1 and o.outcome == 1 for o in obs)
        assert skipped["off_shared_line:total:bp:19"] == 1
        assert skipped["push_or_no_grid:total"] == 1

    def test_the_strikeout_prop_reads_the_box_score(self):
        def prop(game_pk, player, book, line):
            return {
                "game_pk": game_pk,
                "player_id": player,
                "book": book,
                "line": line,
                "over_ml": -115,
                "under_ml": -105,
            }

        rows = [
            prop(1, 7, "bp:12", 5.5),
            prop(1, 7, "bp:10", 5.5),
            prop(1, 7, "bp:19", 6.5),
            prop(1, 8, "bp:12", 4.5),  # did not pitch
            prop(2, 9, "bp:12", 6.0),  # a push
        ]
        ks = {(1, 7): 7, (2, 9): 6}
        obs = sharp.strikeout_observations(rows, ks)
        assert sorted((o.book, o.outcome) for o in obs) == [("bp:10", 1), ("bp:12", 1)]
        assert obs[0].unit == (1, 7) and obs[0].market == "strikeouts"

    def test_the_modal_line_breaks_a_tie_to_the_smaller_line(self):
        assert sharp.modal_line([8.5, 8.5, 9.0]) == 8.5
        assert sharp.modal_line([9.0, 8.5]) == 8.5
        assert sharp.modal_line([]) is None

    def test_the_clustered_range_resamples_games(self):
        assert sharp.clustered_range([0.2, 0.2, 0.2], [1, 2, 3], n_boot=50) == pytest.approx(
            (0.2, 0.2)
        )
        assert sharp.clustered_range([0.2, 0.4], [1, 1], n_boot=50) is None  # one game
        a = sharp.clustered_range([0.1, 0.5, 0.3, 0.9], [1, 2, 3, 4], n_boot=300, seed=3)
        b = sharp.clustered_range([0.1, 0.5, 0.3, 0.9], [1, 2, 3, 4], n_boot=300, seed=3)
        assert a == b and a[0] < a[1]

    def test_the_pool_keeps_a_book_that_skips_a_niche_market(self):
        rows, results = _synthetic_season(200)
        for g, res in results.items():
            home_first = res.home_score > res.away_score
            results[g] = sharp.GameResult(
                res.home_score,
                res.away_score,
                {
                    "home": [1 if home_first else 0] + [0] * 8,
                    "away": [0 if home_first else 1] + [0] * 8,
                },
            )
            # first to score: only BetMGM and the blend quote it
            p = 0.6 if home_first else 0.4
            rows.append(_ml_row(g, "bp:19", p, market="first_to_score"))
            rows.append(_ml_row(g, "bp:0", p, market="first_to_score"))
        report = sharp.build_report(rows, [], results, {}, n_boot=50)
        pooled = report["pooled_fixed_line"]
        assert pooled["markets"] == ["moneyline", "first_to_score"]
        assert pooled["anchor"] == "moneyline"
        # DraftKings skips the niche market: the market leaves the pool, the book stays.
        assert pooled["books"] == ["bp:12", "bp:19"]
        assert pooled["units_by_market"] == {"moneyline": 200, "first_to_score": 0}
        assert pooled["dropped_markets"] == {"first_to_score": "never quoted by bp:12"}
        assert pooled["left_out"] == {}
        assert [e["book"] for e in pooled["read"]["ranking"]] == ["bp:19", "bp:12"]
        suggestion = report["suggested_preference"]
        assert suggestion[:2] == [19, 12]
        assert sorted(suggestion) == sorted(GRADED_BOOK_PREFERENCE)
        assert "too few quotes to rank" in report["suggestion_note"]

    @staticmethod
    def _real_coverage_season(*, score_quotes_fir: bool = False, fanatics_ml: bool = False):
        """The coverage of the real event 99026 (tests/fixtures/bettingpros).

        Nine sportsbooks quote the moneyline and only BetMGM (bp:19) prices the
        truth; the first team to score is quoted by theScore (bp:33) and
        Fanatics (bp:14) only; a run in the first inning by FanDuel (bp:10) and
        DraftKings (bp:12) only (and theScore, when ``score_quotes_fir``).
        """
        rng = np.random.default_rng(1)
        rows: list[dict[str, Any]] = []
        results: dict[int, Any] = {}
        ml_books = (10, 12, 19, 13, 24, 33, 15, 18, 27) + ((14,) if fanatics_ml else ())
        for g in range(1, 401):
            p = float(rng.uniform(0.3, 0.7))
            home_wins = bool(rng.random() < p)
            home_first = bool(rng.random() < 0.5)
            grid = {
                "home": [1 if home_first else 0] + [0] * 8,
                "away": [0 if home_first else 1] + [0] * 8,
            }
            results[g] = sharp.GameResult(5 if home_wins else 2, 2 if home_wins else 5, grid)
            for b in ml_books:
                q = p if b == 19 else float(rng.uniform(0.3, 0.7))
                rows.append(_ml_row(g, f"bp:{b}", q))
            for b in (33, 14):
                rows.append(_ml_row(g, f"bp:{b}", 0.5, market="first_to_score"))
            for b in (10, 12, 33) if score_quotes_fir else (10, 12):
                rows.append(
                    {
                        "game_pk": g,
                        "market_type": "first_inning_run",
                        "book": f"bp:{b}",
                        "home_ml": None,
                        "away_ml": None,
                        "draw_ml": None,
                        "total_line": 0.5,
                        "over_ml": 110,
                        "under_ml": -130,
                    }
                )
        return rows, results

    def test_the_pool_on_the_real_coverage_ranks_the_sharp_book_first(self):
        rows, results = self._real_coverage_season(fanatics_ml=True)
        report = sharp.build_report(rows, [], results, {}, n_boot=50)
        pooled = report["pooled_fixed_line"]
        assert pooled["anchor"] == "moneyline"
        assert "bp:12" in pooled["books"] and "bp:19" in pooled["books"]
        # The niche markets leave the pool; the moneyline decides.
        assert set(pooled["dropped_markets"]) == {"first_to_score", "first_inning_run"}
        assert pooled["read"]["ranking"][0]["book"] == "bp:19"
        assert report["suggested_preference"][0] == 19
        assert "Suggested GRADED_BOOK_PREFERENCE (the pooled ranking" in sharp.format_report(report)

    def test_a_book_that_quotes_every_market_is_not_promoted(self):
        # theScore quotes all three markets, and its moneyline is noise.
        rows, results = self._real_coverage_season(score_quotes_fir=True, fanatics_ml=True)
        report = sharp.build_report(rows, [], results, {}, n_boot=50)
        pooled = report["pooled_fixed_line"]
        assert pooled["read"]["ranking"][0]["book"] == "bp:19"
        assert report["suggested_preference"][0] == 19
        assert report["suggested_preference"].index(33) > 0

    def test_no_suggestion_when_a_preferred_book_is_left_out_of_the_pool(self):
        # Fanatics (on today's list) is compared in first_to_score only.
        rows, results = self._real_coverage_season()
        report = sharp.build_report(rows, [], results, {}, n_boot=50)
        pooled = report["pooled_fixed_line"]
        assert pooled["left_out"] == {"bp:14": ["first_to_score"]}
        assert report["suggested_preference"] is None
        assert "Fanatics (bp:14" in report["suggestion_note"]
        text = sharp.format_report(report)
        assert "Suggested GRADED_BOOK_PREFERENCE: no suggestion" in text
        assert "the pooled ranking" not in text.split("Suggested GRADED_BOOK_PREFERENCE")[1]

    def test_no_suggestion_without_a_pool(self):
        assert sharp.suggested_preference({"read": None, "left_out": {}})[0] is None
        empty = sharp.pooled_ranking({})
        assert empty["read"] is None and empty["anchor"] is None
        assert sharp.suggested_preference(empty) == (None, "the pool ranks no book on common games")

    def test_a_book_on_part_of_a_market_keeps_those_games_in_the_pool(self):
        # DraftKings quotes the first-five moneyline on half the games only (its
        # 2025 first-five entries are excluded): it stays in the pool, and the
        # market pools the games it quoted.
        rows, results = _synthetic_season(200)
        for g, res in results.items():
            results[g] = sharp.GameResult(
                res.home_score,
                res.away_score,
                {"home": [res.home_score] + [0] * 8, "away": [res.away_score] + [0] * 8},
            )
            for book in ("bp:19", "bp:12") if g <= 100 else ("bp:19",):
                row = _ml_row(g, book, 0.45, market="f5_moneyline")
                row["draw_ml"] = 400
                rows.append(row)
        report = sharp.build_report(rows, [], results, {}, n_boot=20)
        pooled = report["pooled_fixed_line"]
        assert pooled["books"] == ["bp:12", "bp:19"]
        assert pooled["units_by_market"] == {"moneyline": 200, "f5_moneyline": 100}
        assert pooled["read"]["n_common_units"] == 300

    def test_the_report_is_json(self):
        rows, results = _synthetic_season(60)
        report = sharp.build_report(rows, [], results, {}, n_boot=20)
        json.dumps(report)
        text = sharp.format_report(report)
        assert "moneyline" in text and "Suggested GRADED_BOOK_PREFERENCE" in text
