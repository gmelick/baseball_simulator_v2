"""
scripts/sim517_part_b_rebuild.py — the SIM-517 part-B rebuild, end to end.

1. Apply DuckDB migration 0022 (pitch-pool catcher_id + got_away).
2. Rebuild sim.pitch_pool for the window seasons (2023-2026) with the
   sim517.1 builder.
3. Verify the pool: got_away volume vs the raw per-season measurement,
   catcher_id coverage.
4. Re-export the engine-artifact pools + actor embeddings (the new
   uncaught_k3_rate_eb column enters the catcher embedding here).
5. Verify the loader round-trip: HandPool.catcher_id / got_away present and
   at the same rates.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402

DUCKDB_PATH = "/data/baseball_sim.duckdb"
ART_DIR = "/data/play_pool/engine_artifacts"
SEASONS = [2023, 2024, 2025, 2026]


def main() -> int:
    dsn = os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )

    print("=== 1. migration 0022 ===", flush=True)
    con = duckdb.connect(DUCKDB_PATH)
    con.execute(
        (_ROOT / "db" / "migrations" / "duckdb" / "0022_sim517_pitch_pool_catcher.sql").read_text(
            encoding="utf-8"
        )
    )
    con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY);")

    print("=== 2. pitch-pool rebuild (sim517.1) ===", flush=True)
    from pipeline.batch.player_profile_computor import PlayerProfileComputor

    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = con
    comp._build_pitch_pool(SEASONS, incremental=False)

    print("=== 3. pool verification ===", flush=True)
    rows = con.execute(
        "SELECT season, COUNT(*), "
        "COUNT(*) FILTER (WHERE got_away), "
        "COUNT(*) FILTER (WHERE catcher_id IS NOT NULL AND catcher_id > 0), "
        "COUNT(*) FILTER (WHERE got_away AND outcome_type IN "
        "('swinging_strike','called_strike') AND events IN "
        "('strikeout','strikeout_double_play')) "
        "FROM sim.pitch_pool WHERE season IN (2023,2024,2025,2026) "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    expected_got_away = {2023: 1724 + 99, 2024: 1587 + 69, 2025: 1575 + 78, 2026: 1315 + 50}
    ok = True
    for season, n, got, catchered, k3_got in rows:
        exp = expected_got_away[season]
        # The pool excludes data_quality_flag rows, so allow a small deficit.
        drift = abs(got - exp) / exp
        status = "OK" if (drift < 0.02 and catchered / n > 0.999) else "MISMATCH"
        ok = ok and status == "OK"
        print(
            f"  {season}: rows={n}  got_away={got} (raw {exp}, drift {drift:+.3%})  "
            f"catcher_id coverage={catchered / n:.4%}  uncaught_k3={k3_got}  {status}",
            flush=True,
        )
    con.close()
    if not ok:
        print("POOL VERIFICATION FAILED", flush=True)
        return 2

    print("=== 4. artifact export (pools + actors) ===", flush=True)
    from pipeline.batch.engine_artifacts import main as art_main

    rc = art_main(["--duckdb-path", DUCKDB_PATH, "--out-dir", ART_DIR, "--what", "pool"]) or 0
    rc |= art_main(["--duckdb-path", DUCKDB_PATH, "--out-dir", ART_DIR, "--what", "actors"]) or 0
    if rc:
        print("ARTIFACT EXPORT FAILED", flush=True)
        return rc

    print("=== 5. loader round-trip ===", flush=True)
    from pipeline.batch.engine_artifacts import EngineArtifacts

    art = EngineArtifacts.load(ART_DIR)
    for hand in ("L", "R"):
        p = art.pools[hand]
        assert p.catcher_id is not None and p.got_away is not None, f"{hand}: columns missing"
        rate = float(np.asarray(p.got_away, dtype=np.int64).sum()) / p.n
        cov = float((np.asarray(p.catcher_id) > 0).mean())
        print(f"  pool[{hand}]: n={p.n}  got_away rate={rate:.5f}  catcher coverage={cov:.4%}")
    cemb = art.actor_emb.get("catcher")
    assert cemb is not None and "uncaught_k3_rate_eb" in list(cemb.get("features", [])), (
        "uncaught_k3_rate_eb missing from the catcher embedding"
    )
    samples = [f for f in cemb["features"] if f.startswith("sample_")]
    assert not samples, f"sample_ columns leaked into the embedding: {samples}"
    print(
        f"  catcher embedding: {len(cemb['features'])} features, "
        "uncaught_k3_rate_eb present, no sample_ leakage"
    )
    print("PART B REBUILD COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
