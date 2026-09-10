"""SIM-491 — apply migration 0019, rebuild sim.outcome_pool with bat_home, validate.

Pool rebuild only (minutes, not hours): the outcome pool gains the batting-side
column (the SIM-412 home-field rebuild as a draw weight). No profile recompute,
no re-sweep, and the advancement opportunity pool is untouched (bat_home does
not enter it).

Run (from the repo root; scripts/ is not baked into the current image):

    docker compose run -d --name sim491_rebuild \
        -v "$PWD/scripts:/app/scripts" app \
        python scripts/sim491_rebuild_pools.py --seasons 2017 2018 2019 2020 \
        2021 2022 2023 2024 2025 2026

Then re-export the batted-ball artifact:

    docker compose run -d --name sim491_artifacts app \
        python -m pipeline.batch.engine_artifacts --what pool

Steps:
  1. Apply db/migrations/duckdb/0019_sim491_bat_home.sql (idempotent).
  2. Rebuild sim.outcome_pool for the requested seasons (incremental=False —
     the POOL_BUILDER_VERSION bump to sim491.1 makes every season stale anyway).
  3. Validate: the bat_home split per season must sit near 50/50 (every game
     has both halves; extra-inning walk-offs skew it a hair below half), and
     zero NULL bat_home rows.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, "/app")

from pipeline.batch.player_profile_computor import (  # noqa: E402
    PlayerProfileComputor,
)

MIGRATION = "/app/db/migrations/duckdb/0019_sim491_bat_home.sql"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument(
        "--duckdb-path",
        default=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"),
    )
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"))
    ap.add_argument(
        "--validate-only",
        action="store_true",
        help="Skip the migration and rebuild; print the measurements only.",
    )
    args = ap.parse_args()
    if not args.dsn:
        print("ERROR: no Postgres DSN. Set BASEBALL_DB_DSN or pass --dsn.")
        return 2

    comp = PlayerProfileComputor(pg_dsn=args.dsn, duckdb_path=args.duckdb_path)
    comp._connect()
    con = comp._conn
    season_list = ", ".join(str(s) for s in args.seasons)

    if not args.validate_only:
        # -- 1. The migration (idempotent) --------------------------------
        print(f"Applying {MIGRATION} …", flush=True)
        con.execute(
            "CREATE TABLE IF NOT EXISTS migration_history ("
            "migration_id VARCHAR PRIMARY KEY, description VARCHAR, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        with open(MIGRATION, encoding="utf-8") as fh:
            con.execute(fh.read())
        print("Migration 0019 applied.", flush=True)

        # -- 2. The outcome-pool rebuild -----------------------------------
        comp._build_outcome_pool(args.seasons, incremental=False)

    # -- 3. Validation measurements ---------------------------------------
    def show(title: str, sql: str) -> None:
        print(f"\n== {title} ==", flush=True)
        rows = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description]
        print("  " + " | ".join(cols))
        for r in rows:
            print("  " + " | ".join(str(v) for v in r))

    show(
        "bat_home split per season (expect ~50/50, zero NULLs)",
        f"""
        SELECT season, COUNT(*) AS rows_,
               SUM(CASE WHEN bat_home IS NULL THEN 1 ELSE 0 END) AS nulls,
               ROUND(AVG(CASE WHEN bat_home THEN 1 ELSE 0 END), 4) AS home_share
        FROM sim.outcome_pool WHERE season IN ({season_list})
        GROUP BY season ORDER BY season
        """,
    )
    show(
        "the SIM-510 guard is unchanged by the rebuild (ok_rate per season)",
        f"""
        SELECT season, COUNT(*) AS rows_,
               ROUND(AVG(CASE WHEN dest_outs_consistent THEN 1 ELSE 0 END), 4) AS ok_rate
        FROM sim.outcome_pool WHERE season IN ({season_list})
        GROUP BY season ORDER BY season
        """,
    )
    comp._close()
    print("\nSIM-491 rebuild + validation complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
