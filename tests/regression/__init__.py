# tests/regression: the similarity engine invariant gate.
#
# Purpose:
#   Catch a defect in a similarity engine's scores (a sign error, a NaN).
#   Each test class runs a deterministic synthetic engine (via __new__ + fixed
#   profile injection) and asserts mathematical properties that must hold
#   regardless of weights or data. The gate holds no golden-file snapshots
#   (owner ruling 2026-09-10; see test_engine_regression.py).
#
# Marks: @pytest.mark.regression (excluded from default unit run; included in CI)
