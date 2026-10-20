"""The running-game census (``scripts/probes/running_game_census.py``): it reads
where the simulator's steals and pickoffs land, two arms side by side.

Plan: docs/audit/2026-09-29-sim554-running-game-on-the-pitch-plan.md.

These tests pin the census's reading of one step (opportunity, attempt, class,
no-pitch third out, intentional walk, dropped-third-strike reach) and the
report's stop rules, on in-memory fixtures (no live DB, no bundle).
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.script_loader import load_script

census = load_script("running_game_census")


from simulation.game_state import NO_PITCH, PlayResult  # noqa: E402


def _snap(
    *,
    first=None,
    second=None,
    third=None,
    outs=0,
    balls=0,
    strikes=0,
    inning=1,
    half="TOP",
    batter=7,
):
    return census.Snap(
        first=first,
        second=second,
        third=third,
        outs=outs,
        balls=balls,
        strikes=strikes,
        inning=inning,
        half=half,
        batter=batter,
    )


class TestTheCensusStep:
    def test_a_thrown_pitch_with_a_stealable_runner_is_an_opportunity(self):
        rec = census.Recorder()
        rec.start_game_sim()
        pre = _snap(first=11)
        res = PlayResult(pitch_outcome="ball", steal_attempted=True, steal_outcome="safe")
        rec.observe(pre, res, pre)
        assert rec.opp["2"]["ball"] == 1 and rec.att["2"]["ball"] == 1 and rec.safe["2"] == 1
        rec.observe(_snap(first=11, second=12), PlayResult(pitch_outcome="foul"), pre)
        assert rec.opp["3"]["foul"] == 1 and rec.att["3"]["foul"] == 0  # the lead runner
        rec.observe(_snap(first=11, second=12, third=13), PlayResult(pitch_outcome="ball"), pre)
        rec.observe(_snap(), PlayResult(pitch_outcome="in_play"), pre)
        assert sum(rec.opp["2"].values()) + sum(rec.opp["3"].values()) == 2

    def test_a_caught_steal_counts_an_attempt_and_no_safe(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike", steal_attempted=True, steal_outcome="caught"
        )
        rec.observe(_snap(second=12), res, _snap(second=None, outs=1))
        assert rec.att["3"]["called_strike"] == 1 and rec.safe["3"] == 0

    def test_an_intentional_walk_is_skipped(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome="ball", event="intentional_walk", pa_terminal=True)
        rec.observe(_snap(first=11), res, _snap(first=7, second=11))
        assert rec.ibb == 1 and sum(rec.opp["2"].values()) == 0

    def test_a_no_pitch_third_out_and_the_same_batter_next_time(self):
        rec = census.Recorder()
        rec.start_game_sim()
        pre = _snap(first=11, outs=2, balls=3, strikes=2, batter=7)
        res = PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True, pa_voided="pickoff_third_out")
        rec.observe(pre, res, _snap(inning=1, half="BOTTOM", batter=20))
        assert rec.no_pitch == {"n": 1, "half_rolled": 1, "labelled": 1}
        assert rec.pickoff_third_outs == 1 and rec.pickoff_third_outs_pa_credited == 0
        assert rec.pickoffs_by_count == {"3-2": 1}
        assert sum(rec.opp["2"].values()) == 0  # no pitch, no opportunity
        # The other side bats, then the same batter leads off at 0-0.
        rec.observe(_snap(half="BOTTOM", batter=20), PlayResult(pitch_outcome="ball"), pre)
        rec.observe(_snap(inning=2, batter=7), PlayResult(pitch_outcome="ball"), pre)
        assert rec.leadoff == {"same": 1, "other": 0, "unchecked": 0}

    def test_a_different_leadoff_batter_is_counted_and_an_unread_one_is_unchecked(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True, pa_voided="pickoff_third_out")
        rec.observe(_snap(first=11, outs=2, batter=7), res, _snap(half="BOTTOM"))
        rec.observe(_snap(inning=2, batter=8), PlayResult(pitch_outcome="ball"), _snap())
        assert rec.leadoff["other"] == 1
        rec.observe(_snap(first=11, outs=2, batter=8), res, _snap(half="BOTTOM"))
        rec.end_game_sim([], 1.0)  # the game ended before that side batted again
        assert rec.leadoff["unchecked"] == 1

    def test_the_old_order_credits_a_pickoff_third_out_as_a_plate_appearance(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike", pa_terminal=True, event="strikeout", pickoff_out=True
        )
        rec.observe(_snap(first=11, outs=2, strikes=2), res, _snap(half="BOTTOM"))
        assert rec.pickoff_third_outs == 1 and rec.pickoff_third_outs_pa_credited == 1
        assert rec.opp["2"]["called_strike"] == 1  # a pitch was thrown

    def test_a_voided_steal_is_counted_by_reason(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="called_strike",
            pa_terminal=True,
            event="strikeout",
            steal_voided="third_out_first",
        )
        rec.observe(_snap(first=11, outs=2, strikes=2), res, _snap(half="BOTTOM"))
        assert rec.voided == {"third_out_first": 1}

    def test_a_dropped_third_strike_reach_and_the_runners_it_moved(self):
        rec = census.Recorder()
        rec.start_game_sim()
        # A runner on second, first open: unforced. He goes to third.
        res = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            baserunner_advances={7: 1, 12: 3},
        )
        rec.observe(_snap(second=12, outs=1, strikes=2), res, _snap(first=7, third=12, outs=1))
        # Bases loaded, two outs: every runner is forced; the one on third scores.
        res2 = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            baserunner_advances={8: 1, 11: 2, 12: 3, 13: 0},
        )
        rec.observe(
            _snap(first=11, second=12, third=13, outs=2, strikes=2, batter=8),
            res2,
            _snap(first=8, second=11, third=12, outs=2),
        )
        # A plain strikeout reaches nobody.
        rec.observe(
            _snap(first=11, strikes=2),
            PlayResult(pitch_outcome="swinging_strike", pa_terminal=True, event="strikeout"),
            _snap(first=11, outs=1),
        )
        assert rec.d3k == {
            "reaches": 2,
            "runners_on": 4,
            "runners_moved": 4,
            "unforced_chances": 1,
            "unforced_moved": 1,
            "with_running_play": 0,
        }

    def test_a_reach_with_a_steal_on_the_pitch_is_set_aside(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(
            pitch_outcome="swinging_strike",
            pa_terminal=True,
            event="strikeout",
            steal_attempted=True,
            steal_outcome="safe",
            baserunner_advances={7: 1, 11: 2},
        )
        rec.observe(_snap(first=11, strikes=2), res, _snap(first=7, second=11))
        assert rec.d3k["reaches"] == 1 and rec.d3k["with_running_play"] == 1
        assert rec.d3k["runners_on"] == 0

    def test_an_expected_attempt_lands_on_the_class_of_the_pitch(self):
        rec = census.Recorder()
        rec.start_game_sim()
        rec.stage_expected_attempt(2, 0.25)
        rec.observe(_snap(first=11), PlayResult(pitch_outcome="foul"), _snap(first=11))
        rec.stage_expected_attempt(2, 0.5)  # a no-pitch step drops it
        rec.observe(
            _snap(first=11, outs=2),
            PlayResult(pitch_outcome=NO_PITCH, pickoff_out=True),
            _snap(half="BOTTOM"),
        )
        assert rec.exp_attempt["2"]["foul"] == 0.25
        assert sum(rec.exp_attempt["2"].values()) == 0.25

    def test_the_box_and_the_seconds_close_a_game_sim(self):
        rec = census.Recorder()
        rec.start_game_sim()
        res = PlayResult(pitch_outcome="ball", steal_attempted=True, steal_outcome="safe")
        rec.observe(_snap(first=11), res, _snap(second=11))

        class Line:
            def __init__(self, sb, cs):
                self.sb, self.cs = sb, cs

        rec.end_game_sim([Line(1, 0), Line(0, 1), Line(2, 0)], 0.9)
        assert rec.per_sim == [
            {
                "sb": 3,
                "cs": 1,
                "pickoffs": 0,
                "opp2": 1,
                "att2": 1,
                "opp3": 0,
                "att3": 0,
                "seconds": 0.9,
            }
        ]


class _Pool:
    def __init__(self):
        self.pickoff_out = np.array([1, 0, 0, 0], dtype=np.int8)
        self.pickoff_error = np.array([0, 0, 1, 0], dtype=np.int8)
        self.attempted = np.array([0, 1, 0, 1], dtype=np.int8)


class _FakeSampler:
    """The sampler seams the census wraps, with fixed weights."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.pool = _Pool()

    def steal_weights(self, *a, rows=None, aggression=1.0, **kw):
        rows = np.arange(4) if rows is None else np.asarray(rows)
        w = np.array([1.0, 1.0, 2.0, 0.0])[rows] * np.where(
            self.pool.attempted[rows] > 0, aggression, 1.0
        )
        return self.pool, rows, w

    def _steal_meta(self, target):
        cells = {(0, 0, 0): np.arange(4)}
        return {
            "cells": cells,
            "pickoff_cells": cells,
            "pickoff_live": {(0, 0, 0)},
            "class_cells": {(0, 0, 0, 1): np.array([1, 2])},
        }

    def steal_draw(self, *a, **kw):
        self.calls.append(("steal_draw", kw))
        return (False, False, True, True, False)

    def pickoff_draw(self, *a, **kw):
        self.calls.append(("pickoff_draw", kw))
        return (True, False, False)


class TestTheCensusSeams:
    def test_the_expected_shares_read_the_weights(self):
        fp = _FakeSampler()
        kw = {"outs": 0, "balls": 0, "strikes": 0, "score_diff": 0}
        got = census.expected_shares(fp, (2, "r", "p", None), kw, rows=None, aggression=1.0)
        assert got == (0.75, 0.25)

    def test_the_wrappers_go_on_once_and_feed_the_recorder(self):
        fp = _FakeSampler()
        rec = census.Recorder()
        holder = {"rec": rec}
        census.install_seams(fp, holder)
        census.install_seams(fp, holder)  # a second install must not stack the wrappers
        kw = {"outs": 0, "balls": 0, "strikes": 0, "score_diff": 0}
        fp.steal_draw(2, "r", "p", None, aggression=1.0, **kw)  # the single pre-pitch draw
        fp.pickoff_draw(2, "r", "p", None, **kw)  # the pickoff draw before the pitch
        fp.steal_draw(2, "r", "p", None, aggression=1.0, pitch_class="ball", **kw)
        assert [name for name, _ in fp.calls] == ["steal_draw", "pickoff_draw", "steal_draw"]
        assert "pitch_class" not in fp.calls[0][1] and fp.calls[2][1]["pitch_class"] == "ball"
        assert rec.exp_pickoff["2"] == [1.5, 2]  # 0.75 at each of the two pickoff reads
        assert rec.seam_kind == {"out": 1, "out_advancing": 1, "error": 0}
        # The class group holds rows 1 and 2: one attempted, of weights 1 and 2.
        assert rec._staged_attempt == (2, pytest.approx(1.0 / 3.0))


def _summary(
    *,
    arm,
    opp=(1000, 300),
    att=(21.0, 1.5),
    misplaced=0.0,
    exp_po=0.0010,
    third_out_first=0,
    se=0.002,
):
    """A planted arm summary in the shape ``census.summarize`` returns."""
    return {
        "arm": arm,
        "att_per_opp": {"2": (att[0] / opp[0], se), "3": (att[1] / opp[1], se / 2)},
        "n_att": int(att[0] + att[1]),
        "misplaced_share": misplaced,
        "exp_pickoff_per_draw": exp_po,
        "third_out_first": third_out_first,
    }


class TestTheStopRules:
    def _stops(self, off, on):
        return [r.name for r in census.stop_rules(off, on) if not r.ok]

    def test_a_clean_pair_passes_every_rule(self):
        off = _summary(arm=0, misplaced=0.36, exp_po=0.0010)
        on = _summary(arm=1, misplaced=0.01, exp_po=0.00128)
        assert self._stops(off, on) == []

    def test_a_misplaced_share_above_five_percent_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        on = _summary(arm=1, misplaced=0.06, exp_po=0.00128)
        assert self._stops(off, on) == ["misplaced"]

    def test_a_pickoff_ratio_outside_the_window_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        assert self._stops(off, _summary(arm=1, exp_po=0.0011)) == ["pickoff_ratio"]
        assert self._stops(off, _summary(arm=1, exp_po=0.0015)) == ["pickoff_ratio"]

    def test_an_attempt_volume_moved_beyond_two_standard_errors_stops(self):
        off = _summary(arm=0, exp_po=0.0010, se=0.002)
        # 0.021 -> 0.027: 0.006 against an unpaired SE of 0.0028.
        on = _summary(arm=1, exp_po=0.00128, att=(27.0, 1.5), se=0.002)
        assert self._stops(off, on) == ["volume_2"]

    def test_a_third_out_first_on_the_new_arm_stops(self):
        off = _summary(arm=0, exp_po=0.0010, third_out_first=3)
        on = _summary(arm=1, exp_po=0.00128, third_out_first=1)
        assert self._stops(off, on) == ["third_out_first"]

    def test_no_attempt_on_the_new_arm_stops(self):
        off = _summary(arm=0, exp_po=0.0010)
        on = _summary(arm=1, exp_po=0.00128, att=(0.0, 0.0))
        assert "misplaced" in self._stops(off, on)

    def test_the_ratio_estimate_and_its_standard_error(self):
        rate, se = census.ratio_se([(1, 10), (3, 10), (2, 10)])
        assert rate == pytest.approx(0.2)
        # The residuals a - r*o are -1, 1, 0: sqrt(2 * 3/2) / 30.
        assert se == pytest.approx(np.sqrt(3.0) / 30.0)


class TestThePoolReference:
    def test_the_pool_reference_reads_pitch_rows_by_recency(self):
        class P:  # one target's pool: five pitch rows and one pickoff row
            attempted = np.array([1, 1, 0, 0, 1, 0])
            success = np.array([1, 0, 0, 0, 1, 0])
            recency = np.array([2.0, 1.0, 1.0, 1.0, 1.0, 5.0])
            pickoff_out = np.array([0, 0, 1, 0, 0, 1])
            pickoff_error = np.zeros(6)
            pitch_class = np.array([1, 1, 2, 4, 5, 0])

        ref = census.pool_reference({"2": P()})
        t = ref["targets"]["2"]
        assert (t["pitch_rows"], t["pickoff_rows"], t["tags"], t["pickoff_row_outcomes"]) == (
            5,
            1,
            1,
            1,
        )
        assert t["att_rate_rcy"] == pytest.approx(4.0 / 6.0)  # (2 + 1 + 1) / 6
        assert t["safe_share"] == pytest.approx(3.0 / 4.0)  # (2 + 1) / (2 + 1 + 1)
        assert ref["att_share"]["ball"] == pytest.approx(0.75)  # weights 2 + 1 of 4
        assert ref["att_share"]["in_play"] == pytest.approx(0.25)
        assert ref["att_share_raw"]["ball"] == pytest.approx(2.0 / 3.0)
        assert ref["pickoff_ratio"] == pytest.approx(2.0)  # (1 tag + 1 row) / 1 tag


class TestTheCensusSummary:
    def test_the_summary_reads_shares_rates_and_the_misplaced_attempts(self):
        rec = census.Recorder()
        for _ in range(2):
            rec.start_game_sim()
            for cls, att in (
                ("ball", True),
                ("ball", False),
                ("foul", True),
                ("called_strike", False),
            ):
                res = PlayResult(
                    pitch_outcome=cls, steal_attempted=att, steal_outcome="safe" if att else None
                )
                rec.observe(_snap(first=11), res, _snap(first=11))
            rec.add_expected_pickoff(2, 0.002)
            rec.end_game_sim([], 1.0)
        s = census.summarize(rec.to_jsonable() | {"arm": 1, "game_pks": [1], "iters": 2})
        assert s["game_sims"] == 2
        assert s["att_per_opp"]["2"][0] == pytest.approx(0.5)
        assert s["misplaced_share"] == pytest.approx(0.5)
        assert s["att_share"]["ball"] == pytest.approx(0.5)
        assert s["exp_pickoff_per_draw"] == pytest.approx(0.002)
        assert s["safe_share"]["2"] == pytest.approx(1.0)

    def test_the_report_names_every_rule_and_a_verdict(self):
        rec = census.Recorder()
        rec.start_game_sim()
        rec.end_game_sim([], 1.0)
        arm = rec.to_jsonable() | {"game_pks": [1], "iters": 1, "pool": {}}
        lines, stops = census.report_lines(arm | {"arm": 0}, arm | {"arm": 1})
        text = "\n".join(lines)
        for name in ("misplaced", "pickoff_ratio", "volume_2", "volume_3", "third_out_first"):
            assert name in text
        assert "STOP" in text and stops  # an empty census stops: no attempt on either arm
