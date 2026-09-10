"""

ARCHIVED 2026-09-09: these tests measured the SIM-517 bell-curve receiving
kernel, which SIM-523 part E deleted (the two sigmas no longer exist on the
sampler; setting them changes nothing). Kept as the record of that measurement.
scripts/sim523_sim_tests.py — SIM-523 tests 2 + 4 on the SIMULATOR (production config, index off).

Test 4 — measure the draw directly. For N games, after every plate appearance's
weights are assembled, record the share of the total draw weight that lands on
(a) rows caught by the live catcher himself and (b) rows thrown by his own
staff (the pitchers he caught that season, from the pool), with the receiving
kernel OFF and ON. The pool's unweighted share is the reference.

Test 2 — swap the catcher's team. For each game, replace both live catchers
with a "twin": the catcher-season with the closest receiving profile (the
kernel's own metric) whose staff shares no pitcher with the real one. Run
--iters games per arm: real catcher / twin catcher, kernel ON, plus the
kernel-OFF reference. Skill would leave walks, strikeouts, hit-by-pitches and
called strikes unchanged; a move means the weight reads the team.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_sim_tests.py --test 4 --iters 10 744795 661032 564734
    ... --test 2 --iters 100 744795 661032 564734 825108
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
from sim_stats import (  # noqa: E402
    _FACTORY,
    _game_summary,
    _resolve,
    open_sim_duckdb,
    sim_kwargs_from_state,
)

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
SEASONS = "2023, 2024, 2025, 2026"


def _staffs(catcher_ids, season):
    """catcher_id -> set of pitcher ids he caught that season (from the pool)."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute(
            "SELECT catcher_id, pitcher_id FROM sim.pitch_pool "
            f"WHERE season = {int(season)} AND catcher_id IN ({', '.join(str(int(c)) for c in catcher_ids)}) "
            "GROUP BY 1, 2"
        ).fetchall()
        allc = con.execute(
            f"SELECT catcher_id, pitcher_id FROM sim.pitch_pool WHERE season = {int(season)} "
            "AND catcher_id IS NOT NULL GROUP BY 1, 2"
        ).fetchall()
    finally:
        con.close()
    staff = defaultdict(set)
    for c, p in rows:
        staff[int(c)].add(int(p))
    every = defaultdict(set)
    for c, p in allc:
        every[int(c)].add(int(p))
    return staff, every


def _run(machine, kw, seeds, home_ids, away_ids):
    machine._fp_pitcher_key = None
    machine._fp_pa_key = None
    sums = []
    for seed in seeds:
        machine.boxscore = BoxScore()
        res = simulate_game(state_machine=machine, seed=seed, **kw)
        sums.append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
    return sums


class _Tap:
    """Count pitch-draw outcomes and terminal plate appearances by tapping draw()."""

    def __init__(self, fp):
        self.fp, self.out = fp, Counter()
        self._draw = fp.draw

    def install(self):
        tap = self

        def draw(balls=0, strikes=0):
            o = tap._draw(balls, strikes)
            tap.out[o] += 1
            return o

        self.fp.draw = draw

    def remove(self):
        self.fp.draw = self._draw


def _resolve_games(game_pks):
    duck = open_sim_duckdb()
    try:
        return [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()


def _ids(state):
    home = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
    away = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
    if getattr(state, "home_pitcher_id", None):
        home.add(int(state.home_pitcher_id))
    if getattr(state, "away_pitcher_id", None):
        away.add(int(state.away_pitcher_id))
    return home, away


def test4(states, iters):
    print("\n=== TEST 4 — share of the draw weight on the live catcher's own rows / own staff ===")
    res = {False: defaultdict(list), True: defaultdict(list)}
    for state in states:
        kw = sim_kwargs_from_state(state)
        season = int(kw.get("season", 2024))
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        fp.pitch_cell_index = False
        cids = [c for c in (kw.get("home_catcher_id"), kw.get("away_catcher_id")) if c]
        staff, _ = _staffs(cids, season)
        orig = fp.new_plate_appearance
        sigmas = (
            getattr(fp, "catcher_framing_sigma", 0.0),
            getattr(fp, "catcher_block_sigma", 0.0),
        )

        def npa(batter_key, base_out, *, fp=fp, staff=staff, orig=orig, **k):
            orig(batter_key, base_out, **k)
            hand = fp._hand
            pool = fp.a.pools[hand]
            meta = fp._pool_meta(hand)
            ck = fp._catcher_key
            if not ck or fp._bucket_cdf is None:
                return
            live_c = int(ck.split(":")[0])
            own = pool.catcher_id == live_c
            staff_mask = np.isin(pool.pitcher_id, list(staff.get(live_c, ())))
            tot = own_m = staff_m = 0.0
            for cdf, rows in zip(fp._bucket_cdf, meta["bucket_rows"], strict=False):
                if cdf is None:
                    continue
                w = np.diff(np.concatenate([[0.0], cdf]))
                tot += float(cdf[-1])
                own_m += float(w[own[rows]].sum())
                staff_m += float(w[staff_mask[rows]].sum())
            if tot > 0:
                key = getattr(fp, "catcher_framing_sigma", 0.0) > 0
                res[key]["own"].append(own_m / tot)
                res[key]["staff"].append(staff_m / tot)
                res[key]["pool_own"].append(float(own[meta["bucket_rows"][0]].mean()))
                res[key]["pool_staff"].append(float(staff_mask[meta["bucket_rows"][0]].mean()))

        fp.new_plate_appearance = npa
        for on in (False, True):
            fp.catcher_framing_sigma, fp.catcher_block_sigma = sigmas if on else (0.0, 0.0)
            _run(machine, kw, list(range(iters)), *_ids(state))
        fp.new_plate_appearance = orig
    for on in (False, True):
        r = res[on]
        print(
            f"  receiving kernel {'ON ' if on else 'OFF'}: PAs={len(r['own'])}  "
            f"weight on the catcher's OWN rows = {np.mean(r['own']):.2%}  "
            f"on his STAFF's rows = {np.mean(r['staff']):.2%}  "
            f"(pool share: own {np.mean(r['pool_own']):.2%}, staff {np.mean(r['pool_staff']):.2%})"
        )


def _twin(fp, live_key, every, live_staff):
    """The closest catcher-season in the receiving metric whose staff shares no pitcher."""
    data = fp._catcher_receiving_data()
    if data is None:
        return None
    ki, z = data
    if live_key not in ki:
        return None
    lz = z[ki[live_key]]
    if not np.isfinite(lz).all():
        return None
    best, best_d = None, None
    season = live_key.split(":")[1]
    for key, i in ki.items():
        if key == live_key or not key.endswith(":" + season):
            continue
        cid = int(key.split(":")[0])
        if every.get(cid, set()) & live_staff:
            continue
        zz = z[i]
        if not np.isfinite(zz).all():
            continue
        d = float(((zz - lz) ** 2).sum())
        if best is None or d < best_d:
            best, best_d = key, d
    return best, best_d


def test2(states, iters):
    print(
        "\n=== TEST 2 — swap the catcher's team (twin with the closest receiving profile, disjoint staff) ==="
    )
    arms = {"real, kernel ON": Counter(), "twin, kernel ON": Counter(), "kernel OFF": Counter()}
    box = {k: [] for k in arms}
    for state in states:
        kw = sim_kwargs_from_state(state)
        season = int(kw.get("season", 2024))
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        fp.pitch_cell_index = False
        sigmas = (
            getattr(fp, "catcher_framing_sigma", 0.0),
            getattr(fp, "catcher_block_sigma", 0.0),
        )
        cids = [c for c in (kw.get("home_catcher_id"), kw.get("away_catcher_id")) if c]
        staff, every = _staffs(cids, season)
        twin_kw = dict(kw)
        for side in ("home_catcher_id", "away_catcher_id"):
            c = kw.get(side)
            if not c:
                continue
            t = _twin(fp, f"{int(c)}:{season}", every, staff.get(int(c), set()))
            if t and t[0]:
                twin_kw[side] = int(t[0].split(":")[0])
                print(
                    f"  game {kw.get('game_pk', '?')} {side}: {c} -> twin {twin_kw[side]} (receiving distance {t[1]:.3f})"
                )
        for arm, arm_kw, on in (
            ("real, kernel ON", kw, True),
            ("twin, kernel ON", twin_kw, True),
            ("kernel OFF", kw, False),
        ):
            fp.catcher_framing_sigma, fp.catcher_block_sigma = sigmas if on else (0.0, 0.0)
            tap = _Tap(fp)
            tap.install()
            box[arm].extend(_run(machine, arm_kw, list(range(iters)), *_ids(state)))
            tap.remove()
            arms[arm].update(tap.out)
    print(
        f"\n  {'arm':>16} {'R/g':>7} {'BB/g':>7} {'K/g':>7} {'HBP draws/g':>12} {'called-strike share of taken':>29}"
    )
    for arm in arms:
        n = max(1, len(box[arm]))
        c = arms[arm]
        taken = c["ball"] + c["called_strike"]
        print(
            f"  {arm:>16} {np.mean([b['R'] for b in box[arm]]):7.2f} {np.mean([b['BB'] for b in box[arm]]):7.2f} "
            f"{np.mean([b['K'] for b in box[arm]]):7.2f} {c['hit_by_pitch'] / n:12.3f} {c['called_strike'] / max(1, taken):29.4f}"
        )
    print(
        "  Read: 'twin' keeps the catcher's SKILL numbers but swaps his staff. Skill = no move from 'real'."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--test", type=int, choices=(2, 4), required=True)
    ap.add_argument("--iters", type=int, default=20)
    args = ap.parse_args()
    states = _resolve_games(args.game_pks)
    if args.test == 4:
        test4(states, args.iters)
    else:
        test2(states, args.iters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
