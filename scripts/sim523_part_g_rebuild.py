"""
scripts/sim523_part_g_rebuild.py — SIM-523 part G: the data-add window, end to end.

Runs with the app STOPPED (the forkserver holds the DuckDB writer lock —
SIM-524). About three hours detached:

1. Migration 0024 (fielder sprint_speed; the outcome pool's alignment
   fielder_2..9 + the putout / assist position masks). Idempotent.
2. Sprint speed into the profiles: derived.baserunner_season_metrics (the
   column and the join existed; raw.sprint_speed was EMPTY until part G ran
   the loader) and derived.fielder_season_metrics (the new column), from
   raw.sprint_speed through the Postgres attachment.
3. The SIM-469 pitch-pool rebuild (scripts/sim518_rebuild_pools.py, as is:
   migration 0023, the rebuild, its independent verification, the pool
   export, the loader round-trip) — the batting side, the pitch count and
   the times-through-order reach the pitch pool.
4. The outcome-pool rebuild for the window seasons with the sim523g.1
   builder (the chain), verified: per-season row counts unchanged; on out
   plays the putout mask is set on ~all rows; grounded-into-double-play rows
   carry two putout positions and an assist; hits carry no mask.
5. Re-export the batted-ball pool (the chain columns; the pitch-id join),
   then the actor embeddings (the baserunner and fielder speeds; the catcher
   embedding without its got-away columns).
6. The loader round-trip: BattedBallPool.fielders / putout_mask / assist_mask
   present at the measured rates; the embeddings carry the speed.

    docker compose stop app
    MSYS_NO_PATHCONV=1 docker compose run -d -T --name sim523_part_g \\
        -v "$PWD/scripts:/app/scripts" app python scripts/sim523_part_g_rebuild.py
    ... then `docker compose up -d app`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import duckdb  # noqa: E402
import numpy as np  # noqa: E402

DUCKDB_PATH = os.environ.get("BASEBALL_DUCKDB_PATH", "/data/baseball_sim.duckdb")
ART_DIR = os.environ.get("BASEBALL_ENGINE_ARTIFACT_DIR", "/data/play_pool/engine_artifacts")
SEASONS = [2023, 2024, 2025, 2026]
MIGRATION = _ROOT / "db" / "migrations" / "duckdb" / "0024_sim523_part_g_data_adds.sql"
_SEASON_LIST = ", ".join(str(s) for s in SEASONS)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _dsn() -> str:
    return os.environ.get(
        "BASEBALL_DB_DSN", "postgresql://baseball_user:baseball_pass@db:5432/baseball_sim"
    )


def _outcome_counts(con: duckdb.DuckDBPyConnection) -> dict[int, int]:
    return {
        int(s): int(n)
        for s, n in con.execute(
            f"SELECT season, COUNT(*) FROM sim.outcome_pool WHERE season IN ({_SEASON_LIST}) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }


def step_migration_and_speed() -> bool:
    _log("=== 1. migration 0024 ===")
    con = duckdb.connect(DUCKDB_PATH)
    try:
        con.execute(MIGRATION.read_text(encoding="utf-8"))
        con.execute(f"ATTACH '{_dsn()}' AS pg (TYPE postgres, READ_ONLY);")
        _log("=== 2. sprint speed into the profiles ===")
        n_raw = con.execute("SELECT COUNT(*) FROM pg.raw.sprint_speed").fetchone()[0]
        _log(f"raw.sprint_speed rows: {n_raw}")
        if n_raw == 0:
            _log("raw.sprint_speed is EMPTY — run pipeline/etl/etl_sprint_speed_loader.py first")
            return False
        con.execute(
            "UPDATE derived.baserunner_season_metrics AS b SET sprint_speed = ss.sprint_speed "
            "FROM pg.raw.sprint_speed ss "
            "WHERE b.player_id = ss.player_id AND b.season = ss.season"
        )
        con.execute(
            "UPDATE derived.fielder_season_metrics AS f SET sprint_speed = ss.sprint_speed "
            "FROM pg.raw.sprint_speed ss "
            "WHERE f.player_id = ss.player_id AND f.season = ss.season"
        )
        for t in ("baserunner_season_metrics", "fielder_season_metrics"):
            rows = con.execute(
                f"SELECT season, COUNT(*), COUNT(sprint_speed), ROUND(AVG(sprint_speed), 2) "
                f"FROM derived.{t} WHERE season IN ({_SEASON_LIST}) GROUP BY 1 ORDER BY 1"
            ).fetchall()
            _log(f"{t} (season, rows, with a speed, mean ft/s): {rows}")
    finally:
        con.close()
    return True


def step_sim469() -> bool:
    _log("=== 3. the SIM-469 pitch-pool rebuild (sim518_rebuild_pools) ===")
    import sim518_rebuild_pools

    rc = sim518_rebuild_pools.main()
    _log(f"sim518_rebuild_pools returned {rc}")
    return rc == 0


def step_outcome_pool() -> bool:
    _log("=== 4. the outcome-pool rebuild (the chain, sim523g.1) ===")
    from pipeline.batch.player_profile_computor import POOL_BUILDER_VERSION, PlayerProfileComputor

    assert POOL_BUILDER_VERSION == "sim523g.1", POOL_BUILDER_VERSION
    con = duckdb.connect(DUCKDB_PATH)
    try:
        con.execute(f"ATTACH '{_dsn()}' AS pg (TYPE postgres, READ_ONLY);")
        before = _outcome_counts(con)
        _log(f"pre-rebuild outcome-pool rows by season: {before}")
        t0 = time.time()
        comp = PlayerProfileComputor.__new__(PlayerProfileComputor)
        comp._conn = con
        comp._build_outcome_pool(SEASONS, incremental=False)
        _log(f"outcome-pool rebuild done in {(time.time() - t0) / 60:.1f} min")
        after = _outcome_counts(con)
        # The invariant: one outcome-pool row per in-play pitch-pool row. The
        # current season GROWS when the outcome pool catches up with games the
        # pitch pool already holds (2026 read 92,709 -> 103,714 on 2026-09-09).
        expected = {
            int(s): int(n)
            for s, n in con.execute(
                "SELECT season, COUNT(*) FROM sim.pitch_pool WHERE outcome_type = 'in_play' "
                f"AND season IN ({_SEASON_LIST}) GROUP BY 1 ORDER BY 1"
            ).fetchall()
        }
        ok = all(after.get(s) == expected.get(s) for s in SEASONS)
        _log(
            f"post-rebuild rows by season: {after}; the pitch pool's in-play rows: {expected} "
            f"{'OK' if ok else 'MISMATCH'}; before: {before}"
        )
        rows = con.execute(
            f"""
            SELECT season,
                   COUNT(*) AS n,
                   SUM(CASE WHEN fielder_6 IS NOT NULL THEN 1 ELSE 0 END) AS with_alignment,
                   SUM(CASE WHEN result_outs >= 1 THEN 1 ELSE 0 END) AS out_rows,
                   SUM(CASE WHEN result_outs >= 1 AND putout_pos_mask > 0 THEN 1 ELSE 0 END)
                       AS out_rows_with_putout,
                   SUM(CASE WHEN result_hits >= 1 AND putout_pos_mask > 0 THEN 1 ELSE 0 END)
                       AS hit_rows_with_putout,
                   SUM(CASE WHEN events = 'grounded_into_double_play' THEN 1 ELSE 0 END) AS gidp,
                   SUM(CASE WHEN events = 'grounded_into_double_play'
                              AND bit_count(putout_pos_mask::INTEGER) = 2
                              AND assist_pos_mask > 0 THEN 1 ELSE 0 END) AS gidp_full_chain
            FROM sim.outcome_pool WHERE season IN ({_SEASON_LIST}) GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        for r in rows:
            season, n, align, outs, outs_po, hits_po, gidp, gidp_full = r
            _log(
                f"  {season}: rows {n}, alignment {align / n:.4f}, out rows with a putout "
                f"{outs_po / max(1, outs):.4f}, hit rows with a putout {hits_po / n:.5f}, "
                f"GIDP with the full chain {gidp_full}/{gidp}"
            )
            ok = ok and (outs_po / max(1, outs)) > 0.995 and (gidp_full / max(1, gidp)) > 0.97
        meta = con.execute(
            "SELECT season, builder_version FROM sim.pool_build_metadata "
            f"WHERE pool_name = 'outcome_pool' AND season IN ({_SEASON_LIST}) ORDER BY season"
        ).fetchall()
        _log(f"  pool_build_metadata: {meta}")
        if not ok:
            _log("OUTCOME-POOL VERIFICATION FAILED — the batted-ball artifact is NOT exported")
            return False
        _log("=== 5. artifact export: the batted-ball pool, then the actor embeddings ===")
        from pipeline.batch.engine_artifacts import (
            build_actor_embeddings,
            build_battedball_pool_artifact,
        )

        build_battedball_pool_artifact(con, ART_DIR, SEASONS)
        build_actor_embeddings(con, ART_DIR)
    finally:
        con.close()
    return True


def step_round_trip() -> bool:
    _log("=== 6. loader round-trip ===")
    from pipeline.batch.engine_artifacts import EngineArtifacts

    a = EngineArtifacts.load(ART_DIR)
    ok = True
    for hand, bb in a.bb_pools.items():
        if bb.fielders is None or bb.putout_mask is None or bb.assist_mask is None:
            _log(f"  bb_pool[{hand}]: the chain is MISSING")
            ok = False
            continue
        outs = bb.result_outs >= 1
        _log(
            f"  bb_pool[{hand}]: fielders {bb.fielders.shape}, alignment "
            f"{float((bb.fielders[:, 4] > 0).mean()):.4f}, out rows with a putout "
            f"{float((bb.putout_mask[outs] > 0).mean()):.4f}, rows with an assist "
            f"{float((bb.assist_mask > 0).mean()):.4f}"
        )
    for actor in ("baserunner", "fielder"):
        emb = a.actor_emb.get(actor)
        feats = list(emb.get("features", [])) if emb else []
        if emb is None or "sprint_speed" not in feats:
            _log(f"  {actor}_emb: NO sprint_speed feature")
            ok = False
            continue
        col = feats.index("sprint_speed")
        v = np.asarray(emb["vecs"])[:, col]
        keys = emb.get("keys")
        if keys is None:
            keys = [k for k, _ in sorted(emb["key_index"].items(), key=lambda kv: kv[1])]
        in_window = np.array([int(str(k).split(":")[-1]) in SEASONS for k in keys])
        share_window = float((v[in_window] != 0).mean()) if in_window.any() else 0.0
        _log(
            f"  {actor}_emb: sprint_speed nonzero {float((v != 0).mean()):.4f} of {v.shape[0]} rows "
            f"(the window seasons: {share_window:.4f}), "
            f"mean {float(emb['mean'][col]):.2f} std {float(emb['std'][col]):.2f}"
        )
        ok = ok and share_window > 0.9
    cemb = a.actor_emb.get("catcher")
    cfeats = list(cemb.get("features", [])) if cemb else []
    leaked = [f for f in cfeats if "pbwp" in f or "block" in f or "uncaught" in f]
    _log(f"  catcher_emb features: {len(cfeats)}; got-away-derived left: {leaked}")
    ok = ok and not leaked
    with open(os.path.join(ART_DIR, "battedball_pool", "manifest.json"), encoding="utf-8") as fh:
        _log(f"  battedball manifest chain flag: {json.load(fh).get('chain')}")
    return ok


def main() -> int:
    t0 = time.time()
    if not step_migration_and_speed():
        return 2
    if not step_sim469():
        _log("the SIM-469 rebuild failed — stopping before the outcome pool")
        return 3
    if not step_outcome_pool():
        return 4
    ok = step_round_trip()
    _log(
        f"=== part G data window {'DONE' if ok else 'FAILED'} in {(time.time() - t0) / 60:.1f} min ==="
    )
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
