"""
scripts/sim_stats.py — Monte-Carlo box-score averages after the SIM-421 fix.

Runs N iterations per game through the PRODUCTION sim (real DuckDB/FAISS sampler +
the wired FingerprintDeriver) and reports the average per-game box line (both
teams combined and per-team) so we can sanity-check realism against MLB norms.

Usage (in the app container):
    python scripts/sim_stats.py --iters 25 744795 661032 564734 825108
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


def _sim_kwargs(state) -> dict:
    return {
        "away_lineup": list(getattr(state, "away_lineup", []) or []),
        "home_lineup": list(getattr(state, "home_lineup", []) or []),
        "season": int(getattr(state, "season", 2024) or 2024),
        "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
        "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
        "bat_hands": dict(getattr(state, "bat_hands", {}) or {}),
        "throw_hands": dict(getattr(state, "throw_hands", {}) or {}),
        "home_pitcher_id": getattr(state, "home_pitcher_id", None),
        "away_pitcher_id": getattr(state, "away_pitcher_id", None),
        "home_catcher_id": getattr(state, "home_catcher_id", None),
        "away_catcher_id": getattr(state, "away_catcher_id", None),
        "k": 25,
        "max_innings": 12,
    }


def _game_totals(result) -> dict:
    """Both-teams totals for one simulated game from its final box score."""
    box = result.boxscore
    h = hr = b2 = b3 = ab = 0
    k = bb = 0
    for ln in box.lines.values():
        h += ln.h
        hr += ln.hr
        b2 += ln.b2
        b3 += ln.b3
        ab += ln.ab
        k += ln.k  # pitching K (every PA K is charged to a pitcher)
        bb += ln.bb  # pitching BB
    runs = int(getattr(result, "home_score", 0)) + int(getattr(result, "away_score", 0))
    return {"R": runs, "H": h, "HR": hr, "2B": b2, "3B": b3, "BB": bb, "K": k, "AB": ab}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=25)
    args = ap.parse_args()

    keys = ["R", "H", "HR", "2B", "3B", "BB", "K"]
    all_games: dict[str, list[float]] = {k: [] for k in keys}

    for gp in args.game_pks:
        state = asyncio.run(_resolve(gp))
        kw = _sim_kwargs(state)
        spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        machine = production_machine_factory(0, spec)  # warm, reused across seeds
        per: dict[str, list[float]] = {k: [] for k in keys}
        for seed in range(args.iters):
            machine.boxscore = BoxScore()  # reset per iteration
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            t = _game_totals(res)
            for k in keys:
                per[k].append(t[k])
                all_games[k].append(t[k])
        means = {k: statistics.mean(per[k]) for k in keys}
        print(
            f"game {gp} (n={args.iters}): "
            + "  ".join(f"{k}={means[k]:.1f}" for k in keys)
        )

    n = len(all_games["R"])
    print(f"\n=== OVERALL per-game, both teams (n={n} sims) ===")
    print("  " + "  ".join(f"{k}={statistics.mean(all_games[k]):.2f}" for k in keys))
    print("=== per-team (both-teams / 2) vs MLB-2023 ===")
    mlb = {"R": 4.62, "H": 8.60, "HR": 1.21, "2B": 1.60, "3B": 0.14, "BB": 3.30, "K": 8.60}
    for k in keys:
        sim = statistics.mean(all_games[k]) / 2.0
        print(f"  {k:3s} sim={sim:5.2f}   mlb={mlb[k]:5.2f}")


if __name__ == "__main__":
    main()
