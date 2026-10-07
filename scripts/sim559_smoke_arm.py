#!/usr/bin/env python
"""
scripts/sim559_smoke_arm.py
===========================
The ten-game smoke of the starting-position defect (SIM-559), one arm per run.

``--arm stored`` runs the defense maps production builds from ``raw.game_lineups``
today. ``--arm true`` runs the same games with the eight fielding slots and the
catcher rebuilt from the box feed's ``allPositions[0]`` (the pitcher and the
lineups unchanged). Both arms use seeds 0..iters-1, so a game without a defect
comes back byte-identical and a difference is the maps' alone.

Per fielding draw the harness tallies whether the live defender at the drawn
row's position is missing or a different player than the true starter; per
running-game draw (the pickoff and steal draws) whether the catcher key is
missing or wrong. The channel report is ``scripts/sim_stats.py``'s.

    docker compose run -d --no-deps -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim559_smoke_arm.py --arm stored --iters 40 --out /app/scripts/sim559_smoke_stored.json

The record of the 2026-10-06 run is ``scripts/sim559_smoke.txt``. After the
backfill the ``stored`` arm must tally zero missing and zero wrong reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import sim_stats as ss  # noqa: E402

from pipeline.etl.boxscore_ingest import starting_position  # noqa: E402
from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.game_state import Team  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_kwargs import sim_kwargs_from_state  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

FIELD = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
GAME_SET = _ROOT / "scripts" / "sim523_game_set.json"


def _get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-sim/SIM-559"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed host
        return json.load(r)


def true_map(teams: dict[str, Any], side: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in (teams.get(side) or {}).get("players", {}).values():
        bo = p.get("battingOrder")
        if not bo or int(bo) % 100:
            continue
        pos = starting_position(p)
        if pos in FIELD:
            out[pos] = int(p["person"]["id"])
    return out


def first_games(n: int) -> list[int]:
    data = json.loads(GAME_SET.read_text(encoding="utf-8"))
    order = [int(x["game_pk"]) if isinstance(x, dict) else int(x) for x in data["order"]]
    return order[:n]


def box_totals(res: Any, ids: set[int]) -> Counter:
    out: Counter = Counter()
    for pid, ln in res.boxscore.lines.items():
        if int(pid) not in ids:
            continue
        for k in ("h", "b2", "b3", "hr", "bb", "k", "sb", "cs", "r", "rbi"):
            out[k] += int(getattr(ln, k, 0) or 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--arm", choices=("stored", "true"), required=True)
    ap.add_argument("--iters", type=int, default=40)
    ap.add_argument(
        "--games", type=int, nargs="*", default=None, help="Default: the balanced set's first ten."
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    games = args.games or first_games(10)
    t0 = time.time()
    duck = ss.open_sim_duckdb()
    tally: Counter = Counter()
    per_game: list[list[dict]] = []
    record: dict[str, Any] = {"arm": args.arm, "iters": args.iters, "games": {}}
    for gp in games:
        state = asyncio.run(ss._resolve(gp, duck))
        teams = _get(f"https://statsapi.mlb.com/api/v1/game/{gp}/boxscore")["teams"]
        truth = {"home": true_map(teams, "home"), "away": true_map(teams, "away")}
        stored = {
            "home": dict(getattr(state, "home_defense", {}) or {}),
            "away": dict(getattr(state, "away_defense", {}) or {}),
        }
        kw = sim_kwargs_from_state(state)
        if args.arm == "true":
            for side in ("home", "away"):
                m = {k: v for k, v in stored[side].items() if k == "P"}
                m.update(truth[side])
                kw[f"{side}_defense"] = m
                kw[f"{side}_catcher_id"] = truth[side].get("C")
        diff = {
            side: {
                "missing": [p for p in FIELD if p in truth[side] and p not in stored[side]],
                "wrong": [
                    p
                    for p in FIELD
                    if p in truth[side] and p in stored[side] and stored[side][p] != truth[side][p]
                ],
            }
            for side in ("home", "away")
        }
        record["games"][str(gp)] = {"diff": diff, "stored_n": {s: len(stored[s]) for s in stored}}
        spec = GameSpec(machine_factory=ss._FACTORY, sim_kwargs=dict(kw))
        machine = production_machine_factory(0, spec)

        orig_lf = machine._live_fielder_at_drawn_position

        def hooked_lf(state, fp, _o=orig_lf, _truth=truth):
            fid, pos = _o(state, fp)
            side = "home" if state.defense == Team.HOME else "away"
            tally["bip_draws"] += 1
            if pos is None:
                tally["bip_no_position"] += 1
                return fid, pos
            t = _truth[side].get(pos)
            if fid is None:
                tally["bip_live_missing"] += 1
            elif t is not None and int(fid) != int(t):
                tally["bip_live_wrong"] += 1
            return fid, pos

        machine._live_fielder_at_drawn_position = hooked_lf  # type: ignore[method-assign]

        orig_keys = machine._running_game_keys

        def hooked_keys(state, runner_id, _o=orig_keys, _truth=truth):
            rk, pk_, ck = _o(state, runner_id)
            side = "home" if state.defense == Team.HOME else "away"
            tally["run_draws"] += 1
            tc = _truth[side].get("C")
            if ck is None:
                tally["run_catcher_missing"] += 1
            elif tc is not None and int(ck.split(":")[0]) != int(tc):
                tally["run_catcher_wrong"] += 1
            return rk, pk_, ck

        machine._running_game_keys = hooked_keys  # type: ignore[method-assign]

        home_ids = {int(x) for x in (state.home_lineup or [])}
        away_ids = {int(x) for x in (state.away_lineup or [])}
        if state.home_pitcher_id:
            home_ids.add(int(state.home_pitcher_id))
        if state.away_pitcher_id:
            away_ids.add(int(state.away_pitcher_id))
        gs: list[dict] = []
        ch: Counter = Counter()
        for seed in range(args.iters):
            machine.boxscore = BoxScore()
            res = simulate_game(state_machine=machine, seed=seed, **kw)
            gs.append(ss._game_summary(res, home_ids=home_ids, away_ids=away_ids))
            ch.update(box_totals(res, home_ids | away_ids))
        per_game.append(gs)
        record["games"][str(gp)]["channels_per_game"] = {k: v / args.iters for k, v in ch.items()}
        print(
            f"  game {gp} arm={args.arm}: diff={diff}  R={sum(s['R'] for s in gs) / len(gs):.2f}"
            f"  ({time.time() - t0:.0f}s)",
            flush=True,
        )
    if duck is not None:
        duck.close()
    agg = ss._aggregate(per_game)
    ss._print_report(agg, n_games=len(games))
    record["tally"] = dict(tally)
    record["aggregate"] = agg
    record["per_game"] = per_game
    record["elapsed_s"] = time.time() - t0
    print("TALLY", json.dumps(dict(tally)))
    Path(args.out).write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    print(f"SIM559-ARM-{args.arm}-OK elapsed {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
