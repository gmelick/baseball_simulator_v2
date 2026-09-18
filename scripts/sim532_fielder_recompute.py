"""
scripts/sim532_fielder_recompute.py — Savant's per-position outs above average and the
outfield jump in the fielder profile (SIM-532), run-book step 4: the DuckDB migration,
the fielder chain, the league rows, and the verify block.

What it does, in order (the app must be STOPPED — a DuckDB write; the forkserver's
writer lock, SIM-524):

  1. applies ``db/migrations/duckdb/0030_sim532_fielder_oaa_jump.sql`` (idempotent
     ``ADD COLUMN IF NOT EXISTS``): the six columns after ``asof_date`` on
     ``derived.fielder_season_metrics`` — Savant's outs above average at the
     position, the same per 100 of our chances, the three jump parts (reaction,
     burst, route; feet against the league) and the plays the jump was scored on.
     The fielder INSERT is positional, so the six sit LAST, in
     ``OAA_JUMP_COLUMN_ORDER``.
  2. runs the FIELDER CHAIN through the computor's own methods, with Postgres
     attached (the aggregator joins ``pg.raw.*``): the run-expectancy matrix
     first (the double-play run value reads it), then the outfield catch model,
     the infield range model, the double plays, the bunt defense, the first-base
     scoops and the error split, then the aggregator (its two new joins:
     ``raw.savant_outs_above_average`` per (player, season, POSITION) and
     ``raw.savant_outfield_jump`` per (player, season), outfield rows only), then
     the outfield arm fill from the advancement pool (the aggregator writes the
     arm block NULL; the pools exist, so the fill runs again). Not the pitcher
     GMM, not the pools: well under an hour, not the five-hour full rebuild.
  3. recomputes ``derived.league_averages`` for the seasons: every fielder
     position row gains ``savant_oaa_per_100``; the ``fielder_LF`` /
     ``fielder_CF`` / ``fielder_RF`` rows also gain the three jump keys the model
     shrinks a thin jump toward.
  4. verifies, read-only: the fielder table's last seven columns are
     ``FIELDER_TAIL_COLUMNS`` (the positional-insert trap); per season the
     fielder-position rows with a Savant figure (the plan expects about 1,150 of
     1,200; the floor is 900 for a full season, 300 for 2020, 500 for the season
     in progress) and the outfield rows with a jump (about 210; floors 150 / 60
     / 100); the jump columns NULL on every infield and catcher row; the per-100
     figure equal to the count times 100 over our chances, and NULL where the
     count is NULL; three named 2024 checks against the raw Postgres rows, both
     sides computed here (the outfielder with the most jump plays, value for
     value at every outfield position he has a row; a player with rows at two
     outfield positions whose Savant figure DIFFERS between them; an outfielder
     with a fielder row and no jump row); the league keys per season; one cutoff
     per table.

Seasons: 2017 to 2026 by default — ``raw.pitches`` starts in 2017, so the
fielder chain has nothing to build for 2016. The two boards were loaded for
2016 too; those rows stay in the raw tables for the record and no profile row
reads them. Pass 2017 as the first season: raw.pitches starts in 2017, so a
2016 fielder row does not exist and the coverage check reports it.

The one-cutoff guard (SIM-551): a partial rebuild must include the season in
progress, or the computor refuses it before writing (a survivor row of the
cutoff's own season would sit at another date). The script refuses such a
season list earlier, with a plainer message; ``--allow-partial`` hands the
decision to the guard.

The paired accuracy read does NOT run for this change (owner ruling 2026-09-16:
every change lands ON at its best-known default; the draw weights are fitted
together in one designed experiment later).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim532_fielder_recompute.py \\
        --seasons 2017 2018 2019 2020 2021 2022 2023 2024 2025 2026

Before this: Alembic 0027 applied and the two boards loaded
(``python -m pipeline.etl.savant_loader --boards outs_above_average outfield_jump
--seasons 2016 ... 2026``; the outs-above-average board is seven pulls a season, one
per position). After this: ``make calibrate`` (then write the win-probability curve
back — the SIM-427 trap), copy ``sigma_of_range`` and the range weights into the
module defaults, and rebuild the seven fielder matrices
(``python -m pipeline.batch.engine_artifacts --what actors_sim --matrix fielder_LF
...``). Run book: ``docs/audit/2026-09-17-sim532-fielder-hand-split-and-jump-plan.md`` §8.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

from pipeline.batch.player_profile_computor import (  # noqa: E402
    FIELDER_TAIL_COLUMNS,
    LeagueAverageProfiles,
    PlayerProfileComputor,
    _table_exists,
    build_run_expectancy_matrix,
)

log = logging.getLogger("sim532_fielder_recompute")

MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0030_sim532_fielder_oaa_jump.sql"
DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")
#: ``raw.pitches`` starts in 2017: the fielder chain covers 2017 to 2026. The
#: boards' 2016 rows stay in the raw tables for the record.
DEFAULT_SEASONS: tuple[int, ...] = tuple(range(2017, 2027))

#: The seven fielding positions Savant's board is pulled for, and the three
#: outfield ones the jump board covers.
FIELDING_POSITIONS: tuple[str, ...] = ("1B", "2B", "3B", "SS", "LF", "CF", "RF")
FIELDING_SQL = "('1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF')"
OUTFIELD: tuple[str, ...] = ("LF", "CF", "RF")
OUTFIELD_SQL = "('LF', 'CF', 'RF')"
INFIELD: tuple[str, ...] = ("1B", "2B", "3B", "SS")

#: The jump columns: outfield rows only (plan §4.2).
JUMP_COLUMNS: tuple[str, ...] = ("jump_reaction_ft", "jump_burst_ft", "jump_route_ft", "jump_plays")

#: The least fielder-position rows with a Savant figure a season may carry
#: (the plan expects about 1,150 of 1,200; 2020 was a 60-game season; the
#: season in progress is still filling).
OAA_FLOOR_FULL = 900
OAA_FLOOR_2020 = 300
OAA_FLOOR_CURRENT = 500
#: The least outfield rows with a jump a season may carry (about 210 expected).
JUMP_FLOOR_FULL = 150
JUMP_FLOOR_2020 = 60
JUMP_FLOOR_CURRENT = 100

#: The league keys per position: Savant's figure everywhere, the jump parts on
#: the outfield rows (plan §4.2; decision 4 for the infield).
LEAGUE_OAA_KEY = "savant_oaa_per_100"
LEAGUE_JUMP_KEYS: tuple[str, ...] = ("jump_reaction_ft", "jump_burst_ft", "jump_route_ft")

#: The named checks read this season (the plan's 2024 examples).
NAMED_SEASON = 2024
VALUE_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# Connections and the migration
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


def migration_statements(text: str) -> list[str]:
    """The statements of a migration file. Drop the comments first (a whole
    comment line, and the tail of a line after ``--`` — 0030 annotates each
    column on its own statement line), then split on ';'."""
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def apply_migration(duckdb_path: str, migration: Path = MIGRATION) -> int:
    """Apply the 0030 migration (idempotent). Returns the statement count."""
    con = _connect_writable(duckdb_path)
    try:
        statements = migration_statements(migration.read_text(encoding="utf-8"))
        for stmt in statements:
            con.execute(stmt)
        log.info("applied %s (%d statements)", migration.name, len(statements))
    finally:
        con.close()
    return len(statements)


def oaa_floor(season: int, today: date) -> int:
    """The least fielder-position rows with a Savant figure ``season`` may carry."""
    if season == 2020:
        return OAA_FLOOR_2020
    if season >= today.year:
        return OAA_FLOOR_CURRENT
    return OAA_FLOOR_FULL


def jump_floor(season: int, today: date) -> int:
    """The least outfield rows with a jump ``season`` may carry."""
    if season == 2020:
        return JUMP_FLOOR_2020
    if season >= today.year:
        return JUMP_FLOOR_CURRENT
    return JUMP_FLOOR_FULL


# ---------------------------------------------------------------------------
# The fielder chain
# ---------------------------------------------------------------------------


def run_fielder_chain(
    dsn: str,
    duckdb_path: str,
    seasons: list[int],
    computor: PlayerProfileComputor | None = None,
) -> None:
    """The fielder half of the nightly ``run()``, in its order, with no cutoff:
    the run-expectancy matrix, the six per-play builders, the aggregator, the
    arm fill. ``computor`` is injectable for the tests; the script builds one
    over the DSN and the DuckDB path and attaches Postgres through
    ``_connect``."""
    if computor is None:
        computor = PlayerProfileComputor(pg_dsn=dsn, duckdb_path=duckdb_path)
    computor._connect()
    try:
        # The double-play run value reads the matrix; ``run()`` builds it
        # before the defensive steps for the same reason.
        computor._re_matrix = build_run_expectancy_matrix(computor._conn, seasons, asof=None)
        computor._compute_outfield_catch_probability(seasons, asof=None)
        computor._compute_infield_oaa(seasons, asof=None)
        computor._compute_dp_metrics(seasons, asof=None)
        computor._compute_bunt_defense(seasons, asof=None)
        computor._compute_first_base_scooping(seasons, asof=None)
        computor._compute_error_decomposition(seasons, asof=None)
        computor._aggregate_fielder_season_metrics(seasons, asof=None)
        # The aggregator writes the arm block NULL; the pools exist, so the
        # fill puts it back (SIM-550).
        computor._fill_outfield_arm_block(seasons, asof=None)
    finally:
        computor._close()


# ---------------------------------------------------------------------------
# The verify block, one check per function; each returns the problems it found
# ---------------------------------------------------------------------------


def check_tail_order(con: Any) -> list[str]:
    """The fielder table's last columns, by ordinal position, are
    ``FIELDER_TAIL_COLUMNS`` — the positional INSERT wrote into the right
    columns only if this holds."""
    cols = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'derived' AND table_name = 'fielder_season_metrics' "
            "ORDER BY ordinal_position"
        ).fetchall()
    ]
    tail = tuple(cols[-len(FIELDER_TAIL_COLUMNS) :])
    log.info("  fielder_season_metrics tail: %s", tail)
    if tail != FIELDER_TAIL_COLUMNS:
        return [
            f"fielder_season_metrics tail is {tail}, expected {FIELDER_TAIL_COLUMNS} — "
            "the positional INSERT wrote into the wrong columns, or migration 0030 did not run"
        ]
    return []


def check_coverage(con: Any, seasons: list[int], today: date | None = None) -> list[str]:
    """Per season: the fielder-position rows with a Savant figure and the
    outfield rows with a jump, each against its floor."""
    today = today or date.today()
    problems: list[str] = []
    rows = con.execute(
        "SELECT season, COUNT(*), COUNT(savant_oaa), "
        f"COUNT(CASE WHEN position IN {OUTFIELD_SQL} THEN 1 END), "
        f"COUNT(CASE WHEN position IN {OUTFIELD_SQL} THEN jump_plays END) "
        f"FROM derived.fielder_season_metrics WHERE position IN {FIELDING_SQL} "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    seen: set[int] = set()
    for season, n_rows, n_oaa, n_of, n_jump in rows:
        season = int(season)
        seen.add(season)
        log.info(
            "  season %s: %d fielder-position rows, %d with a Savant figure; %d outfield rows, "
            "%d with a jump",
            season,
            n_rows,
            n_oaa,
            n_of,
            n_jump,
        )
        if season not in seasons:
            continue
        floor = oaa_floor(season, today)
        if n_oaa < floor:
            problems.append(
                f"season {season}: only {n_oaa} of {n_rows} fielder-position rows carry a Savant "
                f"figure (floor {floor}) — the board was not loaded for the season, or the "
                "per-position join missed"
            )
        floor = jump_floor(season, today)
        if n_jump < floor:
            problems.append(
                f"season {season}: only {n_jump} of {n_of} outfield rows carry a jump (floor "
                f"{floor}) — the jump board was not loaded for the season, or the join missed"
            )
    for season in seasons:
        if season not in seen:
            problems.append(f"season {season}: no fielder-position rows at all")
    return problems


def check_jump_outfield_only(con: Any) -> list[str]:
    """The jump columns are NULL on every row that is not an outfield row."""
    problems: list[str] = []
    n = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        f"WHERE position NOT IN {OUTFIELD_SQL} AND ("
        + " OR ".join(f"{c} IS NOT NULL" for c in JUMP_COLUMNS)
        + ")"
    ).fetchone()[0]
    log.info("  non-outfield rows with a jump value: %d", n)
    if n:
        problems.append(
            f"{n} infield, catcher or pitcher rows carry a jump value — the aggregator's "
            "outfield-only CASE is missing"
        )
    return problems


def check_per_100(con: Any) -> list[str]:
    """``savant_oaa_per_100`` equals the count times 100 over our chances on
    every row where both are present, and is NULL where the count is NULL.

    The column is a 32-bit FLOAT; the recomputation is a DOUBLE. The check
    casts the recomputation to FLOAT first, so a thin row (1 out on 3 chances
    = 33.333...) compares at the precision the column can hold. Without the
    cast a correct row reads as off by about 1e-6.
    """
    problems: list[str] = []
    off = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        "WHERE savant_oaa IS NOT NULL AND savant_oaa_per_100 IS NOT NULL "
        "AND ABS(savant_oaa_per_100 - CAST(savant_oaa * 100.0 / opportunities AS FLOAT)) "
        f"> {VALUE_TOLERANCE}"
    ).fetchone()[0]
    orphan = con.execute(
        "SELECT COUNT(*) FROM derived.fielder_season_metrics "
        "WHERE savant_oaa IS NULL AND savant_oaa_per_100 IS NOT NULL"
    ).fetchone()[0]
    log.info("  per-100 rows off the count: %d; per-100 without a count: %d", off, orphan)
    if off:
        problems.append(
            f"{off} rows carry a savant_oaa_per_100 that is not savant_oaa * 100 / opportunities "
            "— the aggregator divides by the wrong chance count"
        )
    if orphan:
        problems.append(
            f"{orphan} rows carry a savant_oaa_per_100 with no savant_oaa — the two columns "
            "come from different joins"
        )
    return problems


@dataclass
class RawFieldingRows:
    """The raw Postgres rows the named checks compare against, for one season.

    ``jump``: player_id -> (reaction_ft, burst_ft, route_ft, n_plays).
    ``oaa``: (player_id, position) -> outs_above_average at that position.
    """

    jump: dict[int, tuple[float | None, float | None, float | None, int | None]] = field(
        default_factory=dict
    )
    oaa: dict[tuple[int, str], int | None] = field(default_factory=dict)


def read_raw_fielding_rows(dsn: str, season: int = NAMED_SEASON) -> RawFieldingRows:
    """The two raw tables for ``season``, read through psycopg2 (the verify
    connection is DuckDB read-only, without Postgres attached)."""
    import psycopg2

    raw = RawFieldingRows()
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_id, reaction_ft, burst_ft, route_ft, n_plays "
            "FROM raw.savant_outfield_jump WHERE season = %s",
            (int(season),),
        )
        for pid, reaction, burst, route, plays in cur.fetchall():
            raw.jump[int(pid)] = (reaction, burst, route, plays)
        cur.execute(
            "SELECT player_id, position, outs_above_average "
            "FROM raw.savant_outs_above_average WHERE season = %s",
            (int(season),),
        )
        for pid, pos, oaa in cur.fetchall():
            raw.oaa[(int(pid), str(pos))] = oaa
    finally:
        conn.close()
    log.info(
        "  raw %s: %d jump rows, %d outs-above-average rows", season, len(raw.jump), len(raw.oaa)
    )
    return raw


def _same(got: Any, want: Any) -> bool:
    if got is None or want is None:
        return got is None and want is None
    return abs(float(got) - float(want)) <= VALUE_TOLERANCE


def _fielder_rows(con: Any, player_id: int, season: int) -> dict[str, tuple]:
    """The player's outfield fielder rows for ``season``: position ->
    (savant_oaa, reaction, burst, route, plays)."""
    rows = con.execute(
        "SELECT position, savant_oaa, jump_reaction_ft, jump_burst_ft, jump_route_ft, "
        "jump_plays FROM derived.fielder_season_metrics "
        f"WHERE player_id = ? AND season = ? AND position IN {OUTFIELD_SQL}",
        [int(player_id), int(season)],
    ).fetchall()
    return {str(r[0]): tuple(r[1:]) for r in rows}


def check_named_players(con: Any, raw: RawFieldingRows, season: int = NAMED_SEASON) -> list[str]:
    """Three named checks for ``season``, both sides computed here, no id
    hard-coded: the outfielder with the most jump plays (value for value at
    every outfield position he has a row); a player with fielder rows at two
    outfield positions whose Savant figure differs between them (each row
    reads the figure AT its position); an outfielder with a fielder row and no
    jump row (jump NULL, the Savant figure filled). A check with no candidate
    is logged and skipped."""
    problems: list[str] = []

    # 1. The outfielder with the most jump plays.
    if raw.jump:
        pid = max(raw.jump, key=lambda p: ((raw.jump[p][3] or 0), -p))
        reaction, burst, route, plays = raw.jump[pid]
        rows = _fielder_rows(con, pid, season)
        log.info(
            "  most jump plays %s: player %s (%s plays), fielder rows %s", season, pid, plays, rows
        )
        if not rows:
            problems.append(
                f"season {season}: player {pid} has the most jump plays ({plays}) and no outfield "
                "fielder row"
            )
        for pos, (_oaa, got_reaction, got_burst, got_route, got_plays) in rows.items():
            for name, got, want in (
                ("jump_reaction_ft", got_reaction, reaction),
                ("jump_burst_ft", got_burst, burst),
                ("jump_route_ft", got_route, route),
                ("jump_plays", got_plays, plays),
            ):
                if not _same(got, want):
                    problems.append(
                        f"season {season}: {pos} {pid} {name} = {got}, the raw jump row says {want}"
                    )
    else:
        log.info("  named check skipped: no raw jump rows for %s", season)

    # 2. A two-position outfielder whose Savant figure differs by position.
    by_player: dict[int, dict[str, int | None]] = {}
    for (pid, pos), oaa in raw.oaa.items():
        if pos in OUTFIELD:
            by_player.setdefault(pid, {})[pos] = oaa
    two_position = None
    for pid in sorted(by_player):
        figures = by_player[pid]
        rows = _fielder_rows(con, pid, season)
        shared = [p for p in figures if p in rows and figures[p] is not None]
        if len(shared) >= 2 and len({figures[p] for p in shared}) >= 2:
            two_position = (pid, shared)
            break
    if two_position is None:
        log.info(
            "  named check skipped: no two-position outfielder with differing figures in %s", season
        )
    else:
        pid, positions = two_position
        rows = _fielder_rows(con, pid, season)
        log.info(
            "  two-position %s: player %s at %s; raw %s; fielder %s",
            season,
            pid,
            positions,
            {p: by_player[pid][p] for p in positions},
            {p: rows[p][0] for p in positions},
        )
        for pos in positions:
            if not _same(rows[pos][0], by_player[pid][pos]):
                problems.append(
                    f"season {season}: {pos} {pid} savant_oaa = {rows[pos][0]}, the raw row at "
                    f"{pos} says {by_player[pid][pos]} — the join is not per position"
                )

    # 3. An outfielder with a fielder row and no jump row.
    candidates = con.execute(
        "SELECT player_id, position, savant_oaa, jump_reaction_ft, jump_burst_ft, "
        "jump_route_ft, jump_plays FROM derived.fielder_season_metrics "
        f"WHERE season = ? AND position IN {OUTFIELD_SQL} ORDER BY player_id, position",
        [int(season)],
    ).fetchall()
    no_jump = next(
        (r for r in candidates if int(r[0]) not in raw.jump and (int(r[0]), str(r[1])) in raw.oaa),
        None,
    )
    if no_jump is None:
        log.info(
            "  named check skipped: every %s outfielder with a Savant figure has a jump row", season
        )
    else:
        pid, pos, oaa, *jump = no_jump
        want = raw.oaa[(int(pid), str(pos))]
        log.info(
            "  no jump row %s: player %s at %s, savant_oaa %s (raw %s), jump %s",
            season,
            pid,
            pos,
            oaa,
            want,
            jump,
        )
        if any(v is not None for v in jump):
            problems.append(
                f"season {season}: {pos} {pid} has no raw jump row and carries a jump {jump} — "
                "the jump join is not per player-season"
            )
        if not _same(oaa, want):
            problems.append(
                f"season {season}: {pos} {pid} savant_oaa = {oaa}, the raw row says {want}"
            )
    return problems


def _finite(v: Any) -> bool:
    if v is None:
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def check_league_rows(con: Any, seasons: list[int]) -> list[str]:
    """Every fielder position's league row carries ``savant_oaa_per_100``; the
    three outfield rows also carry the three jump keys — finite values, for
    every requested season with data at that position."""
    problems: list[str] = []
    if not _table_exists(con, "derived", "league_averages"):
        return ["derived.league_averages is absent — the league writer did not run"]
    for pos in FIELDING_POSITIONS:
        keys = (LEAGUE_OAA_KEY, *LEAGUE_JUMP_KEYS) if pos in OUTFIELD else (LEAGUE_OAA_KEY,)
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
            vals = {k: d.get(k) for k in keys}
            log.info("  league fielder_%s %s: %s", pos, season, vals)
            bad = [k for k in keys if not _finite(d.get(k))]
            if bad:
                problems.append(
                    f"'fielder_{pos}' league row {season} lacks a finite value for {bad} — "
                    "the shrinkage has no target"
                )
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


def verify(
    duckdb_path: str,
    seasons: list[int],
    *,
    dsn: str = "",
    skip_named_checks: bool = False,
    raw: RawFieldingRows | None = None,
) -> None:
    """Every check, read-only; exit non-zero with every problem logged. The
    named checks need the raw Postgres rows: ``raw`` when given (the tests),
    else a psycopg2 read over ``dsn``, else they are skipped with a log line."""
    con = duckdb.connect(duckdb_path, read_only=True)
    problems: list[str] = []
    try:
        if not _table_exists(con, "derived", "fielder_season_metrics"):
            problems.append("derived.fielder_season_metrics is absent — nothing to verify")
        else:
            problems += check_tail_order(con)
            if not problems:
                problems += check_coverage(con, seasons)
                problems += check_jump_outfield_only(con)
                problems += check_per_100(con)
                if not skip_named_checks and NAMED_SEASON in seasons:
                    if raw is None and dsn:
                        raw = read_raw_fielding_rows(dsn, NAMED_SEASON)
                    if raw is None:
                        log.info(
                            "  named checks skipped: no Postgres DSN (BASEBALL_DB_DSN or --dsn)"
                        )
                    else:
                        problems += check_named_players(con, raw, NAMED_SEASON)
                problems += check_league_rows(con, seasons)
                problems += check_one_asof(con)
    finally:
        con.close()
    if problems:
        for p in problems:
            log.error("VERIFY: %s", p)
        raise SystemExit(f"{len(problems)} verification problem(s); see the log")
    log.info("verify: every check passed")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def missing_current_season(seasons: list[int], today: date | None = None) -> int | None:
    """The season in progress when ``seasons`` omits it, else None. The
    one-cutoff guard refuses a partial rebuild that leaves the current
    season's rows at another date (SIM-551)."""
    today = today or date.today()
    return today.year if today.year not in seasons else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEASONS),
        help="the seasons to rebuild (default 2017 to 2026). A partial list must include "
        "the season in progress: the one-cutoff guard (SIM-551) refuses a rebuild that leaves "
        "the current season's rows at another date.",
    )
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument(
        "--skip-profiles", action="store_true", help="only the league averages + verify"
    )
    ap.add_argument(
        "--skip-named-checks",
        action="store_true",
        help="skip the three value-for-value checks of 2024 against the raw Postgres rows",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="run a season list that omits the season in progress and let the computor's "
        "one-cutoff guard decide (it refuses when a current-season row would be left at "
        "another date)",
    )
    args = ap.parse_args(argv)
    seasons = sorted({int(s) for s in args.seasons})
    current = missing_current_season(seasons)
    if current is not None and not args.allow_partial and not args.skip_profiles:
        log.error(
            "the season list %s omits the season in progress (%d): the one-cutoff guard "
            "(SIM-551) refuses a rebuild that leaves its rows at another date. Add %d to "
            "--seasons, or pass --allow-partial to let the guard decide.",
            seasons,
            current,
            current,
        )
        return 2
    if not args.dsn and not args.skip_profiles:
        log.error("no Postgres DSN (BASEBALL_DB_DSN or --dsn)")
        return 2
    t0 = time.perf_counter()
    # The lock probe on every path: the league-row recompute opens its own
    # writable connection, so ``--skip-profiles`` against a running app must
    # fail with the run-book instruction, not a raw traceback.
    _connect_writable(args.duckdb_path).close()
    apply_migration(args.duckdb_path)
    if not args.skip_profiles:
        run_fielder_chain(args.dsn, args.duckdb_path, seasons)
        log.info("the fielder chain recomputed in %.0fs", time.perf_counter() - t0)
    LeagueAverageProfiles(args.duckdb_path).compute(seasons)
    verify(
        args.duckdb_path,
        seasons,
        dsn=args.dsn,
        skip_named_checks=args.skip_named_checks,
    )
    log.info("done in %.0fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
