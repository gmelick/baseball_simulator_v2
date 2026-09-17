"""
scripts/sim531_runner_recompute.py — SIM-531 run-book step 4: the three runner-side
profile tables and the league averages, recomputed on their own (not the five-hour
full rebuild).

What it does, in order (the app must be STOPPED — a DuckDB write; the forkserver's
lock, SIM-524):

  1. applies ``db/migrations/duckdb/0028_sim537_baserunner_catcher_fielder_asof.sql``
     and then ``0029_sim531_lead_distance.sql`` (both idempotent
     ``ADD COLUMN IF NOT EXISTS``). 0028 first, because the live database had
     never applied it (checked 2026-09-16: no ``asof_date`` on the five tables)
     and ``derived.baserunner_season_metrics`` is a positional insert — its
     ``asof_date`` must sit before 0029's four columns;
  2. rebuilds ``derived.baserunner_season_metrics`` (the advancement runner: the
     extra-base attempt rate above expectation, four positional-tail columns),
     ``derived.baserunner_steal_metrics`` (the steal runner: the lead and the jump;
     the driver widened to every runner with a chance) and
     ``derived.pitcher_steal_metrics`` (the pitcher: the lead and the jump he
     allows) for the seasons, from Postgres through the computor's own methods;
  3. recomputes ``derived.league_averages`` for the seasons — the ``baserunner``
     row gains the new key, and the ``baserunner_steal`` / ``pitcher_steal`` rows
     exist for the first time (the shrinkage target the engines always read);
  4. verifies: the baserunner table's tail order (the positional-insert trap),
     rows and Savant coverage per season — a COMPLETED season from 2023 on must
     carry at least 550 steal runners / 700 pitchers / 500 advancement runners
     with a Savant figure (the n=1 loads give about 640 / 850 / 630; the
     qualified defaults a lost ``n=1`` falls back to give 432 / 496 / 305), the
     zero-attempt runner rows, one cutoff per table, the two new league-average
     rows, and three named players value for value against the Savant CSV
     (2024: runner 691026 primary 12.752 / jump 3.289; pitcher 676775 11.629 /
     4.240; extra bases 691026 60 / 152 = 0.395 against 0.346 expected).

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim531_runner_recompute.py --seasons 2023 2024 2025 2026

Before this: Alembic 0026 applied and the five boards loaded
(``python -m pipeline.etl.savant_loader --boards running catcher_throwing
first_base_receiving --seasons 2023 2024 2025 2026``). After this: ``make calibrate``
(then write the win-probability curve back — the SIM-427 trap), copy the two fitted
sigmas into the module defaults, and rebuild the three matrices
(``python -m pipeline.batch.engine_artifacts --what actors_sim --matrix runner_steal
--matrix pitcher_steal --matrix runner_adv``) — see the plan's run book. The paired
accuracy read is cross-bundle by nature (the matrices and the calibration file both
change between the OFF and ON arms), so ``scripts/sim518_pair_accuracy.py`` needs
``--force`` for this ticket's pair.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

from pipeline.batch.player_profile_computor import (  # noqa: E402
    BASERUNNER_TAIL_COLUMNS,
    LeagueAverageProfiles,
    PlayerProfileComputor,
)

log = logging.getLogger("sim531_runner_recompute")

MIGRATIONS = (
    _ROOT / "db" / "migrations" / "duckdb" / "0028_sim537_baserunner_catcher_fielder_asof.sql",
    _ROOT / "db" / "migrations" / "duckdb" / "0029_sim531_lead_distance.sql",
)
DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")

#: The first season the three Savant boards carry (they start with the 2023
#: Statcast running-game tracking). Earlier seasons are expected to be NULL.
FIRST_SAVANT_SEASON = 2023

#: The least rows with a Savant figure a COMPLETED season may carry, per table.
#: The n=1 loads give about 640 / 850 / 630 (2024); the qualified defaults a
#: lost ``n=1`` silently falls back to give 432 / 496 / 305 (plan §9 item 6).
#: The season in progress is logged, not gated (its board is still filling).
COVERAGE_FLOOR = {
    "derived.baserunner_steal_metrics": 550,
    "derived.pitcher_steal_metrics": 700,
    "derived.baserunner_season_metrics": 500,
}

#: Three named players, value for value against the 2024 CSV (plan §5.7).
#: (table, id column, id, season, {column: expected}, tolerance)
NAMED_CHECKS: tuple[tuple[str, str, int, int, dict[str, float], float], ...] = (
    (
        "derived.baserunner_steal_metrics",
        "player_id",
        691026,
        2024,
        {"lead_primary_ft": 12.752, "lead_jump_ft": 3.289},
        0.002,
    ),
    (
        "derived.pitcher_steal_metrics",
        "pitcher_id",
        676775,
        2024,
        {"lead_allowed_primary_ft": 11.629, "lead_allowed_jump_ft": 4.240},
        0.002,
    ),
    (
        "derived.baserunner_season_metrics",
        "player_id",
        691026,
        2024,
        {
            "xb_opportunities": 152,
            "xb_attempt_rate": 60 / 152,
            "xb_expected_attempt_rate": 0.346,
            "xb_attempt_rate_above_expected": 60 / 152 - 0.346,
        },
        0.002,
    ),
)


def apply_migration(duckdb_path: str) -> None:
    """Apply 0028 then 0029 (both idempotent, in that order). Drop the comment
    lines first, then split on ';' — a chunk that starts with a comment still
    carries a statement."""
    con = _connect_writable(duckdb_path)
    try:
        for migration in MIGRATIONS:
            lines = [
                line
                for line in migration.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            ]
            body = "\n".join(lines)
            statements = [s.strip() for s in body.split(";") if s.strip()]
            for stmt in statements:
                con.execute(stmt)
            log.info("applied %s (%d statements)", migration.name, len(statements))
    finally:
        con.close()


def _connect_writable(duckdb_path: str):
    try:
        return duckdb.connect(duckdb_path)
    except duckdb.IOException as exc:
        raise SystemExit(
            f"cannot open {duckdb_path} for writing ({exc}). The app holds the DuckDB "
            "writer lock (SIM-524): stop it first — docker compose stop app."
        ) from exc


def verify(duckdb_path: str, seasons: list[int], *, skip_named_checks: bool = False) -> None:
    con = duckdb.connect(duckdb_path, read_only=True)
    problems: list[str] = []
    try:
        # 1. the positional-insert trap: the baserunner table's tail order.
        cols = [r[0] for r in con.execute("DESCRIBE derived.baserunner_season_metrics").fetchall()]
        tail = tuple(cols[-len(BASERUNNER_TAIL_COLUMNS) :])
        log.info("baserunner_season_metrics tail: %s", tail)
        if tail != BASERUNNER_TAIL_COLUMNS:
            problems.append(
                f"baserunner_season_metrics tail is {tail}, expected {BASERUNNER_TAIL_COLUMNS} "
                "— the positional INSERT wrote into the wrong columns"
            )

        # 2. rows + Savant coverage per season, per table.
        for table, lead_col, count_col, label in (
            (
                "derived.baserunner_steal_metrics",
                "lead_primary_ft",
                "savant_steal_opps",
                "steal runners",
            ),
            (
                "derived.pitcher_steal_metrics",
                "lead_allowed_primary_ft",
                "savant_hold_opps",
                "pitchers",
            ),
            (
                "derived.baserunner_season_metrics",
                "xb_attempt_rate_above_expected",
                "xb_opportunities",
                "advancement runners",
            ),
        ):
            rows = con.execute(
                f"SELECT season, COUNT(*), COUNT({lead_col}), MIN({count_col}), "
                f"SUM(below_minimum_sample::int), MAX(asof_date) "
                f"FROM {table} GROUP BY 1 ORDER BY 1"
            ).fetchall()
            for season, n, n_lead, min_count, below, asof_max in rows:
                log.info(
                    "  %s %s: %d rows, %d with a Savant figure (min count %s), %d below "
                    "minimum, asof %s",
                    label,
                    season,
                    n,
                    n_lead,
                    min_count,
                    below,
                    asof_max,
                )
                if season in seasons and season >= FIRST_SAVANT_SEASON and n_lead == 0:
                    problems.append(
                        f"{table} season {season}: no row carries {lead_col} — the Savant "
                        "board was not loaded (run-book step 3) or the join missed"
                    )
                elif (
                    season in seasons
                    and FIRST_SAVANT_SEASON <= season < date.today().year
                    and n_lead < COVERAGE_FLOOR[table]
                ):
                    problems.append(
                        f"{table} season {season}: only {n_lead} rows carry {lead_col} "
                        f"(floor {COVERAGE_FLOOR[table]}) — the n=1 load fell back to the "
                        "qualified default (432 / 496 / 305 rows), or the join missed; "
                        "re-load the board (run-book step 3)"
                    )
            # One cutoff per table: the engines refuse a mixed set at boot.
            stamps = con.execute(
                f"SELECT COUNT(DISTINCT COALESCE(asof_date::VARCHAR, 'NULL')) FROM {table}"
            ).fetchone()[0]
            if stamps != 1:
                problems.append(
                    f"{table}: {stamps} distinct asof_date stamps — the engines refuse a "
                    "mixed set at boot; rebuild every season at one date"
                )

        # 3. the widened driver: runners who never went now carry a row, and no
        #    row reads confidence 0 (its chances are never below its attempts).
        zero = con.execute(
            "SELECT season, COUNT(*) FROM derived.baserunner_steal_metrics "
            "WHERE sample_steal_attempts = 0 GROUP BY 1 ORDER BY 1"
        ).fetchall()
        for season, n in zero:
            log.info("  zero-attempt steal rows %s: %d", season, n)
        if not any(s in seasons for s, _ in zero):
            problems.append(
                "no zero-attempt runner row in any requested season — the steal driver "
                "still runs off the attempts"
            )
        null_success = con.execute(
            "SELECT COUNT(*) FROM derived.baserunner_steal_metrics "
            "WHERE sample_steal_attempts = 0 AND steal_success_rate IS NOT NULL"
        ).fetchone()[0]
        if null_success:
            problems.append(
                f"{null_success} zero-attempt rows carry a steal_success_rate (must be NULL)"
            )
        no_chance = con.execute(
            "SELECT COUNT(*) FROM derived.baserunner_steal_metrics "
            "WHERE COALESCE(sample_first_base_opps, 0) + COALESCE(sample_second_base_opps, 0) = 0 "
            "AND COALESCE(sample_steal_attempts, 0) = 0"
        ).fetchone()[0]
        if no_chance:
            problems.append(
                f"{no_chance} steal rows carry no chance and no attempt — the driver admitted "
                "a row the model would score at confidence 0"
            )

        # 4. the two new league-average rows, with the lead keys.
        for entity, keys in (
            ("baserunner_steal", ("lead_primary_ft", "lead_jump_ft")),
            ("pitcher_steal", ("lead_allowed_primary_ft", "lead_allowed_jump_ft")),
            ("baserunner", ("xb_attempt_rate_above_expected",)),
        ):
            rows = con.execute(
                "SELECT season, profile_json FROM derived.league_averages "
                "WHERE entity_type = ? ORDER BY season",
                [entity],
            ).fetchall()
            have = {int(s) for s, _ in rows}
            missing = [s for s in seasons if s not in have]
            if missing:
                problems.append(f"no '{entity}' league-average row for seasons {missing}")
            for season, pj in rows:
                d = json.loads(pj) if isinstance(pj, str) else pj
                vals = {k: d.get(k) for k in keys}
                log.info("  league %s %s: %s", entity, season, vals)
                if (
                    int(season) in seasons
                    and int(season) >= FIRST_SAVANT_SEASON
                    and any(v is None for v in vals.values())
                ):
                    problems.append(
                        f"'{entity}' league row {season} lacks a lead key: {vals} — the "
                        "shrinkage has no target"
                    )

        # 5. three named players against the CSV.
        if not skip_named_checks:
            for table, id_col, pid, season, expected, tol in NAMED_CHECKS:
                if season not in seasons:
                    continue
                cols_sql = ", ".join(expected)
                row = con.execute(
                    f"SELECT {cols_sql} FROM {table} WHERE {id_col} = ? AND season = ?",
                    [pid, season],
                ).fetchone()
                if row is None:
                    problems.append(f"{table}: no row for {id_col}={pid} season {season}")
                    continue
                got = dict(zip(expected, row, strict=True))
                log.info("  %s %s/%s: %s", table, pid, season, got)
                for k, want in expected.items():
                    v = got.get(k)
                    if v is None or abs(float(v) - float(want)) > tol:
                        problems.append(
                            f"{table} {id_col}={pid} season {season}: {k} = {v}, expected "
                            f"{want} (tolerance {tol})"
                        )
    finally:
        con.close()
    if problems:
        for p in problems:
            log.error("VERIFY: %s", p)
        raise SystemExit(f"{len(problems)} verification problem(s); see the log")
    log.info("verify: every check passed")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--duckdb-path", default=DEFAULT_DUCKDB)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument(
        "--skip-profiles", action="store_true", help="only the league averages + verify"
    )
    ap.add_argument(
        "--skip-named-checks",
        action="store_true",
        help="skip the three value-for-value checks against the 2024 Savant CSV",
    )
    args = ap.parse_args(argv)
    if not args.dsn and not args.skip_profiles:
        log.error("no Postgres DSN (BASEBALL_DB_DSN or --dsn)")
        return 2
    t0 = time.perf_counter()
    apply_migration(args.duckdb_path)
    if not args.skip_profiles:
        computor = PlayerProfileComputor(pg_dsn=args.dsn, duckdb_path=args.duckdb_path)
        computor._connect()
        try:
            computor._compute_baserunner_profiles(args.seasons, asof=None)
            computor._build_baserunner_steal_metrics(args.seasons, asof=None)
            computor._build_pitcher_steal_metrics(args.seasons, asof=None)
        finally:
            computor._close()
        log.info("the three runner tables recomputed in %.0fs", time.perf_counter() - t0)
    LeagueAverageProfiles(args.duckdb_path).compute(args.seasons)
    verify(args.duckdb_path, args.seasons, skip_named_checks=args.skip_named_checks)
    log.info("done in %.0fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
