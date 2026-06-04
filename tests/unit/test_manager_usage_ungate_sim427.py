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


def test_available_reliever_usage_rate_wired_as_seventh_usage_feature():
    # SIM-427 capstone: the opportunity-normalized metric is now a full USAGE
    # FEATURE in the manager engine (the 7th), not merely a written column.
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
    # SIM-427: the manager calibrator must slice 7 usage columns (col(0, 7)) and
    # shift aggression/platoon accordingly, so sigma_manager_usage is fit over the
    # new feature rather than silently dropping it.
    from similarity.similarity_calibration import SimilarityCalibrator

    src = inspect.getsource(SimilarityCalibrator._calibrate_manager_params)
    assert "available_reliever_usage_rate" in src
    assert "col(0, 7)" in src, "usage slice must cover 7 features"
    assert "col(7, 5)" in src, "aggression slice must start at 7"
    assert "col(12, 5)" in src, "platoon slice must start at 12"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
