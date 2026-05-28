"""
test_ml_engines_sim361.py
=========================
SIM-361 (Phase 5, Sprint 3) — CalibrationReport JSON persistence, the
win-probability CalibrationMap-from-report factory, and build_all_engines.

Covers the three deliverables of the ticket, all without a live DuckDB:

  1. **CalibrationReport.to_json/from_json round-trip is lossless.** A
     representative report (scalars + numpy reliability-weight arrays + sub-score
     arrays + a fitted reliability curve + metadata) survives a JSON round-trip
     equal to the original (via ``report.equals(...)``, which handles the NDArray
     fields ``@dataclass.__eq__`` cannot).

  2. **CalibrationMap.from_report.** A report carrying a fitted reliability curve
     yields a monotone ``CalibrationMap`` whose ``apply(p)`` differs from
     identity at a test point; a report with no curve yields the
     ``IDENTITY_CALIBRATION`` singleton. Also exercises the api.state
     ``load_calibration_report`` / ``load_calibration_map`` loaders against a
     written JSON file and a missing path.

  3. **build_all_engines.** With the engine loader monkeypatched to fakes (no
     DuckDB, no faiss), it returns all 11 keyed entries; a fake engine that
     raises in ``build()`` (or in construction) is logged + skipped without
     failing the others.

These tests use only the public persistence/factory/builder API plus injectable
seams (``registry`` / ``loader``) — no live DuckDB, Postgres, or Redis.
"""

from __future__ import annotations

import numpy as np

from api.state import (
    ENGINE_REGISTRY,
    build_all_engines,
    load_calibration_map,
    load_calibration_report,
)
from similarity.similarity_calibration import CalibrationReport
from simulation.win_probability import IDENTITY_CALIBRATION, CalibrationMap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _representative_report(*, with_curve: bool = True) -> CalibrationReport:
    """A CalibrationReport populated across all field kinds: scalar floats,
    numpy reliability-weight arrays, sub-score arrays, metadata, and (optionally)
    a fitted reliability curve."""
    return CalibrationReport(
        sigma_discipline=0.831,
        sigma_batted_ball=0.742,
        sigma_command=0.910,
        sigma_results=0.655,
        arsenal_gamma=0.173,
        arsenal_median_w2=4.10,
        eb_n_prior_batter=50.0,
        eb_n_prior_pitcher=120.0,
        eb_n_prior_baserunner=18.5,
        sigma_if_range=0.6,
        sigma_of_arm=0.45,
        sigma_baserunner_speed=0.77,
        bats_penalty_lr=0.781,
        reliability_weights_discipline=np.array([0.7, 0.62, 0.91, 0.4]),
        reliability_weights_batted_ball=np.array([0.5, 0.55, 0.6]),
        reliability_weights_baserunner_speed=np.array([0.88]),
        batter_subscores=np.array([0.25, 0.25, 0.25, 0.25]),
        pitcher_subscores=np.array([0.6, 0.4]),
        reliability_curve=(
            [[0.0, 0.05], [0.25, 0.30], [0.5, 0.5], [0.75, 0.70], [1.0, 0.95]]
            if with_curve
            else None
        ),
        n_profiles_used=1234,
        seasons_used=[2022, 2023, 2024],
        target_median_score=0.50,
        calibration_time_sec=12.3,
    )


class _FakeEngine:
    """A stand-in similarity engine: records the duckdb_path and a build() call.
    No DuckDB, no numpy, no faiss."""

    def __init__(self, duckdb_path: str) -> None:
        self.duckdb_path = duckdb_path
        self.built = False

    def build(self) -> None:
        self.built = True


class _ExplodingEngine:
    """A fake engine whose build() raises — exercises build_all_engines'
    per-engine resilience (it must be skipped, not propagate)."""

    def __init__(self, duckdb_path: str) -> None:
        self.duckdb_path = duckdb_path

    def build(self) -> None:
        raise RuntimeError("simulated bad profile table")


# ===========================================================================
# 1. CalibrationReport JSON persistence
# ===========================================================================


class TestCalibrationPersistence:
    def test_round_trip_is_lossless(self):
        r = _representative_report()
        r2 = CalibrationReport.from_json(r.to_json())
        assert r.equals(r2), "to_json -> from_json must reconstruct an equal report"

    def test_round_trip_via_dict(self):
        r = _representative_report()
        r2 = CalibrationReport.from_dict(r.to_dict())
        assert r.equals(r2)

    def test_ndarray_fields_restore_as_ndarray(self):
        r = _representative_report()
        r2 = CalibrationReport.from_json(r.to_json())
        # The reliability-weight + sub-score fields must come back as ndarrays,
        # not plain lists, so downstream engine math keeps working.
        assert isinstance(r2.reliability_weights_discipline, np.ndarray)
        assert isinstance(r2.batter_subscores, np.ndarray)
        np.testing.assert_array_equal(
            r2.reliability_weights_discipline, r.reliability_weights_discipline
        )
        np.testing.assert_array_equal(r2.batter_subscores, r.batter_subscores)

    def test_none_array_fields_stay_none(self):
        # An array field never populated must round-trip as None, not [].
        r = CalibrationReport(sigma_discipline=0.5)
        r2 = CalibrationReport.from_json(r.to_json())
        assert r2.reliability_weights_power is None
        assert r2.batter_subscores is None
        assert r.equals(r2)

    def test_metadata_preserved(self):
        r = _representative_report()
        r2 = CalibrationReport.from_json(r.to_json())
        assert r2.seasons_used == [2022, 2023, 2024]
        assert r2.n_profiles_used == 1234
        assert r2.target_median_score == 0.50

    def test_to_json_is_valid_json_string(self):
        import json

        r = _representative_report()
        parsed = json.loads(r.to_json())  # must not raise
        assert parsed["__schema_version__"] == CalibrationReport.SCHEMA_VERSION
        assert parsed["sigma_discipline"] == 0.831

    def test_unknown_keys_ignored_on_load(self):
        # Forward-compat: a payload with an extra/unknown key must not crash.
        d = _representative_report().to_dict()
        d["a_future_field"] = 99
        r2 = CalibrationReport.from_dict(d)
        assert r2.sigma_discipline == 0.831

    def test_numpy_scalars_are_serializable(self):
        # A numpy scalar slipped into a float field must not break json.dumps.
        r = CalibrationReport(sigma_discipline=np.float64(0.42))
        s = r.to_json()
        r2 = CalibrationReport.from_json(s)
        assert r2.sigma_discipline == 0.42


# ===========================================================================
# 2. CalibrationMap.from_report + api.state loaders
# ===========================================================================


class TestCalibrationMapFromReport:
    def test_curve_yields_monotone_non_identity_map(self):
        m = CalibrationMap.from_report(_representative_report(with_curve=True))
        assert m is not IDENTITY_CALIBRATION
        xs = [0.1, 0.3, 0.5, 0.7, 0.9]
        ys = [m.apply(x) for x in xs]
        # Monotone non-decreasing.
        assert all(ys[i] <= ys[i + 1] for i in range(len(ys) - 1))
        # Differs from identity at a test point (the curve shrinks 0.9 -> <0.9).
        assert m.apply(0.9) != 0.9
        # Always clamped to [0, 1].
        assert all(0.0 <= y <= 1.0 for y in ys)

    def test_no_curve_yields_identity_singleton(self):
        m = CalibrationMap.from_report(_representative_report(with_curve=False))
        assert m is IDENTITY_CALIBRATION
        assert m.apply(0.9) == 0.9

    def test_empty_curve_yields_identity(self):
        r = CalibrationReport(reliability_curve=[])
        assert CalibrationMap.from_report(r) is IDENTITY_CALIBRATION

    def test_single_point_curve_falls_back_to_identity(self):
        # Need >= 2 points to interpolate.
        r = CalibrationReport(reliability_curve=[[0.5, 0.5]])
        assert CalibrationMap.from_report(r) is IDENTITY_CALIBRATION

    def test_map_clamps_outside_anchor_range(self):
        r = CalibrationReport(reliability_curve=[[0.2, 0.3], [0.8, 0.9]])
        m = CalibrationMap.from_report(r)
        # Below first anchor -> first observed; above last -> last observed.
        assert m.apply(0.0) == 0.3
        assert m.apply(1.0) == 0.9

    def test_map_enforces_monotonicity_on_non_monotone_curve(self):
        # A raw curve that dips must be made monotone via the running max.
        r = CalibrationReport(reliability_curve=[[0.0, 0.1], [0.5, 0.05], [1.0, 0.9]])
        m = CalibrationMap.from_report(r)
        ys = [m.apply(x) for x in (0.0, 0.25, 0.5, 0.75, 1.0)]
        assert all(ys[i] <= ys[i + 1] for i in range(len(ys) - 1))

    def test_load_calibration_report_from_file(self, tmp_path):
        r = _representative_report()
        p = tmp_path / "calib.json"
        p.write_text(r.to_json(), encoding="utf-8")
        loaded = load_calibration_report(str(p))
        assert loaded is not None
        assert r.equals(loaded)

    def test_load_calibration_report_missing_path_returns_none(self, tmp_path):
        assert load_calibration_report(str(tmp_path / "nope.json")) is None

    def test_load_calibration_report_no_path_no_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("CALIBRATION_REPORT_PATH", raising=False)
        assert load_calibration_report() is None

    def test_load_calibration_report_bad_json_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not-valid-json{{", encoding="utf-8")
        # Best-effort: a corrupt report must degrade, not raise.
        assert load_calibration_report(str(p)) is None

    def test_load_calibration_map_from_file_with_curve(self, tmp_path):
        r = _representative_report(with_curve=True)
        p = tmp_path / "calib.json"
        p.write_text(r.to_json(), encoding="utf-8")
        m = load_calibration_map(str(p))
        assert m is not IDENTITY_CALIBRATION
        assert m.apply(0.9) != 0.9

    def test_load_calibration_map_missing_path_is_identity(self, tmp_path):
        m = load_calibration_map(str(tmp_path / "nope.json"))
        assert m is IDENTITY_CALIBRATION

    def test_load_calibration_map_env_var(self, tmp_path, monkeypatch):
        r = _representative_report(with_curve=True)
        p = tmp_path / "calib.json"
        p.write_text(r.to_json(), encoding="utf-8")
        monkeypatch.setenv("CALIBRATION_REPORT_PATH", str(p))
        m = load_calibration_map()  # no arg -> reads env
        assert m is not IDENTITY_CALIBRATION


# ===========================================================================
# 3. build_all_engines
# ===========================================================================


class TestBuildAllEngines:
    def test_builds_all_eleven_with_fake_loader(self):
        def fake_loader(module_path, class_name):
            return _FakeEngine

        engines = build_all_engines(duckdb_path="/fake/db.duckdb", loader=fake_loader)
        # All 11 registry entries built.
        assert set(engines.keys()) == set(ENGINE_REGISTRY.keys())
        assert len(engines) == 11
        # Every one got the duckdb_path and was build()-ed.
        for _name, eng in engines.items():
            assert eng.duckdb_path == "/fake/db.duckdb"
            assert eng.built is True

    def test_eleven_keys_are_the_expected_names(self):
        expected = {
            "pitcher",
            "batter",
            "fielder",
            "baserunner",
            "baserunner_steal",
            "catcher",
            "pitcher_steal",
            "manager",
            "situation",
            "pitch_pitch",
            "batted_ball",
        }
        assert set(ENGINE_REGISTRY.keys()) == expected

    def test_failing_engine_is_skipped_not_fatal(self):
        # One engine's loader returns an _ExplodingEngine; it must be skipped
        # while the other ten still build.
        def fake_loader(module_path, class_name):
            if class_name == "BatterSimilarityEngine":
                return _ExplodingEngine
            return _FakeEngine

        engines = build_all_engines(duckdb_path="/fake/db.duckdb", loader=fake_loader)
        assert "batter" not in engines  # the exploding one is skipped
        assert len(engines) == 10  # the other ten survived
        assert "pitcher" in engines

    def test_loader_import_error_is_skipped(self):
        # A loader that raises (e.g. ImportError for faiss) skips just that engine.
        def fake_loader(module_path, class_name):
            if class_name == "PitchPitchSimilarityEngine":
                raise ImportError("faiss not installed")
            return _FakeEngine

        engines = build_all_engines(duckdb_path="/x", loader=fake_loader)
        assert "pitch_pitch" not in engines
        assert len(engines) == 10

    def test_all_failing_returns_empty_dict_not_exception(self):
        def fake_loader(module_path, class_name):
            return _ExplodingEngine

        engines = build_all_engines(duckdb_path="/x", loader=fake_loader)
        assert engines == {}

    def test_custom_registry_is_respected(self):
        reg = {"only_one": ("some.module", "SomeEngine")}

        def fake_loader(module_path, class_name):
            return _FakeEngine

        engines = build_all_engines(registry=reg, loader=fake_loader)
        assert set(engines.keys()) == {"only_one"}

    def test_duckdb_path_env_precedence(self, monkeypatch):
        monkeypatch.setenv("BASEBALL_DUCKDB_PATH", "/from/env.duckdb")

        def fake_loader(module_path, class_name):
            return _FakeEngine

        # No explicit path -> env wins.
        engines = build_all_engines(loader=fake_loader)
        assert engines["pitcher"].duckdb_path == "/from/env.duckdb"
        # Explicit arg wins over env.
        engines2 = build_all_engines(duckdb_path="/explicit.duckdb", loader=fake_loader)
        assert engines2["pitcher"].duckdb_path == "/explicit.duckdb"
