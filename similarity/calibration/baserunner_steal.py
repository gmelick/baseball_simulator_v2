"""The stolen-base engine's calibration: the tendency, success and lead sigmas."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from similarity.calibration.base import CalibratorBase
from similarity.calibration.report import CalibrationReport

log = logging.getLogger("similarity_calibration")


class BaserunnerStealCalibratorMixin(CalibratorBase):
    """The stolen-base engine's part of ``SimilarityCalibrator``."""

    def _calibrate_baserunner_steal_params(
        self,
        conn: Any,
        seasons: list[int],
        target: float,
        report: CalibrationReport,
    ) -> CalibrationReport:
        """Fit the stolen-base engine's tendency, success and lead sigmas.

        The lead sigma fits over the rows that carry BOTH lead features (the
        running-game data, SIM-531); with none measured it stays 0.0 and the
        engine keeps its module bandwidth.
        """
        sl = ", ".join(str(s) for s in seasons)
        lead_cols = (
            "lead_primary_ft, lead_jump_ft"
            if self._has_columns(
                conn, "baserunner_steal_metrics", ("lead_primary_ft", "lead_jump_ft")
            )
            else "NULL AS lead_primary_ft, NULL AS lead_jump_ft"
        )
        try:
            rows = conn.execute(f"""
                SELECT steal_attempt_rate, steal_attempt_rate_2b,
                       steal_success_rate, steal_success_rate_2b,
                       {lead_cols}
                FROM derived.baserunner_steal_metrics
                WHERE NOT below_minimum_sample AND season IN ({sl})
            """).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Baserunner-steal calibration skipped (%s: %s).", type(exc).__name__, exc)
            return report
        if not rows:
            log.warning("No baserunner-steal profiles found for calibration.")
            return report

        tend = self._zscore_matrix(np.array([[(r[0] or 0.0), (r[1] or 0.0)] for r in rows]))
        succ = self._zscore_matrix(np.array([[(r[2] or 0.0), (r[3] or 0.0)] for r in rows]))
        report.sigma_baserunner_steal_tendency = self._fit_sigma(tend, target)
        report.sigma_baserunner_steal_success = self._fit_sigma(succ, target)
        lead_rows = np.array(
            [[float(r[4]), float(r[5])] for r in rows if r[4] is not None and r[5] is not None],
            dtype=np.float64,
        ).reshape(-1, 2)
        report.sigma_baserunner_steal_lead = (
            self._fit_sigma(self._zscore_matrix(lead_rows), target) if len(lead_rows) else 0.0
        )
        log.info(
            "Baserunner-steal calibration complete: %d profiles, sigma_tendency=%.3f, "
            "sigma_success=%.3f, sigma_lead=%.3f (%d rows with a lead)",
            len(rows),
            report.sigma_baserunner_steal_tendency,
            report.sigma_baserunner_steal_success,
            report.sigma_baserunner_steal_lead,
            len(lead_rows),
        )
        return report
