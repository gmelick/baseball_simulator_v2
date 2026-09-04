"""
scripts/sim517_catcher_probe.py — the SIM-517 part-E receiving instrument.

WHAT THIS IS
============
Measures the sim's two receiving channels CONDITIONAL on the live catcher's
skill tier, against the pool's own per-tier rates:

  * **framing** — called strikes per TAKEN pitch (ball | called_strike),
    conditional on the live catcher's FRAMING tier (recency-weighted terciles
    of the pool rows' catchers on the shadow-zone strike-rate z);
  * **blocking** — got-away rows drawn per pitch, conditional on the live
    catcher's BLOCKING tier (terciles on the block-rate z).

The receiving kernel (``SIM_CATCHER_KERNEL_SIGMA``) is correct when a
poor-framing live catcher gets called strikes at the pool's own
poor-framer rate, and a poor blocker lets pitches get away at the pool's own
poor-blocker rate. The fit rule: the LARGEST sigma whose per-tier reads sit
inside the band floors (control-subtracted — the game set's catcher mix is
composition, not kernel).

Wrap-once seams (the SIM-514 stacking trap guarded): ``new_half_inning``
stages the live catcher's tiers; ``draw`` records the outcome + the drawn
row's got-away fact under those tiers.

USAGE
-----
    SIM_CATCHER_KERNEL_SIGMA=0.5 SIM_GOT_AWAY=1 \
        python scripts/sim517_catcher_probe.py --iters 200 \
        --json-out /app/scripts/sim517_catcher_s050.json
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

_TIERS = ("low", "mid", "high")
_TAKEN = ("ball", "called_strike")
#: Receiving z-matrix columns (see FullPoolSampler._catcher_receiving_data):
#: 0 = shadow_zone_strike_rate (framing), 2 = block rate (got-aways/pitch).
_FRAMING_COL = 0
_BLOCK_COL = 2


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


class Recorder:
    """Per-tier taken-pitch and got-away tallies."""

    def __init__(self) -> None:
        #: framing tier -> {taken, called}
        self.framing: dict[str, dict[str, int]] = {t: {"taken": 0, "called": 0} for t in _TIERS}
        #: blocking tier -> {pitches, got_away}
        self.blocking: dict[str, dict[str, int]] = {t: {"pitches": 0, "got": 0} for t in _TIERS}
        self.ctx_framing: str | None = None
        self.ctx_blocking: str | None = None
        self.games = 0
        self.got_away_total = 0


class Tiering:
    """Tercile edges over the POOL ROWS' catchers, per receiving column."""

    def __init__(self, sampler: Any) -> None:
        data = sampler._catcher_receiving_data()
        if data is None:
            raise RuntimeError("no receiving data in this bundle (pre-0022 or no embedding)")
        self.ki, self.z = data
        self.edges: dict[int, tuple[float, float]] = {}
        # Row-weighted (recency) tercile edges per column, pooled across hands.
        for col in (_FRAMING_COL, _BLOCK_COL):
            vals: list[np.ndarray] = []
            wts: list[np.ndarray] = []
            for hand in sampler.a.pools:
                idx = sampler._pp_catcher_recv_idx(hand)
                pool = sampler.a.pools[hand]
                if idx is None:
                    continue
                v = np.where(idx >= 0, self.z[np.clip(idx, 0, len(self.z) - 1), col], np.nan)
                m = np.isfinite(v)
                vals.append(v[m])
                wts.append(pool.recency.astype(np.float64)[m])
            v = np.concatenate(vals)
            w = np.concatenate(wts)
            order = np.argsort(v)
            cum = np.cumsum(w[order])
            lo = float(v[order][np.searchsorted(cum, cum[-1] / 3.0)])
            hi = float(v[order][np.searchsorted(cum, 2.0 * cum[-1] / 3.0)])
            self.edges[col] = (lo, hi)

    def tier(self, col: int, zval: float) -> str:
        lo, hi = self.edges[col]
        if zval < lo:
            return "low"
        if zval < hi:
            return "mid"
        return "high"

    def live_tiers(self, catcher_key: str | None) -> tuple[str | None, str | None]:
        if not catcher_key:
            return None, None
        i = self.ki.get(catcher_key, -1)
        if i < 0 or not np.isfinite(self.z[i]).all():
            return None, None
        return (
            self.tier(_FRAMING_COL, float(self.z[i, _FRAMING_COL])),
            self.tier(_BLOCK_COL, float(self.z[i, _BLOCK_COL])),
        )


def _pool_reference(sampler: Any, tiering: Tiering) -> dict[str, Any]:
    """The pool's own per-tier rates: called/taken by ROW-catcher framing
    tier; got-away/pitch by ROW-catcher blocking tier."""
    framing = {t: [0.0, 0.0] for t in _TIERS}  # [called, taken]
    blocking = {t: [0.0, 0.0] for t in _TIERS}  # [got, pitches]
    for hand in sampler.a.pools:
        pool = sampler.a.pools[hand]
        idx = sampler._pp_catcher_recv_idx(hand)
        if idx is None or pool.got_away is None:
            raise RuntimeError(f"the {hand} pool lacks receiving columns")
        rcy = pool.recency.astype(np.float64)
        out = np.asarray(pool.outcome_type, dtype=object)
        taken = np.isin(out, _TAKEN)
        called = out == "called_strike"
        got = np.asarray(pool.got_away, dtype=np.int8) > 0
        for col, agg, num, den in (
            (_FRAMING_COL, framing, called, taken),
            (_BLOCK_COL, blocking, got, np.ones(pool.n, dtype=bool)),
        ):
            zv = np.where(idx >= 0, tiering.z[np.clip(idx, 0, len(tiering.z) - 1), col], np.nan)
            fin = np.isfinite(zv)
            lo, hi = tiering.edges[col]
            for t, m in (
                ("low", fin & (zv < lo)),
                ("mid", fin & (zv >= lo) & (zv < hi)),
                ("high", fin & (zv >= hi)),
            ):
                agg[t][0] += float(rcy[m & num].sum())
                agg[t][1] += float(rcy[m & den].sum())
    return {
        "framing": {t: (v[0] / v[1] if v[1] else 0.0) for t, v in framing.items()},
        "blocking": {t: (v[0] / v[1] if v[1] else 0.0) for t, v in blocking.items()},
        "marginal_called_taken": None,  # filled by caller if wanted
    }


def _install(machine: Any, rec: Recorder, tiering: Tiering) -> None:
    fp = machine.full_pool_sampler
    if getattr(fp.draw, "_sim517_wrapped", False):
        return  # the cached sampler is already instrumented (the stacking trap)

    orig_half = fp.new_half_inning

    def new_half_inning(hand: str, pitcher_key: str, catcher_key: str | None = None) -> None:
        rec.ctx_framing, rec.ctx_blocking = tiering.live_tiers(catcher_key)
        return orig_half(hand, pitcher_key, catcher_key=catcher_key)

    fp.new_half_inning = new_half_inning

    orig_draw = fp.draw

    def draw(balls: int = 0, strikes: int = 0) -> str:
        out = orig_draw(balls, strikes)
        if rec.ctx_blocking is not None:
            b = rec.blocking[rec.ctx_blocking]
            b["pitches"] += 1
            if fp.last_pitch_got_away():
                b["got"] += 1
                rec.got_away_total += 1
        if rec.ctx_framing is not None and out in _TAKEN:
            f = rec.framing[rec.ctx_framing]
            f["taken"] += 1
            if out == "called_strike":
                f["called"] += 1
        return out

    draw._sim517_wrapped = True  # type: ignore[attr-defined]
    fp.draw = draw


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
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)
    sigma_f = float(os.environ.get("SIM_CATCHER_FRAMING_SIGMA", "0"))
    sigma_b = float(os.environ.get("SIM_CATCHER_BLOCK_SIGMA", "0"))

    started = time.perf_counter()
    print(
        f"sim517_catcher_probe: {len(game_pks)} games x {args.iters} sims  "
        f"framing_sigma={sigma_f} block_sigma={sigma_b}  "
        f"SIM_GOT_AWAY={os.environ.get('SIM_GOT_AWAY', '<unset>')}"
    )

    rec = Recorder()
    tiering: Tiering | None = None
    pool_ref: dict[str, Any] | None = None
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if tiering is None:
                tiering = Tiering(machine.full_pool_sampler)
                pool_ref = _pool_reference(machine.full_pool_sampler, tiering)
            _install(machine, rec, tiering)
            t0 = time.perf_counter()
            for seed in range(args.seed_base, args.seed_base + args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
                rec.games += 1
            print(f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s")
    finally:
        if duck is not None:
            duck.close()

    assert pool_ref is not None
    print("\n=== called strikes per TAKEN pitch, by live FRAMING tier ===")
    print(f" {'tier':>5} {'simTaken':>9} {'simRate':>9} {'poolRate':>9} {'delta':>9}")
    for t in _TIERS:
        f = rec.framing[t]
        rate = f["called"] / f["taken"] if f["taken"] else float("nan")
        pr = pool_ref["framing"][t]
        print(f" {t:>5} {f['taken']:>9d} {rate:9.5f} {pr:9.5f} {rate - pr:+9.5f}")
    print("\n=== got-away rows drawn per pitch, by live BLOCKING tier ===")
    print(f" {'tier':>5} {'simPitch':>9} {'simRate':>9} {'poolRate':>9} {'delta':>9}")
    for t in _TIERS:
        b = rec.blocking[t]
        rate = b["got"] / b["pitches"] if b["pitches"] else float("nan")
        pr = pool_ref["blocking"][t]
        print(f" {t:>5} {b['pitches']:>9d} {rate:9.5f} {pr:9.5f} {rate - pr:+9.5f}")
    tg = 2.0 * rec.games
    print(f"\n got-away drawn per team-game: {rec.got_away_total / tg:.4f}  (MLB ~0.33-0.35)")
    print(f" elapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload = {
            "iters": args.iters,
            "seed_base": args.seed_base,
            "game_pks": game_pks,
            "framing_sigma": sigma_f,
            "block_sigma": sigma_b,
            "framing": rec.framing,
            "blocking": rec.blocking,
            "pool": pool_ref,
            "got_away_total": rec.got_away_total,
            "games": rec.games,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
