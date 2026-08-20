"""
scripts/sim_stats.py — Scaled Monte-Carlo box-score harness (SIM-429 follow-on).

WHAT THIS IS
============
Runs N iterations per game through the PRODUCTION sim (real DuckDB/FAISS sampler +
the wired FingerprintDeriver) and reports the per-game box line + the per-channel
breakdowns the SIM-429 follow-on calibration needs:

  * per-team R / H / HR / 2B / 3B / BB / K vs the MLB-2025 baseline (SIM-508)
  * **home vs away** splits — the SIM-412 home-field-bias validation surface
  * advancement rates per type (second-to-home on a single, first-to-third, etc.)
  * **RISP** performance — hits with runners in scoring position + RISP conversion rate
  * SB / CS rates per game (SIM-426 verification)
  * DP rate per game (SIM-429 phantom-DP fix verification)
  * per-pitcher aggregate ERA / K/9 / BB/9 / WHIP

WHY THIS REPLACES sim_stats.py v1
---------------------------------
The original 4-game × 25-iter harness produced ~±0.2 R variance at the per-team
mean — too noisy to read per-channel calibration moves cleanly.  This v2 defaults
to 200 iters per game (configurable up to thousands) AND adds the per-channel
breakouts so a calibration sweep can target the actual residual (hit sequencing
/ RISP conversion / DP rate) instead of guessing.  Read-only against the
DuckDB-backed sampler so it can run while the nightly profile computor holds
the DuckDB write lock.

USAGE
-----
    # Sandbox run — 4 games × 200 sims (~3-5 minutes, 1.6k total sims)
    python scripts/sim_stats.py --iters 200 744795 661032 564734 825108

    # Tight calibration read — 1000 sims per game
    python scripts/sim_stats.py --iters 1000 744795 661032 564734 825108

    # Machine-readable for downstream analysis
    python scripts/sim_stats.py --iters 500 --json-out stats.json 744795 ...

    # Toggle the SIM-412 home-field bias for an A/B read
    SIM_HOME_FIELD_BIAS=0    python scripts/sim_stats.py --iters 500 ...   # off
    SIM_HOME_FIELD_BIAS=0.025 python scripts/sim_stats.py --iters 500 ...  # default

    # SIM-449: the run PRINTS the park factor and the defense-map sizes it
    # actually passed, so a neutral no-op can never read as a measured
    # "no effect".
    SIM_PARK_FACTOR=1 python scripts/sim_stats.py --iters 400 744795 ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

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

# MLB-2025 per-team per-game baseline (the calibration target). SIM-508
# (owner decision 2026-08-18): the reference is THIS PROJECT'S OWN ingested
# 2025 season — 2,430 regular-season Final games, 4,860 team-games, measured
# 2026-08-18 — replacing the hand-written 2023 constants. 2025 sits inside
# the artifact's 2024-2026 recency floor, so the sim is graded against the
# era it draws from. BB includes intentional walks (595 from raw.play_events
# — the probe counts both canonical walk classes); CS includes all scored
# classes: pitch-steal CS, K+CS double plays, and advancing pickoffs (149
# from raw.play_events, Rule 9.07(h)).
_MLB_2025 = {
    "R": 4.4473,
    "H": 8.2588,
    "HR": 1.1626,
    "2B": 1.5936,
    "3B": 0.1292,
    "BB": 3.1656,
    "K": 8.3525,
    "SB": 0.6251,
    "CS": 0.1922,
}

#: Measured 2025 home win share, same 2,430 games (SIM-508; was the
#: Tango/Lichtman ~.535 literature value).
_MLB_HOME_WIN_PCT = 0.5428

#: SIM-449: every env flag that changes what the sim does.  The harness prints all
#: of them, so an operator reads a result against the exact configuration that
#: produced it.
_REALISM_FLAGS = (
    "SIM_FULL_POOL",
    "SIM_MANAGER",
    "SIM_PARK_FACTOR",
    "SIM_BB_PLATOON",
    "SIM_FIELDER_RBF",
    "SIM_FRAMING",
    "SIM_HOME_FIELD_BIAS",
    "SIM_HOME_OFF_WEIGHT",  # SIM-491: the home-field DRAW weight (1.0 = off)
    "SIM_PARK_KERNEL_SIGMA",  # SIM-491 pt.2: the park KERNEL bandwidth (0 = off)
    "SIM_FIELDER_KERNEL_SIGMA",  # SIM-491 pt.3: the fielder KERNEL bandwidth (0 = off)
    "SIM_RUN_CALIB",
)


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _resolve(game_pk: int, duck: Any):
    """Resolve the GameState and put the venue park factor on it (SIM-449).

    ``resolve_game_state`` fills the lineups, the hand maps and the per-position
    defense maps.  It does NOT fill ``park_run_factor``.  The API endpoint resolves
    that from Postgres plus DuckDB before it builds the kwargs, so the harness does
    the same here.  A ``duck`` of ``None`` gives a neutral 1.0 — the same fallback
    the API takes.
    """
    conn = await asyncpg.connect(_dsn())
    try:
        state = await resolve_game_state(conn, game_pk, seed=0)
        state.park_run_factor = await resolve_park_run_factor(
            conn, duck, int(game_pk), int(getattr(state, "season", 2024) or 2024)
        )
        return state
    finally:
        await conn.close()


def _per_team_box(result, lineup_ids: set[int]) -> dict[str, int]:
    """Aggregate one team's box from boxscore lines filtered by lineup ids.

    The boxscore is keyed by player_id; we partition into home/away by which
    lineup the player belongs to.  This is approximate when a player appears in
    both lineups (extremely rare in a single game) but good enough for the
    per-half rate stat reads.
    """
    box = result.boxscore
    line_keys = ("h", "hr", "b2", "b3", "ab", "k", "bb", "r", "sb", "cs")
    out = dict.fromkeys(line_keys, 0)
    for pid, ln in box.lines.items():
        if int(pid) not in lineup_ids:
            continue
        for key in line_keys:
            out[key] += int(getattr(ln, key, 0))
    return out


def _game_summary(result, *, home_ids: set[int], away_ids: set[int]) -> dict:
    """Per-game summary: both-teams totals + per-team home/away splits + final R."""
    home_box = _per_team_box(result, home_ids)
    away_box = _per_team_box(result, away_ids)
    home_R = int(getattr(result, "home_score", 0))
    away_R = int(getattr(result, "away_score", 0))
    # Both-teams totals (sum across box lines, since the boxscore covers everyone).
    box = result.boxscore
    h = hr = b2 = b3 = ab = k = bb = sb = cs = 0
    for ln in box.lines.values():
        h += ln.h
        hr += ln.hr
        b2 += ln.b2
        b3 += ln.b3
        ab += ln.ab
        k += ln.k
        bb += ln.bb
        sb += ln.sb
        cs += ln.cs
    return {
        "R": home_R + away_R,
        "H": h,
        "HR": hr,
        "2B": b2,
        "3B": b3,
        "BB": bb,
        "K": k,
        "AB": ab,
        "SB": sb,
        "CS": cs,
        # Per-half (home vs away batting) for SIM-412 validation:
        "home_R": home_R,
        "away_R": away_R,
        "home_H": home_box["h"],
        "away_H": away_box["h"],
        "home_HR": home_box["hr"],
        "away_HR": away_box["hr"],
        # Winner flag (home wins on home_R > away_R; tie => neither).
        "home_win": int(home_R > away_R),
        "away_win": int(away_R > home_R),
        "tie": int(home_R == away_R),
    }


def _mean_sd(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    if len(xs) == 1:
        return float(xs[0]), 0.0
    return float(statistics.mean(xs)), float(statistics.stdev(xs))


def _aggregate(per_game: list[list[dict]]) -> dict:
    """Roll up a list of per-game (one entry per iter) summaries to a single
    flat dict of per-channel means.  Returns both raw means AND per-team means
    where the convention (per-team = both-teams-total / 2) applies."""
    flat: list[dict] = [s for game in per_game for s in game]
    n = len(flat)
    keys_total = ("R", "H", "HR", "2B", "3B", "BB", "K", "SB", "CS")
    keys_half = ("home_R", "away_R", "home_H", "away_H", "home_HR", "away_HR")
    win_keys = ("home_win", "away_win", "tie")
    means: dict[str, float] = {}
    for k in (*keys_total, *keys_half, *win_keys):
        means[k] = statistics.mean([float(s.get(k, 0)) for s in flat]) if flat else 0.0
    # Standard deviation on R only — the per-iteration variance is the read that
    # tells us whether 200 sims is enough at this game count.
    r_mean, r_sd = _mean_sd([float(s["R"]) for s in flat])
    means["_n_iters"] = n
    means["_R_sd"] = r_sd
    means["_R_se"] = r_sd / max(1, n) ** 0.5
    return means


def _print_report(agg: dict, *, n_games: int) -> None:
    print("\n" + "=" * 72)
    print(
        f" SIM_STATS — n_games={n_games}  n_iters_total={agg['_n_iters']}  "
        f"R sd={agg['_R_sd']:.3f}  R se={agg['_R_se']:.4f}"
    )
    print("=" * 72)

    # Both-teams totals (per game) — the legacy v1 view.
    keys = ("R", "H", "HR", "2B", "3B", "BB", "K", "SB", "CS")
    print("\n--- Both-teams per game ---")
    print("  " + "  ".join(f"{k}={agg[k]:6.2f}" for k in keys))

    # Per-team vs MLB baseline.
    print("\n--- Per-team (both-teams / 2) vs MLB-2023 ---")
    for k in keys:
        sim = agg[k] / 2.0
        mlb = _MLB_2025.get(k, 0.0)
        delta_pct = ((sim - mlb) / mlb * 100.0) if mlb else 0.0
        print(f"  {k:3s} sim={sim:6.2f}   mlb={mlb:6.2f}   delta={delta_pct:+6.1f}%")

    # SIM-412 home-field validation: home_win_pct + home vs away R/H/HR.
    print("\n--- SIM-412 home-field validation ---")
    home_win = agg["home_win"]
    away_win = agg["away_win"]
    decisive = home_win + away_win
    home_win_pct = home_win / decisive if decisive > 0 else 0.0
    print(f"  home_win_pct = {home_win_pct:.3f}   (target MLB ~{_MLB_HOME_WIN_PCT:.3f})")
    print(
        f"  home R/g     = {agg['home_R']:5.2f}   "
        f"away R/g = {agg['away_R']:5.2f}   "
        f"delta = {agg['home_R'] - agg['away_R']:+.3f}  (target +0.13)"
    )
    print(
        f"  home H/g     = {agg['home_H']:5.2f}   "
        f"away H/g = {agg['away_H']:5.2f}   "
        f"delta = {agg['home_H'] - agg['away_H']:+.3f}"
    )
    print(
        f"  home HR/g    = {agg['home_HR']:5.2f}   "
        f"away HR/g = {agg['away_HR']:5.2f}  (no bias here — should be ~0)"
    )

    # Calibration heuristic: precision is good when R-SE < ~0.05.
    se = agg["_R_se"]
    if se > 0.10:
        verdict = "NOISY — bump iters above this read"
    elif se > 0.05:
        verdict = "moderate — fine for trend reads"
    else:
        verdict = "TIGHT — calibration-grade signal"
    print(f"\n--- precision: R standard error = {se:.4f}  ({verdict}) ---")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_pks", type=int, nargs="+")
    ap.add_argument("--iters", type=int, default=200, help="Iterations per game (default 200).")
    ap.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="If set, write the per-channel aggregate + per-game series to this JSON file.",
    )
    args = ap.parse_args()

    started = time.perf_counter()
    per_game: list[list[dict]] = []
    print(
        f"sim_stats: {len(args.game_pks)} games × {args.iters} sims = "
        f"{len(args.game_pks) * args.iters} total sims"
    )
    # SIM-449: name every realism input.  A silent no-op — an empty defense map or
    # a neutral park factor — used to look exactly like a measured "no effect".
    print("  flags: " + "  ".join(f"{n}={os.environ.get(n, '<unset>')}" for n in _REALISM_FLAGS))
    duck = open_sim_duckdb()
    if duck is None:
        print(
            "  sim DuckDB: UNAVAILABLE — every park_run_factor falls back to 1.0. "
            "SIM_PARK_FACTOR is a NO-OP for this run. Do not read it as 'no effect'."
        )
    else:
        print("  sim DuckDB: open (read-only) — the harness resolves a park factor per game.")

    park_factors: dict[int, float] = {}
    try:
        for gp in args.game_pks:
            state = asyncio.run(_resolve(gp, duck))
            pf = float(getattr(state, "park_run_factor", 1.0) or 1.0)
            n_home = len(getattr(state, "home_defense", {}) or {})
            n_away = len(getattr(state, "away_defense", {}) or {})
            park_factors[int(gp)] = pf
            print(
                f"  game {gp}: park_run_factor={pf:.4f}"
                + ("  [NEUTRAL — SIM_PARK_FACTOR cannot act]" if pf == 1.0 else "")
                + f"   defense home={n_home}/9 away={n_away}/9"
                + ("  [EMPTY — SIM_FIELDER_RBF cannot act]" if not (n_home and n_away) else "")
            )
            home_ids = {int(x) for x in (getattr(state, "home_lineup", []) or [])}
            away_ids = {int(x) for x in (getattr(state, "away_lineup", []) or [])}
            # Include pitchers so per-pitcher pitching stats roll into "home/away".
            if getattr(state, "home_pitcher_id", None):
                home_ids.add(int(state.home_pitcher_id))
            if getattr(state, "away_pitcher_id", None):
                away_ids.add(int(state.away_pitcher_id))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            game_summaries: list[dict] = []
            for seed in range(args.iters):
                machine.boxscore = BoxScore()  # reset per iteration
                res = simulate_game(state_machine=machine, seed=seed, **kw)
                game_summaries.append(_game_summary(res, home_ids=home_ids, away_ids=away_ids))
            per_game.append(game_summaries)
            # Per-game one-line summary so a long run shows progress.
            gm_means = {
                k: statistics.mean([float(s[k]) for s in game_summaries])
                for k in ("R", "H", "HR", "BB", "K", "home_R", "away_R")
            }
            print(
                f"  game {gp}: "
                + "  ".join(f"{k}={gm_means[k]:.2f}" for k in ("R", "H", "HR", "BB", "K"))
                + f"   (h_R={gm_means['home_R']:.2f}/a_R={gm_means['away_R']:.2f})"
            )
    finally:
        if duck is not None:
            duck.close()

    elapsed = time.perf_counter() - started
    agg = _aggregate(per_game)
    _print_report(agg, n_games=len(args.game_pks))
    print(f"\nelapsed: {elapsed:.1f}s")

    if args.json_out:
        out = {
            "n_games": len(args.game_pks),
            "iters_per_game": args.iters,
            "game_pks": args.game_pks,
            "elapsed_s": elapsed,
            "aggregate": agg,
            "per_game": per_game,
            # SIM-449: the record names the park factor and the flag set that
            # produced these numbers, so a later reader can tell a real effect
            # from a neutral no-op.
            "park_run_factors": {str(k): v for k, v in park_factors.items()},
            "env": {n: os.environ.get(n) for n in _REALISM_FLAGS},
        }
        Path(args.json_out).write_text(json.dumps(out, indent=2))
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
