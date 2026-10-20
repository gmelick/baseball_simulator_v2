"""The population calibration of the similarity engines' constants.

``SimilarityCalibrator.calibrate_from_population`` reads the profile tables
of the given seasons from DuckDB and fits, for each engine, the values its
``apply_calibration`` reads from the ``CalibrationReport``:

- each sub-score's kernel bandwidth (sigma), so the median random pair scores
  ``target_median_score`` (0.50);
- the pitcher's arsenal anchor, from the W₂ distances the caller passes;
- the empirical-Bayes prior strength of the batter, pitcher and baserunner;
- the reliability weights of the batter, fielder and baserunner features.

The fits live in ``similarity/calibration/``: ``fitting.py`` holds the
functions, ``report.py`` the report, and one mixin per engine reads that
engine's profiles. ``scripts/fit_calibration.py`` (``make calibrate``) runs
the fit and writes ``/data/calibration.json``.

    cal = SimilarityCalibrator(duckdb_path="/data/baseball_sim.duckdb")
    report = cal.calibrate_from_population(seasons=[2023, 2024, 2025])
    print(report.summary())
"""

from __future__ import annotations

import time

import numpy as np
from numpy.typing import NDArray

from similarity.calibration.base import CalibratorBase
from similarity.calibration.baserunner import BaserunnerCalibratorMixin
from similarity.calibration.baserunner_steal import BaserunnerStealCalibratorMixin
from similarity.calibration.batter import BatterCalibratorMixin
from similarity.calibration.catcher import CatcherCalibratorMixin
from similarity.calibration.fielder import FielderCalibratorMixin
from similarity.calibration.manager import ManagerCalibratorMixin
from similarity.calibration.pitcher import PitcherCalibratorMixin
from similarity.calibration.pitcher_steal import PitcherStealCalibratorMixin
from similarity.calibration.report import CalibrationReport


class SimilarityCalibrator(
    BatterCalibratorMixin,
    PitcherCalibratorMixin,
    FielderCalibratorMixin,
    BaserunnerCalibratorMixin,
    CatcherCalibratorMixin,
    BaserunnerStealCalibratorMixin,
    PitcherStealCalibratorMixin,
    ManagerCalibratorMixin,
    CalibratorBase,
):
    """Fit every engine's constants from the profile population of a DuckDB file.

    Each ``_calibrate_<engine>_params`` method comes from its mixin in
    ``similarity/calibration/``. The engines read the report through their own
    ``apply_calibration``.
    """

    def __init__(self, duckdb_path: str) -> None:
        self._duckdb_path = duckdb_path

    def calibrate_from_population(
        self,
        seasons: list[int],
        target_median_score: float = 0.50,
        arsenal_w2_distances: NDArray[np.float64] | None = None,
    ) -> CalibrationReport:
        """Fit every engine's constants from the profiles of ``seasons``.

        The fit needs no outcome data: each value comes from the spread of the
        profiles. A calibrator whose metrics table is absent (an older DuckDB
        file) leaves its fields at 0.0, so its engine keeps its module
        defaults, and the other engines still fit.

        Parameters
        ----------
        arsenal_w2_distances : 1-D array, optional
            Pairwise pitcher arsenal W₂ distances (e.g. from
            ``engine._arsenal_cache.finite_distances()``). With them,
            ``report.arsenal_gamma`` and ``report.arsenal_median_w2`` are fitted
            to ``target_median_score`` for
            ``PitcherSimilarityEngine.apply_calibration``. Without them the
            two fields stay 0.0 and the engine keeps its default scale.
        """
        import duckdb

        t0 = time.time()
        report = CalibrationReport(
            seasons_used=seasons,
            target_median_score=target_median_score,
        )

        conn = duckdb.connect(self._duckdb_path, read_only=True)
        try:
            report = self._calibrate_batter_params(conn, seasons, target_median_score, report)
            report = self._calibrate_pitcher_params(conn, seasons, target_median_score, report)
            report = self._calibrate_arsenal_params(
                arsenal_w2_distances, target_median_score, report
            )
            report = self._calibrate_fielder_params(conn, seasons, target_median_score, report)
            report = self._calibrate_baserunner_params(conn, seasons, target_median_score, report)
            report = self._calibrate_catcher_params(conn, seasons, target_median_score, report)
            report = self._calibrate_baserunner_steal_params(
                conn, seasons, target_median_score, report
            )
            report = self._calibrate_pitcher_steal_params(
                conn, seasons, target_median_score, report
            )
            report = self._calibrate_manager_params(conn, seasons, target_median_score, report)
        finally:
            conn.close()

        report.calibration_time_sec = time.time() - t0
        return report


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    cal = SimilarityCalibrator(duckdb_path="../db/schemas/baseball_simulator.duckdb")
    report = cal.calibrate_from_population(
        seasons=[2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
        target_median_score=0.5,
    )
    print(report.summary())
