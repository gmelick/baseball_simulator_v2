"""
tests/unit/test_sim467_cell_index.py
====================================
SIM-467 — the pitch-draw CELL INDEX (plan: docs/audit/2026-09-04-sim467-518-plan.md §5).

The 12 count buckets generalize to the SIM-451 cell — (runners, outs, count,
score band, batting side) — ordered so each plate-appearance cell is one
contiguous slice with its 12 count sub-cells inside it. What these tests pin:

  * switch OFF: the whole-pool path runs and builds no index (byte-identical);
  * switch ON, MIN_CELL 0: every draw comes from the live cell, and every
    in-cell weight equals the whole-pool path's weight for that row bit for
    bit — the ONLY change is a zero weight outside the cell;
  * decision #19's widening ladder (band → side → count) below MIN_CELL, the
    per-draw level counter, and the empty base-out cell that raises;
  * a pre-0023 bundle (no ``bat_home``) has no side dimension — 1,440 cells —
    and places the same rows in the same (runners, outs, band, count) cell;
  * the drawn row's global index survives (the got-away / geometry reads);
  * the cell algebra lives in ``simulation.filter_cells`` and the SIM-451
    script re-exports the very same objects;
  * the loop passes the live side when the index is on and a mid-PA base-out
    change re-selects the cell.
"""

from __future__ import annotations

import importlib.util
import os

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
from simulation import filter_cells as fc
from simulation.full_pool_sampler import FullPoolSampler
from simulation.game_state import Bases, GameState, Half, Team
from simulation.sim_loop import StateMachine

_SEASON = 2024
_PITCHER = "100:2024"
_BATTERS = ["200:2024", "201:2024"]
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "measure_filter_cells.py")

#: One representative score difference per band (edges -3, -1, 0, 2).
_SD_FOR_BAND = {0: -4, 1: -2, 2: 0, 3: 1, 4: 3}


def _label(rs: int, outs: int, band: int, side: int, cb: int) -> str:
    return f"c:{rs}-{outs}-{band}-{side}-{cb}"


def _grid_pool(
    rows_per_cell: int = 2,
    *,
    with_side: bool = True,
    count_fn=None,
    outcome_fn=None,
    got_away_fn=None,
    skip_rs: int | None = None,
) -> HandPool:
    """A pool with ``rows_per_cell`` rows in EVERY (runners, outs, band, side,
    count) cell (``count_fn`` overrides the count per cell), whose outcome label
    names the cell, so a draw reveals which cell it came from."""
    recs: list[tuple[int, int, int, int, int]] = []
    for rs in range(8):
        if rs == skip_rs:
            continue
        for outs in range(3):
            for band in range(5):
                for side in range(2) if with_side else [0]:
                    for cb in range(12):
                        k = count_fn(rs, outs, band, side, cb) if count_fn else rows_per_cell
                        recs.extend([(rs, outs, band, side, cb)] * int(k))
    n = len(recs)
    arr = np.asarray(recs, dtype=np.int64)
    rs_a, outs_a, band_a, side_a, cb_a = (arr[:, i] for i in range(5))
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 0] = cb_a // 3
    sit[:, 1] = cb_a % 3
    sit[:, 2] = outs_a
    sit[:, 3] = rs_a
    sit[:, 4] = 5.0
    sit[:, 5] = [_SD_FOR_BAND[int(b)] for b in band_a]
    outcomes = [(outcome_fn(*r) if outcome_fn else _label(*r)) for r in map(tuple, arr.tolist())]
    idx = np.arange(n)
    geom = np.zeros((n, 10), dtype=np.float32)
    geom[:, 0] = 90.0 + (idx % 7)  # a real velocity on every row
    return HandPool(
        geom=geom,
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.where(idx % 2 == 0, 200, 201).astype(np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(outcomes, dtype=object),
        recency=(1.0 + (idx % 3) * 0.5).astype(np.float32),
        bat_home=(side_a.astype(np.int8) if with_side else None),
        got_away=(
            np.asarray([got_away_fn(*r) for r in arr.tolist()], dtype=np.int8)
            if got_away_fn
            else None
        ),
    )


def _batter_emb() -> dict:
    vecs = np.array([[0.0, 0.0, 0.0], [1.0, -1.0, 0.5]], dtype=np.float32)
    return {
        "keys": np.asarray(_BATTERS, dtype=object),
        "key_index": {k: i for i, k in enumerate(_BATTERS)},
        "vecs": vecs,
        "mean": np.zeros(3, dtype=np.float32),
        "std": np.ones(3, dtype=np.float32),
        "features": ["a", "b", "c"],
    }


def _sampler(pool: HandPool, *, on: bool, min_cell: int = 0, seed: int = 0) -> FullPoolSampler:
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        actor_emb={"batter": _batter_emb()},
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    fp.pitch_cell_index = on
    fp.pitch_min_cell = min_cell
    return fp


def _base_out(rs: int, outs: int, band: int) -> np.ndarray:
    return np.array([outs, rs, 5, _SD_FOR_BAND[band]], dtype=np.float32)


def _draws(fp: FullPoolSampler, bo: np.ndarray, cb: int, n: int = 60, **kw) -> set[str]:
    fp.new_half_inning("R", _PITCHER)
    out = set()
    for i in range(n):
        fp.new_plate_appearance(_BATTERS[i % 2], bo, **kw)
        out.add(fp.draw(cb // 3, cb % 3))
    return out


# ===========================================================================
# Off = the whole-pool path; on = the live cell only, same in-cell weights
# ===========================================================================


class TestCellPathEquivalence:
    def test_an_all_unknown_side_column_has_no_side_dimension(self):
        # SIM-523 part B found this live: a migration-0023 pool exported before
        # its rebuild carries bat_home = -1 on EVERY row. With a side
        # dimension every live side's cell is empty and every draw widens to
        # level 2 (past the score band). The column must count as absent.
        pool = _grid_pool(with_side=False)
        pool.bat_home = np.full(pool.n, -1, dtype=np.int8)
        fp = _sampler(pool, on=True)
        assert fp._cell_meta("R")["n_side"] == 1
        assert _draws(fp, _base_out(3, 1, 1), cb=4, n=60, bat_home=True) == {_label(3, 1, 1, 0, 4)}
        assert fp.widen_counts.tolist() == [60, 0, 0, 0]

    def test_switch_off_builds_no_index_and_uses_the_whole_pool_buckets(self):
        fp = _sampler(_grid_pool(), on=False)
        _draws(fp, _base_out(3, 1, 1), cb=4, n=5, bat_home=True)
        assert fp._cell_cache == {} and fp._pa_rows is None
        assert int(fp.widen_counts.sum()) == 0

    def test_on_draws_only_from_the_live_cell(self):
        fp = _sampler(_grid_pool(), on=True)
        assert _draws(fp, _base_out(3, 1, 1), cb=4, n=120, bat_home=True) == {_label(3, 1, 1, 1, 4)}
        assert _draws(fp, _base_out(6, 2, 4), cb=0, n=120, bat_home=False) == {
            _label(6, 2, 4, 0, 0)
        }

    def test_the_cell_rows_are_exactly_the_matching_pool_rows(self):
        pool = _grid_pool()
        fp = _sampler(pool, on=True)
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTERS[0], _base_out(5, 0, 3), bat_home=True)
        assert fp._pa_rows is not None
        sit = pool.sit
        for cb in range(12):
            expected = np.nonzero(
                (sit[:, 3] == 5)
                & (sit[:, 2] == 0)
                & (fc.score_band_array(sit[:, 5]) == 3)
                & (pool.bat_home == 1)
                & (sit[:, 0] * 3 + sit[:, 1] == cb)
            )[0]
            assert sorted(fp._pa_rows[cb].tolist()) == sorted(expected.tolist())

    def test_in_cell_weights_are_bit_identical_to_the_whole_pool_path(self):
        pool = _grid_pool()
        off = _sampler(pool, on=False)
        on = _sampler(pool, on=True)
        bo = _base_out(1, 2, 2)
        for fp in (off, on):
            fp.new_half_inning("R", _PITCHER)
            fp.new_plate_appearance(_BATTERS[1], bo, bat_home=False)
        w_off = off._base * off._f_batter("R", _BATTERS[1]) * off._f_situation_baseout("R", bo)
        assert on._pa_rows is not None and on._bucket_cdf is not None
        for cb in range(12):
            rows = on._pa_rows[cb]
            expected = np.cumsum(w_off[rows], dtype=np.float64)
            assert np.array_equal(on._bucket_cdf[cb], expected), cb

    def test_the_drawn_row_keeps_its_global_index(self):
        # got_away only on the live cell's rows: the cell path must resolve the
        # drawn row globally (the SIM-517 read) — a local index would miss it.
        pool = _grid_pool(got_away_fn=lambda rs, o, b, s, cb: int((rs, o, b, s) == (2, 1, 0, 1)))
        fp = _sampler(pool, on=True)
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTERS[0], _base_out(2, 1, 0), bat_home=True)
        fp.draw(1, 1)
        assert fp.last_pitch_got_away() is True
        assert fp._pp_last_i is not None and pool.got_away[fp._pp_last_i] == 1
        fp.new_plate_appearance(_BATTERS[0], _base_out(2, 1, 0), bat_home=False)
        fp.draw(1, 1)
        assert fp.last_pitch_got_away() is False


# ===========================================================================
# Decision #19's widening ladder
# ===========================================================================


def _thin(rs=7, outs=2, band=4, side=1, cb=9):
    """Return count functions that thin ONE target sub-cell at each level."""

    def level1(r, o, b, s, c):  # only the target cell is thin: bands rescue it
        return 1 if (r, o, b, s, c) == (rs, outs, band, side, cb) else 30

    def level2(r, o, b, s, c):  # every band of that side/count is thin: sides rescue it
        return 1 if (r, o, s, c) == (rs, outs, side, cb) else 30

    def level3(r, o, b, s, c):  # every band and side of that count is thin: counts rescue it
        return 1 if (r, o, c) == (rs, outs, cb) else 30

    return level1, level2, level3


class TestWideningLadder:
    def test_a_full_cell_stays_at_level_zero(self):
        fp = _sampler(_grid_pool(rows_per_cell=30), on=True, min_cell=20)
        _draws(fp, _base_out(7, 2, 4), cb=9, n=20, bat_home=True)
        assert fp.widen_counts.tolist() == [20, 0, 0, 0]

    def test_level_one_unions_the_score_bands(self):
        l1, _, _ = _thin()
        fp = _sampler(_grid_pool(count_fn=l1), on=True, min_cell=20)
        seen = _draws(fp, _base_out(7, 2, 4), cb=9, n=200, bat_home=True)
        assert fp.widen_counts.tolist() == [0, 200, 0, 0]
        # Same runners / outs / side / count; the band varies.
        assert seen <= {_label(7, 2, b, 1, 9) for b in range(5)} and len(seen) > 1

    def test_level_two_unions_the_sides_too(self):
        _, l2, _ = _thin()
        fp = _sampler(_grid_pool(count_fn=l2), on=True, min_cell=20)
        seen = _draws(fp, _base_out(7, 2, 4), cb=9, n=200, bat_home=True)
        assert fp.widen_counts.tolist() == [0, 0, 200, 0]
        assert seen <= {_label(7, 2, b, s, 9) for b in range(5) for s in range(2)}
        assert any(lbl.endswith("-0-9") for lbl in seen)  # the other side's rows

    def test_level_three_unions_the_counts_last(self):
        _, _, l3 = _thin()
        fp = _sampler(_grid_pool(count_fn=l3), on=True, min_cell=20)
        seen = _draws(fp, _base_out(7, 2, 4), cb=9, n=200, bat_home=True)
        assert fp.widen_counts.tolist() == [0, 0, 0, 200]
        assert all(lbl.startswith("c:7-2-") for lbl in seen)
        assert any(not lbl.endswith("-9") for lbl in seen)  # other counts' rows

    def test_min_cell_zero_never_widens_a_populated_cell(self):
        l1, _, _ = _thin()
        fp = _sampler(_grid_pool(count_fn=l1), on=True, min_cell=0)
        seen = _draws(fp, _base_out(7, 2, 4), cb=9, n=30, bat_home=True)
        assert seen == {_label(7, 2, 4, 1, 9)} and fp.widen_counts.tolist() == [30, 0, 0, 0]

    def test_an_empty_sub_cell_widens_even_at_min_cell_zero(self):
        l1, _, _ = _thin()
        fp = _sampler(_grid_pool(count_fn=lambda *r: 0 if l1(*r) == 1 else 30), on=True, min_cell=0)
        seen = _draws(fp, _base_out(7, 2, 4), cb=9, n=30, bat_home=True)
        assert fp.widen_counts.tolist() == [0, 30, 0, 0] and seen

    def test_an_empty_base_out_cell_raises(self):
        fp = _sampler(_grid_pool(skip_rs=7), on=True, min_cell=0)
        fp.new_half_inning("R", _PITCHER)
        with pytest.raises(RuntimeError, match="SIM-467"):
            fp.new_plate_appearance(_BATTERS[0], _base_out(7, 0, 2), bat_home=True)

    def test_the_stats_accessor_reports_levels_and_shape(self):
        fp = _sampler(_grid_pool(rows_per_cell=30), on=True, min_cell=20)
        _draws(fp, _base_out(0, 0, 2), cb=0, n=3, bat_home=False)
        s = fp.cell_index_stats()
        assert s["enabled"] and s["min_cell"] == 20
        assert s["draws_by_level"] == [3, 0, 0, 0] and s["n_side"] == {"R": 3}


# ===========================================================================
# The side dimension: 2,880 cells with bat_home, 1,440 without
# ===========================================================================


class TestSideDimension:
    def test_a_pool_without_bat_home_has_no_side_axis(self):
        fp = _sampler(_grid_pool(with_side=False), on=True)
        fp.new_half_inning("R", _PITCHER)
        fp.new_plate_appearance(_BATTERS[0], _base_out(3, 1, 1), bat_home=True)
        assert fp._cell_meta("R")["n_side"] == 1
        assert fp._cell_meta("R")["offsets"].size == 8 * 3 * 5 * 12 + 1

    def test_the_same_rows_land_in_the_same_cell_either_way(self):
        # Identical sit columns; only bat_home differs. The no-side pool's
        # sub-cell must equal the union of the two sides' sub-cells.
        with_side = _sampler(_grid_pool(), on=True)
        no_side = _sampler(_grid_pool(with_side=False, rows_per_cell=4), on=True)
        # rows_per_cell=4 without a side == 2 per side with one: same row count,
        # same (rs, outs, band, cb) membership by construction of the grid.
        a = with_side._subcell_rows("R", 4, 1, 3, None, 7)[0]
        b = no_side._subcell_rows("R", 4, 1, 3, 0, 7)[0]
        assert a.size == b.size == 4

    def test_an_unknown_live_side_unions_the_sides(self):
        fp = _sampler(_grid_pool(), on=True)
        seen = _draws(fp, _base_out(3, 1, 1), cb=4, n=200)  # no bat_home passed
        assert seen == {_label(3, 1, 1, 0, 4), _label(3, 1, 1, 1, 4)}

    def test_unknown_side_rows_join_only_the_side_union(self):
        pool = _grid_pool()
        assert pool.bat_home is not None
        pool.bat_home[:] = np.where(pool.bat_home == 1, -1, pool.bat_home)  # home rows unknown
        fp = _sampler(pool, on=True)
        assert _draws(fp, _base_out(3, 1, 1), cb=4, n=60, bat_home=False) == {_label(3, 1, 1, 0, 4)}
        # The live HOME side has no rows of its own now: level 1 (bands) is
        # still empty for side 1, level 2 (sides) brings the unknown rows in.
        fp2 = _sampler(pool, on=True, min_cell=0)
        seen = _draws(fp2, _base_out(3, 1, 1), cb=4, n=60, bat_home=True)
        assert seen and fp2.widen_counts[2] == 60


# ===========================================================================
# The algebra lives in simulation.filter_cells; the SIM-451 script re-exports it
# ===========================================================================


class TestCellAlgebra:
    def test_cell_key_and_decode_cell_are_inverses_over_all_cells(self):
        for cid in range(fc.N_CELLS):
            c = fc.decode_cell(cid)
            assert (
                fc.cell_key(
                    c.runners_state, c.outs, c.balls, c.strikes, _SD_FOR_BAND[c.band], c.bat_is_home
                )
                == cid
            )

    def test_score_band_array_matches_score_band(self):
        sd = np.arange(-9, 10)
        assert fc.score_band_array(sd).tolist() == [fc.score_band(int(x)) for x in sd]

    def test_the_script_re_exports_the_modules_objects(self):
        spec = importlib.util.spec_from_file_location("measure_filter_cells_467", _SCRIPT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.cell_key is fc.cell_key
        assert mod.decode_cell is fc.decode_cell
        assert mod.score_band is fc.score_band
        assert mod.count_bucket is fc.count_bucket
        assert mod.SCORE_BAND_EDGES is fc.SCORE_BAND_EDGES
        assert mod.N_CELLS == fc.N_CELLS == 2880


# ===========================================================================
# The loop: the side is passed when the index is on; base-out re-selects
# ===========================================================================


class _Recorder(FullPoolSampler):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.pa_kwargs: list[dict] = []

    def new_plate_appearance(self, batter_key, base_out, **kw) -> None:
        self.pa_kwargs.append(dict(kw))
        super().new_plate_appearance(batter_key, base_out, **kw)


def _machine(on: bool) -> tuple[StateMachine, _Recorder]:
    # Valid outcomes so the loop path is exercised: runners on -> called_strike.
    pool = _grid_pool(
        rows_per_cell=3,
        outcome_fn=lambda rs, o, b, s, cb: "called_strike" if rs else "ball",
    )
    art = EngineArtifacts(
        pools={"R": pool, "L": pool},
        pitcher_sim={_PITCHER: {_PITCHER: 1.0}},
        pitcher_sim_index={_PITCHER: 0},
        actor_emb={"batter": _batter_emb()},
    )
    fp = _Recorder(art, np.random.default_rng(3))
    fp.pitch_cell_index = on
    fp.pitch_min_cell = 0
    return StateMachine(fp, rng=np.random.default_rng(3)), fp


def _state() -> GameState:
    state = GameState(
        pitcher_id=100,
        bat_hand="R",
        season=_SEASON,
        away_lineup=[200, 201],
        home_lineup=[200, 201],
    )
    state.batter_id = 200
    assert state.half is Half.TOP and state.offense is Team.AWAY
    return state


class TestLoopWiring:
    def test_off_passes_no_side_and_on_passes_it(self):
        off_machine, off_fp = _machine(on=False)
        off_machine._full_pool_outcome(_state())
        assert off_fp.pa_kwargs == [{}]
        on_machine, on_fp = _machine(on=True)
        on_machine._full_pool_outcome(_state())
        assert on_fp.pa_kwargs == [{"bat_home": False}]  # TOP: the away team bats

    def test_a_mid_pa_base_out_change_re_selects_the_cell(self):
        machine, fp = _machine(on=True)
        state = _state()
        assert machine._full_pool_outcome(state) == "ball"  # bases empty
        state.balls, state.strikes = 1, 0
        state.bases = Bases(first=201)  # a runner reaches mid-PA
        assert machine._full_pool_outcome(state) == "called_strike"
        assert len(fp.pa_kwargs) == 2  # the base-out key rebuilt the cell
