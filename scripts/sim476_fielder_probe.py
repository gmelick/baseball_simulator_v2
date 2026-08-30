"""
scripts/sim476_fielder_probe.py — the SIM-476 part-3 fielder-kernel instrument.

WHAT THIS IS
============
Measures the sim's out-vs-reach rate on FIELDABLE batted balls (home runs
excluded) CONDITIONAL on the live defender's OAA tier at the drawn row's
position, against the pool's own rate conditional on the ROW fielder's OAA
tier. The fielder kernel (``SIM_FIELDER_KERNEL_SIGMA``, a Gaussian on the
standardized ``outs_above_average`` distance between the live defender and the
row's own fielder) is correct when a ball drawn toward an elite live defender
reaches base as rarely as the pool says balls at elite fielders do.

Tiers are recency-weighted terciles of the pool row fielders' OAA z-score,
computed WITHIN each position group (IF = 1B/2B/3B/SS, OF = LF/CF/RF,
battery = P/C) — OAA scales differ by position family, and out rates differ
structurally, so the conditional is read per group. The sigma=0 control arm
carries the composition baseline; the kernel effect is the arm-minus-control
delta per (group, tier).

Instrumentation: wrap-ONCE at the sampler seam (sentinel-guarded — the
SIM-514 stacking trap). The drawn row comes from ``fp._bb_last_i``; the live
defender comes from the game's defense map, staged per game by the harness.

USAGE
-----
    SIM_FIELDER_KERNEL_SIGMA=1.0 python scripts/sim476_fielder_probe.py \
        --iters 200 --json-out /app/scripts/sim476_fielder_s100.json
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

_REACH_EVENTS = {"single", "double", "triple", "field_error"}
#: One tier group PER POSITION (the pooled IF/OF grouping confounded the
#: reference: OAA z distributions and reach rates differ by position, so a
#: cross-position tercile read elite IF as allowing MORE reaches). The battery
#: carries no OAA and is dropped by the empty-group guard.
_GROUPS = {
    "1B": (3,),
    "2B": (4,),
    "3B": (5,),
    "SS": (6,),
    "LF": (7,),
    "CF": (8,),
    "RF": (9,),
}
_POS_NUM_TO_NAME = {
    1: "P",
    2: "C",
    3: "1B",
    4: "2B",
    5: "3B",
    6: "SS",
    7: "LF",
    8: "CF",
    9: "RF",
}
_TIERS = ("low", "mid", "high")


def _group(pos: int) -> str | None:
    for g, nums in _GROUPS.items():
        if pos in nums:
            return g
    return None


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


class Recorder:
    """Per (group, live-defender tier) fieldable-ball reach tallies."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], dict[str, int]] = {}
        #: game context: position number -> ("low"|"mid"|"high") or None.
        self.ctx_tier_by_pos: dict[int, str | None] = {}

    def bump(self, group: str, tier: str, reached: bool) -> None:
        t = self.counts.setdefault((group, tier), {"n": 0, "reach": 0})
        t["n"] += 1
        if reached:
            t["reach"] += 1


class PoolRef:
    """The pool's OAA-tier machinery, shared by the reference and the wrapper."""

    def __init__(self, sampler: Any) -> None:
        self.s = sampler
        z = sampler._emb_z("fielder")
        emb = sampler.a.actor_emb.get("fielder")
        cols = sampler._steal_feat_cols("fielder", sampler._FIELDER_BB_FEATURES)
        if z is None or emb is None or cols is None:
            raise RuntimeError("no fielder embedding / OAA column in this bundle")
        self.z = z
        self.key_index = emb["key_index"]
        self.col = int(cols[0])
        #: (group -> (lo_edge, hi_edge)) recency-weighted tercile edges of the
        #: ROW fielders' OAA z, pooled across hands.
        self.edges: dict[str, tuple[float, float]] = {}
        self._fit_edges()

    def _rows(self, hand: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        pool = self.s.a.bb_pools[hand]
        row_emb = self.s._bb_fielder_emb_rows(hand)
        pos = getattr(pool, "fielder_pos", None)
        if row_emb is None or pos is None:
            return None
        oaa = np.where(row_emb >= 0, self.z[np.clip(row_emb, 0, len(self.z) - 1), self.col], np.nan)
        return (
            np.asarray(pos).astype(np.int64),
            oaa,
            pool.recency.astype(np.float64),
            np.asarray(pool.event, dtype=object),
        )

    def _fit_edges(self) -> None:
        by_group: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {g: [] for g in _GROUPS}
        for hand in self.s.a.bb_pools:
            r = self._rows(hand)
            if r is None:
                raise RuntimeError(f"the {hand} pool has no fielder columns (pre-0012 bundle)")
            pos, oaa, rcy, _ev = r
            for g, nums in _GROUPS.items():
                m = np.isin(pos, nums) & ~np.isnan(oaa)
                by_group[g].append((oaa[m], rcy[m]))
        for g, parts in by_group.items():
            v = np.concatenate([p[0] for p in parts])
            w = np.concatenate([p[1] for p in parts])
            if v.size == 0 or float(w.sum()) <= 0.0:
                # A group with no OAA-embedded row fielders (the battery:
                # pitchers/catchers carry no OAA) has no tiers — drop it.
                print(f"  (group {g}: no OAA rows in the pool — dropped)")
                continue
            order = np.argsort(v)
            cum = np.cumsum(w[order])
            lo = float(v[order][np.searchsorted(cum, cum[-1] / 3.0)])
            hi = float(v[order][np.searchsorted(cum, 2.0 * cum[-1] / 3.0)])
            self.edges[g] = (lo, hi)

    def tier(self, group: str, oaa_z: float) -> str | None:
        if group not in self.edges:
            return None
        lo, hi = self.edges[group]
        if oaa_z < lo:
            return "low"
        if oaa_z < hi:
            return "mid"
        return "high"

    def live_tiers(self, defense_map: dict[str, int], season: int) -> dict[int, str | None]:
        out: dict[int, str | None] = {}
        for p, name in _POS_NUM_TO_NAME.items():
            pid = defense_map.get(name)
            idx = self.key_index.get(f"{int(pid)}:{name}:{int(season)}", -1) if pid else -1
            g = _group(p)
            if idx < 0 or g is None:
                out[p] = None
            else:
                out[p] = self.tier(g, float(self.z[idx, self.col]))
        return out

    def reference(self) -> dict[str, Any]:
        """Pool reach rate on fieldable balls per (group, ROW-fielder tier)."""
        agg: dict[tuple[str, str], list[float]] = {}
        for hand in self.s.a.bb_pools:
            r = self._rows(hand)
            assert r is not None
            pos, oaa, rcy, ev = r
            fieldable = np.array([e != "home_run" for e in ev], dtype=bool)
            reach = np.array([e in _REACH_EVENTS for e in ev], dtype=bool)
            for g, nums in _GROUPS.items():
                if g not in self.edges:
                    continue
                base = np.isin(pos, nums) & ~np.isnan(oaa) & fieldable
                lo, hi = self.edges[g]
                for t, m in (
                    ("low", base & (oaa < lo)),
                    ("mid", base & (oaa >= lo) & (oaa < hi)),
                    ("high", base & (oaa >= hi)),
                ):
                    cur = agg.setdefault((g, t), [0.0, 0.0])
                    cur[0] += float(rcy[m & reach].sum())
                    cur[1] += float(rcy[m].sum())
        return {
            f"{g}|{t}": {"reach_rate": (v[0] / v[1] if v[1] else 0.0), "weight": v[1]}
            for (g, t), v in agg.items()
        }


def _install(machine: Any, rec: Recorder, ref: PoolRef) -> None:
    """Wrap-ONCE sampler wrappers: stage the FIELDING side's tier map at
    ``battedball_new_pa`` (the defense map is a per-PA kwarg — the fielding
    side alternates every half-inning), classify each drawn fieldable ball at
    ``battedball_draw``."""
    fp = machine.full_pool_sampler
    if getattr(fp.battedball_draw, "_sim476_fielder_wrapped", False):
        return  # the cached sampler is already instrumented (the stacking trap)

    orig_new_pa = fp.battedball_new_pa
    tier_cache: dict[tuple, dict[int, str | None]] = {}

    def new_pa(*a: Any, **kw: Any) -> Any:
        dm = kw.get("defense_map")
        season = int(kw.get("live_season") or 0)
        if dm:
            key = (season, tuple(sorted(dm.items())))
            tiers = tier_cache.get(key)
            if tiers is None:
                tiers = ref.live_tiers(dm, season)
                tier_cache[key] = tiers
            rec.ctx_tier_by_pos = tiers
        else:
            rec.ctx_tier_by_pos = {}
        return orig_new_pa(*a, **kw)

    fp.battedball_new_pa = new_pa
    orig_draw = fp.battedball_draw

    def draw(*a: Any, **kw: Any) -> Any:
        res = orig_draw(*a, **kw)
        i = fp._bb_last_i
        hand = fp._bb_hand
        if i is not None and hand is not None and res[0] != "home_run":
            pool = fp.a.bb_pools[hand]
            pos_arr = getattr(pool, "fielder_pos", None)
            if pos_arr is not None:
                pos = int(pos_arr[i])
                g = _group(pos)
                tier = rec.ctx_tier_by_pos.get(pos)
                if g is not None and tier is not None:
                    rec.bump(g, tier, str(res[0]) in _REACH_EVENTS)
        return res

    draw._sim476_fielder_wrapped = True  # type: ignore[attr-defined]
    fp.battedball_draw = draw


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
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)
    sigma = float(os.environ.get("SIM_FIELDER_KERNEL_SIGMA", "0"))

    started = time.perf_counter()
    print(f"sim476_fielder_probe: {len(game_pks)} games x {args.iters} sims  fielder_sigma={sigma}")

    rec = Recorder()
    ref: PoolRef | None = None
    pool_ref: dict[str, Any] | None = None
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if ref is None:
                ref = PoolRef(machine.full_pool_sampler)
                pool_ref = ref.reference()
            _install(machine, rec, ref)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            print(f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s")
    finally:
        if duck is not None:
            duck.close()

    assert pool_ref is not None
    print("\n=== fieldable-ball reach rate by (group, live-defender tier) ===")
    print(f" {'group':>8} {'tier':>5} {'simN':>8} {'simReach':>9} {'poolReach':>10} {'delta':>8}")
    for g in _GROUPS:
        for t in _TIERS:
            sim = rec.counts.get((g, t), {"n": 0, "reach": 0})
            pr = pool_ref.get(f"{g}|{t}", {}).get("reach_rate", 0.0)
            rate = sim["reach"] / sim["n"] if sim["n"] else float("nan")
            print(
                f" {g:>8} {t:>5} {sim['n']:>8d} {rate:9.5f} {pr:10.5f}"
                f" {(rate - pr if sim['n'] else float('nan')):+8.5f}"
            )
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload = {
            "iters": args.iters,
            "game_pks": game_pks,
            "fielder_sigma": sigma,
            "sim_counts": {f"{g}|{t}": v for (g, t), v in rec.counts.items()},
            "pool": pool_ref,
            "edges": {g: list(e) for g, e in (ref.edges.items() if ref else [])},
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
