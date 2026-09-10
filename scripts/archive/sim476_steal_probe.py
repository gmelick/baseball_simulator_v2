"""
scripts/sim476_steal_probe.py — the SIM-476 step-0 steal instrument.

WHAT THIS IS
============
Measures steal attempts per opportunity against the pool's own rates, split
the two ways the step-0 hypothesis predicts:

  * per TARGET base (2B / 3B) — the certified deficit is 2B-specific;
  * per LEVERAGE band (low < 0.8, mid 0.8-1.5, high >= 1.5) — the suspect
    multiplier `0.5 + 0.5*min(LI/1.5, 2)` suppresses LOW-leverage pitches
    hardest, so arm A (current code) should show the deficit concentrated in
    the low band and arm B (neutral aggression) should be flat;
  * per hard cell (outs, balls, strikes) — the step-1 localization, captured
    in the same run so a residual needs no second measurement.

Attempts and outcomes are read from the DRAWN row's own flags at the sampler
seam (`steal_draw`), wrapped ONCE per process (a sentinel guards the cached
sampler against the wrapper-stacking trap the SIM-514 instrument hit); the
leverage context comes from a per-machine wrapper on
`_steal_opportunity_draw` that runs just before the draw.

USAGE
-----
    python scripts/sim476_steal_probe.py --iters 100 --json-out /app/scripts/sim476_armA.json
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

_LI_BANDS = ("low<0.8", "mid", "high>=1.5")


def _li_band(li: float) -> str:
    if li < 0.8:
        return _LI_BANDS[0]
    if li < 1.5:
        return _LI_BANDS[1]
    return _LI_BANDS[2]


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


class Recorder:
    """Opportunity/attempt/safe tallies, keyed three ways."""

    def __init__(self) -> None:
        self.by_target: dict[str, dict[str, int]] = {}
        self.by_band: dict[tuple[str, str], dict[str, int]] = {}
        self.by_cell: dict[tuple[str, int, int, int], dict[str, int]] = {}
        #: The per-draw context the machine wrapper stages for the sampler
        #: wrapper (they run synchronously inside one call).
        self.ctx_li: float = 0.0

    def _bump(self, d: dict, key: Any, attempted: bool, success: bool) -> None:
        t = d.setdefault(key, {"opp": 0, "att": 0, "safe": 0})
        t["opp"] += 1
        if attempted:
            t["att"] += 1
            if success:
                t["safe"] += 1

    def record(self, target: int, outs: int, balls: int, strikes: int, drawn: Any) -> None:
        if drawn is None:
            return
        attempted, success = bool(drawn[0]), bool(drawn[1])
        tk = str(int(target))
        self._bump(self.by_target, tk, attempted, success)
        self._bump(self.by_band, (tk, _li_band(self.ctx_li)), attempted, success)
        self._bump(self.by_cell, (tk, int(outs), int(balls), int(strikes)), attempted, success)


def _install(machine: Any, rec: Recorder) -> None:
    """Per-machine LI context wrapper + a wrap-ONCE sampler wrapper."""
    orig_stage = machine._steal_opportunity_draw

    def stage(state: Any, _o: Any = orig_stage) -> Any:
        rec.ctx_li = float(machine.compute_leverage(state))
        return _o(state)

    machine._steal_opportunity_draw = stage

    fp = machine.full_pool_sampler
    if getattr(fp.steal_draw, "_sim476_wrapped", False):
        return  # the cached sampler is already instrumented (the stacking trap)
    orig_draw = fp.steal_draw

    def draw(target_base: int, *a: Any, **kw: Any) -> Any:
        res = orig_draw(target_base, *a, **kw)
        rec.record(
            int(target_base),
            int(kw.get("outs", 0)),
            int(kw.get("balls", 0)),
            int(kw.get("strikes", 0)),
            res,
        )
        return res

    draw._sim476_wrapped = True  # type: ignore[attr-defined]
    fp.steal_draw = draw


def _pool_rates(sampler: Any) -> dict[str, Any]:
    """The pool's own attempt rates: marginal + per (outs, balls, strikes) cell.
    Steal-pool sit columns are (count_balls, count_strikes, outs, score_diff)."""
    out: dict[str, Any] = {}
    for target, spool in sampler.a.steal_pools.items():
        att = np.asarray(spool.attempted, dtype=np.float64)
        suc = np.asarray(spool.success, dtype=np.float64)
        rcy = spool.recency.astype(np.float64)
        balls = spool.sit[:, 0].astype(np.int64)
        strikes = spool.sit[:, 1].astype(np.int64)
        outs = spool.sit[:, 2].astype(np.int64)
        cells: dict[str, dict[str, float]] = {}
        for o in range(3):
            for b in range(4):
                for s in range(3):
                    m = (outs == o) & (balls == b) & (strikes == s)
                    n = int(m.sum())
                    if n:
                        cells[f"{o}-{b}-{s}"] = {
                            "n": n,
                            "att_rate": float(att[m].mean()),
                        }
        out[target] = {
            "rows": int(len(att)),
            "att_rate": float(att.mean()),
            "att_rate_rcy": float((att * rcy).sum() / rcy.sum()) if rcy.sum() else 0.0,
            "safe_share": float(suc[att > 0].mean()) if att.sum() else 0.0,
            "cells": cells,
        }
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

    started = time.perf_counter()
    print(
        f"sim476_steal_probe: {len(game_pks)} games x {args.iters} sims  "
        f"SIM_MANAGER={os.environ.get('SIM_MANAGER', '<unset>')}"
    )

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
                pool = _pool_rates(machine.full_pool_sampler)
            _install(machine, rec)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            opp2 = rec.by_target.get("2", {}).get("opp", 0)
            print(
                f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s "
                f"(cum 2B opps {opp2})"
            )
    finally:
        if duck is not None:
            duck.close()

    assert pool is not None
    tg = 2.0 * len(game_pks) * args.iters

    print("\n=== per target: sim attempts/opportunity vs the pool's own ===")
    print(
        f" {'tgt':>4} {'opp':>9} {'att':>6} {'att/opp':>9} {'poolUNW':>9} {'poolRCY':>9}"
        f" {'delta%':>8} {'safe':>7} {'poolSafe':>9}"
    )
    for tk in sorted(rec.by_target):
        t = rec.by_target[tk]
        p = pool.get(tk, {})
        rate = t["att"] / t["opp"] if t["opp"] else 0.0
        pr = p.get("att_rate_rcy", 0.0)
        print(
            f" {tk:>4} {t['opp']:>9d} {t['att']:>6d} {rate:9.4f} {p.get('att_rate', 0):9.4f}"
            f" {pr:9.4f} {((rate - pr) / pr * 100 if pr else 0):+8.1f}"
            f" {(t['safe'] / t['att'] if t['att'] else 0):7.4f} {p.get('safe_share', 0):9.4f}"
        )

    print("\n=== per leverage band (the step-0 hypothesis lens) ===")
    print(f" {'tgt':>4} {'band':>10} {'opp':>9} {'att':>6} {'att/opp':>9} {'vs poolRCY%':>12}")
    for tk, band in sorted(rec.by_band):
        t = rec.by_band[(tk, band)]
        rate = t["att"] / t["opp"] if t["opp"] else 0.0
        pr = pool.get(tk, {}).get("att_rate_rcy", 0.0)
        print(
            f" {tk:>4} {band:>10} {t['opp']:>9d} {t['att']:>6d} {rate:9.4f}"
            f" {((rate - pr) / pr * 100 if pr else 0):+12.1f}"
        )

    print("\n=== the 8 biggest 2B cells: sim vs pool att/opp (step-1 localization) ===")
    two = {k: v for k, v in rec.by_cell.items() if k[0] == "2"}
    for key in sorted(two, key=lambda k: -two[k]["opp"])[:8]:
        _tk, o, b, s = key
        t = two[key]
        pc = pool.get("2", {}).get("cells", {}).get(f"{o}-{b}-{s}", {})
        rate = t["att"] / t["opp"] if t["opp"] else 0.0
        pr = pc.get("att_rate", 0.0)
        print(
            f"  outs={o} count={b}-{s}: sim {rate:.4f} ({t['att']}/{t['opp']})"
            f"  pool {pr:.4f} (n={pc.get('n', 0)})"
            f"  delta {((rate - pr) / pr * 100 if pr else 0):+.1f}%"
        )

    att_tot = sum(t["att"] for t in rec.by_target.values())
    sb_tot = sum(t["safe"] for t in rec.by_target.values())
    print(
        f"\n totals: attempts/team-game {att_tot / tg:.4f}   SB/tg {sb_tot / tg:.4f}"
        f"   CS/tg {(att_tot - sb_tot) / tg:.4f}"
    )
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload = {
            "iters": args.iters,
            "game_pks": game_pks,
            "by_target": rec.by_target,
            "by_band": {f"{t}|{b}": v for (t, b), v in rec.by_band.items()},
            "by_cell": {f"{t}|{o}-{b}-{s}": v for (t, o, b, s), v in rec.by_cell.items()},
            "pool": {tk: {k: v for k, v in p.items() if k != "cells"} for tk, p in pool.items()},
            "pool_cells": {tk: p["cells"] for tk, p in pool.items()},
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
