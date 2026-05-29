"""
tests/regression/generate_fixtures.py — SIM-147
=================================================
Golden-file generator for the similarity engine regression gate.

Run this script ONCE after any intentional engine change (weight update,
sigma re-calibration, new features, etc.) to update the golden snapshots.
The updated JSON files should then be committed to the repo.

Usage
-----
    # From repo root (with dev deps installed):
    python tests/regression/generate_fixtures.py

    # Preview what would change (dry run):
    python tests/regression/generate_fixtures.py --dry-run

    # Force overwrite even if files already exist:
    python tests/regression/generate_fixtures.py --force

The script reuses the EXACT same engine construction logic as conftest.py
(via importlib) so the generated snapshots are guaranteed to match what the
test suite will see.

Output
------
One JSON file per engine under tests/regression/fixtures/:
    baserunner_steal.json
    catcher.json
    pitcher_steal.json
    manager.json
    situation.json

File schema
-----------
{
    "engine": "<name>",
    "generated_at": "<ISO-8601 UTC>",
    "seed": 2026,
    "n_profiles": <int>,
    "queries": [
        {
            "query_key": [<id>, <season>],          # [player/catcher/pitcher/manager id, season]
            "top5_keys": [[<id>, <season>], ...],   # top-5 result keys
            "top5_scores": [<float>, ...]           # composite scores for top-5
        },
        ...
    ]
}

For the situation engine the schema is slightly different (no profile_ids):
{
    "engine": "situation",
    "generated_at": "...",
    "seed": 2026,
    "n_situations": <int>,
    "queries": [
        {
            "situation_vector": { ... },     # SituationVector fields as dict
            "top5_play_ids": [<int>, ...],
            "top5_distances": [<float>, ...]
        },
        ...
    ]
}
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import numpy as np

# Ensure repo root is on the path
REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.regression.regression_config import (  # noqa: E402
    FIXTURES_DIR,
    N_FIXTURE_QUERIES,
    REGRESSION_SEED,
    TOP_K_STABLE,
)

# ---------------------------------------------------------------------------
# Engine builders  (mirrors conftest.py exactly)
# ---------------------------------------------------------------------------


def _build_steal_engine():
    from similarity.engines.baserunner_steal_similarity import (
        RBF_SIGMA_SUCCESS,
        RBF_SIGMA_TENDENCY,
        SUCCESS_FEATURES,
        TENDENCY_FEATURES,
        BaserunnerStealProfile,
        BaserunnerStealSimilarityEngine,
        FeatureNormalizer,
        StealPartition,
        WeightedRBFSimilarity,
    )

    rng = np.random.default_rng(REGRESSION_SEED)
    n = 12
    profiles = [
        BaserunnerStealProfile(
            player_id=1000 + i,
            season=2024,
            sample_steal_attempts=40 + i * 3,
            sample_first_base_opps=150 + i * 10,
            tendency_vec=rng.uniform(0.05, 0.95, len(TENDENCY_FEATURES)),
            success_vec=rng.uniform(0.55, 0.90, len(SUCCESS_FEATURES)),
            eb_alpha=1.0,
        )
        for i in range(n)
    ]
    engine = BaserunnerStealSimilarityEngine.__new__(BaserunnerStealSimilarityEngine)
    engine._profiles = {(p.player_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = StealPartition()
    engine._partition.build(profiles, engine._normalizer)
    engine._tend_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_TENDENCY, np.array([w for _, w in TENDENCY_FEATURES])
    )
    engine._succ_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_SUCCESS, np.array([w for _, w in SUCCESS_FEATURES])
    )
    return engine, "baserunner_steal"


def _build_catcher_engine():
    from similarity.engines.catcher_similarity import (
        BLOCKING_FEATURES,
        DETERRENCE_FEATURES,
        FRAMING_FEATURES,
        OFFENSE_FEATURES,
        RBF_SIGMA_BLOCKING,
        RBF_SIGMA_DETERRENCE,
        RBF_SIGMA_FRAMING,
        RBF_SIGMA_OFFENSE,
        RBF_SIGMA_THROWING,
        THROWING_FEATURES,
        CatcherPartition,
        CatcherProfile,
        CatcherSimilarityEngine,
        FeatureNormalizer,
        WeightedRBFSimilarity,
    )

    rng = np.random.default_rng(REGRESSION_SEED)
    n = 12
    profiles = [
        CatcherProfile(
            catcher_id=2000 + i,
            season=2024,
            sample_pitches_received=500 + i * 50,
            sample_block_opps=80 + i * 5,
            sample_steal_attempts_against=30 + i * 3,
            framing_vec=rng.uniform(-0.05, 0.15, len(FRAMING_FEATURES)),
            blocking_vec=rng.uniform(0.0, 0.05, len(BLOCKING_FEATURES)),
            throwing_vec=rng.uniform(0.0, 1.0, len(THROWING_FEATURES)),
            # SIM-072: steal_attempt_rate_against ~ [0.02, 0.18] for MLB catchers
            deterrence_vec=rng.uniform(0.02, 0.18, len(DETERRENCE_FEATURES)),
            offense_vec=rng.uniform(0.0, 1.0, len(OFFENSE_FEATURES)),
            eb_alpha=1.0,
        )
        for i in range(n)
    ]
    engine = CatcherSimilarityEngine.__new__(CatcherSimilarityEngine)
    engine._profiles = {(p.catcher_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = CatcherPartition()
    engine._partition.build(profiles, engine._normalizer)
    engine._framing_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_FRAMING, np.array([w for _, w in FRAMING_FEATURES])
    )
    engine._blocking_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_BLOCKING, np.array([w for _, w in BLOCKING_FEATURES])
    )
    engine._throwing_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_THROWING, np.array([w for _, w in THROWING_FEATURES])
    )
    engine._deterrence_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_DETERRENCE, np.array([w for _, w in DETERRENCE_FEATURES])
    )
    engine._offense_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_OFFENSE, np.array([w for _, w in OFFENSE_FEATURES])
    )
    return engine, "catcher"


def _build_pitcher_steal_engine():
    from similarity.engines.pitcher_steal_similarity import (
        OUTCOME_FEATURES,
        RBF_SIGMA_OUTCOME,
        FeatureNormalizer,
        PitcherStealPartition,
        PitcherStealProfile,
        PitcherStealSimilarityEngine,
        WeightedRBFSimilarity,
    )

    rng = np.random.default_rng(REGRESSION_SEED)
    n = 12
    profiles = [
        PitcherStealProfile(
            pitcher_id=3000 + i,
            season=2024,
            throws="R" if i % 2 == 0 else "L",
            sample_baserunner_events=60 + i * 5,
            sample_steal_attempts_against=15 + i * 2,
            outcome_vec=rng.uniform(0.0, 1.0, len(OUTCOME_FEATURES)),
            eb_alpha=1.0,
        )
        for i in range(n)
    ]
    engine = PitcherStealSimilarityEngine.__new__(PitcherStealSimilarityEngine)
    engine._profiles = {(p.pitcher_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = PitcherStealPartition()
    engine._partition.build(profiles, engine._normalizer)
    engine._out_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
    )
    return engine, "pitcher_steal"


def _build_manager_engine():
    from similarity.engines.manager_similarity import (
        AGGRESSION_FEATURES,
        PLATOON_FEATURES,
        RBF_SIGMA_AGGRESSION,
        RBF_SIGMA_PLATOON,
        RBF_SIGMA_USAGE,
        USAGE_FEATURES,
        FeatureNormalizer,
        ManagerPartition,
        ManagerProfile,
        ManagerSimilarityEngine,
        WeightedRBFSimilarity,
    )

    rng = np.random.default_rng(REGRESSION_SEED)
    n = 12
    profiles = [
        ManagerProfile(
            manager_id=4000 + i,
            season=2024,
            sample_games=80 + i * 5,
            sample_starter_decisions=20 + i * 2,
            usage_vec=rng.uniform(0.0, 1.0, len(USAGE_FEATURES)),
            aggression_vec=rng.uniform(0.0, 0.3, len(AGGRESSION_FEATURES)),
            platoon_vec=rng.uniform(0.0, 0.5, len(PLATOON_FEATURES)),
            eb_alpha=1.0,
        )
        for i in range(n)
    ]
    engine = ManagerSimilarityEngine.__new__(ManagerSimilarityEngine)
    engine._profiles = {(p.manager_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = ManagerPartition()
    engine._partition.build(profiles, engine._normalizer)
    engine._usage_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_USAGE, np.array([w for _, w in USAGE_FEATURES])
    )
    engine._agg_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_AGGRESSION, np.array([w for _, w in AGGRESSION_FEATURES])
    )
    engine._plat_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_PLATOON, np.array([w for _, w in PLATOON_FEATURES])
    )
    return engine, "manager"


def _build_situation_engine():
    from scipy.spatial import KDTree

    from similarity.engines.situation_similarity import (
        FEATURE_WEIGHTS,
        SCORE_DIFF_CLIP,
        NearestSituation,
        SituationNormalizer,
        SituationSimilarityEngine,
        SituationVector,
    )

    rng = np.random.default_rng(REGRESSION_SEED)
    n = 50
    situations = [
        SituationVector(
            inning=int(rng.integers(1, 10)),
            top_or_bottom=int(rng.integers(0, 2)),
            outs=int(rng.integers(0, 3)),
            runner_on_1b=int(rng.integers(0, 2)),
            runner_on_2b=int(rng.integers(0, 2)),
            runner_on_3b=int(rng.integers(0, 2)),
            score_differential=float(rng.integers(-4, 5)),
            leverage_index=float(rng.uniform(0.2, 3.5)),
            pitcher_pitch_count=int(rng.integers(0, 100)),
            batter_pa_count=int(rng.integers(0, 5)),
            park_factor_runs=float(rng.uniform(0.85, 1.15)),
        )
        for _ in range(n)
    ]
    index_meta = []
    raw_rows = []
    for i, sv in enumerate(situations):
        runners = (sv.runner_on_1b * 0b001) | (sv.runner_on_2b * 0b010) | (sv.runner_on_3b * 0b100)
        score_diff_clipped = float(
            np.clip(sv.score_differential, -SCORE_DIFF_CLIP, SCORE_DIFF_CLIP)
        )
        index_meta.append(
            NearestSituation(
                play_id=str(i),
                game_pk=700000 + i,
                distance=0.0,
                inning=sv.inning,
                outs=sv.outs,
                runners=runners,
                leverage_index=sv.leverage_index,
                score_differential=score_diff_clipped,
            )
        )
        raw_rows.append(sv.to_array())
    raw_matrix = np.array(raw_rows, dtype=np.float64)
    feature_scale = np.sqrt(np.array(FEATURE_WEIGHTS, dtype=np.float64))
    normalizer = SituationNormalizer()
    normalizer.fit(raw_matrix)
    engine = SituationSimilarityEngine.__new__(SituationSimilarityEngine)
    engine._index_meta = index_meta
    engine._normalizer = normalizer
    engine._feature_scale = feature_scale
    engine._index_size = n
    engine._kdtree = KDTree(normalizer.normalize_batch(raw_matrix) * feature_scale)
    return engine, "situation"


# ---------------------------------------------------------------------------
# Snapshot generation
# ---------------------------------------------------------------------------


def _generate_rbf_fixture(engine, engine_name: str, id_attr: str, dry_run: bool, force: bool):
    """Generate golden file for an RBF engine."""
    ids = engine.profile_ids()
    rng = np.random.default_rng(REGRESSION_SEED + 1)  # +1 to differ from build seed
    indices = rng.choice(len(ids), size=min(N_FIXTURE_QUERIES, len(ids)), replace=False)
    sample_ids = [ids[i] for i in indices]

    queries = []
    for qk in sample_ids:
        results = engine.query(qk[0], qk[1])
        top5 = results[:TOP_K_STABLE]
        queries.append(
            {
                "query_key": list(qk),
                "top5_keys": [[getattr(r, id_attr), r.season] for r in top5],
                "top5_scores": [r.score for r in top5],
            }
        )

    fixture = {
        "engine": engine_name,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "seed": REGRESSION_SEED,
        "n_profiles": engine.profile_count,
        "queries": queries,
    }
    _write_fixture(engine_name, fixture, dry_run, force)


def _generate_situation_fixture(engine, dry_run: bool, force: bool):
    """Generate golden file for the situation (KDTree) engine."""
    import dataclasses

    from similarity.engines.situation_similarity import SituationVector

    # Fixed query vectors — deterministic, not seeded from the index
    query_vectors = [
        SituationVector(
            inning=7,
            top_or_bottom=0,
            outs=1,
            runner_on_1b=0,
            runner_on_2b=1,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=2.1,
            pitcher_pitch_count=55,
            batter_pa_count=2,
            park_factor_runs=1.02,
        ),
        SituationVector(
            inning=3,
            top_or_bottom=1,
            outs=0,
            runner_on_1b=1,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=-1.0,
            leverage_index=0.8,
            pitcher_pitch_count=30,
            batter_pa_count=1,
            park_factor_runs=0.95,
        ),
        SituationVector(
            inning=9,
            top_or_bottom=0,
            outs=2,
            runner_on_1b=1,
            runner_on_2b=1,
            runner_on_3b=1,
            score_differential=1.0,
            leverage_index=3.2,
            pitcher_pitch_count=75,
            batter_pa_count=3,
            park_factor_runs=1.05,
        ),
        SituationVector(
            inning=1,
            top_or_bottom=0,
            outs=0,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=0.0,
            leverage_index=0.5,
            pitcher_pitch_count=0,
            batter_pa_count=0,
            park_factor_runs=1.00,
        ),
        SituationVector(
            inning=5,
            top_or_bottom=1,
            outs=1,
            runner_on_1b=0,
            runner_on_2b=0,
            runner_on_3b=0,
            score_differential=2.0,
            leverage_index=0.7,
            pitcher_pitch_count=45,
            batter_pa_count=2,
            park_factor_runs=0.98,
        ),
    ]

    queries = []
    for sv in query_vectors[:N_FIXTURE_QUERIES]:
        results = engine.query(sv, k=TOP_K_STABLE)
        queries.append(
            {
                "situation_vector": dataclasses.asdict(sv),
                "top5_play_ids": [r.play_id for r in results],
                "top5_distances": [r.distance for r in results],
            }
        )

    fixture = {
        "engine": "situation",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "seed": REGRESSION_SEED,
        "n_situations": engine._index_size,
        "queries": queries,
    }
    _write_fixture("situation", fixture, dry_run, force)


def _write_fixture(name: str, data: dict, dry_run: bool, force: bool) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"

    if path.exists() and not force:
        print(f"  SKIP  {path.name} (already exists; use --force to overwrite)")
        return

    if dry_run:
        print(f"  DRY   {path.name} ({len(data['queries'])} queries snapshotted)")
        return

    with path.open("w") as f:
        json.dump(data, f, indent=2)
    print(
        f"  WROTE {path.name}  ({len(data['queries'])} queries, {data.get('n_profiles', data.get('n_situations', '?'))} profiles)"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate golden-file snapshots for the similarity engine regression gate."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing fixture files.")
    args = parser.parse_args()

    print("Generating similarity engine regression fixtures...")
    print(f"  Output dir : {FIXTURES_DIR}")
    print(f"  Seed       : {REGRESSION_SEED}")
    print(f"  Top-K      : {TOP_K_STABLE}")
    print(f"  Queries    : {N_FIXTURE_QUERIES} per engine")
    print(f"  Dry run    : {args.dry_run}")
    print(f"  Force      : {args.force}")
    print()

    builders = [
        (_build_steal_engine, "steal", "player_id"),
        (_build_catcher_engine, "catcher", "catcher_id"),
        (_build_pitcher_steal_engine, "pitcher_steal", "pitcher_id"),
        (_build_manager_engine, "manager", "manager_id"),
    ]

    for build_fn, label, id_attr in builders:
        print(f"Building {label} engine...")
        engine, name = build_fn()
        _generate_rbf_fixture(engine, name, id_attr, args.dry_run, args.force)

    print("Building situation engine...")
    engine, _ = _build_situation_engine()
    _generate_situation_fixture(engine, args.dry_run, args.force)

    print()
    if args.dry_run:
        print("Dry run complete — no files written.")
    else:
        print("Done. Commit the updated fixtures/  directory to lock in these snapshots.")
        print("  git add tests/regression/fixtures/")
        print("  git commit -m 'chore: update regression golden files'")


if __name__ == "__main__":
    main()
