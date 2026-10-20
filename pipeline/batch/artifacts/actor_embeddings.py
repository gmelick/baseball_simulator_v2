"""The actor embeddings: each player's standardized profile vector by season."""

from __future__ import annotations

import json
import logging
import os

import duckdb
import numpy as np

from pipeline.batch.artifacts.common import _savez_plain

log = logging.getLogger("engine_artifacts")


#: Actor engines whose per-(id, season) season-metrics vector is exported as a v1
#: embedding (the worker z-scores + RBFs it for the f_batter / f_catcher / ... factor).
#: SIM-424/425/426/427 refine these to each engine's exact feature selection/weights.
_ACTOR_TABLES = {
    "batter": "batter_season_metrics",
    "catcher": "catcher_season_metrics",
    "fielder": "fielder_season_metrics",
    "baserunner": "baserunner_season_metrics",
    "manager": "manager_season_metrics",
    # SIM-474: the pitcher's hold-runner outcomes (sb_against_per_9,
    # cs_rate_forced, steal_attempt_rate_allowed) weight the steal draw. The
    # id column resolves to pitcher_id via the `endswith("_id")` rule below.
    "pitcher_steal": "pitcher_steal_metrics",
}
_NUMERIC_TYPES = {"DOUBLE", "FLOAT", "REAL", "INTEGER", "BIGINT", "SMALLINT", "DECIMAL"}


#: SIM-523 part G (the plan's "got-away rates out of any profile used for
#: selection"): the catcher's got-away-derived columns never enter a
#: similarity kernel again. Part E's receiving ratio reads got-aways from the
#: pool itself as a draw WEIGHT on the got-away row (the intended use); a
#: catcher's got-away rate as a SELECTION feature proxied his staff's wildness
#: (the SIM-523 confound).
_EMBEDDING_EXCLUDE: dict[str, frozenset[str]] = {
    "catcher": frozenset(
        {
            "expected_pbwp",
            "actual_pbwp",
            "blocks_above_average",
            "blocking_runs",
            "blocks_aa_bounced",
            "blocks_aa_high",
            "blocks_aa_lateral",
            "sample_uncaught_k3",
            "uncaught_k3_rate_eb",
        }
    ),
}


def build_actor_embeddings(con: duckdb.DuckDBPyConnection, out_dir: str) -> dict[str, int]:
    """Export per-(actor_id, season) embeddings + global mean/std for each actor
    engine, so the worker can z-score + RBF-score the actor factor on the fly."""
    os.makedirs(out_dir, exist_ok=True)
    counts: dict[str, int] = {}
    for actor, table in _ACTOR_TABLES.items():
        schema = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema='derived' AND table_name='{table}' ORDER BY ordinal_position"
        ).fetchall()
        id_col = next(c for c, _ in schema if c.endswith("_id") or c == "player_id")
        feats = [
            c
            for c, dt in schema
            if dt.upper() in _NUMERIC_TYPES
            and c not in (id_col, "season")
            and not c.startswith("sample_")
            and c not in _EMBEDDING_EXCLUDE.get(actor, frozenset())
        ]
        # SIM-425b: the fielder engine is POSITION-PARTITIONED — the same player has
        # one row per position they played, so keying the embedding by
        # player_id:season alone collapses multi-position fielders (last-wins;
        # ~11.3k rows → ~5.3k keys). Key the FIELDER embedding by
        # player_id:position:season so each (player, position, season) stays distinct.
        # Every other actor keeps the player_id:season key its consumer looks up
        # (batter/catcher/baserunner key lookups in FullPoolSampler).
        use_position = actor == "fielder" and any(c == "position" for c, _ in schema)
        select = [id_col, "season"] + (["position"] if use_position else []) + feats
        d = con.execute(f"SELECT {', '.join(select)} FROM derived.{table}").fetchnumpy()
        ids = np.ma.filled(d[id_col], 0).astype(np.int64)
        seasons = np.ma.filled(d["season"], 0).astype(np.int64)
        if use_position:
            positions = np.asarray(d["position"], dtype=object)
            keys = [
                f"{int(i)}:{p}:{int(s)}" for i, p, s in zip(ids, positions, seasons, strict=False)
            ]
        else:
            keys = [f"{int(i)}:{int(s)}" for i, s in zip(ids, seasons, strict=False)]
        mat = np.stack(
            [np.ma.filled(np.ma.asarray(d[f]).astype(np.float32), np.nan) for f in feats],
            axis=1,
        )
        mean = np.nan_to_num(np.nanmean(mat, axis=0)).astype(np.float32)
        std = np.nan_to_num(np.nanstd(mat, axis=0), nan=1.0).astype(np.float32)
        std[std == 0.0] = 1.0
        _savez_plain(
            os.path.join(out_dir, f"{actor}_emb.npz"),
            keys=json.dumps(keys),
            vecs=np.nan_to_num(mat).astype(np.float32),
            mean=mean,
            std=std,
            features=json.dumps(feats),
        )
        counts[actor] = len(keys)
        log.info("%s_emb: %d (id,season) rows × %d features", actor, len(keys), len(feats))
    return counts


def load_actor_embeddings(art_dir: str, views: dict[str, np.ndarray]) -> dict[str, dict]:
    """Load each actor's embedding; an actor without a file is absent."""
    actor_emb: dict[str, dict] = {}
    for actor in _ACTOR_TABLES:
        p = os.path.join(art_dir, f"{actor}_emb.npz")
        if os.path.exists(p):
            # When all three shareable arrays for this actor are in views,
            # SKIP the .npz load entirely — load only the keys + features
            # (the small JSON members) from a lightweight read.
            vecs_v = views.get(f"actor_emb.{actor}.vecs")
            mean_v = views.get(f"actor_emb.{actor}.mean")
            std_v = views.get(f"actor_emb.{actor}.std")
            if all(isinstance(v, np.ndarray) for v in (vecs_v, mean_v, std_v)):
                # Cheap partial-load of the npz: keys + features are tiny.
                z = np.load(p, allow_pickle=False)
                keys = json.loads(str(z["keys"]))
                features = json.loads(str(z["features"]))
                actor_emb[actor] = {
                    "key_index": {k: i for i, k in enumerate(keys)},
                    "vecs": vecs_v,
                    "mean": mean_v,
                    "std": std_v,
                    "features": features,
                }
            else:
                z = np.load(p, allow_pickle=False)
                keys = json.loads(str(z["keys"]))
                actor_emb[actor] = {
                    "key_index": {k: i for i, k in enumerate(keys)},
                    "vecs": z["vecs"],
                    "mean": z["mean"],
                    "std": z["std"],
                    "features": json.loads(str(z["features"])),
                }
    return actor_emb
