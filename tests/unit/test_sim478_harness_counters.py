"""SIM-478/479/480 — the fence counters in the harness, the wall-play probe's
arithmetic and the acceptance lane's JSON record.

Three pure functions, no live data:

* ``scripts/sim_stats.py`` ``_fence_counter_summary`` (the one report line and
  the JSON record) on a six-entry and a five-entry counter array,
  ``_fence_d_read`` (the §7 D thresholds), and ``_margin_counter_summary``
  (the wall-margin band's line and record, the plan's §12.7).
* ``scripts/sim478_wall_zone_probe.py`` ``wall_zone_shares`` / ``verdict_lines``
  (the shares, their standard errors and the PASS / FAIL rule) on hand-made
  tallies.
* ``tests/acceptance/conftest.py`` ``_lane_json`` on a hand-made
  ``AcceptanceRun`` (the keys the certification script reads).
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sim_stats = _load("sim_stats")
probe = _load("sim478_wall_zone_probe")


# ---------------------------------------------------------------------------
# sim_stats: the fence counter line and the D read
# ---------------------------------------------------------------------------


class TestFenceCounterSummary:
    def test_six_entries_count_air_balls_without_the_ground_balls(self) -> None:
        # over 90, short 1200 (of which 500 ground balls / popups), band 0,
        # passed 1, no rows 2, not an air ball 500 -> air = 90 + 700 + 0 + 1.
        text, rec = sim_stats._fence_counter_summary([90, 1200, 0, 1, 2, 500])
        assert rec["fence_counts"] == [90, 1200, 0, 1, 2, 500]
        assert rec["fence_air_balls"] == 791
        assert rec["fence_over_share"] == pytest.approx(90 / 791)
        assert rec["fence_passed_share"] == pytest.approx(1 / 791)
        assert rec["fence_no_rows_share"] == pytest.approx(2 / 791)
        assert text.startswith("fence stage: over 90 (11.4% of air balls)")
        assert "· short 1200 · band 0 · passed 1 (0.13%)" in text
        assert "· no matching rows 2 (0.25%)" in text
        assert text.endswith("· not an air ball 500")

    def test_five_entries_do_not_crash_and_say_so(self) -> None:
        text, rec = sim_stats._fence_counter_summary([9, 91, 0, 0, 0])
        assert rec["fence_counts"] == [9, 91, 0, 0, 0]
        assert rec["fence_air_balls"] == 100
        assert rec["fence_over_share"] == pytest.approx(0.09)
        assert "five-entry sampler" in text
        assert "not an air ball n/a" in text

    def test_empty_or_missing_counters_read_no_air_balls(self) -> None:
        for counts in (None, [], [0, 0, 0, 0, 0, 0]):
            text, rec = sim_stats._fence_counter_summary(counts)
            assert rec["fence_air_balls"] == 0
            assert rec["fence_over_share"] is None
            assert "n/a" in text
            d_text, ok = sim_stats._fence_d_read(rec)
            assert not ok
            assert d_text.endswith("FAIL")

    def test_numpy_counters_are_accepted(self) -> None:
        np = pytest.importorskip("numpy")
        _, rec = sim_stats._fence_counter_summary(np.array([9, 91, 0, 0, 0, 0], dtype=np.int64))
        assert rec["fence_counts"] == [9, 91, 0, 0, 0, 0]
        assert json.dumps(rec)  # plain ints, so the record serialises


class TestMarginCounterSummary:
    def test_the_line_and_the_record_with_the_air_count(self) -> None:
        text, rec = sim_stats._margin_counter_summary([250, 30, 611], 791)
        assert (
            text == "wall-margin band: applied 250 (31.6% of air balls) · fallback 30 · skipped 611"
        )
        assert rec == {
            "bb_margin_counts": [250, 30, 611],
            "bb_margin_applied_share": pytest.approx(250 / 791),
        }
        assert json.dumps(rec)

    def test_without_the_air_count_the_share_is_not_claimed(self) -> None:
        text, rec = sim_stats._margin_counter_summary([250, 30, 611])
        assert "(n/a of air balls)" in text
        assert rec["bb_margin_applied_share"] is None
        text, rec = sim_stats._margin_counter_summary([250, 30, 611], 0)
        assert rec["bb_margin_applied_share"] is None

    def test_empty_or_numpy_counters(self) -> None:
        text, rec = sim_stats._margin_counter_summary(None, 100)
        assert rec["bb_margin_counts"] == [] and rec["bb_margin_applied_share"] == 0.0
        assert text == "wall-margin band: applied 0 (0.0% of air balls) · fallback 0 · skipped 0"
        np = pytest.importorskip("numpy")
        _, rec = sim_stats._margin_counter_summary(np.array([5, 1, 2], dtype=np.int64), 10)
        assert rec["bb_margin_counts"] == [5, 1, 2]
        assert rec["bb_margin_applied_share"] == pytest.approx(0.5)
        assert json.dumps(rec)

    def test_the_realism_flags_name_the_three_amendment_flags(self) -> None:
        for name in ("SIM_CARRY_OFFSET", "SIM_BB_BORN_PER_FEATURE", "SIM_BB_MARGIN_BAND"):
            assert name in sim_stats._REALISM_FLAGS


class TestFenceDRead:
    def test_inside_every_threshold_passes(self) -> None:
        _, rec = sim_stats._fence_counter_summary([90, 910, 0, 0, 0, 0])
        text, ok = sim_stats._fence_d_read(rec)
        assert ok
        assert text == (
            "D: passed 0.00% (<= 0.1%), no rows 0.00% (<= 0.5%), over share 9.0% (9 ± 1%) — PASS"
        )

    @pytest.mark.parametrize(
        "counts",
        [
            [90, 908, 0, 2, 0, 0],  # passed 0.2% > 0.1%
            [90, 910, 0, 0, 6, 0],  # no rows 0.6% > 0.5%
            [110, 890, 0, 0, 0, 0],  # over 11% outside 9 ± 1
            [70, 930, 0, 0, 0, 0],  # over 7% outside 9 ± 1
        ],
    )
    def test_one_threshold_outside_fails(self, counts: list[int]) -> None:
        _, rec = sim_stats._fence_counter_summary(counts)
        text, ok = sim_stats._fence_d_read(rec)
        assert not ok
        assert text.endswith("FAIL")


# ---------------------------------------------------------------------------
# the wall-play probe: shares, standard errors, verdicts
# ---------------------------------------------------------------------------


def _tally(n: int, born: dict[str, int], drawn: dict[str, int]) -> dict[str, int]:
    t = {"n": n}
    for oc, k in born.items():
        t[f"born_{oc}"] = k
    for oc, k in drawn.items():
        t[f"drawn_{oc}"] = k
    return t


class TestWallZoneShares:
    def test_classify_reads_the_pool_codes(self) -> None:
        assert probe.classify(4, 0) == "home_run"
        assert probe.classify(3, 0) == "triple"
        assert probe.classify(2, 0) == "double"
        assert probe.classify(1, 0) == "single"
        assert probe.classify(0, 1) == "out"
        assert probe.classify(0, 2) == "out"
        assert probe.classify(0, 0) == "other"

    def test_shares_se_and_pass_rule(self) -> None:
        # The production shape: the stage draws 0 home runs on the short side
        # and only home runs on the over side, while the born rows carry the
        # pool's own reading (10% short, 90% over).
        # short: 100 balls, born 10 HR / 20 2B / 2 3B / 60 out; drawn 0 HR /
        # 25 2B / 2 3B / 65 out. Among the non-home-run plays: born n 90
        # (2B 0.222, out 0.667), drawn n 100 (2B 0.25, out 0.65).
        tallies = {
            "short": _tally(
                100,
                {"home_run": 10, "double": 20, "triple": 2, "out": 60},
                {"home_run": 0, "double": 25, "triple": 2, "out": 65},
            ),
            "over": _tally(
                50,
                {"home_run": 45, "double": 3, "triple": 0, "out": 2},
                {"home_run": 50, "double": 0, "triple": 0, "out": 0},
            ),
        }
        s = probe.wall_zone_shares(tallies)
        short = s["short"]
        assert short["n"] == 100
        # The home-run share is graded against the stage's constant, no SE.
        hr = short["shares"]["home_run"]
        assert hr["target"] == 0.0 and hr["se"] == 0.0
        assert hr["born"] == pytest.approx(0.10) and hr["drawn"] == 0.0
        assert hr["tolerance"] == probe.SLACK and hr["pass"]
        # The wall play: the shares among the non-home-run plays, the SE of
        # the born denominator (90).
        nh = short["not_home_run"]
        assert nh["born_n"] == 90 and nh["drawn_n"] == 100
        d = short["shares"]["double"]
        assert d["born"] == pytest.approx(20 / 90)
        assert d["drawn"] == pytest.approx(0.25)
        assert d["se"] == pytest.approx(math.sqrt((20 / 90) * (70 / 90) / 90))
        assert d["tolerance"] == pytest.approx(2 * d["se"] + 0.01)
        assert d["graded"] and d["pass"]
        assert short["shares"]["out"]["pass"]
        assert short["pass"]
        # over: drawn 1.0 against the stage's 1.0 PASSES although the born
        # share is 0.90; the non-home-run shares are not graded (drawn n 0).
        over = s["over"]
        hr = over["shares"]["home_run"]
        assert hr["target"] == 1.0 and hr["born"] == pytest.approx(0.90)
        assert hr["drawn"] == 1.0 and hr["pass"]
        assert over["not_home_run"] == {"born_n": 5, "drawn_n": 0}
        assert over["shares"]["double"]["born"] == pytest.approx(0.6)
        assert not over["shares"]["double"]["graded"]
        assert not over["shares"]["double"]["pass"]
        assert over["pass"]
        assert s["all_pass"]
        lines = probe.verdict_lines(s)
        assert (
            "PASS C over: home run share drawn 1.000 vs the stage's 1.0 (born 0.900, n 50)" in lines
        )
        assert any(line.startswith("INFO C over: double share") for line in lines)

    def test_a_home_run_drawn_short_or_missed_over_fails(self) -> None:
        # A drawn home run on the short side, or a non-home-run on the over
        # side, beyond the slack: the stage did not do what it guarantees.
        tallies = {
            "short": _tally(
                100,
                {"home_run": 10, "double": 20, "triple": 2, "out": 60},
                {"home_run": 5, "double": 25, "triple": 2, "out": 60},
            ),
            "over": _tally(
                50,
                {"home_run": 45, "double": 3, "triple": 0, "out": 2},
                {"home_run": 45, "double": 3, "triple": 0, "out": 2},
            ),
        }
        s = probe.wall_zone_shares(tallies)
        assert not s["short"]["shares"]["home_run"]["pass"]
        assert not s["over"]["shares"]["home_run"]["pass"]
        assert not s["all_pass"]
        # The over side's non-home-run shares are graded once a play exists.
        assert s["over"]["shares"]["double"]["graded"]
        assert s["over"]["shares"]["double"]["pass"]

    def test_a_wall_play_share_off_by_more_than_the_tolerance_fails(self) -> None:
        # short: born doubles 20 of 90 non-home-run plays (0.222, SE 0.044);
        # drawn 45 of 100 (0.45) — 0.228 beyond 2 SE + 0.01 = 0.098.
        tallies = {
            "short": _tally(
                100,
                {"home_run": 10, "double": 20, "triple": 2, "out": 60},
                {"home_run": 0, "double": 45, "triple": 2, "out": 45},
            ),
            "over": _tally(
                50,
                {"home_run": 45, "double": 3, "triple": 0, "out": 2},
                {"home_run": 50, "double": 0, "triple": 0, "out": 0},
            ),
        }
        s = probe.wall_zone_shares(tallies)
        assert s["short"]["shares"]["home_run"]["pass"]
        assert not s["short"]["shares"]["double"]["pass"]
        assert not s["short"]["pass"] and not s["all_pass"]

    def test_a_side_without_balls_fails(self) -> None:
        tallies = {
            "short": _tally(
                20,
                {"home_run": 0, "double": 5, "triple": 0, "out": 15},
                {"home_run": 0, "double": 5, "triple": 0, "out": 15},
            ),
            "over": {},
        }
        s = probe.wall_zone_shares(tallies)
        assert s["short"]["pass"]
        assert s["over"]["n"] == 0
        assert not s["over"]["pass"]
        assert not s["all_pass"]
        lines = probe.verdict_lines(s)
        assert (
            lines[0]
            == "PASS C short: home run share drawn 0.000 vs the stage's 0.0 (born 0.000, n 20)"
        )
        assert lines[-1].startswith("FAIL C over: no born air balls within 30 ft")

    def test_verdict_lines_name_every_side_and_outcome(self) -> None:
        tallies = {
            "short": _tally(
                40,
                {"home_run": 4, "double": 8, "triple": 1, "out": 27},
                {"home_run": 0, "double": 9, "triple": 1, "out": 30},
            ),
            "over": _tally(
                40,
                {"home_run": 36, "double": 3, "triple": 0, "out": 1},
                {"home_run": 40, "double": 0, "triple": 0, "out": 0},
            ),
        }
        s = probe.wall_zone_shares(tallies)
        assert s["all_pass"]
        lines = probe.verdict_lines(s)
        assert len(lines) == 8
        assert all(line.startswith(("PASS C ", "INFO C over: ")) for line in lines)
        assert sum(line.startswith("INFO C over: ") for line in lines) == 3
        assert (
            "PASS C over: home run share drawn 1.000 vs the stage's 1.0 (born 0.900, n 40)" in lines
        )
        assert json.dumps(s)  # the record serialises as the --json-out document


class TestTheTapReadsTheStagesCarry:
    """The probe's side is the fence stage's side: the tap reads the born
    ball's carry in the LIVE park's air (SIM-478 §11), the number the
    stage decides on, not the ball's own-park distance."""

    def _sampler(self, *, offsets: bool):
        import numpy as np
        from tests.unit.test_sim478_carry_offset_and_margin import (
            _COORS,
            _FLY,
            _PNC,
            _geometry,
            _pool,
            _sampler,
        )

        rows = [
            {"event": "home_run", "dist": 410.0, "venue": 40},
            {"event": "double", "dist": 385.0, "venue": 40},
        ]
        fp = _sampler(_pool(rows), _geometry(offsets=offsets))
        fp.fence_stage = True
        fp.bb_class_filter = True
        fp.carry_offset = True
        born = {"cls": _FLY, "spray_raw": 0.0, "spray": 0.0, "dist": 391.0, "venue": _PNC, "row": 1}
        return fp, born, _COORS, np

    def _run(self, fp, born, venue, np) -> tuple[str, int, str]:
        tap = probe._Tap(fp)
        tap.install()
        try:
            fp.battedball_new_pa(
                "R", "200:2024", np.zeros(6, np.float32), born_bb=born, venue_id=venue
            )
            side = tap._kept[2]
            drawn = fp.battedball_draw()[0]
        finally:
            tap.remove()
        return side, int(fp._fence_last_dec), drawn

    def test_a_sea_level_ball_born_at_coors_sits_on_the_stages_side(self) -> None:
        # 391 ft by its own distance, 411 in Coors air (+20) against a 400
        # fence: the stage calls it over and draws the home run. The probe
        # files it on the over side too.
        fp, born, coors, np = self._sampler(offsets=True)
        assert self._run(fp, born, coors, np) == ("over", 0, "home_run")
        assert probe._carry_of(fp, born, coors) == 411.0

    def test_without_offsets_both_read_the_own_distance(self) -> None:
        fp, born, coors, np = self._sampler(offsets=False)
        assert self._run(fp, born, coors, np) == ("short", 1, "double")
        assert probe._carry_of(fp, born, coors) == 391.0


# ---------------------------------------------------------------------------
# the acceptance lane's JSON record
# ---------------------------------------------------------------------------


def test_lane_json_carries_the_keys_the_check_reads() -> None:
    from tests.acceptance.conftest import AcceptanceRun, _lane_json

    run = AcceptanceRun(n_games=2, n_iters=3, elapsed_s=12.5)
    run.per_game = [
        {
            "game_pk": 776151,
            "venue_id": 3,
            "season": 2025,
            "iters": 3,
            "HR": 7,
            "H": 51,
            "R": 27,
            "BIP": 160,
            "PA": 230,
        },
        {
            "game_pk": 776142,
            "venue_id": 17,
            "season": 2025,
            "iters": 3,
            "HR": 5,
            "H": 48,
            "R": 22,
            "BIP": 155,
            "PA": 225,
        },
    ]
    run.pool_counts = {"PA": 455, "BIP": 315}
    run.fence_counts = [30, 400, 0, 0, 1, 180]
    run.bb_margin_counts = [120, 9, 481]
    run.cell_index = {"enabled": True, "min_cell": 20}
    run.park_factors = {776151: 0.9937, 776142: 0.9922}
    doc = _lane_json(run, {"SIM_FENCE_STAGE": "1"})
    assert set(doc) == {
        "n_games",
        "n_iters",
        "elapsed_s",
        "flags",
        "per_game",
        "pool_counts",
        "fence_counts",
        "bb_margin_counts",
        "cell_index",
        "park_factors",
    }
    assert doc["n_games"] == 2 and doc["n_iters"] == 3
    assert doc["flags"] == {"SIM_FENCE_STAGE": "1"}
    assert doc["per_game"][0]["venue_id"] == 3
    assert set(doc["per_game"][1]) == {
        "game_pk",
        "venue_id",
        "season",
        "iters",
        "HR",
        "H",
        "R",
        "BIP",
        "PA",
    }
    assert doc["fence_counts"] == [30, 400, 0, 0, 1, 180]
    assert doc["bb_margin_counts"] == [120, 9, 481]
    assert doc["park_factors"] == {"776151": 0.9937, "776142": 0.9922}
    json.dumps(doc)  # every value is JSON-native
    # A run on a sampler without the band's counters writes an empty list.
    run.bb_margin_counts = []
    assert _lane_json(run, {})["bb_margin_counts"] == []


def test_the_lane_flag_table_carries_the_three_amendment_flags() -> None:
    """The lane grades production: the carry offset, the exponent per feature
    and the wall-margin band at their production values, each with a
    SIM523_LANE_ twin for the other arm; the compose file must agree (the
    parity test in ``tests/acceptance``)."""
    from tests.acceptance.conftest import PRODUCTION_FLAGS

    assert PRODUCTION_FLAGS["SIM_CARRY_OFFSET"] == "1"
    assert PRODUCTION_FLAGS["SIM_BB_BORN_PER_FEATURE"] == "1"
    assert PRODUCTION_FLAGS["SIM_BB_MARGIN_BAND"] == "15"
    assert PRODUCTION_FLAGS["SIM_BB_MARGIN_MIN_ROWS"] == "20"
    src = (Path(__file__).resolve().parents[1] / "acceptance" / "conftest.py").read_text(
        encoding="utf-8"
    )
    for twin in (
        'os.environ.get("SIM523_LANE_CARRY_OFFSET", "1")',
        'os.environ.get("SIM523_LANE_BORN_PER_FEATURE", "1")',
        'os.environ.get("SIM523_LANE_MARGIN_BAND", "15")',
        'os.environ.get("SIM523_LANE_MARGIN_MIN_ROWS", "20")',
    ):
        assert twin in src
