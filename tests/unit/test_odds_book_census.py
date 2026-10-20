"""``scripts/probes/odds_book_census.py``: the sample, the gate math on synthetic
rows, one game read through the provider with a stubbed vendor, and the re-grade of
a saved census.

One of the five scripts of the one-row-per-book re-load (SIM-555, Partition E).
No test touches the network or a database.
"""

from __future__ import annotations

import json
import urllib.parse
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.script_loader import load_script

from pipeline.odds_provider import (
    graded_book_labels,
)
from pipeline.odds_row_guard import (
    F5_TOTAL_LINE_MAX,
    THREE_WAY_SUM_MIN,
)

probe = load_script("odds_book_census")


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
            # A far-off price, not a guard refusal: gate (b) grades the
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
        # The load guard refuses FanDuel's first-five row on every game, so
        # none is stored: gate (b) passes, and the probe reports the refusals
        # beside it without grading them.
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
# The probe: the graded book, the bettable books and the dated rows
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
        """The probe's graded book puts a three-way row with no tie after every
        row with one, as the backtest does."""
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
# The probe: the per-market dated exclusion, the bettable books only, the
# first-five guard rules and the re-grade of a saved census
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
        f5_total_first_inning_line rule names the shape, and the dated table
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
        """A saved row of a made-up game keeps its postponed start, so a re-grade
        refuses a price stamped before it; any other row saves no such key."""
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
