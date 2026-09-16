"""SIM-518 — the offline fatigue-kernel scan's arithmetic (scripts/sim518_fatigue_scan.py).

Three properties pin the scan before its read is trusted:

1. The two kernel limits. With both terms flat the kernel's expected mix at
   every live state is the sub-cell marginal (no conditioning); with a very
   tight kernel it reproduces the pool's own conditional by band exactly.
2. The within-pitcher construction. Two synthetic pitchers with known band
   deltas pool to the row-weighted mean of those deltas — and a pitcher whose
   rows sit in one band contributes nothing (his delta is zero by identity).
3. The sub-cell ids match the sampler's own cell algebra, so the scan
   partitions the pool the way the draw does.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_MODULE_PATH = os.path.join(_REPO_ROOT, "scripts", "sim518_fatigue_scan.py")
_spec = importlib.util.spec_from_file_location("sim518_fatigue_scan", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
scan = importlib.util.module_from_spec(_spec)
sys.modules["sim518_fatigue_scan"] = scan
_spec.loader.exec_module(scan)


def _synthetic_pool(rng: np.random.Generator, n: int = 20_000) -> dict[str, np.ndarray]:
    """One sub-cell's worth of rows: the ball share rises with the pitch count
    (a real fatigue effect) and the whiff share is flat."""
    pc = rng.integers(0, 110, size=n)
    tto = np.where(pc < 30, 1, np.where(pc < 60, 2, np.where(pc < 90, 3, 4)))
    p_ball = 0.30 + 0.002 * pc  # 0.30 at pitch 0 -> 0.52 at pitch 110
    u = rng.random(n)
    out = np.where(u < p_ball, 0, np.where(u < p_ball + 0.10, 2, 4))  # ball / whiff / in play
    weight = np.ones(n, dtype=np.float64)
    sub = np.zeros(n, dtype=np.int64)
    quality = np.zeros((n, 2), dtype=np.float64)
    return {"pc": pc, "tto": tto, "out": out, "weight": weight, "sub": sub, "quality": quality}


class TestKernelLimits:
    def test_flat_kernel_gives_the_subcell_marginal_at_every_band(self) -> None:
        d = _synthetic_pool(np.random.default_rng(1))
        H, M, Q = scan.build_histograms(
            d["sub"], d["pc"], d["tto"], d["out"], d["weight"], d["quality"]
        )
        exp = scan.expected_conditional(H, M, Q, scan.FLAT, scan.FLAT)
        ref = scan.marginal_reference(H)
        # Every band's expected ball share equals the overall ball share.
        for b in range(scan.N_PC_BAND):
            assert exp["band"][b, 0] == pytest.approx(ref["all"][0], abs=1e-5)
        # ... while the pool's own conditional genuinely varies by band.
        assert ref["band"][-1, 0] - ref["band"][0, 0] > 0.10
        assert exp["ess_share"] == pytest.approx(1.0, abs=1e-5)

    def test_a_tight_kernel_reproduces_the_pool_conditional(self) -> None:
        d = _synthetic_pool(np.random.default_rng(2))
        H, M, Q = scan.build_histograms(
            d["sub"], d["pc"], d["tto"], d["out"], d["weight"], d["quality"]
        )
        exp = scan.expected_conditional(H, M, Q, 0.05, 0.05)
        ref = scan.marginal_reference(H)
        for b in range(scan.N_PC_BAND):
            assert exp["band"][b, 0] == pytest.approx(ref["band"][b, 0], abs=1e-4)
        assert exp["ess_share"] < 0.05

    def test_the_ladder_is_monotone_between_the_limits(self) -> None:
        d = _synthetic_pool(np.random.default_rng(3))
        H, M, Q = scan.build_histograms(
            d["sub"], d["pc"], d["tto"], d["out"], d["weight"], d["quality"]
        )
        ref = scan.marginal_reference(H)
        full = ref["band"][-1, 0] - ref["band"][0, 0]
        spreads = []
        for s in (5.0, 20.0, 60.0, 200.0):
            exp = scan.expected_conditional(H, M, Q, s, scan.FLAT)
            spreads.append(exp["band"][-1, 0] - exp["band"][0, 0])
        assert spreads[0] == pytest.approx(full, rel=0.1)
        assert spreads[0] > spreads[1] > spreads[2] > spreads[3] >= 0.0


class TestWithinPitcherReference:
    def test_two_pitchers_pool_to_the_row_weighted_delta(self) -> None:
        # Pitcher 1: 100 rows in band 0 at ball share 0.30, 100 rows in band 4
        # at 0.50 -> his deltas are -0.10 / +0.10. Pitcher 2: 300 rows in band
        # 0 at 0.20, 100 rows in band 4 at 0.60 -> overall 0.30, deltas
        # -0.10 / +0.30. Pooled by rows: band 0 = (100*-0.10 + 300*-0.10)/400
        # = -0.10; band 4 = (100*0.10 + 100*0.30)/200 = +0.20.
        def rows(pitcher: int, pc: int, n: int, ball: float) -> tuple:
            k = np.full(n, pitcher)
            p = np.full(n, pc)
            t = np.ones(n, dtype=np.int64)
            n_ball = int(round(n * ball))
            o = np.array([0] * n_ball + [4] * (n - n_ball))
            return k, p, t, o

        parts = [
            rows(1, 5, 100, 0.30),
            rows(1, 105, 100, 0.50),
            rows(2, 5, 300, 0.20),
            rows(2, 105, 100, 0.60),
        ]
        key = np.concatenate([p[0] for p in parts])
        pc = np.concatenate([p[1] for p in parts])
        tto = np.concatenate([p[2] for p in parts])
        out = np.concatenate([p[3] for p in parts])
        w = np.ones(key.size)
        ref = scan.within_pitcher_reference(key, pc, tto, out, w, min_rows=10)
        assert ref["delta_band"][0, 0] == pytest.approx(-0.10, abs=1e-9)
        assert ref["delta_band"][4, 0] == pytest.approx(0.20, abs=1e-9)
        assert ref["n_pitchers_multi_band"] == 2

    def test_a_one_band_pitcher_contributes_nothing(self) -> None:
        key = np.array([7] * 200 + [8] * 100 + [8] * 100)
        pc = np.array([5] * 200 + [5] * 100 + [105] * 100)
        tto = np.ones(400, dtype=np.int64)
        # Pitcher 7 (one band) is 90% balls; pitcher 8 is 30% then 50%.
        out = np.array([0] * 180 + [4] * 20 + [0] * 30 + [4] * 70 + [0] * 50 + [4] * 50)
        ref = scan.within_pitcher_reference(key, pc, tto, out, np.ones(400), min_rows=10)
        assert ref["delta_band"][0, 0] == pytest.approx(-0.10, abs=1e-9)
        assert ref["delta_band"][4, 0] == pytest.approx(0.10, abs=1e-9)
        assert ref["n_pitchers_multi_band"] == 1


class TestSubcellIds:
    def test_ids_match_the_sampler_cell_algebra(self) -> None:
        from simulation.filter_cells import N_BAND, N_COUNT, N_OUTS, score_band

        rng = np.random.default_rng(4)
        n = 500
        sit = np.zeros((n, 6), dtype=np.float32)
        sit[:, 0] = rng.integers(0, 4, n)  # balls
        sit[:, 1] = rng.integers(0, 3, n)  # strikes
        sit[:, 2] = rng.integers(0, 3, n)  # outs
        sit[:, 3] = rng.integers(0, 8, n)  # runners
        sit[:, 4] = rng.integers(1, 10, n)  # inning
        sit[:, 5] = rng.integers(-5, 6, n)  # score diff
        bat_home = rng.integers(0, 2, n).astype(np.int64)
        ids = scan.subcell_ids(sit, bat_home)
        for i in range(n):
            rs = int(sit[i, 3]) & 0b111
            outs = int(sit[i, 2])
            band = score_band(int(sit[i, 5]))
            side = 1 if bat_home[i] > 0 else 0
            cb = int(sit[i, 0]) * 3 + int(sit[i, 1])
            expected = (((rs * N_OUTS + outs) * N_BAND + band) * 3 + side) * N_COUNT + cb
            assert ids[i] == expected

    def test_an_all_unknown_side_collapses_the_side_axis(self) -> None:
        sit = np.zeros((10, 6), dtype=np.float32)
        ids_none = scan.subcell_ids(sit, None)
        ids_unknown = scan.subcell_ids(sit, np.full(10, -1, dtype=np.int64))
        assert np.array_equal(ids_none, ids_unknown)


class TestPitcherQuality:
    def test_each_row_carries_its_pitchers_own_season_shares(self) -> None:
        key = np.array([1, 1, 1, 1, 2, 2])
        out = np.array([2, 2, 0, 4, 0, 0])  # p1: whiff 0.5, ball 0.25; p2: ball 1.0
        # p1 faces the order twice on half his rows (a starter); p2 never (a reliever)
        tto = np.array([1, 1, 2, 2, 1, 1])
        q = scan.pitcher_quality(key, out, np.ones(6), tto)
        assert q.shape == (6, len(scan.QUALITY_COLS))
        assert q[0, :2].tolist() == pytest.approx([0.5, 0.25])
        assert q[4, :2].tolist() == pytest.approx([0.0, 1.0])
        assert q[0, 2] == 1.0 and q[4, 2] == 0.0  # the role flag
        assert q[0, 3:].tolist() == pytest.approx([0.5, 0.25])  # whiff/ball × starter
        assert q[4, 3:].tolist() == pytest.approx([0.0, 0.0])
