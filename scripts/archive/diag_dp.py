"""SIM-429 root-cause: phantom double plays.

The full-pool batted-ball draw samples an event by batter+situation, then infers
outs (DP -> 2). But a DP is impossible with no runner to force/double off; if such
events are drawn with empty bases the sim records a phantom 2nd out, ending innings
early and suppressing runs. This measures how often a DP event is drawn with no
runner on first / no runner at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.lineup_resolver import resolve_game_state  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import _DOUBLE_PLAY_EVENTS, BoxScore, simulate_game  # noqa: E402

_FACTORY = "simulation.production_factory:production_machine_factory"
_DSN = os.environ.get(
    "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
)


async def _resolve(gp):
    c = await asyncpg.connect(_DSN)
    try:
        return await resolve_game_state(c, gp, seed=0)
    finally:
        await c.close()


def main():
    counts = {"out_total": 0, "dp_total": 0, "dp_no_first": 0, "dp_no_runner": 0}
    for gp in (744795, 661032, 564734, 825108):
        state = asyncio.run(_resolve(gp))
        kw = {
            "away_lineup": list(state.away_lineup),
            "home_lineup": list(state.home_lineup),
            "season": int(state.season),
            "pitcher_id": int(state.pitcher_id or 0),
            "bat_hand": state.bat_hand,
            "bat_hands": dict(state.bat_hands),
            "throw_hands": dict(state.throw_hands),
            "home_pitcher_id": state.home_pitcher_id,
            "away_pitcher_id": state.away_pitcher_id,
            "home_catcher_id": state.home_catcher_id,
            "away_catcher_id": state.away_catcher_id,
            "k": 25,
            "max_innings": 12,
        }
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        orig = machine._full_pool_fielding

        def wrapped(st, _orig=orig):
            sig = _orig(st)
            if sig is not None and int(sig.result_hits) == 0:
                counts["out_total"] += 1
                if sig.event in _DOUBLE_PLAY_EVENTS:
                    counts["dp_total"] += 1
                    if st.bases.first is None:
                        counts["dp_no_first"] += 1
                    if st.runners_state == 0:
                        counts["dp_no_runner"] += 1
            return sig

        machine._full_pool_fielding = wrapped
        for seed in range(20):
            machine.boxscore = BoxScore()
            simulate_game(state_machine=machine, seed=seed, **kw)

    o = counts["out_total"] or 1
    d = counts["dp_total"] or 1
    print("=== phantom double-play audit (80 sims) ===")
    print(f"  in-play outs drawn      : {counts['out_total']}")
    print(
        f"  DP events drawn         : {counts['dp_total']}  ({100 * counts['dp_total'] / o:.1f}% of outs)"
    )
    print(
        f"  DP with NO runner on 1B : {counts['dp_no_first']}  ({100 * counts['dp_no_first'] / d:.1f}% of DPs) <- phantom 2nd out"
    )
    print(
        f"  DP with NO runner at all: {counts['dp_no_runner']}  ({100 * counts['dp_no_runner'] / d:.1f}% of DPs)"
    )


if __name__ == "__main__":
    main()
