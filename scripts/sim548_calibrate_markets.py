"""
scripts/sim548_calibrate_markets.py — SIM-548 §5.4 / §7: the per-market
CALIBRATION LAYER — a map from the simulator's probability to a calibrated one,
fitted on one set of games and validated on another it never saw.

The map corrects probabilities, never plays: the simulated game, its box score
and its play-by-play are untouched. Per market the default map is the
two-parameter logistic ("Platt") map

    logit(p') = a + b * logit(p)

(a shifts, b shrinks or stretches; b < 1 = the simulator was over-spread), fitted
by maximum likelihood on the FIT reports' records. ``--method isotonic`` fits a
monotone step map instead (pool-adjacent-violators; needs thousands of records).
The moneyline is fitted on the RAW home-win share (``sim_prob_raw``, recorded by
the backtest since 2026-09-15) and skipped when the records carry only the
mapped value — a map on top of the stale reliability curve would compound it.
Its BEFORE figures are scored on ``sim_prob``, the mapped value production
shows today; its AFTER figures are the new map applied to the raw share. So
the map gain reads against what users see now.

The logistic fit takes Newton steps with step-halving and reports whether it
converged; an unconverged map never passes and is never written.

The read, per market: the records; a and b; the Brier score, the bias and the
gap to the closing line BEFORE and AFTER the map, on the fit set and on the
CHECK set; the check set's reliability by bin after the map. A map earns its
place only when it improves the CHECK set.

    python scripts/sim548_calibrate_markets.py \\
        --fit scripts/sim548_accuracy_split.json scripts/sim548_baseline_2024_chunk2.json \\
        --check scripts/sim548_baseline_2024_chunk3.json scripts/sim548_baseline_2024_chunk4.json \\
        --json-out scripts/sim548_market_calibration.json

``--write-calibration /data/calibration.json`` merges the fitted maps into the
calibration report under ``market_calibration`` (the app does not read that key
yet — the wiring is a separate step; nothing changes at boot).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import numpy as np

EPS = 0.005
BINS = [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0001]
DEFAULT_MIN_N = 150


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_records(paths: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        rep = json.load(open(path, encoding="utf-8"))
        out.extend(rep.get("accuracy_records", []))
    return out


def by_market(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    d: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        d[str(r["market"])].append(r)
    return d


def sim_probability(recs: list[dict[str, Any]], market: str) -> np.ndarray | None:
    """The probability the layer is fitted on: the raw one where a map already
    applies (the moneyline), the recorded one everywhere else."""
    if market == "moneyline":
        raws = [r.get("sim_prob_raw") for r in recs]
        if any(v is None for v in raws):
            return None
        return np.array([float(v) for v in raws])
    return np.array([float(r["sim_prob"]) for r in recs])


def shown_probability(recs: list[dict[str, Any]]) -> np.ndarray:
    """The probability production shows today (``sim_prob``): on the moneyline
    the stale reliability curve's output, elsewhere the same as the raw one."""
    return np.array([float(r["sim_prob"]) for r in recs])


# ---------------------------------------------------------------------------
# The maps
# ---------------------------------------------------------------------------


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, EPS, 1.0 - EPS)
    return np.log(q / (1.0 - q))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


MAX_ITERS = 100
MAX_HALVINGS = 20
STEP_TOL = 1e-8


def _log_likelihood(X: np.ndarray, beta: np.ndarray, y: np.ndarray) -> float:
    """The Bernoulli log-likelihood of the coefficients, computed in a stable form."""
    z = X @ beta
    return float(np.sum(y * z - np.logaddexp(0.0, z)))


def fit_logistic(
    p: np.ndarray,
    y: np.ndarray,
    max_iters: int = MAX_ITERS,
    max_halvings: int = MAX_HALVINGS,
    tol: float = STEP_TOL,
) -> tuple[float, float, bool]:
    """Maximum-likelihood a, b for logit(p') = a + b * logit(p).

    Newton's method on the two coefficients with step-halving: the fit halves
    a step until the log-likelihood does not decrease (up to ``max_halvings``
    halvings). The fit stops when the step falls below ``tol`` or after
    ``max_iters`` iterations. Returns (a, b, converged). ``converged`` is
    False when the outcomes carry one value only (the likelihood has no
    maximum), when the normal equations are singular, when no halving finds an
    uphill step, or when the iteration limit runs out. A plain Newton step can
    diverge on over-spread data (a true slope near 0.15-0.3); the halving
    keeps every step uphill.
    """
    x = _logit(p)
    X = np.column_stack([np.ones_like(x), x])
    beta = np.array([0.0, 1.0])
    if y.min() == y.max():
        # every outcome is the same value: the likelihood climbs without a
        # maximum, so no finite (a, b) fits
        return float(beta[0]), float(beta[1]), False
    ll = _log_likelihood(X, beta, y)
    converged = False
    for _ in range(max_iters):
        mu = _sigmoid(X @ beta)
        w = mu * (1.0 - mu) + 1e-9
        grad = X.T @ (y - mu)
        hess = (X * w[:, None]).T @ X
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        if not np.all(np.isfinite(step)):
            break
        if np.max(np.abs(step)) < tol:
            # already at the maximum: a full Newton step moves nothing.
            # Tested BEFORE the halving so a one-ulp fall in the likelihood
            # at the optimum cannot read as "no uphill step".
            converged = True
            break
        scale = 1.0
        accepted = False
        for _ in range(max_halvings + 1):
            candidate = beta + scale * step
            ll_new = _log_likelihood(X, candidate, y)
            if ll_new >= ll:
                accepted = True
                break
            scale *= 0.5
        if not accepted:
            break
        beta, ll = candidate, ll_new
        if np.max(np.abs(scale * step)) < tol:
            converged = True
            break
    return float(beta[0]), float(beta[1]), converged


def apply_logistic(p: np.ndarray, a: float, b: float) -> np.ndarray:
    return _sigmoid(a + b * _logit(p))


def fit_isotonic(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool-adjacent-violators: a non-decreasing step map from p to the outcome
    rate. Returns the block edges (sorted p) and the block values.

    The fit groups the records by their unique p first (the sum of y and the
    count per value), then pools adjacent groups by weight. On a 0.01 grid
    (100 iterations per game) many records tie; without the grouping the
    blocks depend on the record order.
    """
    unique_p, inv = np.unique(p, return_inverse=True)
    sums = np.zeros(len(unique_p))
    counts = np.zeros(len(unique_p))
    np.add.at(sums, inv, y.astype(float))
    np.add.at(counts, inv, 1.0)
    # blocks as (sum, count, start_p)
    vals = list(sums)
    cnts = list(counts)
    starts = list(unique_p)
    i = 0
    while i < len(vals) - 1:
        if vals[i] / cnts[i] > vals[i + 1] / cnts[i + 1]:
            vals[i] += vals[i + 1]
            cnts[i] += cnts[i + 1]
            del vals[i + 1], cnts[i + 1], starts[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    edges = np.array(starts)
    values = np.array([v / c for v, c in zip(vals, cnts, strict=True)])
    return edges, values


def apply_isotonic(p: np.ndarray, edges: np.ndarray, values: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(edges, p, side="right") - 1
    idx = np.clip(idx, 0, len(values) - 1)
    return values[idx]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _reliability(p: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    idx = np.digitize(p, BINS) - 1
    out = []
    for i in range(len(BINS) - 1):
        m = idx == i
        out.append(
            {
                "bin": f"{BINS[i]:.2f}-{min(BINS[i + 1], 1.0):.2f}",
                "n": int(m.sum()),
                "mean_p": float(p[m].mean()) if m.any() else None,
                "rate": float(y[m].mean()) if m.any() else None,
            }
        )
    return out


def _game_bootstrap_mean(
    values: np.ndarray, games: np.ndarray, rng: np.random.Generator, n_boot: int
) -> tuple[float, float]:
    g, inv = np.unique(games, return_inverse=True)
    num = np.zeros(len(g))
    den = np.zeros(len(g))
    np.add.at(num, inv, values)
    np.add.at(den, inv, 1.0)
    idx = rng.integers(0, len(g), size=(n_boot, len(g)))
    boots = num[idx].sum(axis=1) / np.maximum(den[idx].sum(axis=1), 1e-12)
    return float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def fit_market(
    market: str,
    fit_recs: list[dict[str, Any]],
    check_recs: list[dict[str, Any]],
    method: str,
    rng: np.random.Generator,
    n_boot: int,
) -> dict[str, Any] | None:
    """Fit one market's map on the fit records and read it on the check records.

    The map is fitted on the RAW probability (``sim_probability``: on the
    moneyline ``sim_prob_raw``, elsewhere ``sim_prob``). Every BEFORE figure
    (Brier, bias, spread, gap) is scored on the probability production shows
    today (``shown_probability`` = ``sim_prob``; on the moneyline that is the
    stale reliability curve's output). Every AFTER figure is the map applied
    to the raw probability. So the map gain reads "the new map against what
    users see now", not against a raw share nobody sees.

    A logistic map that did not converge records ``converged: False`` and
    never passes. A converged negative slope collapses to the base rate.
    """
    p_fit = sim_probability(fit_recs, market)
    p_chk = sim_probability(check_recs, market)
    if p_fit is None or p_chk is None:
        return {
            "market": market,
            "skipped": "no raw probability on the records (the moneyline needs sim_prob_raw)",
        }
    s_fit = shown_probability(fit_recs)
    s_chk = shown_probability(check_recs)
    y_fit = np.array([float(r["outcome"]) for r in fit_recs])
    y_chk = np.array([float(r["outcome"]) for r in check_recs])
    m_chk = np.array([float(r["market_prob"]) for r in check_recs])
    g_chk = np.array([int(r["game_pk"]) for r in check_recs])
    converged = True
    if method == "isotonic":
        edges, values = fit_isotonic(p_fit, y_fit)
        q_fit = apply_isotonic(p_fit, edges, values)
        q_chk = apply_isotonic(p_chk, edges, values)
        params: dict[str, Any] = {"edges": edges.tolist(), "values": values.tolist()}
    else:
        a, b, converged = fit_logistic(p_fit, y_fit)
        collapsed = False
        if converged and b < 0.0:
            # A negative slope says the simulator's probability runs the wrong
            # way on this market — no information to keep. The honest map is
            # the fit set's own outcome rate, not an inverted probability.
            base = float(np.clip(y_fit.mean(), EPS, 1.0 - EPS))
            a, b = float(np.log(base / (1.0 - base))), 0.0
            collapsed = True
        q_fit = apply_logistic(p_fit, a, b)
        q_chk = apply_logistic(p_chk, a, b)
        params = {
            "a": a,
            "b": b,
            "collapsed_to_base_rate": collapsed,
            "converged": converged,
        }
    d_before = (s_chk - y_chk) ** 2 - (m_chk - y_chk) ** 2
    d_after = (q_chk - y_chk) ** 2 - (m_chk - y_chk) ** 2
    gain = (q_chk - y_chk) ** 2 - (s_chk - y_chk) ** 2  # negative = the map helped
    lo_b, hi_b = _game_bootstrap_mean(d_before, g_chk, rng, n_boot)
    lo_a, hi_a = _game_bootstrap_mean(d_after, g_chk, rng, n_boot)
    lo_g, hi_g = _game_bootstrap_mean(gain, g_chk, rng, n_boot)
    return {
        "market": market,
        "method": method,
        "params": params,
        "n_fit": len(fit_recs),
        "n_check": len(check_recs),
        "converged": converged,
        "fit": {
            "brier_before": _brier(s_fit, y_fit),
            "brier_after": _brier(q_fit, y_fit),
            "bias_before": float(s_fit.mean() - y_fit.mean()),
            "bias_after": float(q_fit.mean() - y_fit.mean()),
        },
        "check": {
            "brier_before": _brier(s_chk, y_chk),
            "brier_after": _brier(q_chk, y_chk),
            "brier_market": _brier(m_chk, y_chk),
            "bias_before": float(s_chk.mean() - y_chk.mean()),
            "bias_after": float(q_chk.mean() - y_chk.mean()),
            "spread_before": float(s_chk.std()),
            "spread_after": float(q_chk.std()),
            "spread_market": float(m_chk.std()),
            "gap_before": float(d_before.mean()),
            "gap_before_lo": lo_b,
            "gap_before_hi": hi_b,
            "gap_after": float(d_after.mean()),
            "gap_after_lo": lo_a,
            "gap_after_hi": hi_a,
            "map_gain": float(gain.mean()),
            "map_gain_lo": lo_g,
            "map_gain_hi": hi_g,
            "reliability_before": _reliability(s_chk, y_chk),
            "reliability_after": _reliability(q_chk, y_chk),
        },
        # the map earns its place only when the CHECK set says it helped
        # beyond its range — and never when its fit did not converge
        "passes": bool(converged and hi_g < 0.0),
    }


# ---------------------------------------------------------------------------


def _f(x: float, w: int = 8, d: int = 4, sign: bool = True) -> str:
    return f"{x:+{w}.{d}f}" if sign else f"{x:{w}.{d}f}"


def print_report(results: list[dict[str, Any]], method: str) -> None:
    print(
        f"=== SIM-548 calibration layer ({method} map), fitted on the FIT games, read on the CHECK games ==="
    )
    print(
        "  Brier: lower is better. gap = sim − line on the CHECK set (negative = the simulator beats the line); "
        "map gain = Brier after − before on the CHECK set (negative = the map helped), with game-clustered ranges."
    )
    print(
        f"  {'market':18s} {'n_fit':>6s} {'n_chk':>6s} | {'a':>6s} {'b':>6s} | {'Brier before':>12s} {'after':>7s} {'line':>7s} | "
        f"{'bias bef':>8s} {'aft':>7s} | {'spread bef':>10s} {'aft':>6s} {'line':>6s} | {'gap before [range]':>28s} {'gap after [range]':>28s} | {'map gain [range]':>28s} pass"
    )
    for r in results:
        if r.get("skipped"):
            print(f"  {r['market']:18s} skipped — {r['skipped']}")
            continue
        c = r["check"]
        pa = r["params"]
        a = pa.get("a", float("nan"))
        b = pa.get("b", float("nan"))
        print(
            f"  {r['market']:18s} {r['n_fit']:6d} {r['n_check']:6d} | {a:+6.2f} {b:6.2f} | "
            f"{c['brier_before']:12.4f} {c['brier_after']:7.4f} {c['brier_market']:7.4f} | "
            f"{c['bias_before']:+8.3f} {c['bias_after']:+7.3f} | {c['spread_before']:10.3f} {c['spread_after']:6.3f} {c['spread_market']:6.3f} | "
            f"{_f(c['gap_before'])} [{_f(c['gap_before_lo'])}, {_f(c['gap_before_hi'])}] "
            f"{_f(c['gap_after'])} [{_f(c['gap_after_lo'])}, {_f(c['gap_after_hi'])}] | "
            f"{_f(c['map_gain'])} [{_f(c['map_gain_lo'])}, {_f(c['map_gain_hi'])}] "
            f"{'PASS' if r['passes'] else 'no'}"
            f"{'' if r.get('converged', True) else ' (fit did not converge)'}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--fit", nargs="+", required=True, help="the reports the maps are fitted on")
    ap.add_argument(
        "--check",
        nargs="+",
        required=True,
        help="the reports the maps are read on (never the fit games)",
    )
    ap.add_argument("--method", choices=("logistic", "isotonic"), default="logistic")
    ap.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N,
        help="records needed in BOTH sets to fit a market",
    )
    ap.add_argument("--bootstrap-samples", type=int, default=1000)
    ap.add_argument("--bootstrap-seed", type=int, default=548)
    ap.add_argument("--json-out", default=None)
    ap.add_argument(
        "--write-calibration",
        default=None,
        help="merge the maps into this calibration.json under market_calibration",
    )
    args = ap.parse_args(argv)

    fit = by_market(load_records(args.fit))
    chk = by_market(load_records(args.check))
    fit_games = {int(r["game_pk"]) for recs in fit.values() for r in recs}
    chk_games = {int(r["game_pk"]) for recs in chk.values() for r in recs}
    overlap = fit_games & chk_games
    if overlap:
        sys.exit(f"REFUSED — {len(overlap)} games appear in both the fit and the check sets")
    rng = np.random.default_rng(args.bootstrap_seed)
    results = []
    for market in sorted(set(fit) | set(chk)):
        fr, cr = fit.get(market, []), chk.get(market, [])
        if len(fr) < args.min_n or len(cr) < args.min_n:
            continue
        res = fit_market(market, fr, cr, args.method, rng, args.bootstrap_samples)
        if res is not None:
            results.append(res)
    results.sort(key=lambda r: r["check"]["map_gain"] if not r.get("skipped") else 1.0)
    print_report(results, args.method)
    out = {
        "method": args.method,
        "fit_reports": args.fit,
        "check_reports": args.check,
        "fit_games": len(fit_games),
        "check_games": len(chk_games),
        "markets": results,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, default=float)
        print(f"wrote {args.json_out}")
    if args.write_calibration:
        rep = json.load(open(args.write_calibration, encoding="utf-8"))
        rep["market_calibration"] = {
            "method": args.method,
            "fitted_on_games": len(fit_games),
            "checked_on_games": len(chk_games),
            # an unconverged fit never passes, so it never reaches the report
            "maps": {
                r["market"]: r["params"] for r in results if not r.get("skipped") and r["passes"]
            },
        }
        with open(args.write_calibration, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
        print(f"merged market_calibration into {args.write_calibration} (not read by the app yet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
