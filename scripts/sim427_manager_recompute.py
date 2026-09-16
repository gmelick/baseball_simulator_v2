"""
scripts/sim427_manager_recompute.py — SIM-427 part 4a: the manager profiles and the
manager league-average row, recomputed on their own (not the five-hour full rebuild).

What it does, in order (the app must be STOPPED — a DuckDB write):

  1. applies ``db/migrations/duckdb/0027_sim537_pitcher_manager_asof.sql``
     (idempotent ``ADD COLUMN IF NOT EXISTS``) — the live manager table lacked
     the point-in-time column the SIM-537 INSERT names, so the recompute would
     fail without it;
  2. recomputes ``derived.manager_season_metrics`` for the seasons (the
     computor's own step 5, ``_compute_manager_profiles``);
  3. recomputes ``derived.league_averages`` for the seasons — which now
     includes the ``manager`` entity (the engine's shrinkage target and the
     steal weight's measured denominator);
  4. verifies: rows per season, the manager league rows, and prints the
     league means the plan's §2 table cites.

    MSYS_NO_PATHCONV=1 docker compose run --rm -T -v "$PWD/scripts:/app/scripts" \\
        app python scripts/sim427_manager_recompute.py --seasons 2017 2018 ... 2026

Then: ``make calibrate`` (the manager bandwidths re-fit on shrunk profiles) and
``python -m pipeline.batch.engine_artifacts --what actors_sim`` and ``--what
manager`` (both read-only) — see the plan.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

from pipeline.batch.player_profile_computor import (  # noqa: E402
    LeagueAverageProfiles,
    PlayerProfileComputor,
)

log = logging.getLogger("sim427_manager_recompute")

MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0027_sim537_pitcher_manager_asof.sql"
DEFAULT_DUCKDB = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
DEFAULT_DSN = os.environ.get("BASEBALL_DB_DSN", "")


def apply_migration(duckdb_path: str) -> None:
    # Drop the comment lines first, then split on ';' — a chunk that starts
    # with a comment still carries a statement.
    lines = [
        line
        for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ]
    body = chr(10).join(lines)
    statements = [s.strip() for s in body.split(";") if s.strip()]
    con = duckdb.connect(duckdb_path)
    try:
        for stmt in statements:
            con.execute(stmt)
    finally:
        con.close()
    log.info("applied %s (%d statements)", MIGRATION.name, len(statements))


def verify(duckdb_path: str, seasons: list[int]) -> None:
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        cols = {r[0] for r in con.execute("DESCRIBE derived.manager_season_metrics").fetchall()}
        log.info("asof_date present on the manager table: %s", "asof_date" in cols)
        for season, n, below, asof_max in con.execute(
            "SELECT season, count(*), sum(below_minimum_sample::int), max(asof_date) "
            "FROM derived.manager_season_metrics GROUP BY 1 ORDER BY 1"
        ).fetchall():
            log.info(
                "  managers %s: %d rows (%d below minimum), asof %s", season, n, below, asof_max
            )
        rows = con.execute(
            "SELECT season, profile_json FROM derived.league_averages "
            "WHERE entity_type = 'manager' ORDER BY season"
        ).fetchall()
        log.info("manager league-average rows: %d", len(rows))
        import json

        for season, pj in rows:
            d = json.loads(pj) if isinstance(pj, str) else pj
            log.info(
                "  %s: starter pitches %.2f, pulled<100 %.3f, steal order rate %.4f, "
                "high-lev reliever %.3f",
                season,
                float(d.get("starter_avg_pitch_count") or 0),
                float(d.get("starter_pull_pct_before_100") or 0),
                float(d.get("steal_order_rate_per_1b_opp") or 0),
                float(d.get("high_leverage_reliever_rate") or 0),
            )
        missing = [s for s in seasons if s not in {r[0] for r in rows}]
        if missing:
            raise SystemExit(f"no manager league-average row for seasons {missing}")
    finally:
        con.close()


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
    args = ap.parse_args(argv)
    if not args.dsn:
        log.error("no Postgres DSN (BASEBALL_DB_DSN or --dsn)")
        return 2
    t0 = time.perf_counter()
    apply_migration(args.duckdb_path)
    if not args.skip_profiles:
        computor = PlayerProfileComputor(pg_dsn=args.dsn, duckdb_path=args.duckdb_path)
        computor._connect()
        try:
            computor._compute_manager_profiles(args.seasons, asof=None)
        finally:
            computor._close()
        log.info("manager profiles recomputed in %.0fs", time.perf_counter() - t0)
    LeagueAverageProfiles(args.duckdb_path).compute(args.seasons)
    verify(args.duckdb_path, args.seasons)
    log.info("done in %.0fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
