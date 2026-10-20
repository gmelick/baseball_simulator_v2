"""``scripts/probes/odds_census.sql``: read-only, and its literals in step with the
book vocabulary, the first-five exclusion and the load guard's limits.

One of the five scripts of the one-row-per-book re-load (SIM-555, Partition E).
No test touches the network or a database.
"""

from __future__ import annotations

import re

import pytest
from tests.script_loader import PROBES, load_script

from pipeline.bettingpros.rules import F5_EXCLUDED_BOOKS, F5_TWIN_MARKETS, f5_excluded_markets
from pipeline.odds_provider import (
    GAME_MARKET_KIND,
    GAME_MARKET_TYPES,
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

probe = load_script("odds_book_census")


class TestCensusSql:
    SQL = (PROBES / "odds_census.sql").read_text(encoding="utf-8")

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
        """Section 14 follows the backtest's order: a three-way row with no tie
        after every row with one, THEN the preference list. Section 13 counts a
        three-way game only when a row lists the tie."""
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
        # Each book on its own markets (F5_EXCLUDED_MARKETS).
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
        """Section 4b is the guard's f5_total_first_inning_line rule and section
        5b its three_way_sum_below_one rule, at the guard's limits."""
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
