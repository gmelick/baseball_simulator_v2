"""
scripts/sim523_kernel_scan.py — SIM-523 part F: the offline BANDWIDTH scan of the
(SUPERSEDED 2026-09-09: the runner kernels are retired; the runner engines score
thin profiles and the power scan, scripts/sim523_power_scan.py, covers the runner
matrices. Kept as the record of part F's bandwidth fit.)
runner kernels (the steal draw's runner factor, the advancement draws' runner
factor), the companion of scripts/sim523_power_scan.py for the two factors
whose score matrices cannot work (two thirds of steal rows and half of
advancement rows carry no runner score).

For every live runner of the season with enough own rows, the expected
attempt rate under the kernel weights exp(-d^2 / (2 sigma^2 k)) over the
runner's z-scored features (the sampler's own kernel), for a ladder of
bandwidths, against his own rows — reported as the tier spread ratio and the
effective sample share; both with the sampler's rule for an unscored row
(weight 1.0) and with the draw-neutral rule (the mean scored weight).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_kernel_scan.py --sigmas 1.0,0.5,0.35,0.25,0.15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim523_fit_probe import _keys_by_row  # noqa: E402
from sim523_power_scan import _season_of, _tier_ratio  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402


def scan_kernel(
    fp: Any,
    label: str,
    pools: dict[str, Any],
    rows_of: Any,
    feat_names: tuple[str, ...],
    sigmas: list[float],
    season: int,
    min_rows: int,
) -> dict:
    actor = "baserunner"
    z = fp._emb_z(actor)
    emb = fp.a.actor_emb.get(actor)
    cols = fp._steal_feat_cols(actor, feat_names)
    keys = _keys_by_row(emb)
    if z is None or emb is None or cols is None or keys is None:
        return {"error": "no baserunner embedding / features"}
    per_pool = []
    own = np.zeros((len(keys), 2))
    for key, pool in pools.items():
        rows = rows_of(key)
        if rows is None:
            continue
        att = pool.attempted.astype(np.float64)
        rcy = pool.recency.astype(np.float64)
        ok = rows >= 0
        np.add.at(own, rows[ok], np.column_stack([rcy[ok], rcy[ok] * att[ok]]))
        per_pool.append((rows, att, rcy, ok))
    unscored = float(np.mean(np.concatenate([~pp[3] for pp in per_pool])))
    cands = [
        (k, r) for r, k in enumerate(keys) if _season_of(k) == season and own[r, 0] >= min_rows
    ]
    print(
        f"  {label}: {len(cands)} live runners of {season} with >= {min_rows} own rows; unscored rows {unscored:.4f}"
    )
    own_rate = own[:, 1] / np.maximum(own[:, 0], 1e-9)
    weight = np.array([own[r, 0] for _k, r in cands])
    own_v = np.array([own_rate[r] for _k, r in cands])
    zc = z[:, cols].astype(np.float64)
    k = float(len(cols))
    out: dict[str, Any] = {"n_actors": len(cands), "unscored_share": unscored, "sigmas": {}}
    for sigma in sigmas:
        for rule in ("sampler", "neutral"):
            exp_v = np.zeros(len(cands))
            ess = []
            for ci, (_k, r) in enumerate(cands):
                live = zc[r]
                num = den = wsum = wsq = 0.0
                n = 0
                for rows, att, rcy, ok in per_pool:
                    d2 = ((zc[np.clip(rows, 0, len(zc) - 1)] - live) ** 2).sum(axis=1)
                    f = np.exp(-d2 / (2.0 * sigma * sigma * k))
                    if rule == "sampler":
                        f = np.where(ok, f, 1.0)
                    else:
                        f = np.where(ok, f, float(f[ok].mean()) if ok.any() else 1.0)
                    w = rcy * f
                    num += float((w * att).sum())
                    den += float(w.sum())
                    wsum += float(w.sum())
                    wsq += float(w @ w)
                    n += w.size
                exp_v[ci] = num / den if den > 0 else 0.0
                ess.append(wsum * wsum / wsq / n if wsq > 0 else 0.0)
            rep = {
                "ess_median": float(np.median(ess)),
                "attempt": _tier_ratio(own_v, exp_v, weight),
            }
            out["sigmas"][f"{sigma}:{rule}"] = rep
            a = rep["attempt"]
            print(
                f"    sigma={sigma:<5} {rule:>7} ess {rep['ess_median']:.4f} ratio {a['ratio']:.3f} "
                f"gap {a['gap']:.4f} tiers exp {[round(x, 4) for x in a['tiers_exp']]} own {[round(x, 4) for x in a['tiers_own']]}",
                flush=True,
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sigmas", default="1.0,0.5,0.35,0.25,0.15")
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--min-opps", type=int, default=150)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--game-pk", type=int, default=744795)
    args = ap.parse_args()
    sigmas = [float(x) for x in args.sigmas.split(",") if x.strip()]
    duck = open_sim_duckdb()
    try:
        state = asyncio.run(_resolve(args.game_pk, duck))
    finally:
        if duck is not None:
            duck.close()
    kw = sim_kwargs_from_state(state)
    machine = production_machine_factory(0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw)))
    fp = machine.full_pool_sampler
    report: dict[str, Any] = {"sigmas": sigmas, "season": args.season}
    print("=== the offline kernel-bandwidth scan (the runner kernels) ===")
    report["steal_runner"] = scan_kernel(
        fp,
        "steal draw by runner",
        fp.a.steal_pools,
        lambda k: (fp._steal_meta(k) or {}).get("runner_rows"),
        fp._RUNNER_STEAL_FEATURES,
        sigmas,
        args.season,
        args.min_opps,
    )
    report["adv_runner"] = scan_kernel(
        fp,
        "advancement draws by runner",
        fp.a.adv_pools,
        lambda k: (fp._adv_meta(k) or {}).get("runner_rows"),
        fp._RUNNER_ADV_FEATURES,
        sigmas,
        args.season,
        args.min_opps,
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, default=float)
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
