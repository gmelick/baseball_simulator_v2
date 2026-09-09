"""
scripts/sim523_manager_probe.py — SIM-523 part D: the pitching change as a DRAW vs the formula.

For each game, run --iters iterations per arm on the production configuration
(the manager on) with the SIM-434 pull formula, then with the pitching-change
draw, and print per team-game: pitchers used, starter pitches at the pull and
starter batters faced, changes at half-inning boundaries vs mid-inning, plus
runs / hits / walks / strikeouts per game and the wall time per iteration.
The pool's own rates (its manifest) are the reference: the draw should
reproduce the pool's change rate per boundary and its split.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_manager_probe.py --iters 30 744795 661032 564734 825108
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from sim_stats import (  # noqa: E402
    _FACTORY,
    _game_summary,
    _resolve,
    open_sim_duckdb,
    sim_kwargs_from_state,
)

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402


def _ids(state):
    home = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
    away = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
    if getattr(state, "home_pitcher_id", None):
        home.add(int(state.home_pitcher_id))
    if getattr(state, "away_pitcher_id", None):
        away.add(int(state.away_pitcher_id))
    return home, away


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--sit-sigma", type=float, default=1.0)
    ap.add_argument("--pitcher-power", type=float, default=1.0)
    ap.add_argument("--min-cell", type=int, default=20)
    args = ap.parse_args()

    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in args.game_pks]
    finally:
        if duck is not None:
            duck.close()

    arms = ("formula", "draw")
    box = {a: [] for a in arms}
    tally = {a: Counter() for a in arms}
    secs = dict.fromkeys(arms, 0.0)
    widen = {a: np.zeros(4, dtype=np.int64) for a in arms}
    manifest = None
    for state in states:
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        fp.change_sit_sigma = args.sit_sigma
        fp.change_pitcher_power = args.pitcher_power
        fp.change_min_cell = args.min_cell
        if manifest is None:
            path = os.path.join(
                os.environ.get("SIM_ARTIFACT_DIR", "/data/play_pool/engine_artifacts"),
                "manager_pool",
                "manifest.json",
            )
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
        for arm in arms:
            machine.manager_draw = arm == "draw"
            fp.change_widen_counts[:] = 0
            machine._fp_pitcher_key = None
            machine._fp_pa_key = None
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.manager_decisions = []
                machine.boxscore = BoxScore()
                res = simulate_game(state_machine=machine, seed=seed, **kw)
                home_ids, away_ids = _ids(state)
                box[arm].append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
                changes = [d for d in machine.manager_decisions if d["kind"] == "pitching_change"]
                t = tally[arm]
                t["games"] += 1
                t["changes"] += len(changes)
                starters = {
                    int(getattr(state, "home_pitcher_id", 0) or 0),
                    int(getattr(state, "away_pitcher_id", 0) or 0),
                }
                for d in changes:
                    if d["out_pitcher_id"] in starters:
                        t["starter_pulls"] += 1
                        t["starter_pc_at_pull"] += int(d.get("pitch_count", 0))
                        t["starter_bf_at_pull"] += int(d.get("batters_faced", 0))
                    t["forced"] += int(bool(d.get("forced")))
                    t["new_half_changes"] += int(d.get("new_half", 0))
            secs[arm] += time.perf_counter() - t0
            widen[arm] += fp.change_widen_counts
        print(f"  game {kw.get('game_pk', '?')} done", flush=True)

    print(
        f"\n=== the pitching change: {len(states)} games x {args.iters} iterations per arm "
        f"(sit sigma {args.sit_sigma}, pitcher power {args.pitcher_power}, min cell {args.min_cell}) ==="
    )
    if manifest:
        r = manifest["rates"]
        print(
            f"  the pool: {manifest['count']} boundaries over {manifest['games']} games; change rate "
            f"{r['changed']:.4f} per boundary (half-inning {r['changed_new_half']:.4f}, mid-inning "
            f"{r['changed_mid_inning']:.4f}; starter {r['changed_starter']:.4f}, reliever {r['changed_reliever']:.4f})"
        )
    print(
        f"  {'arm':>8} {'pitchers/team-game':>19} {'starter pulls/game':>19} {'starter PC at pull':>19} "
        f"{'starter BF at pull':>19} {'half-inning changes':>20} {'forced':>7} {'R/g':>6} {'H/g':>6} "
        f"{'BB/g':>6} {'K/g':>6} {'s/iter':>7}"
    )
    for arm in arms:
        t = tally[arm]
        g = max(1, t["games"])
        b = box[arm]
        print(
            f"  {arm:>8} {1 + t['changes'] / (2 * g):19.2f} {t['starter_pulls'] / g:19.2f} "
            f"{t['starter_pc_at_pull'] / max(1, t['starter_pulls']):19.1f} "
            f"{t['starter_bf_at_pull'] / max(1, t['starter_pulls']):19.1f} "
            f"{t['new_half_changes'] / max(1, t['changes']):20.1%} {t['forced'] / g:7.2f} "
            f"{np.mean([x['R'] for x in b]):6.2f} {np.mean([x.get('H', 0) for x in b]):6.2f} "
            f"{np.mean([x['BB'] for x in b]):6.2f} {np.mean([x['K'] for x in b]):6.2f} {secs[arm] / len(b):7.2f}"
        )
    print(f"  draw widening levels 0..3: {widen['draw'].tolist()}")
    print("  Read: pitchers per team-game = 1 + changes per side; MLB ~4.2 (the plan's estimate).")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SIM_PITCH_CELL_INDEX", "1")
    sys.exit(main())
