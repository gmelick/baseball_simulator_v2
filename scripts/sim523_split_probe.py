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
        self._bnpa = fp.battedball_new_pa
        self._bdraw = fp.battedball_draw
        self._born_row = None

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

        def bnpa(hand, batter_key, state, **kw):
            born = kw.get("born_bb")
            tap._born_row = (hand, born["row"]) if born and born.get("row") is not None else None
            return tap._bnpa(hand, batter_key, state, **kw)

        def bdraw():
            out = tap._bdraw()
            # The bias check: the drawn play's hit / home run against the born
            # row's own — an unbiased fielding draw matches the born rows' rates.
            if tap._born_row is not None:
                hand, row = tap._born_row
                pool = tap.fp.a.bb_pools[hand]
                tap.out["born_n"] += 1
                tap.out["born_hit"] += int(pool.result_hits[row] > 0)
                tap.out["born_hr"] += int(pool.result_hits[row] == 4)
                tap.out["drawn_hit"] += int(out[1] > 0)
                tap.out["drawn_hr"] += int(out[1] == 4)
            return out

        self.fp.draw = draw
        self.fp.new_plate_appearance = npa
        self.fp.battedball_new_pa = bnpa
        self.fp.battedball_draw = bdraw

    def remove(self):
        self.fp.draw = self._draw
        self.fp.new_plate_appearance = self._npa
        self.fp.battedball_new_pa = self._bnpa
        self.fp.battedball_draw = self._bdraw


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
    # SIM-523 part C: the fielding draw's filters and the fence stage (ON arm only).
    ap.add_argument("--class-filter", action="store_true")
    ap.add_argument("--speed-sigma", type=float, default=0.0)
    ap.add_argument("--wall-zone-only", action="store_true")
    ap.add_argument("--fence-stage", action="store_true")
    ap.add_argument("--fence-margin", type=float, default=10.0)
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
    per_game = {a: [] for a in arms}
    secs = dict.fromkeys(arms, 0.0)
    fp = None
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
            fp.bb_class_filter = bool(args.class_filter and on)
            fp.bb_speed_sigma = args.speed_sigma if on else 0.0
            fp.park_wall_zone_only = bool(args.wall_zone_only and on)
            fp.fence_stage = bool(args.fence_stage and on)
            fp.fence_margin = args.fence_margin
            fp.fence_counts[:] = 0
            fp.bb_class_counts[:] = 0
            fp.bb_zero_weight_count = 0
            tap = _Tap(fp)
            tap.install()
            t0 = time.perf_counter()
            box[arm].extend(_run(machine, kw, list(range(args.iters)), *_ids(state)))
            secs[arm] += time.perf_counter() - t0
            tap.remove()
            taps[arm].update(tap.out)
            taps[arm].update({f"fence_{i}": int(c) for i, c in enumerate(fp.fence_counts)})
            taps[arm].update({f"class_{i}": int(c) for i, c in enumerate(fp.bb_class_counts)})
            taps[arm]["zero_weight"] += int(fp.bb_zero_weight_count)
            per_game[arm].append(
                (
                    kw.get("game_pk", "?"),
                    getattr(state, "park", None),
                    sum(x.get("HR", 0) for x in box[arm][-args.iters :]),
                    int(tap.out["in_play"]),
                )
            )
        print(
            f"  game {kw.get('game_pk', '?')} (venue {getattr(state, 'park', None)}) done",
            flush=True,
        )

    n_games = max(1, len(states))
    print(
        f"\n=== the pitch / pitch-result split: {len(states)} games x {args.iters} iterations per arm "
        f"(sigma {args.sigma}, density power {args.density_power}, powers pitcher {args.result_pitcher_power} / batter "
        f"{args.result_batter_power} / pitch-batter {args.pitch_batter_power}, born sigma {args.born_sigma}; "
        f"class filter {args.class_filter}, speed sigma {args.speed_sigma}, wall-zone-only "
        f"{args.wall_zone_only}, fence stage {args.fence_stage} margin {args.fence_margin}) ==="
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
    print(
        f"\n  {'arm':>10} {'R/g':>6} {'H/g':>6} {'2B/g':>6} {'HR/g':>6} {'BB/g':>6} {'K/g':>6} "
        f"{'s/iter':>7}"
    )
    for arm in arms:
        b = box[arm]
        n_it = max(1, len(b))
        print(
            f"  {arm:>10} {np.mean([x['R'] for x in b]):6.2f} {np.mean([x.get('H', 0) for x in b]):6.2f} "
            f"{np.mean([x.get('2B', 0) for x in b]):6.2f} "
            f"{np.mean([x.get('HR', 0) for x in b]):6.2f} {np.mean([x['BB'] for x in b]):6.2f} "
            f"{np.mean([x['K'] for x in b]):6.2f} {secs[arm] / n_it:7.2f}"
        )
    c = taps["split ON"]
    fc = [c[f"fence_{i}"] for i in range(5)]
    print(
        f"  fence stage (ON arm): over {fc[0]}, short {fc[1]}, band {fc[2]}, passed {fc[3]}, "
        f"no rows {fc[4]}; class filter: filtered {c['class_0']}, "
        f"empty-class fallback {c['class_1']}"
    )
    if c["born_n"]:
        print(
            f"  bias check (ON arm, {c['born_n']} balls in play with a born ball): the born rows' "
            f"own hit share {c['born_hit'] / c['born_n']:.4f} vs the drawn plays' {c['drawn_hit'] / c['born_n']:.4f}; "
            f"home-run share {c['born_hr'] / c['born_n']:.4f} vs {c['drawn_hr'] / c['born_n']:.4f}; "
            f"zero-weight fallbacks {c['zero_weight']}"
        )
    # The per-park check: home runs per ball in play — the pool's own rate at
    # the game's venue against each arm's.
    pool_rate: dict = {}
    if fp is not None:
        for bb in fp.a.bb_pools.values():
            if bb.venue_id is None:
                continue
            for v in np.unique(bb.venue_id):
                m = bb.venue_id == v
                pool_rate.setdefault(int(v), [0, 0])
                pool_rate[int(v)][0] += int((bb.result_hits[m] == 4).sum())
                pool_rate[int(v)][1] += int(m.sum())
    print(f"\n  {'game':>8} {'venue':>6} {'pool HR/BIP':>12} {'OFF HR/BIP':>11} {'ON HR/BIP':>10}")
    for (gp, venue, hr_off, bip_off), (_, _, hr_on, bip_on) in zip(
        per_game["split OFF"], per_game["split ON"], strict=True
    ):
        try:
            v = int(venue)
        except (TypeError, ValueError):
            v = None
        pr = pool_rate.get(v) if v is not None else None
        pool_txt = f"{pr[0] / max(1, pr[1]):12.4f}" if pr else f"{'?':>12}"
        print(
            f"  {gp!s:>8} {venue!s:>6} {pool_txt} {hr_off / max(1, bip_off):11.4f} "
            f"{hr_on / max(1, bip_on):10.4f}"
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
