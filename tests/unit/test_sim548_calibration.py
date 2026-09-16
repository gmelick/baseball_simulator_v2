"""
SIM-548 — the calibration layer's fit and the composite objective, after the
2026-09-15 fixes (no DB, no bundle):

  * fit_logistic takes Newton steps with step-halving and reports a converged
    flag; it recovers a shrink of 0.15 (the strikeout market's own regime),
    where a plain Newton step diverged;
  * a fit that cannot converge (outcomes all one value) reports
    converged=False, and fit_market never passes it;
  * fit_isotonic groups tied probabilities, so the map does not depend on the
    record order;
  * fit_market scores the moneyline's BEFORE figures on the probability
    production shows (sim_prob) and fits the map on the raw share
    (sim_prob_raw);
  * composite() gives Z a percentile range and drops a zero-standard-error
    market from both the sum and the normaliser.
"""

from __future__ import annotations

import importlib.util
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


calib = _load("sim548_calibrate_markets")
skill = _load("sim548_market_skill")


# ---------------------------------------------------------------------------
# (a) the logistic fit converges on an over-spread market
# ---------------------------------------------------------------------------


def test_logistic_fit_recovers_a_strong_shrink_and_reports_converged():
    rng = np.random.default_rng(11)
    n = 20000
    a_true, b_true = -0.4, 0.15
    p_sim = rng.uniform(0.02, 0.98, size=n)
    z = a_true + b_true * np.log(p_sim / (1 - p_sim))
    p_true = 1 / (1 + np.exp(-z))
    y = (rng.uniform(size=n) < p_true).astype(float)
    a, b, converged = calib.fit_logistic(p_sim, y)
    assert converged is True
    assert a == pytest.approx(a_true, abs=0.06)
    assert b == pytest.approx(b_true, abs=0.04)
    q = calib.apply_logistic(p_sim, a, b)
    assert np.mean((q - y) ** 2) < np.mean((p_sim - y) ** 2)


def test_logistic_fit_never_takes_a_downhill_step():
    """Every accepted step keeps the log-likelihood from falling: the fitted
    coefficients score at least the starting point (a=0, b=1)."""
    rng = np.random.default_rng(12)
    n = 5000
    p_sim = rng.uniform(0.02, 0.98, size=n)
    z = 0.3 + 0.2 * np.log(p_sim / (1 - p_sim))
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-z))).astype(float)
    x = calib._logit(p_sim)
    X = np.column_stack([np.ones_like(x), x])
    a, b, converged = calib.fit_logistic(p_sim, y)
    assert converged
    ll_start = calib._log_likelihood(X, np.array([0.0, 1.0]), y)
    ll_end = calib._log_likelihood(X, np.array([a, b]), y)
    assert ll_end >= ll_start


# ---------------------------------------------------------------------------
# (b) an unconverged fit never passes
# ---------------------------------------------------------------------------


def test_a_degenerate_outcome_reports_unconverged_and_does_not_pass():
    rng = np.random.default_rng(13)
    n = 2000
    p_sim = rng.uniform(0.2, 0.8, size=n)
    y = np.ones(n)
    _, _, converged = calib.fit_logistic(p_sim, y)
    assert converged is False
    recs = [
        {
            "game_pk": i // 5,
            "market": "K",
            "sim_prob": float(p_sim[i]),
            "market_prob": 0.5,
            "outcome": 1,
        }
        for i in range(n)
    ]
    fit, chk = recs[: n // 2], recs[n // 2 :]
    res = calib.fit_market("K", fit, chk, "logistic", np.random.default_rng(0), 50)
    assert res["params"]["converged"] is False
    assert res["converged"] is False
    assert res["passes"] is False
    # the collapse rule stays for a CONVERGED negative slope only
    assert res["params"]["collapsed_to_base_rate"] is False


def test_the_iteration_limit_reports_unconverged():
    """With no room to iterate the fit cannot reach the step tolerance."""
    rng = np.random.default_rng(14)
    n = 3000
    p_sim = rng.uniform(0.05, 0.95, size=n)
    z = -0.4 + 0.15 * np.log(p_sim / (1 - p_sim))
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-z))).astype(float)
    _, _, converged = calib.fit_logistic(p_sim, y, max_iters=1)
    assert converged is False


# ---------------------------------------------------------------------------
# (c) the isotonic map does not depend on the record order
# ---------------------------------------------------------------------------


def test_isotonic_map_is_the_same_whatever_the_order_of_tied_records():
    rng = np.random.default_rng(15)
    n = 4000
    # a 0.01 grid: many records share one p
    p = np.round(rng.uniform(0.05, 0.95, size=n), 2)
    y = (rng.uniform(size=n) < p).astype(float)
    edges_a, values_a = calib.fit_isotonic(p, y)
    perm = rng.permutation(n)
    edges_b, values_b = calib.fit_isotonic(p[perm], y[perm])
    assert np.array_equal(edges_a, edges_b)
    assert np.allclose(values_a, values_b)
    assert np.all(np.diff(values_a) >= 0)
    grid = np.linspace(0.0, 1.0, 101)
    assert np.allclose(
        calib.apply_isotonic(grid, edges_a, values_a),
        calib.apply_isotonic(grid, edges_b, values_b),
    )
    # tied records share one block value, and the map preserves the mean
    q = calib.apply_isotonic(p, edges_a, values_a)
    assert np.mean(q) == pytest.approx(np.mean(y), abs=1e-9)


# ---------------------------------------------------------------------------
# (d) the moneyline: BEFORE on sim_prob, the fit on sim_prob_raw
# ---------------------------------------------------------------------------


def test_moneyline_scores_before_on_the_shown_probability_and_fits_on_the_raw():
    rng = np.random.default_rng(16)
    n = 3000
    raw = rng.uniform(0.2, 0.8, size=n)
    shown = 0.5 + 0.5 * (raw - 0.5)  # the flattened copy production shows
    y = (rng.uniform(size=n) < raw).astype(int)
    recs = [
        {
            "game_pk": i,
            "market": "moneyline",
            "sim_prob": float(shown[i]),
            "sim_prob_raw": float(raw[i]),
            "market_prob": float(raw[i]),
            "outcome": int(y[i]),
        }
        for i in range(n)
    ]
    fit, chk = recs[: n // 2], recs[n // 2 :]
    res = calib.fit_market("moneyline", fit, chk, "logistic", np.random.default_rng(0), 100)
    y_chk = y[n // 2 :].astype(float)
    shown_chk, raw_chk = shown[n // 2 :], raw[n // 2 :]
    c = res["check"]
    # BEFORE is what production shows, not the raw share
    assert c["brier_before"] == pytest.approx(np.mean((shown_chk - y_chk) ** 2))
    assert c["brier_before"] != pytest.approx(np.mean((raw_chk - y_chk) ** 2))
    assert c["bias_before"] == pytest.approx(shown_chk.mean() - y_chk.mean())
    assert c["spread_before"] == pytest.approx(shown_chk.std())
    assert res["fit"]["brier_before"] == pytest.approx(
        np.mean((shown[: n // 2] - y[: n // 2]) ** 2)
    )
    # the map is fitted on the raw share, which is calibrated: a ~ 0, b ~ 1
    a, b = res["params"]["a"], res["params"]["b"]
    assert res["params"]["converged"] is True
    assert a == pytest.approx(0.0, abs=0.15)
    assert b == pytest.approx(1.0, abs=0.15)
    # AFTER is the map on the raw share, and it beats the flattened copy
    q = calib.apply_logistic(raw_chk, a, b)
    assert c["brier_after"] == pytest.approx(np.mean((q - y_chk) ** 2))
    assert c["brier_after"] < c["brier_before"]
    assert c["map_gain"] == pytest.approx(c["brier_after"] - c["brier_before"])


# ---------------------------------------------------------------------------
# (e) the composite: a percentile range, a zero-se market excluded
# ---------------------------------------------------------------------------


def _paired_reads(n_games: int, rng: np.random.Generator):
    """Two reads on the same games: market K differs between them, market H
    is identical in both (zero standard error)."""
    base, arm = [], []
    for g in range(n_games):
        for market, n in (("K", 2), ("H", 3)):
            for j in range(n):
                p_true = float(rng.uniform(0.3, 0.7))
                y = int(rng.uniform() < p_true)
                p_base = float(np.clip(p_true + rng.normal(0, 0.05), 0.01, 0.99))
                p_arm = p_base if market == "H" else float(np.clip(p_true + 0.15, 0.01, 0.99))
                common = {
                    "game_pk": g,
                    "market": market,
                    "market_type": "prop",
                    "market_prob": p_true,
                    "outcome": y,
                    "player_id": j,
                }
                base.append({**common, "sim_prob": p_base})
                arm.append({**common, "sim_prob": p_arm})
    return base, arm


def test_composite_uses_a_percentile_range_and_excludes_a_zero_se_market():
    rng = np.random.default_rng(17)
    base, arm = _paired_reads(300, rng)
    c = skill.composite(base, arm, min_n=10, n_boot=400, seed=17, weights=None)
    assert set(c["markets_entering"]) == {"K", "H"}
    assert c["excluded_zero_se"] == ["H"]
    assert c["per_market"]["H"]["se"] == 0.0
    # Z is K's z alone: the normaliser holds K's weight only
    k = c["per_market"]["K"]
    assert c["Z"] == pytest.approx(k["delta"] / k["se"])
    assert c["Z"] > 0.0  # the arm is worse on K
    # the range is a percentile range of Z's own draws, not +-1.96 sd
    assert c["Z_lo"] < c["Z"] < c["Z_hi"]
    assert c["Z_lo"] != pytest.approx(c["Z"] - 1.96 * c["Z_se"], abs=1e-9) or c[
        "Z_hi"
    ] != pytest.approx(c["Z"] + 1.96 * c["Z_se"], abs=1e-9)
    assert c["Z_lo"] > 0.0
    assert c["worse_beyond_range"] == ["K"]


def test_composite_range_brackets_the_point_when_the_reads_agree_on_average():
    """Two reads that differ by noise only: Z's percentile range covers zero."""
    rng = np.random.default_rng(18)
    base, arm = [], []
    for g in range(300):
        for j in range(3):
            p_true = float(rng.uniform(0.3, 0.7))
            y = int(rng.uniform() < p_true)
            common = {
                "game_pk": g,
                "market": "K",
                "market_type": "prop",
                "market_prob": p_true,
                "outcome": y,
                "player_id": j,
            }
            base.append(
                {**common, "sim_prob": float(np.clip(p_true + rng.normal(0, 0.05), 0.01, 0.99))}
            )
            arm.append(
                {**common, "sim_prob": float(np.clip(p_true + rng.normal(0, 0.05), 0.01, 0.99))}
            )
    c = skill.composite(base, arm, min_n=10, n_boot=400, seed=18, weights=None)
    assert c["excluded_zero_se"] == []
    assert c["Z_lo"] <= 0.0 <= c["Z_hi"]
