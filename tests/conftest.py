"""
Root test configuration — shared fixtures and pytest hooks.

Fixtures defined here are available to ALL tests (unit, integration, backtesting).
Heavy fixtures that require live infrastructure (PostgreSQL, Redis) live in
tests/integration/conftest.py and are only instantiated when integration tests run.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Shared lightweight fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_game_pk() -> int:
    """A stable game_pk used across unit tests to avoid magic numbers."""
    return 745000


@pytest.fixture()
def sample_player_ids() -> dict[str, list[int]]:
    """A small set of fake player IDs for unit test data construction."""
    return {
        "pitchers": [100001, 100002, 100003],
        "batters":  [200001, 200002, 200003, 200004, 200005, 200006, 200007, 200008, 200009],
        "fielders": [200001, 200002, 200003],
    }
