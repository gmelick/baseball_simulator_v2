"""
scripts/sim429_ibb_probe.py — count the sim's intentional walks (SIM-429).

WHAT THIS IS
============
The SIM-429 count diagnosis tracked walks at the pitch draw, which EXCLUDES
intentional walks (``_issue_intentional_walk`` short-circuits the count machine
and throws no pitch). The certified lane counts box walks, which INCLUDE them.
The gap between the two reads (~0.30/team-game) is therefore attributed to the
manager's IBB volume — this probe measures it directly.

It wraps ``machine._issue_intentional_walk`` per game and also sums box BB per
iteration, so the reconciliation is closed in one run:

    box BB  ==  drawn walks  +  IBB          (any residual is a new defect)

MLB-2025 reference: 595 intentional walks / 4,860 team-games = 0.1224/tg
(raw.play_events, the SIM-508 band note).

USAGE
-----
    python scripts/sim429_ibb_probe.py --iters 50
"""

from __future__ import annotations

import argparse
import asyncio
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

_MLB_IBB_PER_TG = 595.0 / 4860.0


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


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
    ap.add_argument("--iters", type=int, default=50)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)

    started = time.perf_counter()
    print(
        f"sim429_ibb_probe: {len(game_pks)} games x {args.iters} sims  "
        f"SIM_MANAGER={os.environ.get('SIM_MANAGER', '<unset>')}"
    )

    ibb = 0
    box_bb = 0
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)

            orig = machine._issue_intentional_walk

            def wrapped(st: Any, _orig=orig) -> Any:
                nonlocal ibb
                ibb += 1
                return _orig(st)

            machine._issue_intentional_walk = wrapped

            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
                box_bb += sum(int(ln.bb) for ln in machine.boxscore.lines.values())
            print(
                f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s "
                f"(cum IBB {ibb}, cum box BB {box_bb})"
            )
    finally:
        if duck is not None:
            duck.close()

    tg = 2.0 * len(game_pks) * args.iters
    print(f"\n IBB/team-game     = {ibb / tg:.4f}   (MLB 2025 = {_MLB_IBB_PER_TG:.4f})")
    print(f" box BB/team-game  = {box_bb / tg:.4f}   (lane read 3.5358; band centre 3.1656)")
    print(f" pitched BB/tg     = {(box_bb - ibb) / tg:.4f}   (the diagnosis tracker read 3.2331)")
    print(f"\nelapsed: {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
