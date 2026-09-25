"""
scripts/sim518_pair_accuracy.py — SIM-518: pair two accuracy-comparison reports.

The sim-vs-closing-line accuracy comparison (scripts/clv_backtest.py, SIM-538)
scores the simulator's probability against what happened, next to the
market's. To learn whether a model change helped, run the comparison TWICE
on the same games, seeds and frozen artifact bundle — the OFF arm and the ON
arm — and read the PAIRED per-record difference of the sim's own score
(ON minus OFF; negative = the ON arm was more accurate). This script does
that pairing (plan: docs/audit/2026-09-12-sim518-fit-plan.md §6):

  * it REFUSES to pair reports whose provenance differs — the bundle's
    manifest timestamps, the calibration file's hash, the base seed, the
    iteration count, the game list, or (SIM-549) the run lines' scoring stamp
    and whether a report was re-scored — unless ``--force`` says the operator
    accepts the confound;
  * per market it reports the records, the games, the mean paired Brier and
    log-loss difference, the game-clustered bootstrap range and the SIM-539
    minimum sample at the floor asked for, and each arm's own mean lead over
    the market;
  * for the props it stratifies by the pitcher's REAL pitch depth in that
    game (``raw.game_player_stats.p_pitches``) when a DSN is given, and can
    write the deep-start game list the 500-iteration subset run reads
    (``--write-deep-start-list``).

    python scripts/sim518_pair_accuracy.py /data/clv_off.json /data/clv_tto05.json \\
        --dsn "$BASEBALL_DB_DSN" --json-out /data/sim518_pair.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from simulation.prop_validation import OUTCOME_PROB_EPS  # noqa: E402


def _load_backtest_module() -> Any:
    """The backtest's statistics helpers, imported by path (scripts/ is not
    a package) so the pairing uses the SAME clustered machinery SIM-539 built."""
    if "clv_backtest" in sys.modules:
        return sys.modules["clv_backtest"]
    spec = importlib.util.spec_from_file_location(
        "clv_backtest", str(_ROOT / "scripts" / "clv_backtest.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clv_backtest"] = mod
    spec.loader.exec_module(mod)
    return mod


DEPTH_EDGES: tuple[int, ...] = (75, 100)
DEPTH_LABELS: tuple[str, ...] = ("<75", "75-99", "100+")
#: The Brier-difference floor the SIM-539 minimum sample is sized on — the
#: plan's estimate of a fatigue-sized improvement (§6).
DEFAULT_FLOOR = 0.0005
DEFAULT_ALPHA = 0.05
DEFAULT_BOOTSTRAP = 2000
#: A start of at least this many real pitches is a DEEP start (the subset run).
DEEP_START_PITCHES = 90


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def provenance_mismatches(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Every reason the two reports must not be paired; [] when they may."""
    pa, pb = a.get("params", {}), b.get("params", {})
    out: list[str] = []
    # SIM-549: "run_line_scoring" too — a run line scored as two bets and one
    # scored as a pair carry different market probabilities.
    for key in (
        "base_seed",
        "iterations",
        "seasons",
        "markets",
        "calibration_applied",
        "run_line_scoring",
    ):
        if pa.get(key) != pb.get(key):
            out.append(f"params.{key}: {pa.get(key)!r} vs {pb.get(key)!r}")
    # SIM-549: a re-scored report holds the run lines' home bets only; a fresh
    # run holds the away bets too, which would pair with nothing.
    if bool(pa.get("rescored")) != bool(pb.get("rescored")):
        out.append(
            f"params.rescored: {bool(pa.get('rescored'))} vs {bool(pb.get('rescored'))} "
            "(a re-scored report has no run-line away bets)"
        )
    prov_a, prov_b = pa.get("provenance") or {}, pb.get("provenance") or {}
    if not prov_a or not prov_b:
        out.append("provenance missing on one report (run the backtest at or after SIM-518)")
    else:
        for key in ("artifact_dir", "manifest_mtimes", "calibration_sha256"):
            if prov_a.get(key) != prov_b.get(key):
                out.append(f"provenance.{key}: {prov_a.get(key)!r} vs {prov_b.get(key)!r}")
    ga = {int(r["game_pk"]) for r in a.get("accuracy_records", [])}
    gb = {int(r["game_pk"]) for r in b.get("accuracy_records", [])}
    if ga != gb:
        out.append(f"game sets differ: {len(ga - gb)} only in A, {len(gb - ga)} only in B")
    return out


# ---------------------------------------------------------------------------
# Scoring and pairing
# ---------------------------------------------------------------------------


def _brier(p: float, y: int) -> float:
    return (float(p) - float(y)) ** 2


def _log_loss(p: float, y: int, eps: float = OUTCOME_PROB_EPS) -> float:
    q = min(max(float(p), eps), 1.0 - eps)
    return -(float(y) * np.log(q) + (1.0 - float(y)) * np.log(1.0 - q))


def _key(r: dict[str, Any]) -> tuple[int, str, int | None]:
    pid = r.get("player_id")
    return (int(r["game_pk"]), str(r["market"]), None if pid is None else int(pid))


def pair_records(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per record present in BOTH reports, with each arm's Brier and
    log loss, the market's, and the paired differences (B minus A)."""
    by_key_b = {_key(r): r for r in b}
    rows: list[dict[str, Any]] = []
    for ra in a:
        rb = by_key_b.get(_key(ra))
        if rb is None:
            continue
        if (
            int(ra["outcome"]) != int(rb["outcome"])
            or abs(float(ra["market_prob"]) - float(rb["market_prob"])) > 1e-9
        ):
            raise ValueError(
                f"record {_key(ra)} differs in outcome or market probability between the "
                "arms — the odds rows or the box score changed between the runs"
            )
        y = int(ra["outcome"])
        pa, pb, pm = float(ra["sim_prob"]), float(rb["sim_prob"]), float(ra["market_prob"])
        rows.append(
            {
                "game_pk": int(ra["game_pk"]),
                "market": str(ra["market"]),
                "market_type": str(ra["market_type"]),
                "player_id": ra.get("player_id"),
                "outcome": y,
                "sim_prob_a": pa,
                "sim_prob_b": pb,
                "market_prob": pm,
                "brier_a": _brier(pa, y),
                "brier_b": _brier(pb, y),
                "brier_market": _brier(pm, y),
                "ll_a": _log_loss(pa, y),
                "ll_b": _log_loss(pb, y),
                "ll_market": _log_loss(pm, y),
                "d_brier": _brier(pb, y) - _brier(pa, y),
                "d_ll": _log_loss(pb, y) - _log_loss(pa, y),
                "d_prob": pb - pa,
            }
        )
    return rows


def summarize(
    rows: list[dict[str, Any]],
    *,
    n_bootstrap: int,
    seed: int,
    floor: float,
    alpha: float,
) -> dict[str, Any]:
    """The paired read for one bucket of rows."""
    bt = _load_backtest_module()
    if not rows:
        return {"n": 0}
    d_brier = np.array([r["d_brier"] for r in rows], dtype=np.float64)
    d_ll = np.array([r["d_ll"] for r in rows], dtype=np.float64)
    d_prob = np.array([r["d_prob"] for r in rows], dtype=np.float64)
    gp = np.array([r["game_pk"] for r in rows], dtype=np.int64)
    by_game: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_game[int(r["game_pk"])].append(float(r["d_brier"]))
    lo, hi = bt._bootstrap_paired_diff_ci(d_brier, gp, n_bootstrap=n_bootstrap, seed=seed)
    lo_ll, hi_ll = bt._bootstrap_paired_diff_ci(d_ll, gp, n_bootstrap=n_bootstrap, seed=seed + 1)
    se = bt._clustered_se(by_game)
    min_n = bt._minimum_observations_clustered(by_game, floor=floor, alpha=alpha)
    n = len(rows)
    lead_a = float(np.mean([r["brier_a"] - r["brier_market"] for r in rows]))
    lead_b = float(np.mean([r["brier_b"] - r["brier_market"] for r in rows]))
    return {
        "n": n,
        "n_games": len(by_game),
        "mean_d_brier": float(d_brier.mean()),
        "d_brier_ci95": [float(lo), float(hi)],
        "d_brier_clustered_se": float(se),
        "mean_d_log_loss": float(d_ll.mean()),
        "d_log_loss_ci95": [float(lo_ll), float(hi_ll)],
        "mean_abs_d_prob": float(np.abs(d_prob).mean()),
        "mean_d_prob": float(d_prob.mean()),
        "brier_vs_market_a": lead_a,
        "brier_vs_market_b": lead_b,
        "min_n_at_floor": None if min_n is None else float(min_n),
        "resolved": (None if min_n is None else bool(n >= min_n)),
        "floor": floor,
    }


# ---------------------------------------------------------------------------
# Depth from the official box score
# ---------------------------------------------------------------------------


async def _fetch_depth(dsn: str, game_pks: list[int]) -> dict[tuple[int, int], tuple[int, bool]]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        rows = await conn.fetch(
            """
            SELECT game_pk, player_id, p_pitches, p_started
            FROM raw.game_player_stats
            WHERE game_pk = ANY($1::int[]) AND played_pitch
            """,
            game_pks,
        )
    finally:
        await conn.close()
    return {
        (int(r["game_pk"]), int(r["player_id"])): (int(r["p_pitches"]), bool(r["p_started"]))
        for r in rows
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _fmt_row(name: str, s: dict[str, Any]) -> str:
    if s.get("n", 0) == 0:
        return f"  {name:<22}{0:>7}"
    ci = s["d_brier_ci95"]
    res = "" if s["resolved"] is None else ("yes" if s["resolved"] else "no")
    min_n = "-" if s["min_n_at_floor"] is None else f"{s['min_n_at_floor']:.0f}"
    return (
        f"  {name:<22}{s['n']:>7}{s['n_games']:>7}"
        f"{s['mean_d_brier'] * 1e4:>+10.2f}{ci[0] * 1e4:>+9.2f}{ci[1] * 1e4:>+9.2f}"
        f"{s['mean_d_log_loss'] * 1e4:>+10.2f}"
        f"{s['mean_abs_d_prob']:>9.4f}{s['mean_d_prob']:>+9.4f}"
        f"{s['brier_vs_market_a'] * 1e4:>+10.2f}{s['brier_vs_market_b'] * 1e4:>+10.2f}"
        f"{min_n:>8}{res:>5}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("report_a", help="the OFF arm's clv_backtest JSON report")
    ap.add_argument("report_b", help="the ON arm's clv_backtest JSON report")
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN", ""))
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    ap.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP)
    ap.add_argument("--bootstrap-seed", type=int, default=518)
    ap.add_argument("--force", action="store_true", help="pair despite a provenance mismatch")
    ap.add_argument("--json-out", default=None)
    ap.add_argument(
        "--write-deep-start-list",
        default=None,
        help=(
            f"write the game_pks where a pitcher with a prop record threw at least "
            f"{DEEP_START_PITCHES} real pitches (needs --dsn) — the subset run's input"
        ),
    )
    args = ap.parse_args(argv)

    a = json.load(open(args.report_a, encoding="utf-8"))
    b = json.load(open(args.report_b, encoding="utf-8"))
    problems = provenance_mismatches(a, b)
    if problems:
        print("PROVENANCE MISMATCH — these reports are not twins:")
        for pr in problems:
            print(f"  - {pr}")
        if not args.force:
            print("refusing to pair (pass --force to accept the confound)")
            return 2
        print("--force given: pairing anyway; the read carries the confound above")

    rows = pair_records(a.get("accuracy_records", []), b.get("accuracy_records", []))
    n_a, n_b = len(a.get("accuracy_records", [])), len(b.get("accuracy_records", []))
    print(
        f"=== SIM-518 paired accuracy read: {len(rows)} records paired "
        f"({n_a} in A, {n_b} in B); B minus A, negative = B more accurate ==="
    )
    prov = (a.get("params") or {}).get("provenance") or {}
    print(
        f"  arm A fatigue: pc={prov.get('fatigue_pc_sigma')} tto={prov.get('fatigue_tto_sigma')}; "
        f"arm B fatigue: pc={((b.get('params') or {}).get('provenance') or {}).get('fatigue_pc_sigma')} "
        f"tto={((b.get('params') or {}).get('provenance') or {}).get('fatigue_tto_sigma')}"
    )
    print(
        f"  iterations {a['params'].get('iterations')}, base seed {a['params'].get('base_seed')}, "
        f"seasons {a['params'].get('seasons')}, floor {args.floor} Brier"
    )

    kw = {
        "n_bootstrap": args.bootstrap_samples,
        "seed": args.bootstrap_seed,
        "floor": args.floor,
        "alpha": args.alpha,
    }
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets["ALL"].append(r)
        buckets[f"type:{r['market_type']}"].append(r)
        buckets[f"market:{r['market']}"].append(r)

    depth: dict[tuple[int, int], tuple[int, bool]] = {}
    if args.dsn:
        try:
            depth = asyncio.run(_fetch_depth(args.dsn, sorted({r["game_pk"] for r in rows})))
        except Exception as exc:  # noqa: BLE001 — the strata are optional
            print(f"  depth unavailable ({type(exc).__name__}: {exc}); no pitch-depth strata")
    if depth:
        for r in rows:
            if r["player_id"] is None:
                continue
            d = depth.get((int(r["game_pk"]), int(r["player_id"])))
            if d is None:
                continue
            pitches, started = d
            if not started:
                continue
            label = DEPTH_LABELS[int(np.digitize(pitches, DEPTH_EDGES))]
            buckets[f"starter depth {label}:{r['market']}"].append(r)

    summary = {name: summarize(rs, **kw) for name, rs in buckets.items()}
    hdr = (
        f"  {'bucket':<22}{'n':>7}{'games':>7}{'dBrier':>10}{'ci lo':>9}{'ci hi':>9}"
        f"{'dLogLoss':>10}{'|dP|':>9}{'dP':>9}{'A-mkt':>10}{'B-mkt':>10}{'min n':>8}{'res':>5}"
    )
    print("\n  (Brier and log-loss columns in units of 1e-4; A-mkt / B-mkt = each arm's mean Brier")
    print("   minus the market's, negative = the arm beat the closing line)")
    print(hdr)
    order = sorted(summary, key=lambda k: (not k.startswith("ALL"), not k.startswith("type:"), k))
    for name in order:
        print(_fmt_row(name, summary[name]))

    if args.write_deep_start_list:
        if not depth:
            print("cannot write the deep-start list without --dsn")
        else:
            deep = sorted(
                {
                    int(r["game_pk"])
                    for r in rows
                    if r["player_id"] is not None
                    and depth.get((int(r["game_pk"]), int(r["player_id"])), (0, False))[1]
                    and depth[(int(r["game_pk"]), int(r["player_id"]))][0] >= DEEP_START_PITCHES
                }
            )
            Path(args.write_deep_start_list).write_text(json.dumps(deep))
            print(f"wrote {len(deep)} deep-start games -> {args.write_deep_start_list}")

    if args.json_out:
        out = {
            "report_a": args.report_a,
            "report_b": args.report_b,
            "provenance_problems": problems,
            "params": {k: getattr(args, k) for k in ("floor", "alpha", "bootstrap_samples")},
            "n_paired": len(rows),
            "summary": summary,
        }
        Path(args.json_out).write_text(json.dumps(out, indent=2))
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
