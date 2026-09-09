"""
scripts/sim523_power_scan.py — SIM-523 part F: the OFFLINE power scan.

For each identity factor — the pitcher and the batter in the pitch draws,
the runner / catcher / pitcher in the steal draw, the runner in the
advancement draws — compute, for every live actor of the season with enough
own rows, the EXPECTED conditional rate under the draw weights score^p over
the pool (recency-weighted; an unscored row is draw-NEUTRAL at the mean
scored weight), against the actor's OWN rows, for a ladder of powers. The
read per factor, channel and power: the tier spread ratio (the expected
rate's spread across quintiles of the actors' own rates, over the own
spread; 1.0 = the factor reproduces the pool's own conditional) and the
effective sample share (100% = flat). This is the sim-free first read of
the power each factor needs; scripts/sim523_fit_probe.py confirms in the
loop, where the other factors dilute it.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_power_scan.py --powers 1,2,3,5,8,12,20 \\
        --json-out /app/scripts/sim523_power_scan.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402

OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
_OUT_IDX = {o: i for i, o in enumerate(OUTCOMES)}
N_OUT = len(OUTCOMES) + 1
CHANNELS = {
    "whiff": "swinging_strike",
    "ball": "ball",
    "in_play": "in_play",
    "called": "called_strike",
}
N_TIERS = 5


def _ess_share(w: np.ndarray) -> float:
    s = float(w.sum())
    return float(s * s / float(w @ w) / w.size) if s > 0.0 else 0.0


def _season_of(key: str) -> int:
    try:
        return int(str(key).split(":")[-1])
    except ValueError:
        return 0


def _tier_ratio(own: np.ndarray, exp: np.ndarray, weight: np.ndarray) -> dict[str, float]:
    """Quintiles of the actors by their own rate (weighted by own rows): the
    expected rate's spread (top - bottom) over the own spread, plus the mean
    absolute per-actor gap."""
    order = np.argsort(own)
    cum = np.cumsum(weight[order])
    edges = np.searchsorted(cum, cum[-1] * np.arange(1, N_TIERS) / N_TIERS)
    groups = [g for g in np.split(order, edges) if g.size]
    if len(groups) < 2:
        return {"ratio": float("nan"), "gap": float("nan")}
    tier_own = [float(np.average(own[g], weights=weight[g])) for g in groups]
    tier_exp = [float(np.average(exp[g], weights=weight[g])) for g in groups]
    s_own = tier_own[-1] - tier_own[0]
    s_exp = tier_exp[-1] - tier_exp[0]
    return {
        "ratio": (s_exp / s_own) if s_own else float("nan"),
        "spread_own": s_own,
        "spread_exp": s_exp,
        "gap": float(np.average(np.abs(exp - own), weights=weight)),
        "tiers_own": tier_own,
        "tiers_exp": tier_exp,
    }


def _neutralize(w: np.ndarray, scored: np.ndarray) -> np.ndarray:
    """An unscored row takes the MEAN scored weight (draw-neutral)."""
    if scored.all():
        return w
    m = float(w[scored].mean()) if scored.any() else 1.0
    out = w.copy()
    out[~scored] = m
    return out


def scan_pitch(fp: Any, actor: str, powers: list[float], season: int, min_rows: int) -> dict:
    """The pitcher (pitcher_sim) or batter (actor_sim['batter']) factor over
    the pitch pools: expected per-outcome rates, standardized to the actor's
    own count mix, per power."""
    if actor == "pitcher":
        matrix = fp.a.pitcher_sim_matrix
        index = dict(fp.a.pitcher_sim_index)
        cols_by_hand = {h: fp._pool_meta(h)["pool_prof"] for h in fp.a.pools}
    else:
        entry = fp._actor_matrix("batter")
        e2m = fp._emb_to_mat("batter")
        if entry is None or e2m is None:
            return {"error": "no batter matrix"}
        matrix = entry["matrix"]
        index = dict(entry["index"])
        cols_by_hand = {}
        for h in fp.a.pools:
            pb = fp._pool_meta(h)["pool_bat"]
            cols_by_hand[h] = np.where(pb >= 0, e2m[np.clip(pb, 0, len(e2m) - 1)], -1)
    n_actor = matrix.shape[0]
    own = np.zeros((n_actor, 12 * N_OUT))
    per_hand = []
    for h in fp.a.pools:
        meta = fp._pool_meta(h)
        pool = meta["pool"]
        oc = np.fromiter(
            (_OUT_IDX.get(str(o), N_OUT - 1) for o in meta["outcome"]), dtype=np.int64, count=pool.n
        )
        balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
        strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
        cb = balls * 3 + strikes
        rcy = pool.recency.astype(np.float64)
        col = cols_by_hand[h]
        ok = col >= 0
        np.add.at(own, (col[ok], cb[ok] * N_OUT + oc[ok]), rcy[ok])
        per_hand.append((col, cb * N_OUT + oc, rcy, ok))
    unscored = float(np.mean(np.concatenate([~ph[3] for ph in per_hand])))
    own_rows = own.sum(axis=1)
    cands = [
        (k, r)
        for k, r in index.items()
        if _season_of(k) == season and r < n_actor and own_rows[r] >= min_rows
    ]
    print(
        f"  {actor}: {len(cands)} live actors of {season} with >= {min_rows} own rows; unscored rows {unscored:.4f}"
    )
    own_rate = own / np.maximum(own_rows[:, None], 1e-9)
    own_share = own.reshape(n_actor, 12, N_OUT).sum(axis=2) / np.maximum(own_rows[:, None], 1e-9)
    out: dict[str, Any] = {"n_actors": len(cands), "unscored_share": unscored, "powers": {}}
    t0 = time.perf_counter()
    for p in powers:
        exp_rates = np.zeros((len(cands), N_OUT))
        ess = []
        for ci, (_k, r) in enumerate(cands):
            acc = np.zeros(12 * N_OUT)
            wsum, wsq, n = 0.0, 0.0, 0
            for col, bin_, rcy, ok in per_hand:
                s = np.where(ok, matrix[r, np.clip(col, 0, n_actor - 1)], 0.0).astype(np.float64)
                w = rcy * np.power(np.clip(s, 0.0, None), p)
                w = _neutralize(w, ok)
                acc += np.bincount(bin_, weights=w, minlength=12 * N_OUT)
                wsum += float(w.sum())
                wsq += float(w @ w)
                n += w.size
            ess.append(wsum * wsum / wsq / n if wsq > 0 else 0.0)
            rb = acc.reshape(12, N_OUT)
            tot = rb.sum(axis=1)
            present = tot > 0
            sh = own_share[r] * present
            sh = sh / sh.sum() if sh.sum() > 0 else sh
            rates_b = rb / np.maximum(tot[:, None], 1e-9)
            exp_rates[ci] = sh @ rates_b
        rep: dict[str, Any] = {"ess_median": float(np.median(ess))}
        weight = np.array([own_rows[r] for _k, r in cands])
        for ch, oc_name in CHANNELS.items():
            oi = _OUT_IDX[oc_name]
            own_v = np.array([own_rate[r].reshape(12, N_OUT).sum(axis=0)[oi] for _k, r in cands])
            rep[ch] = _tier_ratio(own_v, exp_rates[:, oi], weight)
        out["powers"][str(p)] = rep
        print(
            f"    p={p:<4} ess {rep['ess_median']:.4f} "
            + " ".join(f"{ch} {rep[ch]['ratio']:.3f}" for ch in CHANNELS)
            + f"  ({time.perf_counter() - t0:.0f}s)",
            flush=True,
        )
    return out


def scan_rate(
    fp: Any,
    label: str,
    pools: dict[str, Any],
    rows_of: Any,
    matrix_name: str,
    powers: list[float],
    season: int,
    min_rows: int,
    attempted_of: Any,
) -> dict:
    """A binary-rate factor (the steal / advancement draws): the expected
    attempt rate under score^p vs the actor's own rows, per power."""
    entry = fp._actor_matrix(matrix_name)
    e2m = fp._emb_to_mat(matrix_name)
    if entry is None or e2m is None:
        return {"error": f"no {matrix_name} matrix"}
    matrix = entry["matrix"]
    index = dict(entry["index"])
    n_actor = matrix.shape[0]
    own = np.zeros((n_actor, 2))
    per_pool = []
    for key, pool in pools.items():
        rows = rows_of(key)
        if rows is None:
            continue
        col = np.where(rows >= 0, e2m[np.clip(rows, 0, len(e2m) - 1)], -1)
        att = attempted_of(pool).astype(np.float64)
        rcy = pool.recency.astype(np.float64)
        ok = col >= 0
        np.add.at(own, col[ok], np.column_stack([rcy[ok], rcy[ok] * att[ok]]))
        per_pool.append((col, att, rcy, ok))
    if not per_pool:
        return {"error": "no pools"}
    unscored = float(np.mean(np.concatenate([~pp[3] for pp in per_pool])))
    cands = [
        (k, r)
        for k, r in index.items()
        if _season_of(k) == season and r < n_actor and own[r, 0] >= min_rows
    ]
    print(
        f"  {label}: {len(cands)} live actors of {season} with >= {min_rows} own rows; unscored rows {unscored:.4f}"
    )
    out: dict[str, Any] = {"n_actors": len(cands), "unscored_share": unscored, "powers": {}}
    own_rate = own[:, 1] / np.maximum(own[:, 0], 1e-9)
    weight = np.array([own[r, 0] for _k, r in cands])
    own_v = np.array([own_rate[r] for _k, r in cands])
    for p in powers:
        exp_v = np.zeros(len(cands))
        ess = []
        for ci, (_k, r) in enumerate(cands):
            num = den = wsum = wsq = 0.0
            n = 0
            for col, att, rcy, ok in per_pool:
                s = np.where(ok, matrix[r, np.clip(col, 0, n_actor - 1)], 0.0).astype(np.float64)
                w = _neutralize(rcy * np.power(np.clip(s, 0.0, None), p), ok)
                num += float((w * att).sum())
                den += float(w.sum())
                wsum += float(w.sum())
                wsq += float(w @ w)
                n += w.size
            exp_v[ci] = num / den if den > 0 else 0.0
            ess.append(wsum * wsum / wsq / n if wsq > 0 else 0.0)
        rep = {"ess_median": float(np.median(ess)), "attempt": _tier_ratio(own_v, exp_v, weight)}
        out["powers"][str(p)] = rep
        print(
            f"    p={p:<4} ess {rep['ess_median']:.4f} attempt ratio {rep['attempt']['ratio']:.3f} gap {rep['attempt']['gap']:.4f}",
            flush=True,
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--powers", default="1,2,3,5,8,12,20")
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--min-rows", type=int, default=1500)
    ap.add_argument("--min-opps", type=int, default=150)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--game-pk", type=int, default=744795)
    args = ap.parse_args()
    powers = [float(x) for x in args.powers.split(",") if x.strip()]

    duck = open_sim_duckdb()
    try:
        state = asyncio.run(_resolve(args.game_pk, duck))
    finally:
        if duck is not None:
            duck.close()
    kw = sim_kwargs_from_state(state)
    machine = production_machine_factory(0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw)))
    fp = machine.full_pool_sampler
    fp.actor_matrices = True

    report: dict[str, Any] = {"powers": powers, "season": args.season}
    print("=== the offline power scan ===")
    report["pitcher"] = scan_pitch(fp, "pitcher", powers, args.season, args.min_rows)
    report["batter"] = scan_pitch(fp, "batter", powers, args.season, args.min_rows)
    report["steal_runner"] = scan_rate(
        fp,
        "steal draw by runner",
        fp.a.steal_pools,
        lambda k: (fp._steal_meta(k) or {}).get("runner_rows"),
        "runner_steal",
        powers,
        args.season,
        args.min_opps,
        lambda pool: pool.attempted,
    )
    report["steal_catcher"] = scan_rate(
        fp,
        "steal draw by catcher",
        fp.a.steal_pools,
        lambda k: (fp._steal_meta(k) or {}).get("catcher_rows"),
        "catcher_throwing",
        powers,
        args.season,
        args.min_opps,
        lambda pool: pool.attempted,
    )
    report["steal_pitcher"] = scan_rate(
        fp,
        "steal draw by pitcher",
        fp.a.steal_pools,
        lambda k: (fp._steal_meta(k) or {}).get("pitcher_rows"),
        "pitcher_steal",
        powers,
        args.season,
        args.min_opps,
        lambda pool: pool.attempted,
    )
    report["adv_runner"] = scan_rate(
        fp,
        "advancement draws by runner",
        fp.a.adv_pools,
        lambda k: (fp._adv_meta(k) or {}).get("runner_rows"),
        "runner_adv",
        powers,
        args.season,
        args.min_opps,
        lambda pool: pool.attempted,
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, default=float)
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
