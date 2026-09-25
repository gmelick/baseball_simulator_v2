"""
scripts/sim548_market_skill.py — SIM-548 §5.1: the per-market SKILL table of one
production read, and the composite objective between two reads.

Reads one or more ``scripts/clv_backtest.py`` JSON reports (several reports of
the SAME configuration on disjoint game sets merge into one read — the 1,000-game
baseline runs as four 250-game chunks) and prints, per market:

  n, games        the records and games that carry a closing line
  sim / mkt       the simulator's mean Brier score and the closing line's
                  (the squared gap between a probability and the 0/1 outcome)
  base            the Brier of a forecast that always says the market's own
                  outcome rate — the "knows only the average" reference
  skill           base − sim (positive = the simulator beats the average;
                  mkt-skill the same for the line)
  gap             sim − mkt with a game-clustered bootstrap range
                  (negative = the simulator beats the closing line)
  bias            mean probability − outcome rate (sim and mkt)
  spread          the standard deviation of the probabilities (sim and mkt)
  AUC             the chance that a real "yes" drew the higher probability
                  (0.5 = no discrimination; sim and mkt)
  reliability     the outcome rate inside six probability bins

With ``--compare`` (a second read of another configuration on the SAME games and
seeds) it also prints the plan's composite objective: per market the paired
Brier change Δ with its standard error s from the game-clustered bootstrap, and

    Z = Σ_m w_m · (Δ_m / s_m) / sqrt(Σ_m w_m²)

over the markets that clear ``--min-n`` (equal weights unless ``--weights``),
with Z's own bootstrap percentile range (the 2.5th and 97.5th percentiles of
Z's draws, centred on the point estimate). A market whose standard error is
zero leaves both the sum and the normaliser and is listed as excluded.
Negative Z = the compared read is more accurate on the whole. The guard check
lists every market worse beyond its range.

    python scripts/sim548_market_skill.py scripts/sim548_accuracy_split.json \\
        scripts/sim548_baseline_2024_chunk2.json ... --json-out scripts/sim548_baseline_skill.json
    python scripts/sim548_market_skill.py BASE.json --compare ARM.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import numpy as np

BINS = [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0001]
#: A real two-way pair's implied probabilities add to about 1.03-1.10 (the
#: book's margin). Outside this band the two prices are not two sides of one
#: bet, and the market probability is not trustworthy. The guard found the
#: first-five run line, whose closing row is often two separate bets (home
#: -1.5 and away -1.5) that the comparison priced as a pair. Since SIM-549 the
#: comparison scores such a row as two bets, each record with no fade price,
#: so the mean reads the pairs only and the run line enters on its own.
OVERROUND_OK = (1.0, 1.15)
#: SIM-549: the guard above reads only records that carry both prices. A
#: run-line bet the book listed on its own carries no fade price, so it needs
#: its own check: its line's mean probability against its own outcome rate.
#: A gap over ONE_SIDED_LINE_Z standard errors, on at least ONE_SIDED_LINE_MIN_N
#: records, is a mis-stored row (such as the 2025 first-inning +1 / +1 rows),
#: and the row is excluded. The backtest's warning uses the same two numbers.
RUN_LINE_MARKET_TYPES = ("runline", "f5_runline", "f1_runline")
ONE_SIDED_LINE_Z = 4.0
ONE_SIDED_LINE_MIN_N = 30
DEFAULT_MIN_N = 100
DEFAULT_BOOTSTRAP = 1000
GAME_MARKET_ORDER = [
    "moneyline",
    "runline",
    "runline_away",  # SIM-549: the away bet of a run line listed as two bets
    "total",
    "f5_moneyline",
    "f5_runline",
    "f5_runline_away",
    "f5_total",
    "f1_moneyline",
    "f1_runline",
    "f1_runline_away",
    "f1_total",
    "first_inning_run",
    "team_total_home",
    "team_total_away",
    "f5_team_total_home",
    "f5_team_total_away",
    "first_to_score",
]


# ---------------------------------------------------------------------------
# Loading and merging
# ---------------------------------------------------------------------------


def _provenance_key(report: dict[str, Any]) -> dict[str, Any]:
    p = report.get("params", {}) or {}
    prov = p.get("provenance", {}) or {}
    return {
        "base_seed": p.get("base_seed"),
        "iterations": p.get("iterations"),
        "seasons": p.get("seasons"),
        "calibration_applied": p.get("calibration_applied"),
        "artifact_dir": prov.get("artifact_dir"),
        "manifest_mtimes": prov.get("manifest_mtimes"),
        "calibration_sha256": prov.get("calibration_sha256"),
        "fatigue_pc_sigma": prov.get("fatigue_pc_sigma"),
        "fatigue_tto_sigma": prov.get("fatigue_tto_sigma"),
        "manager": prov.get("manager"),
        "split": prov.get("split"),
        # SIM-549: a run line scored as two bets and one scored as a pair are
        # two different market probabilities; never merge them unknowingly.
        "run_line_scoring": p.get("run_line_scoring"),
        # A re-scored report holds the run lines' home bets only; a fresh run
        # holds the away bets too. Merged, the away rows would cover some
        # games and the home rows all of them.
        "run_line_rescored": bool(p.get("rescored")),
    }


def load_reports(paths: list[str], force: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge the accuracy records of several reports of ONE configuration.
    Refuses (unless --force) when their provenance differs or their game sets
    overlap — two reads of one game would double-count it."""
    records: list[dict[str, Any]] = []
    key0: dict[str, Any] | None = None
    seen_games: set[int] = set()
    for path in paths:
        rep = json.load(open(path, encoding="utf-8"))
        key = _provenance_key(rep)
        if key0 is None:
            key0 = key
        else:
            diffs = [k for k in key if key[k] != key0[k]]
            if diffs:
                msg = f"{path}: provenance differs from {paths[0]} on {diffs}"
                if not force:
                    sys.exit("REFUSED — " + msg + " (pass --force to merge anyway)")
                print("WARNING — " + msg, file=sys.stderr)
        recs = rep.get("accuracy_records", [])
        games = {int(r["game_pk"]) for r in recs}
        overlap = games & seen_games
        if overlap:
            msg = f"{path}: {len(overlap)} games already in an earlier report"
            if not force:
                sys.exit("REFUSED — " + msg + " (pass --force to merge anyway)")
            print("WARNING — " + msg, file=sys.stderr)
        seen_games |= games
        records.extend(recs)
    return records, key0 or {}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def auc(p: np.ndarray, y: np.ndarray) -> float:
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allp = np.concatenate([pos, neg])
    order = np.argsort(allp, kind="mergesort")
    ranks = np.empty(len(allp))
    # average ranks for ties
    sorted_vals = allp[order]
    i = 0
    while i < len(sorted_vals):
        j = i
        while j + 1 < len(sorted_vals) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def game_bootstrap(
    per_game_num: np.ndarray, per_game_den: np.ndarray, rng: np.random.Generator, n_boot: int
) -> np.ndarray:
    """Bootstrap a ratio-of-sums statistic (a mean over records) by resampling
    GAMES with replacement — records of one game are not independent."""
    g = len(per_game_num)
    idx = rng.integers(0, g, size=(n_boot, g))
    num = per_game_num[idx].sum(axis=1)
    den = per_game_den[idx].sum(axis=1)
    return num / np.maximum(den, 1e-12)


def per_game_sums(
    game_ids: np.ndarray, values: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    games, inv = np.unique(game_ids, return_inverse=True)
    num = np.zeros(len(games))
    den = np.zeros(len(games))
    np.add.at(num, inv, values)
    np.add.at(den, inv, 1.0)
    return games, num, den


def _implied(american: float) -> float:
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else (-a) / (-a + 100.0)


def _mean_overround(recs: list[dict[str, Any]]) -> float | None:
    """The mean of the two prices' implied probabilities added up, over the
    records that carry both prices. None when none does: a three-way market,
    or a run line's away bet (SIM-549: a bet listed on its own)."""
    vals = [
        _implied(r["market_side_price"]) + _implied(r["market_other_price"])
        for r in recs
        if r.get("market_side_price") is not None and r.get("market_other_price") is not None
    ]
    return float(np.mean(vals)) if vals else None


def _one_sided_line(recs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """SIM-549: the line's calibration over a market's one-sided run-line
    records (no fade price). The bias is the line's mean probability minus
    the outcome rate; z is the bias over sqrt(mean(p(1 - p)) / n), its
    standard error under a calibrated line. None when there is no such record."""
    one = [
        r
        for r in recs
        if r.get("market_type") in RUN_LINE_MARKET_TYPES and r.get("market_other_price") is None
    ]
    if not one:
        return None
    pm = np.array([float(r["market_prob"]) for r in one])
    y = np.array([float(r["outcome"]) for r in one])
    se = float(np.sqrt(max(float(np.mean(pm * (1.0 - pm))), 1e-12) / len(one)))
    bias = float(pm.mean() - y.mean())
    return {
        "n": len(one),
        "line_mean": float(pm.mean()),
        "outcome_rate": float(y.mean()),
        "bias": bias,
        "z": bias / se,
    }


def market_rows(
    records: list[dict[str, Any]], min_n: int, n_boot: int, seed: int
) -> list[dict[str, Any]]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_market[str(r["market"])].append(r)
    rng = np.random.default_rng(seed)
    rows = []
    for mkt, recs in by_market.items():
        y = np.array([float(r["outcome"]) for r in recs])
        ps = np.array([float(r["sim_prob"]) for r in recs])
        pm = np.array([float(r["market_prob"]) for r in recs])
        g = np.array([int(r["game_pk"]) for r in recs])
        n = len(recs)
        base_rate = float(y.mean())
        overround = _mean_overround(recs)
        pairs_ok = overround is None or (OVERROUND_OK[0] <= overround <= OVERROUND_OK[1])
        one_sided = _one_sided_line(recs)
        one_sided_ok = (
            one_sided is None
            or one_sided["n"] < ONE_SIDED_LINE_MIN_N
            or abs(one_sided["z"]) <= ONE_SIDED_LINE_Z
        )
        line_ok = pairs_ok and one_sided_ok
        brier_sim = float(np.mean((ps - y) ** 2))
        brier_mkt = float(np.mean((pm - y) ** 2))
        brier_base = float(base_rate * (1.0 - base_rate))
        d = (ps - y) ** 2 - (pm - y) ** 2
        _, num, den = per_game_sums(g, d)
        boots = game_bootstrap(num, den, rng, n_boot)
        lo, hi = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))
        # the Brier scores' own ranges (the same game resampling)
        _, num_s, den_s = per_game_sums(g, (ps - y) ** 2)
        boots_s = game_bootstrap(num_s, den_s, rng, n_boot)
        _, num_m, den_m = per_game_sums(g, (pm - y) ** 2)
        boots_m = game_bootstrap(num_m, den_m, rng, n_boot)
        rel = []
        idx = np.digitize(ps, BINS) - 1
        for i in range(len(BINS) - 1):
            m = idx == i
            rel.append(
                {
                    "bin": f"{BINS[i]:.2f}-{min(BINS[i + 1], 1.0):.2f}",
                    "n": int(m.sum()),
                    "mean_p": float(ps[m].mean()) if m.any() else None,
                    "rate": float(y[m].mean()) if m.any() else None,
                }
            )
        rows.append(
            {
                "market": mkt,
                "market_type": str(recs[0].get("market_type", "")),
                "n": n,
                "games": int(len(np.unique(g))),
                "enters": n >= min_n and line_ok,
                "overround": overround,
                "one_sided_line": one_sided,
                "line_ok": line_ok,
                "outcome_rate": base_rate,
                "brier_sim": brier_sim,
                "brier_sim_lo": float(np.percentile(boots_s, 2.5)),
                "brier_sim_hi": float(np.percentile(boots_s, 97.5)),
                "brier_mkt": brier_mkt,
                "brier_mkt_lo": float(np.percentile(boots_m, 2.5)),
                "brier_mkt_hi": float(np.percentile(boots_m, 97.5)),
                "brier_base": brier_base,
                "skill_sim": brier_base - brier_sim,
                "skill_mkt": brier_base - brier_mkt,
                "gap": brier_sim - brier_mkt,
                "gap_lo": lo,
                "gap_hi": hi,
                "bias_sim": float(ps.mean() - base_rate),
                "bias_mkt": float(pm.mean() - base_rate),
                "spread_sim": float(ps.std()),
                "spread_mkt": float(pm.std()),
                "auc_sim": auc(ps, y),
                "auc_mkt": auc(pm, y),
                "corr_sim": float(np.corrcoef(ps, y)[0, 1])
                if ps.std() > 0 and y.std() > 0
                else float("nan"),
                "reliability": rel,
            }
        )
    return rows


def _order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    props = sorted([r for r in rows if r["market_type"] == "prop"], key=lambda r: r["gap"])
    games = [r for r in rows if r["market_type"] != "prop"]
    games.sort(
        key=lambda r: (
            GAME_MARKET_ORDER.index(r["market"]) if r["market"] in GAME_MARKET_ORDER else 99
        )
    )
    return props + games


# ---------------------------------------------------------------------------
# The composite objective (two reads on the same games)
# ---------------------------------------------------------------------------


def composite(
    base: list[dict[str, Any]],
    arm: list[dict[str, Any]],
    min_n: int,
    n_boot: int,
    seed: int,
    weights: dict[str, float] | None,
) -> dict[str, Any]:
    key = lambda r: (int(r["game_pk"]), str(r["market"]), r.get("player_id"))  # noqa: E731
    a = {key(r): r for r in base}
    b = {key(r): r for r in arm}
    common = sorted(set(a) & set(b))
    by_market: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for k in common:
        ra, rb = a[k], b[k]
        y = float(ra["outcome"])
        d = (float(rb["sim_prob"]) - y) ** 2 - (float(ra["sim_prob"]) - y) ** 2
        by_market[k[1]].append((k[0], d))
    rng = np.random.default_rng(seed)
    games_all = sorted({k[0] for k in common})
    gidx = {g: i for i, g in enumerate(games_all)}
    G = len(games_all)
    # per market: per-game numerators / denominators aligned on the full game list
    mk_num, mk_den, mk_n = {}, {}, {}
    for m, lst in by_market.items():
        num = np.zeros(G)
        den = np.zeros(G)
        for g, d in lst:
            num[gidx[g]] += d
            den[gidx[g]] += 1.0
        mk_num[m], mk_den[m], mk_n[m] = num, den, len(lst)
    entering = [m for m in by_market if mk_n[m] >= min_n]
    w = {m: (weights or {}).get(m, 1.0) for m in entering}
    idx = rng.integers(0, G, size=(n_boot, G))
    per_market: dict[str, dict[str, float]] = {}
    z_boot = np.zeros(n_boot)
    point_terms = []
    # A market whose bootstrap standard error is zero (the two reads agree on
    # every record) carries no z. It leaves BOTH the sum and the normaliser;
    # a zero in the sum with its weight kept in the normaliser would shrink Z.
    included: list[str] = []
    excluded_zero_se: list[str] = []
    for m in entering:
        num, den = mk_num[m], mk_den[m]
        delta = float(num.sum() / max(den.sum(), 1e-12))
        boots = num[idx].sum(axis=1) / np.maximum(den[idx].sum(axis=1), 1e-12)
        s = float(boots.std(ddof=1))
        lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
        per_market[m] = {"n": mk_n[m], "delta": delta, "se": s, "lo": lo, "hi": hi, "weight": w[m]}
        if s > 0:
            included.append(m)
            point_terms.append(w[m] * delta / s)
            z_boot += w[m] * (boots - delta) / s  # the bootstrap's spread around the point
        else:
            excluded_zero_se.append(m)
    norm = float(np.sqrt(sum(w[m] ** 2 for m in included))) or 1.0
    z_point = float(sum(point_terms) / norm)
    # Z's range is a percentile range of Z's own bootstrap draws, centred on
    # the point estimate — the same kind of range every market row carries.
    z_draws = z_point + z_boot / norm
    z_sd = float(z_draws.std(ddof=1)) if n_boot > 1 else float("nan")
    if n_boot > 1:
        z_lo, z_hi = float(np.percentile(z_draws, 2.5)), float(np.percentile(z_draws, 97.5))
    else:
        z_lo, z_hi = float("nan"), float("nan")
    guards_worse = [m for m in entering if per_market[m]["lo"] > 0]
    guards_better = [m for m in entering if per_market[m]["hi"] < 0]
    return {
        "records_paired": len(common),
        "games": G,
        "markets_entering": entering,
        "excluded_zero_se": excluded_zero_se,
        "per_market": per_market,
        "Z": z_point,
        "Z_se": z_sd,
        "Z_lo": z_lo,
        "Z_hi": z_hi,
        "worse_beyond_range": guards_worse,
        "better_beyond_range": guards_better,
    }


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def _f(x: float | None, w: int = 7, d: int = 3, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return " " * (w - 1) + "—"
    return f"{x:+{w}.{d}f}" if sign else f"{x:{w}.{d}f}"


def print_table(rows: list[dict[str, Any]], prov: dict[str, Any], min_n: int) -> None:
    n_rec = sum(r["n"] for r in rows)
    games = max((r["games"] for r in rows), default=0)
    print(f"=== SIM-548 market skill table: {n_rec} records on {games} games ===")
    print(
        f"  iterations {prov.get('iterations')}, seed {prov.get('base_seed')}, seasons {prov.get('seasons')}; "
        f"fatigue tto={prov.get('fatigue_tto_sigma')} pc={prov.get('fatigue_pc_sigma')}; "
        f"split={(prov.get('split') or {}).get('SIM_PITCH_RESULT_SPLIT')} "
        f"(pitch pitcher {(prov.get('split') or {}).get('SIM_PITCH_PITCHER_POWER')}, "
        f"result pitcher {(prov.get('split') or {}).get('SIM_RESULT_PITCHER_POWER')}, "
        f"result batter {(prov.get('split') or {}).get('SIM_RESULT_BATTER_POWER')}); "
        f"manager draw={(prov.get('manager') or {}).get('SIM_MANAGER_DRAW')}; "
        f"run lines scored {prov.get('run_line_scoring') or 'as pairs (before SIM-549)'}"
        f"{' (re-scored: the home bets only)' if prov.get('run_line_rescored') else ''}"
    )
    print(
        "  Brier: lower is better. base = a forecast that always says the market's outcome rate. "
        f"skill = base − Brier (positive = better than knowing only the average). gap = sim − mkt "
        "(negative = the simulator beats the closing line; the range is a game-clustered bootstrap). "
        f"AUC 0.5 = no discrimination. Markets under {min_n} records are shown but flagged."
    )
    hdr = (
        f"  {'market':18s} {'n':>6s} {'games':>5s} | {'sim':>6s} {'mkt':>6s} {'base':>6s} | "
        f"{'skill':>7s} {'mkt-sk':>7s} | {'gap':>8s} {'lo':>8s} {'hi':>8s} | "
        f"{'bias':>7s} {'m-bias':>7s} | {'spread':>6s} {'m-spr':>6s} | {'AUC':>5s} {'m-AUC':>5s} | note"
    )
    print(hdr)
    for r in rows:
        note = ""
        one = r.get("one_sided_line")
        if not r["line_ok"] and (
            r["overround"] is not None and not OVERROUND_OK[0] <= r["overround"] <= OVERROUND_OK[1]
        ):
            note = f"LINE SUSPECT: the two prices add to {r['overround']:.2f} — not the two sides of one bet — excluded"
        elif not r["line_ok"] and one is not None:
            note = (
                f"LINE SUSPECT: the one-sided line says {one['line_mean']:.3f}, the bets came true "
                f"{one['outcome_rate']:.3f} (z {one['z']:+.1f}) — mis-stored lines? — excluded"
            )
        elif not r["enters"]:
            note = f"under {min_n} records"
        elif r["gap_hi"] < 0:
            note = "BEATS the line"
        elif r["gap_lo"] > 0:
            note = "behind the line"
        else:
            note = "level with the line"
        print(
            f"  {r['market']:18s} {r['n']:6d} {r['games']:5d} | {_f(r['brier_sim'], 6)} {_f(r['brier_mkt'], 6)} {_f(r['brier_base'], 6)} | "
            f"{_f(r['skill_sim'], 7, sign=True)} {_f(r['skill_mkt'], 7, sign=True)} | "
            f"{_f(r['gap'], 8, 4, True)} {_f(r['gap_lo'], 8, 4, True)} {_f(r['gap_hi'], 8, 4, True)} | "
            f"{_f(r['bias_sim'], 7, sign=True)} {_f(r['bias_mkt'], 7, sign=True)} | "
            f"{_f(r['spread_sim'], 6)} {_f(r['spread_mkt'], 6)} | {_f(r['auc_sim'], 5)} {_f(r['auc_mkt'], 5)} | {note}"
        )
    print()
    print("  reliability by bin (the simulator's mean probability → the outcome rate, n):")
    for r in rows:
        cells = []
        for b in r["reliability"]:
            if b["n"] == 0:
                cells.append(f"{b['bin']}: —")
            else:
                cells.append(f"{b['bin']}: {b['mean_p']:.2f}→{b['rate']:.2f} ({b['n']})")
        print(f"    {r['market']:18s} " + " | ".join(cells))


def print_composite(c: dict[str, Any]) -> None:
    print()
    print(
        f"=== the composite objective (arm minus base; negative = the arm more accurate): "
        f"{c['records_paired']} records paired on {c['games']} games ==="
    )
    print(
        f"  {'market':18s} {'n':>6s} {'Δ Brier':>9s} {'se':>8s} {'lo':>9s} {'hi':>9s} {'w':>4s} {'z':>6s}"
    )
    for m in sorted(c["per_market"], key=lambda k: c["per_market"][k]["delta"]):
        r = c["per_market"][m]
        z = r["delta"] / r["se"] if r["se"] > 0 else float("nan")
        print(
            f"  {m:18s} {r['n']:6d} {r['delta']:+9.4f} {r['se']:8.4f} {r['lo']:+9.4f} {r['hi']:+9.4f} "
            f"{r['weight']:4.1f} {z:+6.2f}"
        )
    n_in = len(c["markets_entering"]) - len(c.get("excluded_zero_se", []))
    print(
        f"  Z = {c['Z']:+.2f}  [{c['Z_lo']:+.2f}, {c['Z_hi']:+.2f}]  over {n_in} markets "
        "(negative = more accurate on the whole; the range is Z's bootstrap percentile range)"
    )
    if c.get("excluded_zero_se"):
        print(f"  excluded (zero standard error — the two reads agree): {c['excluded_zero_se']}")
    print(f"  worse beyond its range: {c['worse_beyond_range'] or 'none'}")
    print(f"  better beyond its range: {c['better_beyond_range'] or 'none'}")


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "reports", nargs="+", help="one or more clv_backtest JSON reports of ONE configuration"
    )
    ap.add_argument(
        "--compare",
        nargs="+",
        default=None,
        help="report(s) of another configuration on the same games",
    )
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP)
    ap.add_argument("--bootstrap-seed", type=int, default=548)
    ap.add_argument(
        "--weights", default=None, help="JSON dict of market weights for Z, e.g. '{\"K\": 2}'"
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    records, prov = load_reports(args.reports, args.force)
    rows = _order(market_rows(records, args.min_n, args.bootstrap_samples, args.bootstrap_seed))
    print_table(rows, prov, args.min_n)
    out: dict[str, Any] = {"provenance": prov, "records": len(records), "markets": rows}
    if args.compare:
        arm, prov_b = load_reports(args.compare, args.force)
        weights = json.loads(args.weights) if args.weights else None
        c = composite(
            records, arm, args.min_n, args.bootstrap_samples, args.bootstrap_seed, weights
        )
        print_composite(c)
        out["compare_provenance"] = prov_b
        out["composite"] = c
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=float)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
