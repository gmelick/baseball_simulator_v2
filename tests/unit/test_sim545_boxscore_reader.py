"""
tests/unit/test_sim545_boxscore_reader.py
=========================================
The official box-score ground truth (SIM-545): the reader
``real_props_from_boxscore_rows`` on rows parsed from the real captured
payload, the ``pair_props_for_validation`` prop-tuple keyword, and the audit
script's derivation + comparison on hand-built rows.

No network, no DB.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from datetime import date

from pipeline.etl.boxscore_ingest import parse_boxscore
from simulation.prop_distributions import PropDistribution
from simulation.prop_validation import (
    BOXSCORE_BATTER_PROPS,
    BOXSCORE_PITCHER_PROPS,
    DEFAULT_PROP_LINES,
    DERIVABLE_BATTER_PROPS,
    DERIVABLE_PITCHER_PROPS,
    pair_props_for_validation,
    real_props_from_boxscore_rows,
)

# Import the audit script by path (scripts/ is not a package). It MUST be
# registered in sys.modules before exec_module so that @dataclass(slots=True) can
# resolve the module's namespace (dataclasses looks the module up by name).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "sim545_boxscore_audit.py")
_spec = importlib.util.spec_from_file_location("sim545_boxscore_audit", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules["sim545_boxscore_audit"] = audit
_spec.loader.exec_module(audit)

_FIXTURE = os.path.join(_REPO_ROOT, "tests", "fixtures", "mlb", "boxscore_746437.json")
GAME_PK = 746437


def _fixture_rows() -> list[dict]:
    with open(_FIXTURE, encoding="utf-8") as fh:
        teams = json.load(fh)["teams"]
    parsed = parse_boxscore(teams, game_pk=GAME_PK, season=2024, game_date=date(2024, 8, 15))
    return [r.as_dict() for r in parsed]


# ===========================================================================
# real_props_from_boxscore_rows
# ===========================================================================


class TestReaderOnRealRows:
    def test_every_prop_present_for_every_player(self):
        batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
        assert len(batter) == 20  # 9 away + 11 home batters
        assert len(pitcher) == 7  # 3 away + 4 home pitchers
        for totals in batter.values():
            assert tuple(totals) == BOXSCORE_BATTER_PROPS
        for totals in pitcher.values():
            assert tuple(totals) == BOXSCORE_PITCHER_PROPS

    def test_robles_arithmetic(self):
        """Robles: h=1 (a double), r=0, rbi=0 → 1B = 0, 2B = 1, HRR = 1, TB from the box."""
        batter, _ = real_props_from_boxscore_rows(_fixture_rows())
        r = batter[645302]
        assert r["H"] == 1
        assert r["2B"] == 1
        assert r["1B"] == 0
        assert r["3B"] == 0
        assert r["HR"] == 0
        assert r["TB"] == 2
        assert r["SB"] == 1
        assert r["HRR"] == r["R"] + r["H"] + r["RBI"] == 1

    def test_shelby_miller_pitcher_props(self):
        _, pitcher = real_props_from_boxscore_rows(_fixture_rows())
        m = pitcher[571946]
        assert m["OUTS"] == 4
        assert set(m) == set(BOXSCORE_PITCHER_PROPS)

    def test_dnp_absent_and_pitchers_not_in_batter_dict(self):
        """A DH-game pitcher has played_bat false → no batter entry; the bench has no row."""
        batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
        assert 571946 not in batter  # Miller pitched, never batted
        assert 645302 not in pitcher  # Robles batted, never pitched
        assert 700187 not in batter and 700187 not in pitcher  # Troy Taylor: bullpen, DNP

    def test_side_hit_totals_match_the_box(self):
        with open(_FIXTURE, encoding="utf-8") as fh:
            teams = json.load(fh)["teams"]
        rows = _fixture_rows()
        batter, _ = real_props_from_boxscore_rows(rows)
        side_of = {r["player_id"]: r["side"] for r in rows}
        for side in ("home", "away"):
            box_hits = int(teams[side]["teamStats"]["batting"]["hits"])
            assert sum(t["H"] for pid, t in batter.items() if side_of[pid] == side) == box_hits


class TestReaderOnHandRows:
    @staticmethod
    def _row(pid: int, **over):
        base = {
            "player_id": pid,
            "played_bat": False,
            "played_pitch": False,
            "h": 0,
            "b2": 0,
            "b3": 0,
            "hr": 0,
            "r": 0,
            "rbi": 0,
            "sb": 0,
            "tb": 0,
            "p_k": 0,
            "p_bb": 0,
            "p_er": 0,
            "p_outs": 0,
            "p_h": 0,
        }
        base.update(over)
        return base

    def test_singles_and_hrr_arithmetic(self):
        row = self._row(1, played_bat=True, h=4, b2=1, b3=1, hr=1, r=2, rbi=3, tb=11, sb=1)
        batter, pitcher = real_props_from_boxscore_rows([row])
        assert batter[1] == {
            "H": 4,
            "HR": 1,
            "TB": 11,
            "RBI": 3,
            "1B": 1,
            "2B": 1,
            "3B": 1,
            "R": 2,
            "SB": 1,
            "HRR": 9,
        }
        assert pitcher == {}

    def test_two_way_row_lands_in_both(self):
        row = self._row(
            2, played_bat=True, played_pitch=True, h=1, p_k=7, p_outs=18, p_h=5, p_bb=2, p_er=1
        )
        batter, pitcher = real_props_from_boxscore_rows([row])
        assert batter[2]["H"] == 1
        assert pitcher[2] == {"K": 7, "BB": 2, "ER": 1, "OUTS": 18, "H_ALLOWED": 5}

    def test_dnp_row_is_absent_from_both(self):
        """A row with both flags false (should not exist, but must not crash) is skipped."""
        batter, pitcher = real_props_from_boxscore_rows([self._row(3, h=2)])
        assert batter == {} and pitcher == {}

    def test_null_counts_read_zero(self):
        row = self._row(4, played_bat=True, h=None, tb=None, r=None, rbi=None)
        batter, _ = real_props_from_boxscore_rows([row])
        assert batter[4]["H"] == 0 and batter[4]["HRR"] == 0

    def test_empty(self):
        assert real_props_from_boxscore_rows([]) == ({}, {})

    def test_default_lines_cover_every_boxscore_prop(self):
        for prop in BOXSCORE_BATTER_PROPS + BOXSCORE_PITCHER_PROPS:
            assert prop in DEFAULT_PROP_LINES
            assert DEFAULT_PROP_LINES[prop] % 1 == 0.5  # half-integer: no push

    def test_prop_tuples_grade_every_priced_prop_by_membership(self):
        """The box grades exactly the props the simulator prices; order is not a contract.

        The display order belongs to ``simulation.prop_distributions``
        (``BATTER_PROPS`` / ``PITCHER_PROPS``); nothing renders from these
        tuples, so the comparison is by set, never by position.
        """
        from simulation.prop_distributions import BATTER_PROPS, PITCHER_PROPS

        assert set(BOXSCORE_BATTER_PROPS) == set(BATTER_PROPS)
        assert set(BOXSCORE_PITCHER_PROPS) == set(PITCHER_PROPS)
        assert len(set(BOXSCORE_BATTER_PROPS)) == len(BOXSCORE_BATTER_PROPS)
        assert len(set(BOXSCORE_PITCHER_PROPS)) == len(BOXSCORE_PITCHER_PROPS)
        assert set(DERIVABLE_BATTER_PROPS) <= set(BOXSCORE_BATTER_PROPS)
        assert set(DERIVABLE_PITCHER_PROPS) <= set(BOXSCORE_PITCHER_PROPS)


# ===========================================================================
# pair_props_for_validation with the box-score tuples
# ===========================================================================


class _FakePset:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, pid, prop=None):
        return self._m.get((int(pid), prop))


class TestPairPropsBoxscoreTuples:
    def test_default_pairs_only_the_event_label_props(self):
        """The default keyword keeps scripts/validate_props.py unchanged."""
        pset = _FakePset(
            {
                (10, "H"): PropDistribution.from_samples(10, "H", [1, 2]),
                (10, "R"): PropDistribution.from_samples(10, "R", [0, 1]),
                (99, "OUTS"): PropDistribution.from_samples(99, "OUTS", [15, 18]),
            }
        )
        batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
        bucket: dict = {}
        added = pair_props_for_validation(pset, {10: batter[645302]}, {99: pitcher[571946]}, bucket)
        assert added == 1
        assert list(bucket) == [("H", DEFAULT_PROP_LINES["H"])]

    def test_boxscore_tuples_pair_every_market(self):
        pset = _FakePset(
            {
                (10, "H"): PropDistribution.from_samples(10, "H", [1, 2]),
                (10, "R"): PropDistribution.from_samples(10, "R", [0, 1]),
                (10, "HRR"): PropDistribution.from_samples(10, "HRR", [1, 3]),
                (99, "OUTS"): PropDistribution.from_samples(99, "OUTS", [15, 18]),
                (99, "H_ALLOWED"): PropDistribution.from_samples(99, "H_ALLOWED", [4, 6]),
            }
        )
        batter, pitcher = real_props_from_boxscore_rows(_fixture_rows())
        bucket: dict = {}
        added = pair_props_for_validation(
            pset,
            {10: batter[645302]},
            {99: pitcher[571946]},
            bucket,
            batter_props=BOXSCORE_BATTER_PROPS,
            pitcher_props=BOXSCORE_PITCHER_PROPS,
        )
        assert added == 5
        assert ("OUTS", 16.5) in bucket
        (_dist, actual) = bucket[("OUTS", 16.5)][0]
        assert actual == 4
        (_dist, hrr_actual) = bucket[("HRR", 1.5)][0]
        assert hrr_actual == 1

    def test_missing_prop_in_totals_is_skipped_not_keyerror(self):
        """Event-label actuals (H/HR/TB only) paired with the box tuples: no crash."""
        pset = _FakePset({(10, "R"): PropDistribution.from_samples(10, "R", [0, 1])})
        bucket: dict = {}
        added = pair_props_for_validation(
            pset,
            {10: {"H": 1, "HR": 0, "TB": 1}},
            {},
            bucket,
            batter_props=BOXSCORE_BATTER_PROPS,
        )
        assert added == 0
        assert bucket == {}


# ===========================================================================
# The audit script: derivation + comparison on hand-built rows
# ===========================================================================


def _pitch(
    game_pk: int,
    batter: int,
    pitcher: int,
    events: str | None = None,
    **over,
) -> dict:
    row = {
        "game_pk": game_pk,
        "batter": batter,
        "pitcher": pitcher,
        "events": events,
        "on_1b": None,
        "on_2b": None,
        "on_3b": None,
        "runner_1b_scored": False,
        "runner_2b_scored": False,
        "runner_3b_scored": False,
        "runs_on_pitch": 0,
        "rbis_on_pitch": 0,
        "earned_runs_on_pitch": 0,
        "sb_ok_2b": False,
        "sb_ok_3b": False,
        "sb_ok_home": False,
        "outs_recorded": 0,
    }
    row.update(over)
    return row


class TestAuditDerivation:
    def test_batter_hits_and_bases(self):
        rows = [
            _pitch(1, 10, 99, "single"),
            _pitch(1, 10, 99, "double"),
            _pitch(1, 10, 99, "triple"),
            _pitch(1, 10, 99, "home_run", runs_on_pitch=1, rbis_on_pitch=1, earned_runs_on_pitch=1),
            _pitch(1, 10, 99, None),  # a mid-PA pitch: no event, nothing counted
            _pitch(1, 10, 99, "field_out", outs_recorded=1),
        ]
        batters, pitchers = audit.derive_totals(rows)
        b = batters[(1, 10)]
        assert (b["H"], b["1B"], b["2B"], b["3B"], b["HR"], b["TB"]) == (4, 1, 1, 1, 1, 10)
        assert b["R"] == 1  # the home run is the batter's own run
        assert b["RBI"] == 1
        p = pitchers[(1, 99)]
        assert p["H_ALLOWED"] == 4
        assert p["ER"] == 1
        assert p["OUTS"] == 1

    def test_runs_credited_to_the_runners_then_the_batter(self):
        # Bases loaded, a grand slam: three runner flags + the batter's own run.
        rows = [
            _pitch(
                1,
                10,
                99,
                "home_run",
                on_1b=21,
                on_2b=22,
                on_3b=23,
                runner_1b_scored=True,
                runner_2b_scored=True,
                runner_3b_scored=True,
                runs_on_pitch=4,
                rbis_on_pitch=4,
            )
        ]
        batters, _ = audit.derive_totals(rows)
        assert batters[(1, 10)]["R"] == 1
        assert batters[(1, 10)]["RBI"] == 4
        for runner in (21, 22, 23):
            assert batters[(1, runner)]["R"] == 1
            assert batters[(1, runner)]["H"] == 0

    def test_runs_without_the_batter_scoring(self):
        # A single scores the runner from 2B only: runs_on_pitch == flags → no own run.
        rows = [
            _pitch(
                1,
                10,
                99,
                "single",
                on_2b=22,
                runner_2b_scored=True,
                runs_on_pitch=1,
                rbis_on_pitch=1,
            )
        ]
        batters, _ = audit.derive_totals(rows)
        assert batters[(1, 10)]["R"] == 0
        assert batters[(1, 22)]["R"] == 1

    def test_steals_map_to_the_runner_on_the_origin_base(self):
        rows = [
            _pitch(1, 10, 99, None, on_1b=21, sb_ok_2b=True),
            _pitch(1, 10, 99, None, on_2b=21, sb_ok_3b=True),
            _pitch(1, 10, 99, None, on_3b=21, sb_ok_home=True),
            _pitch(1, 10, 99, None, on_1b=31, sb_ok_2b=False),  # caught: no credit
        ]
        batters, _ = audit.derive_totals(rows)
        assert batters[(1, 21)]["SB"] == 3
        # A runner never credited has no record; the comparison reads him as 0.
        assert (1, 31) not in batters
        assert batters[(1, 10)]["SB"] == 0

    def test_pitcher_k_bb_and_play_events(self):
        rows = [
            _pitch(1, 10, 99, "strikeout", outs_recorded=1),
            _pitch(1, 11, 99, "strikeout_double_play", outs_recorded=2),
            _pitch(1, 12, 99, "walk"),
            _pitch(1, 13, 99, "field_out", outs_recorded=1),
            _pitch(1, 14, 99, None, outs_recorded=1),  # a mid-PA caught stealing
        ]
        events = [
            {"game_pk": 1, "pitcher_id": 99, "event_type": "pickoff", "is_out": True},
            {"game_pk": 1, "pitcher_id": 99, "event_type": "intent_walk", "is_out": False},
            {"game_pk": 1, "pitcher_id": None, "event_type": "pickoff", "is_out": True},
        ]
        _, pitchers = audit.derive_totals(rows, events)
        p = pitchers[(1, 99)]
        assert p["K"] == 2
        assert p["BB"] == 2  # the walk + the intentional walk from play_events
        assert p["OUTS"] == 1 + 2 + 1 + 1 + 1  # pitch outs + the pickoff
        assert p["H_ALLOWED"] == 0

    def test_games_are_kept_apart(self):
        rows = [
            _pitch(1, 10, 99, "single"),
            _pitch(2, 10, 99, "single"),
            _pitch(2, 10, 99, "single"),
        ]
        batters, pitchers = audit.derive_totals(rows)
        assert batters[(1, 10)]["H"] == 1
        assert batters[(2, 10)]["H"] == 2
        assert pitchers[(2, 99)]["H_ALLOWED"] == 2

    def test_every_stat_key_present(self):
        batters, pitchers = audit.derive_totals([_pitch(1, 10, 99, None)])
        assert tuple(batters[(1, 10)]) == audit.BATTER_STATS
        assert tuple(pitchers[(1, 99)]) == audit.PITCHER_STATS


class TestAuditComparison:
    def test_official_totals_keyed_by_game(self):
        rows = _fixture_rows()
        batters, pitchers = audit.official_totals(rows)
        assert (GAME_PK, 645302) in batters
        assert (GAME_PK, 571946) in pitchers
        assert batters[(GAME_PK, 645302)]["2B"] == 1

    def test_compare_exact_rate_mean_abs_and_worst(self):
        official = {
            (1, 10): {"H": 2, "R": 1},
            (1, 11): {"H": 0, "R": 0},
            (1, 12): {"H": 3, "R": 2},
            (2, 10): {"H": 1, "R": 0},
        }
        derived = {
            (1, 10): {"H": 2, "R": 1},  # exact
            (1, 11): {"H": 1, "R": 0},  # H off by +1
            # (1, 12) never derived → reads 0 on every stat: H off by -3, R by -2
            (2, 10): {"H": 1, "R": 0},  # exact
            (3, 50): {"H": 9, "R": 9},  # derived-only: not in the universe
        }
        cmps = {c.stat: c for c in audit.compare_totals(derived, official, ("H", "R"))}
        h = cmps["H"]
        assert h.n == 4
        assert h.n_exact == 2
        assert h.exact_rate == 0.5
        assert h.mean_abs_diff == (1 + 3) / 4
        assert h.mean_signed_diff == (1 - 3) / 4
        assert [(w.game_pk, w.player_id, w.derived, w.official) for w in h.worst] == [
            (1, 12, 0, 3),
            (1, 11, 1, 0),
        ]
        r = cmps["R"]
        assert r.n == 4 and r.n_exact == 3
        assert r.worst[0].abs_diff == 2
        assert audit.derived_only_keys(derived, official) == [(3, 50)]

    def test_worst_is_capped_and_stably_ordered(self):
        official = {(1, pid): {"K": 0} for pid in range(20)}
        derived = {(1, pid): {"K": 1} for pid in range(20)}
        (c,) = audit.compare_totals(derived, official, ("K",), n_worst=10)
        assert len(c.worst) == 10
        assert [w.player_id for w in c.worst] == list(range(10))

    def test_stat_missing_from_official_is_skipped(self):
        (c,) = audit.compare_totals({}, {(1, 1): {"H": 1}}, ("OUTS",))
        assert c.n == 0
        assert math.isnan(c.exact_rate) and math.isnan(c.mean_abs_diff)

    def test_end_to_end_perfect_agreement_on_the_fixture_reader(self):
        """Official vs official through the same reader: every stat exact."""
        rows = _fixture_rows()
        ob, op_ = audit.official_totals(rows)
        for cmps in (
            audit.compare_totals(ob, ob, audit.BATTER_STATS),
            audit.compare_totals(op_, op_, audit.PITCHER_STATS),
        ):
            for c in cmps:
                assert c.n > 0
                assert c.exact_rate == 1.0
                assert c.worst == []

    def test_format_report_renders_every_stat(self):
        official = {(1, 10): {"H": 2}, (1, 11): {"H": 1}}
        derived = {(1, 10): {"H": 2}, (1, 11): {"H": 0}}
        cmps = audit.compare_totals(derived, official, ("H",))
        text = audit.format_report(cmps, [], n_games=1, batter_only=[(9, 9)])
        assert "SIM-545 box-score audit — 1 games" in text
        assert "BATTERS" in text and "PITCHERS" in text
        assert "0.5000" in text  # the exact rate
        assert "worst H" in text
        assert "derived-only player-games" in text

    def test_sql_builders_use_the_shared_out_and_steal_labels(self):
        sql = audit.pitch_rows_sql()
        assert "raw.pitches p" in sql
        assert "AS outs_recorded" in sql
        assert "sb_success_2b" in sql and "stolen_base_home" in sql
        assert "= ANY($1)" in sql
        assert "raw.play_events" in audit.play_events_sql()
        assert "raw.game_player_stats" in audit.boxscore_rows_sql()
        games = audit.games_sql([2023, 2024], 50)
        assert "IN (2023, 2024)" in games and "LIMIT 50" in games
        assert "EXISTS (SELECT 1 FROM raw.game_player_stats" in games
