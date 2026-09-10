"""
SIM-534 — point-in-time batter profiles, and the guards against leakage.

A backtest that uses data recorded after the day it simulates does not fail. It
produces a number, and the number is better than the model deserves. Every test
in this file exists because the corresponding mistake is silent.

THE RULE THESE TESTS ENCODE
---------------------------
A cutoff is not a per-season idea. A season that ENDED before the cutoff is
admissible whole — a 2023 aggregate holds nothing recorded after a 2025 date.
Only the season containing the cutoff has to be truncated. Stamping every source
row with the date its data runs through turns that rule into arithmetic:
``asof_date <= cutoff``.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from pipeline.batch.player_profile_computor import (
    PHYSICAL_COLUMN_ORDER,
    PROFILE_TAIL_COLUMNS,
    _sql_pitch_swing_agg,
    _sql_swing_select,
)
from pipeline.etl import savant_loader as sl
from pipeline.etl import savant_pitch_tracking_loader as ptl
from pipeline.etl.savant_boards import BOARDS

REPO = Path(__file__).resolve().parents[2]
MIG_0026 = REPO / "db" / "migrations" / "duckdb" / "0026_sim534_profile_asof.sql"
VERSION_FILE = REPO / "db" / "schemas" / "duckdb_schema_version.txt"
COMPUTOR = REPO / "pipeline" / "batch" / "player_profile_computor.py"


# ---------------------------------------------------------------------------
# The cutoff reaches every source
# ---------------------------------------------------------------------------

#: Every table the batter profile query reads that carries dated rows. If a
#: source is added without a cutoff, a backtest silently sees the future.
_DATED_SOURCES = (
    "raw.pitches",
    "raw.savant_pitch_tracking",
    "raw.savant_bat_tracking",
    "raw.savant_swing_path",
    "raw.savant_batting_stance",
)


def test_every_dated_source_is_filtered_by_the_cutoff() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _compute_batter_profiles")
    end = src.index("def _assert_batter_profiles_have_no_leakage")
    body = src[start:end]
    for table in _DATED_SOURCES:
        assert f"pg.{table}" in body, f"{table} is no longer read — update this test"
    # The pitch sources are cut by game_date, the leaderboards by asof_date.
    assert "game_date <= DATE '{asof_sql}'" in body
    assert body.count("asof_date <= DATE '{asof_sql}'") == 3, (
        "each of the three leaderboard sources must filter on asof_date"
    )


def test_the_leakage_assertion_covers_every_dated_source() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    start = src.index("def _assert_batter_profiles_have_no_leakage")
    body = src[start : start + 3000]
    for table in _DATED_SOURCES:
        assert f'"{table}"' in body, f"{table} is not checked by the leakage assertion"


def test_a_cutoff_build_deletes_seasons_that_had_not_started() -> None:
    """INSERT OR REPLACE only overwrites rows the query produces. Without the
    delete, a 2025 profile survives a build as of April 2024 and the backtest
    scores against a batter who has not played yet."""
    src = COMPUTOR.read_text(encoding="utf-8")
    assert "DELETE FROM derived.batter_season_metrics" in src
    assert "season > {asof_date.year}" in src


def test_every_profile_ends_up_stamped() -> None:
    src = COMPUTOR.read_text(encoding="utf-8")
    assert "SET asof_date = DATE '{asof_sql}'" in src
    assert "WHERE asof_date IS NULL" in src


# ---------------------------------------------------------------------------
# Choosing between the two swing sources
# ---------------------------------------------------------------------------


def test_the_per_pitch_source_wins_once_it_is_substantially_complete() -> None:
    """While the per-pitch table is backfilling it may hold a few April games
    against a full-season leaderboard row, and preferring it blindly would
    replace a 600-swing average with a 50-swing one. An exact count comparison
    over-corrected — it split the population between two sources — so the
    point-in-time source wins at 80% of the board's swings."""
    sql = _sql_swing_select()
    assert "0.8 * COALESCE(sw.competitive_swings_all, 0)" in sql
    for line in sql.splitlines():
        assert line.strip().startswith("COALESCE(CASE WHEN"), line


def test_the_per_pitch_aggregate_splits_on_the_pitcher_hand() -> None:
    sql = _sql_pitch_swing_agg()
    assert "p.p_throws = 'L'" in sql
    assert "p.p_throws = 'R'" in sql
    # one average per (feature, split) plus one count per split
    assert sql.count("AVG(CASE WHEN") == 18
    assert sql.count("COUNT(CASE WHEN") == 3


# ---------------------------------------------------------------------------
# The stamp on the leaderboard rows
# ---------------------------------------------------------------------------


def test_a_completed_season_is_stamped_at_its_end() -> None:
    out = sl.coerce_row(BOARDS["batting_stance"], {"id": "1", "bat_side": "R"}, 2024, None)
    assert out is not None
    assert out["asof_date"] == dt.date(2024, 12, 31)


def test_the_season_in_progress_is_stamped_today_not_in_the_future() -> None:
    """Stamping the live season 31 December dates it in the future, and the
    builder admits only rows dated at or before its cutoff — so the current
    season would vanish from every live build."""
    this_year = dt.date.today().year
    out = sl.coerce_row(BOARDS["batting_stance"], {"id": "1", "bat_side": "R"}, this_year, None)
    assert out is not None
    assert out["asof_date"] == dt.date.today()
    assert out["asof_date"] <= dt.date.today()


def test_an_explicit_cutoff_is_stamped_verbatim() -> None:
    cutoff = dt.date(2024, 6, 30)
    out = sl.coerce_row(BOARDS["batting_stance"], {"id": "1", "bat_side": "L"}, 2024, None, cutoff)
    assert out is not None
    assert out["asof_date"] == cutoff


def test_a_point_in_time_pull_sends_a_date_range() -> None:
    url = sl.build_url(BOARDS["batting_stance"], 2024, "", dt.date(2024, 6, 30))
    assert "dateStart=2024-01-01" in url
    assert "dateEnd=2024-06-30" in url


def test_a_board_with_no_date_control_refuses_a_cutoff() -> None:
    """Sprint speed, outs above average, arm strength and pop time have no date
    control. Silently returning the whole season would be the worst outcome."""
    assert not BOARDS["arm_strength"].supports_date_range
    with pytest.raises(ValueError, match="no date control"):
        sl.build_url(BOARDS["arm_strength"], 2024, "", dt.date(2024, 6, 30))


def test_the_cutoff_is_part_of_the_key() -> None:
    """Two cutoffs for one player-season are two rows, not an overwrite."""
    sql = sl.upsert_sql(BOARDS["batting_stance"])
    assert "ON CONFLICT (player_id, season, bat_side, asof_date)" in sql


# ---------------------------------------------------------------------------
# The per-pitch loader
# ---------------------------------------------------------------------------


def test_a_day_at_the_row_cap_is_refused() -> None:
    """The export truncates at 25,000 rows and says nothing. A week is over the
    cap; accepting a capped day would silently drop most of it."""
    rows = [{"game_date": "2024-04-01"}] * ptl.ROW_CAP
    with pytest.raises(ptl.TruncatedDayError, match="incomplete"):
        ptl.check_day(rows, dt.date(2024, 4, 1))


def test_a_day_under_the_cap_is_accepted() -> None:
    ptl.check_day([{"game_date": "2024-04-01"}] * 4190, dt.date(2024, 4, 1))


def test_rows_from_another_date_are_refused() -> None:
    rows = [{"game_date": "2024-04-01"}, {"game_date": "2024-04-02"}]
    with pytest.raises(RuntimeError, match="other dates"):
        ptl.check_day(rows, dt.date(2024, 4, 1))


def test_the_request_is_one_day_of_regular_season() -> None:
    url = ptl.build_url(dt.date(2024, 4, 1))
    assert "game_date_gt=2024-04-01" in url
    assert "game_date_lt=2024-04-01" in url
    assert "hfGT=R%7C" in url


def test_the_tracking_row_carries_its_join_key_and_date() -> None:
    row = {
        "game_pk": "746000",
        "at_bat_number": "12",
        "pitch_number": "3",
        "batter": "519317",
        "pitcher": "592450",
        "bat_speed": "74.1",
        "swing_path_tilt": "31.2",
        "intercept_ball_minus_batter_pos_y_inches": "27.5",
    }
    out = ptl.coerce_row(row, dt.date(2024, 4, 1))
    assert out is not None
    assert out["game_pk"] == 746000
    assert out["game_date"] == dt.date(2024, 4, 1)
    assert out["season"] == 2024
    assert out["bat_speed"] == 74.1
    assert out["swing_path_tilt"] == 31.2
    assert out["intercept_y"] == 27.5


def test_a_tracking_row_with_no_join_key_is_dropped() -> None:
    assert ptl.coerce_row({"game_pk": "", "at_bat_number": "1"}, dt.date(2024, 4, 1)) is None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_the_cutoff_stamp_is_the_last_profile_column() -> None:
    assert PROFILE_TAIL_COLUMNS[-1] == "asof_date"
    assert PROFILE_TAIL_COLUMNS[:-1] == PHYSICAL_COLUMN_ORDER


def test_migration_0026_adds_only_the_stamp() -> None:
    sql = MIG_0026.read_text(encoding="utf-8")
    added = re.findall(r"^ALTER TABLE \S+ ADD COLUMN IF NOT EXISTS (\S+)", sql, re.M)
    assert added == ["asof_date"]


def test_the_schema_version_was_bumped() -> None:
    assert VERSION_FILE.read_text(encoding="utf-8").strip() == "26"
