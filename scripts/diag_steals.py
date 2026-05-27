"""SIM-426 calibration: measure steal volume on the full-pool path.

Runs full-pool games and reports SB (and CS if tracked) per game / per team so
``_STEAL_ATTEMPT_K`` can be tuned to ~MLB volume (~0.5 SB + ~0.2 CS per team-game).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.lineup_resolver import resolve_game_state  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

_FACTORY = "simulation.production_factory:production_machine_factory"


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _resolve(game_pk: int):
    conn = await asyncpg.connect(_dsn())
    try:
        return await resolve_game_state(conn, game_pk, seed=0)
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=20)
    args = ap.parse_args()

    sb_per_game: list[int] = []
    cs_per_game: list[int] = []
    for gp in args.game_pks:
        state = asyncio.run(_resolve(gp))
        kw = {
            "away_lineup": list(getattr(state, "away_lineup", []) or []),
            "home_lineup": list(getattr(state, "home_lineup", []) or []),
            "season": int(getattr(state, "season", 2024) or 2024),
            "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
            "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
            "bat_hands": dict(getattr(state, "bat_hands", {}) or {}),
            "throw_hands": dict(getattr(state, "throw_hands", {}) or {}),
            "home_pitcher_id": getattr(state, "home_pitcher_id", None),
            "away_pitcher_id": getattr(state, "away_pitcher_id", None),
            "k": 25,
            "max_innings": 12,
        }
        spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        machine = production_machine_factory(0, spec)
        for seed in range(args.iters):
            machine.boxscore = BoxScore()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            sb = sum(int(getattr(ln, "sb", 0)) for ln in res.boxscore.lines.values())
            cs = sum(int(getattr(ln, "cs", 0)) for ln in res.boxscore.lines.values())
            sb_per_game.append(sb)
            cs_per_game.append(cs)

    n = len(sb_per_game)
    sb = statistics.mean(sb_per_game)
    cs = statistics.mean(cs_per_game)
    print(f"=== steal volume (n={n} sims, both teams) ===")
    print(f"  SB/game={sb:.2f}  CS/game={cs:.2f}  attempts/game={sb + cs:.2f}")
    print(f"  per-team: SB={sb/2:.2f}  CS={cs/2:.2f}  attempts={(sb+cs)/2:.2f}")
    print("  MLB per-team target: SB~0.51  CS~0.19  attempts~0.70")
    if sb + cs > 0:
        print(f"  success rate={sb/(sb+cs):.2f}  (MLB ~0.75-0.80)")


if __name__ == "__main__":
    main()
