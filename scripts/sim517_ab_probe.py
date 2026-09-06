"""
scripts/sim517_ab_probe.py — the SIM-517 red-band ATTRIBUTION instrument.

WHY
---
The 16-band certifying lane (2026-09-04) certified both new receiving bands
but moved three previously-green pitch-level bands red: BB_PA −3.3%,
HBP_PA −9.5%, STEAL_ATT_OPP_2B +7.5%. Two SIM-517 pieces could each own a
share — the receiving kernel (reweights the pitch draw) and the got-away
resolution (moves runners mid-PA, changing the state stream) — and the lane
fixture pins the production flags, so the attribution needs this direct
harness, which reads the env as-is.

WHAT IT COUNTS (the lane's own definitions)
-------------------------------------------
Per PA terminal (an ``_accumulate_pa`` wrapper, per-game machines):
``walk`` -> BB, ``intentional_walk`` -> IBB, ``hit_by_pitch`` -> HBP.
Per steal draw (wrap-once at the sampler seam): 2B opportunities/attempts.
Reported against the pool centres BB_PA 0.08500 / HBP_PA 0.01130 /
STEAL_ATT_OPP_2B 0.02140.

ARMS
----
    # receiving kernel OFF, got-away ON
    SIM_CATCHER_FRAMING_SIGMA=0 SIM_CATCHER_BLOCK_SIGMA=0 SIM_GOT_AWAY=1 ...
    # receiving kernel ON (production), got-away OFF
    SIM_GOT_AWAY=0 ...
The production reference is the certifying lane itself (12x500).
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

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.constants import resolve_event_to_canonical  # noqa: E402
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

_CENTRES = {"BB_PA": 0.08500, "HBP_PA": 0.01130, "STEAL_ATT_OPP_2B": 0.02140}


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


def _install(machine: Any, pc: dict[str, int]) -> None:
    """The lane's own counting, minimally: PA terminals + the steal seam."""
    orig_accumulate = machine._accumulate_pa

    def accumulate(state: Any, result: Any, _o: Any = orig_accumulate) -> Any:
        ev = getattr(result, "event", None)
        canonical = resolve_event_to_canonical(ev) if ev else None
        if canonical is not None:
            pc["PA"] += 1
            if canonical == "walk":
                pc["BB"] += 1
            elif canonical == "intentional_walk":
                pc["IBB"] += 1
            elif canonical == "hit_by_pitch":
                pc["HBP"] += 1
        return _o(state, result)

    machine._accumulate_pa = accumulate

    fp = machine.full_pool_sampler
    if getattr(fp.steal_draw, "_sim517ab_wrapped", False):
        return  # wrap-once (the SIM-514 stacking trap)
    orig_steal = fp.steal_draw

    def steal_draw(target_base: Any, *a: Any, **kw: Any) -> Any:
        res = orig_steal(target_base, *a, **kw)
        if res is not None and int(target_base) == 2:
            pc["STEAL_OPP_2"] += 1
            if bool(res[0]):
                pc["STEAL_ATT_2"] += 1
        return res

    steal_draw._sim517ab_wrapped = True  # type: ignore[attr-defined]
    fp.steal_draw = steal_draw


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
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)

    flags = {
        k: os.environ.get(k, "<unset>")
        for k in ("SIM_CATCHER_FRAMING_SIGMA", "SIM_CATCHER_BLOCK_SIGMA", "SIM_GOT_AWAY")
    }
    started = time.perf_counter()
    print(f"sim517_ab_probe: {len(game_pks)} games x {args.iters} sims  {flags}", flush=True)

    pc: dict[str, int] = dict.fromkeys(("PA", "BB", "IBB", "HBP", "STEAL_OPP_2", "STEAL_ATT_2"), 0)
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            _install(machine, pc)
            t0 = time.perf_counter()
            for seed in range(args.seed_base, args.seed_base + args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            print(f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s", flush=True)
    finally:
        if duck is not None:
            duck.close()

    print(f"\n=== the three red channels, this arm ({flags}) ===")
    rows = {
        "BB_PA": (pc["BB"], pc["PA"]),
        "HBP_PA": (pc["HBP"], pc["PA"]),
        "STEAL_ATT_OPP_2B": (pc["STEAL_ATT_2"], pc["STEAL_OPP_2"]),
    }
    for ch, (num, den) in rows.items():
        rate = num / den if den else float("nan")
        c = _CENTRES[ch]
        print(f" {ch}: {rate:.5f} = {num}/{den} vs centre {c:.5f} ({(rate - c) / c * 100:+.1f}%)")
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"flags": flags, "counts": pc, "iters": args.iters}, indent=2)
        )
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
