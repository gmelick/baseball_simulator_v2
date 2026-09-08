"""
tests/unit/test_sim518_conditioning.py
======================================
SIM-518 — the draw-conditioning enrichment epic, the CODE half (no live DB,
no rebuild). Plan: docs/audit/2026-09-04-sim467-518-plan.md §6 + §8.

Data parts (SIM-464 pitch half, SIM-465, SIM-463):
  * migration 0023 adds ``bat_home`` / ``pitcher_pitch_count`` /
    ``times_through_order`` to ``sim.pitch_pool``, appended LAST (the pool
    INSERT is positional);
  * the builder's two fatigue window expressions agree with the live helper
    ``simulation.sim_loop.times_through_order`` — one definition, both sides;
  * the artifact exports the three pitch-pool columns and the batted-ball
    pool's producing-pitch geometry (``pgeom``, NaN preserved) + ``pitcher_id``;
    the loader reads them, a pre-sim518 bundle still loads (fields None), and
    the numeric columns ride the SIM-403b shared-memory seam.

Consumers (each an env-gated draw WEIGHT, byte-identical off — the owner's
architecture rule):
  * the fatigue kernel on the pitch draw (SIM-465), normalized to a mean of 1
    within each count bucket; unknown rows exactly neutral;
  * the batting-side weight on the pitch draw (SIM-464): 0.0 is a hard match,
    unknown-side rows neutral;
  * the pitch-similarity kernel on the batted-ball draw (SIM-472): the drawn
    pitch's geometry pulls the draw toward batted balls hit off similar
    pitches; rows with missing geometry exactly neutral;
  * the loop passes each input ONLY when its weight is on (the SIM-455
    two-argument call is unchanged otherwise) and snapshots fatigue once per
    plate appearance.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pytest

from pipeline.batch import player_profile_computor as ppc
from pipeline.batch.engine_artifacts import (
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    build_battedball_pool_artifact,
    build_pitch_pool_artifact,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import StateMachine, times_through_order

REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = REPO_ROOT / "db" / "migrations" / "duckdb" / "0023_sim518_pitch_pool_conditioning.sql"
_SCHEMA = REPO_ROOT / "db" / "schemas" / "02_duckdb_schema.sql"

_SEASON = 2024
_PITCHER = "100:2024"
_BATTER = "200:2024"
_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)

#: Two producing pitches, far apart in every geometry column that varies.
_FASTBALL = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)
_CURVE = np.array([78.0, -8.0, 8.0, 2600.0, 40.0, -1.0, 6.0, 6.0, 0.3, 1.5], dtype=np.float32)


# ===========================================================================
# Synthetic pools
# ===========================================================================


def _hand_pool(
    *,
    pitch_count=None,
    tto=None,
    bat_home=None,
    outcomes=None,
    geom_rows=None,
) -> HandPool:
    """8 rows, all in the 0-0 count bucket, so the drawn OUTCOME reveals which
    rows a weight favored. Column args are per-row lists (None = absent)."""
    n = 8
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    geom = np.zeros((n, 10), dtype=np.float32)
    if geom_rows is not None:
        geom = np.asarray(geom_rows, dtype=np.float32)
    return HandPool(
        geom=geom,
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(
            outcomes if outcomes is not None else ["ball"] * 4 + ["called_strike"] * 4,
            dtype=object,
        ),
        recency=np.ones(n, dtype=np.float32),
        bat_home=(np.asarray(bat_home, dtype=np.int8) if bat_home is not None else None),
        pitch_count=(np.asarray(pitch_count, dtype=np.int16) if pitch_count is not None else None),
        tto=(np.asarray(tto, dtype=np.int8) if tto is not None else None),
    )


def _bb_pool(pgeom=None, events=None) -> BattedBallPool:
    """8 transition-carrying batted-ball rows in the (empty, 0 outs) cell:
    the first 4 singles, the last 4 field outs (the drawn EVENT reveals which
    rows the kernel favored)."""
    n = 8
    sit = np.zeros((n, 6), dtype=np.float32)
    ev = events if events is not None else ["single"] * 4 + ["field_out"] * 4
    rh = np.asarray([1 if e == "single" else 0 for e in ev], dtype=np.int8)
    ro = np.asarray([0 if e == "single" else 1 for e in ev], dtype=np.int8)
    bd = np.asarray([1 if e == "single" else 0 for e in ev], dtype=np.int8)
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32),
        sit=sit,
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(ev, dtype=object),
        result_hits=rh,
        result_outs=ro,
        recency=np.ones(n, dtype=np.float32),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=bd,
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=np.zeros(n, dtype=np.int8),
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=np.zeros(n, dtype=np.float32),
        pgeom=(np.asarray(pgeom, dtype=np.float32) if pgeom is not None else None),
    )


def _two_cluster_pgeom(nan_last_four: bool = False) -> np.ndarray:
    pg = np.stack([_FASTBALL] * 4 + [_CURVE] * 4).astype(np.float32)
    if nan_last_four:
        pg[4:, 0] = np.nan
    return pg


def _sampler(pool: HandPool, bb: BattedBallPool | None = None, seed: int = 0) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        bb_pools=({"R": bb} if bb is not None else {}),
    )
    return FullPoolSampler(art, np.random.default_rng(seed))


def _draws(fp: FullPoolSampler, n: int = 40, **kw) -> set[str]:
    fp.new_half_inning("R", _PITCHER)
    out = set()
    for _ in range(n):
        fp.new_plate_appearance(_BATTER, _BASE_OUT, **kw)
        out.add(fp.draw(0, 0))
    return out


def _sequence(fp: FullPoolSampler, n: int = 30, **kw) -> list[str]:
    fp.new_half_inning("R", _PITCHER)
    seq = []
    for _ in range(n):
        fp.new_plate_appearance(_BATTER, _BASE_OUT, **kw)
        seq.append(fp.draw(0, 0))
    return seq


# ===========================================================================
# The fatigue kernel (SIM-465) on the pitch draw
# ===========================================================================


class TestFatigueKernel:
    def test_sigma_zero_is_byte_identical_even_with_inputs(self):
        pool = _hand_pool(pitch_count=[10] * 4 + [90] * 4, tto=[1] * 4 + [3] * 4)
        off = _sequence(_sampler(pool, seed=3))
        with_inputs = _sequence(_sampler(pool, seed=3), pitch_count=90, tto=3)
        assert off == with_inputs

    def test_a_tight_pitch_count_kernel_draws_the_matching_rows(self):
        pool = _hand_pool(pitch_count=[10] * 4 + [90] * 4)
        fp = _sampler(pool)
        fp.fatigue_pc_sigma = 1.0
        assert _draws(fp, pitch_count=10) == {"ball"}
        assert _draws(fp, pitch_count=90) == {"called_strike"}

    def test_a_tight_tto_kernel_draws_the_matching_rows(self):
        pool = _hand_pool(tto=[1] * 4 + [3] * 4)
        fp = _sampler(pool)
        fp.fatigue_tto_sigma = 0.3
        assert _draws(fp, tto=3) == {"called_strike"}
        assert _draws(fp, tto=1) == {"ball"}

    def test_the_factor_mean_is_one_within_the_bucket(self):
        pool = _hand_pool(pitch_count=[10] * 4 + [90] * 4)
        fp = _sampler(pool)
        fp.fatigue_pc_sigma = 20.0
        f = fp._f_fatigue("R", 40, None)
        assert f is not None
        assert f.mean() == pytest.approx(1.0, abs=1e-5)

    def test_unknown_rows_are_exactly_neutral(self):
        # 4 unknown-count rows (ball), 2 matching rows (called_strike), 2 far
        # rows (swinging_strike). Tight sigma: the far rows vanish, the
        # matching rows normalize to the mean, and the unknown rows sit AT the
        # mean — drawn, never starved, never favored.
        pool = _hand_pool(
            pitch_count=[-1] * 4 + [90, 90, 10, 10],
            outcomes=["ball"] * 4 + ["called_strike"] * 2 + ["swinging_strike"] * 2,
        )
        fp = _sampler(pool)
        fp.fatigue_pc_sigma = 1.0
        seen = _draws(fp, n=200, pitch_count=90)
        assert seen == {"ball", "called_strike"}
        f = fp._f_fatigue("R", 90, None)
        assert f is not None
        assert np.allclose(f[:4], 1.0)  # unknown rows untouched
        assert np.allclose(f[4:6], 2.0)  # the two valid matches share the mass
        assert np.allclose(f[6:], 0.0)

    def test_neutral_when_the_pool_has_no_fatigue_columns(self):
        pool = _hand_pool()  # pre-0023 bundle: both columns None
        off = _sequence(_sampler(pool, seed=5))
        fp = _sampler(pool, seed=5)
        fp.fatigue_pc_sigma = 1.0
        fp.fatigue_tto_sigma = 1.0
        assert fp._f_fatigue("R", 90, 3) is None
        assert _sequence(fp, pitch_count=90, tto=3) == off


# ===========================================================================
# The batting-side weight (SIM-464's pitch half) on the pitch draw
# ===========================================================================


class TestPitchHomeWeight:
    def test_weight_one_is_byte_identical(self):
        pool = _hand_pool(bat_home=[1] * 4 + [0] * 4)
        off = _sequence(_sampler(pool, seed=11))
        on = _sequence(_sampler(pool, seed=11), bat_home=True)
        assert off == on

    def test_weight_zero_is_a_hard_match_on_the_side(self):
        pool = _hand_pool(bat_home=[1] * 4 + [0] * 4)
        fp = _sampler(pool)
        fp.pitch_home_off_weight = 0.0
        assert _draws(fp, bat_home=True) == {"ball"}
        assert _draws(fp, bat_home=False) == {"called_strike"}

    def test_unknown_side_rows_stay_neutral(self):
        pool = _hand_pool(bat_home=[-1] * 4 + [0] * 4)
        fp = _sampler(pool)
        fp.pitch_home_off_weight = 0.0
        assert _draws(fp, bat_home=True) == {"ball"}  # the -1 rows survive

    def test_neutral_when_the_pool_has_no_side_column(self):
        pool = _hand_pool()
        seq_off = _sequence(_sampler(pool, seed=2))
        fp = _sampler(pool, seed=2)
        fp.pitch_home_off_weight = 0.0
        assert _sequence(fp, bat_home=True) == seq_off


# ===========================================================================
# The pitch-similarity kernel (SIM-472) on the batted-ball draw
# ===========================================================================


def _bb_draws(fp: FullPoolSampler, n: int = 40, **kw) -> set[str]:
    out = set()
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, dtype=np.float32), **kw)
        out.add(fp.battedball_draw()[0])
    return out


def _bb_sequence(fp: FullPoolSampler, n: int = 30, **kw) -> list[str]:
    seq = []
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, dtype=np.float32), **kw)
        seq.append(fp.battedball_draw()[0])
    return seq


class TestPitchSimilarityKernel:
    def test_sigma_zero_is_byte_identical_even_with_a_pitch(self):
        bb = _bb_pool(pgeom=_two_cluster_pgeom())
        off = _bb_sequence(_sampler(_hand_pool(), bb, seed=7))
        on = _bb_sequence(_sampler(_hand_pool(), bb, seed=7), pitch_geom=_FASTBALL)
        assert off == on

    def test_a_tight_kernel_draws_batted_balls_off_the_similar_pitch(self):
        bb = _bb_pool(pgeom=_two_cluster_pgeom())
        fp = _sampler(_hand_pool(), bb)
        fp.bb_pitch_sigma = 0.05
        assert _bb_draws(fp, pitch_geom=_FASTBALL) == {"single"}
        assert _bb_draws(fp, pitch_geom=_CURVE) == {"field_out"}

    def test_rows_with_missing_geometry_are_exactly_neutral(self):
        bb = _bb_pool(pgeom=_two_cluster_pgeom(nan_last_four=True))
        fp = _sampler(_hand_pool(), bb)
        fp.bb_pitch_sigma = 0.05
        rows = np.arange(8)
        f = fp._f_pitch_similarity("R", rows, _FASTBALL)
        assert f is not None
        assert np.allclose(f[:4], 1.0)  # the four complete matches share the mass
        assert np.allclose(f[4:], 1.0)  # the NaN rows sit at the mean
        assert _bb_draws(fp, n=200, pitch_geom=_FASTBALL) == {"single", "field_out"}

    def test_neutral_when_the_pool_has_no_pgeom(self):
        bb = _bb_pool()  # pre-sim518 export
        seq_off = _bb_sequence(_sampler(_hand_pool(), bb, seed=9))
        fp = _sampler(_hand_pool(), bb, seed=9)
        fp.bb_pitch_sigma = 0.05
        assert fp._f_pitch_similarity("R", np.arange(8), _FASTBALL) is None
        assert _bb_sequence(fp, pitch_geom=_FASTBALL) == seq_off

    def test_last_pitch_geom_reads_the_drawn_row(self):
        geom = np.stack([_FASTBALL] * 8)
        fp = _sampler(_hand_pool(geom_rows=geom))
        assert fp.last_pitch_geom() is None  # before any draw
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        fp.draw(0, 0)
        g = fp.last_pitch_geom()
        assert g is not None and np.allclose(g, _FASTBALL)

    def test_last_pitch_geom_is_none_when_the_row_has_no_velocity(self):
        # The export writes a NULL velocity as 0.0; no real pitch reads 0 mph.
        fp = _sampler(_hand_pool())  # geom all zeros
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        fp.draw(0, 0)
        assert fp.last_pitch_geom() is None


# ===========================================================================
# The loop wiring — inputs pass ONLY when a weight is on; fatigue per PA
# ===========================================================================


class _Recorder(FullPoolSampler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.pa_kwargs: list[dict] = []
        self.bb_kwargs: list[dict] = []

    def new_plate_appearance(self, batter_key, base_out, **kw) -> None:
        self.pa_kwargs.append(dict(kw))
        super().new_plate_appearance(batter_key, base_out, **kw)

    def battedball_new_pa(self, *a, **kw) -> None:
        self.bb_kwargs.append({k: v for k, v in kw.items() if k == "pitch_geom"})
        super().battedball_new_pa(*a, **kw)


def _machine(bb: BattedBallPool | None = None) -> tuple[StateMachine, _Recorder]:
    geom = np.stack([_FASTBALL] * 8)
    pool = _hand_pool(
        pitch_count=[10] * 4 + [90] * 4,
        tto=[1] * 4 + [3] * 4,
        bat_home=[1] * 4 + [0] * 4,
        geom_rows=geom,
    )
    art = EngineArtifacts(
        pools={"R": pool, "L": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        bb_pools=({"R": bb, "L": bb} if bb is not None else {}),
    )
    fp = _Recorder(art, np.random.default_rng(7))
    return StateMachine(fp, rng=np.random.default_rng(7)), fp


def _state(**kw) -> GameState:
    base = {
        "pitcher_id": 100,
        "bat_hand": "R",
        "season": _SEASON,
        "away_lineup": [200, 201, 202],
        "home_lineup": [200, 201, 202],
    }
    base.update(kw)
    state = GameState(**base)
    state.batter_id = 200
    assert state.half is Half.TOP and state.offense is Team.AWAY
    return state


class TestLoopWiring:
    def test_everything_off_passes_no_keyword_arguments(self):
        machine, fp = _machine()
        state = _state()
        state.pitcher_pitch_count = 31
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs == [{}]

    def test_fatigue_is_snapshotted_on_the_first_pitch_and_held_across_the_pa(self):
        machine, fp = _machine()
        fp.fatigue_pc_sigma = 5.0
        state = _state()
        # The loop incremented the count for THIS pitch already: 31 thrown
        # including this one -> 30 before the PA. 18 completed PAs -> the
        # third time through (18 // 9 + 1).
        state.pitcher_pitch_count = 31
        state.pitcher_bf = {100: 18}
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1] == {"pitch_count": 30, "tto": 3}
        assert times_through_order(18) == 3
        # Mid-PA: the count moved on, a runner reached (a base-out change
        # rebuilds the weight) — the snapshot holds.
        state.balls, state.strikes = 2, 1
        state.pitcher_pitch_count = 34
        state.bases = Bases(first=201)
        machine._full_pool_outcome(state)
        assert len(fp.pa_kwargs) == 2
        assert fp.pa_kwargs[-1] == {"pitch_count": 30, "tto": 3}
        # A new PA (0-0, the next batter, one more completed PA): a fresh snapshot.
        state.balls, state.strikes = 0, 0
        state.batter_id = 201
        state.pitcher_pitch_count = 40
        state.pitcher_bf = {100: 19}
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1] == {"pitch_count": 39, "tto": 3}

    def test_a_pitcher_change_refreshes_the_snapshot_mid_pa(self):
        machine, fp = _machine()
        fp.fatigue_pc_sigma = 5.0
        state = _state()
        state.pitcher_pitch_count = 31
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1]["pitch_count"] == 30
        state.balls = 1
        state.pitcher_id = 100  # same arm, later pitch: held
        state.pitcher_pitch_count = 32
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1]["pitch_count"] == 30
        state.pitcher_id = 101  # a new arm mid-PA: his own count
        state.pitcher_pitch_count = 1
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1]["pitch_count"] == 0

    def test_the_batting_side_passes_only_when_its_weight_is_on(self):
        machine, fp = _machine()
        fp.pitch_home_off_weight = 0.0
        state = _state()  # TOP: the away team bats
        machine._full_pool_outcome(state)
        assert fp.pa_kwargs[-1] == {"bat_home": False}

    def test_the_drawn_pitch_reaches_the_batted_ball_draw_only_when_on(self):
        bb = _bb_pool(pgeom=_two_cluster_pgeom())
        machine, fp = _machine(bb)
        state = _state()
        machine._full_pool_outcome(state)  # the drawn row is a fastball
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1] == {}
        fp.bb_pitch_sigma = 0.05
        machine._full_pool_fielding(state)
        assert "pitch_geom" in fp.bb_kwargs[-1]
        assert np.allclose(fp.bb_kwargs[-1]["pitch_geom"], _FASTBALL)


# ===========================================================================
# The builder's fatigue columns agree with the live helper
# ===========================================================================


class TestBuilderWindowExpressions:
    def test_pitch_count_and_tto_match_the_live_definitions(self):
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                "CREATE TABLE pitches (game_pk INTEGER, pitcher INTEGER, "
                "at_bat_number INTEGER, pitch_number INTEGER)"
            )
            # Pitcher 1: PAs 1..12, three pitches each (the 10th PA is the 2nd
            # time through). Pitcher 2 relieves: PAs 13..14, two pitches each.
            rows = []
            for ab in range(1, 13):
                rows += [(1, 1, ab, p) for p in (1, 2, 3)]
            for ab in (13, 14):
                rows += [(1, 2, ab, p) for p in (1, 2)]
            con.executemany("INSERT INTO pitches VALUES (?,?,?,?)", rows)
            got = con.execute(
                f"SELECT pitcher, at_bat_number, pitch_number, "
                f"{ppc.SQL_PITCHER_PITCH_COUNT} AS pc, {ppc.SQL_TIMES_THROUGH_ORDER} AS tto "
                "FROM pitches ORDER BY pitcher, at_bat_number, pitch_number"
            ).fetchall()
        finally:
            con.close()
        for pitcher, ab, _pn, pc, tto in got:
            if pitcher == 1:
                pas_before = ab - 1
                assert pc == 3 * pas_before, (ab, pc)
            else:
                pas_before = ab - 13
                assert pc == 2 * pas_before, (ab, pc)
            assert tto == times_through_order(pas_before), (pitcher, ab, tto)
        # The second time through starts on the 10th batter faced.
        by_ab = {(p, ab): tto for p, ab, _pn, _pc, tto in got}
        assert by_ab[(1, 9)] == 1 and by_ab[(1, 10)] == 2 and by_ab[(2, 13)] == 1

    def test_the_builder_version_moved_and_the_select_appends_the_columns(self):
        assert ppc.POOL_BUILDER_VERSION == "sim518.1"
        src = Path(ppc.__file__).read_text(encoding="utf-8")
        i_got = src.index("AS got_away,")
        i_bh = src.index("AS bat_home,", i_got)
        i_pc = src.index("AS pitcher_pitch_count,", i_bh)
        i_tto = src.index("AS times_through_order", i_pc)
        assert i_got < i_bh < i_pc < i_tto < src.index("FROM pg.raw.pitches", i_tto)


# ===========================================================================
# Migration 0023 + the schema file: appended LAST, in the builder's order
# ===========================================================================


class TestMigration0023:
    def test_the_migration_adds_the_three_columns_non_destructively(self):
        text = _MIGRATION.read_text(encoding="utf-8")
        for col in ("bat_home", "pitcher_pitch_count", "times_through_order"):
            assert f"ADD COLUMN IF NOT EXISTS {col}" in text
        assert "DROP" not in text.upper()

    def test_the_schema_file_lists_them_after_got_away_in_builder_order(self):
        text = _SCHEMA.read_text(encoding="utf-8")
        start = text.index("CREATE TABLE IF NOT EXISTS sim.pitch_pool")
        end = text.index("PRIMARY KEY (pitch_id)", start)
        block = text[start:end]
        i_ga = block.index("got_away")
        i_bh = block.index("bat_home", i_ga)
        i_pc = block.index("pitcher_pitch_count", i_bh)
        i_tto = block.index("times_through_order", i_pc)
        assert i_ga < i_bh < i_pc < i_tto


# ===========================================================================
# The artifact: export + load + the shared-memory seam
# ===========================================================================

_PITCH_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, pitcher_id INTEGER, batter_id INTEGER, season SMALLINT,
    stand VARCHAR, outcome_type VARCHAR, recency_weight FLOAT,
    velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT
    {extra}
);
"""

_OUTCOME_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.outcome_pool (
    pitch_id BIGINT, batter_id INTEGER, season SMALLINT, stand VARCHAR,
    exit_velo FLOAT, launch_angle FLOAT, pull_relative_spray_angle FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT,
    events VARCHAR, result_hits SMALLINT, result_outs SMALLINT,
    result_runs SMALLINT, recency_weight FLOAT,
    p_throws VARCHAR, venue_id INTEGER, fielded_by_position SMALLINT,
    fielder_player_id INTEGER
    {extra}
);
"""

_CONDITIONING_DDL = ", bat_home BOOLEAN, pitcher_pitch_count SMALLINT, times_through_order SMALLINT"
_PGEOM_DDL = (
    ", pitcher_id INTEGER, velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT, "
    "release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT"
)


def _seed(con: duckdb.DuckDBPyConnection, *, sim518: bool) -> None:
    con.execute(_PITCH_POOL_DDL.format(extra=_CONDITIONING_DDL if sim518 else ""))
    con.execute(_OUTCOME_POOL_DDL.format(extra=_PGEOM_DDL if sim518 else ""))
    for pid, stand in ((1, "L"), (2, "L"), (3, "R"), (4, "R")):
        cond = ", ?, ?, ?" if sim518 else ""
        con.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?, ?,'in_play',1.0, "
            "93.0,12.0,5.0,2200.0,180.0, 1.0,6.0,6.5,0.0,2.5, 0,0,0, 0,1,0" + cond + ")",
            [pid, 600000 + pid, 700000 + pid, _SEASON, stand]
            # bat_home: one row per hand NULL (unknown) to pin the -1 export.
            + ([None if pid % 2 else True, 40 + pid, 2] if sim518 else []),
        )
        pg = ", ?, 95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, ?" if sim518 else ""
        con.execute(
            "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, 98.0,12.0,10.0, 0,0,0, 0,1,0, "
            "'single',1,0, 0, 1.0, 'R', 15, 6, 500006" + pg + ")",
            [pid, 700000 + pid, _SEASON, stand]
            # plate_z NULL on the odd rows: the export must PRESERVE the NaN.
            + ([600000 + pid, None if pid % 2 else 2.5] if sim518 else []),
        )


def _export(tmp_path: Path, *, sim518: bool) -> str:
    con = duckdb.connect(":memory:")
    try:
        _seed(con, sim518=sim518)
        build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
        build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
    finally:
        con.close()
    return str(tmp_path)


class TestArtifactRoundTrip:
    def test_a_sim518_export_loads_the_new_columns(self, tmp_path):
        art = EngineArtifacts.load(_export(tmp_path, sim518=True))
        for hand in ("L", "R"):
            p = art.pools[hand]
            assert p.bat_home is not None and p.pitch_count is not None and p.tto is not None
            assert p.bat_home.dtype == np.int8 and p.pitch_count.dtype == np.int16
            assert set(p.bat_home.tolist()) <= {-1, 1}  # NULL -> -1, TRUE -> 1
            assert -1 in p.bat_home.tolist() and 1 in p.bat_home.tolist()
            assert sorted(p.pitch_count.tolist()) in ([41, 42], [43, 44])
            assert p.tto.tolist() == [2, 2]
            bb = art.bb_pools[hand]
            assert bb.pgeom is not None and bb.pgeom.shape == (2, 10)
            assert bb.pgeom.dtype == np.float32
            assert np.isnan(bb.pgeom[:, 9]).sum() == 1  # the NULL plate_z survived
            assert np.allclose(bb.pgeom[:, 0], 95.0)
            assert bb.pitcher_id is not None and bb.pitcher_id.dtype == np.int64
            assert all(600000 < v < 600010 for v in bb.pitcher_id.tolist())
        assert os.path.exists(os.path.join(str(tmp_path), "battedball_pool", "L.pgeom.npy"))

    def test_a_pre_sim518_export_still_loads_with_the_fields_none(self, tmp_path):
        art = EngineArtifacts.load(_export(tmp_path, sim518=False))
        for hand in ("L", "R"):
            p = art.pools[hand]
            assert p.bat_home is None and p.pitch_count is None and p.tto is None
            bb = art.bb_pools[hand]
            assert bb.pgeom is None and bb.pitcher_id is None
        assert not os.path.exists(os.path.join(str(tmp_path), "battedball_pool", "L.pgeom.npy"))

    def test_the_new_columns_ride_the_shared_memory_seam(self, tmp_path):
        art = EngineArtifacts.load(_export(tmp_path, sim518=True))
        shared = art.extract_shared_arrays()
        for hand in ("L", "R"):
            for attr in ("bat_home", "pitch_count", "tto"):
                assert f"pool.{hand}.{attr}" in shared
            for attr in ("pgeom", "pitcher_id"):
                assert f"bb_pool.{hand}.{attr}" in shared
        # Attach round-trips the same values onto a second bundle.
        other = EngineArtifacts.load(_export(tmp_path / "b", sim518=True))
        other.attach_shared_views(shared)
        assert other.pools["R"].pitch_count is shared["pool.R.pitch_count"]
        assert other.bb_pools["R"].pgeom is shared["bb_pool.R.pgeom"]
