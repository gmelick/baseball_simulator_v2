"""
tests/unit/test_sim523_fence_stage.py
=====================================
SIM-523 parts C2-C4 — the PARK GEOMETRY (SIM-478), the CARRY model (SIM-479)
and the FENCE STAGE (SIM-480) — the loop's step 5 (plan:
docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part C).

The bundle carries the fence line of every park per spray sector, as the
pool's own home runs and kept balls reveal it, plus a carry model fitted on
home runs. Before the fielding draw, the born ball's carry meets the live
park's fence at its direction: over it, a certain home run; short of it, no
home run; in the band, the draw decides. What these tests pin:

  * the builder writes the per-venue lines from the home-run and kept-ball
    quantiles, the league line where a sector lacks support, the hand-curated
    overrides on top, and the carry model with its home-run validation;
  * the loader reads the document (None on an older bundle);
  * ``fence_at`` (venue, league and missing), ``carry_of`` (the own distance,
    the model, None) and ``fence_decision`` (over / short / band / passed;
    a ground ball never leaves the park);
  * the stage keeps home-run rows only, drops them, or leaves the rows, and
    counts every decision; the fallback to the whole cell's home-run rows;
  * everything off is byte-identical; the loop passes the venue only when the
    stage is on; ``simulate_game`` carries the venue onto the state; the
    kwargs contract carries ``venue_id`` and the resolver writes it;
  * the factory env reads.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    BB_CLASS,
    PARK_N_SECTORS,
    BattedBallPool,
    EngineArtifacts,
    HandPool,
    build_park_geometry,
    carry_predict,
    park_sector,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half, Team
from simulation.production_factory import apply_fielding_env
from simulation.sim_kwargs import (
    SIM_KWARG_KEYS,
    resolve_park_factor_onto_state,
    sim_kwargs_from_state,
    venue_id_of_state,
)
from simulation.sim_loop import StateMachine, _venue_of, simulate_game

_SEASON = 2024
_PITCHER = "100:2024"
_BATTER = "200:2024"
_FLY = BB_CLASS["fly_ball"]
_LINE = BB_CLASS["line_drive"]
_GROUND = BB_CLASS["ground_ball"]
_FASTBALL = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)


# ===========================================================================
# The builder
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
    fielder_player_id INTEGER, bb_type VARCHAR, spray_angle FLOAT, hit_distance FLOAT
);
"""


def _seed_geometry(con: duckdb.DuckDBPyConnection) -> None:
    """Venue 1: a short left field (home runs from 340 ft, kept balls up to
    330 ft in sector 0) and a deep centre (400 / 390 in sector 4); every other
    sector is thin. Venue 2: only sector 4, home runs from 410 ft with no kept
    balls; the rest thin."""
    con.execute(_OUTCOME_POOL_DDL)
    pid = 0

    def add(venue, spray, events, dist, ev=100.0, la=28.0, n=1):
        nonlocal pid
        for _ in range(n):
            pid += 1
            hits = 4 if events == "home_run" else (0 if events == "field_out" else 2)
            con.execute(
                "INSERT INTO sim.outcome_pool VALUES (?,?,?,?, ?,?,?, 0,0,0, 0,1,0, "
                "?,?,?, 0, 1.0, 'R',?,8,500008, 'fly_ball', ?, ?)",
                [
                    pid,
                    700000 + pid,
                    _SEASON,
                    "R",
                    ev,
                    la,
                    0.0,
                    events,
                    hits,
                    0 if hits else 1,
                    venue,
                    spray,
                    dist,
                ],
            )

    # Every home run carries 5 ft per mph of exit velocity (distance = 5 ev - 150),
    # so the carry model has an exact answer to find.
    def hr(venue, spray, dist, la):
        add(venue, spray, "home_run", dist, ev=(dist + 150.0) / 5.0, la=la)

    # Venue 1, sector 0 (spray -45 .. -35): 30 home runs from 340 up, 30 kept balls up to 330.
    for i in range(30):
        hr(1, -40.0, 340.0 + i, la=26.0 + (i % 5))
        add(1, -40.0, "field_out", 300.0 + i)
    # Venue 1, sector 4 (spray -5 .. 5): home runs from 400, kept balls up to 390.
    for i in range(30):
        hr(1, 0.0, 400.0 + i, la=27.0 + (i % 5))
        add(1, 0.0, "double", 360.0 + i)
    # Venue 2, sector 4: home runs only, from 410.
    for i in range(30):
        hr(2, 0.0, 410.0 + i, la=28.0 + (i % 4))
    # A thin sector everywhere (3 home runs in sector 8 of venue 1): below support.
    hr(1, 40.0, 350.0, la=30.0)
    hr(1, 40.0, 350.0, la=30.0)
    hr(1, 40.0, 350.0, la=30.0)


class TestTheBuilder:
    def test_lines_come_from_the_quantiles_with_league_fallback(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed_geometry(con)
            doc = build_park_geometry(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        assert doc["n_sectors"] == PARK_N_SECTORS == 9
        v1, v2 = doc["venues"]["1"], doc["venues"]["2"]
        # Sector 0 of venue 1: (home-run 10th pct 342.9 + kept 99th pct 328.7) / 2.
        assert v1[0] == pytest.approx((342.9 + 328.71) / 2, abs=0.6)
        assert doc["source"]["1"][0] == "both"
        # Sector 4 of venue 1: both again; venue 2 has home runs only there.
        assert doc["source"]["1"][4] == "both" and doc["source"]["2"][4] == "hr"
        assert v2[4] == pytest.approx(412.9, abs=0.6)
        # The league line in sector 4 is the median of the two venues' evidence.
        assert doc["league"][4] is not None
        # A thin sector falls back to the league line; an empty league sector is None.
        assert doc["source"]["1"][8] == "league" and doc["support_hr"]["1"][8] == 3
        assert v1[8] is None and doc["league"][8] is None
        # Venue 2's sector 0 has nothing of its own: the league line (venue 1's).
        assert doc["source"]["2"][0] == "league" and v2[0] == v1[0]
        # The carry model fitted on the 93 home runs, validated on them.
        carry = doc["carry"]
        assert carry["n_hr"] == 93 and len(carry["coef"]) == 6
        assert carry["hr_mae_ft"] < 1.0
        assert carry_predict(carry["coef"], 110.0, 28.0) == pytest.approx(400.0, abs=2.0)
        with open(tmp_path / "park_geometry.json", encoding="utf-8") as fh:
            assert json.load(fh)["venues"]["1"] == v1

    def test_overrides_replace_sectors(self, tmp_path):
        with open(tmp_path / "park_geometry_overrides.json", "w", encoding="utf-8") as fh:
            json.dump({"1": {"0": 310.0}, "9": {"4": 405.0}}, fh)
        con = duckdb.connect(":memory:")
        try:
            _seed_geometry(con)
            doc = build_park_geometry(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        assert doc["venues"]["1"][0] == 310.0 and doc["source"]["1"][0] == "override"
        # A venue the pool never saw starts from the league line.
        assert doc["venues"]["9"][4] == 405.0 and doc["source"]["9"][4] == "override"
        assert doc["venues"]["9"][0] == doc["league"][0]

    def test_park_sector(self):
        assert park_sector(-45.0) == 0 and park_sector(-35.1) == 0
        assert park_sector(-35.0) == 1 and park_sector(0.0) == 4
        assert park_sector(44.9) == 8 and park_sector(60.0) == 8 and park_sector(-80.0) == 0


# ===========================================================================
# The loader
# ===========================================================================


def _minimal_pitch_pool(tmp_path) -> None:
    d = tmp_path / "pitch_pool"
    os.makedirs(d, exist_ok=True)
    with open(d / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"seasons": [2024], "counts": {"L": 0, "R": 0}}, fh)
    con = duckdb.connect(":memory:")
    for hand in ("L", "R"):
        np.save(d / f"{hand}.geom.npy", np.zeros((0, 3), dtype=np.float32))
        np.save(d / f"{hand}.sit.npy", np.zeros((0, 6), dtype=np.float32))
        con.execute(
            "COPY (SELECT 0::BIGINT AS pitch_id, 0::BIGINT AS pitcher_id, "
            "0::BIGINT AS batter_id, 0::BIGINT AS season, ''::VARCHAR AS outcome_type, "
            "1.0::FLOAT AS recency_weight WHERE 1=0) "
            f"TO '{(d / f'{hand}.meta.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()


class TestTheLoader:
    def test_load_reads_the_geometry_or_none(self, tmp_path):
        _minimal_pitch_pool(tmp_path)
        assert EngineArtifacts.load(str(tmp_path)).park_geometry is None
        con = duckdb.connect(":memory:")
        try:
            _seed_geometry(con)
            build_park_geometry(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        pg = EngineArtifacts.load(str(tmp_path)).park_geometry
        assert pg is not None and "1" in pg["venues"] and pg["carry"]["coef"]


# ===========================================================================
# The sampler: fence_at / carry_of / fence_decision and the stage
# ===========================================================================

_GEOMETRY = {
    "sector_deg": 10,
    "spray_min": -45.0,
    "spray_max": 45.0,
    "n_sectors": 9,
    "league": [340.0, 360.0, 380.0, 395.0, 400.0, 395.0, 380.0, 360.0, 340.0],
    "venues": {"7": [330.0, 350.0, 370.0, 390.0, 410.0, 390.0, 370.0, 350.0, None]},
    "carry": {"coef": [0.0, 4.0, 0.0, 0.0, 0.0, 0.0]},  # carry = 4 x exit velocity
}


def _bb_pool(events: list[str], *, cls: list[int] | None = None) -> BattedBallPool:
    n = len(events)
    rh = np.asarray(
        [4 if e == "home_run" else (1 if e == "single" else 0) for e in events], dtype=np.int8
    )
    return BattedBallPool(
        geom=np.zeros((n, 3), dtype=np.float32) + np.array([95.0, 30.0, 0.0], dtype=np.float32),
        sit=np.zeros((n, 6), dtype=np.float32),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        event=np.asarray(events, dtype=object),
        result_hits=rh,
        result_outs=(rh == 0).astype(np.int8),
        recency=np.ones(n, dtype=np.float32),
        r1_dest=np.full(n, -1, dtype=np.int8),
        r2_dest=np.full(n, -1, dtype=np.int8),
        r3_dest=np.full(n, -1, dtype=np.int8),
        batter_dest=rh.copy(),
        dest_ok=np.ones(n, dtype=np.int8),
        r1_adv_out=np.zeros(n, dtype=np.int8),
        r2_adv_out=np.zeros(n, dtype=np.int8),
        r3_adv_out=np.zeros(n, dtype=np.int8),
        is_air=np.ones(n, dtype=np.int8),
        spray_raw=np.zeros(n, dtype=np.float32),
        hit_dist=np.where(rh == 4, 410.0, 330.0).astype(np.float32),
        bb_class=(np.asarray(cls, dtype=np.int8) if cls is not None else None),
    )


def _sampler(
    bb: BattedBallPool, *, geometry: dict | None = _GEOMETRY, seed: int = 0
) -> FullPoolSampler:
    art = EngineArtifacts(pools={}, bb_pools={"R": bb}, park_geometry=geometry)
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    assert fp.fence_margin == 0.0  # the production default: a decisive fence
    fp.fence_margin = 10.0  # these tests exercise the band
    return fp


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


_MIXED = ["home_run"] * 3 + ["double"] * 3 + ["field_out"] * 3


class TestTheGeometryReads:
    def test_fence_at(self):
        fp = _sampler(_bb_pool(_MIXED))
        assert fp.fence_at(7, -40.0) == 330.0  # the venue's own sector 0
        assert fp.fence_at(7, 0.0) == 410.0
        assert fp.fence_at(7, 44.0) == 340.0  # the venue's sector 8 is None -> league
        assert fp.fence_at(99, 0.0) == 400.0  # an unknown venue -> league
        assert fp.fence_at(None, 0.0) == 400.0
        assert fp.fence_at(7, None) is None and fp.fence_at(7, float("nan")) is None
        assert _sampler(_bb_pool(_MIXED), geometry=None).fence_at(7, 0.0) is None

    def test_carry_of(self):
        fp = _sampler(_bb_pool(_MIXED))
        assert fp.carry_of({"dist": 388.0, "ev": 100.0, "la": 28.0}) == 388.0
        assert fp.carry_of({"dist": None, "ev": 100.0, "la": 28.0}) == pytest.approx(400.0)
        assert fp.carry_of({"dist": 0.0, "ev": 100.0, "la": 28.0}) == pytest.approx(400.0)
        assert fp.carry_of({"dist": None, "ev": None, "la": 28.0}) is None
        assert (
            _sampler(_bb_pool(_MIXED), geometry=None).carry_of(
                {"dist": None, "ev": 100.0, "la": 28.0}
            )
            is None
        )

    def test_fence_decision(self):
        fp = _sampler(_bb_pool(_MIXED))
        over = {"cls": _FLY, "spray_raw": 0.0, "dist": 425.0}
        short = {"cls": _FLY, "spray_raw": 0.0, "dist": 395.0}
        band = {"cls": _LINE, "spray_raw": 0.0, "dist": 405.0}
        assert fp.fence_decision(over, 7) == 0
        assert fp.fence_decision(short, 7) == 1
        assert fp.fence_decision(band, 7) == 2
        # The same ball at the short left-field line is over.
        assert fp.fence_decision({"cls": _FLY, "spray_raw": -40.0, "dist": 345.0}, 7) == 0
        # A ground ball never leaves the park; an unknown class passes.
        assert fp.fence_decision({"cls": _GROUND, "spray_raw": 0.0, "dist": 425.0}, 7) == 1
        assert fp.fence_decision({"cls": None, "spray_raw": 0.0, "dist": 425.0}, 7) == 3
        # No venue, no direction, no carry: the stage passes.
        assert fp.fence_decision(over, None) == 3
        assert fp.fence_decision({"cls": _FLY, "spray_raw": None, "dist": 425.0}, 7) == 3
        assert fp.fence_decision({"cls": _FLY, "spray_raw": 0.0, "dist": None, "ev": None}, 7) == 3
        fp.fence_margin = 30.0
        assert fp.fence_decision(over, 7) == 2  # inside a wider band


class TestTheStage:
    def test_over_keeps_home_run_rows_only(self):
        fp = _sampler(_bb_pool(_MIXED))
        fp.fence_stage = True
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 430.0}
        assert _draws(fp, born_bb=born, venue_id=7) == {"home_run"}
        assert fp.fence_counts.tolist() == [40, 0, 0, 0, 0]

    def test_short_drops_home_run_rows(self):
        fp = _sampler(_bb_pool(_MIXED))
        fp.fence_stage = True
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 380.0}
        assert _draws(fp, born_bb=born, venue_id=7) == {"double", "field_out"}
        assert fp.fence_counts.tolist() == [0, 40, 0, 0, 0]
        # A ground ball: the same, without any geometry read.
        assert _draws(fp, born_bb={"cls": _GROUND, "dist": 100.0}, venue_id=None) == {
            "double",
            "field_out",
        }
        assert fp.fence_counts.tolist() == [0, 80, 0, 0, 0]

    def test_band_and_passed_leave_the_rows(self):
        fp = _sampler(_bb_pool(_MIXED))
        fp.fence_stage = True
        assert _draws(
            fp, born_bb={"cls": _FLY, "spray_raw": 0.0, "dist": 405.0}, venue_id=7
        ) == set(_MIXED)
        assert _draws(
            fp, born_bb={"cls": _FLY, "spray_raw": 0.0, "dist": 430.0}, venue_id=None
        ) == set(_MIXED)
        assert fp.fence_counts.tolist() == [0, 0, 40, 40, 0]

    def test_over_falls_back_to_the_cells_home_runs_past_the_class_filter(self):
        # Every home run in the cell is a line drive; the born ball is a fly
        # ball over the fence. The class filter leaves no home run, so the
        # stage takes the cell's home-run rows regardless of class.
        bb = _bb_pool(_MIXED, cls=[_LINE] * 3 + [_FLY] * 6)
        fp = _sampler(bb)
        fp.fence_stage = True
        fp.bb_class_filter = True
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 430.0}
        assert _draws(fp, born_bb=born, venue_id=7) == {"home_run"}

    def test_a_decision_with_no_matching_rows_is_counted_and_left(self):
        fp = _sampler(_bb_pool(["double"] * 4))
        fp.fence_stage = True
        assert _draws(fp, born_bb={"cls": _FLY, "spray_raw": 0.0, "dist": 430.0}, venue_id=7) == {
            "double"
        }
        assert fp.fence_counts.tolist() == [40, 0, 0, 0, 40]

    def test_off_is_byte_identical(self):
        born = {"cls": _FLY, "spray_raw": 0.0, "dist": 430.0}
        a = _seq(_sampler(_bb_pool(_MIXED), seed=4), born_bb=born, venue_id=7)
        b = _seq(_sampler(_bb_pool(_MIXED), seed=4))
        assert a == b


# ===========================================================================
# The loop, simulate_game and the kwargs contract
# ===========================================================================


class _Recorder(FullPoolSampler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.bb_kwargs: list[dict] = []

    def battedball_new_pa(self, *a, **kw) -> None:
        self.bb_kwargs.append({k: v for k, v in kw.items() if k in ("born_bb", "venue_id")})
        super().battedball_new_pa(*a, **kw)


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


def _state(park: str | None = "7") -> GameState:
    state = GameState(
        pitcher_id=100,
        bat_hand="R",
        season=_SEASON,
        away_lineup=[200, 201, 202],
        home_lineup=[200, 201, 202],
    )
    state.batter_id = 200
    state.park = park
    assert state.half is Half.TOP and state.offense is Team.AWAY
    return state


class TestLoopAndContract:
    def test_the_loop_passes_the_venue_only_when_the_stage_is_on(self):
        bb = _bb_pool(_MIXED, cls=[_FLY] * 9)
        pool = _pitch_pool(list(range(9)))
        art = EngineArtifacts(
            pools={"R": pool, "L": pool},
            pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
            pitcher_sim_index={_PITCHER: 0},
            bb_pools={"R": bb, "L": bb},
            park_geometry=_GEOMETRY,
        )
        fp = _Recorder(art, np.random.default_rng(7))
        fp.pitch_result_split = True
        machine = StateMachine(fp, rng=np.random.default_rng(7))
        state = _state("7")
        assert machine._full_pool_outcome(state) == "in_play"
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1] == {}
        fp.fence_stage = True
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1]["venue_id"] == 7 and "born_bb" in fp.bb_kwargs[-1]
        state.park = "not a venue"
        machine._full_pool_fielding(state)
        assert fp.bb_kwargs[-1]["venue_id"] is None

    def test_venue_of(self):
        assert _venue_of(SimpleNamespace(park="7")) == 7
        assert _venue_of(SimpleNamespace(park=" 12 ")) == 12
        assert _venue_of(SimpleNamespace(park=None)) is None
        assert _venue_of(SimpleNamespace(park="Fenway")) is None
        assert _venue_of(SimpleNamespace()) is None

    def test_the_kwargs_contract_carries_the_venue(self):
        state = _state("7")
        state.park_run_factor = 1.0
        kw = sim_kwargs_from_state(state)
        assert set(kw) == SIM_KWARG_KEYS and kw["venue_id"] == 7
        assert venue_id_of_state(_state(None)) is None
        assert sim_kwargs_from_state(_state(None) if False else _state("x"))["venue_id"] is None

    def test_the_resolver_writes_the_venue_onto_the_state(self):
        class _Pool:
            async def fetchrow(self, sql, game_pk):
                return {"venue_id": 3313} if "venue_id" in sql else None

        state = _state(None)
        asyncio.run(resolve_park_factor_onto_state(state, _Pool(), None, 1, _SEASON))
        assert state.park == "3313"
        # A state that already knows its venue keeps it.
        state2 = _state("7")
        asyncio.run(resolve_park_factor_onto_state(state2, _Pool(), None, 1, _SEASON))
        assert state2.park == "7"

    def test_simulate_game_carries_the_venue_onto_the_state(self):
        from simulation.synthetic_bundle import synthetic_sampler

        fp = synthetic_sampler()
        machine = StateMachine(fp, rng=np.random.default_rng(1))
        seen: list = []

        orig = machine._full_pool_fielding

        def spy(state):
            seen.append(_venue_of(state))
            return orig(state)

        machine._full_pool_fielding = spy  # type: ignore[method-assign]
        simulate_game(
            state_machine=machine,
            seed=1,
            away_lineup=[1, 2, 3, 4, 5, 6, 7, 8, 9],
            home_lineup=[11, 12, 13, 14, 15, 16, 17, 18, 19],
            pitcher_id=100,
            venue_id=2392,
            max_innings=9,
        )
        assert seen and set(seen) == {2392}


# ===========================================================================
# The factory env reads
# ===========================================================================


class TestTheFactoryEnv:
    def test_fence_reads(self):
        s = SimpleNamespace()
        apply_fielding_env(s, env={})
        assert s.fence_stage is False and s.fence_margin == 0.0
        apply_fielding_env(s, env={"SIM_FENCE_STAGE": "1", "SIM_FENCE_MARGIN": "15"})
        assert s.fence_stage is True and s.fence_margin == 15.0
        assert os.environ.get("SIM_FENCE_STAGE") == "0"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
