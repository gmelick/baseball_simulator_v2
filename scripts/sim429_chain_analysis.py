"""
scripts/sim429_chain_analysis.py — the Markov-chain read over the SIM-429 diagnosis JSON.

WHAT THIS IS
============
The count machine is a Markov chain over the 12 counts: each count has one
outcome distribution, and a PA walks when the chain absorbs at ball four. This
script solves that chain for each per-count rate matrix the diagnosis recorded
(sim raw / sim final / pool unweighted / pool recency / MLB-2025) and reports
the implied per-PA walk rate, strikeout rate and pitches-per-PA.

WHY THIS MATTERS
================
The sim IS this chain (the draw conditions on matchup + count only). Real PAs
are not: pitches within a real PA correlate beyond the count (the same pitcher's
command, the same batter's approach). So:

  * chain(MLB rates) vs OBSERVED MLB per-PA rates measures the conditional-
    independence approximation itself on real data. A gap here is STRUCTURAL —
    no per-count rate fix can close it.
  * chain(sim rates) vs OBSERVED sim per-PA rates is a consistency check
    (they should match; the sim is the chain).
  * chain(pool recency) is what a perfectly-neutral pool draw would produce.

USAGE
-----
    python scripts/sim429_chain_analysis.py /app/scripts/sim429_diag_150.json
"""

from __future__ import annotations

import json
import sys

OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")


def solve_chain(rates: list[list[float]]) -> dict[str, float]:
    """Absorption probabilities + expected pitches from (0,0).

    ``rates[b*3+s]`` is the outcome-share row for count (b, s). A foul at two
    strikes self-loops; the closed form divides the state's other terms by
    (1 - p_foul).
    """
    p: dict[tuple[int, int], dict[str, float]] = {}

    def shares(b: int, s: int) -> dict[str, float]:
        row = rates[b * 3 + s]
        tot = sum(row[:6]) or 1.0
        return {o: row[i] / tot for i, o in enumerate(OUTCOMES)}

    def state(b: int, s: int) -> dict[str, float]:
        if (b, s) in p:
            return p[(b, s)]
        sh = shares(b, s)
        acc = {"walk": 0.0, "k": 0.0, "in_play": 0.0, "hbp": 0.0, "pitches": 1.0}

        def add(dest: dict[str, float], w: float) -> None:
            for key in ("walk", "k", "in_play", "hbp", "pitches"):
                acc[key] += w * dest[key]

        if b == 3:
            acc["walk"] += sh["ball"]
        else:
            add(state(b + 1, s), sh["ball"])
        strike = sh["called_strike"] + sh["swinging_strike"]
        if s == 2:
            acc["k"] += strike
        else:
            add(state(b, s + 1), strike)
        if s < 2:
            add(state(b, s + 1), sh["foul"])
            denom = 1.0
        else:
            denom = 1.0 - sh["foul"]  # the two-strike foul self-loop
        acc["in_play"] += sh["in_play"]
        acc["hbp"] += sh["hit_by_pitch"]
        out = {k: v / denom for k, v in acc.items()}
        p[(b, s)] = out
        return out

    return state(0, 0)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/app/scripts/sim429_diag_150.json"
    d = json.loads(open(path).read())

    matrices = {
        "sim RAW": d["sim_raw"],
        "sim FINAL": d["sim_final"],
        "pool UNW": d["pool_unweighted"],
        "pool RCY": d["pool_recency"],
        "MLB 2025": d["mlb_rates"],
    }
    pa = d["sim_pa"]
    obs_sim = {
        "walk": pa["walks"] / pa["pa_total"],
        "k": pa["strikeouts"] / pa["pa_total"],
        "pitches": pa["pitches"] / pa["pa_total"],
    }

    print(
        f"{'matrix':>10} {'P(walk)':>9} {'P(K)':>9} {'P(inplay)':>10} {'P(hbp)':>8} {'pitches/PA':>11}"
    )
    for name, m in matrices.items():
        r = solve_chain(m)
        print(
            f"{name:>10} {r['walk']:9.4f} {r['k']:9.4f} {r['in_play']:10.4f}"
            f" {r['hbp']:8.4f} {r['pitches']:11.3f}"
        )
    print(
        f"{'OBS sim':>10} {obs_sim['walk']:9.4f} {obs_sim['k']:9.4f} {'':>10} {'':>8} {obs_sim['pitches']:11.3f}"
    )
    # Observed MLB per-PA rates come from the walk-per-path table's 0-0 row
    # (every PA visits 0-0) + the totals the diagnosis printed.
    mlb00 = d["mlb_path"]["0"]
    mlb_pas = mlb00["pas_through"]
    print(
        f"{'OBS MLB':>10} {mlb00['walks'] / mlb_pas:9.4f}"
        f" {mlb00['strikeouts'] / mlb_pas:9.4f} {'':>10} {'':>8}"
        f" {sum(sum(r) for r in d['mlb_rates']) / mlb_pas:11.3f}"
    )


if __name__ == "__main__":
    main()
