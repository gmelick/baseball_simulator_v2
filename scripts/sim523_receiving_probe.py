"""
scripts/sim523_receiving_probe.py — SIM-523 part E: the catcher receiving RATIO, on vs off.

For each game, run --iters iterations per arm with the ratio OFF, ON with the
real catchers, ON with the pool's best framer behind both plates and ON with
its worst (by the outside-zone multiplier), and print the called-strike share
of taken pitches, the taken share of all pitches, pitches per plate
appearance, walks and strikeouts per game. The invariant to read: the taken
share and pitches per plate appearance do not move; the called-strike share
of taken pitches does, in the catcher's direction. The best and worst framer
are picked per game SEASON, because a catcher key of a season the document
lacks is neutral by design: choose games inside the pool window (2023-2026),
and add the "OFF best framer" / "OFF worst framer" arms to separate the
substitution's own footprint (the steal draw reads the catcher too) from
the ratio.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -e SIM_MANAGER=1 \\
        -v "$PWD/scripts:/app/scripts" app python scripts/sim523_receiving_probe.py \\
        --iters 60 --arms "OFF,ON real,OFF best framer,ON best framer,OFF worst framer,ON worst framer" \\
        717809 744795 777557 825108
"""

from __future__ import annotations

import argparse
import asyncio
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


class _Tap:
    def __init__(self, fp):
        self.fp = fp
        self.out: Counter = Counter()
        self._draw = fp.draw

    def install(self):
        tap = self

        def draw(balls=0, strikes=0):
            o = tap._draw(balls, strikes)
            tap.out[o] += 1
            tap.out["pitches"] += 1
            if int(balls) == 0 and int(strikes) == 0:
                tap.out["pa"] += 1  # the first pitch of a plate appearance
            return o

        self.fp.draw = draw

    def remove(self):
        self.fp.draw = self._draw


def _ids(state):
    home = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
    away = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
    if getattr(state, "home_pitcher_id", None):
        home.add(int(state.home_pitcher_id))
    if getattr(state, "away_pitcher_id", None):
        away.add(int(state.away_pitcher_id))
    return home, away


def _run(machine, kw, seeds, home_ids, away_ids):
    machine._fp_pitcher_key = None
    machine._fp_pa_key = None
    sums = []
    for seed in seeds:
        machine.boxscore = BoxScore()
        res = simulate_game(state_machine=machine, seed=seed, **kw)
        sums.append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
    return sums


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument(
        "--arms",
        default="OFF,ON real,ON best framer,ON worst framer",
        help="comma-separated arms; an arm starting with ON turns the ratio on; an arm "
        "ending in 'best framer' or 'worst framer' puts that catcher behind both plates "
        "(so 'OFF worst framer' isolates the substitution from the ratio)",
    )
    args = ap.parse_args()

    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in args.game_pks]
    finally:
        if duck is not None:
            duck.close()

    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    taps = {a: Counter() for a in arms}
    box = {a: [] for a in arms}
    secs = dict.fromkeys(arms, 0.0)
    picks: dict[int, tuple] = {}
    for gp, state in zip(args.game_pks, states, strict=True):
        kw = sim_kwargs_from_state(state)
        season = int(kw.get("season", 2024))
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        table = getattr(fp.a, "receiving", None) or {}
        if season not in picks and table.get("catchers"):
            ranked = sorted(
                (
                    (v["frame"].get("outside", 1.0), k)
                    for k, v in table["catchers"].items()
                    if k.endswith(f":{season}") and v.get("taken", 0) >= 3000
                ),
                key=lambda t: t[0],
            )
            if ranked:
                picks[season] = (ranked[0], ranked[-1])
                worst, best = picks[season]
                print(
                    f"  {season}: the pool's best framer {best[1]} (outside x{best[0]:.3f}), "
                    f"worst {worst[1]} (x{worst[0]:.3f}); real catchers "
                    f"{kw.get('home_catcher_id')} / {kw.get('away_catcher_id')}"
                )
        worst, best = picks.get(season, (None, None))
        if best is None:
            print(
                f"  {season}: no catcher-season in the document; the substitution arms keep the real catchers"
            )
        for arm in arms:
            arm_kw = dict(kw)
            fp.catcher_receiving = arm.startswith("ON")
            if arm.endswith("best framer") and best is not None:
                cid = int(best[1].split(":")[0])
                arm_kw["home_catcher_id"] = cid
                arm_kw["away_catcher_id"] = cid
            if arm.endswith("worst framer") and worst is not None:
                cid = int(worst[1].split(":")[0])
                arm_kw["home_catcher_id"] = cid
                arm_kw["away_catcher_id"] = cid
            tap = _Tap(fp)
            tap.install()
            t0 = time.perf_counter()
            box[arm].extend(_run(machine, arm_kw, list(range(args.iters)), *_ids(state)))
            secs[arm] += time.perf_counter() - t0
            tap.remove()
            taps[arm].update(tap.out)
        print(f"  game {gp} done", flush=True)

    print(f"\n=== the receiving ratio: {len(states)} games x {args.iters} iterations per arm ===")
    print(
        f"  {'arm':>16} {'taken share':>12} {'CS of taken':>12} {'ball':>7} {'in play':>8} "
        f"{'pit/PA':>7} {'PA/g':>6} {'BB/g':>6} {'K/g':>6} {'R/g':>6} {'SB/g':>5} {'CS/g':>5} {'s/iter':>7}"
    )
    for arm in arms:
        c = taps[arm]
        n = max(1, c["pitches"])
        taken = c["called_strike"] + c["ball"]
        b = box[arm]
        pas = max(1, c["pa"])
        print(
            f"  {arm:>16} {taken / n:12.4f} {c['called_strike'] / max(1, taken):12.4f} "
            f"{c['ball'] / n:7.4f} {c['in_play'] / n:8.4f} {c['pitches'] / pas:7.3f} "
            f"{pas / max(1, len(b)):6.1f} "
            f"{np.mean([x['BB'] for x in b]):6.2f} {np.mean([x['K'] for x in b]):6.2f} "
            f"{np.mean([x['R'] for x in b]):6.2f} {np.mean([x['SB'] for x in b]):5.2f} "
            f"{np.mean([x['CS'] for x in b]):5.2f} {secs[arm] / max(1, len(b)):7.2f}"
        )
    print(
        "  Read: the taken share and pitches per plate appearance must hold; the called-strike share of taken pitches moves with the catcher."
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SIM_PITCH_CELL_INDEX", "1")
    sys.exit(main())
