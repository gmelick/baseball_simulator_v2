"""SIM-510 — apply migration 0018, rebuild the two pools, and validate.

Pool rebuilds only (minutes, not hours): the outcome pool gains the
transition columns and the advancement opportunity pool is built from it.
No profile recompute, no re-sweep.

Run (from the repo root; scripts/ is not baked into the current image):

    docker compose run -d --name sim510_rebuild \
        -v "$PWD/scripts:/app/scripts" app \
        python scripts/sim510_rebuild_pools.py --seasons 2017 2018 2019 2020 \
        2021 2022 2023 2024 2025 2026

Steps:
  1. Apply db/migrations/duckdb/0018_sim510_transition_columns.sql
     (idempotent — ADD COLUMN / CREATE TABLE IF NOT EXISTS only).
  2. Rebuild sim.outcome_pool + sim.advancement_opportunity_pool for the
     requested seasons (incremental=False — the POOL_BUILDER_VERSION bump
     to sim510.1 makes every season stale anyway).
  3. Print the validation measurements the SIM-510 exit gate needs:
     the dest_outs_consistent rate per season, the 24 base-out cell counts,
     double-play rows naming a retired runner, reach-on-error batter-reached
     truth, and per-decision opportunity/attempt/safe rates.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, "/app")

from pipeline.batch.player_profile_computor import (  # noqa: E402
    PlayerProfileComputor,
)

MIGRATION = "/app/db/migrations/duckdb/0018_sim510_transition_columns.sql"


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
        print("Migration 0018 applied.", flush=True)

        # -- 2. The two pool rebuilds --------------------------------------
        comp._build_outcome_pool(args.seasons, incremental=False)
        comp._build_advancement_opportunity_pool(args.seasons, incremental=False)

    # -- 3. Validation measurements ---------------------------------------
    def show(title: str, sql: str) -> None:
        print(f"\n== {title} ==", flush=True)
        rows = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description]
        print("  " + " | ".join(cols))
        for r in rows:
            print("  " + " | ".join(str(v) for v in r))

    show(
        "dest_outs_consistent rate per season (the derivation guard)",
        f"""
        SELECT season, COUNT(*) AS rows_,
               ROUND(AVG(CASE WHEN dest_outs_consistent THEN 1 ELSE 0 END), 4) AS ok_rate
        FROM sim.outcome_pool WHERE season IN ({season_list})
        GROUP BY season ORDER BY season
        """,
    )
    show(
        "the 24 base-out cells (2024-2026, consistent rows — the owner expects all common)",
        """
        SELECT runners_state, outs, COUNT(*) AS n
        FROM sim.outcome_pool
        WHERE season >= 2024 AND dest_outs_consistent
        GROUP BY runners_state, outs ORDER BY runners_state, outs
        """,
    )
    show(
        "double plays name their retired runner (consistent rows)",
        f"""
        SELECT season,
               COUNT(*) AS dp_rows,
               SUM(CASE WHEN runner_1b_dest = 0 OR runner_2b_dest = 0
                          OR runner_3b_dest = 0 THEN 1 ELSE 0 END) AS with_retired_runner
        FROM sim.outcome_pool
        WHERE season IN ({season_list}) AND dest_outs_consistent
          AND events IN ('grounded_into_double_play', 'double_play', 'triple_play',
                         'sac_fly_double_play', 'sac_bunt_double_play')
        GROUP BY season ORDER BY season
        """,
    )
    show(
        "reach on error carries batter-reached truth (consistent rows)",
        f"""
        SELECT season, COUNT(*) AS roe_rows,
               ROUND(AVG(CASE WHEN batter_dest >= 1 THEN 1 ELSE 0 END), 4) AS batter_reached
        FROM sim.outcome_pool
        WHERE season IN ({season_list}) AND dest_outs_consistent
          AND events = 'field_error'
        GROUP BY season ORDER BY season
        """,
    )
    show(
        "the eight advancement decisions: opportunities / attempt / safe rates (2024-2026)",
        """
        SELECT scenario, from_base, target_base,
               COUNT(*) AS opportunities,
               ROUND(AVG(CASE WHEN attempted THEN 1 ELSE 0 END), 4) AS attempt_rate,
               ROUND(AVG(CASE WHEN attempted AND safe THEN 1 ELSE 0 END)
                     / NULLIF(AVG(CASE WHEN attempted THEN 1 ELSE 0 END), 0), 4) AS safe_given_go
        FROM sim.advancement_opportunity_pool
        WHERE season >= 2024
        GROUP BY scenario, from_base, target_base
        ORDER BY scenario, from_base
        """,
    )
    show(
        "advancement rows per season",
        f"""
        SELECT season, COUNT(*) AS n FROM sim.advancement_opportunity_pool
        WHERE season IN ({season_list}) GROUP BY season ORDER BY season
        """,
    )
    comp._close()
    print("\nSIM-510 rebuild + validation complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
