# Integration test package.
# Tests in this package require live infrastructure (PostgreSQL, Redis).
# Testcontainers spins up ephemeral containers automatically — no manual setup needed.
#
# Run via:
#   make test-integration
#   pytest tests/integration/ -v -m integration
