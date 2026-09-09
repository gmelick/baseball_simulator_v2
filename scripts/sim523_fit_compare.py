"""
scripts/sim523_fit_compare.py — SIM-523 part F: compare the fit probe's arms side by side.

Reads the JSON reports scripts/sim523_fit_probe.py wrote (one per arm) and
prints, per arm: the per-channel tier-spread ratio (sim / own; 1.0 = the
factor reproduces the pool's own conditional spread), the tier residual
(the per-tier delta's spread around its mean — composition-free), the
factor strengths, the steal and advancement attempt rates, the ground-ball
reach by speed tier and the box means. The fit rule: the SMALLEST power
whose spread ratio is inside the noise of 1.0 on every channel it shapes,
with the ordering (pitcher, then batter, then recency) intact.

    python scripts/sim523_fit_compare.py scripts/sim523_fit_p1.json scripts/sim523_fit_p2.json ...
"""

from __future__ import annotations

import json
import sys


def _r(x, nd=3):
    return "-" if x is None else f"{x:.{nd}f}"


def main(paths: list[str]) -> int:
    arms = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        cfg = d.get("config", {})
        tag = (
            f"pP{cfg.get('SIM_PITCH_PITCHER_POWER', '1')}/rP{cfg.get('SIM_RESULT_PITCHER_POWER', '1')}"
            f"/rB{cfg.get('SIM_RESULT_BATTER_POWER', '1')}/F{cfg.get('SIM_ACTOR_POWER_FIELDER', '1')}"
            f"/born{cfg.get('SIM_BB_BORN_SIGMA', '0')}/spd{cfg.get('SIM_BB_SPEED_SIGMA', '0')}"
            f"/cls{cfg.get('SIM_BB_CLASS_FILTER', '0')}/fence{cfg.get('SIM_FENCE_STAGE', '0')}"
            f"/rs{cfg.get('SIM_STEAL_RUNNER_SIGMA', '') or '-'}"
        )
        arms.append((tag, d))
    print(f"{'arm':>48} | " + " | ".join(f"{t:>44}" for t, _ in arms))
    for actor in ("pitcher", "batter"):
        for ch in ("whiff", "ball", "in_play", "called"):
            cells = []
            for _, d in arms:
                r = d.get(actor, {}).get(ch)
                if not r:
                    cells.append(f"{'-':>44}")
                    continue
                cells.append(
                    f"ratio {_r(r['spread_ratio'])} sim {r['spread_sim']:+.4f} own {r['spread_own']:+.4f} "
                    f"res {r['tier_residual']:.4f} d {r['mean_delta']:+.4f}"
                )
            print(f"{actor + ' ' + ch:>48} | " + " | ".join(f"{c:>44}" for c in cells))
    print(
        f"{'ESS pitcher/batter/recency':>48} | "
        + " | ".join(
            f"{_r(d['ess'].get('pitcher'))} / {_r(d['ess'].get('batter'))} / {_r(d['ess'].get('recency'))}".rjust(
                44
            )
            for _, d in arms
        )
    )
    print(
        f"{'0-0 candidate rows / effective / widen':>48} | "
        + " | ".join(
            (
                f"{_r(d.get('candidate_rows', {}).get('median_rows'), 0)} / "
                f"{_r(d.get('candidate_rows', {}).get('median_effective'), 0)} / "
                f"{d.get('candidate_rows', {}).get('widen_counts')}"
            ).rjust(44)
            for _, d in arms
        )
    )
    for key in ("steal_runner", "steal_catcher", "steal_pitcher", "adv_runner"):
        cells = []
        for _, d in arms:
            r = d.get(key, {})
            tiers = r.get("tiers", [])
            spread = ""
            if len(tiers) >= 2:
                spread = (
                    f" tier0 {tiers[0]['sim_att']:.4f}/{tiers[0]['own_att']:.4f}"
                    f" top {tiers[-1]['sim_att']:.4f}/{tiers[-1]['own_att']:.4f}"
                )
            cells.append(
                f"n{r.get('n_actors', 0)} att {_r(r.get('sim_att_all'), 4)}/{_r(r.get('own_att_all'), 4)}{spread}"
            )
        print(f"{key:>48} | " + " | ".join(f"{c:>44}" for c in cells))
    for tier in ("low", "mid", "high"):
        cells = []
        for _, d in arms:
            r = d.get("fielding", {}).get("gb_reach_by_speed", {}).get(tier, {})
            cells.append(
                f"n{r.get('n', 0)} sim {_r(r.get('sim_reach'), 4)} own {_r(r.get('own_reach'), 4)}"
            )
        print(f"{'GB reach speed ' + tier:>48} | " + " | ".join(f"{c:>44}" for c in cells))
    for cls in ("1", "2", "3", "4"):
        cells = []
        for _, d in arms:
            r = d.get("fielding", {}).get("by_born_class", {}).get(cls)
            if not r or not r.get("sim"):
                cells.append(f"{'-':>44}")
                continue
            s, o = r["sim"], r["own"]
            cells.append(
                f"n{int(r['n'])} agree {_r(r.get('agree'))} out {s['out']:.3f}/{o.get('out', 0):.3f} "
                f"HR {s['home_run']:.3f}/{o.get('home_run', 0):.3f} 1B {s['single']:.3f}/{o.get('single', 0):.3f}"
            )
        print(
            f"{'born class ' + cls + ' (sim/own)':>48} | " + " | ".join(f"{c:>44}" for c in cells)
        )
    print(
        f"{'box BB/K/H/HR/R per game; pitches/PA':>48} | "
        + " | ".join(
            (
                f"{d['box']['BB']:.2f}/{d['box']['K']:.2f}/{d['box']['H']:.2f}/{d['box']['HR']:.2f}/"
                f"{d['box']['R']:.2f}; {d['pitches'] / max(1, d['pas']):.3f}"
            ).rjust(44)
            for _, d in arms
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
