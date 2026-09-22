"""SIM-478/479/480 — the fence certification script's metrics on fixtures
with KNOWN answers (``scripts/sim478_fence_check.py``).

No DuckDB, no Postgres, no network: the pure functions take numpy arrays
and a geometry document. The 200-ball fixture (20 home runs, 180 kept
balls at two venues) places exactly two home runs under the line and three
kept balls over it on the version-2 document, so every share is an exact
fraction; the old nine-sector document reads the two down-the-line home
runs as clamped balls, so its answers differ in a known way."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sim478_fence_check as fc  # noqa: E402

from pipeline.batch.engine_artifacts import PARK_N_SECTORS, PARK_SPRAY_MIN  # noqa: E402

CENTRE = 0.0  # spray 0: sector 5 on the eleven-sector grid, sector 4 on the nine
LINE_LEFT = -50.0  # beyond the old grid; sector 0 on the eleven-sector grid


def _balls(rows: list[tuple[int, int, float, float, bool]]) -> dict[str, np.ndarray]:
    venue, season, spray, dist, hr = zip(*rows, strict=True)
    d = np.asarray(dist, dtype=np.float64)
    h = np.asarray(hr, dtype=bool)
    return {
        "venue": np.asarray(venue, dtype=np.int64),
        "season": np.asarray(season, dtype=np.int64),
        "spray": np.asarray(spray, dtype=np.float64),
        "dist": d,
        "hr": h,
        "kept300": (~h) & (d >= 300.0),
    }


@pytest.fixture
def fixture_balls() -> dict[str, np.ndarray]:
    """200 balls, season 2024. Venue 1 (centre line 400): 9 home runs at 420,
    one at 395 (under), 89 kept at 350, one kept at 405 (over). Venue 2
    (centre 400): 7 home runs at 420, one at 395, 80 kept at 350, two kept
    at 405; and ten balls down the left line (spray -50): two home runs at
    345 and eight kept at 300."""
    rows: list[tuple[int, int, float, float, bool]] = []
    rows += [(1, 2024, CENTRE, 420.0, True)] * 9 + [(1, 2024, CENTRE, 395.0, True)]
    rows += [(1, 2024, CENTRE, 350.0, False)] * 89 + [(1, 2024, CENTRE, 405.0, False)]
    rows += [(2, 2024, CENTRE, 420.0, True)] * 7 + [(2, 2024, CENTRE, 395.0, True)]
    rows += [(2, 2024, CENTRE, 350.0, False)] * 80 + [(2, 2024, CENTRE, 405.0, False)] * 2
    rows += [(2, 2024, LINE_LEFT, 345.0, True)] * 2 + [(2, 2024, LINE_LEFT, 300.0, False)] * 8
    b = _balls(rows)
    assert len(b["hr"]) == 200 and int(b["hr"].sum()) == 20
    return b


def _v2_doc() -> dict:
    """Version 2: venue 1 with two season groups (the centre line 400 early,
    380 late — a Camden-like move), venue 2 one group with the left-line
    sector at 330 and centre 400. Every other sector 400."""
    n = 11
    early = [400.0] * n
    late = [400.0] * n
    late[5] = 380.0
    v2 = [400.0] * n
    v2[0] = 330.0
    return {
        "version": 2,
        "seasons": [2023, 2024, 2025, 2026],
        "sector_deg": 10,
        "spray_min": -55.0,
        "spray_max": 55.0,
        "n_sectors": n,
        "league": [400.0] * n,
        "venues": {"1": {"2023-2024": early, "2025-2026": late}, "2": {"2023-2026": v2}},
        "source": {
            "1": {"2023-2024": ["both"] * n, "2025-2026": ["both"] * n},
            "2": {"2023-2026": ["both"] * n},
        },
        "support_hr": {
            "1": {"2023-2024": [30] * n, "2025-2026": [30] * n},
            "2": {"2023-2026": [30] * n},
        },
    }


def _old_doc() -> dict:
    """The live document's shape of 2026-09-08: nine sectors, a plain list
    per venue. Venue 2's edge sector 0 reads 360, so a ball at spray -50
    clamps to it."""
    n = 9
    v2 = [400.0] * n
    v2[0] = 360.0
    return {
        "seasons": [2023, 2024, 2025, 2026],
        "sector_deg": 10,
        "spray_min": -45.0,
        "spray_max": 45.0,
        "n_sectors": n,
        "league": [400.0] * n,
        "venues": {"1": [400.0] * n, "2": v2},
        "source": {"1": ["both"] * n, "2": ["both"] * n},
        "support_hr": {"1": [30] * n, "2": [30] * n},
    }


# ---------------------------------------------------------------------------
# A. the decision
# ---------------------------------------------------------------------------


def test_decision_check_version2_document_exact_fractions(fixture_balls):
    d = fc.decision_check(fixture_balls, _v2_doc())
    assert d["grid"]["n_sectors"] == 11
    assert d["n_air"] == 200 and d["n_hr_all"] == 20 and d["n_decided"] == 200
    assert d["n_passed"] == 0
    # over: 18 home runs (9 + 7 + the two down the line) and 3 kept balls
    assert d["n_over"] == 21 and d["n_short"] == 179
    assert d["p_hr_over"] == pytest.approx(18 / 21)
    assert d["missed"] == pytest.approx(2 / 20)
    assert d["over_but_kept"] == pytest.approx(3 / 21)
    assert d["p_hr_short"] == pytest.approx(2 / 179)
    assert d["hr_decided_over"] == pytest.approx(18 / 20)


def test_decision_check_bands_and_margins(fixture_balls):
    d = fc.decision_check(fixture_balls, _v2_doc())
    bands = {b["band"]: b for b in d["bands"]}
    assert set(bands) == {"0-5", "5-10", "10-20", "20-40", "40+"}
    # 395 vs 400 (two home runs, short) and 405 vs 400 (three kept, over) sit 5 ft off
    assert bands["5-10"]["n"] == 5
    assert bands["5-10"]["n_over"] == 3 and bands["5-10"]["p_hr_over"] == pytest.approx(0.0)
    assert bands["5-10"]["n_short"] == 2 and bands["5-10"]["p_hr_short"] == pytest.approx(1.0)
    # the two down-the-line home runs: 345 vs 330 = 15 ft
    assert bands["10-20"]["n"] == 2 and bands["10-20"]["p_hr_over"] == pytest.approx(1.0)
    # 420 vs 400 (16 home runs) and 300 vs 330 (eight kept) sit 20-40 ft off
    assert bands["20-40"]["n"] == 24 and bands["20-40"]["n_over"] == 16
    assert bands["40+"]["n"] == 169 and bands["40+"]["n_over"] == 0
    assert d["hr_by_margin"]["<10"] == pytest.approx(2 / 20)
    assert d["hr_by_margin"]["10-30"] == pytest.approx(18 / 20)
    assert d["hr_by_margin"]["30+"] == pytest.approx(0.0)


def test_decision_check_sectors_and_venues(fixture_balls):
    d = fc.decision_check(fixture_balls, _v2_doc())
    by_sec = {s["sector"]: s for s in d["sectors"]}
    assert by_sec[0]["n"] == 10 and by_sec[0]["lo"] == -55.0 and by_sec[0]["hi"] == -45.0
    assert by_sec[5]["n"] == 190 and by_sec[5]["lo"] == -5.0
    assert by_sec[5]["missed"] == pytest.approx(2 / 18)
    # only venues with 200+ decided balls join the table: neither has that many
    assert d["venues"] == []
    small = fc.VENUE_MIN_DECIDED
    try:
        fc.VENUE_MIN_DECIDED = 50
        d = fc.decision_check(fixture_balls, _v2_doc())
    finally:
        fc.VENUE_MIN_DECIDED = small
    rows = {v["venue"]: v for v in d["venues"]}
    assert rows[1]["n_decided"] == 100 and rows[1]["p_hr_over"] == pytest.approx(9 / 10)
    assert rows[1]["missed"] == pytest.approx(1 / 10)
    assert rows[2]["p_hr_over"] == pytest.approx(9 / 11) and rows[2]["missed"] == pytest.approx(
        1 / 10
    )


def test_down_the_line_subset_both_document_shapes(fixture_balls):
    new = fc.decision_check(fixture_balls, _v2_doc())["down_the_line"]
    assert new["n"] == 10 and new["n_hr"] == 2 and new["hr_rate"] == pytest.approx(0.2)
    assert new["n_over"] == 2 and new["p_hr_over"] == pytest.approx(1.0)
    assert new["missed"] == pytest.approx(0.0)
    # the old grid clamps spray -50 to sector 0 (line 360): both home runs at 345 read short
    old = fc.decision_check(fixture_balls, _old_doc())
    assert old["grid"]["n_sectors"] == 9
    assert old["down_the_line"]["n"] == 10 and old["down_the_line"]["missed"] == pytest.approx(1.0)
    assert old["n_over"] == 19 and old["p_hr_over"] == pytest.approx(16 / 19)
    assert old["missed"] == pytest.approx(4 / 20)
    assert old["over_but_kept"] == pytest.approx(3 / 19)
    # the breakdowns read the balls inside the grid: the ten clamped balls stay out
    assert old["n_clamped"] == 10 and old["n_inside_grid"] == 190
    assert sum(b["n"] for b in old["bands"]) == 190
    assert old["hr_by_margin"]["<10"] == pytest.approx(2 / 18)
    assert new["n"] == 10 and fc.decision_check(fixture_balls, _v2_doc())["n_clamped"] == 0


def test_group_pick_by_season_reads_the_late_line():
    doc = _v2_doc()
    # a 2025 ball at venue 1, 390 ft to centre: the late group's line is 380 -> over
    late = _balls([(1, 2025, CENTRE, 390.0, True)])
    assert bool(fc.decide(late, doc).over[0]) is True
    early = _balls([(1, 2024, CENTRE, 390.0, True)])
    assert bool(fc.decide(early, doc).short[0]) is True
    # a season outside every group takes the LATEST group
    future = _balls([(1, 2031, CENTRE, 390.0, True)])
    assert bool(fc.decide(future, doc).over[0]) is True
    assert fc.line_for(doc, 1, 2023)[5] == 400.0 and fc.line_for(doc, 1, 2026)[5] == 380.0


def test_decide_passes_without_carry_or_spray_and_falls_to_the_league_line():
    doc = _v2_doc()
    doc["venues"]["2"]["2023-2026"][5] = None  # a sector with no line -> the league line (400)
    b = _balls(
        [
            (2, 2024, CENTRE, 405.0, True),  # over the league line
            (2, 2024, np.nan, 405.0, True),  # no spray -> passed
            (2, 2024, CENTRE, np.nan, True),  # no distance -> passed
            (9, 2024, CENTRE, 399.0, False),  # a venue the document lacks -> the league line
        ]
    )
    d = fc.decide(b, doc)
    assert d.decided.tolist() == [True, False, False, True]
    assert d.over.tolist() == [True, False, False, False]
    assert d.short.tolist() == [False, False, False, True]
    assert d.fence[0] == 400.0 and d.fence[3] == 400.0
    s = fc.decision_check(b, doc)
    assert s["n_passed"] == 2 and s["n_no_carry"] == 1 and s["n_decided"] == 2


# ---------------------------------------------------------------------------
# A3. held-out seasons
# ---------------------------------------------------------------------------


def _two_season_balls() -> dict[str, np.ndarray]:
    """One venue, centre sector, two seasons alike: 15 home runs at 400..414
    and 30 kept balls at 320..349 per season. The rebuilt line from either
    season is the midpoint of 401.4 and about 348.7, so the other season's
    home runs all clear it and its kept balls all fall short."""
    rows: list[tuple[int, int, float, float, bool]] = []
    for y in (2023, 2024):
        rows += [(1, y, CENTRE, 400.0 + i, True) for i in range(15)]
        rows += [(1, y, CENTRE, 320.0 + i, False) for i in range(30)]
    return _balls(rows)


def test_held_out_on_the_builder_grid_uses_build_geometry_document():
    balls = _two_season_balls()
    doc = _v2_doc()
    assert fc.document_grid(doc) == fc.BUILDER_GRID
    rows = fc.held_out(balls, [2023, 2024], doc)
    assert [r["season"] for r in rows] == [2023, 2024]
    for r in rows:
        assert r["method"] == "build_geometry_document"
        assert r["n_decided"] == 45
        assert r["p_hr_over"] == pytest.approx(1.0)
        assert r["missed"] == pytest.approx(0.0)
        assert r["over_but_kept"] == pytest.approx(0.0)


def test_held_out_on_an_old_grid_runs_the_line_rule_on_that_grid():
    balls = _two_season_balls()
    rows = fc.held_out(balls, [2023, 2024], _old_doc())
    for r in rows:
        assert r["method"] != "build_geometry_document"
        assert r["n_decided"] == 45 and r["p_hr_over"] == pytest.approx(1.0)
        assert r["missed"] == pytest.approx(0.0)
    # the rebuilt lines themselves: nine sectors, the centre line near 375
    rebuilt = fc.rebuild_lines(balls, [2023, 2024], fc.document_grid(_old_doc()))
    assert rebuilt["n_sectors"] == 9 and len(rebuilt["venues"]["1"]) == 9
    assert rebuilt["venues"]["1"][4] == pytest.approx((401.4 + 348.71) / 2, abs=0.2)
    assert rebuilt["venues"]["1"][0] is None and rebuilt["league"][4] == rebuilt["venues"]["1"][4]


# ---------------------------------------------------------------------------
# A5. moved walls
# ---------------------------------------------------------------------------


def test_moved_walls_reads_the_live_line_against_the_late_line():
    """A wall moved in for 2025: the home runs land 30 ft shorter from then
    on. The checked document blends the two walls at 375, so a third of the
    late home runs (370..374) read short; the late line misses none."""
    rows: list[tuple[int, int, float, float, bool]] = []
    for y in (2023, 2024, 2025, 2026):
        base = 400.0 if y <= 2024 else 370.0
        rows += [(1, y, CENTRE, base + i, True) for i in range(15)]
        rows += [(1, y, CENTRE, 320.0 + i, False) for i in range(30)]
    balls = _balls(rows)
    doc = _v2_doc()
    doc["venues"]["1"] = {"2023-2026": [375.0] * 11}
    moves = fc.moved_walls(balls, doc, [2023, 2024, 2025, 2026])
    assert len(moves) == 1
    m = moves[0]
    assert m["venue"] == 1 and m["sectors"] == [5] and (m["from"], m["to"]) == (2024, 2025)
    assert m["spray_lo"] == -5.0 and m["spray_hi"] == 5.0
    assert m["n"] == 90 and m["n_hr"] == 30
    assert m["live_missed"] == pytest.approx(10 / 30)
    assert m["late_missed"] == pytest.approx(0.0)
    assert m["excess_missed"] == pytest.approx(10 / 30)
    assert m["early"][0] > m["late"][0] + fc.THRESHOLDS.a5_excess


def test_moved_walls_lists_nothing_without_a_move():
    balls = _two_season_balls()
    assert fc.moved_walls(balls, _v2_doc(), [2023, 2024]) == []


# ---------------------------------------------------------------------------
# A6. thin parks
# ---------------------------------------------------------------------------


def test_thin_parks_lists_league_line_and_thin_sectors_of_the_last_season():
    doc = _v2_doc()
    n = 11
    doc["venues"]["5355"] = {"2026-2026": [350.0] * n}
    doc["source"]["5355"] = {"2026-2026": ["prior"] * 4 + ["league"] * 7}
    doc["support_hr"]["5355"] = {"2026-2026": [3] * n}
    doc["venues"]["7"] = {"2023-2026": [380.0] * n}
    doc["source"]["7"] = {"2023-2026": ["both"] * n}
    doc["support_hr"]["7"] = {"2023-2026": [40] * 5 + [12] + [40] * 5}
    balls = _balls(
        [
            (5355, 2026, CENTRE, 380.0, True),
            (7, 2026, CENTRE, 380.0, True),
            (1, 2026, CENTRE, 380.0, True),  # venue 1: every sector "both" with 30 HR -> not listed
            (2, 2025, CENTRE, 380.0, True),  # not in the last season -> not listed
            (
                99,
                2026,
                CENTRE,
                380.0,
                True,
            ),  # no geometry at all -> every sector on the league line
        ]
    )
    rows = fc.thin_parks(doc, balls, [2023, 2024, 2025, 2026], {5355: 6, 7: 67, 99: 1})
    by = {r["venue"]: r for r in rows}
    assert set(by) == {5355, 7, 99}
    assert by[5355]["league_sectors"] == list(range(4, 11)) and by[5355]["games"] == 6
    assert by[5355]["thin_sectors"] == list(range(n))
    assert by[7]["league_sectors"] == [] and by[7]["thin_sectors"] == [5]
    assert by[99]["has_geometry"] is False and by[99]["league_sectors"] == list(range(n))
    # the old shape: a plain source list per venue
    old = _old_doc()
    old["source"]["2"] = ["league"] * 2 + ["both"] * 7
    old["support_hr"]["2"] = [5, 5] + [30] * 7
    rows = fc.thin_parks(old, _balls([(2, 2026, CENTRE, 380.0, True)]), [2023, 2024, 2025, 2026])
    assert rows[0]["league_sectors"] == [0, 1] and rows[0]["thin_sectors"] == [0, 1]


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------


def _summary(**over) -> dict:
    """A hand-made summary at the pass side of every threshold."""
    t = fc.THRESHOLDS
    base = {
        "decision": {
            "p_hr_over": t.a1_p_hr_over,
            "missed": t.a1_missed,
            "venues": [
                {"venue": 3, "p_hr_over": t.a2_p_hr_over, "missed": t.a2_missed},
                {"venue": 2, "p_hr_over": 0.95, "missed": 0.05},
            ],
            "down_the_line": {"n": 100, "p_hr_over": 0.9, "missed": t.a4_missed},
        },
        "held_out": [
            {
                "season": 2023,
                "p_hr_over": t.a1_p_hr_over + t.a3_delta - 0.001,
                "missed": t.a1_missed,
            },
            {
                "season": 2024,
                "p_hr_over": t.a1_p_hr_over,
                "missed": t.a1_missed - t.a3_delta + 0.001,
            },
        ],
        "moves": [
            {
                "name": "Camden",
                "to": 2025,
                "sectors": [1, 2, 3],
                "live_missed": 0.10 + t.a5_excess,
                "late_missed": 0.10,
                "excess_missed": t.a5_excess,
            }
        ],
        "thin": [{"name": "Kauffman", "league_sectors": [], "source": ["both"], "games": 5}],
    }
    for key, val in over.items():
        section, field = key.split("__", 1)
        if section == "venue0":
            base["decision"]["venues"][0][field] = val
        elif section == "dtl":
            base["decision"]["down_the_line"][field] = val
        elif section == "held0":
            base["held_out"][0][field] = val
        elif section == "move0":
            base["moves"][0][field] = val
        elif section == "thin0":
            base["thin"][0][field] = val
        else:
            base[section][field] = val
    return base


def _status(summary: dict) -> dict[str, bool]:
    return {v["check"]: v["passed"] for v in fc.verdicts(summary)}


def test_verdicts_pass_at_every_threshold():
    s = _status(_summary())
    assert s == {"A1": True, "A2": True, "A3": True, "A4": True, "A5": True, "A6": True}
    assert "B2" not in s


@pytest.mark.parametrize(
    ("override", "check"),
    [
        ({"decision__p_hr_over": fc.THRESHOLDS.a1_p_hr_over - 0.0005}, "A1"),
        ({"decision__missed": fc.THRESHOLDS.a1_missed + 0.0005}, "A1"),
        ({"venue0__p_hr_over": fc.THRESHOLDS.a2_p_hr_over - 0.001}, "A2"),
        ({"venue0__missed": fc.THRESHOLDS.a2_missed + 0.001}, "A2"),
        ({"held0__p_hr_over": fc.THRESHOLDS.a1_p_hr_over + fc.THRESHOLDS.a3_delta + 0.001}, "A3"),
        ({"held0__missed": fc.THRESHOLDS.a1_missed - fc.THRESHOLDS.a3_delta - 0.001}, "A3"),
        ({"dtl__missed": fc.THRESHOLDS.a4_missed + 0.001}, "A4"),
        ({"move0__excess_missed": fc.THRESHOLDS.a5_excess + 0.001}, "A5"),
        ({"thin0__league_sectors": [0, 1]}, "A6"),
    ],
)
def test_each_verdict_flips_just_past_its_threshold(override, check):
    s = _status(_summary(**override))
    assert s[check] is False
    assert all(v for k, v in s.items() if k != check)


def test_verdict_lines_name_the_offender():
    rows = {v["check"]: v for v in fc.verdicts(_summary(thin0__league_sectors=[0, 1]))}
    assert rows["A6"]["passed"] is False and "Kauffman 2 of 1" in rows["A6"]["text"]
    rows = {
        v["check"]: v
        for v in fc.verdicts(_summary(move0__excess_missed=0.2, move0__live_missed=0.3))
    }
    assert "Camden 2025" in rows["A5"]["text"]
    rows = {v["check"]: v for v in fc.verdicts(_summary(venue0__missed=0.5))}
    assert "Fenway" in rows["A2"]["text"]


def test_verdicts_include_b2_when_a_park_read_exists():
    s = _summary()
    s["park"] = {"n_venues": 3, "r": 0.7, "flagged": [], "passed": True}
    assert _status(s)["B2"] is True
    s["park"] = {"n_venues": 3, "r": 0.7, "flagged": [19], "passed": False}
    rows = {v["check"]: v for v in fc.verdicts(s)}
    assert rows["B2"]["passed"] is False and "Coors" in rows["B2"]["text"]


# ---------------------------------------------------------------------------
# B2. the per-park arithmetic
# ---------------------------------------------------------------------------


def test_park_table_r_and_the_three_se_flag():
    per_game = [
        {"game_pk": 1, "venue_id": 19, "season": 2024, "HR": 60, "BIP": 1000},
        {"game_pk": 2, "venue_id": 19, "season": 2024, "HR": 40, "BIP": 1000},
        {"game_pk": 3, "venue_id": 3, "season": 2024, "HR": 45, "BIP": 1000},
        {"game_pk": 4, "venue_id": 7, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 5, "venue_id": 7, "season": 2024, "HR": 30, "BIP": 3000},
        {"game_pk": 6, "venue_id": 1, "season": 2024, "HR": 45, "BIP": 1000},  # no expectation
        {"game_pk": 7, "venue_id": 0, "season": 2024, "HR": 45, "BIP": 1000},  # no venue
        {"game_pk": 8, "venue_id": None, "season": 2024, "HR": 45, "BIP": 1000},  # no venue
    ]
    expected = {1: 0.04, 2: 0.04, 3: 0.045, 4: 0.03, 5: 0.03, 7: 0.04, 8: 0.04}
    factor = {19: 1.20, 3: 1.00, 7: 0.80}
    # min_games 1: every venue enters (the redesign's default is 3; its own
    # test follows).
    out = fc.park_table(per_game, expected, factor, min_games=1)
    # SIM-478: a game with no venue (0 or None) is skipped, never pooled under
    # a venue 0.
    assert out["n_games_skipped"] == 3 and out["n_venues"] == 3
    assert 0 not in {r["venue"] for r in out["rows"]}
    rows = {r["venue"]: r for r in out["rows"]}
    coors = rows[19]
    assert coors["games"] == 2 and coors["bip"] == 2000
    assert coors["sim_hr_bip"] == pytest.approx(0.05)
    assert coors["expected_hr_bip"] == pytest.approx(0.04)
    assert coors["residual"] == pytest.approx(0.01)
    assert coors["se"] == pytest.approx(np.sqrt(0.05 * 0.95 / 2000))
    # No expectation SE given: the combined SE is the sim's.
    assert coors["se_exp"] == 0.0 and coors["se_combined"] == pytest.approx(coors["se"])
    assert coors["entered"] is True
    assert coors["flagged"] is False  # 0.01 < 3 * 0.00487
    kauffman = rows[7]
    assert kauffman["sim_hr_bip"] == pytest.approx(60 / 4000)
    assert kauffman["expected_hr_bip"] == pytest.approx(0.03)
    assert kauffman["residual"] == pytest.approx(-0.015)
    assert kauffman["flagged"] is True  # 0.015 > 3 * 0.00192
    resid = [rows[v]["residual"] for v in (3, 7, 19)]
    fm1 = [factor[v] - 1.0 for v in (3, 7, 19)]
    assert out["r"] == pytest.approx(float(np.corrcoef(resid, fm1)[0, 1]))
    assert out["r"] > fc.THRESHOLDS.b2_r
    assert out["flagged"] == [7] and out["passed"] is False
    assert out["n_entered"] == 3 and out["min_games"] == 1


def test_park_table_passes_when_residuals_track_the_factor_within_se():
    per_game = [
        {"game_pk": i, "venue_id": v, "season": 2024, "HR": hr, "BIP": 2000}
        for i, (v, hr) in enumerate([(19, 92), (3, 80), (7, 70), (2, 82)], start=1)
    ]
    expected = dict.fromkeys(range(1, 5), 0.04)
    factor = {19: 1.2, 3: 1.0, 7: 0.85, 2: 1.05}
    out = fc.park_table(per_game, expected, factor, min_games=1)
    assert out["flagged"] == [] and out["r"] > 0.9 and out["passed"] is True


def test_park_table_needs_three_venues_for_r():
    per_game = [{"game_pk": 1, "venue_id": 19, "season": 2024, "HR": 50, "BIP": 1000}]
    out = fc.park_table(per_game, {1: 0.05}, {19: 1.2}, min_games=1)
    # r is reported, not graded (the redesign): one entering venue with no
    # flag passes, and r is NaN below three venues.
    assert np.isnan(out["r"]) and out["passed"] is True
    # At the default floor (3 games) the one-game venue does not enter, and a
    # read with no entering venue cannot pass.
    out = fc.park_table(per_game, {1: 0.05}, {19: 1.2})
    assert out["n_entered"] == 0 and out["passed"] is False
    assert out["rows"][0]["entered"] is False


def test_counters_read_keeps_the_six_entry_contract():
    # [over, short, band, passed, no matching rows, not an air ball]; a ground
    # ball counts in both [1] and [5]. Every share is over the air balls — the
    # harness's denominator (``scripts/sim_stats.py``), so one lane reads the
    # same no-matching-rows share in both scripts.
    c = fc.counters_read([90, 800, 0, 1, 3, 500])
    assert c["born"] == 891 and c["air"] == 391
    assert c["over_share_of_air"] == pytest.approx(90 / 391)
    assert c["short_share_of_air"] == pytest.approx(300 / 391)
    assert c["passed_share_of_air"] == pytest.approx(1 / 391)
    assert c["no_rows_share_of_air"] == pytest.approx(3 / 391)
    assert fc.counters_read(None) is None and fc.counters_read([1, 2, 3, 4, 5]) is None


def test_thresholds_are_the_plan_s_and_the_grid_is_the_builder_s():
    t = fc.THRESHOLDS
    assert (t.a1_p_hr_over, t.a1_missed) == (0.90, 0.10)
    assert (t.a2_p_hr_over, t.a2_missed) == (0.80, 0.15)
    assert t.a3_delta == 0.03 and t.a4_missed == 0.12 and t.a5_excess == 0.05
    assert (t.b2_r, t.b2_se) == (0.5, 3.0)
    assert fc.BUILDER_GRID.n_sectors == PARK_N_SECTORS == 11
    assert fc.BUILDER_GRID.spray_min == PARK_SPRAY_MIN == -55.0


# ---------------------------------------------------------------------------
# A7. the transfer (the plan's §11): the fair target, (a) and (c)
# ---------------------------------------------------------------------------


def _flat_doc(offsets: dict[str, float] | None) -> dict:
    """Two venues, one season group each, every sector at 400 ft; the carry
    offsets when given (park 2's air carries a ball 20 ft further)."""
    n = 11
    doc = {
        "version": 2,
        "seasons": [2023, 2024, 2025, 2026],
        "sector_deg": 10,
        "spray_min": -55.0,
        "spray_max": 55.0,
        "n_sectors": n,
        "league": [400.0] * n,
        "venues": {"1": {"2023-2026": [400.0] * n}, "2": {"2023-2026": [400.0] * n}},
        "source": {"1": {"2023-2026": ["both"] * n}, "2": {"2023-2026": ["both"] * n}},
        "support_hr": {"1": {"2023-2026": [30] * n}, "2": {"2023-2026": [30] * n}},
    }
    if offsets is not None:
        doc["carry_offset_ft"] = dict(offsets)
        doc["carry_offset_fit"] = {"n_hr": 18, "reference_median_park": 1}
    return doc


@pytest.fixture
def transfer_balls() -> dict[str, np.ndarray]:
    """160 air balls in two cells (A: exit velocity 100, launch angle 30;
    B: 90, 20), spray 0, season 2024. Park 1 holds 60 balls per cell (6 and 3
    home runs), park 2 holds 20 per cell (6 and 3): park 2's cell rates are
    exactly TWICE the league's (0.3 / 0.15 against 0.15 / 0.075). Distances,
    independent of the home-run flag: park 1 has 12 balls at 405 and 108 at
    370; park 2 has 24 at 405 and 16 at 370."""
    rows = []  # (venue, hr, ev, la)
    for venue, per_cell in ((1, 60), (2, 20)):
        for ev, la, n_hr in ((100.0, 30.0, 6), (90.0, 20.0, 3)):
            rows += [(venue, True, ev, la)] * n_hr + [(venue, False, ev, la)] * (per_cell - n_hr)
    venue = np.array([r[0] for r in rows], dtype=np.int64)
    hr = np.array([r[1] for r in rows], dtype=bool)
    ev = np.array([r[2] for r in rows], dtype=np.float64)
    la = np.array([r[3] for r in rows], dtype=np.float64)
    dist = np.full(len(rows), 370.0)
    dist[np.flatnonzero(venue == 1)[:12]] = 405.0
    dist[np.flatnonzero(venue == 2)[:24]] = 405.0
    game_pk = np.where(venue == 1, 1001, 2001).astype(np.int64)
    game_pk[np.flatnonzero(venue == 1)[:30]] = 1002  # park 1 has two games
    return {
        "venue": venue,
        "season": np.full(len(rows), 2024, dtype=np.int64),
        "spray": np.zeros(len(rows)),
        "dist": dist,
        "hr": hr,
        "kept300": (~hr) & (dist >= 300.0),
        "ev": ev,
        "la": la,
        "game_pk": game_pk,
    }


def test_transfer_cells_group_the_grid():
    inv = fc.transfer_cells(
        np.array([100.0, 103.9, 104.0, 100.0, 100.0]),
        np.array([30.0, 30.0, 30.0, 31.9, 32.0]),
        np.array([0.0, 9.9, 0.0, -9.9, 0.0]),
    )
    # 100 / 103.9 mph share a 4-mph cell (100-104); 104 starts the next;
    # 30 / 31.9 share a launch-angle cell (28-32), 32 starts the next; the
    # sign of the spray does not split a cell.
    assert inv[0] == inv[1] == inv[3]
    assert inv[2] != inv[0] and inv[4] != inv[0] and inv[2] != inv[4]
    assert fc.transfer_cells(np.array([]), np.array([]), np.array([])).size == 0


def test_transfer_target_is_the_pool_mix_at_the_park_s_cell_rates(transfer_balls):
    tr = fc.transfer_check(transfer_balls, _flat_doc(None), min_decided=20)
    assert tr["n_pool"] == 160 and tr["has_offsets"] is False and tr["graded"] is False
    assert tr["reason"] == "the document carries no carry offsets"
    parks = {r["venue"]: r for r in tr["parks"]}
    assert set(parks) == {1, 2}
    league_share = 18 / 160
    # Park 2's cell rates are twice the league's, so its target is twice
    # the league share; park 1's is 0.075 (cells at 0.1 and 0.05).
    assert parks[2]["target"] == pytest.approx(2 * league_share)
    assert parks[1]["target"] == pytest.approx(0.075)
    assert parks[2]["covered_share"] == 1.0
    assert parks[1]["games"] == 2 and parks[2]["games"] == 1
    assert parks[1]["n_decided"] == 120 and parks[2]["n_decided"] == 40
    # (a): every 405 ball clears the 400 line at either park: 36 of 160.
    assert parks[1]["over_a"] == pytest.approx(36 / 160)
    assert parks[2]["over_a"] == pytest.approx(36 / 160)
    assert parks[1]["gap_a"] == pytest.approx(36 / 160 - 0.075)
    assert np.isnan(parks[1]["over_c"]) and np.isnan(parks[1]["gap_c"])
    assert parks[1]["offset_ft"] is None
    # The verdict does not grade a document without offsets and does not fail.
    v = fc._a7_verdict(tr, fc.THRESHOLDS)
    assert v["passed"] is True and v["graded"] is False
    assert v["text"].startswith("not graded: the document carries no carry offsets")
    assert "2 parks read with (a) alone" in v["text"]


def test_transfer_with_offsets_lands_on_the_target(transfer_balls):
    doc = _flat_doc({"1": 0.0, "2": 20.0})
    tr = fc.transfer_check(transfer_balls, doc, min_decided=20)
    assert tr["has_offsets"] is True and tr["graded"] is True
    parks = {r["venue"]: r for r in tr["parks"]}
    assert parks[2]["offset_ft"] == 20.0 and parks[1]["offset_ft"] == 0.0
    # Born at park 1, park 2's balls lose 20 ft (405 -> 385, short): only
    # park 1's 12 long balls clear the line = 0.075 = the target.
    assert parks[1]["over_c"] == pytest.approx(12 / 160)
    assert parks[1]["gap_c"] == pytest.approx(0.0)
    # Born at park 2, park 1's balls gain 20 ft (370 -> 390, still short;
    # 405 -> 425 over): 12 + 24 = 36 over = 0.225 = the target.
    assert parks[2]["over_c"] == pytest.approx(36 / 160)
    assert parks[2]["gap_c"] == pytest.approx(0.0)
    # (a) is unchanged by the offsets.
    assert parks[1]["over_a"] == pytest.approx(36 / 160)
    v = fc._a7_verdict(tr, fc.THRESHOLDS)
    assert v["check"] == "A7" and v["passed"] is True and v["graded"] is True
    # The line is 1.5 points: 1.4 off passes, 1.6 off flips the verdict.
    assert fc.THRESHOLDS.a7_gap == pytest.approx(0.015)
    tr["parks"][0]["gap_c"] = -0.014
    v = fc._a7_verdict(tr, fc.THRESHOLDS)
    assert v["passed"] is True and "off:" not in v["text"]
    tr["parks"][0]["gap_c"] = 0.016
    v = fc._a7_verdict(tr, fc.THRESHOLDS)
    assert v["passed"] is False and "off:" in v["text"]


def test_transfer_needs_the_factors_and_skips_thin_parks(fixture_balls, transfer_balls):
    # The old fixture carries no exit velocity / launch angle: not graded.
    tr = fc.transfer_check(fixture_balls, _flat_doc({"1": 0.0}))
    assert tr["graded"] is False and tr["parks"] == []
    assert "exit velocity" in tr["reason"]
    # The 200-ball floor: neither park of the transfer fixture qualifies.
    tr = fc.transfer_check(transfer_balls, _flat_doc({"1": 0.0, "2": 20.0}))
    assert tr["parks"] == [] and tr["graded"] is False
    assert tr["reason"] == "no park with 200+ decided balls"
    # A null offset reads as no offset.
    assert fc.document_offsets({"carry_offset_ft": {"1": None, "2": 20.0}}) == {2: 20.0}
    assert fc.document_offsets({}) == {}


def test_run_check_and_verdicts_carry_a7(transfer_balls):
    summary = fc.run_check(transfer_balls, _flat_doc({"1": 0.0, "2": 20.0}), [2024])
    assert "transfer" in summary
    checks = [v["check"] for v in fc.verdicts(summary)]
    assert checks == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]


# ---------------------------------------------------------------------------
# B2 redesigned: the expectation's own error and the --min-games floor
# ---------------------------------------------------------------------------


class _Rates:
    """A stand-in for ``sim523_game_set.PoolRates``: per (actor, season) the
    rate and the balls-in-play count."""

    def __init__(self, batters: dict[int, tuple[float, float]], pitchers: dict):
        self._rates = {"batter_id": batters, "pitcher_id": pitchers}
        self.bip = {
            actor: {(a, 2024): np.array([n, 0.0, 0.0, 0.0, p * n, 0.0]) for a, (p, n) in d.items()}
            for actor, d in self._rates.items()
        }

    def actor_rates(self, actor: str, key: tuple[int, int]):
        p_n = self._rates[actor].get(key[0])
        if p_n is None:
            return None
        p, n = p_n
        out = {"k_pa": 0.2, "bb_pa": 0.08, "hbp_pa": 0.01, "_pa": 4 * n}
        if n >= 30:
            out["hr_bip"] = p
        return out


def test_expectation_se_is_the_standard_error_of_the_batters_mean():
    # Twelve rated batters at p 0.05 on n 400 each; one rated starter with no
    # home-run-per-ball-in-play rate (fewer than 30 balls): the batter side alone.
    batters = dict.fromkeys(range(1, 13), (0.05, 400.0))
    pitchers = {100: (0.04, 10.0)}
    lineup = {"batters": list(batters), "pitchers": [100]}
    se = fc.expectation_se(_Rates(batters, pitchers), 2024, lineup)
    var_bat = 12 * (0.05 * 0.95 / 400) / 12**2
    assert se == pytest.approx(0.5 * np.sqrt(var_bat))
    # A rated starter adds (0.5 * 0.6)^2 times his own term.
    pitchers = {100: (0.04, 300.0)}
    se = fc.expectation_se(_Rates(batters, pitchers), 2024, lineup)
    var_pit = (0.04 * 0.96 / 300) / 1
    assert se == pytest.approx(np.sqrt(0.25 * var_bat + (0.5 * 0.6) ** 2 * var_pit))
    # An unrated batter (no row) is skipped like the expectation skips him;
    # fewer than 12 rated batters, or no rated starter, is None.
    rates = _Rates(batters, pitchers)
    lineup2 = {"batters": [*list(batters), 99], "pitchers": [100]}
    assert fc.expectation_se(rates, 2024, lineup2) == pytest.approx(se)
    assert fc.expectation_se(rates, 2024, {"batters": [1], "pitchers": [100]}) is None
    assert fc.expectation_se(rates, 2024, {"batters": list(batters), "pitchers": []}) is None


def test_park_table_min_games_and_the_combined_se():
    per_game = [
        {"game_pk": 1, "venue_id": 19, "season": 2024, "HR": 60, "BIP": 1000},
        {"game_pk": 2, "venue_id": 19, "season": 2024, "HR": 40, "BIP": 1000},
        {"game_pk": 3, "venue_id": 19, "season": 2024, "HR": 50, "BIP": 2000},
        {"game_pk": 4, "venue_id": 3, "season": 2024, "HR": 45, "BIP": 1000},
        {"game_pk": 5, "venue_id": 7, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 6, "venue_id": 7, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 7, "venue_id": 7, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 8, "venue_id": 2, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 9, "venue_id": 2, "season": 2024, "HR": 30, "BIP": 1000},
        {"game_pk": 10, "venue_id": 2, "season": 2024, "HR": 30, "BIP": 1000},
    ]
    expected = {1: 0.04, 2: 0.04, 3: 0.04, 4: 0.01, 5: 0.03, 6: 0.03, 7: 0.03}
    expected.update({8: 0.05, 9: 0.05, 10: 0.05})
    expected_se = {1: 0.002, 2: 0.002, 3: 0.004, 4: 0.002, 8: 0.001, 9: 0.001, 10: 0.001}
    factor = {19: 1.2, 3: 1.0, 7: 0.85, 2: 1.05}
    out = fc.park_table(per_game, expected, factor, expected_se=expected_se, min_games=3)
    rows = {r["venue"]: r for r in out["rows"]}
    # Fenway (one game, residual +0.035 — far beyond any SE) does not enter:
    # no flag, and no part in r.
    assert rows[3]["entered"] is False and rows[3]["flagged"] is False
    assert out["n_entered"] == 3 and out["min_games"] == 3
    # Coors: three games; the expectation SE is the BIP-weighted mean of the
    # games' (0.002, 0.002, 0.004 at 1000 / 1000 / 2000 BIP = 0.003), the sim
    # SE the binomial one on 4000 BIP, combined in quadrature.
    coors = rows[19]
    sim = 150 / 4000
    se_sim = np.sqrt(sim * (1 - sim) / 4000)
    assert coors["se"] == pytest.approx(se_sim)
    assert coors["se_exp"] == pytest.approx(0.003)
    assert coors["se_combined"] == pytest.approx(np.sqrt(se_sim**2 + 0.003**2))
    assert coors["residual"] == pytest.approx(sim - 0.04)
    assert coors["flagged"] is False  # |-0.0025| < 3 x 0.0043
    # Camden: sim 0.03 against 0.05, combined SE ~0.0029: beyond 3 SE.
    camden = rows[2]
    assert camden["se_exp"] == pytest.approx(0.001)
    assert camden["flagged"] is True
    # Kauffman: no expectation SE given -> the sim's alone; on the target.
    assert rows[7]["se_exp"] == 0.0 and rows[7]["flagged"] is False
    assert out["flagged"] == [2] and out["passed"] is False
    xs = [rows[v]["residual"] for v in (2, 7, 19)]
    ys = [factor[v] - 1.0 for v in (2, 7, 19)]
    assert out["r"] == pytest.approx(float(np.corrcoef(xs, ys)[0, 1]))
    # The verdict names the flagged venue and reports r without grading it.
    s = _summary()
    s["park"] = out
    v = {r["check"]: r for r in fc.verdicts(s)}["B2"]
    assert v["passed"] is False and "Camden" in v["text"]
    assert "3 of 4 venues with 3+ games enter" in v["text"]
    assert "reported, not graded" in v["text"]
    # Without Camden's games the read passes on the two entering venues,
    # whatever r (NaN below three).
    out = fc.park_table(per_game[:7], expected, factor, expected_se=expected_se, min_games=3)
    assert out["flagged"] == [] and out["passed"] is True and np.isnan(out["r"])


def test_counters_read_with_and_without_the_margin_band():
    fence = [90, 800, 0, 1, 3, 500]  # air = 391
    c = fc.counters_read(fence)
    assert "margin" not in c
    c = fc.counters_read(fence, [250, 30, 611])
    m = c["margin"]
    assert m["counts"] == [250, 30, 611]
    assert m["applied_share_of_air"] == pytest.approx(250 / 391)
    assert m["fallback_share_of_air"] == pytest.approx(30 / 391)
    assert m["skipped"] == 611
    assert "margin" not in fc.counters_read(fence, [1, 2])  # a short array is ignored
    assert "margin" not in fc.counters_read(fence, [])
    # The printer takes both shapes.
    lines: list[str] = []
    fc.print_counters(c, lines.append)
    assert any(line.startswith("  wall-margin band (informational): applied 250") for line in lines)
    lines.clear()
    fc.print_counters(fc.counters_read(fence), lines.append)
    assert not any("wall-margin" in line for line in lines)


def test_print_transfer_prints_both_shapes(transfer_balls):
    lines: list[str] = []
    tr = fc.transfer_check(transfer_balls, _flat_doc(None), min_decided=20)
    fc.print_transfer(tr, lines.append)
    assert lines[0].startswith("=== A7. the transfer")
    assert any("(c) needs the document's carry offsets" in line for line in lines)
    assert not any("(c) + offset" in line for line in lines)
    lines.clear()
    tr = fc.transfer_check(transfer_balls, _flat_doc({"1": 0.0, "2": 20.0}), min_decided=20)
    fc.print_transfer(tr, lines.append)
    assert any("(c) + offset" in line for line in lines)
    assert sum(line.startswith(("  Camden", "  Angel")) for line in lines) == 2
    # An empty read prints its reason and nothing else.
    lines.clear()
    fc.print_transfer(fc.transfer_check(transfer_balls, _flat_doc(None)), lines.append)
    assert lines[1].startswith("  no park read: no park with 200+ decided balls")
