"""
test_sim550_arm_model.py
========================
The outfield arm block rebuilt from our own advancement pool (SIM-550): the
MODEL and CALIBRATION half of the plan's section 7. This file is the sibling
of ``test_sim550_arm_block.py``, which holds the profile computor's half (the
fill step, the aggregator, the point-in-time checks). The plan names one test
file; the two halves were built by two agents at once, so they live apart.

What this file pins:

* the arm group is the three features at the repeats `make calibrate` fitted
  on 2026-09-17 — the throw velocity 0.844, the advancement prevention 0.137,
  the thrown-out rate 0.240 (the plan's 0.60 for the prevention pooled the
  three positions; within a position it repeats at 0.1 to 0.3) — and the
  engine and the computor share the 50-chance prior;
* the arm's confidence is chances-based (25 chances -> alpha 0.33, two
  thirds of the way to the league mean; 200 -> 0.80) and the range, error and
  star groups keep the batted-ball confidence;
* a missing velocity becomes the league mean, never 0 mph; a missing
  prevention on a zero-chance profile becomes the league mean;
* a league row without an arm key loads NaN, not 0.0, and a NaN league value
  leaves the raw rate alone;
* the loader reads the three columns plus the chance count, tolerates a table
  without them, and builds end to end on a canonical-schema DuckDB;
* ``score_all`` and ``query_pair`` agree on an outfield pair;
* a stale four-weight arm calibration is refused;
* the calibrator's outfield SELECT names the three columns in the group's
  order, its arm fit uses measured rows only, the sentinel stays under 20
  measured rows, and every block after the arm in the SELECT still reads its
  own columns (the off-by-one guard);
* the reliability fit pairs a fielder's rows inside ONE position, season to
  season, and the arm's pairs need 50 chances in both seasons (the review
  findings engine-1 / calib-1: a player-only pairing in scan order put half
  the live pairs across positions and read the prevention's repeat at 0.21
  against its measured 0.54).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest

duckdb = pytest.importorskip("duckdb")

from similarity.engines.fielder_similarity import (  # noqa: E402
    ALL_POSITIONS,
    ARM_ALPHA_PRIOR_CHANCES,
    EB_N_PRIOR,
    OF_ARM_FEATURES,
    OF_ERROR_FEATURES,
    OF_RANGE_FEATURES,
    OF_STAR_FEATURES,
    EmpiricalBayesShrinkage,
    FielderProfile,
    FielderSimilarityEngine,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"

# The league row's arm vector: velocity (mph), prevention, thrown-out rate.
LEAGUE_ARM = np.array([88.0, 0.0, 0.05])

# SIM-532: the six fielder columns migration 0030 appends after asof_date, in
# OAA_JUMP_COLUMN_ORDER. The canonical-schema tests add them when the schema
# file does not carry them yet, the way 0030 does (idempotent).
SIM532_FIELDER_COLUMNS = (
    ("savant_oaa", "INTEGER"),
    ("savant_oaa_per_100", "FLOAT"),
    ("jump_reaction_ft", "FLOAT"),
    ("jump_burst_ft", "FLOAT"),
    ("jump_route_ft", "FLOAT"),
    ("jump_plays", "INTEGER"),
)


# ===========================================================================
# Helpers — engines assembled without a database
# ===========================================================================


def _of_profile(
    pid: int,
    arm: list[float],
    chances: int,
    *,
    bb: int = 600,
    season: int = 2024,
    position: str = "CF",
    range_vec: list[float] | None = None,
) -> FielderProfile:
    """One outfielder profile; the range, error and star groups have plain
    defaults so a test can watch the arm group alone."""
    return FielderProfile(
        player_id=pid,
        position=position,
        season=season,
        innings_played=900.0,
        sample_batted_balls=bb,
        # SIM-532: nine range entries — our five, Savant's figure, the three
        # jump parts. The jump plays default to 0, so with a zero league row
        # the jump entries shrink to 0 and the arm reads alone.
        range_vec=np.array(
            range_vec or [3.0, 1.0, 0.5, 2.0, 0.01, 1.2, 0.5, 0.3, 0.2], dtype=np.float64
        ),
        error_vec=np.array([0.02, 0.01], dtype=np.float64),
        arm_vec=np.array(arm, dtype=np.float64),
        star_vec=np.array([0.2, 0.5, 0.97], dtype=np.float64),
        eb_alpha=bb / (bb + EB_N_PRIOR),
        sample_arm_chances=chances,
    )


def _engine_with(profiles: list[FielderProfile], league_arm=LEAGUE_ARM) -> FielderSimilarityEngine:
    """An engine holding ``profiles`` and one CF league row for 2024, ready
    for ``_apply_shrinkage``. The constructor never opens the database."""
    engine = FielderSimilarityEngine(duckdb_path=":memory:")
    engine._profiles = {(p.player_id, p.position, p.season): p for p in profiles}
    for group in engine._pos_avg:
        engine._pos_avg[group]["CF"] = {}
    engine._pos_avg["range"]["CF"][2024] = np.zeros(len(OF_RANGE_FEATURES))
    engine._pos_avg["error"]["CF"][2024] = np.zeros(len(OF_ERROR_FEATURES))
    engine._pos_avg["star"]["CF"][2024] = np.zeros(len(OF_STAR_FEATURES))
    engine._pos_avg["arm"]["CF"][2024] = np.asarray(league_arm, dtype=np.float64)
    return engine


def _assemble(engine: FielderSimilarityEngine) -> None:
    """The tail of ``build()``: fit the normalizer and build the partitions."""
    by_pos: dict[str, list[FielderProfile]] = {pos: [] for pos in ALL_POSITIONS}
    for p in engine._profiles.values():
        by_pos[p.position].append(p)
    engine._normalizer.fit(by_pos)
    for pos, partition in engine._partitions.items():
        partition.build(by_pos.get(pos, []), engine._normalizer)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Answers the information_schema probe with ``present`` and every other
    query with ``rows``; records each SQL text."""

    def __init__(self, present: list[str], rows: list[tuple]) -> None:
        self._present = present
        self._rows = rows
        self.queries: list[str] = []

    def execute(self, sql: str, *args):
        self.queries.append(sql)
        if "information_schema" in sql:
            return _FakeResult([(c,) for c in self._present])
        return _FakeResult(self._rows)


# ===========================================================================
# 1. The feature list
# ===========================================================================


class TestArmFeatureList:
    def test_the_arm_group_is_the_three_features_at_their_repeats(self) -> None:
        assert [f for f, _ in OF_ARM_FEATURES] == [
            "arm_strength",
            "arm_advancement_prevention",
            "arm_thrown_out_rate",
        ]
        assert [w for _, w in OF_ARM_FEATURES] == pytest.approx([0.844, 0.137, 0.240])
        assert ARM_ALPHA_PRIOR_CHANCES == 50

    def test_the_engine_and_the_computor_share_the_arm_prior(self) -> None:
        from pipeline.batch import player_profile_computor as computor

        assert computor.ARM_ALPHA_PRIOR_CHANCES == ARM_ALPHA_PRIOR_CHANCES

    def test_the_arm_scorer_carries_three_normalised_weights(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        assert engine._of_arm_rbf.weights.shape == (3,)
        assert engine._of_arm_rbf.weights.sum() == pytest.approx(1.0)
        # The velocity carries the largest share: its repeat is the highest.
        # The two pool-derived rates repeat weakly within a position, the
        # thrown-out rate a little better than the prevention (0.240 / 0.137).
        assert engine._of_arm_rbf.weights[0] > engine._of_arm_rbf.weights[2]
        assert engine._of_arm_rbf.weights[2] > engine._of_arm_rbf.weights[1]


# ===========================================================================
# 2. Shrinkage — the arm's own confidence
# ===========================================================================


class TestArmShrinkage:
    def test_shrink_takes_a_one_call_prior_and_keeps_the_instance_prior_otherwise(self) -> None:
        eb = EmpiricalBayesShrinkage(n_prior=15)
        raw = np.array([10.0])
        avg = np.array([0.0])
        assert eb.shrink(raw, avg, 15)[0] == pytest.approx(5.0)
        assert eb.shrink(raw, avg, 15, n_prior=None)[0] == pytest.approx(5.0)
        assert eb.shrink(raw, avg, 15, n_prior=ARM_ALPHA_PRIOR_CHANCES)[0] == pytest.approx(
            10.0 * 15 / 65
        )
        assert eb.n_prior == 15, "the instance's prior is unchanged"

    def test_twenty_five_chances_shrink_two_thirds_of_the_way(self) -> None:
        p = _of_profile(1, [90.0, 0.06, 0.11], chances=25)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        alpha = 25 / (25 + ARM_ALPHA_PRIOR_CHANCES)
        assert alpha == pytest.approx(1 / 3)
        assert p.arm_vec[1] == pytest.approx(0.0 + (0.06 - 0.0) * alpha)
        assert p.arm_vec[2] == pytest.approx(0.05 + (0.11 - 0.05) * alpha)
        # The velocity is Savant's, not shrunk.
        assert p.arm_vec[0] == pytest.approx(90.0)

    def test_two_hundred_chances_keep_four_fifths(self) -> None:
        p = _of_profile(1, [90.0, 0.06, 0.11], chances=200)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        alpha = 200 / 250
        assert alpha == pytest.approx(0.80)
        assert p.arm_vec[1] == pytest.approx(0.06 * alpha)
        assert p.arm_vec[2] == pytest.approx(0.05 + 0.06 * alpha)

    def test_the_batted_balls_never_set_the_arm_confidence(self) -> None:
        """An everyday outfielder: 600 batted balls, 25 arm chances. On the
        batted balls his arm was read at face value (alpha 0.976)."""
        p = _of_profile(1, [90.0, 0.06, 0.11], chances=25, bb=600)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        face_value_alpha = 600 / (600 + EB_N_PRIOR)
        assert p.arm_vec[1] != pytest.approx(0.06 * face_value_alpha)
        assert p.arm_vec[1] == pytest.approx(0.06 / 3)

    def test_the_other_groups_keep_the_batted_ball_confidence(self) -> None:
        thin_arm = _of_profile(1, [90.0, 0.06, 0.11], chances=0, bb=600)
        engine = _engine_with([thin_arm])
        engine._apply_shrinkage()
        bb_alpha = 600 / (600 + EB_N_PRIOR)
        # Range, errors and stars shrink toward their (zero) league rows on
        # the batted balls, untouched by the arm's zero chances.
        assert thin_arm.range_vec[0] == pytest.approx(3.0 * bb_alpha)
        assert thin_arm.error_vec[0] == pytest.approx(0.02 * bb_alpha)
        assert thin_arm.star_vec[2] == pytest.approx(0.97 * bb_alpha)
        # … while the arm's rates went all the way to the league mean.
        assert thin_arm.arm_vec[1] == pytest.approx(LEAGUE_ARM[1])
        assert thin_arm.arm_vec[2] == pytest.approx(LEAGUE_ARM[2])

    def test_a_missing_velocity_becomes_the_league_mean_never_zero(self) -> None:
        p = _of_profile(1, [np.nan, 0.02, 0.05], chances=100)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        assert p.arm_vec[0] == pytest.approx(88.0)
        assert p.arm_vec[0] != 0.0
        assert np.isfinite(p.arm_vec).all()

    def test_a_missing_prevention_on_a_zero_chance_profile_becomes_the_league_mean(self) -> None:
        p = _of_profile(1, [90.0, np.nan, np.nan], chances=0)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        assert p.arm_vec[1] == pytest.approx(LEAGUE_ARM[1])
        assert p.arm_vec[2] == pytest.approx(LEAGUE_ARM[2])
        assert p.arm_vec[0] == pytest.approx(90.0)

    def test_a_nan_league_value_leaves_the_raw_rate_alone(self) -> None:
        p = _of_profile(1, [90.0, 0.06, 0.11], chances=25)
        engine = _engine_with([p], league_arm=np.array([np.nan, np.nan, np.nan]))
        engine._apply_shrinkage()
        assert p.arm_vec.tolist() == pytest.approx([90.0, 0.06, 0.11])

    def test_a_missing_velocity_with_a_nan_league_stays_nan_not_zero(self) -> None:
        """No velocity anywhere (a season before the arm-strength board): the
        value stays unmeasured, and the normalizer reads it as the mean."""
        p = _of_profile(1, [np.nan, 0.06, 0.11], chances=25)
        engine = _engine_with([p], league_arm=np.array([np.nan, 0.0, 0.05]))
        engine._apply_shrinkage()
        assert np.isnan(p.arm_vec[0])
        assert p.arm_vec[1] == pytest.approx(0.06 / 3)

    def test_an_infielder_is_untouched_by_the_arm_branch(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        p = FielderProfile(
            player_id=7,
            position="SS",
            season=2024,
            innings_played=800.0,
            sample_batted_balls=300,
            range_vec=np.array([1.0, 1.0, 1.0, 1.0, 0.0, 0.5]),  # SIM-532: six
            error_vec=np.array([0.02, 0.01]),
            dp_vec=np.array([0.5, 0.3, 0.6, 0.1]),
            specialty_vec=np.array([0.5, 0.5]),
            eb_alpha=300 / 315,
        )
        engine._profiles = {(7, "SS", 2024): p}
        engine._apply_shrinkage()
        assert p.arm_vec is None
        assert p.sample_arm_chances == 0


# ===========================================================================
# 3. The league row
# ===========================================================================


class TestLeagueRow:
    def _load(self, profile: dict) -> FielderSimilarityEngine:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn([], [(2024, "fielder_CF", json.dumps(profile))])
        engine._load_positional_averages(conn, [2024])
        return engine

    def test_an_absent_arm_key_loads_nan_not_zero(self) -> None:
        # A league row written before the arm block was rebuilt carries only
        # the hold rate. A 0.0 thrown-out rate would be the weakest arm in
        # the league; a 0.0 mph velocity is no throw at all.
        engine = self._load({"arm_hold_rate": 0.62, "oaa_deep": 1.5})
        arm = engine._pos_avg["arm"]["CF"][2024]
        assert arm.shape == (3,)
        assert np.isnan(arm).all()
        # The other groups keep their rule: an absent key reads 0.0.
        rng = engine._pos_avg["range"]["CF"][2024]
        assert rng[0] == 0.0 and rng[3] == pytest.approx(1.5)

    def test_the_three_arm_keys_load_in_the_groups_order(self) -> None:
        engine = self._load(
            {
                "arm_hold_rate": 0.80,
                "arm_thrown_out_rate": 0.049,
                "arm_advancement_prevention": -0.002,
                "arm_strength": 87.4,
            }
        )
        arm = engine._pos_avg["arm"]["CF"][2024]
        assert arm.tolist() == pytest.approx([87.4, -0.002, 0.049])

    def test_a_null_arm_key_loads_nan(self) -> None:
        engine = self._load(
            {"arm_strength": None, "arm_advancement_prevention": 0.0, "arm_thrown_out_rate": 0.05}
        )
        arm = engine._pos_avg["arm"]["CF"][2024]
        assert np.isnan(arm[0]) and arm[1] == 0.0 and arm[2] == pytest.approx(0.05)


# ===========================================================================
# 4. The profile loader
# ===========================================================================


def _row(
    pid: int,
    position: str,
    arm: tuple,
    *,
    below: bool = False,
    stars: tuple = (4, 1, 10, 4, 100, 98),
    oaa_jump: tuple = (None, None, None, None, None),
) -> tuple:
    """One row in the loader's SELECT order (35 columns since SIM-532: the
    Savant figure, the three jump parts and the jump's play count sit right
    after catch_pct_added)."""
    return (
        pid,
        position,
        2024,
        800.0,
        400,
        # range (5)
        1.0,
        0.5,
        0.2,
        1.5,
        0.01,
        # range, continued (SIM-532: savant_oaa_per_100, reaction, burst, route, jump_plays)
        *oaa_jump,
        # errors (2)
        0.02,
        0.01,
        # DP (4)
        0.3,
        0.2,
        0.6,
        0.1,
        # specialty (2)
        0.5,
        0.9,
        # arm: velocity, prevention, thrown-out, chances (4)
        *arm,
        # star counts (6)
        *stars,
        # meta
        below,
        None,
    )


ARM_COLUMNS = [
    "arm_strength",
    "arm_advancement_prevention",
    "arm_thrown_out_rate",
    "arm_opportunities",
]


class TestProfileLoader:
    def test_the_select_names_the_three_columns_and_the_chances(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn(
            ["asof_date", *ARM_COLUMNS],
            [
                _row(1, "CF", (91.2, 0.03, 0.08, 120)),
                _row(2, "RF", (None, None, None, None)),
                _row(3, "LF", (86.0, -0.01, None, 12)),
                _row(4, "SS", (None, None, None, None)),
            ],
        )
        engine._load_profiles(conn, [2024])
        sql = [q for q in conn.queries if "information_schema" not in q][0]
        assert (
            "fsm.arm_strength, fsm.arm_advancement_prevention, "
            "fsm.arm_thrown_out_rate, fsm.arm_opportunities" in sql
        )
        assert "of_arm_runs" not in sql and "arm_hold_rate" not in sql

        cf = engine._profiles[(1, "CF", 2024)]
        assert cf.arm_vec.tolist() == pytest.approx([91.2, 0.03, 0.08])
        assert cf.sample_arm_chances == 120
        # The star rates still read their own columns after the arm block.
        assert cf.star_vec.tolist() == pytest.approx([0.25, 0.4, 0.98])

        rf = engine._profiles[(2, "RF", 2024)]
        assert np.isnan(rf.arm_vec).all(), "NULL loads as NaN, never 0.0"
        assert rf.sample_arm_chances == 0

        lf = engine._profiles[(3, "LF", 2024)]
        assert lf.arm_vec[:2].tolist() == pytest.approx([86.0, -0.01])
        assert np.isnan(lf.arm_vec[2]), "never challenged: the thrown-out rate is unmeasured"
        assert lf.sample_arm_chances == 12

        ss = engine._profiles[(4, "SS", 2024)]
        assert ss.arm_vec is None and ss.sample_arm_chances == 0

    def test_a_table_without_the_arm_columns_still_loads(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn(["asof_date"], [_row(1, "CF", (None, None, None, None))])
        engine._load_profiles(conn, [2024])
        sql = [q for q in conn.queries if "information_schema" not in q][0]
        for col in ARM_COLUMNS:
            assert f"NULL AS {col}" in sql
        cf = engine._profiles[(1, "CF", 2024)]
        assert np.isnan(cf.arm_vec).all() and cf.sample_arm_chances == 0

    def test_build_end_to_end_on_the_canonical_schema(self, tmp_path: Path) -> None:
        """The real SELECT against the real columns, the league keys, the
        shrinkage and the scorers in one pass — the loader can never name a
        column the canonical schema lacks."""
        path = str(tmp_path / "sim550.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        con.execute(
            "CREATE TABLE derived.league_averages (entity_type VARCHAR, season SMALLINT, "
            "profile_json JSON)"
        )
        con.execute(
            "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
            "innings_played, sample_batted_balls, oaa_glove_side, oaa_arm_side, oaa_charging, "
            "oaa_deep, catch_pct_added, fielding_error_rate, throwing_error_rate, "
            "arm_strength, arm_opportunities, arm_advancement_prevention, arm_thrown_out_rate, "
            "arm_hold_rate, of_arm_runs, five_star_opps, five_star_catches, four_star_opps, "
            "four_star_catches, routine_opps, routine_catches, below_minimum_sample) VALUES "
            "(1, 'CF', 2024, 900, 600, 2.0, 1.0, 0.5, 3.0, 0.02, 0.01, 0.005, "
            " 92.0, 25, 0.06, 0.20, 0.80, NULL, 5, 2, 12, 6, 120, 118, FALSE), "
            "(2, 'CF', 2024, 900, 600, 1.0, 0.0, 0.5, 1.0, 0.01, 0.02, 0.010, "
            " NULL, 200, -0.02, 0.02, 0.70, NULL, 4, 1, 10, 4, 110, 108, FALSE), "
            "(3, 'CF', 2024, 900, 600, 0.0, -1.0, 0.0, -1.0, 0.00, 0.03, 0.015, "
            " 85.0, 0, NULL, NULL, NULL, NULL, 3, 0, 8, 3, 100, 97, FALSE)"
        )
        con.execute(
            "INSERT INTO derived.league_averages VALUES ('fielder_CF', 2024, ?)",
            [
                json.dumps(
                    {
                        "oaa_glove_side": 1.0,
                        "oaa_arm_side": 0.0,
                        "oaa_charging": 0.3,
                        "oaa_deep": 1.0,
                        "catch_pct_added": 0.01,
                        "fielding_error_rate": 0.02,
                        "throwing_error_rate": 0.01,
                        "five_star_catch_rate": 0.3,
                        "four_star_catch_rate": 0.5,
                        "routine_catch_rate": 0.98,
                        "arm_hold_rate": 0.79,
                        "arm_advancement_prevention": 0.0,
                        "arm_thrown_out_rate": 0.05,
                        "arm_strength": 88.0,
                        # SIM-532: the four range keys a post-recompute league
                        # row carries; the rows above have no jump, so each
                        # reads the league's.
                        "savant_oaa_per_100": 0.2,
                        "jump_reaction_ft": 0.5,
                        "jump_burst_ft": 1.0,
                        "jump_route_ft": -0.5,
                    }
                )
            ],
        )
        con.close()

        engine = FielderSimilarityEngine(duckdb_path=path)
        engine.build([2024])
        assert engine.profile_count == 3
        p1 = engine.get_profile(1, "CF", 2024)
        p2 = engine.get_profile(2, "CF", 2024)
        p3 = engine.get_profile(3, "CF", 2024)
        assert p1 is not None and p2 is not None and p3 is not None
        assert (p1.sample_arm_chances, p2.sample_arm_chances, p3.sample_arm_chances) == (25, 200, 0)
        # 25 chances: a third of his own reading; the velocity as measured.
        assert p1.arm_vec.tolist() == pytest.approx([92.0, 0.06 / 3, 0.05 + 0.15 / 3])
        # 200 chances: four fifths; the missing velocity is the league's 88.
        assert p2.arm_vec.tolist() == pytest.approx([88.0, -0.016, 0.05 + (0.02 - 0.05) * 0.8])
        # No chance at all: the league's rates; his own velocity.
        assert p3.arm_vec.tolist() == pytest.approx([85.0, 0.0, 0.05])
        results = engine.query(1, "CF", 2024)
        assert len(results) == 2 and all(0.0 <= r.score <= 1.0 for r in results)


# ===========================================================================
# 5. Scoring
# ===========================================================================


class TestScoring:
    def test_score_all_and_query_pair_agree_on_an_outfield_pair(self) -> None:
        profiles = [
            _of_profile(
                1,
                [92.0, 0.05, 0.10],
                chances=150,
                range_vec=[3.0, 1.0, 0.5, 2.0, 0.01, 1.0, 0.5, 0.2, 0.3],
            ),
            _of_profile(
                2,
                [86.0, -0.03, 0.02],
                chances=40,
                range_vec=[-1.0, 0.0, 0.5, -2.0, -0.01, -0.8, -0.2, 0.1, -0.4],
            ),
            _of_profile(
                3,
                [np.nan, 0.01, np.nan],
                chances=10,
                range_vec=[0.0, 2.0, -0.5, 0.0, 0.0, 0.2, np.nan, np.nan, np.nan],
            ),
            _of_profile(
                4,
                [89.0, 0.02, 0.06],
                chances=90,
                range_vec=[1.0, -1.0, 1.5, 1.0, 0.02, 0.6, 0.1, -0.3, 0.5],
            ),
        ]
        engine = _engine_with(profiles)
        engine._apply_shrinkage()
        _assemble(engine)
        batch = {r.player_id: r for r in engine.query(1, "CF", 2024)}
        assert set(batch) == {2, 3, 4}
        for pid in (2, 3, 4):
            pair = engine.query_pair((1, "CF", 2024), (pid, "CF", 2024))
            assert pair is not None
            assert pair.score == pytest.approx(batch[pid].score)
            assert pair.secondary_score == pytest.approx(batch[pid].secondary_score)
            assert 0.0 <= pair.secondary_score <= 1.0
        # The order is symmetric too.
        back = engine.query_pair((4, "CF", 2024), (1, "CF", 2024))
        assert back is not None and back.score == pytest.approx(batch[4].score)

    def test_a_closer_arm_scores_higher_on_the_arm_sub_score(self) -> None:
        profiles = [
            _of_profile(1, [92.0, 0.05, 0.10], chances=300),
            _of_profile(2, [91.0, 0.04, 0.09], chances=300),
            _of_profile(3, [84.0, -0.05, 0.01], chances=300),
        ]
        engine = _engine_with(profiles)
        engine._apply_shrinkage()
        _assemble(engine)
        near = engine.query_pair((1, "CF", 2024), (2, "CF", 2024))
        far = engine.query_pair((1, "CF", 2024), (3, "CF", 2024))
        assert near is not None and far is not None
        assert near.secondary_score > far.secondary_score


# ===========================================================================
# 6. Calibration wiring
# ===========================================================================


class TestApplyCalibration:
    def test_a_stale_four_weight_arm_report_is_refused(self, caplog) -> None:
        from similarity.similarity_calibration import CalibrationReport

        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        stale = CalibrationReport(
            sigma_of_arm=0.7, reliability_weights_of_arm=np.array([0.5, 0.5, 0.5, 0.5])
        )
        with caplog.at_level(logging.WARNING, logger="fielder_similarity"):
            engine.apply_calibration(stale)
        default = np.array([w for _, w in OF_ARM_FEATURES])
        np.testing.assert_allclose(engine._of_arm_rbf.weights, default / default.sum())
        assert engine._of_arm_rbf.sigma == 0.7, "the sigma still applies"
        assert any("reliability_weights_of_arm" in r.message for r in caplog.records)
        # A query on a 3-feature arm then works (a 4-weight scorer would broadcast-fail).
        profiles = [_of_profile(1, [92.0, 0.05, 0.10], 100), _of_profile(2, [86.0, 0.0, 0.02], 80)]
        engine._profiles = {(p.player_id, p.position, p.season): p for p in profiles}
        _assemble(engine)
        assert engine.query_pair((1, "CF", 2024), (2, "CF", 2024)) is not None

    def test_a_three_weight_arm_report_applies(self) -> None:
        from similarity.similarity_calibration import CalibrationReport

        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        fitted = np.array([0.9, 0.6, 0.3])
        engine.apply_calibration(CalibrationReport(reliability_weights_of_arm=fitted))
        np.testing.assert_allclose(engine._of_arm_rbf.weights, fitted / fitted.sum())


# ===========================================================================
# 7. The calibrator
# ===========================================================================


class _RoutingConn:
    """The fielder calibrator runs two SELECTs: the infield one (position IN
    ('1B', …)) gets ``if_rows``; the outfield one gets ``of_rows``."""

    def __init__(self, if_rows: list[tuple], of_rows: list[tuple]) -> None:
        self._if_rows = if_rows
        self._of_rows = of_rows
        self.queries: list[str] = []

    def execute(self, sql: str, *args):
        self.queries.append(sql)
        if "'1B'" in sql:
            return _FakeResult(self._if_rows)
        return _FakeResult(self._of_rows)


def _of_calibration_rows(n: int, seed: int, measured_every: int = 3) -> list[tuple]:
    """Outfield rows in the calibrator's SELECT order: 4 meta, 9 range (our
    five, Savant's figure, the three jump parts — SIM-532), 2 errors, 3 arm,
    6 star counts, then the arm's chance count, then the jump's play count
    (SIM-532, appended last). The arm is NULL on every ``measured_every``-th
    row."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        arm = (
            (
                float(rng.normal(88, 3)),
                float(rng.normal(0, 0.03)),
                float(rng.beta(2, 30)),
            )
            if i % measured_every
            else (None, None, None)
        )
        f5o, f4o, ro = int(rng.integers(2, 8)), int(rng.integers(6, 20)), int(rng.integers(80, 160))
        rows.append(
            (
                3000 + i % 40,
                "CF",
                2023 + i // 40,
                int(rng.integers(200, 600)),
                *rng.normal(0, 2, 9).tolist(),
                *rng.beta(2, 60, 2).tolist(),
                *arm,
                f5o,
                int(rng.integers(0, f5o + 1)),
                f4o,
                int(rng.integers(0, f4o + 1)),
                ro,
                ro - int(rng.integers(0, 5)),
                int(rng.integers(60, 300)) if arm[0] is not None else None,
                int(rng.integers(25, 80)),
            )
        )
    return rows


def _paired_of_rows(
    prevention: dict[tuple[int, str, int], float],
    chances: int = 200,
    plays: int = 40,
) -> list[tuple]:
    """Outfield rows in the calibrator's SELECT order with a HAND-SET
    prevention per (player, position, season); the velocity and the
    thrown-out rate follow it so every arm feature carries the same signal.
    Everything else is deterministic filler. ``chances`` is the arm's chance
    count and ``plays`` the jump's play count, the two trailing columns in
    that order (SIM-532 appended the second)."""
    rows = []
    for k, ((pid, pos, season), prev) in enumerate(prevention.items()):
        rows.append(
            (
                pid,
                pos,
                season,
                400,
                0.1 * k,
                -0.1 * k,
                0.2,
                0.3,
                0.4,
                # SIM-532: Savant's figure and the three jump parts
                0.5,
                0.6,
                0.7,
                0.8,
                0.02,
                0.01,
                88.0 + 10.0 * prev,
                prev,
                0.05 + prev,
                5,
                3,
                12,
                8,
                120,
                118,
                chances,
                plays,
            )
        )
    return rows


class TestCalibrator:
    def test_the_outfield_select_names_the_three_arm_columns_in_order(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        conn = _RoutingConn([], _of_calibration_rows(120, seed=11))
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        cal._calibrate_fielder_params(conn, [2023, 2024, 2025], 0.5, CalibrationReport())
        of_sql = [q for q in conn.queries if "'LF'" in q][0]
        assert "arm_strength, arm_advancement_prevention, arm_thrown_out_rate," in of_sql
        assert "of_arm_runs" not in of_sql and "arm_hold_rate" not in of_sql
        # The two count columns ride after the blocks — the arm's chances,
        # then the jump's plays (SIM-532) — and both SELECTs come back in
        # (player, position, season) order.
        assert of_sql.rstrip().split("FROM")[0].rstrip().endswith("arm_opportunities, jump_plays")
        if_sql = [q for q in conn.queries if "'1B'" in q][0]
        for sql in (of_sql, if_sql):
            assert "ORDER BY player_id, position, season" in sql

    def test_the_arm_fit_uses_measured_rows_only(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rows = _of_calibration_rows(120, seed=12)
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_fielder_params(
            _RoutingConn([], rows), [2023, 2024, 2025], 0.5, CalibrationReport()
        )
        assert report.sigma_of_arm > 0.0
        assert report.reliability_weights_of_arm is not None
        assert len(report.reliability_weights_of_arm) == len(OF_ARM_FEATURES) == 3
        # The sigma is the one fitted on the MEASURED rows alone …
        # The arm block sits at columns 15 to 17 (four meta, nine range, two
        # errors precede it — SIM-532 widened the range block to nine).
        measured = np.array([r[15:18] for r in rows if r[15] is not None], dtype=np.float64)
        expected = cal._fit_sigma(cal._zscore_matrix(measured), 0.5)
        assert report.sigma_of_arm == pytest.approx(expected)
        # … and more unmeasured rows do not move it (a zero-filled fit would).
        more = rows + [r[:15] + (None, None, None) + r[18:] for r in rows[:60]]
        again = cal._calibrate_fielder_params(
            _RoutingConn([], more), [2023, 2024, 2025], 0.5, CalibrationReport()
        )
        assert again.sigma_of_arm == pytest.approx(expected)

    def test_the_arm_fit_keeps_the_sentinel_under_twenty_measured_rows(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rows = _of_calibration_rows(120, seed=13, measured_every=8)  # 105 measured
        thin = [r[:15] + (None, None, None) + r[18:] for r in rows[:101]] + rows[101:]
        assert sum(r[15] is not None for r in thin) < 20
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
            _RoutingConn([], thin), [2023, 2024, 2025], 0.5, CalibrationReport()
        )
        assert report.sigma_of_arm == 0.0, "the keep-default sentinel"
        assert report.reliability_weights_of_arm is None
        assert report.sigma_of_range > 0.0, "the other groups still fit"
        assert report.sigma_of_stars > 0.0

    def test_every_block_after_the_arm_still_reads_its_own_columns(self, tmp_path: Path) -> None:
        """The off-by-one guard. The calibrator walks the SELECT by the feature
        lists' lengths; the arm went from four columns to three. On a
        canonical-schema DuckDB the star and error sigmas must equal the fits
        over the columns read by NAME."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        path = str(tmp_path / "sim550_cal.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        # SIM-532: the calibrator names the Savant and jump columns directly
        # (the recompute applies migration 0030 before `make calibrate`).
        # Add them here the way 0030 does, so this test holds whether or not
        # the canonical schema already carries them.
        for col, typ in SIM532_FIELDER_COLUMNS:
            con.execute(
                f"ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS {col} {typ}"
            )
        rng = np.random.default_rng(14)
        for i in range(90):
            f5o, f4o, ro = (
                int(rng.integers(2, 8)),
                int(rng.integers(6, 20)),
                int(rng.integers(80, 160)),
            )
            arm = (
                (float(rng.normal(88, 3)), float(rng.normal(0, 0.03)), float(rng.beta(2, 30)))
                if i % 4
                else (None, None, None)
            )
            con.execute(
                "INSERT INTO derived.fielder_season_metrics (player_id, position, season, "
                "innings_played, sample_batted_balls, oaa_glove_side, oaa_arm_side, "
                "oaa_charging, oaa_deep, catch_pct_added, "
                "savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft, "
                "fielding_error_rate, "
                "throwing_error_rate, arm_strength, arm_advancement_prevention, "
                "arm_thrown_out_rate, arm_hold_rate, of_arm_runs, arm_opportunities, "
                "five_star_opps, five_star_catches, four_star_opps, four_star_catches, "
                "routine_opps, routine_catches, below_minimum_sample) VALUES "
                "(?, 'RF', ?, 800, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, FALSE)",
                [
                    5000 + i % 30,
                    2023 + i // 30,
                    int(rng.integers(200, 600)),
                    # SIM-532: nine range values — our five, Savant's figure,
                    # the three jump parts
                    *rng.normal(0, 2, 9).tolist(),
                    *rng.beta(2, 60, 2).tolist(),
                    *arm,
                    # the two stored-only columns carry DISTINCTIVE values: a
                    # shifted read would pick them up
                    float(rng.uniform(0.5, 0.9)),
                    float(rng.normal(5, 1)),
                    int(rng.integers(0, 300)),
                    f5o,
                    int(rng.integers(0, f5o + 1)),
                    f4o,
                    int(rng.integers(0, f4o + 1)),
                    ro,
                    ro - int(rng.integers(0, 5)),
                ],
            )
        con.close()

        con = duckdb.connect(path, read_only=True)
        cal = SimilarityCalibrator(duckdb_path=path)
        report = cal._calibrate_fielder_params(con, [2023, 2024, 2025], 0.5, CalibrationReport())
        by_name = con.execute(
            "SELECT fielding_error_rate, throwing_error_rate, "
            "five_star_catches * 1.0 / five_star_opps, four_star_catches * 1.0 / four_star_opps, "
            "routine_catches * 1.0 / routine_opps, "
            "arm_strength, arm_advancement_prevention, arm_thrown_out_rate "
            "FROM derived.fielder_season_metrics WHERE position = 'RF' "
            # the calibrator reads its rows in this order (SIM-550); the sigma
            # fit samples pairs by seed, so the by-name read must match it
            "ORDER BY player_id, position, season"
        ).fetchall()
        con.close()

        err = np.array([r[0:2] for r in by_name], dtype=np.float64)
        star = np.array([r[2:5] for r in by_name], dtype=np.float64)
        arm = np.array([r[5:8] for r in by_name if r[5] is not None], dtype=np.float64)
        assert report.sigma_of_errors == pytest.approx(cal._fit_sigma(cal._zscore_matrix(err), 0.5))
        assert report.sigma_of_stars == pytest.approx(cal._fit_sigma(cal._zscore_matrix(star), 0.5))
        assert report.sigma_of_arm == pytest.approx(cal._fit_sigma(cal._zscore_matrix(arm), 0.5))
        assert len(report.reliability_weights_of_arm) == 3
        assert len(report.reliability_weights_of_star) == 3
        # A one-column shift on the star block would read a different matrix.
        shifted = np.array(
            [[r[3], r[4], r[5] if r[5] is not None else 0.0] for r in by_name], dtype=np.float64
        )
        assert report.sigma_of_stars != pytest.approx(
            cal._fit_sigma(cal._zscore_matrix(shifted), 0.5)
        )


# ===========================================================================
# The reliability pairing (engine-1 / calib-1)
# ===========================================================================


class TestReliabilityPairing:
    """The fielder table holds one row per (player, position, season). The
    reliability weight is a season-to-season repeat, so a pair must sit inside
    one player AND one position and join consecutive seasons. On the live
    table the player-only pairing in scan order formed 225 arm pairs, 112 of
    them across positions and 46 inside one season, and read the prevention's
    repeat at 0.21 against 0.54 when paired properly."""

    def test_the_helper_pairs_every_consecutive_season_and_skips_a_gap(self) -> None:
        from similarity.similarity_calibration import calibrate_reliability_weights

        rng = np.random.default_rng(21)
        ids, seasons, values = [], [], []
        # 30 players with 2023 and 2024: the feature repeats exactly.
        for pid in range(30):
            v = float(rng.normal())
            for season in (2023, 2024):
                ids.append(pid)
                seasons.append(season)
                values.append(v)
        # 30 players with 2023 and 2025 (a gap): the feature FLIPS. A pairing
        # that ignores the gap would read these as anti-correlated pairs.
        for pid in range(100, 130):
            v = float(rng.normal())
            ids.append(pid)
            seasons.append(2023)
            values.append(v)
            ids.append(pid)
            seasons.append(2025)
            values.append(-v)
        matrix = np.array(values, dtype=np.float64).reshape(-1, 1)
        with_seasons = calibrate_reliability_weights(
            matrix, np.array(ids), seasons=np.array(seasons)
        )
        assert with_seasons[0] == pytest.approx(1.0)
        without = calibrate_reliability_weights(matrix, np.array(ids))
        assert without[0] < 0.5, "the pre-fix rule pairs the gapped rows too"

    def test_the_helper_pairs_both_consecutive_seasons_of_a_three_season_player(self) -> None:
        from similarity.similarity_calibration import calibrate_reliability_weights

        rng = np.random.default_rng(22)
        ids, seasons, values = [], [], []
        for pid in range(30):
            # 2023 and 2024 repeat; 2025 flips. Two pairs per player: one
            # repeating, one flipping, so the correlation lands near 0 only
            # when BOTH pairs are formed (the first-two-rows rule reads 1.0).
            v = float(rng.normal())
            for season, value in ((2023, v), (2024, v), (2025, -v)):
                ids.append(pid)
                seasons.append(season)
                values.append(value)
        matrix = np.array(values, dtype=np.float64).reshape(-1, 1)
        both = calibrate_reliability_weights(matrix, np.array(ids), seasons=np.array(seasons))
        assert both[0] < 0.3
        first_two = calibrate_reliability_weights(matrix, np.array(ids))
        assert first_two[0] == pytest.approx(1.0)

    def test_the_arm_pairs_stay_inside_one_position(self) -> None:
        """One player at LF and CF in the same seasons. His LF prevention
        repeats exactly from 2023 to 2024 and so does his CF prevention, but
        the CF value is the LF value's negative. Paired inside one position
        the repeat is 1.0; paired across positions (the old rule's first two
        rows: 2023 CF with 2023 LF) it is -1.0, clamped to the 0.1 floor."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(23)
        prevention: dict[tuple[int, str, int], float] = {}
        for pid in range(3000, 3030):
            v = float(rng.normal(0.0, 0.03))
            for season in (2023, 2024):
                prevention[(pid, "CF", season)] = -v
                prevention[(pid, "LF", season)] = v
        rows = _paired_of_rows(prevention)
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
            _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
        )
        assert report.reliability_weights_of_arm is not None
        assert report.reliability_weights_of_arm[1] == pytest.approx(1.0)
        assert report.reliability_weights_of_arm[2] == pytest.approx(1.0)

    def test_the_arm_pairs_need_fifty_chances_in_both_seasons(self) -> None:
        """The plan's floor (section 2.2): a pair counts when the fielder had
        ARM_ALPHA_PRIOR_CHANCES or more chances in both seasons. The thin rows
        here flip sign season to season; the full rows repeat exactly."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(24)
        full: dict[tuple[int, str, int], float] = {}
        thin: dict[tuple[int, str, int], float] = {}
        for pid in range(4000, 4030):
            v = float(rng.normal(0.0, 0.03))
            full[(pid, "RF", 2023)] = v
            full[(pid, "RF", 2024)] = v
            thin[(pid + 500, "RF", 2023)] = v
            thin[(pid + 500, "RF", 2024)] = -v
        rows = _paired_of_rows(full, chances=ARM_ALPHA_PRIOR_CHANCES) + _paired_of_rows(
            thin, chances=ARM_ALPHA_PRIOR_CHANCES - 1
        )
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_fielder_params(
            _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
        )
        assert report.reliability_weights_of_arm is not None
        assert report.reliability_weights_of_arm[1] == pytest.approx(1.0)
        # The sigma fit keeps every measured row: the thin rows still count.
        measured = np.array([r[15:18] for r in rows], dtype=np.float64)
        assert report.sigma_of_arm == pytest.approx(
            cal._fit_sigma(cal._zscore_matrix(measured), 0.5)
        )
        # A NULL chance count is no chance: the row never pairs. The chance
        # count is the second-to-last column; the jump's play count rides
        # after it (SIM-532).
        no_count = [r[:-2] + (None, r[-1]) for r in rows]
        report_none = cal._calibrate_fielder_params(
            _RoutingConn([], no_count), [2023, 2024], 0.5, CalibrationReport()
        )
        assert report_none.reliability_weights_of_arm is None

    def test_under_twenty_full_rows_keep_the_default_arm_weights(self, caplog) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(25)
        prevention = {
            (pid, "CF", season): float(rng.normal(0.0, 0.03))
            for pid in range(5000, 5040)
            for season in (2023, 2024)
        }
        rows = _paired_of_rows(prevention, chances=10)
        with caplog.at_level(logging.WARNING, logger="similarity.similarity_calibration"):
            report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
                _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
            )
        assert report.reliability_weights_of_arm is None
        assert report.sigma_of_arm > 0.0, "80 measured rows: the sigma still fits"
        assert "arm chances" in caplog.text

    def test_the_range_pairs_stay_inside_one_position_too(self) -> None:
        """The same rule serves the range, error and star groups (and the
        infield groups): the fix is on the ids, not on the arm alone."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(26)
        rows = []
        for pid in range(6000, 6030):
            v = rng.normal(0.0, 2.0, 9)  # SIM-532: the nine range features
            for season in (2023, 2024):
                for pos, sign in (("CF", 1.0), ("RF", -1.0)):
                    rows.append(
                        (pid, pos, season, 400, *(sign * v).tolist(), 0.02, 0.01)
                        + (88.0, 0.0, 0.05, 5, 3, 12, 8, 120, 118, 200, 40)
                    )
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
            _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
        )
        assert report.reliability_weights_of_range is not None
        assert np.allclose(report.reliability_weights_of_range, 1.0)
