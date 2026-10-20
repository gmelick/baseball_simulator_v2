"""
Tolerance constants for the similarity engine regression gate (the engine
invariant tests). They live here so a threshold change is a one-line edit.

The gate holds no golden-file snapshots (owner ruling 2026-09-10; see
test_engine_regression.py). Only SYMMETRY_TOLERANCE has a reader:
test_engine_regression.py. The other constants have no reader.

Tolerance rationale
-------------------
    RBF / KDTree scoring is deterministic float64 arithmetic; across
    Python + NumPy releases the difference is within float64 ULP.
    1e-9 gives one full order of magnitude slack above typical ULP errors.

SYMMETRY_TOLERANCE = 1e-9
    score(A→B) == score(B→A) by the RBF kernel definition.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REGRESSION_DIR = Path(__file__).parent

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

# The number of random fixture query keys per check.
N_FIXTURE_QUERIES: int = 5

# The number of top comps per check.
TOP_K_STABLE: int = 5

# The RNG seed of the fixtures. conftest.py writes the same seed, 2026, inline.
REGRESSION_SEED: int = 2026
