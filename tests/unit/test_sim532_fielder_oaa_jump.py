"""
test_sim532_fielder_oaa_jump.py
===============================
Savant's per-position outs above average and the outfield jump in the fielder
model (SIM-532): the MODEL and CALIBRATION half of the plan's section 7. The
loader, the migrations and the profile builder have their own files (the
SIM-528, SIM-537 and SIM-542 files); the "finding stays honest" test on the
hand split lives with the profile builder's tests.

What this file pins:

* the outfield range group is nine features in the contract's order — our
  five, Savant's figure, reaction, burst, route — and the infield group is six
  (decision 4 adds Savant's figure); the engine and the computor share the
  25-play prior;
* the jump's confidence is plays-based (10 plays -> alpha 0.29, two thirds of
  the way to the league mean; 64 -> 0.72) and the first six range entries keep
  the batted-ball confidence;
* a missing jump becomes the league mean, never 0; when the league mean is
  missing too the value stays unmeasured and the normalizer reads it as the
  position mean;
* a league row without the four new keys loads NaN for them and 0.0 for the
  five original components;
* the loader reads the five new columns through the column guard, tolerates a
  table without them, and builds end to end on a canonical-schema DuckDB;
* an infield profile's range vector is six long;
* ``score_all`` and ``query_pair`` agree on a centre-field partition with
  mixed missing jumps;
* a stale five-weight range calibration is refused for both groups;
* the calibrator's outfield SELECT names the nine range columns in order and
  the infield SELECT the six, its range sigma fits over the rows that carry
  every range feature and keeps the sentinel under 20 of them;
* the reliability rule, per entry: the raw fit drops NaN pairs per feature
  (and reads 0.5 for a column with no pairs); the report then keeps the
  module default for a new entry with fewer than 20 reliable pairs, and the
  three jump parts pair only on rows with 25 or more plays in both seasons;
* the arm's chance count still reads its own column after the jump's play
  count was appended after it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np
import pytest

duckdb = pytest.importorskip("duckdb")

from similarity.engines.fielder_similarity import (  # noqa: E402
    ALL_POSITIONS,
    EB_N_PRIOR,
    IF_RANGE_FEATURES,
    JUMP_ALPHA_PRIOR_PLAYS,
    OF_ERROR_FEATURES,
    OF_RANGE_BASE_COUNT,
    OF_RANGE_FEATURES,
    OF_STAR_FEATURES,
    FielderProfile,
    FielderSimilarityEngine,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "db" / "schemas" / "02_duckdb_schema.sql"

OUR_FIVE = ["oaa_glove_side", "oaa_arm_side", "oaa_charging", "oaa_deep", "catch_pct_added"]
JUMP_PARTS = ["jump_reaction_ft", "jump_burst_ft", "jump_route_ft"]

# The CF league row's range vector: our five at 0 (centred by construction),
# Savant's figure at 0.4 per 100, the three jump parts in feet against the
# league — non-zero on purpose, so "becomes the league mean" and "becomes 0"
# are different outcomes.
LEAGUE_RANGE_CF = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.4, 1.0, 2.0, 3.0])
LEAGUE_ARM_CF = np.array([88.0, 0.0, 0.05])

# The six fielder columns migration 0030 appends after asof_date, in
# OAA_JUMP_COLUMN_ORDER; added idempotently where a test needs them.
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


def _cf_profile(
    pid: int,
    range_vec: list[float],
    plays: int,
    *,
    bb: int = 600,
    season: int = 2024,
) -> FielderProfile:
    """One centre fielder; the error, arm and star groups have plain defaults
    so a test can watch the range group alone."""
    return FielderProfile(
        player_id=pid,
        position="CF",
        season=season,
        innings_played=900.0,
        sample_batted_balls=bb,
        range_vec=np.array(range_vec, dtype=np.float64),
        error_vec=np.array([0.02, 0.01], dtype=np.float64),
        arm_vec=np.array([90.0, 0.02, 0.06], dtype=np.float64),
        star_vec=np.array([0.2, 0.5, 0.97], dtype=np.float64),
        eb_alpha=bb / (bb + EB_N_PRIOR),
        sample_arm_chances=100,
        sample_jump_plays=plays,
    )


def _engine_with(
    profiles: list[FielderProfile], league_range=LEAGUE_RANGE_CF
) -> FielderSimilarityEngine:
    """An engine holding ``profiles`` and one CF league row for 2024, ready
    for ``_apply_shrinkage``. The constructor never opens the database."""
    engine = FielderSimilarityEngine(duckdb_path=":memory:")
    engine._profiles = {(p.player_id, p.position, p.season): p for p in profiles}
    for group in engine._pos_avg:
        engine._pos_avg[group]["CF"] = {}
    engine._pos_avg["range"]["CF"][2024] = np.asarray(league_range, dtype=np.float64)
    engine._pos_avg["error"]["CF"][2024] = np.zeros(len(OF_ERROR_FEATURES))
    engine._pos_avg["star"]["CF"][2024] = np.zeros(len(OF_STAR_FEATURES))
    engine._pos_avg["arm"]["CF"][2024] = LEAGUE_ARM_CF
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


def _sql_words(sql: str) -> str:
    """The SQL with its ``--`` comments dropped and its whitespace collapsed,
    so a column list can be matched as one string."""
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


# ===========================================================================
# 1. The feature lists
# ===========================================================================


class TestRangeFeatureLists:
    def test_the_outfield_range_group_is_the_nine_in_order(self) -> None:
        assert [f for f, _ in OF_RANGE_FEATURES] == [
            *OUR_FIVE,
            "savant_oaa_per_100",
            *JUMP_PARTS,
        ]
        # Every weight is the repeat `make calibrate` fitted on 2026-09-17 after
        # the fielder recompute (the run book copies the fitted values into the
        # module). The plan's starting values were 0.50 / 0.81 / 0.69 / 0.77.
        assert [w for _, w in OF_RANGE_FEATURES[:5]] == pytest.approx(
            [0.344, 0.373, 0.677, 0.594, 0.203]
        )
        assert [w for _, w in OF_RANGE_FEATURES[5:]] == pytest.approx([0.472, 0.775, 0.658, 0.743])
        assert OF_RANGE_BASE_COUNT == 6
        assert len(OF_RANGE_FEATURES) - OF_RANGE_BASE_COUNT == len(JUMP_PARTS)

    def test_the_infield_range_group_is_the_six_and_names_savants_figure(self) -> None:
        """Decision 4: Savant's per-position figure joins the infield group
        as its sixth entry (the plan started it at 0.45; the fit of
        2026-09-17 reads 0.356)."""
        assert [f for f, _ in IF_RANGE_FEATURES] == [*OUR_FIVE, "savant_oaa_per_100"]
        assert IF_RANGE_FEATURES[5][1] == pytest.approx(0.356)
        assert not any(f in JUMP_PARTS for f, _ in IF_RANGE_FEATURES)

    def test_the_engine_and_the_computor_share_the_jump_prior(self) -> None:
        from pipeline.batch import player_profile_computor as computor

        assert JUMP_ALPHA_PRIOR_PLAYS == 25
        assert computor.JUMP_ALPHA_PRIOR_PLAYS == JUMP_ALPHA_PRIOR_PLAYS

    def test_the_range_scorers_carry_nine_and_six_normalised_weights(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        assert engine._of_range_rbf.weights.shape == (9,)
        assert engine._if_range_rbf.weights.shape == (6,)
        assert engine._of_range_rbf.weights.sum() == pytest.approx(1.0)
        assert engine._if_range_rbf.weights.sum() == pytest.approx(1.0)
        # The reaction carries the largest share of the outfield group: its
        # repeat is the highest.
        assert engine._of_range_rbf.weights.argmax() == 6


# ===========================================================================
# 2. Shrinkage — the jump's own confidence
# ===========================================================================


class TestJumpShrinkage:
    def test_the_jump_confidence_is_plays_based_and_the_base_stays_on_batted_balls(self) -> None:
        """Two centre fielders with the same batted balls and the same raw
        range reading, ten jump plays against sixty-four. The jump entries
        pull toward the league mean by different amounts; the first six
        entries pull by the same amount."""
        raw = [3.0, 1.0, 0.5, 2.0, 0.01, 1.2, 5.0, 5.0, 5.0]
        thin = _cf_profile(1, raw, plays=10)
        full = _cf_profile(2, raw, plays=64)
        engine = _engine_with([thin, full])
        engine._apply_shrinkage()

        alpha_thin = 10 / (10 + JUMP_ALPHA_PRIOR_PLAYS)
        alpha_full = 64 / (64 + JUMP_ALPHA_PRIOR_PLAYS)
        assert alpha_thin == pytest.approx(0.29, abs=0.01)
        assert alpha_full == pytest.approx(0.72, abs=0.01)
        for j, league in enumerate(LEAGUE_RANGE_CF[6:], start=6):
            assert thin.range_vec[j] == pytest.approx(league + (5.0 - league) * alpha_thin)
            assert full.range_vec[j] == pytest.approx(league + (5.0 - league) * alpha_full)
        assert thin.range_vec[6] != pytest.approx(full.range_vec[6])

        # The first six shrink on the batted balls, the same for both.
        bb_alpha = 600 / (600 + EB_N_PRIOR)
        np.testing.assert_allclose(thin.range_vec[:6], full.range_vec[:6])
        assert thin.range_vec[0] == pytest.approx(3.0 * bb_alpha)
        assert thin.range_vec[5] == pytest.approx(0.4 + (1.2 - 0.4) * bb_alpha)

    def test_the_batted_balls_never_set_the_jump_confidence(self) -> None:
        """An everyday outfielder: 600 batted balls, ten jump plays. On the
        batted balls his reaction was read at face value (alpha 0.976)."""
        p = _cf_profile(1, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0, 5.0], plays=10, bb=600)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        face_value = 1.0 + 4.0 * (600 / (600 + EB_N_PRIOR))
        assert p.range_vec[6] != pytest.approx(face_value)
        assert p.range_vec[6] == pytest.approx(1.0 + 4.0 * (10 / 35))

    def test_a_missing_jump_becomes_the_league_mean_never_zero(self) -> None:
        # No jump row at all (plays 0) …
        none = _cf_profile(1, [1.0, 0.0, 0.0, 0.0, 0.0, 0.5, np.nan, np.nan, np.nan], plays=0)
        # … and a jump row with one part missing.
        partial = _cf_profile(2, [1.0, 0.0, 0.0, 0.0, 0.0, 0.5, np.nan, 4.0, 4.0], plays=30)
        engine = _engine_with([none, partial])
        engine._apply_shrinkage()
        assert none.range_vec[6:].tolist() == pytest.approx(LEAGUE_RANGE_CF[6:].tolist())
        assert not np.any(none.range_vec[6:] == 0.0)
        assert partial.range_vec[6] == pytest.approx(LEAGUE_RANGE_CF[6])
        alpha = 30 / (30 + JUMP_ALPHA_PRIOR_PLAYS)
        assert partial.range_vec[7] == pytest.approx(2.0 + (4.0 - 2.0) * alpha)

    def test_a_missing_savant_figure_becomes_the_league_mean_on_the_batted_balls(self) -> None:
        p = _cf_profile(1, [1.0, 0.0, 0.0, 0.0, 0.0, np.nan, 1.0, 2.0, 3.0], plays=50)
        engine = _engine_with([p])
        engine._apply_shrinkage()
        assert p.range_vec[5] == pytest.approx(0.4)

    def test_a_missing_jump_with_a_nan_league_stays_nan_and_scores_finite(self) -> None:
        """No jump anywhere (a league row written before the SIM-532
        recompute): the value stays unmeasured. The normalizer maps it to the
        position mean in z-space, and every score is finite."""
        league = LEAGUE_RANGE_CF.copy()
        league[6:] = np.nan
        p = _cf_profile(1, [1.0, 0.0, 0.0, 0.0, 0.0, 0.5, np.nan, np.nan, np.nan], plays=0)
        q = _cf_profile(2, [0.0, 1.0, 0.0, 0.0, 0.0, 0.3, 1.0, 2.0, 3.0], plays=40)
        r = _cf_profile(3, [0.0, 0.0, 1.0, 0.0, 0.0, 0.1, -1.0, 0.0, 1.0], plays=40)
        engine = _engine_with([p, q, r], league_range=league)
        engine._apply_shrinkage()
        assert np.isnan(p.range_vec[6:]).all()
        assert not np.any(p.range_vec[6:] == 0.0)
        # A measured jump with a NaN league mean is left alone.
        assert q.range_vec[6:].tolist() == pytest.approx([1.0, 2.0, 3.0])
        _assemble(engine)
        results = engine.query(1, "CF", 2024)
        assert len(results) == 2
        assert all(np.isfinite(res.score) and np.isfinite(res.range_score) for res in results)

    def test_an_infield_range_vector_shrinks_all_six_on_the_batted_balls(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        p = FielderProfile(
            player_id=7,
            position="SS",
            season=2024,
            innings_played=800.0,
            sample_batted_balls=300,
            range_vec=np.array([2.0, 2.0, 2.0, 2.0, 0.0, 1.5]),
            error_vec=np.array([0.02, 0.01]),
            dp_vec=np.array([0.5, 0.3, 0.6, 0.1]),
            specialty_vec=np.array([0.5, 0.5]),
            eb_alpha=300 / 315,
        )
        engine._profiles = {(7, "SS", 2024): p}
        for group in engine._pos_avg:
            engine._pos_avg[group]["SS"] = {}
        engine._pos_avg["range"]["SS"][2024] = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.3])
        engine._apply_shrinkage()
        alpha = 300 / (300 + EB_N_PRIOR)
        assert p.range_vec.shape == (6,)
        assert p.range_vec[0] == pytest.approx(2.0 * alpha)
        assert p.range_vec[5] == pytest.approx(0.3 + (1.5 - 0.3) * alpha)
        assert p.sample_jump_plays == 0


# ===========================================================================
# 3. The league row
# ===========================================================================


class TestLeagueRow:
    def _load(self, entity: str, profile: dict) -> FielderSimilarityEngine:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn([], [(2024, entity, json.dumps(profile))])
        engine._load_positional_averages(conn, [2024])
        return engine

    def test_an_absent_new_key_loads_nan_and_an_absent_old_key_loads_zero(self) -> None:
        # A league row written before the SIM-532 recompute carries neither
        # Savant's figure nor the jump. The five original components have
        # never had a key and are centred on 0; the four new ones are real
        # measurements the row lacks.
        engine = self._load("fielder_CF", {"oaa_deep": 1.5, "arm_strength": 88.0})
        rng = engine._pos_avg["range"]["CF"][2024]
        assert rng.shape == (9,)
        assert rng[:5].tolist() == pytest.approx([0.0, 0.0, 0.0, 1.5, 0.0])
        assert np.isnan(rng[5:]).all()

        engine = self._load("fielder_SS", {"oaa_glove_side": -0.2})
        rng = engine._pos_avg["range"]["SS"][2024]
        assert rng.shape == (6,)
        assert rng[:5].tolist() == pytest.approx([-0.2, 0.0, 0.0, 0.0, 0.0])
        assert np.isnan(rng[5])

    def test_the_new_keys_load_in_the_groups_order(self) -> None:
        engine = self._load(
            "fielder_LF",
            {
                "jump_route_ft": 0.3,
                "jump_burst_ft": 0.2,
                "jump_reaction_ft": 0.1,
                "savant_oaa_per_100": 0.45,
                "catch_pct_added": 0.01,
            },
        )
        rng = engine._pos_avg["range"]["LF"][2024]
        assert rng.tolist() == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.01, 0.45, 0.1, 0.2, 0.3])

    def test_a_null_new_key_loads_nan(self) -> None:
        engine = self._load(
            "fielder_RF",
            {"savant_oaa_per_100": None, "jump_reaction_ft": 0.0, "jump_burst_ft": 0.5},
        )
        rng = engine._pos_avg["range"]["RF"][2024]
        assert np.isnan(rng[5])
        assert rng[6] == 0.0 and rng[7] == pytest.approx(0.5)
        assert np.isnan(rng[8])


# ===========================================================================
# 4. The profile loader
# ===========================================================================

OAA_JUMP_LOADER_COLUMNS = [
    "savant_oaa_per_100",
    "jump_reaction_ft",
    "jump_burst_ft",
    "jump_route_ft",
    "jump_plays",
]


def _row(
    pid: int,
    position: str,
    oaa_jump: tuple,
    *,
    below: bool = False,
) -> tuple:
    """One row in the loader's SELECT order (35 columns): the SIM-532 block —
    Savant's figure, reaction, burst, route, the jump's play count — sits
    right after catch_pct_added."""
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
        # range, continued (5)
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
        90.0,
        0.02,
        0.06,
        80,
        # star counts (6)
        4,
        1,
        10,
        4,
        100,
        98,
        # meta
        below,
        None,
    )


class TestProfileLoader:
    def test_the_select_names_the_five_columns_through_the_guard(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn(
            ["asof_date", *OAA_JUMP_LOADER_COLUMNS],
            [
                _row(1, "CF", (1.2, 0.8, -0.3, 0.4, 45)),
                _row(2, "RF", (None, None, None, None, None)),
                _row(3, "LF", (0.6, None, None, None, 0)),
                _row(4, "SS", (-0.9, None, None, None, None)),
            ],
        )
        engine._load_profiles(conn, [2024])
        sql = _sql_words([q for q in conn.queries if "information_schema" not in q][0])
        assert (
            "fsm.catch_pct_added, "
            "fsm.savant_oaa_per_100, fsm.jump_reaction_ft, fsm.jump_burst_ft, "
            "fsm.jump_route_ft, fsm.jump_plays, "
            "fsm.fielding_error_rate" in sql
        )

        cf = engine._profiles[(1, "CF", 2024)]
        assert cf.range_vec.shape == (9,)
        assert cf.range_vec.tolist() == pytest.approx(
            [1.0, 0.5, 0.2, 1.5, 0.01, 1.2, 0.8, -0.3, 0.4]
        )
        assert cf.sample_jump_plays == 45
        # The blocks after the new columns still read their own values.
        assert cf.error_vec.tolist() == pytest.approx([0.02, 0.01])
        assert cf.arm_vec.tolist() == pytest.approx([90.0, 0.02, 0.06])
        assert cf.sample_arm_chances == 80
        assert cf.star_vec.tolist() == pytest.approx([0.25, 0.4, 0.98])

        rf = engine._profiles[(2, "RF", 2024)]
        assert np.isnan(rf.range_vec[5:]).all(), "NULL loads as NaN, never 0.0"
        assert rf.range_vec[:5].tolist() == pytest.approx([1.0, 0.5, 0.2, 1.5, 0.01])
        assert rf.sample_jump_plays == 0

        lf = engine._profiles[(3, "LF", 2024)]
        assert lf.range_vec[5] == pytest.approx(0.6)
        assert np.isnan(lf.range_vec[6:]).all()

        ss = engine._profiles[(4, "SS", 2024)]
        assert ss.range_vec.shape == (6,), "an infield range vector is six long"
        assert ss.range_vec[5] == pytest.approx(-0.9)
        assert ss.sample_jump_plays == 0
        assert ss.dp_vec.tolist() == pytest.approx([0.3, 0.2, 0.6, 0.1])
        assert ss.specialty_vec.tolist() == pytest.approx([0.5, 0.9])

    def test_a_table_without_the_columns_still_loads(self) -> None:
        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        conn = _FakeConn(
            ["asof_date"],
            [_row(1, "CF", (None, None, None, None, None)), _row(2, "2B", (None,) * 5)],
        )
        engine._load_profiles(conn, [2024])
        sql = [q for q in conn.queries if "information_schema" not in q][0]
        for col in OAA_JUMP_LOADER_COLUMNS:
            assert f"NULL AS {col}" in sql
        cf = engine._profiles[(1, "CF", 2024)]
        assert cf.range_vec.shape == (9,) and np.isnan(cf.range_vec[5:]).all()
        assert cf.sample_jump_plays == 0
        second = engine._profiles[(2, "2B", 2024)]
        assert second.range_vec.shape == (6,) and np.isnan(second.range_vec[5])

    def test_build_end_to_end_on_the_canonical_schema(self, tmp_path: Path) -> None:
        """The real SELECT against the real columns, the league keys, the
        two shrinkage bases and the scorers in one pass."""
        path = str(tmp_path / "sim532.duckdb")
        con = duckdb.connect(path)
        con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        for col, typ in SIM532_FIELDER_COLUMNS:
            con.execute(
                f"ALTER TABLE derived.fielder_season_metrics ADD COLUMN IF NOT EXISTS {col} {typ}"
            )
        con.execute(
            "CREATE TABLE derived.league_averages (entity_type VARCHAR, season SMALLINT, "
            "profile_json JSON)"
        )
        common = (
            "innings_played, sample_batted_balls, oaa_glove_side, oaa_arm_side, oaa_charging, "
            "oaa_deep, catch_pct_added, fielding_error_rate, throwing_error_rate, "
            "five_star_opps, five_star_catches, four_star_opps, four_star_catches, "
            "routine_opps, routine_catches, below_minimum_sample"
        )
        con.execute(
            f"INSERT INTO derived.fielder_season_metrics (player_id, position, season, {common}, "
            "arm_strength, arm_opportunities, arm_advancement_prevention, arm_thrown_out_rate, "
            "savant_oaa, savant_oaa_per_100, jump_reaction_ft, jump_burst_ft, jump_route_ft, "
            "jump_plays) VALUES "
            # a full read: Savant's figure and a 25-play jump
            "(1, 'CF', 2024, 900, 600, 2.0, 1.0, 0.5, 3.0, 0.02, 0.01, 0.005, "
            " 5, 2, 12, 6, 120, 118, FALSE, 92.0, 100, 0.06, 0.20, 6, 1.0, 2.0, 1.0, 0.0, 25), "
            # no jump row; Savant's figure present
            "(2, 'CF', 2024, 900, 600, 1.0, 0.0, 0.5, 1.0, 0.01, 0.02, 0.010, "
            " 4, 1, 10, 4, 110, 108, FALSE, 88.0, 100, 0.00, 0.05, -3, -0.5, NULL, NULL, NULL, NULL), "
            # neither (a 2015 row would look like this)
            "(3, 'CF', 2024, 900, 600, 0.0, -1.0, 0.0, -1.0, 0.00, 0.03, 0.015, "
            " 3, 0, 8, 3, 100, 97, FALSE, 85.0, 100, 0.00, 0.05, NULL, NULL, NULL, NULL, NULL, NULL)"
        )
        con.execute(
            f"INSERT INTO derived.fielder_season_metrics (player_id, position, season, {common}, "
            "dp_above_expected, dp_attempt_rate, dp_success_rate, dp_pivot_above_expected, "
            "bunt_fielding_rate, scoop_success_rate, savant_oaa, savant_oaa_per_100) VALUES "
            "(4, 'SS', 2024, 900, 600, 1.0, 1.0, 0.0, 0.0, 0.01, 0.02, 0.010, "
            " 0, 0, 0, 0, 0, 0, FALSE, 0.5, 0.3, 0.6, 0.1, 0.5, 0.5, 4, 0.7)"
        )
        base_keys = {
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
        }
        con.execute(
            "INSERT INTO derived.league_averages VALUES ('fielder_CF', 2024, ?), "
            "('fielder_SS', 2024, ?)",
            [
                json.dumps(
                    {
                        **base_keys,
                        "arm_advancement_prevention": 0.0,
                        "arm_thrown_out_rate": 0.05,
                        "arm_strength": 88.0,
                        "savant_oaa_per_100": 0.2,
                        "jump_reaction_ft": 0.5,
                        "jump_burst_ft": 1.0,
                        "jump_route_ft": -0.5,
                    }
                ),
                json.dumps({**base_keys, "savant_oaa_per_100": 0.1}),
            ],
        )
        con.close()

        engine = FielderSimilarityEngine(duckdb_path=path)
        engine.build([2024])
        assert engine.profile_count == 4
        p1 = engine.get_profile(1, "CF", 2024)
        p2 = engine.get_profile(2, "CF", 2024)
        p3 = engine.get_profile(3, "CF", 2024)
        p4 = engine.get_profile(4, "SS", 2024)
        assert p1 is not None and p2 is not None and p3 is not None and p4 is not None
        assert (p1.sample_jump_plays, p2.sample_jump_plays, p3.sample_jump_plays) == (25, 0, 0)
        bb = 600 / (600 + EB_N_PRIOR)
        # 25 plays: half his own reading on the jump; the base on the batted balls.
        assert p1.range_vec[5] == pytest.approx(0.2 + (1.0 - 0.2) * bb)
        assert p1.range_vec[6:].tolist() == pytest.approx(
            [0.5 + 1.5 * 0.5, 1.0 + 0.0 * 0.5, -0.5 + 0.5 * 0.5]
        )
        # No jump row: the league's jump; his own Savant figure on the batted balls.
        assert p2.range_vec[5] == pytest.approx(0.2 + (-0.5 - 0.2) * bb)
        assert p2.range_vec[6:].tolist() == pytest.approx([0.5, 1.0, -0.5])
        # Nothing at all: the league's figure and the league's jump.
        assert p3.range_vec[5:].tolist() == pytest.approx([0.2, 0.5, 1.0, -0.5])
        # The shortstop: six entries, the sixth on the batted balls.
        assert p4.range_vec.shape == (6,)
        assert p4.range_vec[5] == pytest.approx(0.1 + (0.7 - 0.1) * bb)
        results = engine.query(1, "CF", 2024)
        assert len(results) == 2 and all(0.0 <= r.score <= 1.0 for r in results)


# ===========================================================================
# 5. Scoring
# ===========================================================================


class TestScoring:
    def test_score_all_and_query_pair_agree_with_mixed_missing_jumps(self) -> None:
        profiles = [
            _cf_profile(1, [3.0, 1.0, 0.5, 2.0, 0.01, 1.0, 2.0, 1.0, 0.5], plays=60),
            _cf_profile(2, [-1.0, 0.0, 0.5, -2.0, -0.01, -0.8, np.nan, np.nan, np.nan], plays=0),
            _cf_profile(3, [0.0, 2.0, -0.5, 0.0, 0.0, np.nan, -1.0, np.nan, 2.0], plays=12),
            _cf_profile(4, [1.0, -1.0, 1.5, 1.0, 0.02, 0.4, 0.0, 3.0, -1.0], plays=30),
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
            assert pair.range_score == pytest.approx(batch[pid].range_score)
            assert 0.0 <= pair.range_score <= 1.0
        back = engine.query_pair((4, "CF", 2024), (1, "CF", 2024))
        assert back is not None and back.score == pytest.approx(batch[4].score)

    def test_a_closer_jump_scores_higher_on_the_range_sub_score(self) -> None:
        base = [1.0, 0.5, 0.0, 1.0, 0.01, 0.5]
        profiles = [
            _cf_profile(1, [*base, 3.0, 2.0, 1.0], plays=300),
            _cf_profile(2, [*base, 2.8, 1.9, 1.1], plays=300),
            _cf_profile(3, [*base, -3.0, -2.0, -1.0], plays=300),
        ]
        engine = _engine_with(profiles)
        engine._apply_shrinkage()
        _assemble(engine)
        near = engine.query_pair((1, "CF", 2024), (2, "CF", 2024))
        far = engine.query_pair((1, "CF", 2024), (3, "CF", 2024))
        assert near is not None and far is not None
        assert near.range_score > far.range_score


# ===========================================================================
# 6. Calibration wiring
# ===========================================================================


class TestApplyCalibration:
    def test_a_stale_five_weight_range_report_is_refused_for_both_groups(self, caplog) -> None:
        from similarity.similarity_calibration import CalibrationReport

        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        stale = CalibrationReport(
            sigma_of_range=0.7,
            sigma_if_range=0.8,
            reliability_weights_of_range=np.full(5, 0.1),
            reliability_weights_if_range=np.full(5, 0.1),
        )
        with caplog.at_level(logging.WARNING, logger="fielder_similarity"):
            engine.apply_calibration(stale)
        of_default = np.array([w for _, w in OF_RANGE_FEATURES])
        if_default = np.array([w for _, w in IF_RANGE_FEATURES])
        np.testing.assert_allclose(engine._of_range_rbf.weights, of_default / of_default.sum())
        np.testing.assert_allclose(engine._if_range_rbf.weights, if_default / if_default.sum())
        assert engine._of_range_rbf.sigma == 0.7, "the sigma still applies"
        assert engine._if_range_rbf.sigma == 0.8
        messages = [r.message for r in caplog.records]
        assert any("reliability_weights_of_range" in m for m in messages)
        assert any("reliability_weights_if_range" in m for m in messages)
        # A query on a nine-feature range then works.
        profiles = [
            _cf_profile(1, [1.0, 0.0, 0.0, 0.0, 0.0, 0.5, 1.0, 2.0, 3.0], plays=40),
            _cf_profile(2, [0.0, 1.0, 0.0, 0.0, 0.0, 0.2, 0.0, 1.0, 2.0], plays=40),
        ]
        engine._profiles = {(p.player_id, p.position, p.season): p for p in profiles}
        _assemble(engine)
        assert engine.query_pair((1, "CF", 2024), (2, "CF", 2024)) is not None

    def test_a_nine_weight_and_a_six_weight_range_report_apply(self) -> None:
        from similarity.similarity_calibration import CalibrationReport

        engine = FielderSimilarityEngine(duckdb_path=":memory:")
        nine = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.5, 0.9, 0.7, 0.8])
        six = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.5])
        engine.apply_calibration(
            CalibrationReport(reliability_weights_of_range=nine, reliability_weights_if_range=six)
        )
        np.testing.assert_allclose(engine._of_range_rbf.weights, nine / nine.sum())
        np.testing.assert_allclose(engine._if_range_rbf.weights, six / six.sum())


# ===========================================================================
# 7. The calibrator
# ===========================================================================


def _of_calibration_rows(n: int, seed: int, jump_every: int = 3) -> list[tuple]:
    """Outfield rows in the calibrator's SELECT order: 4 meta, 9 range, 2
    errors, 3 arm, 6 star counts, then the arm's chance count, then the
    jump's play count. The three jump parts and the play count are NULL on
    every ``jump_every``-th row; the other rows carry 25 to 79 plays, at or
    over the jump's pair floor."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        jump = tuple(rng.normal(0, 1, 3).tolist()) if i % jump_every else (None, None, None)
        f5o, f4o, ro = int(rng.integers(2, 8)), int(rng.integers(6, 20)), int(rng.integers(80, 160))
        rows.append(
            (
                3000 + i % 40,
                "CF",
                2023 + i // 40,
                int(rng.integers(200, 600)),
                *rng.normal(0, 2, 5).tolist(),
                float(rng.normal(0, 0.5)),
                *jump,
                *rng.beta(2, 60, 2).tolist(),
                float(rng.normal(88, 3)),
                float(rng.normal(0, 0.03)),
                float(rng.beta(2, 30)),
                f5o,
                int(rng.integers(0, f5o + 1)),
                f4o,
                int(rng.integers(0, f4o + 1)),
                ro,
                ro - int(rng.integers(0, 5)),
                int(rng.integers(60, 300)),
                int(rng.integers(25, 80)) if jump[0] is not None else None,
            )
        )
    return rows


def _if_calibration_rows(n: int, seed: int, savant_every: int = 3) -> list[tuple]:
    """Infield rows in the calibrator's SELECT order: 4 meta, 6 range, 2
    errors, 3 DP, 1 pivot, 2 specialty. Savant's figure is NULL on every
    ``savant_every``-th row."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        savant = float(rng.normal(0, 0.5)) if i % savant_every else None
        rows.append(
            (
                7000 + i % 40,
                "SS",
                2023 + i // 40,
                int(rng.integers(200, 600)),
                *rng.normal(0, 2, 5).tolist(),
                savant,
                *rng.beta(2, 60, 2).tolist(),
                *rng.normal(0, 1, 3).tolist(),
                float(rng.normal(0, 1)),
                float(rng.beta(5, 5)),
                float(rng.beta(5, 5)),
            )
        )
    return rows


def _paired_cf_rows(
    range_by_key: dict[tuple[int, int], list[float]],
    chances: int = 200,
    plays: int | None = 40,
) -> list[tuple]:
    """Centre-field rows in the calibrator's SELECT order with a HAND-SET
    nine-long range block per (player, season); everything else is
    deterministic filler. ``chances`` is the arm's chance count and
    ``plays`` the jump's play count, the two trailing columns in that order.
    ``plays`` may be a per-(player, season) dict."""
    rows = []
    for (pid, season), rng_vec in range_by_key.items():
        row_plays = plays.get((pid, season)) if isinstance(plays, dict) else plays
        rows.append(
            (
                pid,
                "CF",
                season,
                400,
                *rng_vec,
                0.02,
                0.01,
                88.0,
                0.0,
                0.05,
                5,
                3,
                12,
                8,
                120,
                118,
                chances,
                row_plays,
            )
        )
    return rows


def _nullify_nan(rows: list[tuple]) -> list[tuple]:
    """NaN in a hand-set row is the SELECT's NULL."""
    return [tuple(None if isinstance(v, float) and np.isnan(v) else v for v in r) for r in rows]


class TestCalibrator:
    def test_the_two_selects_name_the_range_columns_in_the_groups_order(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        conn = _RoutingConn(_if_calibration_rows(120, seed=31), _of_calibration_rows(120, seed=32))
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        cal._calibrate_fielder_params(conn, [2023, 2024, 2025], 0.5, CalibrationReport())

        of_sql = _sql_words([q for q in conn.queries if "'LF'" in q][0])
        of_range = of_sql.split("sample_batted_balls, ")[1].split(", fielding_error_rate")[0]
        assert of_range.split(", ") == [f for f, _ in OF_RANGE_FEATURES]

        if_sql = _sql_words([q for q in conn.queries if "'1B'" in q][0])
        if_range = if_sql.split("sample_batted_balls, ")[1].split(", fielding_error_rate")[0]
        assert if_range.split(", ") == [f for f, _ in IF_RANGE_FEATURES]
        assert "jump_" not in if_sql

    def test_the_range_sigma_fits_over_the_rows_that_carry_every_feature(self) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        of_rows = _of_calibration_rows(120, seed=33)
        if_rows = _if_calibration_rows(120, seed=34)
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_fielder_params(
            _RoutingConn(if_rows, of_rows), [2023, 2024, 2025], 0.5, CalibrationReport()
        )
        assert report.sigma_of_range > 0.0 and report.sigma_if_range > 0.0

        of_measured = np.array([r[4:13] for r in of_rows if r[10] is not None], dtype=np.float64)
        assert report.sigma_of_range == pytest.approx(
            cal._fit_sigma(cal._zscore_matrix(of_measured), 0.5)
        )
        if_measured = np.array([r[4:10] for r in if_rows if r[9] is not None], dtype=np.float64)
        assert report.sigma_if_range == pytest.approx(
            cal._fit_sigma(cal._zscore_matrix(if_measured), 0.5)
        )
        # More unmeasured rows do not move either sigma (a zero-filled fit would).
        more_of = of_rows + [r[:10] + (None, None, None) + r[13:] for r in of_rows[:60]]
        more_if = if_rows + [r[:9] + (None,) + r[10:] for r in if_rows[:60]]
        again = cal._calibrate_fielder_params(
            _RoutingConn(more_if, more_of), [2023, 2024, 2025], 0.5, CalibrationReport()
        )
        assert again.sigma_of_range == pytest.approx(report.sigma_of_range)
        assert again.sigma_if_range == pytest.approx(report.sigma_if_range)
        # The blocks after the range still read their own columns.
        err = np.array([r[13:15] for r in of_rows], dtype=np.float64)
        assert report.sigma_of_errors == pytest.approx(cal._fit_sigma(cal._zscore_matrix(err), 0.5))
        arm = np.array([r[15:18] for r in of_rows], dtype=np.float64)
        assert report.sigma_of_arm == pytest.approx(cal._fit_sigma(cal._zscore_matrix(arm), 0.5))
        if_err = np.array([r[10:12] for r in if_rows], dtype=np.float64)
        assert report.sigma_if_errors == pytest.approx(
            cal._fit_sigma(cal._zscore_matrix(if_err), 0.5)
        )

    def test_the_range_sigma_keeps_the_sentinel_under_twenty_measured_rows(self, caplog) -> None:
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        of_rows = _of_calibration_rows(120, seed=35, jump_every=8)
        thin_of = [r[:10] + (None, None, None) + r[13:] for r in of_rows[:101]] + of_rows[101:]
        assert sum(r[10] is not None for r in thin_of) < 20
        if_rows = _if_calibration_rows(120, seed=36, savant_every=8)
        thin_if = [r[:9] + (None,) + r[10:] for r in if_rows[:101]] + if_rows[101:]
        assert sum(r[9] is not None for r in thin_if) < 20
        with caplog.at_level(logging.WARNING, logger="similarity.similarity_calibration"):
            report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
                _RoutingConn(thin_if, thin_of), [2023, 2024, 2025], 0.5, CalibrationReport()
            )
        assert report.sigma_of_range == 0.0, "the keep-default sentinel"
        assert report.sigma_if_range == 0.0
        assert "outfield range sigma" in caplog.text and "infield range sigma" in caplog.text
        # The other groups still fit, and the reliability fit still runs on
        # the whole matrix (the five original components carry every pair).
        assert report.sigma_of_arm > 0.0 and report.sigma_if_dp > 0.0
        assert report.reliability_weights_of_range is not None
        assert len(report.reliability_weights_of_range) == 9
        assert report.reliability_weights_if_range is not None
        assert len(report.reliability_weights_if_range) == 6
        # Under 20 pairs the three jump parts and Savant's figure keep their
        # module defaults (the report-level rule; see the tests below).
        assert report.reliability_weights_of_range[6:9].tolist() == [
            w for _, w in OF_RANGE_FEATURES[6:9]
        ]
        assert report.reliability_weights_if_range[5] == IF_RANGE_FEATURES[5][1]

    @staticmethod
    def _range_with_a_nan_reaction(seed: int) -> dict[tuple[int, int], list[float]]:
        """Thirty centre fielders with 2023 and 2024. Savant's figure and the
        route repeat exactly; the reaction is NaN everywhere; our five and
        the burst are noise."""
        rng = np.random.default_rng(seed)
        by_key: dict[tuple[int, int], list[float]] = {}
        for pid in range(8000, 8030):
            savant = float(rng.normal(0, 0.5))
            route = float(rng.normal(0, 1))
            for season in (2023, 2024):
                by_key[(pid, season)] = [
                    *rng.normal(0, 2, 5).tolist(),
                    savant,
                    np.nan,
                    float(rng.normal(0, 1)),
                    route,
                ]
        return by_key

    def test_the_reliability_fit_drops_nan_pairs_per_feature(self) -> None:
        """The RAW fit (``calibrate_reliability_weights``): the nine-long
        vector reads a repeat near 1 for the hand-set features, noise low,
        and exactly 0.5 — its insufficient-data default, above the 0.1
        floor — for the column that never pairs. The report layer overrides
        that 0.5 (the next test)."""
        from similarity.similarity_calibration import calibrate_reliability_weights

        by_key = self._range_with_a_nan_reaction(37)
        matrix = np.array(list(by_key.values()), dtype=np.float64)
        ids = np.array([pid for pid, _ in by_key], dtype=np.int64)
        seasons = np.array([season for _, season in by_key], dtype=np.int64)
        w = calibrate_reliability_weights(matrix, ids, seasons=seasons)
        assert w.shape == (9,)
        assert w[5] == pytest.approx(1.0)
        assert w[8] == pytest.approx(1.0)
        assert w[6] == 0.5, "the raw fit's default for a column with no pair"
        assert w[7] < 0.6, "noise reads low"

    def test_a_new_entry_with_no_pairs_keeps_its_module_default(self, caplog) -> None:
        """The REPORT: the same rows through the calibrator. The all-NaN
        reaction column keeps the module default (0.775) with a warning that
        names it; the repeating route still reads near 1; the sigma has no
        measured row and keeps the sentinel."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rows = _nullify_nan(_paired_cf_rows(self._range_with_a_nan_reaction(37)))
        with caplog.at_level(logging.WARNING, logger="similarity.similarity_calibration"):
            report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
                _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
            )
        w = report.reliability_weights_of_range
        assert w is not None and w.shape == (9,)
        assert w[6] == OF_RANGE_FEATURES[6][1] == 0.775
        assert "jump_reaction_ft" in caplog.text and "0 reliable" in caplog.text
        assert w[5] == pytest.approx(1.0)
        assert w[8] == pytest.approx(1.0)
        assert w[7] < 0.6, "the burst has its pairs and reads its noise"
        assert report.sigma_of_range == 0.0

    def test_a_savant_entry_with_no_pairs_keeps_its_module_default_on_both_groups(
        self, caplog
    ) -> None:
        """One season only: no pair anywhere. The infield and outfield Savant
        entries keep their module defaults with a warning each; the five
        original components are not this rule's business."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        of_rows = [r for r in _of_calibration_rows(120, seed=38) if r[2] == 2023]
        if_rows = [r for r in _if_calibration_rows(120, seed=39) if r[2] == 2023]
        with caplog.at_level(logging.WARNING, logger="similarity.similarity_calibration"):
            report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
                _RoutingConn(if_rows, of_rows), [2023], 0.5, CalibrationReport()
            )
        assert report.reliability_weights_of_range[5] == OF_RANGE_FEATURES[5][1]
        assert report.reliability_weights_if_range[5] == IF_RANGE_FEATURES[5][1]
        assert "savant_oaa_per_100 in the outfield range block" in caplog.text
        assert "savant_oaa_per_100 in the infield range block" in caplog.text

    def test_rows_under_the_plays_floor_form_no_jump_pair(self) -> None:
        """Thirty centre fielders with 40 plays whose jump repeats exactly,
        and sixty with 5 plays whose jump FLIPS sign season to season. Fitted
        on every row the reaction would read the flip (near the 0.1 floor);
        on the rows at or over JUMP_ALPHA_PRIOR_PLAYS it reads near 1. The
        five original components and Savant's figure keep every pair: they
        repeat on every row here and read 1."""
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        rng = np.random.default_rng(40)
        by_key: dict[tuple[int, int], list[float]] = {}
        plays: dict[tuple[int, int], int] = {}
        for pid in range(9000, 9090):
            base = rng.normal(0, 2, 6).tolist()
            jump = rng.normal(0, 1, 3)
            thin = pid >= 9030
            for season, sign in ((2023, 1.0), (2024, -1.0 if thin else 1.0)):
                by_key[(pid, season)] = [*base, *(sign * jump).tolist()]
                plays[(pid, season)] = 5 if thin else 40
        assert JUMP_ALPHA_PRIOR_PLAYS == 25
        rows = _paired_cf_rows(by_key, plays=plays)
        report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
            _RoutingConn([], rows), [2023, 2024], 0.5, CalibrationReport()
        )
        w = report.reliability_weights_of_range
        assert w is not None
        assert w[6] == pytest.approx(1.0)
        assert w[7] == pytest.approx(1.0)
        assert w[8] == pytest.approx(1.0)
        assert np.allclose(w[:6], 1.0)
        # Every pair at 5 plays: the jump trio has no reliable pair and
        # keeps the module defaults.
        thin_only = _paired_cf_rows(by_key, plays=5)
        thin_report = SimilarityCalibrator(duckdb_path=":memory:")._calibrate_fielder_params(
            _RoutingConn([], thin_only), [2023, 2024], 0.5, CalibrationReport()
        )
        assert thin_report.reliability_weights_of_range[6:9].tolist() == [
            w for _, w in OF_RANGE_FEATURES[6:9]
        ]
        assert np.allclose(thin_report.reliability_weights_of_range[:6], 1.0)

    def test_the_arm_chance_count_reads_its_own_column_after_the_play_count(self) -> None:
        """The sentinel rows: a distinct value in each trailing column. With
        200 chances and 5 plays the arm fit runs (its floor is 50 chances);
        swapped — 5 chances and 200 plays — it does not. A trailing read
        (``r[-1]``) would give the opposite answer on both."""
        from similarity.engines.fielder_similarity import ARM_ALPHA_PRIOR_CHANCES
        from similarity.similarity_calibration import CalibrationReport, SimilarityCalibrator

        assert ARM_ALPHA_PRIOR_CHANCES == 50
        rng = np.random.default_rng(41)
        by_key = {
            (pid, season): rng.normal(0, 1, 9).tolist()
            for pid in range(9500, 9530)
            for season in (2023, 2024)
        }
        cal = SimilarityCalibrator(duckdb_path=":memory:")
        right = cal._calibrate_fielder_params(
            _RoutingConn([], _paired_cf_rows(by_key, chances=200, plays=5)),
            [2023, 2024],
            0.5,
            CalibrationReport(),
        )
        assert right.reliability_weights_of_arm is not None
        swapped = cal._calibrate_fielder_params(
            _RoutingConn([], _paired_cf_rows(by_key, chances=5, plays=200)),
            [2023, 2024],
            0.5,
            CalibrationReport(),
        )
        assert swapped.reliability_weights_of_arm is None
        # And the play count reads ITS column: at 5 plays the jump trio has
        # no reliable pair (the defaults); at 200 it is fitted.
        assert right.reliability_weights_of_range[6:9].tolist() == [
            w for _, w in OF_RANGE_FEATURES[6:9]
        ]
        assert swapped.reliability_weights_of_range[6:9].tolist() != [
            w for _, w in OF_RANGE_FEATURES[6:9]
        ]
