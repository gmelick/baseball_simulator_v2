"""
tests/unit/test_sim523_fielding_split.py
========================================
SIM-523 part C1 — the FIELDING draw's filters and weights (plan:
docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part C; the loop's
step 6).

The fielding draw filters on the base-out cell, the batter hand and — new —
the born ball's batted-ball CLASS, and weights by the born ball's similarity,
the fielder at the ball's position, the park (only for a ball in the wall
zone), the batter's profile (sprint speed included) and recency. What these
tests pin:

  * the artifact exports the class from ``bb_type`` and the loader reads it
    (shared-view included); an older bundle reads None and the filter is off;
  * the class filter keeps the cell's rows of the born ball's class, falls back
    to the whole cell when that class is empty there, and counts both;
  * the park kernel applies only to a wall-zone ball under the zone rule;
  * every switch off is byte-identical;
  * the loop passes the born ball whenever any consumer is on;
  * the factory env reads.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    BB_CLASS,
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    build_battedball_pool_artifact,
    build_pitch_pool_artifact,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half, Team
from simulation.production_factory import apply_fielding_env
from simulation.sim_loop import StateMachine

_SEASON = 2024
_PITCHER = "100:2024"
_BATTER = "200:2024"
_FASTBALL = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)


def _bb_pool(
    events: list[str],
    *,
    cls: list[int] | None = None,
    dist: list[float] | None = None,
    batters: list[int] | None = None,
    venues: list[int] | None = None,
) -> BattedBallPool:
    """Transition-carrying rows in the (empty, 0 outs) cell whose EVENT reveals
    which rows a filter or weight favored."""
    n = len(events)
    rh = np.asarray([1 if e == "single" else 0 for e in events], dtype=np.int8)
    is_air = np.asarray([0 if e == "single" else 1 for e in events], dtype=np.int8)
    return BattedBallPool(
        geom=np.column_stack(
            [
                np.full(n, 95.0, dtype=np.float32),
                np.where(is_air == 1, 40.0, 5.0).astype(np.float32),
                np.zeros(n, dtype=np.float32),
            ]
        ),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.asarray(batters if batters is not None else [200] * n, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(events, dtype=object),
        result_hits=rh,
        result_outs=(1 - rh).astype(np.int8),
        recency=np.ones(n, dtype=np.float32),
        venue_id=(np.asarray(venues, dtype=np.int64) if venues is not None else None),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=rh.copy(),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=is_air,
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=(
            np.asarray(dist, dtype=np.float32)
            if dist is not None
            else np.where(is_air == 1, 320.0, 120.0).astype(np.float32)
        ),
        bb_class=(np.asarray(cls, dtype=np.int8) if cls is not None else None),
    )


def _pitch_pool(bb_row: list[int]) -> HandPool:
    n = len(bb_row)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    return HandPool(
        geom=np.stack([_FASTBALL] * n).astype(np.float32),
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(["in_play"] * n, dtype=object),
        recency=np.ones(n, dtype=np.float32),
        bb_row=np.asarray(bb_row, dtype=np.int32),
    )


def _runner_emb(speeds: dict[str, float]) -> dict:
    keys = list(speeds)
    vecs = np.array([[speeds[k], 0.1] for k in keys], dtype=np.float32)
    return {
        "key_index": {k: i for i, k in enumerate(keys)},
        "vecs": vecs,
        "mean": vecs.mean(axis=0),
        "std": vecs.std(axis=0) + 1e-6,
        "features": ["sprint_speed", "sb_attempt_rate"],
    }


def _sampler(
    bb: BattedBallPool, *, actor_emb: dict | None = None, seed: int = 0
) -> FullPoolSampler:
    art = EngineArtifacts(pools={}, bb_pools={"R": bb}, actor_emb=actor_emb or {})
    return FullPoolSampler(art, np.random.default_rng(seed))


def _draws(fp: FullPoolSampler, n: int = 40, **kw) -> set[str]:
    out = set()
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        out.add(fp.battedball_draw()[0])
    return out


def _seq(fp: FullPoolSampler, n: int = 30, **kw) -> list[str]:
    out = []
    for _ in range(n):
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), **kw)
        out.append(fp.battedball_draw()[0])
    return out


_FLY = BB_CLASS["fly_ball"]
_GROUND = BB_CLASS["ground_ball"]
_LINE = BB_CLASS["line_drive"]


# ===========================================================================
# The class filter
# ===========================================================================


class TestTheClassFilter:
    def test_the_born_balls_class_is_a_hard_filter(self):
        # Four fly-ball outs, four ground-ball singles.
        bb = _bb_pool(["field_out"] * 4 + ["single"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        fp = _sampler(bb)
        assert _draws(fp) == {"field_out", "single"}
        fp.bb_class_filter = True
        assert _draws(fp, born_bb={"cls": _FLY}) == {"field_out"}
        assert _draws(fp, born_bb={"cls": _GROUND}) == {"single"}
        assert fp.bb_class_counts.tolist() == [80, 0]

    def test_an_empty_class_falls_back_to_the_cell_and_is_counted(self):
        bb = _bb_pool(["field_out"] * 4 + ["single"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        fp = _sampler(bb)
        fp.bb_class_filter = True
        assert _draws(fp, born_bb={"cls": _LINE}) == {"field_out", "single"}
        assert fp.bb_class_counts.tolist() == [0, 40]

    def test_unknown_class_or_no_class_column_leaves_the_cell_whole(self):
        bb = _bb_pool(["field_out"] * 4 + ["single"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        fp = _sampler(bb)
        fp.bb_class_filter = True
        assert _draws(fp, born_bb={"cls": None}) == {"field_out", "single"}
        assert _draws(fp, born_bb={"cls": 0}) == {"field_out", "single"}
        assert _draws(fp, born_bb={}) == {"field_out", "single"}
        no_col = _sampler(_bb_pool(["field_out"] * 4 + ["single"] * 4))
        no_col.bb_class_filter = True
        assert _draws(no_col, born_bb={"cls": _FLY}) == {"field_out", "single"}
        assert fp.bb_class_counts.tolist() == [0, 0] and no_col.bb_class_counts.tolist() == [0, 0]

    def test_off_is_byte_identical_with_a_born_ball(self):
        bb = _bb_pool(["field_out"] * 4 + ["single"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        assert _seq(_sampler(bb, seed=3), born_bb={"cls": _FLY}) == _seq(_sampler(bb, seed=3))

    def test_the_class_rows_are_cached_per_cell_and_class(self):
        bb = _bb_pool(["field_out"] * 4 + ["single"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        fp = _sampler(bb)
        fp.bb_class_filter = True
        _draws(fp, n=3, born_bb={"cls": _FLY})
        _draws(fp, n=3, born_bb={"cls": _GROUND})
        cache = fp._transition_meta("R")["class_cells"]
        assert set(cache) == {((0, 0), _FLY), ((0, 0), _GROUND)}
        assert cache[((0, 0), _FLY)].tolist() == [0, 1, 2, 3]


# ===========================================================================
# The zero-weight fallback
# ===========================================================================


class TestTheZeroWeightFallback:
    def test_underflowed_weights_fall_back_to_uniform_over_the_filtered_rows(self):
        # A park kernel this tight sends every row's weight to exactly 0 in
        # float32 (the rows' parks are far from the live one); the draw must
        # still return one of the filtered rows, with its transition.
        bb = _bb_pool(["single"] * 2 + ["field_out"] * 2, cls=[_FLY] * 4, venues=[1] * 4)
        fp = _sampler(bb)
        fp.venue_run_factors = {(1, _SEASON): 0.5}
        fp.park_sigma = 0.001
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), park_run_factor=1.5)
        assert fp.bb_zero_weight_count == 1
        assert fp._bb_cdf is not None and fp._bb_cdf[-1] == 4.0
        assert _draws(fp, park_run_factor=1.5) == {"single", "field_out"}
        assert fp.last_transition() is not None
        # A healthy weight vector is untouched.
        fp.park_sigma = 0.0
        fp.bb_zero_weight_count = 0
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32))
        assert fp.bb_zero_weight_count == 0


# ===========================================================================
# The born kernel's density correction
# ===========================================================================


def _gradient_bb_pool(n: int = 4000) -> BattedBallPool:
    """A pool whose only varying feature is exit velocity, dense at the soft
    end and sparse at the hard end (80 + a half-normal x 10); a ball hit over
    95 mph is a single (~13% of the rows), the rest are outs."""
    rng = np.random.default_rng(2)
    ev = (80.0 + np.abs(rng.normal(size=n)) * 10.0).astype(np.float32)
    events = ["single" if v > 95.0 else "field_out" for v in ev]
    bb = _bb_pool(events)
    bb.geom[:, 0] = ev
    bb.geom[:, 1] = 12.0
    bb.hit_dist[:] = 200.0
    return bb


def _hit_share(fp: FullPoolSampler, bb: BattedBallPool, n: int = 4000) -> float:
    """Born balls drawn uniformly from the pool's own rows (the truth), one
    fielding draw each; the share of drawn plays that are hits."""
    rng = np.random.default_rng(9)
    hits = 0
    for j in rng.integers(0, bb.n, size=n):
        born = {
            "ev": float(bb.geom[j, 0]),
            "la": float(bb.geom[j, 1]),
            "spray": float(bb.geom[j, 2]),
            "dist": float(bb.hit_dist[j]),
        }
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), born_bb=born)
        hits += fp.battedball_draw()[1] > 0
    return hits / n


class TestTheBornDensityCorrection:
    def test_the_raw_kernel_under_draws_the_sparse_side_and_the_correction_undoes_it(self):
        # The pool's hit share is 0.135; the raw kernel leans toward the dense
        # soft-contact side and under-draws hits by ~0.03 at this bandwidth;
        # the corrected kernel lands within 0.02 (a share over 4,000 draws has
        # a standard error of ~0.005; the toy's correction overshoots a little,
        # which is why the power stays a fit target).
        bb = _gradient_bb_pool()
        truth = float((bb.result_hits > 0).mean())
        raw = _sampler(bb)
        raw.bb_born_sigma = 0.5
        raw.bb_born_density_power = 0.0
        corrected = _sampler(bb)
        corrected.bb_born_sigma = 0.5
        assert corrected.bb_born_density_power == 1.0
        raw_share, corrected_share = _hit_share(raw, bb), _hit_share(corrected, bb)
        assert truth - raw_share > 0.02
        assert abs(corrected_share - truth) < 0.02

    def test_the_density_is_cached_per_candidate_set(self):
        bb = _bb_pool(["single"] * 4 + ["field_out"] * 4, cls=[_FLY] * 4 + [_GROUND] * 4)
        fp = _sampler(bb)
        fp.bb_born_sigma = 0.5
        born = {"ev": 95.0, "la": 40.0, "spray": 0.0, "dist": 320.0, "cls": _FLY}
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), born_bb=born)
        cache = fp._transition_meta("R")["born_density"]
        assert len(cache) == 1
        # The four hits and the four outs form two feature clusters of equal
        # size: every row sees the same density, so every inverse density is 1.
        (inv,) = cache.values()
        np.testing.assert_allclose(inv, np.ones(8), rtol=1e-5)
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), born_bb=born)
        assert len(cache) == 1
        fp.bb_class_filter = True  # a different candidate set: a new entry
        fp.battedball_new_pa("R", _BATTER, np.zeros(6, np.float32), born_bb=born)
        assert len(cache) == 2

    def test_power_zero_is_the_raw_kernel(self):
        bb = _bb_pool(["single"] * 4 + ["field_out"] * 4)
        a = _sampler(bb, seed=3)
        a.bb_born_sigma, a.bb_born_density_power = 0.5, 0.0
        b = _sampler(bb, seed=3)
        b.bb_born_sigma, b.bb_born_density_power = 0.5, 1.0
        born = {"ev": 95.0, "la": 40.0, "spray": 0.0, "dist": 320.0}
        # Uniform features: the correction is exactly neutral, so both agree.
        assert _seq(a, born_bb=born) == _seq(b, born_bb=born)


# ===========================================================================
# The park wall-zone rule
# ===========================================================================


class TestTheWallZoneRule:
    def _park_sampler(self) -> FullPoolSampler:
        # Rows 0-3 hit in a hitter's park (factor 1.2), rows 4-7 in a pitcher's
        # park (0.8); the live park is the hitter's park.
        bb = _bb_pool(
            ["single"] * 4 + ["field_out"] * 4,
            cls=[_FLY] * 8,
            dist=[350.0] * 8,
            venues=[1] * 4 + [2] * 4,
        )
        fp = _sampler(bb)
        fp.venue_run_factors = {(1, _SEASON): 1.2, (2, _SEASON): 0.8}
        fp.park_sigma = 0.02
        return fp

    def test_in_wall_zone(self):
        fp = _sampler(_bb_pool(["single"]))
        assert fp.in_wall_zone({"cls": _FLY, "dist": 320.0})
        assert fp.in_wall_zone({"cls": _LINE, "dist": 300.0})
        assert not fp.in_wall_zone({"cls": _FLY, "dist": 250.0})
        assert not fp.in_wall_zone({"cls": _GROUND, "dist": 400.0})
        assert not fp.in_wall_zone({"cls": None, "dist": 400.0})
        assert not fp.in_wall_zone({"cls": _FLY, "dist": None})
        fp.wall_zone_distance = 340.0
        assert not fp.in_wall_zone({"cls": _FLY, "dist": 320.0})

    def test_the_park_kernel_applies_only_to_a_wall_zone_ball_under_the_rule(self):
        fp = self._park_sampler()
        kw = {"park_run_factor": 1.2}
        # Rule off: the park kernel applies to every ball (as today).
        assert _draws(fp, born_bb={"cls": _GROUND, "dist": 100.0}, **kw) == {"single"}
        fp.park_wall_zone_only = True
        # A ground ball: no park weight, both parks' plays draw.
        assert _draws(fp, born_bb={"cls": _GROUND, "dist": 100.0}, **kw) == {"single", "field_out"}
        # A deep fly ball: the park weight bites.
        assert _draws(fp, born_bb={"cls": _FLY, "dist": 360.0}, **kw) == {"single"}
        # No born ball at all (the split off): as today.
        assert _draws(fp, **kw) == {"single"}


# ===========================================================================
# The loop wiring
# ===========================================================================


class _Recorder(FullPoolSampler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.bb_kwargs: list[dict] = []

    def battedball_new_pa(self, *a, **kw) -> None:
        self.bb_kwargs.append({k: v for k, v in kw.items() if k == "born_bb"})
        super().battedball_new_pa(*a, **kw)


def _state() -> GameState:
    state = GameState(
        pitcher_id=100,
        bat_hand="R",
        season=_SEASON,
        away_lineup=[200, 201, 202],
        home_lineup=[200, 201, 202],
    )
    state.batter_id = 200
    assert state.half is Half.TOP and state.offense is Team.AWAY
    return state


class TestLoopWiring:
    @pytest.mark.parametrize(
        "attr, value", [("bb_class_filter", True), ("park_wall_zone_only", True)]
    )
    def test_each_consumer_alone_makes_the_loop_pass_the_born_ball(self, attr, value):
        bb = _bb_pool(["field_out"] * 4, cls=[_FLY] * 4)
        pool = _pitch_pool([0, 1, 2, 3])
        art = EngineArtifacts(
            pools={"R": pool, "L": pool},
            pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
            pitcher_sim_index={_PITCHER: 0},
            bb_pools={"R": bb, "L": bb},
        )
        fp = _Recorder(art, np.random.default_rng(7))
        fp.pitch_result_split = True
        machine = StateMachine(fp, rng=np.random.default_rng(7))
        state = _state()
        assert machine._full_pool_outcome(state) == "in_play"
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1] == {}
        setattr(fp, attr, value)
        machine._full_pool_fielding(state)
        born = fp.bb_kwargs[-1]["born_bb"]
        assert born["cls"] == _FLY and born["row"] == fp.last_result_row()


# ===========================================================================
# The artifact export + loader
# ===========================================================================

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
    fielder_player_id INTEGER, bb_type VARCHAR
);
"""

_PITCH_POOL_DDL = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, pitcher_id INTEGER, batter_id INTEGER, season SMALLINT,
    stand VARCHAR, outcome_type VARCHAR, recency_weight FLOAT,
    velo FLOAT, ivb FLOAT, hb FLOAT, spin_rate FLOAT, spin_axis FLOAT,
    release_x FLOAT, release_z FLOAT, release_ext FLOAT, plate_x FLOAT, plate_z FLOAT,
    count_balls SMALLINT, count_strikes SMALLINT, outs SMALLINT,
    runners_state SMALLINT, inning SMALLINT, score_diff SMALLINT
);
"""


def _seed(con: duckdb.DuckDBPyConnection, *, with_type: bool = True) -> None:
    con.execute(
        _OUTCOME_POOL_DDL if with_type else _OUTCOME_POOL_DDL.replace(", bb_type VARCHAR", "")
    )
    con.execute(_PITCH_POOL_DDL)
    rows = [
        (11, "R", "fly_ball"),
        (12, "R", "ground_ball"),
        (13, "R", "bunt_popup"),
        (14, "R", None),
        (21, "L", "line_drive"),
    ]
    for pid, stand, bt in rows:
        con.execute(
            "INSERT INTO sim.pitch_pool VALUES (?,?,?,?, ?,'in_play',1.0, "
            "93.0,12.0,5.0,2200.0,180.0, 1.0,6.0,6.5,0.0,2.5, 0,0,0, 0,1,0)",
            [pid, 600000 + pid, 700000 + pid, _SEASON, stand],
        )
        vals = [pid, 700000 + pid, _SEASON, stand]
        if with_type:
            con.execute(
                "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, 98.0,12.0,10.0, 0,0,0, 0,1,0, "
                "'single',1,0, 0, 1.0, 'R',15,6,500006, ?)",
                vals + [bt],
            )
        else:
            con.execute(
                "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, 98.0,12.0,10.0, 0,0,0, 0,1,0, "
                "'single',1,0, 0, 1.0, 'R',15,6,500006)",
                vals,
            )


class TestTheArtifactClass:
    def test_export_writes_the_class_and_load_reads_it(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        art = EngineArtifacts.load(str(tmp_path))
        bb = art.bb_pools["R"]
        assert bb.bb_class is not None and bb.bb_class.dtype == np.int8
        by_batter = {int(b): int(c) for b, c in zip(bb.batter_id, bb.bb_class, strict=True)}
        assert by_batter == {
            700011: _FLY,
            700012: _GROUND,
            700013: BB_CLASS["bunt_popup"],
            700014: 0,
        }
        assert int(art.bb_pools["L"].bb_class[0]) == _LINE
        shared = art.extract_shared_arrays()
        assert shared["bb_pool.R.bb_class"] is bb.bb_class
        view = np.zeros(bb.n, dtype=np.int8)
        art.attach_shared_views({"bb_pool.R.bb_class": view})
        assert bb.bb_class is view

    def test_a_pool_without_a_type_column_loads_none(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con, with_type=False)
            build_pitch_pool_artifact(con, str(tmp_path), [_SEASON])
            build_battedball_pool_artifact(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        art = EngineArtifacts.load(str(tmp_path))
        assert art.bb_pools["R"].bb_class is None


# ===========================================================================
# The factory env reads
# ===========================================================================


class TestTheFactoryEnv:
    def test_defaults(self):
        s = SimpleNamespace()
        apply_fielding_env(s, env={})
        assert s.bb_class_filter is False and s.park_wall_zone_only is False
        assert s.wall_zone_distance == 300.0

    def test_values_and_junk(self):
        s = SimpleNamespace()
        apply_fielding_env(
            s,
            env={
                "SIM_BB_CLASS_FILTER": "1",
                "SIM_PARK_WALL_ZONE_ONLY": "yes",
                "SIM_WALL_ZONE_DISTANCE": "junk",
            },
        )
        assert s.bb_class_filter is True and s.park_wall_zone_only is True
        assert s.wall_zone_distance == 300.0

    def test_the_unit_suite_pins_everything_off(self):
        assert os.environ.get("SIM_BB_CLASS_FILTER") == "0"
        assert os.environ.get("SIM_PARK_WALL_ZONE_ONLY") == "0"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
