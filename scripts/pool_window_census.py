"""
scripts/pool_window_census.py — measure the three pool-window options + the
pool-referenced frequency centres (the owner's 2026-08-20 questions).

WHAT THIS IS
============
The owner asked two linked questions:

  1. **The grade**: the sim's frequencies should be graded against the play
     pool's OWN totals (the data the sim draws from), not an external season.
  2. **The window**: for a 2026 game, which pool era? Three options:
       W1  full seasons 2023-2026
       W2  full seasons 2024-2026   (the current artifact export)
       W3  a rolling 3 years (game_date 2023-08-20 .. 2026-08-19)

This script measures, per window:

  * pitch pool volume + the per-count outcome shares -> the count-machine
    chain -> the POOL-REFERENCED per-PA centres (BB, K, HBP, pitches/PA);
  * batted-ball (outcome-pool) volume, the per-BIP event mix (1B/2B/3B/HR/
    ROE), the per-cell DP-row rate, and the THINNEST hard-filter cells
    (stand x runners_state x outs — the SIM-511 draw's 24-cell hard filter,
    which never widens and RAISES on an empty cell);
  * steal-opportunity volume, the thinnest (target, outs, balls, strikes)
    cells, attempt rate and safe share;
  * advancement-pool volume per decision.

It then GRADES the 2026-08-20 diagnosis run (12x150, the measured sim
frequencies) against each window's pool centres, so the owner reads the same
sim against all three references side by side.

USAGE
-----
    python scripts/pool_window_census.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from sim429_chain_analysis import solve_chain  # noqa: E402

from simulation.sim_kwargs import open_sim_duckdb  # noqa: E402

#: The three windows as SQL predicates (alias-free; every pool table carries
#: season + game_date).
WINDOWS = {
    "W1 2023-2026": "season BETWEEN 2023 AND 2026",
    "W2 2024-2026": "season BETWEEN 2024 AND 2026",
    "W3 rolling 3y": "game_date BETWEEN DATE '2023-08-20' AND DATE '2026-08-19'",
}

OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")

#: The 2026-08-20 diagnosis run's measured sim frequencies (12x150, the JSONs
#: in docs/audit/). Terminal-PA rates exclude IBB by construction.
SIM = {
    "BB/PA": 0.0850,
    "K/PA": 0.2160,
    "HBP/PA": 0.0113,
    "pitches/PA": 3.940,
    "3B/BIP": 0.00518,
    "DP/opportunity": 0.1417,
    "att/opp 2B": 0.0184,
    "att/opp 3B": 0.0042,
}


def main() -> None:
    con = open_sim_duckdb()
    if con is None:
        print("ERROR: cannot open the sim DuckDB read-only.")
        raise SystemExit(2)

    for name, pred in WINDOWS.items():
        print("\n" + "=" * 78)
        print(f" {name}   ({pred})")
        print("=" * 78)

        # --- pitch pool: volume + chain-implied per-PA centres -------------
        rows = con.execute(
            f"SELECT count_balls, count_strikes, outcome_type, COUNT(*) "
            f"FROM sim.pitch_pool WHERE {pred} GROUP BY 1, 2, 3"
        ).fetchall()
        mat = [[0.0] * 6 for _ in range(12)]
        n_pitch = 0
        for b, s, o, n in rows:
            if o in OUTCOMES:
                mat[int(b) * 3 + int(s)][OUTCOMES.index(o)] += float(n)
                n_pitch += int(n)
        chain = solve_chain(mat)
        print(f" pitch pool: {n_pitch:,} pitches")
        print(
            f"   pool-referenced centres: BB/PA {chain['walk']:.4f}   K/PA {chain['k']:.4f}"
            f"   HBP/PA {chain['hbp']:.4f}   pitches/PA {chain['pitches']:.3f}"
        )

        # --- batted-ball pool: volume, event mix, thin cells ---------------
        n_bip, n_1b, n_2b, n_3b, n_hr, n_roe = con.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN events = 'single' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN events = 'double' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN events = 'triple' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN events = 'home_run' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN events = 'field_error' THEN 1 ELSE 0 END) "
            f"FROM sim.outcome_pool WHERE {pred} AND dest_outs_consistent"
        ).fetchone()
        print(
            f" batted-ball pool (consistent): {n_bip:,} rows — per BIP: "
            f"1B {n_1b / n_bip:.4f}  2B {n_2b / n_bip:.4f}  3B {n_3b / n_bip:.5f}  "
            f"HR {n_hr / n_bip:.4f}  ROE {n_roe / n_bip:.5f}"
        )
        cells = con.execute(
            f"SELECT stand, runners_state, outs, COUNT(*) AS n "
            f"FROM sim.outcome_pool WHERE {pred} AND dest_outs_consistent "
            f"GROUP BY 1, 2, 3 ORDER BY n ASC LIMIT 5"
        ).fetchall()
        n_cells = con.execute(
            f"SELECT COUNT(*) FROM (SELECT stand, runners_state, outs "
            f"FROM sim.outcome_pool WHERE {pred} AND dest_outs_consistent "
            f"GROUP BY 1, 2, 3)"
        ).fetchone()[0]
        print(f"   hard-filter cells present: {n_cells}/48 (stand x rs x outs); the 5 thinnest:")
        for stand, rs, outs, n in cells:
            print(f"     stand={stand} rs={rs} outs={outs}: {n:,}")
        # The DP rate over runner-on-1B, <2-out cells (the (a) decomposition).
        dp_opp, dp_n = con.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN runner_1b_dest = 0 AND batter_dest = 0 "
            f"THEN 1 ELSE 0 END) FROM sim.outcome_pool WHERE {pred} "
            f"AND dest_outs_consistent AND (runners_state & 1) = 1 AND outs < 2"
        ).fetchone()
        print(f"   DP rows / runner-on-1B <2-out BIP: {dp_n:,}/{dp_opp:,} = {dp_n / dp_opp:.4f}")

        # --- steal pool ----------------------------------------------------
        for target in (2, 3):
            n_opp, n_att, n_sb = con.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN attempted THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN attempted AND success THEN 1 ELSE 0 END) "
                f"FROM sim.steal_opportunity_pool WHERE {pred} AND target_base = {target}"
            ).fetchone()
            thin = con.execute(
                f"SELECT outs, count_balls, count_strikes, COUNT(*) AS n "
                f"FROM sim.steal_opportunity_pool WHERE {pred} AND target_base = {target} "
                f"GROUP BY 1, 2, 3 ORDER BY n ASC LIMIT 3"
            ).fetchall()
            thin_s = ", ".join(f"o{o} {b}-{s}:{n:,}" for o, b, s, n in thin)
            print(
                f" steal pool [{target}B]: {n_opp:,} opportunities — att/opp "
                f"{n_att / n_opp:.4f}  safe {n_sb / max(1, n_att):.4f}   thinnest cells: {thin_s}"
            )

        # --- advancement pools ---------------------------------------------
        adv = con.execute(
            f"SELECT scenario, from_base, target_base, COUNT(*), "
            f"AVG(CASE WHEN attempted THEN 1.0 ELSE 0.0 END) "
            f"FROM sim.advancement_opportunity_pool WHERE {pred} "
            f"GROUP BY 1, 2, 3 ORDER BY 4 ASC"
        ).fetchall()
        adv_s = "  ".join(f"{s}_{f}_{t}:{n:,}({a:.2f})" for s, f, t, n, a in adv[:4])
        print(f" advancement pools (4 smallest, rows(att_rate)): {adv_s}")

        # --- the grade: the 2026-08-20 sim run vs THIS window's centres -----
        centres = {
            "BB/PA": chain["walk"],
            "K/PA": chain["k"],
            "HBP/PA": chain["hbp"],
            "pitches/PA": chain["pitches"],
            "3B/BIP": n_3b / n_bip,
            "DP/opportunity": dp_n / dp_opp,
        }
        print(" grade of the 2026-08-20 sim run vs this window's pool centres:")
        for k, c in centres.items():
            s = SIM[k]
            print(f"   {k:>16}: sim {s:.4f}  pool {c:.4f}  delta {(s - c) / c * 100.0:+.1f}%")

    con.close()


if __name__ == "__main__":
    main()
