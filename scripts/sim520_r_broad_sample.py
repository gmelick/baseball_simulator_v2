"""
scripts/sim520_r_broad_sample.py — the SIM-520 runs certification on a broad
game sample.

WHY
---
The 12-game acceptance lane reds R (−4.6%) because its game set's defenders
skew elite (~42% high-OAA-tier vs 33%) and the defense-aware fielder kernel
CORRECTLY scores fewer runs against them — the band's league-average centre
does not condition on defense quality. The owner ruled (2026-09-03): certify
R on a broader, defense-diverse sample instead of re-balancing the lane set.

WHAT THIS RUNS
--------------
A reproducible random sample of N distinct 2024 Final games (ordered by a
stable hash of game_pk, first N that RESOLVE), k iterations each, on the
PRODUCTION config (the docker-compose app env: full pool, manager, platoon,
home w=0, park 0.02, fielder 0.5). It reports:

  * R per team-game vs the band centre (bands.BANDS["R"]) with the SAME
    half-width arithmetic the lane uses (max(Z*sd/sqrt(n), floor));
  * the sample's live-defender OAA tier mix (the diversity check — the
    12-game lane read ~42% high-tier; a diverse sample should read ~33%);
  * the mean park run factor (the run-environment check).

USAGE
-----
    python scripts/sim520_r_broad_sample.py --games 60 --iters 100 \
        --json-out /app/scripts/sim520_r_broad.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
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
_POS_NUM_TO_NAME = {3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _candidate_games(limit: int) -> list[int]:
    """2024 Final games with ingested lineups, in a stable hash order (a
    reproducible shuffle that no one hand-picked)."""
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT g.game_pk FROM raw.games g
            JOIN raw.game_lineups l ON l.game_pk = g.game_pk
            WHERE g.season = 2024 AND g.status = 'Final'
            """
        )
    finally:
        await conn.close()
    pks = sorted(
        (int(r["game_pk"]) for r in rows),
        key=lambda pk: hashlib.sha256(f"sim520:{pk}".encode()).hexdigest(),
    )
    return pks[:limit]


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


class FielderTiers:
    """Per-position OAA tercile edges over the batted-ball pool rows (the
    sim476_fielder_probe machinery, inlined)."""

    def __init__(self, sampler: Any) -> None:
        z = sampler._emb_z("fielder")
        emb = sampler.a.actor_emb.get("fielder")
        cols = sampler._steal_feat_cols("fielder", sampler._FIELDER_BB_FEATURES)
        if z is None or emb is None or cols is None:
            raise RuntimeError("no fielder embedding in this bundle")
        self.z = z
        self.key_index = emb["key_index"]
        self.col = int(cols[0])
        self.edges: dict[int, tuple[float, float]] = {}
        per_pos: dict[int, list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
        for hand in sampler.a.bb_pools:
            pool = sampler.a.bb_pools[hand]
            row_emb = sampler._bb_fielder_emb_rows(hand)
            pos = getattr(pool, "fielder_pos", None)
            if row_emb is None or pos is None:
                raise RuntimeError(f"the {hand} pool has no fielder columns")
            oaa = np.where(
                row_emb >= 0, self.z[np.clip(row_emb, 0, len(self.z) - 1), self.col], np.nan
            )
            p = np.asarray(pos).astype(np.int64)
            for num in _POS_NUM_TO_NAME:
                m = (p == num) & np.isfinite(oaa)
                per_pos[num].append((oaa[m], pool.recency.astype(np.float64)[m]))
        for num, parts in per_pos.items():
            v = np.concatenate([a for a, _ in parts])
            w = np.concatenate([b for _, b in parts])
            order = np.argsort(v)
            cum = np.cumsum(w[order])
            lo = float(v[order][np.searchsorted(cum, cum[-1] / 3.0)])
            hi = float(v[order][np.searchsorted(cum, 2.0 * cum[-1] / 3.0)])
            self.edges[num] = (lo, hi)

    def count_tiers(self, defense: dict[str, int] | None, season: int) -> dict[str, int]:
        out = {"low": 0, "mid": 0, "high": 0, "unknown": 0}
        for num, name in _POS_NUM_TO_NAME.items():
            pid = (defense or {}).get(name)
            idx = self.key_index.get(f"{int(pid)}:{name}:{int(season)}", -1) if pid else -1
            if idx < 0:
                out["unknown"] += 1
                continue
            v = float(self.z[idx, self.col])
            lo, hi = self.edges[num]
            out["low" if v < lo else ("mid" if v < hi else "high")] += 1
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--games", type=int, default=60)
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()

    started = time.perf_counter()
    candidates = asyncio.run(_candidate_games(args.games * 2))
    print(
        f"sim520_r_broad_sample: target {args.games} games x {args.iters} iters "
        f"from {len(candidates)} candidates (production env)",
        flush=True,
    )

    duck = open_sim_duckdb()
    tiers: FielderTiers | None = None
    tier_mix = {"low": 0, "mid": 0, "high": 0, "unknown": 0}
    runs: list[int] = []  # per TEAM-game
    park_factors: list[float] = []
    used: list[int] = []
    try:
        for gp in candidates:
            if len(used) >= args.games:
                break
            try:
                state = asyncio.run(_resolve(gp, duck))
            except Exception as e:  # noqa: BLE001 — skip unresolvable games
                print(f"  game {gp}: skipped ({type(e).__name__})", flush=True)
                continue
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if tiers is None:
                tiers = FielderTiers(machine.full_pool_sampler)
            season = int(getattr(state, "season", 2024) or 2024)
            for side in ("home_defense", "away_defense"):
                for k, v in tiers.count_tiers(getattr(state, side, None), season).items():
                    tier_mix[k] += v
            park_factors.append(float(getattr(state, "park_run_factor", 1.0) or 1.0))
            t0 = time.perf_counter()
            for seed in range(args.seed_base, args.seed_base + args.iters):
                machine.boxscore = BoxScore()
                res = simulate_game(state_machine=machine, seed=seed, **kw)
                runs.append(int(res.home_score))
                runs.append(int(res.away_score))
            used.append(gp)
            print(
                f"  game {gp} ({len(used)}/{args.games}): {args.iters} sims in "
                f"{time.perf_counter() - t0:.1f}s",
                flush=True,
            )
    finally:
        if duck is not None:
            duck.close()

    r = np.asarray(runs, dtype=np.float64)
    n = len(r)
    mean = float(r.mean())
    sd = float(r.std(ddof=1))
    se = sd / np.sqrt(n)

    from tests.acceptance import bands  # noqa: E402 — the lane's own arithmetic

    band = bands.REFERENCES["R"]
    centre = float(band.centre)
    floor = float(band.floor())
    z_gate = float(bands.Z)
    half = max(z_gate * se, floor)
    verdict = "PASS" if abs(mean - centre) <= half else "FAIL"

    known = tier_mix["low"] + tier_mix["mid"] + tier_mix["high"]
    print(f"\n=== SIM-520: R on the broad sample ({len(used)} games x {args.iters}) ===")
    print(
        f" R/team-game: {mean:.4f} vs centre {centre:.4f} "
        f"(delta {(mean - centre) / centre * 100:+.1f}%)"
    )
    print(f" half-width max(Z*se, floor) = max({z_gate * se:.4f}, {floor:.4f}) -> {verdict}")
    print(f" n = {n} team-games (needs ~{2 * 5648 // 2} for the floor to bind)")
    print(
        " defense tier mix: "
        + "  ".join(f"{t} {tier_mix[t] / known:.3f}" for t in ("low", "mid", "high"))
        + f"  (unknown {tier_mix['unknown']}; the 12-game lane read ~0.42 high)"
    )
    print(f" mean park run factor: {float(np.mean(park_factors)):.4f}")
    print(f" elapsed: {(time.perf_counter() - started) / 3600:.2f}h")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "games": used,
                    "iters": args.iters,
                    "seed_base": args.seed_base,
                    "r_mean": mean,
                    "r_sd": sd,
                    "n_team_games": n,
                    "centre": centre,
                    "floor": floor,
                    "verdict": verdict,
                    "tier_mix": tier_mix,
                    "park_factor_mean": float(np.mean(park_factors)),
                },
                indent=2,
            )
        )
        print(f" wrote {args.json_out}")


if __name__ == "__main__":
    main()
