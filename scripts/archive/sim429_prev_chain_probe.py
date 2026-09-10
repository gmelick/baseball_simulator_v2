"""
scripts/sim429_prev_chain_probe.py — validate prev-pitch conditioning OFFLINE (SIM-429).

WHAT THIS IS
============
The SIM-429 diagnosis found the walk/strikeout gap is STRUCTURAL: the sim draws
each pitch independently given (matchup, count), and a Markov chain over counts
on MLB's OWN per-count rates over-walks (0.0839 vs 0.0813 observed) and
under-strikes (0.2142 vs 0.2232) real MLB. The proposed fix conditions the draw
on the PREVIOUS pitch's outcome class as well (the pool already carries
``prev_pitch_outcome``).

This probe answers, from real data alone and BEFORE any sim code is written:
**does first-order prev-pitch conditioning close the gap?**

Method: measure MLB-2025 outcome shares per (balls, strikes, prev_class) —
prev_class in {first, ball, called_strike, swinging_strike, foul} — and solve
the absorbing chain over those states. If chain(count x prev) reproduces the
observed per-PA BB/K where chain(count) does not, the conditioning captures the
within-PA correlation and the design is validated. If not, the correlation is
longer-range (pitcher-day command) and a first-order refinement is not worth
building.

Also reports the (count, prev) bucket sizes — the draw's cells would shrink
from 12 to ~40 reachable buckets, and the thin ones are named here.

USAGE
-----
    python scripts/sim429_prev_chain_probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
PREVS = ("first", "ball", "called_strike", "swinging_strike", "foul")

_OUTCOME_CASE = """
    CASE
        WHEN p.events = 'hit_by_pitch' THEN 'hit_by_pitch'
        WHEN TRIM(p.type) IN ('B', '*B') THEN 'ball'
        WHEN TRIM(p.type) = 'C' THEN 'called_strike'
        WHEN TRIM(p.type) IN ('S', 'W', 'M') THEN 'swinging_strike'
        WHEN TRIM(p.type) IN ('F', 'T', 'L') THEN 'foul'
        WHEN TRIM(p.type) IN ('X', 'D', 'E') THEN 'in_play'
        ELSE 'ball'
    END
"""

_PREV_CASE = """
    CASE
        WHEN prev_type IS NULL THEN 'first'
        WHEN TRIM(prev_type) IN ('B', '*B') THEN 'ball'
        WHEN TRIM(prev_type) = 'C' THEN 'called_strike'
        WHEN TRIM(prev_type) IN ('S', 'W', 'M') THEN 'swinging_strike'
        WHEN TRIM(prev_type) IN ('F', 'T', 'L') THEN 'foul'
        ELSE 'other'
    END
"""


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _fetch() -> list:
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(f"""
            WITH seq AS (
                SELECT p.balls, p.strikes, p.type, p.events,
                       LAG(p.type) OVER (
                           PARTITION BY p.game_pk, p.at_bat_number
                           ORDER BY p.pitch_number
                       ) AS prev_type
                FROM raw.pitches p
                JOIN raw.games g USING (game_pk)
                WHERE p.season = 2025 AND g.game_type = 'R' AND g.status = 'Final'
                  AND p.data_quality_flag = FALSE
            )
            SELECT balls, strikes, {_PREV_CASE} AS prev_class,
                   {_OUTCOME_CASE.replace("p.", "")} AS outcome, COUNT(*) AS n
            FROM seq p
            GROUP BY 1, 2, 3, 4
        """)
    finally:
        await conn.close()


def solve_prev_chain(
    shares: dict[tuple[int, int, str], dict[str, float]],
) -> dict[str, float]:
    """Absorption probabilities + expected pitches from (0, 0, 'first').

    States are (balls, strikes, prev_class). A foul at two strikes moves to
    (b, 2, 'foul'); only from (b, 2, 'foul') is the foul a true self-loop
    (closed form: divide the other terms by 1 - p_foul). Unreachable or
    unmeasured states fall back to the count-marginal shares."""
    memo: dict[tuple[int, int, str], dict[str, float]] = {}

    # Count-marginal fallback for (count, prev) cells with no data.
    marginal: dict[tuple[int, int], dict[str, float]] = {}
    for (b, s, _p), sh in shares.items():
        agg = marginal.setdefault((b, s), dict.fromkeys(OUTCOMES, 0.0))
        for o, v in sh.items():
            agg[o] += v

    def _norm(d: dict[str, float]) -> dict[str, float]:
        tot = sum(d.values()) or 1.0
        return {o: d.get(o, 0.0) / tot for o in OUTCOMES}

    def state(b: int, s: int, prev: str) -> dict[str, float]:
        key = (b, s, prev)
        if key in memo:
            return memo[key]
        raw = shares.get(key) or marginal.get((b, s)) or {}
        sh = _norm(raw)
        acc = {"walk": 0.0, "k": 0.0, "in_play": 0.0, "hbp": 0.0, "pitches": 1.0}

        def add(dest: dict[str, float], w: float) -> None:
            for kk in acc:
                acc[kk] += w * dest[kk]

        if b == 3:
            acc["walk"] += sh["ball"]
        else:
            add(state(b + 1, s, "ball"), sh["ball"])
        for strike in ("called_strike", "swinging_strike"):
            if s == 2:
                acc["k"] += sh[strike]
            else:
                add(state(b, s + 1, strike), sh[strike])
        denom = 1.0
        if s < 2:
            add(state(b, s + 1, "foul"), sh["foul"])
        elif prev == "foul":
            denom = 1.0 - sh["foul"]  # the true self-loop
        else:
            add(state(b, 2, "foul"), sh["foul"])
        acc["in_play"] += sh["in_play"]
        acc["hbp"] += sh["hit_by_pitch"]
        out = {kk: v / denom for kk, v in acc.items()}
        memo[key] = out
        return out

    return state(0, 0, "first")


def main() -> None:
    rows = _fetch_rows()
    shares: dict[tuple[int, int, str], dict[str, float]] = {}
    total_by_state: dict[tuple[int, int, str], int] = {}
    for r in rows:
        key = (int(r["balls"]), int(r["strikes"]), str(r["prev_class"]))
        shares.setdefault(key, {})[str(r["outcome"])] = float(r["n"])
        total_by_state[key] = total_by_state.get(key, 0) + int(r["n"])

    other = sum(n for (b, s, p), n in total_by_state.items() if p == "other")
    print(
        f"prev-chain probe: {sum(total_by_state.values())} pitches, {len(total_by_state)} "
        f"(count, prev) states, {other} rows with prev_class 'other' (expect ~0)"
    )

    # --- the reachable-bucket census (the draw's would-be cells) -----------
    named = sorted(total_by_state.items(), key=lambda kv: kv[1])
    print("\n--- the 10 THINNEST (count, prev) buckets (MLB-2025 pitches) ---")
    for (b, s, p), n in named[:10]:
        print(f"  {b}-{s} after {p:<15} {n:>7d}")

    # --- the chains --------------------------------------------------------
    res = solve_prev_chain(shares)
    # The count-only chain from the same data (marginalize prev away).
    count_only: dict[tuple[int, int, str], dict[str, float]] = {}
    for (b, s, _p), sh in shares.items():
        agg = count_only.setdefault((b, s, "first"), {})
        for o, v in sh.items():
            agg[o] = agg.get(o, 0.0) + v
    flat = {
        (b, s, p): count_only[(b, s, "first")]
        for b in range(4)
        for s in range(3)
        for p in PREVS
        if (b, s, "first") in count_only
    }
    res_flat = solve_prev_chain(flat)

    print("\n--- per-PA rates: does prev-conditioning close the structural gap? ---")
    print(f" {'':>24} {'BB/PA':>8} {'K/PA':>8} {'pitches/PA':>11}")
    print(
        f" {'chain(count only)':>24} {res_flat['walk']:8.4f} {res_flat['k']:8.4f}"
        f" {res_flat['pitches']:11.3f}"
    )
    print(f" {'chain(count x prev)':>24} {res['walk']:8.4f} {res['k']:8.4f} {res['pitches']:11.3f}")
    print(f" {'observed MLB-2025':>24} {'0.0813':>8} {'0.2232':>8} {'3.883':>11}")
    print("\n (observed = terminal-PA-only rates from the SIM-429 diagnosis;")
    print("  the design is VALIDATED if the prev chain lands on the observed row.)")


def _fetch_rows() -> list:
    return asyncio.run(_fetch())


if __name__ == "__main__":
    main()
