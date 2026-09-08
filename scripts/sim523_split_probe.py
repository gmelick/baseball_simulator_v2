"""
scripts/sim523_split_probe.py — SIM-523 part B: the pitch / pitch-result split, ON vs OFF.

For each game, run --iters iterations per arm on the PRODUCTION configuration
(the cell index on, the receiving kernel off) with the split OFF, then ON at
the given bandwidth and powers, and print per-game means of the pitch-result
mix (balls, called and swinging strikes, fouls, in play, hit-by-pitch per
pitch), pitches per plate appearance, walks, strikeouts, hits, home runs and
runs, plus the share of in-play results that carried a born batted ball and
the wall time per iteration. At powers of 1.0 the split conditions the
result on the pitch without moving the pitcher / batter balance, so the
marginals should move little; the powers are part F's fit.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_split_probe.py --iters 60 744795 661032 564734 825108
    ... --sigma 0.5 --result-pitcher-power 0.5 --result-batter-power 1.5
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
    """Count pitch-result outcomes, plate appearances and born batted balls."""

    def __init__(self, fp):
        self.fp = fp
        self.out: Counter = Counter()
        self._draw = fp.draw
        self._npa = fp.new_plate_appearance

    def install(self):
        tap = self

        def draw(balls=0, strikes=0):
            o = tap._draw(balls, strikes)
            tap.out[o] += 1
            tap.out["pitches"] += 1
            if o == "in_play":
                tap.out["born"] += int(tap.fp.last_born_batted_ball() is not None)
            return o

        def npa(batter_key, base_out, **kw):
            tap.out["pa_setups"] += 1
            return tap._npa(batter_key, base_out, **kw)

        self.fp.draw = draw
        self.fp.new_plate_appearance = npa

    def remove(self):
        self.fp.draw = self._draw
        self.fp.new_plate_appearance = self._npa


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
    ap.add_argument("--sigma", type=float, default=1.0)
    ap.add_argument("--density-power", type=float, default=1.0)
    ap.add_argument("--result-pitcher-power", type=float, default=1.0)
    ap.add_argument("--result-batter-power", type=float, default=1.0)
    ap.add_argument("--pitch-batter-power", type=float, default=1.0)
    ap.add_argument("--born-sigma", type=float, default=0.0)
    args = ap.parse_args()

    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in args.game_pks]
    finally:
        if duck is not None:
            duck.close()

    arms = ("split OFF", "split ON")
    taps = {a: Counter() for a in arms}
    box = {a: [] for a in arms}
    secs = dict.fromkeys(arms, 0.0)
    for state in states:
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        for arm in arms:
            on = arm == "split ON"
            fp.pitch_result_split = on
            fp.result_pitch_sigma = args.sigma
            fp.result_density_power = args.density_power
            fp.result_pitcher_power = args.result_pitcher_power
            fp.result_batter_power = args.result_batter_power
            fp.pitch_batter_power = args.pitch_batter_power
            fp.bb_born_sigma = args.born_sigma if on else 0.0
            tap = _Tap(fp)
            tap.install()
            t0 = time.perf_counter()
            box[arm].extend(_run(machine, kw, list(range(args.iters)), *_ids(state)))
            secs[arm] += time.perf_counter() - t0
            tap.remove()
            taps[arm].update(tap.out)
        print(f"  game {kw.get('game_pk', '?')} done", flush=True)

    n_games = max(1, len(states))
    print(
        f"\n=== the pitch / pitch-result split: {len(states)} games x {args.iters} iterations per arm "
        f"(sigma {args.sigma}, density power {args.density_power}, powers pitcher {args.result_pitcher_power} / batter "
        f"{args.result_batter_power} / pitch-batter {args.pitch_batter_power}, born sigma {args.born_sigma}) ==="
    )
    cols = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
    print(f"  {'arm':>10} " + " ".join(f"{c[:8]:>9}" for c in cols) + f" {'pit/PA':>7} {'born':>6}")
    for arm in arms:
        c = taps[arm]
        n = max(1, c["pitches"])
        pas = max(1, sum(int(b.get("PA", 0)) for b in box[arm]) or c["pa_setups"])
        born = c["born"] / max(1, c["in_play"])
        print(
            f"  {arm:>10} "
            + " ".join(f"{c[k] / n:9.4f}" for k in cols)
            + f" {c['pitches'] / pas:7.3f} {born:6.1%}"
        )
    print(f"\n  {'arm':>10} {'R/g':>6} {'H/g':>6} {'HR/g':>6} {'BB/g':>6} {'K/g':>6} {'s/iter':>7}")
    for arm in arms:
        b = box[arm]
        n_it = max(1, len(b))
        print(
            f"  {arm:>10} {np.mean([x['R'] for x in b]):6.2f} {np.mean([x.get('H', 0) for x in b]):6.2f} "
            f"{np.mean([x.get('HR', 0) for x in b]):6.2f} {np.mean([x['BB'] for x in b]):6.2f} "
            f"{np.mean([x['K'] for x in b]):6.2f} {secs[arm] / n_it:7.2f}"
        )
    print(
        "  Read: per-game means over both teams; 'born' = share of in-play results whose "
        "result row carried its own batted ball. Games:",
        n_games,
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SIM_PITCH_CELL_INDEX", "1")
    sys.exit(main())
