"""
scripts/sim429_count_diagnosis.py — the SIM-429 walk-surplus diagnosis (2026-08-19 plan §1).

WHAT THIS IS
============
The sim issues +11.7% walks against the 2025 band centre. This script measures
WHERE the surplus enters, per the diagnosis plan
(docs/audit/2026-08-19-sim429-514-491-diagnosis-plan.md). It reports, side by side:

  1. Per-count pitch-outcome rates from FOUR sources:
       * sim RAW      — the full-pool draw, recorded before framing
       * sim FINAL    — the committed outcome, after SIM-428 framing
       * pool         — the pool's own unweighted per-count rates
       * pool RECENCY — the pool weighted by recency only (the neutral-draw baseline)
     plus the MLB-2025 regular-season rates (the band's reference era).
  2. Count-visitation shares (pitches per count), sim vs pool vs MLB-2025.
  3. Walk rate per count-path (PAs that visit a count -> how often they walk),
     sim vs MLB-2025.

The report then decomposes the per-pitch ball-rate surplus into four additive
parts, one per branch of the plan's decision tree:

  * KERNEL TILT — sim RAW minus pool RECENCY at the same count. A non-zero tilt
    means the matchup weighting (batter / pitcher / situation kernels) prefers
    ball rows inside a count bucket.
  * FRAMING    — sim FINAL minus sim RAW. The SIM-428 nudge should be ~neutral.
  * ERA        — pool RECENCY minus MLB-2025. The pool draws from 2024-2026;
    the band grades against 2025. This part is an OWNER reference question,
    not a code fix.
  * VISITATION — the count-mix shift. Ball-heavy counts weight the mean; a sim
    that reaches 2-0/3-1 more often walks more even at identical per-count rates.

HOW THE SIM SIDE IS INSTRUMENTED
================================
The production path draws every pitch through
``StateMachine._full_pool_outcome`` -> ``sampler.draw(balls, strikes)`` ->
``self._apply_framing(state, outcome)``. The script wraps ``_apply_framing`` on
the machine INSTANCE: the wrapper sees the live count (``state.balls/strikes``),
the raw draw (its argument) and the committed outcome (its return). The PA
tracker replays :func:`simulation.sim_loop.advance_count` on the committed
outcome to follow the count and classify terminals. A count that arrives off
the expected path (a caught-stealing 3rd out reset the half-inning) closes the
open PA as "truncated". Intentional walks throw no pitch, so the tracker's walk
count EXCLUDES IBB; the MLB walk-per-path table reports walk and intent_walk
separately for the same reason.

USAGE
-----
    # Smoke (wiring proof, ~2 min):
    python scripts/sim429_count_diagnosis.py --iters 3 745199 746494

    # The diagnosis read (~1 h serial, 1800 sims):
    python scripts/sim429_count_diagnosis.py --iters 150 --json-out /app/scripts/sim429_diag.json

With no game_pks the script uses the certified acceptance set
(tests/acceptance/bands.py BALANCED_GAME_ORDER — 12 games, park-balanced).
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
from simulation.sim_loop import (  # noqa: E402
    EVENT_STRIKEOUT,
    EVENT_WALK,
    BoxScore,
    advance_count,
    simulate_game,
)

_FACTORY = "simulation.production_factory:production_machine_factory"

#: The certified acceptance game set (tests/acceptance/bands.py
#: BALANCED_GAME_ORDER) — park-balanced, so this read shares the lane's
#: run environment.
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

#: One term per outcome, the pool-builder vocabulary (SIM-509: 6 classes).
OUTCOMES = ("ball", "called_strike", "swinging_strike", "foul", "in_play", "hit_by_pitch")
_OUT_IDX = {o: i for i, o in enumerate(OUTCOMES)}
_N_OUT = len(OUTCOMES) + 1  # +1 = "other" (never expected; a canary column)

#: 2025 per-team-game BB band centre (tests/acceptance/bands.py, SIM-508).
_BB_CENTRE_2025 = 3.1656

_COUNT_NAMES = tuple(f"{b}-{s}" for b in range(4) for s in range(3))


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


# ---------------------------------------------------------------------------
# Sim-side recorder
# ---------------------------------------------------------------------------
class Recorder:
    """Per-pitch and per-PA counters, fed by the ``_apply_framing`` wrapper."""

    def __init__(self) -> None:
        self.raw = np.zeros((12, _N_OUT), dtype=np.int64)
        self.final = np.zeros((12, _N_OUT), dtype=np.int64)
        # PA-level: PAs that VISIT a count -> terminal class counts.
        self.pa_through = np.zeros(12, dtype=np.int64)
        self.walk_through = np.zeros(12, dtype=np.int64)
        self.k_through = np.zeros(12, dtype=np.int64)
        self.pa_total = 0
        self.walks = 0
        self.strikeouts = 0
        self.in_play = 0
        self.hbp = 0
        self.truncated = 0
        self.pitches = 0
        # PA tracker state
        self._visited: set[int] = set()
        self._expect = (0, 0)
        self._open = False

    def record(self, balls: int, strikes: int, raw_out: str, final_out: str) -> None:
        b, s = int(balls), int(strikes)
        bucket = b * 3 + s
        self.raw[bucket, _OUT_IDX.get(raw_out, _N_OUT - 1)] += 1
        self.final[bucket, _OUT_IDX.get(final_out, _N_OUT - 1)] += 1
        self.pitches += 1

        # --- PA tracking (replay the §5.1 count machine) -------------------
        if (b, s) != self._expect:
            # The count moved off-path: a mid-PA half-inning roll (CS 3rd out).
            if self._open:
                self.truncated += 1
            self._visited = set()
        self._visited.add(bucket)
        self._open = True
        adv = advance_count(b, s, final_out)
        if not adv.terminal:
            self._expect = (adv.balls, adv.strikes)
            return
        event = adv.event or "in_play"
        self.pa_total += 1
        if event == EVENT_WALK:
            self.walks += 1
        elif event == EVENT_STRIKEOUT:
            self.strikeouts += 1
        elif event == "hit_by_pitch":
            self.hbp += 1
        else:
            self.in_play += 1
        for c in self._visited:
            self.pa_through[c] += 1
            if event == EVENT_WALK:
                self.walk_through[c] += 1
            elif event == EVENT_STRIKEOUT:
                self.k_through[c] += 1
        self._visited = set()
        self._expect = (0, 0)
        self._open = False


def _instrument(machine: Any, rec: Recorder) -> None:
    """Wrap ``machine._apply_framing`` so every full-pool draw is recorded with
    its live count, raw outcome and committed (post-framing) outcome."""
    orig = machine._apply_framing

    def wrapped(state: Any, outcome: str) -> str:
        b, s = int(state.balls), int(state.strikes)
        final = orig(state, outcome)
        rec.record(b, s, outcome, final)
        return final

    machine._apply_framing = wrapped


# ---------------------------------------------------------------------------
# Pool-side rates (from the loaded engine artifact)
# ---------------------------------------------------------------------------
def _pool_rates(sampler: Any) -> dict[str, np.ndarray]:
    """Per-count outcome COUNTS from the resident hand pools, both hands summed:
    unweighted row counts and recency-weighted mass."""
    unw = np.zeros((12, _N_OUT), dtype=np.float64)
    rec = np.zeros((12, _N_OUT), dtype=np.float64)
    for pool in sampler.a.pools.values():
        balls = np.clip(pool.sit[:, 0].astype(np.int64), 0, 3)
        strikes = np.clip(pool.sit[:, 1].astype(np.int64), 0, 2)
        bucket = balls * 3 + strikes
        oidx = np.array(
            [_OUT_IDX.get(str(o), _N_OUT - 1) for o in pool.outcome_type], dtype=np.int64
        )
        np.add.at(unw, (bucket, oidx), 1.0)
        np.add.at(rec, (bucket, oidx), pool.recency.astype(np.float64))
    return {"unweighted": unw, "recency": rec}


# ---------------------------------------------------------------------------
# MLB-2025 reference (Postgres raw.pitches, regular-season Final games)
# ---------------------------------------------------------------------------
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

_MLB_FROM = """
    FROM raw.pitches p
    JOIN raw.games g USING (game_pk)
    WHERE p.season = 2025 AND g.game_type = 'R' AND g.status = 'Final'
      AND p.data_quality_flag = FALSE
"""


async def _mlb_reference() -> dict[str, Any]:
    conn = await asyncpg.connect(_dsn())
    try:
        rate_rows = await conn.fetch(
            f"SELECT p.balls, p.strikes, {_OUTCOME_CASE} AS outcome_type, COUNT(*) AS n "
            f"{_MLB_FROM} GROUP BY 1, 2, 3"
        )
        path_rows = await conn.fetch(f"""
            WITH term AS (
                SELECT p.game_pk, p.at_bat_number,
                       MAX(p.events) FILTER (WHERE p.events IS NOT NULL AND p.events <> '')
                           AS ev
                {_MLB_FROM}
                GROUP BY 1, 2
            ),
            visits AS (
                SELECT DISTINCT p.game_pk, p.at_bat_number, p.balls, p.strikes
                {_MLB_FROM}
            )
            SELECT v.balls, v.strikes,
                   COUNT(*) AS pas_through,
                   COUNT(*) FILTER (WHERE t.ev = 'walk')        AS walks,
                   COUNT(*) FILTER (WHERE t.ev = 'intent_walk') AS intent_walks,
                   COUNT(*) FILTER (WHERE t.ev IN ('strikeout', 'strikeout_double_play'))
                       AS strikeouts
            FROM visits v
            JOIN term t USING (game_pk, at_bat_number)
            GROUP BY 1, 2
        """)
        totals = await conn.fetchrow(f"""
            SELECT COUNT(*) AS pitches,
                   COUNT(DISTINCT (p.game_pk, p.at_bat_number)) AS pas,
                   COUNT(DISTINCT p.game_pk) AS games
            {_MLB_FROM}
        """)
    finally:
        await conn.close()

    rates = np.zeros((12, _N_OUT), dtype=np.float64)
    for r in rate_rows:
        bucket = int(r["balls"]) * 3 + int(r["strikes"])
        rates[bucket, _OUT_IDX.get(r["outcome_type"], _N_OUT - 1)] += float(r["n"])
    path = {
        int(r["balls"]) * 3 + int(r["strikes"]): {
            "pas_through": int(r["pas_through"]),
            "walks": int(r["walks"]),
            "intent_walks": int(r["intent_walks"]),
            "strikeouts": int(r["strikeouts"]),
        }
        for r in path_rows
    }
    return {
        "rates": rates,
        "path": path,
        "pitches": int(totals["pitches"]),
        "pas": int(totals["pas"]),
        "games": int(totals["games"]),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _shares(counts: np.ndarray) -> np.ndarray:
    """Row-normalized outcome shares; rows with no mass stay zero."""
    tot = counts.sum(axis=1, keepdims=True)
    return np.divide(counts, tot, out=np.zeros_like(counts, dtype=np.float64), where=tot > 0)


def _visitation(counts: np.ndarray) -> np.ndarray:
    tot = counts.sum()
    return counts.sum(axis=1) / tot if tot > 0 else np.zeros(12)


def _print_report(
    rec: Recorder, pool: dict[str, np.ndarray], mlb: dict[str, Any], n_sims: int
) -> dict[str, Any]:
    raw_sh = _shares(rec.raw.astype(np.float64))
    fin_sh = _shares(rec.final.astype(np.float64))
    unw_sh = _shares(pool["unweighted"])
    rcy_sh = _shares(pool["recency"])
    mlb_sh = _shares(mlb["rates"])
    vis_sim = _visitation(rec.final.astype(np.float64))
    vis_pool = _visitation(pool["unweighted"])
    vis_mlb = _visitation(mlb["rates"])

    print("\n" + "=" * 88)
    print(
        f" SIM-429 COUNT DIAGNOSIS — {n_sims} sims, {rec.pitches} sim pitches, "
        f"{rec.pa_total} PAs ({rec.truncated} truncated)"
    )
    print(f" MLB-2025 reference: {mlb['games']} games, {mlb['pitches']} pitches, {mlb['pas']} PAs")
    print("=" * 88)

    bb_tg = rec.walks / (2.0 * n_sims) if n_sims else 0.0
    print(
        f"\n sim BB/team-game (excl. IBB) = {bb_tg:.4f}   "
        f"band centre (incl. IBB) = {_BB_CENTRE_2025:.4f}   "
        f"delta = {(bb_tg - _BB_CENTRE_2025) / _BB_CENTRE_2025 * 100.0:+.1f}%"
    )
    print(
        f" sim per-PA: BB {rec.walks / max(1, rec.pa_total):.4f}  "
        f"K {rec.strikeouts / max(1, rec.pa_total):.4f}  "
        f"HBP {rec.hbp / max(1, rec.pa_total):.4f}  "
        f"pitches/PA {rec.pitches / max(1, rec.pa_total):.3f}"
    )

    # --- 1. BALL rate per count (the headline table) -----------------------
    bi = _OUT_IDX["ball"]
    print("\n--- 1. BALL rate per count (simRAW = pre-framing, simFIN = committed) ---")
    print(
        f" {'count':>5} {'simRAW':>8} {'simFIN':>8} {'poolUNW':>8} {'poolRCY':>8}"
        f" {'MLB25':>8} | {'tilt':>7} {'frame':>7} {'era':>7}"
    )
    for c in range(12):
        tilt = raw_sh[c, bi] - rcy_sh[c, bi]
        frame = fin_sh[c, bi] - raw_sh[c, bi]
        era = rcy_sh[c, bi] - mlb_sh[c, bi]
        print(
            f" {_COUNT_NAMES[c]:>5} {raw_sh[c, bi]:8.4f} {fin_sh[c, bi]:8.4f}"
            f" {unw_sh[c, bi]:8.4f} {rcy_sh[c, bi]:8.4f} {mlb_sh[c, bi]:8.4f} |"
            f" {tilt:+7.4f} {frame:+7.4f} {era:+7.4f}"
        )
    # Visitation-weighted decomposition of the mean per-pitch ball rate.
    w = vis_sim
    tilt_m = float((w * (raw_sh[:, bi] - rcy_sh[:, bi])).sum())
    frame_m = float((w * (fin_sh[:, bi] - raw_sh[:, bi])).sum())
    era_m = float((w * (rcy_sh[:, bi] - mlb_sh[:, bi])).sum())
    visit_m = float(((vis_sim - vis_mlb) * mlb_sh[:, bi]).sum())
    total_m = float((vis_sim * fin_sh[:, bi]).sum() - (vis_mlb * mlb_sh[:, bi]).sum())
    print("\n mean per-pitch ball-rate surplus vs MLB-2025 (visitation-weighted):")
    print(
        f"   KERNEL TILT {tilt_m:+.4f}   FRAMING {frame_m:+.4f}   ERA {era_m:+.4f}"
        f"   VISITATION {visit_m:+.4f}   TOTAL {total_m:+.4f}"
    )

    # --- 2. Full outcome matrix, sim FINAL vs pool RECENCY -----------------
    print("\n--- 2. Outcome shares per count: sim FINAL / pool RECENCY (delta) ---")
    print(f" {'count':>5} " + " ".join(f"{o[:7]:>17}" for o in OUTCOMES))
    for c in range(12):
        cells = []
        for o in OUTCOMES:
            i = _OUT_IDX[o]
            cells.append(
                f"{fin_sh[c, i]:.3f}/{rcy_sh[c, i]:.3f} {fin_sh[c, i] - rcy_sh[c, i]:+.3f}"
            )
        print(f" {_COUNT_NAMES[c]:>5} " + " ".join(f"{x:>17}" for x in cells))
    other_raw = int(rec.raw[:, -1].sum())
    if other_raw:
        print(f" ⚠ {other_raw} draws fell in the OTHER outcome column — investigate.")

    # --- 3. Count visitation ------------------------------------------------
    print("\n--- 3. Count visitation (share of pitches) ---")
    print(f" {'count':>5} {'sim':>8} {'pool':>8} {'MLB25':>8} {'sim-MLB':>9}")
    for c in range(12):
        print(
            f" {_COUNT_NAMES[c]:>5} {vis_sim[c]:8.4f} {vis_pool[c]:8.4f}"
            f" {vis_mlb[c]:8.4f} {vis_sim[c] - vis_mlb[c]:+9.4f}"
        )

    # --- 4. Walk rate per count-path ----------------------------------------
    print("\n--- 4. Walk rate of PAs that VISIT a count (sim excl. IBB; MLB walk + [intent]) ---")
    print(
        f" {'count':>5} {'simPA':>8} {'simBB%':>8} {'mlbPA':>8} {'mlbBB%':>8}"
        f" {'mlbIBB%':>8} {'simK%':>7} {'mlbK%':>7}"
    )
    for c in range(12):
        sp = int(rec.pa_through[c])
        m = mlb["path"].get(c, {"pas_through": 0, "walks": 0, "intent_walks": 0, "strikeouts": 0})
        mp = m["pas_through"]
        print(
            f" {_COUNT_NAMES[c]:>5} {sp:>8d}"
            f" {rec.walk_through[c] / sp if sp else 0.0:8.4f} {mp:>8d}"
            f" {m['walks'] / mp if mp else 0.0:8.4f}"
            f" {m['intent_walks'] / mp if mp else 0.0:8.4f}"
            f" {rec.k_through[c] / sp if sp else 0.0:7.4f}"
            f" {m['strikeouts'] / mp if mp else 0.0:7.4f}"
        )

    # --- Decision-tree read --------------------------------------------------
    print("\n--- decision-tree read (plan §1) ---")
    parts = {"KERNEL TILT": tilt_m, "FRAMING": frame_m, "ERA": era_m, "VISITATION": visit_m}
    lead = max(parts, key=lambda k: abs(parts[k]))
    print(f" the largest ball-rate component is {lead} ({parts[lead]:+.4f}).")
    print(" branch 1 (rates diverge from the pool)  -> kernel/factor fix (SIM-476 style)")
    print(" branch 2 (rates match, visitation off)  -> count-transition fix")
    print(" branch 3 (both match, BB still high)    -> era question for the OWNER")

    return {
        "sim_raw": rec.raw.tolist(),
        "sim_final": rec.final.tolist(),
        "pool_unweighted": pool["unweighted"].tolist(),
        "pool_recency": pool["recency"].tolist(),
        "mlb_rates": mlb["rates"].tolist(),
        "mlb_path": {str(k): v for k, v in mlb["path"].items()},
        "sim_path": {
            "pa_through": rec.pa_through.tolist(),
            "walk_through": rec.walk_through.tolist(),
            "k_through": rec.k_through.tolist(),
        },
        "sim_pa": {
            "pa_total": rec.pa_total,
            "walks": rec.walks,
            "strikeouts": rec.strikeouts,
            "in_play": rec.in_play,
            "hbp": rec.hbp,
            "truncated": rec.truncated,
            "pitches": rec.pitches,
        },
        "decomposition": {
            "kernel_tilt": tilt_m,
            "framing": frame_m,
            "era": era_m,
            "visitation": visit_m,
            "total": total_m,
        },
        "bb_per_team_game_excl_ibb": bb_tg,
        "outcomes": list(OUTCOMES),
        "count_names": list(_COUNT_NAMES),
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
    ap.add_argument("--iters", type=int, default=150, help="Iterations per game (default 150).")
    ap.add_argument("--json-out", type=str, default=None)
    args = ap.parse_args()
    game_pks = args.game_pks or list(_DEFAULT_GAME_PKS)

    started = time.perf_counter()
    print(
        f"sim429_count_diagnosis: {len(game_pks)} games x {args.iters} sims "
        f"= {len(game_pks) * args.iters} total sims"
    )
    print(
        "  SIM_FULL_POOL="
        + os.environ.get("SIM_FULL_POOL", "<unset>")
        + "  SIM_FRAMING="
        + os.environ.get("SIM_FRAMING", "<unset>")
    )

    print("  fetching the MLB-2025 reference from Postgres ...")
    mlb = asyncio.run(_mlb_reference())
    print(f"  MLB-2025: {mlb['games']} games / {mlb['pitches']} pitches / {mlb['pas']} PAs")

    rec = Recorder()
    pool_rates: dict[str, np.ndarray] | None = None
    duck = open_sim_duckdb()
    try:
        for gp in game_pks:
            state = asyncio.run(_resolve(gp, duck))
            kw = sim_kwargs_from_state(state)
            spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(kw))
            machine = production_machine_factory(0, spec)
            if machine.full_pool_sampler is None:
                raise RuntimeError(
                    "the machine has no full_pool_sampler — set SIM_FULL_POOL=1; "
                    "this diagnosis measures the production path only."
                )
            if pool_rates is None:
                pool_rates = _pool_rates(machine.full_pool_sampler)
            _instrument(machine, rec)
            t0 = time.perf_counter()
            for seed in range(args.iters):
                machine.boxscore = BoxScore()
                simulate_game(state_machine=machine, seed=seed, **kw)
            print(
                f"  game {gp}: {args.iters} sims in {time.perf_counter() - t0:.1f}s "
                f"(cum pitches {rec.pitches}, PAs {rec.pa_total})"
            )
    finally:
        if duck is not None:
            duck.close()

    n_sims = len(game_pks) * args.iters
    assert pool_rates is not None
    payload = _print_report(rec, pool_rates, mlb, n_sims)
    print(f"\nelapsed: {time.perf_counter() - started:.1f}s")

    if args.json_out:
        payload["n_sims"] = n_sims
        payload["game_pks"] = game_pks
        payload["iters"] = args.iters
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
