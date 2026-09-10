"""
scripts/sim518_rebuild_pools.py — the SIM-469 rebuild (SIM-518's data parts), end to end.

The pool-only rebuild the plan recommends (docs/audit/2026-09-04-sim467-518-plan.md
§6.5): about two hours detached, NOT the 5.7-hour profile recompute.

1. Apply DuckDB migration 0023 (pitch-pool bat_home + pitcher_pitch_count +
   times_through_order).
2. Rebuild sim.pitch_pool for the window seasons (2023-2026) with the
   sim518.1 builder. The outcome pool, the transition columns, the steal pool
   and the advancement pool are NOT rebuilt: this rebuild appends columns to
   the pitch pool and changes no row the outcome pool reads.
3. Verify the pool: per-season row counts unchanged from before the rebuild;
   bat_home about half per season; the two fatigue columns recomputed
   INDEPENDENTLY from raw.pitches (correlated subqueries, a different
   formulation from the builder's windows) on a random sample of hundreds of
   games and compared row for row.
4. Re-export the engine-artifact pools (the pitch pool gains the three
   columns; the batted-ball pool gains {hand}.pgeom.npy + pitcher_id).
5. Verify the loader round-trip: HandPool.bat_home / pitch_count / tto and
   BattedBallPool.pgeom / pitcher_id present, at the same rates.

Run detached from the repo root (scripts/ is NOT bind-mounted):

    MSYS_NO_PATHCONV=1 docker compose run -d --rm \\
        -v "$PWD/scripts:/app/scripts" app python scripts/sim518_rebuild_pools.py

The live app keeps its in-memory bundle until it restarts; every SIM-518
consumer is OFF, so the restart changes nothing observable.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
ART_DIR = os.environ.get("BASEBALL_ENGINE_ARTIFACT_DIR", "/data/play_pool/engine_artifacts")
SEASONS = [2023, 2024, 2025, 2026]
SAMPLE_GAMES = int(os.environ.get("SIM518_SAMPLE_GAMES", "500"))
MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0023_sim518_pitch_pool_conditioning.sql"

_SEASON_LIST = ", ".join(str(s) for s in SEASONS)


def _season_counts(con: duckdb.DuckDBPyConnection) -> dict[int, int]:
    return {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM sim.pitch_pool WHERE season IN ({_SEASON_LIST}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    dsn = os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )
    t0 = time.time()

    _log("=== 1. migration 0023 ===")
    con = duckdb.connect(DUCKDB_PATH)
    con.execute(MIGRATION.read_text(encoding="utf-8"))
    con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY);")
    before = _season_counts(con)
    _log(f"pre-rebuild pitch-pool rows by season: {before}")

    _log("=== 2. pitch-pool rebuild (sim518.1) ===")
    from pipeline.batch.player_profile_computor import POOL_BUILDER_VERSION, PlayerProfileComputor

    assert POOL_BUILDER_VERSION in ("sim518.1", "sim523g.1"), POOL_BUILDER_VERSION
    comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
    comp._conn = con
    comp._build_pitch_pool(SEASONS, incremental=False)
    _log(f"rebuild done in {(time.time() - t0) / 60:.1f} min")

    _log("=== 3. pool verification ===")
    ok = True
    after = _season_counts(con)
    for s in SEASONS:
        same = before.get(s) == after.get(s)
        ok = ok and same
        _log(
            f"  {s}: rows before={before.get(s)} after={after.get(s)} {'OK' if same else 'MISMATCH'}"
        )
    rows = con.execute(
        "SELECT season, COUNT(*), "
        "COUNT(*) FILTER (WHERE bat_home), "
        "COUNT(*) FILTER (WHERE bat_home IS NULL OR pitcher_pitch_count IS NULL "
        "OR times_through_order IS NULL), "
        "MIN(pitcher_pitch_count), MAX(pitcher_pitch_count), "
        "MIN(times_through_order), MAX(times_through_order), "
        "AVG(times_through_order) "
        f"FROM sim.pitch_pool WHERE season IN ({_SEASON_LIST}) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for season, n, home, nulls, pc_min, pc_max, tto_min, tto_max, tto_avg in rows:
        share = home / n
        good = nulls == 0 and 0.45 < share < 0.55 and pc_min == 0 and tto_min == 1 and tto_max <= 6
        ok = ok and good
        _log(
            f"  {season}: bat_home share={share:.4f} nulls={nulls} "
            f"pitch_count [{pc_min}, {pc_max}] tto [{tto_min}, {tto_max}] avg {tto_avg:.3f} "
            f"{'OK' if good else 'MISMATCH'}"
        )
    # Independent recomputation on a random sample of games (correlated
    # subqueries over raw.pitches — a different formulation from the builder's
    # window functions), compared row for row.
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE sample_games AS "
        f"SELECT DISTINCT game_pk FROM sim.pitch_pool WHERE season IN ({_SEASON_LIST}) "
        f"USING SAMPLE {SAMPLE_GAMES} ROWS"
    )
    n_games = con.execute("SELECT COUNT(*) FROM sample_games").fetchone()[0]
    mism = con.execute(
        """
        WITH raw AS (
            SELECT p.game_pk, p.at_bat_number, p.pitch_number, p.pitcher, p.inning_topbot,
                   (p.game_pk || LPAD(p.at_bat_number::VARCHAR, 3, '0')
                              || LPAD(p.pitch_number::VARCHAR, 3, '0'))::BIGINT AS pitch_id
            FROM pg.raw.pitches p
            WHERE p.data_quality_flag = FALSE
              AND p.game_pk IN (SELECT game_pk FROM sample_games)
        ),
        indep AS (
            SELECT r.pitch_id,
                   (r.inning_topbot = 'Bot') AS bat_home_i,
                   (SELECT COUNT(*) FROM raw r2
                     WHERE r2.game_pk = r.game_pk AND r2.pitcher = r.pitcher
                       AND r2.at_bat_number < r.at_bat_number) AS pc_i,
                   ((SELECT COUNT(DISTINCT r3.at_bat_number) FROM raw r3
                      WHERE r3.game_pk = r.game_pk AND r3.pitcher = r.pitcher
                        AND r3.at_bat_number < r.at_bat_number) // 9 + 1) AS tto_i
            FROM raw r
        )
        SELECT COUNT(*) AS compared,
               COUNT(*) FILTER (WHERE pp.bat_home IS DISTINCT FROM i.bat_home_i) AS bad_home,
               COUNT(*) FILTER (WHERE pp.pitcher_pitch_count IS DISTINCT FROM i.pc_i) AS bad_pc,
               COUNT(*) FILTER (WHERE pp.times_through_order IS DISTINCT FROM i.tto_i) AS bad_tto
        FROM sim.pitch_pool pp JOIN indep i USING (pitch_id)
        """
    ).fetchone()
    compared, bad_home, bad_pc, bad_tto = mism
    good = compared > 0 and bad_home == 0 and bad_pc == 0 and bad_tto == 0
    ok = ok and good
    _log(
        f"  independent recomputation on {n_games} games / {compared} pitches: "
        f"bat_home mismatches={bad_home} pitch_count={bad_pc} tto={bad_tto} "
        f"{'OK' if good else 'MISMATCH'}"
    )
    meta = con.execute(
        "SELECT season, builder_version FROM sim.pool_build_metadata "
        f"WHERE pool_name='pitch_pool' AND season IN ({_SEASON_LIST}) ORDER BY 1"
    ).fetchall()
    # SIM-523 part G: the pool builder version moved to sim523g.1 (the chain); the
    # rebuild records the CURRENT version, so the check reads it from the module.
    versions_ok = all(v == POOL_BUILDER_VERSION for _s, v in meta) and len(meta) == len(SEASONS)
    ok = ok and versions_ok
    _log(f"  pool_build_metadata: {meta} {'OK' if versions_ok else 'MISMATCH'}")
    con.close()
    if not ok:
        _log("POOL VERIFICATION FAILED — artifacts NOT exported")
        return 2

    _log("=== 4. artifact export (pools) ===")
    from pipeline.batch.engine_artifacts import main as art_main

    rc = art_main(["--duckdb-path", DUCKDB_PATH, "--out-dir", ART_DIR, "--what", "pool"]) or 0
    if rc:
        _log("ARTIFACT EXPORT FAILED")
        return rc

    _log("=== 5. loader round-trip ===")
    from pipeline.batch.engine_artifacts import EngineArtifacts

    art = EngineArtifacts.load(ART_DIR)
    for hand in ("L", "R"):
        p = art.pools[hand]
        assert p.bat_home is not None and p.pitch_count is not None and p.tto is not None, (
            f"{hand}: pitch-pool conditioning columns missing"
        )
        home = float((np.asarray(p.bat_home) > 0).mean())
        unknown = int((np.asarray(p.bat_home) < 0).sum() + (np.asarray(p.pitch_count) < 0).sum())
        _log(
            f"  pool[{hand}]: n={p.n} bat_home share={home:.4f} unknown={unknown} "
            f"pitch_count max={int(np.asarray(p.pitch_count).max())} "
            f"tto mean={float(np.asarray(p.tto).mean()):.3f}"
        )
        bb = art.bb_pools[hand]
        assert bb.pgeom is not None and bb.pitcher_id is not None, f"{hand}: bb pgeom missing"
        nan_rows = int((~np.isfinite(bb.pgeom).all(axis=1)).sum())
        _log(
            f"  bb_pool[{hand}]: n={bb.n} pgeom {bb.pgeom.shape} rows-with-a-NaN={nan_rows} "
            f"velo mean={float(np.nanmean(bb.pgeom[:, 0])):.2f} "
            f"pitcher coverage={float((np.asarray(bb.pitcher_id) > 0).mean()):.4%}"
        )
    _log(f"SIM-518 REBUILD COMPLETE in {(time.time() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
