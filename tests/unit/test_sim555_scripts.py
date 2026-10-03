"""SIM-555 — the five scripts of the one-row-per-book re-load (Partition E).

* ``scripts/sim555_book_sharpness.py``: the odds-against-outcome read that ranks
  the books (plan test 17), on a synthetic season;
* ``scripts/sim555_retire_consensus_rows.py``: the refusal, the dry run and the
  archive-then-delete, on a stub connection (never a real database);
* ``scripts/sim555_rescore_reports.py``: the re-pricing of a tiny fake report;
* ``scripts/sim555_book_probe.py``: the sample, the gate math on synthetic rows,
  and one game read through the provider with a stubbed vendor;
* ``scripts/sim555_odds_census.sql``: read-only, and its literals in step with
  the book vocabulary.

No test touches the network or a database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import urllib.parse
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from betting.clv_engine import devig_multiway, devig_two_way, implied_prob_from_american
from pipeline.bettingpros_odds_provider import (
    F5_EXCLUDED_BOOKS,
    F5_TWIN_MARKETS,
    f5_excluded_markets,
)
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
    GRADED_BOOK_PREFERENCE,
    ODDS_ROW_VERSION,
    bettable_labels,
    graded_book_labels,
    non_bettable_labels,
)
from pipeline.odds_row_guard import (
    F5_TOTAL_LINE_MAX,
    THREE_WAY_SUM_MAX,
    THREE_WAY_SUM_MIN,
    THREE_WAY_TEAM_SUM_MAX,
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sharp = _load("sim555_book_sharpness")
retire = _load("sim555_retire_consensus_rows")
rescore = _load("sim555_rescore_reports")
probe = _load("sim555_book_probe")


def _american(q: float) -> int:
    """A price whose implied probability is ``q`` (rounded as the store keeps it)."""
    if q >= 0.5:
        return int(round(-100.0 * q / (1.0 - q)))
    return int(round(100.0 * (1.0 - q) / q))


# ===========================================================================
# The census SQL
# ===========================================================================


class TestCensusSql:
    SQL = (_SCRIPTS / "sim555_odds_census.sql").read_text(encoding="utf-8")

    def _statements(self) -> str:
        """The SQL without its comments and psql meta-commands."""
        lines = []
        for line in self.SQL.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") or stripped.startswith("\\"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_it_is_read_only(self):
        body = self._statements()
        assert "BEGIN READ ONLY;" in body
        assert body.rstrip().endswith("ROLLBACK;")
        for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE", "COMMIT"):
            assert not re.search(rf"\b{word}\b", body), word

    def test_the_sportsbook_list_matches_the_vocabulary(self):
        # Review fix (VOCAB-1): a list of what may be graded, never a blacklist,
        # so a book the vocabulary does not list is never graded.
        literal = "o.book IN (" + ", ".join(f"'{b}'" for b in bettable_labels()) + ")"
        assert self.SQL.count(literal) == 2
        assert "NOT IN ('bp:" not in self.SQL
        assert not any(f"'{b}'" in self._statements() for b in non_bettable_labels())

    def test_the_three_way_markets_match_the_vocabulary(self):
        three_way = [m for m in GAME_MARKET_TYPES if GAME_MARKET_KIND[m] == "three_way"]
        literal = "o.market_type IN (" + ", ".join(f"'{m}'" for m in three_way) + ")"
        # Sections 13 and 14 (the tie-last rule) and the tie-price check.
        assert self.SQL.count(literal) >= 3
        found = re.findall(r"o\.market_type IN \('f1_moneyline'[^)]*\)", self.SQL)
        assert found and set(found) == {literal}

    def test_the_graded_book_section_grades_a_tie_less_three_way_row_last(self):
        """Review fix (contracts #2): section 14 follows the backtest's order, a
        three-way row with no tie after every row with one, THEN the preference
        list; section 13 counts a three-way game only when a row lists the tie."""
        sections = self._sections(self.SQL)
        tie_last = "(o.draw_ml IS NULL AND o.market_type IN ('f1_moneyline', 'f5_moneyline'))"
        body = sections["14"]
        assert body.index(tie_last) < body.index("array_position(")
        assert f"NOT {tie_last}" in sections["13"]

    def test_the_team_total_line_of_one_is_checked(self):
        from pipeline.odds_row_guard import TEAM_TOTAL_LINE_MAX

        body = self._sections(self.SQL)["6b"]
        assert "o.market_type IN ('team_total_home', 'team_total_away')" in body
        # The census reads the guard's own limit.
        assert f"o.total_line <= {TEAM_TOTAL_LINE_MAX:g}" in body

    def test_the_preference_list_matches_the_vocabulary(self):
        literal = "ARRAY[" + ", ".join(f"'{b}'" for b in graded_book_labels()) + "]"
        assert literal in self.SQL

    def test_the_dated_exclusion_matches_the_provider(self):
        # SIM-555 census fix: each book on its own markets (F5_EXCLUDED_MARKETS).
        for book_id, start in F5_EXCLUDED_BOOKS.items():
            markets = ", ".join(
                f"'{probe.F5_MARKET_NAMES[m]}'"
                for m in F5_TWIN_MARKETS
                if m in f5_excluded_markets(book_id)
            )
            assert (
                f"(o.book = 'bp:{book_id}' AND g.game_date >= DATE '{start.isoformat()}' "
                f"AND o.market_type IN ({markets}))" in self.SQL
            )
        assert "'f5_moneyline'" not in self._sections(self.SQL)["9"]

    def test_the_new_guard_rules_are_checked_at_the_guard_s_limits(self):
        """SIM-555 census fix: section 4b is the guard's f5_total_first_inning_line
        rule, section 5b its three_way_sum_below_one rule, at the same limits."""
        sections = self._sections(self.SQL)
        assert "o.market_type = 'f5_total'" in sections["4b"]
        assert f"o.total_line <= {F5_TOTAL_LINE_MAX}" in sections["4b"]
        assert "f5_team_total" not in sections["4b"]
        assert "o.market_type IN ('f1_moneyline', 'f5_moneyline')" in sections["5b"]
        assert f"p_sum < {THREE_WAY_SUM_MIN}" in sections["5b"]
        assert f"p_sum > {THREE_WAY_SUM_MAX}" in sections["5b"]
        assert f"p_teams >= {THREE_WAY_TEAM_SUM_MAX}" in sections["5b"]
        assert "o.draw_ml" in sections["5b"]
        for number in ("4b", "5b"):
            assert "Healthy: zero rows" in self.SQL.split(f"== {number}. ")[1].splitlines()[0]

    @staticmethod
    def _sections(sql: str) -> dict[str, str]:
        """``{section number: its SQL}``, split on the ``== N.`` headings, comments dropped."""
        parts = re.split(r"^\\echo '== (\d+b?)\. ", sql, flags=re.M)
        out: dict[str, str] = {}
        for number, body in zip(parts[1::2], parts[2::2], strict=True):
            out[number] = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith("--")
            )
        return out

    @classmethod
    def _unfiltered(cls, sql: str) -> list[str]:
        """The sections with a read of an odds table that carries no book filter.

        Each ``FROM raw.game_odds`` / ``FROM raw.prop_odds`` must be matched by
        one book filter in its section: the ``bp:`` label, one ``bp:`` book, or
        (sections 13 and 20 only) the old ``consensus`` rows.
        """
        bad: list[str] = []
        for number, body in cls._sections(sql).items():
            reads = len(re.findall(r"\bFROM raw\.(?:game|prop)_odds\b", body))
            filters = len(re.findall(r"\bbook LIKE 'bp:%'", body)) + len(
                re.findall(r"\bbook = 'bp:\d+'", body)
            )
            consensus = len(re.findall(r"\bbook = 'consensus'", body))
            stray_consensus = consensus and number not in ("13", "20")
            unmatched = reads != filters + consensus or (reads and ":season" not in body)
            if stray_consensus or unmatched:
                bad.append(number)
        return bad

    def test_every_census_query_reads_the_bp_rows(self):
        assert len(self._sections(self.SQL)) == 24  # 1-20, 4b, 5b, 6b and 7b
        assert self._unfiltered(self.SQL) == []

    @pytest.mark.parametrize(
        ("section", "old", "new"),
        [
            (
                "10",
                "AND o.book LIKE 'bp:%'\n    AND o.line_type IN ('opening', 'closing')",
                "\n    AND o.line_type IN ('opening', 'closing')",
            ),
            (
                "16",
                "p.book LIKE 'bp:%'\n  AND (p.over_ml IS NULL",
                "true\n  AND (p.over_ml IS NULL",
            ),
            ("9", "AND (o.book = 'bp:12' AND", "AND (true AND"),
        ],
    )
    def test_the_filter_check_sees_a_section_that_loses_its_filter(self, section, old, new):
        assert self.SQL.count(old) == 1
        assert self._unfiltered(self.SQL.replace(old, new)) == [section]

    def test_the_duplicate_checks_leave_out_the_current_rows(self):
        # The live cycle writes one current row per book per price change by design.
        sections = self._sections(self.SQL)
        assert "o.line_type IN ('opening', 'closing')" in sections["10"]
        assert "p.line_type IN ('opening', 'closing')" in sections["18"]

    def test_the_prop_missing_side_check_matches_the_guard(self):
        # The guard refuses a prop row with no line (missing_side), so the census counts it.
        assert (
            "(p.over_ml IS NULL OR p.under_ml IS NULL OR p.line IS NULL)"
            in self._sections(self.SQL)["16"]
        )


# ===========================================================================
# The sharpness read (plan test 17)
# ===========================================================================


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


# ===========================================================================
# The retirement script — a stub connection only
# ===========================================================================

_LIVE_COLUMNS = ["id", "game_pk", "book", "book_line_at"]


class _StubTransaction:
    def __init__(self, conn: _StubConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.outcomes.append("rollback" if exc_type else "commit")
        return False


class _StubConn:
    """Answers the retirement script's queries from canned values; records every call."""

    def __init__(
        self,
        *,
        missing: list[int],
        counts: dict[str, int],
        affected: dict[str, int] | None = None,
        archive_columns: list[str] | None = None,
        missing_props: list[int] | None = None,
    ) -> None:
        self.missing = missing
        self.missing_props = missing_props or []
        self.counts = counts
        self.affected = affected or counts
        self.archive_columns = archive_columns or _LIVE_COLUMNS
        self.calls: list[tuple[str, str]] = []
        self.transactions: list[dict[str, Any]] = []
        self.outcomes: list[str] = []

    def transaction(self, **kwargs: Any) -> _StubTransaction:
        self.transactions.append(kwargs)
        return _StubTransaction(self)

    @staticmethod
    def _table(sql: str) -> str:
        return "prop_odds" if "raw.prop_odds" in sql else "game_odds"

    async def fetch(self, sql: str, *args: Any):
        self.calls.append(("fetch", sql))
        if sql == retire.MISSING_SQL:
            return [{"game_pk": g} for g in self.missing]
        if sql == retire.MISSING_PROPS_SQL:
            return [{"game_pk": g} for g in self.missing_props]
        if sql == retire.COLUMNS_SQL:
            names = self.archive_columns if args[0].endswith("_archive") else _LIVE_COLUMNS
            return [{"column_name": c} for c in names]
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any):
        self.calls.append(("fetchval", sql))
        return self.counts[self._table(sql)]

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql))
        table = self._table(sql)
        n = self.affected[table]
        return f"INSERT 0 {n}" if sql.startswith("INSERT") else f"DELETE {n}"

    def executed(self) -> list[str]:
        return [sql for kind, sql in self.calls if kind == "execute"]


def _run(coro):
    return asyncio.run(coro)


class TestRetire:
    def test_a_season_not_reloaded_is_refused_and_untouched(self, capsys):
        conn = _StubConn(missing=[11, 12, 13], counts={"game_odds": 5, "prop_odds": 9})
        code = _run(retire.run(conn, [2024], dry_run=False))
        assert code == retire.EXIT_REFUSED
        assert conn.executed() == []
        out = capsys.readouterr().out
        assert "REFUSED" in out and "3 Final games" in out and "[11, 12, 13]" in out

    def test_the_dry_run_counts_and_writes_nothing(self, capsys):
        conn = _StubConn(missing=[], counts={"game_odds": 5, "prop_odds": 9})
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        assert conn.executed() == []
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
        out = capsys.readouterr().out
        assert "dry run" in out and "raw.game_odds 5" in out and "raw.prop_odds 9" in out

    def test_a_real_run_archives_then_deletes_each_table(self, capsys):
        conn = _StubConn(missing=[], counts={"game_odds": 5, "prop_odds": 9})
        result = _run(retire.retire_season(conn, 2024, dry_run=False))
        assert result.archived == {"game_odds": 5, "prop_odds": 9}
        assert result.deleted == {"game_odds": 5, "prop_odds": 9}
        sqls = conn.executed()
        assert [s.split()[0] for s in sqls] == ["INSERT", "DELETE", "INSERT", "DELETE"]
        assert "raw.game_odds_archive (id, game_pk, book, book_line_at)" in sqls[0]
        assert all("book = 'consensus'" in s for s in sqls)
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": False}]
        assert conn.outcomes == ["commit"]

    def test_a_count_mismatch_rolls_the_season_back(self, capsys):
        conn = _StubConn(
            missing=[],
            counts={"game_odds": 5, "prop_odds": 9},
            affected={"game_odds": 4, "prop_odds": 9},
        )
        code = _run(retire.run(conn, [2024], dry_run=False))
        assert code == retire.EXIT_ERROR
        assert conn.outcomes == ["rollback"]
        assert "ERROR" in capsys.readouterr().out

    def test_an_archive_missing_a_live_column_is_an_error(self):
        conn = _StubConn(
            missing=[], counts={"game_odds": 5, "prop_odds": 9}, archive_columns=["id", "game_pk"]
        )
        with pytest.raises(retire.RetireError, match="book"):
            _run(retire.retire_season(conn, 2024, dry_run=False))
        assert conn.executed() == []
        assert conn.outcomes == ["rollback"]

    def test_a_refused_season_does_not_stop_the_others(self, capsys):
        class _PerSeason(_StubConn):
            async def fetch(self, sql, *args):
                if sql == retire.MISSING_SQL:
                    return [{"game_pk": 1}] if args[0] == 2020 else []
                return await super().fetch(sql, *args)

        conn = _PerSeason(missing=[], counts={"game_odds": 2, "prop_odds": 3})
        code = _run(retire.run(conn, [2020, 2021], dry_run=False))
        assert code == retire.EXIT_REFUSED
        assert len(conn.executed()) == 4  # 2021 ran
        assert "VACUUM" in capsys.readouterr().out

    def test_games_without_new_props_are_a_warning_not_a_refusal(self, capsys):
        conn = _StubConn(
            missing=[], counts={"game_odds": 5, "prop_odds": 9}, missing_props=[21, 22]
        )
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        out = capsys.readouterr().out
        assert "WARNING: 2 Final games have consensus closing props" in out and "[21, 22]" in out

    def test_rows_affected(self):
        assert retire.rows_affected("INSERT 0 12") == 12
        assert retire.rows_affected("DELETE 7") == 7
        with pytest.raises(retire.RetireError):
            retire.rows_affected("")

    def test_the_cli_needs_seasons_and_a_dsn(self, monkeypatch):
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
        with pytest.raises(SystemExit):
            retire.parse_args(["--seasons", "2024"])
        with pytest.raises(SystemExit):
            retire.parse_args(["--dsn", "postgresql://x"])
        args = retire.parse_args(
            ["--seasons", "2024", "2025", "--dry-run", "--dsn", "postgresql://x"]
        )
        assert args.seasons == [2024, 2025] and args.dry_run


class _NamedStub(_StubConn):
    """SIM-555: the stub, plus a missing list per season and the season of each named game."""

    def __init__(
        self, *, missing_by_season: dict[int, list[int]], season_of: dict[int, int], **kw: Any
    ) -> None:
        super().__init__(missing=[], **kw)
        self.missing_by_season = missing_by_season
        self.season_of = season_of
        self.named_args: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any):
        if sql == retire.NAMED_GAMES_SQL:
            self.calls.append(("fetch", sql))
            self.named_args.append(args)
            return [
                {"game_pk": g, "season": self.season_of[g]} for g in args[0] if g in self.season_of
            ]
        if sql == retire.MISSING_SQL:
            self.calls.append(("fetch", sql))
            return [{"game_pk": g} for g in self.missing_by_season.get(args[0], [])]
        return await super().fetch(sql, *args)


class TestRetireAllowMissing:
    """SIM-555: ``--allow-missing`` lets named games through the refusal, nothing else."""

    _COUNTS = {"game_odds": 5, "prop_odds": 9}

    def test_a_named_missing_game_lets_its_season_through_and_is_printed(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2019], dry_run=False, allow_missing=[567323]))
        assert code == retire.EXIT_OK
        # The named game's rows are archived and deleted with the season: the same
        # four statements, with no game left out of them.
        sqls = conn.executed()
        assert [s.split()[0] for s in sqls] == ["INSERT", "DELETE", "INSERT", "DELETE"]
        assert not any("567323" in s or "ANY(" in s for s in sqls)
        assert conn.outcomes == ["commit"]
        out = capsys.readouterr().out
        assert "REFUSED" not in out and "archived and deleted" in out
        assert "ALLOWED by --allow-missing (1): [567323]" in out
        assert "stale" not in out

    def test_the_result_carries_the_named_game_apart_from_the_refusal(self):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        result = _run(retire.retire_season(conn, 2019, dry_run=False, allow_missing=[567323]))
        assert not result.refused and result.missing_games == 0 and result.examples == []
        assert result.allowed == [567323] and result.stale_names == []
        assert result.archived == self._COUNTS and result.deleted == self._COUNTS

    def test_an_unnamed_missing_game_still_refuses(self, capsys):
        conn = _NamedStub(
            missing_by_season={2024: [745169, 746755, 745175]},
            season_of={745175: 2024},
            counts=self._COUNTS,
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[745175]))
        assert code == retire.EXIT_REFUSED
        assert conn.executed() == []
        out = capsys.readouterr().out
        assert "REFUSED — 2 Final games" in out and "[745169, 746755]" in out
        assert "ALLOWED by --allow-missing (1): [745175]" in out

    def test_a_named_game_that_is_not_missing_is_a_stale_name(self, capsys):
        # 745175 sits in 2024 but has a bp: closing moneyline (it is not missing).
        conn = _NamedStub(
            missing_by_season={2024: []}, season_of={745175: 2024}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[745175]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 4
        out = capsys.readouterr().out
        assert "WARNING: stale --allow-missing names [745175]" in out
        assert "ALLOWED" not in out

    def test_a_named_game_in_no_named_season_is_a_stale_name(self, capsys):
        # 567323 is a 2019 game, 999999999 is in no season; only 2024 runs.
        conn = _NamedStub(
            missing_by_season={2024: []}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2024], dry_run=False, allow_missing=[999999999, 567323]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 4
        assert conn.named_args == [([567323, 999999999],)]
        out = capsys.readouterr().out
        assert "WARNING: stale --allow-missing names [567323, 999999999]" in out
        assert "in no named season" in out

    def test_each_season_gets_only_its_own_named_games(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323], 2024: [745175]},
            season_of={567323: 2019, 745175: 2024},
            counts=self._COUNTS,
        )
        code = _run(retire.run(conn, [2019, 2024], dry_run=False, allow_missing=[745175, 567323]))
        assert code == retire.EXIT_OK
        assert len(conn.executed()) == 8
        out = capsys.readouterr().out
        assert "ALLOWED by --allow-missing (1): [567323]" in out
        assert "ALLOWED by --allow-missing (1): [745175]" in out
        assert "stale" not in out

    def test_the_dry_run_writes_nothing(self, capsys):
        conn = _NamedStub(
            missing_by_season={2019: [567323]}, season_of={567323: 2019}, counts=self._COUNTS
        )
        code = _run(retire.run(conn, [2019], dry_run=True, allow_missing=[567323]))
        assert code == retire.EXIT_OK
        assert conn.executed() == []
        assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
        out = capsys.readouterr().out
        assert "dry run" in out and "ALLOWED by --allow-missing (1): [567323]" in out

    def test_no_names_means_no_named_games_query(self, capsys):
        conn = _NamedStub(missing_by_season={2024: []}, season_of={}, counts=self._COUNTS)
        code = _run(retire.run(conn, [2024], dry_run=True))
        assert code == retire.EXIT_OK
        assert conn.named_args == []
        assert "WARNING" not in capsys.readouterr().out

    def test_the_cli_accepts_game_pks(self):
        base = ["--seasons", "2019", "--dsn", "postgresql://x"]
        assert retire.parse_args(base).allow_missing == []
        args = retire.parse_args([*base, "--allow-missing", "567323", "745175"])
        assert args.allow_missing == [567323, 745175]

    @pytest.mark.parametrize("values", [[], ["abc"], ["0"], ["-5"], ["567323", "1.5"]], ids=str)
    def test_the_cli_rejects_a_value_that_is_not_a_game_pk(self, values):
        with pytest.raises(SystemExit):
            retire.parse_args(
                ["--seasons", "2019", "--dsn", "postgresql://x", "--allow-missing", *values]
            )


# ===========================================================================
# The re-score of stored reports
# ===========================================================================


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
        """SIM-555 (the re-score of 2026-09-29): in a fully re-loaded season a game
        with no bp: row is one the event matcher declines, and its old prices likely
        belong to another game. --drop-not-reloaded drops all its records."""
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
        bt = rescore.bt
        assert rescore.INCOMPLETE_THREE_WAY_LAST_SQL == bt.INCOMPLETE_THREE_WAY_LAST_SQL
        flat = " ".join(rescore.GRADED_GAME_SQL.split())
        assert (
            "ORDER BY game_pk, market_type, "
            f"{bt.INCOMPLETE_THREE_WAY_LAST_SQL}, "
            "array_position($3::varchar[], book) NULLS LAST, fetched_at DESC"
        ) in flat
        # A prop has no tie: its read keeps the plain order.
        assert bt.INCOMPLETE_THREE_WAY_LAST_SQL not in rescore.GRADED_PROP_SQL

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


# ===========================================================================
# The probe: the sample, the gate math, one game through the provider
# ===========================================================================


def _row(book: str, market: str, *, lt: str = "closing", refusal: str | None = None, **cols: Any):
    row = {"book": book, "market_type": market, "line_type": lt, "refusal": refusal}
    row.update(cols)
    return row


def _rl(book, hs, hml, as_, aml, *, market="f5_runline", lt="closing", refusal=None):
    return _row(
        book,
        market,
        lt=lt,
        refusal=refusal,
        home_spread=hs,
        home_spread_ml=hml,
        away_spread=as_,
        away_spread_ml=aml,
    )


def _game(game_pk: int, offers: list[dict[str, Any]], *, season: int = 2025, date="2025-06-01"):
    return {
        "game_pk": game_pk,
        "season": season,
        "month": 6,
        "game_date": date,
        "offers": offers,
        "f5_exclusions": [],
    }


def _offer(market, line_type, rows, error=None):
    return {"market": market, "line_type": line_type, "rows": rows, "error": error}


class TestProbeSample:
    def test_the_sample_spreads_over_seasons_and_months(self):
        games = [
            probe.ProbeGame(s * 1000 + m * 10 + i, s, m)
            for s in (2023, 2024, 2025)
            for m in (4, 5, 6, 7)
            for i in range(5)
        ]
        picked = probe.stratified_sample(games, 24, seed=1)
        assert len(picked) == 24 and len({g.game_pk for g in picked}) == 24
        by_season = Counter(g.season for g in picked)
        assert by_season == {2023: 8, 2024: 8, 2025: 8}
        by_cell = Counter((g.season, g.month) for g in picked)
        assert set(by_cell.values()) == {2}
        assert probe.stratified_sample(games, 24, seed=1) == picked
        assert len(probe.stratified_sample(games, 1000)) == len(games)


class TestProbeGate:
    def test_gate_a_needs_a_preferred_book_kept_at_the_close(self):
        ok = _game(
            1,
            [
                _offer(
                    "moneyline",
                    "closing",
                    [
                        _row("bp:12", "moneyline", refusal="late_closing_stamp"),
                        _row("bp:10", "moneyline"),
                    ],
                )
            ],
        )
        bad = _game(
            2,
            [
                _offer(
                    "moneyline", "closing", [_row("bp:0", "moneyline"), _row("bp:68", "moneyline")]
                ),
                _offer(
                    "f5_moneyline", "closing", [_row("bp:0", "f5_moneyline")]
                ),  # three-way: not gated
                _offer("total", "closing", []),  # not offered
            ],
        )
        gate = probe.gate_graded_coverage([ok, bad])
        assert gate["offers"] == 2 and gate["covered"] == 1 and not gate["pass"]
        assert gate["failures"][0]["game_pk"] == 2
        assert gate["failures"][0]["closing_books"] == ["bp:0", "bp:68"]
        assert probe.gate_graded_coverage([ok])["pass"]

    def test_a_side_far_from_the_median_is_flagged(self):
        rows = [
            _rl("bp:10", -1.5, 200, 1.5, -250),
            _rl("bp:13", -1.5, 210, 1.5, -260),
            _rl("bp:19", -1.5, 190, 1.5, -240),
            _rl("bp:24", -1.5, 600, 1.5, -900),  # first-inning prices at the first-five line
            _rl("bp:33", -0.5, 900, 0.5, -1500),  # alone at its line: no peers, no check
        ]
        far = probe.median_deviation("f5_runline", rows)
        assert far == {"bp:24": ["home", "away"]}

    def test_the_three_way_compares_like_with_like(self):
        rows = [
            _row("bp:10", "f5_moneyline", home_ml=-140, away_ml=120, draw_ml=None),  # two-way
            _row("bp:13", "f5_moneyline", home_ml=120, away_ml=150, draw_ml=450),
            _row("bp:19", "f5_moneyline", home_ml=125, away_ml=145, draw_ml=440),
            _row("bp:24", "f5_moneyline", home_ml=118, away_ml=155, draw_ml=460),
        ]
        assert probe.median_deviation("f5_moneyline", rows) == {}

    def test_a_row_copying_its_own_first_inning_row_is_a_twin(self):
        f5 = _rl("bp:12", 0.5, -380, 0.5, -500)
        f1 = _rl("bp:12", 0.5, -360, 0.5, -475, market="f1_runline")
        assert probe.rows_are_twins("f5_runline", f5, f1)
        assert not probe.rows_are_twins(
            "f5_runline", f5, _rl("bp:12", 0.5, -200, 0.5, -500, market="f1_runline")
        )
        assert not probe.rows_are_twins(
            "f5_runline", f5, _rl("bp:12", 1.5, -380, 0.5, -500, market="f1_runline")
        )
        tie5 = _row("bp:19", "f5_moneyline", home_ml=130, away_ml=150, draw_ml=450)
        tie1 = _row("bp:19", "f1_moneyline", home_ml=130, away_ml=150, draw_ml=-110)
        assert not probe.rows_are_twins("f5_moneyline", tie5, tie1)  # the tie rules the copy out

    @staticmethod
    def _f5_game(i: int, *, dk_flagged=False, fd_flagged=False, date="2024-08-14", opener=None):
        """One game's first-five run line: four books near -1.5 +200, a flag where asked."""
        f5 = [
            # SIM-555: a far-off price, not a guard refusal: gate (b) grades the
            # rows the loader stores, and the guard's refusals are never stored.
            _rl("bp:10", -1.5, 700, 1.5, -1000)
            if fd_flagged
            else _rl("bp:10", -1.5, 200, 1.5, -250),
            _rl("bp:13", -1.5, 210, 1.5, -260),
            _rl("bp:19", -1.5, 190, 1.5, -240),
            _rl("bp:12", -1.5, 205, 1.5, -255),
        ]
        if dk_flagged:
            f5[3] = _rl("bp:12", -1.5, 700, 1.5, -1000)
        offers = [_offer("f5_runline", "closing", f5)]
        if opener is not None:
            offers.append(_offer("f5_runline", "opening", [opener]))
        return _game(i, offers, season=int(date[:4]), date=date)

    def test_gates_b_and_c(self):
        # 1% for FanDuel, which is not on the list: (b) and (c) fail, (c) names the fix.
        games = [self._f5_game(i, fd_flagged=(i == 0)) for i in range(100)]
        gate = probe.gate_first_five(games)
        assert not gate["pass_b"] and not gate["pass_c"]
        assert gate["failing_outside_excluded"] == [("f5_runline", "closing", "bp:10")]
        assert gate["fixes"] == [
            "add bp:10 (FanDuel) to F5_EXCLUDED_BOOKS, dated on or before 2024-08-14: "
            "1 of its 100 f5_runline closing rows are first-inning-shaped"
        ]

        # 1 of 300 is below 0.5% for a book off the list: both pass.
        games = [self._f5_game(i, fd_flagged=(i == 0)) for i in range(300)]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and gate["pass_c"]
        assert gate["verdict_b"] == gate["verdict_c"] == "PASS" and gate["fixes"] == []

    def test_gate_b_skips_the_rows_the_guard_refuses(self):
        # SIM-555: FanDuel's first-five row is refused by the load guard on
        # every game, so none is stored: gate (b) passes and the refusals are
        # reported beside it, not graded.
        games = [self._f5_game(i) for i in range(100)]
        for g in games:
            g["offers"][0]["rows"][0]["refusal"] = "f5_win_or_tie"
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and gate["pass_c"]
        assert gate["guard_refused"] == [
            {"market": "f5_runline", "line_type": "closing", "book": "bp:10", "n": 100}
        ]
        assert all(e["book"] != "bp:10" for e in gate["table"])

    def test_gate_c_catches_a_wrong_exclusion_date(self):
        # DraftKings keeps first-inning-shaped rows on 2024 games, before its
        # 2025-03-01 date: the loader would write them, so the date is wrong.
        games = [self._f5_game(i, dk_flagged=(i < 10)) for i in range(100)]
        gate = probe.gate_first_five(games)
        dk = next(e for e in gate["table"] if e["book"] == "bp:12")
        assert (dk["line_type"], dk["flagged"], dk["n"], dk["fails"]) == ("closing", 10, 100, True)
        assert dk["reasons"] == {"median_dev": 10}
        assert not gate["pass_b"] and not gate["pass_c"]
        assert gate["failing_outside_excluded"] == []
        assert gate["flagged_on_the_list"] == [("f5_runline", "closing", "bp:12")]
        assert gate["fixes"] == [
            "move bp:12 (DraftKings)'s date from 2025-03-01 to 2024-08-14: 10 of its 100 "
            "f5_runline closing rows are first-inning-shaped before 2025-03-01"
        ]
        assert "move bp:12 (DraftKings)'s date" in probe.format_summary(probe.summarise(games))

    def test_gate_c_fails_on_one_flagged_row_of_a_listed_book(self):
        # One flagged DraftKings row in 300 is below the (b) share, but a listed
        # book's flagged row before its date still says the date may be wrong.
        games = [self._f5_game(i, dk_flagged=(i == 0)) for i in range(300)]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and not gate["pass_c"]
        assert gate["flagged_on_the_list"] == [("f5_runline", "closing", "bp:12")]
        assert gate["fixes"][0].startswith("move bp:12 (DraftKings)'s date from 2025-03-01")

    def test_the_opening_rows_do_not_dilute_the_closing_share(self):
        # bet365 has 1 flagged closing row in 150 (0.67%) and opens every game.
        def game(i: int):
            g = self._f5_game(i, opener=_rl("bp:24", -1.5, 195, 1.5, -245, lt="opening"))
            rows = g["offers"][0]["rows"]
            rows[3] = _rl("bp:24", -1.5, 700 if i == 0 else 205, 1.5, -1000 if i == 0 else -255)
            return g

        gate = probe.gate_first_five([game(i) for i in range(150)])
        by_lt = {e["line_type"]: e for e in gate["table"] if e["book"] == "bp:24"}
        assert (by_lt["closing"]["flagged"], by_lt["closing"]["n"]) == (1, 150)
        assert by_lt["closing"]["fails"]
        assert (by_lt["opening"]["flagged"], by_lt["opening"]["n"]) == (0, 150)
        assert not by_lt["opening"]["fails"]
        assert not gate["pass_b"]
        assert gate["failing"] == [("f5_runline", "closing", "bp:24")]

    def test_a_failed_read_makes_the_gates_unknown(self):
        clean = [self._f5_game(i) for i in range(10)]
        # Game 99's first-inning read failed, so its first-five rows cannot be checked.
        broken = self._f5_game(99)
        broken["offers"].append(_offer("f1_runline", "closing", [], error="OSError: down"))
        gate = probe.gate_first_five([*clean, broken])
        assert gate["verdict_b"] == "UNKNOWN" and not gate["pass_b"]
        assert gate["verdict_c"] == "UNKNOWN" and not gate["pass_c"]
        assert gate["unknown"] == [
            {
                "game_pk": 99,
                "market": "f5_runline",
                "line_type": "closing",
                "error": "OSError: down",
            }
        ]
        # A failure outranks a failed read.
        flagged = self._f5_game(100, fd_flagged=True)
        gate = probe.gate_first_five([*clean, broken, flagged])
        assert gate["verdict_b"] == "FAIL"
        # A game with no vendor event is unknown on every market it read.
        no_event = _game(101, [_offer("moneyline", "closing", []), _offer("total", "closing", [])])
        no_event["game_error"] = "no vendor event"
        a = probe.gate_graded_coverage([no_event])
        assert (a["verdict"], a["n_unknown"], a["offers"]) == ("UNKNOWN", 2, 2)
        assert {u["error"] for u in a["unknown"]} == {"no vendor event"}

    def test_gate_a_tells_a_failure_from_a_failed_read(self):
        failing = _game(
            1,
            [
                _offer("total", "opening", [], error="OSError: down"),
                _offer("total", "closing", [_row("bp:0", "total")]),
            ],
        )
        covered = _game(
            2, [_offer("total", "closing", [_row("bp:12", "total")], error="page 2 failed")]
        )
        not_offered = _game(3, [_offer("total", "closing", [])])
        gate = probe.gate_graded_coverage([failing, covered, not_offered])
        # A clean closing read with rows but no preferred book is a real failure.
        assert (gate["n_failures"], gate["n_unknown"], gate["covered"]) == (1, 0, 1)
        assert gate["offers"] == 2 and gate["verdict"] == "FAIL"

    def test_the_twin_check_reads_the_first_inning_offer(self):
        g = _game(
            5,
            [
                _offer("f5_runline", "closing", [_rl("bp:12", 0.5, -380, 0.5, -500)]),
                _offer(
                    "f1_runline",
                    "closing",
                    [_rl("bp:12", 0.5, -370, 0.5, -490, market="f1_runline")],
                ),
            ],
        )
        flags = probe.first_inning_flags(g)
        assert [(f["book"], f["flags"]) for f in flags] == [("bp:12", ["twin_f1"])]

    def test_the_stamp_summary(self):
        def ml(book, minutes, stamp):
            return _row(book, "moneyline", stamp_minus_start_min=minutes, book_line_at=stamp)

        games = [
            _game(
                1,
                [
                    _offer(
                        "moneyline",
                        "closing",
                        [ml("bp:12", 2.0, "a"), ml("bp:10", 2.0, "a"), ml("bp:0", 3.0, "b")],
                    )
                ],
                season=2025,
            ),
            _game(
                2,
                [_offer("moneyline", "closing", [ml("bp:12", -30.0, "c"), ml("bp:10", -9.0, "d")])],
                season=2024,
            ),
        ]
        s = probe.stamp_summary(games)
        assert s["2025"]["newest_max"] == 3.0 and s["2025"]["one_stamp_share"] == 1.0
        assert s["2024"]["newest_median"] == -9.0 and s["2024"]["after_start_share"] == 0.0

    def test_the_summary_prints_the_gate(self):
        g = _game(
            1,
            [
                _offer("moneyline", "opening", [_row("bp:10", "moneyline")]),
                _offer(
                    "moneyline", "closing", [_row("bp:12", "moneyline"), _row("bp:0", "moneyline")]
                ),
                _offer(
                    "f5_runline",
                    "closing",
                    [_rl("bp:10", -1.5, 200, 1.5, -250, refusal="spread_size")],
                ),
            ],
        )
        summary = probe.summarise([g])
        assert summary["markets"]["moneyline"]["graded_book"] == {"bp:12": 1}
        assert summary["markets"]["moneyline"]["opener_coverage"] == 1.0
        assert summary["refusals"]["refused"] == 1
        text = probe.format_summary(summary)
        assert "THE GATE" in text and "(a)" in text and "(c)" in text
        json.dumps(summary)

    def test_exclusion_kinds(self):
        assert probe.exclusion_kind("bp:12", "excluded from 2025-03-01") == "dated"
        assert (
            probe.exclusion_kind("bp:10", "its first-five lines copy its own first-inning lines")
            == "twin"
        )
        assert probe.exclusion_kind("opener", "anything") == "opener_twin"


# --------------------------------------------------------------------------- one game, stubbed vendor

_T = "2025-06-01 23:08:00"


def _line(line, cost, updated=_T, **extra):
    ln = {
        "line": line,
        "cost": cost,
        "updated": updated,
        "main": True,
        "active": True,
        "is_off": False,
    }
    ln.update(extra)
    return ln


def _sel(participant, books, opener=None, selection=""):
    sel = {
        "participant": participant,
        "selection": selection,
        "label": selection.title(),
        "books": [{"id": b, "lines": [ln]} for b, ln in books.items()],
    }
    if opener is not None:
        book_id, line, cost = opener
        sel["opening_line"] = {
            "book_id": book_id,
            "line": line,
            "cost": cost,
            "created": "2025-05-31 12:00:00",
        }
    return sel


def _payload(*sels):
    return {"offers": [{"selections": list(sels), "participants": []}]}


_PAYLOADS = {
    122: _payload(  # the moneyline
        _sel(
            "SD",
            {0: _line(None, -145), 12: _line(None, -150), 10: _line(None, -148)},
            opener=(10, None, -140),
        ),
        _sel(
            "BAL",
            {0: _line(None, 125), 12: _line(None, 130), 10: _line(None, 126)},
            opener=(10, None, 120),
        ),
    ),
    283: _payload(  # the first-five run line; DraftKings is on the dated list from 2025-03-01
        _sel("SD", {12: _line(0.5, -380), 19: _line(-0.5, 110), 13: _line(-0.5, 105)}),
        _sel("BAL", {12: _line(0.5, -500), 19: _line(0.5, -130), 13: _line(0.5, -125)}),
    ),
    282: _payload(  # the first-inning run line
        _sel("SD", {12: _line(0.5, -360), 19: _line(-0.5, 250)}),
        _sel("BAL", {12: _line(0.5, -475), 19: _line(0.5, -300)}),
    ),
}


class _StubVendorProbe(probe.ProbeProvider):
    """The probe's provider with the vendor stubbed below the rate limit.

    ``fail_markets``: the offers read of these market ids raises;
    ``fail_pages``: page 2 of these markets raises (their payload says two pages).
    Any read without a ``market_id`` (the event list, the player lookup) raises.
    """

    def __init__(
        self,
        *,
        payloads: dict[int, Any] | None = None,
        fail_markets: tuple[int, ...] = (),
        fail_pages: tuple[int, ...] = (),
        **kw: Any,
    ) -> None:
        self.waits: list[float] = []
        self.payloads = _PAYLOADS if payloads is None else payloads
        self.fail_markets = set(fail_markets)
        self.fail_pages = set(fail_pages)
        super().__init__(
            api_key="test-key", rate=2.0, sleep=self.waits.append, rate_clock=lambda: 100.0, **kw
        )

    def _resolve_event(self, game_pk):  # type: ignore[override]
        return {"id": 97000, "home": "SD", "visitor": "BAL"}

    def _resolve_game_meta(self, game_pk):  # type: ignore[override]
        return ("2025-06-01", "San Diego Padres", "Baltimore Orioles", datetime(2025, 6, 1, 23, 5))

    def _http_get_json(self, url, headers=None):  # type: ignore[override]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        market = int(query["market_id"][0])
        if market in self.fail_markets:
            raise OSError(f"the vendor is down for market {market}")
        payload = self.payloads.get(market, {"offers": []})
        if market in self.fail_pages:
            if int(query.get("page", ["1"])[0]) > 1:
                raise OSError("page 2 timed out")
            return {**payload, "_pagination": {"total_pages": 2}}
        return payload


class _NoEventProbe(_StubVendorProbe):
    """The stubbed vendor whose event lookup fails (the provider then reads no market)."""

    _resolve_event = probe.ProbeProvider._resolve_event


class TestProbeRead:
    def test_one_game_through_the_provider(self):
        prov = _StubVendorProbe()
        game = probe.ProbeGame(777, 2025, 6, "2025-06-01")
        record = probe.collect_game(prov, game, markets=("moneyline", "f5_runline", "f1_runline"))
        offers = {(o["market"], o["line_type"]): o for o in record["offers"]}
        closing = {r["book"]: r for r in offers[("moneyline", "closing")]["rows"]}
        assert set(closing) == {"bp:0", "bp:12", "bp:10"}
        assert closing["bp:12"]["home_ml"] == -150 and closing["bp:12"]["away_ml"] == 130
        assert closing["bp:12"]["stamp_minus_start_min"] == pytest.approx(3.0)
        assert closing["bp:12"].get("refusal") is None
        opening = offers[("moneyline", "opening")]["rows"]
        assert [r["book"] for r in opening] == ["bp:10"]
        # DraftKings' first-five entry is dropped by the dated rule and recorded.
        f5_books = {r["book"] for r in offers[("f5_runline", "closing")]["rows"]}
        assert "bp:12" not in f5_books and {"bp:19", "bp:13"} <= f5_books
        kinds = {(e["market"], e["who"], e["kind"]) for e in record["f5_exclusions"]}
        assert ("f5_runline", "bp:12", "dated") in kinds
        # One vendor read per market: the opening and the closing share the payload.
        assert prov.vendor_reads == 3
        assert prov.waits == [0.5, 0.5]
        assert all(o["error"] is None for o in record["offers"])
        assert record["game_error"] is None
        json.dumps(record)

    def test_a_dated_exclusion_counts_only_a_book_that_quotes_the_market(self):
        # DraftKings lists no first-five run line: it lost no row, so no exclusion.
        payloads = dict(_PAYLOADS)
        payloads[283] = _payload(
            _sel("SD", {19: _line(-0.5, 110), 13: _line(-0.5, 105)}),
            _sel("BAL", {19: _line(0.5, -130), 13: _line(0.5, -125)}),
        )
        prov = _StubVendorProbe(payloads=payloads)
        record = probe.collect_game(
            prov, probe.ProbeGame(778, 2025, 6, "2025-06-01"), markets=("f5_runline", "f1_runline")
        )
        assert [e for e in record["f5_exclusions"] if e["kind"] == "dated"] == []
        # A line DraftKings does not take (is_off) is no row to lose either.
        payloads[283] = _payload(
            _sel("SD", {12: _line(0.5, -380, is_off=True), 19: _line(-0.5, 110)}),
            _sel("BAL", {12: _line(0.5, -500), 19: _line(0.5, -130)}),
        )
        prov = _StubVendorProbe(payloads=payloads)
        record = probe.collect_game(
            prov, probe.ProbeGame(779, 2025, 6, "2025-06-01"), markets=("f5_runline",)
        )
        assert [e for e in record["f5_exclusions"] if e["kind"] == "dated"] == []

    def test_quoting_books(self):
        sides = {
            "home": _sel("SD", {12: _line(0.5, -380), 19: _line(-0.5, 110)}, opener=(13, 0.5, -1)),
            "away": _sel("BAL", {12: _line(0.5, -500), 10: _line(0.5, -130)}, opener=(13, 0.5, -1)),
        }
        # 19 and 10 each quote one side; the shared opener 13 has a row to lose.
        assert probe.quoting_books(sides) == frozenset({12, 13})
        sides["away"]["opening_line"]["book_id"] = 10
        assert probe.quoting_books(sides) == frozenset({12})

    def test_a_failed_first_inning_read_is_an_error_not_an_empty_market(self):
        prov = _StubVendorProbe(fail_markets=(282,))
        record = probe.collect_game(
            prov, probe.ProbeGame(780, 2025, 6, "2025-06-01"), markets=("f5_runline", "f1_runline")
        )
        offers = {(o["market"], o["line_type"]): o for o in record["offers"]}
        f5 = offers[("f5_runline", "closing")]
        # The provider drops the first-five rows without the twin check; the probe says why.
        assert f5["rows"] == []
        assert "first-inning offers read (market 282) failed" in f5["error"]
        assert "the vendor is down" in offers[("f1_runline", "closing")]["error"]
        gate = probe.gate_first_five([record])
        assert gate["verdict_b"] == "UNKNOWN" and not gate["pass_b"]
        assert gate["verdict_c"] == "UNKNOWN" and not gate["pass_c"]
        assert gate["unknown_games"] == [780] and gate["n_unknown"] == 2  # opening + closing

    def test_a_failed_offers_read_leaves_gate_a_unknown(self):
        prov = _StubVendorProbe(fail_markets=(122,))
        record = probe.collect_game(
            prov, probe.ProbeGame(781, 2025, 6, "2025-06-01"), markets=("moneyline",)
        )
        closing = next(o for o in record["offers"] if o["line_type"] == "closing")
        assert closing["rows"] == [] and "offers read (market 122) failed" in closing["error"]
        gate = probe.gate_graded_coverage([record])
        assert gate["verdict"] == "UNKNOWN" and not gate["pass"]
        assert gate["n_unknown"] == 1 and gate["n_failures"] == 0
        assert gate["unknown_games"] == [781]
        summary = probe.summarise([record])
        assert summary["read_errors"]["offers_errored"] == 2
        text = probe.format_summary(summary)
        assert "(a) every two-way offer quoted by a preferred book: UNKNOWN" in text
        assert "read again: --game-pks 781" in text

    def test_a_partial_offers_read_is_an_error(self):
        prov = _StubVendorProbe(fail_pages=(122,))
        record = probe.collect_game(
            prov, probe.ProbeGame(782, 2025, 6, "2025-06-01"), markets=("moneyline",)
        )
        closing = next(o for o in record["offers"] if o["line_type"] == "closing")
        assert "later offers page (market 122) failed" in closing["error"]
        # A preferred book's kept row still covers the offer.
        gate = probe.gate_graded_coverage([record])
        assert gate["verdict"] == "PASS" and gate["covered"] == 1

    def test_a_game_with_no_vendor_event_is_unknown(self):
        prov = _NoEventProbe()
        record = probe.collect_game(
            prov, probe.ProbeGame(783, 2025, 6, "2025-06-01"), markets=("moneyline", "total")
        )
        assert record["game_error"].startswith("no vendor event")
        assert all(o["rows"] == [] for o in record["offers"])
        gate = probe.gate_graded_coverage([record])
        assert gate["verdict"] == "UNKNOWN" and gate["n_unknown"] == 2
        assert probe.read_error_summary([record])["games_without_event"] == [783]

    def test_a_failed_player_lookup_is_an_error(self):
        # The stubbed vendor has no player lookup: it raises.
        prov = _StubVendorProbe()
        record = probe.collect_game(
            prov, probe.ProbeGame(784, 2025, 6, "2025-06-01"), markets=(), prop_player=5
        )
        assert [o["market"] for o in record["offers"]] == ["strikeouts", "strikeouts"]
        assert all("player lookup (player 5) failed" in o["error"] for o in record["offers"])
        gate = probe.gate_graded_coverage([record])
        assert gate["verdict"] == "UNKNOWN"

    def test_row_record_reads_the_stamp_and_the_verdict(self):
        start = datetime(2025, 6, 1, 23, 5, tzinfo=UTC)
        row = {
            "book": "bp:12",
            "book_id": 12,
            "market_type": "moneyline",
            "line_type": "closing",
            "home_ml": -150,
            "away_ml": 130,
            "book_line_at": datetime(2025, 6, 1, 23, 45, tzinfo=UTC),
            "scheduled_start": start,
        }
        from pipeline.odds_row_guard import check_row

        rec = probe.row_record(row, check_row(row))
        assert rec["stamp_minus_start_min"] == pytest.approx(40.0)
        assert rec["refusal"] == "late_closing_stamp"
        assert rec["book_line_at"].startswith("2025-06-01T23:45")


# ===========================================================================
# The probe: the review fixes of 2026-09-28
# ===========================================================================


def _ml3(book: str, home: float, away: float, draw: float | None, market="f1_moneyline"):
    return _row(book, market, home_ml=home, away_ml=away, draw_ml=draw)


def _dk_dated_game(i: int, *, flagged: bool, market: str = "f5_runline") -> dict[str, Any]:
    """A 2025 game whose DraftKings first-five rows the dated rule drops.

    Three kept books near -1.5 +200. DraftKings' dropped row sits with them
    (a real first-five line) or at a first-inning price (``flagged``).
    """
    kept = [
        _rl("bp:10", -1.5, 200, 1.5, -250, market=market),
        _rl("bp:13", -1.5, 210, 1.5, -260, market=market),
        _rl("bp:19", -1.5, 190, 1.5, -240, market=market),
    ]
    dk = _rl("bp:12", -1.5, 700 if flagged else 205, 1.5, -1000 if flagged else -255, market=market)
    dk["dated_excluded"] = True
    offer = _offer(market, "closing", kept)
    offer["dated_rows"] = [dk]
    return _game(i, [offer], season=2025, date="2025-06-01")


class TestProbeReviewFixes:
    def test_the_graded_book_puts_a_tie_less_three_way_row_last(self):
        """contracts #2: the backtest grades a three-way row with no tie after every
        row with one; the probe's graded book now does the same."""
        rows = [_ml3("bp:12", 290, 300, None), _ml3("bp:10", 300, 290, -125)]
        assert probe.graded_book(rows) == "bp:10"
        assert probe.graded_book(rows, "f1_moneyline") == "bp:10"
        # With no book listing the tie, the list decides among the tie-less rows.
        assert probe.graded_book([_ml3("bp:10", 300, 290, None), rows[0]]) == "bp:12"
        # A two-way market keeps the plain list order.
        two_way = [_row("bp:12", "moneyline", home_ml=-150), _row("bp:10", "moneyline")]
        assert probe.graded_book(two_way) == "bp:12"
        # The market summary counts the same book.
        game = _game(1, [_offer("f1_moneyline", "closing", rows)])
        assert probe.market_summary([game])["f1_moneyline"]["graded_book"] == {"bp:10": 1}

    def test_only_a_sportsbook_is_graded(self):
        rows = [_row("bp:0", "moneyline"), _row("bp:45", "moneyline"), _row("bp:999", "moneyline")]
        assert probe.graded_book(rows) is None
        assert probe.graded_book([*rows, _row("bp:27", "moneyline")]) == "bp:27"

    def test_dated_rows_never_count_as_loaded(self):
        game = _dk_dated_game(1, flagged=False)
        summary = probe.market_summary([game])["f5_runline"]
        assert "bp:12" not in summary["closing_rows_by_book"]
        # The graded book is the first book on the list among the KEPT rows.
        kept = {row["book"] for row in game["offers"][0]["rows"]}
        graded = next(label for label in graded_book_labels() if label in kept)
        assert summary["graded_book"] == {graded: 1}
        # The kept books' median never takes a dropped row as a peer.
        rows = game["offers"][0]["rows"] + game["offers"][0]["dated_rows"]
        assert probe._kept(rows) == game["offers"][0]["rows"]

    def test_gate_c_fails_when_the_dated_rows_do_not_bear_the_exclusion_out(self):
        """F5-2: the probe grades the rows the dated rule drops. DraftKings' 2025
        first-five rows all sit with the other books: the exclusion is not needed."""
        games = [_dk_dated_game(i, flagged=False) for i in range(100)]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and not gate["pass_c"] and gate["verdict_c"] == "FAIL"
        assert gate["dated_not_needed"] == [("f5_runline", "closing", "bp:12")]
        (dk,) = gate["dated_table"]
        assert (dk["book"], dk["n"], dk["flagged"]) == ("bp:12", 100, 0)
        assert gate["fixes"] == [
            "the dated exclusion of bp:12 (DraftKings) is not borne out on f5_runline: 0 of "
            "its 100 closing rows from 2025-03-01 are first-inning-shaped (below 0.5%); take "
            "f5_runline out of its F5_EXCLUDED_MARKETS, or check those rows by hand"
        ]
        assert "dated rows: f5_runline" in probe.format_summary(probe.summarise(games))
        # The (b) table never holds a dropped row.
        assert all(e["book"] != "bp:12" for e in gate["table"])

    def test_gate_c_passes_when_the_dated_rows_are_first_inning_shaped(self):
        games = [_dk_dated_game(i, flagged=True) for i in range(100)]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and gate["pass_c"] and gate["fixes"] == []
        (dk,) = gate["dated_table"]
        assert (dk["flagged"], dk["reasons"]) == (100, {"median_dev": 100})

    def test_the_probe_keeps_the_dated_rows_apart(self):
        """F5-2 through the provider: DraftKings' real 2025 first-five line (not a
        twin) reaches the probe as a dated row, never as a loaded row."""
        payloads = dict(_PAYLOADS)
        payloads[283] = _payload(
            _sel("SD", {12: _line(-0.5, 105), 19: _line(-0.5, 110), 13: _line(-0.5, 105)}),
            _sel("BAL", {12: _line(0.5, -125), 19: _line(0.5, -130), 13: _line(0.5, -125)}),
        )
        prov = _StubVendorProbe(payloads=payloads)
        record = probe.collect_game(
            prov, probe.ProbeGame(790, 2025, 6, "2025-06-01"), markets=("f5_runline", "f1_runline")
        )
        f5 = {o["line_type"]: o for o in record["offers"] if o["market"] == "f5_runline"}
        assert "bp:12" not in {r["book"] for r in f5["closing"]["rows"]}
        (dated,) = f5["closing"]["dated_rows"]
        assert dated["book"] == "bp:12" and dated["dated_excluded"] is True
        assert (dated["home_spread_ml"], dated["away_spread_ml"]) == (105, -125)
        kinds = {(e["who"], e["kind"]) for e in record["f5_exclusions"]}
        assert ("bp:12", "dated") in kinds and ("bp:12", "twin") not in kinds
        # The market summary never counts the dropped row as loaded.
        summary = probe.market_summary([record])["f5_runline"]
        assert "bp:12" not in summary["closing_rows_by_book"]
        json.dumps(record)

    def test_a_twin_exclusion_carries_its_prices_and_its_peers(self):
        """F5-1: each twin exclusion records the book's first-five prices, its own
        first-inning prices and the other sportsbooks' median, so the census can
        see a false twin."""
        payloads = dict(_PAYLOADS)
        payloads[283] = _payload(
            _sel("SD", {13: _line(0.5, -380), 19: _line(0.5, -150), 10: _line(0.5, -160)}),
            _sel("BAL", {13: _line(0.5, -500), 19: _line(0.5, -180), 10: _line(0.5, -170)}),
        )
        payloads[282] = _payload(
            _sel("SD", {13: _line(0.5, -370)}),
            _sel("BAL", {13: _line(0.5, -490)}),
        )
        prov = _StubVendorProbe(payloads=payloads)
        record = probe.collect_game(
            prov, probe.ProbeGame(791, 2025, 6, "2025-06-01"), markets=("f5_runline",)
        )
        (twin,) = [e for e in record["f5_exclusions"] if e["kind"] == "twin"]
        assert twin["who"] == "bp:13"
        home = twin["sides"]["home"]
        assert (home["price"], home["line"], home["f1_price"], home["n_peers"]) == (
            -380,
            0.5,
            -370,
            2,
        )
        assert home["peer_median"] == pytest.approx((150 / 250 + 160 / 260) / 2)
        assert home["deviation"] == pytest.approx(380 / 480 - home["peer_median"])
        (entry,) = probe.twin_exclusions([record])
        assert entry["book"] == "bp:13" and entry["market"] == "f5_runline"
        text = probe.format_summary(probe.summarise([record]))
        assert "the twins, each side's price" in text and "away -500.0 (f1 -490.0" in text
        json.dumps(record)

    def test_the_row_level_twin_needs_identical_moneyline_prices(self):
        """F5-1: the probe's own twin check follows the provider's per-market
        tolerance: identical prices on the moneyline, 0.03 on the run line."""
        f5 = _ml3("bp:13", -120, 100, None, market="f5_moneyline")
        assert not probe.rows_are_twins("f5_moneyline", f5, _ml3("bp:13", -110, -110, None))
        assert probe.rows_are_twins(
            "f5_moneyline",
            _ml3("bp:13", -110, -110, None, market="f5_moneyline"),
            _ml3("bp:13", -110, -110, None),
        )
        # The run line with the same gaps is still a twin.
        assert probe.rows_are_twins(
            "f5_runline",
            _rl("bp:13", -0.5, -120, 0.5, 100),
            _rl("bp:13", -0.5, -110, 0.5, -110, market="f1_runline"),
        )


# ===========================================================================
# The probe: the census fixes of 2026-09-28 (per-market dated exclusion,
# bettable books only, the two new guard rules, the regrade)
# ===========================================================================


def _f5_ml(book: str, home: float, away: float, draw: float | None):
    return _ml3(book, home, away, draw, market="f5_moneyline")


class TestProbeCensusFixes:
    def test_the_dated_rule_reads_each_book_s_markets(self):
        assert probe.dated_rule_covers("bp:12", "f5_runline")
        assert probe.dated_rule_covers("bp:12", "f5_total")
        assert not probe.dated_rule_covers("bp:12", "f5_moneyline")
        assert not probe.dated_rule_covers("bp:10", "f5_runline")  # off the list
        assert not probe.dated_rule_covers("bp:12", "moneyline")  # not a first-five market
        assert probe.excluded_listing() == {"bp:12": "2025-03-01 on f5_runline, f5_total"}
        params = probe.rule_params()
        assert params["f5_excluded_markets"] == {"12": ["f5_runline", "f5_total"]}
        assert params["three_way_sum_min"] == THREE_WAY_SUM_MIN
        assert params["f5_total_line_max"] == F5_TOTAL_LINE_MAX

    def test_the_new_guard_rules_name_a_first_inning_shape(self):
        from pipeline.odds_row_guard import RULES

        shapes = probe.FIRST_INNING_SHAPE_RULES
        expected = {
            "f5_win_or_tie",
            "f5_tie_price",
            "three_way_sum_below_one",
            "f5_total_first_inning_line",
        }
        assert shapes == expected
        assert shapes.issubset(RULES)

    def test_gates_b_and_c_grade_the_bettable_books_only(self):
        """The blend and a prediction market post first-inning prices on one game:
        both are stored but never graded or offered, so they are listed apart and
        the gate passes."""
        games = [TestProbeGate._f5_game(i) for i in range(100)]
        games[0]["offers"][0]["rows"] += [
            _rl("bp:0", -1.5, 700, 1.5, -1000),
            _rl("bp:68", -1.5, 700, 1.5, -1000),
        ]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and gate["pass_c"] and gate["fixes"] == []
        assert all(probe.is_bettable(e["book"]) for e in gate["table"])
        listed = {
            (e["market"], e["line_type"], e["book"]): (e["flagged"], e["n"], e["kind"])
            for e in gate["non_bettable_flags"]
        }
        assert listed == {
            ("f5_runline", "closing", "bp:0"): (1, 1, "blend"),
            ("f5_runline", "closing", "bp:68"): (1, 1, "prediction"),
        }
        assert all("fails" not in e for e in gate["non_bettable_flags"])
        text = probe.format_summary(probe.summarise(games))
        assert "not graded (the blend, a pick'em app" in text
        assert "Kalshi" in text
        # The same first-inning prices from a sportsbook fail the gate.
        games[1]["offers"][0]["rows"].append(_rl("bp:14", -1.5, 700, 1.5, -1000))
        assert not probe.gate_first_five(games)["pass_b"]

    @staticmethod
    def _dk_ml_game(i: int, *, flagged: bool) -> dict[str, Any]:
        """A 2025 first-five moneyline where DraftKings' row is KEPT (off its markets)."""
        rows = [
            _f5_ml("bp:13", 120, 150, 450),
            _f5_ml("bp:19", 125, 145, 440),
            _f5_ml("bp:24", 118, 155, 460),
            _f5_ml("bp:12", 300, -150, 450) if flagged else _f5_ml("bp:12", 122, 150, 450),
        ]
        return _game(i, [_offer("f5_moneyline", "closing", rows)], season=2025, date="2025-06-01")

    def test_a_listed_book_off_its_markets_is_graded_like_any_other_book(self):
        games = [self._dk_ml_game(i, flagged=(i == 0)) for i in range(100)]
        gate = probe.gate_first_five(games)
        assert not gate["pass_b"] and not gate["pass_c"]
        assert gate["failing_outside_excluded"] == [("f5_moneyline", "closing", "bp:12")]
        assert gate["flagged_on_the_list"] == []
        assert gate["fixes"] == [
            "add f5_moneyline to the F5_EXCLUDED_MARKETS of bp:12 (DraftKings), whose "
            "exclusion from 2025-03-01 leaves that market out: 1 of its 100 f5_moneyline "
            "closing rows are first-inning-shaped (first 2025-06-01)"
        ]
        # One flagged row in 300 is below the (b) share: off its markets, DraftKings
        # is graded as any other book, so (c) passes too.
        games = [self._dk_ml_game(i, flagged=(i == 0)) for i in range(300)]
        gate = probe.gate_first_five(games)
        assert gate["pass_b"] and gate["pass_c"]

    def test_the_first_five_total_rule_bears_the_dated_total_out(self):
        """The census: DraftKings' only dated first-five total rows sat at the line 0.5.
        No peer quotes that line, so the median check cannot see them; the guard's
        f5_total_first_inning_line rule now names the shape, and the dated table
        shows the exclusion is needed."""

        def total_game(i: int) -> dict[str, Any]:
            kept = [
                _row(b, "f5_total", total_line=4.5, over_ml=-110, under_ml=-110)
                for b in ("bp:10", "bp:13", "bp:19")
            ]
            dk = _row(
                "bp:12",
                "f5_total",
                refusal="f5_total_first_inning_line",
                total_line=0.5,
                over_ml=-150,
                under_ml=120,
            )
            dk["dated_excluded"] = True
            offer = _offer("f5_total", "closing", kept)
            offer["dated_rows"] = [dk]
            return _game(i, [offer])

        gate = probe.gate_first_five([total_game(i) for i in range(2)])
        (dk,) = gate["dated_table"]
        assert (dk["flagged"], dk["n"], dk["reasons"]) == (2, 2, {"guard_shape": 2})
        assert gate["dated_not_needed"] == [] and gate["pass_c"]

    def test_the_probe_keeps_draftkings_first_five_moneyline(self):
        """Through the provider: on a 2025 game DraftKings' first-five moneyline row is
        a loaded row, its first-five run line a dated row."""
        payloads = dict(_PAYLOADS)
        payloads[279] = _payload(
            _sel("SD", {12: _line(1, 125), 19: _line(1, 120)}),
            _sel("BAL", {12: _line(1, 150), 19: _line(1, 155)}),
            _sel(None, {12: _line(1, 450), 19: _line(1, 440)}, selection="draw"),
        )
        payloads[278] = _payload(
            _sel("SD", {12: _line(1, 250)}),
            _sel("BAL", {12: _line(1, 280)}),
        )
        # A real first-five run line from DraftKings (no twin), so only the dated rule drops it.
        payloads[283] = _payload(
            _sel("SD", {12: _line(-0.5, 105), 19: _line(-0.5, 110), 13: _line(-0.5, 105)}),
            _sel("BAL", {12: _line(0.5, -125), 19: _line(0.5, -130), 13: _line(0.5, -125)}),
        )
        prov = _StubVendorProbe(payloads=payloads)
        record = probe.collect_game(
            prov,
            probe.ProbeGame(792, 2025, 6, "2025-06-01"),
            markets=("f5_moneyline", "f1_moneyline", "f5_runline", "f1_runline"),
        )
        offers = {(o["market"], o["line_type"]): o for o in record["offers"]}
        moneyline = offers[("f5_moneyline", "closing")]
        assert {r["book"] for r in moneyline["rows"]} == {"bp:12", "bp:19"}
        assert moneyline["dated_rows"] == []
        runline = offers[("f5_runline", "closing")]
        assert [r["book"] for r in runline["dated_rows"]] == ["bp:12"]
        dated = {(e["market"], e["who"]) for e in record["f5_exclusions"] if e["kind"] == "dated"}
        assert dated == {("f5_runline", "bp:12")}


# --------------------------------------------------------------------------- the regrade

_START = datetime(2025, 7, 3, 23, 5, tzinfo=UTC)


def _saved_row(book: str, market: str, *, minutes: float = 3.0, old=None, dated=False, **cols):
    """A row as an earlier census saved it: :func:`row_record` under the OLD verdict."""
    from pipeline.odds_row_guard import Refusal

    row = {
        "book": book,
        "book_id": int(book[3:]),
        "line_type": "closing",
        "market_type": market,
        "book_line_at": _START + timedelta(minutes=minutes),
        "scheduled_start": _START,
        **cols,
    }
    rec = probe.row_record(row, None if old is None else Refusal(old, f"old {old}"))
    if dated:
        rec["dated_excluded"] = True
    return rec


def _saved_census() -> dict[str, Any]:
    """Two games saved under the old guard and the old (all-market) dated rule."""
    fanduel = _saved_row("bp:10", "f5_moneyline", home_ml=210, away_ml=410, draw_ml=560)
    caesars = _saved_row("bp:13", "f5_moneyline", home_ml=120, away_ml=150, draw_ml=450)
    dk_ml = _saved_row("bp:12", "f5_moneyline", dated=True, home_ml=125, away_ml=150, draw_ml=450)
    total = {"over_ml": -110, "under_ml": -110}
    betmgm_total = _saved_row(
        "bp:19", "f5_total", total_line=4.5, over_line=4.5, under_line=4.5, **total
    )
    dk_total = _saved_row(
        "bp:12", "f5_total", dated=True, total_line=0.5, over_line=0.5, under_line=0.5, **total
    )
    late = _saved_row(
        "bp:12", "moneyline", minutes=40.0, old="late_closing_stamp", home_ml=-150, away_ml=130
    )
    game_1 = {
        "game_pk": 1,
        "season": 2025,
        "month": 7,
        "game_date": "2025-07-03",
        "home_starter": None,
        "offers": [
            {
                "market": "f5_moneyline",
                "line_type": "closing",
                "rows": [fanduel, caesars],
                "dated_rows": [dk_ml],
                "error": None,
            },
            {
                "market": "f5_total",
                "line_type": "closing",
                "rows": [betmgm_total],
                "dated_rows": [dk_total],
                "error": None,
            },
            {
                "market": "moneyline",
                "line_type": "closing",
                "rows": [late],
                "dated_rows": [],
                "error": None,
            },
        ],
        "f5_exclusions": [
            {"game_pk": 1, "market": m, "who": "bp:12", "kind": "dated", "why": "excluded from"}
            for m in ("f5_moneyline", "f5_total")
        ]
        + [{"game_pk": 1, "market": "f5_runline", "who": "bp:24", "kind": "twin", "why": "copy"}],
        "game_error": None,
    }
    dk_2024 = _saved_row(
        "bp:12", "f5_total", total_line=1.5, over_line=1.5, under_line=1.5, **total
    )
    game_2 = {
        "game_pk": 2,
        "season": 2024,
        "month": 8,
        "game_date": "2024-08-14",
        "home_starter": None,
        "offers": [
            {
                "market": "f5_total",
                "line_type": "closing",
                "rows": [dk_2024],
                "dated_rows": [],
                "error": None,
            }
        ],
        "f5_exclusions": [],
        "game_error": None,
    }
    return {"params": {"seed": 555, "games_read": 2}, "summary": {}, "games": [game_1, game_2]}


class TestProbeRegate:
    def test_the_regrade_applies_the_current_guard_and_dated_rule(self):
        payload = probe.regate(_saved_census(), source="saved.json")
        game_1, game_2 = payload["games"]
        offers = {o["market"]: o for o in game_1["offers"]}
        # FanDuel's first-five row adds to 0.670: now refused.
        moneyline = {r["book"]: r for r in offers["f5_moneyline"]["rows"]}
        assert moneyline["bp:10"]["refusal"] == "three_way_sum_below_one"
        assert "0.670" in moneyline["bp:10"]["refusal_message"]
        assert moneyline["bp:13"].get("refusal") is None
        # DraftKings' first-five moneyline row returns to the kept rows.
        assert "dated_excluded" not in moneyline["bp:12"]
        assert moneyline["bp:12"].get("refusal") is None
        assert offers["f5_moneyline"]["dated_rows"] == []
        # Its first-five total stays dated, now refused by the first-five total rule.
        (dk_total,) = offers["f5_total"]["dated_rows"]
        assert dk_total["dated_excluded"] is True
        assert dk_total["refusal"] == "f5_total_first_inning_line"
        # The stamps are read back: the late closing row is still refused.
        (late,) = offers["moneyline"]["rows"]
        assert late["refusal"] == "late_closing_stamp"
        assert "40 minutes" in late["refusal_message"]
        # Before the date nothing is dated; the 1.5 total is refused.
        (dk_2024,) = game_2["offers"][0]["rows"]
        assert dk_2024["refusal"] == "f5_total_first_inning_line"
        assert game_2["offers"][0]["dated_rows"] == []
        # The exclusions: the moneyline's dated entry goes, the total's and the twin stay.
        kinds = {(e["market"], e["who"], e["kind"]) for e in game_1["f5_exclusions"]}
        assert kinds == {
            ("f5_total", "bp:12", "dated"),
            ("f5_runline", "bp:24", "twin"),
        }

    def test_the_regrade_reports_what_moved_and_stamps_the_params(self):
        payload = probe.regate(_saved_census(), source="saved.json")
        changes = payload["summary"]["regate"]
        assert changes["refusals_added"] == [
            {"rule": "f5_total_first_inning_line", "market": "f5_total", "book": "bp:12", "n": 2},
            {"rule": "three_way_sum_below_one", "market": "f5_moneyline", "book": "bp:10", "n": 1},
        ]
        assert changes["refusals_removed"] == []
        assert changes["rows_to_kept"] == [
            {"market": "f5_moneyline", "line_type": "closing", "book": "bp:12", "n": 1}
        ]
        assert changes["rows_to_dated"] == []
        text = probe.format_regate_changes(changes)
        assert "refusals added: 3" in text and "rows to kept: 1" in text
        params = payload["params"]
        assert params["seed"] == 555 and params["regated_from"] == "saved.json"
        assert params["f5_excluded_markets"] == {"12": ["f5_runline", "f5_total"]}
        assert "gate" in payload["summary"]
        json.dumps(payload)

    def test_the_regrade_is_idempotent(self):
        once = probe.regate(_saved_census())
        twice = probe.regate(once)
        assert twice["games"] == once["games"]
        assert all(v == [] for v in twice["summary"]["regate"].values())

    def test_a_rule_that_no_longer_refuses_is_counted_as_removed(self):
        saved = _saved_census()
        # A row an older guard refused that the current guard keeps.
        saved["games"][0]["offers"][0]["rows"][1]["refusal"] = "f5_tie_price"
        payload = probe.regate(saved)
        assert payload["summary"]["regate"]["refusals_removed"] == [
            {"rule": "f5_tie_price", "market": "f5_moneyline", "book": "bp:13", "n": 1}
        ]

    def test_the_guard_view_restores_a_missing_side_line(self):
        """row_record leaves out a None field; a total that kept one side's line gets
        the other's back as None, so the guard compares the two as the loader did."""
        rec = _saved_row(
            "bp:12", "total", total_line=8.5, over_line=8.5, over_ml=-110, under_ml=-110
        )
        assert "under_line" not in rec
        view = probe.guard_view(rec)
        assert view["under_line"] is None
        assert view["book_line_at"] == _START + timedelta(minutes=3)
        from pipeline.odds_row_guard import check_row

        refusal = check_row(view)
        assert refusal is not None and refusal.rule == "total_line_mismatch"

    def test_the_guard_view_keeps_a_made_up_games_postponed_start(self):
        """SIM-555: a saved row of a made-up game keeps its postponed start, so a
        re-grade refuses a price stamped before it; any other row saves no such key."""
        from pipeline.odds_row_guard import check_row

        # The game was postponed 50 days before it was played; bet365 stamped its
        # price 29 minutes before the postponed start.
        postponed = _START - timedelta(days=50)
        rec = _saved_row(
            "bp:24",
            "moneyline",
            minutes=-(50 * 24 * 60) - 29,
            postponed_start=postponed,
            home_ml=-150,
            away_ml=130,
        )
        assert rec["postponed_start"] == postponed.isoformat()
        view = probe.guard_view(rec)
        assert view["postponed_start"] == postponed
        refusal = check_row(view)
        assert refusal is not None and refusal.rule == "stamped_before_postponement"
        plain = _saved_row("bp:24", "moneyline", home_ml=-150, away_ml=130)
        assert "postponed_start" not in plain
        assert check_row(probe.guard_view(plain)) is None

    def test_the_cli_regrades_a_saved_file_without_a_dsn(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
        src, out = tmp_path / "census.json", tmp_path / "regated.json"
        src.write_text(json.dumps(_saved_census()), encoding="utf-8")
        assert probe.main(["--regate", str(src), "--out", str(out)]) == 0
        printed = capsys.readouterr().out
        assert "== The regrade" in printed and "THE GATE" in printed
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["params"]["regated_from"] == str(src)
        assert saved["summary"]["gate"]["bc_first_five"]["verdict_c"] in ("PASS", "FAIL")
        # Without --regate the DSN is still required.
        with pytest.raises(SystemExit):
            probe.parse_args(["--games", "3"])
