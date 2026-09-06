"""
scripts/trace_game.py — per-pitch sim trace (SIM-408 path-A diagnostic).

Runs ONE game through the PRODUCTION sim loop (the full-pool sampler) and
emits a CSV with one row per pitch:

  * game state BEFORE the pitch (inning/half/outs/count/baserunners/score/batter/pitcher)
  * box-score totals BEFORE (AB/H/HR/RBI aggregated across all batters)
  * the selected pitch + its resolution (pitch_outcome / event / canonical_event /
    runs_scored / outs_recorded / is_error)
  * game state AFTER
  * box-score totals AFTER

This surfaces exactly where hits stop being recorded / runs get over-credited:
watch for rows where ``runs_scored > 0`` (RBI/score climbs) while
``canonical_event`` is NOT a hit and the box ``H`` does not increment.

Usage (inside the app container, which has the DuckDB + Postgres on the net):
    python scripts/trace_game.py 744980 --seed 42 > trace_744980.csv
(CSV goes to stdout; a human-readable summary goes to stderr.)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

from simulation.batch_runner import GameSpec  # noqa: E402
from simulation.lineup_resolver import resolve_game_state  # noqa: E402
from simulation.production_factory import production_machine_factory  # noqa: E402
from simulation.sim_loop import simulate_game  # noqa: E402

_FACTORY = "simulation.production_factory:production_machine_factory"


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _resolve_state(game_pk: int, seed: int):
    conn = await asyncpg.connect(_dsn())
    try:
        return await resolve_game_state(conn, game_pk, seed=seed)
    finally:
        await conn.close()


def _box_totals(box) -> tuple[int, int, int, int]:
    """Aggregate (AB, H, HR, RBI) across every batter line; (0,0,0,0) if no box."""
    if box is None:
        return (0, 0, 0, 0)
    ab = h = hr = rbi = 0
    for ln in box.lines.values():
        ab += int(getattr(ln, "ab", 0) or 0)
        h += int(getattr(ln, "h", 0) or 0)
        hr += int(getattr(ln, "hr", 0) or 0)
        rbi += int(getattr(ln, "rbi", 0) or 0)
    return (ab, h, hr, rbi)


def _state_snap(s) -> dict:
    b = getattr(s, "bases", None)
    half = getattr(s, "half", None)
    return {
        "inning": getattr(s, "inning", None),
        "half": getattr(half, "name", str(half)),
        "outs": getattr(s, "outs", None),
        "balls": getattr(s, "balls", None),
        "strikes": getattr(s, "strikes", None),
        "on1b": getattr(b, "first", None),
        "on2b": getattr(b, "second", None),
        "on3b": getattr(b, "third", None),
        "home_score": getattr(s, "home_score", None),
        "away_score": getattr(s, "away_score", None),
        "batter": getattr(s, "batter_id", None),
        "pitcher": getattr(s, "pitcher_id", None),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-pitch sim trace (SIM-408).")
    ap.add_argument("game_pk", type=int)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    state = asyncio.run(_resolve_state(args.game_pk, args.seed))
    sim_kwargs = {
        "away_lineup": list(getattr(state, "away_lineup", []) or []),
        "home_lineup": list(getattr(state, "home_lineup", []) or []),
        "season": int(getattr(state, "season", 2024) or 2024),
        "pitcher_id": int(getattr(state, "pitcher_id", 0) or 0),
        "bat_hand": str(getattr(state, "bat_hand", "R") or "R"),
        "bat_hands": dict(getattr(state, "bat_hands", {}) or {}),
        "throw_hands": dict(getattr(state, "throw_hands", {}) or {}),
        "home_pitcher_id": getattr(state, "home_pitcher_id", None),
        "away_pitcher_id": getattr(state, "away_pitcher_id", None),
        "max_innings": 12,
    }
    spec = GameSpec(machine_factory=_FACTORY, sim_kwargs=dict(sim_kwargs))
    machine = production_machine_factory(args.seed, spec)

    rows: list[dict] = []
    counter = {"n": 0}
    orig_step = machine.step_pitch

    def traced(st, *a, **k):
        before = _state_snap(st)
        b_hand = getattr(st, "bat_hand", None)
        b_season = getattr(st, "season", None)
        bab, bh, bhr, brbi = _box_totals(machine.boxscore)
        res = orig_step(st, *a, **k)
        after = _state_snap(st)
        aab, ah, ahr, arbi = _box_totals(machine.boxscore)
        counter["n"] += 1
        rows.append(
            {
                "pitch": counter["n"],
                **{f"b_{kk}": vv for kk, vv in before.items()},
                "bat_hand": b_hand,
                "season": b_season,
                "pitch_outcome": getattr(res, "pitch_outcome", None),
                "is_contact": getattr(res, "is_contact", None),
                "pa_terminal": getattr(res, "pa_terminal", None),
                "event": getattr(res, "event", None),
                "canonical_event": getattr(res, "canonical_event", None),
                "runs_scored": getattr(res, "runs_scored", None),
                "outs_recorded": getattr(res, "outs_recorded", None),
                "is_error": getattr(res, "is_error", None),
                **{f"a_{kk}": vv for kk, vv in after.items()},
                "box_AB": f"{bab}->{aab}",
                "box_H": f"{bh}->{ah}",
                "box_HR": f"{bhr}->{ahr}",
                "box_RBI": f"{brbi}->{arbi}",
            }
        )
        return res

    machine.step_pitch = traced  # type: ignore[method-assign]
    result = simulate_game(state_machine=machine, seed=args.seed, **sim_kwargs)

    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    ab, h, hr, rbi = _box_totals(machine.boxscore)
    final_box = result.boxscore
    fh = _box_totals(final_box)[1] if final_box is not None else h
    print(
        f"\n[trace] game={args.game_pk} pitches={len(rows)} | "
        f"final box AB={ab} H={h} HR={hr} RBI={rbi} | "
        f"final score away={getattr(result, 'away_score', '?')} "
        f"home={getattr(result, 'home_score', '?')} | box_H_check={fh}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
