"""
tests/unit/test_sim523_fit_powers.py
=====================================
SIM-523 part F — the FIT's knobs (plan: docs/audit/2026-09-08-sim523-play-picker-
redesign-plan.md, part F). Part F fits every new factor's power against the
pool's own conditional rates; the fit needs three things the code lacked:

  * the PITCH draw's pitcher power (``pitch_pitcher_power``): the pitcher
    factor is the engine's 0-to-1 score, raised to this power in the base
    weight on both the whole-pool and the cell path (1.0 = today, byte-identical);
  * the single draw's batter power: ``pitch_batter_power`` applied when the
    split is OFF too, on both paths (it used to apply on the split path only);
  * the RESULT draw's pitcher power is ABSOLUTE: with the pitch draw at power
    p and the result draw at q, the result weight carries f^q, not f^(p+q-1);
  * the concentration report at a power (the second pass/fail of the fit).
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool, concentration_share
from simulation.full_pool_sampler import FullPoolSampler
from simulation.production_factory import apply_result_split_env

_SEASON = 2024
_LIVE = "100:2024"
_OTHER = "101:2024"
_BATTER = "200:2024"
_BASE_OUT = np.array([0, 0, 5, 0], dtype=np.float32)
_GEOM = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)


def _pool(pitchers: list[int], batters: list[int], outcomes: list[str]) -> HandPool:
    """Every row in the 0-0 count of the empty / no-outs cell, one geometry."""
    n = len(pitchers)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    return HandPool(
        geom=np.stack([_GEOM] * n).astype(np.float32),
        sit=sit,
        pitcher_id=np.asarray(pitchers, dtype=np.int64),
        batter_id=np.asarray(batters, dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        outcome_type=np.asarray(outcomes, dtype=object),
        recency=np.ones(n, dtype=np.float32),
    )


def _sampler(pool: HandPool, *, cell: bool = False, seed: int = 0) -> FullPoolSampler:
    """The live pitcher scores 1.0 against his own rows and 0.5 against the
    other pitcher's; the batter embedding maps batter 200 -> row 0, 201 -> 1."""
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_LIVE: {_LIVE: 1.0, _OTHER: 0.5}},
        pitcher_sim_index={_LIVE: 0, _OTHER: 1},
        bb_pools={},
        actor_emb={
            "batter": {
                "key_index": {"200:2024": 0, "201:2024": 1},
                # two identical batters: the affinity kernel reads 1.0 everywhere
                "vecs": np.zeros((2, 2), dtype=np.float32),
                "mean": np.zeros(2, dtype=np.float32),
                "std": np.ones(2, dtype=np.float32),
            }
        },
    )
    fp = FullPoolSampler(art, np.random.default_rng(seed))
    if cell:
        fp.pitch_cell_index = True
        fp.pitch_min_cell = 0
    return fp


def _weights(fp: FullPoolSampler) -> np.ndarray:
    """The 0-0 bucket's draw weights in pool-row order."""
    cdf = fp._bucket_cdf[0]
    w = np.diff(np.concatenate([[0.0], cdf]))
    rows = fp._pa_rows[0] if fp._pa_rows is not None else fp._pool_meta("R")["bucket_rows"][0]
    out = np.zeros(fp.a.pools["R"].n)
    out[rows] = w
    return out


_HALF = [100, 100, 100, 100, 101, 101, 101, 101]
_SAME_BATTER = [200] * 8
_OUTS = ["swinging_strike"] * 4 + ["ball"] * 4


class TestPitchPitcherPower:
    @pytest.mark.parametrize("cell", [False, True])
    def test_the_default_is_the_raw_score(self, cell):
        fp = _sampler(_pool(_HALF, _SAME_BATTER, _OUTS), cell=cell)
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[:4] == pytest.approx(1.0) and w[4:] == pytest.approx(0.5)
        assert fp._f_pitcher_vec is not None and fp._f_pitcher_vec[4:] == pytest.approx(0.5)

    @pytest.mark.parametrize("cell", [False, True])
    @pytest.mark.parametrize("power", [2.0, 3.0, 0.5])
    def test_the_power_raises_the_pitcher_factor_on_both_paths(self, cell, power):
        fp = _sampler(_pool(_HALF, _SAME_BATTER, _OUTS), cell=cell)
        fp.pitch_pitcher_power = power
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[:4] == pytest.approx(1.0)
        assert w[4:] == pytest.approx(0.5**power)
        # the RAW factor is what the result draw re-raises from
        assert fp._f_pitcher_vec[4:] == pytest.approx(0.5)

    def test_an_unscored_row_stays_neutral_at_any_power(self):
        fp = _sampler(_pool([100, 100, 999, 999], _SAME_BATTER[:4], _OUTS[:4]))
        fp.pitch_pitcher_power = 4.0
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w == pytest.approx(1.0)  # 999 has no profile: neutral 1.0 ** 4


class TestResultPitcherPowerIsAbsolute:
    def _share_of_balls(self, pitch_power: float, result_power: float, n: int = 4000) -> float:
        """With the pitch score neutral and the density correction off, the
        result draw's ball share among the other pitcher's rows reveals the
        pitcher exponent the result draw carries."""
        fp = _sampler(_pool(_HALF, _SAME_BATTER, _OUTS))
        fp.pitch_result_split = True
        fp.result_density_power = 0.0
        fp.pitch_pitcher_power = pitch_power
        fp.result_pitcher_power = result_power
        fp._f_result_pitch = lambda hand, rows, gi: np.ones(len(rows), dtype=np.float32)
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        c = Counter(fp.draw(0, 0) for _ in range(n))
        return c["ball"] / n

    def test_equal_powers_mean_no_re_raise(self):
        # f^2 on the other rows: 4 x 0.25 / (4 + 1) = 0.2
        assert self._share_of_balls(2.0, 2.0) == pytest.approx(0.2, abs=0.03)

    def test_a_smaller_result_power_lowers_the_exponent(self):
        # the result carries f^1: 4 x 0.5 / (4 + 2) = 1/3
        assert self._share_of_balls(2.0, 1.0) == pytest.approx(1 / 3, abs=0.03)

    def test_a_larger_result_power_raises_it(self):
        # the pitch draw at f^1, the result at f^2: 0.2 again
        assert self._share_of_balls(1.0, 2.0) == pytest.approx(0.2, abs=0.03)

    def test_the_defaults_are_the_single_exponent(self):
        assert self._share_of_balls(1.0, 1.0) == pytest.approx(1 / 3, abs=0.03)


class TestSingleDrawBatterPower:
    @pytest.mark.parametrize("cell", [False, True])
    def test_the_batter_power_applies_with_the_split_off(self, cell):
        pool = _pool([100] * 8, [200] * 4 + [201] * 4, _OUTS)
        fp = _sampler(pool, cell=cell)
        # batter 200 (the live one) reads 1.0, batter 201 reads 0.5
        fp._batter_affinity = lambda key: np.array([1.0, 0.5], dtype=np.float32)
        fp.pitch_result_split = False
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        assert _weights(fp)[4:] == pytest.approx(0.5)  # power 1.0: as is
        fp.pitch_batter_power = 3.0
        fp._fp_pa_key = None
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[:4] == pytest.approx(1.0) and w[4:] == pytest.approx(0.125)


class TestFactoryAndConcentration:
    def test_the_factory_reads_the_pitch_pitcher_power(self):
        fp = _sampler(_pool(_HALF, _SAME_BATTER, _OUTS))
        apply_result_split_env(fp, {"SIM_PITCH_PITCHER_POWER": "2.5"})
        assert fp.pitch_pitcher_power == 2.5
        apply_result_split_env(fp, {})
        assert fp.pitch_pitcher_power == 1.0
        apply_result_split_env(fp, {"SIM_PITCH_PITCHER_POWER": "junk"})
        assert fp.pitch_pitcher_power == 1.0

    def test_the_concentration_share_at_a_power(self):
        # live row 0 scores 1.0 on its own staff's rows and 0.5 elsewhere
        matrix = np.array([[1.0, 0.5]], dtype=np.float32)
        cols = np.array([0, 0, 1, 1, 1, 1])
        pitchers = np.array([1, 1, 2, 2, 2, 2])
        own1, unweighted, _ = concentration_share(matrix, 0, cols, pitchers, {1})
        assert unweighted == pytest.approx(2 / 6)
        assert own1 == pytest.approx(2.0 / (2.0 + 4 * 0.5))
        own2, _, ess2 = concentration_share(matrix, 0, cols, pitchers, {1}, power=2.0)
        assert own2 == pytest.approx(2.0 / (2.0 + 4 * 0.25))
        assert own2 > own1 and 0.0 < ess2 < 1.0


# ---------------------------------------------------------------------------
# SIM-523 part F, batch 2: the draw-neutral rule for unscored rows, the batter
# matrix on the cell path, power 0 = the kernel.
# ---------------------------------------------------------------------------

_THREE = [100, 100, 100, 100, 101, 101, 999, 999]  # the live pitcher, a peer, no profile
_THREE_OUTS = ["swinging_strike"] * 4 + ["ball"] * 2 + ["foul"] * 2


class TestNeutralRule:
    @pytest.mark.parametrize("cell", [False, True])
    def test_at_power_one_an_unscored_row_keeps_its_maximum_weight(self, cell):
        # today's certified weight, byte for byte: the unscored rows read 1.0
        fp = _sampler(_pool(_THREE, [200] * 8, _THREE_OUTS), cell=cell)
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[:4] == pytest.approx(1.0) and w[4:6] == pytest.approx(0.5)
        assert w[6:] == pytest.approx(1.0)

    @pytest.mark.parametrize("cell", [False, True])
    def test_at_a_power_an_unscored_row_takes_the_mean_scored_weight(self, cell):
        fp = _sampler(_pool(_THREE, [200] * 8, _THREE_OUTS), cell=cell)
        fp.pitch_pitcher_power = 2.0
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        # scored: 4 rows at 1.0 and 2 at 0.25 -> mean 0.75
        assert w[:4] == pytest.approx(1.0) and w[4:6] == pytest.approx(0.25)
        assert w[6:] == pytest.approx(0.75)
        assert fp._neutral_mean_pitcher("R", _LIVE, 2.0) == pytest.approx(0.75)

    def test_the_result_draw_re_raises_an_unscored_row_by_the_ratio_of_means(self):
        # p=2, q=1: an unscored row goes from mean(f^2)=0.75 to mean(f)=0.833...
        # ball share = 2 x 0.5 / (4 + 1 + 2 x 0.8333) = 0.15; at p=q=2 it is
        # 2 x 0.25 / (4 + 0.5 + 1.5) = 0.0833
        def share(pitch_power, result_power, n=6000):
            fp = _sampler(_pool(_THREE, [200] * 8, _THREE_OUTS))
            fp.pitch_result_split = True
            fp.result_density_power = 0.0
            fp.pitch_pitcher_power = pitch_power
            fp.result_pitcher_power = result_power
            fp._f_result_pitch = lambda hand, rows, gi: np.ones(len(rows), dtype=np.float32)
            fp.new_half_inning("R", _LIVE)
            fp.new_plate_appearance(_BATTER, _BASE_OUT)
            c = Counter(fp.draw(0, 0) for _ in range(n))
            return c["ball"] / n, c["foul"] / n

        ball, foul = share(2.0, 2.0)
        assert ball == pytest.approx(0.5 / 6.0, abs=0.02) and foul == pytest.approx(
            1.5 / 6.0, abs=0.02
        )
        ball, foul = share(2.0, 1.0)
        # result power 1 = today's raw weight in that draw: the unscored rows
        # go back to 1.0 -> 4 + 1 + 2 x 1.0 = 7
        assert ball == pytest.approx(1.0 / 7.0, abs=0.02)
        assert foul == pytest.approx(2.0 / 7.0, abs=0.02)


def _matrix_sampler(pool: HandPool, *, cell: bool, batter_power: float = 1.0) -> FullPoolSampler:
    """The batter MATRIX: batter 200 (live) scores 1.0 on its own rows and
    0.4 on batter 201's; batter 202 has an embedding row but no column."""
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={_LIVE: {_LIVE: 1.0}},
        pitcher_sim_index={_LIVE: 0},
        bb_pools={},
        actor_emb={
            "batter": {
                "key_index": {"200:2024": 0, "201:2024": 1, "202:2024": 2},
                "vecs": np.zeros((3, 2), dtype=np.float32),
                "mean": np.zeros(2, dtype=np.float32),
                "std": np.ones(2, dtype=np.float32),
            }
        },
        actor_sim={
            "batter": {
                "index": {"200:2024": 0, "201:2024": 1},
                "matrix": np.array([[1.0, 0.4], [0.4, 1.0]], dtype=np.float32),
            }
        },
    )
    fp = FullPoolSampler(art, np.random.default_rng(0))
    fp.actor_matrices = True
    fp.actor_power = {"batter": batter_power}
    if cell:
        fp.pitch_cell_index = True
        fp.pitch_min_cell = 0
    return fp


_BATTERS3 = [200, 200, 200, 200, 201, 201, 202, 202]


class TestBatterMatrixOnBothPaths:
    @pytest.mark.parametrize("cell", [False, True])
    def test_the_matrix_reaches_the_cell_path(self, cell):
        fp = _matrix_sampler(_pool([100] * 8, _BATTERS3, _THREE_OUTS), cell=cell)
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[:4] == pytest.approx(1.0) and w[4:6] == pytest.approx(0.4)
        assert w[6:] == pytest.approx(1.0)  # unscored at power 1: as is

    @pytest.mark.parametrize("cell", [False, True])
    def test_the_batter_power_with_the_neutral_rule(self, cell):
        fp = _matrix_sampler(_pool([100] * 8, _BATTERS3, _THREE_OUTS), cell=cell)
        fp.pitch_batter_power = 2.0
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        # scored: 4 at 1.0, 2 at 0.16 -> mean 0.72
        assert w[:4] == pytest.approx(1.0) and w[4:6] == pytest.approx(0.16)
        assert w[6:] == pytest.approx(0.72)

    @pytest.mark.parametrize("cell", [False, True])
    def test_power_zero_keeps_the_kernel(self, cell):
        fp = _matrix_sampler(_pool([100] * 8, _BATTERS3, _THREE_OUTS), cell=cell, batter_power=0.0)
        fp._batter_affinity = lambda key: np.array([1.0, 0.5, 0.25], dtype=np.float32)
        fp.new_half_inning("R", _LIVE)
        fp.new_plate_appearance(_BATTER, _BASE_OUT)
        w = _weights(fp)
        assert w[4:6] == pytest.approx(0.5) and w[6:] == pytest.approx(0.25)
        assert not fp._batter_matrix_on()

    def test_the_fielder_matrices_honour_power_zero(self):
        fp = _matrix_sampler(_pool([100] * 8, _BATTERS3, _THREE_OUTS), cell=False)
        fp.a.actor_sim["fielder_SS"] = {"index": {}, "matrix": np.zeros((0, 0), dtype=np.float32)}
        assert fp._fielder_matrices_on()
        fp.actor_power["fielder"] = 0.0
        assert not fp._fielder_matrices_on()

    def test_the_matrix_gather_neutral_rule_and_power_zero(self):
        fp = _matrix_sampler(_pool([100] * 8, _BATTERS3, _THREE_OUTS), cell=False)
        rows = np.array([0, 1, 2, -1])  # embedding rows: 200, 201, 202 (no column), absent
        fp.actor_power = {"batter": 2.0}
        f = fp._matrix_gather("batter", "200:2024", rows)
        assert f[0] == pytest.approx(1.0) and f[1] == pytest.approx(0.16)
        assert f[2] == pytest.approx(0.58) and f[3] == pytest.approx(0.58)  # the mean scored
        fp.actor_power = {"batter": 0.0}
        assert fp._matrix_gather("batter", "200:2024", rows) is None


# ---------------------------------------------------------------------------
# SIM-523 part F, batch 3: the runner kernels' own bandwidths.
# ---------------------------------------------------------------------------

from pipeline.batch.engine_artifacts import AdvancementPool, StealPool  # noqa: E402


def _steal_pool() -> StealPool:
    n = 2
    return StealPool(
        sit=np.zeros((n, 4), dtype=np.float32),
        runner_id=np.array([1, 1], dtype=np.int64),
        pitcher_id=np.array([2, 2], dtype=np.int64),
        catcher_id=np.array([3, 3], dtype=np.int64),
        season=np.full(n, _SEASON, dtype=np.int64),
        attempted=np.array([0, 1], dtype=np.int8),
        success=np.array([0, 1], dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
        pickoff_out=np.zeros(n, dtype=np.int8),
        pickoff_advancing=np.zeros(n, dtype=np.int8),
        pickoff_error=np.zeros(n, dtype=np.int8),
    )


def _adv_pool() -> AdvancementPool:
    n = 2
    return AdvancementPool(
        feat=np.array(
            [[90.0, 10.0, 0.0, 200.0, 0.0], [80.0, 5.0, 0.0, 150.0, 1.0]], dtype=np.float32
        ),
        runner_id=np.array([1, 1], dtype=np.int64),
        fielder_id=np.zeros(n, dtype=np.int64),
        fielder_pos=np.zeros(n, dtype=np.int8),
        season=np.full(n, _SEASON, dtype=np.int64),
        attempted=np.array([0, 1], dtype=np.int8),
        safe=np.array([0, 1], dtype=np.int8),
        error_extra=np.zeros(n, dtype=np.int8),
        recency=np.ones(n, dtype=np.float32),
    )


class _SigmaRecorder:
    """Stands in for ``_steal_actor_factor``: records the bandwidth each actor
    call carries and returns a neutral factor."""

    def __init__(self) -> None:
        self.calls: dict[str, object] = {}

    def __call__(self, actor, live_key, emb_rows_all, rows, feat_names, sigma=None, matrix=None):
        self.calls[actor] = sigma
        return np.ones(len(rows), dtype=np.float32)


class TestRunnerKernelBandwidths:
    def _sampler(self) -> FullPoolSampler:
        art = EngineArtifacts(
            pools={"R": _pool(_HALF, _SAME_BATTER, _OUTS)},
            pitcher_sim={_LIVE: {_LIVE: 1.0}},
            pitcher_sim_index={_LIVE: 0},
            bb_pools={},
            actor_emb={},
            steal_pools={"2": _steal_pool()},
            adv_pools={"1_1_3": _adv_pool()},
        )
        return FullPoolSampler(art, np.random.default_rng(0))

    def test_the_defaults_leave_the_shared_bandwidths(self):
        fp = self._sampler()
        assert fp.steal_runner_sigma is None and fp.adv_runner_sigma is None
        rec = _SigmaRecorder()
        fp._steal_actor_factor = rec
        assert fp.steal_draw(
            2, "1:2024", "2:2024", "3:2024", outs=0, balls=0, strikes=0, score_diff=0
        )
        assert rec.calls["baserunner"] is None  # None -> steal_sigma inside the factor
        rec = _SigmaRecorder()
        fp._steal_actor_factor = rec
        out = fp.advancement_draw(
            1,
            1,
            3,
            "1:2024",
            None,
            outs=0,
            exit_velo=90.0,
            launch_angle=10.0,
            spray_angle=0.0,
            hit_distance=200.0,
        )
        assert out is not None
        assert rec.calls["baserunner"] == fp.adv_sigma

    def test_the_runner_bandwidths_reach_their_draws(self):
        fp = self._sampler()
        fp.steal_runner_sigma = 0.25
        fp.adv_runner_sigma = 0.35
        rec = _SigmaRecorder()
        fp._steal_actor_factor = rec
        fp.steal_draw(2, "1:2024", "2:2024", "3:2024", outs=0, balls=0, strikes=0, score_diff=0)
        assert rec.calls["baserunner"] == 0.25
        assert rec.calls.get("pitcher_steal") is None and rec.calls.get("catcher") is None
        rec = _SigmaRecorder()
        fp._steal_actor_factor = rec
        fp.advancement_draw(
            1,
            1,
            3,
            "1:2024",
            None,
            outs=0,
            exit_velo=90.0,
            launch_angle=10.0,
            spray_angle=0.0,
            hit_distance=200.0,
        )
        assert rec.calls["baserunner"] == 0.35

    def test_the_factory_reads_the_runner_bandwidths(self):
        from simulation.production_factory import apply_actor_matrix_env

        fp = self._sampler()
        apply_actor_matrix_env(
            fp, {"SIM_STEAL_RUNNER_SIGMA": "0.25", "SIM_ADV_RUNNER_SIGMA": "0.3"}
        )
        assert fp.steal_runner_sigma == 0.25 and fp.adv_runner_sigma == 0.3
        apply_actor_matrix_env(fp, {"SIM_STEAL_RUNNER_SIGMA": "", "SIM_ADV_RUNNER_SIGMA": "junk"})
        assert fp.steal_runner_sigma is None and fp.adv_runner_sigma is None
        apply_actor_matrix_env(fp, {})
        assert fp.steal_runner_sigma is None and fp.adv_runner_sigma is None
