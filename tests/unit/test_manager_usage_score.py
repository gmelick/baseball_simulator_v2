"""
The manager profile's USAGE columns: computed from real pitching changes, never
NULL placeholders, and carried through the engine and the calibrator.

``_compute_manager_profiles`` is one large SQL query over ``pg.raw.pitches``,
so it has no live-database unit test; its values are checked against real data
on the stack. These tests read the source instead. They assert that the usage
columns come from the pitcher-stint CTEs (common table expressions, the
``WITH`` blocks of the query) and not from a ``NULL::FLOAT`` literal, which
would leave the usage sub-score empty and raise no error.
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
    # A NULL placeholder reads e.g. ``NULL::FLOAT AS starter_avg_pitch_count``.
    for col in _USAGE_COLS:
        assert f"NULL::FLOAT AS {col}" not in _SRC, f"{col} is still NULL-gated"


def test_usage_derived_from_stint_ctes():
    # The usage values come from the stint CTEs over raw.pitches.
    for cte in ("staff", "staff_ranked", "game_usage", "mgr_usage", "hi_lev"):
        assert cte in _SRC, f"missing USAGE CTE: {cte}"
    # The final SELECT reads the usage CTEs, not literal NULLs.
    assert "u.starter_avg_pitch_count" in _SRC
    assert "h.high_leverage_reliever_rate" in _SRC
    assert "LEFT JOIN mgr_usage u" in _SRC


def test_available_reliever_usage_rate_capstone():
    # The opportunity-normalised rate joins the bullpen availability table
    # (raw.game_bullpen_availability). It is used / (used + held), so it stays
    # inside [0, 1]; an anti-join finds the arms that were held.
    assert "bullpen_opp" in _SRC
    assert "available_reliever_usage_rate" in _SRC
    assert "game_bullpen_availability" in _SRC
    assert "used_rel" in _SRC and "held_rel" in _SRC
    assert "LEFT JOIN bullpen_opp bo" in _SRC


def test_available_reliever_usage_rate_wired_as_seventh_usage_feature():
    # The opportunity-normalised rate is the manager engine's seventh USAGE
    # FEATURE, not only a column the profile writes.
    from similarity.engines.manager_similarity import (
        USAGE_FEATURES,
        ManagerSimilarityEngine,
    )

    names = [f for f, _ in USAGE_FEATURES]
    assert len(USAGE_FEATURES) == 7, names
    assert names[-1] == "available_reliever_usage_rate", names
    # The engine load SQL must SELECT it so it flows into usage_vec.
    load_src = inspect.getsource(ManagerSimilarityEngine._load_profiles)
    assert "available_reliever_usage_rate" in load_src


def test_calibrator_fits_seven_usage_features():
    # The manager calibrator (similarity/calibration/manager.py) slices 7 usage
    # columns (col(0, 7)) and starts the aggression and platoon slices after
    # them, so sigma_manager_usage fits over all seven features.
    from similarity.similarity_calibration import SimilarityCalibrator

    src = inspect.getsource(SimilarityCalibrator._calibrate_manager_params)
    assert "available_reliever_usage_rate" in src
    assert "col(0, 7)" in src, "usage slice must cover 7 features"
    assert "col(7, 5)" in src, "aggression slice must start at 7"
    assert "col(12, 5)" in src, "platoon slice must start at 12"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
