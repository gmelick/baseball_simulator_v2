"""SIM-428 validation: catcher framing.

(1) Reports the framing-delta distribution across catchers, and (2) runs one game
with the fielding catcher forced to the BEST vs the WORST framer and reports the
called-strike / K / BB difference — a differential test (framing is ~aggregate-
neutral league-wide, so its value is per-catcher differentiation).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402
import numpy as np  # noqa: E402

from pipeline.batch.engine_artifacts import EngineArtifacts  # noqa: E402
from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.lineup_resolver import resolve_game_state  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

_FACTORY = "simulation.production_factory:production_machine_factory"
_DSN = os.environ.get(
    "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
)


def _framing_extremes():
    a = EngineArtifacts.load("/data/play_pool/engine_artifacts")
    c = a.actor_emb["catcher"]
    fi = {f: i for i, f in enumerate(c["features"])}
    saa, tot = fi["strikes_above_average"], fi["pitches_received_total"]
    deltas = []
    for key, idx in c["key_index"].items():
        v = c["vecs"][idx]
        if v[tot] > 500:  # enough volume to be meaningful
            deltas.append((float(v[saa]) / float(v[tot]), key))
    deltas.sort()
    arr = np.array([d for d, _ in deltas])
    print(f"=== framing delta (per-taken-pitch CS shift), n={len(deltas)} catchers ===")
    print(
        f"  min={arr.min():+.4f}  p10={np.percentile(arr, 10):+.4f}  med={np.median(arr):+.4f}"
        f"  p90={np.percentile(arr, 90):+.4f}  max={arr.max():+.4f}"
    )
    return deltas[-1][1], deltas[0][1]  # best, worst keys ("id:season")


async def _run():
    best, worst = _framing_extremes()
    best_id = int(best.split(":")[0])
    worst_id = int(worst.split(":")[0])
    conn = await asyncpg.connect(_DSN)
    st = await resolve_game_state(conn, 744795, seed=0)
    await conn.close()
    base = {
        "away_lineup": list(st.away_lineup),
        "home_lineup": list(st.home_lineup),
        "season": int(st.season),
        "pitcher_id": int(st.pitcher_id or 0),
        "bat_hand": st.bat_hand,
        "bat_hands": dict(st.bat_hands),
        "throw_hands": dict(st.throw_hands),
        "home_pitcher_id": st.home_pitcher_id,
        "away_pitcher_id": st.away_pitcher_id,
        "k": 25,
        "max_innings": 12,
    }
    spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(base))
    machine = production_machine_factory(0, spec)
    for label, cid in (("BEST framer", best_id), ("WORST framer", worst_id)):
        ks, bbs, _css = [], [], []
        for seed in range(40):
            machine.boxscore = BoxScore()
            kw = dict(base, home_catcher_id=cid, away_catcher_id=cid)
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            ks.append(sum(ln.k for ln in res.boxscore.lines.values()))
            bbs.append(sum(ln.bb for ln in res.boxscore.lines.values()))
        print(
            f"  {label:14s} (id={cid}): K/game={statistics.mean(ks):.2f}  BB/game={statistics.mean(bbs):.2f}"
        )


if __name__ == "__main__":
    asyncio.run(_run())
