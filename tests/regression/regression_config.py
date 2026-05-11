"""
regression_config.py — SIM-147
===============================
Tolerance constants and engine metadata for the similarity engine
regression gate.  Centralised here so changes to thresholds are
one-line edits rather than hunts across multiple test files.

Golden-file format
------------------
Each engine's golden file lives at::

    tests/regression/fixtures/<engine_name>.json

and has the shape::

    {
        "engine": "<name>",
        "generated_at": "<ISO-8601 UTC>",
        "seed": <int>,
        "n_profiles": <int>,
        "queries": [
            {
                "query_key": [<player_id>, <season>],
                "top5_keys": [[<player_id>, <season>], ...],
                "top5_scores": [<float>, ...],
                "composite_score_pair_ab": <float>,   # query(A, B)
                "composite_score_pair_ba": <float>    # query(B, A)  -- must equal above
            },
            ...
        ]
    }

Tolerance rationale
-------------------
SCORE_ABS_TOLERANCE = 1e-9
    RBF / KDTree scoring is deterministic float64 arithmetic; across
    Python 3.11 + NumPy releases the difference is within float64 ULP.
    1e-9 gives one full order of magnitude slack above typical ULP errors.

SYMMETRY_TOLERANCE = 1e-9
    score(A→B) == score(B→A) by the RBF kernel definition.

TOP_K_STABLE = 5
    We only lock in the top-5 comps.  Ties beyond rank 5 may reorder
    legally when numeric precision changes; locking the full list would
    produce brittle snapshots.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REGRESSION_DIR = Path(__file__).parent
FIXTURES_DIR = REGRESSION_DIR / "fixtures"

# ---------------------------------------------------------------------------
# Tolerance constants
# ---------------------------------------------------------------------------

SCORE_ABS_TOLERANCE: float = 1e-9
"""Absolute tolerance for score comparisons against golden files."""

SYMMETRY_TOLERANCE: float = 1e-9
"""Maximum acceptable |score(A→B) − score(B→A)|."""

BOUNDS_LO: float = 0.0
BOUNDS_HI: float = 1.0

# The minimum number of fixture profiles to build a meaningful regression set.
MIN_REGRESSION_PROFILES: int = 8

# How many random fixture query keys to snapshot.
N_FIXTURE_QUERIES: int = 5

# How many top comps to lock in the golden files.
TOP_K_STABLE: int = 5

# RNG seed used for fixture generation and regression checks.
REGRESSION_SEED: int = 2026

# ---------------------------------------------------------------------------
# Engine registry — metadata used by both the generator and the test suite
# ---------------------------------------------------------------------------

ENGINE_REGISTRY: list[dict] = [
    {
        "name": "baserunner_steal",
        "module": "similarity.engines.baserunner_steal_similarity",
        "class": "BaserunnerStealSimilarityEngine",
        "sub_score_names": ["tendency_score", "jump_score", "success_score"],
        "query_signature": ("player_id", "season"),  # positional arg names
    },
    {
        "name": "catcher",
        "module": "similarity.engines.catcher_similarity",
        "class": "CatcherSimilarityEngine",
        "sub_score_names": ["framing_score", "blocking_score", "throwing_score", "offense_score"],
        "query_signature": ("player_id", "season"),
    },
    {
        "name": "pitcher_steal",
        "module": "similarity.engines.pitcher_steal_similarity",
        "class": "PitcherStealSimilarityEngine",
        "sub_score_names": ["delivery_score", "pickoff_score", "outcome_score"],
        "query_signature": ("player_id", "season"),
    },
    {
        "name": "manager",
        "module": "similarity.engines.manager_similarity",
        "class": "ManagerSimilarityEngine",
        "sub_score_names": ["usage_score", "aggression_score", "platoon_score"],
        "query_signature": ("manager_id", "season"),
    },
]
