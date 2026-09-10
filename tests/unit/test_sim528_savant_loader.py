"""
SIM-528 — the shared Baseball Savant loader.

The tests that matter here are the season guards. Savant answers an
unrecognised season parameter with HTTP 200 and the CURRENT season's rows, so
the failure mode is not an exception — it is a well-formed CSV holding the wrong
year. Nothing downstream would notice. Every other test in this file is
ordinary coercion cover; the season tests are the ones protecting the data.

No test here touches the network.
"""

from __future__ import annotations

import pytest

from pipeline.etl import savant_loader as sl
from pipeline.etl.savant_boards import BOARDS, PROBE_SEASON, SavantBoard

# ---------------------------------------------------------------------------
# The season parameter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("board_name", "expected"),
    [
        ("arm_strength", "year=2024"),
        ("bat_tracking", "seasonStart=2024"),
        ("baserunning", "season_start=2024"),
        ("first_base_receiving", "season%5B%5D=2024"),
    ],
)
def test_each_season_style_reaches_the_query_string(board_name: str, expected: str) -> None:
    url = sl.build_url(BOARDS[board_name], 2024)
    assert expected in url
    assert "csv=true" in url


def test_camel_style_sends_both_ends_of_the_range() -> None:
    url = sl.build_url(BOARDS["bat_tracking"], 2024)
    assert "seasonStart=2024" in url
    assert "seasonEnd=2024" in url


def test_snake_style_sends_both_ends_of_the_range() -> None:
    url = sl.build_url(BOARDS["baserunning"], 2024)
    assert "season_start=2024" in url
    assert "season_end=2024" in url


def test_the_bracket_style_is_url_encoded() -> None:
    """First base receiving is the only board on this style. It accepts every
    other spelling without complaint and returns the current season, so the
    encoding here is the difference between 2024 data and 2026 data."""
    assert BOARDS["first_base_receiving"].season_params(2024) == {"season[]": "2024"}
    assert "season%5B%5D=2024" in sl.build_url(BOARDS["first_base_receiving"], 2024)


def test_unknown_season_style_raises() -> None:
    bad = SavantBoard(
        name="bad",
        url="https://example.invalid/x",
        season_style="roman-numerals",
        table="raw.x",
        player_column="id",
        columns=(),
    )
    with pytest.raises(ValueError, match="unknown season style"):
        bad.season_params(2024)


def test_the_probe_passes_when_the_impossible_season_is_empty() -> None:
    sl.probe_season_param(BOARDS["bat_tracking"], fetcher=lambda url: "id,name\n")


def test_the_probe_raises_when_the_season_parameter_is_ignored() -> None:
    """The whole point of the probe: rows for 1990 mean the parameter was dropped."""

    def fetcher(url: str) -> str:
        return "id,name\n12345,Someone\n"

    with pytest.raises(sl.SeasonParamError, match="being ignored"):
        sl.probe_season_param(BOARDS["bat_tracking"], fetcher=fetcher)


def test_the_probe_asks_for_a_season_with_no_statcast_data() -> None:
    seen: list[str] = []
    sl.probe_season_param(BOARDS["poptime"], fetcher=lambda url: seen.append(url) or "a\n")
    assert f"year={PROBE_SEASON}" in seen[0]


def test_a_row_carrying_the_wrong_season_raises() -> None:
    board = BOARDS["baserunning"]
    rows = [{"entity_id": "1", "year": "2026"}, {"entity_id": "2", "year": "2024"}]
    with pytest.raises(sl.SeasonParamError, match="2026"):
        sl.check_row_seasons(board, rows, 2024)


def test_matching_rows_pass_the_season_check() -> None:
    board = BOARDS["baserunning"]
    sl.check_row_seasons(board, [{"entity_id": "1", "year": "2024"}], 2024)


def test_a_board_without_a_season_column_skips_the_row_check() -> None:
    # bat_tracking returns no season column at all; the probe is its only guard.
    assert BOARDS["bat_tracking"].season_column is None
    sl.check_row_seasons(BOARDS["bat_tracking"], [{"id": "1"}], 2024)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def test_numeric_and_integer_columns_are_coerced_by_target() -> None:
    row = {
        "id": "665742",
        "avg_bat_speed": "74.3",
        "swing_length": "7.1",
        "swings_competitive": "412",
    }
    out = sl.coerce_row(BOARDS["bat_tracking"], row, 2024, "all")
    assert out == {
        "player_id": 665742,
        "season": 2024,
        "split": "all",
        "avg_bat_speed": 74.3,
        "swing_length": 7.1,
        "competitive_swings": 412,
    }


def test_a_blank_measurement_becomes_none_not_zero() -> None:
    """A zero would read as the slowest swing in the league, not as absent."""
    row = {"id": "1", "avg_bat_speed": "", "swing_length": None, "swings_competitive": ""}
    out = sl.coerce_row(BOARDS["bat_tracking"], row, 2024, "all")
    assert out is not None
    assert out["avg_bat_speed"] is None
    assert out["swing_length"] is None
    assert out["competitive_swings"] is None


def test_a_row_without_a_player_id_is_dropped() -> None:
    assert sl.coerce_row(BOARDS["bat_tracking"], {"id": ""}, 2024, "all") is None


def test_the_stance_board_takes_its_side_from_the_csv() -> None:
    """Savant emits the side itself here, so the loader must not overwrite it."""
    row = {"id": "596019", "bat_side": "L", "avg_foot_sep": "27.0", "avg_stance_angle": "-7.3"}
    out = sl.coerce_row(BOARDS["batting_stance"], row, 2024, None)
    assert out is not None
    assert out["bat_side"] == "L"
    assert out["foot_sep"] == 27.0


def test_a_hand_split_board_takes_its_label_from_the_query() -> None:
    out = sl.coerce_row(BOARDS["swing_path"], {"id": "1", "side": "R"}, 2024, "vs_l")
    assert out is not None
    assert out["split"] == "vs_l"
    assert out["bat_side"] == "R"  # Savant's own label survives alongside it


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_the_upsert_keys_on_the_split_when_the_board_has_one() -> None:
    sql = sl.upsert_sql(BOARDS["batting_stance"])
    # SIM-534 added the cutoff to the key: two cutoffs for one player-season are
    # two rows, not an overwrite.
    assert "ON CONFLICT (player_id, season, bat_side, asof_date)" in sql
    assert "bat_side = EXCLUDED.bat_side" not in sql  # a key is not an update target
    assert "asof_date = EXCLUDED.asof_date" not in sql
    assert "scraped_at = EXCLUDED.scraped_at" in sql


def test_the_upsert_keys_on_player_and_season_without_a_split() -> None:
    sql = sl.upsert_sql(BOARDS["arm_strength"])
    assert "ON CONFLICT (player_id, season)" in sql
    assert "arm_overall = EXCLUDED.arm_overall" in sql


def test_every_target_column_appears_exactly_once_in_the_insert() -> None:
    for board in BOARDS.values():
        cols = board.target_columns
        assert len(cols) == len(set(cols)), f"{board.name} repeats a target column"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_a_hand_split_board_is_pulled_once_per_split() -> None:
    seen: list[str] = []

    def fetcher(url: str) -> str:
        seen.append(url)
        return "id,avg_bat_speed,swing_length,swings_competitive\n1,70.0,7.0,100\n"

    n = sl.load_board_season(None, BOARDS["bat_tracking"], 2024, dry_run=True, fetcher=fetcher)
    assert len(seen) == 3
    assert sum("pitchHand=L" in u for u in seen) == 1
    assert sum("pitchHand=R" in u for u in seen) == 1
    assert sum("pitchHand" not in u for u in seen) == 1
    assert n == 3  # one row per split


def test_a_single_pull_board_is_pulled_once() -> None:
    seen: list[str] = []

    def fetcher(url: str) -> str:
        seen.append(url)
        return "entity_id,maxeff_arm_2b_3b_sba\n1,84.6\n"

    sl.load_board_season(None, BOARDS["poptime"], 2024, dry_run=True, fetcher=fetcher)
    assert len(seen) == 1
    assert "pitchHand" not in seen[0]


def test_html_instead_of_csv_is_a_failure_not_an_empty_load() -> None:
    with pytest.raises(sl.SavantFetchError, match="HTML"):
        sl.load_board_season(
            None,
            BOARDS["poptime"],
            2024,
            dry_run=True,
            fetcher=lambda url: "<!DOCTYPE html><html></html>",
        )


def test_board_groups_resolve_to_the_right_files() -> None:
    assert {b.name for b in sl.resolve_boards(["batter"])} == {
        "bat_tracking",
        "swing_path",
        "batting_stance",
    }
    assert len(sl.resolve_boards(["all"])) == len(BOARDS)
    assert [b.name for b in sl.resolve_boards(["poptime", "poptime"])] == ["poptime"]


def test_an_unknown_board_name_fails_loudly() -> None:
    with pytest.raises(SystemExit, match="unknown board"):
        sl.resolve_boards(["not-a-board"])


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_the_two_boards_that_reject_parameters_send_none() -> None:
    """Baserunning and the run-value boards return zero rows or a 500 if any
    other parameter is added. Keep their extras empty."""
    assert BOARDS["baserunning"].extra == {}


def test_the_full_roster_minimum_is_set_on_every_board_that_takes_one() -> None:
    """A qualified-only pull is roughly 215 batters against roughly 650."""
    assert BOARDS["bat_tracking"].extra["minSwings"] == "0"
    assert BOARDS["swing_path"].extra["minSwings"] == "0"
    assert BOARDS["batting_stance"].extra["minSwings"] == "0"
    assert BOARDS["arm_strength"].extra["minThrows"] == "0"
    assert BOARDS["poptime"].extra["min2b"] == "0"


def test_batting_stance_is_not_under_the_leaderboard_path() -> None:
    assert "/visuals/batting-stance" in BOARDS["batting_stance"].url
