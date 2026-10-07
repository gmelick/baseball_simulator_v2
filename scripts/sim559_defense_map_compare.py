#!/usr/bin/env python
"""
scripts/sim559_defense_map_compare.py
=====================================
Compare the defense map the simulator builds from ``raw.game_lineups`` with
the TRUE starting map read from the MLB box feed (``allPositions[0]``).

The hole census (the plan's SQL) counts the fielding codes a team-game's nine
batting rows carry; it cannot see two starters who swapped positions. This
tool rebuilds each team-game's map exactly as production does
(``resolve_lineup_from_rows`` + ``build_team_defense_map``) and compares it
slot by slot with the box feed. It is the verification step of the
starting-position backfill (SIM-559): after the run it must read zero missing
and zero wrong slots.

    python scripts/sim559_defense_map_compare.py --balanced --random 300
    python scripts/sim559_defense_map_compare.py --game-pks 823372
    python scripts/sim559_defense_map_compare.py --balanced --random 300 --expect-clean

``--expect-clean`` exits 1 when any slot is missing or wrong (the definition
of done). One MLB request per game; read-only on the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

from pipeline.etl.boxscore_ingest import starting_position  # noqa: E402
from simulation.lineup_resolver import (  # noqa: E402
    build_team_defense_map,
    fetch_game_sides,
    fetch_lineup_rows,
    resolve_lineup_from_rows,
)

FIELD = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
GAME_SET = _ROOT / "scripts" / "sim523_game_set.json"


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


def _get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "baseball-sim/SIM-559"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed host
        return json.load(r)


def true_map(teams: dict[str, Any], side: str) -> dict[str, int]:
    """The eight fielding slots as the box feed says the starters began."""
    out: dict[str, int] = {}
    for p in (teams.get(side) or {}).get("players", {}).values():
        bo = p.get("battingOrder")
        if not bo or int(bo) % 100:
            continue
        pos = starting_position(p)
        if pos in FIELD:
            out[pos] = int(p["person"]["id"])
    return out


def balanced_game_pks() -> list[int]:
    data = json.loads(GAME_SET.read_text(encoding="utf-8"))
    return [int(x["game_pk"]) if isinstance(x, dict) else int(x) for x in data["order"]]


async def compare(conn: Any, name: str, pks: list[int]) -> dict[str, Any]:
    t0 = time.time()
    agg: Counter = Counter()
    per_pos_missing: Counter = Counter()
    per_pos_wrong: Counter = Counter()
    examples: list[tuple] = []
    tg = 0
    for pk in pks:
        sides = await fetch_game_sides(conn, pk)
        if sides is None:
            agg["unknown_games"] += 1
            continue
        rows = await fetch_lineup_rows(conn, pk)
        res = resolve_lineup_from_rows(
            game_pk=pk,
            season=int(sides["season"]),
            home_team_id=int(sides["home_team_id"]),
            away_team_id=int(sides["away_team_id"]),
            lineup_rows=rows,
        )
        teams = _get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")["teams"]
        for side, team in (("home", res.home), ("away", res.away)):
            tg += 1
            stored = build_team_defense_map(team)
            truth = true_map(teams, side)
            missing = [p for p in FIELD if p in truth and p not in stored]
            wrong = [p for p in FIELD if p in truth and p in stored and stored[p] != truth[p]]
            n_codes = sum(1 for p in FIELD if p in stored)
            for p in missing:
                per_pos_missing[p] += 1
            for p in wrong:
                per_pos_wrong[p] += 1
            agg["slots_missing"] += len(missing)
            agg["slots_wrong"] += len(wrong)
            if missing or wrong:
                agg["team_games_defective"] += 1
            if n_codes < 8:
                agg["flagged_by_hole_census"] += 1
            if n_codes == 8 and wrong:
                agg["hidden_swaps"] += 1
            if "C" in truth and "C" not in stored:
                agg["catcher_missing"] += 1
            elif "C" in truth and stored.get("C") != truth["C"]:
                agg["catcher_wrong"] += 1
            if (missing or wrong) and len(examples) < 6:
                examples.append((pk, side, missing, wrong))
    out = {
        "set": name,
        "games": len(pks),
        "team_games": tg,
        "elapsed_s": round(time.time() - t0, 1),
        **agg,
        "per_position_missing": dict(per_pos_missing),
        "per_position_wrong": dict(per_pos_wrong),
        "examples": examples,
    }
    d = out.get("team_games_defective", 0)
    print(f"\n== {name}: {len(pks)} games, {tg} team-games ({out['elapsed_s']}s)")
    print(f"  team-games with a missing or wrong slot: {d} ({100 * d / max(tg, 1):.1f}%)")
    print(
        f"  slots missing {out.get('slots_missing', 0)}  wrong {out.get('slots_wrong', 0)}"
        f"  of {8 * tg};  catcher missing {out.get('catcher_missing', 0)}"
        f"  wrong {out.get('catcher_wrong', 0)}"
    )
    print(
        f"  flagged by the hole census {out.get('flagged_by_hole_census', 0)};"
        f"  hidden swaps (eight codes, a wrong slot) {out.get('hidden_swaps', 0)}"
    )
    print(f"  per position missing: {dict(per_pos_missing)}")
    print(f"  per position wrong:   {dict(per_pos_wrong)}")
    if examples:
        print(f"  examples (game, side, missing, wrong): {examples}")
    return out


async def run(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(_dsn())
    results: list[dict[str, Any]] = []
    try:
        if args.balanced:
            results.append(await compare(conn, "balanced", balanced_game_pks()))
        if args.random:
            await conn.execute("SELECT setseed($1)", float(args.seed))
            rows = await conn.fetch(
                "SELECT game_pk FROM raw.games WHERE status = 'Final' ORDER BY random() LIMIT $1",
                int(args.random),
            )
            results.append(
                await compare(conn, f"random{args.random}", [int(r["game_pk"]) for r in rows])
            )
        if args.game_pks:
            results.append(await compare(conn, "game_pks", [int(g) for g in args.game_pks]))
    finally:
        await conn.close()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        print(f"wrote {args.json_out}")
    defects = sum(r.get("slots_missing", 0) + r.get("slots_wrong", 0) for r in results)
    if args.expect_clean and defects:
        print(f"\nEXPECT-CLEAN FAILED: {defects} missing or wrong slots.")
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--balanced", action="store_true", help="The 45 balanced certifying games.")
    p.add_argument("--random", type=int, default=0, help="N random Final games (seeded).")
    p.add_argument("--seed", type=float, default=0.42, help="setseed() value for --random.")
    p.add_argument("--game-pks", type=int, nargs="*", default=[], help="These games.")
    p.add_argument("--json-out", default=None)
    p.add_argument("--expect-clean", action="store_true", help="Exit 1 on any defect.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
