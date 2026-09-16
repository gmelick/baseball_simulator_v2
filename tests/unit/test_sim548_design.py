"""
SIM-548 — the designed-experiment runner (scripts/sim548_design.py), on
synthetic reports (no DB, no bundle). Four defects the first read found:

  * a sparse market must not shrink a dense market's common game set;
  * the production repeats are baseline arms with their own seeds, and the
    curvature exists only when true centre arms exist;
  * at six factors the aliased two-way products are fitted as ONE column per
    alias group, named by the group;
  * a fractional design with a factorial report missing is refused.
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


design = _load("sim548_design")


def _report(
    path: Path,
    rng: np.random.Generator,
    markets: dict[str, tuple[list[int], int, float]],
) -> None:
    """Write a report. ``markets`` maps a market to (its games, records per
    game, the probability shift the arm adds to that market)."""
    recs = []
    for market, (games, n, shift) in markets.items():
        for g in games:
            for j in range(n):
                p_true = 0.5 + 0.2 * np.sin(g * 0.37 + j)
                y = int(rng.uniform() < p_true)
                recs.append(
                    {
                        "game_pk": g,
                        "market": market,
                        "market_type": "prop",
                        "sim_prob": float(np.clip(p_true + shift, 0.01, 0.99)),
                        "market_prob": float(p_true),
                        "outcome": y,
                        "player_id": j,
                    }
                )
    path.write_text(json.dumps({"params": {}, "accuracy_records": recs}), encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) a sparse market beside a dense one
# ---------------------------------------------------------------------------


def test_sparse_market_does_not_zero_the_dense_market(tmp_path: Path):
    """The first-inning total has ~25 records on a few games; the hits market
    has 300 games. Factor A worsens the hits market. The read must grade the
    hits market on all 300 of its games and show A's effect clear of zero."""
    arms = design.build_arms({"A": ["0", "1"], "B": ["0", "1"]}, baseline_repeats=2, base_seed=0)
    dense_games = list(range(1, 301))
    for r, arm in enumerate(arms):
        shift = 0.2 if arm["env"]["A"] == "1" else 0.0
        # the sparse market's games differ per arm: three shared, two of its own
        sparse_games = [1, 2, 3, 400 + r, 500 + r]
        _report(
            tmp_path / f"{arm['name']}.json",
            np.random.default_rng(5),
            {"H": (dense_games, 8, shift), "f1_total": (sparse_games, 5, 0.0)},
        )
    res = design.analyze(arms, tmp_path, min_n=10, n_boot=100, seed=1, weights=None)
    h = res["markets"]["H"]
    assert h["n_games"] == 300
    assert h["n_records"] == 300 * 8
    assert h["effects"]["A"]["effect"] > 0.0 and h["effects"]["A"]["lo"] > 0.0
    assert h["effects"]["A"]["hi"] > h["effects"]["A"]["lo"]
    # the sparse market reads on its own three common games
    assert res["markets"]["f1_total"]["n_games"] == 3
    assert res["games_per_market"] == {"H": 300, "f1_total": 3}


def test_a_market_with_no_common_games_is_skipped(tmp_path: Path):
    arms = design.build_arms({"A": ["0", "1"]}, baseline_repeats=1, base_seed=0)
    for r, arm in enumerate(arms):
        _report(
            tmp_path / f"{arm['name']}.json",
            np.random.default_rng(r),
            {"H": (list(range(1, 51)), 4, 0.0), "f1_total": ([900 + r], 5, 0.0)},
        )
    res = design.analyze(arms, tmp_path, min_n=5, n_boot=20, seed=1, weights=None)
    assert "f1_total" not in res["markets"]
    assert res["skipped_markets"]["f1_total"] == "no common games"
    assert res["markets"]["H"]["n_games"] == 50


# ---------------------------------------------------------------------------
# (b) baseline arms, centre arms, curvature
# ---------------------------------------------------------------------------


def test_baseline_seeds_differ_from_the_factorial_seed():
    arms = design.build_arms(
        {"A": ["1", "2"], "B": ["3", "4"]}, baseline_repeats=3, base_seed=7, centre_arms=0
    )
    names = [a["name"] for a in arms]
    assert names == ["run01", "run02", "run03", "run04", "baseline1", "baseline2", "baseline3"]
    factorial_seeds = {a["seed"] for a in arms if a["kind"] == "factorial"}
    baseline_seeds = [a["seed"] for a in arms if a["kind"] == "baseline"]
    assert factorial_seeds == {7}
    # the repeats stride by the iteration count (the backtest seeds iteration i as
    # base_seed + i), from an offset past the factorial's own hundred seeds
    assert baseline_seeds == [100_007, 100_107, 100_207]
    assert not set(baseline_seeds) & factorial_seeds
    assert len(set(baseline_seeds)) == 3
    # every baseline runs production: the low level of every factor
    assert all(a["env"] == {"A": "1", "B": "3"} for a in arms if a["kind"] == "baseline")


def test_centre_arms_need_a_mid_level_on_every_factor():
    with pytest.raises(SystemExit, match="mid level"):
        design.build_arms({"A": ["1", "2", "1.5"], "B": ["3", "4"]}, 1, 0, centre_arms=1)
    arms = design.build_arms({"A": ["1", "2", "1.5"], "B": ["3", "4", "3.5"]}, 1, 0, centre_arms=2)
    centres = [a for a in arms if a["kind"] == "centre"]
    assert [a["name"] for a in centres] == ["centre1", "centre2"]
    assert [a["seed"] for a in centres] == [200_000, 200_100]
    assert all(a["env"] == {"A": "1.5", "B": "3.5"} for a in centres)
    assert all(a["coded"] == [0, 0] for a in centres)


def test_curvature_is_none_without_centre_arms_and_present_with_them(tmp_path: Path):
    """Factor A at its high level worsens the strikeout market by 0.2; at its
    mid level by 0.1. Without centre arms the curvature is None. With them,
    it is the corner mean minus the centre mean, with a bootstrap range."""
    factors = {"A": ["0", "1", "0.5"], "B": ["0", "1", "0.5"]}
    games = list(range(1, 201))

    def write(arms: list[dict], out: Path) -> None:
        out.mkdir(exist_ok=True)
        for arm in arms:
            shift = {"0": 0.0, "0.5": 0.1, "1": 0.2}[arm["env"]["A"]]
            _report(
                out / f"{arm['name']}.json",
                np.random.default_rng(5),
                {"K": (games, 3, shift)},
            )

    no_centre = design.build_arms(factors, baseline_repeats=2, base_seed=0, centre_arms=0)
    write(no_centre, tmp_path / "a")
    res = design.analyze(no_centre, tmp_path / "a", min_n=10, n_boot=50, seed=1, weights=None)
    k = res["markets"]["K"]
    assert k["curvature"] is None and k["curvature_lo"] is None and k["curvature_hi"] is None
    assert k["centre_means"] == {}
    assert set(k["baseline_means"]) == {"baseline1", "baseline2"}
    # the same report under every baseline seed: the noise floor reads zero
    assert k["noise_floor_sd"] == pytest.approx(0.0, abs=1e-12)

    with_centre = design.build_arms(factors, baseline_repeats=2, base_seed=0, centre_arms=2)
    write(with_centre, tmp_path / "b")
    res = design.analyze(with_centre, tmp_path / "b", min_n=10, n_boot=50, seed=1, weights=None)
    k = res["markets"]["K"]
    assert k["curvature"] is not None
    assert set(k["centre_means"]) == {"centre1", "centre2"}
    corners = np.mean(list(k["arm_means"].values()))
    centres = np.mean(list(k["centre_means"].values()))
    assert k["curvature"] == pytest.approx(corners - centres)
    assert k["curvature_lo"] - 1e-9 <= k["curvature"] <= k["curvature_hi"] + 1e-9
    # a shift of 0.2 on half the corners vs 0.1 at the centre: the Brier curves upward
    assert k["curvature"] > 0.0


def test_an_old_design_json_reads_its_centre_repeats_as_baselines():
    old = {"name": "centre1", "coded": [0, 0], "env": {}, "seed": 0, "centre": False}
    assert design.arm_kind(old) == "factorial"
    old["centre"] = True
    assert design.arm_kind(old) == "baseline"
    assert design.arm_kind({"kind": "centre"}) == "centre"


# ---------------------------------------------------------------------------
# (c) alias groups at six factors
# ---------------------------------------------------------------------------


def _six_factors() -> dict[str, list[str]]:
    return {n: ["0", "1"] for n in "ABCDEF"}


def test_alias_groups_at_six_factors_are_six_pairs_and_one_triple():
    X = design.fractional_factorial(6).astype(float)
    names, cols, dropped = design.alias_groups(X, list("ABCDEF"))
    assert dropped == []
    assert len(names) == 7 and len(cols) == 7
    sizes = sorted(n.count("=") + 1 for n in names)
    assert sizes == [2, 2, 2, 2, 2, 2, 3]
    assert "A×E=B×C=D×F" in names
    assert "A×B=C×E" in names
    # one column per group, and the columns are mutually orthogonal
    M = np.column_stack(cols)
    assert np.allclose(M.T @ M, np.eye(7) * 16)


def test_alias_groups_at_four_factors_are_the_six_plain_products():
    X = design.fractional_factorial(4).astype(float)
    names, cols, dropped = design.alias_groups(X, list("ABCD"))
    assert dropped == []
    assert names == ["A×B", "A×C", "A×D", "B×C", "B×D", "C×D"]


def test_planted_interaction_lands_on_its_alias_group(tmp_path: Path):
    """Factors A and B together (both high) worsen the strikeout market. The
    read fits one column per alias group; the group that holds A×B carries
    the effect and every group appears once in the composite."""
    arms = design.build_arms(_six_factors(), baseline_repeats=2, base_seed=0)
    assert len(arms) == 16 + 2
    games = list(range(1, 201))
    for arm in arms:
        both = arm["env"]["A"] == "1" and arm["env"]["B"] == "1"
        _report(
            tmp_path / f"{arm['name']}.json",
            np.random.default_rng(5),
            {"K": (games, 3, 0.2 if both else 0.0)},
        )
    res = design.analyze(arms, tmp_path, min_n=10, n_boot=100, seed=1, weights=None)
    groups = res["interaction_groups"]
    assert len(groups) == 7
    ab = [g for g in groups if "A×B" in g.split("=")]
    assert ab == ["A×B=C×E"]
    eff = res["markets"]["K"]["effects"]
    # the fitted terms: mean, six main effects, seven groups — no duplicates
    assert list(eff) == ["mean", *"ABCDEF", *groups]
    assert set(res["composite"]) == set(eff)
    assert eff[ab[0]]["effect"] > 0.0 and eff[ab[0]]["lo"] > 0.0
    # the other groups sit on zero
    for g in groups:
        if g != ab[0]:
            assert eff[g]["lo"] - 1e-9 <= 0.0 <= eff[g]["hi"] + 1e-9
    # A and B each carry a main effect too (the corner lifts their high-level means)
    assert eff["A"]["lo"] > 0.0 and eff["B"]["lo"] > 0.0
    assert eff["C"]["lo"] - 1e-9 <= 0.0 <= eff["C"]["hi"] + 1e-9
    # the group's effect is the whole estimable contrast, not a half of it
    assert eff[ab[0]]["effect"] == pytest.approx(eff["A"]["effect"], rel=0.05)


# ---------------------------------------------------------------------------
# (d) a fraction with a factorial report missing
# ---------------------------------------------------------------------------


def test_a_fraction_with_a_missing_factorial_report_is_refused(tmp_path: Path):
    arms = design.build_arms(_six_factors(), baseline_repeats=1, base_seed=0)
    games = list(range(1, 51))
    for arm in arms:
        if arm["name"] in ("run05", "run11"):
            continue
        _report(tmp_path / f"{arm['name']}.json", np.random.default_rng(1), {"K": (games, 3, 0.0)})
    with pytest.raises(SystemExit) as exc:
        design.analyze(arms, tmp_path, min_n=10, n_boot=10, seed=1, weights=None)
    msg = str(exc.value)
    assert "REFUSED" in msg and "run05" in msg and "run11" in msg
    assert "2^(6-2)" in msg


def test_a_full_factorial_with_a_missing_report_is_refused(tmp_path: Path):
    """Three of four 2^2 runs cannot separate the mean, two main effects and
    the interaction: least squares would smear a pure A effect over every term,
    so the read refuses instead of printing a wrong surface."""
    arms = design.build_arms({"A": ["0", "1"], "B": ["0", "1"]}, baseline_repeats=1, base_seed=0)
    games = list(range(1, 51))
    for arm in arms:
        if arm["name"] == "run04":
            continue
        _report(tmp_path / f"{arm['name']}.json", np.random.default_rng(1), {"K": (games, 3, 0.0)})
    with pytest.raises(SystemExit, match="run04"):
        design.analyze(arms, tmp_path, min_n=10, n_boot=10, seed=1, weights=None)
