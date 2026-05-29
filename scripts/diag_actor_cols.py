import os

import duckdb

con = duckdb.connect(
    os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb"), read_only=True
)
for t in [
    "batter_season_metrics",
    "catcher_season_metrics",
    "fielder_season_metrics",
    "baserunner_season_metrics",
    "manager_season_metrics",
]:
    cols = con.execute(
        f"SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='derived' AND table_name='{t}' ORDER BY ordinal_position"
    ).fetchall()
    ids = [c for c, _ in cols if c.endswith("_id") or c == "player_id" or c == "season"]
    numeric = [
        c
        for c, dt in cols
        if dt.upper() in ("DOUBLE", "FLOAT", "REAL", "INTEGER", "BIGINT", "SMALLINT", "DECIMAL")
        and not (c.endswith("_id") or c in ("player_id", "season"))
    ]
    n = con.execute(f"SELECT COUNT(*) FROM derived.{t}").fetchone()[0]
    print(f"{t}: rows={n} ids={ids} #numeric_feats={len(numeric)} sample={numeric[:5]}")
