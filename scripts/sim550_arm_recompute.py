"""
scripts/sim550_arm_recompute.py — the outfield arm block (SIM-550), run-book step 2:
the block rebuilt from our own advancement pool, the league rows, and the verify block.

What it does, in order (the app must be STOPPED — a DuckDB write; the forkserver's
writer lock, SIM-524):

  1. NO migration. The six arm columns already exist on
     ``derived.fielder_season_metrics``; this ticket changes what they hold, not
     the schema.
  2. writes the arm block NULL on every fielder row of the requested seasons
     (as the nightly aggregator does — the fill writes only rows with a pool
     triple, so a row the old runner-view join filled would otherwise keep
     its figures), then fills it from
     ``sim.advancement_opportunity_pool`` through the computor's own step
     (``_fill_outfield_arm_block``, no cutoff): per (fielder, position, season)
     the chances to advance on a ball he fielded, the holds, the runners thrown
     out and the situation-adjusted prevention. The fill reads only DuckDB, so
     the script opens the database itself and never attaches Postgres. Seconds.
  3. recomputes ``derived.league_averages`` for the seasons — the ``fielder_LF`` /
     ``fielder_CF`` / ``fielder_RF`` rows gain the three arm keys the fielder
     model shrinks toward (``arm_advancement_prevention``, ``arm_thrown_out_rate``,
     ``arm_strength``).
  4. verifies, per season: the outfield rows with a block (about 560 to 650 of
     600; the floor is 400 for a full season, 150 for 2020, 300 for the season in
     progress) and the chances quartiles (about 7 / 25 / 81 — the median must sit
     between 10 and 60); the league figures (chances-weighted hold 0.78 to 0.81,
     thrown-out share of attempts 0.04 to 0.06, prevention mean within 0.01 of 0);
     the block exists exactly where the pool says the player fielded a chance at
     that position (so a catcher or a designated hitter has none); the five 2024
     center fielders with the most chances value for value against a direct pool
     query; ``of_arm_runs`` NULL on every row; the arm-strength coverage unchanged
     (above 60% of outfield rows from 2023); the three arm keys on the outfield
     league rows; one cutoff date per table.
  5. THE CROSS-CHECK (``--no-cross-check`` to skip): pulls Savant's baserunning
     board in its FIELDER view (``type=Fld``, ``n=1``) for 2024 — one HTTP request —
     and correlates, per player with the three outfield positions summed, the
     chances (r at least 0.95; measured 0.98) and the thrown-out rate (r at least
     0.7; measured 0.78) for players with 50 or more chances in both. The board is
     not loaded; the registry's ``baserunning`` entry stays pinned to the runner
     view on purpose.

The paired accuracy read does NOT run for this change (owner ruling 2026-09-16:
every change lands ON at its best-known default; the draw weights are fitted
together in one designed experiment later).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim550_arm_recompute.py \\
        --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026

Run book: ``docs/audit/2026-09-16-sim550-outfield-arm-block-plan.md`` §8. After
this: ``make calibrate`` (then write the win-probability curve back — the SIM-427
trap), copy ``sigma_of_arm`` and the three reliability weights into the module
defaults, and rebuild the three outfield matrices
(``python -m pipeline.batch.engine_artifacts --what actors_sim --matrix fielder_LF
--matrix fielder_CF --matrix fielder_RF``).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

from pipeline.batch.player_profile_computor import (  # noqa: E402
    LeagueAverageProfiles,
    PlayerProfileComputor,
    _table_exists,
)

log = logging.getLogger("sim550_arm_recompute")

DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
#: The pool holds every season since 2017; the fill covers them all by default.
DEFAULT_SEASONS: tuple[int, ...] = tuple(range(2017, 2027))

#: The three outfield positions: the fielder table's name and the pool's number.
OUTFIELD: dict[str, int] = {"LF": 7, "CF": 8, "RF": 9}
OUTFIELD_SQL = "('LF', 'CF', 'RF')"

#: The least outfield rows with a block a season may carry (plan §5.4: about
#: 560 to 650 of 600; 2020 was a 60-game season; the season in progress is
#: still filling).
BLOCK_FLOOR_FULL = 400
BLOCK_FLOOR_2020 = 150
BLOCK_FLOOR_CURRENT = 300
#: The median chances per outfielder-position-season (measured 25; plan §2.1).
MEDIAN_CHANCES_BAND = (10.0, 60.0)
#: The league figures, chances-weighted (measured 0.795 / 0.049 in 2024).
HOLD_BAND = (0.78, 0.81)
THROWN_OUT_SHARE_BAND = (0.04, 0.06)
PREVENTION_TOLERANCE = 0.01
#: The arm-strength board starts with the 2023 tracking; its coverage is about
#: 75% of outfield rows and this ticket does not touch it.
FIRST_ARM_STRENGTH_SEASON = 2023
ARM_STRENGTH_COVERAGE_FLOOR = 0.6
#: The three keys the fielder model shrinks toward on the outfield league rows.
LEAGUE_ARM_KEYS = ("arm_advancement_prevention", "arm_thrown_out_rate", "arm_strength")
#: The named check: the center fielders with the most chances in this season.
NAMED_SEASON = 2024
NAMED_TOP = 5
RATE_TOLERANCE = 1e-6
#: The cross-check against Savant's fielder view (plan §2.5).
CROSS_CHECK_SEASON = 2024
CROSS_CHECK_MIN_CHANCES = 50
CROSS_CHECK_MIN_PLAYERS = 30
CROSS_CHECK_R_CHANCES = 0.95
CROSS_CHECK_R_THROWN_OUT = 0.7
#: The fielder view's column names, by role, in the order the script tries them.
#: The runner view names them n_opp_xb / n_att_xb / n_out; the fielder view is
#: expected to reuse them. The script logs the header list so a mismatch shows.
FIELDER_VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "player": ("entity_id", "player_id", "fielder_id"),
    "chances": ("n_opp_xb", "n_opp", "n_opportunities", "opportunities"),
    "attempts": ("n_att_xb", "n_att", "attempts"),
    "thrown_out": ("n_out", "n_thrown_out", "thrown_out"),
}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _connect_writable(duckdb_path: str) -> duckdb.DuckDBPyConnection:
    try:
        return duckdb.connect(duckdb_path)
    except duckdb.IOException as exc:
        raise SystemExit(
            f"cannot open {duckdb_path} for writing ({exc}). The app holds the DuckDB "
            "writer lock (SIM-524): stop it first — docker compose stop app."
        ) from exc


def _season_list(seasons: list[int]) -> str:
    return ", ".join(str(int(s)) for s in seasons)


def block_floor(season: int, today: date) -> int:
    """The least outfield rows with a block ``season`` may carry."""
    if season == 2020:
        return BLOCK_FLOOR_2020
    if season >= today.year:
        return BLOCK_FLOOR_CURRENT
    return BLOCK_FLOOR_FULL


# ---------------------------------------------------------------------------
# The direct pool aggregation — the fill's own arithmetic, written a second time
# ---------------------------------------------------------------------------


def _pool_block_sql(seasons: list[int], *, position_num: int | None = None) -> str:
    """The arm block computed straight from the pool: per (fielder, position,
    season) the chances, attempts, runners thrown out and expected attempts.
    The expectation is the pool's own attempt rate per season x decision x
    outs x POSITION cell over every outfield-fielded chance — the fill's
    rule (runners challenge a left fielder less on the same decision, so a
    position-blind cell would give every left fielder a positive prevention;
    the named check reads value for value, so this cell must stay the fill's)."""
    pos_filter = f"AND p.fielder_pos = {int(position_num)}" if position_num is not None else ""
    return f"""
        WITH pool AS (
            SELECT season, scenario, from_base, target_base, outs,
                   fielder_id, fielder_pos, attempted, safe
            FROM sim.advancement_opportunity_pool
            WHERE season IN ({_season_list(seasons)})
              AND fielder_pos IN (7, 8, 9) AND fielder_id IS NOT NULL
        ),
        expected AS (
            SELECT season, scenario, from_base, target_base, outs, fielder_pos,
                   AVG(attempted::INT) AS rate
            FROM pool GROUP BY 1, 2, 3, 4, 5, 6
        )
        SELECT p.fielder_id AS player_id,
               CASE p.fielder_pos WHEN 7 THEN 'LF' WHEN 8 THEN 'CF' ELSE 'RF' END AS position,
               p.season AS season,
               COUNT(*) AS chances,
               SUM(p.attempted::INT) AS attempts,
               SUM(CASE WHEN p.attempted AND NOT p.safe THEN 1 ELSE 0 END) AS thrown_out,
               SUM(e.rate) AS expected_attempts
        FROM pool p
        JOIN expected e USING (season, scenario, from_base, target_base, outs, fielder_pos)
        WHERE 1 = 1 {pos_filter}
        GROUP BY 1, 2, 3
    """


# ---------------------------------------------------------------------------
# The verify block, one check per function; each returns the problems it found
# ---------------------------------------------------------------------------


def check_block_coverage(con: Any, seasons: list[int], today: date | None = None) -> list[str]:
    """Per season: the outfield rows with a block against the floor, and the
    chances quartiles (the median must sit inside its band)."""
    today = today or date.today()
    problems: list[str] = []
    rows = con.execute(
        "SELECT season, COUNT(*), COUNT(arm_opportunities), "
        "quantile_cont(arm_opportunities, 0.25), quantile_cont(arm_opportunities, 0.5), "
        "quantile_cont(arm_opportunities, 0.75) "
        f"FROM derived.fielder_season_metrics WHERE position IN {OUTFIELD_SQL} "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    seen: set[int] = set()
    for season, n_rows, n_block, q1, q2, q3 in rows:
        season = int(season)
        seen.add(season)
        log.info(
            "  outfield %s: %d rows, %d with a block; chances quartiles %s / %s / %s",
            season,
            n_rows,
            n_block,
            q1,
            q2,
            q3,
        )
        if season not in seasons:
            continue
        floor = block_floor(season, today)
        if n_block < floor:
            problems.append(
                f"season {season}: only {n_block} outfield rows carry a block (floor {floor}) "
                "— the fill missed, or the pool lacks the season"
            )
        if n_block and not (MEDIAN_CHANCES_BAND[0] <= float(q2) <= MEDIAN_CHANCES_BAND[1]):
            problems.append(
                f"season {season}: the median chances per block is {q2}, outside "
                f"{MEDIAN_CHANCES_BAND} — the block is not per fielder x position, or "
                "the pool is not per chance"
            )
    for season in seasons:
        if season not in seen:
            problems.append(f"season {season}: no outfield fielder rows at all")
    return problems


def check_league_figures(con: Any, seasons: list[int]) -> list[str]:
    """Per season, over the outfield rows with a block: the chances-weighted
    hold rate, the thrown-out share of attempts and the prevention mean."""
    problems: list[str] = []
    rows = con.execute(
        "SELECT season, SUM(arm_opportunities), SUM(arm_holds), SUM(arm_assists), "
        "SUM(arm_advancement_prevention * arm_opportunities) / SUM(arm_opportunities), "
        "AVG(arm_advancement_prevention) "
        f"FROM derived.fielder_season_metrics WHERE position IN {OUTFIELD_SQL} "
        "AND arm_opportunities IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for season, chances, holds, assists, prevention_weighted, prevention_plain in rows:
        season = int(season)
        chances = int(chances or 0)
        holds = int(holds or 0)
        assists = int(assists or 0)
        attempts = chances - holds
        hold = holds / chances if chances else float("nan")
        share = assists / attempts if attempts else float("nan")
        log.info(
            "  league %s: chances %d, hold %.4f, thrown-out share of attempts %.4f, "
            "prevention mean %.5f (weighted) / %.5f (plain)",
            season,
            chances,
            hold,
            share,
            float(prevention_weighted or 0.0),
            float(prevention_plain or 0.0),
        )
        if season not in seasons:
            continue
        if not (HOLD_BAND[0] <= hold <= HOLD_BAND[1]):
            problems.append(
                f"season {season}: the chances-weighted hold rate is {hold:.4f}, outside "
                f"{HOLD_BAND} — the holds are not chances minus attempts"
            )
        if not (THROWN_OUT_SHARE_BAND[0] <= share <= THROWN_OUT_SHARE_BAND[1]):
            problems.append(
                f"season {season}: the thrown-out share of attempts is {share:.4f}, outside "
                f"{THROWN_OUT_SHARE_BAND} — the assists are not the attempts that ended safe = FALSE"
            )
        if prevention_weighted is None or abs(float(prevention_weighted)) > PREVENTION_TOLERANCE:
            problems.append(
                f"season {season}: the chances-weighted prevention mean is {prevention_weighted}, "
                f"more than {PREVENTION_TOLERANCE} from 0 — the expectation cells were not "
                "computed over the same rows the block sums"
            )
    # Per position too: the expectation cell carries the position (the
    # 2026-09-17 review), so each position's chances-weighted mean is 0 by
    # construction. A position-blind fill leaves LF about +0.04 and CF / RF
    # about -0.02 here while the pooled mean above still reads 0.
    by_position = con.execute(
        "SELECT season, position, "
        "SUM(arm_advancement_prevention * arm_opportunities) / SUM(arm_opportunities) "
        f"FROM derived.fielder_season_metrics WHERE position IN {OUTFIELD_SQL} "
        "AND arm_opportunities IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    for season, position, prevention_weighted in by_position:
        if int(season) not in seasons:
            continue
        if prevention_weighted is None or abs(float(prevention_weighted)) > PREVENTION_TOLERANCE:
            problems.append(
                f"season {int(season)} {position}: the chances-weighted prevention mean is "
                f"{prevention_weighted}, more than {PREVENTION_TOLERANCE} from 0 — the "
                "expectation cell does not carry the position"
            )
    return problems


def check_block_only_where_fielded(con: Any, seasons: list[int]) -> list[str]:
    """The block exists exactly where the pool says the player fielded a
    chance at that position. A catcher's row or a designated hitter's has no
    outfield chance, so it carries no block; an outfield row with a chance in
    the pool carries one."""
    problems: list[str] = []
    off_position = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        f"WHERE position NOT IN {OUTFIELD_SQL} AND arm_opportunities IS NOT NULL"
    ).fetchone()[0]
    if off_position:
        problems.append(
            f"{off_position} non-outfield rows (catcher, infield, pitcher) carry a block — "
            "the fill joined on the wrong position"
        )
    fielded = f"""
        SELECT DISTINCT fielder_id AS player_id,
               CASE fielder_pos WHEN 7 THEN 'LF' WHEN 8 THEN 'CF' ELSE 'RF' END AS position,
               season
        FROM sim.advancement_opportunity_pool
        WHERE season IN ({_season_list(seasons)})
          AND fielder_pos IN (7, 8, 9) AND fielder_id IS NOT NULL
    """
    without_chance = con.execute(
        f"""
        SELECT COUNT(*) FROM derived.fielder_season_metrics f
        WHERE f.season IN ({_season_list(seasons)}) AND f.position IN {OUTFIELD_SQL}
          AND f.arm_opportunities IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ({fielded}) x
              WHERE x.player_id = f.player_id AND x.position = f.position
                AND x.season = f.season)
        """
    ).fetchone()[0]
    if without_chance:
        problems.append(
            f"{without_chance} outfield rows carry a block although the pool holds no chance "
            "fielded by that player at that position — the block came from somewhere else"
        )
    unfilled = con.execute(
        f"""
        SELECT COUNT(*) FROM ({fielded}) x
        JOIN derived.fielder_season_metrics f
          ON f.player_id = x.player_id AND f.position = x.position AND f.season = x.season
        WHERE f.arm_opportunities IS NULL
        """
    ).fetchone()[0]
    if unfilled:
        problems.append(
            f"{unfilled} outfield rows have chances in the pool but no block — the fill "
            "skipped them"
        )
    catchers_only = con.execute(
        """
        SELECT COUNT(*) FROM derived.fielder_season_metrics f
        WHERE f.arm_opportunities IS NOT NULL
          AND f.player_id IN (
              SELECT player_id FROM derived.fielder_season_metrics
              GROUP BY player_id HAVING BOOL_AND(position = 'C'))
        """
    ).fetchone()[0]
    if catchers_only:
        problems.append(f"{catchers_only} rows of catcher-only players carry a block")
    log.info(
        "  the block sits only on outfield rows with a pool chance (off-position %d, "
        "without a chance %d, unfilled %d, catcher-only %d)",
        off_position,
        without_chance,
        unfilled,
        catchers_only,
    )
    return problems


def check_named_center_fielders(con: Any, season: int = NAMED_SEASON) -> list[str]:
    """The center fielders with the most chances in ``season``: every column
    of the block against the direct pool aggregation, value for value. Both
    sides are computed here; no id is hard-coded."""
    problems: list[str] = []
    top = con.execute(
        f"SELECT player_id, chances, attempts, thrown_out, expected_attempts "
        f"FROM ({_pool_block_sql([season], position_num=OUTFIELD['CF'])}) "
        f"ORDER BY chances DESC, player_id LIMIT {NAMED_TOP}"
    ).fetchall()
    if not top:
        return [f"season {season}: the pool holds no center-field chance"]
    for player_id, chances, attempts, thrown_out, expected in top:
        row = con.execute(
            "SELECT arm_opportunities, arm_holds, arm_hold_rate, arm_assists, "
            "arm_thrown_out_rate, arm_advancement_prevention "
            "FROM derived.fielder_season_metrics "
            "WHERE player_id = ? AND position = 'CF' AND season = ?",
            [int(player_id), int(season)],
        ).fetchone()
        want = {
            "arm_opportunities": int(chances),
            "arm_holds": int(chances) - int(attempts),
            "arm_hold_rate": 1.0 - int(attempts) / int(chances),
            "arm_assists": int(thrown_out),
            "arm_thrown_out_rate": (int(thrown_out) / int(attempts)) if int(attempts) else None,
            "arm_advancement_prevention": (float(expected) - int(attempts)) / int(chances),
        }
        if row is None:
            problems.append(
                f"season {season}: center fielder {player_id} ({chances} chances in the pool) "
                "has no CF row in derived.fielder_season_metrics"
            )
            continue
        got = dict(zip(want, row, strict=True))
        log.info("  CF %s/%s: block %s", player_id, season, got)
        for k, w in want.items():
            g = got[k]
            if w is None or g is None:
                ok = w is None and g is None
            else:
                ok = abs(float(g) - float(w)) <= RATE_TOLERANCE
            if not ok:
                problems.append(f"season {season}: CF {player_id} {k} = {g}, the pool says {w}")
    return problems


def check_of_arm_runs_null(con: Any, seasons: list[int]) -> list[str]:
    """``of_arm_runs`` is NULL on every row of the requested seasons (the
    model does not read it; the runner-view figure is gone). Rows of other
    seasons are logged, not gated: a partial run leaves them as they were."""
    problems: list[str] = []
    inside = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        f"WHERE season IN ({_season_list(seasons)}) AND of_arm_runs IS NOT NULL"
    ).fetchone()[0]
    outside = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        f"WHERE season NOT IN ({_season_list(seasons)}) AND of_arm_runs IS NOT NULL"
    ).fetchone()[0]
    log.info("  of_arm_runs non-NULL: %d in the requested seasons, %d outside", inside, outside)
    if inside:
        problems.append(
            f"{inside} rows in the requested seasons still carry of_arm_runs — the fill did "
            "not write it NULL"
        )
    return problems


def check_arm_strength_coverage(
    con: Any, seasons: list[int], today: date | None = None
) -> list[str]:
    """The throw velocity keeps its source; its coverage stays above the floor
    on every completed season the arm-strength board covers."""
    today = today or date.today()
    problems: list[str] = []
    rows = con.execute(
        "SELECT season, COUNT(arm_strength) * 1.0 / COUNT(*) "
        f"FROM derived.fielder_season_metrics WHERE position IN {OUTFIELD_SQL} "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for season, share in rows:
        season = int(season)
        share = float(share or 0.0)
        log.info("  arm_strength coverage %s: %.3f of outfield rows", season, share)
        if (
            season in seasons
            and FIRST_ARM_STRENGTH_SEASON <= season < today.year
            and share < ARM_STRENGTH_COVERAGE_FLOOR
        ):
            problems.append(
                f"season {season}: arm_strength covers {share:.3f} of outfield rows (floor "
                f"{ARM_STRENGTH_COVERAGE_FLOOR}) — the aggregator lost the arm-strength join"
            )
    return problems


def check_league_rows(con: Any, seasons: list[int]) -> list[str]:
    """The three arm keys on the ``fielder_LF`` / ``fielder_CF`` / ``fielder_RF``
    league rows for every requested season with data. The prevention and the
    thrown-out rate must carry a value on every such season; the throw velocity
    only from the first arm-strength season."""
    problems: list[str] = []
    if not _table_exists(con, "derived", "league_averages"):
        return ["derived.league_averages is absent — the league writer did not run"]
    for pos in OUTFIELD:
        with_data = {
            int(s)
            for (s,) in con.execute(
                "SELECT DISTINCT season FROM derived.fielder_season_metrics "
                "WHERE position = ? AND below_minimum_sample = FALSE",
                [pos],
            ).fetchall()
        }
        rows = con.execute(
            "SELECT season, profile_json FROM derived.league_averages "
            "WHERE entity_type = ? ORDER BY season",
            [f"fielder_{pos}"],
        ).fetchall()
        have = {int(s): (json.loads(pj) if isinstance(pj, str) else pj) for s, pj in rows}
        for season in seasons:
            if season not in with_data:
                continue
            d = have.get(season)
            if d is None:
                problems.append(f"no 'fielder_{pos}' league-average row for season {season}")
                continue
            vals = {k: d.get(k) for k in LEAGUE_ARM_KEYS}
            log.info("  league fielder_%s %s: %s", pos, season, vals)
            missing_keys = [k for k in LEAGUE_ARM_KEYS if k not in d]
            if missing_keys:
                problems.append(
                    f"'fielder_{pos}' league row {season} lacks the keys {missing_keys} — "
                    "the shrinkage has no target"
                )
            for k in ("arm_advancement_prevention", "arm_thrown_out_rate"):
                if k in d and d[k] is None:
                    problems.append(f"'fielder_{pos}' league row {season}: {k} is null")
            if (
                season >= FIRST_ARM_STRENGTH_SEASON
                and "arm_strength" in d
                and d["arm_strength"] is None
            ):
                problems.append(f"'fielder_{pos}' league row {season}: arm_strength is null")
    return problems


def check_one_asof(con: Any) -> list[str]:
    """One cutoff date per table: the engines refuse a mixed set at boot."""
    problems: list[str] = []
    for table in ("derived.fielder_season_metrics",):
        stamps = con.execute(
            f"SELECT COUNT(DISTINCT COALESCE(asof_date::VARCHAR, 'NULL')) FROM {table}"
        ).fetchone()[0]
        log.info("  %s: %d distinct asof_date stamp(s)", table, stamps)
        if stamps != 1:
            problems.append(
                f"{table}: {stamps} distinct asof_date stamps — the engines refuse a mixed "
                "set at boot; rebuild every season at one date"
            )
    return problems


# ---------------------------------------------------------------------------
# The cross-check: Savant's fielder view against the block
# ---------------------------------------------------------------------------


def pearson(x: list[float], y: list[float]) -> float:
    """Pearson's r; NaN when either side has no spread."""
    n = len(x)
    if n < 2 or n != len(y):
        return float("nan")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0.0 or syy <= 0.0:
        return float("nan")
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    return sxy / math.sqrt(sxx * syy)


def resolve_fielder_view_columns(headers: list[str]) -> dict[str, str]:
    """Map each role (player, chances, attempts, thrown_out) to the header
    the fielder view carries. Raise with the header list when one is absent."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for role, names in FIELDER_VIEW_COLUMNS.items():
        hit = next((n for n in names if n in headers), None)
        if hit is None:
            missing.append(role)
        else:
            found[role] = hit
    if missing:
        tried = {r: FIELDER_VIEW_COLUMNS[r] for r in missing}
        raise KeyError(
            f"the fielder view lacks a column for {missing} (tried {tried}); headers: {headers}"
        )
    return found


def savant_fielder_view(
    season: int, fetcher: Callable[[str], str] | None = None
) -> tuple[list[str], list[dict[str, str]]]:
    """Pull the baserunning board's FIELDER view (``type=Fld``, ``n=1``) for
    ``season`` and return (headers, rows). The registry's ``baserunning`` entry
    stays the runner view; the script builds a modified copy of it here."""
    from pipeline.etl.savant_boards import BOARDS
    from pipeline.etl.savant_loader import build_url, check_row_seasons, fetch_csv, parse_rows

    runner_board = BOARDS["baserunning"]
    board = dataclasses.replace(
        runner_board,
        name="baserunning_fielder_view",
        extra={**runner_board.extra, "type": "Fld", "n": "1"},
    )
    url = build_url(board, season)
    log.info("  cross-check: fetching %s", url)
    body = (fetcher or fetch_csv)(url)
    rows = parse_rows(body)
    headers = list(rows[0].keys()) if rows else []
    log.info("  cross-check: %d rows; headers %s", len(rows), headers)
    check_row_seasons(board, rows, season)
    return headers, rows


def cross_check_savant(
    con: Any, season: int = CROSS_CHECK_SEASON, *, fetcher: Callable[[str], str] | None = None
) -> list[str]:
    """Correlate the block (positions summed per player) with Savant's fielder
    view: the chances and the thrown-out rate, over players with
    ``CROSS_CHECK_MIN_CHANCES`` or more chances in both. The two sources count
    chances differently (the pool keeps the batter's stretch and the tag from
    first), so the read is a correlation, not an equality."""
    problems: list[str] = []
    try:
        headers, rows = savant_fielder_view(season, fetcher)
        cols = resolve_fielder_view_columns(headers)
    except Exception as exc:  # noqa: BLE001 — every failure is a loud verify problem
        return [f"cross-check: the fielder view could not be read: {exc}"]
    savant: dict[int, tuple[float, float, float]] = {}
    for r in rows:
        try:
            pid = int(float(r[cols["player"]]))
            chances = float(r[cols["chances"]] or 0)
            attempts = float(r[cols["attempts"]] or 0)
            outs = float(r[cols["thrown_out"]] or 0)
        except (TypeError, ValueError):
            continue
        savant[pid] = (chances, attempts, outs)
    ours = {
        int(pid): (float(chances), float(attempts), float(outs))
        for pid, chances, attempts, outs in con.execute(
            "SELECT player_id, SUM(arm_opportunities), SUM(arm_opportunities - arm_holds), "
            "SUM(arm_assists) FROM derived.fielder_season_metrics "
            f"WHERE season = ? AND position IN {OUTFIELD_SQL} AND arm_opportunities IS NOT NULL "
            "GROUP BY 1",
            [int(season)],
        ).fetchall()
    }
    both = [
        pid
        for pid in ours
        if pid in savant
        and ours[pid][0] >= CROSS_CHECK_MIN_CHANCES
        and savant[pid][0] >= CROSS_CHECK_MIN_CHANCES
    ]
    r_chances = pearson([ours[p][0] for p in both], [savant[p][0] for p in both])
    rate_ids = [p for p in both if ours[p][1] > 0 and savant[p][1] > 0]
    r_rate = pearson(
        [ours[p][2] / ours[p][1] for p in rate_ids],
        [savant[p][2] / savant[p][1] for p in rate_ids],
    )
    log.info(
        "  cross-check %s: %d Savant fielders, %d of ours, %d with >= %d chances in both; "
        "r(chances) = %.3f, r(thrown-out rate) = %.3f over %d",
        season,
        len(savant),
        len(ours),
        len(both),
        CROSS_CHECK_MIN_CHANCES,
        r_chances,
        r_rate,
        len(rate_ids),
    )
    if len(both) < CROSS_CHECK_MIN_PLAYERS:
        problems.append(
            f"cross-check: only {len(both)} player(s) have >= {CROSS_CHECK_MIN_CHANCES} chances "
            f"in both sources (need {CROSS_CHECK_MIN_PLAYERS}) — the player column or the "
            "season did not match"
        )
    if not (r_chances >= CROSS_CHECK_R_CHANCES):
        problems.append(
            f"cross-check: r(chances) = {r_chances:.3f} against Savant's fielder view, below "
            f"{CROSS_CHECK_R_CHANCES} — the fielder attribution is wrong (measured 0.98)"
        )
    if not (r_rate >= CROSS_CHECK_R_THROWN_OUT):
        problems.append(
            f"cross-check: r(thrown-out rate) = {r_rate:.3f} against Savant's fielder view, "
            f"below {CROSS_CHECK_R_THROWN_OUT} — the assists are not the fielder's "
            "(measured 0.78)"
        )
    return problems


# ---------------------------------------------------------------------------
# verify + main
# ---------------------------------------------------------------------------


def verify(
    duckdb_path: str,
    seasons: list[int],
    *,
    skip_named_checks: bool = False,
    cross_check: bool = True,
    fetcher: Callable[[str], str] | None = None,
) -> None:
    con = duckdb.connect(duckdb_path, read_only=True)
    problems: list[str] = []
    try:
        if not _table_exists(con, "sim", "advancement_opportunity_pool"):
            problems.append("sim.advancement_opportunity_pool is absent — nothing to fill from")
        else:
            problems += check_block_coverage(con, seasons)
            problems += check_league_figures(con, seasons)
            problems += check_block_only_where_fielded(con, seasons)
            if not skip_named_checks and NAMED_SEASON in seasons:
                problems += check_named_center_fielders(con, NAMED_SEASON)
            problems += check_of_arm_runs_null(con, seasons)
            problems += check_arm_strength_coverage(con, seasons)
            problems += check_league_rows(con, seasons)
            problems += check_one_asof(con)
            if cross_check and CROSS_CHECK_SEASON in seasons:
                problems += cross_check_savant(con, CROSS_CHECK_SEASON, fetcher=fetcher)
    finally:
        con.close()
    if problems:
        for p in problems:
            log.error("VERIFY: %s", p)
        raise SystemExit(f"{len(problems)} verification problem(s); see the log")
    log.info("verify: every check passed")


#: The seven columns the nightly aggregator writes NULL before the fill step
#: runs: the six arm columns and the run value.
ARM_BLOCK_COLUMNS: tuple[str, ...] = (
    "arm_opportunities",
    "arm_holds",
    "arm_hold_rate",
    "arm_assists",
    "arm_thrown_out_rate",
    "arm_advancement_prevention",
    "of_arm_runs",
)


def clear_arm_block(con: Any, seasons: list[int]) -> int:
    """Write the arm block NULL on every fielder row of ``seasons``, as the
    nightly aggregator does before the fill step runs. The fill's own write
    is a matched UPDATE (it touches only rows with a pool triple), so a row
    the old runner-view join filled but the pool never touches would keep
    its stale figures and ``of_arm_runs``; the live table holds 23 such
    rows, and the verify block reds on them. The fill step resets the block
    itself since the 2026-09-17 review; the script clears it too, so the run
    book does not depend on which computor version the image carries.
    Returns the number of rows that carried a block."""
    stale = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        f"WHERE season IN ({_season_list(seasons)}) AND ("
        + " OR ".join(f"{c} IS NOT NULL" for c in ARM_BLOCK_COLUMNS)
        + ")"
    ).fetchone()[0]
    con.execute(
        "UPDATE derived.fielder_season_metrics SET "
        + ", ".join(f"{c} = NULL" for c in ARM_BLOCK_COLUMNS)
        + f" WHERE season IN ({_season_list(seasons)})"
    )
    log.info("  the arm block cleared on %d rows before the fill", int(stale))
    return int(stale)


def fill_arm_block(duckdb_path: str, seasons: list[int], dsn: str = "") -> None:
    """Clear the block over ``seasons``, then run the computor's fill on
    ``duckdb_path``. The step reads only DuckDB, so the script opens the
    database itself and sets it on the computor; the Postgres DSN is recorded,
    never attached."""
    con = _connect_writable(duckdb_path)
    computor = PlayerProfileComputor(pg_dsn=dsn, duckdb_path=duckdb_path)
    computor._conn = con
    try:
        clear_arm_block(con, seasons)
        computor._fill_outfield_arm_block(seasons, asof=None)
    finally:
        computor._close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", default=list(DEFAULT_SEASONS))
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB)
    ap.add_argument("--skip-fill", action="store_true", help="only the league averages + verify")
    ap.add_argument(
        "--skip-named-checks",
        action="store_true",
        help="skip the value-for-value check of the 2024 center fielders against the pool",
    )
    ap.add_argument(
        "--no-cross-check",
        action="store_true",
        help="skip the one HTTP pull of Savant's fielder view for 2024",
    )
    args = ap.parse_args(argv)
    seasons = sorted({int(s) for s in args.seasons})
    t0 = time.perf_counter()
    # The lock probe on every path: the league-row recompute opens its own
    # writable connection, so ``--skip-fill`` against a running app must fail
    # with the run-book instruction, not a raw traceback.
    _connect_writable(args.duckdb_path).close()
    if not args.skip_fill:
        fill_arm_block(args.duckdb_path, seasons, dsn=DEFAULT_DSN)
        log.info("the arm block filled in %.0fs", time.perf_counter() - t0)
    LeagueAverageProfiles(args.duckdb_path).compute(seasons)
    verify(
        args.duckdb_path,
        seasons,
        skip_named_checks=args.skip_named_checks,
        cross_check=not args.no_cross_check,
    )
    log.info("done in %.0fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
