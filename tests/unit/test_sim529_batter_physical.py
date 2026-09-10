"""
SIM-529 — physical swing and stance features on the batter model.
SIM-530 — the two empty measurement blocks the same build fills.

Three of these tests guard failures that are SILENT in production, and they are
the reason this file exists:

  * the positional-insert contract. ``_compute_batter_profiles`` inserts with no
    column list, so DuckDB matches the SELECT to the table by POSITION. If the
    migration and the SELECT tail ever disagree, every batter's bat speed lands
    in the stance-angle column and nothing errors.
  * a missing measurement scoring as an extreme instead of as neutral. A batter
    Savant has no row for must be draw-neutral; a zero bat speed would make him
    the slowest swinger in the league.
  * the two scoring paths drifting apart. ``score_all`` feeds the nightly actor
    matrix and ``_score_pair`` feeds ``query_pair``; nothing else compares them.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from similarity.engines.batter_similarity import (
    PHYSICAL_FEATURES,
    WEIGHT_BATTED_BALL,
    WEIGHT_BATTED_BALL_PLATOON,
    WEIGHT_DISCIPLINE,
    WEIGHT_DISCIPLINE_PLATOON,
    WEIGHT_PHYSICAL,
    WEIGHT_PHYSICAL_PLATOON,
    WEIGHT_PLATOON,
    WEIGHT_PLATOON_PLATOON,
    WEIGHT_POWER,
    WEIGHT_POWER_PLATOON,
    BatterProfile,
)
from similarity.similarity_calibration import CalibrationReport

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migrations" / "duckdb" / "0025_sim529_batter_physical_features.sql"
DUCKDB_SCHEMA = REPO / "db" / "schemas" / "02_duckdb_schema.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"


def _column_order():
    from pipeline.batch.player_profile_computor import PHYSICAL_COLUMN_ORDER

    return PHYSICAL_COLUMN_ORDER


# ---------------------------------------------------------------------------
# The positional-insert contract
# ---------------------------------------------------------------------------


def test_the_migration_adds_the_columns_in_the_declared_order() -> None:
    """The batter INSERT carries no column list, so order IS the contract."""
    sql = MIGRATION.read_text(encoding="utf-8")
    # Anchor on the statement, not the phrase: the header comment uses the same
    # words ("ADD COLUMN IF NOT EXISTS only.").
    added = re.findall(r"^ALTER TABLE \S+ ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
    assert tuple(added) == _column_order()


def test_the_canonical_schema_carries_the_same_columns_in_the_same_order() -> None:
    schema = DUCKDB_SCHEMA.read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS derived.batter_season_metrics")
    body = schema[start : schema.index("PRIMARY KEY (batter_id, season)", start)]
    order = _column_order()
    positions = []
    for col in order:
        m = re.search(rf"^\s+{re.escape(col)}\s+\w+,\s*$", body, re.M)
        assert m is not None, f"{col} missing from the canonical DuckDB schema"
        positions.append(m.start())
    assert positions == sorted(positions), "the schema file lists them out of order"


def test_the_schema_version_was_bumped_with_the_migration() -> None:
    assert VERSION_FILE.read_text(encoding="utf-8").strip() == "25"


def test_every_feature_lands_three_times() -> None:
    order = _column_order()
    for name, _ in PHYSICAL_FEATURES:
        assert name in order
        assert f"{name}_vs_l" in order
        assert f"{name}_vs_r" in order
    assert len(order) == 3 * len(PHYSICAL_FEATURES) + 3  # + the three sample counts


# ---------------------------------------------------------------------------
# The switch-hitter crossover (owner ruling 2026-09-10)
# ---------------------------------------------------------------------------


def test_the_stance_side_crosses_over_to_the_pitcher_hand() -> None:
    """Facing a LEFT-handed pitcher a switch hitter bats RIGHT, so the ``_vs_l``
    column must read Savant's ``R`` stance row. Getting this backwards is
    invisible in the data — every column is populated, just mirrored."""
    from pipeline.batch.player_profile_computor import _sql_stance_select

    sql = _sql_stance_select()
    checked = 0
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("COALESCE(") and stripped.endswith("_vs_l,"):
            assert re.search(r"COALESCE\(st\.\w+_r, st\.\w+_l\)", stripped), stripped
            checked += 1
        if stripped.startswith("COALESCE(") and stripped.endswith("_vs_r,"):
            assert re.search(r"COALESCE\(st\.\w+_l, st\.\w+_r\)", stripped), stripped
            checked += 1
    assert checked == 8, f"expected 4 stance features x 2 sides, saw {checked}"


def test_the_swing_split_does_not_cross_over() -> None:
    """The swing boards are filtered by the PITCHER's hand already, so ``vs_l``
    maps straight to ``vs_l``. Only the stance board needs the crossover."""
    from pipeline.batch.player_profile_computor import _sql_swing_select

    for line in _sql_swing_select().splitlines():
        if line.strip().endswith("_vs_l,"):
            assert "_vs_l AS" in line, line
        if line.strip().endswith("_vs_r,"):
            assert "_vs_r AS" in line, line


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------


def test_the_five_sub_score_weights_sum_to_one() -> None:
    total = WEIGHT_DISCIPLINE + WEIGHT_BATTED_BALL + WEIGHT_PLATOON + WEIGHT_POWER + WEIGHT_PHYSICAL
    assert total == pytest.approx(1.0)


def test_the_platoon_context_weights_sum_to_one() -> None:
    total = (
        WEIGHT_DISCIPLINE_PLATOON
        + WEIGHT_BATTED_BALL_PLATOON
        + WEIGHT_PLATOON_PLATOON
        + WEIGHT_POWER_PLATOON
        + WEIGHT_PHYSICAL_PLATOON
    )
    assert total == pytest.approx(1.0)


def test_the_existing_groups_kept_their_relative_sizes() -> None:
    """SIM-529 took 0.20 for the physical group proportionally, so every
    judgement encoded in the other four's RELATIVE weights survives."""
    disc_to_bb = WEIGHT_DISCIPLINE / WEIGHT_BATTED_BALL
    plat_to_pow = WEIGHT_PLATOON / WEIGHT_POWER
    assert disc_to_bb == pytest.approx(0.40 / 0.35, rel=1e-6)
    assert plat_to_pow == pytest.approx(0.15 / 0.10, rel=1e-6)


def test_each_feature_weight_is_its_measured_reliability() -> None:
    """Owner ruling 2026-09-10: fit these reliability-proportionally. Every
    weight is a measured year-to-year correlation, so all sit inside (0, 1)."""
    for name, w in PHYSICAL_FEATURES:
        assert 0.0 < w < 1.0, name


# ---------------------------------------------------------------------------
# Missing data must be neutral, never extreme
# ---------------------------------------------------------------------------


def test_a_profile_with_no_savant_row_defaults_to_neutral_not_zero() -> None:
    p = BatterProfile(
        batter_id=1,
        season=2024,
        bats="R",
        sample_pa=400,
        sample_pitches=1600,
        discipline_vec=np.zeros(7),
        batted_ball_vec=np.zeros(8),
        power_vec=np.zeros(4),
        platoon_vs_l_vec=np.zeros(7),
        platoon_vs_r_vec=np.zeros(7),
    )
    assert np.isnan(p.physical_vec).all()
    assert np.isnan(p.physical_vs_l_vec).all()
    assert np.isnan(p.physical_vs_r_vec).all()


def test_two_profiles_with_no_physical_data_score_a_perfect_physical_match() -> None:
    """NaN is zero distance in this kernel, which is what makes it neutral."""
    from similarity.engines.batter_similarity import WeightedRBFSimilarity

    rbf = WeightedRBFSimilarity(
        sigma=1.0, reliability_weights=np.array([w for _, w in PHYSICAL_FEATURES])
    )
    nan_vec = np.full(len(PHYSICAL_FEATURES), np.nan)
    assert rbf.score(nan_vec, nan_vec) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The two scoring paths must agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vs_hand", [None, "L", "R"])
def test_score_all_and_score_pair_agree(vs_hand) -> None:
    from tests.unit.test_batter_similarity import _build_synthetic_engine

    engine = _build_synthetic_engine()
    keys = list(engine._profiles)
    query_key = keys[0]

    batch = {(r.batter_id, r.season): r for r in engine.query(*query_key, vs_hand=vs_hand)}
    compared = 0
    for key in keys:
        if key not in batch:
            continue  # query() leaves the query's own season out of its results
        paired = engine.query_pair(query_key, key, vs_hand=vs_hand)
        assert paired is not None
        assert batch[key].score == pytest.approx(paired.score, abs=1e-9), key
        assert batch[key].physical_score == pytest.approx(paired.physical_score, abs=1e-9), key
        compared += 1
    assert compared >= 2, "the two paths were never actually compared"


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_the_calibration_report_carries_a_physical_sigma() -> None:
    report = CalibrationReport()
    assert report.sigma_physical == 0.0  # the keep-the-engine-default sentinel
    assert report.reliability_weights_physical is None


def test_an_unfitted_physical_sigma_keeps_the_engine_default() -> None:
    from tests.unit.test_batter_similarity import _build_synthetic_engine

    engine = _build_synthetic_engine()
    before = engine._physical_rbf.sigma
    engine.apply_calibration(CalibrationReport())
    assert engine._physical_rbf.sigma == before


def test_a_fitted_physical_sigma_is_applied() -> None:
    from tests.unit.test_batter_similarity import _build_synthetic_engine

    engine = _build_synthetic_engine()
    engine.apply_calibration(CalibrationReport(sigma_physical=1.234))
    assert engine._physical_rbf.sigma == pytest.approx(1.234)


# ---------------------------------------------------------------------------
# SIM-530 — the placeholders are gone
# ---------------------------------------------------------------------------


_ARM_COLUMNS = (
    "arm_strength",
    "arm_opportunities",
    "arm_holds",
    "arm_hold_rate",
    "arm_assists",
    "arm_thrown_out_rate",
    "arm_advancement_prevention",
    "of_arm_runs",
)


def test_no_arm_column_is_still_a_null_placeholder() -> None:
    """Every one of these was a literal NULL on all 4,799 fielder-seasons while
    the fielder model weighted the group at 0.30 of an outfielder's score."""
    src = COMPUTOR.read_text(encoding="utf-8")
    for col in _ARM_COLUMNS:
        assert not re.search(rf"NULL::\w+\s+AS {col}\b", src), f"{col} is still a placeholder"


def test_the_catcher_arm_is_no_longer_a_null_placeholder() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    assert not re.search(r"NULL::\w+\s+AS arm_strength_mean", src)


def test_the_advancement_block_stays_outfield_only() -> None:
    """Savant's baserunning board measures runners taking an extra base on a
    hit. Applying it to an infielder would be wrong, and the column comments
    have always promised it is outfield-only."""
    from pipeline.batch.player_profile_computor import _SQL_IS_OF

    src = COMPUTOR.read_text(encoding="utf-8")
    assert _SQL_IS_OF == "c.position IN ('LF', 'CF', 'RF')"
    for col in _ARM_COLUMNS:
        if col == "arm_strength":
            continue  # a physical fact; filled for every position on purpose
        m = re.search(rf"AS {col}\b", src)
        assert m is not None, col
        window = src[max(0, m.start() - 700) : m.start()]
        assert "_SQL_IS_OF" in window, f"{col} is not guarded to the outfield"


def test_pop_time_prefers_the_measurement_over_the_old_proxy() -> None:
    """The old value was 2.0 + (0.25 - cs_rate) * 2.0 — a function of the
    caught-stealing rate, which the same sub-score already weights."""
    src = COMPUTOR.read_text(encoding="utf-8")
    m = re.search(r"COALESCE\(\s*spt\.pop_time_2b,\s*sct\.pop_time,", src)
    assert m is not None, "pop time no longer prefers the measured sources"
    proxy = src.index("2.0 + (0.25 - t.cs_rate) * 2.0")
    assert proxy > m.start(), "the proxy must be the fallback, not the first choice"


# ---------------------------------------------------------------------------
# The feature must be INERT until its data lands
# ---------------------------------------------------------------------------


def test_with_no_physical_data_the_weights_revert_to_the_pre_sim529_split() -> None:
    """Shipping the code before the profile rebuild must not change any score.

    Without this, every physical sub-score is a perfect 1.0 and the composite
    becomes 0.8 * (the old score) + 0.20 — which compresses the spread of a
    number used as a DRAW WEIGHT, quietly weakening the batter factor.
    """
    from similarity.engines.batter_similarity import sub_score_weights

    assert sub_score_weights(platoon_context=False, physical_available=False) == (
        0.40,
        0.35,
        0.15,
        0.10,
        0.0,
    )
    assert sub_score_weights(platoon_context=True, physical_available=False) == (
        0.30,
        0.25,
        0.35,
        0.10,
        0.0,
    )


def test_with_physical_data_the_weights_are_the_sim529_split() -> None:
    from similarity.engines.batter_similarity import sub_score_weights

    w = sub_score_weights(platoon_context=False, physical_available=True)
    assert w == (
        WEIGHT_DISCIPLINE,
        WEIGHT_BATTED_BALL,
        WEIGHT_PLATOON,
        WEIGHT_POWER,
        WEIGHT_PHYSICAL,
    )
    assert sum(w) == pytest.approx(1.0)


@pytest.mark.parametrize("platoon_context", [False, True])
def test_every_weight_set_sums_to_one(platoon_context: bool) -> None:
    for available in (False, True):
        w = sub_score_weights_for(platoon_context, available)
        assert sum(w) == pytest.approx(1.0), (platoon_context, available)


def sub_score_weights_for(platoon_context: bool, available: bool):
    from similarity.engines.batter_similarity import sub_score_weights

    return sub_score_weights(platoon_context=platoon_context, physical_available=available)


def test_a_population_with_no_physical_data_scores_exactly_as_before() -> None:
    """End to end: the synthetic engine has no physical data, so its composite
    must match a hand-computed pre-SIM-529 weighting."""
    from tests.unit.test_batter_similarity import _build_synthetic_engine

    engine = _build_synthetic_engine()
    assert engine._normalizer.physical_mean is None  # the precondition
    keys = list(engine._profiles)
    result = engine.query_pair(keys[0], keys[1])
    assert result is not None
    expected = (
        0.40 * result.discipline_score
        + 0.35 * result.batted_ball_score
        + 0.15 * result.platoon_score
        + 0.10 * result.power_score
    )
    from similarity.engines.batter_similarity import bats_penalty

    pa, pb = engine._profiles[keys[0]], engine._profiles[keys[1]]
    expected *= bats_penalty(pa.bats, pb.bats)
    expected *= np.sqrt(min(pa.eb_alpha, pb.eb_alpha))
    assert result.score == pytest.approx(expected, abs=1e-9)
