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

THE LABEL CHECK (SIM-553, 2026-09-23)
=====================================
Per window, the census also compares the pool's chain with REAL plate
appearances counted from ``raw.pitches`` (Postgres): strikeouts, walks, hit by
pitch and pitches per plate appearance, with the relative gap and a PASS or
FAIL at 0.5% (``pipeline.batch.pool_chain.LABEL_CHECK_TOLERANCE``). The pool
centres come from the pool's own labels, so a coding defect in the pool build
moves the centre with the sim, and no band sees it. Two such defects hid that
way: the hit-by-pitch rows coded as balls (SIM-509) and the two-strike foul
tips coded as fouls (SIM-553; strikeouts -4.3% against real play). The real
count shares no label with the pool, so a coding defect opens a gap here.

The label check reads the chain over the pool rows of real plate appearances
only (``chain_rates(..., pa_only=True)``), so both sides count the same
groups. Its first line proves that: the pool's plate appearances
(``pa_group_count``) must equal the real count, or the window FAILS, because
the two sides no longer read the same plate appearances (for example,
``raw.pitches`` gained games the pool has not been rebuilt for). The centres
stay on every pool row, because the simulator draws them all. A line after the check prints the all-rows centres' gap for the record;
the groups cut short by a runner out put about +0.6 points on its walks
(``pipeline/batch/pool_chain.py``, THE POPULATION RULE). On the window
2023-2026 the corrected coding reads +0.09% / -0.25% / +0.08% / +0.07%
(strikeouts / walks / hit by pitch / pitches).

The chain solver and the label check live in ``pipeline/batch/pool_chain.py``.
This script imported the solver from ``scripts/sim429_chain_analysis.py``.
Commit 6ab341c deleted that file on 2026-09-06, and the census failed on
import until SIM-553 moved the solver into the pipeline package.

USAGE
-----
    python scripts/pool_window_census.py                  # all three windows
    python scripts/pool_window_census.py --windows W1     # a subset
    python scripts/pool_window_census.py --strict         # exit 1 on a label-check FAIL

Exit codes: 0 done; 1 ``--strict`` and a window's label check failed (a
channel gap, or unequal plate-appearance counts) or could not run (Postgres
unreachable); 2 the sim DuckDB cannot be opened read-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.batch.pool_chain import (  # noqa: E402
    LABEL_CHECK_TOLERANCE,
    attach_pg,
    chain_rates,
    format_gaps,
    format_label_check,
    format_pa_count,
    label_check,
    pa_group_count,
    pool_count_matrix,
    real_pa_rates,
    solve_chain,
)
from simulation.sim_kwargs import open_sim_duckdb  # noqa: E402

#: The three windows as SQL predicates (alias-free; every pool table carries
#: season + game_date).
WINDOWS = {
    "W1 2023-2026": "season BETWEEN 2023 AND 2026",
    "W2 2024-2026": "season BETWEEN 2024 AND 2026",
    "W3 rolling 3y": "game_date BETWEEN DATE '2023-08-20' AND DATE '2026-08-19'",
}

#: The short names ``--windows`` takes: "W1" -> "W1 2023-2026", and so on.
_WINDOW_KEYS = {name.split()[0]: name for name in WINDOWS}

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Measure the pool windows, their centres and the label check (SIM-553)."
    )
    ap.add_argument(
        "--windows",
        nargs="+",
        choices=list(_WINDOW_KEYS),
        default=None,
        help="the windows to measure, by short name (default: all three)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a window's label check fails or cannot run",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    chosen = [_WINDOW_KEYS[key] for key in (args.windows or list(_WINDOW_KEYS))]

    con = open_sim_duckdb()
    if con is None:
        print("ERROR: cannot open the sim DuckDB read-only.")
        raise SystemExit(2)

    # SIM-553: the label check reads real plate appearances from Postgres. An
    # attach failure leaves the rest of the census intact and reports the check
    # as NOT RUN; --strict counts that as a failure.
    pg_error: str | None = None
    try:
        attach_pg(con)
    except RuntimeError as exc:
        pg_error = str(exc)
    failed_windows: list[str] = []

    for name in chosen:
        pred = WINDOWS[name]
        print("\n" + "=" * 78)
        print(f" {name}   ({pred})")
        print("=" * 78)

        # --- pitch pool: volume + chain-implied per-PA centres -------------
        # SIM-553: pool_count_matrix RAISES on a class outside the chain's six.
        # The old loop skipped such a class, so it dropped out of the centres
        # silently.
        mat = pool_count_matrix(con, pred)
        n_pitch = int(sum(sum(row) for row in mat))
        chain = solve_chain(mat)
        chain_rcy = solve_chain(pool_count_matrix(con, pred, weighted=True))
        print(f" pitch pool: {n_pitch:,} pitches")
        print(
            f"   pool-referenced centres: BB/PA {chain['walk']:.4f}   K/PA {chain['k']:.4f}"
            f"   HBP/PA {chain['hbp']:.4f}   pitches/PA {chain['pitches']:.3f}"
        )
        print(
            f"   recency-weighted chain:  BB/PA {chain_rcy['walk']:.4f}"
            f"   K/PA {chain_rcy['k']:.4f}   HBP/PA {chain_rcy['hbp']:.4f}"
            f"   pitches/PA {chain_rcy['pitches']:.3f}"
        )

        # --- the label check: the chain vs real plate appearances (SIM-553) -
        # The check reads the chain over the pool rows of real plate
        # appearances only (pa_only), so both sides count the same groups.
        # The centres above stay on every row: the simulator draws them all.
        if pg_error is not None:
            print(f" label check: NOT RUN ({pg_error})")
            failed_windows.append(name)
        else:
            real = real_pa_rates(con, pred)
            n_real = int(real["n_pa"])
            # SIM-553: both sides must read the same plate appearances. The
            # pool's count comes from the SQL the pa_only chain reads.
            n_pool = pa_group_count(con, pred)
            chain_pa = chain_rates(con, pred, pa_only=True)
            check = label_check(chain_pa, real)
            ok = n_pool == n_real and all(row[4] for row in check)
            print(
                f" label check (the chain over the pool rows of the {n_real:,} real"
                f" PAs vs raw.pitches, tolerance {LABEL_CHECK_TOLERANCE * 100.0:.1f}%):"
                f" {'PASS' if ok else 'FAIL'}"
            )
            print(f"   {format_pa_count(n_pool, n_real)}")
            for line in format_label_check(check):
                print(f"   {line}")
            # For the record: the all-rows centres against the same real PAs.
            # The gap includes the groups cut short (about +0.6 points on
            # walks in 2023-2026), so it is information, not the verdict.
            all_rows = format_gaps(label_check(chain, real))
            print(f"   for the record, the all-rows centres vs the same PAs: {all_rows}")
            if not ok:
                failed_windows.append(name)

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

    print()
    if failed_windows:
        print(f"label check FAILED or NOT RUN on: {', '.join(failed_windows)}")
        if args.strict:
            raise SystemExit(1)
    else:
        print("label check PASSED on every window measured.")


if __name__ == "__main__":
    main()
