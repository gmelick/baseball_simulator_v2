"""
tests/unit/test_manager_usage_ungate_sim427.py
==============================================
SIM-427 — guard that the manager USAGE sub-score is no longer NULL-gated.

``_compute_manager_profiles`` is a DB-heavy method (a single big SQL over
``pg.raw.pitches``), so it has no live-DB unit test; its values are validated
against real data on the stack. This lightweight source-level guard prevents a
silent REGRESSION back to the ``NULL::FLOAT`` gate: it asserts the six USAGE
columns are derived from the pitcher-stint CTEs rather than emitted NULL.
"""

from __future__ import annotations

import inspect

from pipeline.batch.player_profile_computor import PlayerProfileComputor

_SRC = inspect.getsource(PlayerProfileComputor._compute_manager_profiles)
_USAGE_COLS = (
    "starter_avg_pitch_count",
    "starter_pull_pct_before_100",
    "closer_entry_leverage_index",
    "high_leverage_reliever_rate",
    "opener_usage_rate",
    "bulk_innings_rate",
)


def test_usage_columns_not_null_gated():
    # The pre-SIM-427 gate emitted e.g. ``NULL::FLOAT AS starter_avg_pitch_count``.
    for col in _USAGE_COLS:
        assert f"NULL::FLOAT AS {col}" not in _SRC, f"{col} is still NULL-gated"


def test_usage_derived_from_stint_ctes():
    # The un-gated USAGE values come from the raw.pitches stint CTEs.
    for cte in ("staff", "staff_ranked", "game_usage", "mgr_usage", "hi_lev"):
        assert cte in _SRC, f"missing USAGE CTE: {cte}"
    # And the final SELECT references the usage CTEs (not literal NULLs).
    assert "u.starter_avg_pitch_count" in _SRC
    assert "h.high_leverage_reliever_rate" in _SRC
    assert "LEFT JOIN mgr_usage u" in _SRC


def test_available_reliever_usage_rate_capstone():
    # SIM-427 capstone: the opportunity-normalized metric joins the SIM-433-v2
    # availability table; bounded used/(used+held) via an anti-join for held arms.
    assert "bullpen_opp" in _SRC
    assert "available_reliever_usage_rate" in _SRC
    assert "game_bullpen_availability" in _SRC
    assert "used_rel" in _SRC and "held_rel" in _SRC
    assert "LEFT JOIN bullpen_opp bo" in _SRC


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
