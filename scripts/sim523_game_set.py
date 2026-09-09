"""
scripts/sim523_game_set.py — the BALANCED certifying game set (owner ruling 2026-09-09).

The certifying lane grades on a larger game set in which every team appears at
least once, chosen so that every graded statistic is expected near league
average. This tool builds it from FULL-DAY slates (fifteen regular-season
games on one date, every team on the field once), scores each candidate
date's players against the pool's own totals, and picks the best set of
``--slates`` dates from distinct seasons.

The score is the ACTOR-MATCHED expectation the fit probe introduced: for each
game, the two starting pitchers' own rows in the pitch pool (strikeouts,
walks and hit-by-pitches per plate appearance) and the eighteen starting
batters' own rows in the pitch and batted-ball pools (the same per-plate-
appearance rates plus singles, doubles, triples, home runs and reach-on-error
per ball in play), each weighted by the plate appearances a game gives them
(the pitcher side and the batter side averaged), against the pool's own
recency-weighted totals for the window. A date's expectation is the mean over
its fifteen games; a set's is the mean over its dates. The pick minimises the
largest relative deviation across the channels, ties broken by park balance
(the mean regressed run factor near 1.0).

Output: the game keys in a park-balanced order (extremes paired, as
``tests/acceptance/bands.py`` requires), each game's season and regressed park
run factor, and the expectation table — ready to paste into ``bands.py``.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim523_game_set.py --slates 3 --json-out /app/scripts/sim523_game_set.json
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncpg  # noqa: E402
import duckdb  # noqa: E402
import numpy as np  # noqa: E402

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
SEASONS = (2023, 2024, 2025, 2026)
CHANNELS = (
    "k_pa",
    "bb_pa",
    "hbp_pa",
    "single_bip",
    "double_bip",
    "triple_bip",
    "hr_bip",
    "roe_bip",
)
#: A game gives each starting batter about 4.3 plate appearances and its two
#: starters about 5.5 innings each (the bullpen takes the rest at the league mix).
_BATTER_PA = 4.3
_STARTER_SHARE = 0.6


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


async def _slates(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every regular-season date in the window with fifteen Final games, every
    home team distinct, and every game's starting lineups ingested."""
    rows = await conn.fetch(
        """
        WITH d AS (
            SELECT g.game_date, g.season, COUNT(*) AS n,
                   COUNT(DISTINCT g.home_team_id) AS home_n
            FROM raw.games g
            WHERE g.season = ANY($1::int[]) AND g.game_type = 'R' AND g.status ILIKE 'final%'
            GROUP BY 1, 2
            HAVING COUNT(*) = 15 AND COUNT(DISTINCT g.home_team_id) = 15
        )
        SELECT d.game_date, d.season, g.game_pk, g.venue_id, g.home_team_id, g.away_team_id
        FROM d JOIN raw.games g ON g.game_date = d.game_date AND g.game_type = 'R'
        WHERE EXISTS (SELECT 1 FROM raw.game_lineups l WHERE l.game_pk = g.game_pk AND l.is_starter)
        ORDER BY d.game_date, g.game_pk
        """,
        list(SEASONS),
    )
    by_date: dict[Any, list] = defaultdict(list)
    for r in rows:
        by_date[r["game_date"]].append(dict(r))
    out = []
    for date, games in by_date.items():
        if len(games) != 15:
            continue  # a game without lineups drops the date
        teams = {g["home_team_id"] for g in games} | {g["away_team_id"] for g in games}
        if len(teams) != 30:
            continue
        out.append({"date": date, "season": int(games[0]["season"]), "games": games})
    return out


async def _lineups(conn: asyncpg.Connection, game_pks: list[int]) -> dict[int, dict[str, list]]:
    rows = await conn.fetch(
        """
        SELECT game_pk, team_id, player_id, position_code, batting_order
        FROM raw.game_lineups WHERE game_pk = ANY($1::int[]) AND is_starter
        """,
        game_pks,
    )
    out: dict[int, dict[str, list]] = defaultdict(lambda: {"batters": [], "pitchers": []})
    for r in rows:
        if r["position_code"] == "P":
            out[int(r["game_pk"])]["pitchers"].append(int(r["player_id"]))
        elif r["batting_order"] is not None:
            out[int(r["game_pk"])]["batters"].append(int(r["player_id"]))
    return out


class PoolRates:
    """Per actor-season own rates from the pools (recency-weighted) and the
    pool's own totals for the window."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        sl = ", ".join(str(s) for s in SEASONS)
        pa_sql = f"""
            SELECT {{actor}} AS actor, season,
                   SUM(recency_weight) AS pa,
                   SUM(CASE WHEN outcome_type IN ('called_strike','swinging_strike') AND count_strikes = 2 THEN recency_weight ELSE 0 END) AS k,
                   SUM(CASE WHEN outcome_type = 'ball' AND count_balls = 3 THEN recency_weight ELSE 0 END) AS bb,
                   SUM(CASE WHEN outcome_type = 'hit_by_pitch' THEN recency_weight ELSE 0 END) AS hbp
            FROM sim.pitch_pool
            WHERE season IN ({sl}) AND (
                (outcome_type IN ('called_strike','swinging_strike') AND count_strikes = 2)
                OR (outcome_type = 'ball' AND count_balls = 3)
                OR outcome_type IN ('hit_by_pitch', 'in_play'))
            GROUP BY 1, 2
        """
        bip_sql = f"""
            SELECT {{actor}} AS actor, season, SUM(recency_weight) AS bip,
                   SUM(CASE WHEN events = 'single' THEN recency_weight ELSE 0 END) AS single,
                   SUM(CASE WHEN events = 'double' THEN recency_weight ELSE 0 END) AS double,
                   SUM(CASE WHEN events = 'triple' THEN recency_weight ELSE 0 END) AS triple,
                   SUM(CASE WHEN events = 'home_run' THEN recency_weight ELSE 0 END) AS hr,
                   SUM(CASE WHEN events = 'field_error' THEN recency_weight ELSE 0 END) AS roe
            FROM sim.outcome_pool WHERE season IN ({sl}) GROUP BY 1, 2
        """
        self.pa: dict[str, dict[tuple[int, int], np.ndarray]] = {}
        self.bip: dict[str, dict[tuple[int, int], np.ndarray]] = {}
        for actor in ("pitcher_id", "batter_id"):
            self.pa[actor] = {
                (int(a), int(s)): np.array(v, dtype=np.float64)
                for a, s, *v in con.execute(pa_sql.format(actor=actor)).fetchall()
            }
            self.bip[actor] = {
                (int(a), int(s)): np.array(v, dtype=np.float64)
                for a, s, *v in con.execute(bip_sql.format(actor=actor)).fetchall()
            }
        tot_pa = np.sum(list(self.pa["batter_id"].values()), axis=0)
        tot_bip = np.sum(list(self.bip["batter_id"].values()), axis=0)
        self.totals = {
            "k_pa": tot_pa[1] / tot_pa[0],
            "bb_pa": tot_pa[2] / tot_pa[0],
            "hbp_pa": tot_pa[3] / tot_pa[0],
            "single_bip": tot_bip[1] / tot_bip[0],
            "double_bip": tot_bip[2] / tot_bip[0],
            "triple_bip": tot_bip[3] / tot_bip[0],
            "hr_bip": tot_bip[4] / tot_bip[0],
            "roe_bip": tot_bip[5] / tot_bip[0],
        }

    def actor_rates(self, actor: str, key: tuple[int, int]) -> dict[str, float] | None:
        pa = self.pa[actor].get(key)
        bip = self.bip[actor].get(key)
        if pa is None or pa[0] < 60:
            return None
        out = {"k_pa": pa[1] / pa[0], "bb_pa": pa[2] / pa[0], "hbp_pa": pa[3] / pa[0], "_pa": pa[0]}
        if bip is not None and bip[0] >= 30:
            out.update(
                {
                    "single_bip": bip[1] / bip[0],
                    "double_bip": bip[2] / bip[0],
                    "triple_bip": bip[3] / bip[0],
                    "hr_bip": bip[4] / bip[0],
                    "roe_bip": bip[5] / bip[0],
                }
            )
        return out


def _game_expectation(
    rates: PoolRates, season: int, lineup: dict[str, list]
) -> dict[str, float] | None:
    """The game's actor-matched expectation per channel: the batters' own rates
    (equal plate appearances) and the starters' own rates (the starter share of
    the game's plate appearances, the rest at the pool totals), averaged."""
    bat = [rates.actor_rates("batter_id", (b, season)) for b in lineup["batters"]]
    bat = [r for r in bat if r is not None]
    pit = [rates.actor_rates("pitcher_id", (p, season)) for p in lineup["pitchers"]]
    pit = [r for r in pit if r is not None]
    if len(bat) < 12 or not pit:
        return None
    out: dict[str, float] = {}
    for ch in CHANNELS:
        b_vals = [r[ch] for r in bat if ch in r]
        p_vals = [r[ch] for r in pit if ch in r]
        b_side = float(np.mean(b_vals)) if b_vals else rates.totals[ch]
        p_side = (
            _STARTER_SHARE * float(np.mean(p_vals)) + (1.0 - _STARTER_SHARE) * rates.totals[ch]
            if p_vals
            else rates.totals[ch]
        )
        out[ch] = 0.5 * (b_side + p_side)
    return out


def _park_factors(con: duckdb.DuckDBPyConnection) -> dict[tuple[int, int], float]:
    return {
        (int(v), int(s)): float(f)
        for v, s, f in con.execute(
            "SELECT venue_id, season, regressed_factor FROM derived.park_factors "
            "WHERE factor_type = 'R' AND season IN (2023, 2024, 2025, 2026)"
        ).fetchall()
    }


def _balanced_order(games: list[dict]) -> list[dict]:
    """Extremes paired, each pair low-then-high (the bands.py rule)."""
    srt = sorted(games, key=lambda g: g["park"])
    out: list[dict] = []
    lo, hi = 0, len(srt) - 1
    while lo <= hi:
        if lo == hi:
            out.append(srt[lo])
        else:
            out.append(srt[lo])
            out.append(srt[hi])
        lo += 1
        hi -= 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slates", type=int, default=3)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--max-dev", type=float, default=0.03, help="report sets within this deviation")
    args = ap.parse_args()

    async def _load() -> tuple[list[dict], dict[int, dict[str, list]]]:
        conn = await asyncpg.connect(_dsn())
        try:
            slates = await _slates(conn)
            pks = [g["game_pk"] for s in slates for g in s["games"]]
            lineups = await _lineups(conn, pks)
        finally:
            await conn.close()
        return slates, lineups

    slates, lineups = asyncio.run(_load())
    print(
        f"candidate full-day slates with lineups: {len(slates)} "
        f"({', '.join(f'{s}: {sum(1 for x in slates if x["season"] == s)}' for s in SEASONS)})"
    )
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rates = PoolRates(con)
        parks = _park_factors(con)
    finally:
        con.close()
    print("pool totals:", {k: round(v, 4) for k, v in rates.totals.items()})

    scored = []
    for s in slates:
        exps = []
        for g in s["games"]:
            e = _game_expectation(
                rates, s["season"], lineups.get(int(g["game_pk"]), {"batters": [], "pitchers": []})
            )
            if e is None:
                break
            g["park"] = parks.get((int(g["venue_id"]), s["season"]), float("nan"))
            g["expectation"] = e
            exps.append(e)
        if len(exps) != 15 or any(np.isnan(g["park"]) for g in s["games"]):
            continue
        s["expectation"] = {ch: float(np.mean([e[ch] for e in exps])) for ch in CHANNELS}
        s["park_mean"] = float(np.mean([g["park"] for g in s["games"]]))
        scored.append(s)
    print(f"scored slates: {len(scored)}")

    best = None
    per_season = defaultdict(list)
    for s in scored:
        per_season[s["season"]].append(s)
    seasons_avail = [s for s in SEASONS if per_season[s]]
    for combo_seasons in itertools.combinations(
        seasons_avail, min(args.slates, len(seasons_avail))
    ):
        pools = [per_season[s] for s in combo_seasons]
        for combo in itertools.product(*pools):
            exp = {ch: float(np.mean([s["expectation"][ch] for s in combo])) for ch in CHANNELS}
            dev = {ch: exp[ch] / rates.totals[ch] - 1.0 for ch in CHANNELS}
            worst = max(abs(v) for v in dev.values())
            park = float(np.mean([s["park_mean"] for s in combo]))
            key = (worst, abs(park - 1.0))
            if best is None or key < best[0]:
                best = (key, combo, exp, dev, park)
    assert best is not None, "no candidate set"
    key, combo, exp, dev, park = best
    games = [dict(g, season=s["season"], date=str(s["date"])) for s in combo for g in s["games"]]
    order = _balanced_order(games)
    print(
        f"\n=== the set: {len(games)} games, dates {[str(s['date']) for s in combo]}, worst deviation "
        f"{key[0] * 100:.2f}%, mean park factor {park:.4f} ==="
    )
    for ch in CHANNELS:
        print(
            f"  {ch:>10}: expected {exp[ch]:.5f} vs pool {rates.totals[ch]:.5f} ({dev[ch] * 100:+.2f}%)"
        )
    print("  balanced order (game_pk, season, park):")
    for g in order:
        print(f"    {g['game_pk']},  # {g['park']:.4f}  {g['season']}  venue {g['venue_id']}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "dates": [str(s["date"]) for s in combo],
                    "worst_deviation": key[0],
                    "park_mean": park,
                    "expectation": exp,
                    "pool_totals": rates.totals,
                    "deviation": dev,
                    "order": [
                        {
                            "game_pk": int(g["game_pk"]),
                            "season": g["season"],
                            "venue_id": int(g["venue_id"]),
                            "park": g["park"],
                            "date": g["date"],
                        }
                        for g in order
                    ],
                },
                fh,
                default=float,
                indent=1,
            )
        print(f"  wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
