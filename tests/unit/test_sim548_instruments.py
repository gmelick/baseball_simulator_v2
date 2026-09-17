"""
SIM-548 — the joint-fit plan's instruments, on synthetic inputs (no DB, no bundle):

  * the market skill table (scripts/sim548_market_skill.py): Brier, base rate,
    AUC, bias and the overround exclusion on a toy market;
  * the calibration layer (scripts/sim548_calibrate_markets.py): the logistic
    map recovers a known shift and shrink, a wrong-way market collapses to the
    base rate, and a map is written only when the check set says it helped;
  * the designed experiment (scripts/sim548_design.py): the fractional
    factorial is balanced, orthogonal and of resolution IV, and the read
    recovers a planted main effect from synthetic reports;
  * the offline joint fit (scripts/sim548_offline_fit.py): the jar it reads is
    the sampler's own — the weights it takes off the count bucket's CDF equal
    the weights the split's result draw starts from, on the synthetic bundle.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


skill = _load("sim548_market_skill")
calib = _load("sim548_calibrate_markets")
design = _load("sim548_design")


# ---------------------------------------------------------------------------
# the skill table
# ---------------------------------------------------------------------------


def _records(
    n: int,
    market: str,
    rng: np.random.Generator,
    *,
    sim_shift: float = 0.0,
    side=-110.0,
    other=-110.0,
):
    p_true = rng.uniform(0.2, 0.8, size=n)
    y = (rng.uniform(size=n) < p_true).astype(int)
    return [
        {
            "game_pk": 1000 + i // 4,
            "market": market,
            "market_type": "prop",
            "sim_prob": float(np.clip(p_true[i] + sim_shift, 0.01, 0.99)),
            "market_prob": float(p_true[i]),
            "outcome": int(y[i]),
            "player_id": i,
            "market_side_price": side,
            "market_other_price": other,
        }
        for i in range(n)
    ]


def test_skill_table_reads_a_calibrated_and_a_shifted_market():
    rng = np.random.default_rng(1)
    recs = _records(4000, "H", rng) + _records(4000, "TB", rng, sim_shift=0.10)
    rows = {r["market"]: r for r in skill.market_rows(recs, min_n=100, n_boot=200, seed=1)}
    h, tb = rows["H"], rows["TB"]
    # the calibrated market: no bias, the sim equals the line, the gap's range covers zero
    assert abs(h["bias_sim"]) < 0.02
    assert h["gap_lo"] <= 0.0 <= h["gap_hi"]
    assert h["auc_sim"] > 0.6 and h["auc_mkt"] > 0.6
    assert h["line_ok"] and h["enters"]
    # the shifted market: the bias shows and the sim reads behind the line
    assert 0.07 < tb["bias_sim"] < 0.13
    assert tb["gap_lo"] > 0.0
    # the base-rate reference is p(1-p) of the outcome rate
    assert tb["brier_base"] == pytest.approx(tb["outcome_rate"] * (1 - tb["outcome_rate"]))


def test_skill_table_excludes_a_market_whose_prices_are_not_a_pair():
    rng = np.random.default_rng(2)
    # +350 and +150: two long shots, not two sides of one bet (the first-five run line, SIM-549)
    recs = _records(400, "f5_runline", rng, side=350.0, other=150.0)
    row = skill.market_rows(recs, min_n=100, n_boot=50, seed=2)[0]
    assert row["overround"] == pytest.approx(100 / 450 + 100 / 250, abs=1e-6)
    assert not row["line_ok"]
    assert not row["enters"]


def test_auc_is_half_for_a_flat_forecast_and_one_for_a_perfect_one():
    y = np.array([0, 1, 0, 1, 1, 0])
    assert skill.auc(np.full(6, 0.5), y) == pytest.approx(0.5)
    assert skill.auc(y.astype(float), y) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# the calibration layer
# ---------------------------------------------------------------------------


def test_logistic_map_recovers_a_shift_and_a_shrink():
    rng = np.random.default_rng(3)
    n = 20000
    # the truth: logit(p_true) = -0.4 + 0.5 * logit(p_sim)  (the sim over-spread and shifted)
    p_sim = rng.uniform(0.05, 0.95, size=n)
    z = -0.4 + 0.5 * np.log(p_sim / (1 - p_sim))
    p_true = 1 / (1 + np.exp(-z))
    y = (rng.uniform(size=n) < p_true).astype(float)
    a, b, converged = calib.fit_logistic(p_sim, y)
    assert converged
    assert a == pytest.approx(-0.4, abs=0.08)
    assert b == pytest.approx(0.5, abs=0.06)
    q = calib.apply_logistic(p_sim, a, b)
    assert np.mean((q - y) ** 2) < np.mean((p_sim - y) ** 2)


def test_a_wrong_way_market_collapses_to_the_base_rate_and_does_not_pass():
    rng = np.random.default_rng(4)
    n = 3000
    p_sim = rng.uniform(0.2, 0.8, size=n)
    # the outcome runs AGAINST the sim's probability
    y = (rng.uniform(size=n) < (1 - p_sim)).astype(int)
    recs = [
        {
            "game_pk": i // 5,
            "market": "X",
            "sim_prob": float(p_sim[i]),
            "market_prob": 0.5,
            "outcome": int(y[i]),
        }
        for i in range(n)
    ]
    fit, chk = recs[: n // 2], recs[n // 2 :]
    res = calib.fit_market("X", fit, chk, "logistic", np.random.default_rng(0), 100)
    assert res["params"]["collapsed_to_base_rate"] is True
    assert res["params"]["b"] == 0.0
    # the collapsed map says the fit set's outcome rate everywhere
    base = np.mean([r["outcome"] for r in fit])
    assert calib.apply_logistic(np.array([0.3, 0.7]), res["params"]["a"], 0.0) == pytest.approx(
        [base, base], abs=1e-6
    )
    # and it beats the inverted probability on the check set
    assert res["check"]["brier_after"] < res["check"]["brier_before"]


def test_isotonic_map_is_monotone_and_tracks_the_rate():
    p = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95])
    y = np.array([0, 0, 1, 0, 1, 0, 1, 1, 1, 1], dtype=float)
    edges, values = calib.fit_isotonic(p, y)
    assert np.all(np.diff(values) >= 0)
    q = calib.apply_isotonic(p, edges, values)
    assert q[0] <= q[-1]
    assert np.mean(q) == pytest.approx(np.mean(y), abs=1e-9)


# ---------------------------------------------------------------------------
# the designed experiment
# ---------------------------------------------------------------------------


def test_fractional_factorial_is_balanced_orthogonal_and_resolution_iv():
    import itertools

    for k in (2, 3, 4, 5, 6):
        d = design.fractional_factorial(k)
        assert d.shape == ((2**k if k <= 4 else 16), k)
        assert np.all(d.sum(axis=0) == 0)
        assert np.all(d.T @ d == np.eye(k, dtype=int) * d.shape[0])
        if k >= 5:
            for i in range(k):
                for a, b in itertools.combinations(range(k), 2):
                    # a main effect is never aliased with a two-way interaction
                    assert abs(int((d[:, i] * d[:, a] * d[:, b]).sum())) != d.shape[0]


def test_build_arms_names_levels_and_baseline_repeats():
    arms = design.build_arms(
        {"A": ["1", "2"], "B": ["3", "4"]}, baseline_repeats=2, base_seed=7, iterations=100
    )
    assert [a["name"] for a in arms] == [
        "run01",
        "run02",
        "run03",
        "run04",
        "baseline1",
        "baseline2",
    ]
    assert arms[0]["env"] == {"A": "1", "B": "3"}  # (-1, -1) = production levels
    assert arms[3]["env"] == {"A": "2", "B": "4"}  # (+1, +1) = the candidates
    assert arms[4]["env"] == {"A": "1", "B": "3"} and arms[4]["kind"] == "baseline"
    # the repeats stride by the iteration count: the backtest seeds iteration i
    # as base_seed + i, so neighbouring base seeds would share 99 of 100 iterations
    assert arms[5]["seed"] - arms[4]["seed"] >= 100
    assert arms[4]["seed"] - arms[0]["seed"] >= 100


def _fake_report(path: Path, games: list[int], k_shift: float, rng: np.random.Generator) -> None:
    """A report whose K market is worse by ``k_shift`` of probability; H untouched."""
    recs = []
    for g in games:
        for market, n in (("K", 2), ("H", 8)):
            for j in range(n):
                p_true = 0.5 + 0.2 * np.sin(g * 0.37 + j)
                y = int(rng.uniform() < p_true)
                p = p_true + (k_shift if market == "K" else 0.0)
                recs.append(
                    {
                        "game_pk": g,
                        "market": market,
                        "market_type": "prop",
                        "sim_prob": float(np.clip(p, 0.01, 0.99)),
                        "market_prob": float(p_true),
                        "outcome": y,
                        "player_id": j,
                    }
                )
    path.write_text(json.dumps({"params": {}, "accuracy_records": recs}), encoding="utf-8")


def test_design_read_recovers_a_planted_main_effect(tmp_path: Path):
    """Factor A (high level) worsens the strikeout market by 0.2 of probability;
    factor B does nothing. The read must show A's K effect positive and clear
    of zero, B's inside its range, and H untouched."""
    arms = design.build_arms({"A": ["0", "1"], "B": ["0", "1"]}, baseline_repeats=2, base_seed=0)
    games = list(range(1, 301))
    for arm in arms:
        shift = 0.2 if arm["env"]["A"] == "1" else 0.0
        _fake_report(tmp_path / f"{arm['name']}.json", games, shift, np.random.default_rng(5))
    res = design.analyze(arms, tmp_path, min_n=10, n_boot=100, seed=1, weights=None)
    k_eff = res["markets"]["K"]["effects"]
    assert k_eff["A"]["effect"] > 0.0 and k_eff["A"]["lo"] > 0.0
    assert k_eff["B"]["lo"] <= 0.0 <= k_eff["B"]["hi"]
    h_eff = res["markets"]["H"]["effects"]
    assert abs(h_eff["A"]["effect"]) < 1e-9
    assert res["best_corner_by_composite"]["A"] == "low"
    assert res["markets"]["K"]["noise_floor_sd"] == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# the offline fit's jar is the sampler's own
# ---------------------------------------------------------------------------


def test_offline_fit_reads_the_same_weights_the_result_draw_starts_from():
    """On a synthetic bundle with the split on, the weights the offline fit
    takes off the count bucket's CDF equal the sampler's own bucket weights,
    and result_weights returns a finite weight per candidate row."""
    from pipeline.batch.engine_artifacts import EngineArtifacts, HandPool
    from simulation.full_pool_sampler import FullPoolSampler

    n = 8
    fast = np.array([95.0, 15.0, 5.0, 2300.0, 200.0, -1.0, 6.0, 6.5, 0.0, 2.5], dtype=np.float32)
    curve = np.array([78.0, -8.0, 8.0, 2600.0, 40.0, -1.0, 6.0, 6.0, 0.3, 1.5], dtype=np.float32)
    sit = np.zeros((n, 6), dtype=np.float32)
    sit[:, 4] = 5.0
    pool = HandPool(
        geom=np.stack([fast] * 4 + [curve] * 4).astype(np.float32),
        sit=sit,
        pitcher_id=np.full(n, 100, dtype=np.int64),
        batter_id=np.full(n, 200, dtype=np.int64),
        season=np.full(n, 2024, dtype=np.int64),
        outcome_type=np.asarray(["swinging_strike"] * 4 + ["ball"] * 4, dtype=object),
        recency=np.ones(n, dtype=np.float32),
    )
    art = EngineArtifacts(
        pools={"R": pool},
        pitcher_sim={"100:2024": {"100:2024": 1.0}},
        pitcher_sim_index={"100:2024": 0},
        bb_pools={},
        actor_emb={},
        actor_sim=None,
    )
    fp = FullPoolSampler(art, np.random.default_rng(0))
    fp.pitch_result_split = True
    fp.result_pitch_sigma = 0.5
    fp.new_half_inning("R", "100:2024", None)
    fp.new_plate_appearance("200:2024", np.zeros(4, dtype=np.float32))
    b = 0
    cdf = np.asarray(fp._bucket_cdf[b], dtype=np.float64)
    w_from_cdf = np.diff(cdf, prepend=0.0)
    w_bucket = np.asarray(fp._bucket_w[b], dtype=np.float64)
    assert w_from_cdf == pytest.approx(w_bucket, rel=1e-5)
    rows = fp._pa_rows[b] if fp._pa_rows is not None else fp._pool_meta("R")["bucket_rows"][b]
    wr = fp.result_weights(b, rows, int(rows[0]))
    assert wr is not None and wr.shape == (rows.size,)
    assert np.all(np.isfinite(wr)) and wr.sum() > 0
    # anchored on a fastball, the fastball rows carry more of the result weight
    fast_rows = np.asarray([i < 4 for i in rows])
    assert wr[fast_rows].sum() > wr[~fast_rows].sum()
