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


def _two_bet_records(n_games: int, rng: np.random.Generator) -> list[dict]:
    """SIM-549: the first-five run line as the comparison now scores two
    separate bets — per game a home record and an away record, each with its
    own price and no fade price."""
    out = []
    for g in range(n_games):
        for market, price in (("f5_runline", 350.0), ("f5_runline_away", 150.0)):
            p = float(rng.uniform(0.15, 0.45))
            out.append(
                {
                    "game_pk": 5000 + g,
                    "market": market,
                    "market_type": "f5_runline",
                    "sim_prob": p,
                    "market_prob": p,
                    "outcome": int(rng.uniform() < p),
                    "player_id": None,
                    "market_side_price": price,
                    "market_other_price": None,
                }
            )
    return out


def test_skill_table_accepts_the_market_after_the_fix():
    rng = np.random.default_rng(7)
    # 150 pairs (they add to about 1.05) and 300 games of two separate bets
    pairs = _records(150, "f5_runline", rng, side=105.0, other=-125.0)
    for r in pairs:
        r["market_type"] = "f5_runline"
    recs = pairs + _two_bet_records(300, rng)
    rows = {r["market"]: r for r in skill.market_rows(recs, min_n=100, n_boot=50, seed=7)}
    home = rows["f5_runline"]
    # the one-sided records leave the margin's mean; the pairs keep it in band
    assert home["overround"] == pytest.approx(100 / 205 + 125 / 225, abs=1e-6)
    assert home["line_ok"] and home["enters"]
    # the away bet is its own row; it carries no pair, so the guard has nothing to read
    away = rows["f5_runline_away"]
    assert away["overround"] is None and away["line_ok"] and away["enters"]


def test_away_record_is_its_own_market():
    rng = np.random.default_rng(8)
    base = _two_bet_records(150, rng)
    arm = [dict(r, sim_prob=min(r["sim_prob"] + 0.05, 0.99)) for r in base]
    # the composite objective keeps the two bets of one game apart
    c = skill.composite(base, arm, min_n=100, n_boot=50, seed=8, weights=None)
    assert c["records_paired"] == 300
    assert set(c["per_market"]) == {"f5_runline", "f5_runline_away"}
    # the paired read keeps them apart too
    pair = _load("sim518_pair_accuracy")
    rows = pair.pair_records(base, arm)
    assert len(rows) == 300
    # the skill table prints the away row right after its market
    rows_t = skill._order(skill.market_rows(base, min_n=100, n_boot=20, seed=8))
    assert [r["market"] for r in rows_t] == ["f5_runline", "f5_runline_away"]
    for m in ("runline", "f5_runline", "f1_runline"):
        order = skill.GAME_MARKET_ORDER
        assert order.index(m + "_away") == order.index(m) + 1


def test_a_mis_stored_one_sided_line_is_excluded(capsys):
    """The pairs' guard cannot see a one-sided run line; the line's own
    calibration can. The 2025 first-inning +1 / +1 shape: the line says 0.465
    and the bet wins 88% of the time."""
    rng = np.random.default_rng(10)
    bad = []
    for g in range(120):
        bad.append(
            {
                "game_pk": 7000 + g,
                "market": "f1_runline",
                "market_type": "f1_runline",
                "sim_prob": float(rng.uniform(0.7, 0.95)),
                "market_prob": 0.465,
                "outcome": int(rng.uniform() < 0.885),
                "player_id": None,
                "market_side_price": -1600.0,
                "market_other_price": None,
            }
        )
    rows = skill._order(skill.market_rows(bad, min_n=100, n_boot=20, seed=10))
    row = rows[0]
    assert row["overround"] is None  # no pair, so the old guard has nothing to read
    assert row["one_sided_line"]["z"] < -skill.ONE_SIDED_LINE_Z
    assert not row["line_ok"] and not row["enters"]
    skill.print_table(rows, {}, 100)
    assert "one-sided line says 0.465" in capsys.readouterr().out
    # a calibrated one-sided line enters
    good = _two_bet_records(150, rng)
    rows = {r["market"]: r for r in skill.market_rows(good, min_n=100, n_boot=20, seed=10)}
    assert rows["f5_runline"]["line_ok"] and rows["f5_runline_away"]["line_ok"]


def test_the_skill_table_still_prints_the_pairs_note(capsys):
    rng = np.random.default_rng(3)
    recs = _records(150, "f5_runline", rng, side=350.0, other=150.0)
    rows = skill._order(skill.market_rows(recs, min_n=100, n_boot=20, seed=3))
    skill.print_table(rows, {}, 100)
    assert "LINE SUSPECT: the two prices add to 0.62" in capsys.readouterr().out


def test_a_rescored_and_a_fresh_report_do_not_merge(tmp_path: Path):
    rng = np.random.default_rng(11)
    stamp = {"base_seed": 0, "run_line_scoring": "sim549.1"}
    rescored = {
        "params": {**stamp, "rescored": {"from": "x.json"}},
        "accuracy_records": _records(10, "H", rng),
    }
    fresh = {
        "params": dict(stamp),
        "accuracy_records": [dict(r, game_pk=r["game_pk"] + 100) for r in _records(10, "H", rng)],
    }
    a, b = tmp_path / "rescored.json", tmp_path / "fresh.json"
    a.write_text(json.dumps(rescored), encoding="utf-8")
    b.write_text(json.dumps(fresh), encoding="utf-8")
    with pytest.raises(SystemExit, match="run_line_rescored"):
        skill.load_reports([str(a), str(b)], force=False)
    pair = _load("sim518_pair_accuracy")
    assert any("params.rescored" in p for p in pair.provenance_mismatches(rescored, fresh))


def test_a_stamped_and_an_unstamped_report_do_not_merge(tmp_path: Path):
    rng = np.random.default_rng(9)
    old = {"params": {"base_seed": 0}, "accuracy_records": _records(10, "H", rng)}
    new = {
        "params": {"base_seed": 0, "run_line_scoring": "sim549.1"},
        "accuracy_records": [dict(r, game_pk=r["game_pk"] + 100) for r in _records(10, "H", rng)],
    }
    a, b = tmp_path / "old.json", tmp_path / "new.json"
    a.write_text(json.dumps(old), encoding="utf-8")
    b.write_text(json.dumps(new), encoding="utf-8")
    with pytest.raises(SystemExit, match="run_line_scoring"):
        skill.load_reports([str(a), str(b)], force=False)
    records, _ = skill.load_reports([str(b)], force=False)
    assert len(records) == 10
    # the paired read names the same mismatch
    pair = _load("sim518_pair_accuracy")
    problems = pair.provenance_mismatches(old, new)
    assert any("run_line_scoring" in p for p in problems)


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
