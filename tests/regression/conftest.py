"""
tests/regression/conftest.py — SIM-147
========================================
Pytest fixtures that build deterministic synthetic similarity engines for
the regression gate.

All engines are constructed via __new__ to bypass DuckDB — identical to
the pattern used in tests/unit/test_ml_engines_sim066_071.py.  Synthetic
profiles are injected directly after construction.

Seeded random data (seed=2026) guarantees bit-for-bit identical inputs
on every CI run, making golden-file comparisons meaningful.

Profile key conventions (matches internal _profiles dict keying):
  BaserunnerSteal → (player_id, season)
  Catcher         → (catcher_id, season)
  PitcherSteal    → (pitcher_id, season)   [pitcher_id, NOT player_id]
  Manager         → (manager_id, season)
  Situation       → index in _situations list (KDTree, not dict-keyed)
"""

from __future__ import annotations

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Steal engine (BaserunnerStealSimilarityEngine)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def steal_engine():
    """BaserunnerStealSimilarityEngine — 12 synthetic profiles, seed=2026."""
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

    rng = np.random.default_rng(2026)
    n = 12
    t_dim = len(TENDENCY_FEATURES)
    s_dim = len(SUCCESS_FEATURES)

    profiles = [
        BaserunnerStealProfile(
            player_id=1000 + i,
            season=2024,
            sample_steal_attempts=40 + i * 3,
            sample_first_base_opps=150 + i * 10,
            tendency_vec=rng.uniform(0.05, 0.95, t_dim),
            success_vec=rng.uniform(0.55, 0.90, s_dim),
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
    return engine


# ---------------------------------------------------------------------------
# Catcher engine (CatcherSimilarityEngine)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catcher_engine():
    """CatcherSimilarityEngine — 12 synthetic profiles, seed=2026."""
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

    rng = np.random.default_rng(2026)
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
            # SIM-072: steal_attempt_rate_against typically lives in [0.02, 0.18]
            # for MLB catchers; uniform draw mirrors that envelope.
            deterrence_vec=rng.uniform(0.02, 0.18, len(DETERRENCE_FEATURES)),
            offense_vec=rng.uniform(0.0, 1.0, len(OFFENSE_FEATURES)),
            eb_alpha=1.0,
        )
        for i in range(n)
    ]

    engine = CatcherSimilarityEngine.__new__(CatcherSimilarityEngine)
    # Catcher _profiles keyed by (catcher_id, season)
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
    return engine


# ---------------------------------------------------------------------------
# Pitcher-steal engine (PitcherStealSimilarityEngine)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pitcher_steal_engine():
    """PitcherStealSimilarityEngine — 12 synthetic profiles, seed=2026."""
    from similarity.engines.pitcher_steal_similarity import (
        OUTCOME_FEATURES,
        RBF_SIGMA_OUTCOME,
        FeatureNormalizer,
        PitcherStealPartition,
        PitcherStealProfile,
        PitcherStealSimilarityEngine,
        WeightedRBFSimilarity,
    )

    rng = np.random.default_rng(2026)
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
    # PitcherSteal _profiles keyed by (pitcher_id, season)
    engine._profiles = {(p.pitcher_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = PitcherStealPartition()
    engine._partition.build(profiles, engine._normalizer)
    engine._out_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_OUTCOME, np.array([w for _, w in OUTCOME_FEATURES])
    )
    return engine


# ---------------------------------------------------------------------------
# Manager engine (ManagerSimilarityEngine)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manager_engine():
    """ManagerSimilarityEngine — 12 synthetic profiles, seed=2026."""
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

    rng = np.random.default_rng(2026)
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
    # Manager _profiles keyed by (manager_id, season)
    engine._profiles = {(p.manager_id, p.season): p for p in profiles}
    engine._normalizer = FeatureNormalizer()
    engine._normalizer.fit(profiles)
    engine._partition = ManagerPartition()
    engine._partition.build(profiles, engine._normalizer)
    # Manager uses _agg_rbf / _plat_rbf (abbreviated names)
    engine._usage_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_USAGE, np.array([w for _, w in USAGE_FEATURES])
    )
    engine._agg_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_AGGRESSION, np.array([w for _, w in AGGRESSION_FEATURES])
    )
    engine._plat_rbf = WeightedRBFSimilarity(
        RBF_SIGMA_PLATOON, np.array([w for _, w in PLATOON_FEATURES])
    )
    return engine


# ---------------------------------------------------------------------------
# Situation engine (SituationSimilarityEngine)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def situation_engine():
    """SituationSimilarityEngine — 50 synthetic situations, seed=2026."""
    from scipy.spatial import KDTree

    from similarity.engines.situation_similarity import (
        FEATURE_WEIGHTS,
        SCORE_DIFF_CLIP,
        NearestSituation,
        SituationNormalizer,
        SituationSimilarityEngine,
        SituationVector,
    )

    rng = np.random.default_rng(2026)
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

    # Build _index_meta (parallel to KDTree leaves) — mirrors _load_situations()
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
    scaled = normalizer.normalize_batch(raw_matrix) * feature_scale

    engine = SituationSimilarityEngine.__new__(SituationSimilarityEngine)
    engine._index_meta = index_meta
    engine._normalizer = normalizer
    engine._feature_scale = feature_scale
    engine._index_size = n
    engine._kdtree = KDTree(scaled)
    return engine
