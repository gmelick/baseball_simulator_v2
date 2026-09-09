"""

ARCHIVED 2026-09-09: this probe measured the SIM-517 bell-curve receiving kernel,
which SIM-523 part E deleted (its method and sigmas no longer exist on the
sampler). Kept as the record of that measurement; it will not run as is.
scripts/sim523_factor_strength.py — how much does each similarity factor concentrate the pitch draw?

For every plate appearance of N real games (production config, index off) the
per-PA weight is a product: pitcher similarity x recency x batter similarity x
situation similarity x catcher receiving similarity. This probe evaluates each
factor alone over the whole pool and reports its EFFECTIVE SAMPLE SIZE share,
(sum w)^2 / (sum w^2) / N — 100% means the factor is flat (every row equal),
1% means it concentrates the draw on one row in a hundred — plus the share of
the factor's mass on the top 1% of rows. It then does the same for the product
with and without the catcher factor.

Part 2 (real pool): is a catcher-season's got-away rate a staff-wildness proxy?
Correlate it with the same catcher-season's walk and hit-by-pitch rates per
plate appearance, and show the distribution's skew (mean vs median).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_factor_strength.py 744795 661032 564734
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
from sim_stats import _FACTORY, _resolve, open_sim_duckdb, sim_kwargs_from_state  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import BoxScore, simulate_game  # noqa: E402

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")


def _ess_share(w):
    w = np.asarray(w, np.float64)
    s = w.sum()
    return (s * s / (w @ w)) / w.size if s > 0 else 0.0


def _top1_share(w):
    w = np.asarray(w, np.float64)
    k = max(1, w.size // 100)
    top = np.partition(w, -k)[-k:]
    return top.sum() / w.sum() if w.sum() > 0 else 0.0


def part1(game_pks, iters):
    print(
        "=== PART 1 — how much each factor concentrates the pitch draw (whole pool, production config) ==="
    )
    duck = open_sim_duckdb()
    try:
        states = [asyncio.run(_resolve(gp, duck)) for gp in game_pks]
    finally:
        if duck is not None:
            duck.close()
    acc = defaultdict(list)
    for state in states:
        kw = sim_kwargs_from_state(state)
        machine = production_machine_factory(
            0, GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
        )
        fp = machine.full_pool_sampler
        fp.pitch_cell_index = False
        orig = fp.new_plate_appearance

        def npa(batter_key, base_out, *, fp=fp, orig=orig, **k):
            orig(batter_key, base_out, **k)
            hand = fp._hand
            pool = fp.a.pools[hand]
            f_p = fp._base / pool.recency  # f_pitcher alone
            f_b = fp._f_batter(hand, batter_key)
            f_s = fp._f_situation_baseout(hand, base_out)
            f_r = fp._f_catcher_receiving(hand, fp._catcher_key) if fp._catcher_key else None
            same = f_p > 0
            for name, f in (
                ("pitcher", f_p),
                ("recency", pool.recency),
                ("batter", f_b),
                ("situation", f_s),
            ):
                acc[name + "_ess"].append(_ess_share(f))
                acc[name + "_top1"].append(_top1_share(f))
            if f_r is not None:
                acc["catcher_ess"].append(_ess_share(f_r))
                acc["catcher_top1"].append(_top1_share(f_r))
                acc["catcher_zero_share"].append(float((f_r <= 1e-6).mean()))
            base = f_p * pool.recency * f_b * f_s
            acc["product_no_catcher_ess"].append(_ess_share(base))
            if f_r is not None:
                acc["product_with_catcher_ess"].append(_ess_share(base * f_r))
            acc["same_hand_rows"].append(float(same.mean()))

        fp.new_plate_appearance = npa
        machine._fp_pitcher_key = None
        machine._fp_pa_key = None
        for seed in range(iters):
            machine.boxscore = BoxScore()
            simulate_game(state_machine=machine, seed=seed, **kw)
        fp.new_plate_appearance = orig
    n = len(acc["pitcher_ess"])
    print(
        f"  plate appearances measured: {n}   (same-hand pitcher rows in the pool: {np.mean(acc['same_hand_rows']):.1%})"
    )
    print(
        f"  {'factor':>22} {'effective sample (share of pool)':>34} {'mass on the top 1% of rows':>28}"
    )
    for name in ("pitcher", "recency", "batter", "situation", "catcher"):
        if name + "_ess" in acc:
            print(
                f"  {name:>22} {np.mean(acc[name + '_ess']):33.2%} {np.mean(acc[name + '_top1']):27.1%}"
            )
    print(f"  {'catcher: rows at ~0':>22} {np.mean(acc['catcher_zero_share']):33.1%}")
    print(f"  {'product, no catcher':>22} {np.mean(acc['product_no_catcher_ess']):33.2%}")
    print(f"  {'product, with catcher':>22} {np.mean(acc['product_with_catcher_ess']):33.2%}")


def part2():
    print(
        "\n=== PART 2 — is the catcher's got-away rate a staff-wildness proxy? (real pool, catcher-seasons) ==="
    )
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute("""
            WITH pitches AS (
                SELECT game_pk, at_bat_number, pitch_number, season, catcher_id, pitcher_id, events,
                       COALESCE(got_away, FALSE) AS ga, outcome_type
                FROM sim.pitch_pool
                WHERE season IN (2023, 2024, 2025, 2026) AND catcher_id IS NOT NULL AND catcher_id > 0
            ),
            last AS (SELECT game_pk, at_bat_number, MAX(pitch_number) AS pn FROM pitches GROUP BY 1, 2),
            cs AS (
                SELECT p.season, p.catcher_id,
                       COUNT(*) AS pitches,
                       AVG(CASE WHEN p.ga THEN 1.0 ELSE 0.0 END) AS got_away_rate,
                       COUNT(DISTINCT p.pitcher_id) AS pitchers,
                       COUNT(*) FILTER (WHERE l.pn = p.pitch_number) AS pa,
                       COUNT(*) FILTER (WHERE l.pn = p.pitch_number AND p.events = 'walk') AS bb,
                       COUNT(*) FILTER (WHERE l.pn = p.pitch_number AND p.events = 'hit_by_pitch') AS hbp,
                       COUNT(*) FILTER (WHERE p.outcome_type IN ('ball','called_strike')) AS taken,
                       COUNT(*) FILTER (WHERE p.outcome_type = 'called_strike') AS called
                FROM pitches p LEFT JOIN last l USING (game_pk, at_bat_number)
                GROUP BY 1, 2 HAVING COUNT(*) >= 2000
            )
            SELECT got_away_rate, bb * 1.0 / pa AS bb_pa, hbp * 1.0 / pa AS hbp_pa,
                   called * 1.0 / taken AS cs_taken, pitches
            FROM cs
        """).fetchnumpy()
    finally:
        con.close()
    ga = np.asarray(rows["got_away_rate"], float)
    bb = np.asarray(rows["bb_pa"], float)
    hbp = np.asarray(rows["hbp_pa"], float)
    cs = np.asarray(rows["cs_taken"], float)
    print(f"  catcher-seasons with >= 2,000 pitches: {ga.size}")
    print(
        f"  got-away rate: mean {ga.mean():.4%}  median {np.median(ga):.4%}  (skew = mean above median: {'yes' if ga.mean() > np.median(ga) else 'no'})"
    )
    print(
        f"  correlation of the catcher-season got-away rate with: walks/PA {np.corrcoef(ga, bb)[0, 1]:+.3f}   "
        f"hit-by-pitch/PA {np.corrcoef(ga, hbp)[0, 1]:+.3f}   called-strike share {np.corrcoef(ga, cs)[0, 1]:+.3f}"
    )
    lo, hi = np.percentile(ga, [25, 75])
    tame, wild = ga <= lo, ga >= hi
    print(
        f"  tamest quarter of catcher-seasons (got-away <= {lo:.3%}): HBP/PA {hbp[tame].mean():.4%}  BB/PA {bb[tame].mean():.4%}"
    )
    print(
        f"  wildest quarter                (got-away >= {hi:.3%}): HBP/PA {hbp[wild].mean():.4%}  BB/PA {bb[wild].mean():.4%}"
    )
    print(
        "  Read: a wild pitch is the PITCHER'S event but lands in the catcher's got-away rate; if the rate"
    )
    print(
        "  tracks walks and hit-by-pitches, 'a catcher like this one' means 'a staff like this one'."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=3)
    args = ap.parse_args()
    part1(args.game_pks, args.iters)
    part2()
    return 0


if __name__ == "__main__":
    sys.exit(main())
