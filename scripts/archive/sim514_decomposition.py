"""
scripts/sim514_decomposition.py — the SIM-514 four-channel decomposition (2026-08-19 plan §2).

WHAT THIS IS
============
The certified 12x471 lane read four residual reds: DP +8.3%, K -2.5%, SB -7.6%,
3B +11.9%. The plan's hypothesis: most are TRAFFIC effects of the SIM-429 walk
surplus, not defects of their own machinery. This script measures the
per-opportunity decompositions that separate traffic from rate:

  (a) DP  — DP per OPPORTUNITY (a ball-in-play with a runner on 1B and <2 outs)
      vs the pool's own per-cell DP-row rate (``r1_dest = 0`` and
      ``batter_dest = 0`` on a transition row). If per-opportunity matches and
      only occupancy is high, the DP red closes with the SIM-429 walk fix.
  (c) SB  — attempts per opportunity-pitch (each ``steal_draw`` call is one
      SIM-468 opportunity) vs opportunities per game, against the pool's own
      attempt rate. If attempts-per-opportunity matches the certified read,
      the SB red is an opportunity-traffic effect.
  (d) 3B  — the drawn triple rate per base-out cell vs the pool's OWN per-cell
      rate. A match means the surplus is the pool's 2024-26 era vs the 2025
      band centre — an owner reference question, not code.

  (b) K   — K/PA vs PA/game needs no new instrumentation here: the SIM-429
      diagnosis run (scripts/sim429_count_diagnosis.py) reports both.

  (d)(2)  — the double-count check is closed by inspection (2026-08-19): the
      batter-stretch draw ``draw(5, 0, 3, bid)`` fires only inside the
      ``hit == 2`` branch of ``_run_advancement_draws``, and no code path
      relabels an event to ``triple`` — the box's triples come only from drawn
      ``triple`` rows.

HOW THE SIM SIDE IS INSTRUMENTED
================================
Two instance wrappers, installed per game machine:

  * ``machine._full_pool_fielding`` — at entry the GameState carries the live
    base-out cell; the returned FieldingSignal carries the drawn event and the
    transition row. Records: balls-in-play per cell, drawn DP rows and DP-class
    event labels, drawn triples, and DP opportunities.
  * ``machine.full_pool_sampler.steal_draw`` — each call is one steal
    opportunity; the drawn flags say attempted / safe.

USAGE
-----
    # Smoke (wiring proof):
    python scripts/sim514_decomposition.py --iters 3 745199 746494

    # The decomposition read (~1 h serial):
    python scripts/sim514_decomposition.py --iters 150 --json-out /app/scripts/sim514_decomp.json
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

#: The certified acceptance game set (tests/acceptance/bands.py BALANCED_GAME_ORDER).
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

#: 2025 band centres (tests/acceptance/bands.py, SIM-508) for context lines.
_CENTRES_2025 = {"DP": 0.7459, "3B": 0.1292, "SB": 0.6251, "CS": 0.1922}

_DP_EVENTS = frozenset(
    {"grounded_into_double_play", "ground_into_double_play", "double_play", "sac_fly_double_play"}
)


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


# ---------------------------------------------------------------------------
# Sim-side recorder
# ---------------------------------------------------------------------------
class Recorder:
    """Counters fed by the two instance wrappers."""

    def __init__(self) -> None:
        # (a) + (d): per base-out cell (runners_state 0-7, outs 0-2).
        self.bip = np.zeros((8, 3), dtype=np.int64)
        self.dp_row = np.zeros((8, 3), dtype=np.int64)  # r1==0 and batter==0
        self.dp_event = np.zeros((8, 3), dtype=np.int64)  # DP-class event label
        self.triple = np.zeros((8, 3), dtype=np.int64)
        self.dp_opportunities = 0  # runner on 1B and <2 outs at a ball-in-play
        self.no_transition = 0  # canary: legacy-path resolutions (expect 0)
        # (c): steal opportunities and drawn flags, per target base.
        self.steal_opp: dict[str, int] = {}
        self.steal_att: dict[str, int] = {}
        self.steal_sb: dict[str, int] = {}
        self.steal_cs: dict[str, int] = {}
        self.steal_none = 0  # draws that found no pool cell

    def record_bip(self, rstate: int, outs: int, r1_occupied: bool, sig: Any) -> None:
        rs, o = int(rstate) & 0b111, min(max(int(outs), 0), 2)
        self.bip[rs, o] += 1
        tr = getattr(sig, "transition", None)
        if tr is None:
            self.no_transition += 1
        if r1_occupied and o < 2:
            self.dp_opportunities += 1
            if tr is not None and tr.get("r1") == 0 and tr.get("batter") == 0:
                self.dp_row[rs, o] += 1
        if str(sig.event) in _DP_EVENTS:
            self.dp_event[rs, o] += 1
        if str(sig.event) == "triple":
            self.triple[rs, o] += 1

    def record_steal(self, target: str, res: Any) -> None:
        if res is None:
            self.steal_none += 1
            return
        self.steal_opp[target] = self.steal_opp.get(target, 0) + 1
        attempted, success = bool(res[0]), bool(res[1])
        if attempted:
            self.steal_att[target] = self.steal_att.get(target, 0) + 1
            if success:
                self.steal_sb[target] = self.steal_sb.get(target, 0) + 1
            else:
                self.steal_cs[target] = self.steal_cs.get(target, 0) + 1


def _instrument(machine: Any, rec: Recorder) -> None:
    orig_field = machine._full_pool_fielding

    def wrapped_field(state: Any) -> Any:
        rs = int(state.runners_state)
        outs = int(state.outs)
        r1 = state.bases.first is not None
        sig = orig_field(state)
        if sig is not None:
            rec.record_bip(rs, outs, r1, sig)
        return sig

    machine._full_pool_fielding = wrapped_field

    fp = machine.full_pool_sampler
    orig_steal = fp.steal_draw

    def wrapped_steal(target_base: int, *a: Any, **kw: Any) -> Any:
        res = orig_steal(target_base, *a, **kw)
        rec.record_steal(str(int(target_base)), res)
        return res

    fp.steal_draw = wrapped_steal


# ---------------------------------------------------------------------------
# Pool-side per-cell rates (from the loaded transition bundle)
# ---------------------------------------------------------------------------
def _pool_cell_rates(sampler: Any) -> dict[str, Any]:
    """Per-cell DP-row and triple rates over the batted-ball pools, both hands
    summed: unweighted counts and recency-weighted mass."""
    n = np.zeros((8, 3), dtype=np.float64)
    n_rcy = np.zeros((8, 3), dtype=np.float64)
    dp = np.zeros((8, 3), dtype=np.float64)
    dp_rcy = np.zeros((8, 3), dtype=np.float64)
    tri = np.zeros((8, 3), dtype=np.float64)
    tri_rcy = np.zeros((8, 3), dtype=np.float64)
    for hand, pool in sampler.a.bb_pools.items():
        r1d = getattr(pool, "r1_dest", None)
        bd = getattr(pool, "batter_dest", None)
        if r1d is None or bd is None:
            raise RuntimeError(f"the {hand}-hand pool has no transition columns — legacy bundle?")
        outs = np.clip(pool.sit[:, 2].astype(np.int64), 0, 2)
        rs = pool.sit[:, 3].astype(np.int64) & 0b111
        rcy = pool.recency.astype(np.float64)
        is_dp = (np.asarray(r1d) == 0) & (np.asarray(bd) == 0)
        is_tri = np.asarray(pool.event, dtype=object) == "triple"
        np.add.at(n, (rs, outs), 1.0)
        np.add.at(n_rcy, (rs, outs), rcy)
        np.add.at(dp, (rs, outs), is_dp.astype(np.float64))
        np.add.at(dp_rcy, (rs, outs), is_dp * rcy)
        np.add.at(tri, (rs, outs), is_tri.astype(np.float64))
        np.add.at(tri_rcy, (rs, outs), is_tri * rcy)
    out: dict[str, Any] = {
        "n": n,
        "n_rcy": n_rcy,
        "dp": dp,
        "dp_rcy": dp_rcy,
        "tri": tri,
        "tri_rcy": tri_rcy,
    }
    # Steal pools: the pool's own attempt/success rates per target base.
    steals: dict[str, dict[str, float]] = {}
    for target, spool in sampler.a.steal_pools.items():
        att = np.asarray(spool.attempted, dtype=np.float64)
        suc = np.asarray(spool.success, dtype=np.float64)
        rcy = spool.recency.astype(np.float64)
        steals[target] = {
            "rows": float(len(att)),
            "attempt_rate": float(att.mean()) if len(att) else 0.0,
            "attempt_rate_rcy": float((att * rcy).sum() / rcy.sum()) if rcy.sum() else 0.0,
            "safe_share": float(suc[att > 0].mean()) if att.sum() else 0.0,
        }
    out["steals"] = steals
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _print_report(rec: Recorder, pool: dict[str, Any], n_sims: int) -> dict[str, Any]:
    tg = 2.0 * n_sims  # team-games
    bip_total = int(rec.bip.sum())
    dp_total = int(rec.dp_row.sum())
    dp_ev_total = int(rec.dp_event.sum())
    tri_total = int(rec.triple.sum())

    print("\n" + "=" * 84)
    print(
        f" SIM-514 DECOMPOSITION — {n_sims} sims, {bip_total} balls in play, "
        f"{rec.no_transition} legacy resolutions (expect 0)"
    )
    print("=" * 84)

    # --- (a) DP per opportunity --------------------------------------------
    opp = rec.dp_opportunities
    dp_per_opp = dp_total / opp if opp else 0.0
    print("\n--- (a) DP: per-opportunity vs traffic ---")
    print(
        f" DP/team-game (transition rows) = {dp_total / tg:.4f}   centre {_CENTRES_2025['DP']}"
        f"   ({(dp_total / tg - _CENTRES_2025['DP']) / _CENTRES_2025['DP'] * 100.0:+.1f}%)"
    )
    print(f" DP-class event labels/team-game = {dp_ev_total / tg:.4f}")
    print(
        f" opportunities/team-game (BIP, runner on 1B, <2 outs) = {opp / tg:.4f}"
        f"   DP per opportunity = {dp_per_opp:.4f}"
    )
    print(" per cell (runner-on-1B cells, <2 outs): sim rate vs pool unweighted / recency:")
    print(
        f" {'cell':>9} {'simBIP':>7} {'simDP':>6} {'simRate':>8} {'poolUNW':>8}"
        f" {'poolRCY':>8} {'delta':>8}"
    )
    for rs in range(8):
        if not rs & 1:
            continue
        for o in range(2):
            n_sim = int(rec.bip[rs, o])
            if n_sim == 0 and pool["n"][rs, o] == 0:
                continue
            sim_rate = rec.dp_row[rs, o] / n_sim if n_sim else 0.0
            unw = pool["dp"][rs, o] / pool["n"][rs, o] if pool["n"][rs, o] else 0.0
            rcy = pool["dp_rcy"][rs, o] / pool["n_rcy"][rs, o] if pool["n_rcy"][rs, o] else 0.0
            print(
                f" rs={rs} o={o:>2} {n_sim:>7d} {int(rec.dp_row[rs, o]):>6d} {sim_rate:8.4f}"
                f" {unw:8.4f} {rcy:8.4f} {sim_rate - rcy:+8.4f}"
            )
    # The aggregate expected-from-pool DP per opportunity at the SIM's cell mix.
    exp_dp = 0.0
    for rs in range(8):
        if not rs & 1:
            continue
        for o in range(2):
            if pool["n_rcy"][rs, o]:
                exp_dp += rec.bip[rs, o] * (pool["dp_rcy"][rs, o] / pool["n_rcy"][rs, o])
    exp_rate = exp_dp / opp if opp else 0.0
    print(
        f" expected DP/opportunity at the sim's cell mix, pool-recency rates = {exp_rate:.4f}"
        f"   sim = {dp_per_opp:.4f}   ratio = "
        f"{dp_per_opp / exp_rate if exp_rate else 0.0:.3f}"
    )

    # --- (c) SB attempts per opportunity ------------------------------------
    print("\n--- (c) SB: attempts per opportunity vs opportunities per game ---")
    all_opp = sum(rec.steal_opp.values())
    all_att = sum(rec.steal_att.values())
    all_sb = sum(rec.steal_sb.values())
    all_cs = sum(rec.steal_cs.values())
    print(
        f" opportunities/team-game = {all_opp / tg:.3f}   attempts/team-game = {all_att / tg:.4f}"
        f"   SB/tg = {all_sb / tg:.4f} (centre {_CENTRES_2025['SB']})   CS/tg = {all_cs / tg:.4f}"
        f" (centre {_CENTRES_2025['CS']})"
    )
    print(f" no-cell draws = {rec.steal_none}")
    print(
        f" {'base':>5} {'opp':>9} {'att':>7} {'att/opp':>9} {'poolATT':>9} {'poolRCY':>9} {'safe':>7} {'poolSafe':>9}"
    )
    for target in sorted(set(rec.steal_opp) | set(pool["steals"])):
        o = rec.steal_opp.get(target, 0)
        a = rec.steal_att.get(target, 0)
        sb = rec.steal_sb.get(target, 0)
        ps = pool["steals"].get(target, {})
        print(
            f" {target:>5} {o:>9d} {a:>7d} {a / o if o else 0.0:9.4f}"
            f" {ps.get('attempt_rate', 0.0):9.4f} {ps.get('attempt_rate_rcy', 0.0):9.4f}"
            f" {sb / a if a else 0.0:7.4f} {ps.get('safe_share', 0.0):9.4f}"
        )

    # --- (d) 3B per cell ------------------------------------------------------
    print("\n--- (d) 3B: drawn triple rate per cell vs the pool's own ---")
    print(
        f" 3B/team-game = {tri_total / tg:.4f}   centre {_CENTRES_2025['3B']}"
        f"   ({(tri_total / tg - _CENTRES_2025['3B']) / _CENTRES_2025['3B'] * 100.0:+.1f}%)"
    )
    sim_tri_rate = tri_total / bip_total if bip_total else 0.0
    unw_tri = pool["tri"].sum() / pool["n"].sum() if pool["n"].sum() else 0.0
    rcy_tri = pool["tri_rcy"].sum() / pool["n_rcy"].sum() if pool["n_rcy"].sum() else 0.0
    exp_tri = 0.0
    for rs in range(8):
        for o in range(3):
            if pool["n_rcy"][rs, o]:
                exp_tri += rec.bip[rs, o] * (pool["tri_rcy"][rs, o] / pool["n_rcy"][rs, o])
    exp_tri_rate = exp_tri / bip_total if bip_total else 0.0
    print(
        f" triple/BIP: sim = {sim_tri_rate:.5f}   pool unweighted = {unw_tri:.5f}"
        f"   pool recency = {rcy_tri:.5f}"
        f"   expected at sim cell mix = {exp_tri_rate:.5f}"
        f"   sim/expected = {sim_tri_rate / exp_tri_rate if exp_tri_rate else 0.0:.3f}"
    )
    print(
        " (d)(2) is closed by inspection: the stretch draw fires only on hit == 2 and"
        " nothing relabels an event to 'triple'."
    )

    return {
        "n_sims": n_sims,
        "bip": rec.bip.tolist(),
        "dp_row": rec.dp_row.tolist(),
        "dp_event": rec.dp_event.tolist(),
        "triple": rec.triple.tolist(),
        "dp_opportunities": rec.dp_opportunities,
        "no_transition": rec.no_transition,
        "steal": {
            "opp": rec.steal_opp,
            "att": rec.steal_att,
            "sb": rec.steal_sb,
            "cs": rec.steal_cs,
            "none": rec.steal_none,
        },
        "pool": {
            "n": pool["n"].tolist(),
            "n_rcy": pool["n_rcy"].tolist(),
            "dp": pool["dp"].tolist(),
            "dp_rcy": pool["dp_rcy"].tolist(),
            "tri": pool["tri"].tolist(),
            "tri_rcy": pool["tri_rcy"].tolist(),
            "steals": pool["steals"],
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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
    ap.add_argument("--iters", type=int, default=150)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)

    started = time.perf_counter()
    print(
        f"sim514_decomposition: {len(game_pks)} games x {args.iters} sims "
        f"= {len(game_pks) * args.iters} total sims"
    )

    rec = Recorder()
    pool_rates: dict[str, Any] | None = None
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if pool_rates is None:
                pool_rates = _pool_cell_rates(machine.full_pool_sampler)
            _instrument(machine, rec)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            print(
                f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s "
                f"(cum BIP {int(rec.bip.sum())}, steal opps {sum(rec.steal_opp.values())})"
            )
    finally:
        if duck is not None:
            duck.close()

    n_sims = len(game_pks) * args.iters
    assert pool_rates is not None
    payload = _print_report(rec, pool_rates, n_sims)
    print(f"\nelapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload["game_pks"] = game_pks
        payload["iters"] = args.iters
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
