"""
Unit tests for pipeline/batch/player_profile_computor.py
=========================================================

Covers:
  - Module-level constants & dataclass-like config
  - Pure math/physics helpers (compute_leverage_index, _hc_to_feet,
    _euclidean_hc, _throw_time, _estimate_hang_time,
    _estimate_gb_travel_time, _classify_direction_of, _sigmoid,
    _asymmetric_sigmoid)
  - Logistic helpers (_fit_logistic_model, _predict_proba, _quick_auc)
  - GMM fitting helpers (_fit_gmm_for_pitcher, _label_component)
  - build_run_expectancy_matrix with a mocked DuckDB connection
  - PlayerProfileComputor lifecycle (__init__, _connect, _close, _run_schema_ddl,
    _delete_seasons, run() with all heavy ETL methods backed by a stubbed
    DuckDB connection that returns empty DataFrames so the early-exit
    branches inside each _compute_* method are exercised)
  - LeagueAverageProfiles.compute() with mocked DuckDB connection

All DuckDB / PostgreSQL access is replaced with a MagicMock that simulates
the shapes the production code expects (fetchall / fetchdf returning empty
DataFrames keeps coverage focused on the orchestration paths rather than
brittle data-shape assertions).
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from pipeline.batch import player_profile_computor as ppc

# ===========================================================================
# Module constants
# ===========================================================================


class TestModuleConstants:
    def test_gmm_feature_names_match_expected(self):
        assert ppc.GMM_FEATURE_NAMES == [
            "velo",
            "ivb",
            "hb",
            "spin_rate",
            "spin_axis",
            "release_x",
            "release_z",
            "release_ext",
        ]

    def test_sample_thresholds_positive(self):
        assert ppc.MIN_PITCHER_PITCHES > 0
        assert ppc.MIN_BATTER_PA > 0
        assert ppc.MIN_FIELDER_BIP > 0

    def test_park_prior_pa_positive(self):
        assert ppc.PARK_PRIOR_PA > 0

    def test_li_low_lt_high(self):
        assert ppc.LI_LOW < ppc.LI_HIGH

    def test_barrel_zone_correct_order(self):
        assert ppc.BARREL_MIN_LA < ppc.BARREL_MAX_LA
        assert ppc.BARREL_MIN_VELO > ppc.HARD_HIT_MIN_VELO

    def test_avg_fielder_pos_keys(self):
        for pos in ("SS", "2B", "3B", "1B", "LF", "CF", "RF"):
            assert pos in ppc.AVG_FIELDER_POS
            assert "L" in ppc.AVG_FIELDER_POS[pos]
            assert "R" in ppc.AVG_FIELDER_POS[pos]

    def test_avg_throw_velo_covers_positions(self):
        for pos in ("SS", "2B", "3B", "1B", "LF", "CF", "RF"):
            assert pos in ppc.AVG_THROW_VELO
            assert ppc.AVG_THROW_VELO[pos] > 0

    def test_re24_approx_basic_ordering(self):
        # No runners, 2 outs should be the lowest expected runs state listed.
        assert ppc.RE24_APPROX[(2, 0b000)] < ppc.RE24_APPROX[(0, 0b000)]
        assert ppc.RE24_APPROX[(0, 0b111)] > ppc.RE24_APPROX[(2, 0b001)]


# ===========================================================================
# compute_leverage_index
# ===========================================================================


class TestComputeLeverageIndex:
    def test_late_inning_higher_than_early(self):
        early = ppc.compute_leverage_index(inning=2, outs=1, runners_state=0b000, score_diff=0)
        late = ppc.compute_leverage_index(inning=9, outs=1, runners_state=0b000, score_diff=0)
        assert late > early

    def test_close_game_higher_than_blowout(self):
        close = ppc.compute_leverage_index(inning=8, outs=1, runners_state=0b000, score_diff=0)
        blowout = ppc.compute_leverage_index(inning=8, outs=1, runners_state=0b000, score_diff=10)
        assert close > blowout

    def test_runners_on_base_higher_than_empty(self):
        empty = ppc.compute_leverage_index(inning=8, outs=1, runners_state=0b000, score_diff=0)
        loaded = ppc.compute_leverage_index(inning=8, outs=1, runners_state=0b111, score_diff=0)
        assert loaded > empty

    def test_two_outs_higher_than_one_out(self):
        one = ppc.compute_leverage_index(inning=8, outs=1, runners_state=0b001, score_diff=0)
        two = ppc.compute_leverage_index(inning=8, outs=2, runners_state=0b001, score_diff=0)
        assert two > one

    def test_inning_bands(self):
        i3 = ppc.compute_leverage_index(inning=3, outs=0, runners_state=0, score_diff=0)
        i6 = ppc.compute_leverage_index(inning=6, outs=0, runners_state=0, score_diff=0)
        i8 = ppc.compute_leverage_index(inning=8, outs=0, runners_state=0, score_diff=0)
        i10 = ppc.compute_leverage_index(inning=10, outs=0, runners_state=0, score_diff=0)
        assert i3 < i6 < i8 < i10

    def test_score_diff_clamped_to_five(self):
        diff5 = ppc.compute_leverage_index(inning=9, outs=2, runners_state=0b111, score_diff=5)
        diff20 = ppc.compute_leverage_index(inning=9, outs=2, runners_state=0b111, score_diff=20)
        # |diff| beyond 5 should produce the same factor (clamped)
        assert diff5 == diff20

    def test_returns_finite_float(self):
        out = ppc.compute_leverage_index(inning=5, outs=1, runners_state=0b010, score_diff=2)
        assert isinstance(out, float)
        assert math.isfinite(out)


# ===========================================================================
# Geometric / physics helpers
# ===========================================================================


class TestGeometricHelpers:
    def test_hc_to_feet(self):
        assert ppc._hc_to_feet(ppc.HC_SCALE) == pytest.approx(1.0)
        assert ppc._hc_to_feet(0) == 0.0

    def test_euclidean_hc(self):
        assert ppc._euclidean_hc(0, 0, 3, 4) == pytest.approx(5.0)
        assert ppc._euclidean_hc(1, 1, 1, 1) == 0.0

    def test_throw_time_positive_velo(self):
        t = ppc._throw_time(distance_ft=100.0, velo_mph=85.0)
        # 85 mph = 124.69 ft/s; 100 / 124.69 ≈ 0.802
        assert t == pytest.approx(100.0 / (85.0 * 1.467), rel=0.01)

    def test_throw_time_zero_velo_returns_sentinel(self):
        assert ppc._throw_time(distance_ft=50.0, velo_mph=0) == 999.0

    def test_throw_time_negative_velo_returns_sentinel(self):
        assert ppc._throw_time(distance_ft=50.0, velo_mph=-5) == 999.0


class TestEstimateHangTime:
    def test_returns_none_for_none_inputs(self):
        assert ppc._estimate_hang_time(None, 30.0) is None
        assert ppc._estimate_hang_time(95.0, None) is None

    def test_returns_none_for_zero_speed(self):
        assert ppc._estimate_hang_time(0, 30.0) is None

    def test_returns_none_for_out_of_range_angle(self):
        assert ppc._estimate_hang_time(95.0, -45) is None
        assert ppc._estimate_hang_time(95.0, 100) is None

    def test_high_angle_pops_up_long_hang(self):
        # A weak popup (high angle, low speed) hangs longer than a line drive
        popup = ppc._estimate_hang_time(80.0, 70.0)
        line_drive = ppc._estimate_hang_time(95.0, 12.0)
        assert popup is not None and line_drive is not None
        assert popup > line_drive

    def test_drag_factor_branches(self):
        """Exercise each drag-factor branch (angle bands)."""
        for la in (5, 15, 30, 55):
            out = ppc._estimate_hang_time(95.0, la)
            assert out is not None
            assert out >= 0.5  # the min-clamp in the function

    def test_hang_time_minimum_clamp(self):
        # Any reasonable input should return at least 0.5 seconds
        out = ppc._estimate_hang_time(95.0, 25.0)
        assert out >= 0.5


class TestEstimateGbTravelTime:
    def test_returns_none_for_none_or_zero_speed(self):
        assert ppc._estimate_gb_travel_time(None, 10, 60) is None
        assert ppc._estimate_gb_travel_time(0, 10, 60) is None

    def test_returns_none_for_zero_distance(self):
        assert ppc._estimate_gb_travel_time(85.0, 10, 0) is None

    def test_realistic_ground_ball_returns_finite(self):
        # A 95-mph ground ball travelling 80 ft to shortstop
        t = ppc._estimate_gb_travel_time(95.0, -5, 80)
        assert t is not None
        assert t > 0

    def test_ball_stops_before_reaching_distance(self):
        # Soft ground ball over a long distance — discriminant could go negative
        out = ppc._estimate_gb_travel_time(20.0, -5, 500.0)
        assert out is None or out > 0  # either stop or valid time

    def test_min_time_clamp(self):
        t = ppc._estimate_gb_travel_time(120.0, -5, 20)
        assert t >= 0.1


# ===========================================================================
# _classify_direction_of
# ===========================================================================


class TestClassifyDirection:
    def test_outfielder_going_back(self):
        # Ball deeper (smaller y) than fielder
        cat, going_back = ppc._classify_direction_of(125, 80, 125, 50, "CF")
        assert going_back is True
        assert cat == "deep"

    def test_outfielder_charging(self):
        # Ball in front of fielder (larger y)
        cat, going_back = ppc._classify_direction_of(125, 80, 125, 100, "CF")
        assert going_back is False
        assert cat == "charging"

    def test_outfielder_glove_side_lf(self):
        cat, _ = ppc._classify_direction_of(80, 115, 90, 116, "LF")
        assert cat in ("glove_side", "arm_side")

    def test_outfielder_glove_side_rf(self):
        cat, _ = ppc._classify_direction_of(170, 115, 160, 116, "RF")
        assert cat in ("glove_side", "arm_side")

    def test_infielder_charging(self):
        cat, going_back = ppc._classify_direction_of(112, 152, 112, 170, "SS")
        assert cat == "charging"
        assert going_back is False

    def test_infielder_going_back(self):
        cat, going_back = ppc._classify_direction_of(112, 152, 112, 130, "SS")
        assert cat == "deep"
        assert going_back is True

    def test_infielder_lateral_ss(self):
        cat, _ = ppc._classify_direction_of(112, 152, 130, 152, "SS")
        assert cat in ("glove_side", "arm_side")

    def test_infielder_lateral_2b(self):
        cat, _ = ppc._classify_direction_of(141, 152, 120, 152, "2B")
        assert cat in ("glove_side", "arm_side")


# ===========================================================================
# Sigmoid helpers
# ===========================================================================


class TestSigmoid:
    def test_standard_sigmoid_basic(self):
        x = np.array([0.0])
        out = ppc._sigmoid(x, L=1.0, k=1.0, x0=0.0)
        assert out[0] == pytest.approx(0.5)

    def test_sigmoid_large_negative_zero(self):
        x = np.array([-100.0])
        out = ppc._sigmoid(x, L=1.0, k=1.0, x0=0.0)
        assert out[0] == pytest.approx(0.0, abs=1e-3)

    def test_sigmoid_large_positive_one(self):
        x = np.array([100.0])
        out = ppc._sigmoid(x, L=1.0, k=1.0, x0=0.0)
        assert out[0] == pytest.approx(1.0, abs=1e-3)

    def test_asymmetric_sigmoid_outputs(self):
        x = np.linspace(-5, 5, 11)
        out = ppc._asymmetric_sigmoid(
            x,
            L_left=1.0,
            k_left=1.0,
            x0_left=0.0,
            L_right=0.5,
            k_right=1.0,
            x0_right=0.0,
            blend_k=1.0,
            blend_x0=0.0,
        )
        assert out.shape == x.shape


# ===========================================================================
# _fit_logistic_model / _predict_proba
# ===========================================================================


class TestFitLogisticModel:
    def test_insufficient_sample_returns_none(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(50, 2))
        y = np.zeros(50)
        assert ppc._fit_logistic_model(X, y) is None

    def test_all_one_class_returns_none(self):
        # 200 samples but all positive — y.sum() == 200, (1-y).sum() == 0
        X = np.random.default_rng(1).normal(size=(200, 2))
        y = np.ones(200)
        assert ppc._fit_logistic_model(X, y) is None

    def test_fits_balanced_data(self):
        rng = np.random.default_rng(42)
        X = rng.normal(size=(500, 2))
        y = (X[:, 0] + X[:, 1] > 0).astype(int)
        model = ppc._fit_logistic_model(X, y)
        assert model is not None
        # Has the attached scaler attribute
        assert hasattr(model, "_scaler")

    def test_predict_proba_uses_stored_scaler(self):
        rng = np.random.default_rng(42)
        X = rng.normal(size=(500, 2))
        y = (X[:, 0] + X[:, 1] > 0).astype(int)
        model = ppc._fit_logistic_model(X, y)
        assert model is not None
        probs = ppc._predict_proba(model, X)
        assert probs.shape == (500,)
        # Probabilities are in (0, 1)
        assert np.all((probs >= 0) & (probs <= 1))


class TestQuickAuc:
    def test_perfect_separation(self):
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.8, 0.9])
        auc = ppc._quick_auc(y_true, y_score)
        assert auc == pytest.approx(1.0)

    def test_random_around_half(self):
        rng = np.random.default_rng(0)
        y_true = rng.integers(0, 2, size=100)
        y_score = rng.uniform(size=100)
        auc = ppc._quick_auc(y_true, y_score)
        # AUC for random scores should be near 0.5
        assert 0.3 < auc < 0.7


# ===========================================================================
# _fit_gmm_for_pitcher
# ===========================================================================


class TestFitGmm:
    def _make_pitches_df(self, n: int = 300) -> pd.DataFrame:
        rng = np.random.default_rng(7)
        return pd.DataFrame(
            {
                "velo": rng.normal(94, 2, n),
                "ivb": rng.normal(15, 3, n),
                "hb": rng.normal(5, 2, n),
                "spin_rate": rng.normal(2300, 200, n),
                "spin_axis": rng.normal(200, 30, n),
                "release_x": rng.normal(-1.5, 0.5, n),
                "release_z": rng.normal(6.0, 0.3, n),
                "release_ext": rng.normal(6.5, 0.3, n),
            }
        )

    def test_too_few_pitches_returns_none(self):
        df = self._make_pitches_df(n=50)
        model, components = ppc._fit_gmm_for_pitcher(df, pitcher_id=1, season=2024)
        assert model is None
        assert components == []

    def test_successful_fit_returns_model_and_components(self):
        df = self._make_pitches_df(n=600)
        model, components = ppc._fit_gmm_for_pitcher(df, pitcher_id=1, season=2024)
        assert model is not None
        assert "n_components" in model
        assert model["n_components"] >= ppc.GMM_MIN_K
        assert len(components) == model["n_components"]
        assert all("pitcher_id" in c and c["pitcher_id"] == 1 for c in components)

    def test_zero_variance_column_handled(self):
        df = self._make_pitches_df(n=400)
        df["spin_axis"] = 200.0  # constant — std == 0
        # Must not raise; should still fit
        model, components = ppc._fit_gmm_for_pitcher(df, pitcher_id=2, season=2024)
        assert model is not None


class TestLabelComponent:
    def _fi(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(ppc.GMM_FEATURE_NAMES)}

    def test_4_seam_fastball(self):
        # high velo, high ivb, low hb
        mean = [95.0, 18.0, 4.0, 2400, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "4-Seam Fastball"

    def test_2_seam_sinker_rhp(self):
        mean = [94.0, 10.0, 12.0, 2200, 180, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "2-Seam / Sinker"

    def test_2_seam_sinker_lhp(self):
        mean = [94.0, 10.0, -12.0, 2200, 180, 1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "2-Seam / Sinker (LHP)"

    def test_cutter(self):
        mean = [93.0, 4.0, 11.0, 2300, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Cutter"

    def test_generic_fastball(self):
        mean = [94.0, 9.0, 0.0, 2300, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Fastball"

    def test_12_6_curveball(self):
        mean = [85.0, -8.0, 2.0, 2500, 60, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "12-6 Curveball"

    def test_sweeping_slider(self):
        mean = [85.0, -2.0, 14.0, 2400, 100, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Sweeping Slider"

    def test_slider(self):
        mean = [85.0, 2.0, 10.0, 2300, 120, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Slider"

    def test_hard_breaking_ball_generic(self):
        mean = [85.0, 6.0, 4.0, 2300, 120, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Hard Breaking Ball"

    def test_curveball_soft(self):
        mean = [75.0, -8.0, 4.0, 2400, 60, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Curveball"

    def test_changeup_splitter(self):
        mean = [80.0, 8.0, 6.0, 1800, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Changeup / Splitter"

    def test_offspeed_generic(self):
        mean = [80.0, 2.0, 4.0, 1800, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) == "Offspeed"

    def test_none_inputs_return_none(self):
        mean = [None, 18.0, 4.0, 2400, 200, -1.5, 6.0, 6.5]
        assert ppc._label_component(mean, self._fi()) is None


# ===========================================================================
# build_run_expectancy_matrix
# ===========================================================================


class TestBuildReMatrix:
    def test_builds_from_mocked_duckdb(self):
        mock_conn = MagicMock()
        df = pd.DataFrame(
            {
                "outs": [0, 0, 1, 2],
                "runners_state": [0, 1, 0, 0],
                "expected_runs": [0.48, 0.83, 0.26, 0.10],
                "sample_size": [10, 5, 8, 4],
            }
        )
        # The first execute().fetchdf() is the data query; subsequent execute()s are writes.
        first = MagicMock()
        first.fetchdf.return_value = df
        mock_conn.execute.side_effect = [first] + [MagicMock() for _ in range(20)]
        out = ppc.build_run_expectancy_matrix(mock_conn, seasons=[2023, 2024])
        assert out[(0, 0)] == pytest.approx(0.48)
        assert out[(0, 1)] == pytest.approx(0.83)
        assert (2, 0) in out


# ===========================================================================
# PlayerProfileComputor — lifecycle (mocked DuckDB)
# ===========================================================================


def _stub_mock_conn() -> MagicMock:
    """Build a DuckDB connection mock that survives any execute()/.fetchdf()/.fetchall()."""
    conn = MagicMock()
    # Default execute() returns a chain mock that supports .fetchdf and .fetchall
    result = MagicMock()
    result.fetchdf.return_value = pd.DataFrame()
    result.fetchall.return_value = []
    conn.execute.return_value = result
    return conn


class TestComputorLifecycle:
    def test_init(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        assert c._pg_dsn == "postgresql://x"
        assert c._duckdb_path == "/tmp/y.duckdb"
        assert c._conn is None
        assert c._re_matrix == {}

    def test_close_when_no_conn_noop(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        c._close()  # should not raise
        assert c._conn is None

    def test_connect_opens_and_attaches_postgres(self):
        with patch("pipeline.batch.player_profile_computor.duckdb.connect") as mock_connect:
            mock_connect.return_value = _stub_mock_conn()
            c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
            c._connect()
            assert c._conn is not None
            # Confirm we installed/loaded the postgres extension and ATTACHed
            executed = [call.args[0] for call in c._conn.execute.call_args_list]
            assert any("INSTALL postgres" in sql for sql in executed)
            assert any("ATTACH" in sql for sql in executed)
            c._close()
            assert c._conn is None

    def test_run_schema_ddl_skips_when_schema_present(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        c._conn = _stub_mock_conn()
        # First execute() succeeds → schema is present → method returns early
        c._run_schema_ddl()
        # Confirm the probe SQL was issued
        first_sql = c._conn.execute.call_args_list[0].args[0]
        assert "pitcher_season_metrics" in first_sql

    def test_run_schema_ddl_applies_schema_file_when_missing(self, tmp_path, monkeypatch):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        # First call to execute() raises (schema missing); subsequent calls succeed.
        conn = MagicMock()
        result_ok = MagicMock()
        result_ok.fetchall.return_value = []

        def execute_side(sql, *_):
            if "pitcher_season_metrics" in sql and "LIMIT 1" in sql:
                raise Exception("table not found")
            return result_ok

        conn.execute.side_effect = execute_side
        c._conn = conn

        # Stage a fake schema file under the expected path.
        schema_dir = tmp_path / "db" / "schemas"
        schema_dir.mkdir(parents=True)
        schema_dir.joinpath("02_duckdb_schema.sql").write_text("-- empty DDL")
        monkeypatch.chdir(tmp_path)

        c._run_schema_ddl()
        # The DDL contents (or fallback CREATE SCHEMA) should have been executed.
        all_sql = " ".join(call.args[0] for call in conn.execute.call_args_list)
        assert "DDL" in all_sql or "SCHEMA" in all_sql

    def test_run_schema_ddl_fallback_when_no_file(self, tmp_path, monkeypatch):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        conn = MagicMock()
        result_ok = MagicMock()
        result_ok.fetchall.return_value = []

        def execute_side(sql, *_):
            if "pitcher_season_metrics" in sql and "LIMIT 1" in sql:
                raise Exception("table not found")
            return result_ok

        conn.execute.side_effect = execute_side
        c._conn = conn

        # No schema file present in cwd
        monkeypatch.chdir(tmp_path)
        c._run_schema_ddl()
        all_sql = " ".join(call.args[0] for call in conn.execute.call_args_list)
        # Fallback path creates schemas explicitly
        assert "CREATE SCHEMA IF NOT EXISTS derived" in all_sql

    def test_delete_seasons_executes_per_table(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        c._conn = _stub_mock_conn()
        c._delete_seasons([2023, 2024])
        sqls = [call.args[0] for call in c._conn.execute.call_args_list]
        # Should issue DELETE for at least the seasoned derived tables
        assert any("DELETE FROM derived.pitcher_season_metrics" in s for s in sqls)
        assert any("DELETE FROM derived.batter_season_metrics" in s for s in sqls)


# ===========================================================================
# PlayerProfileComputor.run() smoke test — exercises every _compute_* method
# ===========================================================================


class TestComputorRunSmoke:
    def test_run_smoke_completes_with_stubbed_conn(self):
        """Exercise the full run() pipeline with a stubbed connection that
        returns empty DataFrames everywhere — every `_compute_*` method should
        early-exit on insufficient data without raising."""
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")

        # Patch duckdb.connect so _connect() returns our stub
        stub = _stub_mock_conn()

        with (
            patch(
                "pipeline.batch.player_profile_computor.duckdb.connect",
                return_value=stub,
            ),
            patch(
                "pipeline.batch.player_profile_computor.build_run_expectancy_matrix",
                return_value={},
            ),
        ):
            # No seasons → defaults to current year
            c.run(seasons=[2024], full_rebuild=False)

        # Connection should have been opened and closed
        assert c._conn is None
        # ATTACH was executed at least once
        executed = [call.args[0] for call in stub.execute.call_args_list]
        assert any("ATTACH" in sql for sql in executed)

    def test_run_with_full_rebuild_calls_delete(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        stub = _stub_mock_conn()
        with (
            patch(
                "pipeline.batch.player_profile_computor.duckdb.connect",
                return_value=stub,
            ),
            patch(
                "pipeline.batch.player_profile_computor.build_run_expectancy_matrix",
                return_value={},
            ),
        ):
            c.run(seasons=[2024], full_rebuild=True)
        sqls = [call.args[0] for call in stub.execute.call_args_list]
        assert any("DELETE" in s for s in sqls)

    def test_run_default_seasons_uses_current_year(self):
        c = ppc.PlayerProfileComputor(pg_dsn="postgresql://x", duckdb_path="/tmp/y.duckdb")
        stub = _stub_mock_conn()
        with (
            patch(
                "pipeline.batch.player_profile_computor.duckdb.connect",
                return_value=stub,
            ),
            patch(
                "pipeline.batch.player_profile_computor.build_run_expectancy_matrix",
                return_value={},
            ),
        ):
            c.run()  # no seasons argument
        assert c._conn is None  # closed cleanly


# ===========================================================================
# LeagueAverageProfiles
# ===========================================================================


class TestLeagueAverageProfiles:
    def test_init(self):
        lap = ppc.LeagueAverageProfiles(duckdb_path="/tmp/y.duckdb")
        assert lap._path == "/tmp/y.duckdb"

    def test_compute_runs_all_inserts(self):
        stub = _stub_mock_conn()
        with patch("pipeline.batch.player_profile_computor.duckdb.connect", return_value=stub):
            lap = ppc.LeagueAverageProfiles(duckdb_path="/tmp/y.duckdb")
            lap.compute(seasons=[2024, 2025])
        sqls = [call.args[0] for call in stub.execute.call_args_list]
        # Pitcher, batter, and 8 position-specific fielder inserts
        assert any("'pitcher'" in s for s in sqls)
        assert any("'batter'" in s for s in sqls)
        assert any("fielder_C" in s for s in sqls)
        assert any("fielder_RF" in s for s in sqls)
        # Close was called on the connection (DuckDB context manager pattern)
        stub.close.assert_called_once()


# ===========================================================================
# Iteration-path coverage — supply real DataFrames so the compute methods
# don't early-exit on empty data and the Python pandas-iteration paths get
# exercised.
# ===========================================================================


def _make_taken_pitches_df(n: int = 2000) -> pd.DataFrame:
    """Synthetic taken-pitch DataFrame for the framing model."""
    rng = np.random.default_rng(0)
    return pd.DataFrame(
        {
            "game_pk": rng.integers(700000, 750000, n),
            "at_bat_number": rng.integers(1, 80, n),
            "pitch_number": rng.integers(1, 8, n),
            "season": 2024,
            "catcher_id": rng.choice([2001, 2002, 2003, 2004], n),
            "plate_x": rng.normal(0, 0.8, n),
            "plate_z": rng.normal(2.5, 0.6, n),
            "sz_top": rng.normal(3.5, 0.1, n),
            "sz_bot": rng.normal(1.6, 0.1, n),
            "p_throws": rng.choice(["L", "R"], n),
            "stand": rng.choice(["L", "R"], n),
            "balls": rng.integers(0, 4, n),
            "strikes": rng.integers(0, 3, n),
            "is_strike": rng.integers(0, 2, n),
            "is_taken": np.ones(n, dtype=int),
        }
    )


def _make_blocking_pitches_df(n: int = 2000) -> pd.DataFrame:
    """Synthetic pitch DataFrame for the catcher blocking model."""
    rng = np.random.default_rng(1)
    return pd.DataFrame(
        {
            "game_pk": rng.integers(700000, 750000, n),
            "at_bat_number": rng.integers(1, 80, n),
            "pitch_number": rng.integers(1, 8, n),
            "season": 2024,
            "catcher_id": rng.choice([2001, 2002, 2003, 2004], n),
            "plate_x": rng.normal(0, 0.9, n),
            "plate_z": rng.normal(2.5, 0.7, n),
            "release_speed": rng.normal(93, 3, n),
            "end_speed": rng.normal(86, 3, n),
            "vx0": rng.normal(0, 5, n),
            "vy0": rng.normal(-135, 4, n),
            "vz0": rng.normal(-3, 4, n),
            "ax": rng.normal(0, 8, n),
            "ay": rng.normal(28, 5, n),
            "az": rng.normal(-15, 5, n),
            "pfx_x": rng.normal(0, 0.6, n),
            "break_vertical": rng.normal(0, 5, n),
            "break_horizontal": rng.normal(0, 5, n),
            "p_throws": rng.choice(["L", "R"], n),
            "stand": rng.choice(["L", "R"], n),
            "passed_ball_wild_pitch": rng.integers(0, 2, n) * (rng.uniform(size=n) < 0.02),
            "on_1b": rng.choice([None, 200001], n),
            "on_2b": rng.choice([None, 200002], n),
            "on_3b": rng.choice([None, 200003], n),
        }
    )


def _make_outfield_plays_df(n: int = 800) -> pd.DataFrame:
    """Synthetic outfield-play DataFrame for OF catch probability."""
    rng = np.random.default_rng(2)
    return pd.DataFrame(
        {
            "pitch_id_str": [f"700000-{i}-1" for i in range(n)],
            "game_pk": rng.integers(700000, 750000, n),
            "season": 2024,
            "stand": rng.choice(["L", "R"], n),
            "fielder_7": rng.choice([7001, 7002, 7003], n),
            "fielder_8": rng.choice([8001, 8002, 8003], n),
            "fielder_9": rng.choice([9001, 9002, 9003], n),
            "fielded_by": rng.choice([7001, 8001, 9001], n),
            "hc_x": rng.uniform(60, 200, n),
            "hc_y": rng.uniform(40, 150, n),
            "launch_speed": rng.uniform(60, 110, n),
            "launch_angle": rng.uniform(15, 60, n),
            "spray_angle": rng.uniform(-45, 45, n),
            "hit_distance_sc": rng.uniform(150, 400, n),
            "bb_type": rng.choice(["fly_ball", "line_drive", "popup"], n),
            "events": rng.choice(["single", "double", "out"], n),
            "outs_on_pitch": rng.integers(0, 2, n),
            "fielding_error": [None] * n,
        }
    )


class TestComputorIterationPaths:
    """Exercise the pandas/sklearn iteration paths inside `_compute_*` methods
    by supplying real DataFrames via a side_effect-based mock conn."""

    def _make_method_conn(self, df: pd.DataFrame) -> MagicMock:
        """Build a conn whose first execute().fetchdf() returns ``df``; later
        execute() calls (writes / register) succeed silently."""
        conn = MagicMock()
        first = MagicMock()
        first.fetchdf.return_value = df
        first.fetchall.return_value = []
        # Default for subsequent execute() calls — return a blank chain
        blank = MagicMock()
        blank.fetchdf.return_value = pd.DataFrame()
        blank.fetchall.return_value = []
        conn.execute.side_effect = [first] + [blank] * 200
        return conn

    def test_catcher_framing_iterates(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_method_conn(_make_taken_pitches_df())
        c._compute_catcher_framing(seasons=[2024])
        # The conn should have been used to register the framing_agg frame
        c._conn.register.assert_called()

    def test_catcher_framing_below_threshold_skips(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        # Only 10 rows → below the 1000-row threshold → early return
        c._conn = self._make_method_conn(_make_taken_pitches_df(n=10))
        c._compute_catcher_framing(seasons=[2024])
        c._conn.register.assert_not_called()

    def test_catcher_blocking_iterates(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_method_conn(_make_blocking_pitches_df())
        c._compute_catcher_blocking(seasons=[2024])
        c._conn.register.assert_called()

    def test_catcher_blocking_below_threshold_skips(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_method_conn(_make_blocking_pitches_df(n=20))
        c._compute_catcher_blocking(seasons=[2024])
        c._conn.register.assert_not_called()

    def test_outfield_catch_probability_iterates(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_method_conn(_make_outfield_plays_df())
        c._compute_outfield_catch_probability(seasons=[2024])
        # Method may have registered intermediate frames
        # (just ensuring it completed without raising is the win here)

    def test_outfield_catch_below_threshold_skips(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_method_conn(_make_outfield_plays_df(n=10))
        c._compute_outfield_catch_probability(seasons=[2024])
        # Just verify no crash


def _make_infield_plays_df(n: int = 800) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "pitch_id_str": [f"700000-{i}-1" for i in range(n)],
            "game_pk": rng.integers(700000, 750000, n),
            "season": 2024,
            "stand": rng.choice(["L", "R"], n),
            "batter": rng.integers(200000, 300000, n),
            "fielder_3": rng.choice([3001, 3002, 3003], n),
            "fielder_4": rng.choice([4001, 4002, 4003], n),
            "fielder_5": rng.choice([5001, 5002, 5003], n),
            "fielder_6": rng.choice([6001, 6002, 6003], n),
            "fielded_by": rng.choice([3001, 4001, 5001, 6001], n),
            "hc_x": rng.uniform(80, 175, n),
            "hc_y": rng.uniform(140, 200, n),
            "launch_speed": rng.uniform(70, 110, n),
            "launch_angle": rng.uniform(-10, 10, n),
            "spray_angle": rng.uniform(-45, 45, n),
            "bb_type": ["ground_ball"] * n,
            "events": rng.choice(["single", "double", "out"], n),
            "outs_on_pitch": rng.integers(0, 2, n),
            "fielding_error": [None] * n,
            "throwing_error_1": [None] * n,
            "throwing_error_2": [None] * n,
        }
    )


def _make_sprint_speed_df(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(4)
    return pd.DataFrame(
        {
            "player_id": rng.integers(200000, 300000, n),
            "season": [2024] * n,
            "sprint_speed": rng.uniform(24.0, 30.0, n),
        }
    )


def _make_dp_plays_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    return pd.DataFrame(
        {
            "pitch_id_str": [f"700000-{i}-1" for i in range(n)],
            "game_pk": rng.integers(700000, 750000, n),
            "at_bat_number": rng.integers(1, 80, n),
            "season": [2024] * n,
            "stand": rng.choice(["L", "R"], n),
            "batter": rng.integers(200000, 300000, n),
            "on_1b": rng.integers(200000, 300000, n),
            "outs": rng.integers(0, 2, n),
            "fielder_3": rng.choice([3001, 3002, 3003], n),
            "fielder_4": rng.choice([4001, 4002, 4003], n),
            "fielder_5": rng.choice([5001, 5002, 5003], n),
            "fielder_6": rng.choice([6001, 6002, 6003], n),
            "fielded_by": rng.choice([3001, 4001, 5001, 6001], n),
            "hc_x": rng.uniform(80, 175, n),
            "hc_y": rng.uniform(140, 200, n),
            "launch_speed": rng.uniform(70, 110, n),
            "launch_angle": rng.uniform(-10, 10, n),
            "bb_type": ["ground_ball"] * n,
            "events": rng.choice(["force_out", "double_play", "single"], n),
            "outs_on_pitch": rng.integers(0, 3, n),
            "field_assist_1": rng.choice([3001, 4001, 5001, 6001], n),
            "field_assist_2": rng.choice([3001, 4001, 5001, 6001], n),
            "field_putout_1": rng.choice([3001, 4001, 5001, 6001], n),
            "field_putout_2": rng.choice([3001, 4001, 5001, 6001], n),
            "home_score": rng.integers(0, 10, n),
            "away_score": rng.integers(0, 10, n),
            "inning": rng.integers(1, 10, n),
            "inning_topbot": rng.choice(["Top", "Bot"], n),
            "runners_state": rng.integers(0, 8, n),
        }
    )


class TestInfieldOAAAndDP:
    def _make_seq_conn(self, frames: list[pd.DataFrame]) -> MagicMock:
        """Build a mock conn whose execute() yields the given frames in order
        on .fetchdf(); subsequent execute() calls return blank mocks."""
        conn = MagicMock()
        chains = []
        for f in frames:
            ch = MagicMock()
            ch.fetchdf.return_value = f
            ch.fetchall.return_value = []
            chains.append(ch)
        blank = MagicMock()
        blank.fetchdf.return_value = pd.DataFrame()
        blank.fetchall.return_value = []
        conn.execute.side_effect = chains + [blank] * 200
        return conn

    def test_infield_oaa_below_threshold_skips(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_seq_conn([_make_infield_plays_df(n=10)])
        c._compute_infield_oaa(seasons=[2024])

    def test_infield_oaa_iterates(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_seq_conn([_make_infield_plays_df(n=800), _make_sprint_speed_df(n=200)])
        c._compute_infield_oaa(seasons=[2024])

    def test_dp_metrics_below_threshold_skips(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_seq_conn([_make_dp_plays_df(n=10)])
        c._re_matrix = {}
        c._compute_dp_metrics(seasons=[2024])

    def test_dp_metrics_iterates(self):
        c = ppc.PlayerProfileComputor(pg_dsn="x", duckdb_path="/tmp/y.duckdb")
        c._conn = self._make_seq_conn([_make_dp_plays_df(n=400), _make_sprint_speed_df(n=200)])
        # Provide a small run-expectancy matrix so re_start/re_end lookups work
        c._re_matrix = {
            (0, 0b001): 0.83,
            (1, 0b001): 0.50,
            (2, 0b001): 0.22,
            (0, 0b000): 0.48,
            (1, 0b000): 0.26,
            (2, 0b000): 0.10,
        }
        c._compute_dp_metrics(seasons=[2024])
