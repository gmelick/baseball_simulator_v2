"""
scripts/sim558_starter_census.py — does every game resolve its real starters? (SIM-558)

WHAT THIS IS
============
A read-only count over every Final game in ``raw.games``.  For each side of
each game it compares three answers to "who started on the mound":

  * the lineup's pitcher row alone (``raw.game_lineups``, the resolver before
    SIM-558);
  * the official box (``raw.game_player_stats.p_started``);
  * the resolver as it runs now (the box first, the lineup row second).

It sorts each side where the lineup row disagrees with the box into its cause:

  * ``missing``   — the lineup has no pitcher row (a two-way starter, coded by
    his batting position);
  * ``on the mound`` — the lineup's pick pitched in the game and did not start
    it (a position player who finished the game pitching: the lineup stores the
    LAST position a player held);
  * ``scratched`` — the lineup's pick threw no pitch (an announced starter who
    was replaced before his first pitch).

``--async-sample N`` then resolves N games through the real path
(``resolve_game_state`` against Postgres): every game with a disagreement plus
a random rest.  It checks that both starters equal the box, that each starter
has a throwing hand, and that no starter is listed in his own pen.

The exit code is 1 when the resolver as it runs now leaves any side missing or
different from the box, or when a game in the sample fails.

USAGE
-----
    MSYS_NO_PATHCONV=1 docker compose run --rm -T --no-deps \\
        -v "$PWD/scripts:/app/scripts" app \\
        python scripts/sim558_starter_census.py --async-sample 1000
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import random
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402

from simulation.lineup_resolver import (  # noqa: E402
    LineupResolutionError,
    build_game_state,
    resolve_game_state,
    resolve_lineup_from_rows,
)

#: The play pool's window (CLAUDE.md §2b): the seasons the sampler draws from.
POOL_WINDOW = (2023, 2024, 2025, 2026)


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    ).replace("+asyncpg", "")


def classify(lineup_pick: int | None, box_starter: int | None, pick_pitched: bool) -> str:
    """Sort one side into ``ok`` / ``missing`` / ``on the mound`` / ``scratched``
    / ``no box``.  ``pick_pitched`` says whether the lineup's pick threw a pitch
    in the game (his box row's ``played_pitch``)."""
    if box_starter is None:
        return "no box"
    if lineup_pick is None:
        return "missing"
    if int(lineup_pick) == int(box_starter):
        return "ok"
    return "on the mound" if pick_pitched else "scratched"


async def _load(conn: Any) -> tuple[dict, dict, dict, set]:
    games = {
        int(r["game_pk"]): r
        for r in await conn.fetch(
            "SELECT game_pk, season, game_type, home_team_id, away_team_id "
            "FROM raw.games WHERE status = 'Final'"
        )
    }
    rows: dict[int, list] = collections.defaultdict(list)
    for r in await conn.fetch(
        "SELECT game_pk, team_id, player_id, batting_order, position_code, is_starter, "
        "sequence, entered_inning, entered_at_bat, pinch_role FROM raw.game_lineups "
        "ORDER BY game_pk, team_id, batting_order, sequence"
    ):
        rows[int(r["game_pk"])].append(r)
    box: dict[int, dict[int, int]] = collections.defaultdict(dict)
    pitched: set[tuple[int, int]] = set()
    for r in await conn.fetch(
        "SELECT game_pk, team_id, player_id, p_started, played_pitch "
        "FROM raw.game_player_stats WHERE p_started OR played_pitch"
    ):
        if r["p_started"]:
            box[int(r["game_pk"])][int(r["team_id"])] = int(r["player_id"])
        if r["played_pitch"]:
            pitched.add((int(r["game_pk"]), int(r["player_id"])))
    return games, rows, box, pitched


def census(games: dict, rows: dict, box: dict, pitched: set) -> tuple[dict, list[int], int]:
    """Count every side.  Returns (counters, the games with a disagreement, the
    number of sides the resolver as it runs now leaves wrong)."""
    by_cause: collections.Counter = collections.Counter()
    by_season: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    by_side: collections.Counter = collections.Counter()
    now: collections.Counter = collections.Counter()
    problem_games: list[int] = []
    for pk, g in games.items():
        kw = {
            "game_pk": pk,
            "season": int(g["season"]),
            "home_team_id": int(g["home_team_id"]),
            "away_team_id": int(g["away_team_id"]),
            "lineup_rows": rows.get(pk, []),
        }
        if not kw["lineup_rows"]:
            now["no lineup rows"] += 1
            continue
        lineup_only = resolve_lineup_from_rows(**kw)
        fixed = resolve_lineup_from_rows(**kw, box_starters=box.get(pk, {}))
        bad = False
        for side, old, new in (
            ("home", lineup_only.home, fixed.home),
            ("away", lineup_only.away, fixed.away),
        ):
            starter = box.get(pk, {}).get(int(old.team_id))
            cause = classify(
                old.pitcher_id,
                starter,
                old.pitcher_id is not None and (pk, int(old.pitcher_id)) in pitched,
            )
            by_cause[cause] += 1
            if cause not in ("ok", "no box"):
                bad = True
                by_season[int(g["season"])][cause] += 1
                by_side[(cause, side)] += 1
            if new.pitcher_id is None:
                now["missing"] += 1
            elif starter is not None and int(new.pitcher_id) != int(starter):
                now["differs from the box"] += 1
            else:
                now["equals the box"] += 1
        if bad:
            problem_games.append(pk)
            by_season[int(g["season"])]["games"] += 1
        try:
            build_game_state(fixed)
        except LineupResolutionError:
            now["refused by build_game_state"] += 1
    counters = {"by_cause": by_cause, "by_season": by_season, "by_side": by_side, "now": now}
    return counters, problem_games, int(now["missing"] + now["differs from the box"])


async def _async_sample(
    conn: Any, games: dict, box: dict, problem_games: list[int], n: int, seed: int
) -> tuple[collections.Counter, int]:
    rng = random.Random(seed)
    rest_pool = sorted(set(games) - set(problem_games))
    rest = rng.sample(rest_pool, max(0, min(len(rest_pool), n - len(problem_games))))
    out: collections.Counter = collections.Counter()
    failures = 0
    for pk in problem_games + rest:
        out["games"] += 1
        g = games[pk]
        try:
            st = await resolve_game_state(conn, pk, seed=0)
        except LineupResolutionError as exc:
            out["refused"] += 1
            failures += 1
            print(f"  REFUSED {pk}: {exc}")
            continue
        want = (box[pk].get(int(g["home_team_id"])), box[pk].get(int(g["away_team_id"])))
        if (st.home_pitcher_id, st.away_pitcher_id) == want:
            out["both starters equal the box"] += 1
        else:
            out["a starter differs from the box"] += 1
            failures += 1
            print(f"  DIFFERS {pk}: state {(st.home_pitcher_id, st.away_pitcher_id)} box {want}")
        for side_int, pid in ((0, st.away_pitcher_id), (1, st.home_pitcher_id)):
            if pid not in st.throw_hands:
                out["a starter with no throwing hand"] += 1
            if pid in (st.manager.bullpen_available.get(side_int) or []):
                out["a starter listed in his own pen"] += 1
                failures += 1
    return out, failures


async def _main(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(_dsn(), timeout=60)
    try:
        games, rows, box, pitched = await _load(conn)
        counters, problem_games, wrong_now = census(games, rows, box, pitched)
        sides = sum(counters["by_cause"].values())
        print(f"Final games: {len(games):,}  sides (team-games) with lineup rows: {sides:,}")
        print("\nThe lineup's pitcher row alone, against the official box:")
        for cause in ("ok", "missing", "on the mound", "scratched", "no box"):
            print(f"  {cause:14s} {counters['by_cause'][cause]:>7,}")
        print(f"  games with a disagreement on either side: {len(problem_games):,}")
        for cause in ("missing", "on the mound", "scratched"):
            h, a = counters["by_side"][(cause, "home")], counters["by_side"][(cause, "away")]
            print(f"    {cause:14s} home {h:>4}  away {a:>4}")
        print("\n  season   games  missing  on the mound  scratched")
        window_games = 0
        for season in sorted(counters["by_season"]):
            c = counters["by_season"][season]
            if season in POOL_WINDOW:
                window_games += c["games"]
            print(
                f"  {season}   {c['games']:>5}  {c['missing']:>7}  {c['on the mound']:>12}"
                f"  {c['scratched']:>9}"
            )
        n_window = sum(1 for g in games.values() if int(g["season"]) in POOL_WINDOW)
        print(
            f"  the pool window {POOL_WINDOW[0]}-{POOL_WINDOW[-1]}: {window_games} of {n_window:,} games"
        )
        print("\nThe resolver as it runs now (the box first, the lineup row second):")
        for key in (
            "equals the box",
            "missing",
            "differs from the box",
            "refused by build_game_state",
            "no lineup rows",
        ):
            print(f"  {key:28s} {counters['now'][key]:>7,}")
        failures = 0
        if args.async_sample > 0:
            sample, failures = await _async_sample(
                conn, games, box, problem_games, args.async_sample, args.seed
            )
            print(
                f"\nThe real path (resolve_game_state against Postgres), {sample['games']:,} games "
                f"(every game with a disagreement and a random rest, seed {args.seed}):"
            )
            for key in sorted(k for k in sample if k != "games"):
                print(f"  {key:34s} {sample[key]:>6,}")
        ok = wrong_now == 0 and failures == 0
        print("\nVERDICT: " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("USAGE")[0])
    ap.add_argument(
        "--async-sample",
        type=int,
        default=0,
        help="Also resolve this many games through the real path (0 = skip).",
    )
    ap.add_argument("--seed", type=int, default=558, help="Seed of the random rest of the sample.")
    sys.exit(asyncio.run(_main(ap.parse_args())))


if __name__ == "__main__":
    main()
