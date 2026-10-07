"""
scripts/sim558_game_probe.py — what the starter fix changes in one game (SIM-558)

WHAT THIS IS
============
Game 823372 of the balanced certifying set resolved with no away starter, so
the home starter pitched to both sides.  This probe measures what the fix
changes in that game's share of the acceptance lane.

``run`` simulates ONE game N times through the production machine and counts,
for each batting side, the plate appearances, strikeouts, walks, hit by
pitches, hits by type and balls in play, the runs, and which pitcher faced
each side.  Run it once on the code before the fix and once on the fixed code:

    # the code before the fix (the image's default mounts)
    MSYS_NO_PATHCONV=1 docker compose run --rm -T --no-deps \\
        -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim558_game_probe.py run 823372 390 /app/scripts/sim558_game_823372_old.json
    # the fixed code (mount the checkout that holds the fix over simulation/)

``compare`` reads the two records and prints the paired read: who faced whom,
the rates per side, and the size of the move in the 45 x 130 lane's totals
(this game is 130 of the lane's 5,850 game-sims).  The lane totals are the
2026-10-03 lane's (``scripts/sim554_lane.txt``).

    python scripts/sim558_game_probe.py compare OLD.json FIXED.json
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_FACTORY = "simulation.production_factory:production_machine_factory"

#: The 2026-10-03 lane (scripts/sim554_lane.txt): its totals and the band centres.
_LANE_PA = 444_292
_LANE_ITERS = 130
_LANE_GAMES = 45
_LANE_COUNTS = {"K": 0.22408 * _LANE_PA, "BB": 38_067.0, "HBP": 5_401.0}
_LANE_CENTRES = {"K": 0.2262, "BB": 0.0829, "HBP": 0.0112}
_SIDES = ("away_batting", "home_batting")


def run(game_pk: int, n: int, out_path: str) -> None:
    """Simulate ``game_pk`` ``n`` times (seeds 0..n-1) and write the tallies."""
    import asyncpg

    from simulation.batch_runner import GameSpec
    from simulation.constants import resolve_event_to_canonical
    from simulation.lineup_resolver import resolve_game_state
    from simulation.production_factory import production_machine_factory
    from simulation.sim_kwargs import (
        open_sim_duckdb,
        resolve_manager_profiles_onto_state,
        resolve_park_run_factor,
        resolve_venue_id,
        sim_kwargs_from_state,
    )
    from simulation.sim_loop import BoxScore, simulate_game

    duck = open_sim_duckdb()

    async def _resolve() -> Any:
        dsn = os.environ["BASEBALL_DB_DSN"].replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn, timeout=60)
        try:
            state = await resolve_game_state(conn, game_pk, seed=0)
            state.park_run_factor = await resolve_park_run_factor(
                conn, duck, game_pk, int(state.season)
            )
            venue = await resolve_venue_id(conn, game_pk)
            if venue is not None:
                state.park = str(venue)
            resolve_manager_profiles_onto_state(state, duck)
            return state
        finally:
            await conn.close()

    kw = sim_kwargs_from_state(asyncio.run(_resolve()))
    starters = {k: kw[k] for k in ("pitcher_id", "home_pitcher_id", "away_pitcher_id")}
    print(f"kwargs: {starters}  pen source {kw['bullpen_source']}", flush=True)
    machine = production_machine_factory(0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw)))

    # Side 0: the AWAY team bats (the top halves).  Side 1: the HOME team bats.
    tally = {s: collections.Counter() for s in (0, 1)}
    faced = {s: collections.Counter() for s in (0, 1)}
    orig_accumulate = machine._accumulate_pa
    orig_fielding = machine._full_pool_fielding

    def accumulate(st: Any, result: Any) -> Any:
        side = int(st.offense)
        event = result.canonical_event or resolve_event_to_canonical(result.event)
        tally[side]["PA"] += 1
        faced[side][int(st.pitcher_id) if st.pitcher_id is not None else -1] += 1
        if event in ("single", "double", "triple", "home_run"):
            tally[side]["H"] += 1
            tally[side][event] += 1
        elif event == "walk":
            tally[side]["BB"] += 1
        elif event == "intentional_walk":
            tally[side]["IBB"] += 1
        elif event == "strikeout":
            tally[side]["K"] += 1
        elif event == "hit_by_pitch":
            tally[side]["HBP"] += 1
        return orig_accumulate(st, result)

    def fielding(st: Any) -> Any:
        sig = orig_fielding(st)
        if sig is not None:
            tally[int(st.offense)]["BIP"] += 1
        return sig

    machine._accumulate_pa = accumulate
    machine._full_pool_fielding = fielding

    runs = {0: 0, 1: 0}
    runs_sq = {0: 0, 1: 0}
    home_wins = away_wins = pitches = 0
    started = time.perf_counter()
    for seed in range(n):
        machine.boxscore = BoxScore()
        res = simulate_game(state_machine=machine, seed=seed, **kw)
        for side, score in ((0, int(res.away_score)), (1, int(res.home_score))):
            runs[side] += score
            runs_sq[side] += score * score
        home_wins += int(res.home_score > res.away_score)
        away_wins += int(res.away_score > res.home_score)
        pitches += int(res.total_pitches)
    record = {
        "game_pk": game_pk,
        "n": n,
        "kwargs": starters,
        "away_batting": dict(tally[0]),
        "home_batting": dict(tally[1]),
        "faced_away_batters": {str(k): v for k, v in faced[0].most_common(8)},
        "faced_home_batters": {str(k): v for k, v in faced[1].most_common(8)},
        "away_runs": runs[0],
        "home_runs": runs[1],
        "away_runs_sq": runs_sq[0],
        "home_runs_sq": runs_sq[1],
        "home_wins": home_wins,
        "away_wins": away_wins,
        "pitches": pitches,
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
    print(f"wrote {out_path} ({n} game-sims, {record['elapsed_s']} s)")


def _per_sim(arm: dict, key: str) -> float:
    return sum(arm[s].get(key, 0) for s in _SIDES) / arm["n"]


def compare(old_path: str, new_path: str) -> None:
    """Print the paired read of the two records ``run`` wrote."""
    with open(old_path, encoding="utf-8") as fh:
        old = json.load(fh)
    with open(new_path, encoding="utf-8") as fh:
        new = json.load(fh)
    n = old["n"]
    if new["n"] != n or new["game_pk"] != old["game_pk"]:
        raise SystemExit("the two records are not the same game at the same number of sims")
    print(f"Game {old['game_pk']}: {n} game-sims an arm, the same seeds.")
    for name, arm in (("BEFORE the fix", old), ("FIXED", new)):
        print(f"\n{name}: {arm['kwargs']}")
        print(f"  faced the AWAY batters (top halves):    {arm['faced_away_batters']}")
        print(f"  faced the HOME batters (bottom halves): {arm['faced_home_batters']}")
        for side in _SIDES:
            t = arm[side]
            pa, bip = t["PA"], t["BIP"]
            print(
                f"  {side:13s} PA/game {pa / n:5.2f}  K/PA {t['K'] / pa:.4f}  BB/PA {t['BB'] / pa:.4f}"
                f"  HBP/PA {t.get('HBP', 0) / pa:.4f}  H/PA {t['H'] / pa:.4f}"
                f"  HR/BIP {t['home_run'] / bip:.4f}"
            )
        decided = arm["home_wins"] + arm["away_wins"]
        print(
            f"  runs/game away {arm['away_runs'] / n:.3f}  home {arm['home_runs'] / n:.3f}"
            f"   home win share {arm['home_wins'] / decided:.3f}"
        )

    print(f"\nThe move in the {_LANE_GAMES} x {_LANE_ITERS} lane's totals (2026-10-03 lane):")
    d_pa = (_per_sim(new, "PA") - _per_sim(old, "PA")) * _LANE_ITERS
    for ch in ("K", "BB", "HBP"):
        total, centre = _LANE_COUNTS[ch], _LANE_CENTRES[ch]
        d = (_per_sim(new, ch) - _per_sim(old, ch)) * _LANE_ITERS
        before = total / _LANE_PA
        after = (total + d) / (_LANE_PA + d_pa)
        # One standard error of the paired difference (counts, both arms).
        se = math.sqrt((_per_sim(old, ch) + _per_sim(new, ch)) * n) / n * _LANE_ITERS / total
        print(
            f"  {ch}/PA   {before:.5f} -> {after:.5f}  ({(after / before - 1) * 100:+.2f}%,"
            f" one standard error {se * 100:.2f}%)   against the centre:"
            f" {(before / centre - 1) * 100:+.2f}% -> {(after / centre - 1) * 100:+.2f}%"
        )
    for ch in ("single", "double", "triple", "home_run"):
        a, b = _per_sim(old, ch), _per_sim(new, ch)
        print(
            f"  {ch:8s} per game-sim {a:.2f} -> {b:.2f}"
            f"   (the lane's total moves about {(b - a) / a / _LANE_GAMES * 100:+.2f}%)"
        )

    def _var(arm: dict) -> float:
        out = 0.0
        for side in ("away", "home"):
            mean = arm[f"{side}_runs"] / n
            out += arm[f"{side}_runs_sq"] / n - mean * mean
        return out

    r_old = (old["away_runs"] + old["home_runs"]) / n
    r_new = (new["away_runs"] + new["home_runs"]) / n
    se_r = math.sqrt(_var(old) / n + _var(new) / n)
    per_team = 2 * _LANE_GAMES
    print(
        f"  runs per game (both teams) {r_old:.3f} -> {r_new:.3f}"
        f" (one standard error {se_r:.3f}); the lane's runs per team-game move"
        f" {(r_new - r_old) / per_team:+.4f} (one standard error {se_r / per_team:.4f})"
    )
    hw_old = old["home_wins"] / (old["home_wins"] + old["away_wins"])
    hw_new = new["home_wins"] / (new["home_wins"] + new["away_wins"])
    print(
        f"  home win share of this game {hw_old:.3f} -> {hw_new:.3f};"
        f" the lane's share moves {(hw_new - hw_old) / _LANE_GAMES:+.4f}"
    )


def main() -> None:
    if len(sys.argv) == 5 and sys.argv[1] == "run":
        run(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
