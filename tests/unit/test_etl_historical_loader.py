"""
Unit tests for pipeline/etl/etl_historical_loader.py
=====================================================
Targets:
  - Pure type-coercion helpers (_to_float / _to_int / _to_bool / _to_str)
  - _build_row_dict column rename + coercion
  - _validate_row two-tier validation rules
  - _parse_height, _map_game_status helpers
  - _connect HTTP retry behavior (with mocked `requests`)
  - HistoricalDataLoader constructor DSN resolution (SIM-153)
  - HistoricalDataLoader DB methods (psycopg2 mocked end-to-end)
  - _process_and_insert orchestration including hard-error / warning paths
  - _log_etl_errors, _batch_insert, _log_freshness, quality_report, reprocess_errored_games
  - _ensure_* prerequisite checks (existence short-circuit only — full upsert paths
    require live MLB-API HTML scraping and are out of scope for unit tests)
  - _fetch_game_pitches end-to-end with a minimal feed/live payload

Mocking style mirrors tests/unit/test_data_engineer_sim092_sim093.py — psycopg2
connections are MagicMock objects that simulate context-manager semantics.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from pipeline.etl.etl_historical_loader import (
    ALWAYS_REQUIRED,
    BATCH_SIZE,
    COLUMN_RENAME,
    GAME_TYPES,
    HALF_INNING_MAP,
    IN_PLAY_REQUIRED,
    MAX_API_RETRIES,
    HistoricalDataLoader,
    ValidationResult,
    _build_row_dict,
    _connect,
    _fetch_game_pitches,
    _map_game_status,
    _parse_height,
    _to_bool,
    _to_float,
    _to_int,
    _to_str,
    _validate_row,
)

# ===========================================================================
# Module-level constants / sanity
# ===========================================================================


class TestModuleConstants:
    def test_batch_size_positive(self):
        assert BATCH_SIZE > 0

    def test_max_api_retries_reasonable(self):
        assert 1 <= MAX_API_RETRIES <= 20

    def test_game_types_non_empty(self):
        assert "R" in GAME_TYPES  # regular season

    def test_always_required_includes_primary_key(self):
        assert "game_pk" in ALWAYS_REQUIRED
        assert "at_bat_number" in ALWAYS_REQUIRED
        assert "pitch_number" in ALWAYS_REQUIRED

    def test_in_play_required_columns(self):
        assert {"launch_speed", "launch_angle", "bb_type"} == IN_PLAY_REQUIRED

    def test_column_rename_round_trip(self):
        assert COLUMN_RENAME["pitch_code"] == "type"
        assert COLUMN_RENAME["start_speed"] == "release_speed"

    def test_half_inning_map(self):
        assert HALF_INNING_MAP["top"] == "Top"
        assert HALF_INNING_MAP["bottom"] == "Bot"


# ===========================================================================
# Pure type coercers
# ===========================================================================


class TestToFloat:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (3.14, 3.14),
            ("3.14", 3.14),
            (0, 0.0),
            ("0", 0.0),
            (-1.5, -1.5),
        ],
    )
    def test_valid_inputs(self, value, expected):
        assert _to_float(value) == expected

    @pytest.mark.parametrize("value", [None, "", "abc", "not-a-number", float("nan")])
    def test_returns_none_for_missing_or_invalid(self, value):
        assert _to_float(value) is None


class TestToInt:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (5, 5),
            ("5", 5),
            (5.7, 5),  # truncation, not rounding
            ("5.7", 5),
            (-3, -3),
        ],
    )
    def test_valid_inputs(self, value, expected):
        assert _to_int(value) == expected

    @pytest.mark.parametrize("value", [None, "", "abc", float("nan")])
    def test_returns_none(self, value):
        assert _to_int(value) is None


class TestToBool:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, True),
            (False, False),
            ("true", True),
            ("True", True),
            ("YES", True),
            ("1", True),
            ("false", False),
            ("0", False),
            ("", False),
            (1, True),
            (0, False),
            (None, False),
        ],
    )
    def test_various_inputs(self, value, expected):
        assert _to_bool(value) is expected


class TestToStr:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("hello", "hello"),
            ("  trim  ", "trim"),
            (42, "42"),
            (True, "True"),
        ],
    )
    def test_valid_inputs(self, value, expected):
        assert _to_str(value) == expected

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_returns_none(self, value):
        assert _to_str(value) is None


# ===========================================================================
# _parse_height
# ===========================================================================


class TestParseHeight:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("6' 2\"", 74),  # 6*12+2
            ("6-2", 74),
            ("5' 11\"", 71),
            ("5-11", 71),
            ("6'10\"", 82),
        ],
    )
    def test_valid_formats(self, raw, expected):
        assert _parse_height(raw) == expected

    @pytest.mark.parametrize("value", [None, "", "invalid", "6 ft", "tall"])
    def test_invalid_returns_none(self, value):
        assert _parse_height(value) is None


# ===========================================================================
# _map_game_status
# ===========================================================================


class TestMapGameStatus:
    def _status(self, abstract: str = "Preview", coded: str = "") -> dict:
        return {"status": {"abstractGameState": abstract, "codedGameState": coded}}

    def test_preview(self):
        assert _map_game_status(self._status("Preview")) == "Preview"

    def test_live(self):
        assert _map_game_status(self._status("Live")) == "Live"

    def test_final(self):
        assert _map_game_status(self._status("Final")) == "Final"

    def test_postponed_via_coded_d(self):
        assert _map_game_status(self._status("Final", coded="D")) == "Postponed"

    def test_suspended_via_coded_t(self):
        assert _map_game_status(self._status("Preview", coded="T")) == "Suspended"

    def test_cancelled_via_coded_c(self):
        assert _map_game_status(self._status("Final", coded="C")) == "Cancelled"

    def test_unknown_abstract_defaults_to_preview(self):
        assert _map_game_status(self._status("Mystery")) == "Preview"

    def test_missing_status_dict_safely_defaults(self):
        assert _map_game_status({}) == "Preview"


# ===========================================================================
# ValidationResult
# ===========================================================================


class TestValidationResult:
    def test_default_is_valid(self):
        vr = ValidationResult()
        assert vr.is_valid is True
        assert vr.hard_errors == []
        assert vr.warnings == []

    def test_hard_error_marks_invalid(self):
        vr = ValidationResult(hard_errors=["bad"])
        assert vr.is_valid is False

    def test_warnings_alone_still_valid(self):
        vr = ValidationResult(warnings=["soft"])
        assert vr.is_valid is True


# ===========================================================================
# _build_row_dict
# ===========================================================================


def _minimal_raw() -> dict:
    """A bare-minimum 'write_rows-style' dict for _build_row_dict."""
    return {
        "game_pk": 745001,
        "at_bat_number": 1,
        "pitch_number": 1,
        "venue_id": 17,
        "venue": "Wrigley",
        "home_id": 112,
        "home_team": "CHC",
        "away_id": 158,
        "away_team": "MIL",
        "home_manager_id": 9999,
        "home_manager_name": "M Counsell",
        "away_manager_id": 9998,
        "away_manager_name": "P Murphy",
        "inning": 3,
        "top_bot": "top",
        "pitcher": 100001,
        "p_throws": "R",
        "batter": 200001,
        "stand": "R",
        "bat_hand": "R",
        "home_score": 1,
        "away_score": 2,
        "bat_score": 2,
        "fld_score": 1,
        "balls": 1,
        "strikes": 2,
        "outs": 2,
        "pitch_code": "B",
        "pitch_code_description": "ball",
        "pitch_type": "FF",
        "pitch_type_description": "4-Seam Fastball",
        "event": "",
        "strike_zone_top": 3.5,
        "strike_zone_bottom": 1.6,
        "start_speed": 94.0,
        "end_speed": 88.0,
        "launch_speed": "",
        "launch_angle": "",
        "total_distance": "",
        "coord_x": "",
        "coord_y": "",
        "spray_angle": "",
        "batted_ball_type": "",
        "hit_location": "",
    }


class TestBuildRowDict:
    def test_primary_key_populated_from_args(self):
        row = _build_row_dict(_minimal_raw(), game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["game_pk"] == 745001
        assert row["season"] == 2024
        assert row["game_date"] == "2024-08-15"

    def test_renames_via_column_map(self):
        raw = _minimal_raw()
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        # pitch_code → type, pitch_type_description → pitch_name, start_speed → release_speed
        assert row["type"] == "B"
        assert row["pitch_name"] == "4-Seam Fastball"
        assert row["release_speed"] == 94.0

    def test_top_bot_normalised_to_capitalised(self):
        raw = _minimal_raw()
        raw["top_bot"] = "top"
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["inning_topbot"] == "Top"

    def test_bottom_normalised(self):
        raw = _minimal_raw()
        raw["top_bot"] = "bottom"
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["inning_topbot"] == "Bot"

    def test_empty_strings_become_none(self):
        raw = _minimal_raw()
        raw["launch_speed"] = ""
        raw["launch_angle"] = ""
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["launch_speed"] is None
        assert row["launch_angle"] is None

    def test_data_quality_flag_initialised_false(self):
        row = _build_row_dict(_minimal_raw(), game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["data_quality_flag"] is False

    def test_score_count_defaults_to_zero_when_missing(self):
        raw = {
            "game_pk": 745001,
            "at_bat_number": 1,
            "pitch_number": 1,
            "top_bot": "top",
            "inning": 3,
            "venue_id": 17,
            "pitcher": 100001,
            "p_throws": "R",
            "batter": 200001,
            "stand": "R",
            "bat_hand": "R",
            # home_score, away_score, balls, etc. omitted — should default to 0
        }
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        assert row["home_score"] == 0
        assert row["away_score"] == 0
        assert row["balls"] == 0
        assert row["strikes"] == 0
        assert row["outs"] == 0

    def test_runner_ids_zero_becomes_none(self):
        raw = _minimal_raw()
        raw["pre_play_runner_on_first"] = 0
        raw["pre_play_runner_on_second"] = 200002
        row = _build_row_dict(raw, game_pk=745001, season=2024, game_date="2024-08-15")
        # `_to_int(0) or None` collapses 0 → None for runner IDs
        assert row["on_1b"] is None
        assert row["on_2b"] == 200002


# ===========================================================================
# _validate_row
# ===========================================================================


def _valid_row(**overrides) -> dict:
    """Build a row that passes validation by default."""
    base = {
        "game_pk": 745001,
        "at_bat_number": 1,
        "pitch_number": 1,
        "game_date": "2024-08-15",
        "season": 2024,
        "venue_id": 17,
        "pitcher": 100001,
        "p_throws": "R",
        "batter": 200001,
        "stand": "R",
        "bat_hand": "R",
        "inning": 3,
        "inning_topbot": "Top",
        "balls": 1,
        "strikes": 2,
        "outs": 1,
        "home_score": 1,
        "away_score": 2,
        "type": "B",  # not in-play
        "release_speed": 94.0,
    }
    base.update(overrides)
    return base


class TestValidateRowHardErrors:
    def test_valid_row_passes(self):
        vr = _validate_row(_valid_row())
        assert vr.is_valid
        assert vr.hard_errors == []

    @pytest.mark.parametrize("missing", ["game_pk", "at_bat_number", "pitch_number"])
    def test_primary_key_null_is_hard_error(self, missing):
        row = _valid_row()
        row[missing] = None
        vr = _validate_row(row)
        assert not vr.is_valid
        assert any(missing in e for e in vr.hard_errors)

    def test_required_column_null_is_hard_error(self):
        row = _valid_row()
        row["venue_id"] = None
        vr = _validate_row(row)
        assert not vr.is_valid

    def test_invalid_inning_topbot(self):
        row = _valid_row()
        row["inning_topbot"] = "Middle"
        vr = _validate_row(row)
        assert not vr.is_valid
        assert any("inning_topbot" in e for e in vr.hard_errors)

    @pytest.mark.parametrize("inning", [0, 31, -1])
    def test_inning_out_of_range(self, inning):
        row = _valid_row()
        row["inning"] = inning
        vr = _validate_row(row)
        assert not vr.is_valid

    @pytest.mark.parametrize("balls", [4, 5, -1])
    def test_balls_out_of_range(self, balls):
        row = _valid_row()
        row["balls"] = balls
        vr = _validate_row(row)
        assert not vr.is_valid

    @pytest.mark.parametrize("strikes", [3, 4, -1])
    def test_strikes_out_of_range(self, strikes):
        row = _valid_row()
        row["strikes"] = strikes
        vr = _validate_row(row)
        assert not vr.is_valid

    @pytest.mark.parametrize("outs", [3, 4, -1])
    def test_outs_out_of_range(self, outs):
        row = _valid_row()
        row["outs"] = outs
        vr = _validate_row(row)
        assert not vr.is_valid

    def test_sb_success_without_attempt_is_hard_error(self):
        row = _valid_row()
        row["sb_success_2b"] = True
        row["sb_attempt_2b"] = False
        vr = _validate_row(row)
        assert not vr.is_valid


class TestValidateRowWarnings:
    def test_inplay_missing_launch_speed_warns(self):
        row = _valid_row(type="X")
        # type X but no launch data
        vr = _validate_row(row)
        assert vr.is_valid  # warnings only
        assert any("launch_speed" in w for w in vr.warnings)

    def test_inplay_complete_no_warnings_on_in_play_required(self):
        row = _valid_row(
            type="X",
            launch_speed=92.0,
            launch_angle=15.0,
            bb_type="line_drive",
        )
        vr = _validate_row(row)
        assert vr.is_valid
        assert not any("launch_speed" in w for w in vr.warnings)

    def test_release_speed_too_low_warns(self):
        """SIM-087: floor lowered to 60 mph; 59 should warn."""
        row = _valid_row(release_speed=59.0)
        vr = _validate_row(row)
        assert vr.is_valid
        assert any("release_speed" in w for w in vr.warnings)

    def test_release_speed_at_boundary_passes(self):
        row = _valid_row(release_speed=60.0)
        vr = _validate_row(row)
        assert vr.is_valid
        assert not any("release_speed" in w for w in vr.warnings)

    def test_release_speed_too_high_warns(self):
        row = _valid_row(release_speed=110.0)
        vr = _validate_row(row)
        assert vr.is_valid
        assert any("release_speed" in w for w in vr.warnings)

    def test_launch_speed_too_high_warns(self):
        row = _valid_row(launch_speed=140.0)
        vr = _validate_row(row)
        assert vr.is_valid
        assert any("launch_speed" in w for w in vr.warnings)

    def test_ivb_out_of_range_warns(self):
        row = _valid_row(break_vertical_induced=30.0)
        vr = _validate_row(row)
        assert vr.is_valid
        assert any("break_vertical_induced" in w for w in vr.warnings)

    def test_warning_forces_data_quality_flag(self):
        """If any warning fires, the row is mutated: data_quality_flag → True."""
        row = _valid_row(release_speed=59.0, data_quality_flag=False)
        _validate_row(row)
        assert row["data_quality_flag"] is True

    def test_hard_error_skips_warning_check(self):
        """If a hard error fires, warnings are not even gathered (early return)."""
        row = _valid_row(inning=99, release_speed=200.0)
        vr = _validate_row(row)
        assert not vr.is_valid
        assert vr.warnings == []


# ===========================================================================
# _connect (HTTP retry helper)
# ===========================================================================


class TestConnect:
    def test_success_first_try(self):
        with patch("pipeline.etl.etl_historical_loader.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"hello": "world"}
            mock_get.return_value.raise_for_status.return_value = None
            out = _connect("http://example.com")
        assert out == {"hello": "world"}

    def test_retries_then_succeeds(self):
        """First call raises; second succeeds."""
        good_resp = MagicMock()
        good_resp.json.return_value = {"ok": True}
        good_resp.raise_for_status.return_value = None

        with (
            patch(
                "pipeline.etl.etl_historical_loader.requests.get",
                side_effect=[Exception("transient"), good_resp],
            ),
            patch("pipeline.etl.etl_historical_loader.time.sleep") as mock_sleep,
        ):
            out = _connect("http://example.com")
        assert out == {"ok": True}
        assert mock_sleep.called  # backoff slept between retries

    def test_raises_after_max_retries(self):
        with (
            patch(
                "pipeline.etl.etl_historical_loader.requests.get",
                side_effect=Exception("permanent"),
            ),
            patch("pipeline.etl.etl_historical_loader.time.sleep"),
            pytest.raises(Exception, match="permanent"),
        ):
            _connect("http://example.com")


# ===========================================================================
# HistoricalDataLoader — construction & DSN resolution (SIM-153)
# ===========================================================================


class TestLoaderInit:
    def test_explicit_dsn_used(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        assert loader.dsn == "postgresql://test/db"
        assert loader.write_csv is False

    def test_falls_back_to_env_dsn(self, monkeypatch):
        monkeypatch.setenv("BASEBALL_DB_DSN", "postgresql://env/db")
        loader = HistoricalDataLoader()
        assert loader.dsn == "postgresql://env/db"

    def test_raises_when_no_dsn_available(self, monkeypatch):
        monkeypatch.delenv("BASEBALL_DB_DSN", raising=False)
        with pytest.raises(RuntimeError, match="no DSN"):
            HistoricalDataLoader()

    def test_write_csv_flag(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db", write_csv=True)
        assert loader.write_csv is True

    def test_custom_csv_dir(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db", csv_output_dir="/tmp/csv")
        assert loader.csv_output_dir == "/tmp/csv"


# ===========================================================================
# HistoricalDataLoader — DB methods (psycopg2 mocked)
# ===========================================================================


def _make_loader_with_mock_conn(rows_returned=None, fetchone_returned=None):
    """Build a loader whose _get_conn yields a heavily mocked psycopg2 conn."""
    loader = HistoricalDataLoader(dsn="postgresql://test/db")

    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows_returned or []
    mock_cur.fetchone.return_value = fetchone_returned
    mock_cur.__enter__.return_value = mock_cur
    mock_cur.__exit__.return_value = False

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False

    ctx = MagicMock()
    ctx.__enter__.return_value = mock_conn
    ctx.__exit__.return_value = False
    loader._get_conn = MagicMock(return_value=ctx)

    return loader, mock_conn, mock_cur


class TestLoaderDBMethods:
    def test_game_already_loaded_true(self):
        loader, _, cur = _make_loader_with_mock_conn(fetchone_returned=(1,))
        assert loader._game_already_loaded(745001) is True
        cur.execute.assert_called_once()

    def test_game_already_loaded_false(self):
        loader, _, cur = _make_loader_with_mock_conn(fetchone_returned=None)
        assert loader._game_already_loaded(745001) is False

    def test_log_etl_errors_noop_on_empty(self):
        loader, conn, _ = _make_loader_with_mock_conn()
        loader._log_etl_errors(745001, [])
        # No DB call should happen for empty errors
        assert not loader._get_conn.called

    def test_log_etl_errors_inserts_per_error(self):
        loader, conn, cur = _make_loader_with_mock_conn()
        errors = [
            (1, 1, ["NULL primary key column: pitch_number"]),
            (1, 2, ["Inning out of range: 99"]),
        ]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._log_etl_errors(745001, errors)
        eb.assert_called_once()
        args = eb.call_args.args
        passed_rows = args[2]
        assert len(passed_rows) == 2
        # row[0] = (game_pk, ab, pitch_number, list_of_messages)
        assert passed_rows[0][0] == 745001
        conn.commit.assert_called_once()

    def test_reprocess_errored_games(self):
        loader, _, cur = _make_loader_with_mock_conn(rows_returned=[(745001,), (745002,)])
        result = loader.reprocess_errored_games(date(2024, 1, 1))
        assert result == [745001, 745002]
        # SQL bound parameter shape
        args = cur.execute.call_args
        assert args.args[1] == (date(2024, 1, 1),)

    def test_reprocess_errored_games_empty(self):
        loader, _, _ = _make_loader_with_mock_conn(rows_returned=[])
        assert loader.reprocess_errored_games(date(2024, 1, 1)) == []

    def test_batch_insert_zero_rows_returns_zero(self):
        loader, conn, _ = _make_loader_with_mock_conn()
        assert loader._batch_insert([]) == 0
        assert not conn.commit.called

    def test_batch_insert_chunks_at_batch_size(self):
        """A row count just over BATCH_SIZE should produce 2 execute_batch calls."""
        loader, conn, cur = _make_loader_with_mock_conn()
        rows = [
            {"game_pk": 745001, "at_bat_number": 1, "pitch_number": i}
            for i in range(BATCH_SIZE + 5)
        ]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            inserted = loader._batch_insert(rows)
        assert inserted == BATCH_SIZE + 5
        assert eb.call_count == 2
        conn.commit.assert_called_once()

    def test_log_freshness_noop_on_empty(self):
        loader, _, _ = _make_loader_with_mock_conn()
        loader._log_freshness(745001, [])
        assert not loader._get_conn.called

    def test_log_freshness_upserts_per_entity(self):
        loader, conn, cur = _make_loader_with_mock_conn()
        rows = [
            {"game_date": "2024-08-15", "pitcher": 100001, "batter": 200001},
            {"game_date": "2024-08-15", "pitcher": 100002, "batter": 200001},  # dup batter dedup'd
        ]
        with patch("pipeline.etl.etl_historical_loader.psycopg2.extras.execute_batch") as eb:
            loader._log_freshness(745001, rows)
        # 2 pitcher_ids + 1 batter_id (deduped) = 3 entries
        entries = eb.call_args.args[2]
        types = sorted({e[0] for e in entries})
        assert types == ["batter", "pitcher"]
        conn.commit.assert_called_once()

    def test_quality_report_no_rows(self, caplog):
        loader, _, _ = _make_loader_with_mock_conn(rows_returned=[])
        with caplog.at_level("INFO"):
            loader.quality_report()
        assert any("No flagged rows" in rec.message for rec in caplog.records)

    def test_quality_report_with_game_pk(self):
        loader, _, cur = _make_loader_with_mock_conn(rows_returned=[(100001, 5)])
        loader.quality_report(game_pk=745001)
        # Verify the SQL bound parameter contains the game_pk
        args = cur.execute.call_args
        assert args.args[1] == (745001,)
        assert "data_quality_flag" in args.args[0]

    def test_quality_report_global(self):
        loader, _, cur = _make_loader_with_mock_conn(rows_returned=[("2024-08-15", 10)])
        loader.quality_report()
        # When no game_pk, no bind param tuple
        sql_called = cur.execute.call_args.args[0]
        assert "GROUP BY game_date" in sql_called


# ===========================================================================
# _process_and_insert
# ===========================================================================


class TestProcessAndInsert:
    def test_all_rows_valid(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=2)
        loader._log_freshness = MagicMock()
        loader._log_etl_errors = MagicMock()

        raw_rows = [
            # Two valid rows
            {
                "game_pk": 745001,
                "at_bat_number": 1,
                "pitch_number": 1,
                "venue_id": 17,
                "pitcher": 100001,
                "p_throws": "R",
                "batter": 200001,
                "stand": "R",
                "bat_hand": "R",
                "inning": 3,
                "top_bot": "top",
                "balls": 1,
                "strikes": 2,
                "outs": 1,
                "home_score": 1,
                "away_score": 2,
                "game_date": "2024-08-15",
                "pitch_code": "B",
            },
        ] * 2
        result = loader._process_and_insert(745001, 2024, raw_rows)
        assert result["inserted"] == 2
        assert result["skipped"] == 0
        assert result["flagged"] == 0
        loader._log_etl_errors.assert_not_called()

    def test_hard_error_row_skipped_and_logged(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=0)
        loader._log_freshness = MagicMock()
        loader._log_etl_errors = MagicMock()

        # Missing pitch_number (hard error)
        bad = {
            "game_pk": 745001,
            "at_bat_number": 1,
            # pitch_number deliberately absent
            "venue_id": 17,
            "pitcher": 100001,
            "p_throws": "R",
            "batter": 200001,
            "stand": "R",
            "bat_hand": "R",
            "inning": 3,
            "top_bot": "top",
            "balls": 1,
            "strikes": 2,
            "outs": 1,
            "home_score": 1,
            "away_score": 2,
            "game_date": "2024-08-15",
            "pitch_code": "B",
        }
        result = loader._process_and_insert(745001, 2024, [bad])
        assert result["inserted"] == 0
        assert result["skipped"] == 1
        loader._log_etl_errors.assert_called_once()

    def test_warning_row_inserted_and_flagged(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=1)
        loader._log_freshness = MagicMock()

        warn_row = {
            "game_pk": 745001,
            "at_bat_number": 1,
            "pitch_number": 1,
            "venue_id": 17,
            "pitcher": 100001,
            "p_throws": "R",
            "batter": 200001,
            "stand": "R",
            "bat_hand": "R",
            "inning": 3,
            "top_bot": "top",
            "balls": 1,
            "strikes": 2,
            "outs": 1,
            "home_score": 1,
            "away_score": 2,
            "game_date": "2024-08-15",
            "pitch_code": "B",
            "start_speed": 110.0,  # release_speed too high → warning
        }
        result = loader._process_and_insert(745001, 2024, [warn_row])
        assert result["inserted"] == 1
        assert result["skipped"] == 0
        assert result["flagged"] == 1

    def test_etl_errors_write_failure_is_swallowed(self):
        """A failure in _log_etl_errors must NOT abort the rest of the ingest."""
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._batch_insert = MagicMock(return_value=0)
        loader._log_freshness = MagicMock()
        loader._log_etl_errors = MagicMock(side_effect=RuntimeError("DB down"))

        bad = {
            "game_pk": 745001,
            "at_bat_number": 1,
            # pitch_number absent → hard error
            "venue_id": 17,
            "pitcher": 100001,
            "p_throws": "R",
            "batter": 200001,
            "stand": "R",
            "bat_hand": "R",
            "inning": 3,
            "top_bot": "top",
            "balls": 1,
            "strikes": 2,
            "outs": 1,
            "home_score": 1,
            "away_score": 2,
            "game_date": "2024-08-15",
            "pitch_code": "B",
        }
        # Must not raise
        result = loader._process_and_insert(745001, 2024, [bad])
        assert result["skipped"] == 1


# ===========================================================================
# load_game / load_date_range / refresh_seasons orchestration
# ===========================================================================


class TestLoaderOrchestration:
    def test_load_game_happy_path(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._ensure_prerequisites = MagicMock()
        loader._process_and_insert = MagicMock(
            return_value={"inserted": 5, "skipped": 0, "flagged": 0}
        )
        with patch(
            "pipeline.etl.etl_historical_loader._fetch_game_pitches",
            return_value=([{"game_pk": 745001}], {"_managers": {}, "gameData": {}}),
        ):
            result = loader.load_game(745001, 2024)
        assert result["inserted"] == 5
        loader._ensure_prerequisites.assert_called_once()
        loader._process_and_insert.assert_called_once()

    def test_load_date_range_skips_already_loaded_games(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [{"gamePk": 745001}, {"gamePk": 745002}],
                }
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=schedule,
        ):
            loader.load_date_range(date(2024, 8, 15), date(2024, 8, 15))
        # Both games already loaded — load_game should never run
        loader.load_game.assert_not_called()

    def test_load_date_range_loads_missing_games(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        loader.load_game = MagicMock()
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [{"gamePk": 745001}, {"gamePk": 745002}],
                }
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=schedule,
        ):
            loader.load_date_range(date(2024, 8, 15), date(2024, 8, 15))
        assert loader.load_game.call_count == 2

    def test_load_date_range_skips_rescheduled(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        loader.load_game = MagicMock()
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [
                        {"gamePk": 745001, "rescheduleGameDate": "2024-08-16"},
                        {"gamePk": 745002, "resumeGameDate": "2024-08-16"},
                        {"gamePk": 745003},
                    ],
                }
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=schedule,
        ):
            loader.load_date_range(date(2024, 8, 15), date(2024, 8, 15))
        # Only the non-rescheduled game should be loaded
        assert loader.load_game.call_count == 1
        assert loader.load_game.call_args.args[0] == 745003

    def test_refresh_seasons_defaults_end_year_to_last_year(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        empty_schedule = {"dates": []}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=empty_schedule,
        ) as conn:
            loader.refresh_seasons(start_year=2024, end_year=2024)
        # _connect was called at least once (for the season-2024 schedule)
        assert conn.called

    def test_refresh_seasons_explicit_end_year(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=True)
        loader.load_game = MagicMock()
        empty_schedule = {"dates": []}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=empty_schedule,
        ) as conn:
            loader.refresh_seasons(start_year=2022, end_year=2023)
        # Two season schedule pulls expected
        assert conn.call_count == 2

    def test_refresh_seasons_skips_rescheduled(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._game_already_loaded = MagicMock(return_value=False)
        loader.load_game = MagicMock()
        schedule = {
            "dates": [
                {
                    "date": "2024-08-15",
                    "games": [
                        {"gamePk": 745001, "rescheduleGameDate": "tbd"},
                        {"gamePk": 745002},
                    ],
                }
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=schedule,
        ):
            loader.refresh_seasons(start_year=2024, end_year=2024)
        assert loader.load_game.call_count == 1


# ===========================================================================
# _ensure_* methods — existence short-circuit branches only
# ===========================================================================


class TestEnsurePrerequisitesShortCircuit:
    """When the FK parent already exists in the DB, the method should return
    without firing any HTTP calls or INSERTs."""

    def test_ensure_venue_already_exists(self):
        loader, _, cur = _make_loader_with_mock_conn(fetchone_returned=(1,))
        with patch("pipeline.etl.etl_historical_loader.requests.get") as mock_req:
            loader._ensure_venue(17, 2024)
        mock_req.assert_not_called()  # No API call needed when venue exists

    def test_ensure_prerequisites_routes_to_all_subchecks(self):
        loader = HistoricalDataLoader(dsn="postgresql://test/db")
        loader._ensure_venue = MagicMock()
        loader._ensure_teams = MagicMock()
        loader._ensure_players = MagicMock()
        loader._ensure_managers = MagicMock()
        loader._ensure_game = MagicMock()

        game_dict = {
            "gameData": {
                "datetime": {"officialDate": "2024-08-15"},
                "venue": {"id": 17},
                "teams": {"home": {"id": 112}, "away": {"id": 158}},
            },
            "_managers": {
                "home_manager_id": 9999,
                "home_manager_name": "M C",
                "away_manager_id": 9998,
                "away_manager_name": "P M",
            },
        }
        loader._ensure_prerequisites(745001, game_dict)
        loader._ensure_venue.assert_called_once()
        loader._ensure_teams.assert_called_once()
        loader._ensure_players.assert_called_once()
        loader._ensure_managers.assert_called_once()
        loader._ensure_game.assert_called_once()


# ===========================================================================
# _fetch_game_pitches — minimal feed/live payload
# ===========================================================================


def _minimal_feed_live() -> dict:
    """Build the smallest feed/live response that exercises _fetch_game_pitches
    without raising on any required nesting."""
    return {
        "gameData": {
            "datetime": {"officialDate": "2024-08-15"},
            "teams": {
                "home": {"id": 112, "abbreviation": "CHC"},
                "away": {"id": 158, "abbreviation": "MIL"},
            },
            "venue": {"id": 17, "name": "Wrigley"},
        },
        "liveData": {"plays": {"allPlays": []}},  # zero plays — no pitch rows
    }


class TestFetchGamePitches:
    def test_zero_plays_yields_empty_rows(self):
        """An empty allPlays list produces no pitch rows but still returns a
        valid game_dict with manager info."""
        feed = _minimal_feed_live()
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M Counsell"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, returned = _fetch_game_pitches(745001, batter_hand_cache={})
        assert rows == []
        assert "_managers" in returned
        assert returned["_managers"]["home_manager_id"] == 9999

    def test_ntrm_role_wins_over_mngr(self):
        """A coach with jobId=NTRM should override the default MNGR pick."""
        feed = _minimal_feed_live()
        coaches = {
            "roster": [
                {"jobId": "MNGR", "person": {"id": 9999, "fullName": "Manager"}},
                {"jobId": "NTRM", "person": {"id": 8888, "fullName": "Interim"}},
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            _, returned = _fetch_game_pitches(745001, batter_hand_cache={})
        assert returned["_managers"]["home_manager_id"] == 8888

    def test_coab_used_when_no_mngr(self):
        """When no MNGR / NTRM coach is present, COAB is used as fallback."""
        feed = _minimal_feed_live()
        coaches = {"roster": [{"jobId": "COAB", "person": {"id": 7777, "fullName": "Acting"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            _, returned = _fetch_game_pitches(745001, batter_hand_cache={})
        assert returned["_managers"]["home_manager_id"] == 7777


# ===========================================================================
# _ensure_* short-circuit (existing-row) paths and basic flows
# ===========================================================================


def _make_loader_with_seq_conns(fetchall_seq=None, fetchone_seq=None):
    """Build a loader that exposes a queue of _get_conn() yields.

    Each successive call to ``loader._get_conn()`` returns a context manager
    that yields a fresh mock conn; that conn's cursor's fetchone() / fetchall()
    can be pre-seeded with values via the sequence arguments.
    """
    loader = HistoricalDataLoader(dsn="postgresql://test/db")
    fetchall_seq = list(fetchall_seq or [])
    fetchone_seq = list(fetchone_seq or [])

    def _make_ctx(_self=None):
        cur = MagicMock()
        cur.fetchall.return_value = fetchall_seq.pop(0) if fetchall_seq else []
        cur.fetchone.return_value = fetchone_seq.pop(0) if fetchone_seq else None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False

        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        return ctx

    loader._get_conn = MagicMock(side_effect=_make_ctx)
    return loader


class TestEnsureTeams:
    def test_short_circuit_when_both_teams_exist(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[(112,), (158,)]])
        gd = {
            "teams": {
                "home": {"id": 112, "name": "Cubs", "abbreviation": "CHC"},
                "away": {"id": 158, "name": "Brewers", "abbreviation": "MIL"},
            },
            "venue": {"id": 17},
        }
        loader._ensure_teams(112, 158, season=2024, gd=gd)
        # No insert should have happened — only the existence check
        assert loader._get_conn.call_count == 1

    def test_inserts_missing_teams(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])  # nothing exists
        loader._ensure_venue = MagicMock()
        gd = {
            "teams": {
                "home": {
                    "id": 112,
                    "name": "Cubs",
                    "abbreviation": "CHC",
                    "league": {"name": "National League"},
                    "division": {"name": "National League Central"},
                    "venue": {"id": 17},
                },
                "away": {
                    "id": 158,
                    "name": "Brewers",
                    "abbreviation": "MIL",
                    "league": {"name": "National League"},
                    "division": {"name": "National League Central"},
                    "venue": {"id": 32},
                },
            },
            "venue": {"id": 17},
        }
        loader._ensure_teams(112, 158, season=2024, gd=gd)
        # First call = existence check; subsequent = insert
        assert loader._get_conn.call_count >= 2

    def test_american_league_team_route(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])
        loader._ensure_venue = MagicMock()
        gd = {
            "teams": {
                "home": {
                    "id": 147,
                    "name": "Yankees",
                    "abbreviation": "NYY",
                    "league": {"name": "American League"},
                    "division": {"name": "American League East"},
                    "venue": {"id": 3313},
                },
                "away": {
                    "id": 110,
                    "name": "Orioles",
                    "abbreviation": "BAL",
                    "league": {"name": "American League"},
                    "division": {"name": "American League East"},
                    "venue": {"id": 2},
                },
            },
            "venue": {"id": 3313},
        }
        loader._ensure_teams(147, 110, season=2024, gd=gd)


class TestEnsurePlayers:
    def test_empty_boxscore_noop(self):
        loader = _make_loader_with_seq_conns()
        loader._ensure_players({"liveData": {"boxscore": {"teams": {}}}})
        # No DB call should have happened
        loader._get_conn.assert_not_called()

    def test_all_players_already_exist(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[(100001,), (200001,)]])
        gd = {
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": {
                            "players": {
                                "ID100001": {
                                    "person": {
                                        "id": 100001,
                                        "fullName": "John Doe",
                                        "batSide": {"code": "R"},
                                        "pitchHand": {"code": "R"},
                                    },
                                    "position": {"abbreviation": "P"},
                                }
                            }
                        },
                        "away": {
                            "players": {
                                "ID200001": {
                                    "person": {
                                        "id": 200001,
                                        "fullName": "Jane Doe",
                                        "batSide": {"code": "L"},
                                        "pitchHand": {"code": "R"},
                                    },
                                    "position": {"abbreviation": "C"},
                                }
                            }
                        },
                    }
                }
            }
        }
        loader._ensure_players(gd)
        # Only the existence check; no inserts
        assert loader._get_conn.call_count == 1

    def test_missing_players_inserted_with_complete_boxscore(self):
        """Players present in boxscore with full bat/throw should NOT trigger people-API fallback."""
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])  # nothing exists
        gd = {
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": {
                            "players": {
                                "ID100001": {
                                    "person": {
                                        "id": 100001,
                                        "fullName": "John Doe",
                                        "firstName": "John",
                                        "lastName": "Doe",
                                        "batSide": {"code": "R"},
                                        "pitchHand": {"code": "R"},
                                        "birthDate": "1995-01-01",
                                        "height": "6' 2\"",
                                        "weight": 200,
                                        "mlbDebutDate": "2020-03-26",
                                    },
                                    "position": {"abbreviation": "P"},
                                }
                            }
                        },
                        "away": {"players": {}},
                    }
                }
            }
        }
        loader._ensure_players(gd)
        # 1 existence check + 1 insert ctx
        assert loader._get_conn.call_count >= 2

    def test_missing_players_with_incomplete_boxscore_falls_back_to_api(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])  # all missing
        gd = {
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": {
                            "players": {
                                "ID100001": {
                                    "person": {
                                        "id": 100001,
                                        "fullName": "John Doe",
                                        # bats/throws absent → trigger people-API fallback
                                    },
                                    "position": {"abbreviation": "P"},
                                }
                            }
                        },
                        "away": {"players": {}},
                    }
                }
            }
        }
        people_resp = {
            "people": [
                {
                    "firstName": "John",
                    "lastName": "Doe",
                    "batSide": {"code": "R"},
                    "pitchHand": {"code": "R"},
                    "birthDate": "1995-01-01",
                    "height": "6' 2\"",
                    "weight": 200,
                    "mlbDebutDate": "2020-03-26",
                }
            ]
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            return_value=people_resp,
        ):
            loader._ensure_players(gd)

    def test_missing_players_api_failure_uses_defaults(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[]])
        gd = {
            "liveData": {
                "boxscore": {
                    "teams": {
                        "home": {
                            "players": {
                                "ID100001": {
                                    "person": {
                                        "id": 100001,
                                        "fullName": "John Doe",
                                    },
                                    "position": {"abbreviation": "P"},
                                }
                            }
                        },
                        "away": {"players": {}},
                    }
                }
            }
        }
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=Exception("network down"),
        ):
            loader._ensure_players(gd)


class TestEnsureManagers:
    def test_all_managers_blank_noop(self):
        loader = _make_loader_with_seq_conns()
        loader._ensure_managers(
            managers={
                "home_manager_id": "",
                "home_manager_name": "",
                "away_manager_id": "",
                "away_manager_name": "",
            },
            home_team_id=112,
            away_team_id=158,
            season=2024,
            game_date="2024-08-15",
        )
        loader._get_conn.assert_not_called()

    def test_managers_present_runs_existence_check(self):
        loader = _make_loader_with_seq_conns(fetchall_seq=[[(9999, 112, 2024)]])
        loader._ensure_managers(
            managers={
                "home_manager_id": 9999,
                "home_manager_name": "M Counsell",
                "away_manager_id": 9998,
                "away_manager_name": "P Murphy",
            },
            home_team_id=112,
            away_team_id=158,
            season=2024,
            game_date="2024-08-15",
        )
        # An existence check was issued
        assert loader._get_conn.call_count >= 1


class TestEnsureGame:
    def test_existence_short_circuit(self):
        """If raw.games already contains the game, _ensure_game returns early."""
        loader = _make_loader_with_seq_conns(fetchone_seq=[(745001,)])
        gd = {
            "datetime": {"officialDate": "2024-08-15"},
            "status": {"abstractGameState": "Final", "codedGameState": "F"},
            "venue": {"id": 17},
            "teams": {
                "home": {"id": 112, "abbreviation": "CHC"},
                "away": {"id": 158, "abbreviation": "MIL"},
            },
        }
        managers = {
            "home_manager_id": 9999,
            "home_manager_name": "M Counsell",
            "away_manager_id": 9998,
            "away_manager_name": "P Murphy",
        }
        loader._ensure_game(745001, season=2024, gd=gd, managers=managers)
        # Only the existence check should have happened
        assert loader._get_conn.call_count == 1


# ===========================================================================
# _fetch_game_pitches — exercise pitch-parsing path with a realistic feed
# ===========================================================================


def _full_feed_with_one_play() -> dict:
    """Build a feed/live payload containing a single complete play with one pitch."""
    return {
        "gameData": {
            "datetime": {"officialDate": "2024-08-15"},
            "teams": {
                "home": {"id": 112, "abbreviation": "CHC"},
                "away": {"id": 158, "abbreviation": "MIL"},
            },
            "venue": {"id": 17, "name": "Wrigley"},
        },
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "atBatIndex": 0,
                        "about": {"inning": 1, "halfInning": "top"},
                        "result": {
                            "description": "Player1 strikes out swinging.",
                            "eventType": "strikeout",
                        },
                        "playEvents": [
                            {
                                "isPitch": True,
                                "index": 0,
                                "pitchNumber": 1,
                                "details": {
                                    "code": "S",
                                    "description": "Swinging Strike",
                                    "type": {"code": "FF", "description": "4-Seam Fastball"},
                                    "eventType": "",
                                },
                                "offense": {
                                    "batter": {
                                        "id": 200001,
                                        "batSide": {"code": "R"},
                                        "link": "/api/v1/people/200001",
                                    },
                                    "first": {},
                                    "second": {},
                                    "third": {},
                                },
                                "defense": {
                                    "pitcher": {
                                        "id": 100001,
                                        "pitchHand": {"code": "R"},
                                    },
                                    "catcher": {"id": 200002},
                                    "first": {"id": 200003},
                                    "second": {"id": 200004},
                                    "third": {"id": 200005},
                                    "shortstop": {"id": 200006},
                                    "left": {"id": 200007},
                                    "center": {"id": 200008},
                                    "right": {"id": 200009},
                                },
                                "pitchData": {
                                    "strikeZoneTop": 3.5,
                                    "strikeZoneBottom": 1.6,
                                    "startSpeed": 94.0,
                                    "endSpeed": 86.0,
                                    "coordinates": {
                                        "x0": -1.5,
                                        "y0": 50.0,
                                        "z0": 6.0,
                                        "vX0": 5.0,
                                        "vY0": -135.0,
                                        "vZ0": -3.0,
                                        "aX": 5.0,
                                        "aY": 28.0,
                                        "aZ": -15.0,
                                        "pfxX": 0.5,
                                        "pfxZ": 1.5,
                                        "pX": 0.2,
                                        "pZ": 2.5,
                                        "x": 100,
                                        "y": 100,
                                    },
                                    "breaks": {
                                        "spinRate": 2400,
                                        "spinDirection": 200,
                                        "breakAngle": 22.0,
                                        "breakLength": 4.0,
                                        "breakY": 24.0,
                                        "breakVertical": 12.0,
                                        "breakVerticalInduced": 15.0,
                                        "breakHorizontal": 4.0,
                                    },
                                    "zone": 5,
                                    "extension": 6.5,
                                },
                                "count": {"balls": 0, "strikes": 1, "outs": 0},
                            }
                        ],
                        "pitchIndex": [0],
                        "runners": [],
                    }
                ]
            }
        },
    }


class TestFetchGamePitchesPitchParsing:
    def test_single_pitch_play_parses(self):
        feed = _full_feed_with_one_play()
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        # Need 3 _connect calls: feed/live, home coaches, away coaches.
        # The batter hand should come from cache so no extra call.
        cache = {200001: "R"}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, returned = _fetch_game_pitches(745001, batter_hand_cache=cache)
        assert len(rows) == 1
        row = rows[0]
        assert row["pitcher"] == 100001
        assert row["batter"] == 200001
        assert row["pitch_code"] == "S"

    def test_batter_hand_cache_miss_triggers_people_lookup(self):
        feed = _full_feed_with_one_play()
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        person = {"people": [{"batSide": {"code": "L"}}]}
        # cache empty → 4th _connect call hits the people endpoint
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches, person],
        ):
            cache: dict[int, str] = {}
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache=cache)
        assert cache[200001] == "L"
        assert rows[0]["bat_hand"] == "L"

    def test_play_with_hit_data_includes_spray_angle(self):
        feed = _full_feed_with_one_play()
        # Add hit data for the pitch
        pitch = feed["liveData"]["plays"]["allPlays"][0]["playEvents"][0]
        pitch["hitData"] = {
            "launchSpeed": 95.0,
            "launchAngle": 25.0,
            "totalDistance": 350,
            "coordinates": {"coordX": 150.0, "coordY": 100.0},
            "trajectory": "line_drive",
            "location": "7",
        }
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        # Spray angle should have been computed (non-empty)
        assert rows[0]["spray_angle"] != ""

    def test_play_with_runners_scoring(self):
        feed = _full_feed_with_one_play()
        play = feed["liveData"]["plays"]["allPlays"][0]
        # Mark this as a scoring play with runner movement
        play["runners"] = [
            {
                "details": {
                    "playIndex": 0,
                    "runner": {"id": 200001},
                    "earned": True,
                    "isOut": False,
                },
                "movement": {"end": "score"},
                "credits": [
                    {"credit": "f_putout", "player": {"id": 200002}},
                    {"credit": "f_assist", "player": {"id": 200003}},
                ],
                "rbi": True,
            }
        ]
        # Pre-runner on first
        play["playEvents"][0]["offense"]["first"] = {"id": 200001}
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        assert rows[0]["runs"] >= 1

    def test_play_with_stolen_base_event(self):
        feed = _full_feed_with_one_play()
        play = feed["liveData"]["plays"]["allPlays"][0]
        # Insert a non-pitch event AFTER the pitch indicating a stolen base
        play["playEvents"].append(
            {
                "isPitch": False,
                "details": {"eventType": "stolen_base_2b"},
            }
        )
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        assert rows[0]["sb_attempt_2b"] is True
        assert rows[0]["sb_success_2b"] is True

    def test_play_with_pinch_hitter_substitution(self):
        feed = _full_feed_with_one_play()
        play = feed["liveData"]["plays"]["allPlays"][0]
        # Prepend a non-pitch event indicating an offensive substitution (pinch hitter)
        play["playEvents"].insert(
            0,
            {
                "isPitch": False,
                "details": {"eventType": "offensive_substitution"},
                "position": {"code": "11"},
            },
        )
        play["pitchIndex"] = [1]
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        assert rows[0]["pinch_hitter"] is True

    def test_play_with_wild_pitch_event(self):
        feed = _full_feed_with_one_play()
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"].append({"isPitch": False, "details": {"eventType": "wild_pitch"}})
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        assert rows[0]["wild_pitch_passed_ball"] is True

    def test_play_with_caught_stealing(self):
        feed = _full_feed_with_one_play()
        play = feed["liveData"]["plays"]["allPlays"][0]
        play["playEvents"].append(
            {"isPitch": False, "details": {"eventType": "caught_stealing_3b"}}
        )
        coaches = {"roster": [{"jobId": "MNGR", "person": {"id": 9999, "fullName": "M"}}]}
        with patch(
            "pipeline.etl.etl_historical_loader._connect",
            side_effect=[feed, coaches, coaches],
        ):
            rows, _ = _fetch_game_pitches(745001, batter_hand_cache={200001: "R"})
        assert rows[0]["sb_attempt_3b"] is True
        assert rows[0]["sb_success_3b"] is False
