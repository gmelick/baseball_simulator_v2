"""SIM-515 — apply migration 0020 and build sim.ibb_rates.

Run (from the repo root; scripts/ is not baked into the current image):

    docker compose run --rm -v "$PWD/scripts:/app/scripts" app \
        python scripts/sim515_build_ibb_rates.py --seasons 2023 2024 2025 2026

The season window must match the pool's (the owner's 2026-08-20 ruling: the
last three completed seasons plus the current one). Prints the built cells
sorted by issued so the operator sees where the volume lives, plus the
implied league IBB/team-game for a sanity read against MLB (~0.11-0.12).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, "/app")

from pipeline.batch.player_profile_computor import (  # noqa: E402
    PlayerProfileComputor,
)

MIGRATION = "/app/db/migrations/duckdb/0020_sim515_ibb_rates.sql"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument(
        "--duckdb-path",
        default=os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"),
    )
    ap.add_argument("--dsn", default=os.environ.get("BASEBALL_DB_DSN"))
    args = ap.parse_args()
    if not args.dsn:
        print("ERROR: no Postgres DSN. Set BASEBALL_DB_DSN or pass --dsn.")
        return 2

    comp = PlayerProfileComputor(pg_dsn=args.dsn, duckdb_path=args.duckdb_path)
    comp._connect()
    con = comp._conn

    print(f"Applying {MIGRATION} …", flush=True)
    con.execute(
        "CREATE TABLE IF NOT EXISTS migration_history ("
        "migration_id VARCHAR PRIMARY KEY, description VARCHAR, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    with open(MIGRATION, encoding="utf-8") as fh:
        con.execute(fh.read())
    print("Migration 0020 applied.", flush=True)

    comp._build_ibb_rates(args.seasons)

    print("\n== the built cells, by issued DESC (top 15) ==")
    rows = con.execute(
        "SELECT runners_state, outs, is_late, is_close, opportunities, issued, "
        "ROUND(issued * 1.0 / opportunities, 5) AS rate "
        "FROM sim.ibb_rates ORDER BY issued DESC LIMIT 15"
    ).fetchall()
    print("  rs | outs | late | close | opportunities | issued | rate")
    for r in rows:
        print("  " + " | ".join(str(v) for v in r))

    total_opp, total_iss = con.execute(
        "SELECT SUM(opportunities), SUM(issued) FROM sim.ibb_rates"
    ).fetchone()
    # PAs per team-game in the window ~ 38; the implied volume sanity read.
    pa_per_tg = 38.0
    print(
        f"\n league totals: {total_iss:,} IBB / {total_opp:,} PAs "
        f"= {total_iss / total_opp:.5f}/PA ≈ "
        f"{total_iss / total_opp * pa_per_tg:.4f}/team-game (MLB 2025 = 0.1224)"
    )
    comp._close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
