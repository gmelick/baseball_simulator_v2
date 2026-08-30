"""
scripts/sim476_park_probe.py — the SIM-476 part-3 park-kernel instrument.

WHAT THIS IS
============
Measures the sim's per-BIP event mix CONDITIONAL on the live venue's run-factor
class (low / mid / high terciles of the pool's own factor distribution) against
the pool's per-class conditional mix. The park kernel
(``SIM_PARK_KERNEL_SIGMA``, a Gaussian on |live − row| run-factor delta) is
correct when the sim's mix in a high-run park reproduces the pool's own
high-run-park mix — the SIM-516 grading ruling applied to the park dimension.

The fit rule this probe serves: pick the LARGEST sigma whose per-class mix
error is inside the pool-band floors. Smaller sigma always conditions harder,
but it also thins the draw — the probe reports the effective sample size
(ESS = (Σw)² / Σw²) of every batted-ball CDF so the thinning cost is measured,
not guessed.

Instrumentation: the drawn event is recorded at the sampler seam
(``battedball_draw``, wrapped ONCE — sentinel-guarded against the SIM-514
stacking trap) under the LIVE game's factor class, which the harness stages
per game. ESS is read from the staged CDF after each ``battedball_new_pa``.

USAGE
-----
    SIM_PARK_KERNEL_SIGMA=0.05 python scripts/sim476_park_probe.py \
        --iters 200 --json-out /app/scripts/sim476_park_s005.json
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
from simulation.production_factory import (  # noqa: E402
    _load_venue_run_factors,
    production_machine_factory,
)
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

_CLASSES = ("1B", "2B", "3B", "HR", "ROE", "out")
_TERCILES = ("low", "mid", "high")


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
    """Per-live-factor-class drawn-event tallies + CDF effective sample size."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {t: dict.fromkeys(_CLASSES, 0) for t in _TERCILES}
        self.ctx_tercile: str = "mid"
        self.ess_sum = 0.0
        self.ess_n = 0

    def mix(self, tercile: str) -> dict[str, float]:
        n = sum(self.counts[tercile].values())
        return {c: (self.counts[tercile][c] / n if n else 0.0) for c in _CLASSES}


def _install(machine: Any, rec: Recorder) -> None:
    """Wrap-ONCE sampler wrappers: record ESS at new_pa, the event at draw."""
    fp = machine.full_pool_sampler
    if getattr(fp.battedball_new_pa, "_sim476_park_wrapped", False):
        return  # the cached sampler is already instrumented (the stacking trap)
    orig_new_pa = fp.battedball_new_pa

    def new_pa(*a: Any, **kw: Any) -> Any:
        out = orig_new_pa(*a, **kw)
        cdf = getattr(fp, "_bb_cdf", None)
        if cdf is not None and len(cdf) and cdf[-1] > 0:
            w = np.diff(cdf, prepend=0.0)
            s2 = float((w * w).sum())
            if s2 > 0:
                rec.ess_sum += float(cdf[-1]) ** 2 / s2
                rec.ess_n += 1
        return out

    new_pa._sim476_park_wrapped = True  # type: ignore[attr-defined]
    fp.battedball_new_pa = new_pa

    orig_draw = fp.battedball_draw

    def draw(*a: Any, **kw: Any) -> Any:
        res = orig_draw(*a, **kw)
        rec.counts[rec.ctx_tercile][_cls(res[0])] += 1
        return res

    fp.battedball_draw = draw


def _pool_by_class(sampler: Any) -> dict[str, Any]:
    """The pool's own recency-weighted per-BIP mix per factor tercile.

    Tercile edges come from the pool's recency-weighted factor distribution, so
    the classes partition the POOL, and each live game is classed by the same
    edges."""
    factors: list[np.ndarray] = []
    rcys: list[np.ndarray] = []
    evcls: list[np.ndarray] = []
    for hand in sampler.a.bb_pools:
        pf = sampler._bb_park_factors(hand)
        if pf is None:
            raise RuntimeError(f"the {hand} pool has no per-row park factor (no venue map?)")
        pool = sampler.a.bb_pools[hand]
        factors.append(np.asarray(pf, dtype=np.float64))
        rcys.append(pool.recency.astype(np.float64))
        evcls.append(np.array([_cls(e) for e in pool.event], dtype=object))
    f = np.concatenate(factors)
    r = np.concatenate(rcys)
    e = np.concatenate(evcls)
    order = np.argsort(f)
    cum = np.cumsum(r[order])
    lo_edge = float(f[order][np.searchsorted(cum, cum[-1] / 3.0)])
    hi_edge = float(f[order][np.searchsorted(cum, 2.0 * cum[-1] / 3.0)])
    masks = {
        "low": f < lo_edge,
        "mid": (f >= lo_edge) & (f < hi_edge),
        "high": f >= hi_edge,
    }
    out: dict[str, Any] = {"lo_edge": lo_edge, "hi_edge": hi_edge}
    for t, m in masks.items():
        wsum = float(r[m].sum())
        out[t] = {
            "weight_share": wsum / float(r.sum()),
            "mix": {c: float(r[m & (e == c)].sum()) / wsum for c in _CLASSES},
        }
    return out


def _tercile(factor: float, pool: dict[str, Any]) -> str:
    if factor < pool["lo_edge"]:
        return "low"
    if factor < pool["hi_edge"]:
        return "mid"
    return "high"


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
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)
    sigma = float(os.environ.get("SIM_PARK_KERNEL_SIGMA", "0"))

    started = time.perf_counter()
    print(f"sim476_park_probe: {len(game_pks)} games x {args.iters} sims  park_sigma={sigma}")

    rec = Recorder()
    pool: dict[str, Any] | None = None
    duck = open_sim_duckdb()
    game_classes: dict[int, tuple[float, str]] = {}
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            fp = machine.full_pool_sampler
            if fp.venue_run_factors is None:
                # The factory loads the venue map only when the kernel is ON;
                # the POOL reference needs it in every arm.
                fp.venue_run_factors = _load_venue_run_factors()
            if pool is None:
                pool = _pool_by_class(fp)
            _install(machine, rec)
            live_f = float(getattr(state, "park_run_factor", 1.0) or 1.0)
            rec.ctx_tercile = _tercile(live_f, pool)
            game_classes[gp] = (live_f, rec.ctx_tercile)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            print(
                f"  game {gp}: factor {live_f:.4f} [{rec.ctx_tercile}] "
                f"{args.iters} sims in {time.perf_counter() - t0:.1f}s"
            )
    finally:
        if duck is not None:
            duck.close()

    assert pool is not None
    print(
        f"\n pool tercile edges: low<{pool['lo_edge']:.4f}<=mid<{pool['hi_edge']:.4f}<=high"
        f"   mean CDF ESS {rec.ess_sum / max(rec.ess_n, 1):,.0f} over {rec.ess_n} PAs"
    )
    print("\n=== per-BIP mix by live factor class: sim vs the pool's own class mix ===")
    print(f" {'class':>5} {'ch':>4} {'sim':>9} {'pool':>9} {'delta':>9} {'rel%':>7}")
    for t in _TERCILES:
        sim_mix = rec.mix(t)
        n = sum(rec.counts[t].values())
        for c in _CLASSES:
            pm = pool[t]["mix"][c]
            d = sim_mix[c] - pm
            print(
                f" {t:>5} {c:>4} {sim_mix[c]:9.5f} {pm:9.5f} {d:+9.5f}"
                f" {(d / pm * 100 if pm else 0):+7.1f}"
            )
        print(f"       (sim BIP n={n}, pool weight share {pool[t]['weight_share']:.3f})")
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload = {
            "iters": args.iters,
            "game_pks": game_pks,
            "park_sigma": sigma,
            "game_classes": {str(k): v for k, v in game_classes.items()},
            "sim_counts": rec.counts,
            "pool": pool,
            "ess_mean": rec.ess_sum / max(rec.ess_n, 1),
            "ess_n": rec.ess_n,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
