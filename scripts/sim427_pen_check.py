"""
scripts/sim427_pen_check.py — SIM-427 part 4b's check: the real pen against reality.

For a sample of games (hundreds, never dozens — CLAUDE.md §2b) it builds the
pen the resolver would build (``pipeline.bullpen_usage.fetch_pen_for_game``:
the MLB box's listing minus the rotation minus today's starter) and reads:

  * COVERAGE — how many Final games in the seasons have a listing at all;
  * THE PEN — arms per side (the majors carry 8 in a 13-pitcher staff), how
    many of them were the rotation's off-day starters the rule removed;
  * AGAINST THE BOX — every reliever who actually PITCHED in the game
    (``raw.game_player_stats``, ``played_pitch`` and not ``p_started``) must be
    in the pen: the share of relievers-used that the pen contains, per game and
    overall (the plan expects 90%+; a miss is a listing gap or a rotation rule
    that swallowed a real reliever);
  * AGAINST THE SIM-433 TABLE — for the games that also carry
    ``raw.game_bullpen_availability`` rows: the share of the pen the older
    table calls available, and the share of its available arms the pen holds;
  * REST — the pen's days-of-rest and pitched-in-two-days mix, against the
    entering arms' mix on the change pool (the two must agree for the
    reliever draw's rest weights to mean anything).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim427_pen_check.py --seasons 2024 2025 --games 400
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.bullpen_usage import fetch_pen_for_game  # noqa: E402


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def main_async(args: argparse.Namespace) -> int:
    import asyncpg

    conn = await asyncpg.connect(_dsn(), timeout=30)
    try:
        cov = await conn.fetch(
            """
            SELECT g.season,
                   COUNT(*) AS games,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM raw.game_bullpen b WHERE b.game_pk = g.game_pk)) AS listed,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM raw.game_bullpen_availability a WHERE a.game_pk = g.game_pk))
                       AS sim433
            FROM raw.games g
            WHERE g.status = 'Final' AND g.season = ANY($1::int[])
            GROUP BY g.season ORDER BY g.season
            """,
            args.seasons,
        )
        print("coverage (Final games with a box listing / with SIM-433 rows):")
        for r in cov:
            print(
                f"  {r['season']}: {r['games']:>5} games, listing {r['listed']:>5} "
                f"({r['listed'] / max(r['games'], 1):.1%}), SIM-433 {r['sim433']:>5}"
            )
        games = await conn.fetch(
            """
            SELECT g.game_pk, g.season, g.game_date::date AS game_date,
                   g.home_team_id, g.away_team_id
            FROM raw.games g
            WHERE g.status = 'Final' AND g.season = ANY($1::int[])
              AND EXISTS (SELECT 1 FROM raw.game_bullpen b WHERE b.game_pk = g.game_pk)
            ORDER BY g.game_pk
            """,
            args.seasons,
        )
        rng = random.Random(args.seed)
        sample = rng.sample(list(games), min(args.games, len(games)))
        print(f"\nsample: {len(sample)} games of {len(games)} listed")

        pen_sizes: list[int] = []
        used_total = used_in_pen = 0
        games_all_in = games_read = 0
        misses: Counter[str] = Counter()
        rest_mix: Counter[int] = Counter()
        p2d = Counter()
        s433_pen_avail = s433_pen_n = 0
        s433_avail_in_pen = s433_avail_n = 0
        starter_missing = 0
        for g in sample:
            gp = int(g["game_pk"])
            # today's starters from the box
            starters = {
                int(r["team_id"]): int(r["player_id"])
                for r in await conn.fetch(
                    "SELECT team_id, player_id FROM raw.game_player_stats "
                    "WHERE game_pk = $1 AND p_started",
                    gp,
                )
            }
            for team in (int(g["home_team_id"]), int(g["away_team_id"])):
                starter = starters.get(team)
                if starter is None:
                    starter_missing += 1
                out = await fetch_pen_for_game(
                    conn,
                    game_pk=gp,
                    team_id=team,
                    game_date=g["game_date"],
                    exclude=[starter] if starter is not None else [],
                )
                if out is None:
                    misses["no listing for the side"] += 1
                    continue
                pen, usage = out
                games_read += 1
                pen_sizes.append(len(pen))
                pen_set = set(pen)
                for u in usage.values():
                    rest_mix[int(u.days_rest)] += 1
                    p2d[bool(u.pitched_2d)] += 1
                used = [
                    int(r["player_id"])
                    for r in await conn.fetch(
                        "SELECT player_id FROM raw.game_player_stats "
                        "WHERE game_pk = $1 AND team_id = $2 AND played_pitch AND NOT p_started",
                        gp,
                        team,
                    )
                ]
                used_total += len(used)
                hit = sum(1 for u in used if u in pen_set)
                used_in_pen += hit
                if hit == len(used):
                    games_all_in += 1
                else:
                    # why: listed at all? in the rotation?
                    for u in used:
                        if u in pen_set:
                            continue
                        row = await conn.fetchrow(
                            "SELECT listed FROM raw.game_bullpen WHERE game_pk = $1 AND pitcher_id = $2",
                            gp,
                            u,
                        )
                        if row is None:
                            misses["reliever used but not in the listing"] += 1
                        else:
                            misses[f"listed '{row['listed']}' but removed (rotation rule)"] += 1
                s433 = await conn.fetch(
                    "SELECT pitcher_id, available FROM raw.game_bullpen_availability "
                    "WHERE game_pk = $1 AND team_id = $2",
                    gp,
                    team,
                )
                if s433:
                    avail = {int(r["pitcher_id"]) for r in s433 if r["available"]}
                    known = {int(r["pitcher_id"]) for r in s433}
                    in_known = [p for p in pen if p in known]
                    s433_pen_n += len(in_known)
                    s433_pen_avail += sum(1 for p in in_known if p in avail)
                    s433_avail_n += len(avail)
                    s433_avail_in_pen += sum(1 for p in avail if p in pen_set)

        print(
            f"\nsides read: {games_read}; today's starter missing from the box: {starter_missing}"
        )
        if pen_sizes:
            import statistics

            print(
                f"pen size: mean {statistics.mean(pen_sizes):.2f}, "
                f"min {min(pen_sizes)}, max {max(pen_sizes)}, "
                f"share with fewer than 5 arms {sum(1 for n in pen_sizes if n < 5) / len(pen_sizes):.1%}"
            )
        print(
            f"relievers who pitched in the game and are IN the pen: {used_in_pen}/{used_total} "
            f"({used_in_pen / max(used_total, 1):.1%}); sides with every used reliever in the pen: "
            f"{games_all_in}/{games_read} ({games_all_in / max(games_read, 1):.1%})"
        )
        if misses:
            print("  misses by cause:")
            for k, v in misses.most_common():
                print(f"    {v:>5}  {k}")
        if s433_pen_n:
            print(
                f"against SIM-433: the pen's arms the older table calls available "
                f"{s433_pen_avail}/{s433_pen_n} ({s433_pen_avail / s433_pen_n:.1%}); "
                f"its available arms that the pen holds {s433_avail_in_pen}/{s433_avail_n} "
                f"({s433_avail_in_pen / max(s433_avail_n, 1):.1%})"
            )
        else:
            print("against SIM-433: no availability rows on the sampled games")
        n_rest = sum(rest_mix.values())
        if n_rest:
            print(
                "pen rest mix (days since the last appearance, 5 = five or more): "
                + ", ".join(f"{d}: {rest_mix[d] / n_rest:.1%}" for d in sorted(rest_mix))
                + f"; pitched in the last two days {p2d[True] / n_rest:.1%}"
            )
        return 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--games", type=int, default=400)
    ap.add_argument("--seed", type=int, default=427)
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
