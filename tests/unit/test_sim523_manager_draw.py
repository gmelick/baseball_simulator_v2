"""
tests/unit/test_sim523_manager_draw.py
======================================
SIM-523 part D — the manager decisions at the START of a plate appearance,
the pitching change as a DRAW from an opportunity pool (plan:
docs/audit/2026-09-08-sim523-play-picker-redesign-plan.md, part D; the loop's
step 1).

What these tests pin:

  * the builder turns the situation table + the pitch pool into one row per
    plate-appearance boundary with the right pitcher, pitch count, batters
    faced, starter flag, half-inning flag, fielding-side score margin and
    change label; the manifest carries the pool's own rates;
  * the loader reads the pool (shared-view included); an older bundle has none;
  * the draw: the hard cell, the widening ladder and its counts, the soft
    kernel, the live pitcher's similarity weight, the drawn row's change flag,
    None without a pool;
  * the ORDER: the manager decisions run once, on a plate appearance's first
    pitch, before the intentional-walk and steal decisions — never at the end
    of the previous plate appearance nor at the half-inning roll — and the
    half's plate-appearance count and the starters are tracked;
  * the switch: with the draw on, the pull follows the drawn row (no floor, no
    ceiling); without a pool the formula stays; with the manager off nothing
    runs;
  * the factory env reads.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from pipeline.batch.engine_artifacts import (
    ChangePool,
    EngineArtifacts,
    build_pitching_change_pool,
)
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import GameState, Half, Team
from simulation.production_factory import _manager_draw_enabled, apply_manager_env
from simulation.sim_loop import StateMachine
from simulation.synthetic_bundle import league_artifacts

_SEASON = 2024

# ===========================================================================
# The builder
# ===========================================================================

_DDL = """
CREATE SCHEMA IF NOT EXISTS derived;
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE derived.at_bat_situations (
    play_id VARCHAR, game_pk INTEGER, season SMALLINT, venue_id INTEGER, at_bat_number INTEGER,
    inning SMALLINT, top_or_bottom SMALLINT, outs_when_up SMALLINT,
    on_1b INTEGER, on_2b INTEGER, on_3b INTEGER, home_score SMALLINT, away_score SMALLINT,
    leverage_index FLOAT, pitcher_pitch_count INTEGER, batter_pa_count INTEGER
);
CREATE TABLE sim.pitch_pool (
    pitch_id BIGINT, game_pk INTEGER, at_bat_number INTEGER, pitch_number SMALLINT,
    season SMALLINT, pitcher_id INTEGER, batter_id INTEGER, recency_weight FLOAT
);
"""


def _seed(con: duckdb.DuckDBPyConnection) -> None:
    """One game. Top halves: pitcher 100 (home starter) throws innings 1-2, 3
    pitches per plate appearance; pitcher 101 relieves him mid-way through the
    3rd (runner on first, 1 out, home up 2-0). Bottom halves: pitcher 200 (the
    away starter) throws everything, 4 pitches per plate appearance."""
    con.execute(_DDL)
    ab = 0
    pid = 0
    rows = []  # (ab, inning, top_or_bottom, outs, on1, home, away, pitcher, n_pitches)
    # inning 1 top: 3 PAs by pitcher 100; bottom: 3 by 200
    for inning in (1, 2, 3):
        for top in (0, 1):
            for k in range(3):
                pitcher = 100 if top == 0 else 200
                on1 = 0
                if inning == 3 and top == 0 and k >= 1:
                    pitcher = 101  # the reliever enters mid-inning and stays
                    on1 = 555 if k == 1 else 0
                home = 2 if inning >= 3 else 0
                ab += 1
                rows.append(
                    (
                        ab,
                        inning,
                        top,
                        k if k < 3 else 2,
                        on1,
                        home,
                        0,
                        pitcher,
                        3 if top == 0 else 4,
                    )
                )
    for ab_no, inning, top, outs, on1, home, away, pitcher, n_p in rows:
        con.execute(
            "INSERT INTO derived.at_bat_situations VALUES (?, 1, ?, 1, ?, ?, ?, ?, ?, 0, 0, ?, ?, 1.0, 0, 1)",
            [f"1-{ab_no}", _SEASON, ab_no, inning, top, outs, on1, home, away],
        )
        for p in range(1, n_p + 1):
            pid += 1
            con.execute(
                "INSERT INTO sim.pitch_pool VALUES (?, 1, ?, ?, ?, ?, 300, 1.0)",
                [pid, ab_no, p, _SEASON, pitcher],
            )


class TestTheBuilder:
    def test_one_row_per_boundary_with_the_right_facts(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            manifest = build_pitching_change_pool(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        # 18 plate appearances, two sides: the first of each side has no
        # predecessor, so 16 boundaries.
        assert manifest["count"] == 16 and manifest["games"] == 1
        with open(tmp_path / "manager_pool" / "manifest.json", encoding="utf-8") as fh:
            assert json.load(fh)["count"] == 16
        art_dir = str(tmp_path)
        (tmp_path / "pitch_pool").mkdir()
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(art_dir)
        cp = art.change_pool
        assert cp is not None and cp.n == 16
        probe = duckdb.connect(":memory:")
        try:
            meta = probe.execute(
                "SELECT at_bat_number, pitcher_id, incoming_id, is_starter, new_half, changed "
                f"FROM read_parquet('{(tmp_path / 'manager_pool' / 'change.meta.parquet').as_posix()}') "
                "ORDER BY at_bat_number"
            ).fetchall()
        finally:
            probe.close()
        by_ab = {r[0]: r for r in meta}
        # Plate appearance 2 (top 1st, second batter): pitcher 100 stays.
        assert by_ab[2][1:] == (100, 100, 1, 0, 0)
        # Plate appearance 7 (top 2nd, first batter): a half-inning boundary, no change.
        assert by_ab[7][1:] == (100, 100, 1, 1, 0)
        # Plate appearance 14 (top 3rd, second batter): 100 -> 101, mid-inning.
        assert by_ab[14][1:] == (100, 101, 1, 0, 1)
        # Plate appearance 15: 101 stays; he is not the starter.
        assert by_ab[15][1:] == (101, 101, 0, 0, 0)
        # The situation columns of plate appearance 14: 100 had thrown 7 PAs x 3
        # = 21 pitches and faced 7 batters; a runner on first, one out, home
        # (the fielding side in the top) up 2-0 -> +2.
        order = np.argsort(cp.pitcher_id * 0 + np.arange(cp.n))  # file order = at-bat order
        sit14 = cp.sit[
            [i for i in range(cp.n) if cp.incoming_id[i] == 101 and cp.changed[i] == 1][0]
        ]
        assert sit14.tolist() == [21.0, 7.0, 3.0, 1.0, 1.0, 2.0]
        # The bottom-half boundaries carry the away side's view (down 0-2 -> -2 in the 3rd).
        bottom_3 = [cp.sit[i] for i in range(cp.n) if cp.pitcher_id[i] == 200 and cp.sit[i, 2] == 3]
        assert bottom_3 and all(float(s[5]) == -2.0 for s in bottom_3)
        # The manifest's rates.
        assert manifest["rates"]["changed"] == pytest.approx(1 / 16)
        assert manifest["rates"]["changed_new_half"] == 0.0
        assert manifest["rates"]["changed_reliever"] == 0.0
        del order

    def test_the_pool_is_shareable(self, tmp_path):
        con = duckdb.connect(":memory:")
        try:
            _seed(con)
            build_pitching_change_pool(con, str(tmp_path), [_SEASON])
        finally:
            con.close()
        _minimal_pitch_pool(tmp_path)
        art = EngineArtifacts.load(str(tmp_path))
        shared = art.extract_shared_arrays()
        assert shared["change_pool.sit"] is art.change_pool.sit
        view = np.zeros(art.change_pool.n, dtype=np.int8)
        art.attach_shared_views({"change_pool.changed": view})
        assert art.change_pool.changed is view
        art2 = EngineArtifacts.load(str(tmp_path), shared_views={"change_pool.changed": view})
        assert art2.change_pool is not None and art2.change_pool.changed is view

    def test_an_older_bundle_has_no_pool(self, tmp_path):
        _minimal_pitch_pool(tmp_path)
        assert EngineArtifacts.load(str(tmp_path)).change_pool is None


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


# ===========================================================================
# The draw
# ===========================================================================


def _change_pool(
    rows: list[tuple],
) -> ChangePool:
    """rows: (pitch_count, batters_faced, inning, outs, runners, score, is_starter, new_half, changed, pitcher)."""
    a = np.asarray([r[:6] for r in rows], dtype=np.float32)
    n = len(rows)
    return ChangePool(
        sit=a,
        pitcher_id=np.asarray([r[9] for r in rows], dtype=np.int64),
        incoming_id=np.asarray([r[9] + (1 if r[8] else 0) for r in rows], dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        is_starter=np.asarray([r[6] for r in rows], dtype=np.int8),
        new_half=np.asarray([r[7] for r in rows], dtype=np.int8),
        changed=np.asarray([r[8] for r in rows], dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
    )


def _sampler(
    pool: ChangePool | None, *, seed: int = 0, pitcher_sim=None, index=None
) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={},
        pitcher_sim=pitcher_sim or {},
        pitcher_sim_index=index or {},
        change_pool=pool,
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    fp.change_min_cell = 1
    return fp


def _draws(fp: FullPoolSampler, n: int = 60, **kw) -> list[bool]:
    base = {
        "is_starter": True,
        "new_half": False,
        "pitch_count": 95,
        "batters_faced": 22,
        "inning": 7,
        "outs": 1,
        "runners_state": 1,
        "score_diff": 0,
    }
    base.update(kw)
    return [fp.pitching_change_draw("100:2024", **base) for _ in range(n)]


#: Starters at 90-99 pitches, third time through: every one changed; starters
#: at 60-69, second time through: none changed; a reliever boundary: changed.
_ROWS = (
    [(95 + i % 3, 22 + i % 2, 7, 1, 1, 0, 1, 0, 1, 100) for i in range(6)]
    + [(62 + i % 3, 14 + i % 2, 5, 1, 1, 0, 1, 0, 0, 100) for i in range(6)]
    + [(15 + i, 4, 8, 0, 0, 0, 0, 1, 1, 300) for i in range(4)]
)


class TestTheDraw:
    def test_the_cell_decides(self):
        fp = _sampler(_change_pool(_ROWS))
        assert all(_draws(fp, pitch_count=95, batters_faced=22))
        assert not any(_draws(fp, pitch_count=62, batters_faced=14, inning=5))
        # The reliever cell (a half-inning boundary, first time through).
        assert all(
            _draws(fp, is_starter=False, new_half=True, pitch_count=16, batters_faced=4, inning=8)
        )
        assert fp.change_widen_counts.tolist() == [180, 0, 0, 0]

    def test_the_widening_ladder_and_its_counts(self):
        fp = _sampler(_change_pool(_ROWS))
        fp.change_min_cell = 1
        # A starter at 95 pitches on his fourth time through: no such cell ->
        # level 1 (the bucket without the times-through dimension) -> changed.
        assert all(_draws(fp, pitch_count=95, batters_faced=30, n=10))
        assert fp.change_widen_counts.tolist() == [0, 10, 0, 0]
        # A starter at 40 pitches: no bucket -> level 2 (the role and the
        # boundary type): both starter cells, the kernel favours the nearer
        # 60-pitch rows -> mostly unchanged but the 95-pitch rows can draw.
        got = _draws(fp, pitch_count=40, batters_faced=10, inning=4, n=60)
        assert fp.change_widen_counts.tolist() == [0, 10, 60, 0]
        assert sum(got) < 30
        # A starter at a half-inning boundary: no such cells -> level 3 (the role).
        _draws(fp, new_half=True, n=5)
        assert fp.change_widen_counts.tolist() == [0, 10, 60, 5]
        # The floor: with a high floor every draw widens at once.
        fp2 = _sampler(_change_pool(_ROWS))
        fp2.change_min_cell = 100
        _draws(fp2, n=3)
        assert fp2.change_widen_counts.tolist() == [0, 0, 0, 3]

    def test_the_soft_kernel_favours_the_nearer_situation(self):
        # One cell holding two groups: tied games unchanged, blowouts changed.
        rows = [(95, 22, 7, 1, 1, 0, 1, 0, 0, 100)] * 6 + [(95, 22, 7, 1, 1, 8, 1, 0, 1, 100)] * 6
        fp = _sampler(_change_pool(rows))
        fp.change_sit_sigma = 0.05
        assert not any(_draws(fp, score_diff=0))
        assert all(_draws(fp, score_diff=8))
        fp.change_sit_sigma = 1e6
        got = _draws(fp, score_diff=0, n=80)
        assert 15 < sum(got) < 65

    def test_the_live_pitchers_similarity_weights_the_rows(self):
        # Two pitchers' boundaries in one cell: pitcher 100's rows changed,
        # pitcher 101's did not. The live arm is 100: similar to 100 (1.0), far
        # from 101 (0.05).
        rows = [(95, 22, 7, 1, 1, 0, 1, 0, 1, 100)] * 6 + [(95, 22, 7, 1, 1, 0, 1, 0, 0, 101)] * 6
        sim = {"100:2024": {"100:2024": 1.0, "101:2024": 0.05}}
        index = {"100:2024": 0, "101:2024": 1}
        fp = _sampler(_change_pool(rows), pitcher_sim=sim, index=index)
        fp.change_pitcher_power = 1.0
        got = _draws(fp, n=100)
        assert sum(got) > 85
        fp.change_pitcher_power = 0.0  # off: the two groups draw evenly
        got = _draws(fp, n=100)
        assert 30 < sum(got) < 70
        # A row whose pitcher has no profile is neutral.
        rows2 = rows[:6] + [(95, 22, 7, 1, 1, 0, 1, 0, 0, 999)] * 6
        fp2 = _sampler(_change_pool(rows2), pitcher_sim=sim, index=index)
        f = fp2._change_pitcher_factor("100:2024", fp2._change_meta()["prof"])
        assert f is not None and f[6:].tolist() == [1.0] * 6

    def test_no_pool_is_none_and_the_last_row_is_readable(self):
        assert (
            _sampler(None).pitching_change_draw(
                "100:2024",
                is_starter=True,
                new_half=False,
                pitch_count=95,
                batters_faced=22,
                inning=7,
                outs=1,
                runners_state=1,
                score_diff=0,
            )
            is None
        )
        assert _sampler(None).last_change_row() is None
        fp = _sampler(_change_pool(_ROWS))
        assert _draws(fp, n=1) == [True]
        last = fp.last_change_row()
        assert last is not None and last["changed"] is True and last["incoming_id"] == 101
        assert last["pitcher_id"] == 100

    def test_a_manager_weight_multiplies_the_rows(self):
        rows = [(95, 22, 7, 1, 1, 0, 1, 0, 1, 100)] * 6 + [(95, 22, 7, 1, 1, 0, 1, 0, 0, 100)] * 6
        fp = _sampler(_change_pool(rows))
        w = np.array([1.0] * 6 + [0.0] * 6, dtype=np.float32)
        base = {
            "is_starter": True,
            "new_half": False,
            "pitch_count": 95,
            "batters_faced": 22,
            "inning": 7,
            "outs": 1,
            "runners_state": 1,
            "score_diff": 0,
        }
        assert all(fp.pitching_change_draw("100:2024", manager_weight=w, **base) for _ in range(20))


# ===========================================================================
# The order and the switch
# ===========================================================================


class _Recorder(StateMachine):
    """Records every start-of-plate-appearance hook call with the count and
    the pre-pitch hook calls, to pin the order."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.calls: list[tuple[str, int, int, int]] = []

    def _start_of_pa_hook(self, state):
        self.calls.append(("start", int(state.balls), int(state.strikes), int(state.half_pa_count)))
        return super()._start_of_pa_hook(state)

    def _pre_pitch_hook(self, state):
        self.calls.append(("pre", int(state.balls), int(state.strikes), int(state.half_pa_count)))
        return super()._pre_pitch_hook(state)


def _machine(manager=None, *, draw: bool = False) -> _Recorder:
    from simulation.synthetic_bundle import synthetic_sampler

    fp = synthetic_sampler(league_artifacts(), seed=3)
    m = _Recorder(fp, rng=np.random.default_rng(3), manager=manager)
    m.manager_draw = draw
    return m


def _run_game(m: StateMachine, **kw):
    from simulation.sim_loop import simulate_game

    return simulate_game(
        state_machine=m,
        seed=3,
        away_lineup=[1, 2, 3, 4, 5, 6, 7, 8, 9],
        home_lineup=[11, 12, 13, 14, 15, 16, 17, 18, 19],
        pitcher_id=100,
        home_pitcher_id=100,
        away_pitcher_id=200,
        max_innings=9,
        **kw,
    )


class TestTheOrder:
    def test_the_hook_runs_once_per_plate_appearance_on_its_first_pitch_before_the_pre_pitch_hook(
        self,
    ):
        m = _machine(manager={"starter_pull_pct_before_100": 0.0})
        _run_game(m)
        starts = [c for c in m.calls if c[0] == "start"]
        assert starts and all(c[1] == 0 and c[2] == 0 for c in starts)
        # Every start call is immediately followed by the pre-pitch hook on the same pitch.
        for i, c in enumerate(m.calls):
            if c[0] == "start":
                assert m.calls[i + 1][0] == "pre" and m.calls[i + 1][1:] == c[1:]
        # No pre-pitch hook at 0-0 without a start call right before it.
        for i, c in enumerate(m.calls):
            if c[0] == "pre" and c[1] == 0 and c[2] == 0:
                assert m.calls[i - 1][0] == "start"
        # The half's plate-appearance count restarts at every half inning and
        # reads 0 on the first plate appearance of a half.
        assert any(c[3] == 0 for c in starts) and any(c[3] > 0 for c in starts)

    def test_with_no_manager_the_hook_is_a_no_op(self):
        m = _machine(None)
        _run_game(m)
        assert not m.manager_decisions

    def test_the_starters_are_kept_apart_from_the_current_pitchers(self):
        m = _machine(None)
        res = _run_game(m)
        state = res.final_state if hasattr(res, "final_state") else None
        if state is not None:
            assert state.home_starter_id == 100 and state.away_starter_id == 200


class _DrawSampler:
    """A duck-typed sampler stand-in whose draw answers a fixed script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def pitching_change_draw(self, pitcher_key, **kw):
        self.calls.append({"key": pitcher_key, **kw})
        return self.answers.pop(0) if self.answers else None


def _state_for_pull(pitch_count: int) -> GameState:
    s = GameState(
        pitcher_id=100,
        bat_hand="R",
        season=_SEASON,
        away_lineup=[1, 2, 3],
        home_lineup=[11, 12, 13],
    )
    s.batter_id = 1
    s.pitcher_pitch_count = pitch_count
    s.pitcher_bf = {100: 20}
    s.home_starter_id = 100
    s.away_starter_id = 200
    s.inning = 6
    s.home_score, s.away_score = 3, 1
    s.manager.bullpen_available = {Team.HOME: [901, 902]}
    assert s.half is Half.TOP and s.defense is Team.HOME
    return s


class TestTheSwitch:
    def _machine(self, sampler, draw=True):
        m = StateMachine(
            sampler, rng=np.random.default_rng(1), manager={"starter_pull_pct_before_100": 0.9}
        )
        m.manager_draw = draw
        return m

    def test_the_draw_decides_without_floor_or_ceiling(self):
        # 30 pitches — far below the formula's 75-pitch floor — but the drawn
        # row says "changed": the pitcher leaves.
        fp = _DrawSampler([True])
        m = self._machine(fp)
        s = _state_for_pull(30)
        m._maybe_pull_starter(s, 1.0)
        # (the positional picker takes the back of the pen at low leverage)
        assert s.pitcher_id in (901, 902) and m.manager_decisions[-1]["source"] == "draw"
        call = fp.calls[0]
        assert call["key"] == "100:2024" and call["is_starter"] is True
        assert (
            call["new_half"] is True and call["pitch_count"] == 30 and call["batters_faced"] == 20
        )
        assert call["inning"] == 6 and call["score_diff"] == 2  # the fielding (home) side's view
        # 120 pitches — past the formula's forced ceiling — but the drawn row
        # says "no change": he stays.
        fp2 = _DrawSampler([False])
        m2 = self._machine(fp2)
        s2 = _state_for_pull(120)
        m2._maybe_pull_starter(s2, 1.0)
        assert s2.pitcher_id == 100 and not m2.manager_decisions

    def test_a_reliever_is_not_the_starter_and_the_half_count_feeds_new_half(self):
        fp = _DrawSampler([False])
        m = self._machine(fp)
        s = _state_for_pull(10)
        s.pitcher_id = 901  # a reliever on the mound
        s.half_pa_count = 3
        s.pitcher_bf = {901: 3}
        m._maybe_pull_starter(s, 1.0)
        assert fp.calls[0]["is_starter"] is False and fp.calls[0]["new_half"] is False
        assert fp.calls[0]["batters_faced"] == 3

    def test_no_pool_keeps_the_formula(self):
        fp = _DrawSampler([None])
        m = self._machine(fp)
        s = _state_for_pull(120)  # the formula's ceiling forces the pull
        m._maybe_pull_starter(s, 1.0)
        assert s.pitcher_id in (901, 902) and m.manager_decisions[-1]["source"] == "formula"
        assert m.manager_decisions[-1]["forced"] is True

    def test_the_switch_off_never_asks_the_sampler(self):
        fp = _DrawSampler([True])
        m = self._machine(fp, draw=False)
        s = _state_for_pull(30)
        m._maybe_pull_starter(s, 1.0)
        assert not fp.calls and s.pitcher_id == 100


# ===========================================================================
# The factory env reads
# ===========================================================================


class TestTheFactoryEnv:
    def test_switch(self, monkeypatch):
        monkeypatch.delenv("SIM_MANAGER_DRAW", raising=False)
        assert _manager_draw_enabled() is False
        monkeypatch.setenv("SIM_MANAGER_DRAW", "1")
        assert _manager_draw_enabled() is True
        monkeypatch.setenv("SIM_MANAGER_DRAW", "off")
        assert _manager_draw_enabled() is False

    def test_sampler_reads(self):
        s = SimpleNamespace()
        apply_manager_env(s, env={})
        assert (
            s.change_sit_sigma == 1.0 and s.change_pitcher_power == 1.0 and s.change_min_cell == 20
        )
        apply_manager_env(
            s,
            env={
                "SIM_CHANGE_SIT_SIGMA": "0.5",
                "SIM_CHANGE_PITCHER_POWER": "0",
                "SIM_CHANGE_MIN_CELL": "junk",
            },
        )
        assert (
            s.change_sit_sigma == 0.5 and s.change_pitcher_power == 0.0 and s.change_min_cell == 20
        )

    def test_the_unit_suite_pins_the_draw_off(self):
        assert os.environ.get("SIM_MANAGER_DRAW") == "0"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
