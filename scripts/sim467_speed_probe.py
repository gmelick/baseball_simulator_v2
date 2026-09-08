"""
scripts/sim467_speed_probe.py — SIM-467: the whole-pool path vs the cell path, one warm process.

Builds the PRODUCTION machine once for one game (the harness's own wiring:
``sim_stats.py`` resolves the state, ``production_machine_factory`` builds the
sampler from the on-disk bundle under the compose env flags), then times N
iterations with the cell index OFF and N with it ON on the SAME sampler, so
the load, the per-worker caches and the host are identical between arms.
Reports seconds per iteration, the speed-up, the draw-weighted widening
share (``cell_index_stats``), and the box means (R/H/HR/BB/K per team-game)
of both arms as a first, low-power realism read. ``--profile`` adds a
cProfile of a few iterations per arm, restricted to the simulator's modules.

Run inside the app container (scripts/ is NOT bind-mounted):

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim467_speed_probe.py --iters 20 --profile 744795
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import pstats
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sim_stats import (  # noqa: E402
    _FACTORY,
    _game_summary,
    _resolve,
    open_sim_duckdb,
    sim_kwargs_from_state,  # noqa: E402
)

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

_BOX = ("R", "H", "HR", "BB", "K")


def _run(machine, kw, seeds, home_ids, away_ids) -> tuple[float, list[dict]]:
    # A clean matchup cache per arm, so the first PA never reuses the other
    # arm's weights.
    machine._fp_pitcher_key = None
    machine._fp_pa_key = None
    sums: list[dict] = []
    t0 = time.perf_counter()
    for seed in seeds:
        machine.boxscore = BoxScore()
        res = simulate_game(state_machine=machine, seed=seed, **kw)
        sums.append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
    return (time.perf_counter() - t0) / max(1, len(seeds)), sums


def _means(sums: list[dict]) -> dict[str, float]:
    return {k: statistics.mean(float(s[k]) for s in sums) for k in _BOX}


def _profile(machine, kw, seeds, home_ids, away_ids, label: str) -> None:
    pr = cProfile.Profile()
    pr.enable()
    _run(machine, kw, seeds, home_ids, away_ids)
    pr.disable()
    buf = io.StringIO()
    st = pstats.Stats(pr, stream=buf)
    st.sort_stats("tottime")
    st.print_stats("simulation/", 18)
    print(
        f"\n--- cProfile [{label}] top functions in simulation/ by own time "
        f"({len(seeds)} iterations) ---"
    )
    for line in buf.getvalue().splitlines():
        if "simulation/" in line or "function calls" in line or "tottime" in line:
            print(line.rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(description="SIM-467 speed probe: whole-pool vs cell path.")
    ap.add_argument("game_pk", type=int)
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--min-cell", type=int, default=20)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    duck = open_sim_duckdb()
    try:
        state = asyncio.run(_resolve(args.game_pk, duck))
    finally:
        if duck is not None:
            duck.close()
    home_ids = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
    away_ids = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
    if getattr(state, "home_pitcher_id", None):
        home_ids.add(int(state.home_pitcher_id))
    if getattr(state, "away_pitcher_id", None):
        away_ids.add(int(state.away_pitcher_id))
    kw = sim_kwargs_from_state(state)
    spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))

    t0 = time.perf_counter()
    machine = production_machine_factory(0, spec)
    fp = machine.full_pool_sampler
    print(
        f"machine built in {time.perf_counter() - t0:.1f}s; pools: "
        + ", ".join(f"{h}={p.n}" for h, p in fp.a.pools.items())
        + f"; bat_home column: {fp.a.pools['R'].bat_home is not None}",
        flush=True,
    )

    seeds = list(range(args.iters))
    # Warm-up (off): first-touch caches (pool meta, batter affinities).
    fp.pitch_cell_index = False
    _run(machine, kw, [1000, 1001], home_ids, away_ids)
    t_off, s_off = _run(machine, kw, seeds, home_ids, away_ids)
    print(f"OFF (whole pool): {t_off:.3f} s/iter over {args.iters}", flush=True)

    fp.pitch_cell_index = True
    fp.pitch_min_cell = args.min_cell
    t_idx = time.perf_counter()
    _run(machine, kw, [1000], home_ids, away_ids)  # builds the per-hand index
    print(f"index build + 1 warm game: {time.perf_counter() - t_idx:.2f}s", flush=True)
    fp.widen_counts[:] = 0
    t_on, s_on = _run(machine, kw, seeds, home_ids, away_ids)
    print(
        f"ON  (cell index, MIN_CELL={args.min_cell}): {t_on:.3f} s/iter over {args.iters}",
        flush=True,
    )
    print(f"speed-up: {t_off / t_on:.2f}x", flush=True)
    stats = fp.cell_index_stats()
    total = sum(stats["draws_by_level"]) or 1
    shares = [f"L{i}={n / total:.4%}" for i, n in enumerate(stats["draws_by_level"])]
    print(f"widening (share of draws): {'  '.join(shares)}  (n_side={stats['n_side']})")
    m_off, m_on = _means(s_off), _means(s_on)
    print("box means per team-game (low power — a direction, not a verdict):")
    for k in _BOX:
        print(f"  {k:>2}: off {m_off[k]:.3f}  on {m_on[k]:.3f}  ({m_on[k] - m_off[k]:+.3f})")

    if args.profile:
        prof_seeds = [2000, 2001, 2002]
        fp.pitch_cell_index = False
        _profile(machine, kw, prof_seeds, home_ids, away_ids, "OFF")
        fp.pitch_cell_index = True
        _profile(machine, kw, prof_seeds, home_ids, away_ids, "ON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
