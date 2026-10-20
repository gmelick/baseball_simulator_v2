"""
test_calibration_reconciliation.py
=========================================
The similarity calibrator and the ``validate_props`` script against the live
DuckDB / Postgres schema. The fit and validate jobs must run on that schema, so
the app can apply a real (non-identity) calibration.

Each guard has a test here:

  1. **Pitcher calibrator import.** The pitcher engine has only arsenal and
     command sub-scores. It exports ``COMMAND_FEATURES`` and NOT
     ``RESULT_FEATURES``. The calibrator fits ``sigma_command`` over the real
     command features and leaves ``sigma_results`` at the 0.0 keep-default
     sentinel.

  2. **Degenerate-sigma safety.** Raw ``calibrate_sigma`` returns a degenerate
     ``1.0`` when a sub-score has no usable spread (e.g. sprint_speed is
     unpopulated on the live DB). ``apply_calibration`` (``v if v > 0 else
     current``) applies any positive value as a real override, so a 1.0 would
     silently overwrite a tuned default (baserunner ``RBF_SIGMA_SPEED=0.8171``).
     The fielder, baserunner and manager calibrators therefore route through
     ``_fit_sigma``, which yields the 0.0 keep-default sentinel for ANY matrix
     it cannot calibrate.

  3. **validate_props and raw.games.** The live Postgres stores the final
     score in ``home_score_final`` / ``away_score_final``. ``_fetch_final_games``
     selects those columns under the names ``home_score`` / ``away_score``.

All calibration-module tests use synthetic data + a tiny fake DuckDB connection, so
no live DuckDB / Postgres is required.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.script_loader import load_script

from similarity.calibration.fitting import calibrate_sigma
from similarity.calibration.report import CalibrationReport
from similarity.similarity_calibration import SimilarityCalibrator


# ---------------------------------------------------------------------------
# Tiny fake DuckDB connection: dispatches information_schema vs data queries.
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    """Mimics the ``conn.execute(sql[, params]).fetchall()`` surface the
    calibrator uses. Returns ``present_cols`` for any information_schema probe and
    ``data_rows`` for the actual SELECT."""

    def __init__(self, present_cols: list[str], data_rows: list[tuple]) -> None:
        self._present = present_cols
        self._data = data_rows
        self.queries: list[str] = []

    def execute(self, sql: str, *args: object) -> _FakeResult:
        self.queries.append(sql)
        if "information_schema" in sql:
            return _FakeResult([(c,) for c in self._present])
        return _FakeResult(self._data)


# ===========================================================================
# 1. calibrate_sigma degenerate_value
# ===========================================================================
class TestCalibrateSigmaDegenerateValue:
    def test_default_degenerate_value_is_one(self):
        # A fully-constant matrix has no pairwise spread -> the documented 1.0.
        const = np.ones((50, 3), dtype=np.float64)
        assert calibrate_sigma(const) == 1.0

    def test_zero_sentinel_returned_on_degenerate(self):
        const = np.ones((50, 3), dtype=np.float64)
        assert calibrate_sigma(const, degenerate_value=0.0) == 0.0

    def test_varied_matrix_still_calibrates(self):
        rng = np.random.default_rng(432)
        varied = rng.normal(size=(200, 4))
        sig = calibrate_sigma(varied, degenerate_value=0.0)
        assert sig > 0.0  # real spread -> real sigma, sentinel never used


# ===========================================================================
# 2. _fit_sigma keep-default sentinel
# ===========================================================================
class TestFitSigmaSentinel:
    def test_empty_matrix_returns_zero(self):
        assert SimilarityCalibrator._fit_sigma(np.empty((0, 3)), 0.5) == 0.0

    def test_fully_constant_returns_zero(self):
        assert SimilarityCalibrator._fit_sigma(np.zeros((40, 2)), 0.5) == 0.0

    def test_mostly_constant_returns_zero(self):
        # >half of the values identical -> median pair distance 0 -> sentinel.
        col = np.concatenate([np.zeros(80), np.ones(20)]).reshape(-1, 1)
        assert SimilarityCalibrator._fit_sigma(col, 0.5) == 0.0

    def test_varied_returns_positive_sigma(self):
        rng = np.random.default_rng(7)
        z = rng.normal(size=(150, 3))
        assert SimilarityCalibrator._fit_sigma(z, 0.5) > 0.0


# ===========================================================================
# 3. Pitcher calibrator: no RESULT_FEATURES, sigma_results stays keep-default
# ===========================================================================
class TestPitcherCalibratorReconciliation:
    def test_engine_dropped_result_features(self):
        import similarity.engines.pitcher_similarity as ps

        assert hasattr(ps, "COMMAND_FEATURES")
        assert not hasattr(ps, "RESULT_FEATURES"), (
            "pitcher engine exports RESULT_FEATURES again — the SIM-432 pitcher "
            "calibrator import will break; re-reconcile _calibrate_pitcher_params."
        )

    def test_fits_command_only(self):
        from similarity.engines.pitcher_similarity import COMMAND_FEATURES

        rng = np.random.default_rng(11)
        n_cmd = len(COMMAND_FEATURES)
        rows = [(1000 + i, 2024, 500 + i, *rng.normal(size=n_cmd).tolist()) for i in range(120)]
        conn = _FakeConn(present_cols=list(COMMAND_FEATURES), data_rows=rows)

        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_pitcher_params(conn, [2024], 0.5, CalibrationReport())

        assert report.sigma_command > 0.0
        assert report.sigma_results == 0.0  # vestigial: the engine has no result sub-score
        assert report.eb_n_prior_pitcher > 0.0
        # The information_schema guard was consulted.
        assert any("information_schema" in q for q in conn.queries)

    def test_absent_command_column_is_guarded(self):
        # Drop one command column from the live schema; its value comes back NULL.
        # The fit must still succeed off the remaining varied columns.
        from similarity.engines.pitcher_similarity import COMMAND_FEATURES

        rng = np.random.default_rng(13)
        n_cmd = len(COMMAND_FEATURES)
        rows = []
        for i in range(120):
            vals = rng.normal(size=n_cmd).tolist()
            vals[-1] = None  # the absent column -> NULL placeholder in real SQL
            rows.append((2000 + i, 2024, 600 + i, *vals))
        present = list(COMMAND_FEATURES)[:-1]  # last column absent
        conn = _FakeConn(present_cols=present, data_rows=rows)

        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_pitcher_params(conn, [2024], 0.5, CalibrationReport())
        assert report.sigma_command > 0.0  # no crash, real fit from the rest


# ===========================================================================
# 4. Baserunner calibrator keeps the tuned default on a degenerate sub-score
# ===========================================================================
class TestBaserunnerKeepsDefaultOnDegenerate:
    def test_constant_speed_yields_keep_default_sentinel(self):
        # sprint_speed constant (unpopulated on live DB); aggression/success varied.
        rng = np.random.default_rng(17)
        rows = []
        for i in range(80):
            speed = 27.0  # constant -> degenerate
            agg = rng.normal(size=6).tolist()
            suc = rng.normal(size=5).tolist()
            # The SELECT carries a seventh aggression column
            # (xb_attempt_rate_above_expected) — NULL on a database without the
            # 0029 column, which is what an empty information_schema probe means.
            rows.append((3000 + i, 2024, 100 + i, speed, *agg, None, *suc))
        conn = _FakeConn(present_cols=[], data_rows=rows)  # no 0029 column on this DB

        cal = SimilarityCalibrator(duckdb_path=":memory:")
        report = cal._calibrate_baserunner_params(conn, [2024], 0.5, CalibrationReport())

        assert report.sigma_baserunner_speed == 0.0  # keep-default, NOT a spurious 1.0
        assert report.sigma_baserunner_aggression > 0.0
        assert report.sigma_baserunner_success > 0.0

    def test_keep_default_field_preserves_engine_sigma_on_apply(self):
        # End-to-end: a 0.0 speed sigma must leave RBF_SIGMA_SPEED untouched.
        from similarity.engines.baserunner_similarity import (
            RBF_SIGMA_SPEED,
            BaserunnerSimilarityEngine,
        )

        eng = BaserunnerSimilarityEngine(duckdb_path=":memory:")
        eng.apply_calibration(CalibrationReport(sigma_baserunner_speed=0.0))
        assert eng._speed_rbf.sigma == RBF_SIGMA_SPEED


# ===========================================================================
# 5. validate_props ↔ raw.games column reconciliation
# ===========================================================================
def _load_validate_props():
    """``scripts/validate_props.py`` as a fresh module."""
    return load_script("validate_props", module_name="calibration_reconciliation_validate_props")


class TestValidatePropsFinalGamesQuery:
    def test_selects_final_score_columns(self, monkeypatch):
        import asyncio

        asyncpg = pytest.importorskip("asyncpg")
        vp = _load_validate_props()

        captured: dict[str, str] = {}

        class _FakePgConn:
            async def fetch(self, query: str, *args: object) -> list[dict]:
                captured["query"] = query
                return [{"game_pk": 1, "home_score": 5, "away_score": 3}]

            async def close(self) -> None:
                pass

        async def _fake_connect(dsn: str):
            return _FakePgConn()

        monkeypatch.setattr(asyncpg, "connect", _fake_connect)

        rows = asyncio.run(vp._fetch_final_games("dsn://x", [2024], None))

        assert rows == [{"game_pk": 1, "home_score": 5, "away_score": 3}]
        q = captured["query"]
        # The live schema exposes the final score as *_final; the query must alias
        # them back to the names the caller consumes and filter on the real columns.
        assert "home_score_final AS home_score" in q
        assert "away_score_final AS away_score" in q
        assert "home_score_final IS NOT NULL" in q
        assert "away_score_final IS NOT NULL" in q
