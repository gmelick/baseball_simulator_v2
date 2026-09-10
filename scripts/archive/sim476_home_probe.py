"""
scripts/sim476_home_probe.py — the SIM-476 part-2 home-kernel instrument.

WHAT THIS IS
============
Measures the sim's HOME-batting vs AWAY-batting per-BIP event mix against the
pool's own ``bat_home`` conditional mix (migration 0019). The fit objective the
plan pre-registers: the sim's home-minus-away differential per event class
should reproduce the pool's own bat_home=1 minus bat_home=0 differential.

The analytic frame the run verifies: with mismatched-side rows weighted by
``w = home_off_weight`` and a home-row pool share ``p``, the drawn home-half
mix is ``(p·r_H + (1-p)·w·r_A) / (p + (1-p)·w)`` and symmetrically for the
away half. At p≈0.5 the sim differential is ``(1-w)/(1+w)`` of the pool's —
so the probe prints both the measured and the predicted recovery fraction.

Batted-ball draws are read at the sampler seam: ``battedball_new_pa`` is
wrapped ONCE per process (sentinel-guarded — the SIM-514 stacking trap) to
stage the live ``bat_home`` side, and ``battedball_draw`` records the drawn
event under that side. Home/away final scores are tallied from each
``GameSimResult`` for a direction read on home_win_pct (noise-level at this n;
the 13,365-game lane is the real verify).

USAGE
-----
    SIM_HOME_OFF_WEIGHT=0.7 python scripts/sim476_home_probe.py \
        --iters 100 --json-out /app/scripts/sim476_home_w070.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402
import numpy as np  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.lineup_resolver import resolve_game_state  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_kwargs import (  # noqa: E402
    open_sim_duckdb,
    resolve_park_run_factor,
    sim_kwargs_from_state,
)
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

_FACTORY = "simulation.production_factory:production_machine_factory"

_DEFAULT_GAME_PKS = (
    745199,
    746494,
    745036,
    745444,
    745280,
    746560,
    745118,
    746088,
    744795,
    745521,
    746331,
    745441,
)

#: The event classes the differential is read over. Everything not listed is
#: a ball-in-play out (the complement channel).
_CLASSES = ("1B", "2B", "3B", "HR", "ROE", "out")


def _cls(event: str) -> str:
    return {
        "single": "1B",
        "double": "2B",
        "triple": "3B",
        "home_run": "HR",
        "field_error": "ROE",
    }.get(str(event), "out")


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


class Recorder:
    """Per-side (home/away batting) drawn-event tallies."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {
            "home": dict.fromkeys(_CLASSES, 0),
            "away": dict.fromkeys(_CLASSES, 0),
        }
        self.ctx_side: str = "away"
        self.home_wins = 0
        self.away_wins = 0
        self.ties = 0
        self.home_runs = 0
        self.away_runs = 0
        self.games = 0

    def mix(self, side: str) -> dict[str, float]:
        n = sum(self.counts[side].values())
        return {c: (self.counts[side][c] / n if n else 0.0) for c in _CLASSES}


def _install(machine: Any, rec: Recorder) -> None:
    """Wrap-ONCE sampler wrappers: stage the side, record the drawn event."""
    fp = machine.full_pool_sampler
    if getattr(fp.battedball_new_pa, "_sim476_wrapped", False):
        return  # the cached sampler is already instrumented (the stacking trap)
    orig_new_pa = fp.battedball_new_pa

    def new_pa(*a: Any, **kw: Any) -> Any:
        rec.ctx_side = "home" if kw.get("bat_home") else "away"
        return orig_new_pa(*a, **kw)

    new_pa._sim476_wrapped = True  # type: ignore[attr-defined]
    fp.battedball_new_pa = new_pa

    orig_draw = fp.battedball_draw

    def draw(*a: Any, **kw: Any) -> Any:
        res = orig_draw(*a, **kw)
        rec.counts[rec.ctx_side][_cls(res[0])] += 1
        return res

    fp.battedball_draw = draw


def _pool_mix(sampler: Any) -> dict[str, Any]:
    """The pool's own recency-weighted per-BIP mix, conditional on bat_home."""
    tot = {side: dict.fromkeys(_CLASSES, 0.0) for side in ("home", "away")}
    w_home = 0.0
    w_away = 0.0
    for hand, pool in sampler.a.bb_pools.items():
        bh = getattr(pool, "bat_home", None)
        if bh is None:
            raise RuntimeError(f"the {hand} batted-ball pool has no bat_home (pre-0019 bundle)")
        rcy = pool.recency.astype(np.float64)
        classes = np.array([_cls(e) for e in pool.event], dtype=object)
        hmask = np.asarray(bh) > 0
        for side, mask in (("home", hmask), ("away", ~hmask)):
            wsum = float(rcy[mask].sum())
            if side == "home":
                w_home += wsum
            else:
                w_away += wsum
            for c in _CLASSES:
                tot[side][c] += float(rcy[mask & (classes == c)].sum())
    out: dict[str, Any] = {"p_home": w_home / (w_home + w_away)}
    for side in ("home", "away"):
        wsum = w_home if side == "home" else w_away
        out[side] = {c: tot[side][c] / wsum for c in _CLASSES}
    return out


async def _resolve(game_pk: int, duck: Any) -> Any:
    conn = await asyncpg.connect(_dsn())
    try:
        state = await resolve_game_state(conn, game_pk, seed=0)
        state.park_run_factor = await resolve_park_run_factor(
            conn, duck, int(game_pk), int(getattr(state, "season", 2024) or 2024)
        )
        return state
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_pks", type=int, nargs="*", default=None)
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)
    w = float(os.environ.get("SIM_HOME_OFF_WEIGHT", "1.0"))

    started = time.perf_counter()
    print(f"sim476_home_probe: {len(game_pks)} games x {args.iters} sims  home_off_weight={w}")

    rec = Recorder()
    pool: dict[str, Any] | None = None
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if pool is None:
                pool = _pool_mix(machine.full_pool_sampler)
            _install(machine, rec)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                res = simulate_game(state_machine=machine, seed=seed, **kw)
                if res.home_score > res.away_score:
                    rec.home_wins += 1
                elif res.away_score > res.home_score:
                    rec.away_wins += 1
                else:
                    rec.ties += 1
                rec.home_runs += int(res.home_score)
                rec.away_runs += int(res.away_score)
                rec.games += 1
            bip = sum(sum(rec.counts[s].values()) for s in ("home", "away"))
            print(
                f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s (cum BIP {bip})"
            )
    finally:
        if duck is not None:
            duck.close()

    assert pool is not None
    sim_h = rec.mix("home")
    sim_a = rec.mix("away")
    pred = (1.0 - w) / (1.0 + w)

    print(f"\n pool home-row share p = {pool['p_home']:.4f}")
    print(f" predicted recovery fraction (1-w)/(1+w) at w={w}: {pred:.4f}")
    print("\n=== per-BIP mix: sim home/away vs the pool's own bat_home split ===")
    print(
        f" {'class':>6} {'simHome':>9} {'simAway':>9} {'simDiff':>9}"
        f" {'poolHome':>9} {'poolAway':>9} {'poolDiff':>9} {'recovered':>10}"
    )
    for c in _CLASSES:
        sdiff = sim_h[c] - sim_a[c]
        pdiff = pool["home"][c] - pool["away"][c]
        frac = sdiff / pdiff if abs(pdiff) > 1e-9 else float("nan")
        print(
            f" {c:>6} {sim_h[c]:9.5f} {sim_a[c]:9.5f} {sdiff:+9.5f}"
            f" {pool['home'][c]:9.5f} {pool['away'][c]:9.5f} {pdiff:+9.5f} {frac:10.3f}"
        )

    n_h = sum(rec.counts["home"].values())
    n_a = sum(rec.counts["away"].values())
    dec = rec.home_wins + rec.away_wins
    g = rec.games or 1
    print(f"\n BIP: home {n_h}  away {n_a}")
    print(
        f" home_win_pct (direction read only, n={dec}): "
        f"{(rec.home_wins / dec if dec else 0.0):.4f}  (ties {rec.ties})"
    )
    print(
        f" runs/game: home {rec.home_runs / g:.4f}  away {rec.away_runs / g:.4f}"
        f"  home-away {(rec.home_runs - rec.away_runs) / g:+.4f}"
    )
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload = {
            "iters": args.iters,
            "game_pks": game_pks,
            "home_off_weight": w,
            "sim_counts": rec.counts,
            "sim_mix": {"home": sim_h, "away": sim_a},
            "pool": pool,
            "home_wins": rec.home_wins,
            "away_wins": rec.away_wins,
            "ties": rec.ties,
            "home_runs": rec.home_runs,
            "away_runs": rec.away_runs,
            "games": rec.games,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
