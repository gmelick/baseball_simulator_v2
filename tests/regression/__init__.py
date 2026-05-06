# tests/regression — Similarity engine regression gate (SIM-147)
#
# Purpose:
#   Detect unintended drift in similarity engine outputs between commits.
#   Each test class runs a deterministic synthetic engine (via __new__ + fixed
#   profile injection), compares computed scores against golden-file snapshots,
#   and asserts mathematical properties that must hold regardless of data.
#
# Golden files live in tests/regression/fixtures/*.json.
# To regenerate after an intentional engine change:
#   python tests/regression/generate_fixtures.py
#
# Marks: @pytest.mark.regression (excluded from default unit run; included in CI)
